"""Saved metadata review stays offline and produces only a private report."""

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sap_metadata import catalog_metadata
from sap_schema_review import FLAGS, MAX_METADATA_BYTES, SchemaReviewError, main, review_schema
from test_sap_worker import metadata


class OfflineSchemaReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name).resolve()
        self.source = self.folder / "saved-metadata.xml"
        self.output = self.folder / "schema-report.json"
        self.source.write_bytes(metadata())

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_review_frames_schema_as_offline_without_contract_or_connection_claim(self):
        marker = b"schema-comment-that-must-not-be-echoed"
        self.source.write_bytes(
            metadata().replace(b"<edmx:DataServices>", b"<!--" + marker + b"--><edmx:DataServices>")
        )
        result = review_schema(self.source, self.output)
        saved = json.loads(self.output.read_text())
        self.assertEqual(result, saved)
        self.assertEqual(saved["status"], "offline_schema_review")
        self.assertTrue(saved["report_created"])
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertTrue(saved["catalog"]["metadata_verified"])
        self.assertEqual(saved["catalog"]["entity_sets"][0]["name"], "PurchaseRequisitionItems")
        self.assertNotIn(marker.decode(), self.output.read_text())
        self.assertNotIn("contract_compatible", saved)
        for key, value in FLAGS.items():
            self.assertIs(saved[key], value)
        self.assertEqual(
            sorted(path.name for path in self.folder.iterdir()), ["saved-metadata.xml", "schema-report.json"]
        )

    def test_explicit_entity_selection_is_exact_and_missing_or_invalid_names_publish_nothing(self):
        original = b'<EntitySet Name="PurchaseRequisitionItems" EntityType="Pilot.Item"/>'
        two_sets = metadata().replace(original, original + b'<EntitySet Name="OtherItems" EntityType="Pilot.Item"/>')
        self.source.write_bytes(two_sets)
        result = review_schema(self.source, self.output, entity_set="OtherItems")
        self.assertEqual([item["name"] for item in result["catalog"]["entity_sets"]], ["OtherItems"])
        for name in ("AbsentItems", "otheritems", "OtherItems/../", "", 12):
            failed_output = self.folder / "no-report.json"
            with self.subTest(entity=name), self.assertRaises(SchemaReviewError):
                review_schema(self.source, failed_output, entity_set=name)
            self.assertFalse(failed_output.exists())

    def test_duplicate_entity_names_cannot_be_silently_selected(self):
        original = b'<EntitySet Name="PurchaseRequisitionItems" EntityType="Pilot.Item"/>'
        self.source.write_bytes(metadata().replace(original, original + original))
        with self.assertRaises(SchemaReviewError):
            review_schema(self.source, self.output, entity_set="PurchaseRequisitionItems")
        self.assertFalse(self.output.exists())

    def test_empty_catalog_stays_an_inventory_without_readiness_claim(self):
        declaration = b'<EntitySet Name="PurchaseRequisitionItems" EntityType="Pilot.Item"/>'
        self.source.write_bytes(metadata().replace(declaration, b""))
        result = review_schema(self.source, self.output)
        self.assertEqual(result["catalog"]["entity_sets"], [])
        self.assertFalse(result["contract_evaluated"])
        self.assertFalse(result["sap_connection_verified"])

    def test_malformed_truncated_dtd_wrong_encoding_and_oversized_sources_fail_without_report(self):
        dtd = b'<!DOCTYPE x [<!ENTITY probe SYSTEM "https://unrelated.example.invalid/private">]><x/>'
        sources = [
            b"",
            b"not XML",
            metadata()[:-10],
            dtd,
            dtd.decode().encode("utf-16"),
            b"x" * (MAX_METADATA_BYTES + 1),
        ]
        for raw in sources:
            self.source.write_bytes(raw)
            with self.subTest(size=len(raw)), self.assertRaises(SchemaReviewError):
                review_schema(self.source, self.output)
            self.assertFalse(self.output.exists())
            self.assertFalse((self.folder / "requests.db").exists())

    def test_input_must_be_absolute_regular_and_non_symlink(self):
        linked = self.folder / "linked.xml"
        linked.symlink_to(self.source)
        directory = self.folder / "a-directory"
        directory.mkdir()
        fifo = self.folder / "metadata-fifo"
        os.mkfifo(fifo)
        for source in (Path("relative.xml"), self.folder / "missing.xml", linked, directory, fifo):
            with self.subTest(source_type=source.name), self.assertRaises(SchemaReviewError):
                review_schema(source, self.output)
            self.assertFalse(self.output.exists())

    def test_input_parent_symlink_and_in_place_output_are_rejected(self):
        real = self.folder / "real-input"
        real.mkdir()
        (real / "metadata.xml").write_bytes(metadata())
        linked = self.folder / "linked-input"
        linked.symlink_to(real, target_is_directory=True)
        with self.assertRaises(SchemaReviewError):
            review_schema(linked / "metadata.xml", self.output)
        before = self.source.read_bytes()
        with self.assertRaises(SchemaReviewError):
            review_schema(self.source, self.source)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(self.output.exists())

    def test_existing_output_and_package_output_are_never_overwritten(self):
        self.output.write_text("previous report")
        with self.assertRaises(SchemaReviewError):
            review_schema(self.source, self.output)
        self.assertEqual(self.output.read_text(), "previous report")
        release = self.folder / "release"
        agent = release / "agent"
        agent.mkdir(parents=True)
        (release / "MANIFEST.json").write_text("{}")
        forbidden = release / "schema-report.json"
        with patch("sap_worker.ROOT", agent), self.assertRaises(SchemaReviewError):
            review_schema(self.source, forbidden)
        self.assertFalse(forbidden.exists())

    def test_input_directory_swap_is_rejected_after_anchored_read(self):
        source_parent = self.folder / "source-parent"
        source_parent.mkdir()
        self.source = source_parent / "metadata.xml"
        self.source.write_bytes(metadata())
        other = self.folder / "other-source"
        other.mkdir()
        (other / "metadata.xml").write_bytes(metadata(entity_set="OtherItems"))
        original_read = os.read
        swapped = [False]

        def read_and_swap(descriptor, size):
            raw = original_read(descriptor, size)
            if raw and not swapped[0]:
                swapped[0] = True
                source_parent.rename(self.folder / "source-parent-original")
                source_parent.symlink_to(other, target_is_directory=True)
            return raw

        with patch("sap_schema_review.os.read", side_effect=read_and_swap), self.assertRaises(SchemaReviewError):
            review_schema(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_input_changes_during_read_do_not_publish_a_mixed_snapshot(self):
        original_read = os.read
        changed = [False]

        def read_and_change(descriptor, size):
            raw = original_read(descriptor, size)
            if raw and not changed[0]:
                changed[0] = True
                self.source.write_bytes(metadata().replace(b"Pilot", b"Other"))
            return raw

        with patch("sap_schema_review.os.read", side_effect=read_and_change), self.assertRaises(SchemaReviewError):
            review_schema(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_output_directory_swap_does_not_publish_through_replacement(self):
        destination = self.folder / "output-parent"
        destination.mkdir()
        other = self.folder / "replacement-parent"
        other.mkdir()
        self.output = destination / "report.json"

        def catalog_then_swap(raw, *, max_bytes):
            result = catalog_metadata(raw, max_bytes=max_bytes)
            destination.rename(self.folder / "original-output-parent")
            destination.symlink_to(other, target_is_directory=True)
            return result

        with (
            patch("sap_schema_review.catalog_metadata", side_effect=catalog_then_swap),
            self.assertRaises(SchemaReviewError),
        ):
            review_schema(self.source, self.output)
        self.assertFalse((other / "report.json").exists())
        self.assertEqual(list((self.folder / "original-output-parent").iterdir()), [])

    def test_cli_success_is_small_and_error_json_never_echoes_paths_or_xml(self):
        code, stdout, stderr = self.run_cli(["--metadata", str(self.source), "--output", str(self.output)])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        summary = json.loads(stdout)
        self.assertEqual(summary["entity_sets_reviewed"], 1)
        self.assertNotIn("catalog", summary)
        self.assertNotIn(str(self.source), stdout)
        self.assertNotIn(str(self.output), stdout)
        sentinel = "private-source-sentinel"
        self.source.write_text("<" + sentinel)
        missing_report = self.folder / "no-output.json"
        code, stdout, stderr = self.run_cli(["--metadata", str(self.source), "--output", str(missing_report)])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        error = json.loads(stderr)
        self.assertEqual(error["reason"], "offline_schema_review_failed")
        self.assertFalse(error["report_created"])
        self.assertNotIn(sentinel, stderr)
        self.assertNotIn(str(self.source), stderr)
        self.assertFalse(missing_report.exists())

    def test_invalid_cli_arguments_are_fixed_safe_json(self):
        for arguments in ([], ["--metadata", str(self.source)], ["--unknown", "private-argument-sentinel"]):
            code, stdout, stderr = self.run_cli(arguments)
            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertEqual(json.loads(stderr)["reason"], "offline_schema_review_failed")
            self.assertNotIn("private-argument-sentinel", stderr)
            self.assertNotIn(str(self.source), stderr)

    def test_standalone_command_has_no_sdk_network_config_or_secret_access(self):
        script = """
import os,sys
from unittest.mock import patch
import sap_schema_review
original_get=os.environ.get
def environment_guard(name,default=None):
    if name in {'TEST_SAP_PASSWORD','OPENAI_API_KEY','ANTHROPIC_API_KEY'}:
        raise AssertionError('Secret access is forbidden')
    return original_get(name,default)
with patch('os.environ.get',side_effect=environment_guard), \\
     patch('socket.getaddrinfo',side_effect=AssertionError('No DNS')), \\
     patch('socket.create_connection',side_effect=AssertionError('No network')), \\
     patch('sap_connection.load_connection',side_effect=AssertionError('No configuration')), \\
     patch('sap_adapter._credential',side_effect=AssertionError('No credentials')):
    code=sap_schema_review.main(['--metadata',sys.argv[1],'--output',sys.argv[2]])
assert not any(name in sys.modules for name in ('lfx','langflow','openai','anthropic'))
raise SystemExit(code)
"""
        env = dict(os.environ)
        env.update(
            TEST_SAP_PASSWORD="synthetic-not-read",
            OPENAI_API_KEY="synthetic-not-read",
            ANTHROPIC_API_KEY="synthetic-not-read",
            PYTHONDONTWRITEBYTECODE="1",
        )
        process = subprocess.run(
            [sys.executable, "-c", script, str(self.source), str(self.output)],
            cwd=Path(__file__).resolve().parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(json.loads(process.stdout)["report_created"])
        self.assertFalse((self.folder / "requests.db").exists())
        self.assertFalse((self.folder / "drafts").exists())


if __name__ == "__main__":
    unittest.main()
