"""SAP PR source identity tests; isolated synthetic queues, no SAP connection."""

import copy
import tempfile
import unittest
from pathlib import Path

from case_store import CaseStore


def sap_request():
    return {"id": "SAPPR-synthetic-item", "material_code": "000000000000000123", "material_name": "Sentetik hammadde",
            "quantity": "10", "unit": "KG", "required_specs": {}, "requester": None,
            "sap_requisition": {"source_system": "SYNTHETIC-DEV:200", "purchase_requisition": "0000123456",
                                "purchase_requisition_item": "00010"}}


class SAPCaseMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "requests.db"
        self.store = CaseStore(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_exact_metadata_persisted_leading_zeros_preserved_no_owner_guess(self):
        request = sap_request()
        result = self.store.upsert_request(request, "sap_api")
        saved = result["request"]
        self.assertEqual(saved["sap_requisition"], request["sap_requisition"])
        self.assertEqual(saved["material_code"], "000000000000000123")
        self.assertIsNone(saved["requester"])
        self.assertIsNone(saved["deadline_at"])
        self.assertEqual(saved["state"], "awaiting_deadline")
        self.assertFalse(result["question_draft"]["sent"])

    def test_metadata_immutable_and_identical_source_replay_allowed(self):
        request = sap_request()
        self.store.upsert_request(request, "sap")
        replay = self.store.upsert_request(copy.deepcopy(request), "sap")
        self.assertEqual(replay["request"]["sap_requisition"], request["sap_requisition"])
        for field in request["sap_requisition"]:
            changed = copy.deepcopy(request)
            changed["sap_requisition"][field] += "2"
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.upsert_request(changed, "sap")

    def test_metadata_rejected_for_non_sap_sources(self):
        for source in ("manual", "mail_request"):
            request = sap_request()
            request.update(requester="owner@example.com", message_id="synthetic-mail-1")
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.store.upsert_request(request, source)

    def test_exact_fields_required_unknown_missing_and_nonobject_rejected(self):
        metadata = sap_request()["sap_requisition"]
        variants = [None, [], "PR", {}, {**metadata, "purchase_order": "NOT-A-PR"},
                    {key: value for key, value in metadata.items() if key != "purchase_requisition_item"}]
        for index, value in enumerate(variants):
            request = sap_request()
            request["sap_requisition"] = value
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.store.upsert_request(request, "sap")

    def test_identity_fields_are_nonempty_bounded_strings_not_numbers(self):
        for field in sap_request()["sap_requisition"]:
            for value in (None, "", " ", True, 10, "x" * 101, "line\nbreak"):
                request = sap_request()
                request["sap_requisition"][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.store.upsert_request(request, "sap")

    def test_legacy_sap_without_metadata_keeps_original_shape_and_replay(self):
        request = sap_request()
        request.pop("sap_requisition")
        first = self.store.upsert_request(request, "sap")
        second = self.store.upsert_request(request, "sap_api")
        self.assertNotIn("sap_requisition", first["request"])
        self.assertEqual(first["request"], second["request"])


if __name__ == "__main__":
    unittest.main()
