"""Separate procurement mailbox intake and per-request deadline question drafts.

This component parses supplied data only. No mailbox connection, SMTP call,
message sending, purchasing action or instruction execution is implemented.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from lfx.custom import Component
from lfx.io import MessageTextInput, MultilineInput, Output
from lfx.schema.message import Message

MARKER = "TALEP GİRİŞİ (JSON)\n"
EMAIL = re.compile(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+")


def _mail_text(value):
    return value.text if isinstance(value, Message) else str(value or "")


def _mail_string(value, limit=300):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("E-posta talebindeki metin alanı geçersiz veya çok uzun")
    return value.strip()


def _mail_number(value):
    if value is None or value == "":
        return None
    raw = str(value).strip()
    if isinstance(value, bool) or len(raw) > 32 or not re.fullmatch(r"\+?\d+(?:[.,]\d{1,6})?", raw):
        raise ValueError("Talep miktarı sıfır veya pozitif, en çok 6 ondalıklı olmalı")
    try:
        quantity = Decimal(raw.replace(",", "."))
    except InvalidOperation:
        raise ValueError("Talep miktarı geçersiz") from None
    if not quantity.is_finite() or quantity > Decimal("1e12"):
        raise ValueError("Talep miktarı sınır dışında")
    return format(quantity.normalize(), "f")


def _mail_deadline(value, now):
    if not value:
        return None, "awaiting_deadline", None
    try:
        text = _mail_string(value, 80)
        date = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if date.utcoffset() is None or date <= now:
            raise ValueError
    except (ValueError, TypeError):
        return None, "awaiting_deadline", "Bitiş tarihini saat dilimiyle birlikte gelecekte bir zaman olarak belirtin"
    return date.isoformat(), "research_ready", None


def _mail_specs(value):
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict) or len(value) > 30:
        raise ValueError("Teknik özellikler en çok 30 alanlı JSON nesnesi olmalı")
    specs = {}
    for key, item in value.items():
        key = _mail_string(key, 100)
        if not key or not isinstance(item, (str, int, float, bool)) or item is None:
            raise ValueError("Teknik özellik alanı geçersiz")
        if isinstance(item, float) and not Decimal(str(item)).is_finite():
            raise ValueError("Teknik özellik sayısı sonlu olmalı")
        specs[key] = _mail_string(str(item), 500)
    return specs


def normalize_mail_request(payload=None, *, material_name="", material_code="", quantity="", unit="",
                           requester="", deadline_at="", required_specs=None, bot_mailbox="", now=None):
    """Structured fields are authoritative; mail body is preserved as untrusted data."""
    now = now or datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError("Referans zamanı saat dilimi içermeli")
    result = {"source": "mail_request", "status": "invalid", "materials": [], "errors": [], "warnings": [],
              "mailbox": None, "mailbox_connectivity_verified": False, "outbound_sent": False,
              "request": {"id": None, "requester": None, "deadline_at": None,
                          "state": "awaiting_deadline", "required_specs": {}},
              "deadline_question": {"status": "draft", "recipient": None, "body": "", "sent": False}}
    try:
        if payload is None or payload == "":
            payload = {}
        if isinstance(payload, str):
            if len(payload) > 100_000:
                raise ValueError("E-posta talebi boyut sınırını aşıyor")
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise ValueError("E-posta talebi JSON nesnesi olmalı")
        mailbox = _mail_string(bot_mailbox, 320)
        if mailbox and not EMAIL.fullmatch(mailbox):
            raise ValueError("Ayrı satın alma/bot e-posta adresi geçersiz")
        result["mailbox"] = mailbox or None
        if not mailbox:
            result["warnings"].append("Ayrı satın alma/bot kutusu henüz yapılandırılmadı")
        result["warnings"].append("Canlı e-posta bağlantısı doğrulanmadı; sağlanan talep verisi kullanılıyor")
        name = _mail_string(material_name or payload.get("material_name") or payload.get("name"))
        code = _mail_string(material_code or payload.get("material_code"), 80)
        request_unit = _mail_string(unit or payload.get("unit"), 32)
        amount = _mail_number(quantity if quantity != "" and quantity is not None else payload.get("quantity"))
        specs = _mail_specs(required_specs if required_specs else payload.get("required_specs"))
        if not name:
            result["errors"].append("Araştırılacak malzeme adı seçilmeli veya yazılmalı")
        else:
            result["materials"] = [{"material_code": code or None, "name": name,
                                    "quantity": amount, "unit": request_unit or None, "required_specs": specs}]
        if amount is not None and not request_unit:
            result["warnings"].append("Miktar var ancak birim eksik; teklif miktarları henüz karşılaştırılamaz")
        sender = _mail_string(requester or payload.get("requester") or payload.get("sender"), 320)
        known_sender = sender if sender and EMAIL.fullmatch(sender) else None
        if sender and not known_sender:
            result["warnings"].append("İsteyen kişinin e-posta adresi geçersiz; alıcı atanmadı")
        if not known_sender:
            result["warnings"].append("İlgili kişi bilinmiyor; süre sorusu gönderilemez")
        message_id = _mail_string(payload.get("message_id"), 500)
        request_id = _mail_string(payload.get("request_id") or payload.get("id"), 100)
        if not request_id and name:
            fingerprint = json.dumps({"mailbox": mailbox, "message_id": message_id, "sender": known_sender, "name": name,
                                      "code": code, "quantity": amount, "unit": request_unit,
                                      "specs": specs}, ensure_ascii=False, sort_keys=True, allow_nan=False)
            request_id = "MAIL-" + hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
        date, state, deadline_error = _mail_deadline(deadline_at or payload.get("deadline_at"), now)
        if deadline_error:
            result["errors"].append(deadline_error)
        result["request"].update(id=request_id or None, requester=known_sender, deadline_at=date,
                                 state=state, required_specs=specs, material_code=code or None,
                                 material_name=name or None, quantity=amount, unit=request_unit or None,
                                 mailbox=mailbox or None, message_id=message_id or None)
        # Body/subject are not used to change state, deadlines, specifications or credentials.
        result["source_message"] = {"message_id": message_id or None, "sender": known_sender,
                                    "subject": _mail_string(payload.get("subject"), 500),
                                    "body": _mail_string(payload.get("body"), 20_000),
                                    "received_at": _mail_string(payload.get("received_at"), 80),
                                    "trust": "untrusted_source_data"}
        if state == "awaiting_deadline":
            material_label = name or "belirteceğiniz malzeme"
            body = (f"Talep: {request_id or 'henüz tanımlanmadı'}\n"
                    f"{material_label} araştırmasını ve gelen tedarikçi tekliflerinin karşılaştırmasını "
                    "hangi tarih ve saate kadar tamamlamamı istersiniz? "
                    "Lütfen bitiş tarihini ve saat dilimini belirtin; örnek: YYYY-MM-DD 17:00 Türkiye saati. "
                    "Süre kesinleştiğinde o zamana kadar gelen cevaplar yönetici raporunda karşılaştırılacaktır.")
            result["deadline_question"].update(recipient=known_sender, body=body)
        else:
            result["deadline_question"]["status"] = "not_required"
        result["status"] = "invalid" if result["errors"] else "ready"
    except (ValueError, TypeError, OverflowError):
        result["errors"].append("E-posta talebi biçimi veya alanları geçersiz; giriş düzeltilmeli")
    return result


class MailResearchInputComponent(Component):
    display_name = "E-posta Özel Araştırma Talebi"
    description = "Ayrı satın alma/bot kutusu için talep ve kişiye özel bitiş süresini hazırlar; mesaj göndermez."
    name = "MailResearchInput"
    icon = "Mail"
    inputs = [
        MessageTextInput(name="bot_mailbox", display_name="Ayrı satın alma/bot e-posta adresi", value=""),
        MessageTextInput(name="material_name", display_name="Araştırılacak malzeme", value=""),
        MessageTextInput(name="quantity", display_name="İstenen miktar (isteğe bağlı)", value=""),
        MessageTextInput(name="unit", display_name="Birim (isteğe bağlı)", value=""),
        MessageTextInput(name="requester", display_name="Talebi isteyen kişinin e-postası", value=""),
        MessageTextInput(name="deadline_at", display_name="Kişinin belirttiği bitiş zamanı", value="",
                         info="Her talepte sorulur. ISO tarih ve saat dilimi; boşken araştırma bekler."),
        MessageTextInput(name="mail_message", display_name="Bağlı kutudan talep (JSON)", required=False,
                         input_types=["Message"], advanced=True),
        MultilineInput(name="request_payload", display_name="Talep JSON / yerel deneme", value="", advanced=True),
        MessageTextInput(name="material_code", display_name="Malzeme kodu (isteğe bağlı)", value="", advanced=True),
        MultilineInput(name="required_specs_json", display_name="İstenen teknik özellikler (JSON)", value="", advanced=True),
    ]
    outputs = [
        Output(name="context", display_name="E-posta talep bağlamı", method="build_context", group_outputs=True),
        Output(name="deadline_question", display_name="Süre sorusu taslağı", method="build_deadline_question", group_outputs=True),
    ]

    def _context(self):
        payload = _mail_text(getattr(self, "request_payload", "")) or _mail_text(getattr(self, "mail_message", ""))
        return normalize_mail_request(
            payload, material_name=_mail_text(getattr(self, "material_name", "")),
            material_code=_mail_text(getattr(self, "material_code", "")),
            quantity=_mail_text(getattr(self, "quantity", "")), unit=_mail_text(getattr(self, "unit", "")),
            requester=_mail_text(getattr(self, "requester", "")),
            deadline_at=_mail_text(getattr(self, "deadline_at", "")),
            required_specs=_mail_text(getattr(self, "required_specs_json", "")),
            bot_mailbox=_mail_text(getattr(self, "bot_mailbox", "")),
        )

    def build_context(self) -> Message:
        result = self._context()
        self.status = result["request"]["state"]
        return Message(text=MARKER + json.dumps(result, ensure_ascii=False, allow_nan=False))

    def build_deadline_question(self) -> Message:
        result = self._context()
        question = result["deadline_question"]
        self.status = "Süre sorusu taslağı; gönderilmedi"
        text = ("E-POSTA TASLAĞI — GÖNDERİLMEDİ\n"
                f"Alıcı: {question['recipient'] or 'bilinmiyor'}\n\n"
                + (question["body"] or ("Talep alanları düzeltilmeli; süre sorusu henüz hazırlanamadı."
                                         if result["status"] == "invalid" else
                                         "Geçerli bitiş zamanı mevcut; süre sorusu gerekmiyor.")))
        return Message(text=text)
