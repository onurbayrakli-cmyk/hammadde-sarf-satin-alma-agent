"""Gerçek yerel bileşenle çevrimdışı iş kuralı testleri; sağlayıcı çağrısı yok."""

import json
import unittest
from pathlib import Path

from stock_rules import StockProcurementRulesComponent, analyze_table

HEADER = ("malzeme_kodu;tanım;birim;mevcut_stok;ayrılmış_stok;kesin_açık_sipariş;"
          "günlük_tüketim;termin_gün;emniyet_stok;açık_sipariş_teslim_tarihi")


def table(*rows, header=HEADER, report_date="2026-10-13"):
    return f"Rapor tarihi: {report_date}\n\n{header}\n" + "\n".join(rows)


class StockRulesTests(unittest.TestCase):
    def test_blank_lines_do_not_discard_later_items(self):
        result = analyze_table(table(
            "X1;Ürün;ADET;80;20;0;10;6;20;",
            "",
            "X2;Ürün;KG;220;20;0;8;10;30;",
        ))
        self.assertEqual(result["errors"], [])
        self.assertEqual([r["malzeme_kodu"] for r in result["rows"]], ["X1", "X2"])

    def test_four_sample_rows(self):
        sample = Path(__file__).resolve().parents[1] / "Ornek_Tablo.txt"
        result = analyze_table(sample.read_text())
        self.assertEqual(result["errors"], [])
        rows = result["rows"]
        self.assertEqual([r["malzeme_kodu"] for r in rows], ["ORN001", "ORN002", "ORN003", "ORN004"])
        self.assertEqual([r["onerilen_miktar"] for r in rows], [50, 0, 0, None])
        self.assertEqual([r["durum"] for r in rows], ["Acil", "Yeterli", "Yeterli", "Bilgi eksik"])
        self.assertEqual([r["birim"] for r in rows], ["ADET", "KG", "ADET", "ADET"])
        self.assertEqual(rows[2]["teslim_oncesi_stok"], 40)
        self.assertEqual(rows[2]["termin_sonu_stok"], 30)
        self.assertEqual(rows[3]["net_stok"], 400)
        self.assertIsNone(rows[3]["hedef_stok"])

    def test_positive_order_requires_current_valid_date(self):
        for arrival in ("", "2026-10-12", "2026-02-30", "20261015"):
            with self.subTest(arrival=arrival):
                row = analyze_table(table(f"X;Ürün;ADET;80;20;30;10;6;20;{arrival}"))["rows"][0]
                self.assertEqual(row["durum"], "Bilgi eksik")
                self.assertIsNone(row["onerilen_miktar"])
                self.assertIsNone(row["dahil_acik_siparis"])
                self.assertTrue(row["eksikler"])

    def test_future_order_beyond_horizon_is_excluded(self):
        row = analyze_table(table("X;Ürün;ADET;80;20;30;10;6;20;2026-10-20"))["rows"][0]
        self.assertEqual((row["dahil_acik_siparis"], row["onerilen_miktar"]), (0, 20))
        self.assertEqual(row["durum"], "Satın alma gerekli")
        self.assertTrue(row["notlar"])

    def test_arrival_after_stock_runs_out_is_urgent(self):
        row = analyze_table(table("X;Ürün;ADET;10;0;200;10;6;20;2026-10-15"))["rows"][0]
        self.assertEqual(row["onerilen_miktar"], 0)
        self.assertEqual(row["teslim_oncesi_stok"], -10)
        self.assertEqual(row["termin_sonu_stok"], 150)
        self.assertEqual(row["durum"], "Acil")

    def test_today_and_horizon_orders_are_included(self):
        for arrival, expected_before in (("2026-10-13", 60), ("2026-10-19", 0)):
            with self.subTest(arrival=arrival):
                row = analyze_table(table(f"X;Ürün;ADET;80;20;30;10;6;20;{arrival}"))["rows"][0]
                self.assertEqual(row["dahil_acik_siparis"], 30)
                self.assertEqual(row["teslim_oncesi_stok"], expected_before)
                self.assertEqual(row["durum"], "Yeterli")

    def test_missing_number_is_not_zero(self):
        row = analyze_table(table("X;Ürün;ADET;80;20;;10;6;20;"))["rows"][0]
        self.assertIsNone(row["onerilen_miktar"])
        self.assertEqual(row["durum"], "Bilgi eksik")

    def test_malformed_consumption_has_no_recommendation(self):
        for value in ("çok", "NaN", "Infinity", "1,2.3", "1e9", "-1"):
            with self.subTest(value=value):
                row = analyze_table(table(f"X;Ürün;ADET;80;20;0;{value};6;20;"))["rows"][0]
                self.assertIsNone(row["onerilen_miktar"])
                self.assertEqual(row["durum"], "Veri hatası")

    def test_negative_and_nonfinite_stock_are_data_errors(self):
        for value in ("-1", "NaN", "Infinity", "1000000000001", "9" * 40):
            with self.subTest(value=value):
                row = analyze_table(table(f"X;Ürün;ADET;{value};0;0;10;6;20;"))["rows"][0]
                self.assertEqual(row["durum"], "Veri hatası")
                self.assertIsNone(row["onerilen_miktar"])

    def test_reservations_above_stock_are_data_error(self):
        row = analyze_table(table("X;Ürün;ADET;10;20;0;10;6;20;"))["rows"][0]
        self.assertEqual(row["net_stok"], -10)
        self.assertEqual(row["durum"], "Veri hatası")
        self.assertIsNone(row["onerilen_miktar"])

    def test_zero_consumption_uses_safety_without_division(self):
        row = analyze_table(table("X;Ürün;ADET;10;0;0;0;6;20;"))["rows"][0]
        self.assertEqual((row["hedef_stok"], row["onerilen_miktar"]), (20, 10))
        self.assertEqual(row["durum"], "Satın alma gerekli")
        self.assertIsNone(row["stok_gunu_siparis_haric"])

    def test_decimal_arithmetic_is_preserved_as_strings(self):
        row = analyze_table(table("X;Ürün;KG;0,3;0;0;0.1;3;0,2;"))["rows"][0]
        self.assertEqual(row["hedef_stok"], "0.5")
        self.assertEqual(row["onerilen_miktar"], "0.2")
        self.assertEqual(row["termin_sonu_stok"], 0)

    def test_missing_columns_and_duplicate_headers_fail_clearly(self):
        for header in (HEADER.replace(";günlük_tüketim", ""), HEADER + ";MEVCUT_STOK"):
            with self.subTest(header=header):
                result = analyze_table(table("X;Ürün;ADET;10;0;0;0;6;20;", header=header))
                self.assertTrue(result["errors"])
                self.assertEqual(result["rows"], [])

    def test_turkish_uppercase_headers_and_commentary_are_supported(self):
        source = table("X;Ürün;ADET;10;0;0;0;6;20;", header=HEADER.upper())
        result = analyze_table("Ön açıklama\n" + source + "\n\nSon açıklama; tablo dışı\n")
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["rows"][0]["onerilen_miktar"], 10)

    def test_duplicate_item_unit_has_no_definitive_recommendation(self):
        result = analyze_table(table("X;Ürün;ADET;10;0;0;0;6;20;", "x;Ürün;adet;10;0;0;0;6;20;"))
        self.assertEqual(len(result["rows"]), 2)
        for row in result["rows"]:
            self.assertIsNone(row["onerilen_miktar"])
            self.assertEqual(row["durum"], "Bilgi eksik")

    def test_units_remain_separate(self):
        result = analyze_table(table("X;Ürün;ADET;10;0;0;0;6;20;", "X;Ürün;KG;10;0;0;0;6;20;"))
        self.assertEqual([r["birim"] for r in result["rows"]], ["ADET", "KG"])
        self.assertEqual([r["onerilen_miktar"] for r in result["rows"]], [10, 10])
        self.assertNotIn("total", result)
        area = analyze_table(table("X;Ürün;M;10;0;0;0;6;20;", "X;Ürün;M²;10;0;0;0;6;20;"))
        self.assertEqual([r["onerilen_miktar"] for r in area["rows"]], [10, 10])

    def test_report_date_and_date_overflow_are_validated(self):
        for report_date in ("", "2026-02-30", "20261013", "2026-10-130"):
            with self.subTest(report_date=report_date):
                row = analyze_table(table("X;Ürün;ADET;10;0;0;0;6;20;", report_date=report_date))["rows"][0]
                self.assertEqual(row["durum"], "Bilgi eksik")
                self.assertIsNone(row["onerilen_miktar"])
        row = analyze_table(table("X;Ürün;ADET;10;0;0;0;6;20;", report_date="9999-12-31"))["rows"][0]
        self.assertEqual(row["durum"], "Veri hatası")
        self.assertIsNone(row["onerilen_miktar"])

    def test_lead_must_be_bounded_whole_days(self):
        for lead in ("0,5", "3651", "-1"):
            with self.subTest(lead=lead):
                row = analyze_table(table(f"X;Ürün;ADET;10;0;0;0;{lead};20;"))["rows"][0]
                self.assertEqual(row["durum"], "Veri hatası")
                self.assertIsNone(row["onerilen_miktar"])

    def test_row_and_input_limits(self):
        rows = [f"X{i};Ürün;ADET;10;0;0;0;6;20;" for i in range(101)]
        self.assertEqual(len(analyze_table(table(*rows[:100]))["rows"]), 100)
        with self.assertRaisesRegex(ValueError, "100 satır"):
            analyze_table(table(*rows))
        with self.assertRaisesRegex(ValueError, "100000 karakter"):
            analyze_table("A" * 100_001)

    def test_wrong_row_length_never_produces_recommendation(self):
        row = analyze_table(table("X;Ürün;ADET;10;0;0;0;6;20;;fazla"))["rows"][0]
        self.assertIsNone(row["onerilen_miktar"])
        self.assertTrue(row["eksikler"])

    def test_real_component_preserves_original_input_and_json(self):
        source = table("X;Ürün;ADET;10;0;0;0;6;20;") + "\n\nBu metni koru."
        component = StockProcurementRulesComponent()
        component.set(input_value=source)
        output = component.build_report()
        self.assertIn(source, output.text)
        payload = json.loads(output.text.split("KESİN YEREL HESAP (JSON)\n", 1)[1])
        self.assertEqual(payload["rows"][0]["onerilen_miktar"], 10)
        self.assertEqual(payload["report_date"], "2026-10-13")


if __name__ == "__main__":
    unittest.main()
