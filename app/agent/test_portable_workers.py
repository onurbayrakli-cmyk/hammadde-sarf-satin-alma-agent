"""Target mode must not silently reuse development authentication or data."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import case_worker
import monitor_procurement
from runtime_config import get_runtime


class PortableWorkersTests(unittest.TestCase):
    def setUp(self):
        self.settings = replace(get_runtime(), configured=True, flow_id="11111111-1111-4111-8111-111111111111",
                                langflow_url="https://langflow.example.invalid", api_key_env="TARGET_TEST_KEY")

    def test_target_graph_api_failure_never_reads_development_database(self):
        with patch.object(monitor_procurement, "RUNTIME", self.settings), \
                patch("langflow_client.fetch_flow", side_effect=RuntimeError("unavailable")), \
                patch.object(monitor_procurement.sqlite3, "connect") as database:
            with self.assertRaises(RuntimeError):
                monitor_procurement.current_graph()
        database.assert_not_called()

    def test_target_server_failure_never_starts_development_checkout(self):
        with patch.object(monitor_procurement, "RUNTIME", self.settings), \
                patch.object(monitor_procurement.requests, "get", side_effect=monitor_procurement.requests.ConnectionError), \
                patch.object(monitor_procurement.subprocess, "Popen") as start:
            with self.assertRaises(RuntimeError):
                monitor_procurement.ensure_server()
        start.assert_not_called()

    def test_status_does_not_claim_sap_or_mail_connectivity(self):
        flow = {"data": {"nodes": [
            {"data": {"type": "MailResearchInput", "node": {"template": {"bot_mailbox": {"value": "bot@example.invalid"}}}}},
            {"data": {"type": "SAPProcurementInput", "node": {"template": {"endpoint": {"value": "https://sap.example.invalid"},
                "enable_api": {"value": True}}}}},
        ]}}
        with tempfile.TemporaryDirectory() as folder, patch.object(case_worker, "RUNTIME", self.settings), \
                patch("langflow_client.fetch_flow", return_value=copy.deepcopy(flow)), \
                patch.object(case_worker.sqlite3, "connect") as database:
            result = case_worker.status(queue_path=Path(folder) / "not-created.db")
            self.assertFalse((Path(folder) / "not-created.db").exists())
        self.assertTrue(result["flow_found"])
        self.assertTrue(result["performed_api_call"])
        self.assertFalse(result["sap_connectivity_verified"])
        self.assertFalse(result["mailbox_connectivity_verified"])
        database.assert_not_called()

    def test_missing_target_credentials_stay_unverified_without_fallback(self):
        with patch.object(case_worker, "RUNTIME", self.settings), \
                patch("langflow_client.fetch_flow", side_effect=ValueError("unavailable")), \
                patch.object(case_worker.sqlite3, "connect") as database:
            result = case_worker.read_connection_status()
        self.assertEqual(result["configuration_status"], "target_flow_unavailable")
        self.assertIsNone(result["performed_api_call"])
        self.assertFalse(result["flow_found"])
        database.assert_not_called()

    def test_all_worker_defaults_use_target_state_in_fresh_process(self):
        with tempfile.TemporaryDirectory() as folder:
            state = (Path(folder) / "external-state").resolve()
            config = Path(folder) / "config.json"
            config.write_text(json.dumps({"schema_version": 1, "state_dir": str(state), "langflow": {
                "base_url": "http://127.0.0.1:17860", "flow_id": self.settings.flow_id,
                "api_key_env": "TARGET_TEST_KEY", "database_path": None}}))
            script = """
import inspect,json
import case_worker,monitor_procurement,negotiation_worker,rfq_worker
values=[case_worker.DEFAULT_QUEUE,rfq_worker.DEFAULT_PATH,
 inspect.signature(monitor_procurement.run_request_research).parameters['queue_path'].default,
 inspect.signature(monitor_procurement.confirmed_request_materials).parameters['queue'].default,
 inspect.signature(negotiation_worker.prepare_negotiations).parameters['queue_path'].default]
print(json.dumps([str(x) for x in values]))
"""
            env = {key: value for key, value in os.environ.items() if key not in {"SATINALMA_CONFIG", "SATINALMA_STATE_DIR", "TARGET_TEST_KEY"}}
            env["SATINALMA_CONFIG"] = str(config)
            result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parent,
                                    env=env, capture_output=True, text=True, timeout=30, check=True)
            self.assertEqual(json.loads(result.stdout), [str(state / "cases" / "requests.db")] * 5)
            self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
