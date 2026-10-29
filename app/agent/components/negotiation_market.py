"""Tarihli TCMB/EIA gözlemleri; Brent*kur yalnız TL petrol göstergesidir.

Canlı okuma kullanıcı tarafından açılmadıkça hiçbir ağ çağrısı yapılmaz.
EIA günlük HTML ve TCMB tarihli XML sabit resmi adreslerden okunur; anahtar
ve yeni paket gerekmez. Sağlanan gözlemler canlı doğrulama sayılmaz.
"""

import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from html.parser import HTMLParser
from urllib.parse import urlsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx
from lfx.custom import Component
from lfx.io import BoolInput, MessageTextInput, MultilineInput, Output
from lfx.schema.message import Message

MARKET_MARKER = "PAZARLIK PİYASA VERİSİ (JSON)\n"
CASE_MARKER = "TEKLİF DOSYASI (JSON)\n"
EIA_URL = "https://www.eia.gov/dnav/pet/hist/rbrted.htm"
MONTHS = {name: i for i, name in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
MAX_BODY = 1_500_000
MAX_LAG_DAYS = 7
LIVE_BUDGET_SECONDS = 25
PROXY_LABEL = "Brent × USD/TRY: TL/varil petrol göstergesi; Türkiye akaryakıt veya malzeme maliyeti değildir."


def _num(value):
    if isinstance(value, (bool, dict, list)) or value is None:
        return None
    try:
        text = str(value).strip()
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text) or len(text) > 50:
            return None
        number = Decimal(text)
        return number if number.is_finite() and Decimal("0") < number <= Decimal("1e12") else None
    except InvalidOperation:
        return None


def _date(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
    except ValueError:
        return None


def _instant(value):
    try:
        value = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        return value.astimezone(timezone.utc) if isinstance(value, datetime) and value.tzinfo is not None else None
    except ValueError:
        return None


def _decimal_text(value):
    return format(value, "f") if isinstance(value, Decimal) else value


def percent_change(baseline, current):
    """Saf Decimal yüzde; eksik/sıfır/negatif gözlemde sonuç üretmez."""
    before, after = _num(baseline), _num(current)
    if before is None or after is None:
        return None
    with localcontext() as context:
        context.prec = 40
        return format((after / before - 1) * 100, "f")


def tcmb_url(day):
    return "https://www.tcmb.gov.tr/kurlar/" + day.strftime("%Y%m/%d%m%Y.xml")


def official_source(series, value):
    """Aday URL sabit resmi veri yoluna uymalı; özel uçlara istek yapılmaz."""
    if not isinstance(value, str) or len(value) > 250:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if series == "brent":
        return parsed.netloc.lower() == "www.eia.gov" and parsed.path.lower() == "/dnav/pet/hist/rbrted.htm"
    return (series == "usdtry" and parsed.netloc.lower() == "www.tcmb.gov.tr"
            and bool(re.fullmatch(r"/kurlar/\d{6}/\d{8}\.xml", parsed.path)))


def _http_get(url):
    # URL is generated internally. No caller-supplied host or redirect.
    if not (official_source("brent", url) or official_source("usdtry", url)):
        raise ValueError("Yalnız sabit TCMB/EIA veri adresleri okunabilir.")
    # Respect the existing workstation network configuration; never print it.
    with httpx.Client(timeout=8, follow_redirects=False) as client:
        with client.stream("GET", url) as response:
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise ValueError("Resmi kaynak şu anda okunamadı.")
            pieces, size = [], 0
            for piece in response.iter_bytes():
                size += len(piece)
                if size > MAX_BODY:
                    raise ValueError("Resmi kaynak yanıtı boyut sınırını aşıyor.")
                pieces.append(piece)
    return b"".join(pieces).decode("utf-8-sig")


def parse_tcmb_xml(text, expected_date, retrieved_at):
    """USD döviz satış; banknot veya alış kuruyla karıştırılmaz."""
    if not isinstance(text, str) or len(text) > MAX_BODY or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("TCMB XML biçimi güvenli değil veya sınırı aşıyor.")
    try:
        root = ElementTree.fromstring(text)
        observed = datetime.strptime(root.attrib.get("Tarih", ""), "%d.%m.%Y").date()
    except (ValueError, ElementTree.ParseError) as error:
        raise ValueError("TCMB bülten tarihi ayrıştırılamadı.") from error
    if root.tag != "Tarih_Date" or observed != expected_date:
        raise ValueError("TCMB bülten tarihi istenen dosyayla eşleşmedi.")
    if observed < date(2005, 1, 1):
        raise ValueError("2005 öncesi TL birimi otomatik olarak güncel TRY ile birleştirilmez.")
    usd = [node for node in root.findall("Currency") if node.attrib.get("Kod") == "USD"]
    if len(usd) != 1 or _num(usd[0].findtext("Unit")) != Decimal("1"):
        raise ValueError("TCMB USD birimi tekil veya 1 USD değil.")
    value = _num(usd[0].findtext("ForexSelling"))
    if value is None:
        raise ValueError("TCMB USD döviz satış değeri bulunamadı.")
    return {"date": observed.isoformat(), "value": format(value, "f"), "unit": "TRY/USD",
            "source_url": tcmb_url(observed), "source_name": "TCMB USD döviz satış",
            "source_quote": f"Tarih={root.attrib['Tarih']}; Kod=USD; Unit=1; ForexSelling={value}",
            "retrieved_at": retrieved_at, "publication_date": observed.isoformat(), "origin": "official_download"}


class _EiaRows(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.cells, self.cell, self.capture = [], [], [], False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.cells = []
        elif tag in {"td", "th"}:
            self.cell, self.capture = [], True

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.capture:
            self.cells.append(" ".join("".join(self.cell).split()))
            self.capture = False
        elif tag == "tr" and self.cells:
            self.rows.append(self.cells)
            self.cells = []

    def handle_data(self, value):
        if self.capture:
            self.cell.append(value)


def parse_eia_html(text, retrieved_at):
    if not isinstance(text, str) or len(text) > MAX_BODY:
        raise ValueError("EIA yanıtı boyut sınırını aşıyor.")
    if "Europe Brent Spot Price FOB" not in text or "Dollars per Barrel" not in text:
        raise ValueError("EIA yanıtı günlük Brent USD/varil tablosu değil.")
    parser = _EiaRows()
    parser.feed(text)
    release = re.search(r"(?<!Next )Release Date:\s*(\d{1,2}/\d{1,2}/\d{4})", text)
    publication = datetime.strptime(release.group(1), "%m/%d/%Y").date().isoformat() if release else None
    if publication is None:
        raise ValueError("EIA yayın tarihi bulunamadı; bilgi kesimi doğrulanamadı.")
    observations, seen = [], set()
    for row in parser.rows:
        if len(row) != 6:
            continue
        match = re.fullmatch(r"(\d{4}) ([A-Z][a-z]{2})-\s*(\d{1,2}) to ([A-Z][a-z]{2})-\s*(\d{1,2})", row[0])
        if not match or match.group(2) not in MONTHS:
            continue
        monday = date(int(match.group(1)), MONTHS[match.group(2)], int(match.group(3)))
        if monday.weekday() != 0:
            raise ValueError("EIA hafta başlangıcı pazartesi değil.")
        for offset, cell in enumerate(row[1:]):
            value = _num(cell)
            if value is None:
                continue
            day = monday + timedelta(days=offset)
            if day in seen:
                raise ValueError("EIA günlük gözlem tarihi yinelenmiş.")
            seen.add(day)
            observations.append({"date": day.isoformat(), "value": format(value, "f"), "unit": "USD/barrel",
                                 "source_url": EIA_URL, "source_name": "EIA Europe Brent Spot Price FOB",
                                 "source_quote": " | ".join(row) + f"; weekday={offset + 1}; value={value}",
                                 "retrieved_at": retrieved_at, "publication_date": publication,
                                 "origin": "official_download"})
    if not observations:
        raise ValueError("EIA günlük Brent gözlemi bulunamadı.")
    return observations


def _validate_observation(series, item, cutoff, now):
    if not isinstance(item, dict):
        return None
    day, value = _date(item.get("date")), _num(item.get("value"))
    retrieved = _instant(item.get("retrieved_at"))
    quote = item.get("source_quote")
    unit = "TRY/USD" if series == "usdtry" else "USD/barrel"
    published = _date(item.get("publication_date"))
    if (day is None or value is None or day > cutoff or retrieved is None or retrieved > now
            or retrieved.astimezone(ZoneInfo("Europe/Istanbul")).date() > cutoff
            or not isinstance(quote, str) or not 1 <= len(quote) <= 3000
            or item.get("unit") != unit or not official_source(series, item.get("source_url"))
            or (published and published > cutoff)):
        return None
    if series == "usdtry" and (item["source_url"] != tcmb_url(day) or day < date(2005, 1, 1)):
        return None
    # A quoted value must occur as a complete numeric token; text remains untrusted.
    if not any(_num(token) == value for token in re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", quote)):
        return None
    origin = item.get("origin")
    if origin not in {"official_download", "manual_supplied", "synthetic"}:
        return None
    return {"date": day.isoformat(), "value": format(value, "f"), "unit": unit,
            "source_url": item["source_url"], "source_name": "TCMB USD döviz satış" if series == "usdtry" else "EIA Europe Brent Spot Price FOB",
            "source_quote": quote, "retrieved_at": retrieved.isoformat(), "origin": origin,
            "publication_date": published.isoformat() if published else None}


def select_observation(series, observations, target, cutoff, now):
    """En yakın geçmiş gözlem; ileri tarih ve 7 günü aşan taşıma kullanılmaz."""
    candidates = []
    for raw in observations:
        item = _validate_observation(series, raw, cutoff, now)
        if item and 0 <= (target - _date(item["date"])).days <= MAX_LAG_DAYS:
            candidates.append(item)
    if not candidates:
        return None
    latest = max(_date(item["date"]) for item in candidates)
    same = [item for item in candidates if _date(item["date"]) == latest]
    if len({_num(item["value"]) for item in same}) > 1:
        return None
    return {**same[0], "requested_date": target.isoformat(), "lag_days": (target - latest).days}


def _payload(value, marker=None, default=None):
    if isinstance(value, Message):
        value = value.text
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    elif isinstance(value, str):
        text = value.rsplit(marker, 1)[1] if marker and marker in value else value
    else:
        raise ValueError("Girdi JSON metni veya nesnesi olmalı.")
    if len(text) > 500_000:
        raise ValueError("Piyasa girdisi boyut sınırını aşıyor.")
    # Langflow text inputs may unescape \n inside a JSON string before handing
    # it to the component. These control characters remain source data.
    return json.loads(text, strict=False)


def market_context(case, *, baseline_date=None, observations=None, enable_live=False, now=None, fetcher=None):
    """Bir dosya için resmi serilerin tarihli çiftleri; geçmiş fiyat tahmini yok."""
    now = _instant(now) or datetime.now(timezone.utc)
    case = _payload(case, CASE_MARKER, {})
    if not isinstance(case, dict):
        raise ValueError("Piyasa karşılaştırması bir talep dosyası gerektirir.")
    as_of = _instant(case.get("as_of"))
    if as_of is None or as_of > now:
        raise ValueError("Rapor zamanı saat dilimli olmalı ve gelecekte olmamalı.")
    cutoff = as_of.astimezone(ZoneInfo("Europe/Istanbul")).date()
    baseline = _date(baseline_date)
    if baseline and baseline > cutoff:
        raise ValueError("Geçmiş alım tarihi rapor tarihinden sonra olamaz.")
    supplied = _payload(observations, default={})
    if not isinstance(supplied, dict) or any(not isinstance(supplied.get(key, []), list) for key in ("usdtry", "brent")):
        raise ValueError("Gözlemler usdtry/brent listeleri olmalı.")
    if any(len(supplied.get(key, [])) > 1000 for key in ("usdtry", "brent")):
        raise ValueError("Seri başına en çok 1000 gözlem sağlanabilir.")
    # A JSON claim cannot impersonate a fresh download performed by this component.
    values = {key: [{**item, "origin": "synthetic" if item.get("origin") == "synthetic" else "manual_supplied"}
                    for item in supplied.get(key, []) if isinstance(item, dict)] for key in ("usdtry", "brent")}
    warnings = []
    if not baseline:
        warnings.append("Kaynaklı önceki alım tarihi bulunmadı; geçmiş piyasa değişimi hesaplanmadı.")
    if enable_live and baseline:
        if now.astimezone(ZoneInfo("Europe/Istanbul")).date() > cutoff:
            warnings.append("Geçmiş bilgi kesimi için bugün indirilen veri kullanılmadı.")
        else:
            get = _cached_reader(fetcher)
            retrieved = now.isoformat()
            try:
                html = get(EIA_URL)
                if html:
                    values["brent"].extend(parse_eia_html(html, retrieved))
            except (ValueError, httpx.HTTPError, UnicodeError):
                warnings.append("EIA canlı Brent verisi okunamadı; değer tahmin edilmedi.")
            for target in dict.fromkeys([cutoff, *([baseline] if baseline else [])]):
                try:
                    for lag in range(MAX_LAG_DAYS + 1):
                        day = target - timedelta(days=lag)
                        if day.weekday() >= 5:
                            continue
                        xml = get(tcmb_url(day))
                        if xml is None:
                            continue
                        values["usdtry"].append(parse_tcmb_xml(xml, day, retrieved))
                        break
                except (ValueError, httpx.HTTPError, UnicodeError):
                    warnings.append(f"TCMB {target.isoformat()} kesimi okunamadı; kur tahmin edilmedi.")
    elif not enable_live:
        warnings.append("Canlı piyasa okuması kapalı; sağlanan gözlemler canlı doğrulama sayılmaz.")
    else:
        warnings.append("Kaynaklı geçmiş tarih adayı olmadığından resmi kaynaklara ağ isteği yapılmadı.")
    series = {}
    for name in ("usdtry", "brent"):
        before = select_observation(name, values[name], baseline, cutoff, now) if baseline else None
        current = select_observation(name, values[name], cutoff, cutoff, now)
        series[name] = {"baseline": before, "current": current,
                        "change_pct": percent_change(before["value"], current["value"]) if before and current else None}
        if current is None:
            warnings.append(f"{name}: rapor gününe kadar en fazla 7 gün eski kullanılabilir gözlem yok.")
        if baseline and before is None:
            warnings.append(f"{name}: önceki alım gününe kadar en fazla 7 gün eski gözlem yok.")
    proxy = {"baseline_value": None, "current_value": None, "change_pct": None, "unit": "TRY/barrel", "label": PROXY_LABEL}
    for period, key in (("baseline", "baseline_value"), ("current", "current_value")):
        fx, brent = series["usdtry"][period], series["brent"][period]
        if fx and brent:
            with localcontext() as context:
                context.prec = 40
                proxy[key] = format(_num(fx["value"]) * _num(brent["value"]), "f")
    proxy["change_pct"] = percent_change(proxy["baseline_value"], proxy["current_value"])
    origins = {item[period]["origin"] for item in series.values() for period in ("baseline", "current") if item[period]}
    return {"request_id": (case.get("request") or {}).get("id"), "as_of": as_of.isoformat(),
            "baseline_requested_date": baseline.isoformat() if baseline else None,
            "current_requested_date": cutoff.isoformat(), "series": series, "brent_try_proxy": proxy,
            "data_mode": "synthetic" if "synthetic" in origins else "official_download" if origins == {"official_download"} else "manual_or_partial",
            "coverage": "complete" if all(item["current"] and (item["baseline"] or not baseline) for item in series.values()) else "partial",
            "warnings": warnings + ["Kur ve Brent değişimi malzeme fiyatına aynı yüzdeyle yansıtılmaz; maliyet payları ve vergi/dağıtım etkileri bilinmiyor."],
            "sent": False}


def _history_records(history):
    history = _payload(history, default={"purchases": []})
    records = history.get("purchases") if isinstance(history, dict) else history
    if not isinstance(records, list) or len(records) > 100 or any(not isinstance(item, dict) for item in records):
        raise ValueError("Geçmiş alımlar en çok 100 kayıtlı purchases listesi olmalı.")
    return records


def _history_dates(history, request, cutoff):
    """Fiyat önermez; belgeyle destekli tarih adaylarını piyasa okuyucuya verir.

    Fiyat/vade/teknik karşılaştırılabilirliği bağımsız pazarlık planı doğrular.
    Birden fazla aday tutularak geçersiz son fiyat, geçerli eski alımı örtmez.
    """
    days = set()
    for item in _history_records(history):
        day = _date(item.get("purchase_date"))
        if (not day or day > cutoff or item.get("material_code") != request.get("material_code")
                or (item.get("request_id") and item["request_id"] != request.get("id"))):
            continue
        fields = item.get("source_fields")
        evidence = fields.get("purchase_date") if isinstance(fields, dict) else None
        if not isinstance(evidence, dict):
            continue
        source, key = item.get("source"), "document_id"
        if not isinstance(source, dict):
            source, key = item.get("email"), "message_id"
        if not isinstance(source, dict) or not source.get(key) or evidence.get(key) != source[key]:
            continue
        content = source.get("content") if key == "document_id" else source.get("body_excerpt")
        quote = evidence.get("quote")
        if (not isinstance(content, str) or not isinstance(quote, str) or not 0 < len(quote) <= 10_000
                or quote not in content or not any(text in quote for text in (day.isoformat(), day.strftime("%d.%m.%Y")))):
            continue
        days.add(day)
    if len(days) > 10:
        raise ValueError("Bir talep için en çok 10 farklı kaynaklı geçmiş alım tarihi karşılaştırılabilir; tarih aralığını daraltın.")
    return sorted(days, reverse=True)


def _cached_reader(fetcher=None):
    get, cache, started = fetcher or _http_get, {}, time.monotonic()
    def read(url):
        if url not in cache:
            if time.monotonic() - started >= LIVE_BUDGET_SECONDS:
                raise ValueError("Piyasa okuma süresi aşıldı; kalan kaynaklar bilinmiyor bırakıldı.")
            try:
                cache[url] = (True, get(url))
            except (ValueError, httpx.HTTPError, UnicodeError) as error:
                cache[url] = (False, error)
        success, result = cache[url]
        if not success:
            raise result
        return result
    return read


def case_market_context(case, *, price_history=None, observations=None, enable_live=False, now=None, fetcher=None):
    """Her kaynaklı tarih adayı için kesim; planner seçtiği alımla eşleştirir."""
    case = _payload(case, CASE_MARKER, {})
    as_of = _instant(case.get("as_of")) if isinstance(case, dict) else None
    if as_of is None:
        raise ValueError("Talep rapor zamanı bulunamadı.")
    cutoff = as_of.astimezone(ZoneInfo("Europe/Istanbul")).date()
    dates = _history_dates(price_history, case.get("request") or {}, cutoff)
    cached_get = _cached_reader(fetcher)
    contexts = [market_context(case, baseline_date=day.isoformat(), observations=observations,
                               enable_live=enable_live, now=now, fetcher=cached_get) for day in dates]
    if not contexts:
        return market_context(case, observations=observations, enable_live=enable_live, now=now, fetcher=cached_get)
    result = dict(contexts[0])
    result["baseline_options"] = contexts
    result["warnings"] = [*result["warnings"],
                          "Geçmiş tarih adayları fiyat/vade/şartname eşitliği anlamına gelmez; pazarlık planı kaynaklı uygun alımı ayrıca seçer."]
    return result


class NegotiationMarketComponent(Component):
    display_name = "Pazarlık · Kur ve Brent"
    description = "Önceki alım ve rapor tarihleri için TCMB USD satış ve EIA Brent gözlemlerini kaynaklarıyla karşılaştırır. Canlı okuma başlangıçta kapalıdır."
    name = "NegotiationMarket"
    icon = "ChartLine"
    inputs = [MessageTextInput(name="case_data", display_name="Teklif dosyası", required=True),
              MultilineInput(name="price_history", display_name="Kaynaklı geçmiş alımlar (JSON)", value="", advanced=True),
              MultilineInput(name="observations_json", display_name="Sağlanan piyasa gözlemleri (JSON)", value="", advanced=True),
              BoolInput(name="enable_live", display_name="Resmi TCMB/EIA verilerini oku", value=False)]
    outputs = [Output(name="market_context", display_name="Tarihli piyasa verisi", method="build_context", group_outputs=True),
               Output(name="history", display_name="Kaynaklı geçmiş alımlar", method="build_history", group_outputs=True)]

    def build_history(self) -> Message:
        return Message(text=json.dumps({"purchases": _history_records(self.price_history)}, ensure_ascii=False, allow_nan=False))

    def build_context(self) -> Message:
        payload = _payload(self.case_data, CASE_MARKER, {})
        cases = payload.get("cases", [payload]) if isinstance(payload, dict) else []
        if not isinstance(cases, list) or not 1 <= len(cases) <= 10:
            raise ValueError("Piyasa karşılaştırması 1–10 dosya içermeli.")
        shared_get = _cached_reader()
        results = [case_market_context(case, price_history=self.price_history, observations=self.observations_json,
                                       enable_live=self.enable_live, fetcher=shared_get) for case in cases]
        result = results[0] if len(results) == 1 else {"cases": results, "sent": False}
        self.status = "Canlı okuma kapalı" if not self.enable_live else "Resmi kaynak kesimleri denetlendi"
        return Message(text=MARKET_MARKER + json.dumps(result, ensure_ascii=False, allow_nan=False))
