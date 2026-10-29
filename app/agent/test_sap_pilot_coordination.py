"""SAP PR selection preserves document/item identities through the existing queue."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "components"))
from case_store import CaseStore
from request_coordinator import coordinate_request


def context(request_id="SAPPR-ONE", item="00010"):
    return {
        "source": "sap_api",
        "data_kind": "purchase_requisitions",
        "status": "ready",
        "errors": [],
        "materials": [{"material_code": "MAT-ONE", "name": "Sentetik sarf", "quantity": "10", "unit": "KG"}],
        "request": {
            "id": request_id,
            "requester": "owner@example.com",
            "deadline_at": None,
            "sap_requisition": {
                "source_system": "SYNTHETIC-200",
                "purchase_requisition": "0000123456",
                "purchase_requisition_item": item,
            },
        },
    }


class SapPilotCoordinationTests(unittest.TestCase):
    def test_pr_case_keeps_identity_without_material_suffix_and_replays(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CaseStore(Path(folder) / "cases.db")
            source = context()
            first = coordinate_request(source, store=store)
            second = coordinate_request(source, store=store)
            self.assertEqual(first["case"]["request"]["id"], "SAPPR-ONE")
            self.assertEqual(second["case"]["request"]["id"], "SAPPR-ONE")
            self.assertFalse(first["ready"])
            self.assertEqual(first["case"]["request"]["state"], "awaiting_deadline")
            self.assertEqual(first["case"]["request"]["sap_requisition"], source["request"]["sap_requisition"])

    def test_same_material_in_two_requisition_items_stays_two_cases(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CaseStore(Path(folder) / "cases.db")
            a = coordinate_request(context(), store=store)
            b = coordinate_request(context("SAPPR-TWO", "00020"), store=store)
            self.assertNotEqual(a["case"]["request"]["id"], b["case"]["request"]["id"])
            self.assertEqual(a["case"]["request"]["quantity"], "10")
            self.assertEqual(b["case"]["request"]["quantity"], "10")

    def test_missing_source_keys_cannot_open_a_pr_case(self):
        source = context()
        del source["request"]["sap_requisition"]
        result = coordinate_request(source)
        self.assertFalse(result["ready"])
        self.assertIsNone(result["case"])

    def test_unreviewed_or_unselected_pr_stays_pending_without_creating_queue(self):
        for status in ("awaiting_selection", "awaiting_review", "blocked", "unconfigured"):
            source = copy.deepcopy(context())
            source["status"] = status
            result = coordinate_request(source)
            self.assertFalse(result["ready"])
            self.assertIsNone(result["case"])


if __name__ == "__main__":
    unittest.main()
