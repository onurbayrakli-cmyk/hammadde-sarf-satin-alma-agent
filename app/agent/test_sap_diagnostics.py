"""Synthetic diagnostics tests: no SAP, provider, secret or business queue access."""

import contextlib
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import sap_diagnostics as diagnostic
from sap_connection import _validate

PR, ITEM = "0000123456", "00010"
MINIMUM = diagnostic.MINIMUM_FIELDS
OPTIONAL = diagnostic.OPTIONAL_FIELDS


def config():
    return {
        "schema_version": 1,
        "enabled": True,
        "source_system": "SYNTHETIC_DEV_200",
        "origin": "https://sap.example.invalid",
        "service_root": "/sap/opu/odata/sap/SYNTHETIC_PR_SRV/",
        "entity_set": "A_PurchaseRequisitionItem",
        "sap_client": "200",
        "allowed_networks": ["10.23.42.9/32"],
        "auth": {"mode": "none", "username_env": None, "password_env": None, "bearer_env": None},
        "tls": {"ca_file": None},
        "field_map": {},
        "max_response_bytes": 1048576,
        "timeout_seconds": 8,
    }


def metadata(*, optional=True, v2=False, alias=False, semantics=None, base=False, keys=None, missing=(), types=None):
    edm = "http://schemas.microsoft.com/ado/2008/09/edm" if v2 else "http://docs.oasis-open.org/odata/ns/edm"
    edmx = "http://schemas.microsoft.com/ado/2007/06/edmx" if v2 else "http://docs.oasis-open.org/odata/ns/edmx"
    kinds = {field: "Edm.Decimal" if field in MINIMUM[4:6] else "Edm.String" for field in MINIMUM}
    if optional:
        kinds.update({field: "Edm.Boolean" if field == "IsClosed" else "Edm.String" for field in OPTIONAL})
    kinds.update(types or {})
    props = "".join(
        f'<Property Name="{field}" Type="{kind}"/>' for field, kind in kinds.items() if field not in missing
    )
    keys = MINIMUM[:2] if keys is None else keys
    key = "<Key>" + "".join(f'<PropertyRef Name="{name}"/>' for name in keys) + "</Key>"
    semantics_xml = "" if semantics is None else f' sap:semantics="{semantics}"'
    base_xml = ' BaseType="Pilot.Base"' if base else ""
    base_type = '<EntityType Name="Base"><Property Name="Other" Type="Edm.String"/></EntityType>' if base else ""
    return (
        f'<edmx:Edmx xmlns:edmx="{edmx}" xmlns:sap="http://www.sap.com/Protocols/SAPData" Version="4.0">'
        f'<edmx:DataServices><Schema xmlns="{edm}" Namespace="Pilot"' + (' Alias="P"' if alias else "") + ">"
        f'{base_type}<EntityType Name="Item"{semantics_xml}{base_xml}>{key}{props}</EntityType>'
        '<EntityContainer Name="Container"><EntitySet Name="A_PurchaseRequisitionItem" '
        f'EntityType="{"P" if alias else "Pilot"}.Item"/></EntityContainer>'
        "</Schema></edmx:DataServices></edmx:Edmx>"
    ).encode()


def row(*, optional=True):
    result = {
        "PurchaseRequisition": PR,
        "PurchaseRequisitionItem": ITEM,
        "Material": "SYNTHETIC-SARF-01",
        "PurchaseRequisitionItemText": "Sentetik sarf",
        "RequestedQuantity": "12.500",
        "OrderedQuantity": "3.000",
        "BaseUnit": "KG",
    }
    if optional:
        result.update({field: False if field == "IsClosed" else "" for field in OPTIONAL})
        result.update(PurReqnReleaseStatus="02", ProcessingStatus="B", Plant="S001")
    return result


def raw_response(*, v2=False, value=None):
    items = [row()] if value is None else value
    return json.dumps({"d": {"results": items}} if v2 else {"value": items}, ensure_ascii=False).encode()


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name).resolve()
        self.output = self.folder / "private-report.json"
        self.config_path = self.folder / "config.json"
        self.config_path.write_text(json.dumps(config()))
        self.connection = _validate(config())
        self.guards = [
            patch("socket.getaddrinfo", side_effect=AssertionError("network forbidden")),
            patch("socket.create_connection", side_effect=AssertionError("network forbidden")),
            patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
        ]
        for guard in self.guards:
            guard.start()

    def tearDown(self):
        for guard in reversed(self.guards):
            guard.stop()
        self.temp.cleanup()

    def sample(self, meta=None, raw=None):
        return diagnostic.diagnose_sample(
            self.connection,
            PR,
            ITEM,
            self.output,
            metadata_reader=lambda _: metadata() if meta is None else meta,
            sample_reader=lambda *_: raw_response() if raw is None else raw,
        )

    def assert_no_report(self):
        self.assertFalse(self.output.exists())
        self.assertFalse(any(path.name.startswith(".sap-source-") for path in self.folder.iterdir()))

    def test_schema_catalog_report_is_private_without_business_acceptance(self):
        raw = metadata(semantics="aggregate")
        result = diagnostic.diagnose_schema(self.connection, self.output, metadata_reader=lambda _: raw)
        saved = json.loads(self.output.read_text())
        self.assertEqual(saved["catalog"]["entity_sets"][0]["semantics"], "aggregate")
        self.assertEqual(saved["metadata_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertFalse(saved["requisition_read_verified"])
        self.assertNotIn("catalog", result)
        self.assertEqual(saved["intake_eligible"], "not_evaluated")
        for key, value in diagnostic.FLAGS.items():
            self.assertEqual(saved[key], value)

    def test_raw_v2_mixed_flags_and_partially_ordered_sample_remain_uninterpreted(self):
        raw = raw_response(v2=True)
        result = self.sample(metadata(v2=True), raw)
        saved = json.loads(self.output.read_text())
        self.assertEqual(saved["source_record"], row())
        self.assertEqual(saved["source_record"]["IsDeleted"], "")
        self.assertIs(saved["source_record"]["IsClosed"], False)
        self.assertEqual(saved["source_record"]["OrderedQuantity"], "3.000")
        self.assertTrue(saved["requisition_read_verified"])
        self.assertEqual(saved["payload_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertNotIn("source_record", result)
        self.assertNotIn("request", saved)
        self.assertFalse(saved["source_file_created"])
        self.assertNotIn("requester_email", self.output.read_text())

    def test_minimum_v4_alias_sample_requires_no_optional_or_email_fields(self):
        result = self.sample(metadata(optional=False, alias=True), raw_response(value=[row(optional=False)]))
        saved = json.loads(self.output.read_text())
        self.assertEqual(saved["selected_fields"], list(MINIMUM))
        self.assertEqual(result["selected_field_count"], 7)
        self.assertFalse(result["business_semantics_verified"])

    def test_aggregate_parameters_inheritance_unknown_semantics_and_wrong_keys_prevent_data_call(self):
        bad = [
            metadata(semantics="aggregate"),
            metadata(semantics="parameters"),
            metadata(base=True),
            metadata(semantics="other"),
            metadata(keys=("Material",)),
            metadata(keys=MINIMUM[:3]),
        ]
        for raw in bad:
            data_reader = Mock(side_effect=AssertionError("data must not be read"))
            with self.subTest(raw=raw[:100]), self.assertRaises(diagnostic.SapDiagnosticError):
                diagnostic.diagnose_sample(
                    self.connection, PR, ITEM, self.output, metadata_reader=lambda _: raw, sample_reader=data_reader
                )
            data_reader.assert_not_called()
            self.assert_no_report()

    def test_required_types_missing_fields_and_optional_complex_types_are_rejected_before_data(self):
        bad = [
            metadata(missing=(MINIMUM[4],)),
            metadata(types={MINIMUM[0]: "Edm.Int64"}),
            metadata(types={MINIMUM[5]: "Edm.String"}),
            metadata(types={"IsDeleted": "Collection(Edm.String)"}),
            metadata(types={"Plant": "Edm.Boolean"}),
        ]
        for raw in bad:
            data_reader = Mock()
            with self.subTest(raw=raw[:80]), self.assertRaises(diagnostic.SapDiagnosticError):
                diagnostic.diagnose_sample(
                    self.connection, PR, ITEM, self.output, metadata_reader=lambda _: raw, sample_reader=data_reader
                )
            data_reader.assert_not_called()
            self.assert_no_report()

    def test_missing_entity_and_duplicate_schema_definitions_fail(self):
        for raw in [
            metadata().replace(b"A_PurchaseRequisitionItem", b"OtherItems"),
            metadata().replace(b'<Property Name="Material"', b'<Property Name="PurchaseRequisition"'),
        ]:
            with self.assertRaises(diagnostic.SapDiagnosticError):
                self.sample(raw)
            self.assert_no_report()

    def test_empty_duplicate_wrong_identity_and_ambiguous_shapes_fail(self):
        mismatch = row()
        mismatch["PurchaseRequisitionItem"] = "00020"
        bad = [
            raw_response(value=[]),
            raw_response(value=[row(), row()]),
            raw_response(value=[mismatch]),
            json.dumps({"value": [row()], "d": {"results": [row()]}}).encode(),
            json.dumps({"d": {"value": [row()]}}).encode(),
            b"[]",
        ]
        for raw in bad:
            with self.subTest(raw=raw[:60]), self.assertRaises(diagnostic.SapDiagnosticError):
                self.sample(raw=raw)
            self.assert_no_report()

    def test_next_delta_and_inconsistent_count_are_never_followed_or_published(self):
        for v2 in (False, True):
            for key, value in [
                ("@odata.nextLink", None),
                ("odata.nextLink", "elsewhere"),
                ("__next", "x"),
                ("@odata.deltaLink", "x"),
                ("odata.deltaLink", "x"),
                ("__delta", "x"),
                ("@odata.count", 2),
                ("odata.count", "0"),
                ("__count", True),
            ]:
                payload = {"d": {"results": [row()]}} if v2 else {"value": [row()]}
                (payload["d"] if v2 else payload)[key] = value
                with self.subTest(v2=v2, key=key), self.assertRaises(diagnostic.SapDiagnosticError):
                    self.sample(raw=json.dumps(payload).encode())
                self.assert_no_report()

    def test_matching_counts_and_odata_metadata_are_preserved_only_as_payload_hash(self):
        item = row()
        item["__metadata"] = {"uri": "https://unrelated.example.invalid/private-marker"}
        raw = json.dumps({"d": {"results": [item], "__count": "1"}}).encode()
        self.sample(raw=raw)
        self.assertNotIn("private-marker", self.output.read_text())
        self.assertNotIn("__metadata", self.output.read_text())

    def test_unrequested_personal_fields_and_missing_selected_fields_are_rejected(self):
        extra = row()
        extra["RequesterEmail"] = "sensitive@example.invalid"
        missing = row()
        del missing["IsClosed"]
        for value in (extra, missing):
            with self.assertRaises(diagnostic.SapDiagnosticError):
                self.sample(raw=raw_response(value=[value]))
            self.assert_no_report()

    def test_duplicate_json_nonfinite_oversized_truncated_and_nested_values_are_rejected(self):
        wrong_value = row()
        wrong_value["Material"] = {"nested": "invalid"}
        bad = [
            b'{"value":[],"value":[]}',
            b'{"value":NaN}',
            b"x" * 1048577,
            raw_response()[:-2],
            raw_response(value=[wrong_value]),
        ]
        for raw in bad:
            with self.subTest(size=len(raw)), self.assertRaises(diagnostic.SapDiagnosticError):
                self.sample(raw=raw)
            self.assert_no_report()

    def test_boolean_string_and_numeric_boolean_type_confusion_are_not_accepted(self):
        for field, value in [("IsClosed", "false"), ("IsDeleted", False), ("RequestedQuantity", True)]:
            item = row()
            item[field] = value
            with self.subTest(field=field), self.assertRaises(diagnostic.SapDiagnosticError):
                self.sample(raw=raw_response(value=[item]))
            self.assert_no_report()

    def test_numeric_strings_must_be_finite_and_minimum_fields_cannot_be_null(self):
        for value in ("NaN", "Infinity", "-Infinity", "", "not-a-number", None):
            item = row()
            item["RequestedQuantity"] = value
            with self.subTest(value=value), self.assertRaises(diagnostic.SapDiagnosticError):
                self.sample(raw=raw_response(value=[item]))
            self.assert_no_report()
        for field in ("BaseUnit", "Material", "PurchaseRequisitionItemText"):
            item = row()
            item[field] = None
            with self.subTest(field=field), self.assertRaises(diagnostic.SapDiagnosticError):
                self.sample(raw=raw_response(value=[item]))
            self.assert_no_report()

    def test_optional_unknown_null_values_are_preserved_without_inference(self):
        item = row()
        item["IsClosed"] = None
        item["PurReqnReleaseStatus"] = None
        self.sample(raw=raw_response(value=[item]))
        saved = json.loads(self.output.read_text())
        self.assertIsNone(saved["source_record"]["IsClosed"])
        self.assertIsNone(saved["source_record"]["PurReqnReleaseStatus"])
        self.assertEqual(saved["intake_eligible"], "not_evaluated")

    def test_unsafe_identities_fail_before_any_reader(self):
        for value in ["' or 1 eq 1", "a/b", "", "a\n", 12]:
            reader = Mock()
            with self.subTest(value=value), self.assertRaises(diagnostic.SapDiagnosticError):
                diagnostic.diagnose_sample(
                    self.connection, value, ITEM, self.output, metadata_reader=reader, sample_reader=reader
                )
            reader.assert_not_called()
            self.assert_no_report()

    def test_unsafe_or_existing_outputs_fail_before_readers_and_do_not_overwrite(self):
        self.output.write_text("keep me")
        linked = self.folder / "linked.json"
        linked.symlink_to(self.output)
        for output in (self.output, linked, Path("relative.json"), diagnostic.ROOT / "forbidden-report.json"):
            reader = Mock()
            with self.subTest(output=output.name), self.assertRaises(diagnostic.SapDiagnosticError):
                diagnostic.diagnose_schema(self.connection, output, metadata_reader=reader)
            reader.assert_not_called()
        self.assertEqual(self.output.read_text(), "keep me")

    def test_parent_symlink_and_manifest_package_paths_are_rejected(self):
        actual = self.folder / "actual"
        actual.mkdir()
        linked = self.folder / "linked"
        linked.symlink_to(actual, target_is_directory=True)
        package = self.folder / "release"
        agent = package / "agent"
        agent.mkdir(parents=True)
        (package / "MANIFEST.json").write_text("{}")
        with self.assertRaises(diagnostic.SapDiagnosticError):
            diagnostic.diagnose_schema(self.connection, linked / "report.json", metadata_reader=lambda _: metadata())
        with patch("sap_worker.ROOT", agent), self.assertRaises(diagnostic.SapDiagnosticError):
            diagnostic.diagnose_schema(self.connection, package / "report.json", metadata_reader=lambda _: metadata())
        self.assertFalse((package / "report.json").exists())

    def test_output_directory_swap_is_detected_without_publishing_to_replacement(self):
        parent = self.folder / "reports"
        parent.mkdir()
        replaced = self.folder / "replaced"
        output = parent / "report.json"

        def swap(_):
            parent.rename(replaced)
            parent.mkdir()
            return metadata()

        with self.assertRaises(diagnostic.SapDiagnosticError):
            diagnostic.diagnose_schema(self.connection, output, metadata_reader=swap)
        self.assertFalse(output.exists())
        self.assertFalse((replaced / "report.json").exists())
        self.assertEqual(list(replaced.iterdir()), [])

    def test_config_is_bounded_absolute_regular_and_non_symlink(self):
        linked = self.folder / "linked-config.json"
        linked.symlink_to(self.config_path)
        fifo = self.folder / "fifo-config"
        os.mkfifo(fifo)
        oversized = self.folder / "large-config.json"
        oversized.write_bytes(b"x" * 32769)
        for value in (Path("relative-config.json"), linked, fifo, oversized, self.folder):
            with self.subTest(value=value.name), self.assertRaises(diagnostic.SapDiagnosticError):
                diagnostic.load_diagnostic_connection(value)
        self.assertEqual(diagnostic.load_diagnostic_connection(self.config_path), _validate(config(), self.config_path))

    def test_disabled_connection_does_not_contact_reader_or_create_report(self):
        value = config()
        value["enabled"] = False
        reader = Mock()
        with self.assertRaises(diagnostic.SapDiagnosticError):
            diagnostic.diagnose_schema(_validate(value), self.output, metadata_reader=reader)
        reader.assert_not_called()
        self.assert_no_report()

    def run_cli(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = diagnostic.main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_logs_only_summary_not_source_identity_path_or_raw_values(self):
        item = row()
        item["PurchaseRequisitionItemText"] = "PRIVATE-MARKER"
        with (
            patch("sap_diagnostics.fetch_metadata", return_value=metadata()),
            patch("sap_diagnostics._sample_bytes", return_value=raw_response(value=[item])),
        ):
            code, stdout, stderr = self.run_cli(
                ["sample", "--config", str(self.config_path), "--pr", PR, "--item", ITEM, "--output", str(self.output)]
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        for marker in (PR, ITEM, str(self.output), "PRIVATE-MARKER", "SYNTHETIC_DEV_200"):
            self.assertNotIn(marker, stdout)
        self.assertEqual(json.loads(stdout)["intake_eligible"], "not_evaluated")

    def test_cli_safe_errors_do_not_echo_argument_or_exception_text(self):
        args = ["schema", "--config", str(self.config_path), "--output", str(self.output)]
        with patch("sap_diagnostics.fetch_metadata", side_effect=RuntimeError("secret:do-not-echo")):
            code, stdout, stderr = self.run_cli(args)
        self.assertEqual((code, stdout), (2, ""))
        self.assertNotIn("do-not-echo", stderr)
        self.assertNotIn(str(self.config_path), stderr)
        self.assertIsNone(json.loads(stderr)["performed_api_call"])
        code, stdout, stderr = self.run_cli(["sample", "--secret-invalid-option=DO-NOT-ECHO"])
        self.assertEqual((code, stdout), (2, ""))
        self.assertNotIn("DO-NOT-ECHO", stderr)
        self.assertFalse(json.loads(stderr)["performed_api_call"])
        self.assert_no_report()

    def test_existing_secure_transport_uses_get_tls_host_no_redirect_no_retry_and_minimal_query(self):
        meta, raw = metadata(), raw_response()
        responses = []
        for content, mime in ((meta, "application/xml"), (raw, "application/json")):
            response = Mock(status=200, headers={"Content-Type": mime, "Content-Length": str(len(content))})
            response.read1.side_effect = [content, b""]
            responses.append(response)
        pool = Mock()
        pool.urlopen.side_effect = responses
        with (
            patch("sap_adapter._addresses", return_value="10.23.42.9"),
            patch("sap_adapter.urllib3.HTTPSConnectionPool", return_value=pool) as pool_factory,
        ):
            diagnostic.diagnose_sample(self.connection, PR, ITEM, self.output)
        self.assertEqual(pool.urlopen.call_count, 2)
        factory_kwargs = pool_factory.call_args.kwargs
        self.assertEqual(factory_kwargs["server_hostname"], "sap.example.invalid")
        self.assertEqual(factory_kwargs["assert_hostname"], "sap.example.invalid")
        self.assertFalse(factory_kwargs["retries"])
        for call in pool.urlopen.call_args_list:
            self.assertEqual(call.args[0], "GET")
            self.assertFalse(call.kwargs["redirect"])
            self.assertFalse(call.kwargs["retries"])
        from urllib.parse import parse_qs, urlsplit

        target = pool.urlopen.call_args_list[1].args[1]
        query = parse_qs(urlsplit(target).query)
        self.assertEqual(query["$top"], ["2"])
        self.assertEqual(query["sap-client"], ["200"])
        self.assertEqual(query["$filter"], [f"PurchaseRequisition eq '{PR}' and PurchaseRequisitionItem eq '{ITEM}'"])
        self.assertEqual(query["$select"], [",".join(MINIMUM + OPTIONAL)])
        self.assertNotIn("RequesterEmail", target)
        self.assertNotIn("CreatedByUser", target)

    def test_metadata_dtd_truncation_and_missing_auth_diagnostics_do_not_publish(self):
        for raw in (metadata()[:-10], b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///secret">]><a/>'):
            with self.assertRaises(diagnostic.SapDiagnosticError):
                self.sample(meta=raw)
            self.assert_no_report()


if __name__ == "__main__":
    unittest.main()
