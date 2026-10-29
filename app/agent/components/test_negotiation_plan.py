import copy
import json
import unittest
from datetime import date
from decimal import Decimal

from lfx.schema.message import Message

from negotiation_plan import NEGOTIATION_MARKER, NegotiationPlanComponent, plan_negotiation
from test_quote_comparison import sample_case, sample_offer


def negotiation_case():
    case = sample_case()
    for index, offer in enumerate(case["offers"]):
        offer["email"]["sender"] = f"tedarikci{index + 1}@example.com"
    return case


def purchase_record(purchase_date="2026-07-10", price="95"):
    values = {"material_code": "PE", "quantity": "10", "unit": "KG", "currency": "TRY", "unit_price": price,
              "price_basis": "vat_exclusive", "term_days": "60", "purchase_date": purchase_date,
              "technical_specs.grade": "A"}
    quotes = {"material_code": "Malzeme PE", "quantity": "Miktar 10 KG", "unit": "Miktar 10 KG", "currency": "Para birimi TRY",
              "unit_price": f"Alış birim fiyatı {price}", "price_basis": "Fiyat KDV hariç", "term_days": "Vade 60 gün",
              "purchase_date": f"Alış tarihi {purchase_date}", "technical_specs.grade": "Kalite A"}
    return {**{key: value for key, value in values.items() if not key.startswith("technical_specs.")},
            "technical_specs": {"grade": "A"}, "source": {"document_id": "SAP-45001", "document_type": "sap_purchase_order",
                                                             "content": "\n".join(quotes.values())},
            "source_fields": {key: {"quote": quotation, "document_id": "SAP-45001"} for key, quotation in quotes.items()}}


def market_data(fx_before="30", fx_current="36", brent_before="80", brent_current="90"):
    def observation(name, day, value):
        return {"date": day, "value": value, "unit": "TRY/USD" if name == "usdtry" else "USD/barrel",
                "source_url": f"https://www.tcmb.gov.tr/kurlar/{date.fromisoformat(day):%Y%m}/{date.fromisoformat(day):%d%m%Y}.xml" if name == "usdtry"
                else "https://www.eia.gov/dnav/pet/hist/rbrteD.htm",
                "source_name": "TCMB" if name == "usdtry" else "EIA", "source_quote": f"{day}: {value}",
                "retrieved_at": "2026-10-13T06:01:00+00:00", "publication_date": day, "origin": "manual_supplied"}
    return {"as_of": "2026-10-13T09:00:00+03:00", "baseline_requested_date": "2026-07-10", "series": {
        "usdtry": {"baseline": observation("usdtry", "2026-07-10", fx_before), "current": observation("usdtry", "2026-10-13", fx_current)},
        "brent": {"baseline": observation("brent", "2026-07-10", brent_before), "current": observation("brent", "2026-10-13", brent_current)}}}


class NegotiationPlanTests(unittest.TestCase):
    def setUp(self):
        self.case = negotiation_case()

    def plan(self, **kwargs):
        return plan_negotiation(self.case, **kwargs)

    def first(self, **kwargs):
        return self.plan(**kwargs)["requests"][0]

    def update_offer(self, index, field, value, quote):
        offer = self.case["offers"][index]
        offer[field] = value
        offer["source_fields"][field] = {"quote": quote, "message_id": offer["email"]["message_id"]}
        offer["email"]["body_excerpt"] += "\n" + quote

    def test_discount_decimal_targets_and_draft_state(self):
        plan = self.first()
        self.assertTrue(plan["draft_allowed"])
        self.assertEqual(plan["target_offer_id"], "Q1")
        self.assertEqual(plan["negotiation_basis"], "term")
        self.assertEqual(Decimal(plan["opening_target_price"]), Decimal("107.8"))
        self.assertEqual(Decimal(plan["fallback_target_price"]), Decimal("108.9"))
        self.assertEqual(plan["opening_discount_pct"], "2")
        self.assertFalse(plan["sent"])
        self.assertTrue(plan["draft_only"])

    def test_extension_is_separate_request_and_not_accepted(self):
        plan = self.first()
        self.assertEqual(plan["extra_term_days"], "30")
        self.assertEqual(plan["requested_term_days"], "90")
        self.assertTrue(plan["constraints"]["ask_term_markup_separately"])
        self.assertFalse(plan["constraints"]["extension_acceptance"])

    def test_before_deadline_blocks_draft(self):
        self.case["as_of"] = "2026-10-13T08:59:59+03:00"
        self.assertEqual(self.plan()["status"], "blocked")

    def test_finished_clock_but_pending_state_blocks_draft(self):
        self.case["request"]["state"] = "collecting"
        self.assertFalse(self.first()["draft_allowed"])

    def test_single_quote_does_not_invent_competition(self):
        self.case["offers"] = self.case["offers"][:1]
        self.assertFalse(self.first()["draft_allowed"])

    def test_same_sender_does_not_count_as_two_independent_firms(self):
        self.case["offers"][1]["email"]["sender"] = self.case["offers"][0]["email"]["sender"]
        self.assertFalse(self.first()["draft_allowed"])

    def test_same_supplier_name_does_not_count_as_two_firms(self):
        self.case["offers"][1]["supplier"] = self.case["offers"][0]["supplier"]
        self.assertFalse(self.first()["draft_allowed"])

    def test_sender_header_injection_is_not_a_recipient(self):
        self.case["offers"][0]["email"]["sender"] = "a@example.com\nBcc: b@example.com"
        self.assertFalse(self.first()["draft_allowed"])

    def test_recipient_comes_from_lowest_offer_only(self):
        self.assertEqual(self.first()["recipient"], "tedarikci1@example.com")

    def test_competitor_names_and_prices_absent_from_facts(self):
        row = self.first()
        facts = json.dumps(row["facts_allowed"], ensure_ascii=False)
        self.assertNotIn("Tedarikçi B", facts)
        self.assertNotIn("115.5", facts)
        self.assertEqual(row["excluded_supplier_names"], ["Tedarikçi B"])

    def test_forged_low_price_does_not_make_a_target(self):
        self.case["offers"][0]["term_unit_price"] = "1"
        self.case["offers"][0]["cash_unit_price"] = "1"
        self.assertFalse(self.first()["draft_allowed"])

    def test_different_currency_is_not_combined(self):
        self.update_offer(1, "currency", "EUR", "Para birimi EUR")
        self.assertFalse(self.first()["draft_allowed"])

    def test_different_quantity_is_not_combined(self):
        self.update_offer(1, "quantity", "20", "Miktar 20 KG")
        self.assertFalse(self.first()["draft_allowed"])

    def test_technical_noncompliance_is_excluded(self):
        self.case["offers"][1] = sample_offer("Q2", "Tedarikçi B", "105", "115.5", spec="B")
        self.case["offers"][1]["email"]["sender"] = "tedarikci2@example.com"
        self.assertFalse(self.first()["draft_allowed"])

    def test_missing_technical_source_does_not_pass(self):
        del self.case["offers"][1]["source_fields"]["technical_specs.grade"]
        self.assertFalse(self.first()["draft_allowed"])

    def test_late_offer_is_not_competition(self):
        self.case["offers"][1]["email"]["received_at"] = "2026-10-13T09:00:01+03:00"
        self.case["as_of"] = "2026-10-13T10:00:00+03:00"
        self.assertFalse(self.first()["draft_allowed"])

    def test_duplicate_offer_identity_is_excluded(self):
        self.case["offers"][1]["id"] = self.case["offers"][0]["id"]
        self.assertFalse(self.first()["draft_allowed"])

    def test_different_terms_use_explicit_cash_basis(self):
        self.update_offer(1, "term_days", "90", "Vade 90 gün")
        row = self.first()
        self.assertTrue(row["draft_allowed"])
        self.assertEqual(row["negotiation_basis"], "cash")
        self.assertEqual(row["current_price"], "100")
        self.assertEqual(row["extra_term_days"], "30")

    def test_latest_offer_revision_is_selected_before_price_and_term_grouping(self):
        revision = sample_offer("Q1R", "Tedarikçi A", cash="120", term="125")
        revision["email"]["sender"] = "tedarikci1@example.com"
        revision["email"]["received_at"] = "2026-10-13T08:30:00+03:00"
        revision["term_days"] = "45"
        revision["source_fields"]["term_days"]["quote"] = "Vade 45 gün"
        revision["email"]["body_excerpt"] += "\nVade 45 gün"
        self.case["offers"].append(revision)
        row = self.first()
        self.assertTrue(row["draft_allowed"])
        self.assertEqual(row["negotiation_basis"], "cash")
        self.assertEqual(row["target_offer_id"], "Q2")
        self.assertEqual(row["current_price"], "105")

    def test_latest_noncomparable_revision_does_not_restore_old_valid_price(self):
        revision = sample_offer("Q1R", "Tedarikçi A", cash="120", term="125")
        revision["email"]["sender"] = "tedarikci1@example.com"
        revision["email"]["received_at"] = "2026-10-13T08:30:00+03:00"
        revision["quantity"] = "20"
        revision["source_fields"]["quantity"]["quote"] = "Miktar 20 KG"
        revision["email"]["body_excerpt"] += "\nMiktar 20 KG"
        self.case["offers"].append(revision)
        self.assertFalse(self.first()["draft_allowed"])

    def test_cash_only_offers_work_without_inventing_extension(self):
        for offer in self.case["offers"]:
            offer["term_unit_price"] = None
            offer["term_days"] = None
        row = self.first()
        self.assertTrue(row["draft_allowed"])
        self.assertEqual(row["negotiation_basis"], "cash")
        self.assertIsNone(row["extra_term_days"])

    def test_unknown_price_basis_blocks_negotiation(self):
        self.case["offers"][0]["price_basis"] = None
        self.assertFalse(self.first()["draft_allowed"])

    def test_empty_history_is_optional(self):
        self.assertEqual(self.first()["history"]["status"], "unavailable")
        self.assertTrue(self.first()["draft_allowed"])

    def test_unreadable_optional_history_or_market_does_not_invent_or_block_price_bargain(self):
        row = self.first(price_history="bad", market_context="bad")
        self.assertTrue(row["draft_allowed"])
        self.assertEqual(row["history"]["status"], "unavailable")
        self.assertEqual(row["market_context"], {})
        self.assertTrue(any("okunamadı" in warning for warning in row["warnings"]))

    def test_unsupported_history_schema_is_skipped_with_warning(self):
        row = self.first(price_history={"price": 123})
        self.assertTrue(row["draft_allowed"])
        self.assertEqual(row["history"]["status"], "unavailable")
        self.assertTrue(any("purchases" in warning for warning in row["warnings"]))

    def test_verified_historical_purchase_and_elapsed_time(self):
        row = self.first(price_history={"purchases": [purchase_record()]})
        history = row["history"]
        self.assertEqual(history["status"], "validated")
        self.assertEqual(history["display_date"], "10.07.2026")
        self.assertEqual(history["elapsed_days"], 95)
        self.assertEqual(history["elapsed_full_months"], 3)
        self.assertEqual(history["unit_price"], "95")
        self.assertAlmostEqual(float(history["price_change_pct"]), 15.7894736842)

    def test_historical_date_must_be_in_real_source(self):
        record = purchase_record()
        record["purchase_date"] = "2026-06-10"
        self.assertEqual(self.first(price_history={"purchases": [record]})["history"]["status"], "unavailable")

    def test_old_supplier_quote_proforma_or_untyped_record_is_not_a_purchase(self):
        for document_type in ("supplier_quote", "quote", "proforma", "cancelled_record", None):
            with self.subTest(document_type=document_type):
                record = purchase_record()
                record["source"]["document_type"] = document_type
                row = self.first(price_history={"purchases": [record]})
                self.assertEqual(row["history"]["status"], "unavailable")
                self.assertTrue(row["draft_allowed"])
                self.assertNotIn("purchase_date", row["facts_allowed"]["history"])

    def test_cancelled_purchase_document_cannot_be_a_previous_purchase_claim(self):
        for metadata_field in ("status", "purchase_status", "state"):
            with self.subTest(metadata_field=metadata_field):
                record = purchase_record()
                record[metadata_field] = "cancelled"
                row = self.first(price_history={"purchases": [record]})
                self.assertEqual(row["history"]["status"], "unavailable")
                self.assertTrue(row["draft_allowed"])
        for flag in ("cancelled", "canceled"):
            with self.subTest(flag=flag):
                record = purchase_record()
                record["source"][flag] = True
                self.assertEqual(self.first(price_history={"purchases": [record]})["history"]["status"], "unavailable")

    def test_historical_email_requires_explicit_purchase_confirmation_document_type(self):
        record = purchase_record()
        source = record.pop("source")
        record["email"] = {"message_id": source["document_id"], "body_excerpt": source["content"]}
        for value in record["source_fields"].values():
            value["message_id"] = value.pop("document_id")
        self.assertEqual(self.first(price_history={"purchases": [record]})["history"]["status"], "unavailable")
        record["document_type"] = "purchase_order_confirmation"
        self.assertEqual(self.first(price_history={"purchases": [record]})["history"]["status"], "validated")

    def test_historical_price_must_be_in_real_source(self):
        record = purchase_record()
        record["unit_price"] = "50"
        self.assertEqual(self.first(price_history={"purchases": [record]})["history"]["status"], "unavailable")

    def test_historical_purchase_price_cannot_reuse_quantity_or_cash_offer_evidence(self):
        for price, quotation in (("10", "Miktar 10 KG"), ("95", "Peşin birim fiyatı 95"), ("20", "KDV %20")):
            with self.subTest(quotation=quotation):
                record = purchase_record(price=price)
                record["source"]["content"] += "\n" + quotation
                record["source_fields"]["unit_price"]["quote"] = quotation
                row = self.first(price_history={"purchases": [record]})
                self.assertEqual(row["history"]["status"], "unavailable")
                self.assertTrue(row["draft_allowed"])

    def test_future_purchase_is_not_a_past_purchase(self):
        self.assertEqual(self.first(price_history={"purchases": [purchase_record("2027-07-10")]})["history"]["status"], "unavailable")

    def test_historical_scope_or_payment_terms_mismatch_skip(self):
        for field, value in (("term_days", "90"), ("unit", "ADET"), ("currency", "EUR"), ("quantity", "100"), ("price_basis", "vat_inclusive")):
            with self.subTest(field=field):
                record = purchase_record()
                record[field] = value
                self.assertEqual(self.first(price_history={"purchases": [record]})["history"]["status"], "unavailable")

    def test_historical_technical_proof_required(self):
        record = purchase_record()
        del record["source_fields"]["technical_specs.grade"]
        self.assertEqual(self.first(price_history={"purchases": [record]})["history"]["status"], "unavailable")

    def test_latest_valid_history_selected_not_invalid_newer_one(self):
        invalid = purchase_record("2026-08-10", "96")
        invalid["unit_price"] = "1"
        row = self.first(price_history={"purchases": [purchase_record(), invalid]})
        self.assertEqual(row["history"]["purchase_date"], "2026-07-10")

    def test_case_previous_purchase_supported(self):
        self.case["previous_purchase"] = purchase_record()
        self.assertEqual(self.first()["history"]["status"], "validated")

    def test_macro_adverse_uses_one_percent_and_recomputes_percent(self):
        market = market_data()
        market["series"]["usdtry"]["change_pct"] = "-99"
        row = self.first(market_context=market, price_history={"purchases": [purchase_record()]})
        self.assertEqual(row["opening_discount_pct"], "1")
        self.assertEqual(Decimal(row["market_context"]["usdtry"]["change_pct"]), Decimal(20))
        self.assertEqual(Decimal(row["market_context"]["brent_try_proxy"]["change_pct"]), Decimal(35))
        self.assertIn("eşit değildir", row["market_context"]["brent_try_proxy"]["label"])

    def test_macro_favorable_uses_two_percent(self):
        row = self.first(market_context=market_data("36", "30", "90", "80"), price_history={"purchases": [purchase_record()]})
        self.assertEqual(row["opening_discount_pct"], "2")

    def test_macro_mixed_uses_one_and_half_percent(self):
        row = self.first(market_context=market_data("30", "36", "90", "60"), price_history={"purchases": [purchase_record()]})
        self.assertEqual(row["opening_discount_pct"], "1.5")
        self.assertEqual(Decimal(row["opening_target_price"]), Decimal("108.35"))

    def test_no_verified_history_does_not_invent_market_baseline(self):
        row = self.first(market_context=market_data())
        self.assertEqual(row["market_context"], {})
        self.assertEqual(row["opening_discount_pct"], "2")

    def test_untrusted_primary_domain_is_rejected(self):
        market = market_data()
        market["series"]["usdtry"]["current"]["source_url"] = "https://tcmb.gov.tr.evil.example/rates"
        row = self.first(market_context=market, price_history={"purchases": [purchase_record()]})
        self.assertNotIn("usdtry", row["market_context"])
        self.assertEqual(row["opening_discount_pct"], "2")

    def test_synthetic_market_values_never_become_live_facts(self):
        market = market_data()
        for series in market["series"].values():
            for observation in series.values():
                observation["origin"] = "synthetic"
        self.assertEqual(self.first(market_context=market, price_history={"purchases": [purchase_record()]})["market_context"], {})

    def test_market_value_requires_source_quote(self):
        market = market_data()
        market["series"]["usdtry"]["current"]["value"] = "100"
        self.assertNotIn("usdtry", self.first(market_context=market, price_history={"purchases": [purchase_record()]})["market_context"])

    def test_baseline_far_from_purchase_is_rejected(self):
        market = market_data()
        market["series"]["usdtry"]["baseline"]["date"] = "2026-06-01"
        self.assertNotIn("usdtry", self.first(market_context=market, price_history={"purchases": [purchase_record()]})["market_context"])

    def test_future_market_observation_or_release_is_rejected(self):
        for key, value in (("date", "2026-10-14"), ("publication_date", "2026-10-14"), ("retrieved_at", "2026-10-14T01:00:00+03:00")):
            with self.subTest(field=key):
                market = market_data()
                market["series"]["usdtry"]["current"][key] = value
                self.assertNotIn("usdtry", self.first(market_context=market, price_history={"purchases": [purchase_record()]})["market_context"])

    def test_previous_business_day_uses_actual_observation_date(self):
        market = market_data()
        market["series"]["usdtry"]["baseline"]["date"] = "2026-07-09"
        market["series"]["usdtry"]["baseline"]["source_quote"] = "2026-07-09: 30"
        market["series"]["usdtry"]["baseline"]["source_url"] = "https://www.tcmb.gov.tr/kurlar/202607/09072026.xml"
        row = self.first(market_context=market, price_history={"purchases": [purchase_record()]})
        self.assertEqual(row["market_context"]["usdtry"]["baseline_date"], "2026-07-09")

    def test_observation_date_cannot_be_relabelled(self):
        market = market_data()
        market["series"]["usdtry"]["baseline"]["date"] = "2026-07-09"
        self.assertNotIn("usdtry", self.first(market_context=market, price_history={"purchases": [purchase_record()]})["market_context"])

    def test_eia_week_row_date_is_verified(self):
        market = market_data()
        market["series"]["brent"]["current"]["source_quote"] = "2026 Oct-12 to Oct-16 | 89 | 90 | 91 | 92 | 93; weekday=2; value=90"
        self.assertIn("brent", self.first(market_context=market, price_history={"purchases": [purchase_record()]})["market_context"])

    def test_baseline_options_match_valid_history_date(self):
        selected = market_data()
        market = {"baseline_requested_date": "2026-08-10", "series": {}, "baseline_options": [selected]}
        row = self.first(market_context=market, price_history={"purchases": [purchase_record()]})
        self.assertIn("usdtry", row["market_context"])

    def test_prefixed_market_message_from_component(self):
        value = Message(text="PAZARLIK PİYASA VERİSİ (JSON)\n" + json.dumps(market_data()))
        self.assertEqual(self.first(market_context=value, price_history={"purchases": [purchase_record()]})["opening_discount_pct"], "1")

    def test_langflow_editor_unescaped_body_newlines_remain_source_data(self):
        payload = json.dumps(self.case, ensure_ascii=False).replace("\\n", "\n")
        plan = plan_negotiation(Message(text="TEKLİF DOSYASI (JSON)\n" + payload))
        self.assertTrue(plan["requests"][0]["draft_allowed"])
        self.assertEqual(plan["requests"][0]["current_price"], "110")
        self.assertEqual(plan["requests"][0]["recipient"], "tedarikci1@example.com")

    def test_langflow_editor_unescaped_history_newlines_do_not_relax_json_structure(self):
        history = json.dumps({"purchases": [purchase_record()]}, ensure_ascii=False).replace("\\n", "\n")
        row = self.first(price_history=Message(text=history))
        self.assertEqual(row["history"]["status"], "validated")
        self.assertEqual(row["history"]["unit_price"], "95")
        with self.assertRaises(ValueError):
            plan_negotiation('{"request": {"id": "R1"\n BROKEN}}')

    def test_bundle_preserves_request_scopes(self):
        second = copy.deepcopy(self.case)
        second["request"]["id"] = "REQ2"
        second["request"]["material_code"] = "PP"
        second["request"]["material_name"] = "Polipropilen"
        for offer in second["offers"]:
            offer["material_code"] = "PP"
        result = plan_negotiation({"cases": [self.case, second]})
        self.assertEqual([row["request_id"] for row in result["requests"]], ["REQ1", "REQ2"])
        self.assertEqual([row["material_code"] for row in result["requests"]], ["PE", "PP"])

    def test_bundle_partial_does_not_merge_quotes(self):
        second = copy.deepcopy(self.case)
        second["request"]["id"] = "REQ2"
        second["offers"] = []
        result = plan_negotiation({"cases": [self.case, second]})
        self.assertEqual(result["status"], "partial")
        self.assertEqual([row["draft_allowed"] for row in result["requests"]], [True, False])

    def test_duplicate_request_ids_rejected(self):
        with self.assertRaises(ValueError):
            plan_negotiation({"cases": [self.case, self.case]})

    def test_component_model_input_hides_competitor_and_internal_fallback(self):
        component = NegotiationPlanComponent(case_data="TEKLİF DOSYASI (JSON)\n" + json.dumps(self.case))
        text = component.build_model_input().text
        self.assertIn(NEGOTIATION_MARKER, text)
        self.assertNotIn("Tedarikçi B", text)
        self.assertNotIn("fallback_target_price", text)
        # Yerel WriterPrompt alıcıyı doğrular; gerçek model promptu adresi çıkarır.
        self.assertIn("tedarikci1@example.com", text)

    def test_writer_prompt_receives_ready_facts_and_hides_address_before_model(self):
        from negotiation_draft import build_negotiation_prompt
        component = NegotiationPlanComponent(case_data="TEKLİF DOSYASI (JSON)\n" + json.dumps(self.case))
        prompt = build_negotiation_prompt(component.build_model_input())
        data = json.loads(prompt)
        self.assertEqual(len(data["requests"]), 1)
        self.assertNotIn("tedarikci1@example.com", prompt)
        self.assertNotIn("Tedarikçi B", prompt)

    def test_component_fail_closed_on_bad_json_without_network(self):
        component = NegotiationPlanComponent(case_data="bad")
        value = json.loads(component.build_plan().text.split(NEGOTIATION_MARKER)[1])
        self.assertEqual(value["status"], "blocked")
        self.assertEqual(component.build_model_input().text, "")

    def test_conditional_guard_clears_old_exclusions_when_ready(self):
        class FakeGraph:
            def __init__(self):
                self.calls = []
            def exclude_branches_conditionally(self, vertex, excluded):
                self.calls.append((vertex, excluded))
        class Fake:
            _vertex = object()
            _id = "PlanTest"
            graph = FakeGraph()
            stopped = []
            def stop(self, output):
                self.stopped.append(output)
        fake = Fake()
        NegotiationPlanComponent._guard(fake, {"requests": []})
        NegotiationPlanComponent._guard(fake, {"requests": [{"draft_allowed": True}]})
        self.assertEqual(fake.graph.calls, [("PlanTest", ["model_input"]), ("PlanTest", [])])
        self.assertEqual(fake.stopped, ["model_input"])


if __name__ == "__main__":
    unittest.main()
