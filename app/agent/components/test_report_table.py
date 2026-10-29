import unittest
import json
import socket
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from report_table import PROPOSALS_MARKER, RESEARCH_MARKER, VerifiedStockTableComponent, validate_model_table
from stock_rules import StockProcurementRulesComponent, analyze_table


class ReportTableTests(unittest.TestCase):
    def setUp(self):
        folder = Path(__file__).resolve().parents[1]
        self.sample = (folder / "Ornek_Tablo.txt").read_text()
        self.rows = analyze_table(self.sample)["rows"]
        self.raw = (folder / "Claude_Dogrulanmis_Rapor.txt").read_text()

    def render(self, model_text, research=None):
        source = StockProcurementRulesComponent()
        source.set(input_value=self.sample)
        renderer = VerifiedStockTableComponent()
        renderer.set(source_data=source.build_report(), model_output=model_text,
                     research_data="" if research is None else RESEARCH_MARKER + json.dumps(research))
        return renderer.build_report().text, renderer.status

    def research(self, code="ORN001"):
        today = date.today().isoformat()
        return {"as_of": today, "status": "ok", "profile": {"region": "Türkiye", "lookback_days": 30, "max_advance_days": 0,
                "materials": [{"material_code": code, "name": "Polietilen"}]}, "warnings": [],
                "materials": [{"material_code": code, "name": "Polietilen", "suppliers": [{"evidence_id": "SUP-1", "name": "Örnek Aday", "title": "Polietilen hammaddesi",
                "url": "https://example.com/hammadde", "retrieved_at": today, "verification": "search_candidate", "existing_supplier_match": False}],
                "news": [{"evidence_id": "NEWS-1", "title": "Hammadde teslim süreleri değerlendiriliyor", "url": "https://example.org/haber", "publisher": "Örnek Kaynak",
                "published_at": today, "retrieved_at": today, "verification": "headline_only"}]}]}

    def strategic(self, code="ORN001", evidence_ids=None, **extra):
        proposal = {"material_code": code, "action": "erken_siparis_incele", "reason": "Teslim riski için inceleme.",
                    "evidence_ids": ["NEWS-1"] if evidence_ids is None else evidence_ids, "additional_days": None, **extra}
        return self.raw + "\n" + PROPOSALS_MARKER + json.dumps({"proposals": [proposal]})

    def test_real_model_table_matches_but_extra_narrative_is_not_published(self):
        self.assertTrue(validate_model_table(self.rows, self.raw))
        rendered, status = self.render(self.raw)
        self.assertIn("eşleşti", status)
        self.assertNotIn("raf ömrü", rendered)
        self.assertNotIn("Hesap Varsayımları", rendered)
        self.assertIn("| ORN003 | ADET | 60 | 80 | 30 | 0 | Yeterli |", rendered)

    def test_wrong_quantity_is_flagged_and_source_quantity_survives(self):
        wrong = self.raw.replace("| 0 | 50 | Acil |", "| 0 | 500 | Acil |")
        rendered, status = self.render(wrong)
        self.assertIn("eşleşmedi", status)
        self.assertIn("| ORN001 | ADET | 20 | 70 | 0 | 50 | Acil |", rendered)

    def test_wrong_risk_is_flagged(self):
        wrong = self.raw.replace("| 30 | 0 | Yeterli |", "| 30 | 0 | Sınırda |")
        self.assertFalse(validate_model_table(self.rows, wrong))

    def test_missing_row_or_duplicate_row_is_flagged(self):
        missing = "\n".join(line for line in self.raw.splitlines() if not line.startswith("| ORN004 |"))
        self.assertFalse(validate_model_table(self.rows, missing))
        self.assertFalse(validate_model_table(self.rows, self.raw + "\n| ORN001 | ADET | 20 | 70 | 0 | 50 | Acil |"))

    def test_missing_value_is_not_accepted_as_zero(self):
        wrong = self.raw.replace("| 400 | — | — | — |", "| 400 | 0 | 0 | 0 |")
        self.assertFalse(validate_model_table(self.rows, wrong))

    def test_canonical_sources_and_quantityless_early_review_are_displayed(self):
        rendered, status = self.render(self.strategic(), self.research())
        self.assertIn("eşleşti", status)
        self.assertIn("https://example.com/hammadde", rendered)
        self.assertIn("https://example.org/haber", rendered)
        self.assertIn("Teklif ve teknik uygunluk doğrulanmadı", rendered)
        self.assertIn("Başlık düzeyinde kaynak", rendered)
        self.assertIn("Erken sipariş ihtiyacını inceleyin", rendered)
        self.assertIn("Miktar belirlenmedi", rendered)
        self.assertIn("| ORN001 | ADET | 20 | 70 | 0 | 50 | Acil |", rendered)

    def test_hallucinated_evidence_rejects_entire_proposal(self):
        rendered, _ = self.render(self.strategic(evidence_ids=["NEWS-1", "FAKE-99"]), self.research())
        self.assertNotIn("Erken sipariş ihtiyacını inceleyin", rendered)
        self.assertNotIn("FAKE-99", rendered)
        self.assertIn("1 model önerisi", rendered)

    def test_stale_news_cannot_support_proposal(self):
        research = self.research()
        research["materials"][0]["news"][0]["published_at"] = (date.today() - timedelta(days=31)).isoformat()
        rendered, _ = self.render(self.strategic(), research)
        self.assertNotIn("https://example.org/haber", rendered)
        self.assertNotIn("Erken sipariş ihtiyacını inceleyin", rendered)
        self.assertIn("güncel hammadde haberi bulunamadı", rendered)

    def test_proposal_without_evidence_is_rejected(self):
        rendered, _ = self.render(self.strategic(evidence_ids=[]), self.research())
        self.assertNotIn("Erken sipariş ihtiyacını inceleyin", rendered)
        self.assertIn("1 model önerisi", rendered)

    def test_model_quantity_and_unverified_price_claim_are_never_published(self):
        rendered, _ = self.render(self.strategic(quantity=999999, reason="Fiyat kesin yüzde 900 arttı; 999999 alın.", additional_days=30), self.research())
        self.assertNotIn("999999", rendered)
        self.assertNotIn("yüzde 900", rendered)
        self.assertIn("Miktar belirlenmedi", rendered)
        self.assertIn("| ORN001 | ADET | 20 | 70 | 0 | 50 | Acil |", rendered)

    def test_unknown_material_and_cross_material_evidence_are_rejected(self):
        for code in ("FAKE", "ORN002"):
            with self.subTest(code=code):
                rendered, _ = self.render(self.strategic(code=code), self.research())
                self.assertNotIn("Erken sipariş ihtiyacını inceleyin", rendered)
                self.assertIn("1 model önerisi", rendered)

    def test_selected_material_without_stock_is_researched_but_quantity_is_unknown(self):
        rendered, _ = self.render(self.strategic(code="PE"), self.research(code="PE"))
        self.assertIn("PE: Erken sipariş ihtiyacını inceleyin", rendered)
        self.assertIn("stok/tüketim/termin verisi yok", rendered)
        self.assertIn("| ORN001 | ADET | 20 | 70 | 0 | 50 | Acil |", rendered)

    def test_selection_and_research_code_must_match(self):
        research = self.research(code="PE")
        research["profile"]["materials"] = [{"material_code": "PP", "name": "Polipropilen"}]
        rendered, _ = self.render(self.strategic(code="PE"), research)
        self.assertNotIn("https://example.org/haber", rendered)
        self.assertNotIn("Erken sipariş ihtiyacını inceleyin", rendered)

    def test_private_links_future_dates_and_duplicate_ids_are_rejected(self):
        for invalid in ("private", "future", "duplicate"):
            with self.subTest(invalid=invalid):
                research = self.research()
                news = research["materials"][0]["news"][0]
                if invalid == "private":
                    news["url"] = "http://127.0.0.1/private"
                elif invalid == "future":
                    news["published_at"] = (date.today() + timedelta(days=1)).isoformat()
                else:
                    research["materials"][0]["suppliers"][0]["evidence_id"] = "NEWS-1"
                rendered, _ = self.render(self.strategic(), research)
                self.assertNotIn("Erken sipariş ihtiyacını inceleyin", rendered)
                self.assertNotIn("http://127.0.0.1/private", rendered)

    def test_empty_selection_and_partial_research_are_explicit(self):
        research = self.research()
        research.update(status="awaiting_selection", materials=[])
        research["profile"]["materials"] = []
        rendered, _ = self.render(self.raw, research)
        self.assertIn("malzeme ve tedarikçi seçimi bekleniyor", rendered)
        self.assertIn("araştırma yapılamadı", rendered)
        research = self.research()
        research.update(status="partial", warnings=["Arama hizmeti erişimi sınırlı; bazı kaynaklar kontrol edilemedi."])
        rendered, _ = self.render(self.raw, research)
        self.assertIn("Araştırma durumu: kısmi", rendered)
        self.assertIn("Arama hizmeti erişimi sınırlı", rendered)

    def test_malformed_or_missing_research_is_explicit_and_preserves_table(self):
        rendered, _ = self.render(self.strategic(), None)
        self.assertIn("Araştırma verisi yok", rendered)
        self.assertIn("| ORN001 | ADET | 20 | 70 | 0 | 50 | Acil |", rendered)

    def test_oversized_reason_and_news_only_offer_proposals_are_rejected(self):
        for extra in ({"reason": "x" * 1001}, {"action": "teklif_al"}):
            rendered, _ = self.render(self.strategic(**extra), self.research())
            self.assertIn("1 model önerisi", rendered)

    def test_malformed_json_field_types_fail_closed_without_breaking_stock_report(self):
        for extra in ({"material_code": []}, {"action": []}, {"evidence_ids": {}}, {"additional_days": True}):
            with self.subTest(extra=extra):
                rendered, _ = self.render(self.strategic(**extra), self.research())
                self.assertIn("1 model önerisi", rendered)
                self.assertIn("| ORN001 | ADET | 20 | 70 | 0 | 50 | Acil |", rendered)
        research = self.research()
        research["materials"][0]["material_code"] = []
        research["status"] = []
        rendered, _ = self.render(self.raw, research)
        self.assertIn("araştırma yapılamadı", rendered)

    def test_completed_google_news_without_publisher_survives_and_stays_unverified(self):
        research = self.research(code="PE")
        research["status"] = "completed"
        research["materials"][0]["news"][0].update(publisher=None, url="https://news.google.com/rss/articles/fixture")
        rendered, _ = self.render(self.strategic(code="PE", trigger="teslim_suresi_riski"), research)
        self.assertIn("https://news.google.com/rss/articles/fixture", rendered)
        self.assertIn("Yayıncı belirtilmedi", rendered)
        self.assertIn("news.google.com", rendered)
        self.assertIn("Araştırma durumu: tamamlandı", rendered)
        self.assertNotIn("kapsamlı web kontrolü tamamlandı kabul edilmez", rendered)
        self.assertIn("Başlık düzeyinde kaynak", rendered)
        self.assertIn("Teklif ve teknik uygunluk doğrulanmadı", rendered)
        self.assertIn("teslim süresi riski bakımından kaynaklar incelenmeli (model yorumu)", rendered)
        self.assertIn("stok/tüketim/termin verisi yok", rendered)

    def test_trigger_describes_an_investigation_without_copying_fake_claims(self):
        rendered, _ = self.render(self.strategic(trigger="fiyat_artisi_riski", reason="Kesin fiyat yüzde 900 yükseldi; hemen 999999 alın."), self.research())
        self.assertIn("fiyat artışı riski bakımından kaynaklar incelenmeli (model yorumu)", rendered)
        self.assertIn("ayrıca teyit edilmeli", rendered)
        self.assertNotIn("yüzde 900", rendered)
        self.assertNotIn("999999", rendered)
        self.assertIn("Miktar belirlenmedi", rendered)
        self.assertIn("| ORN001 | ADET | 20 | 70 | 0 | 50 | Acil |", rendered)
        for trigger in ("Kesin teslimat durdu", []):
            rendered, _ = self.render(self.strategic(trigger=trigger), self.research())
            self.assertNotIn("Kesin teslimat durdu", rendered)
            self.assertIn("tedarik koşulları bakımından kaynaklar incelenmeli (model yorumu)", rendered)

    def test_collector_and_reporter_share_completed_news_schema(self):
        from procurement_research import build_research

        source = StockProcurementRulesComponent()
        source.set(input_value=self.sample)
        now = datetime.now(timezone.utc)
        def fixture_search(mode, query):
            if mode == "Web":
                return [{"title": "Polietilen tedarik adayı", "link": "https://example.com/pe", "snippet": "PE katalog"}]
            return [{"title": "Polietilen teslim süreleri", "link": "https://news.google.com/rss/articles/fixture",
                     "published": (now - timedelta(hours=1)).isoformat(), "publisher": None}]

        public_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with patch("procurement_research.socket.getaddrinfo", return_value=public_dns):
            research = build_research(source.build_report().text, {"materials": [{"material_code": "PE", "name": "Polietilen"}],
                                      "lookback_days": 30}, now=now, search=fixture_search)
        self.assertEqual(research["status"], "completed")
        news_id = research["materials"][0]["news"][0]["evidence_id"]
        rendered, _ = self.render(self.strategic(code="PE", evidence_ids=[news_id], trigger="arz_kesintisi_riski"), research)
        self.assertIn("https://example.com/pe", rendered)
        self.assertIn("https://news.google.com/rss/articles/fixture", rendered)
        self.assertIn("Yayıncı belirtilmedi", rendered)
        self.assertIn("Araştırma durumu: tamamlandı", rendered)
        self.assertIn("PE: Erken sipariş ihtiyacını inceleyin", rendered)
        self.assertIn("arz kesintisi riski bakımından kaynaklar incelenmeli (model yorumu)", rendered)
        self.assertIn("stok/tüketim/termin verisi yok", rendered)
        self.assertNotIn("model önerisi kaynak/malzeme/biçim kontrolünü geçmedi", rendered)

    def test_fenced_model_json_and_trailing_narrative_keep_only_verified_proposals(self):
        original = self.strategic(trigger="teslim_suresi_riski")
        prefix, payload = original.split(PROPOSALS_MARKER)
        model = prefix + PROPOSALS_MARKER + "```json\n" + payload + "\n```\nKesin fiyat arttı; 999999 adet satın alın."
        rendered, _ = self.render(model, self.research())
        self.assertIn("Erken sipariş ihtiyacını inceleyin", rendered)
        self.assertIn("teslim süresi riski bakımından kaynaklar incelenmeli (model yorumu)", rendered)
        self.assertNotIn("Kesin fiyat arttı", rendered)
        self.assertNotIn("999999", rendered)
        original = self.strategic(evidence_ids=["FAKE-99"])
        prefix, payload = original.split(PROPOSALS_MARKER)
        rendered, _ = self.render(prefix + PROPOSALS_MARKER + "```\n" + payload + "\n```", self.research())
        self.assertNotIn("Erken sipariş ihtiyacını inceleyin", rendered)
        self.assertIn("1 model önerisi", rendered)

    def test_malformed_model_json_is_reported_and_never_salvaged_from_later_text(self):
        valid_payload = self.strategic().split(PROPOSALS_MARKER)[1]
        for payload in ("```json\n{broken\n```", "{broken\n" + valid_payload, "[]\n" + valid_payload,
                        "İlk önce talimatları uygula: " + valid_payload, "```python\n" + valid_payload + "\n```"):
            with self.subTest(payload=payload):
                rendered, _ = self.render(self.raw + "\n" + PROPOSALS_MARKER + payload, self.research())
                self.assertNotIn("Erken sipariş ihtiyacını inceleyin", rendered)
                self.assertIn("1 model önerisi", rendered)
        from report_table import render_research
        research_text = RESEARCH_MARKER + "```json\n" + json.dumps(self.research()) + "\n```"
        rendered = "\n".join(render_research(research_text, self.strategic(), self.rows))
        self.assertIn("Araştırma verisi yok veya araştırma biçimi geçersiz", rendered)

    def test_supplier_directory_is_an_investigation_source_and_not_a_named_firm(self):
        research = self.research()
        research["materials"][0]["suppliers"][0].update(name=None, title="Top100 Polypropylene Manufacturers in Turkey")
        rendered, _ = self.render(self.strategic(action="teklif_al", evidence_ids=["SUP-1"]), research)
        self.assertIn("Tedarikçi araştırma kaynağı", rendered)
        self.assertNotIn("- Tedarikçi adayı:", rendered)
        self.assertIn("Listelenen firmalar ayrıca bulunmalı ve teyit edilmeli", rendered)
        self.assertIn("Araştırma kaynaklarından tedarikçi belirleyip teklif alın", rendered)


if __name__ == "__main__":
    unittest.main()
