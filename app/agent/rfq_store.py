"""Local RFQ drafts and caller-supplied mail evidence. Never sends email.

Offer canonicalization uses CaseStore in an isolated temporary database. The
canonical offer and its RFQ binding are then committed in one target transaction,
so crashes cannot leave an offer falsely reported as a bound supplier response.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import sys
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path

from case_store import DEFAULT_PATH, CaseStore, _email, _instant, _json, _quantity, _text

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "cases" / "rfq"


def _hash(value):
    return hashlib.sha256(_json(value, maximum=500_000).encode()).hexdigest()


def _read_connection(path):
    connection = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


class RFQStore:
    """RFQ tracking shares the case DB; constructing this object is read-only."""

    def __init__(self, queue_path=DEFAULT_PATH, output_dir=None):
        self.path = Path(queue_path).expanduser().resolve()
        self.output = Path(output_dir or DEFAULT_OUTPUT).expanduser().resolve()

    @contextmanager
    def _write(self):
        if not self.path.is_file():
            raise ValueError("Talep kuyruğu yok; önce gerçek kaynaklı talep kaydedilmeli")
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS rfq_drafts (
                id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests(id),
                supplier_id TEXT NOT NULL, recipient TEXT NOT NULL, payload TEXT NOT NULL,
                fingerprint TEXT NOT NULL, prepared_at TEXT NOT NULL, deadline_at TEXT NOT NULL,
                artifacts TEXT NOT NULL, dispatch TEXT, dispatch_key TEXT UNIQUE
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS rfq_responses (
                message_key TEXT PRIMARY KEY, rfq_id TEXT NOT NULL REFERENCES rfq_drafts(id),
                offer_id TEXT NOT NULL UNIQUE REFERENCES offers(id), payload_hash TEXT NOT NULL,
                received_at TEXT NOT NULL, late INTEGER NOT NULL
            )""")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _request(connection, request_id):
        row = connection.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise ValueError("Talep bulunamadı")
        return row

    @staticmethod
    def _row(connection, rfq_id):
        row = connection.execute("SELECT * FROM rfq_drafts WHERE id=?", (rfq_id,)).fetchone()
        if row is None:
            raise ValueError("Teklif isteği bulunamadı")
        return row

    @staticmethod
    def _artifact_ok(artifacts):
        try:
            return all(Path(item["path"]).is_file() and not Path(item["path"]).is_symlink()
                       and Path(item["path"]).stat().st_size <= 2_000_000
                       and hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest()
                       == item["sha256"] for item in artifacts.values()) and len(artifacts) == 3
        except (OSError, TypeError, KeyError):
            return False

    def _artifacts(self, draft, fingerprint):
        self.output.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix="rfq-" + fingerprint[:16] + "-", dir=self.output))
        body = draft["body"]
        text = ("YEREL TEKLİF İSTEĞİ TASLAĞI — GÖNDERİLMEDİ\n"
                f"Alıcı: {draft['recipient']}\nKonu: {draft['subject']}\n\n{body}\n")
        rendered = ("<!doctype html><html lang=\"tr\"><meta charset=\"utf-8\">"
                    "<title>Teklif isteği taslağı</title><h1>Teklif isteği taslağı</h1>"
                    "<p>Yerel taslak · Gönderilmedi</p>"
                    f"<p>Alıcı: {html.escape(draft['recipient'])}</p>"
                    f"<h2>{html.escape(draft['subject'])}</h2>"
                    f"<pre style=\"white-space:pre-wrap\">{html.escape(body)}</pre></html>")
        contents = {"text": ("Teklif_Istegi.txt", text), "html": ("Teklif_Istegi.html", rendered),
                    "metadata": ("Teklif_Istegi.json", _json({**draft, "fingerprint": fingerprint,
                                                                           "sent": False, "draft_only": True}, 500_000))}
        result = {}
        for kind, (filename, content) in contents.items():
            path = folder / filename
            with path.open("x", encoding="utf-8") as handle:
                handle.write(content)
            path.chmod(0o600)
            result[kind] = {"path": str(path), "sha256": hashlib.sha256(content.encode()).hexdigest()}
        return result

    @staticmethod
    def _public(connection, row, now):
        draft = json.loads(row["payload"])
        dispatch = json.loads(row["dispatch"]) if row["dispatch"] else None
        responses = connection.execute("SELECT count(*),coalesce(sum(late),0) FROM rfq_responses WHERE rfq_id=?",
                                       (row["id"],)).fetchone()
        if responses[0]:
            state = "response_received"
        elif dispatch and now >= _instant(row["deadline_at"]):
            state = "deadline_elapsed_no_reply"
        elif dispatch:
            state = "awaiting_reply"
        else:
            state = "draft_prepared"
        artifacts = json.loads(row["artifacts"])
        return {"rfq_id": row["id"], "request_id": row["request_id"], "supplier_id": row["supplier_id"],
                "supplier_name": draft["supplier_name"], "recipient": row["recipient"], "kind": draft["kind"],
                "state": state, "deadline_at": row["deadline_at"], "prepared_at": row["prepared_at"],
                "dispatch_evidence_recorded": dispatch is not None, "network_send_verified": False,
                "sent_by_this_worker": False, "responses": responses[0], "late_responses": responses[1],
                "artifact_status": "verified" if RFQStore._artifact_ok(artifacts) else "artifact_missing_or_changed",
                "path": artifacts["text"]["path"], "html_path": artifacts["html"]["path"],
                "metadata_path": artifacts["metadata"]["path"], "fingerprint": row["fingerprint"]}

    def snapshot(self, request_id, now=None):
        """Read existing case facts without initializing a queue or its schema."""
        if not self.path.is_file():
            raise ValueError("Talep kuyruğu yok; önce gerçek kaynaklı talep kaydedilmeli")
        with closing(_read_connection(self.path)) as connection:
            row = self._request(connection, request_id)
            return CaseStore.__new__(CaseStore)._snapshot(connection, row, _instant(now))

    def prepare(self, request_id, suppliers, now=None, *, expected_scope_hash=None, expected_deadline_at=None):
        now = _instant(now)
        if not self.path.is_file():
            raise ValueError("Talep kuyruğu yok; önce gerçek kaynaklı talep kaydedilmeli")
        # Scope and deadline are rechecked while holding the target write lock.
        with closing(_read_connection(self.path)) as connection:
            before = self._request(connection, request_id)
            original = (before["payload"], before["deadline_at"])
        snapshot = self.snapshot(request_id, now)
        if snapshot["request"]["state"] != "collecting":
            raise ValueError("Teklif isteği için gelecekteki bitiş zamanı talep sahibi tarafından onaylanmalı")
        sys.path.insert(0, str(ROOT / "components")) if str(ROOT / "components") not in sys.path else None
        from rfq import build_rfq_plan
        plan = build_rfq_plan(snapshot, suppliers, now=now)
        if expected_scope_hash is not None and expected_scope_hash != plan.get("scope_hash"):
            raise ValueError("Bağlanan talebin kapsamı kayıtlı taleple eşleşmiyor")
        if expected_deadline_at is not None and _instant(expected_deadline_at) != _instant(plan.get("deadline_at")):
            raise ValueError("Bağlanan talebin bitiş zamanı kayıtlı taleple eşleşmiyor")
        drafts = plan.get("drafts", [])
        if not isinstance(drafts, list):
            raise ValueError("Teklif isteği planı geçersiz")
        result = []
        with self._write() as connection:
            current = self._request(connection, request_id)
            if (current["payload"], current["deadline_at"]) != original:
                raise ValueError("Teklif isteği hazırlanırken talep kapsamı veya süresi değişti; yeniden deneyin")
            if now >= _instant(current["deadline_at"]):
                raise ValueError("Teklif toplama süresi doldu")
            for item in drafts:
                draft = dict(item)
                if draft.get("request_id") != request_id or draft.get("sent") is not False or draft.get("draft_only") is not True:
                    raise ValueError("Taslak talep kimliği veya gönderim durumu geçersiz")
                rfq_id = _text(draft.get("rfq_id"), maximum=200, required=True)
                _email(draft.get("recipient"), required=True)
                _text(draft.get("supplier_id"), maximum=200, required=True)
                _text(draft.get("supplier_name"), maximum=500, required=True)
                _text(draft.get("subject"), maximum=1000, required=True)
                _text(draft.get("body"), maximum=100_000, required=True)
                fingerprint = _hash(draft)
                existing = connection.execute("SELECT * FROM rfq_drafts WHERE id=?", (rfq_id,)).fetchone()
                if existing is not None:
                    if existing["fingerprint"] != fingerprint:
                        raise ValueError("Aynı teklif isteği kimliği farklı içerikle yeniden kullanılamaz")
                    if not self._artifact_ok(json.loads(existing["artifacts"])):
                        artifacts = self._artifacts(draft, fingerprint)
                        connection.execute("UPDATE rfq_drafts SET artifacts=? WHERE id=?", (_json(artifacts), rfq_id))
                else:
                    artifacts = self._artifacts(draft, fingerprint)
                    connection.execute("""INSERT INTO rfq_drafts
                        (id,request_id,supplier_id,recipient,payload,fingerprint,prepared_at,deadline_at,artifacts)
                        VALUES(?,?,?,?,?,?,?,?,?)""", (rfq_id, request_id, draft["supplier_id"], draft["recipient"],
                                                     _json(draft, 500_000), fingerprint, now.isoformat(),
                                                     current["deadline_at"], _json(artifacts)))
                result.append(self._public(connection, self._row(connection, rfq_id), now))
        return {"status": plan.get("status", "blocked"), "request_id": request_id, "drafts": result,
                "blocked_suppliers": plan.get("blocked_suppliers", []), "warnings": plan.get("warnings", []),
                "emails_sent": False, "providers_called": False}

    def record_dispatch(self, rfq_id, evidence, now=None):
        now = _instant(now)
        if not isinstance(evidence, dict) or set(evidence) != {"mailbox", "message_id", "conversation_id", "recipient", "dispatched_at"}:
            raise ValueError("Gönderim kaydı mailbox, message_id, conversation_id, recipient, dispatched_at içermeli")
        normalized = {"mailbox": _email(evidence["mailbox"], required=True),
                      "message_id": _text(evidence["message_id"], maximum=500, required=True),
                      "conversation_id": _text(evidence["conversation_id"], maximum=500, required=True),
                      "recipient": _email(evidence["recipient"], required=True),
                      "dispatched_at": _instant(evidence["dispatched_at"]).isoformat()}
        with self._write() as connection:
            row = self._row(connection, rfq_id)
            request_row = self._request(connection, row["request_id"])
            request = json.loads(request_row["payload"])
            if request_row["deadline_at"] != row["deadline_at"]:
                raise ValueError("Talep süresi değişti; yeni süreyle teklif isteği hazırlanmalı")
            if normalized["recipient"].casefold() != row["recipient"].casefold():
                raise ValueError("Gönderim alıcısı teklif isteği alıcısıyla eşleşmiyor")
            if request["mailbox"] and normalized["mailbox"].casefold() != request["mailbox"].casefold():
                raise ValueError("Gönderim posta kutusu kayıtlı talep kutusuyla eşleşmiyor")
            at = _instant(normalized["dispatched_at"])
            if at > now or at < _instant(row["prepared_at"]) or at >= _instant(row["deadline_at"]):
                raise ValueError("Gönderim zamanı hazırlama ve bitiş zamanı arasında ve gelecekte olmayacak şekilde belirtilmeli")
            ambiguous = connection.execute("""SELECT 1 FROM rfq_drafts WHERE id<>? AND dispatch IS NOT NULL
                AND lower(json_extract(dispatch,'$.mailbox'))=?
                AND json_extract(dispatch,'$.conversation_id')=? LIMIT 1""",
                (rfq_id, normalized["mailbox"].casefold(), normalized["conversation_id"])).fetchone()
            if ambiguous:
                raise ValueError("Aynı posta kutusu ve konuşma başka teklif isteğine bağlı; eşleşme belirsiz")
            serialized = _json(normalized)
            if row["dispatch"]:
                if row["dispatch"] != serialized:
                    raise ValueError("Kayıtlı gönderim kanıtı değiştirilemez")
            else:
                key = _json([normalized["mailbox"].casefold(), normalized["message_id"]])
                try:
                    connection.execute("UPDATE rfq_drafts SET dispatch=?,dispatch_key=? WHERE id=?", (serialized, key, rfq_id))
                except sqlite3.IntegrityError:
                    raise ValueError("Aynı gönderim kaynağı başka teklif isteğine bağlı") from None
            return self._public(connection, self._row(connection, rfq_id), now)

    @staticmethod
    def _canonical_offer(request, source, offer):
        # This invokes the existing CaseStore validator without another write
        # transaction on the target database or persisting intermediate facts.
        with tempfile.TemporaryDirectory(prefix="rfq-validation-") as folder:
            isolated = CaseStore(Path(folder) / "validation.db")
            isolated.upsert_request(request, source)
            return isolated.append_offer(request["id"], offer)["offers"][0]

    def ingest_response(self, rfq_id, response, now=None):
        now = _instant(now)
        required = {"request_id", "supplier_id", "conversation_id", "mailbox", "offer"}
        if not isinstance(response, dict) or set(response) != required or not isinstance(response["offer"], dict):
            raise ValueError("Yanıt request_id, supplier_id, conversation_id, mailbox ve offer içermeli")
        _json(response, maximum=500_000)
        with self._write() as connection:
            row = self._row(connection, rfq_id)
            if not row["dispatch"]:
                raise ValueError("Gönderim kanıtı kaydedilmeden yanıt teklif isteğine bağlanamaz")
            dispatch = json.loads(row["dispatch"])
            ambiguous = connection.execute("""SELECT 1 FROM rfq_drafts WHERE id<>? AND dispatch IS NOT NULL
                AND lower(json_extract(dispatch,'$.mailbox'))=?
                AND json_extract(dispatch,'$.conversation_id')=? LIMIT 1""",
                (rfq_id, dispatch["mailbox"].casefold(), dispatch["conversation_id"])).fetchone()
            if ambiguous:
                raise ValueError("Kayıtlı posta konuşması birden çok teklif isteğine bağlı; yanıt birleştirilemez")
            request_row = self._request(connection, row["request_id"])
            request = {**json.loads(request_row["payload"]), "id": row["request_id"]}
            draft = json.loads(row["payload"])
            if response["request_id"] != row["request_id"] or response["supplier_id"] != row["supplier_id"]:
                raise ValueError("Yanıt talep veya tedarikçi kimliğiyle eşleşmiyor")
            if response["conversation_id"] != dispatch["conversation_id"] or response["mailbox"] != dispatch["mailbox"]:
                raise ValueError("Yanıtın konuşması veya posta kutusu gönderim kaydıyla eşleşmiyor")
            offer = response["offer"]
            email = offer.get("email")
            if not isinstance(email, dict):
                raise ValueError("Yanıtın gerçek kaynak e-postası gerekli")
            source_texts = [email.get("subject"), email.get("body_excerpt")]
            attachments = email.get("attachments", [])
            if isinstance(attachments, list):
                source_texts.extend(item.get("text_excerpt") for item in attachments if isinstance(item, dict))
            references = {match.casefold() for text in source_texts if isinstance(text, str)
                          for match in re.findall(r"\bRFQ-[a-f0-9]{24}\b", text, flags=re.IGNORECASE)}
            if references and references != {rfq_id.casefold()}:
                raise ValueError("Kaynak e-posta veya ekteki teklif isteği referansı farklı ya da birden çok; eşleşme belirsiz")
            if email.get("mailbox") != dispatch["mailbox"] or _email(email.get("sender"), required=True).casefold() != row["recipient"].casefold():
                raise ValueError("Yanıt posta kutusu veya gerçek göndereni teklif isteğiyle eşleşmiyor")
            if offer.get("supplier") != draft["supplier_name"]:
                raise ValueError("Teklif firma adı kayıtlı tedarikçiyle eşleşmiyor")
            if _quantity(offer.get("quantity")) != request["quantity"] or offer.get("unit") != request["unit"]:
                raise ValueError("Teklif miktarı veya birimi taleple eşleşmiyor")
            received = _instant(email.get("received_at")) if email.get("received_at") else None
            if received is None or received > now or received < _instant(dispatch["dispatched_at"]):
                raise ValueError("Yanıt zamanı gönderimden sonra ve gelecekte olmayacak şekilde kaynakta belirtilmeli")
            if email.get("message_id") == dispatch["message_id"]:
                raise ValueError("Gönderilen ileti tedarikçi yanıtı olarak kaydedilemez")
            canonical = self._canonical_offer(request, request_row["source"], offer)
            serialized = _json(canonical, maximum=500_000)
            key = _json([dispatch["mailbox"], canonical["email"]["message_id"]])
            binding_hash = _hash({**{key: value for key, value in response.items() if key != "offer"}, "offer": canonical})
            existing = connection.execute("SELECT * FROM rfq_responses WHERE message_key=? OR offer_id=?", (key, canonical["id"])).fetchall()
            if existing:
                if (len(existing) != 1 or existing[0]["rfq_id"] != rfq_id or existing[0]["message_key"] != key
                        or existing[0]["payload_hash"] != binding_hash):
                    raise ValueError("Aynı yanıt kaynağı başka içerik veya teklif isteğiyle yeniden kullanılamaz")
                return self._public(connection, row, now)
            saved = connection.execute("SELECT * FROM offers WHERE message_key=? OR id=?", (key, canonical["id"])).fetchall()
            if saved:
                if (len(saved) != 1 or saved[0]["request_id"] != row["request_id"] or saved[0]["message_key"] != key
                        or saved[0]["payload"] != serialized):
                    raise ValueError("Yanıt kaynağı mevcut teklif kaydıyla çakışıyor")
            else:
                connection.execute("INSERT INTO offers(id,request_id,message_key,payload,added_at) VALUES(?,?,?,?,?)",
                                   (canonical["id"], row["request_id"], key, serialized, now.isoformat()))
                connection.execute("UPDATE requests SET offer_revision=offer_revision+1 WHERE id=?", (row["request_id"],))
            connection.execute("INSERT INTO rfq_responses VALUES(?,?,?,?,?,?)", (key, rfq_id, canonical["id"], binding_hash,
                                                                             received.isoformat(), int(received > _instant(row["deadline_at"]))))
            return self._public(connection, row, now)

    def status(self, request_id=None, now=None):
        now = _instant(now)
        result = {"queue_status": "not_created", "drafts": [], "total": 0, "counts": {}, "emails_sent": False,
                  "network_send_verified": False}
        if not self.path.is_file():
            return result
        with closing(_read_connection(self.path)) as connection:
            result["queue_status"] = "existing"
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rfq_drafts'").fetchone() is None:
                return result
            query, args = "SELECT * FROM rfq_drafts", []
            if request_id is not None:
                query += " WHERE request_id=?"
                args.append(request_id)
            rows = connection.execute(query + " ORDER BY prepared_at,id", args).fetchall()
            result["drafts"] = [self._public(connection, row, now) for row in rows]
        for item in result["drafts"]:
            result["counts"][item["state"]] = result["counts"].get(item["state"], 0) + 1
        result["total"] = len(result["drafts"])
        return result
