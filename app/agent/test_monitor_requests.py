"""Request-specific monitor guards, scope, cache and linkage without network."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from case_store import CaseStore
from case_worker import prepare_due
from monitor_procurement import (
    GATE_MARKER,
    NODE_ID,
    RESEARCH_MARKER,
    case_research_path,
    main,
    resolve_request_selection,
    run_request_research,
    run_research,
)

BEFORE = "2026-10-13T08:00:00+03:00"
DEADLINE = "2026-10-13T09:00:00+03:00"
AFTER = "2026-10-13T09:01:00+03:00"


def request():
    return {"id": "R1", "material_code": "PE", "material_name": "Polietilen", "quantity": "999",
            "unit": "KG", "requester": "owner@example.com", "mailbox": "bot@example.com", "message_id": "mail-R1",
            "required_specs": {"internal": "PRIVATE_INTERNAL_SENTINEL"}}


def graph():
    raw = {"region": "Türkiye", "lookback_days": 30, "materials": [{"material_code": "PE", "name": "Polietilen",
           "existing_suppliers": ["Mevcut Firma"], "excluded_domains": ["existing.example.com"], "search_terms": ["polyethylene resin"]}]}
    return {"nodes": [{"id": NODE_ID, "data": {"type": "ProcurementResearch", "node": {"template": {
        "research_profile": {"value": json.dumps(raw)}, "material_selection": {"value": []},
        "excluded_suppliers": {"value": ["Hariç Firma"]}, "material_catalog": {"value": ""}, "custom_materials": {"value": ""},
        "allow_demo": {"value": False}, "use_incoming_materials": {"value": True},
        "anthropic_api_key": {"value": "ANTHROPIC_API_KEY", "load_from_db": True},
        "save_monitor_result": {"value": False}, "source_data": {"value": ""},
    }}}}], "edges": []}


def research(profile):
    return {"as_of": BEFORE, "status": "completed", "performed_search": True, "search_attempted": True,
        "profile": profile, "materials": [{"material_code": "PE", "name": "Polietilen",
        "suppliers": [{"evidence_id": "S1", "name": "Public Firma", "title": "PE adayı", "url": "https://supplier.example.com/pe",
                       "retrieved_at": BEFORE, "verification": "search_candidate", "existing_supplier_match": False}], "news": []}], "warnings": []}


class RequestMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.queue = self.folder / "requests.db"
        self.monitor = self.folder / "monitor"
        self.calls = 0

    def tearDown(self):
        self.temporary.cleanup()

    def store(self, deadline=DEADLINE):
        store = CaseStore(self.queue)
        store.upsert_request(request(), "mail_request")
        if deadline:
            store.set_deadline("R1", deadline, "owner@example.com", BEFORE)
        return store

    def runner(self, node, source):
        self.calls += 1
        profile = json.loads(node["data"]["node"]["template"]["research_profile"]["value"])
        return research(profile), "synthetic-job"

    def run_monitor(self, **kwargs):
        defaults = {"run": True, "queue_path": self.queue, "monitor_dir": self.monitor,
                    "now": BEFORE, "graph_loader": graph, "runner": self.runner}
        defaults.update(kwargs)
        with patch("requests.Session", side_effect=AssertionError("Network is disabled in request tests")):
            return run_request_research("R1", **defaults)

    def test_absent_queue_and_unknown_request_never_create_or_search(self):
        self.assertEqual(self.run_monitor()["status"], "request_queue_unavailable")
        self.assertFalse(self.queue.exists())
        self.assertFalse(self.monitor.exists())
        CaseStore(self.queue)
        self.assertEqual(self.run_monitor()["status"], "request_unavailable")
        self.assertEqual(self.calls, 0)

    def test_owner_deadline_confirmation_required_before_search(self):
        self.store(None)
        result = self.run_monitor()
        self.assertEqual(result["status"], "awaiting_deadline")
        self.assertEqual(self.calls, 0)
        self.assertFalse(self.monitor.exists())

    def test_elapsed_request_never_starts_paid_search(self):
        self.store()
        for now in (DEADLINE, AFTER):
            result = self.run_monitor(now=now)
            self.assertEqual(result["status"], "deadline_elapsed")
        self.assertEqual(self.calls, 0)

    def test_status_without_run_only_shows_ready_and_does_not_make_files(self):
        self.store()
        result = self.run_monitor(run=False)
        self.assertEqual(result["status"], "request_research_ready")
        self.assertFalse(result["performed_search"])
        self.assertEqual(self.calls, 0)
        self.assertFalse(self.monitor.exists())

    def test_request_node_preserves_exclusions_without_private_source_or_graph_mutation(self):
        store = self.store()
        original = graph()
        previous = json.dumps(original, sort_keys=True)
        node, source, profile = resolve_request_selection(original, store.snapshot("R1", BEFORE)["request"])
        material = profile["materials"][0]
        self.assertEqual(material["existing_suppliers"], ["Mevcut Firma", "Hariç Firma"])
        self.assertEqual(material["excluded_domains"], ["existing.example.com"])
        self.assertEqual(material["search_terms"], ["polyethylene resin"])
        self.assertEqual(node["data"]["node"]["template"]["material_selection"]["value"], [])
        self.assertFalse(node["data"]["node"]["template"]["use_incoming_materials"]["value"])
        self.assertNotIn("999", source)
        self.assertNotIn("PRIVATE_INTERNAL_SENTINEL", source)
        self.assertNotIn("owner@example.com", source)
        self.assertIn("TALEP GİRİŞİ (JSON)", source)
        self.assertIn('"state": "collecting"', source)
        self.assertIn(store.snapshot("R1", BEFORE)["request"]["deadline_at"], source)
        self.assertEqual(json.dumps(original, sort_keys=True), previous)

    def test_native_runner_result_saved_once_per_case_and_repeat_uses_cache(self):
        self.store()
        result = self.run_monitor()
        self.assertEqual(result["status"], "request_research_saved")
        self.assertTrue(result["performed_search"])
        self.assertEqual(self.calls, 1)
        saved = json.loads(Path(result["research_path"]).read_text())
        self.assertEqual(saved["request_id"], "R1")
        self.assertEqual(saved["deadline_at"], "2026-10-13T06:00:00+00:00")
        self.assertNotIn("PRIVATE_INTERNAL_SENTINEL", json.dumps(saved))
        repeated = self.run_monitor()
        self.assertEqual(repeated["status"], "request_research_cached")
        self.assertFalse(repeated["performed_search"])
        self.assertEqual(self.calls, 1)

    def test_partial_empty_cache_does_not_repeat_automatic_paid_attempt(self):
        self.store()

        def empty(node, source):
            self.calls += 1
            raw, job = self.runner_without_count(node, source)
            raw["status"] = "partial"
            raw["materials"][0]["suppliers"] = []
            return raw, job

        self.run_monitor(runner=empty)
        cached = self.run_monitor()
        self.assertEqual(cached["research_status"], "partial")
        self.assertFalse(cached["performed_search"])
        self.assertEqual(self.calls, 1)

    def runner_without_count(self, node, source):
        return research(json.loads(node["data"]["node"]["template"]["research_profile"]["value"])), "synthetic-job"

    def test_rescheduled_deadline_invalidates_old_request_cache(self):
        store = self.store()
        self.run_monitor()
        store.set_deadline("R1", "2026-10-13T10:00:00+03:00", "owner@example.com", BEFORE)
        changed = self.run_monitor()
        self.assertEqual(changed["status"], "request_research_saved")
        self.assertEqual(self.calls, 2)

    def test_forged_other_material_cache_is_not_reused(self):
        self.store()
        first = self.run_monitor()
        path = Path(first["research_path"])
        cached = json.loads(path.read_text())
        cached["request_id"] = "R2"
        path.write_text(json.dumps(cached))
        result = self.run_monitor()
        self.assertEqual(result["status"], "request_research_saved")
        self.assertEqual(self.calls, 2)

    def test_manager_report_prefers_per_case_record_over_unrelated_global_research(self):
        self.store()
        result = self.run_monitor()
        global_path = self.monitor / "current_research.json"
        global_path.write_text(json.dumps({"materials": [{"material_code": "PP", "name": "PP_OTHER_SENTINEL"}]}))
        final = prepare_due(self.queue, self.folder / "reports", AFTER, research_path=global_path)
        report = final["reports"][0]
        self.assertEqual(report["research_source_count"], 1)
        text = Path(report["path"]).read_text()
        self.assertIn("Public Firma", text)
        self.assertNotIn("PP_OTHER_SENTINEL", text)
        self.assertTrue(Path(result["research_path"]).is_file())

    def test_canonical_cache_drops_unused_api_and_mail_fields(self):
        self.store()

        def runner(node, source):
            raw, job = self.runner_without_count(node, source)
            raw["api_key"] = "synthetic-secret-no-copy"
            raw["profile"]["api_key"] = "synthetic-secret-no-copy"
            raw["materials"][0]["mail_body"] = "PRIVATE_BODY_NO_COPY"
            raw["materials"][0]["suppliers"][0]["api_key"] = "synthetic-secret-no-copy"
            return raw, job

        result = self.run_monitor(runner=runner)
        saved = Path(result["research_path"]).read_text()
        self.assertNotIn("synthetic-secret-no-copy", saved)
        self.assertNotIn("PRIVATE_BODY_NO_COPY", saved)
        self.assertFalse(result["full_multi_model_run_verified"])

    def test_case_file_path_uses_hash_not_untrusted_request_id(self):
        path = case_research_path("../../escape", self.monitor)
        self.assertEqual(path.parent, self.monitor / "cases")
        self.assertNotIn("escape", path.name)

    def test_cli_runtime_failure_does_not_claim_provider_call_never_started(self):
        captured = io.StringIO()
        with patch("monitor_procurement.run_request_research", side_effect=RuntimeError("synthetic interrupted run")):
            with contextlib.redirect_stdout(captured):
                code = main(["--request-id", "R1", "--run"])
        result = json.loads(captured.getvalue())
        self.assertIsNone(result["performed_search"])
        self.assertTrue(result["attempted_search"])
        self.assertEqual(result["status"], "request_research_failed")
        self.assertNotEqual(code, 0)

    def test_runtime_deadline_gate_does_not_cache_or_claim_search(self):
        self.store()
        def blocked(node, source):
            return {"status": "deadline_elapsed", "performed_search": False}, "blocked-native-job"
        result = self.run_monitor(runner=blocked)
        self.assertEqual(result["status"], "deadline_elapsed")
        self.assertFalse(result["performed_search"])
        self.assertFalse(case_research_path("R1", self.monitor).exists())

    def test_unknown_provider_outcome_is_not_promoted_to_search_success_or_retried(self):
        self.store()
        def unknown(node, source):
            self.calls += 1
            raw, job = self.runner_without_count(node, source)
            raw.update(status="partial", performed_search=None, search_attempted=True)
            return raw, job
        result = self.run_monitor(runner=unknown)
        self.assertIsNone(result["performed_search"])
        self.assertTrue(result["attempted_search"])
        repeated = self.run_monitor(runner=unknown)
        self.assertFalse(repeated["performed_search"])
        self.assertTrue(repeated["prior_search_outcome_unknown"])
        self.assertEqual(self.calls, 1)
        captured = io.StringIO()
        with patch("monitor_procurement.run_request_research", return_value=repeated), contextlib.redirect_stdout(captured):
            self.assertNotEqual(main(["--request-id", "R1", "--run"]), 0)

    def test_short_native_gate_survives_truncated_context_without_overwriting_prior_research(self):
        self.monitor.mkdir()
        previous_path = self.monitor / "current_research.json"
        previous_path.write_text('{"old_public_research":"preserve"}')
        previous = previous_path.read_bytes()
        short_gate = {"status": "deadline_elapsed", "performed_search": False, "search_attempted": False}
        long_context = (GATE_MARKER + json.dumps(short_gate) + "\n\n" + RESEARCH_MARKER
                        + json.dumps({"status": "deadline_elapsed", "profile": {"names": "x" * 10000}}))[:6000]
        events = [
            {"event": "end_vertex", "data": {"build_data": {"id": NODE_ID, "valid": True, "data": {"outputs": {
                "context": {"message": long_context}, "gate_status": {"message": ""}
            }}}}},
            {"event": "end"},
        ]
        auth, build, event_reply = Mock(), Mock(), Mock()
        auth.json.return_value = {"access_token": "synthetic-test-reference"}
        build.json.return_value = {"job_id": "synthetic-blocked-job"}
        event_reply.text = "\n".join(json.dumps(event) for event in events)
        session = Mock()
        session.headers = {}
        session.get.side_effect, session.post.return_value = [auth, event_reply], build
        with patch("monitor_procurement.ensure_server"), patch("monitor_procurement.requests.Session", return_value=session), patch("monitor_procurement.MONITOR", self.monitor):
            result, job = run_research(graph()["nodes"][0], "synthetic local request")
        self.assertEqual(result, short_gate)
        self.assertEqual(job, "synthetic-blocked-job")
        self.assertEqual(previous_path.read_bytes(), previous)

    def test_explicit_missing_request_is_failure_but_waiting_or_empty_work_is_normal(self):
        for status, expected in (("request_queue_unavailable", 2), ("request_unavailable", 2),
                                 ("awaiting_deadline", 0), ("deadline_elapsed", 0),
                                 ("research_already_running", 0), ("request_research_ready", 0)):
            captured = io.StringIO()
            with self.subTest(status=status), patch("monitor_procurement.run_request_research", return_value={"status": status}), contextlib.redirect_stdout(captured):
                code = main(["--request-id", "R1"])
            self.assertEqual(code, expected)
            self.assertEqual(json.loads(captured.getvalue())["status"], status)

    def test_provider_failure_in_saved_or_cached_result_is_nonzero(self):
        for status in ("request_research_saved", "request_research_cached"):
            captured = io.StringIO()
            with self.subTest(status=status), patch("monitor_procurement.run_request_research", return_value={"status": status, "research_status": "error"}), contextlib.redirect_stdout(captured):
                code = main(["--request-id", "R1", "--run"])
            self.assertNotEqual(code, 0)

    def test_general_monitor_failure_has_structured_nonzero_result_without_exception_text(self):
        captured = io.StringIO()
        with patch("monitor_procurement.current_graph", side_effect=OSError("PRIVATE_ERROR_SENTINEL")), contextlib.redirect_stdout(captured):
            code = main(["--run"])
        result = json.loads(captured.getvalue())
        self.assertNotEqual(code, 0)
        self.assertEqual(result["status"], "monitor_failed")
        self.assertIsNone(result["performed_search"])
        self.assertNotIn("PRIVATE_ERROR_SENTINEL", captured.getvalue())

    def test_cli_process_exposes_failure_exit_code_to_scheduler(self):
        script = """import monitor_procurement as monitor
def interrupted(*args, **kwargs):
    raise RuntimeError('synthetic interrupted native build')
monitor.run_request_research = interrupted
raise SystemExit(monitor.main(['--request-id', 'R1', '--run']))
"""
        process = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parent,
                                 capture_output=True, text=True, timeout=30, check=False)
        self.assertNotEqual(process.returncode, 0)
        payload = json.loads(process.stdout)
        self.assertEqual(payload["status"], "request_research_failed")
        self.assertIsNone(payload["performed_search"])


if __name__ == "__main__":
    unittest.main()
