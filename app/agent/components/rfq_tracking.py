"""Langflow RFQ draft persistence, isolated to the procurement case queue.

Imports for the local case/RFQ helpers are resolved from this checkout at run
time so the native custom-component compiler can load the component itself.
There is no mailbox access, provider call or message dispatch in this module.
"""

import importlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from lfx.custom import Component
from lfx.io import MessageTextInput, Output, TableInput
from lfx.schema.data import Data
from lfx.schema.message import Message
from lfx.schema.table import EditMode

RFQ_TRACKING_MARKER = "TEKLİF TALEBİ TAKİBİ (JSON)\n"


def _rt_dependencies():
    root = (Path.cwd() / ".codex/agents/satin-alma-stok").resolve()
    if not root.is_dir():
        return _rt_source_dependencies()
    return _rt_load_dependencies(root)


def _rt_source_dependencies():
    source = globals().get("__file__")
    if not isinstance(source, (str, Path)):
        raise ValueError("Satın alma yardımcı kaynak klasörü doğrulanamadı.")
    source_path = Path(source).resolve()
    helper_root = source_path.parent.parent
    if (source_path.name != "rfq_tracking.py" or source_path.parent.name != "components"
            or not source_path.is_file()
            or not all((helper_root / name).is_file() for name in ("case_store.py", "rfq_store.py", "runtime_config.py"))):
        raise ValueError("Satın alma yardımcı kaynak klasörü doğrulanamadı.")
    return _rt_load_dependencies(helper_root)


def _rt_load_dependencies(root):
    for folder in (root, root / "components"):
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
    loaded = []
    for name, folder in (("case_store", root), ("rfq", root / "components"), ("rfq_store", root)):
        module = importlib.import_module(name)
        if Path(module.__file__).resolve().parent != folder:
            raise ValueError("Satın alma yardımcı modülü başka bir proje klasöründen yüklendi; işlem durduruldu.")
        loaded.append(module)
    return loaded


def _rt_path(value, default, label):
    if value is None or value == "":
        return Path(default).resolve()
    if not isinstance(value, (str, Path)):
        raise ValueError(f"{label} geçerli yerel dosya yolu olmalı.")
    text = str(value)
    if len(text) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError(f"{label} geçerli yerel dosya yolu olmalı.")
    return Path(text).expanduser().resolve()


def _rt_registry(rows, material_code, pure):
    if not isinstance(rows, list) or len(rows) > pure.RFQ_SUPPLIER_MAXIMUM:
        raise ValueError("Bu talep için seçilen tedarikçi tablosu en çok 50 satır içermeli.")
    registry = []
    for row in rows:
        if isinstance(row, Data):
            row = row.data
        if not isinstance(row, dict):
            raise ValueError("Tedarikçi tablosu nesne satırlarından oluşmalı.")
        registry.append({key: row.get(key) for key in (
            "supplier_id", "name", "email", "kind", "contact_confirmed", "source_url"
        )} | {"material_codes": [material_code]})
    # Applies JSON size/depth/finite-number bounds before persistence begins.
    return pure._rfq_payload(registry)


def _rt_verified_status(store, request_id, scope_hash, deadline, instant, pure):
    tracking = store.status(request_id, now=instant)
    for draft in tracking["drafts"]:
        expected_id = "RFQ-" + pure._rfq_hash([request_id, draft["supplier_id"], scope_hash])[:24]
        if (draft["rfq_id"] != expected_id
                or pure._rfq_instant(draft["deadline_at"], "Kayıtlı teklif isteği bitişi") != deadline):
            raise ValueError("Kayıtlı teklif isteğinin kapsamı veya süresi güncel talepten farklı; yeni talep kimliği gerekli.")
    return tracking


def prepare_rfq_tracking(case_data, supplier_rows, *, queue_path="", output_dir="", now=None):
    """Persist only a collecting case whose supplied scope matches its queue.

    The table selects contacts for this single material. It cannot override the
    saved request's material scope or owner-confirmed deadline. RFQStore repeats
    expected-scope and deadline checks before its transaction writes anything.
    """
    result = {"schema_version": 1, "status": "blocked", "request_id": None, "scope_hash": None,
              "deadline_at": None, "drafts": [], "blocked_suppliers": [], "warnings": [],
              "queue_status": "not_checked", "counts": {}, "emails_sent": False,
              "providers_called": False, "network_send_verified": False, "draft_only": True}
    try:
        cases, pure, rfqs = _rt_dependencies()
        instant = pure._rfq_instant(datetime.now(timezone.utc) if now is None else now, "Referans zamanı")
        payload = pure._rfq_payload(case_data, pure.RFQ_CASE_MARKER)
        if not isinstance(payload, dict) or "cases" in payload or not isinstance(payload.get("request"), dict):
            raise ValueError("Teklif isteği takibi tek kayıtlı malzeme talebi içermeli.")
        request = payload["request"]
        if request.get("state") not in {"collecting", "ready_for_report", "report_prepared"}:
            raise ValueError("Talep sahibinin onaylı süresi bekleniyor; teklif isteği kaydı oluşturulmadı.")
        result["request_id"] = pure._rfq_text(request.get("id"), "Talep kimliği", 200)
        pure._rfq_email(request.get("requester"), "Talep sahibi")
        scope_hash = pure._rfq_hash(pure._rfq_scope(request))
        deadline = pure._rfq_instant(request.get("deadline_at"), "Araştırma bitişi")
        if request.get("deadline_confirmed") is False or request.get("owner_confirmed") is False:
            raise ValueError("Araştırma süresi talep sahibi tarafından onaylanmalı.")
        if request.get("deadline_confirmed_by") is not None and request["deadline_confirmed_by"] != request["requester"]:
            raise ValueError("Süreyi yalnız kayıtlı talep sahibi onaylayabilir.")
        if request.get("state") != "collecting" and deadline > instant:
            raise ValueError("Talebin toplama durumu ile bitiş zamanı birbiriyle uyuşmuyor.")
        registry = _rt_registry(supplier_rows, request.get("material_code"), pure)
        supplied = pure.build_rfq_plan(payload, registry, now=instant)
        for key in ("blocked_suppliers", "warnings"):
            result[key] = supplied[key]
        result.update(scope_hash=scope_hash, deadline_at=deadline.isoformat())
        path = _rt_path(queue_path, cases.DEFAULT_PATH, "Talep kuyruğu")
        output = _rt_path(output_dir, rfqs.DEFAULT_OUTPUT, "Taslak klasörü")
        store = rfqs.RFQStore(path, output)
        if not path.is_file():
            result["queue_status"] = "not_created"
            raise ValueError("Talep kuyruğu yok; önce kaynaklı ve süresi onaylı gerçek talep kaydedilmeli.")
        result["queue_status"] = "existing"
        saved = store.snapshot(result["request_id"], now=instant)
        # Build the same pure canonical scope from the authoritative saved case.
        current = pure.build_rfq_plan(saved, registry, now=instant)
        if (saved["request"].get("state") not in {"collecting", "ready_for_report", "report_prepared"}
                or pure._rfq_hash(pure._rfq_scope(saved["request"])) != scope_hash
                or pure._rfq_instant(saved["request"].get("deadline_at"), "Kayıtlı araştırma bitişi") != deadline):
            raise ValueError("Bağlanan talebin malzeme kapsamı, toplama durumu veya bitiş zamanı kayıtlı taleple eşleşmiyor.")
        provided_request, saved_request = payload["request"], saved["request"]
        for field in ("requester", "mailbox", "message_id"):
            if provided_request.get(field) != saved_request.get(field):
                raise ValueError("Bağlanan talebin sahibi veya kaynak kimliği kayıtlı taleple eşleşmiyor.")
        if payload.get("source") is not None and payload["source"] != saved.get("source"):
            raise ValueError("Bağlanan talebin kaynağı kayıtlı taleple eşleşmiyor.")
        if deadline <= instant or not supplied["drafts"]:
            tracking = _rt_verified_status(store, result["request_id"], scope_hash, deadline, instant, pure)
            result.update(status="tracking_only", read_only=True, drafts=tracking["drafts"],
                          queue_status=tracking["queue_status"], counts=tracking["counts"])
            if deadline <= instant:
                result["warnings"] = ["Süre doldu; yeni taslak hazırlanmadı. Yalnız aynı talebin kayıtlı teklif/yanıt durumu gösteriliyor."]
            return result
        if not current["drafts"]:
            raise ValueError("Kayıtlı talep yeni teklif isteği hazırlamaya uygun değil.")
        prepared = store.prepare(result["request_id"], registry, now=instant,
                                 expected_scope_hash=scope_hash, expected_deadline_at=deadline.isoformat())
        tracking = _rt_verified_status(store, result["request_id"], scope_hash, deadline, instant, pure)
        result.update(status=prepared["status"], drafts=prepared["drafts"],
                      blocked_suppliers=prepared["blocked_suppliers"], warnings=prepared["warnings"],
                      queue_status=tracking["queue_status"], counts=tracking["counts"])
        # The report uses exactly the bodies that RFQStore has fingerprinted.
        prepared_ids = {item["rfq_id"] for item in prepared["drafts"]}
        preview = {**current, "drafts": [item for item in current["drafts"] if item["rfq_id"] in prepared_ids]}
        result["draft_preview"] = pure.render_rfq_report(preview)
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, OverflowError, RecursionError, ImportError) as error:
        result["warnings"].append(str(error))
    return result


def render_rfq_tracking_report(plan):
    lines = ["TEDARİKÇİ TEKLİF TALEBİ VE YANIT TAKİBİ", ""]
    if plan.get("draft_preview"):
        lines.append(plan["draft_preview"])
    else:
        lines.append("Hazırlanmış yeni teklif isteği taslağı yok.")
    if plan.get("drafts"):
        lines.extend(["", "KAYITLI TASLAKLAR", "Taslağın hazırlanması, gönderim veya yanıt bekleme kanıtı değildir."])
    labels = {"draft_prepared": "Yerel taslak hazır · Gönderilmedi",
              "awaiting_reply": "Haricen sağlanan gönderim kaydı var · Yanıt bekleniyor",
              "response_received": "Kaynaklı tedarikçi yanıtı kaydedildi",
              "deadline_elapsed_no_reply": "Haricen sağlanan gönderim kaydı var · Süre doldu, yanıt kaydı yok"}
    for draft in plan.get("drafts", []):
        lines.extend(["", draft["supplier_name"] + ": " + labels.get(draft["state"], "Durum doğrulanamadı"),
                      "Metin taslağı: " + draft["path"], "Görsel taslak: " + draft["html_path"]])
        if draft.get("artifact_status") != "verified":
            lines.append("Taslak dosyası eksik veya değişmiş; yeniden doğrulanmalı.")
        if draft.get("late_responses", 0):
            lines.append(f"Geç gelen yanıt: {draft['late_responses']} · Süre sonu sıralamasına katılmaz.")
    if not plan.get("draft_preview"):
        lines.extend("Not: " + warning for warning in plan.get("warnings", []))
        lines.extend("Bekleyen tedarikçi: " + str(item.get("supplier_id") or "(kimlik eksik)") + " — " + item["reason"]
                     for item in plan.get("blocked_suppliers", []))
    lines.extend(["", "Bu bileşen hiçbir e-posta göndermedi, posta kutusuna bağlanmadı ve sipariş oluşturmadı."])
    return "\n".join(lines).strip()


class SupplierRFQTracking(Component):
    display_name = "Teklif İste ve Yanıtları Takip Et"
    description = "Mevcut ve yeni tedarikçiler için ortak şartlarla yerel teklif isteği taslakları kaydeder; e-posta göndermez."
    name = "SupplierRFQTracking"
    icon = "MailCheck"
    inputs = [
        MessageTextInput(name="case_data", display_name="Süresi onaylı talep dosyası", required=True),
        TableInput(name="supplier_rows", display_name="Bu malzeme için teklif istenecek firmalar", value=[],
                   info="Mevcut/yeni firmaları seçin. Bu tablo yalnız bağlı talebin malzemesine uygulanır; yeni firmada kaynak ve teyitli adres gerekir.",
                   table_schema=[
                       {"name": "supplier_id", "display_name": "Firma kodu", "type": "str", "default": "", "edit_mode": EditMode.INLINE},
                       {"name": "name", "display_name": "Firma adı", "type": "str", "default": "", "edit_mode": EditMode.INLINE},
                       {"name": "email", "display_name": "Teklif e-postası", "type": "str", "default": "", "edit_mode": EditMode.INLINE},
                       {"name": "kind", "display_name": "Mevcut / yeni", "type": "str", "options": ["existing", "new"],
                        "default": "existing", "edit_mode": EditMode.INLINE},
                       {"name": "contact_confirmed", "display_name": "Adres teyitli", "type": "boolean", "default": False,
                        "edit_mode": EditMode.INLINE},
                       {"name": "source_url", "display_name": "Yeni firma kaynak adresi", "type": "str", "default": "",
                        "edit_mode": EditMode.INLINE},
                   ]),
        MessageTextInput(name="queue_path", display_name="Yerel talep kuyruğu", value="", advanced=True,
                         info="Boşsa satın alma agentının kayıtlı requests.db dosyası kullanılır."),
        MessageTextInput(name="output_dir", display_name="Yerel taslak klasörü", value="", advanced=True),
    ]
    outputs = [Output(name="plan", display_name="Kaydedilmiş teklif talepleri", method="build_plan", group_outputs=True),
               Output(name="report", display_name="Teklif istekleri ve takip durumu", method="build_report", group_outputs=True)]

    def _prepare(self):
        if not hasattr(self, "_rfq_tracking_prepared"):
            self._rfq_tracking_prepared = prepare_rfq_tracking(self.case_data, self.supplier_rows,
                                                              queue_path=self.queue_path, output_dir=self.output_dir)
            self.status = f"{len(self._rfq_tracking_prepared['drafts'])} kayıtlı teklif talebi · otomatik gönderim yok."
        return self._rfq_tracking_prepared

    def build_plan(self) -> Message:
        plan = {key: value for key, value in self._prepare().items() if key != "draft_preview"}
        return Message(text=RFQ_TRACKING_MARKER + json.dumps(plan, ensure_ascii=False, allow_nan=False))

    def build_report(self) -> Message:
        return Message(text=render_rfq_tracking_report(self._prepare()))
