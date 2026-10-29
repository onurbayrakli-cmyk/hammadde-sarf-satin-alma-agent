"""Seçilen kalemleri takip eder; anahtarları Langflow çalışma anında çözer."""

import argparse
import copy
import json
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from runtime_config import get_runtime

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[2]
sys.path.insert(0, str(ROOT / "components"))
from case_store import CaseStore
from case_worker import _case_name, _instant, _material_name, _save_text
from procurement_research import (
    GATE_MARKER,
    INTAKE_MARKER,
    RESEARCH_MARKER,
    STOCK_MARKER,
    normalize_profile,
    selection_profile,
)
from report_table import render_research
from stock_rules import StockProcurementRulesComponent

RUNTIME = get_runtime()
FLOW_ID = RUNTIME.flow_id
NODE_ID = "ProcurementResearch-PiyasaTakibi"
BASE = RUNTIME.langflow_url
MONITOR = RUNTIME.monitor_dir


def case_research_path(request_id, monitor_dir=MONITOR):
    return Path(monitor_dir) / "cases" / (_case_name(request_id) + ".json")


def resolve_request_selection(graph, request):
    """Make a temporary one-material node; never pass quantities/mail to web."""
    node = copy.deepcopy(next(node for node in graph["nodes"] if node["id"] == NODE_ID))
    template = node["data"]["node"]["template"]

    def value(name, default=""):
        return template.get(name, {}).get("value", default)

    raw = selection_profile(value("research_profile"), value("material_selection", []),
                            value("material_catalog"), value("excluded_suppliers", []), value("allow_demo", False),
                            value("custom_materials"))
    code, name = request.get("material_code"), request.get("material_name")
    if not isinstance(code, str) or not code or len(code) > 80 or not isinstance(name, str) or not name or len(name) > 200:
        raise ValueError("Araştırma için açık malzeme kodu ve adı gerekli")
    matching = [item for item in raw.get("materials", []) if isinstance(item, dict)
                and item.get("material_code") == code and _material_name(item.get("name")) == _material_name(name)]
    if len(matching) > 1:
        raise ValueError("Araştırma malzeme kapsamı belirsiz")
    material = {**(matching[0] if matching else {}), "material_code": code, "name": name}
    raw.update(materials=[material], use_stock_materials=False, allow_demo=False)
    # Runtime checks need the owner-confirmed state and cutoff even if the
    # queued native build starts later. No requester, quantity, price or body
    # is copied into this isolated research node.
    envelope = {"status": "ready", "request": {"state": request.get("state"),
                "deadline_at": request.get("deadline_at")}, "materials": [{"material_code": code, "name": name}]}
    source = INTAKE_MARKER + json.dumps(envelope, ensure_ascii=False) + "\n\n" + STOCK_MARKER + json.dumps({"rows": [], "errors": []})
    profile = normalize_profile(raw, source)
    # UI fields still override research_profile in the component, so disable only
    # those overrides in this temporary node; saved graph fields remain intact.
    for key, field_value in (("research_profile", json.dumps(profile, ensure_ascii=False)),
                             ("material_selection", []), ("use_incoming_materials", False),
                             ("allow_demo", False)):
        template.setdefault(key, {})["value"] = field_value
    return node, source, profile


def _canonical_case_research(raw, request, now):
    if not isinstance(raw, dict) or not raw.get("as_of"):
        raise ValueError("Araştırma kaydının zamanı eksik")
    if raw.get("request_id") not in {None, request["id"]}:
        raise ValueError("Araştırma başka talebe ait")
    as_of = _instant(raw["as_of"])
    if as_of > _instant(now):
        raise ValueError("Araştırma kaydı gelecekte")
    source = STOCK_MARKER + json.dumps({"rows": [], "errors": []})
    profile = normalize_profile(raw.get("profile"), source)
    materials = raw.get("materials")
    if not isinstance(materials, list) or len(materials) != 1 or len(profile["materials"]) != 1:
        raise ValueError("Talep araştırması tek malzemeli olmalı")
    code, name = request["material_code"], _material_name(request["material_name"])
    if any(not isinstance(item, dict) or item.get("material_code") != code or _material_name(item.get("name")) != name
           for item in [materials[0], profile["materials"][0]]):
        raise ValueError("Araştırma malzemesi taleple eşleşmiyor")
    supplier_keys = {"evidence_id", "name", "title", "url", "snippet", "retrieved_at", "verification", "existing_supplier_match", "directory_flag"}
    news_keys = {"evidence_id", "title", "url", "publisher", "published_at", "retrieved_at", "verification"}
    material = {"material_code": code, "name": request["material_name"], "suppliers": [], "news": []}
    dropped = 0
    for kind, keys in (("suppliers", supplier_keys), ("news", news_keys)):
        items = materials[0].get(kind, [])
        if not isinstance(items, list) or len(items) > 100:
            raise ValueError("Araştırma kaynak listesi geçersiz")
        for item in items:
            if not isinstance(item, dict) or not item.get("retrieved_at"):
                dropped += 1
                continue
            try:
                retrieved = _instant(item["retrieved_at"])
                if retrieved > as_of:
                    raise ValueError
                if kind == "news" and (not item.get("published_at") or _instant(item["published_at"]) > retrieved):
                    raise ValueError
            except ValueError:
                dropped += 1
                continue
            material[kind].append({key: value for key, value in item.items() if key in keys})
    status = raw.get("status")
    if not isinstance(status, str) or status not in {"ok", "completed", "partial", "error", "empty", "awaiting_selection"}:
        status = "partial"
    if dropped:
        status = "partial"
    performed, attempted = raw.get("performed_search"), raw.get("search_attempted")
    if not any(performed is value for value in (True, False, None)) or not any(attempted is value for value in (True, False, None)):
        raise ValueError("Araştırma çağrı durumu geçersiz")
    return {"request_id": request["id"], "deadline_at": request["deadline_at"], "as_of": as_of.isoformat(),
            "status": status, "profile": profile, "materials": [material],
            "performed_search": performed, "search_attempted": attempted,
            "warnings": ["Bazı araştırma kayıtları tarih kontrolünden geçmedi."] if dropped else [],
            "full_multi_model_run_verified": False}


def _request_cache(path, request, now):
    if not Path(path).is_file() or Path(path).stat().st_size > 250_000:
        return None
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if (raw.get("request_id") != request["id"] or not raw.get("deadline_at")
                or _instant(raw["deadline_at"]) != _instant(request["deadline_at"])):
            return None
        return _canonical_case_research(raw, request, now)
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        return None


def run_request_research(request_id, *, run=False, queue_path=RUNTIME.queue_path,
                         monitor_dir=MONITOR, now=None, graph_loader=None, runner=None):
    """Collect a confirmed request once; cached partial results are not recharged."""
    common = {"request_id": request_id, "performed_search": False, "full_multi_model_run_verified": False,
              "orders_placed": False, "outbound_sent": False}
    if not Path(queue_path).is_file():
        return {**common, "status": "request_queue_unavailable"}
    try:
        snapshot = CaseStore(queue_path).snapshot(request_id, now)
    except (ValueError, sqlite3.Error):
        return {**common, "status": "request_unavailable"}
    request = snapshot["request"]
    if request["state"] != "collecting" or _instant(now) >= _instant(request["deadline_at"]):
        return {**common, "status": "awaiting_deadline" if request["deadline_at"] is None else "deadline_elapsed"}
    path = case_research_path(request_id, monitor_dir)
    cached = _request_cache(path, request, now)
    if cached is not None:
        return {**common, "status": "request_research_cached", "research_status": cached["status"],
                "prior_search_outcome_unknown": cached.get("performed_search") is None and cached.get("search_attempted") is True,
                "research_path": str(path.resolve()), "deadline_at": request["deadline_at"]}
    if not run:
        return {**common, "status": "request_research_ready", "deadline_at": request["deadline_at"]}
    import fcntl
    Path(monitor_dir).mkdir(parents=True, exist_ok=True)
    with (Path(monitor_dir) / "run.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**common, "status": "research_already_running"}
        # The waiting period and another process's cache can change before lock.
        request = CaseStore(queue_path).snapshot(request_id, now)["request"]
        if request["state"] != "collecting":
            return {**common, "status": "deadline_elapsed"}
        cached = _request_cache(path, request, now)
        if cached is not None:
            return {**common, "status": "request_research_cached", "research_status": cached["status"],
                    "prior_search_outcome_unknown": cached.get("performed_search") is None and cached.get("search_attempted") is True,
                    "research_path": str(path.resolve())}
        node, source, _ = resolve_request_selection((graph_loader or current_graph)(), request)
        raw, job_id = (runner or run_research)(node, source)
        if isinstance(raw, dict) and raw.get("status") in {
                "deadline_elapsed", "awaiting_deadline", "invalid_request_context", "request_not_collecting"}:
            return {**common, "status": raw["status"], "job_id": job_id,
                    "performed_search": raw.get("performed_search"),
                    "attempted_search": raw.get("search_attempted", False),
                    "deadline_at": request["deadline_at"]}
        canonical = _canonical_case_research(raw, request, now)
        _save_text(path, json.dumps(canonical, ensure_ascii=False, indent=2, allow_nan=False))
        return {**common, "status": "request_research_saved", "research_status": canonical["status"],
                "research_path": str(path.resolve()), "job_id": job_id,
                "performed_search": canonical["performed_search"], "attempted_search": canonical["search_attempted"],
                "deadline_at": request["deadline_at"], "as_of": canonical["as_of"]}


def confirmed_request_materials(queue=RUNTIME.queue_path):
    """Süresi onaylanmış kalem adları; e-posta gövdesi ve miktar okunmaz."""
    if not queue.is_file():
        return []
    with sqlite3.connect(queue.as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute("""SELECT DISTINCT json_extract(payload,'$.material_code'),
            json_extract(payload,'$.material_name') FROM requests
            WHERE deadline_at IS NOT NULL ORDER BY 1,2""").fetchall()
    if len(rows) > 10:
        raise ValueError("Süreli dosyalarda 10'dan fazla malzeme var; haber takibi için en çok 10 kalemi seçin.")
    return [{"material_code": code, "name": name} for code, name in rows if code and name]


def current_graph():
    """Yalnız seçimi kullanır; ham akış veya sır alanları çıktıya yazılmaz."""
    if RUNTIME.configured:
        from langflow_client import fetch_flow
        return fetch_flow(RUNTIME)["data"]
    database = PROJECT / ".codex/data/langflow.db"
    if database.exists():
        with sqlite3.connect("file:" + str(database) + "?mode=ro", uri=True) as connection:
            row = connection.execute("select data from flow where id = ?", (FLOW_ID.replace("-", ""),)).fetchone()
            if row is None:
                row = connection.execute("select data from flow where id = ?", (FLOW_ID,)).fetchone()
        if row:
            data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            if any(node["id"] == NODE_ID for node in data["nodes"]):
                return data
    return json.loads((ROOT / "Satin_Alma_Stok_OpenAI_Claude.json").read_text())["data"]


def resolve_selection(graph):
    node = next(node for node in graph["nodes"] if node["id"] == NODE_ID)
    template = node["data"]["node"]["template"]
    def value(name, default=""):
        return template.get(name, {}).get("value", default)
    raw = selection_profile(value("research_profile"), value("material_selection", []),
                            value("material_catalog"), value("excluded_suppliers", []), value("allow_demo", False),
                            value("custom_materials"))
    stock_input = next((n for n in graph["nodes"] if n["data"]["type"] == "ChatInput"), None)
    text = stock_input["data"]["node"]["template"]["input_value"].get("value", "") if stock_input else ""
    if stock_input is None and (raw.get("use_stock_materials") or
            (not raw.get("materials") and value("use_incoming_materials", False))):
        raw["materials"] = confirmed_request_materials()
        raw["use_stock_materials"] = False
        source = STOCK_MARKER + json.dumps({"rows": [], "errors": []})
    elif raw.get("use_stock_materials"):
        stock = StockProcurementRulesComponent()
        stock.set(input_value=text)
        source = stock.build_report().text
    else:
        source = STOCK_MARKER + json.dumps({"rows": [], "errors": []})
    profile = normalize_profile(raw, source)
    return node, source, profile


def ensure_server():
    try:
        response = requests.get(BASE + "/health", timeout=3)
        if response.ok and response.json().get("status") == "ok":
            return
    except (requests.RequestException, ValueError):
        pass
    if RUNTIME.configured:
        raise RuntimeError("Yapılandırılmış Langflow servisi erişilebilir değil; hedef bilgisayarda servisi kontrol edin")
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 7860)) == 0:
            raise RuntimeError("7860 portunda farklı veya sağlıksız uygulama var; otomatik kapatılmadı")
    MONITOR.mkdir(parents=True, exist_ok=True)
    with (MONITOR / "server_start.log").open("a") as log:
        subprocess.Popen([str(Path.home() / ".local/bin/uv"), "run", "--no-sync", "langflow", "run",
                          "--host", "127.0.0.1", "--port", "7860", "--env-file", ".env",
                          "--frontend-path", "src/frontend/build", "--no-open-browser"],
                         cwd=PROJECT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(45):
        time.sleep(1)
        try:
            response = requests.get(BASE + "/health", timeout=2)
            if response.ok and response.json().get("status") == "ok":
                return
        except (requests.RequestException, ValueError):
            pass
    raise RuntimeError("Langflow zamanında açılamadı; takip çalışmadı")


def run_research(node, source):
    ensure_server()
    if RUNTIME.configured:
        from langflow_client import session_for
        session = session_for(RUNTIME)
    else:
        session = requests.Session()
        response = session.get(BASE + "/api/v1/auto_login", timeout=10)
        response.raise_for_status()
        session.headers["Authorization"] = "Bearer " + response.json()["access_token"]
    node = copy.deepcopy(node)
    template = node["data"]["node"]["template"]
    template["source_data"]["value"] = source
    template["save_monitor_result"]["value"] = True
    # Sır yerine global değişken referansı. Anahtar yalnız sunucu bileşenine çözülür.
    template["anthropic_api_key"]["value"] = "ANTHROPIC_API_KEY"
    template["anthropic_api_key"]["load_from_db"] = True
    start = time.time()
    response = session.post(BASE + "/api/v1/build/" + FLOW_ID + "/flow",
                            params={"event_delivery": "polling", "log_builds": "false"},
                            json={"data": {"nodes": [node], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
                                  "inputs": {"input_value": "", "session": "hammadde-takip-" + uuid.uuid4().hex}}, timeout=20)
    response.raise_for_status()
    job_id = response.json()["job_id"]
    finished, valid, output_research = False, False, None
    while time.time() - start < 300:
        response = session.get(BASE + "/api/v1/build/" + job_id + "/events",
                               params={"event_delivery": "polling"}, timeout=35)
        response.raise_for_status()
        for line in response.text.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            data = event.get("data", {})
            if event.get("event") == "end_vertex":
                build = data.get("build_data", {})
                if build.get("id") == NODE_ID:
                    valid = build.get("valid") is True
                    # A blocked runtime gate intentionally does not overwrite
                    # the last good global research file. Read its explicit
                    # result from the native output rather than mistaking an
                    # old disk artifact for this build's output.
                    message = build.get("data", {}).get("outputs", {}).get("context", {}).get("message", "")
                    if isinstance(message, str) and RESEARCH_MARKER in message:
                        try:
                            output_research = json.loads(message.rsplit(RESEARCH_MARKER, 1)[1])
                        except ValueError:
                            output_research = None
                    # Unconnected group outputs are not built in every native
                    # graph. The connected context therefore carries the same
                    # bounded metadata before any long source/profile content.
                    if isinstance(message, str) and message.startswith(GATE_MARKER):
                        line = message[len(GATE_MARKER):].split("\n", 1)[0]
                        if len(line) < 1000:
                            try:
                                short_gate = json.loads(line)
                                if isinstance(short_gate, dict):
                                    output_research = short_gate
                            except ValueError:
                                pass
                    gate = build.get("data", {}).get("outputs", {}).get("gate_status", {}).get("message", "")
                    if isinstance(gate, str) and gate.startswith(GATE_MARKER) and len(gate) < 1000:
                        try:
                            short_gate = json.loads(gate[len(GATE_MARKER):])
                            if isinstance(short_gate, dict):
                                output_research = short_gate
                        except ValueError:
                            pass
            if event.get("event") == "end":
                finished = True
        if finished:
            break
        time.sleep(0.5)
    if (finished and valid and isinstance(output_research, dict)
            and output_research.get("status") in {"deadline_elapsed", "awaiting_deadline",
                                                 "invalid_request_context", "request_not_collecting"}):
        return output_research, job_id
    path = MONITOR / "current_research.json"
    if not finished or not valid or not path.exists() or path.stat().st_mtime < start:
        raise RuntimeError("Araştırma koşusu tam ve güncel dosyayla doğrulanamadı")
    research = json.loads(path.read_text())
    return research, job_id


def fingerprints(research):
    return sorted((material["material_code"], kind, item["evidence_id"], item["title"], item.get("published_at"))
                  for material in research.get("materials", []) for kind in ("suppliers", "news")
                  for item in material.get(kind, []))


def monitor_exit_code(result):
    """Waiting/empty work is normal; a failed research job must fail the CLI."""
    state = result.get("status")
    if state in {"request_queue_unavailable", "request_unavailable", "invalid_profile", "invalid_request_context"}:
        return 2
    if (state in {"request_research_failed", "monitor_failed", "error", "failed"}
            or result.get("research_status") in {"error", "failed", "invalid_profile"}
            or result.get("prior_search_outcome_unknown") is True
            or (result.get("performed_search") is None and result.get("attempted_search") is True)):
        return 1
    return 0


def _run_general(args):
    graph = current_graph()
    node, source, profile = resolve_selection(graph)
    selected = [{"material_code": item["material_code"], "name": item["name"]} for item in profile["materials"]]
    if not args.run or not selected:
        return {"status": "ready" if selected else "awaiting_selection", "selected_materials": selected,
                "region": "Türkiye", "schedule": "Pazartesi ve cuma 09:00 Europe/Istanbul", "performed_search": False}
    MONITOR.mkdir(parents=True, exist_ok=True)
    import fcntl
    with (MONITOR / "run.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "research_already_running", "performed_search": False}
        research, job_id = run_research(node, source)
        state_path = MONITOR / "last_seen.json"
        previous = json.loads(state_path.read_text()) if state_path.exists() else None
        changed = previous is None or fingerprints(previous) != fingerprints(research) or previous.get("status") != research.get("status")
        report = "\n".join(render_research(RESEARCH_MARKER + json.dumps(research, ensure_ascii=False), "", []))
        _save_text(MONITOR / "latest_report.txt", report)
        _save_text(state_path, json.dumps(research, ensure_ascii=False, indent=2))
        return {"checked_at": datetime.now(timezone.utc).isoformat(), "status": research["status"], "changed": changed,
                "selected_materials": selected, "job_id": job_id,
                "performed_search": research.get("performed_search"), "attempted_search": research.get("search_attempted"),
                "report_path": str(MONITOR / "latest_report.txt"), "research_path": str(MONITOR / "current_research.json"),
                "full_multi_model_run": False, "orders_placed": False}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="Seçilen kalemler için kaynakları yeniler")
    parser.add_argument("--request-id", help="Süresi onaylanmış tek talebi bir kez araştırır")
    args = parser.parse_args(argv)
    try:
        if args.request_id:
            result = run_request_research(args.request_id, run=args.run)
        else:
            result = _run_general(args)
    except (ValueError, TypeError, KeyError, StopIteration, OSError, sqlite3.Error,
            requests.RequestException, RuntimeError, ImportError):
        result = {"status": "request_research_failed" if args.request_id else "monitor_failed",
                  "performed_search": None if args.run else False, "attempted_search": args.run,
                  "reason": "Talep, yerel araştırma veya kaynak dosyası doğrulanamadı", "outbound_sent": False}
        if args.request_id:
            result["request_id"] = args.request_id
    print(json.dumps(result, ensure_ascii=False))
    return monitor_exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
