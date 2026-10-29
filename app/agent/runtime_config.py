"""Portable, non-secret runtime configuration; reading never creates state.

Without SATINALMA_CONFIG only a development checkout keeps its existing paths
and local flow. A packaged installation requires external configuration, keeps
mutable state outside the release and names its Langflow API key variable.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

SOURCE_ROOT = Path(__file__).resolve().parent
LEGACY_FLOW_ID = "cd59249a-14c8-4c1b-a180-8b1f5e8441f7"
LEGACY_LANGFLOW_URL = "http://127.0.0.1:7860"
MAX_CONFIG_BYTES = 32_768
MAX_CONFIG_DEPTH = 8


class RuntimeConfigurationError(ValueError):
    """A safe configuration error containing no supplied paths or values."""


@dataclass(frozen=True)
class RuntimeSettings:
    source_root: Path
    configured: bool
    state_dir: Path
    queue_path: Path
    output_dir: Path
    monitor_dir: Path
    langflow_url: str
    flow_id: str | None
    api_key_env: str | None
    langflow_database: Path | None
    config_path: Path | None = None

    @property
    def langflow_database_path(self) -> Path | None:
        return self.langflow_database


def _fail(message: str) -> None:
    raise RuntimeConfigurationError(message) from None


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("Yapılandırmada yinelenen JSON alanı var.")
        result[key] = value
    return result


def _constant(_value):
    _fail("Yapılandırmada sonlu olmayan JSON sayısı var.")


def _depth(value, level=0):
    if level > MAX_CONFIG_DEPTH:
        _fail("Yapılandırmanın JSON derinliği sınırı aşıyor.")
    if isinstance(value, float) and not math.isfinite(value):
        _fail("Yapılandırmada sonlu olmayan JSON sayısı var.")
    if isinstance(value, dict):
        for item in value.values():
            _depth(item, level + 1)
    elif isinstance(value, list):
        for item in value:
            _depth(item, level + 1)


def _text(value, maximum=4096):
    if (not isinstance(value, str) or not value or len(value) > maximum
            or value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        _fail("Yapılandırmada gerekli metin alanı geçersiz.")
    return value


def _absolute_path(value, *, directory=False):
    value = _text(os.fspath(value) if isinstance(value, Path) else value)
    try:
        path = Path(value)
        if not path.is_absolute():
            _fail("Yapılandırma yolları mutlak olmalı.")
        path = path.resolve()
        if directory and path.exists() and not path.is_dir():
            _fail("Kalıcı veri yolu bir klasör olmalı.")
        return path
    except (OSError, RuntimeError, ValueError):
        _fail("Yapılandırma yolu geçersiz.")


def validate_langflow_url(value):
    """Allow HTTPS, or HTTP on a literal loopback host; never resolve DNS."""
    value = _text(value, 2048)
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or "?" in value or "#" in value or "\\" in value or (port is not None and not 1 <= port <= 65535)):
            raise ValueError
        if parsed.scheme == "http":
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host.lower() == "localhost"
            if not loopback:
                raise ValueError
        if not host.isascii() or any(char.isspace() for char in host):
            raise ValueError
        authority = "[" + host.lower() + "]" if ":" in host else host.lower()
        if port is not None:
            authority += ":" + str(port)
        return urlunsplit((parsed.scheme, authority, parsed.path.rstrip("/"), "", ""))
    except (ValueError, TypeError):
        _fail("Langflow adresi güvenli bir HTTPS veya yerel HTTP adresi olmalı.")


def _flow_id(value):
    if value is None:
        return None
    value = _text(value, 36)
    try:
        parsed = str(UUID(value))
    except ValueError:
        _fail("Langflow akış kimliği UUID veya null olmalı.")
    if parsed != value.lower():
        _fail("Langflow akış kimliği UUID veya null olmalı.")
    return parsed


def _state_override(state_dir):
    supplied = os.environ.get("SATINALMA_STATE_DIR")
    if supplied is not None and _absolute_path(supplied, directory=True) != state_dir:
        _fail("SATINALMA_STATE_DIR ile çalışma yapılandırmasının veri klasörü uyuşmuyor.")


def get_runtime(config_path: str | Path | None = None) -> RuntimeSettings:
    """Read an explicit file or SATINALMA_CONFIG; never read an API key value.

    Exact schema (all fields required): {schema_version: 1, state_dir: ABS,
    langflow: {base_url: URL, flow_id: UUID|null, api_key_env: ENV_NAME,
    database_path: ABS|null}}. A null flow_id is valid during setup/preflight;
    the client refuses to fetch a flow until a real ID is configured.
    """
    source_root = SOURCE_ROOT.resolve()
    package_root = (source_root.parent if source_root.name == "agent"
                    and (source_root.parent / "MANIFEST.json").is_file() else None)
    supplied = config_path if config_path is not None else os.environ.get("SATINALMA_CONFIG")
    if supplied is None:
        if package_root is not None:
            _fail("Paketlenmiş kurulumda SATINALMA_CONFIG veya açık yapılandırma yolu zorunludur.")
        _state_override(source_root)
        return RuntimeSettings(source_root=source_root, configured=False, state_dir=source_root,
                               queue_path=source_root / "cases" / "requests.db",
                               output_dir=source_root / "cases" / "drafts", monitor_dir=source_root / "monitor",
                               langflow_url=LEGACY_LANGFLOW_URL, flow_id=LEGACY_FLOW_ID, api_key_env=None,
                               langflow_database=source_root.parents[2] / ".codex" / "data" / "langflow.db")
    path = _absolute_path(supplied)
    if package_root is not None and (path == package_root or package_root in path.parents):
        _fail("Çalışma yapılandırması değişmez dağıtım paketinin dışında olmalı.")
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            _fail("Yapılandırma dosyasının boyutu sınırı aşıyor.")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
        _depth(data)
    except RuntimeConfigurationError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError):
        _fail("Çalışma yapılandırması okunamadı veya geçerli JSON değil.")
    if not isinstance(data, dict) or set(data) != {"schema_version", "state_dir", "langflow"}:
        _fail("Çalışma yapılandırmasının alanları beklenen şemayla uyuşmuyor.")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        _fail("Çalışma yapılandırmasının şema sürümü desteklenmiyor.")
    connection = data["langflow"]
    if not isinstance(connection, dict) or set(connection) != {"base_url", "flow_id", "api_key_env", "database_path"}:
        _fail("Langflow bağlantı alanları beklenen şemayla uyuşmuyor.")
    state_dir = _absolute_path(data["state_dir"], directory=True)
    release_root = package_root or source_root
    if state_dir == release_root or release_root in state_dir.parents:
        _fail("Kalıcı veri klasörü yazılım sürüm klasörünün dışında olmalı.")
    _state_override(state_dir)
    api_key_env = _text(connection["api_key_env"], 128)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", api_key_env):
        _fail("Langflow anahtarı için yalnız geçerli bir ortam değişkeni adı yazılmalı.")
    database = connection["database_path"]
    database = None if database is None else _absolute_path(database)
    return RuntimeSettings(source_root=source_root, configured=True, state_dir=state_dir,
                           queue_path=state_dir / "cases" / "requests.db", output_dir=state_dir / "cases" / "drafts",
                           monitor_dir=state_dir / "monitor", langflow_url=validate_langflow_url(connection["base_url"]),
                           flow_id=_flow_id(connection["flow_id"]), api_key_env=api_key_env,
                           langflow_database=database, config_path=path)
