"""Local worker integration tests; no network, mailbox or real procurement data."""

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from case_store import CaseStore
from case_worker import (
    FLOW_ID,
    _load_json,
    _report_html,
    ingest_request,
    list_collecting,
    main,
    prepare_due,
    prepare_questions,
    read_connection_status,
    read_queue_counts,
    research_attachment,
    status,
)

BEFORE = "2026-10-13T08:00:00+03:00"
DEADLINE = "2026-10-13T09:00:00+03:00"
AFTER = "2026-10-13T09:01:00+03:00"


def request_payload(request_id="R1", deadline=None):
    return {"source": "mail_request", "request": {
        "id": request_id, "material_code": "PE", "material_name": "Polietilen", "quantity": "10", "unit": "KG",
        "required_specs": {"grade": "A"}, "requester": "talep@example.com", "mailbox": "bot@example.com",
        "message_id": "request-" + request_id, "deadline_at": deadline,
    }}


def offer_payload(offer_id="Q1", received_at=BEFORE):
    values = {"quantity": "10", "unit": "KG", "currency": "TRY", "cash_unit_price": "100", "term_unit_price": "110",
              "term_days": "60", "vat_rate_pct": "20", "vat_basis": "including_term_markup",
              "price_basis": "vat_exclusive", "delivery_days": "3"}
    quotations = {
        "quantity": "Miktar 10 KG", "unit": "Miktar 10 KG", "currency": "Para birimi TRY",
        "cash_unit_price": "Peşin birim 100 TRY", "term_unit_price": "Vadeli birim 110 TRY",
        "term_days": "Vade 60 gün", "vat_rate_pct": "KDV %20", "vat_basis": "KDV matrahı anapara ve vade farkı",
        "price_basis": "Fiyatlar KDV hariç", "delivery_days": "Teslim 3 gün", "technical_specs.grade": "Grade A",
    }
    message_id = "offer-" + offer_id
    return {"id": offer_id, "request_id": "R1", "supplier": "Sentetik Tedarikçi " + offer_id, "material_code": "PE",
            **values, "technical_specs": {"grade": "A"}, "missing_fields": [],
            "email": {"message_id": message_id, "sender": "teklif@example.com", "received_at": received_at,
                      "subject": "PE fiyat teklifi", "body_excerpt": "\n".join(quotations.values())},
            "source_fields": {key: {"quote": quote, "message_id": message_id} for key, quote in quotations.items()}}


def node(kind, **fields):
    return {"id": kind + "-test", "data": {"type": kind, "node": {"template": {key: {"value": value} for key, value in fields.items()}}}}


def research_payload(code="PE", name="Polietilen", as_of=BEFORE):
    return {"as_of": as_of, "status": "completed", "profile": {"region": "Türkiye", "lookback_days": 30,
            "materials": [{"material_code": code, "name": name}]},
            "materials": [{"material_code": code, "name": name,
                "suppliers": [{"evidence_id": "S1", "name": "Kamuya açık aday", "title": "PE tedarikçisi",
                    "url": "https://supplier.example.com/pe", "retrieved_at": BEFORE,
                    "verification": "search_candidate", "existing_supplier_match": False}],
                "news": [{"evidence_id": "N1", "title": "PE piyasası hakkında haber", "url": "https://news.example.com/pe",
                    "publisher": "Örnek Haber", "published_at": "2026-10-12T10:00:00+03:00", "retrieved_at": BEFORE,
                    "verification": "headline_only"}]}], "warnings": []}


class CaseWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.queue = self.folder / "cases" / "requests.db"
        self.langflow = self.folder / "langflow.db"
        self.output = self.folder / "drafts"
        self.research = self.folder / "current_research.json"

    def tearDown(self):
        self.temporary.cleanup()

    def store(self, deadline=DEADLINE):
        store = CaseStore(self.queue)
        ingest_request(request_payload(deadline=deadline), store, now=BEFORE)
        return store

    def configured_flow(self, mailbox="", enabled=False, endpoint=""):
        with sqlite3.connect(self.langflow) as connection:
            connection.execute("CREATE TABLE flow (id TEXT PRIMARY KEY,data TEXT)")
            graph = {"nodes": [node("MailResearchInput", bot_mailbox=mailbox, api_key="synthetic-secret-never-output"),
                               node("SAPProcurementInput", enable_api=enabled, endpoint=endpoint, bearer_token="synthetic-secret-never-output")], "edges": []}
            connection.execute("INSERT INTO flow VALUES (?,?)", (FLOW_ID.replace("-", ""), json.dumps(graph)))
            other = {"nodes": [node("MailResearchInput", bot_mailbox="other-flow@example.com")], "edges": []}
            connection.execute("INSERT INTO flow VALUES (?,?)", ("other-flow", json.dumps(other)))

    def command(self, args, now=BEFORE):
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = main(args, queue_path=self.queue, database=self.langflow, output_dir=self.output, now=now)
        return code, json.loads(captured.getvalue()), captured.getvalue()

    def write_payload(self, name, value):
        path = self.folder / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_status_missing_configuration_does_not_create_queue_or_output(self):
        result = status(self.queue, self.langflow, BEFORE)
        self.assertEqual(result["status"], "mailbox_unconfigured")
        self.assertEqual(result["queue"]["queue_status"], "not_created")
        self.assertEqual(result["queue"]["total_requests"], 0)
        self.assertFalse(self.queue.exists())
        self.assertFalse(self.output.exists())

    def test_configuration_reads_selected_flow_safe_projections_only(self):
        self.configured_flow("bot@example.com", True, "https://sap.example.com/path?api_key=synthetic-secret-never-output")
        result = read_connection_status(self.langflow)
        self.assertEqual(result["bot_mailbox"], "bot@example.com")
        self.assertTrue(result["sap_api_enabled"])
        self.assertTrue(result["sap_endpoint_configured"])
        self.assertFalse(result["mailbox_connectivity_verified"])
        self.assertFalse(result["sap_connectivity_verified"])
        serialized = json.dumps(result)
        self.assertNotIn("synthetic-secret-never-output", serialized)
        self.assertNotIn("sap.example.com", serialized)
        self.assertNotIn("other-flow@example.com", serialized)

    def test_blank_or_invalid_mailbox_is_unconfigured(self):
        self.configured_flow("not a mailbox")
        result = read_connection_status(self.langflow)
        self.assertEqual(result["status"], "mailbox_unconfigured")
        self.assertIsNone(result["bot_mailbox"])

    def test_unreadable_local_databases_are_reported_without_dumping_errors(self):
        self.langflow.write_text("not SQLite")
        result = read_connection_status(self.langflow)
        self.assertEqual(result["configuration_status"], "local_configuration_unreadable")
        self.queue.parent.mkdir()
        self.queue.write_text("not SQLite")
        self.assertEqual(read_queue_counts(self.queue, BEFORE)["queue_status"], "unreadable")

    def test_queue_counts_follow_deadline_and_saved_artifact(self):
        store = self.store(None)
        self.assertEqual(read_queue_counts(self.queue, BEFORE)["awaiting_deadline"], 1)
        store.set_deadline("R1", DEADLINE, "talep@example.com", BEFORE)
        self.assertEqual(read_queue_counts(self.queue, BEFORE)["collecting"], 1)
        self.assertEqual(read_queue_counts(self.queue, DEADLINE)["ready_for_report"], 1)
        prepare_due(self.queue, self.output, AFTER)
        counts = read_queue_counts(self.queue, AFTER)
        self.assertEqual(counts["report_prepared"], 1)
        self.assertEqual(counts["ready_for_report"], 0)

    def test_ingest_request_deadline_requires_valid_original_requester(self):
        store = CaseStore(self.queue)
        pending = ingest_request(request_payload(deadline=None), store, BEFORE)
        self.assertEqual(pending["state"], "awaiting_deadline")
        active = ingest_request(request_payload(deadline=DEADLINE), store, BEFORE)
        self.assertEqual(active["state"], "collecting")
        wrong = request_payload("R2", DEADLINE)
        wrong["request"]["requester"] = None
        with self.assertRaises(ValueError):
            ingest_request(wrong, store, BEFORE)

    def test_empty_worker_calls_do_not_create_data(self):
        self.assertEqual(prepare_questions(self.queue, self.output)["draft_count"], 0)
        self.assertEqual(prepare_due(self.queue, self.output, AFTER)["report_count"], 0)
        self.assertFalse(self.queue.exists())
        self.assertFalse(self.output.exists())

    def test_list_collecting_readonly_metadata_no_queue_create(self):
        empty = list_collecting(self.queue, BEFORE)
        self.assertEqual(empty["request_count"], 0)
        self.assertFalse(self.queue.exists())
        self.store()
        active = list_collecting(self.queue, BEFORE)
        self.assertEqual(active["requests"], [{"request_id": "R1", "deadline_at": "2026-10-13T06:00:00+00:00", "material_code": "PE"}])
        self.assertNotIn("talep@example.com", json.dumps(active))
        self.assertNotIn("Polietilen", json.dumps(active))
        self.assertEqual(list_collecting(self.queue, DEADLINE)["request_count"], 0)

    def test_list_collecting_cli_outputs_only_confirmed_future_request(self):
        self.store()
        code, result, output = self.command(["list-collecting"])
        self.assertEqual(code, 0)
        self.assertEqual(result["request_count"], 1)
        self.assertFalse(result["performed_api_call"])
        self.assertNotIn("required_specs", output)

    def test_question_draft_asks_exact_time_once_and_is_never_sent(self):
        store = self.store(None)
        first = prepare_questions(self.queue, self.output)
        second = prepare_questions(self.queue, self.output)
        self.assertEqual(first["draft_count"], 1)
        self.assertEqual(second["draft_count"], 0)
        text = Path(first["drafts"][0]["path"]).read_text()
        self.assertIn("hangi tarih ve saate", text)
        self.assertIn("Alıcı: talep@example.com", text)
        self.assertIn("GÖNDERİLMEDİ", text)
        self.assertFalse(first["outbound_sent"])
        self.assertFalse(store.snapshot("R1", BEFORE)["question_draft"]["sent"])
        self.assertEqual(read_queue_counts(self.queue, BEFORE)["question_drafts_prepared"], 1)

    def test_interrupted_question_claim_resumes_without_any_send(self):
        store = self.store(None)
        self.assertEqual(len(store.claim_pending_questions()), 1)
        recovered = prepare_questions(self.queue, self.output)
        self.assertEqual(recovered["draft_count"], 1)
        self.assertFalse(recovered["outbound_sent"])
        self.assertEqual(prepare_questions(self.queue, self.output)["draft_count"], 0)

    def test_source_derived_file_name_cannot_escape_output_directory(self):
        store = CaseStore(self.queue)
        ingest_request(request_payload("../../escape", None), store, BEFORE)
        result = prepare_questions(self.queue, self.output)
        path = Path(result["drafts"][0]["path"])
        self.assertEqual(path.parent.parent, self.output.resolve())
        self.assertNotIn("escape", str(path.relative_to(self.output.resolve())))

    def test_missing_empty_changed_question_is_not_ready_and_recovers_original_content(self):
        store = self.store(None)
        first = prepare_questions(self.queue, self.output)
        path = Path(first["drafts"][0]["path"])
        original = path.read_text()
        for mutation in ("empty", "changed", "deleted"):
            if mutation == "deleted":
                path.unlink()
            else:
                path.write_text("" if mutation == "empty" else "SYNTHETIC altered recipient and deadline")
            self.assertEqual(store.snapshot("R1", BEFORE)["question_draft"]["status"], "artifact_missing_or_changed")
            self.assertEqual(read_queue_counts(self.queue, BEFORE)["question_drafts_prepared"], 0)
            repaired = prepare_questions(self.queue, self.output)
            self.assertEqual(repaired["draft_count"], 1)
            new_path = Path(repaired["drafts"][0]["path"])
            self.assertNotEqual(path, new_path)
            self.assertEqual(new_path.read_text(), original)
            if mutation != "deleted":
                self.assertNotEqual(path.read_text(), original)
            path = new_path
        self.assertEqual(read_queue_counts(self.queue, BEFORE)["question_drafts_prepared"], 1)
        self.assertFalse(store.snapshot("R1", BEFORE)["question_draft"]["sent"])

    def test_concurrent_question_repair_publishes_once_without_overwrite(self):
        self.store(None)
        first = prepare_questions(self.queue, self.output)
        path = Path(first["drafts"][0]["path"])
        path.unlink()
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: prepare_questions(self.queue, self.output), range(4)))
        self.assertEqual(sum(item["draft_count"] for item in results), 1)
        self.assertEqual(len(list(self.output.iterdir())), 2)
        self.assertEqual(read_queue_counts(self.queue, BEFORE)["question_drafts_prepared"], 1)

    def test_legacy_question_schema_status_readonly_and_safe_lazy_upgrade(self):
        self.store(None)
        legacy = self.output / "legacy-question.txt"
        legacy.parent.mkdir()
        legacy.write_text("Unverified legacy question")
        fields = ("question_draft_text", "question_draft_sha256", "question_source_sha256")
        with sqlite3.connect(self.queue) as connection:
            for name in fields:
                connection.execute("ALTER TABLE requests DROP COLUMN " + name)
            connection.execute("UPDATE requests SET question_claimed=1,question_draft_path=?", (str(legacy),))
        self.assertEqual(read_queue_counts(self.queue, BEFORE)["question_drafts_prepared"], 0)
        with sqlite3.connect(self.queue) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(requests)")}
        self.assertTrue(set(fields).isdisjoint(columns))
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: CaseStore(self.queue), range(2)))
        repaired = prepare_questions(self.queue, self.output)
        self.assertEqual(repaired["draft_count"], 1)
        self.assertIn("hangi tarih ve saate", Path(repaired["drafts"][0]["path"]).read_text())
        self.assertEqual(legacy.read_text(), "Unverified legacy question")

    def test_report_counts_require_same_request_and_report_revision_as_snapshot(self):
        store = self.store(DEADLINE)
        result = prepare_due(self.queue, self.output, AFTER)
        path = Path(result["reports"][0]["manifest_path"])
        saved = json.loads(path.read_text())
        for field, value in (("request_id", "R2"), ("report_revision", saved["report_revision"] + 1)):
            path.write_text(json.dumps({**saved, field: value}))
            self.assertEqual(store.snapshot("R1", AFTER)["request"]["state"], "ready_for_report")
            self.assertEqual(read_queue_counts(self.queue, AFTER)["report_prepared"], 0)
        path.write_text(json.dumps(saved))
        self.assertEqual(read_queue_counts(self.queue, AFTER)["report_prepared"], 1)

    def test_no_final_report_before_deadline_or_without_reply(self):
        self.store(None)
        self.assertEqual(prepare_due(self.queue, self.output, AFTER)["report_count"], 0)
        self.assertFalse(self.output.exists())
        CaseStore(self.queue).set_deadline("R1", DEADLINE, "talep@example.com", BEFORE)
        self.assertEqual(prepare_due(self.queue, self.output, BEFORE)["report_count"], 0)
        self.assertFalse(self.output.exists())

    def test_no_offer_deadline_report_is_saved_and_not_repeated(self):
        store = self.store()
        result = prepare_due(self.queue, self.output, DEADLINE)
        self.assertEqual(result["report_count"], 1)
        text = Path(result["reports"][0]["path"]).read_text()
        self.assertIn("SÜRE SONU YEREL TASLAK", text)
        self.assertIn("Henüz doğrulanabilir tedarikçi", text)
        self.assertIn("Gönderim: yapılmadı", text)
        self.assertIn("bu talebe bağlanmış doğrulanabilir araştırma kanıtı yok", text)
        self.assertIsNone(result["reports"][0]["research_path"])
        self.assertTrue(Path(result["reports"][0]["html_path"]).is_file())
        self.assertEqual(store.snapshot("R1", AFTER)["request"]["state"], "report_prepared")
        self.assertEqual(prepare_due(self.queue, self.output, AFTER)["report_count"], 0)

    def test_actual_financial_sources_and_technical_fields_in_saved_report(self):
        store = self.store()
        store.append_offer("R1", offer_payload())
        result = prepare_due(self.queue, self.output, AFTER)
        self.assertEqual(result["report_count"], 1)
        text = Path(result["reports"][0]["path"]).read_text()
        for expected in ("Vade farkı toplam", "Basit aylık %", "Efektif aylık %", "KDV %", "Teknik özellikler", "Grade A", "offer-Q1"):
            self.assertIn(expected, text)
        self.assertIn("1320.00", text)

    def test_late_offer_generates_new_revision_and_is_excluded_from_cutoff_ranking(self):
        store = self.store()
        prepare_due(self.queue, self.output, DEADLINE)
        store.append_offer("R1", offer_payload(received_at=AFTER))
        result = prepare_due(self.queue, self.output, AFTER)
        self.assertEqual(result["reports"][0]["report_revision"], 2)
        text = Path(result["reports"][0]["path"]).read_text()
        self.assertIn("Son tarihten sonra gelen teklif", text)
        self.assertNotIn("maliyet incelemesine alınabilecek teklif", text)

    def test_report_revision_race_does_not_mark_stale_report_prepared(self):
        store = self.store()

        def analyze(snapshot):
            store.append_offer("R1", offer_payload())
            return {"report_status": "final"}

        result = prepare_due(self.queue, self.output, AFTER, analyze=analyze, render=lambda _: "Sentetik rapor", write_html=False)
        self.assertEqual(result["status"], "report_preparation_failed")
        self.assertEqual(result["report_count"], 0)
        self.assertEqual(store.snapshot("R1", AFTER)["request"]["state"], "ready_for_report")

    def test_preliminary_analyzer_result_cannot_be_marked_prepared(self):
        store = self.store()
        result = prepare_due(self.queue, self.output, AFTER, analyze=lambda _: {"report_status": "preliminary"}, render=lambda _: "Pending")
        self.assertEqual(result["report_count"], 0)
        self.assertEqual(store.snapshot("R1", AFTER)["request"]["state"], "ready_for_report")
        self.assertFalse(self.output.exists())

    def test_html_escapes_untrusted_email_markup_and_formats_table(self):
        rendered = _report_html("Yönetici raporu\n| Alan | Değer |\n|---|---|\n| Gövde | <script>bad()</script> |\n<img src=x onerror=bad()>\n")
        self.assertIn("<table>", rendered)
        self.assertIn("&lt;script&gt;bad()&lt;/script&gt;", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("<img src=x", rendered)
        self.assertIn('<html lang="tr">', rendered)

    def test_exact_material_public_evidence_linked_with_saved_case_binding(self):
        store = self.store()
        self.research.write_text(json.dumps(research_payload(), ensure_ascii=False))
        result = prepare_due(self.queue, self.output, AFTER, research_path=self.research)
        report = result["reports"][0]
        self.assertEqual(report["research_status"], "matching_public_evidence_linked")
        self.assertEqual(report["research_source_count"], 2)
        binding = json.loads(Path(report["research_path"]).read_text())
        self.assertEqual(binding["request_id"], "R1")
        self.assertEqual(binding["material_code"], "PE")
        self.assertEqual(binding["source_as_of"], "2026-10-13T05:00:00+00:00")
        self.assertEqual(binding["source_retrieval_instants"]["N1"], "2026-10-13T05:00:00+00:00")
        text = Path(report["path"]).read_text()
        self.assertIn("PE piyasası hakkında haber", text)
        self.assertIn("Kamuya açık aday", text)
        self.assertFalse(binding["full_multi_model_run_verified"])
        self.assertEqual(store.snapshot("R1", AFTER)["request"]["state"], "report_prepared")

    def test_other_material_global_research_is_never_attached(self):
        store = self.store()
        self.research.write_text(json.dumps(research_payload("PP", "Polipropilen")))
        result = prepare_due(self.queue, self.output, AFTER, research_path=self.research)
        report = result["reports"][0]
        self.assertEqual(report["research_status"], "scope_mismatch")
        self.assertIsNone(report["research_path"])
        self.assertNotIn("PE piyasası hakkında haber", Path(report["path"]).read_text())
        self.assertEqual(store.snapshot("R1", AFTER)["request"]["state"], "report_prepared")

    def test_matching_multiple_material_scope_narrows_only_requested_material(self):
        request = self.store().snapshot("R1", AFTER)["request"]
        payload = research_payload()
        other = research_payload("PP", "Polipropilen")
        other["materials"][0]["suppliers"][0]["name"] = "PP_OTHER_SUPPLIER_SENTINEL"
        other["materials"][0]["news"][0]["title"] = "PP_OTHER_NEWS_SENTINEL"
        payload["profile"]["materials"] += other["profile"]["materials"]
        payload["materials"] += other["materials"]
        self.research.write_text(json.dumps(payload))
        section = research_attachment(request, self.research, AFTER)
        self.assertEqual(section["source_count"], 2)
        canonical = section["payload"]["research"]
        self.assertEqual(len(canonical["profile"]["materials"]), 1)
        self.assertEqual(len(canonical["materials"]), 1)
        self.assertEqual(canonical["materials"][0]["material_code"], "PE")
        serialized = json.dumps(section["payload"]) + "\n".join(section["lines"])
        self.assertNotIn("PP_OTHER_SUPPLIER_SENTINEL", serialized)
        self.assertNotIn("PP_OTHER_NEWS_SENTINEL", serialized)

    def test_deadline_hour_not_date_controls_public_research_cutoff(self):
        store = self.store()
        self.research.write_text(json.dumps(research_payload(as_of="2026-10-13T09:00:01+03:00")))
        section = research_attachment(store.snapshot("R1", AFTER)["request"], self.research, AFTER)
        self.assertEqual(section["status"], "after_deadline")
        self.assertIsNone(section["payload"])
        payload = research_payload()
        for kind in ("suppliers", "news"):
            payload["materials"][0][kind][0]["retrieved_at"] = "2026-10-13T09:00:01+03:00"
        self.research.write_text(json.dumps(payload))
        section = research_attachment(store.snapshot("R1", AFTER)["request"], self.research, AFTER)
        self.assertEqual(section["status"], "no_valid_public_sources")

    def test_scope_name_profile_and_request_reference_must_all_match(self):
        request = self.store().snapshot("R1", AFTER)["request"]
        for variant in ("different_name", "profile_differs", "multiple_materials", "other_request", "naive_as_of"):
            payload = research_payload()
            if variant == "different_name":
                payload["materials"][0]["name"] = "Başka malzeme"
            elif variant == "profile_differs":
                payload["profile"]["materials"][0]["material_code"] = "PP"
            elif variant == "multiple_materials":
                payload["materials"].append({"material_code": "PP", "name": "Polipropilen"})
            elif variant == "other_request":
                payload["request_id"] = "R2"
            else:
                payload["as_of"] = "2026-10-13T08:00:00"
            self.research.write_text(json.dumps(payload))
            with self.subTest(variant=variant):
                self.assertIsNone(research_attachment(request, self.research, AFTER)["payload"])

    def test_bound_case_research_for_old_deadline_is_not_reused_after_reschedule(self):
        request = self.store().snapshot("R1", AFTER)["request"]
        payload = research_payload()
        payload["request_id"] = "R1"
        payload["deadline_at"] = "2026-10-13T08:30:00+03:00"
        self.research.write_text(json.dumps(payload))
        self.assertEqual(research_attachment(request, self.research, AFTER)["status"], "request_deadline_mismatch")

    def test_bound_public_research_drops_unused_secret_configuration_fields(self):
        request = self.store().snapshot("R1", AFTER)["request"]
        payload = research_payload()
        payload["api_key"] = "synthetic-secret-never-copy"
        payload["profile"]["api_key"] = "synthetic-secret-never-copy"
        payload["materials"][0]["suppliers"][0]["api_key"] = "synthetic-secret-never-copy"
        self.research.write_text(json.dumps(payload))
        section = research_attachment(request, self.research, AFTER)
        self.assertEqual(section["source_count"], 2)
        self.assertNotIn("synthetic-secret-never-copy", json.dumps(section["payload"]))

    def test_partly_late_research_sources_make_attached_research_partial(self):
        request = self.store().snapshot("R1", AFTER)["request"]
        payload = research_payload()
        payload["materials"][0]["news"][0]["retrieved_at"] = "2026-10-13T09:00:01+03:00"
        self.research.write_text(json.dumps(payload))
        section = research_attachment(request, self.research, AFTER)
        self.assertEqual(section["source_count"], 1)
        self.assertEqual(section["payload"]["research"]["status"], "partial")
        self.assertIn("Araştırma durumu: kısmi", "\n".join(section["lines"]))
        self.assertNotIn("PE piyasası hakkında haber", "\n".join(section["lines"]))

    def test_cli_ingests_and_reports_metadata_without_body_to_stdout(self):
        path = self.write_payload("request.json", request_payload(deadline=DEADLINE))
        code, result, output = self.command(["ingest-request", "--file", str(path)])
        self.assertEqual(code, 0)
        self.assertEqual(result["state"], "collecting")
        offer = offer_payload()
        offer["email"]["body_excerpt"] += "\nPRIVATE BODY SENTINEL"
        path = self.write_payload("offer.json", offer)
        code, result, output = self.command(["ingest-offer", "--request-id", "R1", "--file", str(path)])
        self.assertEqual(code, 0)
        self.assertEqual(result["offer_count"], 1)
        self.assertNotIn("PRIVATE BODY SENTINEL", output)
        code, result, output = self.command(["prepare-due", "--no-html"], AFTER)
        self.assertEqual(code, 0)
        self.assertEqual(result["report_count"], 1)
        self.assertIsNone(result["reports"][0]["html_path"])
        self.assertNotIn("PRIVATE BODY SENTINEL", output)

    def test_cli_unauthorized_deadline_or_secret_payload_fails_safely(self):
        self.store(None)
        code, result, output = self.command(["set-deadline", "--request-id", "R1", "--at", DEADLINE, "--requester", "other@example.com"])
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "local_command_failed")
        payload = request_payload("R2")
        payload["request"]["api_key"] = "synthetic-secret-never-output"
        path = self.write_payload("secret.json", payload)
        code, result, output = self.command(["ingest-request", "--file", str(path)])
        self.assertEqual(code, 1)
        self.assertNotIn("synthetic-secret-never-output", output)

    def test_duplicate_request_identity_json_is_rejected_before_queue_write(self):
        path = self.folder / "duplicate-request.json"
        path.write_text('{"source":"manual","request":{"id":"VISIBLE-R1","id":"ACTUAL-R2",'
                        '"material_name":"Sentetik PE","quantity":"10","unit":"KG",'
                        '"requester":"owner@example.com"}}')
        code, result, output = self.command(["ingest-request", "--file", str(path)])
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "local_command_failed")
        self.assertFalse(self.queue.exists())
        self.assertNotIn("VISIBLE-R1", output)
        self.assertNotIn("ACTUAL-R2", output)

    def test_duplicate_financial_source_json_rejected_before_any_queue_write(self):
        path = self.folder / "duplicate-offer.json"
        path.write_text('{"request_id":"R1","source_fields":{"cash_unit_price":{'
                        '"message_id":"original-message","message_id":"rebound-message",'
                        '"quote":"SYNTHETIC_PRIVATE_PRICE"}}}')
        code, result, output = self.command(["ingest-offer", "--request-id", "R1", "--file", str(path)])
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "local_command_failed")
        self.assertFalse(self.queue.exists())
        self.assertNotIn("SYNTHETIC_PRIVATE_PRICE", output)

    def test_json_file_rejects_constants_depth_and_growth_after_stat(self):
        path = self.folder / "unsafe.json"
        for content in ('{"value":NaN}', '{"value":Infinity}', '{"value":1e999}', "[" * 20 + "]" * 20):
            path.write_text(content)
            with self.subTest(content=content), self.assertRaises(ValueError):
                _load_json(path)
        path.write_bytes(b"[" + b" " * 500_001 + b"]")
        with patch("case_worker.Path.stat", return_value=SimpleNamespace(st_size=1)), self.assertRaises(ValueError):
            _load_json(path)

    def test_valid_json_file_keeps_native_fields_and_values_unchanged(self):
        payload = request_payload("R-SAFE", DEADLINE)
        payload["request"]["required_specs"] = {"grade": "A", "width": "30 mikron"}
        path = self.write_payload("valid-source.json", payload)
        self.assertEqual(_load_json(path), payload)


if __name__ == "__main__":
    unittest.main()
