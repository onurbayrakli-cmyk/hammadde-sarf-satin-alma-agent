"""Non-secret, explicit configuration for a bounded SAP read-only connection."""

from __future__ import annotations

import ipaddress
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit, urlunsplit

MAX_CONFIG_BYTES = 32_768
MAX_RESPONSE_BYTES = 1_048_576
FIELDS = {
    "purchase_requisition": "PurchaseRequisition",
    "purchase_requisition_item": "PurchaseRequisitionItem",
    "material_code": "Material",
    "name": "PurchaseRequisitionItemText",
    "quantity": "RequestedQuantity",
    "ordered_quantity": "OrderedQuantity",
    "unit": "BaseUnit",
    "requester_email": None,
    "deleted": None,
    "closed": None,
    "blocked": None,
    "released": None,
}
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
ENVIRONMENT_KEYS = {"mode", "username_env", "password_env", "bearer_env"}
CONFIG_KEYS = {
    "schema_version",
    "enabled",
    "source_system",
    "origin",
    "service_root",
    "entity_set",
    "sap_client",
    "allowed_networks",
    "auth",
    "tls",
    "field_map",
    "max_response_bytes",
    "timeout_seconds",
}
METADATA_ADDRESSES = {
    ipaddress.ip_address(value) for value in ("169.254.169.254", "168.63.129.16", "100.100.100.200", "fd00:ec2::254")
}


class SAPConfigurationError(ValueError):
    """A safe error that never contains configuration values or credentials."""


def _fail(message="SAP bağlantı yapılandırması beklenen güvenli şemayla uyuşmuyor"):
    raise SAPConfigurationError(message) from None


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("SAP JSON alanları yinelenmemeli")
        result[key] = value
    return result


def _constant(_value):
    _fail("SAP JSON yalnız sonlu sayılar içermeli")


def check_json(value, depth=0):
    if depth > 12 or (isinstance(value, float) and not math.isfinite(value)):
        _fail("SAP JSON derinlik veya sonlu sayı sınırını aşıyor")
    if isinstance(value, dict):
        for item in value.values():
            check_json(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            check_json(item, depth + 1)


def parse_json(raw):
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
        check_json(value)
        return value
    except SAPConfigurationError:
        raise
    except (ValueError, UnicodeError, RecursionError, TypeError):
        _fail("SAP JSON okunamadı")


def _text(value, maximum=2048):
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        _fail()
    return value


def origin_parts(value):
    """Validate an origin only; neither DNS nor environment values are read."""
    value = _text(value)
    try:
        parsed = urlsplit(value)
        host, raw_port = parsed.hostname, parsed.port
        port = 443 if raw_port is None else raw_port
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "?" in value
            or "#" in value
            or "\\" in value
            or "%" in value
            or not host.isascii()
            or len(host) > 253
            or host.endswith(".")
            or parsed.netloc.endswith(":")
            or not 1 <= port <= 65535
        ):
            _fail()
        host = host.lower()
        if host == "localhost" or host.endswith(".localhost") or host in {"metadata", "metadata.google.internal"}:
            _fail()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
                _fail()
        else:
            if forbidden_address(address):
                _fail()
        authority = "[" + host + "]" if ":" in host else host
        if port != 443:
            authority += ":" + str(port)
        return urlunsplit(("https", authority, "", "", "")), host, port
    except SAPConfigurationError:
        raise
    except (ValueError, TypeError):
        _fail()


def forbidden_address(address):
    return (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        or getattr(address, "ipv4_mapped", None) is not None
        or address in METADATA_ADDRESSES
    )


@dataclass(frozen=True)
class SAPAuth:
    mode: str
    username_env: str | None
    password_env: str | None
    bearer_env: str | None


@dataclass(frozen=True)
class SAPTLS:
    ca_file: Path | None


@dataclass(frozen=True)
class Connection:
    enabled: bool
    source_system: str
    origin: str | None
    service_root: str | None
    entity_set: str | None
    sap_client: str | None
    allowed_networks: tuple
    auth: SAPAuth
    tls: SAPTLS
    field_map: MappingProxyType
    max_response_bytes: int
    timeout_seconds: int
    config_path: Path | None = None


def _validate(value, config_path=None):
    if not isinstance(value, dict) or set(value) != CONFIG_KEYS:
        _fail()
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or type(value["enabled"]) is not bool:
        _fail()
    source = _text(value["source_system"], 100)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", source):
        _fail()
    origin = None if value["origin"] is None else origin_parts(value["origin"])[0]
    service = value["service_root"]
    if service is not None:
        service = _text(service)
        if (
            not service.startswith("/sap/")
            or not service.endswith("/")
            or "//" in service
            or not re.fullmatch(r"/[A-Za-z0-9_.~/-]+/", service)
            or any(segment in {".", ".."} for segment in service.split("/"))
        ):
            _fail()
    entity = value["entity_set"]
    if entity is not None and (not isinstance(entity, str) or not IDENTIFIER.fullmatch(entity)):
        _fail()
    client = value["sap_client"]
    if client is not None and (not isinstance(client, str) or not re.fullmatch(r"[0-9]{3}", client)):
        _fail()
    raw_networks = value["allowed_networks"]
    if not isinstance(raw_networks, list) or len(raw_networks) > 16:
        _fail()
    networks = []
    for raw in raw_networks:
        try:
            network = ipaddress.ip_network(_text(raw, 80), strict=True)
        except (ValueError, TypeError):
            _fail()
        if network.prefixlen < (8 if network.version == 4 else 32) or network in networks:
            _fail()
        networks.append(network)
    auth = value["auth"]
    if (
        not isinstance(auth, dict)
        or set(auth) != ENVIRONMENT_KEYS
        or not isinstance(auth["mode"], str)
        or auth["mode"] not in {"none", "basic", "bearer"}
    ):
        _fail()
    for key in ("username_env", "password_env", "bearer_env"):
        if auth[key] is not None and (not isinstance(auth[key], str) or not ENV_NAME.fullmatch(auth[key])):
            _fail()
    expected = {"none": (False, False, False), "basic": (True, True, False), "bearer": (False, False, True)}[
        auth["mode"]
    ]
    if tuple(auth[key] is not None for key in ("username_env", "password_env", "bearer_env")) != expected:
        _fail()
    tls = value["tls"]
    if not isinstance(tls, dict) or set(tls) != {"ca_file"}:
        _fail()
    ca_file = None
    if tls["ca_file"] is not None:
        raw = _text(tls["ca_file"])
        ca_file = Path(raw)
        if not ca_file.is_absolute() or ".." in ca_file.parts:
            _fail()
    mapping = value["field_map"]
    if not isinstance(mapping, dict) or not set(mapping) <= FIELDS.keys():
        _fail()
    merged = dict(FIELDS)
    for key, field in mapping.items():
        if not isinstance(field, str) or not IDENTIFIER.fullmatch(field):
            _fail()
        merged[key] = field
    source_fields = [field for field in merged.values() if field is not None]
    if len(source_fields) != len(set(source_fields)):
        _fail("SAP kaynak alanları farklı iç alanlara yinelenerek eşlenemez")
    for name, lower, upper in (("max_response_bytes", 1, MAX_RESPONSE_BYTES), ("timeout_seconds", 1, 30)):
        if type(value[name]) is not int or not lower <= value[name] <= upper:
            _fail()
    return Connection(
        value["enabled"],
        source,
        origin,
        service,
        entity,
        client,
        tuple(networks),
        SAPAuth(**auth),
        SAPTLS(ca_file),
        MappingProxyType(merged),
        value["max_response_bytes"],
        value["timeout_seconds"],
        config_path,
    )


def load_connection(path):
    """Read a small configuration file; no credential, DNS or SAP read occurs."""
    try:
        location = Path(path).expanduser().resolve()
        with location.open("rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            _fail("SAP yapılandırma dosyası boyut sınırını aşıyor")
        return _validate(parse_json(raw), location)
    except SAPConfigurationError:
        raise
    except (OSError, ValueError, TypeError, RuntimeError):
        _fail("SAP bağlantı yapılandırması okunamadı")


def validate_connection(connection):
    """Revalidate a supplied immutable object before any network operation."""
    if not isinstance(connection, Connection):
        _fail()
    try:
        document = {
            "schema_version": 1,
            "enabled": connection.enabled,
            "source_system": connection.source_system,
            "origin": connection.origin,
            "service_root": connection.service_root,
            "entity_set": connection.entity_set,
            "sap_client": connection.sap_client,
            "allowed_networks": [str(item) for item in connection.allowed_networks],
            "auth": {name: getattr(connection.auth, name) for name in ENVIRONMENT_KEYS},
            "tls": {"ca_file": None if connection.tls.ca_file is None else str(connection.tls.ca_file)},
            "field_map": {name: value for name, value in connection.field_map.items() if value is not None},
            "max_response_bytes": connection.max_response_bytes,
            "timeout_seconds": connection.timeout_seconds,
        }
        return _validate(document, connection.config_path)
    except SAPConfigurationError:
        raise
    except (TypeError, ValueError, AttributeError):
        _fail()


def preflight(connection):
    """Summarize setup without reading secrets, files, DNS, or a service."""
    connection = validate_connection(connection)
    missing = [
        name for name in ("origin", "service_root", "entity_set", "allowed_networks") if not getattr(connection, name)
    ]
    return {
        "status": "disabled" if not connection.enabled else "awaiting_configuration" if missing else "configured",
        "enabled": connection.enabled,
        "missing_fields": missing,
        "auth_mode": connection.auth.mode,
        "custom_ca_configured": connection.tls.ca_file is not None,
        "source_system": connection.source_system,
        "metadata_verified": False,
        "sap_connectivity_verified": False,
        "performed_api_call": False,
        "outbound_sent": False,
        "orders_placed": False,
    }
