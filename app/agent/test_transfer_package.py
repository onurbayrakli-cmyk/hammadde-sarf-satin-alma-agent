"""Synthetic offline handoff packaging; no install, service or provider calls."""

import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from transfer import build_package as builder
from transfer import verify_package as verifier


class TransferPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name).resolve()
        self.source = self.folder / "purchasing"
        (self.source / "transfer").mkdir(parents=True)
        self.verifier_source = Path(verifier.__file__)
        shutil.copyfile(self.verifier_source, self.source / "transfer/verify_package.py")
        (self.source / "worker.py").write_text('raise RuntimeError("package code must never execute")\n')
        (self.source / "README.md").write_text("Synthetic source pilot; no active integration.\n")
        self.flow = {
            "name": "Synthetic purchasing",
            "description": "Isolated source pilot",
            "data": {
                "nodes": [
                    {
                        "id": "node-" + str(index),
                        "data": {
                            "node": {
                                "template": {
                                    "_type": "Component",
                                    "code": {"value": "pass\n"},
                                    "requester": {"value": ""},
                                    "enable_api": {"value": False},
                                    "session_id": {"value": ""},
                                    "api_key": {
                                        "value": "OPENAI_API_KEY",
                                        "password": True,
                                        "load_from_db": True,
                                    },
                                }
                            }
                        },
                    }
                    for index in range(21)
                ],
                "edges": [
                    {
                        "id": "edge-" + str(index),
                        "source": "node-" + str(index % 21),
                        "target": "node-" + str((index + 1) % 21),
                    }
                    for index in range(28)
                ],
            },
        }
        self.write_flow()
        self.files = [
            {"source": "worker.py", "path": "agent/worker.py", "kind": "source"},
            {"source": "flow.json", "path": "flow/purchasing.json", "kind": "flow"},
            {"source": "README.md", "path": "README.md", "kind": "setup"},
            {"source": "transfer/verify_package.py", "path": verifier.VERIFIER, "kind": "verifier"},
        ]
        self.output = self.folder / "Purchasing_v0.1.0_macos.zip"

    def tearDown(self):
        self.temporary.cleanup()

    def write_flow(self):
        (self.source / "flow.json").write_text(json.dumps(self.flow))

    def build(self, **kwargs):
        return builder.build_package(self.output, self.files, version="0.1.0", source_root=self.source, **kwargs)

    def contents(self):
        with zipfile.ZipFile(self.output) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def write_archive(self, contents, *, filename="attack.zip", mode=None, comment=b"", extra=b""):
        target = self.folder / filename
        with zipfile.ZipFile(target, "w") as archive:
            for name, raw in contents.items():
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o600 if mode is None else mode) << 16
                info.extra = extra
                archive.writestr(info, raw)
            archive.comment = comment
        return target

    def update_record(self, contents, path, raw, *, new_path=None):
        manifest = json.loads(contents[verifier.MANIFEST])
        for item in manifest["files"]:
            if item["path"] == path:
                item.update(path=new_path or path, size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        contents.pop(path)
        contents[new_path or path] = raw
        manifest["total_bytes"] = sum(item["size"] for item in manifest["files"])
        contents[verifier.MANIFEST] = verifier._json(manifest)

    def test_roundtrip_exact_inventory_and_temporary_extract_without_execution(self):
        (self.source / ".env").write_text("SECRET=never copy\n")
        (self.source / "cases").mkdir()
        (self.source / "cases/requests.db").write_bytes(b"not exported")
        result = self.build()
        self.assertEqual(result["status"], "created_and_verified")
        self.assertTrue(result["temporary_extract_verified"])
        self.assertFalse(result["production_ready"])
        self.assertFalse(result["signature_verified"])
        self.assertFalse(result["code_executed"])
        data = self.contents()
        manifest = json.loads(data[verifier.MANIFEST])
        self.assertEqual(set(data), {row["path"] for row in self.files} | {verifier.MANIFEST})
        self.assertNotIn(str(self.source).encode(), data[verifier.MANIFEST])
        self.assertEqual(manifest["total_files"], 4)
        self.assertTrue(verifier.verify_package(self.output, extract_smoke=True)["temporary_extract_removed"])
        self.assertFalse(list(self.folder.glob(".purchasing-build-*")))

    def test_standalone_extracted_verifier_runs_with_standard_library(self):
        self.build()
        target = self.folder / "release"
        with zipfile.ZipFile(self.output) as archive:
            archive.extractall(target)
        run = subprocess.run(
            [sys.executable, "-B", str(target / verifier.VERIFIER), str(target), "--extract-smoke"],
            cwd=self.folder,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["status"], "verified")
        self.assertFalse(list(target.rglob("__pycache__")))
        self.assertEqual(verifier.verify_package(target)["files"], 4)

    def test_versioned_create_only_does_not_replace_existing_release(self):
        self.build()
        original = self.output.read_bytes()
        with self.assertRaises(ValueError):
            self.build()
        self.assertEqual(self.output.read_bytes(), original)
        for version, name in [("dev", "Purchasing_dev.zip"), ("0.1.0", "Purchasing.zip")]:
            with self.subTest(version=version), self.assertRaises(ValueError):
                builder.build_package(self.folder / name, self.files, version=version, source_root=self.source)

    def test_selection_schema_is_explicit_small_and_inside_agent(self):
        selection = self.source / "transfer/package_files.json"
        selection.write_text(json.dumps({"schema_version": 1, "files": self.files}))
        self.assertEqual(builder.load_selection(selection, source_root=self.source), self.files)
        for raw in [b'{"schema_version":1,"schema_version":1,"files":[]}', b'{"schema_version":1,"files":NaN}']:
            selection.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                builder.load_selection(selection, source_root=self.source)
        outside = self.folder / "outside.json"
        outside.write_text("{}")
        with self.assertRaises(ValueError):
            builder.load_selection(outside, source_root=self.source)

    def test_hidden_state_reports_databases_and_other_project_paths_rejected(self):
        for source in [".env", "cases/a.py", "review/a.py", "runtime/a.py", "../other.py", "requests.db", "other.bin"]:
            with self.subTest(source=source), self.assertRaises(ValueError):
                builder.build_package(
                    self.output,
                    [*self.files, {"source": source, "path": "agent/extra.py", "kind": "source"}],
                    version="0.1.0",
                    source_root=self.source,
                )
        self.assertFalse(self.output.exists())

    def test_source_and_output_symlinks_and_hardlinks_rejected(self):
        original = self.source / "worker.py"
        original.rename(self.source / "actual.py")
        original.symlink_to(self.source / "actual.py")
        with self.assertRaises(ValueError):
            self.build()
        original.unlink()
        os.link(self.source / "actual.py", original)
        with self.assertRaises(ValueError):
            self.build()
        original.unlink()
        (self.source / "actual.py").rename(original)
        directory = self.folder / "alias"
        directory.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ValueError):
            builder.build_package(directory / "new_v0.1.0.zip", self.files, version="0.1.0", source_root=self.source)

    def test_personal_machine_path_or_known_secret_is_rejected_without_rewriting(self):
        worker = self.source / "worker.py"
        for value in ["/" + "Users/synthetic-user/private", "sk-" + "X" * 30]:
            original = ("VALUE = " + repr(value) + "\n").encode()
            worker.write_bytes(original)
            with self.subTest(value=value[:4]), self.assertRaises(ValueError):
                self.build()
            self.assertEqual(worker.read_bytes(), original)
        self.assertFalse(self.output.exists())

    def test_duplicate_source_destination_and_case_collision_rejected(self):
        for extra in [
            self.files[0],
            {"source": "README.md", "path": "AGENT/WORKER.py", "kind": "source"},
        ]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                builder.build_package(self.output, [*self.files, extra], version="0.1.0", source_root=self.source)

    def test_wrong_graph_counts_or_invalid_links_rejected(self):
        original = copy.deepcopy(self.flow)
        self.flow["data"]["nodes"].pop()
        self.write_flow()
        with self.assertRaises(ValueError):
            self.build()
        self.flow = original
        self.flow["data"]["edges"][0]["target"] = "other-flow-node"
        self.write_flow()
        with self.assertRaises(ValueError):
            self.build()

    def test_native_agent_typed_metadata_supported_without_arbitrary_string_fields(self):
        node = self.flow["data"]["nodes"][0]
        node["data"]["type"] = "Agent"
        template = node["data"]["node"]["template"]
        template["is_refresh"] = True
        self.write_flow()
        self.assertEqual(self.build()["status"], "created_and_verified")
        for field, value in [("_type", "OtherComponent"), ("is_refresh", "true"), ("unknown_metadata", "text")]:
            malformed = copy.deepcopy(self.flow)
            malformed["data"]["nodes"][0]["data"]["node"]["template"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                verifier._validate_flow(verifier._json(malformed))
        malformed = copy.deepcopy(self.flow)
        malformed["data"]["nodes"][0]["data"]["type"] = "SyntheticCustomComponent"
        with self.assertRaises(ValueError):
            verifier._validate_flow(verifier._json(malformed))

    def test_flow_account_identifiers_business_inputs_and_live_modes_rejected(self):
        original = copy.deepcopy(self.flow)
        scenarios = [
            ("top", "user_id", "source-account"),
            ("top", "id", "source-flow"),
            ("field", "requester", "person@example.com"),
            ("field", "session_id", "source-session"),
            ("field", "enable_api", True),
            ("field", "enable_live", True),
            ("field", "save_monitor_result", True),
        ]
        for location, field, value in scenarios:
            self.flow = copy.deepcopy(original)
            if location == "top":
                self.flow[field] = value
            else:
                self.flow["data"]["nodes"][0]["data"]["node"]["template"][field] = {"value": value}
            self.write_flow()
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.build()

    def test_flow_allows_only_blank_or_generic_provider_reference(self):
        field = self.flow["data"]["nodes"][0]["data"]["node"]["template"]["api_key"]
        for value, load in [("private-global-name", True), ("OPENAI_API_KEY", False), ("actual-password", False)]:
            field.update(value=value, load_from_db=load)
            self.write_flow()
            with self.subTest(load=load), self.assertRaises(ValueError):
                self.build()
        field.update(value="", load_from_db=False)
        self.write_flow()
        self.build()

    def test_mandatory_flow_and_independent_verifier_are_enforced(self):
        for kind in ["flow", "verifier"]:
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                builder.build_package(
                    self.output,
                    [row for row in self.files if row["kind"] != kind],
                    version="0.1.0",
                    source_root=self.source,
                )
        (self.source / "transfer/verify_package.py").write_text("# replaced verifier\n")
        with self.assertRaises(ValueError):
            self.build()

    def test_explicit_synthetic_fixture_can_be_included(self):
        fixture = self.source / "Synthetic_Table.txt"
        fixture.write_text("material|quantity\nsynthetic|2\n")
        self.files.append({"source": fixture.name, "path": "agent/" + fixture.name, "kind": "fixture"})
        self.assertEqual(self.build()["files"], 5)
        self.assertEqual(self.contents()["agent/" + fixture.name], fixture.read_bytes())

    def test_hash_size_missing_or_unlisted_files_are_rejected(self):
        self.build()
        original = self.contents()
        for change in ["tamper", "missing", "unlisted"]:
            data = dict(original)
            if change == "tamper":
                data["agent/worker.py"] += b"# changed\n"
            elif change == "missing":
                data.pop("agent/worker.py")
            else:
                data["extra.py"] = b"pass\n"
            with self.subTest(change=change), self.assertRaises(ValueError):
                verifier.verify_package(self.write_archive(data))

    def test_traversal_absolute_hidden_and_case_conflicting_zip_paths_rejected(self):
        self.build()
        for path in ["../escape.py", "/escape.py", "C:/escape.py", "agent\\escape.py", ".env", "agent/./escape.py"]:
            data = self.contents()
            self.update_record(data, "agent/worker.py", b"pass\n", new_path=path)
            with self.subTest(path=path), self.assertRaises(ValueError):
                verifier.verify_package(self.write_archive(data))
        data = self.contents()
        data["AGENT/WORKER.py"] = data["agent/worker.py"]
        with self.assertRaises(ValueError):
            verifier.verify_package(self.write_archive(data))
        self.assertFalse((self.folder / "escape.py").exists())

    def test_unicode_noncanonical_and_control_paths_rejected(self):
        self.build()
        for path in ["agent/e\u0301.py", "agent/evil\n.py", "agent/file .py."]:
            data = self.contents()
            self.update_record(data, "agent/worker.py", b"pass\n", new_path=path)
            with self.subTest(path=path), self.assertRaises(ValueError):
                verifier.verify_package(self.write_archive(data))

    def test_macos_directory_spelling_and_file_directory_collisions_rejected(self):
        self.build()
        for path in ["AGENT/readme.txt", "agent/worker.py/readme.txt"]:
            data = self.contents()
            self.update_record(data, "README.md", b"Synthetic note\n", new_path=path)
            with self.subTest(path=path), self.assertRaises(ValueError):
                verifier.verify_package(self.write_archive(data))

    def test_symlink_zip_entry_extra_metadata_and_archive_comment_rejected(self):
        self.build()
        for options in [
            {"mode": stat.S_IFLNK | 0o777},
            {"mode": stat.S_IFREG | 0o6755},
            {"extra": b"\x00\x00\x00\x00"},
            {"comment": b"unlisted"},
        ]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                verifier.verify_package(self.write_archive(self.contents(), **options))

    def test_duplicate_zip_member_and_compression_bomb_rejected(self):
        self.build()
        target = self.folder / "duplicate.zip"
        with zipfile.ZipFile(target, "w") as archive:
            for name, raw in self.contents().items():
                archive.writestr(name, raw)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr("agent/worker.py", b"pass\n")
        with self.assertRaises(ValueError):
            verifier.verify_package(target)
        target = self.folder / "ratio.zip"
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("large.py", b"#" * 1_000_000)
            archive.writestr(verifier.MANIFEST, b"{}")
        with self.assertRaises(ValueError):
            verifier.verify_package(target)

    def test_unlisted_bytes_before_or_after_zip_are_rejected(self):
        self.build()
        raw = self.output.read_bytes()
        target = self.folder / "hidden-bytes.zip"
        for mutated in [b"unlisted-prefix" + raw, raw + b"unlisted-suffix"]:
            target.write_bytes(mutated)
            with self.subTest(prefix=mutated.startswith(b"unlisted")), self.assertRaises(ValueError):
                verifier.verify_package(target)

    def test_manifest_duplicate_fields_extra_state_and_false_production_claim_rejected(self):
        self.build()
        original = self.contents()
        for key, value in [
            ("production_ready", True),
            ("includes_business_data", True),
            ("auto_execute", True),
            ("private", "state"),
        ]:
            data = dict(original)
            manifest = json.loads(data[verifier.MANIFEST])
            manifest[key] = value
            data[verifier.MANIFEST] = verifier._json(manifest)
            with self.subTest(key=key), self.assertRaises(ValueError):
                verifier.verify_package(self.write_archive(data))
        data = dict(original)
        data[verifier.MANIFEST] = data[verifier.MANIFEST].replace(
            b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1'
        )
        with self.assertRaises(ValueError):
            verifier.verify_package(self.write_archive(data))

    def test_self_consistent_manifest_cannot_hide_personal_data_or_unsafe_flow(self):
        self.build()
        data = self.contents()
        self.update_record(
            data, "agent/worker.py", ("VALUE = " + repr("/" + "Users/private-user/live") + "\n").encode()
        )
        with self.assertRaises(ValueError):
            verifier.verify_package(self.write_archive(data))
        data = self.contents()
        unsafe_flow = copy.deepcopy(self.flow)
        unsafe_flow["id"] = "source-identity"
        self.update_record(data, "flow/purchasing.json", verifier._json(unsafe_flow))
        with self.assertRaises(ValueError):
            verifier.verify_package(self.write_archive(data))

    def test_escaped_json_personal_path_nonfinite_number_and_bad_flow_shape_rejected(self):
        self.build()
        original = self.contents()
        malformed = copy.deepcopy(self.flow)
        malformed["data"]["nodes"][0]["data"] = None
        personal = "/" + "Users/private-user/live"
        raw_personal = json.dumps({**self.flow, "description": personal}).replace("/", "\\/").encode()
        for raw in [raw_personal, verifier._json(malformed), b'{"value":1e999}']:
            data = dict(original)
            self.update_record(data, "flow/purchasing.json", raw)
            with self.subTest(raw=raw[:12]), self.assertRaises(ValueError):
                verifier.verify_package(self.write_archive(data))

    def test_bounded_source_file_total_and_inventory_limits(self):
        with patch.object(verifier, "MAX_FILES", 3), self.assertRaises(ValueError):
            self.build()
        with patch.object(builder, "MAX_TOTAL", 10), self.assertRaises(ValueError):
            self.build()
        self.build()
        with patch.object(verifier, "MAX_FILE", 10), self.assertRaises(ValueError):
            verifier.verify_package(self.output)

    def test_directory_tamper_unlisted_empty_directory_and_symlink_rejected(self):
        self.build()
        for scenario in ["tamper", "empty", "symlink"]:
            target = self.folder / scenario
            with zipfile.ZipFile(self.output) as archive:
                archive.extractall(target)
            if scenario == "tamper":
                (target / "README.md").write_text("Changed\n")
            elif scenario == "empty":
                (target / "extra").mkdir()
            else:
                (target / "extra.py").symlink_to(target / "agent/worker.py")
            with self.subTest(scenario=scenario), self.assertRaises(ValueError):
                verifier.verify_package(target, extract_smoke=True)

    def test_source_mutation_during_build_never_publishes_partial_release(self):
        original_verify = builder.verify_package

        def changed(*args, **kwargs):
            result = original_verify(*args, **kwargs)
            (self.source / "worker.py").write_text("# changed during source collection\n")
            return result

        with patch.object(builder, "verify_package", side_effect=changed), self.assertRaises(ValueError):
            self.build()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.folder.glob(".purchasing-build-*")))

    def test_cli_failures_exit_nonzero_without_private_values(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = verifier.main([str(self.folder / "private-missing.zip")])
        self.assertEqual(code, 2)
        self.assertNotIn("private-missing", stdout.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["status"], "error")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = builder.main(
                ["--version", "0.1.0", "--files", str(self.folder / "absent.json"), "--output", str(self.output)]
            )
        self.assertEqual(code, 2)
        self.assertNotIn(str(self.folder), stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
