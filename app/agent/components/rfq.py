"""Source-scoped RFQ drafts for existing and new procurement suppliers.

This module is deliberately pure: it does not read the request database, open a
mailbox, visit evidence URLs, send messages or place orders. ``collecting`` is
the authoritative CaseStore state after the request owner confirms a deadline;
raw intake states do not open this branch.
"""

import hashlib
import ipaddress
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from lfx.custom import Component
from lfx.io import MessageTextInput, MultilineInput, Output
from lfx.schema.message import Message

RFQ_CASE_MARKER = "TEKLİF DOSYASI (JSON)\n"
RFQ_PLAN_MARKER = "TEKLİF TALEBİ PLANI (JSON)\n"
RFQ_INPUT_MAXIMUM = 200_000
RFQ_SUPPLIER_MAXIMUM = 50
RFQ_EMAIL = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}"
)
RFQ_CONTROL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]")
RFQ_URI = re.compile(r"(?:[A-Za-z][A-Za-z0-9+.-]*://|(?:javascript|data|mailto):)", re.IGNORECASE)


def _rfq_unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON aynı alan adını birden çok kez içeremez.")
        result[key] = value
    return result


def _rfq_json_check(value, depth=0):
    if depth > 12:
        raise ValueError("Teklif talebi girdisinin iç içe yapısı çok derin.")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON alan adları metin olmalı.")
            _rfq_json_check(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _rfq_json_check(item, depth + 1)


def _rfq_payload(value, marker=None):
    if isinstance(value, Message):
        value = value.text
    if isinstance(value, str):
        if len(value.encode("utf-8")) > RFQ_INPUT_MAXIMUM:
            raise ValueError("Teklif talebi girdisi 200 KB sınırını aşıyor.")
        value = value.strip()
        if marker and value.startswith(marker):
            value = value[len(marker):]
        try:
            value = json.loads(value, object_pairs_hook=_rfq_unique_object,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Sonlu JSON sayısı gerekli.")))
        except (ValueError, RecursionError):
            raise ValueError("Teklif talebi girdisi geçerli JSON olmalı.") from None
    _rfq_json_check(value)
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("Teklif talebi yalnız sonlu sayılar ve JSON alanları içermeli.") from None
    if len(raw.encode("utf-8")) > RFQ_INPUT_MAXIMUM:
        raise ValueError("Teklif talebi girdisi 200 KB sınırını aşıyor.")
    return value


def _rfq_text(value, label, maximum=300, *, optional=False, allow_uri=False):
    if optional and (value is None or value == ""):
        return None
    if not isinstance(value, str) or len(value) > maximum or RFQ_CONTROL.search(value):
        raise ValueError(f"{label} geçerli, tek satırlı bir metin olmalı.")
    value = value.strip()
    if not value or (not allow_uri and RFQ_URI.search(value)):
        raise ValueError(f"{label} boş olamaz veya çalıştırılabilir bağlantı içeremez.")
    return value


def _rfq_email(value, label):
    address = _rfq_text(value, label, 254)
    if not RFQ_EMAIL.fullmatch(address):
        raise ValueError(f"{label} geçerli bir e-posta adresi olmalı.")
    local, domain = address.rsplit("@", 1)
    if (len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local
            or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                   for part in domain.split("."))):
        raise ValueError(f"{label} geçerli bir e-posta adresi olmalı.")
    return local + "@" + domain.lower()


def _rfq_instant(value, label):
    if isinstance(value, str):
        value = _rfq_text(value, label, 80)
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"{label} ISO tarih, saat ve saat dilimi içermeli.") from None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{label} ISO tarih, saat ve saat dilimi içermeli.")
    return value.astimezone(timezone.utc)


def _rfq_quantity(value):
    raw = str(value).strip()
    if isinstance(value, bool) or len(raw) > 32 or not re.fullmatch(r"\d+(?:[.,]\d{1,6})?", raw):
        raise ValueError("Miktar sıfırdan büyük ve en çok 6 ondalıklı olmalı.")
    try:
        number = Decimal(raw.replace(",", "."))
    except InvalidOperation:
        raise ValueError("Miktar geçersiz.") from None
    if not number.is_finite() or not 0 < number <= Decimal("1e12"):
        raise ValueError("Miktar sıfırdan büyük ve sınır içinde olmalı.")
    return format(number.normalize(), "f")


def _rfq_specs(value):
    if not isinstance(value, dict) or len(value) > 30:
        raise ValueError("Teknik şartname açıkça JSON nesnesi olarak belirtilmeli; yoksa {} kullanın.")
    result = {}
    for key, item in value.items():
        name = _rfq_text(key, "Teknik özellik adı", 100)
        if not isinstance(item, (str, int, float, bool)) or item is None:
            raise ValueError("Teknik özellik değerleri tek satırlı metin veya sonlu sayı olmalı.")
        if isinstance(item, float) and not Decimal(str(item)).is_finite():
            raise ValueError("Teknik özellik sayısı sonlu olmalı.")
        if name in result:
            raise ValueError("Teknik özellik adları benzersiz olmalı.")
        result[name] = _rfq_text(str(item), "Teknik özellik değeri", 500)
    return dict(sorted(result.items()))


def _rfq_public_https(value):
    url = _rfq_text(value, "Yeni tedarikçinin kaynak URL'si", 2000, allow_uri=True)
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("Yeni tedarikçi için public HTTPS kaynak URL'si gerekli.") from None
    if (parsed.scheme != "https" or not hostname or parsed.username or parsed.password
            or port not in (None, 443) or "\\" in url or " " in url
            or hostname.casefold().endswith((".local", ".localhost", ".internal", ".invalid", ".test"))
            or hostname.casefold() == "localhost"):
        raise ValueError("Yeni tedarikçi için public HTTPS kaynak URL'si gerekli.")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        labels = hostname.split(".")
        if (len(labels) < 2 or not re.fullmatch(r"[A-Za-z]{2,63}", labels[-1])
                or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part) for part in labels)):
            raise ValueError("Yeni tedarikçi için public HTTPS kaynak URL'si gerekli.") from None
    else:
        if not address.is_global:
            raise ValueError("Yeni tedarikçinin kaynak URL'si özel ağ adresi olamaz.")
    return url


def _rfq_scope(request):
    scope = {
        "material_code": _rfq_text(request.get("material_code"), "Malzeme kodu", 100),
        "material_name": _rfq_text(request.get("material_name"), "Malzeme adı", 300),
        "quantity": _rfq_quantity(request.get("quantity")),
        "unit": _rfq_text(request.get("unit"), "Birim", 32),
        "required_specs": _rfq_specs(request.get("required_specs")),
    }
    delivery = _rfq_text(request.get("delivery_place"), "Teslim yeri", 500, optional=True)
    if delivery:
        scope["delivery_place"] = delivery
    need_by = _rfq_text(request.get("need_by"), "İhtiyaç tarihi", 80, optional=True)
    if need_by:
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", need_by):
                date.fromisoformat(need_by)
            else:
                _rfq_instant(need_by, "İhtiyaç tarihi")
        except ValueError:
            raise ValueError("İhtiyaç tarihi ISO biçiminde; saat varsa saat dilimli olmalı.") from None
        scope["need_by"] = need_by
    return scope


def _rfq_hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _rfq_firm_key(value):
    # Unicode casefold of Turkish capital İ includes a combining dot.
    return " ".join(value.casefold().replace("\u0307", "").split())


def _rfq_supplier(row):
    if not isinstance(row, dict):
        raise ValueError("Tedarikçi kaydı JSON nesnesi olmalı.")
    identifier = _rfq_text(row.get("supplier_id"), "Tedarikçi kimliği", 100)
    name = _rfq_text(row.get("name"), "Tedarikçi adı", 300)
    email = _rfq_email(row.get("email"), "Tedarikçi e-postası")
    kind = row.get("kind")
    if kind not in {"existing", "new"}:
        raise ValueError("Tedarikçi türü existing veya new olmalı.")
    codes = row.get("material_codes")
    if not isinstance(codes, list) or not 1 <= len(codes) <= 100:
        raise ValueError("Tedarikçinin açık malzeme kodu listesi gerekli.")
    codes = [_rfq_text(code, "Tedarikçi malzeme kodu", 100) for code in codes]
    if len(set(codes)) != len(codes):
        raise ValueError("Tedarikçi malzeme kodları tekrarlanmamalı.")
    if row.get("contact_confirmed") is not True:
        raise ValueError("Tedarikçi iletişim adresi kullanıcı tarafından teyit edilmeli.")
    if kind == "new":
        _rfq_public_https(row.get("source_url"))
    return {"supplier_id": identifier, "name": name, "email": email, "kind": kind, "material_codes": codes}


def _rfq_body(scope, deadline, rfq_id):
    local_deadline = _rfq_instant(deadline, "Araştırma bitişi").astimezone(ZoneInfo("Europe/Istanbul"))
    deadline_label = local_deadline.strftime("%d.%m.%Y %H:%M:%S") + " (Türkiye saati, UTC+03:00)"
    lines = [
        "Merhaba,", "",
        "Aşağıdaki malzeme için fiyat ve teslim koşullarınızı paylaşabilir misiniz?", "",
        f"Teklif talebi referansı: {rfq_id}",
        f"Malzeme: {scope['material_name']}",
        f"Malzeme kodu: {scope['material_code']}",
        f"Miktar: {scope['quantity']} {scope['unit']}",
    ]
    if scope["required_specs"]:
        lines.append("İstenen teknik özellikler:")
        lines.extend(f"  • {key}: {value}" for key, value in scope["required_specs"].items())
    else:
        lines.append("Teknik şartname henüz belirtilmedi; ürününüzün teknik özelliklerini ve dokümanını paylaşmanızı rica ederiz.")
    if scope.get("delivery_place"):
        lines.append("Teslim yeri: " + scope["delivery_place"])
    if scope.get("need_by"):
        lines.append("İhtiyaç tarihi: " + scope["need_by"])
    lines.extend([
        f"Teklifinizi en geç {deadline_label} tarih ve saatine kadar iletmenizi rica ederiz.",
        "", "Karşılaştırmayı aynı kapsamda yapabilmek için teklifinizde şunları ayrı ayrı belirtir misiniz?",
        "• Peşin birim fiyatı, varsa vadeli birim fiyatı, fiyatın hangi miktar/birim için olduğu ve para birimi.",
        "• Ödeme vadesi ve gün sayısı; varsa vade farkı tutarı/yüzdesi ve aylık oranı ile hesaplama yöntemi.",
        "• KDV oranı, KDV matrahı, fiyatın KDV dahil/hariç olduğu ve vade farkının KDV'ye etkisi.",
        "• Teklifin geçerlilik tarihi, minimum sipariş miktarı ve stok/üretim uygunluğu.",
        "• Teslim süresi, teslim şekli, navlun/kargo bedeli ve bu bedelin fiyata dahil olup olmadığı.",
        "• Teknik özellikler, şartnameye uygunluk veya sapmalar ve erişilebilir teknik doküman/kalite belgeleri.",
        "", "Bu ileti fiyat ve koşul talebidir; sipariş veya ödeme taahhüdü içermez.",
        "Teşekkürler, iyi çalışmalar.",
    ])
    return "\n".join(lines)


def build_rfq_plan(case_data, suppliers, now=None):
    """Build independent draft messages using only an owner-approved case scope.

    ``suppliers`` is an explicit registry, never scraped contact data. A new
    supplier needs a syntactically public HTTPS source and an operator-confirmed
    contact; neither is presented as a live network verification.
    """
    result = {"schema_version": 1, "request_id": None, "scope_hash": None, "deadline_at": None,
              "status": "blocked", "drafts": [], "blocked_suppliers": [], "warnings": [], "outbound_sent": False}
    try:
        payload = _rfq_payload(case_data, RFQ_CASE_MARKER)
        if not isinstance(payload, dict) or "cases" in payload or not isinstance(payload.get("request"), dict):
            raise ValueError("Teklif talebi planı tek bir kayıtlı talep dosyası içermeli.")
        request = payload["request"]
        result["request_id"] = _rfq_text(request.get("id"), "Talep kimliği", 200)
        if request.get("state") != "collecting":
            raise ValueError("Teklif talebi için sahibinin onayladığı süresi devam eden collecting kaydı gerekli.")
        owner = _rfq_email(request.get("requester"), "Talep sahibi")
        if request.get("deadline_confirmed") is False or request.get("owner_confirmed") is False:
            raise ValueError("Araştırma süresi talep sahibi tarafından onaylanmalı.")
        if request.get("deadline_confirmed_by") is not None and _rfq_email(request["deadline_confirmed_by"], "Süreyi onaylayan") != owner:
            raise ValueError("Süreyi yalnız kayıtlı talep sahibi onaylayabilir.")
        instant = _rfq_instant(datetime.now(timezone.utc) if now is None else now, "Referans zamanı")
        deadline = _rfq_instant(request.get("deadline_at"), "Araştırma bitişi")
        result["deadline_at"] = deadline.isoformat()
        if deadline <= instant:
            raise ValueError("Araştırma süresi dolmuş; yeni teklif talebi taslağı hazırlanmadı.")
        scope = _rfq_scope(request)
        result["scope_hash"] = _rfq_hash(scope)
        rows = _rfq_payload(suppliers)
        if not isinstance(rows, list) or len(rows) > RFQ_SUPPLIER_MAXIMUM:
            raise ValueError("Tedarikçi kaydı en çok 50 satırlı JSON listesi olmalı.")
    except (ValueError, TypeError, OverflowError, RecursionError) as error:
        result["warnings"].append(str(error))
        return result
    if not rows:
        result["warnings"].append("Teklif istenecek mevcut veya yeni tedarikçi seçilmedi.")
        return result
    parsed = []
    for row in rows:
        raw_id = row.get("supplier_id") if isinstance(row, dict) else None
        safe_id = raw_id if isinstance(raw_id, str) and len(raw_id) <= 100 and not RFQ_CONTROL.search(raw_id) else None
        try:
            parsed.append((_rfq_supplier(row), None))
        except (ValueError, TypeError, OverflowError) as error:
            parsed.append((None, {"supplier_id": safe_id, "reason": str(error)}))
    # Duplicates remain ambiguous even when one copy has another invalid field.
    # Do not select the first valid-looking contact from a conflicting registry.
    ids, emails, names = [], [], []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field, destination in (("supplier_id", ids), ("name", names)):
            value = row.get(field)
            if isinstance(value, str) and value.strip() and not RFQ_CONTROL.search(value):
                destination.append(_rfq_firm_key(value) if field == "name" else value.strip().casefold())
        try:
            emails.append(_rfq_email(row.get("email"), "Tedarikçi e-postası").casefold())
        except (ValueError, TypeError):
            pass
    for supplier, error in parsed:
        if error:
            result["blocked_suppliers"].append(error)
            continue
        identity = supplier["supplier_id"]
        if (ids.count(identity.casefold()) > 1 or emails.count(supplier["email"].casefold()) > 1
                or names.count(_rfq_firm_key(supplier["name"])) > 1):
            result["blocked_suppliers"].append({"supplier_id": identity, "reason": "Aynı tedarikçi kimliği, e-posta veya firma adı birden çok satırda; tek firma olarak netleştirin."})
            continue
        if scope["material_code"] not in supplier["material_codes"]:
            result["blocked_suppliers"].append({"supplier_id": identity, "reason": "Tedarikçinin malzeme kapsamı bu kodu açıkça içermiyor."})
            continue
        rfq_id = "RFQ-" + _rfq_hash([result["request_id"], identity, result["scope_hash"]])[:24]
        draft = {"rfq_id": rfq_id, "request_id": result["request_id"], "supplier_id": identity,
                 "supplier_name": supplier["name"], "recipient": supplier["email"], "kind": supplier["kind"],
                 "scope_hash": result["scope_hash"], **scope, "deadline_at": result["deadline_at"],
                 "subject": f"Teklif talebi: {scope['material_name']} [{rfq_id}]",
                 "body": _rfq_body(scope, result["deadline_at"], rfq_id), "sent": False, "draft_only": True}
        result["drafts"].append(draft)
    if any(supplier and supplier["kind"] == "new" for supplier, _ in parsed):
        result["warnings"].append("Yeni tedarikçi kaynak adresleri yalnız biçim/kapsam açısından kontrol edildi; web veya posta erişimi doğrulanmadı.")
    if not scope["required_specs"]:
        result["warnings"].append("Teknik şartname belirtilmedi; teknik uygunluk henüz değerlendirilemez.")
    if result["drafts"]:
        result["status"] = "partial" if result["blocked_suppliers"] else "ready"
    return result


def render_rfq_report(plan):
    """Human-readable local draft preview; no JSON internals or active links."""
    lines = ["TEDARİKÇİ TEKLİF TALEPLERİ", "Yerel taslaklar; hiçbir ileti gönderilmedi.", ""]
    for draft in plan.get("drafts", []):
        lines.extend([f"Tedarikçi: {draft['supplier_name']} ({'mevcut' if draft['kind'] == 'existing' else 'yeni aday'})",
                      f"Alıcı: {draft['recipient']}", f"Konu: {draft['subject']}", "", draft["body"], "", "—" * 36, ""])
    if not plan.get("drafts"):
        lines.append("Hazırlanmış teklif talebi taslağı yok.")
    for item in plan.get("blocked_suppliers", []):
        lines.append(f"Bekleyen tedarikçi {item.get('supplier_id') or '(kimlik eksik)'}: {item['reason']}")
    lines.extend("Not: " + warning for warning in plan.get("warnings", []))
    return "\n".join(lines).strip()


class SupplierRFQComponent(Component):
    display_name = "Tedarikçilerden Teklif İste"
    description = "Mevcut ve yeni firmalar için aynı şartlarla ayrı fiyat talebi taslakları hazırlar; ileti göndermez."
    name = "SupplierRFQ"
    icon = "Send"
    inputs = [
        MessageTextInput(name="case_data", display_name="Süreli talep dosyası", required=True),
        MultilineInput(name="supplier_registry", display_name="Seçilen tedarikçiler (JSON)", value="[]",
                       info="Mevcut/yeni firma, teyitli e-posta ve açık malzeme kodları. Yeni firma için public HTTPS kaynak adresi gerekir."),
    ]
    outputs = [Output(name="plan", display_name="Teklif talebi planı", method="build_plan", group_outputs=True),
               Output(name="draft_report", display_name="Teklif isteği taslakları", method="build_draft_report", group_outputs=True)]

    def _prepare(self):
        if not hasattr(self, "_rfq_prepared"):
            self._rfq_prepared = build_rfq_plan(self.case_data, self.supplier_registry)
            self.status = f"{len(self._rfq_prepared['drafts'])} teklif talebi taslağı; gönderilmedi."
        return self._rfq_prepared

    def build_plan(self) -> Message:
        return Message(text=RFQ_PLAN_MARKER + json.dumps(self._prepare(), ensure_ascii=False, allow_nan=False))

    def build_draft_report(self) -> Message:
        return Message(text=render_rfq_report(self._prepare()))
