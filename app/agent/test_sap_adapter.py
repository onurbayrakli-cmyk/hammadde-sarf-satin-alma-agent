"""Explicit-origin SAP reads tested with synthetic DNS/TLS responses only."""

import json
import os
import socket
import ssl
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from sap_adapter import SAPReadError, fetch_metadata, fetch_requisition
from sap_connection import MAX_CONFIG_BYTES, SAPConfigurationError, load_connection, preflight


def connection_document():
    return {
        "schema_version": 1,
        "enabled": True,
        "source_system": "SYNTHETIC_DEV_200",
        "origin": "https://sap.internal:4300",
        "service_root": "/sap/opu/odata4/sap/pr/0001/",
        "entity_set": "PurchaseRequisitionItems",
        "sap_client": "200",
        "allowed_networks": ["10.42.0.0/16"],
        "auth": {"mode": "none", "username_env": None, "password_env": None, "bearer_env": None},
        "tls": {"ca_file": None},
        "field_map": {},
        "max_response_bytes": 1_048_576,
        "timeout_seconds": 8,
    }


class FakeResponse:
    def __init__(self, body=b'{"value":[]}', status=200, content_type="application/json", headers=None):
        self.body, self.status = body, status
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.closed, self.offset = False, 0

    def read1(self, amount, *, decode_content):
        if decode_content:
            raise AssertionError("Response decompression must stay disabled")
        chunk = self.body[self.offset : self.offset + amount]
        self.offset += len(chunk)
        return chunk

    def close(self):
        self.closed = True


class SAPAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name).resolve()
        self.config_file = self.folder / "connection.json"

    def tearDown(self):
        self.temporary.cleanup()

    def config(self, document=None):
        self.config_file.write_text(json.dumps(connection_document() if document is None else document))
        return load_connection(self.config_file)

    @staticmethod
    def dns(*addresses):
        return [
            (socket.AF_INET6 if ":" in value else socket.AF_INET, socket.SOCK_STREAM, 6, "", (value, 4300))
            for value in addresses
        ]

    def request(self, connection=None, response=None, *, metadata=False):
        connection = connection or self.config()
        response = response or FakeResponse()
        pool = Mock()
        pool.urlopen.return_value = response
        with (
            patch("sap_adapter.socket.getaddrinfo", return_value=self.dns("10.42.0.7")) as resolver,
            patch("sap_adapter.urllib3.HTTPSConnectionPool", return_value=pool) as factory,
        ):
            result = fetch_metadata(connection) if metadata else fetch_requisition(connection, "0000123456", "00010")
        return result, pool, factory, resolver

    def test_empty_setup_is_valid_but_disabled_and_never_dispatches(self):
        document = connection_document()
        document.update(
            enabled=False, origin=None, service_root=None, entity_set=None, sap_client=None, allowed_networks=[]
        )
        connection = self.config(document)
        with (
            patch("sap_adapter.socket.getaddrinfo") as dns,
            patch("sap_adapter.ssl.create_default_context") as tls,
            patch("sap_adapter.os.environ.get", side_effect=AssertionError("No secret read")),
        ):
            result = preflight(connection)
            with self.assertRaises(SAPReadError) as failure:
                fetch_metadata(connection)
        self.assertEqual(result["status"], "disabled")
        self.assertFalse(result["performed_api_call"])
        self.assertFalse(failure.exception.request_attempted)
        dns.assert_not_called()
        tls.assert_not_called()

    def test_load_and_preflight_do_not_resolve_dns_or_read_credential_values(self):
        document = connection_document()
        document["auth"] = {
            "mode": "basic",
            "username_env": "TEST_SAP_USERNAME",
            "password_env": "TEST_SAP_PASSWORD",
            "bearer_env": None,
        }
        with (
            patch("socket.getaddrinfo") as dns,
            patch("sap_adapter.os.environ.get", side_effect=AssertionError("No secret read")),
        ):
            result = preflight(self.config(document))
        self.assertEqual(result["status"], "configured")
        self.assertFalse(result["sap_connectivity_verified"])
        dns.assert_not_called()

    def test_field_map_defaults_are_immutable_and_unknown_business_fields_stay_missing(self):
        mapping = self.config().field_map
        self.assertEqual(mapping["purchase_requisition"], "PurchaseRequisition")
        self.assertIsNone(mapping["released"])
        self.assertIsNone(mapping["requester_email"])
        with self.assertRaises(TypeError):
            mapping["released"] = "InventedApproval"

    def test_exact_schema_rejects_unknown_missing_and_duplicate_fields(self):
        for mutate in (
            lambda value: value.update(password="synthetic credential"),
            lambda value: value.pop("enabled"),
            lambda value: value["tls"].update(verify=False),
            lambda value: value["auth"].update(password="synthetic credential"),
        ):
            document = connection_document()
            mutate(document)
            with self.subTest(document=document), self.assertRaises(SAPConfigurationError):
                self.config(document)
        self.config_file.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(SAPConfigurationError):
            load_connection(self.config_file)

    def test_config_rejects_nonfinite_deep_and_oversized_json(self):
        for raw in (
            '{"x":NaN}',
            '{"x":1e999}',
            '{"x":Infinity}',
            "[" * 20 + "0" + "]" * 20,
            " " * (MAX_CONFIG_BYTES + 1),
        ):
            self.config_file.write_text(raw)
            with self.subTest(sample=raw[:20]), self.assertRaises(SAPConfigurationError):
                load_connection(self.config_file)

    def test_origin_rejects_authentication_paths_fragments_and_unsafe_targets(self):
        values = [
            "http://sap.internal",
            "https://person:credential@sap.internal",
            "https://sap.internal/path",
            "https://sap.internal?x=1",
            "https://sap.internal#",
            "https://sap.internal:0",
            "https://sap.internal:",
            "https://localhost",
            "https://app.localhost",
            "https://127.0.0.1",
            "https://[::1]",
            "https://169.254.169.254",
            "https://[fd00:ec2::254]",
            "https://metadata.google.internal",
            "https://sap.internal.",
            "https://sap%2einternal",
            "https://sap.internal\\other",
        ]
        for value in values:
            document = connection_document()
            document["origin"] = value
            with self.subTest(origin=value), self.assertRaises(SAPConfigurationError):
                self.config(document)

    def test_service_path_rejects_traversal_encoding_and_query(self):
        for value in (
            "/other/",
            "/sap/../admin/",
            "/sap/./api/",
            "/sap/%2e%2e/api/",
            "/sap//api/",
            "/sap/api?x=1/",
            "/sap/api#fragment/",
            "/sap/api\\other/",
            "sap/api/",
            "/sap/api",
        ):
            document = connection_document()
            document["service_root"] = value
            with self.subTest(path=value), self.assertRaises(SAPConfigurationError):
                self.config(document)

    def test_entity_and_mapping_only_accept_direct_identifiers(self):
        for field, value in (
            ("entity_set", "Items?$expand=Secrets"),
            ("entity_set", "../Items"),
            ("field_map", {"released": "Status/Released"}),
            ("field_map", {"requester_email": "Material"}),
            ("field_map", {"unknown": "SomeField"}),
        ):
            document = connection_document()
            document[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(SAPConfigurationError):
                self.config(document)

    def test_auth_refs_require_exact_mode_and_environment_names(self):
        bad = [
            {"mode": "basic", "username_env": "TEST_USER", "password_env": None, "bearer_env": None},
            {"mode": "none", "username_env": None, "password_env": None, "bearer_env": "TEST_TOKEN"},
            {"mode": "bearer", "username_env": None, "password_env": None, "bearer_env": "token with spaces"},
            {"mode": "sso", "username_env": None, "password_env": None, "bearer_env": None},
            {"mode": [], "username_env": None, "password_env": None, "bearer_env": None},
            {"mode": {}, "username_env": None, "password_env": None, "bearer_env": None},
        ]
        for auth in bad:
            document = connection_document()
            document["auth"] = auth
            with self.subTest(auth=auth), self.assertRaises(SAPConfigurationError):
                self.config(document)

    def test_limits_types_client_and_network_list_are_strict(self):
        values = [
            ("enabled", 1),
            ("schema_version", True),
            ("max_response_bytes", True),
            ("max_response_bytes", 1_048_577),
            ("timeout_seconds", 0),
            ("timeout_seconds", 31),
            ("sap_client", 200),
            ("sap_client", "200?x=1"),
            ("allowed_networks", ["0.0.0.0/0"]),
            ("allowed_networks", ["10.42.0.1/16"]),
            ("allowed_networks", ["10.42.0.0/16", "10.42.0.0/16"]),
        ]
        for field, value in values:
            document = connection_document()
            document[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(SAPConfigurationError):
                self.config(document)

    def test_unknown_connection_fields_fail_before_dns_or_auth(self):
        for field, value in (
            ("origin", "http://sap.internal"),
            ("service_root", "/outside/"),
            ("entity_set", "../Other"),
            ("enabled", 1),
        ):
            connection = replace(self.config(), **{field: value})
            with (
                self.subTest(field=field),
                patch("sap_adapter.socket.getaddrinfo") as resolver,
                patch("sap_adapter.os.environ.get") as credentials,
            ):
                with self.assertRaises(SAPReadError):
                    fetch_metadata(connection)
                resolver.assert_not_called()
                credentials.assert_not_called()

    def test_all_dns_answers_must_be_allowed_before_loading_credentials(self):
        for addresses in (
            ("10.42.0.7", "10.43.0.8"),
            ("10.42.0.7", "127.0.0.1"),
            ("10.42.0.7", "169.254.169.254"),
            ("::1",),
            ("fd00:ec2::254",),
            ("224.0.0.1",),
            (),
        ):
            with (
                self.subTest(addresses=addresses),
                patch("sap_adapter.socket.getaddrinfo", return_value=self.dns(*addresses)),
                patch("sap_adapter.os.environ.get") as credentials,
                patch("sap_adapter.urllib3.HTTPSConnectionPool") as factory,
            ):
                with self.assertRaises(SAPReadError) as failure:
                    fetch_metadata(self.config())
                self.assertFalse(failure.exception.request_attempted)
                credentials.assert_not_called()
                factory.assert_not_called()

    def test_pinned_ip_original_hostname_and_custom_port_verify_tls(self):
        payload, pool, factory, resolver = self.request()
        self.assertEqual(payload, {"value": []})
        self.assertEqual(factory.call_args.args, ("10.42.0.7", 4300))
        kwargs = factory.call_args.kwargs
        self.assertEqual(kwargs["server_hostname"], "sap.internal")
        self.assertEqual(kwargs["assert_hostname"], "sap.internal")
        self.assertEqual(kwargs["ssl_context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(kwargs["ssl_context"].check_hostname)
        self.assertFalse(kwargs["retries"])
        self.assertEqual(resolver.call_count, 1)
        self.assertEqual(pool.urlopen.call_args.args[0], "GET")
        self.assertEqual(pool.urlopen.call_args.kwargs["headers"]["Host"], "sap.internal:4300")
        self.assertFalse(pool.urlopen.call_args.kwargs["redirect"])
        self.assertFalse(pool.urlopen.call_args.kwargs["retries"])
        pool.close.assert_called_once()

    def test_literal_allowed_ip_does_not_resolve_a_second_name(self):
        document = connection_document()
        document["origin"] = "https://10.42.0.7:4300"
        _, _, factory, resolver = self.request(self.config(document))
        resolver.assert_not_called()
        self.assertEqual(factory.call_args.kwargs["assert_hostname"], "10.42.0.7")

    def test_query_preserves_identity_and_only_selects_mapped_fields(self):
        _, pool, _, _ = self.request()
        target = pool.urlopen.call_args.args[1]
        parsed = urlsplit(target)
        self.assertEqual(parsed.path, "/sap/opu/odata4/sap/pr/0001/PurchaseRequisitionItems")
        query = parse_qs(parsed.query)
        self.assertEqual(
            query["$filter"], ["PurchaseRequisition eq '0000123456' and PurchaseRequisitionItem eq '00010'"]
        )
        self.assertEqual(query["$top"], ["2"])
        self.assertEqual(query["sap-client"], ["200"])
        self.assertNotIn("$expand", query)
        self.assertNotIn("None", query["$select"][0])

    def test_id_injection_and_numeric_identity_fail_before_network(self):
        for pr, item in ((12345, "00010"), ("0000123456' or true", "00010"), ("0000123456", "../0010"), ("", "00010")):
            with self.subTest(pr=pr, item=item), patch("sap_adapter.socket.getaddrinfo") as resolver:
                with self.assertRaises(SAPReadError):
                    fetch_requisition(self.config(), pr, item)
                resolver.assert_not_called()

    def test_metadata_is_one_get_with_no_followup_or_credentials_by_default(self):
        response = FakeResponse(b'<edmx:Edmx xmlns:edmx="urn:synthetic"/>', content_type="application/xml")
        content, pool, _, _ = self.request(response=response, metadata=True)
        self.assertEqual(content, response.body)
        self.assertEqual(pool.urlopen.call_count, 1)
        self.assertEqual(urlsplit(pool.urlopen.call_args.args[1]).path, "/sap/opu/odata4/sap/pr/0001/$metadata")
        self.assertNotIn("Authorization", pool.urlopen.call_args.kwargs["headers"])
        self.assertTrue(response.closed)

    def test_basic_and_bearer_are_read_only_at_dispatch(self):
        document = connection_document()
        document["auth"] = {
            "mode": "basic",
            "username_env": "TEST_SAP_USERNAME",
            "password_env": "TEST_SAP_PASSWORD",
            "bearer_env": None,
        }
        with patch.dict("os.environ", {"TEST_SAP_USERNAME": "synthetic-user", "TEST_SAP_PASSWORD": "synthetic-value"}):
            _, pool, _, _ = self.request(self.config(document))
        self.assertTrue(pool.urlopen.call_args.kwargs["headers"]["Authorization"].startswith("Basic "))
        document["auth"] = {
            "mode": "bearer",
            "username_env": None,
            "password_env": None,
            "bearer_env": "TEST_SAP_TOKEN",
        }
        with patch.dict("os.environ", {"TEST_SAP_TOKEN": "synthetic-value"}):
            _, pool, _, _ = self.request(self.config(document))
        self.assertEqual(pool.urlopen.call_args.kwargs["headers"]["Authorization"], "Bearer synthetic-value")

    def test_missing_or_header_injected_credentials_fail_without_request(self):
        document = connection_document()
        document["auth"] = {
            "mode": "bearer",
            "username_env": None,
            "password_env": None,
            "bearer_env": "TEST_SAP_TOKEN",
        }
        connection = self.config(document)
        workdir = self.folder / "credential-check-cwd"
        workdir.mkdir()
        for value in (None, "", "value\r\nInjected: yes", "contains space"):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {} if value is None else {"TEST_SAP_TOKEN": value}),
                patch("sap_adapter.socket.getaddrinfo", return_value=self.dns("10.42.0.7")),
                patch("sap_adapter.urllib3.HTTPSConnectionPool") as factory,
            ):
                if value is None:
                    os.environ.pop("TEST_SAP_TOKEN", None)
                previous_cwd = Path.cwd()
                try:
                    os.chdir(workdir)
                    with self.assertRaises(SAPReadError) as failure:
                        fetch_metadata(connection)
                    # A broad os.environ.get mock previously made SSL treat
                    # the fake token as an SSLKEYLOGFILE and create files.
                    self.assertEqual(list(workdir.iterdir()), [])
                finally:
                    os.chdir(previous_cwd)
                self.assertFalse(failure.exception.request_attempted)
                factory.assert_not_called()

    def test_additional_ca_is_loaded_without_disabling_default_verification(self):
        document = connection_document()
        document["tls"]["ca_file"] = str(self.folder / "company-public-ca.pem")
        context = Mock()
        with patch("sap_adapter.ssl.create_default_context", return_value=context) as create:
            self.request(self.config(document))
        create.assert_called_once_with()
        context.load_verify_locations.assert_called_once_with(cafile=Path(document["tls"]["ca_file"]))

    def test_redirect_auth_failure_wrong_media_and_compression_are_rejected(self):
        responses = [
            FakeResponse(status=302, headers={"Location": "https://elsewhere.invalid/"}),
            FakeResponse(status=401),
            FakeResponse(status=500),
            FakeResponse(content_type="text/html"),
            FakeResponse(headers={"Content-Encoding": "gzip"}),
            FakeResponse(headers={"Content-Length": "-1"}),
            FakeResponse(headers={"Content-Length": "1048577"}),
            FakeResponse(headers={"Content-Length": "3"}),
        ]
        for response in responses:
            pool = Mock()
            pool.urlopen.return_value = response
            with (
                self.subTest(status=response.status, headers=response.headers),
                patch("sap_adapter.socket.getaddrinfo", return_value=self.dns("10.42.0.7")),
                patch("sap_adapter.urllib3.HTTPSConnectionPool", return_value=pool),
            ):
                with self.assertRaises(SAPReadError) as failure:
                    fetch_requisition(self.config(), "0000123456", "00010")
                self.assertTrue(failure.exception.request_attempted)
                self.assertEqual(pool.urlopen.call_count, 1)
                self.assertTrue(response.closed)

    def test_raw_and_json_limits_do_not_leak_bodies_or_error_details(self):
        document = connection_document()
        document["max_response_bytes"] = 64
        bodies = [b"x" * 65, b'{"value":[],"value":[]}', b'{"value":NaN}', b'{"value":1e999}', b"[]", b"not json", b""]
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(SAPReadError) as failure:
                self.request(self.config(document), FakeResponse(body))
            self.assertTrue(failure.exception.request_attempted)
            self.assertNotIn(body.decode(), str(failure.exception)) if body else None

    def test_stream_failure_is_unknown_after_dispatch_and_never_retried(self):
        response = FakeResponse()
        response.read1 = Mock(side_effect=RuntimeError("synthetic credential and response text"))
        pool = Mock()
        pool.urlopen.return_value = response
        with (
            patch("sap_adapter.socket.getaddrinfo", return_value=self.dns("10.42.0.7")),
            patch("sap_adapter.urllib3.HTTPSConnectionPool", return_value=pool),
        ):
            with self.assertRaises(SAPReadError) as failure:
                fetch_metadata(self.config())
        self.assertTrue(failure.exception.request_attempted)
        self.assertNotIn("synthetic credential", str(failure.exception))
        self.assertEqual(pool.urlopen.call_count, 1)
        self.assertTrue(response.closed)
        pool.close.assert_called_once()

    def test_response_read_deadline_is_bounded(self):
        with patch("sap_adapter.time.monotonic", side_effect=[1, 10]), self.assertRaises(SAPReadError):
            self.request()

    def test_slow_trickle_stops_between_single_reads_instead_of_waiting_for_full_chunk(self):
        elapsed = [0]
        response = FakeResponse()

        def trickle(amount, *, decode_content):
            self.assertEqual(amount, 16_384)
            self.assertFalse(decode_content)
            elapsed[0] += 1
            return b" "

        response.read1 = Mock(side_effect=trickle)
        with patch("sap_adapter.time.monotonic", side_effect=lambda: elapsed[0]), self.assertRaises(SAPReadError):
            self.request(response=response)
        self.assertEqual(response.read1.call_count, 8)
        self.assertTrue(response.closed)

    def test_next_link_is_preserved_but_not_followed(self):
        response = FakeResponse(b'{"value":[],"@odata.nextLink":"https://elsewhere.invalid/"}')
        payload, pool, _, _ = self.request(response=response)
        self.assertIn("@odata.nextLink", payload)
        self.assertEqual(pool.urlopen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
