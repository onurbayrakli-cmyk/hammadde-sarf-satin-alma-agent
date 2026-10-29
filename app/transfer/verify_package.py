"""Verify a purchasing source handoff without installing or running its code.

Uses only the standard library. Hashes establish internal consistency, not the
publisher's identity. No provider, service, business database or environment is
opened. Extraction smoke tests use a new temporary directory and remove it.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import math
import os
import re
import stat
import struct
import tempfile
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

SCHEMA_VERSION = 1
PACKAGE = "hammadde-sarf-satin-alma-agent"
MANIFEST = "MANIFEST.json"
VERIFIER = "transfer/verify_package.py"
MAX_FILES = 300
MAX_FILE = 10_000_000
MAX_MANIFEST = 300_000
MAX_TOTAL = 64_000_000
MAX_ARCHIVE = 64_000_000
MAX_RATIO = 250
KINDS = {"source", "test", "protocol_test", "flow", "setup", "config", "verifier", "fixture"}
SUFFIXES = {
    "source": {".py"},
    "test": {".py"},
    "protocol_test": {".py"},
    "flow": {".json"},
    "setup": {".md", ".txt", ".py", ".toml", ".lock", ".command", ".sh"},
    "config": {".json", ".toml", ".txt"},
    "verifier": {".py"},
    "fixture": {".txt", ".json"},
}
FORBIDDEN_PARTS = {
    "review",
    "cases",
    "outputs",
    "runtime",
    "monitor",
    "demo",
    "demos",
    "userdata",
    "user_data",
    "history",
    "historical",
    "automations",
    "node_modules",
    "__pycache__",
}
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9]+(?:[.-][a-z0-9]+)*)?\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
PERSONAL_PATH = re.compile(
    r"/(?:Users|home)/[A-Za-z0-9][^/\\\s\"'<>]*|[A-Za-z]:[\\/]Users[\\/][A-Za-z0-9]", re.IGNORECASE
)
KNOWN_SECRET = re.compile(
    r"sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{24,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)
PROVIDER_REFERENCES = {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"}
FLOW_KEYS = {"name", "description", "data"}
EMPTY_FIELDS = {
    "_frontend_node_flow_id",
    "_frontend_node_folder_id",
    "context_id",
    "project_id",
    "session_id",
    "input_value",
    "analysis_output",
    "source_data",
    "model_output",
    "research_data",
    "research_profile",
    "custom_materials",
    "material_catalog",
    "material_selection",
    "excluded_suppliers",
    "supplier_catalog",
    "endpoint",
    "bearer_token",
    "fixture_payload",
    "requester",
    "deadline_at",
    "source_system",
    "purchase_requisition",
    "purchase_requisition_item",
    "bot_mailbox",
    "mail_message",
    "material_code",
    "material_name",
    "quantity",
    "request_payload",
    "required_specs_json",
    "unit",
    "sap_context",
    "mail_context",
    "manual_context",
    "selected_material_code",
    "case_data",
    "negotiation_draft",
    "quote_report",
    "research_report",
    "observations_json",
    "price_history",
    "market_context",
    "plan",
    "delivery_place",
    "need_by",
    "request_id",
    "required_specs",
    "output_dir",
    "queue_path",
    "tools",
}
OFF_FIELDS = {"enable_api", "enable_live", "save_monitor_result", "allow_demo", "should_store_message"}
IDENTITY_KEYS = {"user_id", "owner_id", "workspace_id", "folder_id", "session_id", "project_id"}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON yinelenen alan içeriyor")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("JSON sonlu olmayan sayı içeriyor")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON sayı sınırını aşıyor")
    return number


def _load_json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_unique, parse_constant=_constant, parse_float=_finite_float)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Paket JSON içeriği geçersiz") from error


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"


def _relative(value):
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ValueError("Paket yolu geçersiz")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or "\\" in value
        or ":" in value
        or any(unicodedata.category(char).startswith("C") for char in value)
        or unicodedata.normalize("NFC", value) != value
        or any(part.startswith(".") or part.endswith((".", " ")) for part in path.parts)
        or any(part.casefold() in FORBIDDEN_PARTS for part in path.parts)
    ):
        raise ValueError("Paket yolu güvenli kaynak kapsamı dışında")
    return path


def _path(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError("Paket işlemi sembolik bağlantı kullanamaz")
    return path.resolve()


def _read_file(path, maximum=MAX_FILE):
    path = _path(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= maximum:
            raise ValueError("Paket dosyası düzenli, tek bağlantılı ve sınırlı boyutta olmalı")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(maximum + 1)
        after = os.fstat(descriptor)
        current = path.stat()
        if (
            len(raw) != before.st_size
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValueError("Paket dosyası okuma sırasında değişti")
        return raw
    finally:
        os.close(descriptor)


def _empty(value):
    return value is None or value == "" or value == [] or value == {}


def _check_json_strings(value):
    if isinstance(value, dict):
        for key, item in value.items():
            _check_json_strings(key)
            _check_json_strings(item)
    elif isinstance(value, list):
        for item in value:
            _check_json_strings(item)
    elif isinstance(value, str) and (PERSONAL_PATH.search(value) or KNOWN_SECRET.search(value)):
        raise ValueError("JSON kişisel makine yolu veya bilinen gizli anahtar biçimi içeriyor")


def _validate_flow(raw):
    flow = _load_json(raw)
    _check_json_strings(flow)
    if (
        not isinstance(flow, dict)
        or set(flow) - FLOW_KEYS
        or not isinstance(flow.get("name"), str)
        or not flow["name"].strip()
        or not isinstance(flow.get("data"), dict)
    ):
        raise ValueError("Akış hesap kimliklerinden arındırılmış hedef bağımsız dışa aktarım olmalı")
    data = flow["data"]
    if set(data) - {"nodes", "edges", "viewport"}:
        raise ValueError("Akış beklenmeyen çalışma verisi içeriyor")
    nodes, edges = data.get("nodes"), data.get("edges")
    if not isinstance(nodes, list) or len(nodes) != 21 or not isinstance(edges, list) or len(edges) != 28:
        raise ValueError("Satın alma akışı 21 düğüm ve 28 bağlantı içermeli")
    node_ids, edge_ids = set(), set()
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not node["id"]:
            raise ValueError("Akış düğüm kimliği geçersiz")
        if node["id"] in node_ids:
            raise ValueError("Akış düğüm kimliği yineleniyor")
        node_ids.add(node["id"])
        node_data = node.get("data")
        component = node_data.get("node") if isinstance(node_data, dict) else None
        template = component.get("template") if isinstance(component, dict) else None
        if not isinstance(template, dict):
            raise ValueError("Akış bileşen alanları geçersiz")
        for key, field in template.items():
            if key == "_type":
                if field != "Component":
                    raise ValueError("Akış bileşen metadata türü geçersiz")
                continue
            if key == "is_refresh" and node_data.get("type") == "Agent":
                if type(field) is not bool:
                    raise ValueError("Agent yenileme metadatası bool olmalı")
                continue
            if not isinstance(field, dict):
                raise ValueError("Akış bileşen alan tanımı geçersiz")
            value = field.get("value")
            if key in EMPTY_FIELDS and not _empty(value):
                raise ValueError("Akış giriş veya makineye bağlı alanları boş olmalı")
            if key in OFF_FIELDS and value is not False:
                raise ValueError("Akış dış bağlantı ve kalıcı yazım seçenekleri kapalı olmalı")
            normalized = re.sub(r"[^a-z]", "", key.casefold())
            if field.get("password") is True or normalized in {"apikey", "anthropicapikey", "bearertoken"}:
                if not _empty(value) and not (
                    isinstance(value, str) and value in PROVIDER_REFERENCES and field.get("load_from_db") is True
                ):
                    raise ValueError("Akış yalnız boş veya izinli sağlayıcı değişken adı taşıyabilir")
    for edge in edges:
        if (
            not isinstance(edge, dict)
            or not isinstance(edge.get("id"), str)
            or not edge["id"]
            or edge["id"] in edge_ids
            or not isinstance(edge.get("source"), str)
            or not isinstance(edge.get("target"), str)
            or edge["source"] not in node_ids
            or edge["target"] not in node_ids
        ):
            raise ValueError("Akış bağlantısı geçersiz")
        edge_ids.add(edge["id"])

    def identities(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in IDENTITY_KEYS and not _empty(item):
                    # Template field dictionaries are allowed when their value
                    # is empty; saved user/session/account identities are not.
                    if not isinstance(item, dict) or not _empty(item.get("value")):
                        raise ValueError("Akış kişisel hesap veya oturum kimliği içeriyor")
                identities(item)
        elif isinstance(value, list):
            for item in value:
                identities(item)

    identities(flow)


def _validate_payload(path, raw, kind):
    relative = _relative(path)
    if not isinstance(kind, str) or kind not in KINDS or relative.suffix.casefold() not in SUFFIXES[kind]:
        raise ValueError("Dosya türü izinli teslim kapsamına uymuyor")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as error:
        raise ValueError("Teslim paketi yalnız UTF-8 kaynak ve kurulum belgesi içerebilir") from error
    if "\x00" in text or PERSONAL_PATH.search(text) or KNOWN_SECRET.search(text):
        raise ValueError("Paket kişisel makine yolu veya bilinen gizli anahtar biçimi içeriyor")
    if relative.suffix == ".py":
        try:
            ast.parse(text)
        except (SyntaxError, ValueError, RecursionError) as error:
            raise ValueError("Paket Python kaynağı derlenebilir değil") from error
    if kind == "flow":
        _validate_flow(raw)
    elif relative.suffix.casefold() == ".json":
        _check_json_strings(_load_json(raw))


def _manifest(raw):
    if len(raw) > MAX_MANIFEST:
        raise ValueError("Paket manifest boyutu sınırı aşıyor")
    value = _load_json(raw)
    required = {
        "schema_version",
        "package",
        "version",
        "platform",
        "release_stage",
        "production_ready",
        "includes_business_data",
        "auto_execute",
        "created_at",
        "files",
        "total_files",
        "total_bytes",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("Paket manifest şeması geçersiz")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != SCHEMA_VERSION
        or value["package"] != PACKAGE
        or not isinstance(value["version"], str)
        or not VERSION.fullmatch(value["version"])
        or value["platform"] != "macos"
        or value["release_stage"] != "source_pilot"
        or value["production_ready"] is not False
        or value["includes_business_data"] is not False
        or value["auto_execute"] is not False
    ):
        raise ValueError("Paket sürümü veya pilot güvenlik durumu geçersiz")
    try:
        created = datetime.fromisoformat(value["created_at"].replace("Z", "+00:00"))
        if created.utcoffset() is None:
            raise ValueError
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("Paket oluşturma zamanı saat dilimli olmalı") from error
    records = value["files"]
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_FILES:
        raise ValueError("Paket dosya sayısı sınırı geçersiz")
    names, folded, kinds, total = set(), set(), [], 0
    for item in records:
        if not isinstance(item, dict) or set(item) != {"path", "kind", "size", "sha256"}:
            raise ValueError("Manifest dosya kaydı geçersiz")
        path = str(_relative(item["path"]))
        canonical = path.casefold()
        if canonical == MANIFEST.casefold() or path in names or canonical in folded:
            raise ValueError("Manifest yinelenen veya çakışan dosya içeriyor")
        if (
            not isinstance(item["kind"], str)
            or item["kind"] not in KINDS
            or PurePosixPath(path).suffix.casefold() not in SUFFIXES[item["kind"]]
            or type(item["size"]) is not int
            or not 0 < item["size"] <= MAX_FILE
            or not isinstance(item["sha256"], str)
            or not HASH.fullmatch(item["sha256"])
        ):
            raise ValueError("Manifest dosya boyutu, türü veya özeti geçersiz")
        names.add(path)
        folded.add(canonical)
        kinds.append(item["kind"])
        total += item["size"]
    spellings = {}
    for name in names:
        for part in (PurePosixPath(name), *PurePosixPath(name).parents):
            if str(part) == ".":
                continue
            spelling = str(part)
            canonical = spelling.casefold()
            if canonical in spellings and spellings[canonical] != spelling:
                raise ValueError("Paket dizin yazımları macOS üzerinde çakışıyor")
            spellings[canonical] = spelling
        if any(str(parent).casefold() in folded for parent in PurePosixPath(name).parents if str(parent) != "."):
            raise ValueError("Paket dosya ve dizin yolu çakışıyor")
    if (
        type(value["total_files"]) is not int
        or value["total_files"] != len(records)
        or type(value["total_bytes"]) is not int
        or value["total_bytes"] != total
        or total > MAX_TOTAL
        or kinds.count("flow") != 1
        or not any(item["path"] == VERIFIER and item["kind"] == "verifier" for item in records)
    ):
        raise ValueError("Paket toplamları veya zorunlu dosyaları geçersiz")
    return value


def _check_content(manifest, contents):
    expected = {item["path"] for item in manifest["files"]} | {MANIFEST}
    if set(contents) != expected:
        raise ValueError("Paket dosya listesi manifest ile birebir eşleşmiyor")
    for item in manifest["files"]:
        raw = contents[item["path"]]
        if len(raw) != item["size"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("Paket dosya boyutu veya özeti eşleşmiyor")
        _validate_payload(item["path"], raw, item["kind"])


def _directory_contents(root):
    if not root.is_dir():
        raise ValueError("Paket klasörü bulunamadı")
    contents = {}
    directories = set()
    for parent, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames:
            directory = Path(parent) / name
            relative = directory.relative_to(root).as_posix()
            _relative(relative)
            if directory.is_symlink() or not stat.S_ISDIR(directory.lstat().st_mode):
                raise ValueError("Paket klasörü bağlantı veya özel dosya içeriyor")
            directories.add(relative)
        for name in filenames:
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            if relative != MANIFEST:
                _relative(relative)
            if len(contents) >= MAX_FILES + 1:
                raise ValueError("Paket dosya sayısı sınırı aşıldı")
            contents[relative] = _read_file(path, MAX_MANIFEST if relative == MANIFEST else MAX_FILE)
            if sum(map(len, contents.values())) > MAX_TOTAL + MAX_MANIFEST:
                raise ValueError("Paket toplam boyut sınırı aşıldı")
    allowed_directories = {
        str(parent) for name in contents for parent in PurePosixPath(name).parents if str(parent) != "."
    }
    if directories != allowed_directories:
        raise ValueError("Paket listelenmemiş boş klasör içeriyor")
    return contents


def _zip_contents(path):
    raw_archive = _read_file(path, MAX_ARCHIVE)
    contents, folded, total = {}, set(), 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw_archive)) as archive:
            infos = archive.infolist()
            if not 2 <= len(infos) <= MAX_FILES + 1:
                raise ValueError("ZIP dosya sayısı sınırı geçersiz")
            # Accept only the simple bounded ZIP format produced by our
            # builder. Reject prepended/trailing payloads, hidden local entries,
            # ZIP64, multiple disks, and unlisted local metadata too.
            if len(raw_archive) < 22:
                raise ValueError("ZIP son kaydı eksik")
            end = struct.unpack("<4s4H2IH", raw_archive[-22:])
            if (
                end[0] != b"PK\x05\x06"
                or end[1:3] != (0, 0)
                or end[3] != len(infos)
                or end[4] != len(infos)
                or end[6] != archive.start_dir
                or end[5] + end[6] != len(raw_archive) - 22
                or end[7] != 0
            ):
                raise ValueError("ZIP yalnız listelenmiş tek diskli standart arşiv olmalı")
            offset = 0
            for info in infos:
                name = info.filename
                if name != MANIFEST:
                    _relative(name)
                mode = info.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if (
                    name in contents
                    or name.casefold() in folded
                    or info.is_dir()
                    or (kind and kind != stat.S_IFREG)
                    or mode & 0o7111
                    or info.flag_bits & ~0x800
                    or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    or info.comment
                    or info.extra
                    or info.orig_filename != name
                ):
                    raise ValueError("ZIP bağlantı, yinelenen veya desteklenmeyen kayıt içeriyor")
                if info.header_offset != offset or len(raw_archive) - offset < 30:
                    raise ValueError("ZIP listelenmemiş yerel kayıt veya boşluk içeriyor")
                local = struct.unpack("<4s5H3I2H", raw_archive[offset : offset + 30])
                encoded_name = name.encode("utf-8" if info.flag_bits & 0x800 else "cp437")
                if (
                    local[0] != b"PK\x03\x04"
                    or local[2] != info.flag_bits
                    or local[3] != info.compress_type
                    or local[6] != info.CRC
                    or local[7] != info.compress_size
                    or local[8] != info.file_size
                    or local[9] != len(encoded_name)
                    or local[10] != 0
                    or raw_archive[offset + 30 : offset + 30 + local[9]] != encoded_name
                ):
                    raise ValueError("ZIP yerel ve merkezi kayıtları uyuşmuyor")
                offset += 30 + local[9] + info.compress_size
                maximum = MAX_MANIFEST if name == MANIFEST else MAX_FILE
                total += info.file_size
                if (
                    not 0 < info.file_size <= maximum
                    or total > MAX_TOTAL + MAX_MANIFEST
                    or info.file_size > max(info.compress_size, 1) * MAX_RATIO
                ):
                    raise ValueError("ZIP açılmış boyut veya sıkıştırma sınırını aşıyor")
                with archive.open(info) as source:
                    raw = source.read(maximum + 1)
                if len(raw) != info.file_size:
                    raise ValueError("ZIP dosya boyutu uyuşmuyor")
                contents[name] = raw
                folded.add(name.casefold())
            if offset != archive.start_dir:
                raise ValueError("ZIP listelenmemiş yerel içerik taşıyor")
            if archive.comment:
                raise ValueError("ZIP listelenmemiş arşiv açıklaması içeriyor")
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as error:
        raise ValueError("ZIP bütünlüğü doğrulanamadı") from error
    return contents


def verify_package(path, *, extract_smoke=False):
    """Verify a ZIP or unpacked release; optional extraction is TEMP-only."""
    target = _path(path)
    contents = _directory_contents(target) if target.is_dir() else _zip_contents(target)
    if MANIFEST not in contents:
        raise ValueError("Paket manifesti yok")
    manifest = _manifest(contents[MANIFEST])
    _check_content(manifest, contents)
    if extract_smoke:
        with tempfile.TemporaryDirectory(prefix="purchasing-handoff-verify-") as temporary:
            root = Path(temporary).resolve()
            for name, raw in contents.items():
                destination = root / name
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with destination.open("xb") as handle:
                    handle.write(raw)
                destination.chmod(0o600)
            copied = _directory_contents(root)
            _check_content(_manifest(copied[MANIFEST]), copied)
            if copied != contents:
                raise ValueError("Geçici çıkarma dosya eşitliği doğrulanamadı")
    return {
        "status": "verified",
        "package": manifest["package"],
        "version": manifest["version"],
        "platform": "macos",
        "release_stage": "source_pilot",
        "files": manifest["total_files"],
        "bytes": manifest["total_bytes"],
        "manifest_sha256": hashlib.sha256(contents[MANIFEST]).hexdigest(),
        "temporary_extract_verified": bool(extract_smoke),
        "temporary_extract_removed": bool(extract_smoke),
        "signature_verified": False,
        "production_ready": False,
        "code_executed": False,
        "service_started": False,
        "business_data_included": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", help="Sürümlü ZIP veya çıkarılmış paket klasörü")
    parser.add_argument(
        "--extract-smoke", action="store_true", help="Yalnız yeni TEMP klasörüne çıkar ve tekrar doğrula"
    )
    args = parser.parse_args(argv)
    try:
        result = verify_package(args.package, extract_smoke=args.extract_smoke)
    except (OSError, ValueError, TypeError, RecursionError):
        print(
            json.dumps(
                {"status": "error", "reason": "Paket doğrulanamadı; kaynak veya hedef durumu değiştirilmedi."},
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
