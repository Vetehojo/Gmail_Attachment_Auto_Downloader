import base64
import hashlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import gmail_monitor
from job_queue import JobQueue


class Execute:
    def __init__(self, value):
        self.value = value

    def execute(self, num_retries=None):
        return self.value


class AttachmentApi:
    def __init__(self, data):
        self.data = data
        self.calls = 0

    def get(self, **kwargs):
        self.calls += 1
        encoded = base64.urlsafe_b64encode(self.data).decode("ascii")
        return Execute({"data": encoded})


class MessagesApi:
    def __init__(self, data, message_payload=None):
        self._attachments = AttachmentApi(data)
        self.message_payload = message_payload
        self.get_calls = 0

    def attachments(self):
        return self._attachments

    def get(self, **kwargs):
        self.get_calls += 1
        return Execute({"payload": self.message_payload or {}})


class UsersApi:
    def __init__(self, data, message_payload=None):
        self._messages = MessagesApi(data, message_payload)

    def messages(self):
        return self._messages


class FakeService:
    def __init__(self, data, message_payload=None):
        self._users = UsersApi(data, message_payload)

    def users(self):
        return self._users


class ByIdService:
    """users().messages().attachments().get(id=...) returns the bytes mapped to that id."""

    def __init__(self, data_by_id):
        self.data_by_id = data_by_id

    def users(self):
        return self

    def messages(self):
        return self

    def attachments(self):
        return self

    def get(self, **kwargs):
        encoded = base64.urlsafe_b64encode(self.data_by_id[kwargs["id"]]).decode("ascii")
        return Execute({"data": encoded})


class StaticPool:
    def __init__(self, service):
        self.service = service

    def get(self, account_email):
        return self.service

    def invalidate(self, account_email):
        pass


class FailingAuthPool:
    def __init__(self):
        self.invalidated = []

    def get(self, account_email):
        raise gmail_monitor.AuthenticationRequiredError("Gmail re-authentication is required.")

    def invalidate(self, account_email):
        self.invalidated.append(account_email)


class CrashIdempotencyTest(unittest.TestCase):
    def payload(self, final_dir, message_id="m1"):
        return {
            "account_email": "a@example.com",
            "final_dir": final_dir,
            "message_id": message_id,
            "attachment_id": "att1",
            "part_id": "",
            "inline": False,
            "filename": "document.pdf",
            "received_date": "20260825",
            "sender_email": "sender@example.com",
            "mail_subject": "subject",
        }

    def test_same_job_retry_reuses_target_but_new_message_gets_number(self):
        data = b"same-pdf-content"
        service = FakeService(data)
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "state")
            final = os.path.join(td, "final")
            os.makedirs(state)
            with mock.patch.object(gmail_monitor, "STATE_DIR", state):
                first = gmail_monitor.run_attachment_job(1, service, self.payload(final))
                marker_path = os.path.join(state, "attachmentjob_1.json")
                with open(marker_path, "w", encoding="utf-8") as handle:
                    json.dump({"success": False, "phase": "prepared", "result": first}, handle)

                retry = gmail_monitor.run_attachment_job(1, service, self.payload(final))
                self.assertEqual(first["target_path"], retry["target_path"])
                self.assertEqual(1, len([name for name in os.listdir(final) if name.endswith(".pdf")]))

                second = gmail_monitor.run_attachment_job(2, service, self.payload(final, "m2"))
                self.assertNotEqual(first["target_path"], second["target_path"])
                self.assertTrue(os.path.basename(second["target_path"]).endswith("(1).pdf"))
                self.assertEqual(2, len([name for name in os.listdir(final) if name.endswith(".pdf")]))

    def test_inline_attachment_is_saved_by_refetching_the_message_and_walking_parts(self):
        # C5: the payload never carries attachment bytes. For an inline part
        # the worker must re-fetch the message and locate the part by id,
        # never call attachments().get() (that path is for attachment_id
        # attachments only), and never touch the AttachmentApi at all.
        data = b"inline-part-bytes"
        encoded = base64.urlsafe_b64encode(data).decode("ascii")
        message_payload = {
            "parts": [
                {"partId": "0", "filename": "", "body": {"size": 1}},
                {
                    "partId": "1.1",
                    "filename": "memo.txt",
                    "body": {"data": encoded, "size": len(data)},
                },
            ]
        }
        service = FakeService(b"should-not-be-used", message_payload=message_payload)
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "state")
            final = os.path.join(td, "final")
            os.makedirs(state)
            payload = self.payload(final)
            payload.update({
                "attachment_id": "",
                "part_id": "1.1",
                "inline": True,
                "filename": "memo.txt",
            })
            with mock.patch.object(gmail_monitor, "STATE_DIR", state):
                result = gmail_monitor.run_attachment_job(7, service, payload)

            self.assertEqual(hashlib.sha256(data).hexdigest(), result["hash"])
            with open(result["target_path"], "rb") as handle:
                self.assertEqual(data, handle.read())
            self.assertEqual(0, service.users().messages().attachments().calls)
            self.assertEqual(1, service.users().messages().get_calls)

    def test_inline_attachment_raises_a_clear_error_when_the_part_is_gone(self):
        service = FakeService(b"unused", message_payload={"parts": []})
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "state")
            final = os.path.join(td, "final")
            os.makedirs(state)
            payload = self.payload(final)
            payload.update({"attachment_id": "", "part_id": "1.1", "inline": True, "filename": "memo.txt"})
            with mock.patch.object(gmail_monitor, "STATE_DIR", state):
                with self.assertRaises(RuntimeError):
                    gmail_monitor.run_attachment_job(8, service, payload)

    def test_filename_budget_from_final_dir_is_applied_to_the_saved_name(self):
        # C1: run_attachment_job must pass filename_budget(final_dir) into
        # render_filename so a deep save folder cannot push the final path
        # past MAX_PATH. filename_budget itself belongs to filename_rules;
        # here we only assert gmail_monitor wires it through correctly.
        data = b"x"
        service = FakeService(data)
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "state")
            final = os.path.join(td, "final")
            os.makedirs(state)
            os.makedirs(final)
            payload = self.payload(final)
            payload["filename"] = ("a" * 300) + ".pdf"
            with mock.patch.object(gmail_monitor, "STATE_DIR", state), \
                 mock.patch.object(gmail_monitor, "filename_budget", return_value=20) as budget:
                result = gmail_monitor.run_attachment_job(9, service, payload)

            budget.assert_called_once_with(os.path.abspath(final))
            self.assertLessEqual(len(result["saved_filename"]), 20)

    def test_temp_name_length_is_independent_of_the_final_filename_length(self):
        # C2: the temp name must be a short fixed form next to the final
        # file, not derived from it -- otherwise a final path that fits
        # MAX_PATH can still fail at the temp-write step.
        with tempfile.TemporaryDirectory() as td:
            short_target = os.path.join(td, "a.pdf")
            long_target = os.path.join(td, ("b" * 60) + ".pdf")
            real_open = open
            opened = []

            def spy_open(path, *args, **kwargs):
                opened.append(path)
                return real_open(path, *args, **kwargs)

            with mock.patch("builtins.open", side_effect=spy_open):
                gmail_monitor._write_bytes_atomic(b"1", short_target, 42)
                gmail_monitor._write_bytes_atomic(b"2", long_target, 42)

            temp_names = [
                os.path.basename(p) for p in opened
                if os.path.basename(p).startswith(gmail_monitor.TEMP_PREFIX)
            ]
            self.assertEqual(2, len(temp_names))
            self.assertEqual(temp_names[0], temp_names[1])
            self.assertEqual(gmail_monitor.temp_file_name(42), temp_names[0])
            self.assertTrue(os.path.isfile(short_target))
            self.assertTrue(os.path.isfile(long_target))

    def test_final_file_before_success_marker_is_reconciled(self):
        data = b"committed-before-marker"
        expected = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "state")
            final = os.path.join(td, "final")
            os.makedirs(state)
            os.makedirs(final)
            queue = JobQueue(os.path.join(state, "jobs.sqlite3"))
            payload = self.payload(final)
            queue.enqueue("attachment:a:m1:x", payload)
            job = queue.claim_next()

            target = os.path.join(final, "document.pdf")
            with open(target, "wb") as handle:
                handle.write(data)
            marker = {
                "success": False,
                "phase": "prepared",
                "result": {
                    "hash": expected,
                    "filename": "document.pdf",
                    "saved_filename": "document.pdf",
                    "target_path": target,
                    "account_email": "a@example.com",
                    "final_dir": final,
                },
            }
            with open(os.path.join(state, f"attachmentjob_{job['id']}.json"), "w", encoding="utf-8") as handle:
                json.dump(marker, handle)

            with mock.patch.object(gmail_monitor, "STATE_DIR", state):
                self.assertEqual(1, gmail_monitor.reconcile_completed_jobs(queue))
            self.assertEqual(1, queue.counts()["success"])
            self.assertEqual(0, queue.recover_processing_jobs())
            self.assertEqual(["document.pdf"], os.listdir(final))

    def test_reconcile_does_not_resurrect_failed_ignored_job(self):
        data = b"already-there"
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "state")
            final = os.path.join(td, "final")
            os.makedirs(state)
            os.makedirs(final)
            queue = JobQueue(os.path.join(state, "jobs.sqlite3"))
            payload = self.payload(final)
            queue.enqueue("attachment:a:m1:x", payload, max_attempts=1)
            job = queue.claim_next()
            self.assertEqual("failed", queue.mark_failure(job["id"], "broken"))
            queue.ignore_job(job["id"])

            target = os.path.join(final, "document.pdf")
            with open(target, "wb") as handle:
                handle.write(data)
            with open(os.path.join(state, f"attachmentjob_{job['id']}.json"), "w", encoding="utf-8") as handle:
                json.dump({"success": True, "result": {"target_path": target, "hash": digest}}, handle)

            with mock.patch.object(gmail_monitor, "STATE_DIR", state):
                self.assertEqual(0, gmail_monitor.reconcile_completed_jobs(queue))
            current = queue.get_job(job["id"])
            self.assertEqual("failed", current["status"])
            self.assertTrue(current["ignored"])

    def test_unreadable_saved_file_does_not_stop_startup_reconciliation(self):
        # K3 / probe startup_reconciliation_read_error: hashing one job's saved
        # file fails. That journal is kept and its job stays for the normal
        # retry, the other job is still reconciled, and startup continues.
        data = b"saved-before-marker"
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "state")
            final = os.path.join(td, "final")
            os.makedirs(state)
            os.makedirs(final)
            queue = JobQueue(os.path.join(state, "jobs.sqlite3"))
            queue.enqueue("attachment:a:m1:x", self.payload(final, "m1"))
            queue.enqueue("attachment:a:m2:x", self.payload(final, "m2"))
            locked_job = queue.claim_next()
            done_job = queue.claim_next()
            locked = os.path.join(final, "locked.pdf")
            done = os.path.join(final, "done.pdf")
            for path in (locked, done):
                with open(path, "wb") as handle:
                    handle.write(data)
            locked_journal = self.write_journal(state, locked_job["id"], locked, data)
            done_journal = self.write_journal(state, done_job["id"], done, data)
            real_sha256 = gmail_monitor._sha256_file

            def read_denied(path):
                if os.path.abspath(path) == os.path.abspath(locked):
                    raise PermissionError(13, "simulated read denial", path)
                return real_sha256(path)

            logged = []
            with mock.patch.object(gmail_monitor, "STATE_DIR", state), \
                 mock.patch.object(gmail_monitor, "_sha256_file", side_effect=read_denied), \
                 mock.patch.object(gmail_monitor, "log",
                                   lambda message, procedure="monitor": logged.append(message)):
                gmail_monitor.recover_interrupted_jobs(queue)

            self.assertEqual("success", queue.get_job(done_job["id"])["status"])
            self.assertFalse(os.path.exists(done_journal))
            retry = queue.get_job(locked_job["id"])
            self.assertEqual("pending", retry["status"])
            self.assertEqual(0, retry["attempts"])
            self.assertTrue(os.path.exists(locked_journal))
            self.assertTrue(any(
                f"job {locked_job['id']}" in line and "simulated read denial" in line for line in logged
            ))

    def test_authentication_outage_defers_job_without_consuming_attempts(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "state")
            final = os.path.join(td, "final")
            os.makedirs(state)
            queue = JobQueue(os.path.join(state, "jobs.sqlite3"))
            queue.enqueue("attachment:a:m1:x", self.payload(final))
            pool = FailingAuthPool()

            with mock.patch.object(gmail_monitor, "STATE_DIR", state), \
                 mock.patch.object(gmail_monitor, "write_heartbeat", lambda *a, **k: None), \
                 mock.patch.object(gmail_monitor, "log", lambda *a, **k: None):
                self.assertTrue(gmail_monitor.process_one_job(queue, pool))

            deferred = queue.get_job(1)
            self.assertEqual("pending", deferred["status"])
            self.assertEqual(0, deferred["attempts"])
            self.assertGreater(deferred["next_attempt_at"], time.time())
            self.assertEqual(["a@example.com"], pool.invalidated)
            self.assertEqual([], queue.list_failed(include_ignored=True))
            # A deferred job must not leave a customer-facing error notice.
            self.assertFalse(os.path.isdir(final) and os.listdir(final))

    def write_journal(self, state, job_id, target, data, phase="prepared", success=False):
        marker = {
            "success": success,
            "phase": phase,
            "result": {
                "hash": hashlib.sha256(data).hexdigest(),
                "filename": "document.pdf",
                "saved_filename": os.path.basename(target),
                "target_path": target,
                "account_email": "a@example.com",
                "final_dir": os.path.dirname(target),
            },
        }
        if phase is None:
            del marker["phase"]
        path = os.path.join(state, f"attachmentjob_{job_id}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(marker, handle)
        return path

    def test_recovered_prepared_job_relocates_when_a_fresh_job_took_its_name(self):
        # F3: job A is killed after its "prepared" journal pinned name(1).pdf.
        # After restart A is pending with next_attempt_at=now, so a fresh
        # same-name job B (next_attempt_at=0) is claimed first and saves
        # name(1).pdf. A must move to name(2).pdf -- never name(1)(1).pdf --
        # and succeed instead of failing on every retry.
        data = {"attC": b"earlier-file", "attA": b"recovered-job", "attB": b"fresh-job"}
        pool = StaticPool(ByIdService(data))
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "state")
            final = os.path.join(td, "final")
            os.makedirs(state)
            db_path = os.path.join(state, "jobs.sqlite3")
            queue = JobQueue(db_path)

            def job_payload(message_id, attachment_id):
                payload = self.payload(final, message_id)
                payload["attachment_id"] = attachment_id
                return payload

            with mock.patch.object(gmail_monitor, "STATE_DIR", state), \
                 mock.patch.object(gmail_monitor, "write_heartbeat", lambda *a, **k: None), \
                 mock.patch.object(gmail_monitor, "log", lambda *a, **k: None):
                queue.enqueue("attachment:a:mC:x", job_payload("mC", "attC"))
                self.assertTrue(gmail_monitor.process_one_job(queue, pool))
                [base_name] = os.listdir(final)
                stem, ext = os.path.splitext(base_name)

                queue.enqueue("attachment:a:mA:x", job_payload("mA", "attA"))
                job_a = queue.claim_next()
                with mock.patch.object(gmail_monitor, "_write_bytes_atomic", side_effect=OSError("killed")):
                    with self.assertRaises(OSError):
                        gmail_monitor.run_attachment_job(job_a["id"], pool.service, job_a["payload"])
                with open(os.path.join(state, f"attachmentjob_{job_a['id']}.json"), encoding="utf-8") as handle:
                    journal = json.load(handle)
                self.assertEqual("prepared", journal["phase"])
                self.assertEqual(f"{stem}(1){ext}", journal["result"]["saved_filename"])
                self.assertEqual(1, queue.recover_processing_jobs())

                queue.enqueue("attachment:a:mB:x", job_payload("mB", "attB"))
                self.assertTrue(gmail_monitor.process_one_job(queue, pool))
                # The fixture relies on B being claimed before the recovered A.
                self.assertEqual("pending", queue.get_job(job_a["id"])["status"])
                self.assertTrue(gmail_monitor.process_one_job(queue, pool))

            self.assertEqual({"success": 3}, {k: v for k, v in queue.counts().items() if v})
            expected = {
                base_name: b"earlier-file",
                f"{stem}(1){ext}": b"fresh-job",
                f"{stem}(2){ext}": b"recovered-job",
            }
            self.assertEqual(sorted(expected), sorted(os.listdir(final)))
            for name, content in expected.items():
                with open(os.path.join(final, name), "rb") as handle:
                    self.assertEqual(content, handle.read())
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute("SELECT result_json FROM jobs WHERE id = ?", (job_a["id"],)).fetchone()
            finally:
                conn.close()
            result = json.loads(row[0])
            self.assertEqual(f"{stem}(2){ext}", result["saved_filename"])
            self.assertEqual(os.path.join(os.path.abspath(final), f"{stem}(2){ext}"), result["target_path"])

    def test_relocation_is_journaled_before_the_write_and_updates_saved_filename(self):
        data = b"job-bytes"
        service = FakeService(data)
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "state")
            final = os.path.join(td, "final")
            os.makedirs(state)
            os.makedirs(final)
            payload = self.payload(final)
            with mock.patch.object(gmail_monitor, "STATE_DIR", state):
                pinned = gmail_monitor._fresh_target_path(payload, os.path.abspath(final))
                with open(pinned, "wb") as handle:
                    handle.write(b"someone-else")
                marker_path = self.write_journal(state, 5, pinned, data)

                with mock.patch.object(gmail_monitor, "_write_bytes_atomic", side_effect=OSError("killed")):
                    with self.assertRaises(OSError):
                        gmail_monitor.run_attachment_job(5, service, payload)
                with open(marker_path, encoding="utf-8") as handle:
                    journal = json.load(handle)
                moved = journal["result"]["target_path"]
                self.assertEqual("prepared", journal["phase"])
                self.assertFalse(journal["success"])
                self.assertNotEqual(pinned, moved)
                self.assertEqual(os.path.dirname(pinned), os.path.dirname(moved))
                self.assertEqual(os.path.basename(moved), journal["result"]["saved_filename"])
                self.assertFalse(os.path.exists(moved))

                result = gmail_monitor.run_attachment_job(5, service, payload)

            self.assertEqual(moved, result["target_path"])
            self.assertEqual(os.path.basename(moved), result["saved_filename"])
            with open(marker_path, encoding="utf-8") as handle:
                self.assertEqual({"success": True, "phase": "committed", "result": result}, json.load(handle))
            with open(pinned, "rb") as handle:
                self.assertEqual(b"someone-else", handle.read())
            with open(moved, "rb") as handle:
                self.assertEqual(data, handle.read())

    def test_prepared_journal_with_matching_file_commits_without_writing(self):
        data = b"already-saved"
        service = FakeService(data)
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "state")
            final = os.path.join(td, "final")
            os.makedirs(state)
            os.makedirs(final)
            pinned = os.path.join(final, "document.pdf")
            with open(pinned, "wb") as handle:
                handle.write(data)
            marker_path = self.write_journal(state, 6, pinned, data)
            with mock.patch.object(gmail_monitor, "STATE_DIR", state), \
                 mock.patch.object(gmail_monitor, "_write_bytes_atomic") as write:
                result = gmail_monitor.run_attachment_job(6, service, self.payload(final))

            write.assert_not_called()
            self.assertEqual(pinned, result["target_path"])
            self.assertEqual(["document.pdf"], os.listdir(final))
            with open(marker_path, encoding="utf-8") as handle:
                self.assertEqual("committed", json.load(handle)["phase"])

    def test_committed_or_legacy_journal_with_different_content_still_raises(self):
        data = b"job-bytes"
        service = FakeService(data)
        for label, phase, success in (
            ("committed", "committed", True),
            ("legacy without phase", None, False),
            ("legacy success without phase", None, True),
        ):
            with self.subTest(label), tempfile.TemporaryDirectory() as td:
                state = os.path.join(td, "state")
                final = os.path.join(td, "final")
                os.makedirs(state)
                os.makedirs(final)
                pinned = os.path.join(final, "document.pdf")
                with open(pinned, "wb") as handle:
                    handle.write(b"someone-else")
                marker_path = self.write_journal(state, 7, pinned, data, phase=phase, success=success)
                with open(marker_path, "rb") as handle:
                    before = handle.read()

                with mock.patch.object(gmail_monitor, "STATE_DIR", state):
                    with self.assertRaises(RuntimeError):
                        gmail_monitor.run_attachment_job(7, service, self.payload(final))

                self.assertEqual(["document.pdf"], os.listdir(final))
                with open(pinned, "rb") as handle:
                    self.assertEqual(b"someone-else", handle.read())
                with open(marker_path, "rb") as handle:
                    self.assertEqual(before, handle.read())

    def test_unreadable_pinned_file_is_retried_not_relocated(self):
        # A locked file cannot be hashed; it must never be treated as "different".
        data = b"job-bytes"
        service = FakeService(data)
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "state")
            final = os.path.join(td, "final")
            os.makedirs(state)
            os.makedirs(final)
            pinned = os.path.join(final, "document.pdf")
            with open(pinned, "wb") as handle:
                handle.write(b"someone-else")
            marker_path = self.write_journal(state, 8, pinned, data)
            with open(marker_path, "rb") as handle:
                before = handle.read()
            real_sha256 = gmail_monitor._sha256_file

            def locked(path):
                if os.path.abspath(path) == os.path.abspath(pinned):
                    raise PermissionError(13, "locked", path)
                return real_sha256(path)

            with mock.patch.object(gmail_monitor, "STATE_DIR", state), \
                 mock.patch.object(gmail_monitor, "_sha256_file", side_effect=locked):
                with self.assertRaises(PermissionError):
                    gmail_monitor.run_attachment_job(8, service, self.payload(final))

            self.assertEqual(["document.pdf"], os.listdir(final))
            with open(marker_path, "rb") as handle:
                self.assertEqual(before, handle.read())

    def single_part_message(self, filename, body):
        return {
            "partId": "",
            "mimeType": "application/octet-stream",
            "filename": filename,
            "headers": [
                {"name": "Subject", "value": "single part"},
                {"name": "From", "value": "sender@example.com"},
                {"name": "Date", "value": "Tue, 25 Aug 2026 00:00:00 +0900"},
                {"name": "Content-Disposition", "value": f'attachment; filename="{filename}"'},
            ],
            "body": body,
        }

    def enqueue_and_run_single_part(self, service, allowed_extensions):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "state")
            final = os.path.join(td, "final")
            os.makedirs(state)
            queue = JobQueue(os.path.join(state, "jobs.sqlite3"))
            settings = {"auth_mode": "oauth", "target_email": "a@example.com", "final_dir": final}
            with mock.patch.object(gmail_monitor, "STATE_DIR", state), \
                 mock.patch.object(gmail_monitor, "load_settings", return_value=settings), \
                 mock.patch.object(gmail_monitor, "log", lambda *a, **k: None):
                added = gmail_monitor.enqueue_message_jobs(
                    queue, service, "m1", "a@example.com", final, allowed_extensions=allowed_extensions
                )
                job = queue.claim_next()
                result = gmail_monitor.run_attachment_job(job["id"], service, job["payload"])
            with open(result["target_path"], "rb") as handle:
                saved = handle.read()
        return added, job["payload"], saved

    def test_single_part_message_with_attachment_id_is_enqueued_and_saved(self):
        # F6: a bare-PDF mail carries filename + attachmentId on the top-level
        # payload (partId ""), not in payload["parts"].
        data = b"%PDF-single-part"
        message = self.single_part_message("scan.pdf", {"attachmentId": "att-top", "size": len(data)})
        service = FakeService(data, message_payload=message)

        added, payload, saved = self.enqueue_and_run_single_part(service, {".pdf"})

        self.assertEqual(1, added)
        self.assertEqual("att-top", payload["attachment_id"])
        self.assertEqual("", payload["part_id"])
        self.assertFalse(payload["inline"])
        self.assertEqual(data, saved)
        self.assertEqual(1, service.users().messages().attachments().calls)

    def test_single_part_message_with_inline_data_is_enqueued_and_saved(self):
        data = b"a,b\n1,2\n"
        encoded = base64.urlsafe_b64encode(data).decode("ascii")
        message = self.single_part_message("list.csv", {"data": encoded, "size": len(data)})
        service = FakeService(b"should-not-be-used", message_payload=message)

        added, payload, saved = self.enqueue_and_run_single_part(service, {".csv"})

        self.assertEqual(1, added)
        self.assertEqual("", payload["attachment_id"])
        self.assertEqual("", payload["part_id"])
        self.assertTrue(payload["inline"])
        self.assertEqual(data, saved)
        self.assertEqual(0, service.users().messages().attachments().calls)
        # One fetch to enqueue, one re-fetch by the worker (bytes never persist).
        self.assertEqual(2, service.users().messages().get_calls)


if __name__ == "__main__":
    unittest.main()
