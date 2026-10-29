"""Offline target preparation for the purchasing agent; never starts a service.

The release stays immutable. Configuration and generated import files belong in
the operator's external state directory. Langflow assigns a new flow UUID when
the operator imports the generated file through its authenticated UI.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import shutil
import stat
import sys
from pathlib import Path

RELEASE_ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = RELEASE_ROOT / "agent" if (RELEASE_ROOT / "agent" / "runtime_config.py").is_file() else RELEASE_ROOT
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

MAX_FLOW_BYTES = 10_000_000
SOURCE_QUEUE = 'Path.cwd() / ".codex/agents/satin-alma-stok/cases/requests.db"'
SOURCE_MONITOR = 'Path.cwd() / ".codex/agents/satin-alma-stok/monitor"'
SOURCE_AGENT_ROOT = '(Path.cwd() / ".codex/agents/satin-alma-stok").resolve()'
COORDINATOR = "ProcurementRequestCoordinator-Sure"
RESEARCH = "ProcurementResearch-PiyasaTakibi"
RFQ = "SupplierRFQTracking-TeklifIstegi"
EXPECTED_NODES = {
    "StockProcurementRules-StokHesabi": "StockProcurementRules",
    "Agent-OpenAIAnaliz": "Agent",
    "Prompt-KaynakVeAnaliz": "Prompt",
    "Agent-ClaudeKontrol": "Agent",
    "ChatOutput-StokRaporu": "ChatOutput",
    "VerifiedStockTable-SonKontrol": "VerifiedStockTable",
    RESEARCH: "ProcurementResearch",
    "SAPProcurementInput-SAP": "SAPProcurementInput",
    "MailResearchInput-OzelTalep": "MailResearchInput",
    COORDINATOR: "ProcurementRequestCoordinator",
    "SupplierQuoteComparison-Teklifler": "SupplierQuoteComparison",
    "ProcurementManagerPackage-Yonetici": "ProcurementManagerPackage",
    "ChatOutput-Durum": "ChatOutput",
    "NegotiationMarket-Piyasa": "NegotiationMarket",
    "NegotiationPlan-Pazarlik": "NegotiationPlan",
    "NegotiationWriterPrompt-Yazisma": "NegotiationWriterPrompt",
    "NegotiationDraftReview-Kontrol": "NegotiationDraftReview",
    "Agent-Pazarlik": "Agent",
    "PurchaseRequestInput-Talep": "PurchaseRequestInput",
    RFQ: "SupplierRFQTracking",
    "ChatOutput-TeklifIstegi": "ChatOutput",
}
IDENTITY_FIELDS = {
    "_frontend_node_flow_id",
    "_frontend_node_folder_id",
    "flow_id",
    "folder_id",
    "workspace_id",
    "user_id",
    "owner_id",
    "deployment_id",
}
AUTHOR_PATH = re.compile(r"(?:/Users/|/home/|[A-Za-z]:[\\/]Users[\\/])")
KNOWN_SECRET = re.compile(
    r"(?:sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{24,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)
SECRET_NAMES = {
    "apikey",
    "anthropicapikey",
    "openaiapikey",
    "token",
    "accesstoken",
    "refreshtoken",
    "bearertoken",
    "password",
    "secret",
    "clientsecret",
    "credential",
    "credentials",
    "authorization",
    "headers",
    "authentication",
}
CLEAN_FIELDS = {
    "StockProcurementRules": ("input_value",),
    "Agent": ("input_value", "context_id", "project_id", "tools"),
    "Prompt": ("analysis_output", "source_data"),
    "ChatOutput": ("input_value", "context_id", "session_id"),
    "VerifiedStockTable": ("model_output", "research_data", "source_data"),
    "ProcurementResearch": (
        "custom_materials",
        "excluded_suppliers",
        "material_catalog",
        "material_selection",
        "research_profile",
        "source_data",
        "supplier_catalog",
    ),
    "SAPProcurementInput": (
        "deadline_at",
        "endpoint",
        "fixture_payload",
        "purchase_requisition",
        "purchase_requisition_item",
        "requester",
        "source_system",
        "bearer_token",
    ),
    "MailResearchInput": (
        "bot_mailbox",
        "deadline_at",
        "mail_message",
        "material_code",
        "material_name",
        "quantity",
        "request_payload",
        "requester",
        "required_specs_json",
        "unit",
    ),
    "ProcurementRequestCoordinator": ("mail_context", "manual_context", "sap_context", "selected_material_code"),
    "SupplierQuoteComparison": ("case_data",),
    "ProcurementManagerPackage": ("negotiation_draft", "quote_report", "research_report"),
    "NegotiationMarket": ("case_data", "observations_json", "price_history"),
    "NegotiationPlan": ("case_data", "market_context", "price_history"),
    "NegotiationWriterPrompt": ("input_value",),
    "NegotiationDraftReview": ("model_output", "plan"),
    "PurchaseRequestInput": (
        "deadline_at",
        "delivery_place",
        "material_code",
        "material_name",
        "need_by",
        "quantity",
        "request_id",
        "requester",
        "required_specs",
        "unit",
    ),
    "SupplierRFQTracking": ("case_data", "supplier_rows", "queue_path", "output_dir"),
}
DISABLED_FIELDS = {
    "ChatOutput": ("should_store_message",),
    "ProcurementResearch": ("allow_demo", "save_monitor_result"),
    "SAPProcurementInput": ("enable_api",),
    "NegotiationMarket": ("enable_live",),
}


class TargetSetupError(ValueError):
    """A bounded validation failure whose text contains no input values."""


def _runtime(config):
    from runtime_config import get_runtime

    settings = get_runtime(config)
    if not settings.configured:
        raise TargetSetupError("Harici hedef yapılandırması gerekli")
    if settings.state_dir == RELEASE_ROOT or RELEASE_ROOT in settings.state_dir.parents:
        raise TargetSetupError("Durum dizini salt okunur yayın klasörünün dışında olmalı")
    return settings


def _config_bytes(config):
    path = _safe_path(config)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise TargetSetupError("Hedef yapılandırma düzenli dosya olmalı")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read(32_769)
    finally:
        os.close(descriptor)
    if not 0 < len(content) <= 32_768:
        raise TargetSetupError("Hedef yapılandırma boyutu geçersiz")
    return content


def _safe_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise TargetSetupError("Sembolik bağlantı hedef hazırlığında kullanılamaz")
    return path.resolve()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TargetSetupError("Akış JSON alanları tekrarlanamaz")
        result[key] = value
    return result


def _read_flow(path):
    path = _safe_path(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_FLOW_BYTES:
            raise TargetSetupError("Akış kaynağı boş, geçersiz veya boyut sınırını aşıyor")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_FLOW_BYTES + 1)
        after = os.fstat(descriptor)
        current = path.stat()
        if (
            len(raw) != before.st_size
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise TargetSetupError("Akış kaynağı okunurken değişti")
    finally:
        os.close(descriptor)
    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(TargetSetupError("Sonlu JSON sayıları gerekli")),
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise TargetSetupError("Akış kaynağı geçerli sınırlı JSON olmalı") from None
    _bounded(parsed)
    return parsed, hashlib.sha256(raw).hexdigest()


def _bounded(value, depth=0):
    if depth > 40:
        raise TargetSetupError("Akış JSON derinlik sınırını aşıyor")
    if isinstance(value, float) and not math.isfinite(value):
        raise TargetSetupError("Sonlu JSON sayıları gerekli")
    if isinstance(value, dict):
        if len(value) > 1000:
            raise TargetSetupError("Akış JSON alan sınırını aşıyor")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 1000:
                raise TargetSetupError("Akış JSON alan adı geçersiz")
            _bounded(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 10_000:
            raise TargetSetupError("Akış JSON liste sınırını aşıyor")
        for item in value:
            _bounded(item, depth + 1)
    elif isinstance(value, str) and len(value) > 2_000_000:
        raise TargetSetupError("Akış JSON metin sınırını aşıyor")


def _fields(node):
    try:
        fields = node["data"]["node"]["template"]
    except (KeyError, TypeError):
        raise TargetSetupError("Akış bileşen şablonu eksik") from None
    if not isinstance(fields, dict):
        raise TargetSetupError("Akış bileşen şablonu geçersiz")
    return fields


def _validate_graph(flow):
    if not isinstance(flow, dict) or not {"name", "data"} <= flow.keys():
        raise TargetSetupError("Temiz satın alma akışı gerekli")
    if set(flow) - {"name", "description", "data"}:
        raise TargetSetupError("Kaynak akış önce yayın paketi için temizlenmeli")
    if not isinstance(flow["name"], str) or not flow["name"].strip():
        raise TargetSetupError("Akış adı eksik")
    data = flow["data"]
    if not isinstance(data, dict) or not {"nodes", "edges"} <= data.keys():
        raise TargetSetupError("Akış düğümleri ve bağlantıları gerekli")
    nodes, edges = data["nodes"], data["edges"]
    if not isinstance(nodes, list) or len(nodes) != 21 or not isinstance(edges, list) or len(edges) != 28:
        raise TargetSetupError("Bu yayın için 21 düğüm ve 28 bağlantı gerekli")
    found = {}
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or node["id"] in found:
            raise TargetSetupError("Akış düğüm kimliği belirsiz")
        found[node["id"]] = node
        if not isinstance(node.get("data"), dict) or node["data"].get("type") != EXPECTED_NODES.get(node["id"]):
            raise TargetSetupError("Akış bileşen türü bu yayınla uyuşmuyor")
        _fields(node)
    if set(found) != set(EXPECTED_NODES):
        raise TargetSetupError("Akış düğümleri bu satın alma yayınıyla uyuşmuyor")
    connections = set()
    for edge in edges:
        try:
            key = (
                edge["source"],
                edge["data"]["sourceHandle"]["name"],
                edge["target"],
                edge["data"]["targetHandle"]["fieldName"],
            )
        except (KeyError, TypeError):
            raise TargetSetupError("Akış bağlantısı eksik") from None
        if not all(isinstance(item, str) and item for item in key):
            raise TargetSetupError("Akış bağlantı alanları geçersiz")
        if key[0] not in found or key[2] not in found or key in connections:
            raise TargetSetupError("Akış bağlantısı belirsiz")
        connections.add(key)
    return found


def _clear_identities(value):
    if isinstance(value, dict):
        for key in list(value):
            if key in IDENTITY_FIELDS:
                if isinstance(value[key], dict):
                    value[key]["value"] = ""
                    value[key]["load_from_db"] = False
                else:
                    value.pop(key)
            else:
                _clear_identities(value[key])
    elif isinstance(value, list):
        for item in value:
            _clear_identities(item)


def _validate_empty_inputs(nodes):
    for node in nodes.values():
        node_type = node["data"]["type"]
        fields = _fields(node)
        for name in CLEAN_FIELDS.get(node_type, ()):
            if name in fields and (
                not isinstance(fields[name], dict) or fields[name].get("value") not in (None, "", [], {})
            ):
                raise TargetSetupError("Akışta önceki kullanıcı girdileri bulunamaz")
        for name in DISABLED_FIELDS.get(node_type, ()):
            if name in fields and (not isinstance(fields[name], dict) or fields[name].get("value") is not False):
                raise TargetSetupError("Akıştaki dış işlem ve demo seçenekleri kapalı olmalı")


def _validate_clean(value, key="", provider=None):
    if key == "password" and isinstance(value, bool):
        return
    secret_name = re.sub(r"[^a-z0-9]", "", key.lower()) in SECRET_NAMES
    if secret_name or (isinstance(value, dict) and value.get("password") is True):
        reference = (
            "ANTHROPIC_API_KEY"
            if key == "anthropic_api_key" or (key == "api_key" and provider == "Anthropic")
            else "OPENAI_API_KEY"
            if key == "openai_api_key" or (key == "api_key" and provider == "OpenAI")
            else ""
        )
        if isinstance(value, dict):
            actual = value.get("value")
            if actual not in (None, "") and (
                not reference or actual != reference or value.get("load_from_db") is not True
            ):
                raise TargetSetupError("Akışta izinli sağlayıcı başvurusu dışında gizli alan bulunamaz")
        elif value not in (None, ""):
            raise TargetSetupError("Akışta doğrudan gizli değer bulunamaz")
    if isinstance(value, dict):
        if isinstance(value.get("template"), dict):
            template = value["template"]
            model = template.get("model", {}).get("value", []) if isinstance(template.get("model"), dict) else []
            template_provider = (
                model[0].get("provider") if isinstance(model, list) and model and isinstance(model[0], dict) else None
            )
            for name, item in template.items():
                _validate_clean(item, name, template_provider)
            for name, item in value.items():
                if name != "template":
                    _validate_clean(item, name, provider)
        else:
            for name, item in value.items():
                _validate_clean(item, name, provider)
    elif isinstance(value, list):
        for item in value:
            _validate_clean(item, provider=provider)
    elif isinstance(value, str):
        if AUTHOR_PATH.search(value):
            raise TargetSetupError("Kaynak kullanıcıya ait mutlak yol içeriyor")
        if KNOWN_SECRET.search(value):
            raise TargetSetupError("Akışta doğrudan gizli değer bulunamaz")


def _same_ast(node, source):
    return ast.dump(node, include_attributes=False) == ast.dump(
        ast.parse(source, mode="eval").body, include_attributes=False
    )


def _replace_spans(code, replacements):
    lines = code.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line.encode("utf-8")))
    raw = code.encode("utf-8")
    spans = [
        (
            offsets[node.lineno - 1] + node.col_offset,
            offsets[node.end_lineno - 1] + node.end_col_offset,
            text.encode("utf-8"),
        )
        for node, text in replacements
    ]
    for start, end, text in sorted(spans, reverse=True):
        raw = raw[:start] + text + raw[end:]
    result = raw.decode("utf-8")
    ast.parse(result)
    return result


def _bind_coordinator(code, queue_path):
    try:
        tree = ast.parse(code)
    except (SyntaxError, TypeError, ValueError):
        raise TargetSetupError("Koordinatör kaynağı ayrıştırılamadı") from None
    defaults = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "DEFAULT_PATH" for target in node.targets)
    ]
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "coordinate_request"]
    if len(defaults) != 1 or not _same_ast(defaults[0].value, SOURCE_QUEUE) or len(functions) != 1:
        raise TargetSetupError("Koordinatör kalıcı yol şekli bu yayınla uyuşmuyor")
    calls = [
        node
        for node in ast.walk(functions[0])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "CaseStore"
    ]
    if len(calls) != 1 or len(calls[0].args) != 1 or calls[0].keywords or not _same_ast(calls[0].args[0], SOURCE_QUEUE):
        raise TargetSetupError("Koordinatör kuyruk bağı bu yayınla uyuşmuyor")
    replacement = "Path(" + repr(str(queue_path)) + ")"
    return _replace_spans(code, [(defaults[0].value, replacement), (calls[0].args[0], replacement)])


def _bind_research(code, monitor_dir):
    try:
        tree = ast.parse(code)
    except (SyntaxError, TypeError, ValueError):
        raise TargetSetupError("Araştırma kaynağı ayrıştırılamadı") from None
    classes = [
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ProcurementResearchComponent"
    ]
    methods = [
        node
        for cls in classes
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "_prepare_research"
    ]
    assignments = [
        node
        for method in methods
        for node in ast.walk(method)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "folder" for target in node.targets)
    ]
    if (
        len(classes) != 1
        or len(methods) != 1
        or len(assignments) != 1
        or not _same_ast(assignments[0].value, SOURCE_MONITOR)
    ):
        raise TargetSetupError("Araştırma kalıcı yol şekli bu yayınla uyuşmuyor")
    return _replace_spans(code, [(assignments[0].value, "Path(" + repr(str(monitor_dir)) + ")")])


def _bind_rfq_dependencies(code, source_root):
    try:
        tree = ast.parse(code)
    except (SyntaxError, TypeError, ValueError):
        raise TargetSetupError("Teklif takibi kaynağı ayrıştırılamadı") from None
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_rt_dependencies"]
    assignments = [
        node
        for function in functions
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "root" for target in node.targets)
    ]
    if len(functions) != 1 or len(assignments) != 1 or not _same_ast(assignments[0].value, SOURCE_AGENT_ROOT):
        raise TargetSetupError("Teklif takibi kaynak klasörü bağı bu yayınla uyuşmuyor")
    return _replace_spans(code, [(assignments[0].value, "Path(" + repr(str(source_root)) + ")")])


def prepare_flow(config, source, output):
    """Write a new offline import file; do not change source, queue or service."""
    original_config = _config_bytes(config)
    settings = _runtime(config)
    if settings.flow_id is not None:
        raise TargetSetupError("Yeni akış içe aktarma hazırlığında flow_id null olmalı")
    if _config_bytes(config) != original_config:
        raise TargetSetupError("Hedef yapılandırması okunurken değişti")
    destination = _safe_path(output)
    if destination == RELEASE_ROOT or RELEASE_ROOT in destination.parents:
        raise TargetSetupError("Hazırlanan dosya salt okunur yayın klasörüne yazılamaz")
    if not destination.parent.is_dir() or destination.exists():
        raise TargetSetupError("Hedef üst dizini mevcut ve hedef dosyası yeni olmalı")
    flow, source_hash = _read_flow(source)
    flow = copy.deepcopy(flow)
    nodes = _validate_graph(flow)
    _clear_identities(flow)
    _validate_clean(flow)
    _validate_empty_inputs(nodes)
    coordinator_fields, research_fields, rfq_fields = (_fields(nodes[key]) for key in (COORDINATOR, RESEARCH, RFQ))
    try:
        coordinator_fields["code"]["value"] = _bind_coordinator(
            coordinator_fields["code"]["value"], settings.queue_path
        )
        research_fields["code"]["value"] = _bind_research(research_fields["code"]["value"], settings.monitor_dir)
        rfq_fields["code"]["value"] = _bind_rfq_dependencies(rfq_fields["code"]["value"], settings.source_root)
        for field, value in (("queue_path", settings.queue_path), ("output_dir", settings.output_dir)):
            rfq_fields[field]["value"] = str(value)
            rfq_fields[field]["load_from_db"] = False
    except (KeyError, TypeError):
        raise TargetSetupError("Kalıcı yol bağlama alanları eksik") from None
    content = (json.dumps(flow, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if _config_bytes(config) != original_config:
        raise TargetSetupError("Hedef yapılandırması hazırlanırken değişti")
    config_hash = hashlib.sha256(original_config).hexdigest()
    descriptor = None
    created = None
    try:
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        created = os.fstat(descriptor)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise TargetSetupError("Hedef dosya zaten var; üzerine yazılmaz") from None
    except OSError:
        if created is not None:
            current = destination.lstat()
            if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                destination.unlink()
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return {
        "schema_version": 1,
        "command": "prepare-flow",
        "status": "prepared_for_manual_import",
        "source_sha256": source_hash,
        "config_sha256": config_hash,
        "output_sha256": hashlib.sha256(content).hexdigest(),
        "nodes": 21,
        "edges": 28,
        "persistent_bindings": 5,
        "source_root_bindings": 1,
        "total_path_bindings": 6,
        "source_flow_id_retained": False,
        "target_flow_id_assigned": False,
        "flow_imported": False,
        "queue_created": False,
        "service_started": False,
        "network_called": False,
        "models_called": False,
        "no_secret_values": True,
    }


def preflight(config):
    """Describe this machine and planned target configuration without probes."""
    _config_bytes(config)
    settings = _runtime(config)
    versions = {}
    for distribution in ("langflow", "langflow-base", "lfx"):
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = None
    uv = shutil.which("uv")
    fallback = Path.home() / ".local" / "bin" / "uv"
    uv_found = bool(uv) or (fallback.is_file() and os.access(fallback, os.X_OK))
    version = sys.version_info
    python_supported = (3, 10) <= version[:2] < (3, 15)
    current_os = platform.system()
    missing = []
    if current_os != "Darwin":
        missing.append("target_macos_not_observed")
    if not python_supported:
        missing.append("supported_python_missing")
    if not uv_found:
        missing.append("uv_command_missing")
    if any(value is None for value in versions.values()):
        missing.append("langflow_runtime_metadata_missing")
    return {
        "schema_version": 1,
        "command": "preflight",
        "status": "requirements_missing" if missing else "local_prerequisites_observed",
        "observed_machine": {
            "os": current_os,
            "os_release": platform.release(),
            "architecture": platform.machine(),
            "python_version": platform.python_version(),
            "python_supported": python_supported,
            "uv_command_available": uv_found,
            "distribution_versions": versions,
        },
        "planned_configuration": {
            "external_state": True,
            "state_directory_exists": settings.state_dir.is_dir(),
            "flow_id_configured": bool(settings.flow_id),
            "api_key_env": settings.api_key_env,
            "database_fallback_configured": settings.langflow_database is not None,
        },
        "missing": missing,
        "target_machine_verified": False,
        "service_connectivity_verified": False,
        "runtime_compatibility_verified": False,
        "api_key_value_read": False,
        "secrets_checked": False,
        "network_called": False,
        "files_written": False,
        "no_secret_values": True,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Satın alma agentı için çevrimdışı hedef hazırlığı")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("preflight")
    check.add_argument("--config", required=True)
    prepare = sub.add_parser("prepare-flow")
    prepare.add_argument("--config", required=True)
    prepare.add_argument("--source", required=True)
    prepare.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = (
            preflight(args.config)
            if args.command == "preflight"
            else prepare_flow(args.config, args.source, args.output)
        )
    except (ValueError, OSError, TypeError, RecursionError, AttributeError):
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": "Hedef hazırlığı doğrulanamadı; yapılandırma, temiz akış ve hedef dosya koşullarını kontrol edin",
                    "no_secret_values": True,
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("missing") else 0


if __name__ == "__main__":
    raise SystemExit(main())
