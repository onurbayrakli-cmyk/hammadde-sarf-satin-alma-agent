import copy
import json
import unittest

from quote_comparison import MARKER, SupplierQuoteComparisonComponent, analyze_case, render_case_report


def sample_offer(offer_id="Q1", supplier="Tedarikçi A", cash="100", term="110", currency="TRY", spec="A"):
    values = {"quantity": "10", "unit": "KG", "currency": currency, "cash_unit_price": cash,
              "term_unit_price": term, "term_days": "60", "vat_rate_pct": "20", "delivery_days": "7",
              "price_basis": "vat_exclusive", "vat_basis": "including_term_markup", "technical_specs.grade": spec}
    quotations = {"quantity": "Miktar 10 KG", "unit": "Miktar 10 KG", "currency": f"Para birimi {currency}",
                  "cash_unit_price": f"Peşin birim {cash}", "term_unit_price": f"Vadeli birim {term}",
                  "term_days": "Vade 60 gün", "vat_rate_pct": "KDV %20", "delivery_days": "Teslim 7 gün",
                  "price_basis": "Fiyatlar KDV hariç", "vat_basis": "KDV matrahına vade farkı dahil",
                  "technical_specs.grade": f"Kalite sınıfı {spec}"}
    message_id = "mail-" + offer_id
    email = {"message_id": message_id, "received_at": "2026-10-13T08:00:00+03:00", "sender": "teklif@example.com",
             "subject": "Hammadde teklifi", "body_excerpt": "\n".join(quotations.values()),
             "source_url": "https://outlook.office.com/mail/inbox/id/" + message_id}
    return {"id": offer_id, "supplier": supplier, "email": email, "material_code": "PE", "technical_specs": {"grade": spec},
            **{key: value for key, value in values.items() if not key.startswith("technical_specs.")},
            "source_fields": {key: {"quote": quote, "message_id": message_id} for key, quote in quotations.items()}, "missing_fields": []}


def sample_case():
    return {"request": {"id": "REQ1", "material_code": "PE", "material_name": "Polietilen", "quantity": "10", "unit": "KG",
                        "required_specs": {"grade": "A"}, "deadline_at": "2026-10-13T09:00:00+03:00", "state": "completed"},
            "as_of": "2026-10-13T09:00:00+03:00", "offers": [sample_offer(), sample_offer("Q2", "Tedarikçi B", "105", "115.5")]}


class QuoteComparisonTests(unittest.TestCase):
    def setUp(self):
        self.case = sample_case()

    def analyze(self):
        return analyze_case(self.case)

    def first(self):
        return self.analyze()["offers"][0]

    def update(self, field, value, quote):
        offer = self.case["offers"][0]
        offer[field] = value
        offer["source_fields"][field]["quote"] = quote
        offer["email"]["body_excerpt"] += "\n" + quote

    def test_math_vat_totals_and_monthly_rates(self):
        row = self.first()
        self.assertEqual(row["term_markup_amount"], "100")
        self.assertEqual(row["term_markup_percent"], "10.0")
        self.assertEqual(row["monthly_simple_percent"], "5.0")
        self.assertAlmostEqual(float(row["monthly_effective_percent"]), 4.880884817, places=8)
        self.assertEqual(row["term_total_ex_vat"], "1100")
        self.assertEqual(row["term_vat_total"], "220.0")
        self.assertEqual(row["term_total_gross"], "1320.0")
        self.assertEqual(self.analyze()["recommended_offer_id"], "Q1")

    def test_missing_rate_does_not_default_to_twenty(self):
        self.case["offers"][0]["vat_rate_pct"] = None
        row = self.first()
        self.assertIsNone(row["vat_rate_pct"])
        self.assertIsNone(row["term_total_gross"])
        self.assertEqual(row["term_markup_amount"], "100")

    def test_missing_cash_price_does_not_invent_markup_but_keeps_known_term_total(self):
        self.case["offers"][0]["cash_unit_price"] = None
        row = self.first()
        self.assertIsNone(row["cash_unit_ex_vat"])
        self.assertIsNone(row["term_markup_percent"])
        self.assertEqual(row["term_total_gross"], "1320.0")

    def test_zero_rate_is_known(self):
        self.update("vat_rate_pct", "0", "KDV %0")
        self.assertEqual(self.first()["term_total_gross"], "1100")

    def test_unknown_vat_basis_blocks_ambiguous_tax_total(self):
        self.case["offers"][0]["vat_basis"] = None
        row = self.first()
        self.assertIsNone(row["term_total_gross"])
        self.assertIsNone(row["term_vat_total"])
        self.assertEqual(row["term_total_ex_vat"], "1100")
        self.assertTrue(any("matrahına" in warning for warning in row["warnings"]))

    def test_principal_only_is_supplier_declaration(self):
        self.update("vat_basis", "principal_only", "KDV matrahı yalnız anapara")
        row = self.first()
        self.assertEqual(row["term_total_gross"], "1300")
        self.assertEqual(row["term_vat_total"], "200")
        self.assertTrue(any("hukuki uygunluk" in warning for warning in row["warnings"]))

    def test_vat_inclusive_decomposition(self):
        self.update("cash_unit_price", "120", "Peşin birim 120")
        self.update("term_unit_price", "132", "Vadeli birim 132")
        self.update("price_basis", "vat_inclusive", "Fiyatlar KDV dahil")
        row = self.first()
        self.assertEqual(row["cash_unit_ex_vat"], "100")
        self.assertEqual(row["term_unit_ex_vat"], "110")
        self.assertEqual(row["term_markup_amount"], "100")
        self.assertEqual(row["term_total_gross"], "1320")

    def test_inclusive_without_tax_basis_does_not_invent_ex_vat(self):
        self.update("price_basis", "vat_inclusive", "Fiyatlar KDV dahil")
        self.case["offers"][0]["vat_basis"] = None
        row = self.first()
        self.assertIsNone(row["term_unit_ex_vat"])
        self.assertIsNone(row["term_markup_percent"])
        self.assertEqual(row["term_total_gross"], "1100")

    def test_unknown_price_basis_blocks_totals(self):
        self.case["offers"][0]["price_basis"] = None
        self.assertIsNone(self.first()["term_total_ex_vat"])
        self.assertIsNone(self.first()["term_markup_percent"])

    def test_source_id_mismatch_blocks_financial_field(self):
        self.case["offers"][0]["source_fields"]["cash_unit_price"]["message_id"] = "fake"
        self.assertIsNone(self.first()["cash_unit_price"])
        self.assertIsNone(self.first()["term_markup_percent"])

    def test_matching_id_without_real_quote_blocks_price(self):
        self.case["offers"][0]["source_fields"]["term_unit_price"]["quote"] = "Kaynakta olmayan fiyat 110"
        self.assertIsNone(self.first()["term_unit_price"])

    def test_quote_with_different_price_blocks_changed_value(self):
        self.case["offers"][0]["term_unit_price"] = "999"
        self.assertIsNone(self.first()["term_unit_price"])

    def test_ambiguous_numeric_separator_requires_confirmation(self):
        self.update("term_unit_price", "1000", "Vadeli birim 1.000")
        self.assertIsNone(self.first()["term_unit_price"])

    def test_unambiguous_turkish_numeric_separator_supported(self):
        self.update("term_unit_price", "1000.5", "Vadeli birim 1.000,50")
        self.assertEqual(self.first()["term_unit_price"], "1000.5")

    def test_quantities_tax_and_days_are_not_price_evidence(self):
        for field in ("cash_unit_price", "term_unit_price"):
            for value, quote in (("10", "Miktar 10 KG"), ("20", "KDV %20"), ("60", "Vade 60 gün"), ("7", "Teslim 7 gün")):
                with self.subTest(field=field, quote=quote):
                    self.case = sample_case()
                    self.update(field, value, quote)
                    row = self.first()
                    self.assertIsNone(row[field])
                    self.assertNotIn(field, row["sources"])
                    self.assertTrue(any(field + ": kaynak" in message for message in row["warnings"]))

    def test_numeric_source_fields_cannot_be_swapped(self):
        examples = (
            ("quantity", "10", "Peşin birim fiyat 10 TRY"),
            ("vat_rate_pct", "20", "Miktar 20 KG"),
            ("term_days", "60", "Teslimat süresi 60 gün"),
            ("delivery_days", "7", "Vade 7 gün"),
            ("cash_unit_price", "110", "Vadeli birim 110"),
            ("term_unit_price", "100", "Peşin birim 100"),
        )
        for field, value, quote in examples:
            with self.subTest(field=field):
                self.case = sample_case()
                self.update(field, value, quote)
                self.assertIsNone(self.first()[field])

    def test_mislabelled_cheapest_offer_is_excluded_from_report_ranking(self):
        self.case["offers"].append(sample_offer("Q3", "Tedarikçi C", "108", "119"))
        for field in ("cash_unit_price", "term_unit_price"):
            self.update(field, "10", "Miktar 10 KG")
        result = self.analyze()
        self.assertEqual(result["rankings"][0]["offer_ids"], ["Q2", "Q3"])
        self.assertEqual(result["recommended_offer_id"], "Q2")
        row = result["offers"][0]
        self.assertIsNone(row["cash_total_gross"])
        self.assertIsNone(row["term_total_gross"])
        self.assertFalse(row["comparable"])
        report = render_case_report(result)
        self.assertIn("cash\\_unit\\_price: kaynak alıntısı doğrulanamadı", report)
        self.assertNotIn("| Tedarikçi A / Q1 | TRY | 10.00", report)

    def test_distinct_labels_bind_each_number_in_one_paragraph(self):
        quote = "Miktar: 10 KG; Peşin birim fiyat: 100 TRY; Vadeli birim fiyat: 110 TRY; Vade: 60 gün; KDV: %20; Teslimat süresi: 7 gün."
        values = {"quantity": "10", "cash_unit_price": "100", "term_unit_price": "110", "term_days": "60", "vat_rate_pct": "20", "delivery_days": "7"}
        for field, value in values.items():
            self.update(field, value, quote)
        row = self.first()
        for field, value in values.items():
            with self.subTest(field=field):
                self.assertEqual(row[field], value)
        self.assertTrue(row["comparable"])

    def test_other_number_in_labelled_paragraph_cannot_be_selected(self):
        quote = "Peşin birim fiyat 100 TRY; Vadeli birim fiyat 110 TRY; Miktar 10 KG; KDV %20"
        for field in ("cash_unit_price", "term_unit_price"):
            with self.subTest(field=field):
                self.update(field, "10", quote)
                self.assertIsNone(self.first()[field])

    def test_ambiguous_price_range_or_repeated_price_requires_narrow_source(self):
        for quote in ("Peşin birim fiyat 100 veya 90 TRY", "Peşin birim 100; Peşin birim 90", "Peşin/vadeli birim 100/110", "Birim fiyat 100 TRY", "100"):
            with self.subTest(quote=quote):
                self.update("cash_unit_price", "100", quote)
                self.assertIsNone(self.first()["cash_unit_price"])

    def test_turkish_ascii_and_english_field_vocabulary(self):
        examples = (
            ("quantity", "10", "Qty: 10 KG"),
            ("cash_unit_price", "100", "Cash unit price: 100.00 TRY/KG"),
            ("term_unit_price", "110", "Credit unit price: 110 TRY/KG"),
            ("term_days", "60", "Payment terms: 60 days"),
            ("vat_rate_pct", "20", "VAT rate: 20%"),
            ("delivery_days", "7", "Lead time: 7 days"),
            ("cash_unit_price", "100", "PESIN BIRIM FIYAT: 100 TL"),
            ("cash_unit_price", "100", "Cash price: 100 USD/KG"),
        )
        for field, value, quote in examples:
            with self.subTest(quote=quote):
                self.case = sample_case()
                self.update(field, value, quote)
                self.assertEqual(self.first()[field], value)

    def test_field_label_alone_does_not_invent_percent_or_day_unit(self):
        for field, value, quote in (("vat_rate_pct", "20", "KDV tutarı 20 TL"), ("vat_rate_pct", "20", "VAT 20 EUR"),
                                    ("term_days", "2", "Vade 2 ay"), ("delivery_days", "7", "Delivery 7 weeks")):
            with self.subTest(quote=quote):
                self.update(field, value, quote)
                self.assertIsNone(self.first()[field])

    def test_explicit_label_does_not_override_conflicting_value_unit(self):
        for field, value, quote in (("cash_unit_price", "20", "Peşin %20 indirim"),
                                    ("cash_unit_price", "60", "Peşin 60 gün"),
                                    ("term_unit_price", "60", "Vadeli birim fiyat 60 gün"),
                                    ("quantity", "10", "Miktar: 10 USD"),
                                    ("quantity", "10", "Miktar %10"),
                                    ("term_days", "60", "term_days: USD 60"),
                                    ("vat_rate_pct", "20", "KDV oranı: 20 TL"),
                                    ("vat_rate_pct", "20", "VAT rate: 20 KG"),
                                    ("cash_unit_price", "10", "Peşin birim fiyat: 10 KG")):
            with self.subTest(quote=quote):
                self.update(field, value, quote)
                self.assertIsNone(self.first()[field])

    def test_price_without_unit_price_scope_cannot_be_assumed_per_unit(self):
        for quote in ("Peşin fiyat 100 TRY", "Cash 100 USD", "Peşin toplam 100 TRY"):
            with self.subTest(quote=quote):
                self.update("cash_unit_price", "100", quote)
                self.assertIsNone(self.first()["cash_unit_price"])

    def test_english_thousands_format_with_decimal_is_source_bound(self):
        self.update("cash_unit_price", "1000.5", "Cash unit price: USD 1,000.50")
        self.assertEqual(self.first()["cash_unit_price"], "1000.5")

    def test_quantity_scope_source_is_required(self):
        del self.case["offers"][0]["source_fields"]["quantity"]
        self.assertFalse(self.first()["comparable"])
        self.assertIsNone(self.first()["term_total_gross"])

    def test_currency_quote_must_match_currency(self):
        self.case["offers"][0]["currency"] = "USD"
        self.assertIsNone(self.first()["currency"])

    def test_cross_currency_not_ranked_together(self):
        self.case["offers"][1] = sample_offer("Q2", "Dövizli Firma", "1", "1.1", currency="USD")
        result = self.analyze()
        self.assertEqual(len(result["rankings"]), 2)
        self.assertIsNone(result["recommended_offer_id"])
        self.assertTrue(any("kur varsayımı" in warning for warning in result["warnings"]))

    def test_wrong_spec_cheapest_not_recommended(self):
        self.case["offers"][0] = sample_offer(cash="1", term="1.1", spec="B")
        row = self.first()
        self.assertEqual(row["spec_status"], "uygunsuz")
        self.assertFalse(row["comparable"])
        self.assertNotEqual(self.analyze()["recommended_offer_id"], "Q1")
        self.assertEqual(self.analyze()["rankings"][0]["offer_ids"], ["Q2"])

    def test_unproven_spec_not_published(self):
        offer = self.case["offers"][0]
        offer["technical_specs"]["grade"] = "HALLUCINATED"
        row = self.first()
        self.assertNotIn("grade", row["technical_specs"])
        self.assertEqual(row["spec_status"], "eksik")
        self.assertNotIn("HALLUCINATED", render_case_report(self.analyze()))

    def test_attachment_quotes_price_and_specs(self):
        offer = self.case["offers"][0]
        offer["email"]["attachments"] = [{"attachment_id": "A1", "filename": "teklif.pdf",
            "text_excerpt": "Vadeli birim 110\nKalite sınıfı A", "source_url": "https://example.com/teklif.pdf", "sha256": "abc"}]
        for field in ("term_unit_price", "technical_specs.grade"):
            quote = offer["source_fields"][field]["quote"]
            offer["email"]["body_excerpt"] = offer["email"]["body_excerpt"].replace(quote, "")
            offer["source_fields"][field]["attachment_id"] = "A1"
        row = self.first()
        self.assertEqual(row["term_unit_price"], "110")
        self.assertEqual(row["technical_specs"], {"grade": "A"})
        self.assertIn("teklif.pdf", render_case_report(self.analyze()))

    def test_attachment_id_must_match_real_attachment(self):
        self.case["offers"][0]["source_fields"]["cash_unit_price"]["attachment_id"] = "FAKE"
        self.assertIsNone(self.first()["cash_unit_price"])

    def test_preliminary_before_deadline_even_if_state_completed(self):
        self.case["as_of"] = "2026-10-13T08:59:59+03:00"
        self.assertEqual(self.analyze()["report_status"], "preliminary")
        self.assertIsNone(self.analyze()["recommended_offer_id"])

    def test_same_instant_in_utc_counts_as_deadline(self):
        self.case["as_of"] = "2026-10-13T06:00:00Z"
        self.assertEqual(self.analyze()["report_status"], "final")

    def test_store_final_states_require_expiry(self):
        for state in ("ready_for_report", "report_prepared"):
            with self.subTest(state=state):
                self.case["request"]["state"] = state
                self.case["as_of"] = "2026-10-13T09:00:00+03:00"
                self.assertEqual(self.analyze()["report_status"], "final")
                self.case["as_of"] = "2026-10-13T08:30:00+03:00"
                self.assertEqual(self.analyze()["report_status"], "preliminary")

    def test_render_dates_in_istanbul_timezone(self):
        self.case["as_of"] = "2026-10-13T06:00:00Z"
        report = render_case_report(self.analyze())
        self.assertIn("13.10.2026 09:00 (Europe/Istanbul)", report)

    def test_naive_deadline_does_not_finish_case(self):
        self.case["request"]["deadline_at"] = "2026-10-13T09:00:00"
        self.assertEqual(self.analyze()["report_status"], "preliminary")

    def test_missing_duration_remains_pending(self):
        self.case["request"]["deadline_at"] = None
        self.case["request"]["state"] = "awaiting_deadline"
        result = self.analyze()
        self.assertEqual(result["report_status"], "preliminary")
        self.assertTrue(any("süre istenmeli" in warning for warning in result["warnings"]))

    def test_expired_but_uncompleted_is_preliminary(self):
        self.case["request"]["state"] = "collecting_offers"
        self.assertEqual(self.analyze()["report_status"], "preliminary")

    def test_naive_report_timestamp_blocks_calculations(self):
        self.case["as_of"] = "2026-10-13T09:00:00"
        self.assertIsNone(self.first()["term_total_gross"])

    def test_future_email_blocks_calculations(self):
        self.case["offers"][0]["email"]["received_at"] = "2026-10-13T10:00:00+03:00"
        self.assertIsNone(self.first()["term_total_gross"])

    def test_late_offer_displayed_but_not_ranked(self):
        self.case["as_of"] = "2026-10-13T10:00:00+03:00"
        self.case["offers"][0]["email"]["received_at"] = "2026-10-13T09:05:00+03:00"
        self.assertTrue(self.first()["late"])
        self.assertFalse(self.first()["comparable"])
        self.assertIn("Son tarihten sonra", render_case_report(self.analyze()))

    def test_missing_zero_days_does_not_create_monthly_percentage(self):
        for days in (None, "0"):
            with self.subTest(days=days):
                if days is None:
                    self.case["offers"][0]["term_days"] = None
                else:
                    self.update("term_days", "0", "Vade 0 gün")
                self.assertIsNone(self.first()["monthly_simple_percent"])
                self.assertIsNone(self.first()["monthly_effective_percent"])

    def test_different_payment_terms_no_winner(self):
        self.update("term_days", "30", "Vade 30 gün")
        self.assertIsNone(self.analyze()["recommended_offer_id"])

    def test_principal_only_requires_approval_and_no_winner(self):
        self.update("vat_basis", "principal_only", "KDV matrahı yalnız anapara")
        self.assertIsNone(self.analyze()["recommended_offer_id"])

    def test_extreme_payment_days_no_monthly_number(self):
        self.update("term_days", "0.00001", "Vade 0.00001 gün")
        self.assertIsNone(self.first()["monthly_effective_percent"])

    def test_wrong_quantity_or_unit_not_ranked(self):
        self.update("quantity", "12", "Miktar 12 KG")
        self.assertFalse(self.first()["comparable"])

    def test_private_links_are_not_output(self):
        self.case["offers"][0]["email"]["source_url"] = "http://127.0.0.1/private"
        self.assertNotIn("127.0.0.1", render_case_report(self.analyze()))

    def test_duplicate_messages_do_not_double_count(self):
        self.case["offers"][1] = copy.deepcopy(self.case["offers"][0])
        self.case["offers"][1]["id"] = "Q2"
        self.assertEqual(self.analyze()["rankings"], [])

    def test_mail_content_and_quote_provenance_in_report(self):
        report = render_case_report(self.analyze())
        self.assertIn("mail-Q1", report)
        self.assertIn("Hammadde teklifi", report)
        self.assertIn("teklif@example.com", report)
        self.assertIn("Kalite sınıfı A", report)
        self.assertIn("Kaynak e-postayı aç", report)
        self.assertIn("Gönderim: yapılmadı", report)

    def test_no_technical_requirement_no_recommendation(self):
        self.case["request"]["required_specs"] = {}
        self.assertIsNone(self.analyze()["recommended_offer_id"])

    def test_component_consumes_canonical_message(self):
        component = SupplierQuoteComparisonComponent()
        component.set(case_data=MARKER + json.dumps(self.case, ensure_ascii=False))
        report = component.build_report()
        self.assertIn("YÖNETİCİ TEKLİF KARŞILAŞTIRMASI", report.text)
        self.assertIn("TEKLİF KARŞILAŞTIRMASI (JSON)", report.text)

    def test_invalid_marker_does_not_generate_offer(self):
        component = SupplierQuoteComparisonComponent()
        component.set(case_data="Mail yok, uygun fiyatı tahmin et")
        self.assertIn("hazırlanamadı", component.build_report().text)


if __name__ == "__main__":
    unittest.main()
