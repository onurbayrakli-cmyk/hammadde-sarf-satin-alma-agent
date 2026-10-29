"""Local procurement request queue; it never connects to SAP or sends email.

The request owner's explicit, timezone-aware reply starts a collection window.
Saved question drafts are deliberately distinct from sent messages. A report is
prepared only after its file exists and the window has ended. SQLite transactions
make message replay and local concurrent monitor runs idempotent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parent / "cases" / "requests.db"
EMAIL = re.compile(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+")
SECRET_KEYS = {
    "apikey", "accesstoken", "refreshtoken", "token", "password", "secret",
    "clientsecret", "authorization", "headers", "credentials", "bearertoken",
}
OFFER_FIELDS = {
    "id", "request_id", "supplier", "email", "material_code", "material_name",
    "quantity", "unit", "currency", "cash_unit_price", "term_unit_price", "term_days",
    "vat_rate_pct", "vat_basis", "price_basis", "delivery_days", "technical_specs",
    "source_fields", "missing_fields",
}
EMAIL_FIELDS = {"message_id", "mailbox", "received_at", "sender", "subject", "body_excerpt", "source_url", "attachments"}
ATTACHMENT_FIELDS = {"attachment_id", "filename", "text_excerpt", "source_url", "sha256"}


def _text(value: Any, *, maximum: int = 500, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise ValueError("Zorunlu metin alanı eksik")
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError("Metin alanı geçersiz veya çok uzun")
    value = value.strip()
    if required and not value:
        raise ValueError("Zorunlu metin alanı eksik")
    return value or None


def _email(value: Any, *, required: bool = False) -> str | None:
    value = _text(value, maximum=320, required=required)
    if value is not None and not EMAIL.fullmatch(value):
        raise ValueError("E-posta adresi geçersiz")
    return value


def _instant(value: Any = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Tarih ISO biçiminde ve saat dilimi içermeli") from None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Tarih ISO biçiminde ve saat dilimi içermeli")
    return value.astimezone(timezone.utc)


def _quantity(value: Any) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    if isinstance(value, bool) or len(text) > 32 or not re.fullmatch(r"\d+(?:[.,]\d{1,6})?", text):
        raise ValueError("Miktar sıfır veya pozitif, en çok 6 ondalıklı olmalı")
    try:
        number = Decimal(text.replace(",", "."))
    except InvalidOperation:
        raise ValueError("Miktar geçersiz") from None
    if not number.is_finite() or number > Decimal("1e12"):
        raise ValueError("Miktar sınır dışında")
    return format(number.normalize(), "f")


def _no_secrets(value: Any, depth: int = 0) -> None:
    if depth > 12:
        raise ValueError("Veri iç içe yapı sınırını aşıyor")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON alan adları metin olmalı")
            if re.sub(r"[^a-z0-9]", "", key.casefold()) in SECRET_KEYS:
                raise ValueError("Bağlantı sırrı veya HTTP başlığı talep kuyruğuna yazılamaz")
            _no_secrets(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _no_secrets(item, depth + 1)


def _json(value: Any, maximum: int = 200_000) -> str:
    _no_secrets(value)
    try:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError):
        raise ValueError("Yalnız sonlu sayılar ve JSON alanları kaydedilebilir") from None
    if len(serialized.encode("utf-8")) > maximum:
        raise ValueError("Talep veya teklif boyut sınırını aşıyor")
    return serialized


def _regular_file_bytes(filename, maximum):
    """Read a bounded regular artifact without following a final symlink."""
    path = Path(filename)
    if path.is_symlink():
        raise ValueError("Taslak dosyası sembolik bağlantı olamaz")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= maximum:
            raise ValueError("Taslak dosyası boş veya boyut sınırının dışında")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            value = handle.read(maximum + 1)
        if not 0 < len(value) <= maximum:
            raise ValueError("Taslak dosyası boş veya boyut sınırının dışında")
        return value
    finally:
        os.close(descriptor)


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON alanları yinelenemez")
        result[key] = value
    return result


def _question_artifact_valid(row):
    """A path alone is not a prepared question; source and bytes must match."""
    keys = row.keys()
    fields = ("question_draft_text", "question_draft_sha256", "question_source_sha256")
    if not row["question_draft_path"] or any(key not in keys or not row[key] for key in fields):
        return False
    try:
        content = row["question_draft_text"].encode("utf-8")
        source_hash = hashlib.sha256(row["payload"].encode("utf-8")).hexdigest()
        return (row["question_source_sha256"] == source_hash
                and hashlib.sha256(content).hexdigest() == row["question_draft_sha256"]
                and _regular_file_bytes(row["question_draft_path"], 200_000) == content)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _question_text(request):
    return (
        "E-POSTA TASLAĞI — GÖNDERİLMEDİ\n"
        f"Alıcı: {request['requester']}\n"
        f"Konu: {request['id']} — araştırma bitiş zamanı\n\n"
        f"{request['material_name']} için tedarikçi araştırmasını ve gelen tekliflerin karşılaştırmasını "
        "hangi tarih ve saate kadar tamamlamamı istersiniz?\n"
        "Lütfen bitiş tarihini ve saati Türkiye saati olarak belirtin. Her talebin süresi ayrı belirlenir; "
        "varsayılan süre atanmaz.\n"
        "Süre kesinleştiğinde teklif e-postaları ve varsa teknik özellikleri, vade farkı, aylık fark oranları "
        "ve KDV beyanları karşılaştırılarak yönetici raporu taslağı hazırlanacaktır.\n"
        f"Talep kimliği: {request['id']}\n"
    )


def _report_artifact_valid(filename, *, request_id=None, offer_revision=None, report_revision=None):
    """Verify new worker manifests; older manually recorded files must be nonempty."""
    if not filename:
        return False
    try:
        path = Path(filename)
        _regular_file_bytes(path, 10_000_000)
        manifest_path = path.parent / "manifest.json"
        worker_directory = re.search(r"_yonetici_raporu_v\d+_o\d+_", path.parent.name)
        if manifest_path.is_symlink():
            return False
        if not manifest_path.is_file():
            return not worker_directory
        manifest = json.loads(_regular_file_bytes(manifest_path, 64_000), object_pairs_hook=_unique_json_object)
        for key, expected in (("request_id", request_id), ("offer_revision", offer_revision),
                              ("report_revision", report_revision)):
            if expected is not None and manifest.get(key) != expected:
                return False
        files = manifest.get("files")
        if not isinstance(files, list) or not 1 <= len(files) <= 10:
            return False
        names = set()
        for item in files:
            name, digest = item["name"], item["sha256"]
            if (not isinstance(name, str) or Path(name).name != name or name in names
                    or not re.fullmatch(r"[a-f0-9]{64}", digest)):
                return False
            names.add(name)
            artifact = path.parent / name
            if hashlib.sha256(_regular_file_bytes(artifact, 10_000_000)).hexdigest() != digest:
                return False
        return path.name in names
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return False


class CaseStore:
    def __init__(self, path: str | Path = DEFAULT_PATH):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL, created_at TEXT NOT NULL, deadline_at TEXT,
                    question_claimed INTEGER NOT NULL DEFAULT 0, question_draft_path TEXT,
                    offer_revision INTEGER NOT NULL DEFAULT 0, report_revision INTEGER NOT NULL DEFAULT 0,
                    report_offer_revision INTEGER NOT NULL DEFAULT -1, report_path TEXT, report_at TEXT
                );
                CREATE TABLE IF NOT EXISTS offers (
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests(id),
                    message_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, added_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS offers_request ON offers(request_id);
                CREATE INDEX IF NOT EXISTS requests_deadline ON requests(deadline_at);
            """)
            # Existing queues migrate only during a mutating Store open. Status
            # uses read-only SQL and treats legacy unverified artifacts as pending.
            connection.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(requests)")}
            for name in ("question_draft_text", "question_draft_sha256", "question_source_sha256"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE requests ADD COLUMN {name} TEXT")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _request_row(connection: sqlite3.Connection, request_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise ValueError("Talep bulunamadı")
        return row

    def _snapshot(self, connection: sqlite3.Connection, row: sqlite3.Row, now: datetime) -> dict:
        request = json.loads(row["payload"])
        request["id"] = row["id"]
        request["deadline_at"] = row["deadline_at"]
        if row["deadline_at"] is None:
            state = "awaiting_deadline"
        elif now < _instant(row["deadline_at"]):
            state = "collecting"
        elif (row["report_offer_revision"] == row["offer_revision"]
              and _report_artifact_valid(row["report_path"], request_id=row["id"],
                                         offer_revision=row["offer_revision"], report_revision=row["report_revision"])):
            state = "report_prepared"
        else:
            state = "ready_for_report"
        request["state"] = state
        request["report_revision"] = row["report_revision"]
        request["offer_revision"] = row["offer_revision"]
        offers = [json.loads(item["payload"]) for item in connection.execute(
            "SELECT payload FROM offers WHERE request_id=? ORDER BY added_at,id", (row["id"],)
        )]
        question_status = "draft_preparing" if row["question_claimed"] else "pending"
        if row["question_draft_path"]:
            question_status = "draft_prepared" if _question_artifact_valid(row) else "artifact_missing_or_changed"
        return {
            "request": request, "offers": offers, "as_of": now.isoformat(),
            "source": row["source"], "report_path": row["report_path"], "report_at": row["report_at"],
            "question_draft": {
                "status": question_status,
                "path": row["question_draft_path"], "recipient": request["requester"], "sent": False,
            },
        }

    def upsert_request(self, request: dict, source: str) -> dict:
        """Register immutable intake facts; deadline replies use set_deadline()."""
        if not isinstance(request, dict):
            raise ValueError("Talep JSON nesnesi olmalı")
        _no_secrets(request)
        if not isinstance(source, str):
            raise ValueError("Talep kaynağı metin olmalı")
        source = {"sap_api": "sap", "mail": "mail_request"}.get(source, source)
        if source not in {"sap", "mail_request", "manual"}:
            raise ValueError("Talep kaynağı sap, mail_request veya manual olmalı")
        material_name = _text(request.get("material_name") or request.get("name"), maximum=300, required=True)
        supplied_id = _text(request.get("id") or request.get("request_id"), maximum=200)
        requester = _email(request.get("requester") or request.get("sender"), required=source in {"mail_request", "manual"})
        mailbox = _email(request.get("mailbox"))
        message_id = _text(request.get("message_id"), maximum=500, required=source == "mail_request")
        if source in {"sap", "manual"}:
            if supplied_id is None:
                raise ValueError("Talep kimliği açıkça sağlanmalı")
            identity = source + ":" + supplied_id
            request_id = supplied_id
        else:
            identity = "mail_request:" + _json([mailbox or "", message_id])
            request_id = supplied_id or "MAIL-" + hashlib.sha256(identity.encode()).hexdigest()[:20]
        specs = request.get("required_specs") or {}
        if not isinstance(specs, dict) or len(specs) > 30:
            raise ValueError("Teknik şartname en çok 30 alanlı nesne olmalı")
        unit = _text(request.get("unit"), maximum=32)
        payload = {
            "material_code": _text(request.get("material_code"), maximum=100),
            "material_name": material_name, "quantity": _quantity(request.get("quantity")),
            "unit": unit, "required_specs": specs, "requester": requester,
            "mailbox": mailbox, "message_id": message_id,
        }
        if "sap_requisition" in request:
            metadata = request["sap_requisition"]
            fields = {"source_system", "purchase_requisition", "purchase_requisition_item"}
            if source != "sap" or not isinstance(metadata, dict) or set(metadata) != fields:
                raise ValueError("SAP talep kaynağı yalnız sap için üç açık kimlik alanıyla belirtilmeli")
            canonical_metadata = {key: _text(metadata[key], maximum=100, required=True) for key in sorted(fields)}
            if any(any(ord(char) < 32 or ord(char) == 127 for char in value) for value in canonical_metadata.values()):
                raise ValueError("SAP talep kimlik alanları tek satırlı metin olmalı")
            payload["sap_requisition"] = canonical_metadata
        # Keep optional purchasing scope immutable without changing old records.
        for key in ("delivery_place", "need_by", "material_kind"):
            if request.get(key) not in (None, ""):
                payload[key] = _text(request[key], maximum=500, required=True)
        if payload.get("material_kind") not in (None, "hammadde", "sarf"):
            raise ValueError("Malzeme türü hammadde veya sarf olmalı")
        serialized = _json(payload)
        now = _instant()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM requests WHERE source_key=?", (identity,)).fetchone()
            if existing is not None:
                if existing["id"] != request_id or existing["payload"] != serialized:
                    raise ValueError("Aynı kaynak talep kimliği farklı malzeme veya içerikle yeniden kullanılamaz")
                return self._snapshot(connection, existing, now)
            if connection.execute("SELECT 1 FROM requests WHERE id=?", (request_id,)).fetchone():
                raise ValueError("Talep kimliği başka kaynağa ait; eşleşme belirsiz")
            connection.execute(
                "INSERT INTO requests(id,source,source_key,payload,created_at) VALUES(?,?,?,?,?)",
                (request_id, source, identity, serialized, now.isoformat()),
            )
            return self._snapshot(connection, self._request_row(connection, request_id), now)

    def set_deadline(self, request_id: str, deadline_at: str, requester_email: str, now=None) -> dict:
        now = _instant(now)
        if not deadline_at:
            raise ValueError("Bitiş zamanı talep sahibinin yanıtında açıkça belirtilmeli")
        deadline = _instant(deadline_at)
        owner = _email(requester_email, required=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._request_row(connection, request_id)
            request = json.loads(row["payload"])
            if request["requester"] is None or owner != request["requester"]:
                raise ValueError("Bitiş zamanını yalnız talebin kayıtlı sahibi belirleyebilir")
            if row["deadline_at"] and deadline == _instant(row["deadline_at"]):
                return self._snapshot(connection, row, now)
            if deadline <= now:
                raise ValueError("Yeni bitiş zamanı gelecekte olmalı")
            if row["deadline_at"] and (now >= _instant(row["deadline_at"]) or row["report_revision"]):
                raise ValueError("Tamamlanan araştırmanın bitiş zamanı değiştirilemez; yeni talep açılmalı")
            connection.execute("UPDATE requests SET deadline_at=? WHERE id=?", (deadline.isoformat(), request_id))
            return self._snapshot(connection, self._request_row(connection, request_id), now)

    def append_offer(self, request_id: str, offer: dict) -> dict:
        if not isinstance(offer, dict):
            raise ValueError("Teklif JSON nesnesi olmalı")
        _no_secrets(offer)
        if offer.get("request_id") != request_id:
            raise ValueError("Teklif talep kimliğine açıkça bağlanmalı")
        canonical = {key: value for key, value in offer.items() if key in OFFER_FIELDS}
        email = canonical.get("email")
        if not isinstance(email, dict):
            raise ValueError("Teklifin kaynak e-postası gerekli")
        email = {key: value for key, value in email.items() if key in EMAIL_FIELDS}
        canonical["email"] = email
        message_id = _text(email.get("message_id"), maximum=500, required=True)
        mailbox = _email(email.get("mailbox"))
        _email(email.get("sender"), required=True)
        _instant(email.get("received_at")) if email.get("received_at") else None
        if not email.get("received_at"):
            raise ValueError("Teklif e-postasının saat dilimli geliş tarihi gerekli")
        attachments = email.get("attachments", [])
        if not isinstance(attachments, list) or len(attachments) > 30:
            raise ValueError("E-posta ekleri en çok 30 kayıtlı liste olmalı")
        if any(not isinstance(item, dict) for item in attachments):
            raise ValueError("E-posta eki JSON nesnesi olmalı")
        email["attachments"] = [{key: value for key, value in item.items() if key in ATTACHMENT_FIELDS} for item in attachments]
        offer_id = _text(offer.get("id"), maximum=200) or "OFFER-" + hashlib.sha256(_json([mailbox, message_id]).encode()).hexdigest()[:20]
        canonical["id"] = offer_id
        _text(canonical.get("supplier"), maximum=500, required=True)
        serialized = _json(canonical, maximum=500_000)
        now = _instant()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._request_row(connection, request_id)
            request = json.loads(row["payload"])
            original_code = request["material_code"]
            if original_code:
                if _text(offer.get("material_code"), maximum=100) != original_code:
                    raise ValueError("Teklif malzeme kodu özgün taleple eşleşmiyor")
            elif _text(offer.get("material_name"), maximum=300) != request["material_name"]:
                raise ValueError("Kod yoksa teklif özgün malzeme adıyla açıkça eşleşmeli")
            # Missing mailbox uses the known original mailbox, never a guessed address.
            identity = _json([mailbox or request["mailbox"] or "", message_id])
            existing = connection.execute("SELECT * FROM offers WHERE message_key=? OR id=?", (identity, offer_id)).fetchall()
            if existing:
                if (len(existing) != 1 or existing[0]["request_id"] != request_id
                        or existing[0]["message_key"] != identity or existing[0]["payload"] != serialized):
                    raise ValueError("Aynı teklif e-postası farklı talep veya içerikle eşleştirilemez")
                return self._snapshot(connection, row, now)
            has_rfq_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rfq_drafts'"
            ).fetchone()
            if has_rfq_table and connection.execute(
                "SELECT 1 FROM rfq_drafts WHERE request_id=? LIMIT 1", (request_id,)
            ).fetchone():
                raise ValueError("Bu talebin teklif isteği kaydı var; yeni yanıtı rfq_worker.py ingest-response ile kaynak konuşmasına bağlayın")
            connection.execute(
                "INSERT INTO offers(id,request_id,message_key,payload,added_at) VALUES(?,?,?,?,?)",
                (offer_id, request_id, identity, serialized, now.isoformat()),
            )
            connection.execute("UPDATE requests SET offer_revision=offer_revision+1 WHERE id=?", (request_id,))
            return self._snapshot(connection, self._request_row(connection, request_id), now)

    def snapshot(self, request_id: str, now=None) -> dict:
        now = _instant(now)
        with self._connect() as connection:
            return self._snapshot(connection, self._request_row(connection, request_id), now)

    def list_due(self, now=None) -> list[dict]:
        now = _instant(now)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM requests WHERE deadline_at IS NOT NULL ORDER BY deadline_at,id").fetchall()
            snapshots = [self._snapshot(connection, row, now) for row in rows]
            return [item for item in snapshots if item["request"]["state"] == "ready_for_report"]

    def claim_pending_questions(self) -> list[dict]:
        """Claim question *drafts* once; no sent/delivered state is invented."""
        now = _instant()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM requests WHERE deadline_at IS NULL AND question_claimed=0 ORDER BY created_at,id"
            ).fetchall()
            output = []
            for row in rows:
                if json.loads(row["payload"])["requester"] is None:
                    continue
                connection.execute("UPDATE requests SET question_claimed=1 WHERE id=?", (row["id"],))
                output.append(self._snapshot(connection, self._request_row(connection, row["id"]), now))
            return output

    def set_question_draft(self, request_id: str, path: str | Path) -> dict:
        path = Path(path).expanduser().absolute()
        path = path.parent.resolve() / path.name
        try:
            content = _regular_file_bytes(path, 200_000).decode("utf-8")
        except (OSError, ValueError, UnicodeError):
            raise ValueError("Süre sorusu taslağı önce dosyaya kaydedilmeli")
        now = _instant()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._request_row(connection, request_id)
            if row["deadline_at"] is not None:
                raise ValueError("Bitiş zamanı biliniyor; yeni süre sorusu hazırlanmamalı")
            if json.loads(row["payload"])["requester"] is None:
                raise ValueError("Taslak alıcısı bilinmiyor")
            if (_question_artifact_valid(row) and row["question_draft_path"] != str(path)):
                raise ValueError("Bu talep için süre sorusu taslağı zaten hazırlanmış")
            if row["question_draft_text"] and row["question_draft_text"] != content:
                raise ValueError("Kaydedilmiş süre sorusunun içeriği değiştirilemez")
            connection.execute("""UPDATE requests SET question_claimed=1,question_draft_path=?,
                question_draft_text=?,question_draft_sha256=?,question_source_sha256=? WHERE id=?""",
                (str(path), content, hashlib.sha256(content.encode()).hexdigest(),
                 hashlib.sha256(row["payload"].encode()).hexdigest(), request_id))
            return self._snapshot(connection, self._request_row(connection, request_id), now)

    def prepare_question_drafts(self, output_dir: str | Path) -> list[dict]:
        """Publish/recover one immutable artifact per pending request under lock.

        A process interrupted after claiming or after file creation is recoverable:
        durable DB text is authoritative and uncommitted artifact paths are never
        reported ready. No claim lease is needed because this bounded local write
        holds the SQLite transaction until the selected path and hashes commit.
        """
        now = _instant()
        output = Path(output_dir).expanduser().resolve()
        drafts = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT * FROM requests WHERE deadline_at IS NULL ORDER BY created_at,id").fetchall()
            for row in rows:
                request = {**json.loads(row["payload"]), "id": row["id"]}
                if not request["requester"] or _question_artifact_valid(row):
                    continue
                source_hash = hashlib.sha256(row["payload"].encode()).hexdigest()
                content = row["question_draft_text"]
                if (not isinstance(content, str) or not content or len(content.encode()) > 200_000
                        or row["question_source_sha256"] != source_hash
                        or hashlib.sha256(content.encode()).hexdigest() != row["question_draft_sha256"]):
                    content = _question_text(request)
                output.mkdir(parents=True, exist_ok=True)
                prefix = "talep_" + hashlib.sha256(row["id"].encode()).hexdigest()[:20] + "_sure_sorusu_"
                directory = Path(tempfile.mkdtemp(prefix=prefix, dir=output))
                path = directory / "sure_sorusu.txt"
                with path.open("x", encoding="utf-8") as handle:
                    handle.write(content)
                path.chmod(0o600)
                digest = hashlib.sha256(content.encode()).hexdigest()
                if hashlib.sha256(_regular_file_bytes(path, 200_000)).hexdigest() != digest:
                    raise ValueError("Süre sorusu dosya özeti doğrulanamadı")
                connection.execute("""UPDATE requests SET question_claimed=1,question_draft_path=?,
                    question_draft_text=?,question_draft_sha256=?,question_source_sha256=? WHERE id=?""",
                    (str(path), content, digest, source_hash, row["id"]))
                drafts.append(self._snapshot(connection, self._request_row(connection, row["id"]), now))
        return drafts

    def mark_report_prepared(self, request_id: str, artifact_path: str | Path, now=None,
                             *, expected_offer_revision: int | None = None) -> dict:
        now = _instant(now)
        path = Path(artifact_path).expanduser().absolute()
        path = path.parent.resolve() / path.name
        if not _report_artifact_valid(path):
            raise ValueError("Yönetici raporu önce boş olmayan dosyaya kaydedilmeli")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._request_row(connection, request_id)
            if row["deadline_at"] is None or now < _instant(row["deadline_at"]):
                raise ValueError("Araştırma süresi dolmadan rapor tamamlandı sayılamaz")
            if expected_offer_revision is not None and row["offer_revision"] != expected_offer_revision:
                raise ValueError("Rapor hazırlanırken yeni teklif geldi; rapor yenilenmeli")
            if (row["report_offer_revision"] == row["offer_revision"]
                    and _report_artifact_valid(row["report_path"], request_id=row["id"],
                                               offer_revision=row["offer_revision"], report_revision=row["report_revision"])):
                if row["report_path"] != str(path):
                    raise ValueError("Bu teklif revizyonu için rapor zaten hazırlandı")
                return self._snapshot(connection, row, now)
            if not _report_artifact_valid(path, request_id=request_id, offer_revision=row["offer_revision"],
                                          report_revision=row["report_revision"] + 1):
                raise ValueError("Yönetici raporu dosya özeti, talep veya revizyon bilgisi doğrulanamadı")
            connection.execute(
                "UPDATE requests SET report_revision=report_revision+1,report_offer_revision=offer_revision,report_path=?,report_at=? WHERE id=?",
                (str(path), now.isoformat(), request_id),
            )
            return self._snapshot(connection, self._request_row(connection, request_id), now)
