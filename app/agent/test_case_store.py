"""SQLite queue tests with virtual times and temporary synthetic files only."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from case_store import CaseStore

BEFORE = "2026-10-13T08:00:00+03:00"
DEADLINE = "2026-10-13T09:00:00+03:00"
AFTER = "2026-10-13T09:01:00+03:00"


def sample_request(request_id="R1", message_id="request-message-1", mailbox="bot@example.com"):
    return {
        "id": request_id, "material_code": "PE", "material_name": "Polietilen", "quantity": "10",
        "unit": "KG", "required_specs": {"grade": "A"}, "requester": "talep@example.com",
        "mailbox": mailbox, "message_id": message_id,
    }


def sample_offer(offer_id="Q1", message_id="offer-message-1", request_id="R1"):
    return {
        "id": offer_id, "request_id": request_id, "supplier": "Sentetik tedarikçi",
        "email": {"message_id": message_id, "sender": "teklif@example.com", "received_at": BEFORE,
                  "subject": "PE teklifi", "body_excerpt": "Peşin birim fiyat 100 TRY; miktar 10 KG",
                  "attachments": [{"attachment_id": "A1", "filename": "teklif.txt", "text_excerpt": "Grade A"}]},
        "material_code": "PE", "quantity": "10", "unit": "KG", "currency": "TRY",
        "cash_unit_price": "100", "term_unit_price": "110", "term_days": "60",
        "vat_rate_pct": "20", "vat_basis": "including_term_markup", "price_basis": "vat_exclusive",
        "delivery_days": "3", "technical_specs": {"grade": "A"},
        "source_fields": {"cash_unit_price": {"quote": "Peşin birim fiyat 100 TRY", "message_id": message_id}},
        "missing_fields": [],
    }


class CaseStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "requests.db"
        self.store = CaseStore(self.path)
        self.store.upsert_request(sample_request(), "mail_request")

    def tearDown(self):
        self.temporary.cleanup()

    def deadline(self):
        return self.store.set_deadline("R1", DEADLINE, "talep@example.com", now=BEFORE)

    def artifact(self, name="report.md"):
        path = Path(self.temporary.name) / name
        path.write_text("Sentetik yönetici raporu: teklif yok.", encoding="utf-8")
        return path

    def test_intake_does_not_trust_supplied_deadline_or_sent_state(self):
        request = sample_request()
        request.update(deadline_at=DEADLINE, state="report_prepared", question_sent=True)
        self.store.upsert_request(request, "mail_request")
        snapshot = self.store.snapshot("R1", BEFORE)
        self.assertIsNone(snapshot["request"]["deadline_at"])
        self.assertEqual(snapshot["request"]["state"], "awaiting_deadline")
        self.assertFalse(snapshot["question_draft"]["sent"])
        self.assertEqual(self.store.list_due(AFTER), [])

    def test_mail_requires_original_sender_and_message_identity(self):
        for field in ("requester", "message_id"):
            request = sample_request("R2", "request-message-2")
            request.pop(field)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.upsert_request(request, "mail_request")

    def test_same_mail_replay_is_idempotent_across_restart(self):
        self.store.upsert_request(sample_request(), "mail_request")
        self.deadline()
        other_store = CaseStore(self.path)
        replay = other_store.upsert_request(sample_request(), "mail_request")
        self.assertEqual(replay["request"]["id"], "R1")
        self.assertEqual(other_store.snapshot("R1", BEFORE)["request"]["state"], "collecting")

    def test_mailbox_part_of_identity_and_ids_cannot_be_forged(self):
        other = sample_request("R2", mailbox="other@example.com")
        self.store.upsert_request(other, "mail")
        self.assertEqual(self.store.snapshot("R2", BEFORE)["request"]["mailbox"], "other@example.com")
        other["id"] = "R1"
        with self.assertRaises(ValueError):
            self.store.upsert_request(other, "mail")

    def test_same_message_cannot_change_material_or_create_new_case_id(self):
        for field, value in (("material_code", "PP"), ("id", "R2"), ("quantity", "11"), ("requester", "other@example.com")):
            request = sample_request()
            request[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.upsert_request(request, "mail_request")

    def test_generated_mail_ids_stable_and_no_guessed_recipient(self):
        request = sample_request("R2", "request-message-2", mailbox=None)
        request.pop("id")
        first = self.store.upsert_request(request, "mail_request")
        second = self.store.upsert_request(request, "mail_request")
        self.assertEqual(first["request"]["id"], second["request"]["id"])
        self.assertIsNone(first["request"]["mailbox"])
        self.assertEqual(first["question_draft"]["recipient"], "talep@example.com")

    def test_sap_requires_explicit_id_and_can_await_unknown_owner(self):
        request = sample_request("SAP-42")
        request.update(requester=None, message_id=None, mailbox=None)
        result = self.store.upsert_request(request, "sap_api")
        self.assertEqual(result["source"], "sap")
        self.assertEqual(result["request"]["id"], "SAP-42")
        with self.assertRaises(ValueError):
            self.store.set_deadline("SAP-42", DEADLINE, "manager@example.com", BEFORE)
        request.pop("id")
        with self.assertRaises(ValueError):
            self.store.upsert_request(request, "sap")

    def test_deadline_only_owner_future_offset_and_real_instant(self):
        for deadline, owner in ((DEADLINE, "manager@example.com"), (None, "talep@example.com"), ("", "talep@example.com"),
                                ("2026-10-13T09:00:00", "talep@example.com"),
                                (BEFORE, "talep@example.com"), ("not-a-date", "talep@example.com")):
            with self.subTest(deadline=deadline, owner=owner), self.assertRaises(ValueError):
                self.store.set_deadline("R1", deadline, owner, now=BEFORE)
        self.deadline()
        self.assertEqual(self.store.snapshot("R1", "2026-10-13T05:59:59+00:00")["request"]["state"], "collecting")
        self.assertEqual(self.store.snapshot("R1", "2026-10-13T06:00:00+00:00")["request"]["state"], "ready_for_report")

    def test_duplicate_deadline_reply_after_due_remains_idempotent(self):
        self.deadline()
        result = self.store.set_deadline("R1", "2026-10-13T06:00:00Z", "talep@example.com", now=AFTER)
        self.assertEqual(result["request"]["state"], "ready_for_report")
        with self.assertRaises(ValueError):
            self.store.set_deadline("R1", "2026-10-14T09:00:00+03:00", "talep@example.com", now=AFTER)

    def test_deadline_change_allowed_only_while_collecting(self):
        self.deadline()
        result = self.store.set_deadline("R1", "2026-10-13T10:00:00+03:00", "talep@example.com", now=BEFORE)
        self.assertEqual(result["request"]["deadline_at"], "2026-10-13T07:00:00+00:00")

    def test_question_claimed_once_across_restart_and_saved_as_draft_only(self):
        claimed = self.store.claim_pending_questions()
        self.assertEqual([item["request"]["id"] for item in claimed], ["R1"])
        self.assertFalse(claimed[0]["question_draft"]["sent"])
        self.assertEqual(CaseStore(self.path).claim_pending_questions(), [])
        path = self.artifact("deadline_question.md")
        result = self.store.set_question_draft("R1", path)
        self.assertEqual(result["question_draft"]["status"], "draft_prepared")
        self.assertFalse(result["question_draft"]["sent"])
        with self.assertRaises(ValueError):
            self.store.set_question_draft("R1", self.artifact("second_question.md"))

    def test_question_file_must_exist_and_no_question_after_answer(self):
        with self.assertRaises(ValueError):
            self.store.set_question_draft("R1", self.path.parent / "missing.md")
        self.deadline()
        self.assertEqual(self.store.claim_pending_questions(), [])
        with self.assertRaises(ValueError):
            self.store.set_question_draft("R1", self.artifact("question.md"))

    def test_question_bytes_and_source_are_durable_and_modified_file_not_ready(self):
        path = self.artifact("question.md")
        original = path.read_text()
        self.store.set_question_draft("R1", path)
        with sqlite3.connect(self.path) as connection:
            text, digest, source_digest, payload = connection.execute(
                "SELECT question_draft_text,question_draft_sha256,question_source_sha256,payload FROM requests WHERE id='R1'"
            ).fetchone()
        self.assertEqual(text, original)
        self.assertEqual(digest, hashlib.sha256(original.encode()).hexdigest())
        self.assertEqual(source_digest, hashlib.sha256(payload.encode()).hexdigest())
        path.write_text("Changed synthetic question")
        self.assertEqual(self.store.snapshot("R1", BEFORE)["question_draft"]["status"], "artifact_missing_or_changed")
        with self.assertRaises(ValueError):
            self.store.set_question_draft("R1", path)

    def test_question_symlink_is_not_ready_and_recovery_does_not_follow_it(self):
        path = self.artifact("question.md")
        original = path.read_text()
        self.store.set_question_draft("R1", path)
        other = self.artifact("other.md")
        path.unlink()
        path.symlink_to(other)
        self.assertEqual(self.store.snapshot("R1", BEFORE)["question_draft"]["status"], "artifact_missing_or_changed")
        recovered = self.store.prepare_question_drafts(self.path.parent / "repaired")
        self.assertEqual(len(recovered), 1)
        self.assertEqual(Path(recovered[0]["question_draft"]["path"]).read_text(), original)
        self.assertTrue(path.is_symlink())

    def test_question_publish_transaction_failure_can_recover_without_false_ready(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("""CREATE TRIGGER synthetic_fail_question BEFORE UPDATE OF question_draft_path ON requests
                                BEGIN SELECT RAISE(ABORT,'synthetic write failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.prepare_question_drafts(self.path.parent / "drafts")
        snapshot = self.store.snapshot("R1", BEFORE)
        self.assertEqual(snapshot["question_draft"]["status"], "pending")
        self.assertIsNone(snapshot["question_draft"]["path"])
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TRIGGER synthetic_fail_question")
        self.assertEqual(len(self.store.prepare_question_drafts(self.path.parent / "drafts")), 1)

    def test_concurrent_question_claims_cannot_duplicate(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: CaseStore(self.path).claim_pending_questions(), range(4)))
        self.assertEqual(sum(len(item) for item in results), 1)

    def test_unknown_request_explicit_reference_and_material_required(self):
        self.deadline()
        for request_id, offer in (("unknown", sample_offer(request_id="unknown")), ("R1", sample_offer(request_id="other")),
                                  ("R1", {**sample_offer(), "material_code": "PP"})):
            with self.subTest(request_id=request_id), self.assertRaises(ValueError):
                self.store.append_offer(request_id, offer)
        offer = sample_offer()
        offer.pop("request_id")
        with self.assertRaises(ValueError):
            self.store.append_offer("R1", offer)

    def test_name_reference_required_when_original_material_code_missing(self):
        request = sample_request("R2", "request-message-2")
        request["material_code"] = None
        self.store.upsert_request(request, "mail_request")
        self.store.set_deadline("R2", DEADLINE, "talep@example.com", now=BEFORE)
        offer = sample_offer("Q2", "offer-message-2", request_id="R2")
        with self.assertRaises(ValueError):
            self.store.append_offer("R2", offer)
        offer["material_name"] = "Polietilen"
        self.assertEqual(len(self.store.append_offer("R2", offer)["offers"]), 1)

    def test_offer_replay_idempotent_and_evidence_attachments_preserved(self):
        self.deadline()
        first = self.store.append_offer("R1", sample_offer())
        second = CaseStore(self.path).append_offer("R1", sample_offer())
        self.assertEqual(len(second["offers"]), 1)
        self.assertEqual(second["request"]["offer_revision"], 1)
        self.assertEqual(first["offers"][0]["email"]["attachments"][0]["text_excerpt"], "Grade A")
        self.assertEqual(first["offers"][0]["source_fields"]["cash_unit_price"]["message_id"], "offer-message-1")

    def test_offer_message_and_offer_id_cannot_claim_other_case(self):
        self.deadline()
        self.store.append_offer("R1", sample_offer())
        self.store.upsert_request(sample_request("R2", "request-message-2"), "mail_request")
        self.store.set_deadline("R2", DEADLINE, "talep@example.com", now=BEFORE)
        with self.assertRaises(ValueError):
            self.store.append_offer("R2", sample_offer(request_id="R2"))
        modified = sample_offer()
        modified["cash_unit_price"] = "90"
        with self.assertRaises(ValueError):
            self.store.append_offer("R1", modified)

    def test_invalid_offer_email_or_naive_date_rejected(self):
        for field, value in (("message_id", None), ("sender", "not-an-email"), ("received_at", "2026-10-13T08:00:00")):
            offer = sample_offer()
            offer["email"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.append_offer("R1", offer)

    def test_no_offer_report_after_deadline_prepared_once(self):
        self.deadline()
        self.assertEqual(self.store.list_due(BEFORE), [])
        self.assertEqual(len(self.store.list_due(DEADLINE)), 1)
        path = self.artifact()
        first = self.store.mark_report_prepared("R1", path, now=DEADLINE, expected_offer_revision=0)
        second = self.store.mark_report_prepared("R1", path, now=AFTER)
        self.assertEqual(first["request"]["state"], "report_prepared")
        self.assertEqual(second["request"]["report_revision"], 1)
        self.assertEqual(self.store.list_due(AFTER), [])
        self.assertEqual(second["offers"], [])

    def test_report_cannot_be_prepared_early_missing_or_empty(self):
        path = self.artifact()
        with self.assertRaises(ValueError):
            self.store.mark_report_prepared("R1", path, now=AFTER)
        self.deadline()
        with self.assertRaises(ValueError):
            self.store.mark_report_prepared("R1", path, now=BEFORE)
        for invalid_path in (self.path.parent / "missing.md", self.path.parent):
            with self.subTest(path=invalid_path), self.assertRaises(ValueError):
                self.store.mark_report_prepared("R1", invalid_path, now=AFTER)
        empty = self.path.parent / "empty.md"
        empty.touch()
        with self.assertRaises(ValueError):
            self.store.mark_report_prepared("R1", empty, now=AFTER)

    def test_new_worker_report_manifest_identity_revision_and_hash_verified_before_commit(self):
        self.deadline()
        folder = self.path.parent / "talep_demo_yonetici_raporu_v1_o0_test"
        folder.mkdir()
        path = folder / "report.txt"
        path.write_text("Synthetic manager draft")
        manifest = {"request_id": "R1", "offer_revision": 0, "report_revision": 1,
                    "files": [{"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]}
        manifest_path = folder / "manifest.json"
        for field, value in (("request_id", "R2"), ("report_revision", 2), ("offer_revision", 1)):
            manifest_path.write_text(json.dumps({**manifest, field: value}))
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.mark_report_prepared("R1", path, now=AFTER)
            self.assertEqual(self.store.snapshot("R1", AFTER)["request"]["report_revision"], 0)
        broken = json.loads(json.dumps(manifest))
        broken["files"][0]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(broken))
        with self.assertRaises(ValueError):
            self.store.mark_report_prepared("R1", path, now=AFTER)
        manifest_path.write_text(json.dumps(manifest))
        self.assertEqual(self.store.mark_report_prepared("R1", path, now=AFTER)["request"]["state"], "report_prepared")

    def test_report_symlink_and_deep_or_duplicate_manifest_fail_safely(self):
        self.deadline()
        folder = self.path.parent / "talep_demo_yonetici_raporu_v1_o0_test"
        folder.mkdir()
        path = folder / "report.txt"
        path.write_text("Synthetic manager draft")
        manifest_path = folder / "manifest.json"
        for text in ('{"request_id":"R2","request_id":"R1"}', "[" * 1500 + "]" * 1500):
            manifest_path.write_text(text)
            with self.assertRaises(ValueError):
                self.store.mark_report_prepared("R1", path, now=AFTER)
        outside = self.artifact("outside.md")
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.store.mark_report_prepared("R1", path, now=AFTER)

    def test_late_offer_creates_report_revision_and_duplicate_is_not_revision(self):
        self.deadline()
        self.store.mark_report_prepared("R1", self.artifact(), now=DEADLINE)
        self.store.append_offer("R1", sample_offer())
        self.assertEqual(self.store.snapshot("R1", AFTER)["request"]["state"], "ready_for_report")
        self.assertEqual(len(self.store.list_due(AFTER)), 1)
        self.store.mark_report_prepared("R1", self.artifact("report-v2.md"), now=AFTER, expected_offer_revision=1)
        self.store.append_offer("R1", sample_offer())
        result = self.store.snapshot("R1", AFTER)
        self.assertEqual(result["request"]["state"], "report_prepared")
        self.assertEqual(result["request"]["report_revision"], 2)

    def test_new_offer_between_snapshot_and_report_does_not_mark_stale_report(self):
        self.deadline()
        snapshot = self.store.snapshot("R1", AFTER)
        self.store.append_offer("R1", sample_offer())
        with self.assertRaises(ValueError):
            self.store.mark_report_prepared("R1", self.artifact(), now=AFTER,
                                            expected_offer_revision=snapshot["request"]["offer_revision"])

    def test_missing_prepared_artifact_becomes_due_again(self):
        self.deadline()
        path = self.artifact()
        self.store.mark_report_prepared("R1", path, now=AFTER)
        path.unlink()
        self.assertEqual(len(self.store.list_due(AFTER)), 1)
        replacement = self.store.mark_report_prepared("R1", self.artifact("replacement.md"), now=AFTER)
        self.assertEqual(replacement["request"]["report_revision"], 2)

    def test_credentials_never_persisted_even_nested(self):
        request = sample_request("R2", "request-message-2")
        request["required_specs"]["api_key"] = "synthetic-never-store-key"
        with self.assertRaises(ValueError):
            self.store.upsert_request(request, "mail_request")
        offer = sample_offer()
        offer["email"]["headers"] = {"Authorization": "synthetic-never-store-key"}
        with self.assertRaises(ValueError):
            self.store.append_offer("R1", offer)
        self.assertNotIn(b"synthetic-never-store-key", self.path.read_bytes())

    def test_unknown_safe_fields_not_saved_and_nonfinite_json_rejected(self):
        self.deadline()
        offer = sample_offer()
        offer["unused_configuration"] = {"endpoint": "https://example.com"}
        result = self.store.append_offer("R1", offer)
        self.assertNotIn("unused_configuration", result["offers"][0])
        request = sample_request("R2", "request-message-2")
        request["required_specs"] = {"density": float("nan")}
        with self.assertRaises(ValueError):
            self.store.upsert_request(request, "mail_request")
        json.dumps(result, allow_nan=False)

    def test_attached_offer_is_preserved_without_starting_before_deadline_reply(self):
        self.store.append_offer("R1", sample_offer())
        pending = self.store.snapshot("R1", BEFORE)
        self.assertEqual(len(pending["offers"]), 1)
        self.assertEqual(pending["request"]["state"], "awaiting_deadline")
        self.assertEqual(self.store.list_due(AFTER), [])
        with self.assertRaises(ValueError):
            self.store.mark_report_prepared("R1", self.artifact(), now=AFTER)
        self.deadline()
        self.assertEqual(len(self.store.snapshot("R1", BEFORE)["offers"]), 1)

    def test_concurrent_offer_replays_preserve_one_revision(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: CaseStore(self.path).append_offer("R1", sample_offer()), range(4)))
        self.assertEqual([len(result["offers"]) for result in results], [1, 1, 1, 1])
        self.assertEqual(self.store.snapshot("R1", BEFORE)["request"]["offer_revision"], 1)


if __name__ == "__main__":
    unittest.main()
