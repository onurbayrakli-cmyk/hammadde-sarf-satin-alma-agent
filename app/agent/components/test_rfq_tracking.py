"""Persistent RFQ Langflow tests use synthetic temporary request databases."""

import copy
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from case_store import DEFAULT_PATH, CaseStore
from lfx.schema.data import Data
from lfx.schema.message import Message
from rfq_store import RFQStore
from rfq_tracking import (
    RFQ_TRACKING_MARKER,
    SupplierRFQTracking,
    _rt_dependencies,
    prepare_rfq_tracking,
    render_rfq_tracking_report,
)

TRACK_NOW = datetime(2030, 1, 2, 10, 0, tzinfo=timezone.utc)


def tracking_rows():
    return [{"supplier_id": "S1", "name": "Sentetik Mevcut", "email": "sales1@example.org", "kind": "existing",
             "contact_confirmed": True, "source_url": ""},
            {"supplier_id": "S2", "name": "Sentetik Yeni", "email": "sales2@example.org", "kind": "new",
             "contact_confirmed": True, "source_url": "https://supplier.example.org/contact"}]


class RFQTrackingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.queue = self.folder / "requests.db"
        self.output = self.folder / "drafts"
        self.store = CaseStore(self.queue)
        self.request = {"id": "RT-1", "material_code": "SARF-01", "material_name": "Sentetik sarf malzeme",
                        "quantity": "10", "unit": "ADET", "required_specs": {"Boyut": "20 mm"},
                        "requester": "owner@example.org", "mailbox": "procurement-bot@example.org", "message_id": "request-message-1"}
        self.store.upsert_request(self.request, "mail_request")
        self.store.set_deadline("RT-1", (TRACK_NOW + timedelta(days=2)).isoformat(), "owner@example.org", now=TRACK_NOW)
        self.case = self.store.snapshot("RT-1", now=TRACK_NOW)

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self, case=None, rows=None, **kwargs):
        return prepare_rfq_tracking(self.case if case is None else case, tracking_rows() if rows is None else rows,
                                    queue_path=self.queue, output_dir=self.output, now=TRACK_NOW, **kwargs)

    def rfq_table_exists(self):
        with sqlite3.connect(self.queue) as connection:
            return connection.execute("SELECT 1 FROM sqlite_master WHERE name='rfq_drafts'").fetchone() is not None

    def test_selected_table_uses_exact_case_material_and_persists_real_artifacts(self):
        result = self.prepare()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["drafts"]), 2)
        self.assertEqual(result["counts"], {"draft_prepared": 2})
        for draft in result["drafts"]:
            saved = json.loads(Path(draft["metadata_path"]).read_text())
            self.assertEqual(saved["material_code"], "SARF-01")
            self.assertEqual(saved["quantity"], "10")
            self.assertEqual(saved["unit"], "ADET")
            self.assertEqual(saved["required_specs"], {"Boyut": "20 mm"})
            self.assertEqual(draft["state"], "draft_prepared")
            self.assertFalse(draft["dispatch_evidence_recorded"])
            self.assertFalse(draft["network_send_verified"])
            self.assertTrue(Path(draft["path"]).is_file())
            self.assertTrue(Path(draft["html_path"]).is_file())
        self.assertFalse(result["emails_sent"])
        self.assertFalse(result["providers_called"])

    def test_second_prepare_reuses_saved_files_without_duplicate_rows(self):
        first, second = self.prepare(), self.prepare()
        self.assertEqual([item["path"] for item in first["drafts"]], [item["path"] for item in second["drafts"]])
        self.assertEqual(second["counts"], {"draft_prepared": 2})
        self.assertEqual(len(list(self.output.iterdir())), 2)

    def test_missing_database_does_not_create_database_parent_or_output(self):
        queue = self.folder / "absent" / "requests.db"
        output = self.folder / "absent-output"
        result = prepare_rfq_tracking(self.case, tracking_rows(), queue_path=queue, output_dir=output, now=TRACK_NOW)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["queue_status"], "not_created")
        self.assertFalse(queue.parent.exists())
        self.assertFalse(output.exists())

    def test_pending_case_does_not_write_schema_or_files(self):
        pending = copy.deepcopy(self.case)
        pending["request"].update(state="awaiting_deadline", deadline_at=None)
        before = self.queue.read_bytes()
        result = self.prepare(pending)
        self.assertFalse(result["drafts"])
        self.assertEqual(self.queue.read_bytes(), before)
        self.assertFalse(self.rfq_table_exists())
        self.assertFalse(self.output.exists())

    def test_pending_queue_cannot_be_opened_by_forged_collecting_input(self):
        with sqlite3.connect(self.queue) as connection:
            connection.execute("UPDATE requests SET deadline_at=NULL WHERE id='RT-1'")
        before = self.queue.read_bytes()
        result = self.prepare()
        self.assertFalse(result["drafts"])
        self.assertEqual(self.queue.read_bytes(), before)
        self.assertFalse(self.output.exists())

    def test_same_id_altered_material_quantity_unit_specs_delivery_or_deadline_is_rejected(self):
        changes = ({"material_code": "OTHER"}, {"material_name": "Başka malzeme"}, {"quantity": "11"},
                   {"unit": "KG"}, {"required_specs": {"Boyut": "21 mm"}}, {"delivery_place": "Başka depo"},
                   {"need_by": "2030-01-31"}, {"deadline_at": (TRACK_NOW + timedelta(days=3)).isoformat()})
        before = self.queue.read_bytes()
        for change in changes:
            with self.subTest(change=change):
                modified = copy.deepcopy(self.case)
                modified["request"].update(change)
                result = self.prepare(modified)
                self.assertEqual(result["status"], "blocked")
                self.assertFalse(result["drafts"])
                self.assertEqual(self.queue.read_bytes(), before)
                self.assertFalse(self.output.exists())

    def test_source_owner_or_mailbox_mismatch_does_not_create_drafts(self):
        for change in ({"requester": "another@example.org"}, {"mailbox": "other@example.org"},
                       {"message_id": "other-request-message"}):
            with self.subTest(change=change):
                modified = copy.deepcopy(self.case)
                modified["request"].update(change)
                self.assertFalse(self.prepare(modified)["drafts"])
                self.assertFalse(self.output.exists())
        modified = copy.deepcopy(self.case)
        modified["source"] = "other-agent"
        self.assertFalse(self.prepare(modified)["drafts"])

    def test_same_quantity_with_equivalent_decimal_form_is_accepted(self):
        modified = copy.deepcopy(self.case)
        modified["request"]["quantity"] = "10.000"
        self.assertEqual(self.prepare(modified)["status"], "ready")

    def test_empty_rows_and_invalid_new_contact_do_not_create_schema(self):
        self.assertFalse(self.prepare(rows=[])["drafts"])
        invalid = tracking_rows()[1]
        invalid["contact_confirmed"] = False
        self.assertFalse(self.prepare(rows=[invalid])["drafts"])
        self.assertFalse(self.rfq_table_exists())
        self.assertFalse(self.output.exists())

    def test_table_extra_material_codes_cannot_override_scope(self):
        rows = tracking_rows()
        rows[0]["material_codes"] = ["OTHER"]
        result = self.prepare(rows=rows)
        self.assertEqual(result["status"], "ready")
        saved = json.loads(Path(result["drafts"][0]["metadata_path"]).read_text())
        self.assertEqual(saved["material_code"], "SARF-01")

    def test_data_rows_and_case_marker_supported_without_mutation(self):
        rows = tracking_rows()
        original = copy.deepcopy((self.case, rows))
        payload = Message(text="TEKLİF DOSYASI (JSON)\n" + json.dumps(self.case))
        result = self.prepare(case=payload, rows=[Data(data=row) for row in rows])
        self.assertEqual(result["status"], "ready")
        self.assertEqual((self.case, rows), original)

    def test_expired_supplied_case_never_prepares_new_artifacts(self):
        result = prepare_rfq_tracking(self.case, tracking_rows(), queue_path=self.queue, output_dir=self.output,
                                      now=TRACK_NOW + timedelta(days=3))
        self.assertFalse(result["drafts"])
        self.assertFalse(self.output.exists())
        self.assertFalse(self.rfq_table_exists())

    def test_elapsed_unsent_existing_drafts_remain_unsent_and_read_only(self):
        self.prepare()
        before = self.queue.read_bytes()
        folders = sorted(item.name for item in self.output.iterdir())
        expired = self.store.snapshot("RT-1", now=TRACK_NOW + timedelta(days=3))
        result = prepare_rfq_tracking(expired, [], queue_path=self.queue, output_dir=self.output,
                                      now=TRACK_NOW + timedelta(days=3))
        self.assertEqual(result["status"], "tracking_only")
        self.assertTrue(result["read_only"])
        self.assertEqual(result["counts"], {"draft_prepared": 2})
        self.assertEqual(self.queue.read_bytes(), before)
        self.assertEqual(sorted(item.name for item in self.output.iterdir()), folders)
        report = render_rfq_tracking_report(result)
        self.assertIn("Yerel taslak hazır · Gönderilmedi", report)
        self.assertNotIn("Yanıt bekleniyor", report)
        self.assertNotIn("Süre doldu, yanıt kaydı yok", report)

    def test_elapsed_dispatched_no_reply_visible_without_new_draft(self):
        prepared = self.prepare()
        first = prepared["drafts"][0]
        sent_at = TRACK_NOW + timedelta(minutes=1)
        RFQStore(self.queue, self.output).record_dispatch(first["rfq_id"], {
            "mailbox": "procurement-bot@example.org", "message_id": "synthetic-out-1",
            "conversation_id": "synthetic-thread-1", "recipient": first["recipient"],
            "dispatched_at": sent_at.isoformat(),
        }, now=sent_at)
        before = self.queue.read_bytes()
        result = prepare_rfq_tracking(self.case, [], queue_path=self.queue, output_dir=self.output,
                                      now=TRACK_NOW + timedelta(days=3))
        self.assertEqual(result["counts"], {"deadline_elapsed_no_reply": 1, "draft_prepared": 1})
        self.assertEqual(self.queue.read_bytes(), before)
        self.assertFalse(result["network_send_verified"])
        self.assertFalse(result["emails_sent"])
        self.assertIn("Haricen sağlanan gönderim kaydı var · Süre doldu, yanıt kaydı yok", render_rfq_tracking_report(result))

    def test_expired_wrong_scope_does_not_expose_other_case_drafts(self):
        self.prepare()
        modified = self.store.snapshot("RT-1", now=TRACK_NOW + timedelta(days=3))
        modified["request"]["material_code"] = "OTHER"
        result = prepare_rfq_tracking(modified, [], queue_path=self.queue, output_dir=self.output,
                                      now=TRACK_NOW + timedelta(days=3))
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["drafts"])

    def test_saved_rfq_scope_or_deadline_cannot_be_relabeled_as_changed_case(self):
        self.prepare()
        changed_deadline = (TRACK_NOW + timedelta(days=2, hours=1)).isoformat()
        with sqlite3.connect(self.queue) as connection:
            connection.execute("UPDATE requests SET deadline_at=? WHERE id='RT-1'", (changed_deadline,))
        updated = self.store.snapshot("RT-1", now=TRACK_NOW + timedelta(days=3))
        before = self.queue.read_bytes()
        result = prepare_rfq_tracking(updated, [], queue_path=self.queue, output_dir=self.output,
                                      now=TRACK_NOW + timedelta(days=3))
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["drafts"])
        self.assertEqual(self.queue.read_bytes(), before)

    def test_late_source_response_remains_visible_and_labeled_after_cutoff(self):
        first = self.prepare()["drafts"][0]
        rfqs = RFQStore(self.queue, self.output)
        sent_at = TRACK_NOW + timedelta(minutes=1)
        received_at = TRACK_NOW + timedelta(days=2, minutes=1)
        rfqs.record_dispatch(first["rfq_id"], {
            "mailbox": "procurement-bot@example.org", "message_id": "synthetic-out-1",
            "conversation_id": "synthetic-thread-1", "recipient": first["recipient"],
            "dispatched_at": sent_at.isoformat(),
        }, now=sent_at)
        rfqs.ingest_response(first["rfq_id"], {
            "request_id": "RT-1", "supplier_id": first["supplier_id"], "conversation_id": "synthetic-thread-1",
            "mailbox": "procurement-bot@example.org", "offer": {
                "id": "SYNTHETIC-OFFER-1", "request_id": "RT-1", "supplier": first["supplier_name"],
                "material_code": "SARF-01", "quantity": "10", "unit": "ADET", "currency": "TRY",
                "cash_unit_price": "100", "email": {
                    "message_id": "synthetic-in-1", "mailbox": "procurement-bot@example.org",
                    "sender": first["recipient"], "received_at": received_at.isoformat(),
                    "subject": "Teklif", "body_excerpt": "Peşin birim fiyat 100 TRY/ADET",
                }, "source_fields": {"cash_unit_price": {"message_id": "synthetic-in-1", "quote": "Peşin birim fiyat 100 TRY/ADET"}},
            },
        }, now=received_at)
        before = self.queue.read_bytes()
        result = prepare_rfq_tracking(self.case, [], queue_path=self.queue, output_dir=self.output,
                                      now=TRACK_NOW + timedelta(days=3))
        late = next(item for item in result["drafts"] if item["rfq_id"] == first["rfq_id"])
        self.assertEqual(late["state"], "response_received")
        self.assertEqual(late["late_responses"], 1)
        self.assertIn("Geç gelen yanıt: 1 · Süre sonu sıralamasına katılmaz", render_rfq_tracking_report(result))
        self.assertEqual(self.queue.read_bytes(), before)

    def test_human_report_contains_local_draft_and_paths_without_waiting_claim(self):
        result = self.prepare()
        report = render_rfq_tracking_report(result)
        self.assertIn("Yerel taslak hazır · Gönderilmedi", report)
        self.assertIn("Peşin birim fiyatı", report)
        self.assertIn(result["drafts"][0]["html_path"], report)
        self.assertNotIn("Yanıt bekleniyor", report)
        self.assertNotIn(RFQ_TRACKING_MARKER, report)

    def test_component_has_table_selection_and_two_idempotent_outputs(self):
        component = SupplierRFQTracking()
        component.set(case_data=Message(text="TEKLİF DOSYASI (JSON)\n" + json.dumps(self.case)),
                      supplier_rows=tracking_rows(), queue_path=str(self.queue), output_dir=str(self.output))
        plan_message = component.build_plan()
        self.assertTrue(plan_message.text.startswith(RFQ_TRACKING_MARKER))
        plan = json.loads(plan_message.text[len(RFQ_TRACKING_MARKER):])
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(len(plan["drafts"]), 2)
        self.assertNotIn("draft_preview", plan)
        self.assertIn("Gönderilmedi", component.build_report().text)
        self.assertEqual(len(list(self.output.iterdir())), 2)

    def test_default_queue_comes_from_actual_case_store(self):
        modules = _rt_dependencies()
        self.assertEqual(modules[0].DEFAULT_PATH, DEFAULT_PATH)
        self.assertEqual(DEFAULT_PATH.name, "requests.db")
        table = next(item for item in SupplierRFQTracking.inputs if item.name == "supplier_rows")
        kind = next(item for item in table.table_schema if item["name"] == "kind")
        self.assertEqual(kind["options"], ["existing", "new"])
        self.assertTrue(all(item.advanced for item in SupplierRFQTracking.inputs if item.name in {"queue_path", "output_dir"}))

    def test_source_helpers_work_from_relocated_package_and_unrelated_cwd(self):
        source_root = Path(__file__).resolve().parents[1]
        package = self.folder.resolve() / "release" / "agent"
        components = package / "components"
        components.mkdir(parents=True)
        for name in ("case_store.py", "rfq_store.py", "runtime_config.py"):
            shutil.copyfile(source_root / name, package / name)
        for name in ("rfq_tracking.py", "rfq.py"):
            shutil.copyfile(source_root / "components" / name, components / name)
        unrelated = self.folder.resolve() / "unrelated-cwd"
        unrelated.mkdir()
        script = """import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from rfq_tracking import _rt_dependencies
print(json.dumps([str(Path(module.__file__).resolve()) for module in _rt_dependencies()]))
"""
        result = subprocess.run([sys.executable, "-c", script, str(components)], cwd=unrelated,
                                capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        paths = [Path(value) for value in json.loads(result.stdout)]
        self.assertEqual(paths, [package / "case_store.py", components / "rfq.py", package / "rfq_store.py"])
        self.assertFalse((package / "cases").exists())
        self.assertFalse((unrelated / ".codex").exists())

    def test_dynamic_source_without_real_helper_markers_is_rejected(self):
        incomplete = self.folder / "components" / "rfq_tracking.py"
        incomplete.parent.mkdir()
        incomplete.write_text("# synthetic incomplete source", encoding="utf-8")
        for name in ("case_store.py", "rfq_store.py"):
            (self.folder / name).write_text("# synthetic marker", encoding="utf-8")
        for source in (None, "<dynamic-component>", incomplete):
            with self.subTest(source=source):
                with (patch.object(Path, "cwd", return_value=self.folder.resolve()),
                      patch.dict(_rt_dependencies.__globals__, {"__file__": source})):
                    with self.assertRaisesRegex(ValueError, "yardımcı kaynak klasörü doğrulanamadı"):
                        _rt_dependencies()

    def test_fallback_preserves_wrong_project_module_rejection(self):
        import case_store

        with (patch.object(Path, "cwd", return_value=self.folder.resolve()),
              patch.object(case_store, "__file__", str(self.folder / "other-project" / "case_store.py"))):
            with self.assertRaisesRegex(ValueError, "başka bir proje klasöründen"):
                _rt_dependencies()


if __name__ == "__main__":
    unittest.main()
