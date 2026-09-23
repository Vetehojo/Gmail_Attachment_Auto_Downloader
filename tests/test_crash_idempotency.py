import base64
import hashlib
import json
import os
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
                gmail_monitor._write_bytes_atomic(b"1", short_target, "tagtag")
                gmail_monitor._write_bytes_atomic(b"2", long_target, "tagtag")

            temp_names = [
                os.path.basename(p) for p in opened
                if os.path.basename(p).startswith(gmail_monitor.TEMP_PREFIX)
            ]
            self.assertEqual(2, len(temp_names))
            self.assertEqual(temp_names[0], temp_names[1])
            self.assertEqual(f"{gmail_monitor.TEMP_PREFIX}tagtag{gmail_monitor.TEMP_SUFFIX}", temp_names[0])
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


if __name__ == "__main__":
    unittest.main()
