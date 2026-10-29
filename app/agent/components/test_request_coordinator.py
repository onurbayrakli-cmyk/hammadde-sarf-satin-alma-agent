"""Real intake/coordinator/SQLite contracts with synthetic temporary requests."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from case_store import CaseStore
from lfx.schema.message import Message
from mail_intake import normalize_mail_request
from request_coordinator import CASE_MARKER, INTAKE_MARKER, ProcurementRequestCoordinator, coordinate_request
from sap_intake import normalize_sap_payload
from stock_rules import analyze_table


def sap_context(count=2, *, deadline=None):
    rows = [{"Material": f"SAP-{index}", "MaterialDescription": f"Sentetik malzeme {index}",
             "BaseUnit": "KG", "Quantity": "5", "UnrestrictedStock": "50", "ReservedStock": "0",
             "DailyConsumption": "10", "LeadTimeDays": "5", "SafetyStock": "10", "OpenOrderQuantity": "0"}
            for index in range(count)]
    result = normalize_sap_payload({"value": rows}, requester="owner@example.org", deadline_at=deadline or "")
    result["request"]["id"] = "SAP-BATCH-TEST"
    return result


def mail_context(*, deadline=None):
    return normalize_mail_request({"request_id": "MAIL-TEST", "message_id": "request-source-message",
                                   "sender": "owner@example.org", "material_name": "Sentetik PP",
                                   "material_code": "PP", "quantity": "5", "unit": "KG",
                                   "deadline_at": deadline or ""}, bot_mailbox="bot@example.org")


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = CaseStore(Path(self.temporary.name) / "requests.db")
        self.future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    def tearDown(self):
        self.temporary.cleanup()

    def test_all_sap_materials_registered_while_deadline_pending(self):
        result = coordinate_request(sap_context(), store=self.store)
        self.assertFalse(result["ready"])
        self.assertEqual(len(result["context"]["materials"]), 2)
        self.assertEqual([case["request"]["id"] for case in result["cases"]],
                         ["SAP-BATCH-TEST-SAP-0", "SAP-BATCH-TEST-SAP-1"])
        self.assertTrue(all(case["request"]["state"] == "awaiting_deadline" for case in result["cases"]))
        self.assertIn("Sentetik malzeme 0", result["reason"])
        self.assertIn("Sentetik malzeme 1", result["reason"])

    def test_all_sap_materials_ready_with_common_owner_and_deadline(self):
        result = coordinate_request(sap_context(deadline=self.future), store=self.store)
        self.assertTrue(result["ready"])
        self.assertEqual(len(result["cases"]), 2)
        self.assertEqual(result["context"]["request"]["state"], "collecting")
        self.assertTrue(all(case["request"]["requester"] == "owner@example.org" for case in result["cases"]))
        self.assertEqual(len({case["request"]["deadline_at"] for case in result["cases"]}), 1)

    def test_ten_materials_supported_without_silent_discard(self):
        result = coordinate_request(sap_context(10, deadline=self.future), store=self.store)
        self.assertTrue(result["ready"])
        self.assertEqual(len(result["cases"]), 10)
        self.assertEqual(len(result["context"]["materials"]), 10)

    def test_over_limit_rejected_before_creating_cases(self):
        result = coordinate_request(sap_context(11, deadline=self.future), store=self.store)
        self.assertFalse(result["ready"])
        self.assertEqual(result["cases"], [])
        self.assertIn("10", result["reason"])
        with self.assertRaises(ValueError):
            self.store.snapshot("SAP-BATCH-TEST-SAP-0")

    def test_explicit_selection_keeps_only_requested_sap_code(self):
        result = coordinate_request(sap_context(11, deadline=self.future), "SAP-7", store=self.store)
        self.assertTrue(result["ready"])
        self.assertEqual([item["material_code"] for item in result["context"]["materials"]], ["SAP-7"])
        self.assertEqual(result["case"]["request"]["id"], "SAP-BATCH-TEST-SAP-7")

    def test_same_single_sap_id_with_or_without_explicit_selection(self):
        context = sap_context(1, deadline=self.future)
        first = coordinate_request(context, store=self.store)
        second = coordinate_request(context, "SAP-0", store=self.store)
        self.assertEqual(first["case"]["request"]["id"], second["case"]["request"]["id"])

    def test_duplicate_material_codes_rejected(self):
        context = sap_context()
        context["materials"][1]["material_code"] = "SAP-0"
        result = coordinate_request(context, store=self.store)
        self.assertFalse(result["ready"])
        self.assertEqual(result["cases"], [])

    def test_email_requires_source_message_and_one_material(self):
        context = mail_context(deadline=self.future)
        context["materials"].append(dict(context["materials"][0]))
        self.assertFalse(coordinate_request(context, store=self.store)["ready"])
        context = mail_context(deadline=self.future)
        context["request"]["message_id"] = None
        result = coordinate_request(context, store=self.store)
        self.assertFalse(result["ready"])
        self.assertIn("kimliği", result["reason"])

    def _expire_saved_mail(self):
        try:
            existing = self.store.snapshot("MAIL-TEST")
            if existing["request"]["deadline_at"]:
                return mail_context(deadline=existing["request"]["deadline_at"])
        except ValueError:
            pass
        context = mail_context()
        self.assertFalse(coordinate_request(context, store=self.store)["ready"])
        now = datetime.now(timezone.utc)
        past = now - timedelta(minutes=1)
        self.store.set_deadline("MAIL-TEST", past.isoformat(), "owner@example.org",
                                now=now - timedelta(minutes=2))
        return mail_context(deadline=past.isoformat())

    def test_expired_mail_resumes_confirmed_case_snapshot(self):
        expired_context = self._expire_saved_mail()
        self.assertEqual(expired_context["status"], "invalid")
        result = coordinate_request(expired_context, store=self.store)
        self.assertTrue(result["ready"])
        self.assertEqual(result["case"]["request"]["state"], "ready_for_report")
        self.assertEqual(result["context"]["request"]["deadline_at"], result["case"]["request"]["deadline_at"])
        self.assertTrue(result["context"]["warnings"])

    def test_new_invalid_deadline_does_not_open_case(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        result = coordinate_request(mail_context(deadline=past), store=self.store)
        self.assertFalse(result["ready"])
        self.assertEqual(result["cases"], [])
        with self.assertRaises(ValueError):
            self.store.snapshot("MAIL-TEST")

    def test_expired_case_cannot_change_immutable_material_or_owner(self):
        for field, value in (("name", "Başka malzeme"), ("quantity", "6")):
            context = self._expire_saved_mail()
            context["materials"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                coordinate_request(context, store=self.store)
        context = self._expire_saved_mail()
        context["request"]["requester"] = "other@example.org"
        with self.assertRaises(ValueError):
            coordinate_request(context, store=self.store)

    def test_non_deadline_errors_never_bypass_even_existing_case(self):
        context = self._expire_saved_mail()
        context["errors"].append("Eksik malzeme adı")
        result = coordinate_request(context, store=self.store)
        self.assertFalse(result["ready"])
        self.assertEqual(result["cases"], [])

    def test_expired_sap_batch_resumes_all_original_cases(self):
        context = sap_context()
        registered = coordinate_request(context, store=self.store)
        now = datetime.now(timezone.utc)
        deadline = (now - timedelta(minutes=1)).isoformat()
        for case in registered["cases"]:
            self.store.set_deadline(case["request"]["id"], deadline, "owner@example.org",
                                    now=now - timedelta(minutes=2))
        expired = sap_context(deadline=deadline)
        result = coordinate_request(expired, store=self.store)
        self.assertTrue(result["ready"])
        self.assertEqual(len(result["cases"]), 2)
        self.assertTrue(all(case["request"]["state"] == "ready_for_report" for case in result["cases"]))

    def test_real_component_emits_batch_case_bundle_and_stock_rows(self):
        component = ProcurementRequestCoordinator()
        component.set(sap_context=Message(text=INTAKE_MARKER + json.dumps(sap_context(deadline=self.future))),
                      mail_context="", input_source="SAP API", selected_material_code="")
        with patch("request_coordinator.CaseStore", return_value=self.store):
            table = component.build_stock_table().text
            bundle = json.loads(component.build_case_data().text.split(CASE_MARKER, 1)[1])
            pending = component.build_pending().text
        self.assertEqual(len(bundle["cases"]), 2)
        self.assertEqual(pending, "")
        stock = analyze_table(table)
        self.assertEqual([item["malzeme_kodu"] for item in stock["rows"]], ["SAP-0", "SAP-1"])
        self.assertEqual([item["onerilen_miktar"] for item in stock["rows"]], [10, 10])

    def test_single_mail_component_preserves_old_case_schema(self):
        component = ProcurementRequestCoordinator()
        component.set(sap_context="", mail_context=INTAKE_MARKER + json.dumps(mail_context(deadline=self.future)),
                      input_source="E-posta özel talep", selected_material_code="")
        with patch("request_coordinator.CaseStore", return_value=self.store):
            bundle = json.loads(component.build_case_data().text.split(CASE_MARKER, 1)[1])
        self.assertIn("request", bundle)
        self.assertNotIn("cases", bundle)


if __name__ == "__main__":
    unittest.main()
