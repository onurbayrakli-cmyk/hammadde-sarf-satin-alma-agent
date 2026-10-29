"""Kaynaklı tekliflerden pazarlık hedefleri üretir; yazışma veya sipariş göndermez."""

import calendar
import copy
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from lfx.custom import Component
from lfx.io import MessageTextInput, Output
from lfx.schema.message import Message

from quote_comparison import (_bounded, _decimal, _evidence, _instant, _json_numbers,
                              _number_in_quote, _public_url, _same_spec, analyze_case)

NEGOTIATION_MARKER = "PAZARLIK PLANI (JSON)\n"
NEGOTIATION_CASE_MARKER = "TEKLİF DOSYASI (JSON)\n"
NEGOTIATION_PURCHASE_DOCUMENT_TYPES = {"sap_purchase_order", "purchase_order", "purchase_order_confirmation",
                                       "supplier_order_confirmation", "purchase_invoice", "invoice", "sap_invoice",
                                       "goods_receipt", "sap_goods_receipt"}


def _np_payload(value, marker=None, optional=False):
    if isinstance(value, Message):
        value = value.text
    if optional and (value is None or value == ""):
        return {}
    if isinstance(value, (dict, list)):
        payload = value
    elif isinstance(value, str) and len(value) <= 750_000:
        if marker and marker in value:
            value = value.rsplit(marker, 1)[1]
        try:
            # Langflow metin girdisi JSON'daki \n kaçışını gerçek satır sonuna
            # çevirebilir. Bunu yalnız kaynak dizesi verisi olarak kabul et;
            # JSON yapısı, sınırlar ve alıntı doğrulamaları yine geçerlidir.
            payload = json.loads(value, strict=False)
        except ValueError as error:
            raise ValueError("Pazarlık girdisi geçerli JSON olmalıdır.") from error
    else:
        raise ValueError("Pazarlık girdisi metin veya JSON nesnesi olmalıdır.")
    if len(json.dumps(payload, ensure_ascii=False)) > 750_000:
        raise ValueError("Pazarlık girdisi boyut sınırını aşıyor.")
    return payload


def _np_email(value):
    if not isinstance(value, str) or len(value) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value):
        return None
    return value.lower()


def _np_date(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None
    except ValueError:
        return None


def _np_spec_key(specs):
    return tuple(sorted((str(key), " ".join(str(value).split()).casefold()) for key, value in specs.items()))


def _np_groups(result, basis):
    """Aynı fiyat kapsamı, miktar, kalite ve vadede iki ayrı firmanın son teklifleri."""
    rows = result["offers"]
    ids = [row["id"] for row in rows]
    message_ids = [row["email"]["message_id"] for row in rows]
    groups, supplier_rows = {}, {}
    as_of = _instant(result.get("as_of"))
    for row in rows:
        identifier, email = row["id"], row["email"]
        received = _instant(email.get("received_at"))
        if (not identifier or ids.count(identifier) != 1 or not email["message_id"]
                or message_ids.count(email["message_id"]) != 1 or not _np_email(email.get("sender"))
                or not row.get("supplier") or not received or not as_of or received > as_of):
            continue
        supplier_rows.setdefault(row["supplier"].casefold().strip(), []).append(row)
    latest_rows = []
    for offers in supplier_rows.values():
        newest = max(_instant(item["email"]["received_at"]) for item in offers)
        candidates = [item for item in offers if _instant(item["email"]["received_at"]) == newest]
        # Aynı anda iki revizyon varsa hangisinin yürürlükte olduğu bilinmiyor.
        if len(candidates) == 1:
            latest_rows.append(candidates[0])
    for row in latest_rows:
        if row["late"] or row["spec_status"] in {"eksik", "uygunsuz"}:
            continue
        # cash_total_gross doğrulanmış kaynak, talep kapsamı ve bilinen KDV gerektirir.
        price = _decimal(row.get(f"{basis}_unit_price"))
        total = _decimal(row.get(f"{basis}_total_gross"))
        if price is None or price <= 0 or total is None or row["price_basis"] not in {"vat_inclusive", "vat_exclusive"}:
            continue
        days = _decimal(row.get("term_days"))
        if basis == "term" and (not row["comparable"] or days is None or days <= 0 or days != days.to_integral_value()
                                or row["vat_basis"] != "including_term_markup"):
            continue
        key = (row["currency"], row["unit"], row["quantity"], row["price_basis"], row["vat_rate_pct"],
               row["vat_basis"] if basis == "term" else "cash", str(days) if basis == "term" else "0",
               _np_spec_key(row["technical_specs"]))
        supplier = row["supplier"].casefold().strip()
        group = groups.setdefault(key, {})
        group[supplier] = row
    result_groups = []
    for group in groups.values():
        values = list(group.values())
        if len(values) >= 2 and len({_np_email(item["email"]["sender"]) for item in values}) == len(values):
            result_groups.append(sorted(values, key=lambda item: (_decimal(item[f"{basis}_unit_price"]), item["id"])))
    return result_groups


def _np_history_source(record):
    """SAP belgesi veya e-postayı aynı alıntı denetiminden geçecek biçime çevirir."""
    source = record.get("source") if isinstance(record.get("source"), dict) else {}
    document_type = source.get("document_type") or record.get("document_type")
    # Önceki teklif/proforma alım kanıtı değildir. Belge türü tahmin edilmez.
    if not isinstance(document_type, str) or document_type.strip().lower() not in NEGOTIATION_PURCHASE_DOCUMENT_TYPES:
        return None
    rejected_states = {"cancelled", "canceled", "cancelled_record", "cancelledrecord", "canceled_record", "draft", "proforma", "quotation", "quote",
                       "supplier_quote", "iptal", "iptal_edildi", "taslak"}
    for metadata in (record, source):
        if metadata.get("cancelled") is True or metadata.get("canceled") is True:
            return None
        for key in ("status", "purchase_status", "state"):
            state = metadata.get(key)
            if isinstance(state, str) and state.strip().lower().replace(" ", "_") in rejected_states:
                return None
    if isinstance(record.get("email"), dict):
        evidence_record = copy.deepcopy(record)
    else:
        source = record.get("source")
        if not isinstance(source, dict) or not _bounded(source.get("document_id"), 500) or not isinstance(source.get("content"), str):
            return None
        evidence_record = copy.deepcopy(record)
        evidence_record["email"] = {"message_id": source["document_id"], "body_excerpt": source["content"],
                                    "source_url": source.get("source_url")}
        fields = evidence_record.get("source_fields")
        if not isinstance(fields, dict):
            return None
        for value in fields.values():
            if isinstance(value, dict):
                value["message_id"] = value.get("document_id")
    fields = evidence_record.get("source_fields")
    if not isinstance(fields, dict):
        return None
    evidence_record["cash_unit_price"] = record.get("unit_price")
    fields["cash_unit_price"] = fields.get("unit_price")
    return evidence_record


def _np_history_records(history, case):
    if history in ({}, None):
        record = case.get("previous_purchase")
        return [record] if isinstance(record, dict) else []
    if isinstance(history, list):
        return history
    if isinstance(history, dict) and isinstance(history.get("purchases"), list):
        return history["purchases"]
    raise ValueError("Geçmiş alış girdisi purchases listesi içermelidir.")


def _np_history(history, case, target, basis, as_of):
    warnings, candidates = [], []
    try:
        records = _np_history_records(history, case)
    except ValueError:
        return {"status": "unavailable"}, ["Geçmiş alış girdisi purchases listesi içermiyor; tarihsel fiyat gerekçesi kullanılmadı."]
    if len(records) > 500:
        raise ValueError("Geçmiş alış listesi 500 kayıt sınırını aşıyor.")
    request = case["request"]
    as_of_date = as_of.astimezone(ZoneInfo("Europe/Istanbul")).date()
    for record in records:
        if not isinstance(record, dict) or record.get("material_code") != request.get("material_code"):
            continue
        if record.get("request_id") not in {None, "", request.get("id")}:
            continue
        purchase_date = _np_date(record.get("purchase_date"))
        source_record = _np_history_source(record)
        price, days = _decimal(record.get("unit_price")), _decimal(record.get("term_days"))
        expected_days = _decimal(target["term_days"]) if basis == "term" else Decimal(0)
        fields = ("material_code", "quantity", "unit", "currency", "price_basis", "term_days", "cash_unit_price", "purchase_date")
        valid = bool(purchase_date and purchase_date <= as_of_date and source_record and price is not None and price > 0
                     and days == expected_days and record.get("currency") == target["currency"]
                     and record.get("unit") == target["unit"] and _decimal(record.get("quantity")) == _decimal(target["quantity"])
                     and record.get("price_basis") == target["price_basis"])
        if valid:
            for field in fields:
                value = source_record.get(field)
                # The historical unit price is aliased for storage compatibility,
                # but its evidence must state a purchase price, not a cash offer.
                source = _evidence(source_record, field, value,
                                   numeric_field="purchase_unit_price" if field == "cash_unit_price" else None)
                if source is None:
                    valid = False
                    break
                if field == "purchase_date":
                    representations = (purchase_date.isoformat(), purchase_date.strftime("%d.%m.%Y"), purchase_date.strftime("%d/%m/%Y"))
                    if not any(item in source["quote"] for item in representations):
                        valid = False
                elif field == "material_code" and not re.search(r"(?<!\w)" + re.escape(str(value)) + r"(?!\w)", source["quote"], re.IGNORECASE):
                    valid = False
        required = request.get("required_specs", {})
        specs = record.get("technical_specs", {})
        if not isinstance(specs, dict):
            valid = False
        if valid:
            # Şartname yoksa iki farklı kaliteyi geçmiş alış olarak bağlamayız.
            expected_specs = required or target["technical_specs"]
            for key, expected in expected_specs.items():
                actual = specs.get(key)
                if actual is None or not _same_spec(actual, expected) or not _evidence(source_record, f"technical_specs.{key}", actual):
                    valid = False
                    break
        if valid:
            candidates.append((purchase_date, record, source_record, price))
        else:
            warnings.append("Bir geçmiş alış kaydı kaynak/tarih, miktar, kalite, fiyat kapsamı veya vade bakımından doğrulanamadı; pazarlık gerekçesine alınmadı.")
    if not candidates:
        return {"status": "unavailable"}, warnings
    purchase_date, record, source_record, price = max(candidates, key=lambda item: item[0])
    months = (as_of_date.year - purchase_date.year) * 12 + as_of_date.month - purchase_date.month
    if as_of_date.day < min(purchase_date.day, calendar.monthrange(as_of_date.year, as_of_date.month)[1]):
        months -= 1
    current = _decimal(target[f"{basis}_unit_price"])
    email = source_record["email"]
    return {"status": "validated", "purchase_date": purchase_date.isoformat(), "display_date": purchase_date.strftime("%d.%m.%Y"),
            "unit_price": str(price), "currency": record["currency"], "unit": record["unit"], "price_basis": record["price_basis"],
            "term_days": str(_decimal(record.get("term_days"))), "elapsed_days": (as_of_date - purchase_date).days, "elapsed_full_months": months,
            "price_change_pct": format((current / price - 1) * 100, "f"),
            "source": {"document_id": email["message_id"], "source_url": _public_url(email.get("source_url"))}}, warnings


def _np_observation(observation, series, requested_date, as_of):
    if not isinstance(observation, dict) or observation.get("origin") not in {"official_download", "manual_supplied"}:
        return None
    observed_date = _np_date(observation.get("date"))
    value = _decimal(observation.get("value"))
    url = _public_url(observation.get("source_url"))
    unit = "TRY/USD" if series == "usdtry" else "USD/barrel"
    hostname = (urlsplit(url).hostname or "").lower() if url else ""
    domains = ("tcmb.gov.tr",) if series == "usdtry" else ("eia.gov",)
    source_quote = observation.get("source_quote")
    publication = _np_date(observation.get("publication_date"))
    retrieved = _instant(observation.get("retrieved_at"))
    parsed_url = urlsplit(url) if url else None
    exact_source = bool(parsed_url and parsed_url.scheme == "https" and not parsed_url.query and not parsed_url.fragment
                        and ((series == "usdtry" and parsed_url.netloc == "www.tcmb.gov.tr" and observed_date
                              and parsed_url.path == f"/kurlar/{observed_date:%Y%m}/{observed_date:%d%m%Y}.xml")
                             or (series == "brent" and parsed_url.netloc == "www.eia.gov"
                                 and parsed_url.path.lower() == "/dnav/pet/hist/rbrted.htm")))
    date_proven = False
    if observed_date and isinstance(source_quote, str):
        date_proven = any(item in source_quote for item in (observed_date.isoformat(), observed_date.strftime("%d.%m.%Y"),
                                                           observed_date.strftime("%d/%m/%Y")))
        if series == "brent" and not date_proven:
            week = re.search(r"(\d{4}) ([A-Z][a-z]{2})-\s*(\d{1,2}) to [A-Z][a-z]{2}-\s*\d{1,2}", source_quote)
            weekday = re.search(r"weekday=([1-5])(?:;|\s|$)", source_quote)
            if week and weekday:
                try:
                    start = datetime.strptime(" ".join(week.groups()), "%Y %b %d").date()
                    date_proven = start + timedelta(days=int(weekday.group(1)) - 1) == observed_date
                except ValueError:
                    pass
    if (not observed_date or not value or value <= 0 or not requested_date or not publication or not retrieved
            or not 0 <= (requested_date - observed_date).days <= 7
            or observation.get("unit") != unit or not any(hostname == domain or hostname.endswith("." + domain) for domain in domains)
            or not exact_source or not date_proven or not isinstance(source_quote, str) or not _number_in_quote(value, source_quote)
            or (publication and publication > as_of.astimezone(ZoneInfo("Europe/Istanbul")).date())
            or (retrieved and retrieved.astimezone(ZoneInfo("Europe/Istanbul")).date()
                > as_of.astimezone(ZoneInfo("Europe/Istanbul")).date())):
        return None
    return {"date": observed_date.isoformat(), "value": format(value, "f"), "unit": unit, "source_url": url,
            "source_name": observation.get("source_name"), "origin": observation["origin"],
            "publication_date": publication.isoformat() if publication else None}


def _np_direction(number):
    return "increase" if number > 0 else "decrease" if number < 0 else "unchanged"


def _np_market(market, request_id, history, as_of):
    warnings, result = [], {}
    if not isinstance(market, dict):
        return {}, ["Piyasa verisi nesne biçiminde değil; pazarlıkta kullanılmadı."]
    if isinstance(market.get("cases"), list):
        matches = [item for item in market["cases"] if isinstance(item, dict) and item.get("request_id") == request_id]
        market = matches[0] if len(matches) == 1 else {}
    if history.get("status") == "validated" and isinstance(market.get("baseline_options"), list):
        options = [item for item in market["baseline_options"] if isinstance(item, dict)
                   and item.get("baseline_requested_date") == history["purchase_date"]]
        if len(options) == 1:
            market = options[0]
    series = market.get("series", {})
    if not isinstance(series, dict) or history.get("status") != "validated":
        return {}, []
    baseline_date = _np_date(history["purchase_date"])
    current_date = as_of.astimezone(ZoneInfo("Europe/Istanbul")).date()
    for name in ("usdtry", "brent"):
        raw = series.get(name, {})
        if not isinstance(raw, dict):
            continue
        baseline = _np_observation(raw.get("baseline"), name, baseline_date, as_of)
        current = _np_observation(raw.get("current"), name, current_date, as_of)
        if baseline and current and _np_date(baseline["date"]) <= _np_date(current["date"]):
            change = (_decimal(current["value"]) / _decimal(baseline["value"]) - 1) * 100
            result[name] = {"baseline_date": baseline["date"], "current_date": current["date"],
                            "baseline_value": baseline["value"], "current_value": current["value"],
                            "change_pct": format(change, "f"), "direction": _np_direction(change), "unit": baseline["unit"],
                            "sources": [baseline, current]}
        elif raw:
            warnings.append(f"{name}: geçmiş alışa ve rapor kesimine uygun resmî kaynaklı tarih çifti doğrulanamadı; yön/yüzde gerekçeye alınmadı.")
    if "usdtry" in result and "brent" in result:
        before = _decimal(result["usdtry"]["baseline_value"]) * _decimal(result["brent"]["baseline_value"])
        after = _decimal(result["usdtry"]["current_value"]) * _decimal(result["brent"]["current_value"])
        change = (after / before - 1) * 100
        result["brent_try_proxy"] = {"baseline_value": str(before), "current_value": str(after), "change_pct": format(change, "f"),
                                     "direction": _np_direction(change),
                                     "label": "Brent × USD/TRY göstergesi; gerçek yakıt, nakliye veya malzeme maliyetine eşit değildir."}
    return result, warnings


def _np_numeric_values(value):
    values = set()
    def collect(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if key not in {"source_url", "source_name", "document_id", "material_code", "request_id", "target_offer_id", "recipient", "target_supplier"}:
                    collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)
        elif isinstance(item, (int, Decimal)) and not isinstance(item, bool):
            values.add(str(item))
        elif isinstance(item, str):
            values.update(re.findall(r"\d+(?:[.,]\d+)*", item))
    collect(value)
    return sorted(values)


def _np_case_plan(case, history_input, market_input):
    result = analyze_case(case)
    request = result["request"]
    base = {"request_id": request.get("id"), "material_code": request.get("material_code"),
            "material_name": request.get("material_name"), "status": "blocked", "draft_allowed": False,
            "sent": False, "draft_only": True, "warnings": list(result["warnings"])}
    if result["report_status"] != "final":
        base["warnings"].append("Araştırma süresi tamamlanmadan pazarlık taslağı hazırlanmaz.")
        return base
    term_groups = _np_groups(result, "term")
    groups, basis = (term_groups, "term") if term_groups else (_np_groups(result, "cash"), "cash")
    if len(groups) != 1:
        base["warnings"].append("Aynı malzeme, miktar, birim, para birimi, teknik kapsam ve fiyat/vade bazında en az iki ayrı firmanın doğrulanmış teklifi gerekli; birden fazla bağımsız grup varsa baz seçilmelidir.")
        return base
    group = groups[0]
    target = group[0]
    as_of = _instant(result["as_of"])
    history, history_warnings = _np_history(history_input, case, target, basis, as_of)
    market, market_warnings = _np_market(market_input, request.get("id"), history, as_of)
    opening = Decimal(2)
    strategy = "Doğrulanmış maliyet göstergesi çifti yok; başlangıç indirim talebi %2."
    if "usdtry" in market and "brent_try_proxy" in market:
        fx, proxy = _decimal(market["usdtry"]["change_pct"]), _decimal(market["brent_try_proxy"]["change_pct"])
        if fx > 0 and proxy > 0:
            opening, strategy = Decimal(1), "Kur ve TL cinsinden Brent göstergesi birlikte arttığı için ilişkiyi koruyan %1 indirim talebi."
        elif fx <= 0 and proxy <= 0:
            opening, strategy = Decimal(2), "Kur ve TL cinsinden Brent göstergesi artmadığı için %2 indirim talebi."
        else:
            opening, strategy = Decimal("1.5"), "Kur ile TL cinsinden Brent göstergesi farklı yönlerde; temkinli %1,5 indirim talebi."
    price = _decimal(target[f"{basis}_unit_price"])
    days = _decimal(target.get("term_days"))
    extra = Decimal(30) if days is not None and 0 < days <= 36_500 and days == days.to_integral_value() else None
    facts = {"verified_competition": True, "comparable_offer_count": len(group), "material_name": request.get("material_name"),
             "quantity": target["quantity"], "unit": target["unit"], "currency": target["currency"],
             "price_basis": target["price_basis"], "negotiation_basis": basis, "current_price": str(price),
             "opening_discount_pct": str(opening), "opening_target_price": format(price * (1 - opening / 100), "f"),
             "term_days": str(days) if days is not None else None, "extra_term_days": "30" if extra else None,
             "cash_comparison_term_days": "0" if basis == "cash" else None,
             "requested_term_days": str(days + extra) if extra else None, "history": history, "market_context": market}
    base.update({"status": "ready", "draft_allowed": True, "target_offer_id": target["id"],
                 "recipient": _np_email(target["email"]["sender"]), "target_supplier": target["supplier"],
                 "source_message_id": target["email"]["message_id"],
                 **{key: value for key, value in facts.items() if key not in {"verified_competition", "comparable_offer_count"}},
                 "fallback_discount_pct": "1", "fallback_target_price": format(price * Decimal("0.99"), "f"),
                 "strategy_reason": strategy, "facts_allowed": facts,
                 "excluded_supplier_names": [item["supplier"] for item in result["offers"] if item["supplier"] != target["supplier"]],
                 "constraints": {"no_competitor_identity_or_price_disclosure": True, "fallback_is_internal_only": True,
                                 "ask_term_markup_separately": bool(extra), "extension_acceptance": False,
                                 "no_market_cost_passthrough_assumption": True, "no_order_or_payment_commitment": True,
                                 "supplier_acceptance_known": False},
                 "warnings": base["warnings"] + history_warnings + market_warnings})
    base["allowed_numeric_values"] = _np_numeric_values(facts)
    return _json_numbers(base)


def plan_negotiation(case_data, market_context=None, price_history=None):
    payload = _np_payload(case_data, NEGOTIATION_CASE_MARKER)
    input_warnings = []
    try:
        market = _np_payload(market_context, "PAZARLIK PİYASA VERİSİ (JSON)\n", optional=True)
    except ValueError:
        market = {}
        input_warnings.append("Piyasa girdisi okunamadı; kaynaklı fiyat pazarlığı sürer, kur/Brent gerekçesi kullanılmaz.")
    try:
        history = _np_payload(price_history, optional=True)
    except ValueError:
        history = {}
        input_warnings.append("Geçmiş alış girdisi okunamadı; doğrulanmamış tarih ve fiyat pazarlık gerekçesine alınmadı.")
    cases = payload.get("cases") if isinstance(payload, dict) and "cases" in payload else [payload]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 10 or any(not isinstance(case, dict) for case in cases):
        raise ValueError("Pazarlık dosyası 1–10 ayrı talep içermelidir.")
    identifiers = [case.get("request", {}).get("id") for case in cases if isinstance(case.get("request"), dict)]
    if len(identifiers) != len(cases) or any(not identifier for identifier in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError("Her pazarlık kaleminin ayrı talep kimliği gereklidir.")
    plans = [_np_case_plan(case, history, market) for case in cases]
    for plan in plans:
        plan["warnings"].extend(input_warnings)
    ready_count = sum(item["draft_allowed"] for item in plans)
    return {"schema_version": 1, "status": "ready" if ready_count == len(plans) else "partial" if ready_count else "blocked",
            "draft_only": True, "sent": False, "requests": plans}


class NegotiationPlanComponent(Component):
    display_name = "Kaynaklı Pazarlık Planı"
    description = "Aynı kapsamlı en düşük teklif için %1–2 indirim hedefi, koşullu +30 gün ve kaynaklı geçmiş/kur/Brent gerekçesi hazırlar."
    name = "NegotiationPlan"
    icon = "Handshake"
    inputs = [MessageTextInput(name="case_data", display_name="Teklif Dosyası", required=True),
              MessageTextInput(name="market_context", display_name="Kur ve Brent Kanıtları", value=""),
              MessageTextInput(name="price_history", display_name="Kaynaklı Geçmiş Alışlar", value="", advanced=True)]
    outputs = [Output(display_name="Pazarlık Planı", name="plan", method="build_plan", group_outputs=True),
               Output(display_name="Yazışma Agentına", name="model_input", method="build_model_input", group_outputs=True)]

    def _prepare(self):
        if not hasattr(self, "_negotiation_prepared"):
            try:
                self._negotiation_prepared = plan_negotiation(self.case_data, self.market_context, self.price_history)
            except (ValueError, TypeError, KeyError) as error:
                self._negotiation_prepared = {"schema_version": 1, "status": "blocked", "draft_only": True, "sent": False,
                                              "requests": [], "warnings": [str(error)]}
            self.status = "Pazarlık taslağına hazır" if any(item.get("draft_allowed") for item in self._negotiation_prepared["requests"]) else "Kaynak/süre/karşılaştırılabilir teklif bekleniyor"
        return self._negotiation_prepared

    def _guard(self, plan):
        if getattr(self, "_vertex", None) is not None:
            excluded = [] if any(item.get("draft_allowed") for item in plan["requests"]) else ["model_input"]
            for output in excluded:
                self.stop(output)
            self.graph.exclude_branches_conditionally(self._id, excluded)

    def build_plan(self) -> Message:
        plan = self._prepare()
        self._guard(plan)
        return Message(text=NEGOTIATION_MARKER + json.dumps(plan, ensure_ascii=False))

    def build_model_input(self) -> Message:
        plan = self._prepare()
        self._guard(plan)
        safe = copy.deepcopy(plan)
        safe["requests"] = [item for item in safe["requests"] if item.get("draft_allowed")]
        for item in safe["requests"]:
            for key in ("excluded_supplier_names", "fallback_discount_pct", "fallback_target_price", "source_message_id"):
                item.pop(key, None)
        return Message(text=NEGOTIATION_MARKER + json.dumps(safe, ensure_ascii=False) if safe["requests"] else "")
