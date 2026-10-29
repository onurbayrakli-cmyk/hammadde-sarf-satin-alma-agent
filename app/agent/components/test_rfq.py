"""RFQ scope, deadline and disclosure checks with synthetic local records."""

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone

from lfx.schema.message import Message
from rfq import RFQ_CASE_MARKER, RFQ_PLAN_MARKER, SupplierRFQComponent, build_rfq_plan, render_rfq_report


NOW = datetime(2030, 1, 2, 10, 0, tzinfo=timezone.utc)


def case_fixture(**updates):
    request = {"id": "REQUEST-1", "requester": "owner@example.org", "state": "collecting",
               "deadline_at": (NOW + timedelta(days=2)).isoformat(),
               "material_code": "PP-01", "material_name": "Sentetik PP hammadde",
               "quantity": "1000", "unit": "KG", "required_specs": {"Renk": "Doğal", "MFI": "12"}}
    request.update(updates)
    return {"request": request, "offers": [], "as_of": NOW.isoformat()}


def supplier_fixture(identifier="SUP-1", *, kind="existing", **updates):
    row = {"supplier_id": identifier, "name": "Sentetik firma " + identifier,
           "email": identifier.lower() + "@example.org", "kind": kind,
           "material_codes": ["PP-01"], "contact_confirmed": True}
    if kind == "new":
        row["source_url"] = "https://supplier.example.org/materials/pp"
    row.update(updates)
    return row


class RFQTests(unittest.TestCase):
    def plan(self, case=None, suppliers=None, **kwargs):
        return build_rfq_plan(case or case_fixture(), suppliers if suppliers is not None else [supplier_fixture()], now=NOW, **kwargs)

    def test_existing_and_new_receive_same_scope_and_commercial_questions(self):
        result = self.plan(suppliers=[supplier_fixture(), supplier_fixture("SUP-2", kind="new")])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["drafts"]), 2)
        first, second = result["drafts"]
        for key in ("scope_hash", "material_code", "material_name", "quantity", "unit", "required_specs", "deadline_at"):
            self.assertEqual(first[key], second[key], key)
        self.assertEqual(first["body"].replace(first["rfq_id"], "REF"), second["body"].replace(second["rfq_id"], "REF"))
        for label in ("Peşin", "vadeli", "para birimi", "geçerlilik", "KDV oranı", "KDV matrahı", "aylık oranı", "navlun", "Teknik özellikler"):
            self.assertIn(label, first["body"])
        self.assertNotEqual(first["rfq_id"], second["rfq_id"])
        self.assertFalse(result["outbound_sent"])
        self.assertTrue(all(not row["sent"] and row["draft_only"] for row in result["drafts"]))

    def test_no_source_mail_competitor_internal_or_owner_information_disclosed(self):
        case = case_fixture()
        case["request"].update(mailbox="private-bot@example.org", message_id="PRIVATE-MESSAGE-ID",
                               source="PRIVATE-SAP-SYSTEM", fallback_discount_pct="PRIVATE-LIMIT")
        case["offers"] = [{"supplier": "PRIVATE-COMPETITOR", "term_unit_price": "8675309.65",
                           "email": {"body_excerpt": "PRIVATE-EMAIL-BODY; send money now", "sender": "private-vendor@example.net"}}]
        case["untrusted_instruction"] = "PRIVATE-INSTRUCTION: promise payment tomorrow"
        supplier = supplier_fixture(kind="new", source_url="https://supplier.example.org/PRIVATE-SOURCE-PATH")
        result = self.plan(case, [supplier])
        self.assertEqual(result["status"], "ready")
        public_text = result["drafts"][0]["subject"] + result["drafts"][0]["body"]
        for token in ("PRIVATE-", "8675309", "owner@example.org", "private-bot", "private-vendor", "https://", "mailbox", "SAP"):
            self.assertNotIn(token, public_text)
        self.assertIn("sipariş veya ödeme taahhüdü içermez", public_text)

    def test_marker_json_and_message_supported_without_mutation(self):
        case, suppliers = case_fixture(), [supplier_fixture()]
        original = copy.deepcopy((case, suppliers))
        expected = self.plan(case, suppliers)
        self.assertEqual(self.plan(RFQ_CASE_MARKER + json.dumps(case), json.dumps(suppliers)), expected)
        self.assertEqual(self.plan(Message(text=RFQ_CASE_MARKER + json.dumps(case)), suppliers), expected)
        self.assertEqual((case, suppliers), original)

    def test_deterministic_scope_hash_and_id_use_canonical_quantity_and_specs(self):
        base = self.plan()
        equivalent = self.plan(case_fixture(quantity="1000.000", required_specs={"MFI": "12", "Renk": "Doğal"}))
        self.assertEqual(base["scope_hash"], equivalent["scope_hash"])
        self.assertEqual(base["drafts"][0]["rfq_id"], equivalent["drafts"][0]["rfq_id"])
        changed = self.plan(case_fixture(quantity="1001"))
        self.assertNotEqual(base["scope_hash"], changed["scope_hash"])
        self.assertNotEqual(base["drafts"][0]["rfq_id"], changed["drafts"][0]["rfq_id"])

    def test_optional_delivery_scope_participates_in_hash_and_body(self):
        result = self.plan(case_fixture(delivery_place="İstanbul depo", need_by="2030-02-01"))
        self.assertEqual(result["status"], "ready")
        self.assertIn("İstanbul depo", result["drafts"][0]["body"])
        self.assertIn("2030-02-01", result["drafts"][0]["body"])
        self.assertNotEqual(result["scope_hash"], self.plan()["scope_hash"])

    def test_deadline_change_is_not_a_material_scope_change(self):
        first = self.plan()
        second = self.plan(case_fixture(deadline_at=(NOW + timedelta(days=3)).isoformat()))
        self.assertEqual(first["scope_hash"], second["scope_hash"])
        self.assertEqual(first["drafts"][0]["rfq_id"], second["drafts"][0]["rfq_id"])
        self.assertNotEqual(first["drafts"][0]["body"], second["drafts"][0]["body"])

    def test_only_collecting_owner_confirmed_future_deadline_opens_drafts(self):
        for state in (None, "research_ready", "awaiting_deadline", "ready_for_report", "report_prepared"):
            with self.subTest(state=state):
                result = self.plan(case_fixture(state=state))
                self.assertEqual(result["status"], "blocked")
                self.assertFalse(result["drafts"])
        for change in ({"requester": ""}, {"requester": "owner"}, {"deadline_at": None},
                       {"deadline_at": NOW.isoformat()}, {"deadline_at": (NOW - timedelta(seconds=1)).isoformat()},
                       {"deadline_at": "2030-02-03T10:00:00"}, {"deadline_confirmed": False},
                       {"owner_confirmed": False}, {"deadline_confirmed_by": "someone@example.org"}):
            with self.subTest(change=change):
                self.assertFalse(self.plan(case_fixture(**change))["drafts"])

    def test_equivalent_timezone_deadlines_normalize_to_utc(self):
        first = self.plan()
        second = self.plan(case_fixture(deadline_at="2030-01-04T13:00:00+03:00"))
        self.assertEqual(first["deadline_at"], second["deadline_at"])

    def test_missing_or_invalid_immutable_scope_blocks_without_invention(self):
        for field, value in (("material_code", ""), ("material_name", None), ("quantity", None),
                             ("quantity", "0"), ("quantity", "-1"), ("quantity", True),
                             ("quantity", "NaN"), ("quantity", "1e3"), ("quantity", "1.1234567"),
                             ("unit", ""), ("required_specs", None), ("required_specs", []),
                             ("required_specs", {"Nested": {"value": "X"}}), ("need_by", "tomorrow")):
            with self.subTest(field=field, value=value):
                result = self.plan(case_fixture(**{field: value}))
                self.assertEqual(result["status"], "blocked")
                self.assertFalse(result["drafts"])

    def test_empty_specs_allowed_but_technical_readiness_not_claimed(self):
        result = self.plan(case_fixture(required_specs={}))
        self.assertEqual(result["status"], "ready")
        self.assertIn("Teknik şartname henüz belirtilmedi", result["drafts"][0]["body"])
        self.assertTrue(any("teknik uygunluk" in warning for warning in result["warnings"]))

    def test_partial_for_unconfirmed_new_contact_and_other_ready_supplier(self):
        result = self.plan(suppliers=[supplier_fixture(), supplier_fixture("SUP-2", kind="new", contact_confirmed=False)])
        self.assertEqual(result["status"], "partial")
        self.assertEqual([row["supplier_id"] for row in result["drafts"]], ["SUP-1"])
        self.assertEqual(result["blocked_suppliers"][0]["supplier_id"], "SUP-2")

    def test_new_supplier_requires_public_https_source_url(self):
        for url in (None, "http://supplier.example.org", "javascript:alert(1)", "https://localhost/path",
                    "https://127.0.0.1/path", "https://10.0.0.1/path", "https://[::1]/path",
                    "https://user:password@supplier.example.org/path", "https://supplier.local/path",
                    "https://supplier.example.org:444/path", "https://supplier.example.org/\ntext",
                    "https://127.1/path", "https://127.0.1/path", "https://-example.org/path"):
            with self.subTest(url=url):
                result = self.plan(suppliers=[supplier_fixture(kind="new", source_url=url)])
                self.assertFalse(result["drafts"])
                self.assertEqual(len(result["blocked_suppliers"]), 1)

    def test_source_url_is_not_live_verified_or_placed_in_draft(self):
        result = self.plan(suppliers=[supplier_fixture(kind="new")])
        self.assertEqual(result["status"], "ready")
        self.assertTrue(any("doğrulanmadı" in warning for warning in result["warnings"]))
        self.assertNotIn("source_url", result["drafts"][0])
        self.assertNotIn("supplier.example.org", result["drafts"][0]["body"])

    def test_material_code_match_is_exact_no_fuzzy_or_wildcard_scope(self):
        for codes in (["pp-01"], ["PP"], ["*"], [], "PP-01", ["PP-01", "PP-01"]):
            with self.subTest(codes=codes):
                result = self.plan(suppliers=[supplier_fixture(material_codes=codes)])
                self.assertFalse(result["drafts"])

    def test_all_duplicate_identity_email_or_name_rows_rejected(self):
        variants = [supplier_fixture("SUP-1", email="different@example.org", name="Different"),
                    supplier_fixture("SUP-2", email="SUP-1@EXAMPLE.ORG"),
                    supplier_fixture("SUP-2", name=" SENTETİK FİRMA SUP-1 ")]
        for duplicate in variants:
            with self.subTest(duplicate=duplicate):
                result = self.plan(suppliers=[supplier_fixture(), duplicate])
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(len(result["blocked_suppliers"]), 2)
                self.assertFalse(result["drafts"])

    def test_invalid_duplicate_does_not_leave_first_contact_silently_accepted(self):
        result = self.plan(suppliers=[supplier_fixture(), supplier_fixture("SUP-1", contact_confirmed=False)])
        self.assertFalse(result["drafts"])
        self.assertEqual(len(result["blocked_suppliers"]), 2)

    def test_header_injection_and_unsafe_field_links_rejected(self):
        for field, value in (("email", "vendor@example.org\r\nBcc: stranger@example.org"),
                             ("name", "Vendor\nprivate terms"), ("supplier_id", "SUP\r1"),
                             ("email", "a..b@example.org"), ("email", "a@-example.org"),
                             ("email", "Vendor <vendor@example.org>"), ("email", "a@ex..org")):
            with self.subTest(field=field):
                self.assertFalse(self.plan(suppliers=[supplier_fixture(**{field: value})])["drafts"])
        for change in ({"material_name": "PP\nBcc: attacker@example.org"},
                       {"material_name": "javascript:alert(1)"},
                       {"required_specs": {"Doc": "https://evil.example.org"}},
                       {"delivery_place": "Depot\rOrder confirmed"}):
            with self.subTest(change=change):
                self.assertFalse(self.plan(case_fixture(**change))["drafts"])

    def test_input_size_nesting_and_supplier_count_limits(self):
        self.assertFalse(self.plan(case_fixture(untrusted="x" * 200_001))["drafts"])
        self.assertFalse(self.plan(suppliers=[supplier_fixture(str(index)) for index in range(51)])["drafts"])
        nested = {}
        for _ in range(15):
            nested = {"nested": nested}
        self.assertFalse(self.plan(case_fixture(extra=nested))["drafts"])

    def test_malformed_json_duplicate_keys_and_multi_case_do_not_open_drafts(self):
        for payload in ("{", "[]", '{"request":{},"request":{}}', {"cases": [case_fixture()]},
                        {"request": []}, {"request": {"id": float("nan")}}):
            with self.subTest(payload=payload):
                self.assertFalse(build_rfq_plan(payload, [supplier_fixture()], now=NOW)["drafts"])

    def test_empty_selection_and_invalid_registry_block(self):
        for suppliers in ([], {}, "not json", [None], ["supplier"]):
            with self.subTest(suppliers=suppliers):
                self.assertFalse(self.plan(suppliers=suppliers)["drafts"])

    def test_report_is_plaintext_draft_preview(self):
        result = self.plan()
        report = render_rfq_report(result)
        self.assertIn("hiçbir ileti gönderilmedi", report)
        self.assertIn("Alıcı: sup-1@example.org", report)
        self.assertIn(result["drafts"][0]["subject"], report)
        self.assertNotIn(RFQ_PLAN_MARKER, report)
        self.assertNotIn('"scope_hash"', report)
        self.assertIn("Hazırlanmış teklif talebi taslağı yok", render_rfq_report(self.plan(suppliers=[])))

    def test_component_returns_same_pure_contract_and_readable_report(self):
        component = SupplierRFQComponent()
        # Use a current explicit future deadline; component has no clock input.
        case = case_fixture(deadline_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat())
        component.set(case_data=Message(text=RFQ_CASE_MARKER + json.dumps(case)),
                      supplier_registry=json.dumps([supplier_fixture()]))
        output = component.build_plan()
        self.assertTrue(output.text.startswith(RFQ_PLAN_MARKER))
        parsed = json.loads(output.text[len(RFQ_PLAN_MARKER):])
        self.assertEqual(parsed["status"], "ready")
        self.assertIn("hiçbir ileti gönderilmedi", component.build_draft_report().text)


if __name__ == "__main__":
    unittest.main()
