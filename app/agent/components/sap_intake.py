"""Read-only SAP/OData intake. No connection or write is enabled by default."""

import hashlib
import ipaddress
import json
import re
import socket
import ssl
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlparse

from lfx.custom import Component
from lfx.io import BoolInput, DropdownInput, MessageTextInput, MultilineInput, Output, SecretStrInput
from lfx.schema.message import Message

MARKER = "TALEP GİRİŞİ (JSON)\n"
MAX_BYTES = 1_048_576
DEFAULT_FIELD_MAP = {
    "material_code": "Material", "name": "MaterialDescription", "unit": "BaseUnit",
    "quantity": "Quantity", "stock": "UnrestrictedStock", "reserved_stock": "ReservedStock",
    "daily_consumption": "DailyConsumption", "lead_time_days": "LeadTimeDays",
    "safety_stock": "SafetyStock", "open_order_quantity": "OpenOrderQuantity",
    "open_order_delivery_date": "OpenOrderDeliveryDate",
}
NUMBER_FIELDS = {
    "quantity", "stock", "reserved_stock", "daily_consumption", "lead_time_days",
    "safety_stock", "open_order_quantity",
}
PR_FIELD_MAP = {
    "purchase_requisition": "PurchaseRequisition",
    "purchase_requisition_item": "PurchaseRequisitionItem",
    "material_code": "Material", "name": "PurchaseRequisitionItemText",
    "quantity": "RequestedQuantity", "ordered_quantity": "OrderedQuantity", "unit": "BaseUnit",
    # These are organization-specific fields. They must be mapped explicitly;
    # SAP user names and release-status codes are never treated as email/bools.
    "requester_email": None, "deleted": None, "closed": None, "blocked": None, "released": None,
}


def _pr_string(value, label, maximum=300, *, required=False):
    if value is None or value == "":
        if required:
            raise ValueError(label + " gerekli")
        return ""
    if not isinstance(value, str) or len(value) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(label + " tek satırlı metin olmalı")
    value = value.strip()
    if required and not value:
        raise ValueError(label + " gerekli")
    return value


def _pr_identity(value, label):
    value = _pr_string(value, label, 40, required=True)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise ValueError(label + " açık belge/kalem kimliği olmalı")
    return value


def _pr_email(value):
    value = _pr_string(value, "Talep sahibi e-postası", 254)
    if not value:
        return None
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value):
        raise ValueError("Talep sahibi için açık ve geçerli e-posta adresi gerekli")
    local, domain = value.rsplit("@", 1)
    if (len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local
            or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in domain.split("."))):
        raise ValueError("Talep sahibi için açık ve geçerli e-posta adresi gerekli")
    return local + "@" + domain.lower()


def _pr_unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("SAP PR JSON alanları yinelenmemeli")
        result[key] = value
    return result


def _pr_payload(payload):
    if isinstance(payload, str):
        if len(payload.encode("utf-8")) > MAX_BYTES:
            raise ValueError("SAP PR yanıtı boyut sınırını aşıyor")
        payload = json.loads(payload, object_pairs_hook=_pr_unique_object)
    if not isinstance(payload, dict):
        raise ValueError("SAP PR yanıtı JSON nesnesi olmalı")
    if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_BYTES:
        raise ValueError("SAP PR yanıtı boyut sınırını aşıyor")
    v2 = payload.get("d")
    if "value" in payload and isinstance(v2, dict) and "results" in v2:
        raise ValueError("SAP PR yanıtı tek ve açık OData koleksiyonu içermeli")
    rows = payload.get("value") if "value" in payload else v2.get("results") if isinstance(v2, dict) else None
    if not isinstance(rows, list) or not rows or len(rows) > 200 or any(not isinstance(row, dict) for row in rows):
        raise ValueError("SAP PR koleksiyonu 1–200 nesne satırı içermeli")
    paginated = bool(payload.get("@odata.nextLink") or (v2.get("__next") if isinstance(v2, dict) else False))
    return rows, paginated


def _pr_mapping(field_map):
    if isinstance(field_map, str):
        if len(field_map.encode("utf-8")) > 20_000:
            raise ValueError("SAP PR alan eşlemesi çok uzun")
        field_map = json.loads(field_map, object_pairs_hook=_pr_unique_object) if field_map.strip() else {}
    if field_map is not None and not isinstance(field_map, dict):
        raise ValueError("SAP PR alan eşlemesi JSON nesnesi olmalı")
    mapping = dict(PR_FIELD_MAP)
    for key, value in (field_map or {}).items():
        if key not in mapping:
            raise ValueError("SAP PR alan eşlemesi bilinmeyen alan içeriyor")
        mapping[key] = _pr_string(value, "SAP PR kaynak alan adı", 100, required=True)
    return mapping


def normalize_sap_purchase_requisitions(payload, field_map=None, *, source_system="", purchase_requisition="",
                                        purchase_requisition_item="", requester="", deadline_at="", now=None):
    """Select one explicit PR+item; unknown eligibility never opens purchasing.

    The namespace is a configured logical system/client alias. Stable identity
    depends only on that namespace and the string PR/item keys, not mutable
    quantity, status or descriptive fields. This function performs no I/O.
    """
    result = {"source": "sap_api", "data_kind": "purchase_requisitions", "status": "invalid",
              "materials": [], "request": {}, "requisitions": [], "errors": [], "warnings": [],
              "sap_connectivity_verified": False, "outbound_sent": False}
    try:
        now = now or datetime.now(timezone.utc)
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise ValueError("Referans zamanı saat dilimi içermeli")
        namespace = _pr_string(source_system, "SAP mantıksal sistem/istemci alanı", 100, required=True)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", namespace):
            raise ValueError("SAP mantıksal sistem/istemci alanı açık bir takma ad olmalı")
        rows, paginated = _pr_payload(payload)
        mapping = _pr_mapping(field_map)
        if paginated:
            result["warnings"].append("SAP yanıtı sayfalı; bu liste tüm satın alma taleplerinin tamamı olarak doğrulanmadı.")
        normalized, owners, identities = [], [], set()
        for row in rows:
            pr = _pr_identity(row.get(mapping["purchase_requisition"]), "Satın alma talebi numarası")
            item_id = _pr_identity(row.get(mapping["purchase_requisition_item"]), "Satın alma talebi kalemi")
            if (pr, item_id) in identities:
                raise ValueError("Aynı SAP satın alma talebi ve kalemi birden çok satırda")
            identities.add((pr, item_id))
            stable_key = json.dumps([namespace, pr, item_id], ensure_ascii=False, separators=(",", ":"))
            digest = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()
            code = _pr_string(row.get(mapping["material_code"]), "Malzeme kodu", 100)
            warnings = []
            quantity = _sap_number(row.get(mapping["quantity"]), "RequestedQuantity", warnings)
            ordered_quantity = _sap_number(row.get(mapping["ordered_quantity"]), "OrderedQuantity", warnings)
            if ordered_quantity is None:
                warnings.append("Siparişe dönüşen miktar açık, sonlu ve sıfırdan küçük olmayan sayı olarak doğrulanmadı; inceleme gerekli.")
            elif Decimal(ordered_quantity) > 0:
                warnings.append("Talep kısmen/tam siparişe dönüşmüş; kalan miktar teyidi gerekli. Kalan miktar otomatik hesaplanmadı.")
            if quantity is not None and Decimal(quantity) <= 0:
                quantity = None
                warnings.append("Talep miktarı sıfırdan büyük olmalı; açık/sipariş miktarı yerine kullanılmadı.")
            flags = {key: row.get(mapping[key]) if mapping[key] else None for key in ("deleted", "closed", "blocked", "released")}
            flags = {key: value if isinstance(value, bool) else None for key, value in flags.items()}
            if flags["deleted"] is True or flags["closed"] is True or flags["blocked"] is True or flags["released"] is False:
                eligibility = "blocked"
                warnings.append("Kaynak silinmiş, kapalı, blokajlı veya onaylanmamış talep bildiriyor; satın alma araştırması açılmadı.")
            elif flags == {"deleted": False, "closed": False, "blocked": False, "released": True} and ordered_quantity == "0":
                eligibility = "eligible"
            else:
                eligibility = "unverified"
                if flags != {"deleted": False, "closed": False, "blocked": False, "released": True}:
                    warnings.append("Silinme, kapanma, blokaj veya onay durumu kaynakta boolean olarak doğrulanmadı; inceleme gerekli.")
            normalized.append({"purchase_requisition": pr, "purchase_requisition_item": item_id,
                               "material_code": code, "material_code_missing": not bool(code),
                               "material_name": _pr_string(row.get(mapping["name"]), "Talep kalemi açıklaması", 300),
                               "quantity": quantity, "ordered_quantity": ordered_quantity,
                               "unit": _pr_string(row.get(mapping["unit"]), "Talep birimi", 32),
                               "eligibility": {"status": eligibility, **flags}, "warnings": warnings,
                               "request_id": "SAPPR-" + digest[:24]})
            # Never infer an email address from CreatedByUser, RequestedByUser,
            # DeliveryDate or another unconfigured source field.
            owners.append(row.get(mapping["requester_email"]) if mapping["requester_email"] else None)
        result["requisitions"] = normalized
        selected_pr = _pr_string(purchase_requisition, "Seçilen satın alma talebi", 40)
        selected_item = _pr_string(purchase_requisition_item, "Seçilen talep kalemi", 40)
        if not selected_pr or not selected_item:
            result.update(status="awaiting_selection")
            result["warnings"].append("Araştırılacak satın alma talebi numarası ve kalemi açıkça birlikte seçilmeli; hiçbir kalem otomatik birleştirilmedi.")
            return result
        selected_pr, selected_item = _pr_identity(selected_pr, "Seçilen satın alma talebi"), _pr_identity(selected_item, "Seçilen talep kalemi")
        matches = [index for index, row in enumerate(normalized)
                   if (row["purchase_requisition"], row["purchase_requisition_item"]) == (selected_pr, selected_item)]
        if len(matches) != 1:
            raise ValueError("Seçilen satın alma talebi ve kalemi bu yanıtta bulunamadı")
        index = matches[0]
        selected = normalized[index]
        result["selected_requisition"] = selected
        result["warnings"].extend(selected["warnings"])
        if selected["eligibility"]["status"] != "eligible":
            result["status"] = "blocked" if selected["eligibility"]["status"] == "blocked" else "awaiting_review"
            return result
        if not selected["material_code"]:
            result["status"] = "awaiting_review"
            result["warnings"].append("SAP malzeme kodu yok; serbest metin kalemi insan incelemesi gerektirir. İç malzeme kodu üretilmedi.")
            return result
        if not selected["material_name"] or not selected["unit"] or selected["quantity"] is None:
            raise ValueError("Seçilen talebin açıklaması, pozitif istenen miktarı ve birimi açık olmalı")
        owner = _pr_email(owners[index] if owners[index] not in (None, "") else requester)
        if owner is None:
            result["status"] = "awaiting_requester"
            result["warnings"].append("Talep sahibinin açık e-posta adresi eşlenmeli veya mevcut kişi alanına yazılmalı; SAP kullanıcı adından adres üretilmedi.")
            return result
        deadline, state, deadline_error = _sap_deadline(deadline_at, now)
        material = {"material_code": selected["material_code"], "name": selected["material_name"],
                    "quantity": selected["quantity"], "unit": selected["unit"], "required_specs": {}}
        request = {"id": selected["request_id"], "requester": owner, "deadline_at": deadline, "state": state,
                   "material_code": material["material_code"], "material_name": material["name"],
                   "quantity": material["quantity"], "unit": material["unit"], "required_specs": {},
                   "sap_requisition": {"source_system": namespace, "purchase_requisition": selected_pr,
                                       "purchase_requisition_item": selected_item}}
        result.update(status="invalid" if deadline_error else "ready", materials=[material], request=request)
        if deadline_error:
            result["errors"].append(deadline_error)
        result["warnings"].append("SAP teslim/oluşturma tarihleri araştırma bitiş süresi değildir; kişi tarafından ayrıca belirlenir.")
    except (ValueError, TypeError, OverflowError, RecursionError):
        result["status"] = "invalid"
        result["materials"], result["request"] = [], {}
        result["errors"].append("SAP satın alma talebi kimliği, alan eşlemesi veya seçilen kalem verileri doğrulanamadı; giriş düzeltilmeli.")
    return result


def _sap_text(value):
    return value.text if isinstance(value, Message) else str(value or "")


def _sap_string(value, limit=300):
    if value is None:
        return ""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError("Metin alanı beklenen biçimde değil")
    text = str(value).strip()
    if len(text) > limit:
        raise ValueError("Metin alanı uzunluk sınırını aşıyor")
    return text


def _sap_number(value, field, warnings):
    if value is None or value == "":
        warnings.append(f"{field}: veri eksik; sıfır kabul edilmedi")
        return None
    raw = str(value).strip()
    if isinstance(value, bool) or len(raw) > 32 or not re.fullmatch(r"\+?\d+(?:[.,]\d{1,6})?", raw):
        warnings.append(f"{field}: geçersiz sayı")
        return None
    try:
        amount = Decimal(raw.replace(",", "."))
    except InvalidOperation:
        amount = Decimal("NaN")
    if not amount.is_finite() or amount > Decimal("1e12"):
        warnings.append(f"{field}: sayı sınır dışında")
        return None
    if field == "lead_time_days" and (amount != amount.to_integral_value() or amount > 3650):
        warnings.append("lead_time_days: 0–3650 aralığında tam gün olmalı")
        return None
    return format(amount.normalize(), "f")


def _sap_deadline(value, now):
    if not value:
        return None, "awaiting_deadline", None
    try:
        text = _sap_string(value, 80)
        date = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if date.utcoffset() is None or date <= now:
            raise ValueError
    except (ValueError, TypeError):
        return None, "invalid", "Bitiş tarihi saat dilimiyle birlikte gelecekte olmalı"
    return date.isoformat(), "research_ready", None


def _sap_public_target(endpoint):
    """Reject credentials, redirects, private hosts and credential-bearing query fields."""
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 2000:
        raise ValueError("SAP API adresi eksik veya geçersiz")
    try:
        parsed = urlparse(endpoint)
        host, port = parsed.hostname, parsed.port
        if (parsed.scheme != "https" or not host or parsed.username or parsed.password
                or port not in {None, 443} or parsed.fragment or host.casefold() == "localhost"
                or host.casefold().endswith((".local", ".localhost"))):
            raise ValueError
        forbidden = {"apikey", "api_key", "key", "token", "access_token", "password", "secret"}
        if any(key.casefold() in forbidden for key, _ in parse_qsl(parsed.query)):
            raise ValueError
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            addresses = [ipaddress.ip_address(item[4][0].split("%")[0])
                         for item in socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)]
        if not addresses or any(not address.is_global for address in addresses):
            raise ValueError
    except (ValueError, OSError):
        raise ValueError("SAP API adresi public HTTPS olmalı; yerel/özel ağ ve adres içi sır kabul edilmez") from None
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return endpoint, host, str(addresses[0]), path


def _sap_public_endpoint(endpoint):
    return _sap_public_target(endpoint)[0]


def fetch_sap_payload(endpoint, auth_mode="None", bearer_token=None):
    """One bounded GET. Secrets and raw HTTP errors never leave this function."""
    _, host, address, path = _sap_public_target(endpoint)
    if auth_mode not in {"None", "Bearer"}:
        raise ValueError("SAP kimlik doğrulama yöntemi geçersiz")
    headers = {"Accept": "application/json"}
    if auth_mode == "Bearer":
        key = bearer_token.get_secret_value() if hasattr(bearer_token, "get_secret_value") else bearer_token
        if not isinstance(key, str) or not key.strip() or "\n" in key or "\r" in key:
            raise ValueError("SAP bağlantı anahtarı güvenli global değişkenden yüklenemedi")
        headers["Authorization"] = "Bearer " + key.strip()
    import urllib3

    # Pin the checked public IP while verifying TLS against the original host.
    # A second DNS answer cannot redirect the authenticated GET to a private host.
    headers["Host"] = f"[{host}]" if ":" in host else host
    pool = urllib3.HTTPSConnectionPool(
        address, 443, ssl_context=ssl.create_default_context(), server_hostname=host,
        assert_hostname=host, retries=False, timeout=urllib3.Timeout(total=8, connect=8, read=8),
    )
    response = None
    try:
        started = time.monotonic()
        response = pool.urlopen("GET", path, headers=headers, redirect=False, preload_content=False)
        if response.status != 200 or int(response.headers.get("Content-Length", "0")) > MAX_BYTES:
            raise ValueError
        content = bytearray()
        for chunk in response.stream(16_384, decode_content=True):
            content.extend(chunk)
            if len(content) > MAX_BYTES or time.monotonic() - started > 8:
                raise ValueError
        return json.loads(content.decode("utf-8"), object_pairs_hook=_pr_unique_object)
    except Exception:
        raise ValueError("SAP API okunamadı; bağlantı/yetki veya yanıt sınırı kontrol edilmeli") from None
    finally:
        if response is not None:
            response.close()
        pool.close()


def normalize_sap_payload(payload, field_map=None, *, requester="", deadline_at="", now=None):
    """Preserve absent numbers, flag duplicate keys; never interpret source prose."""
    now = now or datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError("Referans zamanı saat dilimi içermeli")
    result = {"source": "sap_api", "status": "invalid", "materials": [], "errors": [], "warnings": [],
              "request": {"id": None, "requester": requester or None, "deadline_at": None,
                          "state": "awaiting_deadline", "required_specs": {}},
              "sap_connectivity_verified": False}
    try:
        if isinstance(payload, str):
            if len(payload.encode("utf-8")) > MAX_BYTES:
                raise ValueError("SAP yanıtı boyut sınırını aşıyor")
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise ValueError("SAP yanıtı bir JSON nesnesi olmalı")
        rows = payload.get("value")
        if not isinstance(rows, list) or len(rows) > 200:
            raise ValueError("SAP yanıtındaki value listesi eksik veya 200 satırı aşıyor")
        if not rows:
            raise ValueError("SAP yanıtında malzeme yok")
        if isinstance(field_map, str):
            field_map = json.loads(field_map) if field_map.strip() else {}
        if field_map is not None and not isinstance(field_map, dict):
            raise ValueError("SAP alan eşlemesi JSON nesnesi olmalı")
        mapping = dict(DEFAULT_FIELD_MAP)
        for key, value in (field_map or {}).items():
            if key not in mapping or not isinstance(value, str) or not value.strip() or len(value) > 100:
                raise ValueError("SAP alan eşlemesi bilinmeyen veya geçersiz alan içeriyor")
            mapping[key] = value.strip()
        for index, source in enumerate(rows, 1):
            if not isinstance(source, dict):
                raise ValueError(f"SAP satırı {index}: JSON nesnesi olmalı")
            row_warnings = []
            item = {key: None for key in mapping}
            for key, api_field in mapping.items():
                value = source.get(api_field)
                if key in NUMBER_FIELDS:
                    item[key] = _sap_number(value, key, row_warnings)
                else:
                    item[key] = _sap_string(value) or None
            if not item["material_code"] or not item["name"] or not item["unit"]:
                row_warnings.append("Malzeme kodu, adı veya birimi eksik")
            item["required_specs"] = {}
            item["warnings"] = row_warnings
            result["materials"].append(item)
        counts = Counter((item["material_code"], item["unit"]) for item in result["materials"])
        for item in result["materials"]:
            if counts[(item["material_code"], item["unit"])] > 1:
                item["warnings"].append("Malzeme kodu + birim yineleniyor; çift sayım yapılmamalı")
                result["warnings"].append("Yinelenen SAP malzemesi var; stok hesaplarına doğrudan toplam verilmez")
        date, state, deadline_error = _sap_deadline(deadline_at, now)
        canonical = json.dumps(result["materials"], ensure_ascii=False, sort_keys=True, allow_nan=False)
        result["request"].update(id="SAP-" + hashlib.sha256(canonical.encode()).hexdigest()[:16],
                                 deadline_at=date, state=state)
        if len(result["materials"]) == 1:
            item = result["materials"][0]
            result["request"].update(material_code=item["material_code"], material_name=item["name"],
                                     quantity=item["quantity"], unit=item["unit"])
        if deadline_error:
            result["errors"].append(deadline_error)
        if any(not item["material_code"] or not item["name"] or not item["unit"] for item in result["materials"]):
            result["errors"].append("Eksik malzeme kimliği bulunan SAP satırları düzeltilmeli")
        result["status"] = "invalid" if result["errors"] else "ready"
    except (ValueError, TypeError, OverflowError):
        # JSON errors can quote an input substring. Do not propagate source data.
        result["errors"].append("SAP veri biçimi veya alan eşlemesi geçersiz; giriş düzeltilmeli")
    return result


class SAPProcurementInputComponent(Component):
    display_name = "SAP API Malzeme Girişi"
    description = "SAP/OData okuma bağlantısını hazırlar; açık izin olmadan API çağrısı yapmaz."
    name = "SAPProcurementInput"
    icon = "Database"
    inputs = [
        DropdownInput(name="data_kind", display_name="SAP veri kapsamı", options=["Malzeme", "Satın alma talepleri"], value="Malzeme"),
        MessageTextInput(name="endpoint", display_name="SAP API/OData adresi", value="",
                         info="Public HTTPS okuma adresi; henüz hazır değilse boş bırakın."),
        DropdownInput(name="auth_mode", display_name="Kimlik doğrulama", options=["None", "Bearer"], value="None"),
        SecretStrInput(name="bearer_token", display_name="SAP güvenli bağlantı anahtarı", required=False,
                       load_from_db=True, advanced=True),
        BoolInput(name="enable_api", display_name="SAP API okumayı etkinleştir", value=False),
        MultilineInput(name="field_map", display_name="SAP alan eşlemesi (JSON)", value="", advanced=True,
                       info="Örneğin {\"material_code\": \"Material\", \"name\": \"MaterialDescription\"}."),
        MessageTextInput(name="source_system", display_name="SAP mantıksal sistem / istemci takma adı", value="",
                         info="PR kimliğini diğer sistem/istemcilerden ayırır; bağlantı adresi veya kullanıcı sırrı değildir."),
        MessageTextInput(name="purchase_requisition", display_name="Seçilen satın alma talebi numarası", value=""),
        MessageTextInput(name="purchase_requisition_item", display_name="Seçilen talep kalemi", value="",
                         info="Numara ve kalemi kaynaktaki baştaki sıfırlarıyla birlikte aynen seçin."),
        MultilineInput(name="pr_field_map", display_name="Satın alma talebi alan eşlemesi (JSON)", value="", advanced=True,
                       info="requester_email, deleted, closed, blocked, released kaynak alanları açıkça eşlenir. Durumlar gerçek boolean olmalı. ordered_quantity varsayılan OrderedQuantity alanıdır; yalnız doğrulanmış sıfır yeni araştırmaya açılır, kalan miktar hesaplanmaz."),
        MultilineInput(name="fixture_payload", display_name="SAP yanıtı ile yerel deneme (JSON)", value="", advanced=True),
        MessageTextInput(name="requester", display_name="Araştırmayı isteyen kişi", value="", advanced=True),
        MessageTextInput(name="deadline_at", display_name="Talep bitiş tarihi", value="", advanced=True,
                         info="Her talepte kişiden istenir. ISO tarih + saat dilimi; boşsa araştırma bekler."),
    ]
    outputs = [Output(name="context", display_name="SAP talep bağlamı", method="build_context")]

    def build_context(self) -> Message:
        fixture = _sap_text(getattr(self, "fixture_payload", "")).strip()
        enabled = getattr(self, "enable_api", False) is True
        endpoint = _sap_text(getattr(self, "endpoint", "")).strip()
        is_pr = getattr(self, "data_kind", "Malzeme") == "Satın alma talepleri"
        if not fixture and not enabled:
            result = {"source": "sap_api", "status": "unconfigured", "materials": [],
                      "errors": [], "warnings": ["SAP okuma bağlantısı henüz etkin değil"],
                      "api_configured": bool(endpoint), "sap_connectivity_verified": False,
                      "request": {"id": None, "requester": None, "deadline_at": None,
                                  "state": "unconfigured", "required_specs": {}}}
            if is_pr:
                result.update(data_kind="purchase_requisitions", requisitions=[])
        else:
            try:
                payload = fixture if fixture else fetch_sap_payload(
                    endpoint, getattr(self, "auth_mode", "None"), getattr(self, "bearer_token", None))
                if is_pr:
                    result = normalize_sap_purchase_requisitions(payload, _sap_text(getattr(self, "pr_field_map", "")),
                        source_system=_sap_text(getattr(self, "source_system", "")),
                        purchase_requisition=_sap_text(getattr(self, "purchase_requisition", "")),
                        purchase_requisition_item=_sap_text(getattr(self, "purchase_requisition_item", "")),
                        requester=_sap_text(getattr(self, "requester", "")),
                        deadline_at=_sap_text(getattr(self, "deadline_at", "")))
                else:
                    result = normalize_sap_payload(payload, _sap_text(getattr(self, "field_map", "")),
                                                   requester=_sap_text(getattr(self, "requester", "")),
                                                   deadline_at=_sap_text(getattr(self, "deadline_at", "")))
                result["api_configured"] = bool(endpoint)
                result["sap_connectivity_verified"] = not bool(fixture) and (
                    result["status"] not in {"invalid", "unconfigured"} if is_pr else result["status"] == "ready")
                result["fixture_used"] = bool(fixture)
            except ValueError:
                result = {"source": "sap_api", "status": "invalid", "materials": [],
                          "errors": ["SAP API bağlantısı okunamadı; bağlantı/yetki ayarı kontrol edilmeli"],
                          "warnings": [], "sap_connectivity_verified": False,
                          "request": {"id": None, "requester": None, "deadline_at": None,
                                      "state": "invalid", "required_specs": {}}}
        if is_pr:
            result.setdefault("data_kind", "purchase_requisitions")
            result.setdefault("requisitions", [])
        self.status = result["status"]
        return Message(text=MARKER + json.dumps(result, ensure_ascii=False, allow_nan=False))
