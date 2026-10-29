"""Explicit SAP schema/sample diagnostics; never create an intake source or queue.

Sample reads use fixed standard PR field names. The connection's field_map is
intentionally not a diagnostic transformation or a business-acceptance rule.
Reports contain private source data; stdout contains only bounded status facts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sap_adapter import SAPReadError, _read, fetch_metadata
from sap_connection import MAX_CONFIG_BYTES, _validate, parse_json, preflight, validate_connection
from sap_metadata import CATALOG_MAX_OUTPUT_BYTES, NUMBER_TYPES, catalog_metadata
from sap_worker import _complete_single_row, _open_parent, _target_path, _write_new

ROOT = Path(__file__).resolve().parent
IDENTITY = re.compile(r"[A-Za-z0-9_.-]{1,40}\Z")
MINIMUM_FIELDS = (
    "PurchaseRequisition",
    "PurchaseRequisitionItem",
    "Material",
    "PurchaseRequisitionItemText",
    "RequestedQuantity",
    "OrderedQuantity",
    "BaseUnit",
)
OPTIONAL_FIELDS = (
    "IsDeleted",
    "IsClosed",
    "IsPurReqnBlocked",
    "ReleaseIsNotCompleted",
    "PurReqnReleaseStatus",
    "ProcessingStatus",
    "Plant",
    "MaterialGroup",
    "PurchasingOrganization",
    "PurchasingGroup",
    "PurchaseRequisitionType",
)
RAW_INDICATORS = frozenset(OPTIONAL_FIELDS[:6])
KEY_MAPPING = {"purchase_requisition": MINIMUM_FIELDS[0], "purchase_requisition_item": MINIMUM_FIELDS[1]}
FLAGS = {
    "business_semantics_verified": False,
    "contract_evaluated": False,
    "intake_eligible": "not_evaluated",
    "source_file_created": False,
    "queue_written": False,
    "sap_written": False,
    "outbound_sent": False,
}


class SapDiagnosticError(ValueError):
    """Fixed diagnostic errors never include input paths, values or credentials."""


def _fail():
    raise SapDiagnosticError("sap_diagnostic_failed") from None


def _destination(value):
    path = _target_path(value)
    if path == ROOT or ROOT in path.parents:
        _fail()
    return path


def _config_path(value):
    path = Path(value)
    if (
        not path.is_absolute()
        or path.anchor != "/"
        or ".." in path.parts
        or len(str(path)) > 4096
        or any(ord(c) < 32 or ord(c) == 127 for c in str(path))
    ):
        _fail()
    return path


def _file_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def load_diagnostic_connection(value):
    """Read the existing config schema with a bounded, no-follow regular-file read."""
    descriptor = parent_fd = None
    try:
        path = _config_path(value)
        parent_fd = _open_parent(path)
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_CONFIG_BYTES:
            _fail()
        raw = bytearray()
        while len(raw) <= MAX_CONFIG_BYTES:
            block = os.read(descriptor, min(16384, MAX_CONFIG_BYTES + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        after = os.fstat(descriptor)
        if len(raw) != before.st_size or len(raw) > MAX_CONFIG_BYTES or _file_identity(before) != _file_identity(after):
            _fail()
        checked = _open_parent(path)
        try:
            old_parent, new_parent = os.fstat(parent_fd), os.fstat(checked)
            current = os.stat(path.name, dir_fd=checked, follow_symlinks=False)
            if (
                (old_parent.st_dev, old_parent.st_ino) != (new_parent.st_dev, new_parent.st_ino)
                or not stat.S_ISREG(current.st_mode)
                or _file_identity(before) != _file_identity(current)
            ):
                _fail()
        finally:
            os.close(checked)
        return _validate(parse_json(bytes(raw)), path)
    except (ValueError, TypeError, RuntimeError, OSError):
        _fail()
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_fd is not None:
            os.close(parent_fd)


def _ready(connection):
    connection = validate_connection(connection)
    if preflight(connection)["status"] != "configured":
        _fail()
    return connection


def _metadata(connection, reader):
    raw = (reader or fetch_metadata)(connection)
    return catalog_metadata(raw, max_bytes=connection.max_response_bytes)


def _operational(catalog, entity_name):
    matches = [entry for entry in catalog["entity_sets"] if entry["name"] == entity_name]
    if len(matches) != 1:
        _fail()
    entry = matches[0]
    if (
        entry["semantics"] is not None
        or entry["base_type"] is not None
        or entry["warnings"]
        or len(entry["keys"]) != 2
        or set(entry["keys"]) != set(MINIMUM_FIELDS[:2])
    ):
        _fail()
    properties = {prop["name"]: prop["type"] for prop in entry["properties"]}
    for field in MINIMUM_FIELDS:
        accepted = NUMBER_TYPES if field in {"RequestedQuantity", "OrderedQuantity"} else {"Edm.String"}
        if properties.get(field) not in accepted:
            _fail()
    selected = list(MINIMUM_FIELDS)
    for field in OPTIONAL_FIELDS:
        if field not in properties:
            continue
        accepted = {"Edm.String", "Edm.Boolean"} if field in RAW_INDICATORS else {"Edm.String"}
        if properties[field] not in accepted:
            _fail()
        selected.append(field)
    return entry, selected, properties


def _sample_bytes(connection, pr, item, selected):
    query = {
        "$filter": f"PurchaseRequisition eq '{pr}' and PurchaseRequisitionItem eq '{item}'",
        "$select": ",".join(selected),
        "$top": "2",
        "$format": "json",
    }
    return _read(connection, connection.entity_set, query, {"application/json"}, "application/json")


def _source_record(payload, selected, properties, pr, item):
    row = _complete_single_row(payload, KEY_MAPPING, pr, item)
    if set(row) - set(selected) - {"__metadata"} or any(field not in row for field in selected):
        _fail()
    record = {}
    for field in selected:
        value = row[field]
        kind = properties[field]
        if value is None and field in MINIMUM_FIELDS:
            _fail()
        if value is not None:
            if kind == "Edm.String" and not isinstance(value, str):
                _fail()
            if kind == "Edm.Boolean" and type(value) is not bool:
                _fail()
            if kind in NUMBER_TYPES and (type(value) not in {str, int, float}):
                _fail()
            if kind in NUMBER_TYPES:
                try:
                    if not Decimal(str(value)).is_finite():
                        _fail()
                except (InvalidOperation, ValueError, OverflowError):
                    _fail()
        record[field] = value
    return record


def _publish(destination, parent_fd, report):
    content = (json.dumps(report, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(content) > CATALOG_MAX_OUTPUT_BYTES + 8192:
        _fail()
    _write_new(destination, content, parent_fd)
    excluded = {"catalog", "source_record", "selected_fields", "source_system", "entity_set", "read_at"}
    summary = {name: value for name, value in report.items() if name not in excluded}
    summary["report_sha256"] = hashlib.sha256(content).hexdigest()
    return summary


def diagnose_schema(connection, output, *, metadata_reader=None):
    """Fetch/catalog metadata without evaluating an intake contract."""
    parent_fd = None
    try:
        destination = _destination(output)
        parent_fd = _open_parent(destination)
        connection = _ready(connection)
        catalog = _metadata(connection, metadata_reader)
        report = {
            "status": "sap_schema_diagnostic",
            "diagnostic_report_created": True,
            "performed_api_call": True,
            "metadata_read_verified": True,
            "requisition_read_verified": False,
            "operational_schema_verified": False,
            **FLAGS,
            "source_system": connection.source_system,
            "catalog": catalog,
            "metadata_sha256": catalog["metadata_sha256"],
            "entity_sets_reviewed": len(catalog["entity_sets"]),
        }
        return _publish(destination, parent_fd, report)
    except (ValueError, TypeError, KeyError, RuntimeError, OSError):
        _fail()
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def diagnose_sample(connection, pr, item, output, *, metadata_reader=None, sample_reader=None):
    """Diagnose one raw operational PR item; no status conversion or requester lookup."""
    parent_fd = None
    try:
        if any(not isinstance(value, str) or not IDENTITY.fullmatch(value) for value in (pr, item)):
            _fail()
        destination = _destination(output)
        parent_fd = _open_parent(destination)
        connection = _ready(connection)
        catalog = _metadata(connection, metadata_reader)
        entry, selected, properties = _operational(catalog, connection.entity_set)
        raw = (sample_reader or _sample_bytes)(connection, pr, item, selected)
        if not isinstance(raw, bytes) or not 0 < len(raw) <= connection.max_response_bytes:
            _fail()
        payload = parse_json(raw)
        record = _source_record(payload, selected, properties, pr, item)
        record_raw = json.dumps(
            record, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
        ).encode()
        report = {
            "status": "sap_sample_diagnostic",
            "diagnostic_report_created": True,
            "performed_api_call": True,
            "metadata_read_verified": True,
            "requisition_read_verified": True,
            "operational_schema_verified": True,
            **FLAGS,
            "source_system": connection.source_system,
            "entity_set": entry["name"],
            "read_at": datetime.now(timezone.utc).isoformat(),
            "collection_scope": "single_pr_item",
            "collection_complete": True,
            "metadata_sha256": catalog["metadata_sha256"],
            "payload_sha256": hashlib.sha256(raw).hexdigest(),
            "source_record_sha256": hashlib.sha256(record_raw).hexdigest(),
            "source_record": record,
            "selected_fields": selected,
            "selected_field_count": len(selected),
        }
        return _publish(destination, parent_fd, report)
    except (ValueError, TypeError, KeyError, RuntimeError, OSError):
        _fail()
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


class _SafeParser(argparse.ArgumentParser):
    def error(self, _message):
        _fail()


def main(argv=None):
    attempted = False
    try:
        parser = _SafeParser(description="Salt okunur SAP bağlantı tanısı; özel rapor oluşturur, talep/kuyruk üretmez")
        commands = parser.add_subparsers(dest="command", required=True, parser_class=_SafeParser)
        for name in ("schema", "sample"):
            subparser = commands.add_parser(name)
            subparser.add_argument("--config", required=True)
            subparser.add_argument("--output", required=True)
            if name == "sample":
                subparser.add_argument("--pr", required=True)
                subparser.add_argument("--item", required=True)
        args = parser.parse_args(argv)
        _destination(args.output)
        if args.command == "sample" and any(not IDENTITY.fullmatch(value) for value in (args.pr, args.item)):
            _fail()
        connection = _ready(load_diagnostic_connection(args.config))
        attempted = True
        if args.command == "schema":
            result = diagnose_schema(connection, args.output)
        else:
            result = diagnose_sample(connection, args.pr, args.item, args.output)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, TypeError, KeyError, RuntimeError, OSError, SAPReadError):
        print(
            json.dumps(
                {
                    "status": "error",
                    "reason": "SAP tanı isteği veya özel rapor doğrulanamadı",
                    "performed_api_call": None if attempted else False,
                    "diagnostic_report_created": False,
                    **FLAGS,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
