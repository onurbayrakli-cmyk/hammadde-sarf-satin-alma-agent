"""Create an allowlisted macOS purchasing source-pilot ZIP, without activation.

The input selection maps relative purchasing sources to relative release paths.
The builder never rewrites Python or sanitizes a live flow: provide an already
neutral, reviewed flow. Business state and machine credentials are rejected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

try:
    from .verify_package import (
        MANIFEST,
        MAX_FILES,
        MAX_TOTAL,
        PACKAGE,
        SCHEMA_VERSION,
        SUFFIXES,
        VERIFIER,
        VERSION,
        _json,
        _load_json,
        _manifest,
        _path,
        _read_file,
        _relative,
        _validate_payload,
        verify_package,
    )
except ImportError:
    from verify_package import (
        MANIFEST,
        MAX_FILES,
        MAX_TOTAL,
        PACKAGE,
        SCHEMA_VERSION,
        SUFFIXES,
        VERIFIER,
        VERSION,
        _json,
        _load_json,
        _manifest,
        _path,
        _read_file,
        _relative,
        _validate_payload,
        verify_package,
    )

AGENT_ROOT = Path(__file__).resolve().parent.parent


def load_selection(path, *, source_root=AGENT_ROOT):
    """Read the small explicit allowlist, not a recursive directory scan."""
    root = _path(source_root)
    selection = _path(path)
    if not selection.is_relative_to(root):
        raise ValueError("Dosya seçim kaydı bu satın alma agentı dışında")
    value = _load_json(_read_file(selection, 100_000))
    if not isinstance(value, dict) or set(value) != {"schema_version", "files"}:
        raise ValueError("Dosya seçim şeması geçersiz")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Dosya seçim sürümü desteklenmiyor")
    return value["files"]


def _selection(files, root):
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError("Teslim dosya seçimi sınırlı bir liste olmalı")
    rows, sources, destinations = [], set(), set()
    total = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"source", "path", "kind"}:
            raise ValueError("Teslim dosya seçim kaydı geçersiz")
        source_relative = _relative(item["source"])
        target_relative = str(_relative(item["path"]))
        if (
            not isinstance(item["kind"], str)
            or item["kind"] not in SUFFIXES
            or source_relative.suffix.casefold() not in SUFFIXES[item["kind"]]
        ):
            raise ValueError("Teslim kaynağı dosya türü izinli kapsamda değil")
        source = _path(root / source_relative)
        if not source.is_relative_to(root):
            raise ValueError("Teslim kaynağı satın alma agentı dışında")
        if source in sources or target_relative.casefold() in destinations:
            raise ValueError("Teslim kaynağı veya hedef yolu yineleniyor")
        raw = _read_file(source)
        _validate_payload(target_relative, raw, item["kind"])
        if item["kind"] == "verifier" and (
            target_relative != VERIFIER or raw != _read_file(Path(__file__).with_name("verify_package.py"))
        ):
            raise ValueError("Paket doğrulayıcısı bu sürümün bağımsız doğrulayıcısı olmalı")
        sources.add(source)
        destinations.add(target_relative.casefold())
        total += len(raw)
        if total > MAX_TOTAL:
            raise ValueError("Teslim kaynak toplamı boyut sınırını aşıyor")
        rows.append((source, target_relative, item["kind"], raw))
    return rows


def build_package(output_path, files, *, version, source_root=AGENT_ROOT):
    """Create-only archive; failed collection never leaves a published ZIP."""
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError("Teslim sürümü semantik biçimde olmalı")
    root = _path(source_root)
    if not root.is_dir():
        raise ValueError("Satın alma kaynak klasörü yok")
    output = _path(output_path)
    _relative(output.name)
    if output.suffix != ".zip" or not re.search(r"(?:^|[_-])v" + re.escape(version) + r"(?:[_-]|\.zip$)", output.name):
        raise ValueError("ZIP dosya adı açık sürümü içermeli")
    if output.exists() or not output.parent.is_dir():
        raise ValueError("Teslim hedefi yeni olmalı ve üst klasörü önceden bulunmalı")
    rows = _selection(files, root)
    inventory = [
        {"path": path, "kind": kind, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        for _, path, kind, raw in sorted(rows, key=lambda row: row[1])
    ]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "package": PACKAGE,
        "version": version,
        "platform": "macos",
        "release_stage": "source_pilot",
        "production_ready": False,
        "includes_business_data": False,
        "auto_execute": False,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "files": inventory,
        "total_files": len(inventory),
        "total_bytes": sum(item["size"] for item in inventory),
    }
    manifest_raw = _json(manifest)
    _manifest(manifest_raw)
    with tempfile.TemporaryDirectory(prefix=".purchasing-build-", dir=output.parent) as temporary:
        staged = Path(temporary) / output.name
        with zipfile.ZipFile(staged, mode="x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path, raw in [(MANIFEST, manifest_raw), *((path, raw) for _, path, _, raw in rows)]:
                info = zipfile.ZipInfo(path)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, raw)
        staged.chmod(0o600)
        result = verify_package(staged, extract_smoke=True)
        # Detect collection races before the create-only publication. Never
        # replace an existing release, even if another build finishes first.
        for source, _, _, raw in rows:
            if _read_file(source) != raw:
                raise ValueError("Teslim kaynağı paketleme sırasında değişti")
        os.link(staged, output)
        staged.unlink()
    return {
        **result,
        "status": "created_and_verified",
        "archive": str(output),
        "archive_sha256": hashlib.sha256(_read_file(output, 64_000_000)).hexdigest(),
        "archive_bytes": output.stat().st_size,
        "source_files_rechecked": True,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--files", required=True, help="Satın alma klasörü altındaki açık dosya seçim JSON'u")
    parser.add_argument("--output", required=True, help="Üzerine yazılmayacak sürümlü .zip yolu")
    args = parser.parse_args(argv)
    try:
        result = build_package(args.output, load_selection(args.files), version=args.version)
    except (OSError, ValueError, TypeError, RecursionError):
        print(
            json.dumps(
                {"status": "error", "reason": "Teslim paketi oluşturulamadı; mevcut sürüm değiştirilmedi."},
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
