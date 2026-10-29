"""Gerçek yerel plan ve denetleyici ile geçici dosyalarda işleyici doğrulaması."""

import contextlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

COMPONENTS = Path(__file__).resolve().parent / "components"
sys.path.insert(0, str(COMPONENTS))

from case_store import CaseStore
from negotiation_draft import build_negotiation_prompt
from negotiation_worker import AGENT, MARKET, _configured_history, main, prepare_negotiations
from test_case_store import AFTER, BEFORE, DEADLINE, sample_request
from test_negotiation_plan import purchase_record
from test_quote_comparison import sample_offer


class NegotiationWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.queue = self.folder / "cases" / "requests.db"
        self.output = self.folder / "drafts"
        self.writer_calls = []
        self.market_calls = []
        self.graph_calls = []
        self.graph = {
            "nodes": [
                {
                    "id": MARKET,
                    "data": {"node": {"template": {"price_history": {"value": ""}, "enable_live": {"value": False}}}},
                }
            ],
            "edges": [],
        }

    def tearDown(self):
        self.temporary.cleanup()

    def create_case(self, *, deadline=True, offers=True):
        store = CaseStore(self.queue)
        store.upsert_request(sample_request(), "mail_request")
        if deadline:
            store.set_deadline("R1", DEADLINE, "talep@example.com", now=BEFORE)
        if offers:
            for index in range(2):
                store.append_offer("R1", self.offer(index))
        return store

    def offer(self, index, *, quantity="10"):
        offer = sample_offer(
            "Q" + str(index + 1), "Tedarikçi " + str(index + 1), cash=str(100 + 5 * index), term=str(110 + 5 * index)
        )
        offer["request_id"] = "R1"
        offer["email"]["sender"] = "teklif" + str(index + 1) + "@example.com"
        offer["quantity"] = quantity
        quote = "Miktar " + quantity + " KG"
        offer["email"]["body_excerpt"] += "\n" + quote
        offer["source_fields"]["quantity"]["quote"] = quote
        return offer

    def load_graph(self):
        self.graph_calls.append(True)
        return self.graph

    def market_reader(self, case, **kwargs):
        self.market_calls.append((case, kwargs))
        return {}

    def writer(self, plan, graph):
        self.writer_calls.append((plan, graph))
        prompt = json.loads(build_negotiation_prompt(plan))
        drafts = []
        for row in prompt["requests"]:
            drafts.append(
                {
                    "request_id": row["request_id"],
                    "target_offer_id": row["target_offer_id"],
                    "subject": row["suggested_subject"],
                    "body": "Merhaba,\n\n" + "\n\n".join(row["required_paragraphs"]) + "\n\nİyi çalışmalar.",
                }
            )
        return json.dumps({"drafts": drafts}, ensure_ascii=False), {
            "run_completed": True,
            "provider": "synthetic_callback",
        }

    def prepare(self, **kwargs):
        return prepare_negotiations(
            queue_path=self.queue,
            output_dir=self.output,
            now=AFTER,
            graph_loader=self.load_graph,
            writer=self.writer,
            market_reader=self.market_reader,
            **kwargs,
        )

    def metadata(self):
        with sqlite3.connect(self.queue) as connection:
            row = connection.execute("""SELECT j.metadata_path FROM negotiation_heads h
                JOIN negotiation_jobs j ON j.job_key=h.job_key WHERE h.request_id='R1'""").fetchone()
        self.assertIsNotNone(row)
        return json.loads(Path(row[0]).read_text(encoding="utf-8"))

    def test_missing_queue_does_not_create_queue_output_or_touch_integrations(self):
        result = self.prepare(use_model=True)
        self.assertEqual(result["status"], "no_requests")
        self.assertFalse(self.queue.exists())
        self.assertFalse(self.output.exists())
        self.assertEqual((len(self.graph_calls), len(self.market_calls), len(self.writer_calls)), (0, 0, 0))

    def test_awaiting_deadline_never_reads_market_or_runs_writer(self):
        self.create_case(deadline=False)
        result = self.prepare(use_model=True)
        self.assertEqual(result["status"], "no_due_negotiations")
        self.assertEqual((len(self.graph_calls), len(self.market_calls), len(self.writer_calls)), (0, 0, 0))
        self.assertFalse(self.output.exists())

    def test_before_deadline_never_reads_market_or_runs_writer(self):
        self.create_case()
        result = prepare_negotiations(
            queue_path=self.queue,
            output_dir=self.output,
            now=BEFORE,
            graph_loader=self.load_graph,
            writer=self.writer,
            market_reader=self.market_reader,
            use_model=True,
        )
        self.assertEqual(result["status"], "no_due_negotiations")
        self.assertEqual((len(self.market_calls), len(self.writer_calls)), (0, 0))

    def test_wrong_quantity_scope_blocks_market_and_writer(self):
        store = self.create_case(offers=False)
        for index in range(2):
            store.append_offer("R1", self.offer(index, quantity="20"))
        result = self.prepare(use_model=True)
        self.assertEqual(result["status"], "awaiting_comparable_offers")
        self.assertEqual((len(self.market_calls), len(self.writer_calls)), (0, 0))
        self.assertFalse(self.output.exists())

    def test_single_offer_blocks_market_and_writer(self):
        store = self.create_case(offers=False)
        store.append_offer("R1", self.offer(0))
        result = self.prepare(use_model=True)
        self.assertEqual(result["draft_count"], 0)
        self.assertEqual((len(self.market_calls), len(self.writer_calls)), (0, 0))

    def test_real_source_plan_and_reviewer_prepare_draft_only_files(self):
        self.create_case()
        result = self.prepare(use_model=True)
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(result["draft_count"], 1)
        row = result["drafts"][0]
        self.assertTrue(row["model_completed"])
        self.assertTrue(row["model_text_verified"])
        self.assertTrue(Path(row["path"]).is_file())
        self.assertTrue(Path(row["html_path"]).is_file())
        self.assertFalse(result["emails_sent"])
        self.assertFalse(result["orders_placed"])
        self.assertFalse(result["full_multi_model_run_verified"])
        metadata = self.metadata()
        self.assertEqual(metadata["plan"]["requests"][0]["target_offer_id"], "Q1")
        self.assertEqual(metadata["draft_result"]["drafts"][0]["recipient"], "teklif1@example.com")

    def test_cached_second_check_avoids_market_and_paid_writer_repeat(self):
        self.create_case()
        first = self.prepare(use_model=True)
        second = self.prepare(use_model=True)
        self.assertEqual(second["drafts"][0]["status"], "cached")
        self.assertEqual(second["drafts"][0]["path"], first["drafts"][0]["path"])
        self.assertEqual((len(self.market_calls), len(self.writer_calls)), (1, 1))

    def test_offer_revision_invalidates_cached_result(self):
        store = self.create_case()
        self.prepare(use_model=True)
        first = self.metadata()
        store.append_offer("R1", self.offer(2))
        result = self.prepare(use_model=True)
        second = self.metadata()
        self.assertEqual(result["drafts"][0]["status"], "ready")
        self.assertEqual((len(self.market_calls), len(self.writer_calls)), (2, 2))
        self.assertEqual(second["offer_revision"], first["offer_revision"] + 1)
        self.assertNotEqual(second["input_fingerprint"], first["input_fingerprint"])

    def test_unavailable_writer_uses_verified_local_template_without_model_claim(self):
        self.create_case()

        def fail_writer(plan, graph):
            self.writer_calls.append((plan, graph))
            raise RuntimeError("Synthetic API outage")

        result = prepare_negotiations(
            queue_path=self.queue,
            output_dir=self.output,
            now=AFTER,
            graph_loader=self.load_graph,
            writer=fail_writer,
            market_reader=self.market_reader,
            use_model=True,
        )
        row = result["drafts"][0]
        self.assertFalse(row["model_completed"])
        self.assertFalse(row["model_text_verified"])
        metadata = self.metadata()
        self.assertEqual(metadata["draft_result"]["drafts"][0]["generation_source"], "local_template")
        self.assertFalse(metadata["draft_result"]["drafts"][0]["model_review"]["verified"])
        self.assertNotIn("Synthetic API outage", json.dumps(metadata, ensure_ascii=False))
        second = self.prepare(use_model=True)
        self.assertEqual(second["drafts"][0]["status"], "cached")
        self.assertEqual(len(self.writer_calls), 1)

    def test_history_update_invalidates_cache_and_updates_proven_historical_price(self):
        self.create_case()
        first_history = {"purchases": [purchase_record(price="95")]}
        self.prepare(use_model=True, history=first_history)
        first = self.metadata()
        self.prepare(use_model=True, history={"purchases": [purchase_record(price="96")]})
        second = self.metadata()
        self.assertEqual(len(self.writer_calls), 2)
        self.assertNotEqual(second["input_fingerprint"], first["input_fingerprint"])
        self.assertEqual(second["plan"]["requests"][0]["history"]["unit_price"], "96")

    def test_default_graph_market_live_download_is_disabled(self):
        self.create_case()
        self.prepare()
        self.assertFalse(self.market_calls[0][1]["enable_live"])
        self.assertEqual(len(self.writer_calls), 0)
        self.assertFalse(self.metadata()["model_attempted"])

    def test_completed_model_response_still_falls_back_when_new_price_is_invented(self):
        self.create_case()

        def unsafe_writer(plan, graph):
            text, metadata = self.writer(plan, graph)
            payload = json.loads(text)
            payload["drafts"][0]["body"] = payload["drafts"][0]["body"].replace(
                "İyi çalışmalar.", "Fiyatınız 1 TRY olsun.\nİyi çalışmalar."
            )
            return json.dumps(payload, ensure_ascii=False), metadata

        result = prepare_negotiations(
            queue_path=self.queue,
            output_dir=self.output,
            now=AFTER,
            graph_loader=self.load_graph,
            writer=unsafe_writer,
            market_reader=self.market_reader,
            use_model=True,
        )
        self.assertTrue(result["drafts"][0]["model_completed"])
        self.assertFalse(result["drafts"][0]["model_text_verified"])
        self.assertEqual(self.metadata()["draft_result"]["drafts"][0]["generation_source"], "local_template")
        self.assertNotIn("Fiyatınız 1 TRY olsun", Path(result["drafts"][0]["path"]).read_text(encoding="utf-8"))

    def test_history_is_read_from_saved_flow_single_field(self):
        self.create_case()
        history_text = json.dumps({"purchases": [purchase_record()]})
        self.graph["nodes"][0]["data"]["node"]["template"]["price_history"]["value"] = history_text
        self.prepare()
        self.assertEqual(self.market_calls[0][1]["price_history"], history_text)
        self.assertEqual(self.metadata()["plan"]["requests"][0]["history"]["unit_price"], "95")

    def test_configured_history_handles_missing_node_and_explicit_enable_flag(self):
        self.assertEqual(_configured_history({"nodes": []}), ("", False))
        self.graph["nodes"][0]["data"]["node"]["template"]["enable_live"]["value"] = "true"
        self.assertFalse(_configured_history(self.graph)[1])
        self.graph["nodes"][0]["data"]["node"]["template"]["enable_live"]["value"] = True
        self.assertTrue(_configured_history(self.graph)[1])

    def test_live_market_configuration_change_invalidates_cached_draft(self):
        self.create_case()
        self.prepare(use_model=True)
        first = self.metadata()
        self.graph["nodes"][0]["data"]["node"]["template"]["enable_live"]["value"] = True
        second = self.prepare(use_model=True)
        self.assertNotEqual(self.metadata()["input_fingerprint"], first["input_fingerprint"])
        self.assertEqual(len(self.writer_calls), 2)
        self.assertTrue(self.market_calls[-1][1]["enable_live"])
        self.assertNotEqual(second["drafts"][0]["status"], "cached")

    def test_supplied_market_observations_change_invalidates_cached_draft_and_is_forwarded(self):
        self.create_case()
        self.prepare(use_model=True)
        first = self.metadata()
        observations = '{"usdtry":[]}'
        self.graph["nodes"][0]["data"]["node"]["template"]["observations_json"] = {"value": observations}
        self.prepare(use_model=True)
        self.assertEqual(self.market_calls[-1][1]["observations"], observations)
        self.assertNotEqual(self.metadata()["input_fingerprint"], first["input_fingerprint"])
        self.assertEqual(len(self.writer_calls), 2)

    def test_selected_writer_model_change_invalidates_cached_draft(self):
        self.create_case()
        self.prepare(use_model=True)
        first = self.metadata()
        self.graph["nodes"].append(
            {
                "id": AGENT,
                "data": {
                    "node": {
                        "template": {"model": {"value": [{"provider": "Anthropic", "name": "synthetic-new-model"}]}}
                    }
                },
            }
        )
        self.prepare(use_model=True)
        self.assertNotEqual(self.metadata()["input_fingerprint"], first["input_fingerprint"])
        self.assertEqual(len(self.writer_calls), 2)

    def test_request_filter_never_touches_other_due_requests(self):
        self.create_case()
        result = self.prepare(request_id="UNKNOWN", use_model=True)
        self.assertEqual(result["status"], "no_due_negotiations")
        self.assertEqual((len(self.market_calls), len(self.writer_calls)), (0, 0))

    def test_local_draft_can_be_upgraded_once_to_model_checked_draft(self):
        self.create_case()
        self.prepare(use_model=False)
        second = self.prepare(use_model=True)
        self.assertEqual(len(self.writer_calls), 1)
        self.assertTrue(second["drafts"][0]["model_text_verified"])
        self.assertTrue(self.metadata()["model_attempted"])

    def test_refresh_explicitly_rebuilds_and_reruns_writer(self):
        self.create_case()
        self.prepare(use_model=True)
        self.prepare(use_model=True, refresh=True)
        self.assertEqual(len(self.writer_calls), 2)

    def agent_settings(self, **values):
        node = next((item for item in self.graph["nodes"] if item["id"] == AGENT), None)
        if node is None:
            node = {"id": AGENT, "data": {"node": {"template": {}}}}
            self.graph["nodes"].append(node)
        node["data"]["node"]["template"].update({key: {"value": value} for key, value in values.items()})

    def test_concurrent_triggers_and_refresh_inflight_call_one_writer(self):
        self.create_case()
        entered, release = Event(), Event()

        def waiting_writer(plan, graph):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return self.writer(plan, graph)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                prepare_negotiations,
                queue_path=self.queue,
                output_dir=self.output,
                now=AFTER,
                use_model=True,
                graph_loader=self.load_graph,
                writer=waiting_writer,
                market_reader=self.market_reader,
            )
            self.assertTrue(entered.wait(timeout=5))
            second = self.prepare(use_model=True, refresh=True)
            self.assertEqual(second["status"], "in_progress")
            self.assertEqual(second["drafts"][0]["status"], "in_progress")
            self.assertEqual(second["draft_count"], 0)
            release.set()
            prepared = first.result(timeout=5)
        cached = self.prepare(use_model=True)
        self.assertEqual(len(self.writer_calls), 1)
        self.assertEqual(len(self.market_calls), 1)
        self.assertEqual(cached["drafts"][0]["path"], prepared["drafts"][0]["path"])

    def test_system_message_temperature_tools_and_policy_versions_invalidate_cache(self):
        self.create_case()
        self.agent_settings(system_message="policy-v1", temperature=0, tools=[], max_tokens=2048)
        self.prepare(use_model=True)
        previous = self.metadata()["input_fingerprint"]
        for setting in (
            {"system_message": "policy-v2"},
            {"temperature": 0.3},
            {"tools": ["new-safe-tool"]},
            {"max_tokens": 64},
        ):
            self.agent_settings(**setting)
            result = self.prepare(use_model=True)
            current = self.metadata()["input_fingerprint"]
            self.assertNotEqual(current, previous)
            self.assertNotEqual(result["drafts"][0]["status"], "cached")
            previous = current
        with patch("negotiation_worker.POLICY_VERSION", 999):
            self.prepare(use_model=True)
            self.assertNotEqual(self.metadata()["input_fingerprint"], previous)
        self.assertEqual(len(self.writer_calls), 6)

    def test_secrets_do_not_enter_fingerprint_artifacts_or_job_database(self):
        self.create_case()
        self.agent_settings(
            system_message="safe-policy",
            api_key="synthetic-secret-A",
            password="synthetic-secret-B",
            tools=[{"name": "safe-tool", "api_key": "synthetic-secret-C", "API Key": "synthetic-secret-G"}],
        )
        self.prepare(use_model=True)
        fingerprint = self.metadata()["input_fingerprint"]
        self.agent_settings(
            api_key="synthetic-secret-D",
            password="synthetic-secret-E",
            tools=[{"name": "safe-tool", "api_key": "synthetic-secret-F", "API Key": "synthetic-secret-H"}],
        )
        result = self.prepare(use_model=True)
        self.assertEqual(result["drafts"][0]["status"], "cached")
        self.assertEqual(self.metadata()["input_fingerprint"], fingerprint)
        all_bytes = self.queue.read_bytes() + b"".join(
            path.read_bytes() for path in self.output.rglob("*") if path.is_file()
        )
        self.assertNotIn(b"synthetic-secret-", all_bytes)
        self.assertEqual(len(self.writer_calls), 1)

    def test_cache_verifies_txt_html_metadata_and_manifest_before_reuse(self):
        self.create_case()
        first = self.prepare(use_model=True)["drafts"][0]
        for key in ("path", "html_path", "metadata_path", "manifest_path"):
            path = Path(first[key])
            original = path.read_bytes()
            path.write_bytes(b"tampered")
            with self.subTest(key=key):
                result = self.prepare(use_model=True)
                self.assertEqual(result["drafts"][0]["status"], "artifact_invalid")
                self.assertEqual(result["draft_count"], 0)
                self.assertEqual(len(self.writer_calls), 1)
            path.write_bytes(original)
        cached = self.prepare(use_model=True)["drafts"][0]
        self.assertTrue(cached["model_attempted"])
        self.assertTrue(cached["model_completed"])
        self.assertTrue(cached["model_text_verified"])

    def test_cache_reapplies_current_validator_before_claiming_verified(self):
        self.create_case()
        self.prepare(use_model=True)
        from negotiation_draft import review_negotiation_drafts as reviewer

        with patch(
            "negotiation_worker.review_negotiation_drafts", side_effect=lambda plan, response=None: reviewer(plan)
        ):
            result = self.prepare(use_model=True)
        self.assertEqual(result["drafts"][0]["status"], "artifact_invalid")
        self.assertFalse(result["drafts"][0]["model_text_verified"])
        self.assertEqual(len(self.writer_calls), 1)

    def test_changed_source_is_checked_before_cache_return(self):
        store = self.create_case()
        self.prepare(use_model=True)
        from negotiation_plan import plan_negotiation as planner

        def blocked(case, **kwargs):
            altered = dict(case)
            altered["offers"] = []
            return planner(altered, **kwargs)

        with patch("negotiation_worker.plan_negotiation", side_effect=blocked):
            result = self.prepare(use_model=True)
        self.assertEqual(result["drafts"][0]["status"], "awaiting_comparable_offers")
        self.assertEqual(result["draft_count"], 0)
        self.assertEqual(len(self.writer_calls), 1)
        self.assertEqual(store.snapshot("R1", AFTER)["request"]["offer_revision"], 2)

    def test_process_crash_after_provider_attempt_never_blindly_retries(self):
        self.create_case()
        root = str(Path(__file__).resolve().parent)
        script = """import os,sys
sys.path[:0]=[sys.argv[1],sys.argv[1]+'/components']
from negotiation_worker import prepare_negotiations
prepare_negotiations(queue_path=sys.argv[2],output_dir=sys.argv[3],now=sys.argv[4],use_model=True,
 graph_loader=lambda:{'nodes':[]},market_reader=lambda *a,**k:{},writer=lambda *a:os._exit(17))
"""
        exited = subprocess.run(
            [sys.executable, "-c", script, root, str(self.queue), str(self.output), AFTER],
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(exited.returncode, 17)
        with sqlite3.connect(self.queue) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM negotiation_attempts").fetchone()[0], 1)
            connection.execute("UPDATE negotiation_jobs SET lease_until=0")
        for refresh in (False, True):
            result = self.prepare(use_model=True, refresh=refresh)
            self.assertEqual(result["drafts"][0]["status"], "model_outcome_uncertain")
            self.assertTrue(result["drafts"][0]["model_attempted"])
            self.assertFalse(result["drafts"][0]["model_completed"])
            self.assertEqual(result["draft_count"], 0)
        self.assertEqual(len(self.writer_calls), 0)

    def test_expired_claim_before_attempt_is_recoverable(self):
        self.create_case()

        def stopped_market(*args, **kwargs):
            raise KeyboardInterrupt("synthetic process stop before provider")

        with self.assertRaises(KeyboardInterrupt):
            prepare_negotiations(
                queue_path=self.queue,
                output_dir=self.output,
                now=AFTER,
                use_model=True,
                graph_loader=self.load_graph,
                writer=self.writer,
                market_reader=stopped_market,
            )
        with sqlite3.connect(self.queue) as connection:
            connection.execute("UPDATE negotiation_jobs SET lease_until=0")
        result = self.prepare(use_model=True)
        self.assertTrue(result["drafts"][0]["model_text_verified"])
        self.assertEqual(len(self.writer_calls), 1)

    def test_failed_model_attempt_cached_fallback_is_honest_and_refresh_is_blocked(self):
        self.create_case()

        def failed(plan, graph):
            self.writer_calls.append(True)
            raise RuntimeError("synthetic ambiguous provider response")

        first = prepare_negotiations(
            queue_path=self.queue,
            output_dir=self.output,
            now=AFTER,
            use_model=True,
            graph_loader=self.load_graph,
            writer=failed,
            market_reader=self.market_reader,
        )
        self.assertTrue(first["errors"])
        second = self.prepare(use_model=True, refresh=True)
        row = second["drafts"][0]
        self.assertEqual(row["status"], "cached")
        self.assertTrue(row["refresh_blocked"])
        self.assertTrue(row["model_outcome_uncertain"])
        self.assertFalse(row["model_completed"])
        self.assertFalse(row["model_text_verified"])
        self.assertEqual(len(self.writer_calls), 1)

    def test_changed_policy_cannot_evade_unresolved_provider_attempt(self):
        self.create_case()

        def failed(plan, graph):
            self.writer_calls.append(True)
            raise RuntimeError("synthetic unknown provider outcome")

        prepare_negotiations(
            queue_path=self.queue,
            output_dir=self.output,
            now=AFTER,
            use_model=True,
            graph_loader=self.load_graph,
            writer=failed,
            market_reader=self.market_reader,
        )
        self.agent_settings(system_message="changed-policy-after-provider-failure")
        second = self.prepare(use_model=True, refresh=True)
        self.assertEqual(second["drafts"][0]["status"], "model_outcome_uncertain")
        self.assertEqual(second["draft_count"], 0)
        self.assertEqual(len(self.writer_calls), 1)
        # A deterministic local draft can still be prepared without charging.
        local = self.prepare(use_model=False)
        self.assertEqual(local["draft_count"], 1)
        self.assertFalse(local["drafts"][0]["model_attempted"])

    def test_new_offer_during_model_run_cannot_publish_stale_draft(self):
        store = self.create_case()

        def changed(plan, graph):
            store.append_offer("R1", self.offer(2))
            return self.writer(plan, graph)

        stale = prepare_negotiations(
            queue_path=self.queue,
            output_dir=self.output,
            now=AFTER,
            use_model=True,
            graph_loader=self.load_graph,
            writer=changed,
            market_reader=self.market_reader,
        )
        self.assertEqual(stale["draft_count"], 0)
        self.assertEqual(stale["drafts"][0]["status"], "stale_source_or_generation")
        current = self.prepare(use_model=True)
        self.assertEqual(self.metadata()["offer_revision"], 3)
        self.assertTrue(current["drafts"][0]["model_text_verified"])

    def test_completed_provider_with_failed_artifact_write_is_not_automatically_recharged(self):
        self.create_case()
        with patch("negotiation_worker._write_artifacts", side_effect=OSError("synthetic disk failure")):
            first = self.prepare(use_model=True)
        self.assertEqual(first["draft_count"], 0)
        self.assertTrue(first["drafts"][0]["model_attempted"])
        self.assertTrue(first["drafts"][0]["model_completed"])
        self.assertFalse(first["drafts"][0]["model_text_verified"])
        for refresh in (False, True):
            second = self.prepare(use_model=True, refresh=refresh)
            self.assertEqual(second["drafts"][0]["status"], "completed_result_unpublished")
            self.assertTrue(second["drafts"][0]["model_completed"])
        self.assertEqual(len(self.writer_calls), 1)

    def test_old_policy_producer_never_replaces_newer_published_generation(self):
        self.create_case()
        self.agent_settings(system_message="old-policy")
        entered, release = Event(), Event()

        def old_writer(plan, graph):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return self.writer(plan, graph)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                prepare_negotiations,
                queue_path=self.queue,
                output_dir=self.output,
                now=AFTER,
                use_model=True,
                graph_loader=self.load_graph,
                writer=old_writer,
                market_reader=self.market_reader,
            )
            self.assertTrue(entered.wait(timeout=5))
            self.agent_settings(system_message="new-policy")
            current = self.prepare(use_model=True)
            path = Path(current["drafts"][0]["path"])
            original = path.read_bytes()
            fingerprint = self.metadata()["input_fingerprint"]
            release.set()
            stale = first.result(timeout=5)
        self.assertEqual(stale["drafts"][0]["status"], "stale_source_or_generation")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.metadata()["input_fingerprint"], fingerprint)

    def test_explicit_success_refresh_retains_previous_immutable_artifacts(self):
        self.create_case()
        first = self.prepare(use_model=True)["drafts"][0]
        before = {key: Path(first[key]).read_bytes() for key in ("path", "html_path", "metadata_path", "manifest_path")}
        second = self.prepare(use_model=True, refresh=True)["drafts"][0]
        self.assertNotEqual(first["path"], second["path"])
        for key, original in before.items():
            self.assertEqual(Path(first[key]).read_bytes(), original)
        self.assertEqual(len(self.writer_calls), 2)

    def test_cli_returns_failure_for_error_and_model_fallback_without_leaking_exception(self):
        for value in (
            {"errors": ["model_result_unverified_no_automatic_retry"], "status": "negotiation_attention_required"},
            ValueError("synthetic-secret-do-not-print"),
        ):
            with self.subTest(value=type(value).__name__), contextlib.redirect_stdout(io.StringIO()) as output:
                settings = {"side_effect": value} if isinstance(value, Exception) else {"return_value": value}
                with patch("negotiation_worker.prepare_negotiations", **settings):
                    code = main(["--run-model"])
                self.assertEqual(code, 2)
                self.assertNotIn("synthetic-secret-do-not-print", output.getvalue())
        with (
            patch("negotiation_worker.prepare_negotiations", return_value={"status": "no_requests"}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main([]), 0)


if __name__ == "__main__":
    unittest.main()
