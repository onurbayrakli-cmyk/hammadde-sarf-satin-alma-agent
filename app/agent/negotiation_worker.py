"""Kaynaklı pazarlık taslağı hazırlar; e-posta/sipariş göndermez."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "components"))
from case_store import CaseStore
from case_worker import DEFAULT_OUTPUT, DEFAULT_QUEUE, _case_name, _instant, _report_html
from job_store import NegotiationJobs, StaleNegotiation
from monitor_procurement import BASE, FLOW_ID, RUNTIME, current_graph
from negotiation_draft import build_negotiation_prompt, render_negotiation_drafts, review_negotiation_drafts
from negotiation_market import case_market_context
from negotiation_plan import plan_negotiation

AGENT = "Agent-Pazarlik"
MARKET = "NegotiationMarket-Piyasa"
POLICY_VERSION = 2
MAX_ARTIFACT_BYTES = 2_000_000
COMMON = {"emails_sent": False, "orders_placed": False, "full_multi_model_run_verified": False}
SECRET_NAME = re.compile(r"apikey|api_key|token|password|secret|credential|authorization|headers|bearer", re.IGNORECASE)
PUBLIC_TOKEN_PARAMETERS = {
    "maxtokens",
    "maxcompletiontokens",
    "maxoutputtokens",
    "maxinputtokens",
    "maxtotaltokens",
    "mintokens",
    "tokenlimit",
    "tokenbudget",
}


def _sensitive_name(name):
    normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
    return normalized not in PUBLIC_TOKEN_PARAMETERS and (
        bool(SECRET_NAME.search(normalized)) or normalized in {"auth", "authentication", "basicauth", "httpauth"}
    )


def run_writer(plan, graph):
    """Yalnız pazarlık modelini çalıştırır; anahtar Langflow'da çözülür."""
    node = copy.deepcopy(next(n for n in graph["nodes"] if n["id"] == AGENT))
    t = node["data"]["node"]["template"]
    t["input_value"]["value"] = build_negotiation_prompt(plan)
    model = t.get("model", {}).get("value", [])
    provider = model[0].get("provider") if model else None
    if provider not in {"Anthropic", "OpenAI"}:
        raise ValueError("Pazarlık sağlayıcısı henüz desteklenmiyor")
    t["api_key"]["value"] = "ANTHROPIC_API_KEY" if provider == "Anthropic" else "OPENAI_API_KEY"
    t["api_key"]["load_from_db"] = True
    if RUNTIME.configured:
        from langflow_client import session_for
        s = session_for(RUNTIME)
    else:
        s = requests.Session()
        r = s.get(BASE + "/api/v1/auto_login", timeout=10)
        r.raise_for_status()
        s.headers["Authorization"] = "Bearer " + r.json()["access_token"]
    r = s.post(
        BASE + "/api/v1/build/" + FLOW_ID + "/flow",
        params={"event_delivery": "polling", "log_builds": "false"},
        json={
            "data": {"nodes": [node], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}},
            "inputs": {"input_value": "", "session": "pazarlik-" + uuid.uuid4().hex},
        },
        timeout=20,
    )
    r.raise_for_status()
    job = r.json()["job_id"]
    started = time.monotonic()
    done = False
    valid = False
    response = ""
    while time.monotonic() - started < 80:
        r = s.get(BASE + "/api/v1/build/" + job + "/events", params={"event_delivery": "polling"}, timeout=30)
        r.raise_for_status()
        for line in r.text.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("event") == "end_vertex":
                build = event.get("data", {}).get("build_data", {})
                if build.get("id") == AGENT:
                    valid = build.get("valid") is True
                    response = build.get("data", {}).get("outputs", {}).get("response", {}).get("message", "")
            if event.get("event") == "end":
                done = True
        if done:
            break
        time.sleep(0.1)
    if not done or not valid or not isinstance(response, str) or not response.strip():
        raise RuntimeError("Pazarlık modeli tamamlanmış yanıtla doğrulanamadı")
    return response, {"job_id": job, "provider": provider, "run_completed": True}


def _configured_history(graph):
    node = next((n for n in graph["nodes"] if n["id"] == MARKET), None)
    template = node["data"]["node"]["template"] if node else {}
    return template.get("price_history", {}).get("value", ""), template.get("enable_live", {}).get(
        "value", False
    ) is True


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _nonsecret(value):
    if isinstance(value, dict):
        return {
            key: _nonsecret(item) for key, item in value.items() if isinstance(key, str) and not _sensitive_name(key)
        }
    if isinstance(value, list):
        return [_nonsecret(item) for item in value]
    return value


def _policy(graph):
    """Hash behavior, skipping secret fields before reading their values."""
    node = next((item for item in graph["nodes"] if item["id"] == AGENT), None)
    template = node["data"]["node"]["template"] if node else {}
    effective = {}
    for name, field in template.items():
        if (
            _sensitive_name(name)
            or name == "input_value"
            or not isinstance(field, dict)
            or field.get("password") is True
        ):
            continue
        if "value" in field:
            effective[name] = _nonsecret(field["value"])
    modules = {
        name: hashlib.sha256((ROOT / "components" / name).read_bytes()).hexdigest()
        for name in ("negotiation_draft.py", "negotiation_plan.py", "quote_comparison.py", "negotiation_market.py")
    }
    modules["negotiation_worker.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return _digest({"version": POLICY_VERSION, "effective_agent": effective, "modules": modules})


def _fingerprint(case, initial_plan, policy, history, enable_live, observations, use_model):
    request = {
        key: value
        for key, value in case["request"].items()
        if key not in {"state", "report_revision", "offer_revision"}
    }
    return _digest(
        {
            "version": POLICY_VERSION,
            "request": request,
            "offers": case["offers"],
            "offer_revision": case["request"]["offer_revision"],
            "history": _nonsecret(history),
            "enable_live": enable_live,
            "observations": _nonsecret(observations),
            "policy": policy,
            "initial_plan": initial_plan,
            "initial_prompt": build_negotiation_prompt(initial_plan),
            "use_model": bool(use_model),
        }
    )


def _candidate_from_checked(checked):
    return {
        "drafts": [
            {key: row[key] for key in ("request_id", "target_offer_id", "subject", "body")}
            for row in checked.get("drafts", [])
        ]
    }


def _selection(checked):
    return [
        {key: row[key] for key in ("request_id", "target_offer_id", "recipient", "subject", "body")}
        for row in checked.get("drafts", [])
    ]


def _cached_payload(claim, case, history):
    """Verify manifest bytes and reapply current source and language rules."""
    manifest_path, metadata_path = Path(claim["manifest_path"]), Path(claim["metadata_path"])
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or manifest_path.stat().st_size > 64_000
        or hashlib.sha256(manifest_path.read_bytes()).hexdigest() != claim["manifest_sha256"]
    ):
        raise ValueError("Pazarlık manifesti doğrulanamadı")
    manifest = json.loads(manifest_path.read_text())
    for field, expected in (
        ("request_id", case["request"]["id"]),
        ("fingerprint", claim["fingerprint"]),
        ("generation", claim["generation"]),
        ("offer_revision", case["request"]["offer_revision"]),
    ):
        if manifest.get(field) != expected:
            raise ValueError("Pazarlık manifesti başka revizyona ait")
    files = manifest.get("files")
    if (
        not isinstance(files, list)
        or len(files) != 3
        or {item.get("name") for item in files} != {"Pazarlik.txt", "Pazarlik.html", "Pazarlik.json"}
    ):
        raise ValueError("Pazarlık dosya listesi doğrulanamadı")
    for item in files:
        path = manifest_path.parent / item["name"]
        if (
            path.is_symlink()
            or not path.is_file()
            or not 0 < path.stat().st_size <= MAX_ARTIFACT_BYTES
            or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]
        ):
            raise ValueError("Pazarlık dosyası eksik veya değişmiş")
    if metadata_path != manifest_path.parent / "Pazarlik.json":
        raise ValueError("Pazarlık metadata yolu doğrulanamadı")
    payload = json.loads(metadata_path.read_text())
    if (
        payload.get("input_fingerprint") != claim["fingerprint"]
        or payload.get("request_id") != case["request"]["id"]
        or payload.get("text_path") != str(manifest_path.parent / "Pazarlik.txt")
        or payload.get("html_path") != str(manifest_path.parent / "Pazarlik.html")
        or bool(payload.get("model_attempted")) != bool(claim["model_attempted"])
        or payload.get("model", {}).get("run_completed", False) is not bool(claim["model_completed"])
    ):
        raise ValueError("Pazarlık kayıt/dosya bağı doğrulanamadı")
    plan = plan_negotiation(case, price_history=history, market_context=payload.get("market_context"))
    if _digest(plan) != payload.get("plan_fingerprint") or _digest(plan) != _digest(payload.get("plan")):
        raise ValueError("Pazarlık planı güncel kaynaklarla uyuşmuyor")
    original = payload.get("draft_result", {})
    verified = bool(original.get("drafts")) and all(
        row.get("generation_source") == "model_reviewed" and row.get("model_review", {}).get("verified") is True
        for row in original["drafts"]
    )
    checked = review_negotiation_drafts(plan, _candidate_from_checked(original) if verified else None)
    if _selection(checked) != _selection(original) or (
        verified
        and (not claim["model_completed"] or not all(row["model_review"]["verified"] for row in checked["drafts"]))
    ):
        raise ValueError("Önbellek güncel dil/kaynak denetiminden geçmedi")
    return payload


def _public(payload, *, status, metadata_path, manifest_path, refresh_blocked=False):
    checked = payload["draft_result"]
    return {
        "request_id": payload["request_id"],
        "status": status,
        "path": payload["text_path"],
        "html_path": payload["html_path"],
        "metadata_path": str(metadata_path),
        "manifest_path": str(manifest_path),
        "model_attempted": payload["model_attempted"],
        "model_completed": payload["model"]["run_completed"],
        "model_text_verified": bool(checked["drafts"])
        and all(row["model_review"]["verified"] for row in checked["drafts"]),
        "model_outcome_uncertain": payload["model_attempted"] and not payload["model"]["run_completed"],
        "refresh_blocked": refresh_blocked,
        "sent": False,
    }


def _write_artifacts(output_dir, claim, payload):
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="talep_" + _case_name(payload["request_id"]) + "_pazarlik_", dir=output))
    txt, html, metadata = [folder / name for name in ("Pazarlik.txt", "Pazarlik.html", "Pazarlik.json")]
    payload.update(text_path=str(txt), html_path=str(html))
    report = render_negotiation_drafts(payload["draft_result"])
    files = []
    for path, content in {txt: report + "\n", html: _report_html(report), metadata: _json(payload)}.items():
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
        path.chmod(0o600)
        files.append({"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest_path = folder / "manifest.json"
    manifest = {
        "request_id": payload["request_id"],
        "fingerprint": claim["fingerprint"],
        "generation": claim["generation"],
        "offer_revision": payload["offer_revision"],
        "files": files,
        "sent": False,
    }
    with manifest_path.open("x", encoding="utf-8") as handle:
        handle.write(_json(manifest))
    manifest_path.chmod(0o600)
    return metadata, manifest_path, hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def prepare_negotiations(
    *,
    queue_path=DEFAULT_QUEUE,
    output_dir=DEFAULT_OUTPUT,
    request_id=None,
    now=None,
    use_model=False,
    refresh=False,
    history=None,
    graph_loader=None,
    writer=None,
    market_reader=None,
):
    if not Path(queue_path).is_file():
        return {**COMMON, "status": "no_requests", "draft_count": 0, "drafts": []}
    now = _instant(now)
    with sqlite3.connect(Path(queue_path).resolve().as_uri() + "?mode=ro", uri=True) as connection:
        ids = [
            row[0] for row in connection.execute("SELECT id FROM requests WHERE deadline_at IS NOT NULL ORDER BY id")
        ]
    if request_id:
        ids = [value for value in ids if value == request_id]
    snapshots = [CaseStore(queue_path).snapshot(identifier, now) for identifier in ids]
    snapshots = [case for case in snapshots if case["request"]["state"] in {"ready_for_report", "report_prepared"}]
    if not snapshots:
        return {**COMMON, "status": "no_due_negotiations", "draft_count": 0, "drafts": []}
    graph = copy.deepcopy((graph_loader or current_graph)())
    saved_history, enable_live = _configured_history(graph)
    market_node = next((item for item in graph["nodes"] if item["id"] == MARKET), None)
    observations = (
        market_node["data"]["node"]["template"].get("observations_json", {}).get("value", "") if market_node else ""
    )
    history = saved_history if history is None else history
    policy, jobs, result, errors = _policy(graph), NegotiationJobs(queue_path), [], []
    for case in snapshots:
        request, claim = case["request"], None
        try:
            initial = plan_negotiation(case, price_history=history)
            if not any(row.get("draft_allowed") for row in initial["requests"]):
                result.append({"request_id": request["id"], "status": "awaiting_comparable_offers", "sent": False})
                continue
            fingerprint = _fingerprint(case, initial, policy, history, enable_live, observations, use_model)
            claim = jobs.claim(request, fingerprint, refresh=refresh, request_model=bool(use_model))
            if claim["action"] == "cached":
                try:
                    payload = _cached_payload(claim, case, history)
                except (OSError, ValueError, TypeError, KeyError):
                    result.append(
                        {
                            "request_id": request["id"],
                            "status": "artifact_invalid",
                            "model_attempted": bool(claim["model_attempted"]),
                            "model_completed": bool(claim["model_completed"]),
                            "model_text_verified": False,
                            "sent": False,
                        }
                    )
                    errors.append("artifact_invalid")
                    continue
                jobs.activate_cached(claim, request)
                result.append(
                    _public(
                        payload,
                        status="cached",
                        metadata_path=claim["metadata_path"],
                        manifest_path=claim["manifest_path"],
                        refresh_blocked=claim.get("refresh_blocked", False),
                    )
                )
                continue
            if claim["action"] != "claimed":
                result.append(
                    {
                        "request_id": request["id"],
                        "status": claim["action"],
                        "model_attempted": bool(claim["model_attempted"]),
                        "model_completed": bool(claim["model_completed"]),
                        "model_text_verified": False,
                        "sent": False,
                    }
                )
                if claim["action"] in {"model_outcome_uncertain", "completed_result_unpublished"}:
                    errors.append(claim["action"])
                continue
            try:
                market = (market_reader or case_market_context)(
                    case, price_history=history, observations=observations, enable_live=enable_live, now=now
                )
            except (ValueError, TypeError, KeyError):
                market = {
                    "warnings": ["Piyasa/geçmiş verisi doğrulanamadı; fiyat hedefi mevcut tekliflerle sınırlandı."]
                }
            plan = plan_negotiation(case, market_context=market, price_history=history)
            response, model_meta, attempted = None, {"run_completed": False}, False
            if use_model:
                jobs.begin_attempt(claim, request)
                attempted = True
                try:
                    response, returned_meta = (writer or run_writer)(plan, graph)
                    completed = isinstance(returned_meta, dict) and returned_meta.get("run_completed") is True
                    model_meta = {"run_completed": completed}
                    if not completed:
                        response = None
                except (ValueError, TypeError, KeyError, StopIteration, RuntimeError, requests.RequestException):
                    model_meta = {
                        "run_completed": False,
                        "reason": "Dil modeli sonucu doğrulanamadı; otomatik tekrar yapılmaz.",
                    }
                jobs.finish_attempt(claim, completed=model_meta["run_completed"])
            checked = review_negotiation_drafts(plan, response)
            payload = {
                "request_id": request["id"],
                "input_fingerprint": fingerprint,
                "policy_fingerprint": policy,
                "policy_version": POLICY_VERSION,
                "offer_revision": request["offer_revision"],
                "generated_at": now.isoformat(),
                "plan": plan,
                "plan_fingerprint": _digest(plan),
                "prompt_fingerprint": _digest(build_negotiation_prompt(plan)),
                "market_context": market,
                "draft_result": checked,
                "model_attempted": attempted,
                "model": model_meta,
                **COMMON,
            }
            metadata_path, manifest_path, manifest_digest = _write_artifacts(output_dir, claim, payload)
            # Verify bytes and current language rules before publishing any path.
            candidate_claim = {
                **claim,
                "metadata_path": str(metadata_path),
                "manifest_path": str(manifest_path),
                "manifest_sha256": manifest_digest,
                "model_attempted": int(attempted),
                "model_completed": int(model_meta["run_completed"]),
            }
            _cached_payload(candidate_claim, case, history)
            if not jobs.publish(claim, request, metadata_path, manifest_path, manifest_digest):
                result.append({"request_id": request["id"], "status": "stale_source_or_generation", "sent": False})
                errors.append("stale_source_or_generation")
                continue
            result.append(
                _public(payload, status=checked["status"], metadata_path=metadata_path, manifest_path=manifest_path)
            )
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
            if claim and claim.get("action") == "claimed":
                jobs.fail(claim)
            reason = "stale_source_or_generation" if isinstance(error, StaleNegotiation) else "negotiation_not_prepared"
            result.append(
                {
                    "request_id": request["id"],
                    "status": reason,
                    "model_attempted": bool(claim and claim.get("model_attempted")),
                    "model_completed": bool(claim and claim.get("model_completed")),
                    "model_text_verified": False,
                    "sent": False,
                }
            )
            errors.append(reason)
    # A valid local fallback is useful, but it is not a completed model run.
    if any(
        row.get("model_attempted") and not row.get("model_completed") and row["status"] != "in_progress"
        for row in result
    ):
        errors.append("model_result_unverified_no_automatic_retry")
    if any(
        row.get("model_attempted")
        and row.get("model_completed")
        and not row.get("model_text_verified")
        and row.get("path")
        for row in result
    ):
        errors.append("model_text_rejected")
    count = sum(bool(row.get("path")) for row in result)
    status = (
        "negotiation_attention_required"
        if errors
        else "prepared"
        if count
        else "in_progress"
        if any(row["status"] == "in_progress" for row in result)
        else "awaiting_comparable_offers"
    )
    return {**COMMON, "status": status, "draft_count": count, "drafts": result, "errors": list(dict.fromkeys(errors))}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Yerel pazarlık taslakları; ileti veya sipariş göndermez")
    parser.add_argument("--request-id")
    parser.add_argument("--run-model", action="store_true")
    parser.add_argument(
        "--refresh", action="store_true", help="Tamamlanmış işi yeniler; belirsiz sağlayıcı denemesini tekrarlamaz"
    )
    parser.add_argument("--history-file")
    args = parser.parse_args(argv)
    try:
        history = json.loads(Path(args.history_file).read_text()) if args.history_file else None
        result = prepare_negotiations(
            request_id=args.request_id, use_model=args.run_model, refresh=args.refresh, history=history
        )
        print(json.dumps(result, ensure_ascii=False))
        return 2 if result.get("errors") else 0
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, requests.RequestException):
        print(
            json.dumps(
                {**COMMON, "status": "negotiation_not_prepared", "reason": "Kaynak veya yerel bağlantı doğrulanamadı."},
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
