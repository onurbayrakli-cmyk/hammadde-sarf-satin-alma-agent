import copy
import json
import unittest

from negotiation_draft import (
    NEGOTIATION_DRAFT_MARKER,
    NEGOTIATION_PLAN_MARKER,
    NEGOTIATION_SYSTEM_PROMPT,
    NegotiationDraftReview,
    build_negotiation_prompt,
    nd_local_draft,
    negotiation_factual_paragraphs,
    render_negotiation_drafts,
    review_negotiation_drafts,
)


def negotiation_row(request_id="REQ1", offer_id="Q1"):
    return {
        "request_id": request_id, "target_offer_id": offer_id, "status": "ready", "draft_allowed": True,
        "recipient": "teklif@example.com", "target_supplier": "Doğrulanmış Firma", "material_name": "Polietilen",
        "material_code": "PE", "quantity": "10", "unit": "KG", "currency": "TRY", "price_basis": "vat_exclusive",
        "negotiation_basis": "term", "current_price": "100", "opening_discount_pct": "2", "opening_target_price": "98",
        "fallback_discount_pct": "1", "fallback_target_price": "99", "term_days": "60", "extra_term_days": "30", "requested_term_days": "90",
        "history": {"status": "validated", "purchase_date": "2026-08-01", "display_date": "01.08.2026", "unit_price": "95", "currency": "TRY", "unit": "KG",
                    "price_basis": "vat_exclusive", "elapsed_days": 74, "elapsed_full_months": 2,
                    "source": {"document_id": "SAP-PO-1", "source_url": None}},
        "market_context": {
            "usdtry": {"baseline_date": "2026-08-01", "current_date": "2026-10-14", "baseline_value": "40", "current_value": "42", "change_pct": "5", "direction": "increase"},
            "brent": {"baseline_date": "2026-08-01", "current_date": "2026-10-14", "baseline_value": "80", "current_value": "72", "change_pct": "-10", "direction": "decrease"},
            "brent_try_proxy": {"change_pct": "-5.5", "direction": "decrease"},
        },
        "facts_allowed": {"verified_competition": True},
        "allowed_numeric_values": ["100", "98", "2", "99", "1", "60", "30", "90", "95", "74", "5", "10", "-10"],
        "excluded_supplier_names": ["Rakip Firma"], "warnings": [],
    }


def negotiation_plan():
    return {"schema_version": 1, "status": "ready", "sent": False, "draft_only": True, "requests": [negotiation_row()]}


def model_response(row):
    local = nd_local_draft(row, negotiation_factual_paragraphs(row))
    return {"request_id": row["request_id"], "target_offer_id": row["target_offer_id"], **local}


class NegotiationDraftTests(unittest.TestCase):
    def setUp(self):
        self.plan = negotiation_plan()
        self.row = self.plan["requests"][0]

    def review(self, response=None):
        return review_negotiation_drafts(self.plan, response)

    def draft(self, response=None):
        return self.review(response)["drafts"][0]

    def generated(self, change=None):
        output = model_response(self.row)
        if change:
            change(output)
        return {"drafts": [output]}

    def test_natural_local_fallback_discloses_model_unavailable(self):
        item = self.draft()
        self.assertEqual(item["generation_source"], "local_template")
        self.assertFalse(item["model_review"]["verified"])
        self.assertIn("Merhaba,", item["body"])
        self.assertIn("mümkün olur mu?", item["body"])
        self.assertIn("yerel taslak", item["model_review"]["reasons"][0])
        self.assertFalse(item["sent"])

    def test_verified_model_keeps_recipient_from_plan(self):
        item = self.draft(self.generated())
        self.assertEqual(item["generation_source"], "model_reviewed")
        self.assertTrue(item["model_review"]["verified"])
        self.assertEqual(item["recipient"], self.row["recipient"])
        self.assertFalse(item["sent"])

    def test_model_can_add_natural_courtesy(self):
        response = self.generated(lambda item: item.update(body=item["body"].replace("Teklifiniz için teşekkür ederim.", "Hızlı dönüşünüz için teşekkür ederim. Teklifiniz için teşekkür ederim.")))
        self.assertEqual(self.draft(response)["generation_source"], "model_reviewed")

    def test_source_outside_payment_and_certificate_assertions_fall_back(self):
        for sentence in ("Bedelini yarın hesabınıza yatıracağız.", "Ürünleriniz gerekli sertifikalara sahiptir.",
                         "Banka transferini cuma günü tamamlarız.", "Şartlar bizim açımızdan uygundur.",
                         "Ürünleriniz steril olarak üretilmektedir.", "Fabrikamız önümüzdeki hafta ürünü kullanacak."):
            with self.subTest(sentence=sentence):
                response = self.generated(lambda item: item.update(body=item["body"].replace("İyi çalışmalar.", sentence + " İyi çalışmalar.")))
                draft = self.draft(response)
                self.assertEqual(draft["generation_source"], "local_template")
                self.assertFalse(draft["model_review"]["verified"])
                self.assertNotIn(sentence, draft["body"])
                self.assertTrue(any("izinli nezaket" in reason for reason in draft["model_review"]["reasons"]))

    def test_courtesy_prefix_cannot_smuggle_new_commercial_claim(self):
        for sentence in ("Teklifiniz için teşekkür ederim, bedelini yarın yatıracağız.",
                         "Teklifiniz için teşekkür ederim. Bedelini yarın yatıracağız.",
                         "Desteğiniz için teşekkür ederim; ürünler gerekli sertifikalara sahiptir.",
                         "Teklifiniz için teşekkür ederim.XXX"):
            with self.subTest(sentence=sentence):
                response = self.generated(lambda item: item.update(body=item["body"].replace("Teklifiniz için teşekkür ederim.", sentence)))
                self.assertEqual(self.draft(response)["generation_source"], "local_template")

    def test_subject_cannot_carry_new_facts_or_commitment(self):
        for subject in ("Bedelini yarın hesabınıza yatıracağız", "Ürünleriniz gerekli sertifikalara sahiptir", "Teklif kabul edildi"):
            with self.subTest(subject=subject):
                response = self.generated(lambda item: item.update(subject=subject))
                draft = self.draft(response)
                self.assertEqual(draft["generation_source"], "local_template")
                self.assertTrue(any("konusu" in reason for reason in draft["model_review"]["reasons"]))

    def test_model_can_choose_whole_courtesy_sentences_and_subject_variants(self):
        paragraphs = negotiation_factual_paragraphs(self.row)
        response = self.generated(lambda item: item.update(
            subject="Polietilen için teklif revizyonu",
            body="Merhaba,\n\nPaylaştığınız teklif için teşekkür ederim.\n\n" + "\n\n".join(paragraphs)
                 + "\n\nDeğerlendirmenizi rica ederim. Desteğiniz için teşekkür ederim.\n\nİyi çalışmalar."))
        self.assertTrue(self.draft(response)["model_review"]["verified"])

    def test_courtesy_can_neither_negate_nor_modify_a_valid_core(self):
        response = self.generated(lambda item: item.update(body=item["body"].replace(
            "Aynı kapsamda aldığımız diğer teklifleri de değerlendirdik.",
            "Şu beyanımız geçerli değildir: Aynı kapsamda aldığımız diğer teklifleri de değerlendirdik.")))
        self.assertEqual(self.draft(response)["generation_source"], "local_template")

    def test_prompt_explicitly_defines_finite_nonfactual_language(self):
        prompt = json.loads(build_negotiation_prompt(self.plan))
        self.assertIn("Teklifiniz için teşekkür ederim.", prompt["allowed_courtesy_sentences"])
        self.assertIn("Polietilen için teklif revizyonu", prompt["requests"][0]["allowed_subjects"])
        self.assertIn("allowed_courtesy_sentences", NEGOTIATION_SYSTEM_PROMPT)
        self.assertIn("allowed_subjects", NEGOTIATION_SYSTEM_PROMPT)

    def test_private_fallback_not_disclosed_in_email(self):
        item = self.draft()
        self.assertNotIn("%1 ", item["body"])
        self.assertNotIn("99,00 TRY", item["body"])
        self.assertEqual(item["internal_strategy"]["fallback_target_price"], "99")
        self.assertIn("giden e-postaya eklenmez", render_negotiation_drafts(self.review()))

    def test_discount_can_be_one_one_point_five_or_two(self):
        for discount, target in (("1", "99"), ("1.5", "98.5"), ("2", "98")):
            with self.subTest(discount=discount):
                self.row.update(opening_discount_pct=discount, opening_target_price=target)
                self.assertIn("%" + discount.replace(".", ","), self.draft()["body"])

    def test_discount_outside_user_range_blocks_draft(self):
        self.row.update(opening_discount_pct="3", opening_target_price="97")
        self.assertFalse(self.review()["drafts"])

    def test_wrong_target_math_blocks_draft(self):
        self.row["opening_target_price"] = "96"
        self.assertFalse(self.review()["drafts"])

    def test_unknown_vat_basis_blocks_email(self):
        self.row["price_basis"] = "not_stated"
        self.assertFalse(self.review()["drafts"])

    def test_history_date_price_days_are_source_scoped(self):
        body = self.draft()["body"]
        self.assertIn("01.08.2026", body)
        self.assertIn("95,00 TRY/KG", body)
        self.assertIn("74 gün geçti", body)

    def test_unvalidated_history_and_market_not_published(self):
        self.row["history"]["status"] = "unverified"
        body = self.draft()["body"]
        self.assertNotIn("01.08.2026", body)
        self.assertNotIn("95,00 TRY/KG", body)
        self.assertNotIn("Brent", body)

    def test_currency_mismatch_skips_history(self):
        self.row["history"]["currency"] = "USD"
        self.assertNotIn("alımımızda", self.draft()["body"])

    def test_impossible_history_date_or_negative_elapsed_days_not_quoted(self):
        for changes in ({"purchase_date": "31.02.2026"}, {"elapsed_days": -1}, {"unit_price": "-95"}):
            with self.subTest(changes=changes):
                self.row["history"] = {**negotiation_row()["history"], **changes}
                self.assertNotIn("alımımızda", self.draft()["body"])

    def test_tax_basis_mismatch_skips_historical_price(self):
        self.row["history"]["price_basis"] = "vat_inclusive"
        self.assertNotIn("95,00 TRY/KG", self.draft()["body"])

    def test_correct_fx_and_brent_directions_no_actual_fuel_claim(self):
        body = self.draft()["body"]
        self.assertIn("USD/TRY kuru %5 arttı", body)
        self.assertIn("Brent petrolü %10 geriledi", body)
        self.assertIn("Türkiye'deki fiili yakıt bedeli değildir", body)
        self.assertIn("bire bir yansıma anlamına gelmez", body)

    def test_inconsistent_market_direction_not_used(self):
        self.row["market_context"]["brent"]["direction"] = "increase"
        self.assertNotIn("Brent petrolü %10", self.draft()["body"])
        self.assertIn("USD/TRY kuru %5 arttı", self.draft()["body"])

    def test_macro_percent_has_at_most_two_readable_decimals_and_review_accepts_it(self):
        self.row["market_context"]["usdtry"].update(change_pct="1.534908735", direction="increase")
        body = self.draft()["body"]
        self.assertIn("USD/TRY kuru %1,53 arttı", body)
        self.assertNotIn("1,534908735", body)
        self.assertEqual(self.draft(self.generated())["generation_source"], "model_reviewed")
        self.row.update(opening_discount_pct="1.5", opening_target_price="98.5")
        self.assertIn("%1,5 iyileştirme", self.draft()["body"])

    def test_macro_decrease_rounds_half_up_and_does_not_flip_direction(self):
        self.row["market_context"]["usdtry"].update(change_pct="-1.535", direction="decrease")
        body = self.draft()["body"]
        self.assertIn("USD/TRY kuru %1,54 geriledi", body)
        self.assertNotIn("%-1,54", body)
        self.assertEqual(self.draft(self.generated())["generation_source"], "model_reviewed")

    def test_unchanged_macro_has_no_false_increase_or_decrease(self):
        self.row["market_context"]["usdtry"].update(change_pct="0", direction="unchanged")
        body = self.draft()["body"]
        self.assertIn("USD/TRY kuru değişmedi", body)
        self.assertNotIn("USD/TRY kuru %0", body)

    def test_tiny_macro_change_is_not_presented_as_zero_percent_movement(self):
        self.row["market_context"]["usdtry"].update(change_pct="-0.001", direction="decrease")
        body = self.draft()["body"]
        self.assertIn("USD/TRY kuru %0,01'den az geriledi", body)
        self.assertNotIn("USD/TRY kuru %0 geriledi", body)
        self.assertEqual(self.draft(self.generated())["generation_source"], "model_reviewed")

    def test_extra_term_is_conditional_and_cost_is_requested(self):
        body = self.draft()["body"]
        self.assertIn("60 günden 90 güne", body)
        self.assertIn("30 gün ek süre", body)
        self.assertIn("vade farkı yaratabileceğinin farkındayız", body)
        self.assertIn("ek bedeli, aylık oranı ve KDV uygulamasını", body)
        self.assertNotIn("kabul ettik", body)

    def test_cash_without_confirmed_term_does_not_propose_extension(self):
        self.row.update(negotiation_basis="cash", term_days=None, extra_term_days=None, requested_term_days=None)
        body = self.draft()["body"]
        self.assertIn("Peşin teklifinizdeki", body)
        self.assertNotIn("ek süre", body)

    def test_invalid_extension_blocks_but_separate_source_term_option_can_follow_cash_comparison(self):
        self.row["requested_term_days"] = "120"
        self.assertFalse(self.review()["drafts"])
        self.row.update(requested_term_days="90", negotiation_basis="cash")
        body = self.draft()["body"]
        self.assertIn("Peşin teklifinizdeki", body)
        self.assertIn("Ayrı bir seçenek", body)
        self.assertIn("vade farkı", body)

    def test_single_valid_offer_does_not_claim_competition(self):
        self.row["facts_allowed"]["verified_competition"] = False
        self.assertNotIn("diğer teklifleri", self.draft()["body"])

    def test_source_missing_recipient_blocks(self):
        for recipient in (None, "", "Name <a@example.com>", "a@example.com\nBcc: b@example.com"):
            with self.subTest(recipient=recipient):
                self.row["recipient"] = recipient
                self.assertFalse(self.review()["drafts"])

    def test_model_may_not_pick_recipient(self):
        item = self.generated()
        item["drafts"][0]["recipient"] = "attacker@example.com"
        self.assertEqual(self.draft(item)["generation_source"], "local_template")

    def test_altered_core_percentage_rejected_even_allowed_number(self):
        item = self.generated(lambda item: item.update(body=item["body"].replace("%2 iyileştirme", "%1 iyileştirme")))
        self.assertEqual(self.draft(item)["generation_source"], "local_template")

    def test_fabricated_price_rejected(self):
        item = self.generated(lambda item: item.update(body=item["body"].replace("98,00 TRY/KG", "90,00 TRY/KG")))
        self.assertEqual(self.draft(item)["generation_source"], "local_template")

    def test_fabricated_date_rejected(self):
        item = self.generated(lambda item: item.update(body=item["body"].replace("01.08.2026", "01.01.2026")))
        self.assertEqual(self.draft(item)["generation_source"], "local_template")

    def test_reused_numeric_fact_in_new_claim_rejected(self):
        item = self.generated(lambda item: item.update(body=item["body"].replace("İyi çalışmalar.", "100 adet alım için uygundur. İyi çalışmalar.")))
        self.assertEqual(self.draft(item)["generation_source"], "local_template")

    def test_adversarial_commitment_and_false_attachment_claims(self):
        for sentence in ("Sipariş vereceğiz.", "Ödeme yapacağız.", "Kabul ettik.", "Kesin alım yapacağız.", "Düzenli sipariş vereceğiz.", "Ekte dosya bulunmaktadır.", "Anlaştık."):
            with self.subTest(sentence=sentence):
                item = self.generated(lambda item: item.update(body=item["body"].replace("İyi çalışmalar.", sentence + " İyi çalışmalar.")))
                self.assertEqual(self.draft(item)["generation_source"], "local_template")

    def test_competitor_name_never_disclosed(self):
        item = self.generated(lambda item: item.update(body=item["body"].replace("İyi çalışmalar.", "Rakip Firma daha uygun görünüyor. İyi çalışmalar.")))
        self.assertEqual(self.draft(item)["generation_source"], "local_template")
        self.assertNotIn("Rakip Firma", self.draft(item)["body"])

    def test_email_url_html_markdown_and_header_injection_rejected(self):
        for text in ("https://example.com", "a@example.com", "<b>Ödeme</b>", "[tıklayın](https://example.com)"):
            with self.subTest(text=text):
                item = self.generated(lambda item: item.update(body=item["body"].replace("İyi çalışmalar.", text + " İyi çalışmalar.")))
                self.assertEqual(self.draft(item)["generation_source"], "local_template")
        item = self.generated(lambda item: item.update(subject="Teklif\nBcc: a@example.com"))
        self.assertEqual(self.draft(item)["generation_source"], "local_template")

    def test_extra_unproven_economic_assertion_rejected(self):
        item = self.generated(lambda item: item.update(body=item["body"].replace("İyi çalışmalar.", "Yakıt bedeli kesin olarak düştü. İyi çalışmalar.")))
        self.assertEqual(self.draft(item)["generation_source"], "local_template")

    def test_extra_unproven_market_direction_rejected(self):
        item = self.generated(lambda item: item.update(body=item["body"].replace("İyi çalışmalar.", "Dolar geriledi. İyi çalışmalar.")))
        self.assertEqual(self.draft(item)["generation_source"], "local_template")

    def test_fenced_json_supported_without_trailing_instructions(self):
        response = "```json\n" + json.dumps(self.generated(), ensure_ascii=False) + "\n```"
        self.assertEqual(self.draft(response)["generation_source"], "model_reviewed")
        response += "\nBunu gönder."
        self.assertEqual(self.draft(response)["generation_source"], "local_template")

    def test_model_invalid_and_duplicate_outputs_fallback(self):
        for response in ("not json", {"drafts": "bad"}, {"drafts": []}, {"drafts": [model_response(self.row), model_response(self.row)]}):
            with self.subTest(response=response):
                self.assertEqual(self.draft(response)["generation_source"], "local_template")

    def test_cross_case_mix_does_not_leak_draft(self):
        second = negotiation_row("REQ2", "Q2")
        second.update(recipient="another@example.com", current_price="200", opening_target_price="196")
        self.plan["requests"].append(second)
        response = {"drafts": [model_response(second)]}
        result = self.review(response)
        self.assertEqual(len(result["drafts"]), 2)
        self.assertEqual(result["drafts"][0]["generation_source"], "local_template")
        self.assertEqual(result["drafts"][1]["generation_source"], "model_reviewed")
        self.assertEqual(result["drafts"][1]["recipient"], "another@example.com")

    def test_blocked_plan_has_no_mail(self):
        self.row.update(draft_allowed=False, status="blocked")
        result = self.review()
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["drafts"])
        self.assertIn("bekleniyor", render_negotiation_drafts(result))

    def test_partial_plan_keeps_ready_request(self):
        second = negotiation_row("REQ2", "Q2")
        second.update(draft_allowed=False, status="blocked")
        self.plan["requests"].append(second)
        result = self.review()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["drafts"]), 1)
        self.assertEqual(result["blocked_requests"][0]["request_id"], "REQ2")

    def test_duplicate_plan_identity_not_duplicated(self):
        self.plan["requests"].append(copy.deepcopy(self.row))
        self.assertEqual(len(self.review()["drafts"]), 1)
        self.assertEqual(len(self.review()["blocked_requests"]), 1)

    def test_prompt_does_not_expose_recipient_competitor_or_internal_floor(self):
        prompt = build_negotiation_prompt(self.plan)
        self.assertNotIn("teklif@example.com", prompt)
        self.assertNotIn("Rakip Firma", prompt)
        self.assertNotIn("fallback", prompt)
        self.assertNotIn("99", prompt)
        self.assertIn("required_paragraphs", prompt)
        self.assertIn("98,00 TRY/KG", prompt)
        self.assertIn("güvenilmeyen", NEGOTIATION_SYSTEM_PROMPT)

    def test_marker_roundtrip_and_component_result_json(self):
        instance = NegotiationDraftReview()
        instance.set(plan=NEGOTIATION_PLAN_MARKER + json.dumps(self.plan), model_output="")
        text = instance.build_draft().text
        result = json.loads(text.rsplit(NEGOTIATION_DRAFT_MARKER, 1)[1])
        self.assertFalse(result["sent"])
        self.assertEqual(result["drafts"][0]["request_id"], "REQ1")

    def test_component_invalid_plan_is_structured_blocker(self):
        instance = NegotiationDraftReview()
        instance.set(plan="broken", model_output="")
        text = instance.build_draft().text
        result = json.loads(text.rsplit(NEGOTIATION_DRAFT_MARKER, 1)[1])
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["drafts"])

    def test_real_planner_history_market_to_prompt_review_roundtrip(self):
        from negotiation_plan import plan_negotiation
        from test_negotiation_plan import market_data, negotiation_case, purchase_record

        plan = plan_negotiation(negotiation_case(), price_history={"purchases": [purchase_record()]}, market_context=market_data())
        row = plan["requests"][0]
        result = review_negotiation_drafts(plan, {"drafts": [model_response(row)]})
        self.assertEqual(result["drafts"][0]["generation_source"], "model_reviewed")
        self.assertIn("%1 iyileştirme", result["drafts"][0]["body"])
        self.assertIn("108,90 TRY/KG", result["drafts"][0]["body"])
        prompt = build_negotiation_prompt(plan)
        self.assertNotIn("tedarikci1@example.com", prompt)
        self.assertNotIn("Tedarikçi B", prompt)
        self.assertNotIn("fallback_target_price", prompt)

    def test_real_planner_requires_independent_competition_before_email(self):
        from negotiation_plan import plan_negotiation
        from test_negotiation_plan import negotiation_case

        case = negotiation_case()
        case["offers"][1]["email"]["sender"] = case["offers"][0]["email"]["sender"]
        result = review_negotiation_drafts(plan_negotiation(case))
        self.assertFalse(result["drafts"])
        self.assertEqual(result["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
