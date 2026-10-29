"""Local material requisition; independent of SAP, mailboxes and model calls."""

import hashlib
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from lfx.custom import Component
from lfx.io import DropdownInput, MessageTextInput, MultilineInput, Output
from lfx.schema.message import Message

INTAKE_MARKER = "TALEP GİRİŞİ (JSON)\n"


def _pi_text(value, maximum=500):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError("Talep alanı metin olmalı ve uzunluk sınırını aşmamalı")
    value = value.strip()
    if any(ord(char) < 32 for char in value):
        raise ValueError("Talep alanında kontrol karakteri bulunmamalı")
    return value


def purchase_specs(value):
    """A short key:value list is data, never a set of model instructions."""
    if isinstance(value, dict):
        items = list(value.items())
    elif isinstance(value, str) and len(value) <= 20_000:
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        items = []
        for line in lines:
            if ":" not in line:
                raise ValueError("Her teknik özellik 'özellik: değer' biçiminde olmalı")
            items.append(line.split(":", 1))
    else:
        raise ValueError("Teknik özellik listesi geçersiz")
    if len(items) > 30:
        raise ValueError("En çok 30 teknik özellik girilebilir")
    specs = {}
    for key, item in items:
        key, item = _pi_text(key, 100), _pi_text(item, 500)
        if not key or not item or key.casefold() in {name.casefold() for name in specs}:
            raise ValueError("Teknik özellik adı/değeri eksik veya yinelenmiş")
        specs[key] = item
    return specs


def normalize_purchase_request(*, request_id="", material_name="", material_code="", material_kind="Hammadde",
                               quantity="", unit="", required_specs="", delivery_place="", need_by="",
                               requester="", deadline_at="", now=None):
    result = {"source": "manual", "status": "unconfigured", "materials": [], "request": {},
              "errors": [], "warnings": [], "outbound_sent": False}
    if not any((request_id, material_name, material_code, quantity, requester, deadline_at)):
        result["warnings"].append("Hammadde veya sarf malzeme talebini girin; SAP bağlantısı gerekmez.")
        return result
    try:
        now = now or datetime.now(timezone.utc)
        if now.utcoffset() is None:
            raise ValueError("Referans zamanı saat dilimi içermeli")
        ref, name, code = _pi_text(request_id, 200), _pi_text(material_name, 300), _pi_text(material_code, 100)
        owner, measure = _pi_text(requester, 320), _pi_text(unit, 32)
        kind = {"Hammadde": "hammadde", "Sarf malzeme": "sarf", "hammadde": "hammadde", "sarf": "sarf"}.get(material_kind)
        if not ref or not name or not kind:
            raise ValueError("Talep numarası, malzeme adı ve türü gerekli")
        if not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", owner):
            raise ValueError("Talep sahibinin geçerli e-posta adresi gerekli")
        if isinstance(quantity, bool):
            raise ValueError("Miktar geçersiz")
        number = str(quantity).strip()
        if not re.fullmatch(r"\d+(?:[.,]\d{1,6})?", number) or len(number) > 32:
            raise ValueError("Miktar pozitif ve en çok 6 ondalıklı olmalı")
        amount = Decimal(number.replace(",", "."))
        if not amount.is_finite() or not 0 < amount <= Decimal("1e12") or not measure:
            raise ValueError("Pozitif miktar ve birim gerekli")
        specs = purchase_specs(required_specs)
        place, need = _pi_text(delivery_place, 500), _pi_text(need_by, 20)
        if need:
            date.fromisoformat(need)
        deadline = None
        deadline_error = None
        if deadline_at:
            deadline = datetime.fromisoformat(_pi_text(deadline_at, 80).replace("Z", "+00:00"))
            if deadline.utcoffset() is None:
                raise ValueError("Bitiş tarihi saat dilimi içermeli")
            if deadline <= now:
                deadline_error = "Bitiş tarihi saat dilimiyle birlikte gelecekte olmalı"
        code = code or "M_" + hashlib.sha256(name.casefold().encode()).hexdigest()[:10]
        item = {"material_code": code, "name": name, "quantity": format(amount.normalize(), "f"),
                "unit": measure, "required_specs": specs}
        request = {"id": ref, "requester": owner, "material_code": code, "material_name": name,
                   "quantity": item["quantity"], "unit": measure, "required_specs": specs,
                   "material_kind": kind, "delivery_place": place or None, "need_by": need or None,
                   "deadline_at": deadline.isoformat() if deadline else None,
                   "state": "research_ready" if deadline else "awaiting_deadline"}
        result.update(status="invalid" if deadline_error else "ready", materials=[item], request=request)
        if deadline_error:
            # The coordinator resumes only an already registered owner-confirmed
            # case. A new request with an expired deadline must remain blocked.
            result["errors"].append(deadline_error)
        if not specs:
            result["warnings"].append("Teknik şartname belirtilmedi; uygunluk varsayılmayacak.")
    except (ValueError, TypeError, InvalidOperation, AttributeError):
        result.update(status="invalid", materials=[], request={})
        result["errors"].append("Talep numarası, malzeme/miktar/birim, sahibi, teknik alanlar ve saat dilimli bitiş zamanını kontrol edin.")
    return result


class PurchaseRequestInput(Component):
    display_name = "Hammadde / Sarf Malzeme Talebi"
    description = "Satın alınacak malzeme ve ortak teklif şartlarını alır; SAP veya e-posta bağlantısı gerektirmez."
    name = "PurchaseRequestInput"
    icon = "ClipboardList"
    inputs = [
        MessageTextInput(name="request_id", display_name="Talep numarası", value="",
                         info="Her yeni talep için farklı numara. Aynı numara aynı talebi korur."),
        DropdownInput(name="material_kind", display_name="Malzeme türü", options=["Hammadde", "Sarf malzeme"], value="Hammadde"),
        MessageTextInput(name="material_name", display_name="Malzeme adı", value=""),
        MessageTextInput(name="material_code", display_name="Malzeme kodu (isteğe bağlı)", value="", advanced=True),
        MessageTextInput(name="quantity", display_name="İstenen miktar", value=""),
        MessageTextInput(name="unit", display_name="Birim", value=""),
        MultilineInput(name="required_specs", display_name="Teknik özellikler", value="", info="Her satırda özellik: değer. Örnek: Kalınlık: 30 mikron"),
        MessageTextInput(name="delivery_place", display_name="Teslim yeri", value=""),
        MessageTextInput(name="need_by", display_name="İhtiyaç tarihi", value="", info="YYYY-AA-GG; belirtilmediyse boş bırakın."),
        MessageTextInput(name="requester", display_name="Talep sahibinin e-postası", value=""),
        MessageTextInput(name="deadline_at", display_name="Talep sahibinin belirttiği bitiş zamanı", value="",
                         info="Kişiye sorulur; örnek biçim 2026-10-25T16:00:00+03:00. Boşsa süre beklenir."),
    ]
    outputs = [Output(name="context", display_name="Satın alma talebi", method="build_context")]

    def build_context(self) -> Message:
        values = {key: getattr(self, key, "") for key in (
            "request_id", "material_name", "material_code", "material_kind", "quantity", "unit", "required_specs",
            "delivery_place", "need_by", "requester", "deadline_at")}
        result = normalize_purchase_request(**values)
        self.status = result["status"]
        return Message(text=INTAKE_MARKER + json.dumps(result, ensure_ascii=False, allow_nan=False))
