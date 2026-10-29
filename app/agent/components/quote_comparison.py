"""Kaynak e-posta/ek alıntılarıyla doğrulanan, yerel teklif karşılaştırması.

KDV hesabı tedarikçinin açık beyanını kıyaslar; vergi uygulamasının hukuki
uygunluğunu doğrulamaz. Eksik veriler için oran, süre veya fiyat varsayılmaz.
"""

import ipaddress
import json
import re
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
from urllib.parse import quote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from lfx.custom import Component
from lfx.io import MessageTextInput, Output
from lfx.schema.message import Message

MARKER = "TEKLİF DOSYASI (JSON)\n"
RESULT_MARKER = "TEKLİF KARŞILAŞTIRMASI (JSON)\n"
NUMERIC_FIELDS = {"quantity", "cash_unit_price", "term_unit_price", "term_days", "vat_rate_pct", "delivery_days"}
PRICE_BASES = {"vat_exclusive", "vat_inclusive", "not_stated"}
VAT_BASES = {"including_term_markup", "principal_only", "not_stated"}
FINAL_STATES = {"completed", "final", "ready_for_report", "report_prepared"}


def _text(value, limit=500_000):
    value = value.text if isinstance(value, Message) else value
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("Teklif dosyası metin olmalı ve boyut sınırını aşmamalıdır.")
    return value


def _bounded(value, limit=500):
    return value.strip() if isinstance(value, str) and 0 < len(value.strip()) <= limit else None


def _decimal(value):
    if isinstance(value, bool) or value is None or isinstance(value, (dict, list)):
        return None
    try:
        text = str(value).strip()
        if len(text) > 80:
            return None
        number = Decimal(text.replace(",", ".") if "." not in text else text)
        return number if number.is_finite() and abs(number) <= Decimal("1e18") else None
    except InvalidOperation:
        return None


def _instant(value):
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return instant.astimezone(timezone.utc) if instant.tzinfo is not None else None
    except ValueError:
        return None


def _display_instant(value):
    instant = _instant(value)
    return instant.astimezone(ZoneInfo("Europe/Istanbul")).strftime("%d.%m.%Y %H:%M (Europe/Istanbul)") if instant else "—"


def _public_url(value):
    if not isinstance(value, str) or len(value) > 2000 or any(char.isspace() for char in value):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            return None
        if parsed.port is not None and parsed.port not in {80, 443}:
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if "." not in host or host.lower().endswith((".local", ".localhost", ".internal", ".invalid", ".test")):
                return None
        return urlunsplit((parsed.scheme, parsed.netloc, quote(parsed.path, safe="/%:@-._~"),
                           quote(parsed.query, safe="=&%:@/?-._~"), quote(parsed.fragment, safe="%-._~")))
    except (ValueError, UnicodeError):
        return None


def _escape(value):
    text = "—" if value is None else str(value)
    text = " ".join(text.split())
    for char in "\\`*_{}[]()<>|~":
        text = text.replace(char, "\\" + char)
    return text


def _format(value, percent=False):
    number = _decimal(value)
    if number is None:
        return "—"
    return f"{number:.2f}" + ("%" if percent else "")


def _number_in_quote(number, quotation):
    # 1.000,50 / 1,000.50 ve sade ondalık yazımlar desteklenir; yuvarlama yapılmaz.
    for token in re.findall(r"(?<![\w])[-+]?\d[\d.,]*(?![\w])", quotation):
        if re.fullmatch(r"\d{1,3}[.,]\d{3}", token):
            # 1.000 / 1,000 hem ondalık hem binlik ayracı olabilir; yerel ayar varsayılmaz.
            continue
        variants = [token]
        if "," in token and "." in token:
            variants.append(token.replace(".", "").replace(",", ".") if token.rfind(",") > token.rfind(".")
                            else token.replace(",", ""))
        elif token.count(".") > 1 or token.count(",") > 1:
            variants.append(token.replace(".", "").replace(",", ""))
        if any(_decimal(candidate) == number for candidate in variants):
            return True
    return False


def _semantic_text(value):
    # Turkish/ASCII supplier quotations share the same small field vocabulary.
    text = unicodedata.normalize("NFKD", value.casefold().replace("ı", "i"))
    return "".join(char for char in text if not unicodedata.combining(char))


_NUMERIC_LABELS = {
    "cash_unit_price": r"cash_unit_price|(?:pesin|nakit)(?:\s+(?:birim(?:\s+fiyati?)?|fiyati?))?|cash(?:\s+(?:unit\s+price|price))?",
    "term_unit_price": r"term_unit_price|(?:vadeli|veresiye)(?:\s+(?:birim(?:\s+fiyati?)?|fiyati?))?|(?:term|credit|deferred)\s+(?:unit\s+price|price)",
    "purchase_unit_price": r"purchase_unit_price|(?:alis|satin\s+alma)\s+birim\s+fiyati?|purchase\s+unit\s+price",
    "quantity": r"quantity|qty|miktari?|adet\s+sayisi",
    "term_days": r"term_days|vade(?:\s+(?:suresi|gunu|gun))?|odeme\s+(?:suresi|vadesi)|payment\s+terms?|term(?:\s+days)?|net",
    "vat_rate_pct": r"vat_rate_pct|(?:kdv|vat)(?:\s+(?:orani|rate))?",
    "delivery_days": r"delivery_days|teslim(?:at)?(?:\s+suresi)?|termin(?:\s+suresi)?|delivery(?:\s+(?:time|days))?|lead\s+time",
}
_NUMERIC_LABEL_PATTERN = re.compile(
    r"(?<![a-z0-9_])(?:" + "|".join(f"(?P<{key}>{pattern})" for key, pattern in _NUMERIC_LABELS.items()) + r")(?![a-z_])"
)
_QUOTED_NUMBER_PATTERN = r"[-+]?\d+(?:[.,]\d+)*"
_MONEY_UNIT_PATTERN = r"(?:(?:try|tl|usd|eur|gbp)\b|[₺$€])"
_DAY_UNIT_PATTERN = r"(?:is\s+)?(?:gun|days?|d)\b"
_NUMERIC_VALUE_PATTERN = re.compile(
    r"\s*[:=]?\s*(?P<currency>" + _MONEY_UNIT_PATTERN + r"\s*)?(?P<percent>%\s*)?"
    r"(?P<number>" + _QUOTED_NUMBER_PATTERN + r")(?!\d|[.,]\d)"
)


def _numeric_field_in_quote(field, number, quotation):
    """Bind a value to its own explicit label, never to any number in a paragraph.

    One quotation may contain several distinctly labelled fields. Multiple values
    for the same field (ranges, old/new prices, alternatives) remain ambiguous;
    callers must provide a narrower, exact source quotation or request review.
    """
    text = _semantic_text(quotation)
    labels = list(_NUMERIC_LABEL_PATTERN.finditer(text))
    candidates = []
    for index, label in enumerate(labels):
        if label.lastgroup != field:
            continue
        end = labels[index + 1].start() if index + 1 < len(labels) else len(text)
        segment = text[label.end():end]
        match = _NUMERIC_VALUE_PATTERN.match(segment)
        if match is None:
            continue
        # A second number in the same label's span cannot be silently selected.
        if len(re.findall(_QUOTED_NUMBER_PATTERN, segment)) != 1:
            return False
        suffix = segment[match.end():]
        percent = bool(match.group("percent") or re.match(r"\s*%", suffix))
        monetary = bool(match.group("currency") or re.match(r"\s*" + _MONEY_UNIT_PATTERN, suffix))
        days = bool(re.match(r"\s*" + _DAY_UNIT_PATTERN, suffix))
        physical_unit = bool(re.match(r"\s*(?:kg|kilogram|g|gr|ton|adet|pcs|lt|l|m2|m3|metre)\b", suffix))
        if field.endswith("unit_price"):
            explicit_price = re.search(r"birim|unit[_\s]+price", label.group())
            per_unit = re.match(r"\s*(?:" + _MONEY_UNIT_PATTERN + r")?\s*/\s*[a-z]\w*", suffix)
            if percent or days or physical_unit or not (explicit_price or per_unit):
                continue
        if field == "quantity" and (percent or monetary or days):
            continue
        if field in {"term_days", "delivery_days"}:
            day_label = label.group().endswith(("_days", " days", " gun", " gunu"))
            if percent or monetary or physical_unit or (not day_label and not days):
                continue
        if field == "vat_rate_pct":
            percent_label = label.group().endswith(("_pct", " orani", " rate"))
            if monetary or days or physical_unit or not (percent_label or percent):
                continue
        candidates.append(match.group("number"))
    return len(candidates) == 1 and _number_in_quote(number, candidates[0])


def _evidence(offer, field, value, *, numeric_field=None):
    """Alıntı kimliği yanında gerçek kaynak içeriği ve sayısal değeri de denetlenir."""
    email = offer.get("email")
    fields = offer.get("source_fields")
    evidence = fields.get(field) if isinstance(fields, dict) else None
    if not isinstance(email, dict) or not isinstance(evidence, dict):
        return None
    message_id = _bounded(email.get("message_id"), 500)
    quotation = _bounded(evidence.get("quote"), 10_000)
    if not message_id or evidence.get("message_id") != message_id or not quotation:
        return None
    attachment_id = evidence.get("attachment_id")
    source = email.get("body_excerpt")
    source_url = _public_url(email.get("source_url"))
    source_name = "E-posta gövdesi"
    if attachment_id is not None:
        attachments = email.get("attachments", [])
        matching = [item for item in attachments if isinstance(item, dict) and item.get("attachment_id") == attachment_id] if isinstance(attachments, list) else []
        if len(matching) != 1:
            return None
        attachment = matching[0]
        source = attachment.get("text_excerpt")
        source_name = _bounded(attachment.get("filename"), 300) or "E-posta eki"
        source_url = _public_url(attachment.get("source_url")) or source_url
    if not isinstance(source, str) or len(source) > 100_000 or quotation not in source:
        return None
    if field in NUMERIC_FIELDS:
        number = _decimal(value)
        if number is None or not _numeric_field_in_quote(numeric_field or field, number, quotation):
            return None
    elif field.startswith("technical_specs."):
        if isinstance(value, (dict, list, bool)) or value is None:
            return None
        if isinstance(value, (int, float, Decimal)):
            if not _number_in_quote(_decimal(value), quotation):
                return None
        elif " ".join(str(value).split()).casefold() not in " ".join(quotation.split()).casefold():
            return None
    elif field in {"currency", "unit"}:
        aliases = {"TRY": ("TRY", "TL", "₺"), "USD": ("USD", "$"), "EUR": ("EUR", "€"),
                   "ADET": ("ADET", "AD.", "PCS"), "KG": ("KG", "KİLOGRAM", "KILOGRAM")}
        if not any(alias.casefold() in quotation.casefold() for alias in aliases.get(str(value).upper(), (str(value),))):
            return None
    elif field in {"price_basis", "vat_basis"}:
        labels = {"vat_exclusive": ("kdv hariç", "vat exclusive", "vat excluded", "vat_exclusive"),
                  "vat_inclusive": ("kdv dahil", "vat inclusive", "vat included", "vat_inclusive"),
                  "including_term_markup": ("vade farkı dahil", "vade farkına da", "anapara ve vade farkı", "including_term_markup"),
                  "principal_only": ("yalnız anapara", "sadece anapara", "anapara üzerinden", "principal_only"),
                  "not_stated": ("belirtilmedi", "not stated", "not_stated")}
        if not any(label in " ".join(quotation.split()).casefold() for label in labels.get(str(value), ())):
            return None
    return {"message_id": message_id, "quote": quotation, "attachment_id": attachment_id,
            "source_name": source_name, "source_url": source_url}


def _value(offer, field, warnings, sources, numeric=False):
    raw = offer.get(field)
    if raw is None or raw == "":
        warnings.append(f"{field}: belirtilmedi")
        return None
    value = _decimal(raw) if numeric else _bounded(raw)
    if value is None or (numeric and value < 0):
        warnings.append(f"{field}: geçersiz değer")
        return None
    source = _evidence(offer, field, value)
    if source is None:
        warnings.append(f"{field}: kaynak alıntısı doğrulanamadı")
        return None
    sources[field] = source
    return value


def _json_numbers(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: _json_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_numbers(item) for item in value]
    return value


def _same_spec(actual, expected):
    # Eşdeğer kalite veya alternatif standart yorumlanmaz; sadece aynı beyan eşleşir.
    if isinstance(actual, (int, float, Decimal)) and not isinstance(actual, bool):
        return _decimal(actual) == _decimal(expected)
    return " ".join(str(actual).split()).casefold() == " ".join(str(expected).split()).casefold()


def _analyze_offer(offer, request, as_of, deadline):
    warnings, sources = [], {}
    email = offer.get("email") if isinstance(offer.get("email"), dict) else {}
    received = _instant(email.get("received_at"))
    usable_email = bool(_bounded(email.get("message_id"), 500) and received and as_of and received <= as_of
                        and _bounded(email.get("sender"), 500))
    result = {"id": _bounded(offer.get("id"), 120), "supplier": _bounded(offer.get("supplier"), 200),
              "email": {"message_id": _bounded(email.get("message_id"), 500), "received_at": email.get("received_at"),
                        "sender": _bounded(email.get("sender"), 500), "subject": _bounded(email.get("subject"), 1000),
                        "body_excerpt": _bounded(email.get("body_excerpt"), 20_000),
                        "source_url": _public_url(email.get("source_url"))},
              "late": bool(received and deadline and received > deadline), "sources": sources,
              "warnings": warnings, "technical_specs": {}, "required_specs_missing": [], "required_specs_mismatched": []}
    if not usable_email:
        warnings.append("E-posta kimliği/göndereni veya saat dilimli alınma tarihi doğrulanamadı; teklif hesaplamaya alınmadı.")
    fields = {field: _value(offer, field, warnings, sources, field in NUMERIC_FIELDS)
              for field in ("quantity", "unit", "currency", "cash_unit_price", "term_unit_price", "term_days",
                            "vat_rate_pct", "vat_basis", "price_basis", "delivery_days")}
    if fields["price_basis"] not in PRICE_BASES:
        fields["price_basis"] = None
    if fields["vat_basis"] not in VAT_BASES:
        fields["vat_basis"] = None
    rate = fields["vat_rate_pct"]
    if rate is not None and rate > 100:
        warnings.append("KDV oranı geçersiz; yüzde 0–100 aralığında olmalıdır.")
        fields["vat_rate_pct"] = rate = None
    specs = offer.get("technical_specs", {})
    if isinstance(specs, dict) and len(specs) <= 50:
        for key, value in specs.items():
            if not _bounded(key, 200) or isinstance(value, (dict, list)) or value is None:
                continue
            source = _evidence(offer, f"technical_specs.{key}", value)
            if source:
                result["technical_specs"][key] = value
                sources[f"technical_specs.{key}"] = source
            else:
                warnings.append(f"technical_specs.{key}: kaynak alıntısı doğrulanamadı; yayımlanmadı")
    required = request.get("required_specs", {})
    for key, expected in required.items():
        if key not in result["technical_specs"]:
            result["required_specs_missing"].append(key)
        elif not _same_spec(result["technical_specs"][key], expected):
            result["required_specs_mismatched"].append(key)
    result["spec_status"] = ("uygunsuz" if result["required_specs_mismatched"] else "eksik" if result["required_specs_missing"]
                             else "uygun" if required else "şartname belirtilmedi")
    material_matches = bool(offer.get("material_code") == request.get("material_code") and request.get("material_code"))
    scope_matches = bool(material_matches and fields["quantity"] is not None and fields["quantity"] > 0
                         and fields["quantity"] == _decimal(request.get("quantity")) and fields["unit"] == request.get("unit"))
    if not scope_matches:
        warnings.append("Malzeme kodu, miktar veya birim talep kapsamıyla aynı değil; sıralamaya alınmaz.")
    result.update(fields)
    result.update({key: None for key in ("cash_unit_ex_vat", "term_unit_ex_vat", "cash_total_ex_vat", "term_total_ex_vat",
                                        "cash_total_gross", "term_total_gross", "cash_vat_total", "term_vat_total",
                                        "term_markup_amount", "term_markup_percent", "monthly_simple_percent", "monthly_effective_percent")})
    cash, term, quantity = fields["cash_unit_price"], fields["term_unit_price"], fields["quantity"]
    cash_ex = term_ex = cash_gross = term_gross = None
    basis, price_basis = fields["vat_basis"], fields["price_basis"]
    eligible_math = bool(usable_email and scope_matches and fields["currency"])
    both_prices = cash is not None and term is not None
    if eligible_math and price_basis == "vat_exclusive":
        cash_ex, term_ex = cash, term
        if rate is not None:
            cash_gross = cash * (1 + rate / 100) if cash is not None else None
            if term is not None and (basis == "including_term_markup" or (both_prices and cash == term) or rate == 0):
                term_gross = term * (1 + rate / 100)
            elif both_prices and basis == "principal_only":
                term_gross = term + cash * rate / 100
    elif eligible_math and price_basis == "vat_inclusive" and rate is not None:
        cash_gross, term_gross = cash, term
        cash_ex = cash / (1 + rate / 100) if cash is not None else None
        if term is not None and (basis == "including_term_markup" or (both_prices and cash == term) or rate == 0):
            term_ex = term / (1 + rate / 100)
        elif both_prices and basis == "principal_only":
            term_ex = term - (cash - cash_ex)
    if eligible_math and not (both_prices and cash == term) and basis not in {"including_term_markup", "principal_only"} and rate != 0:
        warnings.append("Vade farkının KDV matrahına dahil edilip edilmediği belirtilmedi; belirsiz KDV hesabı yapılmadı.")
    if basis == "principal_only":
        warnings.append("KDV matrahı yalnız anapara: tedarikçi beyanı kıyaslandı; hukuki uygunluk teyidi değildir, firma onayı gerekir.")
    if eligible_math and both_prices and term < cash:
        warnings.append("Vadeli fiyat peşin fiyatın altında; indirim/koşul teyidi gerekir.")
    if eligible_math and price_basis not in {"vat_exclusive", "vat_inclusive"}:
        warnings.append("Fiyatın KDV dahil/hariç olduğu belirtilmedi; toplam hesaplanmadı.")
    result.update({"cash_unit_ex_vat": cash_ex, "term_unit_ex_vat": term_ex,
                   "cash_total_ex_vat": cash_ex * quantity if cash_ex is not None else None,
                   "term_total_ex_vat": term_ex * quantity if term_ex is not None else None,
                   "cash_total_gross": cash_gross * quantity if cash_gross is not None else None,
                   "term_total_gross": term_gross * quantity if term_gross is not None else None,
                   "cash_vat_total": (cash_gross - cash_ex) * quantity if cash_gross is not None and cash_ex is not None else None,
                   "term_vat_total": (term_gross - term_ex) * quantity if term_gross is not None and term_ex is not None else None})
    if cash_ex is not None and term_ex is not None:
        result["term_markup_amount"] = (term_ex - cash_ex) * quantity
        if cash_ex > 0:
            markup = (term_ex / cash_ex - 1) * 100
            result["term_markup_percent"] = markup
            days = fields["term_days"]
            if days is not None and 1 <= days <= 36_500:
                result["monthly_simple_percent"] = markup * 30 / days
                with localcontext() as context:
                    context.prec = 28
                    try:
                        effective = ((term_ex / cash_ex) ** (Decimal(30) / days) - 1) * 100
                        if effective.is_finite() and abs(effective) < Decimal("1e12"):
                            result["monthly_effective_percent"] = effective
                    except (DecimalException, OverflowError):
                        pass
            elif days == 0 and cash_ex != term_ex:
                warnings.append("Sıfır gün vadede fiyat farkı var; aylık oran hesaplanmadı.")
            elif days is not None and (days < 1 or days > 36_500):
                warnings.append("Vade süresi 1–36500 gün dışında; aylık oran karşılaştırması yapılmadı.")
    missing = offer.get("missing_fields", [])
    if isinstance(missing, list):
        warnings.extend("Tedarikçi bilgisi eksik: " + item for item in missing if _bounded(item, 200))
    result["comparable"] = bool(usable_email and scope_matches and fields["currency"] and result["term_total_gross"] is not None
                                and result["spec_status"] not in {"eksik", "uygunsuz"} and not result["late"])
    return result


def analyze_case(case_data):
    """Saf hesap; ağ, takvim veya gönderim işlemi gerçekleştirmez."""
    if isinstance(case_data, dict):
        payload = case_data
        if len(json.dumps(payload, ensure_ascii=False)) > 500_000:
            raise ValueError("Teklif dosyası boyut sınırını aşıyor.")
    else:
        text = _text(case_data)
        if MARKER not in text:
            raise ValueError("TEKLİF DOSYASI (JSON) işareti bulunamadı.")
        try:
            payload = json.loads(text.rsplit(MARKER, 1)[1])
        except (ValueError, TypeError) as error:
            raise ValueError("Teklif dosyası JSON biçimi geçersiz.") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("request"), dict):
        raise ValueError("Talep bilgisi bulunamadı.")
    request = payload["request"]
    required = request.get("required_specs", {})
    offers = payload.get("offers", [])
    if not isinstance(required, dict) or len(required) > 50 or not isinstance(offers, list) or len(offers) > 100:
        raise ValueError("Şartname veya teklif listesi geçersiz ya da sınırın üzerinde.")
    if any(not _bounded(key, 200) or isinstance(value, (dict, list, bool)) or value is None for key, value in required.items()):
        raise ValueError("Teknik şartname alanları sade metin veya sayı olmalıdır.")
    if any(not isinstance(offer, dict) for offer in offers):
        raise ValueError("Her teklif bir JSON nesnesi olmalıdır.")
    as_of, deadline = _instant(payload.get("as_of")), _instant(request.get("deadline_at"))
    status = "final" if (as_of and deadline and as_of >= deadline and request.get("state") in FINAL_STATES) else "preliminary"
    warnings = []
    if as_of is None:
        warnings.append("Rapor zamanı eksik veya saat dilimi yok; teklif hesapları engellendi.")
    if deadline is None:
        warnings.append("Araştırma süresi/son tarih henüz belirlenmedi; ilgili kişiden süre istenmeli.")
    elif as_of and as_of < deadline:
        warnings.append("Araştırma süresi devam ediyor; bu bir ön değerlendirmedir.")
    elif request.get("state") not in FINAL_STATES:
        warnings.append("Süre dolmuş olsa da iş akışı tamamlanmadı; rapor ön değerlendirmedir.")
    rows = [_analyze_offer(offer, request, as_of, deadline) for offer in offers]
    ids = [row["id"] for row in rows]
    duplicate_ids = {item for item in ids if item is None or ids.count(item) > 1}
    messages = [row["email"]["message_id"] for row in rows]
    duplicate_messages = {item for item in messages if item is not None and messages.count(item) > 1}
    for row in rows:
        if row["id"] in duplicate_ids or row["email"]["message_id"] in duplicate_messages:
            row["comparable"] = False
            row["warnings"].append("Teklif kimliği eksik/yinelenmiş veya e-posta birden çok teklif olarak sayılmış; sıralama engellendi.")
    groups = {}
    for row in rows:
        if row["comparable"]:
            groups.setdefault((row["currency"], row["unit"], str(row["quantity"])), []).append(row)
    rankings = []
    for (currency, unit, quantity), group in groups.items():
        ordered = sorted(group, key=lambda row: row["term_total_gross"])
        rankings.append({"currency": currency, "unit": unit, "quantity": quantity,
                         "offer_ids": [row["id"] for row in ordered]})
    if len({row["currency"] for row in rows if row["currency"]}) > 1:
        warnings.append("Farklı para birimleri kur varsayımıyla birleştirilmedi; ayrı gruplarda kıyaslandı.")
    if not required:
        warnings.append("Teknik şartname belirtilmedi; finansal sıralama satın alma önerisi değildir.")
    recommended = None
    if status == "final" and required and len(rankings) == 1 and len(rankings[0]["offer_ids"]) >= 2:
        eligible = [row for row in rows if row["id"] in rankings[0]["offer_ids"]]
        if (len({row["term_days"] for row in eligible}) == 1
                and all(row["term_days"] is not None and row["cash_unit_ex_vat"] is not None and row["term_unit_ex_vat"] is not None
                        and row["vat_basis"] == "including_term_markup" for row in eligible)):
            recommended = rankings[0]["offer_ids"][0]
        else:
            warnings.append("Vadeler/fiyat kapsamı aynı değil, eksik veya KDV matrahı teyit gerektiriyor; düşük brüt toplam tek başına kazanan seçtirmez.")
    return _json_numbers({"request": request, "as_of": payload.get("as_of"), "report_status": status, "sent": False,
                          "offers": rows, "rankings": rankings, "recommended_offer_id": recommended, "warnings": warnings,
                          "monthly_method": "30 gün normalizasyonu; basit oran = toplam fark yüzdesi × 30/vade günü; efektif oran = (vadeli/peşin)^(30/vade günü) − 1"})


def render_case_report(result):
    request = result["request"]
    final = result["report_status"] == "final"
    lines = ["YÖNETİCİ TEKLİF KARŞILAŞTIRMASI — " + ("SÜRE SONU YEREL TASLAK" if final else "ÖN DEĞERLENDİRME"),
             f"Talep: {_escape(request.get('id'))} · Malzeme: {_escape(request.get('material_code'))} / {_escape(request.get('material_name'))}",
             f"Miktar: {_escape(request.get('quantity'))} {_escape(request.get('unit'))} · Son tarih: {_display_instant(request.get('deadline_at'))}",
             f"Rapor zamanı: {_display_instant(result.get('as_of'))} · Gönderim: yapılmadı; yönetici alıcısı henüz belirlenmedi.", "",
             "Fiyatlar yalnız doğrulanan e-posta/ek alıntılarından hesaplandı. Aylık oranlar 30 gün normalizasyonudur; bankacılık faizi veya hukuki vergi teyidi değildir.", "",
             "| Tedarikçi / teklif | Para birimi | Peşin birim (KDV hariç) | Vadeli birim (KDV hariç) | Vade gün | Vade farkı toplam | Toplam fark % | Basit aylık % | Efektif aylık % | KDV % | Vadeli toplam KDV hariç | Vadeli KDV tutarı | Vadeli toplam KDV dahil | Teslim gün | Teknik uygunluk |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for row in result["offers"]:
        cells = [_escape(row.get("supplier")) + " / " + _escape(row.get("id")), _escape(row.get("currency")),
                 _format(row.get("cash_unit_ex_vat")), _format(row.get("term_unit_ex_vat")), _format(row.get("term_days")),
                 _format(row.get("term_markup_amount")), _format(row.get("term_markup_percent"), True),
                 _format(row.get("monthly_simple_percent"), True), _format(row.get("monthly_effective_percent"), True),
                 _format(row.get("vat_rate_pct"), True), _format(row.get("term_total_ex_vat")), _format(row.get("term_vat_total")),
                 _format(row.get("term_total_gross")), _format(row.get("delivery_days")), _escape(row.get("spec_status"))]
        lines.append("| " + " | ".join(cells) + " |")
    if not result["offers"]:
        lines.append("Henüz doğrulanabilir tedarikçi teklif e-postası bulunmuyor.")
    lines += ["", "Karşılaştırma:"]
    for group in result["rankings"]:
        lines.append("- Aynı miktar/birim/para biriminde brüt vadeli toplam sıralaması: " + " → ".join(_escape(item) for item in group["offer_ids"])
                     + f" ({_escape(group['currency'])}, {_escape(group['quantity'])} {_escape(group['unit'])}).")
    if result["recommended_offer_id"]:
        lines.append("- Belirtilen teknik şartları ve aynı vadeyi sağlayan teklifler arasında maliyet incelemesine alınabilecek teklif: "
                     + _escape(result["recommended_offer_id"]) + ". Satın alma onayı/gönderimi yapılmadı.")
    else:
        lines.append("- Kesin tedarikçi seçimi yapılmadı; teknik uygunluk, kaynak, fiyat kapsamı ve vade şartları birlikte değerlendirilmelidir.")
    for warning in result["warnings"]:
        lines.append("- " + _escape(warning))
    for row in result["offers"]:
        email = row["email"]
        lines += ["", f"Tedarikçi: {_escape(row['supplier'])} · Teklif: {_escape(row['id'])}",
                  f"E-posta: {_escape(email['message_id'])} · Gönderen: {_escape(email['sender'])} · Alındı: {_display_instant(email['received_at'])}",
                  f"Konu: {_escape(email['subject'])}"]
        if email["source_url"]:
            lines.append(f"[Kaynak e-postayı aç]({email['source_url']})")
        if row["late"]:
            lines.append("Son tarihten sonra gelen teklif; süre sonu sıralamasına alınmadı.")
        lines.append("E-posta içeriği (alıntı): " + _escape(email["body_excerpt"]))
        lines.append("Teknik özellikler: " + ("; ".join(_escape(key) + ": " + _escape(value) for key, value in row["technical_specs"].items()) or "Kaynakla doğrulanan teknik özellik yok."))
        if row["required_specs_missing"]:
            lines.append("Eksik teknik alanlar: " + ", ".join(_escape(key) for key in row["required_specs_missing"]))
        if row["required_specs_mismatched"]:
            lines.append("Uymayan teknik alanlar: " + ", ".join(_escape(key) for key in row["required_specs_mismatched"]))
        lines.append("KDV matrahı beyanı: " + _escape(row["vat_basis"]) + "; fiyat kapsamı: " + _escape(row["price_basis"]))
        for field, source in row["sources"].items():
            lines.append("- " + _escape(field) + ": “" + _escape(source["quote"]) + "” · " + _escape(source["source_name"])
                         + " · e-posta " + _escape(source["message_id"]))
            if source["source_url"] and source["source_url"] != email["source_url"]:
                lines.append(f"  [Kaynak eki aç]({source['source_url']})")
        lines.extend("- " + _escape(warning) for warning in row["warnings"])
    return "\n".join(lines)


class SupplierQuoteComparisonComponent(Component):
    display_name = "Tedarikçi Teklif Karşılaştırması"
    description = "E-posta ve ek kaynaklarını doğrular; teknik özellik, vade farkı, aylık oran ve KDV beyanlarını yerel hesapla karşılaştırır."
    name = "SupplierQuoteComparison"
    icon = "Table"
    inputs = [MessageTextInput(name="case_data", display_name="Teklif Dosyası", required=True)]
    outputs = [Output(display_name="Yönetici Raporu", name="report", method="build_report")]

    def build_report(self) -> Message:
        try:
            text = _text(self.case_data)
            payload = json.loads(text.rsplit(MARKER, 1)[1]) if MARKER in text else None
            if isinstance(payload, dict) and "cases" in payload:
                cases = payload["cases"]
                if not isinstance(cases, list) or not 1 <= len(cases) <= 10:
                    raise ValueError("SAP dosyası 1–10 ayrı malzeme talebi içermeli.")
                results = [analyze_case(case) for case in cases]
                ids = [item["request"].get("id") for item in results]
                if any(not value for value in ids) or len(set(ids)) != len(ids):
                    raise ValueError("Her SAP kaleminin ayrı talep kimliği gerekli.")
                result = {"cases": results, "sent": False,
                          "report_status": "final" if all(item["report_status"] == "final" for item in results) else "preliminary"}
                report = "\n\n---\n\n".join(render_case_report(item) for item in results)
            else:
                result = analyze_case(self.case_data)
                report = render_case_report(result)
            self.status = "Süre sonu yerel taslak" if result["report_status"] == "final" else "Ön değerlendirme"
            return Message(text=report + "\n\n" + RESULT_MARKER + json.dumps(result, ensure_ascii=False))
        except (ValueError, TypeError, KeyError) as error:
            self.status = "Teklif dosyası doğrulanamadı"
            return Message(text="Teklif karşılaştırması hazırlanamadı: " + str(error))
