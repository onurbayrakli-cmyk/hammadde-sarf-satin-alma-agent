"""Request research must stop at the owner's strict deadline, without network."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from procurement_research import (
    GATE_MARKER,
    INTAKE_MARKER,
    RESEARCH_MARKER,
    ProcurementResearchComponent,
    build_research,
    incoming_material_profile,
)

GATE_NOW = datetime(2030, 1, 2, 10, 0, tzinfo=timezone.utc)
UNSET_DEADLINE = object()


def source_fixture(*, deadline=UNSET_DEADLINE, state="collecting", status="ready", **request_fields):
    request = {"id": "GATE-ONLY", "deadline_at": (GATE_NOW + timedelta(hours=1)).isoformat() if deadline is UNSET_DEADLINE else deadline,
               "state": state, **request_fields}
    context = {"status": status, "request": request,
               "materials": [{"material_code": "GATE-MAT", "name": "Sentetik kesim kontrolü", "quantity": "PRIVATE-QUANTITY"}]}
    return INTAKE_MARKER + json.dumps(context)


class RequestResearchGateTests(unittest.TestCase):
    def result(self, source, now=GATE_NOW):
        calls = []
        def offline_search(mode, query):
            calls.append(mode)
            return []
        result = build_research(source, {"materials": [{"material_code": "GATE-MAT", "name": "Sentetik kesim kontrolü"}]},
                                now=now, search=offline_search)
        return result, calls

    def test_expired_final_case_never_starts_web_or_news(self):
        result, calls = self.result(source_fixture(deadline=(GATE_NOW - timedelta(days=1)).isoformat(), state="ready_for_report"))
        self.assertEqual(calls, [])
        self.assertEqual(result["status"], "deadline_elapsed")
        self.assertIs(result["performed_search"], False)

    def test_exact_deadline_is_closed_but_microsecond_before_is_open(self):
        source = source_fixture(deadline=GATE_NOW.isoformat())
        before, calls_before = self.result(source, GATE_NOW - timedelta(microseconds=1))
        at, calls_at = self.result(source)
        after, calls_after = self.result(source, GATE_NOW + timedelta(microseconds=1))
        self.assertEqual(calls_before, ["Web", "News"])
        self.assertTrue(before["performed_search"])
        self.assertEqual(calls_at, [])
        self.assertEqual(calls_after, [])
        self.assertEqual(at["status"], "deadline_elapsed")
        self.assertEqual(after["status"], "deadline_elapsed")

    def test_missing_naive_malformed_or_non_string_deadline_never_searches(self):
        for deadline in (None, "", "2030-01-03T12:00:00", "2030-01-03", "not a date", "2030-13-50T12:00:00+03:00", 123, True):
            with self.subTest(deadline=deadline):
                result, calls = self.result(source_fixture(deadline=deadline))
                self.assertEqual(calls, [])
                self.assertEqual(result["status"], "awaiting_deadline")
                self.assertIs(result["performed_search"], False)

    def test_pending_or_explicit_unconfirmed_owner_never_searches(self):
        for source in (source_fixture(state="awaiting_deadline"), source_fixture(status="invalid"),
                       source_fixture(owner_confirmed=False), source_fixture(deadline_confirmed=False)):
            with self.subTest(source=source):
                result, calls = self.result(source)
                self.assertEqual(calls, [])
                self.assertEqual(result["status"], "awaiting_deadline")

    def test_final_states_do_not_reopen_research_even_with_future_date(self):
        for state in ("ready_for_report", "report_prepared", "completed", "final"):
            with self.subTest(state=state):
                result, calls = self.result(source_fixture(state=state))
                self.assertEqual(calls, [])
                self.assertEqual(result["status"], "deadline_elapsed")

    def test_invalid_collection_state_cannot_start_search(self):
        for state in (None, [], {}, "research_ready", "awaiting_configuration"):
            with self.subTest(state=state):
                result, calls = self.result(source_fixture(state=state))
                self.assertEqual(calls, [])
                self.assertEqual(result["status"], "awaiting_deadline")

    def test_generic_weekly_monitor_without_envelope_keeps_search_behavior(self):
        result, calls = self.result("General selected material monitor")
        self.assertEqual(calls, ["Web", "News"])
        self.assertEqual(result["status"], "partial")

    def test_malformed_incoming_context_is_not_downgraded_to_generic_monitor(self):
        result, calls = self.result(INTAKE_MARKER + "{broken")
        self.assertEqual(calls, [])
        self.assertEqual(result["status"], "invalid_request_context")
        self.assertFalse(result["performed_search"])

    def test_disabled_material_auto_selection_cannot_bypass_expired_gate(self):
        component = ProcurementResearchComponent()
        source = source_fixture(deadline="2020-01-01T10:00:00+00:00", state="ready_for_report")
        component.set(source_data=source, use_incoming_materials=False,
                      research_profile=json.dumps({"materials": [{"name": "Selected generic name"}]}))
        with patch("procurement_research._run_search", side_effect=AssertionError("No network may start")) as search:
            text = component.build_context().text
            self.assertEqual(component.build_analysis_context().text, "")
        search.assert_not_called()
        self.assertTrue(text.startswith(GATE_MARKER))
        self.assertTrue(text.split("\n\n", 1)[1].startswith(source))
        self.assertEqual(json.loads(text.split(RESEARCH_MARKER)[1])["status"], "deadline_elapsed")

    def test_expired_context_profile_scope_preserved_without_fake_results(self):
        result, calls = self.result(source_fixture(deadline=GATE_NOW.isoformat()))
        self.assertEqual(calls, [])
        self.assertEqual(result["profile"]["materials"][0]["material_code"], "GATE-MAT")
        self.assertEqual(result["materials"], [])
        self.assertNotIn("PRIVATE-QUANTITY", json.dumps(result))

    def test_closed_run_does_not_overwrite_existing_monitor_evidence(self):
        component = ProcurementResearchComponent()
        component.set(source_data=source_fixture(deadline="2020-01-01T10:00:00+00:00", state="ready_for_report"),
                      save_monitor_result=True, research_profile=json.dumps({"materials": [{"name": "Existing"}]}))
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / ".codex/agents/satin-alma-stok/monitor"
            directory.mkdir(parents=True)
            evidence = directory / "current_research.json"
            evidence.write_text('{"status":"completed","existing_evidence":true}')
            before = evidence.read_bytes()
            with patch("procurement_research.Path.cwd", return_value=Path(folder)), patch("procurement_research._run_search") as search:
                component.build_context()
            search.assert_not_called()
            self.assertEqual(evidence.read_bytes(), before)

    def test_three_component_outputs_share_one_search_run(self):
        source = source_fixture(deadline=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        component = ProcurementResearchComponent()
        component.set(source_data=source, save_monitor_result=False)
        with patch("procurement_research._run_search", return_value=[]) as search:
            metadata = json.loads(component.build_gate_status().text[len(GATE_MARKER):])
            first = component.build_context().text
            second = component.build_analysis_context().text
        self.assertEqual(first, second)
        self.assertEqual(search.call_count, 2)  # One Web and one News callback.
        self.assertEqual(metadata, {"status": "partial", "performed_search": True, "search_attempted": True,
                                    "deadline_at": json.loads(source[len(INTAKE_MARKER):])["request"]["deadline_at"]})

    def test_long_closed_profile_has_short_parseable_machine_status_without_new_run(self):
        source = source_fixture(deadline="2020-01-01T00:00:00+00:00", state="ready_for_report")
        component = ProcurementResearchComponent()
        component.set(source_data=source, use_incoming_materials=False,
                      research_profile=json.dumps({"materials": [{"material_code": "GATE-MAT", "name": "Sentetik kesim kontrolü",
                          "existing_suppliers": [f"Sentetik firma {index} " + "A" * 170 for index in range(50)]}]}))
        with patch("procurement_research._run_search", side_effect=AssertionError("No network may start")) as search:
            gate_text = component.build_gate_status().text
            context = component.build_context().text
            self.assertEqual(component.build_analysis_context().text, "")
            self.assertEqual(component.build_gate_status().text, gate_text)
        search.assert_not_called()
        self.assertGreater(len(context), 6000)
        self.assertLess(len(gate_text), 1000)
        self.assertTrue(gate_text.startswith(GATE_MARKER))
        metadata = json.loads(gate_text[len(GATE_MARKER):])
        self.assertEqual(metadata, {"status": "deadline_elapsed", "performed_search": False, "search_attempted": False,
                                    "deadline_at": "2020-01-01T00:00:00+00:00"})
        # The port may be unconnected in a full graph. The same short JSON is
        # therefore also first in context, before its long source/profile body.
        prefix = context[:6000].split("\n\n", 1)[0]
        self.assertEqual(prefix, gate_text)
        self.assertEqual(json.loads(prefix[len(GATE_MARKER):]), metadata)
        for private_field in ("profile", "source", "warnings", "request_id", "GATE-ONLY", "PRIVATE-QUANTITY", "Sentetik firma"):
            self.assertNotIn(private_field, gate_text)

    def test_ambiguous_callback_failure_does_not_claim_no_call(self):
        def failed(mode, query):
            raise TimeoutError("Remote outcome unknown")
        result = build_research(source_fixture(), {"materials": [{"name": "Sentetik kesim kontrolü"}]},
                                now=GATE_NOW, search=failed)
        self.assertTrue(result["search_attempted"])
        self.assertIsNone(result["performed_search"])
        self.assertEqual(result["status"], "partial")

    def test_incoming_profile_uses_explicit_reference_time(self):
        source = source_fixture()
        profile = incoming_material_profile(source, {}, now=GATE_NOW)
        self.assertEqual(profile["materials"][0]["material_code"], "GATE-MAT")
        with self.assertRaises(ValueError):
            incoming_material_profile(source, {}, now=GATE_NOW + timedelta(days=1))

    def test_callback_returning_after_cutoff_is_not_published_as_completed(self):
        import procurement_research
        clock = {"at": GATE_NOW}
        class Clock:
            @classmethod
            def now(cls, tz=None):
                return clock["at"]
        calls = []
        def offline_search(mode, query):
            calls.append(mode)
            clock["at"] = GATE_NOW + timedelta(seconds=2)
            return [{"title": "After cutoff", "link": "https://supplier.example.org"}]
        with patch.object(procurement_research, "datetime", Clock):
            result = build_research(source_fixture(deadline=(GATE_NOW + timedelta(seconds=1)).isoformat()),
                                    {"materials": [{"name": "Sentetik kesim kontrolü"}]}, search=offline_search)
        self.assertEqual(calls, ["Web"])
        self.assertEqual(result["status"], "deadline_elapsed")
        self.assertTrue(result["performed_search"])
        self.assertEqual(result["materials"], [])


if __name__ == "__main__":
    unittest.main()
