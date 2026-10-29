"""Exercise the shipped graph and bound components without a Langflow server."""

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from lfx.custom.eval import eval_custom_component_code
from lfx.graph.graph.base import Graph
from transfer.target_setup import prepare_flow

ROOT = Path(__file__).resolve().parent
FLOW = ROOT / "transfer" / "flow.clean.json"
if not FLOW.is_file():
    FLOW = ROOT.parent / "flow.json"


class HandoffFlowTests(unittest.TestCase):
    def test_shipped_clean_graph_builds_without_server_or_provider(self):
        flow = json.loads(FLOW.read_text())
        with patch("requests.sessions.Session.request") as network:
            graph = Graph.from_payload(flow["data"])
        self.assertEqual(len(graph.vertices), 21)
        self.assertEqual(len(graph.edges), 28)
        network.assert_not_called()

    def test_bound_native_deadline_and_rfq_use_external_state_from_another_cwd(self):
        with tempfile.TemporaryDirectory() as folder:
            temporary = Path(folder).resolve()
            state = temporary / "state"
            config = temporary / "config.json"
            prepared = temporary / "flow.target.json"
            config.write_text(json.dumps({"schema_version": 1, "state_dir": str(state), "langflow": {
                "base_url": "http://127.0.0.1:7860", "flow_id": None,
                "api_key_env": "SATINALMA_LANGFLOW_API_KEY", "database_path": None}}))
            report = prepare_flow(config, FLOW, prepared)
            self.assertEqual(report["total_path_bindings"], 6)
            nodes = {node["id"]: node for node in json.loads(prepared.read_text())["data"]["nodes"]}
            classes = {}
            for key in ("ProcurementRequestCoordinator-Sure", "ProcurementResearch-PiyasaTakibi", "SupplierRFQTracking-TeklifIstegi"):
                classes[key] = eval_custom_component_code(nodes[key]["data"]["node"]["template"]["code"]["value"])
            self.assertFalse(state.exists())
            coordinate = classes["ProcurementRequestCoordinator-Sure"]._prepare.__globals__["coordinate_request"]
            prepare_rfq = classes["SupplierRFQTracking-TeklifIstegi"]._prepare.__globals__["prepare_rfq_tracking"]
            context = {"source": "sap_api", "data_kind": "purchase_requisitions", "status": "ready", "errors": [],
                "materials": [{"material_code": "SYNTHETIC-01", "name": "Sentetik sarf", "quantity": "10", "unit": "KG"}],
                "request": {"id": "SAPPR-TARGET-SYNTHETIC", "requester": "owner@example.invalid", "deadline_at": None,
                    "sap_requisition": {"source_system": "SYNTHETIC", "purchase_requisition": "0000123456", "purchase_requisition_item": "00010"}}}
            old_cwd = Path.cwd()
            try:
                os.chdir(temporary)
                with patch("requests.sessions.Session.request") as network:
                    pending = coordinate(copy.deepcopy(context))
                    self.assertFalse(pending["ready"])
                    self.assertEqual(pending["case"]["request"]["state"], "awaiting_deadline")
                    context["request"]["deadline_at"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
                    collecting = coordinate(copy.deepcopy(context))
                    self.assertTrue(collecting["ready"])
                    self.assertEqual(collecting["case"]["request"]["state"], "collecting")
                    result = prepare_rfq(collecting["case"], [{"supplier_id": "S1", "name": "Sentetik Firma",
                        "email": "sales@example.invalid", "kind": "existing", "contact_confirmed": True, "source_url": ""}],
                        queue_path=state / "cases" / "requests.db", output_dir=state / "cases" / "drafts")
                network.assert_not_called()
            finally:
                os.chdir(old_cwd)
            self.assertEqual(result["status"], "ready", result)
            self.assertEqual(len(result["drafts"]), 1)
            self.assertTrue((state / "cases" / "requests.db").is_file())
            self.assertTrue(Path(result["drafts"][0]["metadata_path"]).is_relative_to(state))


if __name__ == "__main__":
    unittest.main()
