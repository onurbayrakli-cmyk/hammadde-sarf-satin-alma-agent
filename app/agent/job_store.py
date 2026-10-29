"""Durable local negotiation claims; no provider, mailbox or credential access."""

from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class StaleNegotiation(ValueError):
    pass


class NegotiationJobs:
    """A crashed attempted provider call is uncertain, never automatically retried."""

    def __init__(self, path, *, lease_seconds=180, clock=None):
        self.path = Path(path).resolve()
        self.lease_seconds = lease_seconds
        self.clock = clock or time.time

    @contextmanager
    def _write(self):
        # A read/empty worker must never initialize a procurement database.
        if not self.path.is_file():
            raise ValueError("Talep kuyruğu bulunamadı")
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS negotiation_jobs (
                job_key TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests(id),
                fingerprint TEXT NOT NULL, offer_revision INTEGER NOT NULL, deadline_at TEXT NOT NULL,
                generation INTEGER NOT NULL, owner TEXT NOT NULL, state TEXT NOT NULL,
                lease_until REAL NOT NULL, model_attempted INTEGER NOT NULL DEFAULT 0,
                model_completed INTEGER NOT NULL DEFAULT 0, attempt_id TEXT,
                metadata_path TEXT, manifest_path TEXT, manifest_sha256 TEXT,
                UNIQUE(request_id,fingerprint))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS negotiation_heads (
                request_id TEXT PRIMARY KEY REFERENCES requests(id),
                generation INTEGER NOT NULL, job_key TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS negotiation_attempts (
                attempt_id TEXT PRIMARY KEY, job_key TEXT NOT NULL REFERENCES negotiation_jobs(job_key),
                owner TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL, outcome TEXT NOT NULL)""")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _source_current(connection, request):
        row = connection.execute(
            "SELECT offer_revision,deadline_at FROM requests WHERE id=?", (request["id"],)
        ).fetchone()
        return bool(
            row and row["offer_revision"] == request["offer_revision"] and row["deadline_at"] == request["deadline_at"]
        )

    @staticmethod
    def _row(connection, key):
        row = connection.execute("SELECT * FROM negotiation_jobs WHERE job_key=?", (key,)).fetchone()
        if row is None:
            raise ValueError("Pazarlık işi bulunamadı")
        return dict(row)

    def claim(self, request, fingerprint, *, refresh=False, request_model=False):
        key = hashlib.sha256((request["id"] + "\0" + fingerprint).encode()).hexdigest()
        now, owner = self.clock(), uuid.uuid4().hex
        with self._write() as connection:
            if not self._source_current(connection, request):
                raise StaleNegotiation("Pazarlık sırasında teklif revizyonu veya süre değişti")
            row = connection.execute("SELECT * FROM negotiation_jobs WHERE job_key=?", (key,)).fetchone()
            if row is not None:
                row = dict(row)
                if row["state"] == "prepared" and not refresh:
                    return {"action": "cached", **row}
                if row["model_attempted"] and (row["state"] != "prepared" or not row["model_completed"]):
                    if row["state"] == "prepared":
                        # A useful local fallback remains readable, but --refresh
                        # cannot blindly repeat a failed/ambiguous paid attempt.
                        return {"action": "cached", "refresh_blocked": bool(refresh), **row}
                    if row["state"] == "running" and row["lease_until"] > now:
                        return {"action": "in_progress", **row}
                    connection.execute("UPDATE negotiation_jobs SET state='uncertain' WHERE job_key=?", (key,))
                    action = "completed_result_unpublished" if row["model_completed"] else "model_outcome_uncertain"
                    return {"action": action, **self._row(connection, key)}
                if row["state"] == "running" and row["lease_until"] > now:
                    return {"action": "in_progress", **row}
            if request_model:
                # Changing a prompt/model must not evade an unresolved paid
                # attempt after a crash or unverified provider response.
                unresolved = connection.execute(
                    """SELECT * FROM negotiation_jobs WHERE request_id=? AND job_key<>?
                    AND model_attempted=1 AND (
                      state='uncertain' OR
                      (model_completed=0 AND state='prepared') OR
                      (state='running' AND lease_until<=?)) ORDER BY generation DESC LIMIT 1""",
                    (request["id"], key, now),
                ).fetchone()
                if unresolved is not None:
                    if unresolved["state"] == "running":
                        connection.execute(
                            "UPDATE negotiation_jobs SET state='uncertain' WHERE job_key=?", (unresolved["job_key"],)
                        )
                    action = (
                        "completed_result_unpublished" if unresolved["model_completed"] else "model_outcome_uncertain"
                    )
                    return {"action": action, **self._row(connection, unresolved["job_key"])}
            head = connection.execute(
                "SELECT generation FROM negotiation_heads WHERE request_id=?", (request["id"],)
            ).fetchone()
            generation = head["generation"] + 1 if head else 1
            connection.execute(
                """INSERT INTO negotiation_heads VALUES(?,?,?)
                ON CONFLICT(request_id) DO UPDATE SET generation=excluded.generation,job_key=excluded.job_key""",
                (request["id"], generation, key),
            )
            connection.execute(
                """INSERT INTO negotiation_jobs
                (job_key,request_id,fingerprint,offer_revision,deadline_at,generation,owner,state,lease_until)
                VALUES(?,?,?,?,?,?,?,'running',?) ON CONFLICT(job_key) DO UPDATE SET
                generation=excluded.generation,owner=excluded.owner,state='running',lease_until=excluded.lease_until,
                model_attempted=0,model_completed=0,attempt_id=NULL,metadata_path=NULL,manifest_path=NULL,manifest_sha256=NULL""",
                (
                    key,
                    request["id"],
                    fingerprint,
                    request["offer_revision"],
                    request["deadline_at"],
                    generation,
                    owner,
                    now + self.lease_seconds,
                ),
            )
            return {"action": "claimed", **self._row(connection, key)}

    def begin_attempt(self, claim, request):
        """Commit BEFORE invoking a provider; process death leaves a durable fence."""
        with self._write() as connection:
            row = self._row(connection, claim["job_key"])
            head = connection.execute(
                "SELECT generation FROM negotiation_heads WHERE request_id=?", (request["id"],)
            ).fetchone()
            if (
                row["owner"] != claim["owner"]
                or row["state"] != "running"
                or row["model_attempted"]
                or not head
                or head["generation"] != claim["generation"]
                or not self._source_current(connection, request)
            ):
                raise StaleNegotiation("Pazarlık işi güncelliğini veya sahipliğini kaybetti")
            attempt, now = uuid.uuid4().hex, self.clock()
            connection.execute(
                "INSERT INTO negotiation_attempts VALUES(?,?,?,?,NULL,'attempted')",
                (attempt, claim["job_key"], claim["owner"], now),
            )
            connection.execute(
                "UPDATE negotiation_jobs SET model_attempted=1,attempt_id=?,lease_until=? WHERE job_key=?",
                (attempt, now + self.lease_seconds, claim["job_key"]),
            )
            claim["attempt_id"], claim["model_attempted"] = attempt, 1

    def finish_attempt(self, claim, *, completed):
        with self._write() as connection:
            row = self._row(connection, claim["job_key"])
            if row["owner"] != claim["owner"] or row["attempt_id"] != claim.get("attempt_id"):
                raise StaleNegotiation("Model denemesi farklı bir işe ait")
            connection.execute(
                "UPDATE negotiation_attempts SET finished_at=?,outcome=? WHERE attempt_id=?",
                (self.clock(), "completed" if completed else "unverified", row["attempt_id"]),
            )
            connection.execute(
                "UPDATE negotiation_jobs SET model_completed=? WHERE job_key=?", (int(completed), claim["job_key"])
            )
            claim["model_completed"] = int(completed)

    def publish(self, claim, request, metadata_path, manifest_path, manifest_sha256):
        with self._write() as connection:
            row = self._row(connection, claim["job_key"])
            head = connection.execute(
                "SELECT generation FROM negotiation_heads WHERE request_id=?", (request["id"],)
            ).fetchone()
            if (
                row["owner"] != claim["owner"]
                or row["state"] not in {"running", "uncertain"}
                or not head
                or head["generation"] != claim["generation"]
                or not self._source_current(connection, request)
            ):
                if row["owner"] == claim["owner"]:
                    connection.execute(
                        "UPDATE negotiation_jobs SET state='superseded' WHERE job_key=?", (claim["job_key"],)
                    )
                return False
            connection.execute(
                """UPDATE negotiation_jobs SET state='prepared',metadata_path=?,manifest_path=?,
                manifest_sha256=? WHERE job_key=?""",
                (str(metadata_path), str(manifest_path), manifest_sha256, claim["job_key"]),
            )
            return True

    def fail(self, claim):
        with self._write() as connection:
            row = self._row(connection, claim["job_key"])
            if row["owner"] == claim["owner"] and row["state"] in {"running", "uncertain"}:
                state = "uncertain" if row["model_attempted"] else "failed_safe"
                connection.execute(
                    "UPDATE negotiation_jobs SET state=?,lease_until=0 WHERE job_key=?", (state, claim["job_key"])
                )

    def activate_cached(self, claim, request):
        """Make an already validated cache current only under a source revision lock."""
        with self._write() as connection:
            if not self._source_current(connection, request):
                raise StaleNegotiation("Önbellek doğrulanırken teklif revizyonu değişti")
            row = self._row(connection, claim["job_key"])
            if row["state"] != "prepared" or row["manifest_sha256"] != claim["manifest_sha256"]:
                raise StaleNegotiation("Önbellek doğrulanırken yeni bir çalışma başladı")
            head = connection.execute("SELECT * FROM negotiation_heads WHERE request_id=?", (request["id"],)).fetchone()
            if head and head["job_key"] != claim["job_key"]:
                connection.execute(
                    "UPDATE negotiation_heads SET generation=generation+1,job_key=? WHERE request_id=?",
                    (claim["job_key"], request["id"]),
                )
