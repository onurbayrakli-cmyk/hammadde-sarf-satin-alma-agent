"""Review a saved OData schema offline; never contact or configure SAP."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path

from sap_metadata import MetadataError, catalog_metadata
from sap_worker import _open_parent, _target_path, _write_new

MAX_METADATA_BYTES = 1_048_576
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
FLAGS = {
    "performed_api_call": False,
    "sap_connection_verified": False,
    "business_semantics_verified": False,
    "contract_evaluated": False,
    "source_file_created": False,
    "queue_written": False,
    "sap_written": False,
    "outbound_sent": False,
}


class SchemaReviewError(ValueError):
    """Fixed reason codes only; supplied paths and XML never enter errors."""


def _fail(reason="offline_schema_review_failed"):
    raise SchemaReviewError(reason) from None


def _input_path(value):
    try:
        path = Path(value)
        if (
            not path.is_absolute()
            or path.anchor != "/"
            or ".." in path.parts
            or len(str(path)) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in str(path))
        ):
            _fail()
        return path
    except (TypeError, ValueError, OSError):
        _fail()


def _identity(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read_metadata(path):
    """Read a bounded regular file through anchored no-follow descriptors."""
    parent_fd = _open_parent(path)
    descriptor = None
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode) or not 0 < initial.st_size <= MAX_METADATA_BYTES:
            _fail()
        raw = bytearray()
        while len(raw) <= MAX_METADATA_BYTES:
            block = os.read(descriptor, min(16_384, MAX_METADATA_BYTES + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        if (
            len(raw) != initial.st_size
            or len(raw) > MAX_METADATA_BYTES
            or _identity(os.fstat(descriptor)) != _identity(initial)
        ):
            _fail()
        current_parent = _open_parent(path)
        try:
            old_parent, new_parent = os.fstat(parent_fd), os.fstat(current_parent)
            current = os.stat(path.name, dir_fd=current_parent, follow_symlinks=False)
            if (
                (old_parent.st_dev, old_parent.st_ino) != (new_parent.st_dev, new_parent.st_ino)
                or not stat.S_ISREG(current.st_mode)
                or _identity(current) != _identity(initial)
            ):
                _fail()
        finally:
            os.close(current_parent)
        return bytes(raw)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def review_schema(metadata_path, output_path, *, entity_set=None):
    """Create one private schema report, without business-contract evaluation."""
    parent_fd = None
    try:
        if entity_set is not None and (not isinstance(entity_set, str) or not IDENTIFIER.fullmatch(entity_set)):
            _fail()
        source = _input_path(metadata_path)
        destination = _target_path(output_path)
        parent_fd = _open_parent(destination)
        raw = _read_metadata(source)
        catalog = catalog_metadata(raw, max_bytes=MAX_METADATA_BYTES)
        if entity_set is not None:
            selected = [item for item in catalog["entity_sets"] if item["name"] == entity_set]
            if len(selected) != 1:
                _fail()
            catalog = {**catalog, "entity_sets": selected}
        report = {"status": "offline_schema_review", "report_created": True, **FLAGS, "catalog": catalog}
        content = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        _write_new(destination, content, parent_fd)
        return report
    except (ValueError, RuntimeError, OSError, TypeError, KeyError, MetadataError):
        _fail()
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


class _SafeParser(argparse.ArgumentParser):
    def error(self, _message):
        _fail("arguments_invalid")


def main(argv=None):
    parser = _SafeParser(description="Yerel SAP metadata şema incelemesi; SAP veya iş kuyruğuna bağlanmaz")
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--entity-set")
    try:
        args = parser.parse_args(argv)
        report = review_schema(args.metadata, args.output, entity_set=args.entity_set)
        # The full bounded catalog is in the report file, not echoed to logs.
        summary = {key: value for key, value in report.items() if key != "catalog"}
        summary.update(
            entity_sets_reviewed=len(report["catalog"]["entity_sets"]),
            metadata_sha256=report["catalog"]["metadata_sha256"],
        )
        print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, RuntimeError, OSError, TypeError, KeyError, MetadataError):
        print(
            json.dumps(
                {"status": "error", "reason": "offline_schema_review_failed", "report_created": False, **FLAGS},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
