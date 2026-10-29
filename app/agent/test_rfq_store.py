"""RFQ persistence tests use synthetic cases and temporary isolated queues only."""

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from case_store import CaseStore
from rfq_store import RFQStore

BEFORE = "2026-10-20T09:00:00+03:00"
SENT = "2026-10-20T09:01:00+03:00"
RECEIVED = "2026-10-20T09:02:00+03:00"
DEADLINE = "2026-10-20T10:00:00+03:00"
AFTER = "2026-10-20T10:01:00+03:00"


def sample_request():
    return {"id": "R1", "material_code": "PE", "material_name": "Polietilen", "quantity": "10", "unit": "KG",
            "required_specs": {"grade": "A"}, "requester": "owner@example.com", "mailbox": "bot@example.com",
            "message_id": "request-1"}


def suppliers():
    return [{"supplier_id": "S1", "name": "Firma A", "email": "sales-a@example.com", "kind": "existing",
             "material_codes": ["PE"], "contact_confirmed": True},
            {"supplier_id": "S2", "name": "Firma B", "email": "sales-b@example.com", "kind": "new",
             "material_codes": ["PE"], "contact_confirmed": True, "source_url": "https://example.com/contact"}]


def evidence(recipient="sales-a@example.com", message_id="out-1"):
    return {"mailbox": "bot@example.com", "message_id": message_id, "conversation_id": "thread-1",
            "recipient": recipient, "dispatched_at": SENT}


def response():
    return {"request_id": "R1", "supplier_id": "S1", "conversation_id": "thread-1", "mailbox": "bot@example.com",
            "offer": {"id": "Q1", "request_id": "R1", "supplier": "Firma A", "material_code": "PE",
                      "quantity": "10", "unit": "KG", "currency": "TRY", "cash_unit_price": "100",
                      "email": {"message_id": "in-1", "mailbox": "bot@example.com", "sender": "sales-a@example.com",
                                "received_at": RECEIVED, "subject": "Yanıt", "body_excerpt": "Peşin birim fiyat 100 TRY / KG"},
                      "source_fields": {"cash_unit_price": {"message_id": "in-1", "quote": "Peşin birim fiyat 100 TRY / KG"}}}}


class RFQStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.path = self.folder / "requests.db"
        self.cases = CaseStore(self.path)
        self.cases.upsert_request(sample_request(), "mail_request")
        self.cases.set_deadline("R1", DEADLINE, "owner@example.com", now=BEFORE)
        self.store = RFQStore(self.path, self.folder / "drafts")

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self):
        result = self.store.prepare("R1", suppliers(), now=BEFORE)
        self.assertEqual(len(result["drafts"]), 2)
        return next(item for item in result["drafts"] if item["supplier_id"] == "S1")

    def dispatch(self):
        draft = self.prepare()
        self.store.record_dispatch(draft["rfq_id"], evidence(), now=SENT)
        return draft

    def test_missing_status_and_constructor_never_create_queue(self):
        path = self.folder / "missing" / "requests.db"
        store = RFQStore(path, self.folder / "unused")
        self.assertEqual(store.status()["queue_status"], "not_created")
        self.assertFalse(path.parent.exists())
        with self.assertRaises(ValueError):
            store.prepare("R1", suppliers(), now=BEFORE)
        self.assertFalse(path.exists())

    def test_existing_status_does_not_create_rfq_tables(self):
        self.assertEqual(self.store.status()["total"], 0)
        with sqlite3.connect(self.path) as connection:
            names = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        self.assertNotIn(("rfq_drafts",), names)

    def test_readonly_snapshot_matches_cases_without_schema_creation(self):
        self.assertEqual(self.store.snapshot("R1", BEFORE), self.cases.snapshot("R1", BEFORE))
        absent = self.folder / "absent" / "requests.db"
        with self.assertRaises(ValueError):
            RFQStore(absent).snapshot("R1", BEFORE)
        self.assertFalse(absent.parent.exists())

    def test_expected_scope_or_deadline_mismatch_creates_no_artifacts(self):
        for options in ({"expected_scope_hash": "altered"},
                        {"expected_deadline_at": "2026-10-20T11:00:00+03:00"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.store.prepare("R1", suppliers(), now=BEFORE, **options)
        self.assertFalse((self.folder / "drafts").exists())

    def test_shared_scope_immutable_drafts_real_files_and_no_send(self):
        result = self.store.prepare("R1", suppliers(), now=BEFORE)
        drafts = result["drafts"]
        contents = [json.loads(Path(item["metadata_path"]).read_text()) for item in drafts]
        self.assertEqual(contents[0]["scope_hash"], contents[1]["scope_hash"])
        self.assertEqual({item["kind"] for item in drafts}, {"existing", "new"})
        self.assertFalse(result["emails_sent"])
        for item in drafts:
            self.assertTrue(Path(item["path"]).is_file())
            self.assertTrue(Path(item["html_path"]).is_file())
            self.assertEqual(item["artifact_status"], "verified")
            self.assertFalse(item["network_send_verified"])
        replay = self.store.prepare("R1", suppliers(), now=SENT)
        self.assertEqual([item["path"] for item in drafts], [item["path"] for item in replay["drafts"]])

    def test_concurrent_prepare_has_one_record_and_artifact_per_rfq(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: RFQStore(self.path, self.folder / "drafts").prepare("R1", suppliers(), now=BEFORE), range(4)))
        self.assertEqual(self.store.status(now=BEFORE)["total"], 2)
        self.assertEqual(len({item["drafts"][0]["path"] for item in results}), 1)
        self.assertEqual(len(list((self.folder / "drafts").iterdir())), 2)

    def test_lost_or_modified_artifact_repaired_to_unique_path(self):
        draft = self.prepare()
        Path(draft["html_path"]).unlink()
        current = self.store.status(now=BEFORE)["drafts"]
        broken = next(item for item in current if item["rfq_id"] == draft["rfq_id"])
        self.assertEqual(broken["artifact_status"], "artifact_missing_or_changed")
        repaired = self.prepare()
        self.assertNotEqual(repaired["path"], draft["path"])
        self.assertTrue(Path(draft["path"]).is_file())
        self.assertEqual(repaired["artifact_status"], "verified")

    def test_unsent_draft_never_claimed_overdue_or_waiting(self):
        self.prepare()
        self.assertEqual(self.store.status(now=AFTER)["counts"], {"draft_prepared": 2})

    def test_no_draft_without_deadline_or_after_deadline(self):
        for now in (DEADLINE, AFTER):
            with self.subTest(now=now), self.assertRaises(ValueError):
                self.store.prepare("R1", suppliers(), now=now)
        self.assertFalse((self.folder / "drafts").exists())

    def test_dispatch_evidence_waiting_and_elapsed_only_after_record(self):
        draft = self.prepare()
        recorded = self.store.record_dispatch(draft["rfq_id"], evidence(), now=SENT)
        self.assertEqual(recorded["state"], "awaiting_reply")
        self.assertFalse(recorded["network_send_verified"])
        self.assertFalse(recorded["sent_by_this_worker"])
        replay = self.store.record_dispatch(draft["rfq_id"], evidence(), now=AFTER)
        self.assertEqual(replay["state"], "deadline_elapsed_no_reply")

    def test_dispatch_rejects_wrong_recipient_mailbox_and_invalid_times(self):
        draft = self.prepare()
        for key, value in (("recipient", "other@example.com"), ("mailbox", "personal@example.com"),
                           ("dispatched_at", "2026-10-20T08:59:00+03:00"), ("dispatched_at", DEADLINE),
                           ("dispatched_at", "2026-10-20T09:02:00+03:00"), ("dispatched_at", "2026-10-20T09:01:00")):
            item = {**evidence(), key: value}
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.store.record_dispatch(draft["rfq_id"], item, now=SENT)

    def test_changed_deadline_invalidates_dispatch_of_old_draft(self):
        draft = self.prepare()
        self.cases.set_deadline("R1", "2026-10-20T11:00:00+03:00", "owner@example.com", now=SENT)
        with self.assertRaises(ValueError):
            self.store.record_dispatch(draft["rfq_id"], evidence(), now=SENT)

    def test_dispatch_identity_cannot_be_reused_for_other_supplier(self):
        drafts = self.store.prepare("R1", suppliers(), now=BEFORE)["drafts"]
        first = next(item for item in drafts if item["supplier_id"] == "S1")
        second = next(item for item in drafts if item["supplier_id"] == "S2")
        self.store.record_dispatch(first["rfq_id"], evidence(), now=SENT)
        with self.assertRaises(ValueError):
            self.store.record_dispatch(second["rfq_id"], evidence("sales-b@example.com"), now=SENT)

    def second_request(self):
        request = sample_request()
        request.update(id="R2", message_id="request-2")
        self.cases.upsert_request(request, "mail_request")
        self.cases.set_deadline("R2", DEADLINE, "owner@example.com", now=BEFORE)
        return next(item for item in self.store.prepare("R2", suppliers(), now=BEFORE)["drafts"] if item["supplier_id"] == "S1")

    def test_same_mailbox_conversation_cannot_dispatch_for_two_requests(self):
        self.dispatch()
        second = self.second_request()
        item = {**evidence(), "message_id": "out-2"}
        with self.assertRaises(ValueError):
            self.store.record_dispatch(second["rfq_id"], item, now=SENT)

    def test_legacy_ambiguous_conversation_rejects_response_without_offer_insert(self):
        first = self.dispatch()
        second = self.second_request()
        item = {**evidence(), "message_id": "out-2", "conversation_id": "thread-2"}
        self.store.record_dispatch(second["rfq_id"], item, now=SENT)
        item["conversation_id"] = "thread-1"
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE rfq_drafts SET dispatch=? WHERE id=?", (json.dumps(item), second["rfq_id"]))
        for draft in (first, second):
            record = response()
            if draft == second:
                record["request_id"] = "R2"
                record["offer"]["request_id"] = "R2"
            with self.subTest(rfq_id=draft["rfq_id"]), self.assertRaises(ValueError):
                self.store.ingest_response(draft["rfq_id"], record, now=RECEIVED)
        for request_id in ("R1", "R2"):
            self.assertEqual(self.cases.snapshot(request_id, AFTER)["offers"], [])

    def test_source_rfq_reference_mismatch_or_multiple_rejected_without_insert(self):
        draft = self.dispatch()
        wrong = "RFQ-" + "0" * 24
        for field, text in (("subject", wrong), ("body_excerpt", wrong),
                            ("body_excerpt", draft["rfq_id"] + " " + wrong), ("attachment", wrong)):
            record = response()
            if field == "attachment":
                record["offer"]["email"]["attachments"] = [{"attachment_id": "A1", "text_excerpt": text}]
            else:
                record["offer"]["email"][field] = text
            with self.subTest(field=field, text=text), self.assertRaises(ValueError):
                self.store.ingest_response(draft["rfq_id"], record, now=RECEIVED)
        self.assertEqual(self.cases.snapshot("R1", AFTER)["offers"], [])
        record = response()
        record["offer"]["email"]["subject"] = "Yanıt " + draft["rfq_id"]
        self.assertEqual(self.store.ingest_response(draft["rfq_id"], record, now=RECEIVED)["responses"], 1)

    def test_response_bound_atomically_to_offer_and_idempotent_revision(self):
        draft = self.dispatch()
        item = self.store.ingest_response(draft["rfq_id"], response(), now=RECEIVED)
        self.assertEqual(item["state"], "response_received")
        self.assertEqual(item["responses"], 1)
        self.assertEqual(self.cases.snapshot("R1", AFTER)["request"]["offer_revision"], 1)
        self.store.ingest_response(draft["rfq_id"], response(), now=AFTER)
        self.assertEqual(self.cases.snapshot("R1", AFTER)["request"]["offer_revision"], 1)
        self.assertEqual(len(self.cases.snapshot("R1", AFTER)["offers"]), 1)

    def test_response_wrong_thread_mail_sender_material_scope_or_supplier_rejected(self):
        draft = self.dispatch()
        mutations = [("conversation_id", "other"), ("mailbox", "personal@example.com"), ("request_id", "R2"),
                     ("supplier_id", "S2"), ("offer.material_code", "PP"), ("offer.quantity", "11"),
                     ("offer.unit", "G"), ("offer.email.sender", "attacker@example.com"),
                     ("offer.email.mailbox", "personal@example.com"), ("offer.request_id", "R2"),
                     ("offer.supplier", "Firma X"), ("offer.email.message_id", "out-1"),
                     ("offer.email.received_at", BEFORE)]
        for dotted, value in mutations:
            item = response()
            current = item
            parts = dotted.split(".")
            for key in parts[:-1]:
                current = current[key]
            current[parts[-1]] = value
            with self.subTest(field=dotted), self.assertRaises(ValueError):
                self.store.ingest_response(draft["rfq_id"], item, now=RECEIVED)
        self.assertEqual(self.cases.snapshot("R1", AFTER)["request"]["offer_revision"], 0)

    def test_reply_without_dispatch_rejected(self):
        draft = self.prepare()
        with self.assertRaises(ValueError):
            self.store.ingest_response(draft["rfq_id"], response(), now=RECEIVED)

    def test_conflicting_source_replay_rejected(self):
        draft = self.dispatch()
        self.store.ingest_response(draft["rfq_id"], response(), now=RECEIVED)
        changed = response()
        changed["offer"]["cash_unit_price"] = "101"
        with self.assertRaises(ValueError):
            self.store.ingest_response(draft["rfq_id"], changed, now=RECEIVED)
        self.assertEqual(self.cases.snapshot("R1", AFTER)["offers"][0]["cash_unit_price"], "100")

    def test_prior_existing_identical_offer_can_be_bound_without_revision_increment(self):
        # Legacy source imports remain valid only before an RFQ is registered.
        self.cases.append_offer("R1", response()["offer"])
        draft = self.dispatch()
        self.store.ingest_response(draft["rfq_id"], response(), now=RECEIVED)
        self.assertEqual(self.cases.snapshot("R1", AFTER)["request"]["offer_revision"], 1)

    def test_new_legacy_offer_cannot_bypass_rfq_source_binding(self):
        draft = self.prepare()
        with self.assertRaisesRegex(ValueError, "ingest-response"):
            self.cases.append_offer("R1", response()["offer"])
        self.assertEqual(self.cases.snapshot("R1", RECEIVED)["offers"], [])
        self.store.record_dispatch(draft["rfq_id"], evidence(), now=SENT)
        with self.assertRaisesRegex(ValueError, "ingest-response"):
            self.cases.append_offer("R1", response()["offer"])
        self.assertEqual(self.cases.snapshot("R1", RECEIVED)["request"]["offer_revision"], 0)
        bound = self.store.ingest_response(draft["rfq_id"], response(), now=RECEIVED)
        self.assertEqual(bound["responses"], 1)
        self.assertEqual(self.cases.snapshot("R1", RECEIVED)["request"]["offer_revision"], 1)

    def test_identical_pre_rfq_offer_replay_remains_idempotent(self):
        first = self.cases.append_offer("R1", response()["offer"])
        self.prepare()
        replay = self.cases.append_offer("R1", response()["offer"])
        self.assertEqual(first["offers"], replay["offers"])
        self.assertEqual(replay["request"]["offer_revision"], 1)

    def test_rfq_guard_applies_only_to_its_own_request(self):
        self.prepare()
        second = {**sample_request(), "id": "R2", "message_id": "request-2"}
        self.cases.upsert_request(second, "mail_request")
        offer = response()["offer"]
        offer["request_id"] = "R2"
        result = self.cases.append_offer("R2", offer)
        self.assertEqual(result["request"]["offer_revision"], 1)
        self.assertEqual(len(result["offers"]), 1)
        self.assertEqual(self.cases.snapshot("R1", RECEIVED)["offers"], [])

    def test_concurrent_same_response_cannot_duplicate_offer(self):
        draft = self.dispatch()
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: RFQStore(self.path).ingest_response(draft["rfq_id"], response(), now=RECEIVED), range(3)))
        self.assertEqual([item["responses"] for item in results], [1, 1, 1])
        self.assertEqual(self.cases.snapshot("R1", AFTER)["request"]["offer_revision"], 1)

    def test_transaction_rollback_does_not_publish_false_response(self):
        draft = self.dispatch()
        with sqlite3.connect(self.path) as connection:
            connection.execute("""CREATE TRIGGER reject_binding BEFORE INSERT ON rfq_responses
                                  BEGIN SELECT RAISE(ABORT,'synthetic binding failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.ingest_response(draft["rfq_id"], response(), now=RECEIVED)
        self.assertEqual(self.cases.snapshot("R1", AFTER)["request"]["offer_revision"], 0)
        self.assertEqual(self.cases.snapshot("R1", AFTER)["offers"], [])
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TRIGGER reject_binding")
        self.assertEqual(self.store.ingest_response(draft["rfq_id"], response(), now=RECEIVED)["responses"], 1)

    def test_late_response_archived_explicitly(self):
        draft = self.dispatch()
        item = response()
        item["offer"]["email"]["received_at"] = AFTER
        result = self.store.ingest_response(draft["rfq_id"], item, now=AFTER)
        self.assertEqual(result["late_responses"], 1)
        self.assertEqual(result["state"], "response_received")

    def test_status_never_exposes_source_email_body(self):
        draft = self.dispatch()
        self.store.ingest_response(draft["rfq_id"], response(), now=RECEIVED)
        serialized = json.dumps(self.store.status(now=AFTER))
        self.assertNotIn("body_excerpt", serialized)
        self.assertNotIn("Peşin birim fiyat", serialized)


if __name__ == "__main__":
    unittest.main()
