"""Explicit read-only SAP pilot; it never writes a queue, SAP or an email."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import stat
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sap_adapter import fetch_metadata, fetch_requisition
from sap_connection import load_connection, preflight
from sap_metadata import MetadataError, inspect_metadata

ROOT = Path(__file__).resolve().parent


class SapWorkerError(ValueError):
    """A local, safe failure that cannot expose source data."""


def _fail(reason):
    raise SapWorkerError(reason) from None


def _instant(now=None):
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.utcoffset() is None:
        _fail("reference_time_invalid")
    return now.astimezone(timezone.utc)


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", value):
        _fail("requisition_identity_invalid")
    return value


def check_connection(connection, *, metadata_reader=None):
    """Read metadata and prove structural compatibility, without PR data."""
    reader = metadata_reader or fetch_metadata
    raw = reader(connection)
    result = inspect_metadata(raw, connection.entity_set, connection.field_map, max_bytes=connection.max_response_bytes)
    return {
        **result,
        "status": "metadata_contract_compatible" if result["contract_compatible"] else "metadata_contract_incomplete",
        "performed_api_call": True,
        "requisition_read_verified": False,
        "source_file_created": False,
        "queue_written": False,
        "sap_written": False,
        "outbound_sent": False,
    }


def _target_path(value):
    try:
        path = Path(value)
    except (ValueError, TypeError):
        _fail("output_path_invalid")
    if (
        not path.is_absolute()
        or ".." in path.parts
        or len(str(path)) > 4096
        or any(ord(char) < 32 or ord(char) == 127 for char in str(path))
    ):
        _fail("output_path_invalid")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            _fail("output_path_invalid")
    try:
        path = path.resolve()
        release = ROOT.parent if ROOT.name == "agent" and (ROOT.parent / "MANIFEST.json").is_file() else ROOT
        if path == release or release in path.parents or path.exists() or not path.parent.is_dir():
            _fail("output_path_invalid")
    except (OSError, RuntimeError):
        _fail("output_path_invalid")
    return path


def _open_parent(path):
    """Walk every directory with no-follow semantics, anchored at filesystem root."""
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW") or path.anchor != "/":
        _fail("output_path_invalid")
    descriptor = None
    try:
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for component in path.parent.parts[1:]:
            next_descriptor = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        result, descriptor = descriptor, None
        return result
    except (OSError, ValueError):
        _fail("output_path_invalid")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_new(path, raw, parent_fd):
    """Fsync private staging data, then atomically link a new filename."""
    staging = ".sap-source-" + uuid.uuid4().hex
    descriptor = None
    staged = linked = False
    identity = None
    try:
        descriptor = os.open(
            staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent_fd
        )
        staged = True
        info = os.fstat(descriptor)
        identity = info.st_dev, info.st_ino
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        # The parent descriptor, rather than a re-resolved string path, binds
        # publication to the directory that was validated for this operation.
        os.link(staging, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
        linked = True
        os.fsync(parent_fd)
        verified_fd = _open_parent(path)
        try:
            current, parent = os.fstat(verified_fd), os.fstat(parent_fd)
            target = os.stat(path.name, dir_fd=verified_fd, follow_symlinks=False)
            if (
                (current.st_dev, current.st_ino) != (parent.st_dev, parent.st_ino)
                or not stat.S_ISREG(target.st_mode)
                or (target.st_dev, target.st_ino) != identity
            ):
                _fail("output_path_changed")
        finally:
            os.close(verified_fd)
    except (OSError, ValueError):
        if linked and parent_fd is not None:
            try:
                current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == identity:
                    os.unlink(path.name, dir_fd=parent_fd)
            except OSError:
                pass
        _fail("source_file_not_published")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if staged and parent_fd is not None:
            try:
                os.unlink(staging, dir_fd=parent_fd)
            except OSError:
                pass


def _normalize(payload, connection, pr, item, instant):
    # Status/schema checks do not need Langflow/provider SDK imports. Only an
    # explicit data read uses the existing purchasing normalizer.
    components = Path(__file__).resolve().parent / "components"
    if str(components) not in sys.path:
        sys.path.insert(0, str(components))
    try:
        module = importlib.import_module("sap_intake")
        if Path(module.__file__).resolve() != components / "sap_intake.py":
            _fail("normalizer_unavailable")
    except (ImportError, AttributeError, TypeError, ValueError, OSError):
        _fail("normalizer_unavailable")
    return module.normalize_sap_purchase_requisitions(
        payload,
        {key: value for key, value in connection.field_map.items() if value is not None},
        source_system=connection.source_system,
        purchase_requisition=pr,
        purchase_requisition_item=item,
        now=instant,
    )


def _complete_single_row(payload, field_map, pr, item):
    if not isinstance(payload, dict):
        _fail("requisition_response_invalid")
    v2 = payload.get("d")
    containers = (payload, v2) if isinstance(v2, dict) else (payload,)
    for container in containers:
        if any(
            name in container
            for name in (
                "@odata.nextLink",
                "odata.nextLink",
                "__next",
                "@odata.deltaLink",
                "odata.deltaLink",
                "__delta",
            )
        ):
            _fail("requisition_response_incomplete")
    if "value" in payload and "d" in payload:
        _fail("requisition_response_invalid")
    if isinstance(v2, dict) and "value" in v2:
        _fail("requisition_response_invalid")
    rows = payload.get("value") if "value" in payload else v2.get("results") if isinstance(v2, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        _fail("requisition_selection_not_unique")
    row = rows[0]
    if row.get(field_map["purchase_requisition"]) != pr or row.get(field_map["purchase_requisition_item"]) != item:
        _fail("requisition_scope_mismatch")
    for container in containers:
        for name in ("@odata.count", "odata.count", "__count"):
            if name in container:
                count = container[name]
                if not (type(count) is int and count == 1 or isinstance(count, str) and count == "1"):
                    _fail("requisition_response_incomplete")
    return row


def prepare_request(connection, pr, item, output, *, metadata_reader=None, requisition_reader=None, now=None):
    """Prepare a source-backed request file; queue ingestion remains explicit."""
    pr, item = _identity(pr), _identity(item)
    destination = _target_path(output)
    parent_fd = _open_parent(destination)
    try:
        return _prepare_request(
            connection,
            pr,
            item,
            destination,
            parent_fd,
            metadata_reader=metadata_reader,
            requisition_reader=requisition_reader,
            now=now,
        )
    finally:
        os.close(parent_fd)


def _prepare_request(
    connection, pr, item, destination, parent_fd, *, metadata_reader=None, requisition_reader=None, now=None
):
    instant = _instant(now)
    metadata = check_connection(connection, metadata_reader=metadata_reader)
    if not metadata["contract_compatible"]:
        _fail("metadata_contract_incomplete")
    payload = (requisition_reader or fetch_requisition)(connection, pr, item)
    source_row = _complete_single_row(payload, connection.field_map, pr, item)
    try:
        payload_raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (ValueError, TypeError, OverflowError, RecursionError):
        _fail("requisition_response_invalid")
    if len(payload_raw) > connection.max_response_bytes:
        _fail("requisition_response_invalid")
    normalized = _normalize(payload, connection, pr, item, instant)
    if normalized.get("status") != "ready" or normalized.get("request", {}).get("state") != "awaiting_deadline":
        _fail("requisition_requires_review")
    request = normalized["request"]
    source_record = {name: source_row[name] for name in connection.field_map.values()}
    source_record_raw = json.dumps(
        source_record, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    envelope = {
        "source": "sap_api",
        "request": request,
        "sap_read_evidence": {
            "metadata_sha256": metadata["metadata_sha256"],
            "payload_sha256": hashlib.sha256(payload_raw).hexdigest(),
            "read_at": instant.isoformat(),
            "source_record": source_record,
            "field_map": dict(connection.field_map),
            "source_record_sha256": hashlib.sha256(source_record_raw).hexdigest(),
            "source_system": connection.source_system,
            "entity_set": connection.entity_set,
            "purchase_requisition": pr,
            "purchase_requisition_item": item,
            "collection_scope": "single_pr_item",
            "collection_complete": True,
            "business_semantics_verified": False,
            "queue_written": False,
            "sap_written": False,
        },
    }
    raw = (json.dumps(envelope, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    _write_new(destination, raw, parent_fd)
    return {
        "status": "request_source_prepared",
        "request_id": request["id"],
        "state": "awaiting_deadline",
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_file_created": True,
        "performed_api_call": True,
        "requisition_read_verified": True,
        "queue_written": False,
        "sap_written": False,
        "outbound_sent": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="SAP satın alma talebi salt okuma pilotu; kuyruk/SAP/e-posta yazmaz")
    parser.add_argument("--config", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("check")
    read = commands.add_parser("read")
    read.add_argument("--pr", required=True)
    read.add_argument("--item", required=True)
    read.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    attempted = False
    try:
        connection = load_connection(args.config)
        if args.command == "status":
            result = {
                **preflight(connection),
                "performed_api_call": False,
                "queue_written": False,
                "sap_written": False,
                "outbound_sent": False,
            }
        elif args.command == "check":
            attempted = True
            result = check_connection(connection)
        else:
            # Invalid local destinations/identities are rejected before a read.
            _identity(args.pr)
            _identity(args.item)
            _target_path(args.output)
            attempted = True
            result = prepare_request(connection, args.pr, args.item, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if result.get("contract_compatible", True) else 2
    except (ValueError, RuntimeError, OSError, MetadataError, KeyError, TypeError):
        print(
            json.dumps(
                {
                    "status": "error",
                    "reason": "SAP yapılandırması, okuma veya kaynak sözleşmesi doğrulanamadı",
                    "performed_api_call": None if attempted else False,
                    "source_file_created": False,
                    "queue_written": False,
                    "sap_written": False,
                    "outbound_sent": False,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
