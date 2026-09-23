import base64
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import gmail_monitor
import runtime_state
from job_queue import JobQueue


def deny_replace_onto(target):
    """Patch os.replace so replacing `target` fails as it does on Windows while
    another process (the tray or watchdog) holds the file open; every other
    rename goes through."""
    real_replace = os.replace

    def replace(src, dst, *args, **kwargs):
        if os.path.abspath(dst) == os.path.abspath(target):
            raise PermissionError(13, "Access is denied", dst)
        return real_replace(src, dst, *args, **kwargs)

    return mock.patch.object(runtime_state.os, "replace", side_effect=replace)


def log_lines(path, text):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        return [line for line in handle.read().splitlines() if text in line]


class Execute:
    def __init__(self, value):
        self.value = value

    def execute(self, num_retries=None):
        return self.value


class AttachmentService:
    def __init__(self, data):
        self.data = data

    def users(self):
        return self

    def messages(self):
        return self

    def attachments(self):
        return self

    def get(self, **kwargs):
        return Execute({"data": base64.urlsafe_b64encode(self.data).decode("ascii")})


class StaticPool:
    def __init__(self, service):
        self.service = service

    def get(self, account_email):
        return self.service

    def invalidate(self, account_email):
        pass


class AtomicWriteJsonTest(unittest.TestCase):
    def test_denied_heartbeat_replace_is_logged_once_and_leaves_no_temp(self):
        with tempfile.TemporaryDirectory() as td:
            heartbeat = os.path.join(td, "state", "heartbeat.json")
            log_path = os.path.join(td, "log", "mail_log.txt")
            runtime_state.write_heartbeat(heartbeat, "ok", "before", log_path=log_path)

            with deny_replace_onto(heartbeat):
                runtime_state.write_heartbeat(heartbeat, "ok", "denied-1", log_path=log_path)
                runtime_state.write_heartbeat(heartbeat, "error", "denied-2", log_path=log_path)

            self.assertFalse(os.path.exists(heartbeat + ".tmp"))
            # The last complete document stays in place for the tray/watchdog.
            self.assertEqual("before", runtime_state.read_heartbeat(heartbeat)["detail"])
            self.assertEqual(1, len(log_lines(log_path, "Heartbeat write failed")))
            self.assertEqual([], log_lines(log_path, "Heartbeat write recovered"))

            runtime_state.write_heartbeat(heartbeat, "ok", "after", log_path=log_path)
            runtime_state.write_heartbeat(heartbeat, "ok", "after-2", log_path=log_path)

            self.assertEqual("after-2", runtime_state.read_heartbeat(heartbeat)["detail"])
            self.assertEqual(1, len(log_lines(log_path, "Heartbeat write failed")))
            self.assertEqual(1, len(log_lines(log_path, "Heartbeat write recovered")))

    def test_journal_write_still_raises_and_removes_its_temp(self):
        # The commit journal must keep failing loudly; only the heartbeat is best-effort.
        self.assertIs(runtime_state.atomic_write_json, gmail_monitor.atomic_write_json)
        with tempfile.TemporaryDirectory() as td:
            journal = os.path.join(td, "state", "attachmentjob_1.json")
            runtime_state.atomic_write_json(journal, {"phase": "prepared"})

            with deny_replace_onto(journal):
                with self.assertRaises(PermissionError):
                    runtime_state.atomic_write_json(journal, {"phase": "committed"})

            self.assertFalse(os.path.exists(journal + ".tmp"))
            with open(journal, "r", encoding="utf-8") as handle:
                self.assertEqual({"phase": "prepared"}, json.load(handle))

    def test_writers_to_the_same_path_are_serialized(self):
        # Scanner and worker threads share one fixed "<path>.tmp". Hold the
        # first writer just before its rename: the second must wait instead
        # of truncating the first writer's temp.
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "heartbeat.json")
            real_replace = os.replace
            entered = threading.Event()
            release = threading.Event()
            calls = []
            errors = []

            def gated_replace(src, dst, *args, **kwargs):
                calls.append(dst)
                if len(calls) == 1:
                    entered.set()
                    release.wait(5)
                return real_replace(src, dst, *args, **kwargs)

            def writer(value):
                try:
                    runtime_state.atomic_write_json(path, {"writer": value})
                except Exception as exc:
                    errors.append(exc)

            with mock.patch.object(runtime_state.os, "replace", side_effect=gated_replace):
                first = threading.Thread(target=writer, args=(1,))
                first.start()
                self.assertTrue(entered.wait(5))
                second = threading.Thread(target=writer, args=(2,))
                second.start()
                second.join(0.3)
                try:
                    self.assertTrue(second.is_alive())
                    with open(path + ".tmp", "r", encoding="utf-8") as handle:
                        self.assertEqual({"writer": 1}, json.load(handle))
                finally:
                    release.set()
                    first.join(5)
                    second.join(5)

            self.assertEqual([], errors)
            self.assertEqual(2, len(calls))
            self.assertFalse(os.path.exists(path + ".tmp"))
            with open(path, "r", encoding="utf-8") as handle:
                self.assertEqual({"writer": 2}, json.load(handle))


class HeartbeatDoesNotStrandJobsTest(unittest.TestCase):
    def payload(self, final_dir):
        return {
            "account_email": "a@example.com",
            "final_dir": final_dir,
            "message_id": "m1",
            "attachment_id": "att1",
            "part_id": "",
            "inline": False,
            "filename": "document.pdf",
            "received_date": "20260825",
            "sender_email": "sender@example.com",
            "mail_subject": "subject",
        }

    def test_unexpected_heartbeat_exception_releases_the_claimed_job(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "state")
            os.makedirs(state)
            queue = JobQueue(os.path.join(state, "jobs.sqlite3"))
            queue.enqueue("attachment:a:m1:x", self.payload(os.path.join(td, "final")))
            pool = mock.Mock()
            exploded = RuntimeError("heartbeat exploded")

            with mock.patch.object(gmail_monitor, "STATE_DIR", state), \
                 mock.patch.object(gmail_monitor, "write_heartbeat", side_effect=exploded), \
                 mock.patch.object(gmail_monitor, "log", lambda *a, **k: None):
                self.assertTrue(gmail_monitor.process_one_job(queue, pool))

            job = queue.get_job(1)
            self.assertEqual("pending", job["status"])
            self.assertIn("heartbeat exploded", job["last_error"])
            self.assertEqual(0, queue.counts()["processing"])
            pool.get.assert_not_called()

    def test_denied_heartbeat_replace_does_not_fail_the_job(self):
        data = b"attachment-bytes"
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "state")
            final = os.path.join(td, "final")
            heartbeat = os.path.join(state, "heartbeat.json")
            log_path = os.path.join(td, "log", "mail_log.txt")
            os.makedirs(state)
            queue = JobQueue(os.path.join(state, "jobs.sqlite3"))
            queue.enqueue("attachment:a:m1:x", self.payload(final))

            with mock.patch.object(gmail_monitor, "STATE_DIR", state), \
                 mock.patch.object(gmail_monitor, "HEARTBEAT_FILE", heartbeat), \
                 mock.patch.object(gmail_monitor, "LOG_FILE", log_path), \
                 deny_replace_onto(heartbeat):
                self.assertTrue(gmail_monitor.process_one_job(queue, StaticPool(AttachmentService(data))))

            self.assertEqual("success", queue.get_job(1)["status"])
            [saved] = os.listdir(final)
            with open(os.path.join(final, saved), "rb") as handle:
                self.assertEqual(data, handle.read())
            self.assertFalse(os.path.exists(heartbeat + ".tmp"))
            self.assertEqual(1, len(log_lines(log_path, "Heartbeat write failed")))

    def test_denied_heartbeat_replace_does_not_abort_a_scan(self):
        class MetadataQueue:
            def __init__(self):
                self.metadata = {}

            def set_metadata(self, key, value):
                self.metadata[key] = value

        settings = {"auth_mode": "oauth", "target_email": "a@example.com", "lookback_days": "7"}
        with tempfile.TemporaryDirectory() as td:
            heartbeat = os.path.join(td, "state", "heartbeat.json")
            log_path = os.path.join(td, "log", "mail_log.txt")
            queue = MetadataQueue()
            # Ten messages also reach the every-10-messages heartbeat.
            message_ids = [f"m{index}" for index in range(10)]

            with mock.patch.object(gmail_monitor, "HEARTBEAT_FILE", heartbeat), \
                 mock.patch.object(gmail_monitor, "LOG_FILE", log_path), \
                 mock.patch.object(gmail_monitor, "load_settings", return_value=settings), \
                 mock.patch.object(gmail_monitor, "get_mail_cursor", return_value=None), \
                 mock.patch.object(gmail_monitor, "iter_message_ids", return_value=iter(message_ids)), \
                 mock.patch.object(gmail_monitor, "enqueue_message_jobs", return_value=0), \
                 deny_replace_onto(heartbeat):
                result = gmail_monitor.scan_gmail(queue, object(), account_email="a@example.com", final_dir=td)

            self.assertEqual(10, result["messages"])
            self.assertIn(gmail_monitor.MAIL_CURSOR_KEY, queue.metadata)
            self.assertFalse(os.path.exists(heartbeat + ".tmp"))
            self.assertEqual(1, len(log_lines(log_path, "Heartbeat write failed")))


if __name__ == "__main__":
    unittest.main()
