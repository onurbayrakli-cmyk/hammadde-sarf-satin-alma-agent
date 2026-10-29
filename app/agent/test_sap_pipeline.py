"""Synthetic SAP transport through source review, deadline and RFQ drafting."""

import json
import socket
import stat
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from case_store import CaseStore
from case_worker import ingest_request, prepare_questions
from rfq_store import RFQStore
from sap_connection import load_connection
from sap_metadata import MetadataError
from sap_worker import SapWorkerError, prepare_request
from test_rfq_store import suppliers
from test_sap_adapter import FakeResponse, connection_document
from test_sap_worker import ITEM, MAPPING, NOW, PR, metadata, response


class SAPPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name).resolve()
        self.source = self.folder / "source.json"
        self.queue = self.folder / "requests.db"
        self.output = self.folder / "drafts"
        config = connection_document()
        config["field_map"] = dict(MAPPING)
        self.config_path = self.folder / "connection.json"
        self.config_path.write_text(json.dumps(config))
        self.connection = load_connection(self.config_path)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def pool(body, content_type):
        result = Mock()
        result.urlopen.return_value = FakeResponse(body, content_type=content_type)
        return result

    def fake_transport(self, *pools):
        return patch("sap_adapter.urllib3.HTTPSConnectionPool", side_effect=pools)

    def fake_dns(self):
        return patch(
            "sap_adapter.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.42.0.7", 4300)),
            ],
        )

    def test_v2_v4_read_is_separate_from_manual_deadline_and_two_supplier_drafts(self):
        for v2 in (False, True):
            with self.subTest(v2=v2):
                self.source = self.folder / ("source-v2.json" if v2 else "source-v4.json")
                self.queue = self.folder / ("requests-v2.db" if v2 else "requests-v4.db")
                self.output = self.folder / ("drafts-v2" if v2 else "drafts-v4")
                schema = metadata(v2=v2)
                if v2:
                    schema = schema.replace(b'Version="4.0"', b'Version="1.0"')
                metadata_pool = self.pool(schema, "application/xml")
                source_payload = response(v2=v2)
                data_pool = self.pool(json.dumps(source_payload).encode(), "application/json")
                with (
                    self.fake_dns() as dns,
                    self.fake_transport(metadata_pool, data_pool) as transport,
                    patch(
                        "requests.sessions.Session.request", side_effect=AssertionError("No model or mail HTTP")
                    ) as network,
                    patch("httpx.Client.send", side_effect=AssertionError("No model HTTP")) as model_network,
                    patch("socket.create_connection", side_effect=AssertionError("No real connection")) as sockets,
                ):
                    prepared = prepare_request(self.connection, PR, ITEM, self.source, now=NOW)
                    self.assertEqual(dns.call_count, 2)
                    self.assertEqual(transport.call_count, 2)
                    self.assertFalse(self.queue.exists())
                    self.assertFalse(self.output.exists())
                    self.assertEqual(stat.S_IMODE(self.source.stat().st_mode), 0o600)
                    self.assertFalse(prepared["queue_written"])
                    self.assertFalse(prepared["sap_written"])
                    self.assertFalse(prepared["outbound_sent"])
                    envelope = json.loads(self.source.read_text())
                    request = envelope["request"]
                    self.assertEqual(
                        request["sap_requisition"],
                        {
                            "source_system": self.connection.source_system,
                            "purchase_requisition": PR,
                            "purchase_requisition_item": ITEM,
                        },
                    )
                    self.assertEqual((request["quantity"], request["unit"]), ("10.5", "KG"))
                    evidence = envelope["sap_read_evidence"]
                    row = source_payload["d"]["results"][0] if v2 else source_payload["value"][0]
                    self.assertEqual(evidence["source_record"], row)
                    self.assertFalse(evidence["business_semantics_verified"])

                    # This explicit local ingestion is a separate operator step.
                    cases = CaseStore(self.queue)
                    registered = ingest_request(envelope, cases, now=NOW)
                    request_id = registered["request_id"]
                    snapshot = cases.snapshot(request_id, now=NOW)
                    self.assertEqual(snapshot["request"]["state"], "awaiting_deadline")
                    self.assertEqual(snapshot["request"]["sap_requisition"], request["sap_requisition"])
                    self.assertEqual(snapshot["request"]["quantity"], "10.5")
                    questions = prepare_questions(self.queue, self.output / "questions")
                    self.assertEqual(questions["draft_count"], 1)
                    self.assertFalse(questions["outbound_sent"])
                    self.assertFalse(questions["performed_api_call"])
                    self.assertTrue(Path(questions["drafts"][0]["path"]).is_file())
                    deadline = (NOW + timedelta(hours=2)).isoformat()
                    with self.assertRaises(ValueError):
                        cases.set_deadline(request_id, deadline, "someone-else@example.invalid", now=NOW)
                    self.assertEqual(cases.snapshot(request_id, now=NOW)["request"]["state"], "awaiting_deadline")
                    cases.set_deadline(request_id, deadline, request["requester"], now=NOW)
                    self.assertEqual(cases.snapshot(request_id, now=NOW)["request"]["state"], "collecting")
                    firms = suppliers()
                    for firm in firms:
                        firm["material_codes"] = [request["material_code"]]
                    rfqs = RFQStore(self.queue, self.output / "rfqs").prepare(request_id, firms, now=NOW)
                    self.assertEqual(len(rfqs["drafts"]), 2)
                    self.assertFalse(rfqs["emails_sent"])
                    self.assertFalse(rfqs["providers_called"])
                    for draft in rfqs["drafts"]:
                        saved = json.loads(Path(draft["metadata_path"]).read_text())
                        self.assertEqual(saved["request_id"], request_id)
                        self.assertEqual(saved["material_code"], request["material_code"])
                        self.assertEqual((saved["quantity"], saved["unit"]), ("10.5", "KG"))
                        self.assertTrue(saved["draft_only"])
                        self.assertFalse(saved["sent"])
                    self.assertEqual(metadata_pool.urlopen.call_args.args[0], "GET")
                    self.assertEqual(data_pool.urlopen.call_args.args[0], "GET")
                    self.assertEqual(transport.call_count, 2)
                    network.assert_not_called()
                    model_network.assert_not_called()
                    sockets.assert_not_called()

    def test_non_requisition_service_stops_after_metadata_without_source_or_queue(self):
        metadata_pool = self.pool(metadata(entity_set="Materials"), "application/xml")
        with (
            self.fake_dns(),
            self.fake_transport(metadata_pool) as transport,
            patch("socket.create_connection", side_effect=AssertionError("No real connection")) as sockets,
        ):
            with self.assertRaises(MetadataError):
                prepare_request(self.connection, PR, ITEM, self.source, now=NOW)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(metadata_pool.urlopen.call_args.args[0], "GET")
        self.assertIn("$metadata", metadata_pool.urlopen.call_args.args[1])
        self.assertFalse(self.source.exists())
        self.assertFalse(self.queue.exists())
        self.assertFalse(self.output.exists())
        sockets.assert_not_called()

    def test_untrusted_pagination_is_not_followed_or_published(self):
        metadata_pool = self.pool(metadata(), "application/xml")
        payload = response()
        payload["@odata.nextLink"] = "https://unrelated.example.invalid/other"
        data_pool = self.pool(json.dumps(payload).encode(), "application/json")
        with (
            self.fake_dns(),
            self.fake_transport(metadata_pool, data_pool) as transport,
            patch("socket.create_connection", side_effect=AssertionError("No real connection")) as sockets,
        ):
            with self.assertRaises(SapWorkerError):
                prepare_request(self.connection, PR, ITEM, self.source, now=NOW)
        self.assertEqual(transport.call_count, 2)
        self.assertFalse(self.source.exists())
        self.assertFalse(self.queue.exists())
        self.assertFalse(self.output.exists())
        sockets.assert_not_called()


if __name__ == "__main__":
    unittest.main()
