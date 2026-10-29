"""Explicit Langflow authentication and safe flow reads; no provider calls."""

from __future__ import annotations

import copy
import os
import re
from urllib.parse import urlsplit

import requests

from runtime_config import LEGACY_LANGFLOW_URL, RuntimeSettings, get_runtime, validate_langflow_url

MAX_FLOW_BYTES = 10_000_000
SECRET_NAMES = {"apikey", "anthropicapikey", "openaiapikey", "token", "accesstoken", "refreshtoken",
                "bearertoken", "password", "secret", "clientsecret", "credential", "credentials",
                "authorization", "headers", "authentication"}


class LangflowClientError(RuntimeError):
    """Safe client failure: no response body, headers or supplied key values."""


def _error(message="Langflow bağlantısı doğrulanamadı; adres, yetki ve servis durumu kontrol edilmeli."):
    raise LangflowClientError(message) from None


def _origin(url):
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


class _LangflowSession(requests.Session):
    """Keep API-key headers on the configured origin and disable redirects."""

    def __init__(self, base_url):
        super().__init__()
        self._base_url = base_url
        self.trust_env = False

    def request(self, method, url, **kwargs):
        try:
            target = validate_langflow_url(url)
            prefix = urlsplit(self._base_url).path.rstrip("/")
            target_path = urlsplit(target).path
            if _origin(target) != _origin(self._base_url) or (prefix and target_path != prefix and not target_path.startswith(prefix + "/")):
                _error("Langflow oturumu farklı bir adrese yönlendirilemez.")
            kwargs["allow_redirects"] = False
            return super().request(method, url, **kwargs)
        except LangflowClientError:
            raise
        except (requests.RequestException, ValueError, TypeError):
            _error()


def session_for(settings: RuntimeSettings | None = None):
    """Read a key only now. Configured installations never use auto_login."""
    settings = settings or get_runtime()
    base_url = validate_langflow_url(settings.langflow_url)
    if not settings.configured and base_url != LEGACY_LANGFLOW_URL:
        _error("Yerel geliştirme oturumu yalnız eski yerel adres için kullanılabilir.")
    session = _LangflowSession(base_url)
    if settings.configured:
        name = settings.api_key_env
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name):
            session.close()
            _error("Langflow API anahtarı ortam değişkeni yapılandırılmalı.")
        key = os.environ.get(name)
        if (not isinstance(key, str) or not key or len(key) > 8192
                or any(ord(char) < 33 or ord(char) > 126 for char in key)):
            session.close()
            _error("Langflow API anahtarı ortam değişkeninde yok veya geçersiz.")
        session.headers["x-api-key"] = key
        return session
    try:
        response = session.get(base_url + "/api/v1/auto_login", timeout=10)
        if response.status_code != 200:
            _error()
        token = response.json().get("access_token")
        if not isinstance(token, str) or not token or len(token) > 8192 or any(ord(char) < 33 or ord(char) > 126 for char in token):
            _error()
        session.headers["Authorization"] = "Bearer " + token
        return session
    except (requests.RequestException, ValueError, TypeError, AttributeError, LangflowClientError):
        session.close()
        _error()


def _name(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _provider(template):
    model = template.get("model", {}).get("value", []) if isinstance(template.get("model"), dict) else []
    if isinstance(model, list) and model and isinstance(model[0], dict):
        return model[0].get("provider")
    return None


def safe_flow(flow):
    """Copy a flow and replace only allowlisted provider secrets with DB refs.

    Unknown password/credential fields are blanked, never carried to a worker.
    Existing OpenAI/Anthropic model nodes still resolve their keys in Langflow.
    """
    result = copy.deepcopy(flow)

    def clean(value, key="", provider=None):
        if key == "password" and isinstance(value, bool):
            return value
        secret_name = _name(key) in SECRET_NAMES
        if secret_name and not (isinstance(value, dict) and ("value" in value or value.get("password") is True)):
            return ""
        if isinstance(value, list):
            return [clean(item, provider=provider) for item in value]
        if not isinstance(value, dict):
            return value
        is_secret = value.get("password") is True or secret_name
        if is_secret:
            reference = ("ANTHROPIC_API_KEY" if key == "anthropic_api_key" or (key == "api_key" and provider == "Anthropic")
                         else "OPENAI_API_KEY" if key == "openai_api_key" or (key == "api_key" and provider == "OpenAI") else "")
            field = {name: clean(item, name, provider) for name, item in value.items() if name not in {"value", "load_from_db"}}
            field.update(value=reference, load_from_db=bool(reference))
            return field
        if isinstance(value.get("template"), dict):
            value = dict(value)
            template = value["template"]
            value["template"] = {name: clean(item, name, _provider(template)) for name, item in template.items()}
            return {name: item if name == "template" else clean(item, name, provider) for name, item in value.items()}
        return {name: clean(item, name, provider) for name, item in value.items()}

    return clean(result)


def fetch_flow(settings: RuntimeSettings | None = None):
    """Fetch exactly the configured flow; no local DB, export or other-ID fallback."""
    settings = settings or get_runtime()
    if (not isinstance(settings.flow_id, str)
            or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", settings.flow_id)):
        _error("Langflow akış kimliği henüz yapılandırılmadı.")
    session = session_for(settings)
    try:
        response = session.get(settings.langflow_url + "/api/v1/flows/" + settings.flow_id, timeout=15)
        if response.status_code != 200 or len(response.content) > MAX_FLOW_BYTES:
            _error()
        flow = response.json()
        if (not isinstance(flow, dict) or flow.get("id") != settings.flow_id or not isinstance(flow.get("data"), dict)
                or not isinstance(flow["data"].get("nodes"), list) or not isinstance(flow["data"].get("edges"), list)):
            _error("Langflow yanıtı istenen akışla eşleşmiyor.")
        return safe_flow(flow)
    except (requests.RequestException, ValueError, TypeError, AttributeError, RecursionError, LangflowClientError):
        _error()
    finally:
        session.close()
