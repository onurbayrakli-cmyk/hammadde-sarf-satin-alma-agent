"""Bounded local JSON and CLI behavior; never reads live cases or calls models."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rfq_worker import main, read_json_file


class RFQWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.queue = self.folder / "missing" / "requests.db"

    def tearDown(self):
        self.temporary.cleanup()

    def test_status_missing_queue_is_quiet_metadata_without_creating(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--queue", str(self.queue), "status"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["queue_status"], "not_created")
        self.assertFalse(self.queue.parent.exists())

    def test_json_bad_encoding_and_oversized_file_rejected(self):
        for content in (b"not JSON", b"\xff", b"[" + b" " * 500_001 + b"]", b'{"a":NaN}',
                        b'{"request_id":"R1","request_id":"R2"}', b"[" * 5000 + b"]" * 5000):
            path = self.folder / "bad.json"
            path.write_bytes(content)
            with self.subTest(length=len(content)), self.assertRaises(ValueError):
                read_json_file(path)

    def test_json_file_contents_are_data_not_shell_code(self):
        path = self.folder / "suppliers.json"
        value = [{"name": "$(touch /tmp/not-a-command) `echo hi`\nveri"}]
        path.write_text(json.dumps(value))
        self.assertEqual(read_json_file(path), value)

    def test_prepare_passes_registry_and_literal_request_id(self):
        path = self.folder / "suppliers.json"
        path.write_text("[]")
        with patch("rfq_worker.RFQStore") as mock, contextlib.redirect_stdout(io.StringIO()):
            mock.return_value.prepare.return_value = {"status": "blocked", "emails_sent": False}
            result = main(["--queue", str(self.queue), "prepare", "--request-id", "R;$(echo never)", "--suppliers", str(path)])
        self.assertEqual(result, 0)
        mock.return_value.prepare.assert_called_once_with("R;$(echo never)", [])

    def test_input_errors_return_nonzero_and_do_not_echo_file_body(self):
        path = self.folder / "invalid.json"
        path.write_text("sensitive_source_body_not_json")
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            result = main(["--queue", str(self.queue), "record-dispatch", "--rfq-id", "RFQ1", "--file", str(path)])
        self.assertEqual(result, 2)
        self.assertNotIn("sensitive_source_body", error.getvalue())
        self.assertFalse(self.queue.exists())

    def test_ingest_and_dispatch_use_separate_commands(self):
        path = self.folder / "evidence.json"
        path.write_text('{"caller_supplied":true}')
        with patch("rfq_worker.RFQStore") as mock, contextlib.redirect_stdout(io.StringIO()):
            mock.return_value.record_dispatch.return_value = {"network_send_verified": False}
            mock.return_value.ingest_response.return_value = {"network_send_verified": False}
            self.assertEqual(main(["record-dispatch", "--rfq-id", "RFQ1", "--file", str(path)]), 0)
            self.assertEqual(main(["ingest-response", "--rfq-id", "RFQ1", "--file", str(path)]), 0)
        mock.return_value.record_dispatch.assert_called_once_with("RFQ1", {"caller_supplied": True})
        mock.return_value.ingest_response.assert_called_once_with("RFQ1", {"caller_supplied": True})


if __name__ == "__main__":
    unittest.main()
