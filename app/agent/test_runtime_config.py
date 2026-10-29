"""Portable runtime and HTTP client contracts, using temporary files/fake HTTP."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
import runtime_config
from langflow_client import LangflowClientError, fetch_flow, safe_flow, session_for
from runtime_config import RuntimeConfigurationError, get_runtime

FLOW_ID = runtime_config.LEGACY_FLOW_ID


class RuntimeFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.release = self.root / "release"
        self.release.mkdir()
        self.state = self.root / "state"
        self.config = self.root / "runtime.json"
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.source = patch("runtime_config.SOURCE_ROOT", self.release)
        self.source.start()
        self.addCleanup(self.source.stop)

    def data(self):
        return {"schema_version": 1, "state_dir": str(self.state), "langflow": {
            "base_url": "https://langflow.example.org", "flow_id": FLOW_ID,
            "api_key_env": "PILOT_LANGFLOW_API_KEY", "database_path": None}}

    def configured(self, data=None):
        self.config.write_text(json.dumps(self.data() if data is None else data), encoding="utf-8")
        return get_runtime(self.config)


class RuntimeSettingsTests(RuntimeFixture):
    def test_absent_config_preserves_legacy_paths_and_no_state_is_created(self):
        settings = get_runtime()
        self.assertFalse(settings.configured)
        self.assertEqual(settings.source_root, self.release)
        self.assertEqual(settings.state_dir, self.release)
        self.assertEqual(settings.queue_path, self.release / "cases" / "requests.db")
        self.assertEqual(settings.output_dir, self.release / "cases" / "drafts")
        self.assertEqual(settings.monitor_dir, self.release / "monitor")
        self.assertEqual(settings.langflow_database, self.release.parents[2] / ".codex" / "data" / "langflow.db")
        self.assertEqual(settings.langflow_url, "http://127.0.0.1:7860")
        self.assertEqual(settings.flow_id, FLOW_ID)
        self.assertIsNone(settings.api_key_env)
        self.assertEqual(list(self.release.iterdir()), [])

    def test_external_config_resolves_all_state_paths_without_creating_them(self):
        expected = self.configured()
        os.environ["SATINALMA_CONFIG"] = str(self.config)
        settings = get_runtime()
        self.assertEqual(settings, expected)
        self.assertTrue(settings.configured)
        self.assertEqual(settings.queue_path, self.state / "cases" / "requests.db")
        self.assertEqual(settings.output_dir, self.state / "cases" / "drafts")
        self.assertEqual(settings.monitor_dir, self.state / "monitor")
        self.assertEqual(settings.config_path, self.config)
        self.assertIsNone(settings.langflow_database)
        self.assertFalse(self.state.exists())
        self.assertEqual(list(self.release.iterdir()), [])

    def test_settings_never_read_key_value_until_session_creation(self):
        self.configured()

        class ObservedEnvironment(dict):
            def get(self, key, default=None):
                if key == "PILOT_LANGFLOW_API_KEY":
                    raise AssertionError("Config loading must not read a key")
                return super().get(key, default)

        with patch("runtime_config.os.environ", ObservedEnvironment(SATINALMA_CONFIG=str(self.config))):
            settings = get_runtime()
        self.assertEqual(settings.api_key_env, "PILOT_LANGFLOW_API_KEY")

    def test_null_flow_id_is_allowed_for_preflight_only(self):
        data = self.data()
        data["langflow"]["flow_id"] = None
        settings = self.configured(data)
        self.assertIsNone(settings.flow_id)
        with patch("langflow_client.session_for") as connect, self.assertRaises(LangflowClientError):
            fetch_flow(settings)
        connect.assert_not_called()

    def test_database_path_is_optional_absolute_and_never_created(self):
        data = self.data()
        database = self.root / "langflow" / "langflow.db"
        data["langflow"]["database_path"] = str(database)
        settings = self.configured(data)
        self.assertEqual(settings.langflow_database_path, database)
        self.assertFalse(database.exists())
        data["langflow"]["database_path"] = "relative/langflow.db"
        with self.assertRaises(RuntimeConfigurationError):
            self.configured(data)

    def test_state_must_be_absolute_and_outside_release_including_symlinks(self):
        alias = self.root / "release-alias"
        alias.symlink_to(self.release, target_is_directory=True)
        for value in (None, "", "relative/state", str(self.release), str(self.release / "state"), str(alias / "state")):
            with self.subTest(value=value), self.assertRaises(RuntimeConfigurationError):
                data = self.data()
                data["state_dir"] = value
                self.configured(data)
        not_a_directory = self.root / "state-file"
        not_a_directory.write_text("fixture", encoding="utf-8")
        data = self.data()
        data["state_dir"] = str(not_a_directory)
        with self.assertRaises(RuntimeConfigurationError):
            self.configured(data)

    def test_state_env_must_agree_and_cannot_silently_override_legacy(self):
        settings = self.configured()
        os.environ["SATINALMA_CONFIG"] = str(self.config)
        os.environ["SATINALMA_STATE_DIR"] = str(self.state)
        self.assertEqual(get_runtime(), settings)
        os.environ["SATINALMA_STATE_DIR"] = str(self.root / "different")
        with self.assertRaises(RuntimeConfigurationError):
            get_runtime()
        os.environ.pop("SATINALMA_CONFIG")
        with self.assertRaises(RuntimeConfigurationError):
            get_runtime()
        os.environ["SATINALMA_STATE_DIR"] = str(self.release)
        self.assertFalse(get_runtime().configured)

    def test_packaged_release_boundary_includes_parent_of_agent_directory(self):
        package = self.root / "portable-release"
        agent = package / "agent"
        agent.mkdir(parents=True)
        manifest = package / "MANIFEST.json"
        manifest.write_text("not parsed; only presence identifies the release", encoding="utf-8")
        with patch("runtime_config.SOURCE_ROOT", agent):
            self.assertEqual(self.configured().state_dir, self.state)
            for location in (package, package / "state", agent / "state"):
                with self.subTest(location=location.name), self.assertRaises(RuntimeConfigurationError):
                    data = self.data()
                    data["state_dir"] = str(location)
                    self.configured(data)
            with self.assertRaises(RuntimeConfigurationError), patch(
                    "runtime_config.Path.open", side_effect=AssertionError("Missing config cannot open a file")):
                get_runtime()
        self.assertFalse((package / "state").exists())
        self.assertFalse((agent / "cases").exists())

    def test_packaged_config_must_stay_outside_whole_release_including_symlink(self):
        package = self.root / "portable-release"
        agent = package / "agent"
        agent.mkdir(parents=True)
        (package / "MANIFEST.json").write_text("{}", encoding="utf-8")
        inside = package / "runtime.json"
        inside.write_text(json.dumps(self.data()), encoding="utf-8")
        alias = self.root / "config-link.json"
        alias.symlink_to(inside)
        with patch("runtime_config.SOURCE_ROOT", agent):
            for location in (inside, alias):
                with self.subTest(location=location.name), self.assertRaises(RuntimeConfigurationError), patch(
                        "runtime_config.Path.open", side_effect=AssertionError("Package config must not be read")):
                    get_runtime(location)

    def test_agent_directory_without_manifest_keeps_development_boundary(self):
        release = self.root / "checkout"
        agent = release / "agent"
        agent.mkdir(parents=True)
        data = self.data()
        data["state_dir"] = str(release / "external-to-agent")
        with patch("runtime_config.SOURCE_ROOT", agent):
            self.assertEqual(self.configured(data).state_dir, release / "external-to-agent")
            self.assertFalse(get_runtime().configured)
            self.assertEqual(get_runtime().state_dir, agent)

    def test_packaged_ingest_without_config_stops_before_source_read_or_queue_creation(self):
        package = self.root / "ingest-package"
        agent = package / "agent"
        agent.mkdir(parents=True)
        (package / "MANIFEST.json").write_text("{}", encoding="utf-8")
        source_folder = Path(__file__).resolve().parent
        for filename in ("runtime_config.py", "case_worker.py", "case_store.py"):
            (agent / filename).write_bytes((source_folder / filename).read_bytes())
        source = self.root / "synthetic-request.json"
        source.write_text(json.dumps({"source": "manual", "request": {
            "id": "SYNTHETIC-REQUEST", "material_code": "SYNTHETIC-MAT", "material_name": "Sentetik sarf",
            "quantity": "1", "unit": "KG", "requester": "owner@example.invalid", "required_specs": {}}}))
        script = """
import os,runpy,sys
from pathlib import Path
from runtime_config import RuntimeConfigurationError
source=Path(sys.argv[1])
worker=Path.cwd()/'case_worker.py'
def guard(event,arguments):
    if event=='open' and isinstance(arguments[0],(str,bytes,os.PathLike)):
        if Path(os.fsdecode(arguments[0])).absolute()==source:
            raise AssertionError('Request source must not be decoded before configuration')
sys.addaudithook(guard)
sys.argv=[str(worker),'ingest-request','--file',str(source)]
try:
    runpy.run_path(str(worker),run_name='__main__')
except RuntimeConfigurationError:
    print('package_config_required')
    raise SystemExit(2)
"""
        result = subprocess.run([sys.executable, "-c", script, str(source)], cwd=agent,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout.strip(), "package_config_required")
        self.assertEqual(result.stderr, "")
        self.assertFalse((agent / "cases").exists())
        self.assertFalse((agent / "monitor").exists())
        self.assertFalse((package / "cases").exists())

    def test_exact_schema_rejects_unknown_missing_and_secret_value_fields(self):
        for mutate in (lambda data: data.update(api_key="SYNTHETIC_SECRET"),
                       lambda data: data["langflow"].update(api_key="SYNTHETIC_SECRET"),
                       lambda data: data.pop("state_dir"),
                       lambda data: data["langflow"].pop("database_path")):
            data = self.data()
            mutate(data)
            with self.subTest(data_keys=list(data)), self.assertRaises(RuntimeConfigurationError) as error:
                self.configured(data)
            self.assertNotIn("SYNTHETIC_SECRET", str(error.exception))

    def test_strict_json_rejects_duplicate_nonfinite_deep_and_oversized_inputs(self):
        deep = None
        for _ in range(12):
            deep = [deep]
        examples = ('{"schema_version":1,"schema_version":1}', '{"schema_version":NaN}',
                    '{"schema_version":Infinity}', '{"schema_version":-Infinity}',
                    '{"schema_version":1e999}',
                    json.dumps({"deep": deep}), " " * (runtime_config.MAX_CONFIG_BYTES + 1),
                    '{"state_dir":"SYNTHETIC_SECRET"')
        for raw in examples:
            with self.subTest(length=len(raw)):
                self.config.write_text(raw, encoding="utf-8")
                with self.assertRaises(RuntimeConfigurationError) as error:
                    get_runtime(self.config)
                self.assertNotIn("SYNTHETIC_SECRET", str(error.exception))
        self.config.write_bytes(b"\xff")
        with self.assertRaises(RuntimeConfigurationError):
            get_runtime(self.config)

    def test_config_file_must_be_explicit_absolute_and_readable(self):
        for path in ("", "relative/config.json", self.root / "missing.json", self.release):
            with self.subTest(path=type(path).__name__), self.assertRaises(RuntimeConfigurationError):
                get_runtime(path)

    def test_schema_version_env_name_and_flow_id_are_strict(self):
        for key, values in (("schema_version", (True, 1.0, 2, "1")),
                            ("api_key_env", (None, "", "KEY=value", "not-a-name", " KEY", "KEY\n")),
                            ("flow_id", ("", "not-a-uuid", FLOW_ID.replace("-", ""), True))):
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(RuntimeConfigurationError):
                    data = self.data()
                    if key == "schema_version":
                        data[key] = value
                    else:
                        data["langflow"][key] = value
                    self.configured(data)

    def test_urls_allow_https_and_http_loopback_without_auth_or_query(self):
        for url in ("https://example.org/", "https://example.org/langflow/", "http://127.0.0.1:7860",
                    "http://localhost:7860", "http://[::1]:7860"):
            with self.subTest(url=url):
                data = self.data()
                data["langflow"]["base_url"] = url
                self.assertEqual(self.configured(data).langflow_url, url.rstrip("/"))
        for url in ("http://example.org", "http://192.168.1.10", "http://127.0.0.1.example.org",
                    "https://user:password@example.org", "https://example.org?token=SYNTHETIC_SECRET",
                    "https://example.org#fragment", "https://example.org?", "https://example.org#",
                    "https://example.org:99999", "ftp://example.org", "https://example.org\\other", "https://example.org\n"):
            with self.subTest(url=url), self.assertRaises(RuntimeConfigurationError) as error:
                data = self.data()
                data["langflow"]["base_url"] = url
                self.configured(data)
            self.assertNotIn("SYNTHETIC_SECRET", str(error.exception))


class LangflowClientTests(RuntimeFixture):
    def test_invalid_flow_identity_is_rejected_before_authentication(self):
        settings = self.configured()
        for value in (None, "../another-flow", True, "not-a-uuid"):
            with self.subTest(value=value), patch("langflow_client.session_for") as connect:
                with self.assertRaises(LangflowClientError):
                    fetch_flow(replace(settings, flow_id=value))
                connect.assert_not_called()

    def test_configured_auth_reads_named_key_without_login_or_network(self):
        settings = self.configured()
        os.environ[settings.api_key_env] = "synthetic-test-key"
        with patch("requests.sessions.Session.request", side_effect=AssertionError("No HTTP expected")) as transport:
            session = session_for(settings)
        transport.assert_not_called()
        self.assertEqual(session.headers["x-api-key"], "synthetic-test-key")
        self.assertNotIn("Authorization", session.headers)
        self.assertFalse(session.trust_env)
        session.close()

    def test_missing_or_invalid_key_never_falls_back_to_auto_login(self):
        settings = self.configured()
        for value in (None, "", "synthetic-test-key\r\nBcc:another", " spaced "):
            if value is None:
                os.environ.pop(settings.api_key_env, None)
            else:
                os.environ[settings.api_key_env] = value
            with self.subTest(value_type=type(value).__name__), patch("langflow_client._LangflowSession.get") as request:
                with self.assertRaises(LangflowClientError) as error:
                    session_for(settings)
                request.assert_not_called()
                self.assertNotIn("synthetic-test-key", str(error.exception))

    def test_only_unconfigured_legacy_uses_local_auto_login(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"access_token": "synthetic-access-token"}
        with patch("langflow_client._LangflowSession.get", return_value=response) as request:
            session = session_for(get_runtime())
        request.assert_called_once_with("http://127.0.0.1:7860/api/v1/auto_login", timeout=10)
        self.assertEqual(session.headers["Authorization"], "Bearer synthetic-access-token")
        session.close()
        settings = replace(get_runtime(), langflow_url="https://example.org")
        with patch("langflow_client._LangflowSession.get") as request, self.assertRaises(LangflowClientError):
            session_for(settings)
        request.assert_not_called()

    def test_login_failures_do_not_expose_response_or_exception_secrets(self):
        with patch("langflow_client._LangflowSession.get", side_effect=requests.ConnectionError("SYNTHETIC_SECRET")):
            with self.assertRaises(LangflowClientError) as error:
                session_for(get_runtime())
        self.assertNotIn("SYNTHETIC_SECRET", str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)

    def test_authenticated_session_refuses_other_origin_and_disables_redirects(self):
        settings = self.configured()
        os.environ[settings.api_key_env] = "synthetic-test-key"
        session = session_for(settings)
        self.addCleanup(session.close)
        with patch("requests.sessions.Session.request", return_value=MagicMock(status_code=200)) as transport:
            with self.assertRaises(LangflowClientError):
                session.get("https://other.example.org/api/v1/flows/")
            transport.assert_not_called()
            session.get(settings.langflow_url + "/health", allow_redirects=True)
            self.assertIs(transport.call_args.kwargs["allow_redirects"], False)

    def test_fetch_reads_exact_flow_and_preserves_only_safe_provider_references(self):
        settings = self.configured()
        raw = {"id": FLOW_ID, "data": {"nodes": [{"data": {"node": {"template": {
            "model": {"value": [{"provider": "OpenAI"}]},
            "api_key": {"value": "SYNTHETIC_SECRET_OPENAI", "password": True, "load_from_db": False},
            "bearer_token": {"value": "SYNTHETIC_SECRET_BEARER", "password": True, "load_from_db": True},
            "visible": {"value": "normal input"}}}}}, {"data": {"node": {"template": {
                "model": {"value": [{"provider": "Anthropic"}]},
                "api_key": {"value": "SYNTHETIC_SECRET_CLAUDE", "password": True},
                "unknown_key": {"value": "SYNTHETIC_SECRET_OTHER", "password": True}}}}}], "edges": []},
            "headers": {"Cookie": "SYNTHETIC_SECRET_COOKIE"}, "access_token": ["SYNTHETIC_SECRET_TOKEN"]}
        before = copy.deepcopy(raw)
        response = MagicMock(status_code=200, content=json.dumps(raw).encode())
        response.json.return_value = raw
        session = MagicMock()
        session.get.return_value = response
        with patch("langflow_client.session_for", return_value=session):
            result = fetch_flow(settings)
        session.get.assert_called_once_with(settings.langflow_url + "/api/v1/flows/" + FLOW_ID, timeout=15)
        session.close.assert_called_once()
        self.assertEqual(raw, before)
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))
        templates = [node["data"]["node"]["template"] for node in result["data"]["nodes"]]
        self.assertEqual(templates[0]["api_key"]["value"], "OPENAI_API_KEY")
        self.assertEqual(templates[1]["api_key"]["value"], "ANTHROPIC_API_KEY")
        self.assertIs(templates[0]["api_key"]["password"], True)
        self.assertIs(templates[0]["api_key"]["load_from_db"], True)
        self.assertEqual(templates[0]["bearer_token"]["value"], "")
        self.assertFalse(templates[0]["bearer_token"]["load_from_db"])
        self.assertEqual(templates[0]["visible"]["value"], "normal input")

    def test_fetch_errors_never_use_local_flow_fallback_or_expose_body(self):
        settings = self.configured()
        for response in (MagicMock(status_code=401, content=b"SYNTHETIC_SECRET"),
                         MagicMock(status_code=302, content=b"SYNTHETIC_SECRET"),
                         MagicMock(status_code=200, content=b"SYNTHETIC_SECRET")):
            response.json.return_value = {"id": "another-flow", "data": {"nodes": [], "edges": []}}
            session = MagicMock()
            session.get.return_value = response
            with self.subTest(status=response.status_code), patch("langflow_client.session_for", return_value=session):
                with self.assertRaises(LangflowClientError) as error:
                    fetch_flow(settings)
                self.assertNotIn("SYNTHETIC_SECRET", str(error.exception))
            session.get.assert_called_once()
            session.close.assert_called_once()

    def test_unknown_password_and_nested_credential_shapes_are_cleared(self):
        data = {"client_secret": "SYNTHETIC_SECRET", "credentials": {"arbitrary": "SYNTHETIC_SECRET"},
                "field": {"password": True, "value": "SYNTHETIC_SECRET", "load_from_db": True}}
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(safe_flow(data)))


if __name__ == "__main__":
    unittest.main()
