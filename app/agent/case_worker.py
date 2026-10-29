"""Local request/deadline/report worker. No SAP request or email is sent.

Status reads only safe configuration projections and counts. Work commands accept
structured, explicitly linked source facts; queue/report paths remain local.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from runtime_config import get_runtime

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[2]
RUNTIME = get_runtime()
FLOW_ID = RUNTIME.flow_id
DEFAULT_QUEUE = RUNTIME.queue_path
DEFAULT_LANGFLOW_DB = RUNTIME.langflow_database
DEFAULT_OUTPUT = RUNTIME.output_dir
DEFAULT_RESEARCH = RUNTIME.monitor_dir / "current_research.json"

from case_store import CaseStore, _json, _question_artifact_valid, _report_artifact_valid, _unique_json_object


def _instant(now=None):
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, str):
        try:
            now = datetime.fromisoformat(now.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Referans zamanı saat dilimli ISO biçiminde olmalı") from None
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("Referans zamanı saat dilimli ISO biçiminde olmalı")
    return now.astimezone(timezone.utc)


def read_connection_status(database=DEFAULT_LANGFLOW_DB, flow_id=FLOW_ID):
    """Read safe setup flags; do not verify or contact SAP or a mailbox."""
    result = {
        "status": "mailbox_unconfigured", "flow_found": False, "bot_mailbox": None,
        "sap_api_enabled": False, "sap_endpoint_configured": False,
        "mailbox_connectivity_verified": False, "sap_connectivity_verified": False,
        "performed_api_call": False,
    }
    if RUNTIME.configured:
        # A target installation never falls back to this development database.
        from langflow_client import fetch_flow
        try:
            flow = fetch_flow(RUNTIME)
            result["performed_api_call"] = True
            result["flow_found"] = True
            for node in flow["data"]["nodes"]:
                template = node.get("data", {}).get("node", {}).get("template", {})
                kind = node.get("data", {}).get("type")
                if kind == "MailResearchInput":
                    mailbox = template.get("bot_mailbox", {}).get("value", "")
                    if isinstance(mailbox, str) and len(mailbox) <= 320 and re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", mailbox.strip()):
                        result["bot_mailbox"] = mailbox.strip()
                elif kind == "SAPProcurementInput":
                    result["sap_api_enabled"] = template.get("enable_api", {}).get("value") is True
                    result["sap_endpoint_configured"] = bool(template.get("endpoint", {}).get("value"))
            result["configuration_status"] = "read"
            result["status"] = "mailbox_configured_connection_unverified" if result["bot_mailbox"] else "mailbox_unconfigured"
        except (RuntimeError, ValueError, KeyError, TypeError):
            result["configuration_status"] = "target_flow_unavailable"
            # A safe client error may occur before authentication or after an
            # HTTP request. Do not claim that no request took place.
            result["performed_api_call"] = None
        return result
    database = Path(database).expanduser().resolve()
    if not database.is_file():
        result["configuration_status"] = "local_langflow_database_unavailable"
        return result
    connection = None
    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        flow = connection.execute("SELECT 1 FROM flow WHERE id IN (?,?) LIMIT 1", (flow_id, flow_id.replace("-", ""))).fetchone()
        result["flow_found"] = flow is not None
        rows = connection.execute("""
            SELECT json_extract(n.value,'$.data.type'),
                   CASE WHEN json_extract(n.value,'$.data.type')='MailResearchInput'
                        THEN json_extract(n.value,'$.data.node.template.bot_mailbox.value') END,
                   CASE WHEN json_extract(n.value,'$.data.type')='SAPProcurementInput'
                        THEN json_extract(n.value,'$.data.node.template.enable_api.value') END,
                   CASE WHEN json_extract(n.value,'$.data.type')='SAPProcurementInput'
                        THEN length(trim(coalesce(json_extract(n.value,'$.data.node.template.endpoint.value'),'')))>0 END
            FROM flow AS f, json_each(f.data,'$.nodes') AS n
            WHERE f.id IN (?,?) AND json_extract(n.value,'$.data.type') IN ('MailResearchInput','SAPProcurementInput')
        """, (flow_id, flow_id.replace("-", ""))).fetchall()
        for node_type, mailbox, enabled, endpoint in rows:
            if node_type == "MailResearchInput" and isinstance(mailbox, str):
                mailbox = mailbox.strip()
                if mailbox and len(mailbox) <= 320 and re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", mailbox):
                    result["bot_mailbox"] = mailbox
            elif node_type == "SAPProcurementInput":
                result["sap_api_enabled"] = enabled is True or enabled == 1
                result["sap_endpoint_configured"] = bool(endpoint)
        result["status"] = "mailbox_configured_connection_unverified" if result["bot_mailbox"] else "mailbox_unconfigured"
        result["configuration_status"] = "read" if result["flow_found"] else "flow_not_found"
    except (sqlite3.Error, ValueError):
        result["configuration_status"] = "local_configuration_unreadable"
    finally:
        if connection is not None:
            connection.close()
    return result


def read_queue_counts(queue_path=DEFAULT_QUEUE, now=None):
    """Read an existing queue without creating it or loading source email bodies."""
    queue_path = Path(queue_path).expanduser().resolve()
    now = _instant(now)
    result = {"total_requests": 0, "offers": 0, "awaiting_deadline": 0, "collecting": 0,
              "ready_for_report": 0, "report_prepared": 0, "question_drafts_prepared": 0,
              "queue_status": "not_created"}
    if not queue_path.is_file():
        return result
    connection = None
    try:
        connection = sqlite3.connect(queue_path.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(requests)")}
        optional = [name if name in columns else "NULL AS " + name
                    for name in ("question_draft_text", "question_draft_sha256", "question_source_sha256")]
        rows = connection.execute("SELECT id,payload,deadline_at,offer_revision,report_offer_revision,report_revision,report_path,"
                                  "question_draft_path," + ",".join(optional) + " FROM requests").fetchall()
        result["offers"] = connection.execute("SELECT count(*) FROM offers").fetchone()[0]
        result["total_requests"] = len(rows)
        for row in rows:
            deadline = row["deadline_at"]
            if _question_artifact_valid(row):
                result["question_drafts_prepared"] += 1
            if deadline is None:
                result["awaiting_deadline"] += 1
            elif now < _instant(deadline):
                result["collecting"] += 1
            elif (row["offer_revision"] == row["report_offer_revision"]
                  and _report_artifact_valid(row["report_path"], request_id=row["id"],
                                             offer_revision=row["offer_revision"], report_revision=row["report_revision"])):
                result["report_prepared"] += 1
            else:
                result["ready_for_report"] += 1
        result["queue_status"] = "read"
    except (sqlite3.Error, ValueError):
        result["queue_status"] = "unreadable"
    finally:
        if connection is not None:
            connection.close()
    return result


def status(queue_path=DEFAULT_QUEUE, database=DEFAULT_LANGFLOW_DB, now=None):
    return {**read_connection_status(database), "queue": read_queue_counts(queue_path, now),
            "checked_at": _instant(now).isoformat(),
            "outbound_sent": False}


def list_collecting(queue_path=DEFAULT_QUEUE, now=None):
    """Expose only IDs, confirmed deadlines and material codes for scheduling."""
    result = {"status": "no_collecting_requests", "request_count": 0, "requests": [],
              "outbound_sent": False, "performed_api_call": False}
    path = Path(queue_path).expanduser().resolve()
    if not path.is_file():
        return result
    now = _instant(now)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT id,deadline_at,json_extract(payload,'$.material_code') FROM requests WHERE deadline_at IS NOT NULL ORDER BY deadline_at,id").fetchall()
    finally:
        connection.close()
    result["requests"] = [{"request_id": request_id, "deadline_at": deadline, "material_code": code}
                          for request_id, deadline, code in rows if now < _instant(deadline)]
    result["request_count"] = len(result["requests"])
    if result["requests"]:
        result["status"] = "collecting_requests"
    return result


def _summary(snapshot, action):
    request = snapshot["request"]
    return {"status": action, "request_id": request["id"], "state": request["state"],
            "deadline_at": request["deadline_at"], "offer_count": len(snapshot["offers"]),
            "offer_revision": request["offer_revision"], "report_revision": request["report_revision"],
            "outbound_sent": False, "performed_api_call": False}


def ingest_request(payload, store, now=None):
    if not isinstance(payload, dict) or not isinstance(payload.get("request"), dict):
        raise ValueError("Giriş source ve request içeren JSON nesnesi olmalı")
    request = payload["request"]
    snapshot = store.upsert_request(request, payload.get("source"))
    if request.get("deadline_at"):
        snapshot = store.set_deadline(snapshot["request"]["id"], request["deadline_at"], request.get("requester"), now=now)
    return _summary(snapshot, "request_registered")


def _save_text(path, content):
    """Replace local artifacts atomically; source-derived names are hashed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".draft-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _case_name(request_id):
    return hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:20]


def prepare_questions(queue_path=DEFAULT_QUEUE, output_dir=DEFAULT_OUTPUT):
    if not Path(queue_path).is_file():
        return {"status": "no_pending_questions", "draft_count": 0, "outbound_sent": False, "performed_api_call": False}
    store = CaseStore(queue_path)
    drafts = [{"request_id": snapshot["request"]["id"], "path": snapshot["question_draft"]["path"], "sent": False}
              for snapshot in store.prepare_question_drafts(output_dir)]
    return {"status": "question_drafts_prepared" if drafts else "no_pending_questions", "draft_count": len(drafts),
            "drafts": drafts, "outbound_sent": False, "performed_api_call": False}


def _report_html(report):
    """Safe standalone view with a readable comparison table; no active content."""
    lines = report.splitlines()
    body = []
    table_rows = []

    def flush_table():
        if not table_rows:
            return
        body.append('<div class="table-wrap"><table>')
        for index, line in enumerate(table_rows):
            cells = [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
            if all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                continue
            tag = "th" if index == 0 else "td"
            body.append("<tr>" + "".join(f"<{tag}>{html.escape(cell)}</{tag}>" for cell in cells) + "</tr>")
        body.append("</table></div>")
        table_rows.clear()

    for index, line in enumerate(lines):
        if line.startswith("|") and line.endswith("|"):
            table_rows.append(line)
            continue
        flush_table()
        if not line:
            continue
        escaped = html.escape(line)
        if index == 0:
            body.append("<h1>" + escaped + "</h1>")
        elif line.startswith("Tedarikçi:") or line == "Karşılaştırma:":
            body.append("<h2>" + escaped + "</h2>")
        elif line.startswith("- "):
            body.append('<p class="detail">' + html.escape(line[2:]) + "</p>")
        else:
            body.append("<p>" + escaped + "</p>")
    flush_table()
    return (
        '<!doctype html><html lang="tr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Yönetici teklif karşılaştırması</title><style>'
        'body{font:15px/1.55 system-ui,sans-serif;color:#172232;background:#f2f5f8;margin:0;padding:28px}'
        'main{max-width:1560px;margin:auto;background:white;padding:30px;border:1px solid #dce4ec;border-radius:12px}'
        'h1{font-size:23px;line-height:1.3}h2{font-size:18px;border-top:1px solid #dce4ec;padding-top:22px;margin-top:28px}'
        'p{overflow-wrap:anywhere;white-space:pre-wrap}.detail{padding-left:18px;font-size:13px}'
        '.table-wrap{overflow-x:auto;margin:24px 0}table{border-collapse:collapse;min-width:1320px;width:100%;font-size:12px}'
        'th,td{border:1px solid #dce4ec;padding:10px;text-align:left;vertical-align:top}th{background:#eaf1f8;font-weight:650}'
        'tr:nth-child(even){background:#f8fafc}@media print{body{padding:0;background:white}main{border:0;padding:12px}'
        '.table-wrap{overflow:visible}table{min-width:0;font-size:8px}th,td{padding:4px}@page{size:A3 landscape;margin:14mm}}'
        '</style><main>' + "\n".join(body) + "</main></html>"
    )


def _comparison_functions():
    component_dir = str(ROOT / "components")
    if component_dir not in sys.path:
        sys.path.insert(0, component_dir)
    from quote_comparison import analyze_case, render_case_report
    return analyze_case, render_case_report


def _material_name(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split()) if isinstance(value, str) else ""


def research_attachment(request, path=DEFAULT_RESEARCH, now=None):
    """Bind only matching public evidence captured by the exact deadline instant.

    This does not claim the saved public research was produced by this request or
    that both models completed. A separate saved binding records what was reused.
    """
    unavailable = {
        "status": "not_linked", "source_count": 0, "payload": None,
        "lines": ["", "Araştırma raporu bağlantısı",
                  "Süre sonunda bu talebe bağlanmış doğrulanabilir araştırma kanıtı yok; teklif karşılaştırma raporu hazırlandı.",
                  "Alternatif tedarikçi ve hammadde haber bulguları bu dosyada tamamlandı kabul edilmemelidir."],
    }
    if not Path(path).is_file():
        return unavailable
    try:
        raw = _load_json(path, public_research=True)
        if not isinstance(raw, dict) or raw.get("request_id") not in {None, request["id"]}:
            return {**unavailable, "status": "request_reference_mismatch"}
        if raw.get("deadline_at") and _instant(raw["deadline_at"]) != _instant(request.get("deadline_at")):
            return {**unavailable, "status": "request_deadline_mismatch"}
        materials = raw.get("materials")
        profile = raw.get("profile")
        selected = profile.get("materials") if isinstance(profile, dict) else None
        if (not isinstance(materials, list) or not 1 <= len(materials) <= 10
                or not isinstance(selected, list) or len(selected) != len(materials)):
            return {**unavailable, "status": "scope_mismatch"}
        code, name = request.get("material_code"), _material_name(request.get("material_name"))

        def scope(items):
            output = {}
            for item in items:
                if not isinstance(item, dict):
                    return None
                item_code, item_name = item.get("material_code"), _material_name(item.get("name"))
                if not isinstance(item_code, str) or not item_code or not item_name or item_code in output:
                    return None
                output[item_code] = item_name
            return output

        material_scope, selected_scope = scope(materials), scope(selected)
        if (not code or not name or material_scope is None or selected_scope is None
                or material_scope != selected_scope or material_scope.get(code) != name):
            return {**unavailable, "status": "scope_mismatch"}
        selected_material = next(item for item in materials if item["material_code"] == code)
        if not raw.get("as_of") or not request.get("deadline_at"):
            return {**unavailable, "status": "invalid_timestamp"}
        as_of, deadline, now = _instant(raw["as_of"]), _instant(request["deadline_at"]), _instant(now)
        if as_of > deadline or as_of > now:
            return {**unavailable, "status": "after_deadline"}
        material = {"material_code": code, "name": request["material_name"], "suppliers": [], "news": []}
        retrievals = {}
        for kind in ("suppliers", "news"):
            items = selected_material.get(kind, [])
            if not isinstance(items, list) or len(items) > 100:
                return {**unavailable, "status": "invalid_sources"}
            for item in items:
                if not isinstance(item, dict) or not item.get("retrieved_at"):
                    continue
                try:
                    retrieved = _instant(item["retrieved_at"])
                    if retrieved > as_of or retrieved > deadline:
                        continue
                    if kind == "news" and (not item.get("published_at") or _instant(item["published_at"]) > retrieved):
                        continue
                except ValueError:
                    continue
                material[kind].append(item)
                if isinstance(item.get("evidence_id"), str):
                    retrievals[item["evidence_id"]] = retrieved.isoformat()
        # Only public research fields survive; unused API/configuration fields
        # and evidence for other material selections are never copied to cases.
        raw_status = raw.get("status")
        research_status = raw_status if raw_status in {"ok", "completed", "partial", "awaiting_selection", "error", "empty"} else "partial"
        narrowed = {"as_of": as_of.isoformat(), "status": research_status,
                    "profile": {"region": profile.get("region"), "lookback_days": profile.get("lookback_days", 30),
                                "materials": [{"material_code": code, "name": request["material_name"]}]},
                    "materials": [material], "warnings": []}
        _comparison_functions()  # Makes the existing local component path available.
        from report_table import RESEARCH_MARKER, render_research, validate_research
        text = RESEARCH_MARKER + json.dumps(narrowed, ensure_ascii=False, allow_nan=False)
        clean, sources, _ = validate_research(text, [])
        if clean is None or not sources:
            return {**unavailable, "status": "no_valid_public_sources"}
        # Validation normalizes source metadata and drops unsupported properties.
        canonical = {key: clean[key] for key in ("as_of", "status", "profile", "materials")}
        original_source_count = sum(len(selected_material.get(kind, [])) for kind in ("suppliers", "news"))
        if len(sources) < original_source_count:
            canonical["status"] = "partial"
            canonical["warnings"] = ["Bazı kaynaklar saatli son tarih veya kaynak doğrulamasını geçmedi; araştırma eki kısmi."]
        for clean_material in canonical["materials"]:
            for supplier in clean_material["suppliers"]:
                supplier.update(verification="search_candidate", existing_supplier_match=False)
            for news in clean_material["news"]:
                news["verification"] = "headline_only"
        rendered = render_research(RESEARCH_MARKER + json.dumps(canonical, ensure_ascii=False), "", [])
        binding = {"request_id": request["id"], "material_code": code, "material_name": request["material_name"],
                   "deadline_at": deadline.isoformat(), "source_as_of": as_of.isoformat(), "linked_at": now.isoformat(),
                   "binding_kind": "matching_public_material_evidence", "full_multi_model_run_verified": False,
                   "source_retrieval_instants": {key: value for key, value in retrievals.items() if key in sources},
                   "research": canonical}
        return {"status": "matching_public_evidence_linked", "source_count": len(sources), "payload": binding,
                "lines": ["", "Araştırma raporu bağlantısı", "Aynı malzemeye ait kamuya açık araştırma kaynakları bu teklif dosyasına bağlandı.",
                          "Kaynaklar araştırma bitiş zamanından önce kaydedilmiş olmalıdır; iki modelin ortak çalışması bu ekle doğrulanmış sayılmaz.",
                          *rendered]}
    except (ValueError, TypeError, KeyError, OSError):
        return {**unavailable, "status": "invalid_research_file"}


def prepare_due(queue_path=DEFAULT_QUEUE, output_dir=DEFAULT_OUTPUT, now=None, *, write_html=True,
                analyze=None, render=None, research_path=DEFAULT_RESEARCH):
    if not Path(queue_path).is_file():
        return {"status": "no_due_reports", "report_count": 0, "outbound_sent": False, "performed_api_call": False}
    now = _instant(now)
    store = CaseStore(queue_path)
    due = store.list_due(now)
    if not due:
        return {"status": "no_due_reports", "report_count": 0, "outbound_sent": False, "performed_api_call": False}
    if analyze is None or render is None:
        default_analyze, default_render = _comparison_functions()
        analyze, render = analyze or default_analyze, render or default_render
    reports, errors = [], []
    for snapshot in due:
        request = snapshot["request"]
        try:
            result = analyze(snapshot)
            if result.get("report_status") != "final":
                raise ValueError("Süre sonu karşılaştırması doğrulanamadı")
            per_case_research = Path(research_path).parent / "cases" / (_case_name(request["id"]) + ".json")
            attachment = research_attachment(request, per_case_research if per_case_research.is_file() else research_path, now)
            report = render(result) + "\n" + "\n".join(attachment["lines"])
            revision = request["report_revision"] + 1
            base = "talep_" + _case_name(request["id"]) + f"_yonetici_raporu_v{revision}"
            # Each producer owns a new directory. A stale snapshot can never
            # replace an artifact already published by another producer.
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            artifact_dir = Path(tempfile.mkdtemp(prefix=base + f"_o{request['offer_revision']}_", dir=output_dir))
            text_path = artifact_dir / (base + ".txt")
            _save_text(text_path, report + "\n")
            bound_research_path = None
            if attachment["payload"] is not None:
                bound_research_path = artifact_dir / (base + "_research.json")
                _save_text(bound_research_path, json.dumps(attachment["payload"], ensure_ascii=False, indent=2, allow_nan=False))
            html_path = None
            if write_html:
                html_path = artifact_dir / (base + ".html")
                _save_text(html_path, _report_html(report))
            files = [path for path in (text_path, html_path, bound_research_path) if path is not None]
            manifest_path = artifact_dir / "manifest.json"
            manifest = {"request_id": request["id"], "offer_revision": request["offer_revision"],
                        "report_revision": revision, "generated_at": now.isoformat(),
                        "files": [{"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                                  for path in files], "sent": False}
            _save_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
            for item in manifest["files"]:
                if hashlib.sha256((artifact_dir / item["name"]).read_bytes()).hexdigest() != item["sha256"]:
                    raise ValueError("Rapor dosya özeti doğrulanamadı")
            current = store.mark_report_prepared(request["id"], text_path, now=now,
                                                 expected_offer_revision=request["offer_revision"])
            reports.append({"request_id": request["id"], "path": str(text_path.resolve()),
                            "html_path": str(html_path.resolve()) if html_path else None,
                            "report_revision": current["request"]["report_revision"],
                            "offer_count": len(snapshot["offers"]), "sent": False,
                            "research_status": attachment["status"], "research_source_count": attachment["source_count"],
                            "research_path": str(bound_research_path.resolve()) if bound_research_path else None,
                            "manifest_path": str(manifest_path.resolve()),
                            "full_multi_model_run_verified": False})
        except (ValueError, TypeError, KeyError, OSError):
            errors.append({"request_id": request["id"], "status": "report_not_prepared",
                           "reason": "Kaynak, teklif revizyonu veya yerel rapor dosyası doğrulanamadı"})
    return {"status": "reports_prepared" if reports and not errors else ("report_preparation_failed" if errors else "no_due_reports"),
            "report_count": len(reports), "reports": reports, "errors": errors,
            "outbound_sent": False, "performed_api_call": False}


def _json_depth(value, depth=0):
    if depth > 12:
        raise ValueError("JSON iç içe yapı sınırını aşıyor")
    if isinstance(value, dict):
        for item in value.values():
            _json_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_depth(item, depth + 1)


def _load_json(path, *, public_research=False):
    path = Path(path)
    if path.stat().st_size > 500_000:
        raise ValueError("Giriş dosyası boyut sınırını aşıyor")
    with path.open("rb") as handle:
        raw = handle.read(500_001)
    if len(raw) > 500_000:
        raise ValueError("Giriş dosyası boyut sınırını aşıyor")
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object,
                            parse_constant=_reject_json_constant)
        if public_research:
            # Public research is separately projected through an allowlist below.
            # Preserve its existing drop-unused-configuration behavior, while the
            # CLI ingestion default rejects any supplied connection secret key.
            _json_depth(parsed)
            json.dumps(parsed, allow_nan=False)
        else:
            _json(parsed, maximum=500_000)
        return parsed
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Giriş dosyası geçerli UTF-8 JSON olmalı") from None


def _reject_json_constant(value):
    raise ValueError("JSON yalnız sonlu sayılar içermeli")


def main(argv=None, *, queue_path=DEFAULT_QUEUE, database=DEFAULT_LANGFLOW_DB, output_dir=DEFAULT_OUTPUT, now=None):
    parser = argparse.ArgumentParser(description="Yerel satın alma talep/süre/teklif takibi; e-posta göndermez.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("list-collecting")
    intake = sub.add_parser("ingest-request")
    intake.add_argument("--file", required=True)
    deadline = sub.add_parser("set-deadline")
    deadline.add_argument("--request-id", required=True)
    deadline.add_argument("--at", required=True)
    deadline.add_argument("--requester", required=True)
    offer = sub.add_parser("ingest-offer")
    offer.add_argument("--request-id", required=True)
    offer.add_argument("--file", required=True)
    sub.add_parser("prepare-questions")
    due = sub.add_parser("prepare-due")
    due.add_argument("--no-html", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            result = status(queue_path, database, now)
        elif args.command == "list-collecting":
            result = list_collecting(queue_path, now)
        elif args.command == "ingest-request":
            result = ingest_request(_load_json(args.file), CaseStore(queue_path), now)
        elif args.command == "set-deadline":
            result = _summary(CaseStore(queue_path).set_deadline(args.request_id, args.at, args.requester, now), "deadline_recorded")
        elif args.command == "ingest-offer":
            payload = _load_json(args.file)
            result = _summary(CaseStore(queue_path).append_offer(args.request_id, payload), "offer_archived")
        elif args.command == "prepare-questions":
            result = prepare_questions(queue_path, output_dir)
        else:
            result = prepare_due(queue_path, output_dir, now, write_html=not args.no_html)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 1 if result["status"] == "report_preparation_failed" else 0
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
        print(json.dumps({"status": "local_command_failed", "reason": "Giriş, yetkili süre yanıtı veya yerel dosya doğrulanamadı",
                          "outbound_sent": False, "performed_api_call": False}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
