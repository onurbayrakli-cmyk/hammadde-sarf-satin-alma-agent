"""Synthetic offline metadata/read tests; no SAP or queue is contacted."""

import contextlib
import copy
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sap_metadata import FIELD_TYPES, MetadataError, inspect_metadata
from sap_worker import SapWorkerError, check_connection, main, prepare_request

MAPPING = {
    "purchase_requisition": "PurchaseRequisition",
    "purchase_requisition_item": "PurchaseRequisitionItem",
    "material_code": "Material",
    "name": "PurchaseRequisitionItemText",
    "quantity": "RequestedQuantity",
    "ordered_quantity": "OrderedQuantity",
    "unit": "BaseUnit",
    "requester_email": "RequesterEmail",
    "deleted": "IsDeleted",
    "closed": "IsClosed",
    "blocked": "IsBlocked",
    "released": "IsReleased",
}
PR, ITEM = "0000123456", "00010"
NOW = datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc)


def metadata(
    *,
    v2=False,
    alias=False,
    entity_set="PurchaseRequisitionItems",
    missing=(),
    types=None,
    keys=("PurchaseRequisition", "PurchaseRequisitionItem"),
    base=False,
):
    edm = "http://schemas.microsoft.com/ado/2008/09/edm" if v2 else "http://docs.oasis-open.org/odata/ns/edm"
    edmx = "http://schemas.microsoft.com/ado/2007/06/edmx" if v2 else "http://docs.oasis-open.org/odata/ns/edmx"
    properties = []
    for internal, name in MAPPING.items():
        if internal not in missing:
            kind = (types or {}).get(
                internal,
                "Edm.Decimal"
                if internal in {"quantity", "ordered_quantity"}
                else "Edm.Boolean"
                if internal in {"deleted", "closed", "blocked", "released"}
                else "Edm.String",
            )
            properties.append(f'<Property Name="{name}" Type="{kind}"/>')
    key_xml = "".join(f'<PropertyRef Name="{name}"/>' for name in keys)
    alias_xml = ' Alias="P"' if alias else ""
    base_xml = ' BaseType="Pilot.Base"' if base else ""
    return (
        f'<edmx:Edmx xmlns:edmx="{edmx}" Version="4.0"><edmx:DataServices>'
        f'<Schema xmlns="{edm}" Namespace="Pilot"{alias_xml}><EntityType Name="Item"{base_xml}>'
        f'<Key>{key_xml}</Key>{"".join(properties)}</EntityType><EntityContainer Name="Container">'
        f'<EntitySet Name="{entity_set}" EntityType="{"P" if alias else "Pilot"}.Item"/>'
        "</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>"
    ).encode()


def connection():
    return SimpleNamespace(
        enabled=True,
        entity_set="PurchaseRequisitionItems",
        field_map=dict(MAPPING),
        source_system="SYNTHETIC_DEV_200",
        max_response_bytes=1_048_576,
    )


def response(*, v2=False):
    row = {
        "PurchaseRequisition": PR,
        "PurchaseRequisitionItem": ITEM,
        "Material": "SYNTHETIC-SARF-01",
        "PurchaseRequisitionItemText": "Sentetik sarf",
        "RequestedQuantity": "10.5",
        "OrderedQuantity": "0",
        "BaseUnit": "KG",
        "RequesterEmail": "owner@example.invalid",
        "IsDeleted": False,
        "IsClosed": False,
        "IsBlocked": False,
        "IsReleased": True,
    }
    return {"d": {"results": [row]}} if v2 else {"value": [row]}


class MetadataTests(unittest.TestCase):
    def test_v2_and_v4_entity_set_alias_keys_and_edm_types(self):
        for v2 in (False, True):
            for alias in (False, True):
                with self.subTest(v2=v2, alias=alias):
                    result = inspect_metadata(metadata(v2=v2, alias=alias), "PurchaseRequisitionItems", MAPPING)
                    self.assertTrue(result["metadata_verified"])
                    self.assertTrue(result["key_fields_verified"])
                    self.assertTrue(result["contract_compatible"])
                    self.assertFalse(result["business_semantics_verified"])

    def test_material_service_does_not_pass_as_requisition_api(self):
        with self.assertRaises(MetadataError):
            inspect_metadata(metadata(entity_set="Materials"), "PurchaseRequisitionItems", MAPPING)

    def test_missing_owner_mapping_does_not_infer_sap_username(self):
        mapping = {**MAPPING, "requester_email": None}
        result = inspect_metadata(metadata(), "PurchaseRequisitionItems", mapping)
        self.assertFalse(result["contract_compatible"])
        self.assertEqual(result["missing_mappings"], ["requester_email"])

    def test_status_codes_and_approximate_float_quantities_are_not_accepted(self):
        for internal, kind in (
            ("released", "Edm.String"),
            ("quantity", "Edm.Double"),
            ("purchase_requisition", "Edm.Int64"),
        ):
            with self.subTest(internal=internal):
                result = inspect_metadata(metadata(types={internal: kind}), "PurchaseRequisitionItems", MAPPING)
                self.assertFalse(result["contract_compatible"])
                self.assertEqual(result["type_mismatches"], [internal])

    def test_header_key_and_duplicate_mapping_do_not_prove_item_identity(self):
        result = inspect_metadata(metadata(keys=("PurchaseRequisition",)), "PurchaseRequisitionItems", MAPPING)
        self.assertFalse(result["key_fields_verified"])
        self.assertFalse(result["contract_compatible"])
        mapping = {**MAPPING, "purchase_requisition_item": "PurchaseRequisition"}
        result = inspect_metadata(metadata(), "PurchaseRequisitionItems", mapping)
        self.assertEqual(result["unsupported_schema"], "overlapping_field_mapping")
        source = (
            metadata()
            .replace(b"</Key>", b'<PropertyRef Name="ExtraIdentity"/></Key>')
            .replace(b"</EntityType>", b'<Property Name="ExtraIdentity" Type="Edm.String"/></EntityType>')
        )
        result = inspect_metadata(source, "PurchaseRequisitionItems", MAPPING)
        self.assertFalse(result["key_fields_verified"])
        self.assertFalse(result["contract_compatible"])

    def test_missing_field_and_inheritance_are_explicitly_unsupported(self):
        result = inspect_metadata(metadata(missing=("name",)), "PurchaseRequisitionItems", MAPPING)
        self.assertEqual(result["missing_fields"], ["name"])
        self.assertFalse(result["contract_compatible"])
        result = inspect_metadata(metadata(base=True), "PurchaseRequisitionItems", MAPPING)
        self.assertEqual(result["unsupported_schema"], "entity_inheritance")

    def test_ambiguous_entity_sets_and_unresolved_type_are_rejected(self):
        source = metadata()
        original = b'<EntitySet Name="PurchaseRequisitionItems" EntityType="Pilot.Item"/>'
        for modified in (
            source.replace(original, original + original),
            source.replace(b'EntityType="Pilot.Item"', b'EntityType="Other.Item"'),
        ):
            with self.assertRaises(MetadataError):
                inspect_metadata(modified, "PurchaseRequisitionItems", MAPPING)

    def test_xml_entities_dtd_wrong_namespace_limits_and_malformed_xml_are_rejected(self):
        for source in (
            b'<!DOCTYPE x [<!ENTITY probe "unexpected">]><x/>',
            b"<x/>",
            metadata()[:-5],
            b'<Edmx xmlns="urn:fake"><DataServices/></Edmx>',
            metadata().replace(b"<Key>", b"<Key><a>" * 40),
        ):
            with self.subTest(source_size=len(source)):
                with self.assertRaises(MetadataError):
                    inspect_metadata(source, "PurchaseRequisitionItems", MAPPING)
        with self.assertRaises(MetadataError):
            inspect_metadata(metadata(), "PurchaseRequisitionItems", MAPPING, max_bytes=5)

    def test_utf16_entity_declarations_are_rejected_and_utf8_bom_is_supported(self):
        dangerous = '<!DOCTYPE edmx [<!ENTITY probe "Edm.String">]>' + metadata().decode().replace(
            'Type="Edm.String"', 'Type="&probe;"'
        )
        for source in (dangerous.encode("utf-16"), dangerous.encode("utf-16-le"), dangerous.encode("utf-8")):
            with self.assertRaises(MetadataError):
                inspect_metadata(source, "PurchaseRequisitionItems", MAPPING)
        result = inspect_metadata(b"\xef\xbb\xbf" + metadata(), "PurchaseRequisitionItems", MAPPING)
        self.assertTrue(result["contract_compatible"])

    def test_all_required_business_fields_are_in_contract(self):
        self.assertEqual(set(FIELD_TYPES), set(MAPPING))


class SapReadWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name).resolve()
        self.output = self.folder / "new-source.json"
        self.config = connection()
        self.metadata_reader = Mock(return_value=metadata())
        self.requisition_reader = Mock(return_value=response())

    def tearDown(self):
        self.temporary.cleanup()

    def read(self, **changes):
        return prepare_request(
            self.config,
            PR,
            ITEM,
            self.output,
            now=NOW,
            metadata_reader=self.metadata_reader,
            requisition_reader=self.requisition_reader,
            **changes,
        )

    def test_complete_v2_v4_read_creates_only_source_and_preserves_zeroes(self):
        for v2 in (False, True):
            with self.subTest(v2=v2):
                self.output = self.folder / ("v2.json" if v2 else "v4.json")
                self.requisition_reader.return_value = response(v2=v2)
                rows = (
                    self.requisition_reader.return_value["d"]["results"]
                    if v2
                    else self.requisition_reader.return_value["value"]
                )
                rows[0]["UnrelatedPrivateNote"] = "not needed for this purchasing scope"
                result = self.read()
                envelope = json.loads(self.output.read_text())
                self.assertEqual(envelope["source"], "sap_api")
                self.assertEqual(envelope["request"]["sap_requisition"]["purchase_requisition"], PR)
                self.assertEqual(envelope["request"]["sap_requisition"]["purchase_requisition_item"], ITEM)
                self.assertEqual(envelope["request"]["requester"], "owner@example.invalid")
                self.assertEqual(envelope["request"]["state"], "awaiting_deadline")
                self.assertIsNone(envelope["request"]["deadline_at"])
                self.assertTrue(envelope["sap_read_evidence"]["collection_complete"])
                self.assertFalse(envelope["sap_read_evidence"]["business_semantics_verified"])
                self.assertEqual(set(envelope["sap_read_evidence"]["source_record"]), set(MAPPING.values()))
                self.assertNotIn("UnrelatedPrivateNote", self.output.read_text())
                self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
                self.assertFalse(result["queue_written"])
                self.assertFalse(result["sap_written"])
                self.assertNotIn("owner@example.invalid", json.dumps(result))
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["v2.json", "v4.json"])

    def test_check_reads_no_pr_and_cannot_claim_data_access(self):
        result = check_connection(self.config, metadata_reader=self.metadata_reader)
        self.assertTrue(result["metadata_verified"])
        self.assertTrue(result["contract_compatible"])
        self.assertFalse(result["requisition_read_verified"])
        self.assertFalse(result["source_file_created"])
        self.requisition_reader.assert_not_called()

    def test_incomplete_schema_prevents_pr_read(self):
        self.metadata_reader.return_value = metadata(types={"blocked": "Edm.String"})
        with self.assertRaisesRegex(SapWorkerError, "metadata_contract_incomplete"):
            self.read()
        self.requisition_reader.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_next_links_and_count_disagreement_never_create_partial_success(self):
        bad = [response(), response(v2=True), response(), response(v2=True)]
        bad[0]["@odata.nextLink"] = "https://untrusted.example.invalid/next"
        bad[1]["d"]["__next"] = "next"
        bad[2]["@odata.count"] = 2
        bad[3]["d"]["__count"] = "2"
        for payload in bad:
            with self.subTest(payload=payload):
                self.requisition_reader.return_value = payload
                with self.assertRaisesRegex(SapWorkerError, "incomplete"):
                    self.read()
                self.assertFalse(self.output.exists())

    def test_unsupported_pagination_aliases_and_mixed_envelopes_are_rejected(self):
        for v2 in (False, True):
            for name in ("@odata.nextLink", "odata.nextLink", "__next", "@odata.deltaLink", "__count"):
                payload = response(v2=v2)
                container = payload["d"] if v2 else payload
                container[name] = "2" if name == "__count" else "next-page"
                with self.subTest(v2=v2, name=name):
                    self.requisition_reader.return_value = payload
                    with self.assertRaisesRegex(SapWorkerError, "incomplete"):
                        self.read()
                    self.assertFalse(self.output.exists())
        for extra in ({}, "not-a-v2-envelope", {"results": []}):
            payload = response()
            payload["d"] = extra
            self.requisition_reader.return_value = payload
            with self.assertRaisesRegex(SapWorkerError, "invalid"):
                self.read()
            self.assertFalse(self.output.exists())

    def test_other_pr_multiple_rows_and_duplicate_row_are_rejected(self):
        wrong = response()
        wrong["value"][0]["PurchaseRequisitionItem"] = "00020"
        duplicate = response()
        duplicate["value"].append(copy.deepcopy(duplicate["value"][0]))
        other = response()
        other["value"].append({**other["value"][0], "PurchaseRequisition": "OTHER"})
        for payload in (wrong, duplicate, other, {"value": []}):
            with self.subTest(rows=payload):
                self.requisition_reader.return_value = payload
                with self.assertRaises(SapWorkerError):
                    self.read()
                self.assertFalse(self.output.exists())

    def test_ineligible_unknown_flags_ordered_quantity_and_owner_are_not_imported(self):
        for field, value in (
            ("IsReleased", "X"),
            ("IsDeleted", True),
            ("IsClosed", True),
            ("IsBlocked", True),
            ("OrderedQuantity", "1"),
            ("RequesterEmail", "SAPUSERNAME"),
            ("RequesterEmail", ""),
            ("Material", ""),
        ):
            with self.subTest(field=field, value=value):
                payload = response()
                payload["value"][0][field] = value
                self.requisition_reader.return_value = payload
                with self.assertRaisesRegex(SapWorkerError, "requires_review"):
                    self.read()
                self.assertFalse(self.output.exists())

    def test_local_path_and_id_rejection_happens_before_network(self):
        self.output.write_text("existing")
        with self.assertRaises(SapWorkerError):
            self.read()
        self.assertEqual(self.output.read_text(), "existing")
        self.output.unlink()
        self.output = self.folder / "missing" / "new.json"
        with self.assertRaises(SapWorkerError):
            self.read()
        self.output = self.folder / "new-source.json"
        with self.assertRaises(SapWorkerError):
            prepare_request(
                self.config,
                "bad'filter",
                ITEM,
                self.output,
                metadata_reader=self.metadata_reader,
                requisition_reader=self.requisition_reader,
            )
        self.metadata_reader.assert_not_called()
        self.requisition_reader.assert_not_called()

    def test_source_tree_and_symlink_destinations_are_rejected(self):
        with self.assertRaises(SapWorkerError):
            prepare_request(
                self.config,
                PR,
                ITEM,
                Path(__file__).resolve().parent / "unexpected-source.json",
                metadata_reader=self.metadata_reader,
                requisition_reader=self.requisition_reader,
            )
        link = self.folder / "link"
        link.symlink_to(self.folder, target_is_directory=True)
        with self.assertRaises(SapWorkerError):
            prepare_request(
                self.config,
                PR,
                ITEM,
                link / "new.json",
                metadata_reader=self.metadata_reader,
                requisition_reader=self.requisition_reader,
            )
        self.metadata_reader.assert_not_called()

    def test_write_failure_removes_staging_and_never_reports_success(self):
        with patch("sap_worker.os.fsync", side_effect=OSError("synthetic failure")):
            with self.assertRaisesRegex(SapWorkerError, "not_published"):
                self.read()
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_ancestor_swap_during_read_cannot_publish_into_release(self):
        release = self.folder / "release"
        release.mkdir()
        (release / "MANIFEST.json").write_text("{}")
        agent = release / "agent"
        agent.mkdir()
        (release / "b").mkdir()
        external = self.folder / "external"
        (external / "a" / "b").mkdir(parents=True)
        target = external / "a" / "b" / "source.json"

        def swap_ancestor(_connection):
            (external / "a").rename(external / "old-a")
            (external / "a").symlink_to(release, target_is_directory=True)
            return metadata()

        with patch("sap_worker.ROOT", agent):
            with self.assertRaises(SapWorkerError):
                prepare_request(
                    self.config,
                    PR,
                    ITEM,
                    target,
                    now=NOW,
                    metadata_reader=swap_ancestor,
                    requisition_reader=self.requisition_reader,
                )
        self.assertFalse((release / "b" / "source.json").exists())
        self.assertEqual(list((external / "old-a" / "b").iterdir()), [])
        self.assertEqual(list((release / "b").iterdir()), [])

    def test_concurrent_publication_does_not_overwrite_winner(self):
        real_link = os.link

        def competing_link(*args, **kwargs):
            self.output.write_text("other producer")
            return real_link(*args, **kwargs)

        with patch("sap_worker.os.link", side_effect=competing_link):
            with self.assertRaises(SapWorkerError):
                self.read()
        self.assertEqual(self.output.read_text(), "other producer")
        self.assertEqual([path.name for path in self.folder.iterdir()], ["new-source.json"])

    def test_cli_status_is_configuration_only_and_errors_do_not_echo_source(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("sap_worker.load_connection", return_value=self.config),
            patch("sap_worker.preflight", return_value={"status": "configured"}),
            patch("sap_worker.fetch_metadata") as metadata_get,
            patch("sap_worker.fetch_requisition") as pr_get,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = main(["--config", "explicit-config.json", "status"])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(stdout.getvalue())["performed_api_call"])
        metadata_get.assert_not_called()
        pr_get.assert_not_called()
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("sap_worker.load_connection", side_effect=ValueError("sensitive synthetic detail")),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = main(["--config", "explicit-config.json", "check"])
        self.assertEqual(code, 2)
        self.assertNotIn("sensitive", stderr.getvalue())
        self.assertFalse(json.loads(stderr.getvalue())["performed_api_call"])

    def test_fresh_cli_status_does_not_load_langflow_or_provider_sdks(self):
        config = self.folder / "config.json"
        config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "enabled": False,
                    "source_system": "SYNTHETIC_DEV_200",
                    "origin": None,
                    "service_root": None,
                    "entity_set": None,
                    "sap_client": None,
                    "allowed_networks": [],
                    "auth": {"mode": "none", "username_env": None, "password_env": None, "bearer_env": None},
                    "tls": {"ca_file": None},
                    "field_map": {},
                    "max_response_bytes": 1_048_576,
                    "timeout_seconds": 8,
                }
            )
        )
        script = """import json,sys
import sap_worker
code=sap_worker.main(['--config',sys.argv[1],'status'])
assert not any(name == 'lfx' or name.startswith(('lfx.', 'langflow', 'transformers')) for name in sys.modules)
raise SystemExit(code)
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(config)],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["performed_api_call"])
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
