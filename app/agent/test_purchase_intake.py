"""Manual purchasing scope, persistence and no-SAP branch regressions."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / "components"))

from case_store import CaseStore
from case_worker import prepare_due
from purchase_intake import PurchaseRequestInput, normalize_purchase_request, purchase_specs
from request_coordinator import CASE_MARKER, INTAKE_MARKER, ProcurementRequestCoordinator, coordinate_request
from test_case_store import AFTER
from test_case_worker import offer_payload


class PurchaseIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.now = datetime.now(timezone.utc)
        self.fields = dict(request_id="PUR-1", material_name="Sentetik sarf torbası", material_code="BAG",
                           material_kind="Sarf malzeme", quantity="12,5", unit="KG", required_specs="Kalınlık: 30 mikron",
                           delivery_place="Deneme depo", need_by="2030-12-20", requester="owner@example.com",
                           deadline_at=(self.now + timedelta(days=1)).isoformat())

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_manual_input_is_blocked_without_source_connection(self):
        result = normalize_purchase_request()
        self.assertEqual(result["status"], "unconfigured")
        self.assertFalse(result["outbound_sent"])
        self.assertFalse(coordinate_request(result)["ready"])

    def test_material_scope_and_decimal_preserved(self):
        result = normalize_purchase_request(**self.fields)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["source"], "manual")
        self.assertEqual(result["request"]["material_kind"], "sarf")
        self.assertEqual(result["materials"][0]["quantity"], "12.5")
        self.assertEqual(result["materials"][0]["required_specs"], {"Kalınlık": "30 mikron"})

    def test_direct_request_no_sap_or_mail_message_needed(self):
        store = CaseStore(self.folder / "requests.db")
        result = coordinate_request(normalize_purchase_request(**self.fields), store=store)
        self.assertTrue(result["ready"])
        self.assertEqual(result["case"]["source"], "manual")
        self.assertEqual(result["case"]["request"]["delivery_place"], "Deneme depo")
        self.assertEqual(result["case"]["request"]["need_by"], "2030-12-20")

    def test_no_deadline_asks_owner_and_blocks_work(self):
        store = CaseStore(self.folder / "requests.db")
        result = coordinate_request(normalize_purchase_request(**{**self.fields, "deadline_at": ""}), store=store)
        self.assertFalse(result["ready"])
        self.assertEqual(result["case"]["request"]["state"], "awaiting_deadline")
        self.assertIn("hangi tarih", result["reason"])

    def test_bad_owner_quantity_unit_date_or_header_rejected(self):
        for field, value in [("requester", "bad"), ("quantity", "0"), ("quantity", "-1"), ("unit", ""),
                             ("material_name", "Torba\nBcc: someone"), ("need_by", "yarın"),
                             ("deadline_at", "2030-01-01T10:00:00")]:
            with self.subTest(field=field):
                self.assertEqual(normalize_purchase_request(**{**self.fields, field: value})["status"], "invalid")

    def test_specs_missing_not_invented_and_ambiguous_list_rejected(self):
        result = normalize_purchase_request(**{**self.fields, "required_specs": ""})
        self.assertEqual(result["request"]["required_specs"], {})
        self.assertTrue(result["warnings"])
        for value in ["Kalınlık 30", "Kalınlık: 30\nkalınlık: 50", "Kalite:"]:
            with self.assertRaises(ValueError):
                purchase_specs(value)

    def test_manual_id_is_idempotent_and_immutable_delivery_scope(self):
        store = CaseStore(self.folder / "requests.db")
        context = normalize_purchase_request(**self.fields)
        first = coordinate_request(context, store=store)
        second = coordinate_request(context, store=store)
        self.assertEqual(first["case"]["request"]["id"], second["case"]["request"]["id"])
        with self.assertRaises(ValueError):
            coordinate_request(normalize_purchase_request(**{**self.fields, "delivery_place": "Farklı depo"}), store=store)

    def test_expired_new_manual_request_cannot_open_queue_case(self):
        store = CaseStore(self.folder / "requests.db")
        result = normalize_purchase_request(**{**self.fields, "deadline_at": (self.now - timedelta(days=1)).isoformat()})
        self.assertFalse(coordinate_request(result, store=store)["ready"])
        with self.assertRaises(ValueError):
            store.snapshot("PUR-1")

    def test_same_registered_manual_case_resumes_after_deadline(self):
        store = CaseStore(self.folder / "requests.db")
        context = normalize_purchase_request(**self.fields)
        coordinate_request(context, store=store)
        old = normalize_purchase_request(**self.fields, now=self.now + timedelta(days=2))
        self.assertTrue(coordinate_request(old, store=store)["ready"])

    def test_component_default_is_manual_and_other_sources_not_required(self):
        store = CaseStore(self.folder / "requests.db")
        component = ProcurementRequestCoordinator()
        component.set(input_source="Malzeme talebi", manual_context=INTAKE_MARKER + json.dumps(normalize_purchase_request(**self.fields)),
                      sap_context="", mail_context="", selected_material_code="")
        with patch("request_coordinator.CaseStore", return_value=store):
            case = json.loads(component.build_case_data().text.split(CASE_MARKER, 1)[1])
        self.assertEqual(case["source"], "manual")
        self.assertEqual(case["request"]["material_code"], "BAG")

    def test_new_manual_component_emits_local_context_only(self):
        component = PurchaseRequestInput()
        component.set(**self.fields)
        result = json.loads(component.build_context().text.split(INTAKE_MARKER, 1)[1])
        self.assertEqual(result["status"], "ready")
        self.assertFalse(result["outbound_sent"])


class ReportArtifactIsolationTests(unittest.TestCase):
    def test_changed_or_missing_artifact_is_not_prepared_and_is_rebuilt(self):
        from test_case_worker import CaseWorkerTests
        from case_worker import read_queue_counts
        fixture = CaseWorkerTests()
        fixture.setUp()
        try:
            store = fixture.store()
            for damage in ("empty_text", "changed_html", "missing_manifest"):
                prepared = prepare_due(fixture.queue, fixture.output, AFTER,
                                       analyze=lambda _: {"report_status": "final"}, render=lambda _: "Kaynaklı rapor")
                self.assertEqual(prepared["report_count"], 1)
                current = prepared["reports"][0]
                if damage == "empty_text":
                    Path(current["path"]).write_text("")
                elif damage == "changed_html":
                    Path(current["html_path"]).write_text("Başka rapor")
                else:
                    Path(current["manifest_path"]).unlink()
                self.assertEqual(store.snapshot("R1", AFTER)["request"]["state"], "ready_for_report")
                self.assertEqual(read_queue_counts(fixture.queue, AFTER)["report_prepared"], 0)
            fixed = prepare_due(fixture.queue, fixture.output, AFTER,
                                analyze=lambda _: {"report_status": "final"}, render=lambda _: "Kaynaklı rapor")
            self.assertEqual(fixed["report_count"], 1)
            self.assertEqual(store.snapshot("R1", AFTER)["request"]["state"], "report_prepared")
            self.assertEqual(read_queue_counts(fixture.queue, AFTER)["report_prepared"], 1)
        finally:
            fixture.tearDown()

    def test_stale_process_does_not_replace_fresh_files(self):
        from test_case_worker import CaseWorkerTests
        fixture = CaseWorkerTests()
        fixture.setUp()
        try:
            store = fixture.store()
            captured = {}

            def stale_analyze(snapshot):
                store.append_offer("R1", offer_payload())
                fresh = prepare_due(fixture.queue, fixture.output, AFTER,
                                    analyze=lambda _: {"report_status": "final"}, render=lambda _: "Yeni kaynaklı rapor")
                captured.update(fresh["reports"][0])
                return {"report_status": "final"}

            stale = prepare_due(fixture.queue, fixture.output, AFTER, analyze=stale_analyze, render=lambda _: "Eski rapor")
            self.assertEqual(stale["status"], "report_preparation_failed")
            self.assertTrue(Path(captured["path"]).read_text().startswith("Yeni kaynaklı rapor"))
            self.assertIn("Yeni kaynaklı rapor", Path(captured["html_path"]).read_text())
            self.assertEqual(store.snapshot("R1", AFTER)["report_path"], captured["path"])
            manifest = json.loads(Path(captured["manifest_path"]).read_text())
            self.assertEqual(manifest["offer_revision"], 1)
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
