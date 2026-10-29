"""İki girişi talep, süre ve stok kapsamına bağlar; beklerken model dalını durdurur."""

import csv
import hashlib
import io
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from case_store import CaseStore
from lfx.custom import Component
from lfx.io import DropdownInput, MessageTextInput, Output
from lfx.schema.message import Message

INTAKE_MARKER = "TALEP GİRİŞİ (JSON)\n"
CASE_MARKER = "TEKLİF DOSYASI (JSON)\n"


def _ctx_parse(value):
    text = value.text if isinstance(value, Message) else str(value or "")
    if not text.strip():
        return {}
    if INTAKE_MARKER in text:
        text = text.rsplit(INTAKE_MARKER, 1)[1]
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("Talep bağlamı nesne olmalı")
    return payload


def coordinate_request(context, selected_code="", store=None):
    """SAP kalemlerini ayrı dosyalara bağlar; süre yalnız kayıtlı sahibinden gelir."""
    result = {"ready": False, "context": context, "case": None, "cases": [],
              "reason": "Giriş bağlantısı veya talep bekleniyor."}
    if not isinstance(context, dict):
        return result
    deadline_errors = {
        "Bitiş tarihi saat dilimiyle birlikte gelecekte olmalı",
        "Bitiş tarihini saat dilimiyle birlikte gelecekte bir zaman olarak belirtin",
    }
    errors = context.get("errors") or []
    deadline_only_invalid = (context.get("status") == "invalid" and isinstance(errors, list)
                             and bool(errors) and all(error in deadline_errors for error in errors))
    if context.get("status") != "ready" and not deadline_only_invalid:
        details = context.get("errors") or context.get("warnings") or [result["reason"]]
        result["reason"] = "\n".join(str(value) for value in details)
        return result
    materials = context.get("materials", [])
    if not isinstance(materials, list) or not materials:
        result["reason"] = "Araştırılacak malzeme bilgisi yok."
        return result
    if any(not isinstance(item, dict) for item in materials):
        result["reason"] = "Malzeme listesi nesne satırları içermeli."
        return result
    is_sap = context.get("source") == "sap_api"
    is_requisition = is_sap and context.get("data_kind") == "purchase_requisitions"
    if is_sap and selected_code:
        materials = [item for item in materials if item.get("material_code") == selected_code.strip()]
    if not materials or any(not isinstance(item, dict) or not isinstance(item.get("name"), str)
                            or not item["name"].strip() for item in materials):
        result["reason"] = "Seçilen kod bulunamadı veya malzeme bilgisi eksik."
        return result
    if (is_sap and len(materials) > 10) or (not is_sap and len(materials) != 1):
        result["reason"] = ("SAP araştırması en çok 10 kalem içerebilir; daha küçük kapsam seçin. Hiçbir kalem kırpılmadı."
                            if is_sap else "E-posta özel talebi tek malzeme içermeli.")
        return result
    materials = [dict(item) for item in materials]
    for item in materials:
        item["material_code"] = item.get("material_code") or "M_" + hashlib.sha256(item["name"].casefold().encode()).hexdigest()[:10]
    if len({item["material_code"] for item in materials}) != len(materials):
        result["reason"] = "Aynı SAP malzeme kodu birden çok satırda var; birleştirme veya açık seçim gerekli."
        return result
    request = dict(context.get("request") or {})
    context = {**context, "materials": materials, "request": request}
    result["context"] = context
    if is_requisition and (len(materials) != 1 or not isinstance(request.get("sap_requisition"), dict)
                           or set(request["sap_requisition"]) != {"source_system", "purchase_requisition", "purchase_requisition_item"}):
        result["reason"] = "Tek bir SAP satın alma talep kalemi ve kaynak kimliği seçilmeli."
        return result
    if not request.get("requester"):
        result["reason"] = "Talep sahibi belirlenmeli; araştırma bitiş süresi ilgili kişiden sorulacak."
        return result
    if context.get("source") == "mail_request" and not request.get("message_id"):
        result["reason"] = "Kaynak e-posta kimliği gerekli. Bot kutusu bağlanana kadar bu giriş yalnız yerel ön hazırlıktır."
        return result
    if store is None:
        store = CaseStore(Path.cwd() / ".codex/agents/satin-alma-stok/cases/requests.db")
    cases = []
    requests = []
    for item in materials:
        item_request = {**request, "material_code": item["material_code"], "material_name": item["name"],
                        "quantity": item.get("quantity"), "unit": item.get("unit"),
                        "required_specs": item.get("required_specs") or request.get("required_specs") or {}}
        if is_sap:
            if not request.get("id"):
                result["reason"] = "SAP talep kimliği eksik; kalemler kayıt altına alınamadı."
                return result
            # A PR's identity is its source system + requisition + item, not
            # the current material code or contents of the returned page.
            item_request["id"] = str(request["id"]) if is_requisition else str(request["id"]) + "-" + item["material_code"]
        requests.append(item_request)
    if deadline_only_invalid:
        # Invalid dates never open new cases. Every case must already have an
        # owner-confirmed deadline. upsert below verifies immutable source facts.
        try:
            for item_request in requests:
                snapshot = store.snapshot(item_request.get("id"))
                if not snapshot["request"].get("deadline_at"):
                    raise ValueError("Kaydedilmiş süre yok")
        except (ValueError, TypeError):
            result["reason"] = "Bitiş zamanı geçersiz ve devam edilebilecek onaylı talep bulunamadı."
            return result
    for item_request in requests:
        case = store.upsert_request(item_request, context.get("source"))
        if item_request.get("deadline_at") and not deadline_only_invalid:
            case = store.set_deadline(case["request"]["id"], item_request["deadline_at"], item_request["requester"])
        cases.append(case)
    result["cases"] = cases
    result["case"] = cases[0]
    if len(cases) == 1:
        context["request"] = cases[0]["request"]
    else:
        states = {case["request"]["state"] for case in cases}
        dates = {case["request"]["deadline_at"] for case in cases}
        common_state = ("awaiting_deadline" if "awaiting_deadline" in states else
                        "collecting" if "collecting" in states else
                        "ready_for_report" if "ready_for_report" in states else "report_prepared")
        context["request"] = {**request, "state": common_state,
                              "deadline_at": next(iter(dates)) if len(dates) == 1 else None}
        context["request_ids"] = [case["request"]["id"] for case in cases]
    if deadline_only_invalid:
        context["status"] = "ready"
        context["errors"] = []
        context["warnings"] = [*(context.get("warnings") or []),
                               "Yeni/geçersiz süre kullanılmadı; kayıtlı talep sahibinin onaylı süresiyle devam edildi."]
    if any(case["request"]["state"] == "awaiting_deadline" for case in cases):
        result["reason"] = (f"Talep {request.get('id') or cases[0]['request']['id']}: "
                            f"{', '.join(item['name'] for item in materials)} araştırmasını hangi tarih ve saate kadar "
                            "tamamlamamı istersiniz? Türkiye saatini belirtin. Süre sorusu taslaktır; gönderilmedi.")
        return result
    result["ready"] = True
    result["reason"] = "Araştırma ve teklif takibi için süre kaydedildi."
    return result


def stock_table_from_context(context):
    buffer = io.StringIO()
    # Bağlam başlıktan önce tutulur; JSON tablonun veri satırı sayılmaz.
    buffer.write(INTAKE_MARKER + json.dumps(context, ensure_ascii=False, allow_nan=False) + "\n\n")
    buffer.write("Rapor tarihi: " + datetime.now(ZoneInfo("Europe/Istanbul")).date().isoformat() + "\n")
    writer = csv.writer(buffer, delimiter=";", lineterminator="\n")
    writer.writerow(["malzeme_kodu", "tanım", "birim", "mevcut_stok", "ayrılmış_stok", "kesin_açık_sipariş",
                     "günlük_tüketim", "termin_gün", "emniyet_stok", "açık_sipariş_teslim_tarihi"])
    for item in context.get("materials", []):
        values = [item.get(key) for key in ["material_code", "name", "unit", "stock", "reserved_stock",
                                          "open_order_quantity", "daily_consumption", "lead_time_days",
                                          "safety_stock", "open_order_delivery_date"]]
        writer.writerow(["" if value is None else str(value).replace("\n", " ").replace("\r", " ") for value in values])
    return buffer.getvalue()


class ProcurementRequestCoordinator(Component):
    display_name = "Talep ve Süre Takibi"
    description = "Malzeme talebi, e-posta veya isteğe bağlı SAP girişini seçer; kişiye özel süreyi takip eder."
    name = "ProcurementRequestCoordinator"
    icon = "GitBranch"
    inputs = [
        MessageTextInput(name="manual_context", display_name="Malzeme talebi", required=False, value=""),
        MessageTextInput(name="sap_context", display_name="SAP girişi", required=False, value=""),
        MessageTextInput(name="mail_context", display_name="E-posta talebi", required=False, value=""),
        DropdownInput(name="input_source", display_name="Talep kaynağı", options=["Malzeme talebi", "E-posta özel talep", "SAP API"], value="Malzeme talebi"),
        MessageTextInput(name="selected_material_code", display_name="SAP listesinden malzeme kodu", value="",
                         info="Boşsa SAP'den gelen tüm kalemler araştırılır (en çok10); belirli kodla kapsam daraltılır."),
    ]
    outputs = [
        Output(name="stock_table", display_name="Süreli araştırma girdisi", method="build_stock_table", group_outputs=True),
        Output(name="request_context", display_name="Talep ve süre bağlamı", method="build_request_context", group_outputs=True),
        Output(name="case_data", display_name="Teklif dosyası", method="build_case_data", group_outputs=True),
        Output(name="pending_response", display_name="Süre / bağlantı bekleniyor", method="build_pending", group_outputs=True),
    ]

    def _prepare(self):
        if not hasattr(self, "_prepared_intake"):
            selected = (self.sap_context if self.input_source == "SAP API" else
                        self.manual_context if self.input_source == "Malzeme talebi" else self.mail_context)
            try:
                self._prepared_intake = coordinate_request(_ctx_parse(selected), self.selected_material_code)
            except (ValueError, TypeError, KeyError):
                self._prepared_intake = {"ready": False, "context": {}, "case": None,
                                        "reason": "Talep/süre kaydı doğrulanamadı; kaynak alanlarını kontrol edin."}
            self.status = self._prepared_intake["reason"]
        return self._prepared_intake

    def _guard(self, prepared):
        if getattr(self, "_vertex", None) is not None:
            excluded = ["pending_response"] if prepared["ready"] else ["stock_table", "case_data"]
            for output in excluded:
                self.stop(output)
            # One replacement keeps both pending exclusions under the same
            # source key; separate calls would restore the first excluded path.
            self.graph.exclude_branches_conditionally(self._id, excluded)

    def build_stock_table(self) -> Message:
        prepared = self._prepare()
        self._guard(prepared)
        return Message(text=stock_table_from_context(prepared["context"]) if prepared["ready"] else "")

    def build_request_context(self) -> Message:
        prepared = self._prepare()
        self._guard(prepared)
        return Message(text=INTAKE_MARKER + json.dumps(prepared["context"], ensure_ascii=False))

    def build_case_data(self) -> Message:
        prepared = self._prepare()
        self._guard(prepared)
        cases = prepared.get("cases") or []
        case = ({"cases": cases} if len(cases) > 1 else prepared["case"]) or {"request": {"state": "awaiting_configuration"}, "offers": [],
                                     "as_of": datetime.now(ZoneInfo("Europe/Istanbul")).isoformat()}
        return Message(text=CASE_MARKER + json.dumps(case, ensure_ascii=False))

    def build_pending(self) -> Message:
        prepared = self._prepare()
        self._guard(prepared)
        return Message(text="" if prepared["ready"] else prepared["reason"])
