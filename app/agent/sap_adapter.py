"""Pinned, allowlisted HTTPS GETs for one SAP service; no writes or retries."""

from __future__ import annotations

import base64
import ipaddress
import os
import re
import socket
import ssl
import time
from contextlib import suppress
from urllib.parse import urlencode

import urllib3
from sap_connection import (
    SAPConfigurationError,
    forbidden_address,
    origin_parts,
    parse_json,
    preflight,
    validate_connection,
)


class SAPReadError(RuntimeError):
    """Safe failure; request_attempted distinguishes pre-dispatch rejection."""

    def __init__(self, request_attempted=False):
        super().__init__("SAP salt okunur isteği doğrulanamadı; bağlantı, yetki, TLS veya yanıt sınırını kontrol edin")
        self.request_attempted = request_attempted


def _addresses(host, port, networks):
    try:
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            raw_addresses = [item[4][0] for item in resolved]
            if any("%" in value for value in raw_addresses):
                raise ValueError
            addresses = [ipaddress.ip_address(value) for value in raw_addresses]
        if not addresses or any(
            forbidden_address(address) or not any(address in network for network in networks) for address in addresses
        ):
            raise ValueError
        return str(addresses[0])
    except (ValueError, OSError, TypeError, IndexError):
        raise SAPReadError() from None


def _credential(name, *, bearer=False):
    value = os.environ.get(name)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 8192
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or (bearer and any(ord(char) < 33 or ord(char) > 126 for char in value))
    ):
        raise SAPReadError()
    return value


def _headers(connection, accept, host, port):
    authority = "[" + host + "]" if ":" in host else host
    if port != 443:
        authority += ":" + str(port)
    headers = {"Accept": accept, "Accept-Encoding": "identity", "Host": authority}
    auth = connection.auth
    if auth.mode == "basic":
        username, password = _credential(auth.username_env), _credential(auth.password_env)
        if ":" in username:
            raise SAPReadError()
        headers["Authorization"] = "Basic " + base64.b64encode((username + ":" + password).encode()).decode("ascii")
    elif auth.mode == "bearer":
        headers["Authorization"] = "Bearer " + _credential(auth.bearer_env, bearer=True)
    return headers


def _read(connection, relative_path, query, accepted_types, accept):
    attempted, pool, response = False, None, None
    try:
        connection = validate_connection(connection)
        if preflight(connection)["status"] != "configured":
            raise SAPReadError()
        _, host, port = origin_parts(connection.origin)
        address = _addresses(host, port, connection.allowed_networks)
        context = ssl.create_default_context()
        if connection.tls.ca_file is not None:
            context.load_verify_locations(cafile=connection.tls.ca_file)
        headers = _headers(connection, accept, host, port)
        parameters = dict(query)
        if connection.sap_client is not None:
            parameters["sap-client"] = connection.sap_client
        target = connection.service_root + relative_path
        if parameters:
            target += "?" + urlencode(parameters)
        pool = urllib3.HTTPSConnectionPool(
            address,
            port,
            ssl_context=context,
            server_hostname=host,
            assert_hostname=host,
            retries=False,
            timeout=urllib3.Timeout(
                total=connection.timeout_seconds, connect=connection.timeout_seconds, read=connection.timeout_seconds
            ),
        )
        started = time.monotonic()
        attempted = True
        response = pool.urlopen("GET", target, headers=headers, redirect=False, retries=False, preload_content=False)
        content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
        encoding = str(response.headers.get("Content-Encoding", "identity")).strip().lower()
        length = response.headers.get("Content-Length")
        if (
            response.status != 200
            or content_type not in accepted_types
            or encoding not in {"", "identity"}
            or (length is not None and (not str(length).isdigit() or int(length) > connection.max_response_bytes))
        ):
            raise SAPReadError(attempted)
        chunks = bytearray()
        # read1 returns after one buffered/network read. stream(amt) can wait
        # for amt bytes while a trickling peer keeps resetting its idle timer.
        # DNS and header acquisition still have no hard end-to-end deadline.
        while True:
            if time.monotonic() - started >= connection.timeout_seconds:
                raise SAPReadError(attempted)
            chunk = response.read1(16_384, decode_content=False)
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > connection.max_response_bytes or time.monotonic() - started >= connection.timeout_seconds:
                raise SAPReadError(attempted)
        if (
            not chunks
            or time.monotonic() - started >= connection.timeout_seconds
            or (length is not None and len(chunks) != int(length))
        ):
            raise SAPReadError(attempted)
        return bytes(chunks)
    except Exception:
        # Provider bodies, URLs and credentials never enter the public error.
        raise SAPReadError(attempted) from None
    finally:
        if response is not None:
            with suppress(Exception):
                response.close()
        if pool is not None:
            with suppress(Exception):
                pool.close()


def fetch_metadata(connection):
    """Fetch only this service's metadata; XML parsing is the caller's gate."""
    return _read(connection, "$metadata", {}, {"application/xml", "text/xml"}, "application/xml")


def fetch_requisition(connection, pr, item):
    """Read a filtered PR+item collection; nextLink is returned, never followed."""
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", value) for value in (pr, item)):
        raise SAPReadError()
    try:
        fields = connection.field_map
        expression = (
            fields["purchase_requisition"]
            + " eq '"
            + pr
            + "' and "
            + fields["purchase_requisition_item"]
            + " eq '"
            + item
            + "'"
        )
        query = {
            "$filter": expression,
            "$select": ",".join(value for value in fields.values() if value is not None),
            "$top": "2",
            "$format": "json",
        }
        raw = _read(connection, connection.entity_set, query, {"application/json"}, "application/json")
        payload = parse_json(raw)
        if not isinstance(payload, dict):
            raise SAPReadError(True)
        return payload
    except SAPReadError:
        raise
    except SAPConfigurationError:
        raise SAPReadError(True) from None
    except Exception:
        raise SAPReadError() from None
