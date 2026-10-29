"""Offline target setup tests: synthetic JSON and external temporary folders."""

import ast
import contextlib
import copy
import hashlib
import importlib.metadata
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from transfer import target_setup

COORDINATOR_CODE = """from pathlib import Path
DEFAULT_PATH = Path.cwd() / ".codex/agents/satin-alma-stok/cases/requests.db"
def coordinate_request(context, store=None):
    # İş mantığı korunmalı.
    if store is None:
        store = CaseStore(Path.cwd() / ".codex/agents/satin-alma-stok/cases/requests.db")
    return context, store
"""
RESEARCH_CODE = """from pathlib import Path
class ProcurementResearchComponent:
    def _prepare_research(self):
        folder = Path.cwd() / ".codex/agents/satin-alma-stok/monitor"
        return folder
"""
RFQ_CODE = """from pathlib import Path
def _rt_dependencies():
    root = (Path.cwd() / ".codex/agents/satin-alma-stok").resolve()
    return root
"""


def clean_flow():
    nodes = []
    for identifier, node_type in target_setup.EXPECTED_NODES.items():
        fields = {name: {"value": ""} for name in target_setup.CLEAN_FIELDS.get(node_type, ())}
        fields.update({name: {"value": False} for name in target_setup.DISABLED_FIELDS.get(node_type, ())})
        fields["code"] = {"value": "pass\n"}
        fields["_frontend_node_flow_id"] = {"value": "source-flow-identity", "load_from_db": False}
        fields["_frontend_node_folder_id"] = {"value": "source-folder-identity"}
        if node_type == "Agent":
            fields["api_key"] = {"value": "OPENAI_API_KEY", "password": True, "load_from_db": True}
            fields["model"] = {"value": [{"provider": "OpenAI", "name": "synthetic"}]}
        nodes.append({"id": identifier, "data": {"id": identifier, "type": node_type, "node": {"template": fields}}})
    found = {node["id"]: node for node in nodes}
    found[target_setup.COORDINATOR]["data"]["node"]["template"]["code"]["value"] = COORDINATOR_CODE
    found[target_setup.RESEARCH]["data"]["node"]["template"]["code"]["value"] = RESEARCH_CODE
    found[target_setup.RFQ]["data"]["node"]["template"]["code"]["value"] = RFQ_CODE
    identifiers = list(found)
    edges = [
        {
            "source": identifiers[index % 21],
            "target": identifiers[(index + 1) % 21],
            "data": {"sourceHandle": {"name": "port" + str(index)}, "targetHandle": {"fieldName": "input"}},
        }
        for index in range(28)
    ]
    return {
        "name": "Sentetik satın alma",
        "description": "Yalnız çevrimdışı test",
        "data": {"nodes": nodes, "edges": edges},
    }


class TargetSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / "Hedef ' çalışma"
        self.config = self.root / "config.json"
        self.source = self.root / "source.json"
        self.output = self.root / "prepared.json"
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.payload = {
            "schema_version": 1,
            "state_dir": str(self.state),
            "langflow": {
                "base_url": "http://127.0.0.1:7860",
                "flow_id": None,
                "api_key_env": "SATINALMA_LANGFLOW_API_KEY",
                "database_path": None,
            },
        }
        self.write_config()
        self.write_flow(clean_flow())

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def write_config(self):
        self.config.write_text(json.dumps(self.payload, ensure_ascii=False), encoding="utf-8")

    def write_flow(self, flow):
        self.source.write_text(json.dumps(flow, ensure_ascii=False), encoding="utf-8")

    def prepare(self):
        return target_setup.prepare_flow(self.config, self.source, self.output)

    def test_prepare_is_create_only_binds_five_paths_and_never_creates_state(self):
        before = self.source.read_bytes()
        result = self.prepare()
        prepared = json.loads(self.output.read_text())
        nodes = {node["id"]: node for node in prepared["data"]["nodes"]}
        coordinator = target_setup._fields(nodes[target_setup.COORDINATOR])["code"]["value"]
        research = target_setup._fields(nodes[target_setup.RESEARCH])["code"]["value"]
        queue = self.state / "cases" / "requests.db"
        self.assertEqual(
            coordinator, COORDINATOR_CODE.replace(target_setup.SOURCE_QUEUE, "Path(" + repr(str(queue)) + ")")
        )
        self.assertEqual(
            research,
            RESEARCH_CODE.replace(target_setup.SOURCE_MONITOR, "Path(" + repr(str(self.state / "monitor")) + ")"),
        )
        fields = target_setup._fields(nodes[target_setup.RFQ])
        self.assertEqual(fields["queue_path"]["value"], str(queue))
        self.assertEqual(fields["output_dir"]["value"], str(self.state / "cases" / "drafts"))
        self.assertFalse(self.state.exists())
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(result["persistent_bindings"], 5)
        self.assertEqual(result["total_path_bindings"], 6)
        self.assertEqual(result["source_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(result["config_sha256"], hashlib.sha256(self.config.read_bytes()).hexdigest())
        self.assertEqual(result["output_sha256"], hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertNotIn("source-flow-identity", self.output.read_text())
        self.assertNotIn("source-folder-identity", self.output.read_text())
        self.assertNotIn("id", prepared)
        self.assertFalse(result["network_called"])
        self.assertFalse(result["flow_imported"])
        with self.assertRaises(ValueError):
            self.prepare()

    def test_preflight_observes_metadata_only_and_does_not_probe_or_create(self):
        with (
            patch.object(target_setup.platform, "system", return_value="Darwin"),
            patch.object(target_setup.platform, "release", return_value="synthetic"),
            patch.object(target_setup.platform, "machine", return_value="arm64"),
            patch.object(target_setup.shutil, "which", return_value="/synthetic/uv"),
            patch.object(target_setup.importlib.metadata, "version", return_value="1.12.4"),
        ):
            os.environ["SATINALMA_LANGFLOW_API_KEY"] = "private-value-never-read"
            result = target_setup.preflight(self.config)
        self.assertEqual(result["status"], "local_prerequisites_observed")
        self.assertFalse(result["files_written"])
        self.assertFalse(result["api_key_value_read"])
        self.assertFalse(result["runtime_compatibility_verified"])
        self.assertFalse(result["target_machine_verified"])
        self.assertNotIn("private-value-never-read", json.dumps(result))
        self.assertFalse(self.state.exists())

    def test_preflight_missing_requirements_are_explicit(self):
        with (
            patch.object(target_setup.platform, "system", return_value="Windows"),
            patch.object(target_setup.shutil, "which", return_value=None),
            patch.object(target_setup.Path, "home", return_value=self.root / "absent-home"),
            patch.object(
                target_setup.importlib.metadata, "version", side_effect=importlib.metadata.PackageNotFoundError
            ),
        ):
            result = target_setup.preflight(self.config)
        self.assertEqual(result["status"], "requirements_missing")
        self.assertEqual(
            set(result["missing"]),
            {"target_macos_not_observed", "uv_command_missing", "langflow_runtime_metadata_missing"},
        )
        self.assertFalse(self.state.exists())

    def test_release_state_and_output_are_rejected_without_writes(self):
        self.payload["state_dir"] = str(target_setup.AGENT_ROOT / "forbidden-state")
        self.write_config()
        with self.assertRaises(ValueError):
            self.prepare()
        self.payload["state_dir"] = str(self.state)
        self.write_config()
        with self.assertRaises(ValueError):
            target_setup.prepare_flow(self.config, self.source, target_setup.AGENT_ROOT / "forbidden-prepared.json")
        self.assertFalse(self.output.exists())

    def test_existing_target_flow_identity_prevents_duplicate_import_preparation(self):
        self.payload["langflow"]["flow_id"] = "f85ae12b-4c61-41e6-9816-d03c923c2610"
        self.write_config()
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.state.exists())
        with patch.object(target_setup.importlib.metadata, "version", return_value="1.12.4"):
            status = target_setup.preflight(self.config)
        self.assertTrue(status["planned_configuration"]["flow_id_configured"])

    def test_output_parent_must_exist_and_existing_file_is_unchanged(self):
        with self.assertRaises(ValueError):
            target_setup.prepare_flow(self.config, self.source, self.root / "absent" / "prepared.json")
        self.output.write_text("existing", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(self.output.read_text(), "existing")

    def test_symlink_source_and_destination_are_rejected(self):
        link = self.root / "source-link.json"
        link.symlink_to(self.source)
        with self.assertRaises(ValueError):
            target_setup.prepare_flow(self.config, link, self.output)
        self.output.symlink_to(self.source)
        with self.assertRaises(ValueError):
            self.prepare()

    def test_dirty_input_and_enabled_integrations_are_rejected(self):
        for identifier, field, value in (
            ("MailResearchInput-OzelTalep", "mail_message", "old email body"),
            (target_setup.RFQ, "supplier_rows", [{"email": "old@example.org"}]),
            ("SAPProcurementInput-SAP", "enable_api", True),
            (target_setup.RESEARCH, "allow_demo", True),
        ):
            with self.subTest(identifier=identifier, field=field):
                flow = clean_flow()
                node = next(node for node in flow["data"]["nodes"] if node["id"] == identifier)
                target_setup._fields(node)[field]["value"] = value
                self.write_flow(flow)
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.output.exists())

    def test_password_literal_rejected_global_reference_retained(self):
        flow = clean_flow()
        node = next(node for node in flow["data"]["nodes"] if node["id"] == "Agent-OpenAIAnaliz")
        target_setup._fields(node)["api_key"].update(value="literal-private-value", load_from_db=False)
        self.write_flow(flow)
        with self.assertRaises(ValueError):
            self.prepare()
        self.write_flow(clean_flow())
        self.prepare()
        self.assertIn("OPENAI_API_KEY", self.output.read_text())

    def test_unmarked_secret_and_unknown_global_secret_reference_are_rejected(self):
        for key, value in (
            ("API-Key", {"value": "private-literal"}),
            ("credentials", {"value": "UNKNOWN_SECRET", "load_from_db": True}),
            ("api_key", {"value": "ANTHROPIC_API_KEY", "load_from_db": True}),
        ):
            flow = clean_flow()
            node = next(node for node in flow["data"]["nodes"] if node["id"] == "Agent-OpenAIAnaliz")
            target_setup._fields(node)[key] = value
            self.write_flow(flow)
            with self.assertRaises(ValueError):
                self.prepare()
            self.assertFalse(self.output.exists())

    def test_author_path_anywhere_rejected_without_echo(self):
        flow = clean_flow()
        flow["description"] = "/".join(("", "Users", "synthetic-author", "private", "source"))
        self.write_flow(flow)
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            code = target_setup.main(
                [
                    "prepare-flow",
                    "--config",
                    str(self.config),
                    "--source",
                    str(self.source),
                    "--output",
                    str(self.output),
                ]
            )
        self.assertEqual(code, 1)
        self.assertNotIn("synthetic-author", capture.getvalue())
        self.assertFalse(self.output.exists())

    def test_source_scope_and_owner_identifiers_are_rejected(self):
        for field, value in (("id", "source-uuid"), ("user_id", "source-user"), ("workspace_id", "source-workspace")):
            flow = clean_flow()
            flow[field] = value
            self.write_flow(flow)
            with self.assertRaises(ValueError):
                self.prepare()

    def test_wrong_graph_size_unknown_node_and_duplicate_edge_are_rejected(self):
        for mutation in (
            lambda f: f["data"]["nodes"].pop(),
            lambda f: f["data"]["nodes"][0].update(id="other-agent"),
            lambda f: f["data"]["edges"].__setitem__(1, copy.deepcopy(f["data"]["edges"][0])),
        ):
            flow = clean_flow()
            mutation(flow)
            self.write_flow(flow)
            with self.assertRaises(ValueError):
                self.prepare()

    def test_changed_or_duplicate_ast_binding_shape_is_rejected(self):
        for code in (
            COORDINATOR_CODE.replace(target_setup.SOURCE_QUEUE, 'Path("unexpected")', 1),
            COORDINATOR_CODE + "\nDEFAULT_PATH = Path('other')\n",
            COORDINATOR_CODE.replace(
                "return context, store", "other = CaseStore(DEFAULT_PATH)\n    return context, store"
            ),
        ):
            flow = clean_flow()
            node = next(node for node in flow["data"]["nodes"] if node["id"] == target_setup.COORDINATOR)
            target_setup._fields(node)["code"]["value"] = code
            self.write_flow(flow)
            with self.assertRaises(ValueError):
                self.prepare()
        flow = clean_flow()
        node = next(node for node in flow["data"]["nodes"] if node["id"] == target_setup.RESEARCH)
        target_setup._fields(node)["code"]["value"] = RESEARCH_CODE.replace(
            target_setup.SOURCE_MONITOR, 'Path("other")'
        )
        self.write_flow(flow)
        with self.assertRaises(ValueError):
            self.prepare()

    def test_utf8_source_offsets_do_not_change_other_logic(self):
        code = COORDINATOR_CODE.replace("DEFAULT_PATH =", "başlık = 'şart'; DEFAULT_PATH =")
        bound = target_setup._bind_coordinator(code, self.state / "requests.db")
        ast.parse(bound)
        self.assertIn("başlık = 'şart';", bound)
        self.assertIn("# İş mantığı korunmalı.", bound)

    def test_rfq_helpers_resolve_from_package_root_when_cwd_is_unrelated(self):
        self.prepare()
        prepared = json.loads(self.output.read_text())
        node = next(node for node in prepared["data"]["nodes"] if node["id"] == target_setup.RFQ)
        code = target_setup._fields(node)["code"]["value"]
        namespace = {}
        exec(compile(code, "synthetic-rfq", "exec"), namespace)
        current = Path.cwd()
        unrelated = self.root / "unrelated-cwd"
        unrelated.mkdir()
        try:
            os.chdir(unrelated)
            self.assertEqual(namespace["_rt_dependencies"](), target_setup.AGENT_ROOT)
        finally:
            os.chdir(current)
        self.assertNotIn(target_setup.SOURCE_AGENT_ROOT, code)
        self.assertFalse(self.state.exists())

    def test_changed_rfq_dependency_binding_shape_is_rejected(self):
        flow = clean_flow()
        node = next(node for node in flow["data"]["nodes"] if node["id"] == target_setup.RFQ)
        target_setup._fields(node)["code"]["value"] = RFQ_CODE.replace(
            target_setup.SOURCE_AGENT_ROOT, 'Path("unexpected")'
        )
        self.write_flow(flow)
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_relocated_package_imports_agent_and_protects_entire_release(self):
        release = self.root / "release"
        scripts = release / "transfer"
        agent = release / "agent"
        scripts.mkdir(parents=True)
        agent.mkdir()
        helper = scripts / "target_setup.py"
        shutil.copyfile(target_setup.__file__, helper)
        shutil.copyfile(target_setup.AGENT_ROOT / "runtime_config.py", agent / "runtime_config.py")
        package_flow = release / "flow.json"
        shutil.copyfile(self.source, package_flow)

        def run(*arguments):
            return subprocess.run(
                [sys.executable, str(helper), *arguments],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )

        checked = run("preflight", "--config", str(self.config))
        report = json.loads(checked.stdout)
        self.assertEqual(report["command"], "preflight")
        self.assertTrue(report["planned_configuration"]["external_state"])
        prepared = run(
            "prepare-flow", "--config", str(self.config), "--source", str(package_flow), "--output", str(self.output)
        )
        self.assertEqual(prepared.returncode, 0, prepared.stdout)
        self.assertFalse(prepared.stderr)
        flow = json.loads(self.output.read_text())
        node = next(node for node in flow["data"]["nodes"] if node["id"] == target_setup.RFQ)
        code = target_setup._fields(node)["code"]["value"]
        namespace = {}
        exec(compile(code, "synthetic-package-rfq", "exec"), namespace)
        self.assertEqual(namespace["_rt_dependencies"](), agent)
        forbidden_output = release / "forbidden-prepared.json"
        blocked = run(
            "prepare-flow",
            "--config",
            str(self.config),
            "--source",
            str(package_flow),
            "--output",
            str(forbidden_output),
        )
        self.assertEqual(blocked.returncode, 1)
        self.assertFalse(forbidden_output.exists())
        self.payload["state_dir"] = str(release / "forbidden-state")
        self.write_config()
        blocked = run("preflight", "--config", str(self.config))
        self.assertEqual(blocked.returncode, 1)
        self.assertFalse((release / "forbidden-state").exists())
        self.assertFalse(self.state.exists())

    def test_config_changed_during_preparation_is_not_bound_to_wrong_hash(self):
        real = target_setup._bind_coordinator

        def change_config(code, path):
            self.payload["state_dir"] = str(self.root / "different-state")
            self.write_config()
            return real(code, path)

        with patch.object(target_setup, "_bind_coordinator", side_effect=change_config):
            with self.assertRaises(ValueError):
                self.prepare()
        self.assertFalse(self.output.exists())

    def test_json_duplicate_nonfinite_and_oversize_rejected(self):
        for raw in (
            '{"name":"one","name":"two","data":{}}',
            '{"name":NaN}',
            '{"name":1e999}',
            " " * (target_setup.MAX_FLOW_BYTES + 1),
        ):
            self.source.write_text(raw, encoding="utf-8")
            with self.assertRaises(ValueError):
                self.prepare()
        self.assertFalse(self.output.exists())

    def test_partial_write_failure_removes_only_created_output(self):
        with patch.object(target_setup.os, "fsync", side_effect=OSError("synthetic private details")):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
