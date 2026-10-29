"""SAP PR selection and identity tests use supplied synthetic JSON only."""

import copy
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from sap_intake import SAPProcurementInputComponent, fetch_sap_payload, normalize_sap_purchase_requisitions

PR_NOW = datetime(2030, 1, 2, 10, tzinfo=timezone.utc)
PR_MAP = {"deleted": "DeletedEvidence", "closed": "ClosedEvidence", "blocked": "BlockedEvidence", "released": "ReleasedEvidence",
          "requester_email": "OwnerEmail"}


def pr_row(**changes):
    return {"PurchaseRequisition": "0001234567", "PurchaseRequisitionItem": "00010", "Material": "MAT-01",
            "PurchaseRequisitionItemText": "Sentetik sarf kalemi", "RequestedQuantity": "10.50", "BaseUnit": "ADET",
            "OrderedQuantity": "0",
            "DeletedEvidence": False, "ClosedEvidence": False, "BlockedEvidence": False, "ReleasedEvidence": True,
            "OwnerEmail": "requester@example.org", **changes}


class SAPPurchaseRequisitionTests(unittest.TestCase):
    def normalize(self, rows=None, **kwargs):
        options = {"source_system": "DEV-200", "purchase_requisition": "0001234567",
                   "purchase_requisition_item": "00010", "now": PR_NOW}
        options.update(kwargs)
        payload = {"value": [pr_row()] if rows is None else rows}
        return normalize_sap_purchase_requisitions(payload, options.pop("field_map", PR_MAP), **options)

    def test_explicit_single_item_preserves_source_shape_and_zeroes(self):
        result = self.normalize()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["source"], "sap_api")
        self.assertEqual(result["data_kind"], "purchase_requisitions")
        self.assertEqual(len(result["materials"]), 1)
        self.assertEqual(result["request"]["sap_requisition"], {
            "source_system": "DEV-200", "purchase_requisition": "0001234567", "purchase_requisition_item": "00010"})
        self.assertEqual(result["request"]["quantity"], "10.5")
        self.assertEqual(result["request"]["state"], "awaiting_deadline")
        self.assertFalse(result["sap_connectivity_verified"])

    def test_multiple_items_never_auto_select_or_aggregate(self):
        rows = [pr_row(), pr_row(PurchaseRequisitionItem="00020", RequestedQuantity="20")]
        for selectors in ({"purchase_requisition": "", "purchase_requisition_item": ""},
                          {"purchase_requisition_item": ""}, {"purchase_requisition": ""}):
            with self.subTest(selectors=selectors):
                result = self.normalize(rows, **selectors)
                self.assertEqual(result["status"], "awaiting_selection")
                self.assertEqual(result["materials"], [])
                self.assertEqual(result["request"], {})
                self.assertEqual(len(result["requisitions"]), 2)
        selected = self.normalize(rows, purchase_requisition_item="00020")
        self.assertEqual(selected["request"]["quantity"], "20")
        self.assertEqual(selected["request"]["sap_requisition"]["purchase_requisition_item"], "00020")

    def test_even_single_row_requires_explicit_pair_selection(self):
        self.assertEqual(self.normalize(purchase_requisition="", purchase_requisition_item="")["status"], "awaiting_selection")

    def test_v2_d_results_supported_without_changing_identity(self):
        expected = self.normalize()
        actual = normalize_sap_purchase_requisitions({"d": {"results": [pr_row()]}}, PR_MAP,
            source_system="DEV-200", purchase_requisition="0001234567", purchase_requisition_item="00010", now=PR_NOW)
        self.assertEqual(actual, expected)

    def test_id_depends_only_on_namespace_pr_item_not_mutable_payload(self):
        first = self.normalize()["request"]["id"]
        changed = self.normalize([pr_row(RequestedQuantity="250", PurchaseRequisitionItemText="Yeni açıklama", Material="OTHER")])["request"]["id"]
        self.assertEqual(first, changed)
        self.assertTrue(first.startswith("SAPPR-"))
        self.assertEqual(len(first), 30)
        self.assertNotEqual(first, self.normalize(source_system="PRD-200")["request"]["id"])
        self.assertNotEqual(first, self.normalize([pr_row(PurchaseRequisitionItem="00020")], purchase_requisition_item="00020")["request"]["id"])

    def test_missing_duplicate_numeric_or_non_exact_source_keys_rejected(self):
        for rows in ([pr_row(), pr_row()], [pr_row(PurchaseRequisition="")],
                     [pr_row(PurchaseRequisition=1234567)], [pr_row(PurchaseRequisitionItem=10)],
                     [pr_row(PurchaseRequisitionItem=None)]):
            with self.subTest(rows=rows):
                result = self.normalize(rows)
                self.assertEqual(result["status"], "invalid")
                self.assertEqual(result["materials"], [])
        self.assertEqual(self.normalize(purchase_requisition_item="10")["status"], "invalid")

    def test_free_text_material_requires_review_without_invented_code(self):
        first = self.normalize([pr_row(Material="")])
        second = self.normalize([pr_row(Material=None, PurchaseRequisitionItemText="Başka açıklama")])
        self.assertEqual(first["status"], "awaiting_review")
        self.assertEqual(first["materials"], [])
        self.assertEqual(first["request"], {})
        self.assertTrue(first["selected_requisition"]["material_code_missing"])
        self.assertEqual(first["selected_requisition"]["material_code"], "")
        self.assertEqual(first["selected_requisition"]["request_id"], second["selected_requisition"]["request_id"])
        self.assertTrue(any("serbest metin" in warning and "üretilmedi" in warning for warning in first["warnings"]))
        self.assertFalse(self.normalize()["selected_requisition"]["material_code_missing"])

    def test_missing_eligibility_mapping_is_not_inferred_open(self):
        result = self.normalize(field_map={"requester_email": "OwnerEmail"})
        self.assertEqual(result["status"], "awaiting_review")
        self.assertEqual(result["request"], {})
        self.assertEqual(result["selected_requisition"]["eligibility"]["status"], "unverified")

    def test_string_or_numeric_flags_are_not_assumed_boolean(self):
        for field, value in (("DeletedEvidence", "false"), ("ClosedEvidence", 0), ("ReleasedEvidence", "X"),
                             ("ReleasedEvidence", "02"), ("ReleasedEvidence", 1), ("ReleasedEvidence", None)):
            with self.subTest(field=field, value=value):
                result = self.normalize([pr_row(**{field: value})])
                self.assertEqual(result["status"], "awaiting_review")
                self.assertEqual(result["materials"], [])

    def test_deleted_closed_or_unreleased_rows_are_blocked(self):
        for change in ({"DeletedEvidence": True}, {"ClosedEvidence": True}, {"ReleasedEvidence": False}):
            with self.subTest(change=change):
                result = self.normalize([pr_row(**change)])
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["materials"], [])
                self.assertEqual(result["request"], {})

    def test_released_but_blocked_row_cannot_open_research(self):
        result = self.normalize([pr_row(BlockedEvidence=True)])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["materials"], [])
        self.assertEqual(result["request"], {})
        self.assertIs(result["selected_requisition"]["eligibility"]["blocked"], True)

    def test_missing_or_non_boolean_blocked_status_requires_review(self):
        for value in (None, "", "false", 0, "X"):
            with self.subTest(value=value):
                result = self.normalize([pr_row(BlockedEvidence=value)])
                self.assertEqual(result["status"], "awaiting_review")
                self.assertEqual(result["materials"], [])
                self.assertEqual(result["request"], {})
        mapping = {key: value for key, value in PR_MAP.items() if key != "blocked"}
        self.assertEqual(self.normalize(field_map=mapping)["status"], "awaiting_review")

    def test_already_ordered_quantity_requires_review_without_remaining_quantity_inference(self):
        for amount, normalized in (("0.01", "0.01"), ("5", "5"), ("10.50", "10.5"), ("99", "99")):
            with self.subTest(amount=amount):
                result = self.normalize([pr_row(OrderedQuantity=amount)])
                self.assertEqual(result["status"], "awaiting_review")
                self.assertEqual(result["request"], {})
                self.assertEqual(result["materials"], [])
                self.assertEqual(result["selected_requisition"]["quantity"], "10.5")
                self.assertEqual(result["selected_requisition"]["ordered_quantity"], normalized)
                self.assertTrue(any("kalan miktar teyidi" in warning for warning in result["warnings"]))

    def test_missing_or_invalid_ordered_quantity_requires_review(self):
        missing = pr_row()
        missing.pop("OrderedQuantity")
        rows = [missing] + [pr_row(OrderedQuantity=value) for value in (None, "", "-1", True, "NaN", "1e2", "unknown")]
        for row in rows:
            with self.subTest(value=row.get("OrderedQuantity")):
                result = self.normalize([row])
                self.assertEqual(result["status"], "awaiting_review")
                self.assertIsNone(result["selected_requisition"]["ordered_quantity"])
                self.assertEqual(result["request"], {})
                self.assertEqual(result["materials"], [])
        self.assertEqual(self.normalize([pr_row(OrderedQuantity=0)])["status"], "ready")
        self.assertEqual(self.normalize([pr_row(OrderedQuantity="0.000000")])["status"], "ready")

    def test_sap_usernames_never_become_email_addresses(self):
        result = self.normalize([pr_row(OwnerEmail=None, CreatedByUser="SAPUSER", RequestedByUser="SAPUSER")])
        self.assertEqual(result["status"], "awaiting_requester")
        self.assertEqual(result["request"], {})
        fallback = self.normalize([pr_row(OwnerEmail=None, CreatedByUser="SAPUSER")], requester="owner@example.org")
        self.assertEqual(fallback["request"]["requester"], "owner@example.org")
        invalid = self.normalize([pr_row(OwnerEmail="SAPUSER")], requester="owner@example.org")
        self.assertEqual(invalid["status"], "invalid")

    def test_sap_dates_and_open_quantity_never_supply_research_deadline_or_requested_qty(self):
        row = pr_row(DeliveryDate="2030-02-01", CreationDate="2030-01-01", OpenQuantity="999", Quantity="888")
        result = self.normalize([row])
        self.assertIsNone(result["request"]["deadline_at"])
        self.assertEqual(result["request"]["quantity"], "10.5")
        self.assertNotEqual(result["request"]["quantity"], row["OpenQuantity"])
        missing_requested = self.normalize([pr_row(RequestedQuantity=None, OpenQuantity="999")])
        self.assertEqual(missing_requested["status"], "invalid")

    def test_missing_positive_quantity_unit_or_name_cannot_open_selected_case(self):
        for change in ({"RequestedQuantity": "0"}, {"RequestedQuantity": "-1"}, {"RequestedQuantity": True},
                       {"BaseUnit": ""}, {"PurchaseRequisitionItemText": ""}):
            with self.subTest(change=change):
                self.assertEqual(self.normalize([pr_row(**change)])["status"], "invalid")

    def test_owner_deadline_stays_explicit_timezone_aware_and_future(self):
        ready = self.normalize(deadline_at="2030-01-03T16:00:00+03:00")
        self.assertEqual(ready["request"]["state"], "research_ready")
        for value in ("2030-01-03T16:00:00", "2020-01-03T16:00:00+03:00", "invalid"):
            with self.subTest(value=value):
                self.assertEqual(self.normalize(deadline_at=value)["status"], "invalid")

    def test_custom_field_map_is_explicit_and_unknown_keys_rejected(self):
        row = {"PR": "0001234567", "Item": "00010", "Material": "MAT-01", "Description": "Sentetik", "Q": "5", "U": "KG", "Ordered": "0",
               "Deleted": False, "Closed": False, "Blocked": False, "Released": True, "Owner": "owner@example.org"}
        mapping = {"purchase_requisition": "PR", "purchase_requisition_item": "Item", "name": "Description",
                   "quantity": "Q", "ordered_quantity": "Ordered", "unit": "U", "deleted": "Deleted", "closed": "Closed", "blocked": "Blocked", "released": "Released",
                   "requester_email": "Owner"}
        self.assertEqual(self.normalize([row], field_map=mapping)["status"], "ready")
        self.assertEqual(self.normalize(field_map={"CreatedByUser": "Owner"})["status"], "invalid")

    def test_response_bounds_ambiguity_and_secrets_do_not_escape(self):
        for payload in ({"value": [pr_row()] * 201}, {"value": [], "d": {"results": [pr_row()]}},
                        '{"value": SECRET_FRAGMENT', {"value": [pr_row()], "unused": "x" * 1_048_576}):
            with self.subTest(payload_type=type(payload)):
                result = normalize_sap_purchase_requisitions(payload, PR_MAP, source_system="DEV-200", now=PR_NOW)
                self.assertEqual(result["status"], "invalid")
                self.assertNotIn("SECRET_FRAGMENT", json.dumps(result))

    def test_namespace_and_headers_cannot_be_missing_or_injected(self):
        for source in ("", "DEV 200", "DEV\nBcc: other@example.org"):
            with self.subTest(source=source):
                self.assertEqual(self.normalize(source_system=source)["status"], "invalid")
        self.assertEqual(self.normalize([pr_row(OwnerEmail="owner@example.org\r\nBcc:other@example.org")])["status"], "invalid")

    def test_input_payload_not_mutated_and_unknown_rows_not_auto_selected(self):
        rows = [pr_row(), pr_row(PurchaseRequisitionItem="00020", OwnerEmail="PRIVATE-OTHER-OWNER")]
        original = copy.deepcopy(rows)
        result = self.normalize(rows)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(rows, original)
        self.assertNotIn("PRIVATE-OTHER-OWNER", json.dumps(result))

    def test_component_connection_fields_blank_disabled_never_fetch(self):
        component = SAPProcurementInputComponent()
        component.set(data_kind="Satın alma talepleri")
        with patch("sap_intake.fetch_sap_payload", side_effect=AssertionError("No network may start")) as fetch:
            result = json.loads(component.build_context().text.split("TALEP GİRİŞİ (JSON)\n", 1)[1])
        fetch.assert_not_called()
        self.assertEqual(result["status"], "unconfigured")
        self.assertEqual(result["data_kind"], "purchase_requisitions")
        self.assertFalse(result["sap_connectivity_verified"])

    def test_component_pr_fixture_is_local_even_if_api_enabled(self):
        component = SAPProcurementInputComponent()
        component.set(data_kind="Satın alma talepleri", source_system="DEV-200", purchase_requisition="0001234567",
                      purchase_requisition_item="00010", pr_field_map=json.dumps(PR_MAP), fixture_payload=json.dumps({"value": [pr_row()]}),
                      enable_api=True)
        with patch("sap_intake.fetch_sap_payload", side_effect=AssertionError("No network may start")) as fetch:
            result = json.loads(component.build_context().text.split("TALEP GİRİŞİ (JSON)\n", 1)[1])
        fetch.assert_not_called()
        self.assertTrue(result["fixture_used"])
        self.assertFalse(result["sap_connectivity_verified"])
        self.assertEqual(result["status"], "ready")

    def test_transport_rejects_duplicate_json_keys_before_normalization(self):
        response = MagicMock(status=200, headers={})
        response.stream.return_value = [b'{"value":[{"PurchaseRequisition":"0001","BlockedEvidence":true,"BlockedEvidence":false}]}']
        pool = MagicMock()
        pool.urlopen.return_value = response
        with patch("sap_intake.socket.getaddrinfo", return_value=[(0, 0, 0, "", ("8.8.8.8", 443))]), patch(
                "urllib3.HTTPSConnectionPool", return_value=pool), self.assertRaisesRegex(ValueError, "SAP API okunamadı"):
            fetch_sap_payload("https://example.org/sap")
        response.close.assert_called_once()
        pool.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
