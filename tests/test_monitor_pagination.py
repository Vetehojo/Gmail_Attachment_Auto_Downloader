import os
import re
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

import gmail_monitor


class FakeStopEvent:
    """threading.Event stand-in with a non-blocking wait(), so worker_loop
    tests never sleep for real."""

    def __init__(self):
        self._flag = False

    def is_set(self):
        return self._flag

    def set(self):
        self._flag = True

    def wait(self, timeout=None):
        return self._flag


class FakeExecute:
    def __init__(self, value):
        self.value = value

    def execute(self, num_retries=None):
        return self.value


class FakeMessages:
    def __init__(self):
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        token = kwargs.get("pageToken")
        if token is None:
            return FakeExecute({"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "page2"})
        if token == "page2":
            return FakeExecute({"messages": [{"id": "m3"}]})
        raise AssertionError(f"unexpected page token: {token}")


class FakeUsers:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class FakeService:
    def __init__(self):
        self.messages_api = FakeMessages()
        self._users = FakeUsers(self.messages_api)

    def users(self):
        return self._users


class FakeQueue:
    def __init__(self):
        self.metadata = {}
        self.set_calls = []
        self.deleted_keys = []
        self.enqueued = []

    def get_metadata(self, key, default=None):
        return self.metadata.get(key, default)

    def set_metadata(self, key, value):
        self.metadata[key] = str(value)
        self.set_calls.append((key, value))

    def delete_metadata(self, key):
        self.deleted_keys.append(key)
        return self.metadata.pop(key, None) is not None

    def enqueue(self, job_key, payload, max_attempts=8):
        self.enqueued.append((job_key, payload))
        return True


class FakePool:
    def __init__(self):
        self.invalidated = []

    def get(self, account_email):
        return f"service:{account_email}"

    def invalidate(self, account_email):
        self.invalidated.append(account_email)


def oauth_settings():
    return {
        "auth_mode": "oauth",
        "target_email": "a@example.com",
        "final_dir": tempfile.gettempdir(),
        "lookback_days": "7",
    }


def query_start_timestamp(query):
    match = re.search(r"after:(\d+)", query)
    if not match:
        raise AssertionError(f"query has no after: clause: {query}")
    return int(match.group(1))


class MonitorPaginationTest(unittest.TestCase):
    def setUp(self):
        self.heartbeat = mock.patch.object(gmail_monitor, "write_heartbeat", lambda *a, **k: None)
        self.logger = mock.patch.object(gmail_monitor, "log", lambda *a, **k: None)
        self.heartbeat.start()
        self.logger.start()

    def tearDown(self):
        self.heartbeat.stop()
        self.logger.stop()

    def dwd_accounts(self):
        root = tempfile.gettempdir()
        return [
            {"email": "osaka@example.jp", "final_dir": os.path.join(root, "Osaka")},
            {"email": "nara@example.jp", "final_dir": os.path.join(root, "Nara")},
        ]

    def test_iter_message_ids_follows_next_page_token(self):
        service = FakeService()
        ids = list(gmail_monitor.iter_message_ids(service, "query", "a@example.com"))
        self.assertEqual(["m1", "m2", "m3"], ids)
        self.assertEqual(2, len(service.messages_api.calls))
        self.assertEqual(500, service.messages_api.calls[0]["maxResults"])
        self.assertEqual("page2", service.messages_api.calls[1]["pageToken"])

    def test_cursor_does_not_advance_when_enqueue_fails(self):
        queue = FakeQueue()
        cursor = datetime.now() - timedelta(hours=1)
        with mock.patch.object(gmail_monitor, "load_settings", return_value=oauth_settings()), \
             mock.patch.object(gmail_monitor, "get_mail_cursor", return_value=cursor), \
             mock.patch.object(gmail_monitor, "iter_message_ids", return_value=iter(["m1", "m2"])), \
             mock.patch.object(gmail_monitor, "enqueue_message_jobs", side_effect=[1, RuntimeError("boom")]):
            with self.assertRaises(RuntimeError):
                gmail_monitor.scan_gmail(queue, object(), account_email="a@example.com")
        self.assertEqual([], queue.set_calls)

    def test_existing_cursor_is_not_clamped_by_lookback(self):
        queue = FakeQueue()
        old_cursor = datetime.now() - timedelta(days=20)
        seen = {}

        def ids(_service, query, _account):
            seen["query"] = query
            return iter([])

        with mock.patch.object(gmail_monitor, "load_settings", return_value=oauth_settings()), \
             mock.patch.object(gmail_monitor, "get_mail_cursor", return_value=old_cursor), \
             mock.patch.object(gmail_monitor, "iter_message_ids", side_effect=ids):
            gmail_monitor.scan_gmail(queue, object(), account_email="a@example.com")
        expected_after = int((old_cursor - gmail_monitor.QUERY_OVERLAP).timestamp())
        self.assertEqual(expected_after, query_start_timestamp(seen["query"]))

    def test_initial_scan_uses_lookback_window(self):
        queue = FakeQueue()
        seen = {}

        def ids(_service, query, _account):
            seen["query"] = query
            return iter([])

        with mock.patch.object(gmail_monitor, "load_settings", return_value=oauth_settings()), \
             mock.patch.object(gmail_monitor, "get_mail_cursor", return_value=None), \
             mock.patch.object(gmail_monitor, "iter_message_ids", side_effect=ids):
            before = datetime.now()
            gmail_monitor.scan_gmail(queue, object(), account_email="a@example.com")
            after = datetime.now()
        start = query_start_timestamp(seen["query"])
        self.assertGreaterEqual(start, int((before - timedelta(days=7)).timestamp()) - 2)
        self.assertLessEqual(start, int((after - timedelta(days=7)).timestamp()) + 2)

    def test_query_has_no_recipient_filter_in_either_mode(self):
        # Both OAuth and DWD already select one concrete mailbox. A to: filter
        # would drop BCC, aliases, Group/ML delivery and forwarded mail.
        start = datetime.now() - timedelta(days=1)
        labels = ["FAX", "税務"]
        for mode in ("oauth", "dwd"):
            query = gmail_monitor.build_mail_query(start, "a@example.com", mode, excluded_labels=labels)
            self.assertIn("in:inbox", query)
            self.assertNotIn("to:", query)
            for label in labels:
                self.assertIn(f'-label:"{label}"', query)

    def test_build_mail_query_omits_label_filter_when_no_excluded_labels(self):
        # C4: the shipped default excluded-labels list is empty on purpose.
        start = datetime.now() - timedelta(days=1)
        self.assertNotIn("-label:", gmail_monitor.build_mail_query(start, "a@example.com", "oauth"))
        self.assertNotIn("-label:", gmail_monitor.build_mail_query(start, "a@example.com", "oauth", excluded_labels=[]))

    def test_scan_gmail_sources_excluded_labels_and_allowed_extensions_from_settings(self):
        queue = FakeQueue()
        seen = {}

        def ids(_service, query, _account):
            seen["query"] = query
            return iter(["m1"])

        def fake_enqueue(_q, _service, _message_id, account_email="", final_dir=None, allowed_extensions=None):
            seen["allowed_extensions"] = allowed_extensions
            return 0

        with mock.patch.object(gmail_monitor, "load_settings", return_value=oauth_settings()), \
             mock.patch.object(gmail_monitor, "get_mail_cursor", return_value=None), \
             mock.patch.object(gmail_monitor, "load_excluded_labels", return_value=["FAX"]), \
             mock.patch.object(gmail_monitor, "load_allowed_extensions", return_value={".pdf"}), \
             mock.patch.object(gmail_monitor, "iter_message_ids", side_effect=ids), \
             mock.patch.object(gmail_monitor, "enqueue_message_jobs", side_effect=fake_enqueue):
            gmail_monitor.scan_gmail(queue, object(), account_email="a@example.com")

        self.assertIn('-label:"FAX"', seen["query"])
        self.assertEqual({".pdf"}, seen["allowed_extensions"])

    def test_dwd_cursor_is_per_account(self):
        self.assertEqual("mail_cursor_timestamp:a@example.com", gmail_monitor.cursor_key("A@example.com", "dwd"))
        self.assertEqual("mail_cursor_timestamp:b@example.com", gmail_monitor.cursor_key("b@example.com", "dwd"))
        self.assertEqual(gmail_monitor.MAIL_CURSOR_KEY, gmail_monitor.cursor_key("a@example.com", "oauth"))

    def test_attachment_job_key_is_per_message_and_account(self):
        attachment = {"attachment_id": "att", "inline_data": "", "part_id": "1"}
        a1 = gmail_monitor._attachment_job_key("a@example.com", "m1", attachment)
        a2 = gmail_monitor._attachment_job_key("a@example.com", "m2", attachment)
        b1 = gmail_monitor._attachment_job_key("b@example.com", "m1", attachment)
        self.assertNotEqual(a1, a2)
        self.assertNotEqual(a1, b1)
        # Re-scanning the same message must resolve to the same durable key.
        self.assertEqual(a1, gmail_monitor._attachment_job_key("A@example.com", "m1", dict(attachment)))

    def test_inline_attachment_keys_separate_part_and_content(self):
        base = {"attachment_id": "", "inline_data": "QUFB", "part_id": "1"}
        key = gmail_monitor._attachment_job_key("a@example.com", "m1", base)
        other_part = gmail_monitor._attachment_job_key("a@example.com", "m1", dict(base, part_id="2"))
        other_data = gmail_monitor._attachment_job_key("a@example.com", "m1", dict(base, inline_data="QkJC"))
        self.assertNotEqual(key, other_part)
        self.assertNotEqual(key, other_data)
        # An attachmentId attachment never collides with an inline part.
        by_id = gmail_monitor._attachment_job_key(
            "a@example.com", "m1", {"attachment_id": "QUFB", "inline_data": "", "part_id": "1"}
        )
        self.assertNotEqual(key, by_id)

    def test_get_attachments_from_payload_collects_id_and_inline_parts(self):
        payload = {
            "parts": [
                {"partId": "0", "mimeType": "text/plain", "filename": "", "body": {"size": 12}},
                {"partId": "1", "filename": "report.pdf", "body": {"attachmentId": "att-1", "size": 2048}},
                {
                    "partId": "2",
                    "mimeType": "multipart/mixed",
                    "filename": "",
                    "body": {},
                    "parts": [
                        {"partId": "2.0", "filename": "memo.txt", "body": {"data": "aGVsbG8=", "size": 5}},
                    ],
                },
            ]
        }
        attachments, inline_image_skips = gmail_monitor.get_attachments_from_payload(payload)
        self.assertEqual(["report.pdf", "memo.txt"], [item["filename"] for item in attachments])
        self.assertEqual("att-1", attachments[0]["attachment_id"])
        self.assertEqual("", attachments[0]["inline_data"])
        self.assertEqual("", attachments[1]["attachment_id"])
        self.assertEqual("aGVsbG8=", attachments[1]["inline_data"])
        self.assertEqual("2.0", attachments[1]["part_id"])
        self.assertEqual(0, inline_image_skips)

    def test_get_attachments_from_payload_reads_a_single_part_top_level(self):
        # F6: a single-part message keeps filename + attachmentId/data on the
        # top-level payload, whose partId is "".
        by_id = {
            "partId": "",
            "mimeType": "application/pdf",
            "filename": "scan.pdf",
            "body": {"attachmentId": "att-top", "size": 2048},
        }
        text_body = {"data": "aGVsbG8=", "size": 5}
        inline = {"partId": "", "mimeType": "text/plain", "filename": "memo.txt", "body": text_body}
        plain_body = {"partId": "", "mimeType": "text/plain", "filename": "", "body": text_body}

        attachments, _skips = gmail_monitor.get_attachments_from_payload(by_id)
        self.assertEqual([("scan.pdf", "att-top", "", "")], [
            (a["filename"], a["attachment_id"], a["inline_data"], a["part_id"]) for a in attachments
        ])
        attachments, _skips = gmail_monitor.get_attachments_from_payload(inline)
        self.assertEqual([("memo.txt", "", "aGVsbG8=", "")], [
            (a["filename"], a["attachment_id"], a["inline_data"], a["part_id"]) for a in attachments
        ])
        # A plain text body with no filename is still not an attachment.
        self.assertEqual(([], 0), gmail_monitor.get_attachments_from_payload(plain_body))
        self.assertEqual(([], 0), gmail_monitor.get_attachments_from_payload({}))

    def test_find_part_by_id_matches_the_top_level_then_children(self):
        nested = {"partId": "2.0", "filename": "memo.txt", "body": {"data": "aGVsbG8="}}
        first = {"partId": "1", "filename": "report.pdf", "body": {"attachmentId": "att-1"}}
        child = {"partId": "2", "filename": "", "body": {}, "parts": [nested]}
        payload = {"partId": "", "filename": "", "body": {"size": 0}, "parts": [first, child]}

        self.assertIs(payload, gmail_monitor._find_part_by_id(payload, ""))
        self.assertIs(first, gmail_monitor._find_part_by_id(payload, "1"))
        self.assertIs(child, gmail_monitor._find_part_by_id(payload, "2"))
        self.assertIs(nested, gmail_monitor._find_part_by_id(payload, "2.0"))
        self.assertIsNone(gmail_monitor._find_part_by_id(payload, "9"))
        # A payload without a partId key still resolves its children as before.
        self.assertIs(first, gmail_monitor._find_part_by_id({"parts": [first]}, "1"))

    def test_get_attachments_from_payload_skips_inline_signature_logo(self):
        payload = {
            "parts": [
                {"partId": "1", "filename": "report.pdf", "body": {"attachmentId": "att-1", "size": 2048}},
                {
                    "partId": "2",
                    "filename": "logo.png",
                    "headers": [
                        {"name": "Content-ID", "value": "<logo123>"},
                        {"name": "Content-Disposition", "value": "inline; filename=\"logo.png\""},
                    ],
                    "body": {"attachmentId": "att-2", "size": 900},
                },
            ]
        }
        attachments, inline_image_skips = gmail_monitor.get_attachments_from_payload(payload)
        self.assertEqual(["report.pdf"], [item["filename"] for item in attachments])
        self.assertEqual(1, inline_image_skips)

    def test_is_inline_image_part_accepts_a_real_attachment(self):
        part = {
            "filename": "invoice.pdf",
            "headers": [
                {"name": "Content-Disposition", "value": 'attachment; filename="invoice.pdf"'},
            ],
            "body": {"attachmentId": "att-1", "size": 100},
        }
        self.assertFalse(gmail_monitor._is_inline_image_part(part))

    def test_is_inline_image_part_rejects_a_signature_logo(self):
        part = {
            "filename": "logo.png",
            "headers": [
                {"name": "Content-ID", "value": "<logo123>"},
                {"name": "Content-Disposition", "value": 'inline; filename="logo.png"'},
            ],
            "body": {"attachmentId": "att-2", "size": 2048},
        }
        self.assertTrue(gmail_monitor._is_inline_image_part(part))

    def test_is_inline_image_part_keeps_an_inline_pdf_as_a_real_attachment(self):
        # A sender who inlines a PDF has no image extension, so it must not be skipped.
        part = {
            "filename": "contract.pdf",
            "headers": [
                {"name": "Content-ID", "value": "<contract123>"},
                {"name": "Content-Disposition", "value": 'inline; filename="contract.pdf"'},
            ],
            "body": {"attachmentId": "att-3", "size": 4096},
        }
        self.assertFalse(gmail_monitor._is_inline_image_part(part))

    def test_is_inline_image_part_requires_both_content_id_and_inline_disposition(self):
        no_content_id = {
            "filename": "logo.png",
            "headers": [{"name": "Content-Disposition", "value": "inline"}],
            "body": {"attachmentId": "att-4"},
        }
        not_inline = {
            "filename": "logo.png",
            "headers": [
                {"name": "Content-ID", "value": "<logo123>"},
                {"name": "Content-Disposition", "value": "attachment"},
            ],
            "body": {"attachmentId": "att-5"},
        }
        self.assertFalse(gmail_monitor._is_inline_image_part(no_content_id))
        self.assertFalse(gmail_monitor._is_inline_image_part(not_inline))

    def test_dwd_accounts_scan_into_independent_cursors_and_dirs(self):
        queue = FakeQueue()
        accounts = self.dwd_accounts()
        calls = []

        def fake_scan(q, service, account_email=None, final_dir=None, auth_mode=None):
            calls.append((service, account_email, final_dir, auth_mode))
            q.set_metadata(gmail_monitor.cursor_key(account_email, auth_mode), 1000.0)
            return {"account": account_email, "messages": 0, "attachments": 0}

        with mock.patch.object(gmail_monitor, "load_settings", return_value={"auth_mode": "dwd"}), \
             mock.patch.object(gmail_monitor, "get_account_configs", return_value=accounts), \
             mock.patch.object(gmail_monitor, "scan_gmail", side_effect=fake_scan):
            result = gmail_monitor.scan_all_accounts(queue, FakePool())

        self.assertEqual([], result["errors"])
        self.assertEqual(
            [
                ("service:osaka@example.jp", "osaka@example.jp", accounts[0]["final_dir"], "dwd"),
                ("service:nara@example.jp", "nara@example.jp", accounts[1]["final_dir"], "dwd"),
            ],
            calls,
        )
        self.assertEqual(
            ["mail_cursor_timestamp:osaka@example.jp", "mail_cursor_timestamp:nara@example.jp"],
            [key for key, _value in queue.set_calls],
        )
        self.assertIn(gmail_monitor.LAST_ERROR_KEY, queue.deleted_keys)

    def test_failed_account_does_not_advance_its_own_cursor_or_block_others(self):
        queue = FakeQueue()
        accounts = self.dwd_accounts()
        pool = FakePool()

        def fake_scan(q, service, account_email=None, final_dir=None, auth_mode=None):
            if account_email == "osaka@example.jp":
                raise RuntimeError("mailbox unavailable")
            q.set_metadata(gmail_monitor.cursor_key(account_email, auth_mode), 1000.0)
            return {"account": account_email, "messages": 0, "attachments": 0}

        with mock.patch.object(gmail_monitor, "load_settings", return_value={"auth_mode": "dwd"}), \
             mock.patch.object(gmail_monitor, "get_account_configs", return_value=accounts), \
             mock.patch.object(gmail_monitor, "scan_gmail", side_effect=fake_scan):
            result = gmail_monitor.scan_all_accounts(queue, pool)

        self.assertEqual(1, len(result["errors"]))
        self.assertEqual("osaka@example.jp", result["errors"][0]["account"])
        self.assertEqual(["osaka@example.jp"], pool.invalidated)
        cursor_keys = [
            key for key, _value in queue.set_calls if key.startswith(gmail_monitor.MAIL_CURSOR_KEY)
        ]
        self.assertEqual(["mail_cursor_timestamp:nara@example.jp"], cursor_keys)
        self.assertIsNotNone(queue.get_metadata(gmail_monitor.LAST_ERROR_KEY))


class WorkerLivenessTest(unittest.TestCase):
    """C3: no exception in worker_loop (including ServicePool construction)
    can silently kill the worker thread without leaving a record, and the
    GUI-facing heartbeat is refreshed while the loop runs.
    """

    def setUp(self):
        self.logger = mock.patch.object(gmail_monitor, "log", lambda *a, **k: None)
        self.logger.start()

    def tearDown(self):
        self.logger.stop()

    def test_heartbeat_is_refreshed_and_stale_error_cleared_on_normal_start(self):
        queue = FakeQueue()
        queue.metadata[gmail_monitor.WORKER_ERROR_KEY] = "stale error from a previous run"
        stop_event = FakeStopEvent()

        def fake_process_one_job(_queue, _pool):
            stop_event.set()
            return False

        with mock.patch.object(gmail_monitor, "ServicePool", return_value=object()), \
             mock.patch.object(gmail_monitor, "load_settings", return_value=oauth_settings()), \
             mock.patch.object(gmail_monitor, "process_one_job", side_effect=fake_process_one_job):
            gmail_monitor.worker_loop(queue, stop_event)

        self.assertIn(gmail_monitor.WORKER_HEARTBEAT_KEY, queue.metadata)
        self.assertNotIn(gmail_monitor.WORKER_ERROR_KEY, queue.metadata)

    def test_fatal_exception_during_service_pool_construction_sets_error_key(self):
        queue = FakeQueue()
        stop_event = FakeStopEvent()

        with mock.patch.object(gmail_monitor, "ServicePool", side_effect=RuntimeError("boom: no credentials")), \
             mock.patch.object(gmail_monitor, "load_settings", return_value=oauth_settings()):
            gmail_monitor.worker_loop(queue, stop_event)

        self.assertIn("boom: no credentials", queue.metadata.get(gmail_monitor.WORKER_ERROR_KEY, ""))

    def test_recoverable_exception_inside_the_loop_does_not_set_the_error_key(self):
        queue = FakeQueue()
        stop_event = FakeStopEvent()
        calls = {"count": 0}

        def flaky_process_one_job(_queue, _pool):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("transient sqlite error")
            stop_event.set()
            return False

        with mock.patch.object(gmail_monitor, "ServicePool", return_value=object()), \
             mock.patch.object(gmail_monitor, "load_settings", return_value=oauth_settings()), \
             mock.patch.object(gmail_monitor, "process_one_job", side_effect=flaky_process_one_job):
            gmail_monitor.worker_loop(queue, stop_event)

        self.assertEqual(2, calls["count"])
        self.assertNotIn(gmail_monitor.WORKER_ERROR_KEY, queue.metadata)


class FakeMessageService:
    """A minimal service exposing only users().messages().get(), for
    enqueue_message_jobs tests that never touch list()/pagination."""

    def __init__(self, message_payload):
        self._payload = message_payload

    def users(self):
        return self

    def messages(self):
        return self

    def get(self, **kwargs):
        return FakeExecute({"payload": self._payload})


class EnqueueMessageJobsTest(unittest.TestCase):
    def setUp(self):
        self.heartbeat = mock.patch.object(gmail_monitor, "write_heartbeat", lambda *a, **k: None)
        self.heartbeat.start()

    def tearDown(self):
        self.heartbeat.stop()

    def test_persisted_payload_carries_no_bytes_and_every_skip_is_logged(self):
        # C5: no inline_data in the persisted payload, only attachment_id /
        # part_id / inline. C9: every disallowed/inline-image skip is logged.
        message_payload = {
            "headers": [
                {"name": "subject", "value": "件名"},
                {"name": "from", "value": "sender@example.com"},
                {"name": "date", "value": "Tue, 25 Aug 2026 00:00:00 +0900"},
            ],
            "parts": [
                {"partId": "1", "filename": "report.pdf", "body": {"attachmentId": "att-1", "size": 100}},
                {"partId": "2", "filename": "data.csv", "body": {"attachmentId": "att-2", "size": 50}},
                {"partId": "3", "filename": "memo.txt", "body": {"data": "aGVsbG8=", "size": 5}},
                {
                    "partId": "4",
                    "filename": "logo.png",
                    "headers": [
                        {"name": "Content-ID", "value": "<logo1>"},
                        {"name": "Content-Disposition", "value": "inline"},
                    ],
                    "body": {"attachmentId": "att-4", "size": 900},
                },
            ],
        }
        service = FakeMessageService(message_payload)
        queue = FakeQueue()
        logged = []

        with mock.patch.object(gmail_monitor, "load_settings", return_value=oauth_settings()), \
             mock.patch.object(gmail_monitor, "log", lambda message, procedure="monitor": logged.append(message)):
            added = gmail_monitor.enqueue_message_jobs(
                queue, service, "m1", "a@example.com", tempfile.gettempdir(),
                allowed_extensions={".pdf", ".txt"},
            )

        # report.pdf (attachment_id) and memo.txt (inline) are enqueued;
        # data.csv is disallowed and logo.png is an inline signature image.
        self.assertEqual(2, added)
        self.assertEqual(2, len(queue.enqueued))
        filenames = {payload["filename"] for _key, payload in queue.enqueued}
        self.assertEqual({"report.pdf", "memo.txt"}, filenames)
        for _key, payload in queue.enqueued:
            self.assertNotIn("inline_data", payload)
            self.assertIn("attachment_id", payload)
            self.assertIn("part_id", payload)
            self.assertIn("inline", payload)
        by_filename = {payload["filename"]: payload for _key, payload in queue.enqueued}
        self.assertEqual("att-1", by_filename["report.pdf"]["attachment_id"])
        self.assertFalse(by_filename["report.pdf"]["inline"])
        self.assertEqual("", by_filename["memo.txt"]["attachment_id"])
        self.assertEqual("3", by_filename["memo.txt"]["part_id"])
        self.assertTrue(by_filename["memo.txt"]["inline"])

        self.assertTrue(any("data.csv" in line and "extension .csv is not allowed" in line for line in logged))
        self.assertTrue(any("Skipped 1 inline image part" in line for line in logged))

    def test_multipart_job_keys_are_unchanged_by_top_level_scanning(self):
        # F6/I4: these keys were computed with the pre-F6 code, which never
        # looked at the top-level payload. A changed key would re-download
        # attachments that are already saved.
        message_payload = {
            "partId": "",
            "mimeType": "multipart/mixed",
            "filename": "",
            "headers": [
                {"name": "subject", "value": "件名"},
                {"name": "from", "value": "sender@example.com"},
                {"name": "date", "value": "Tue, 25 Aug 2026 00:00:00 +0900"},
                {"name": "Content-Type", "value": "multipart/mixed; boundary=b1"},
            ],
            "body": {"size": 0},
            "parts": [
                {"partId": "0", "mimeType": "text/plain", "filename": "", "body": {"size": 12}},
                {"partId": "1", "filename": "report.pdf", "body": {"attachmentId": "att-1", "size": 2048}},
                {
                    "partId": "2",
                    "mimeType": "multipart/mixed",
                    "filename": "",
                    "body": {},
                    "parts": [
                        {"partId": "2.0", "filename": "memo.txt", "body": {"data": "aGVsbG8=", "size": 5}},
                    ],
                },
            ],
        }
        queue = FakeQueue()
        with mock.patch.object(gmail_monitor, "load_settings", return_value=oauth_settings()), \
             mock.patch.object(gmail_monitor, "log", lambda *a, **k: None):
            added = gmail_monitor.enqueue_message_jobs(
                queue, FakeMessageService(message_payload), "m1", "a@example.com", tempfile.gettempdir(),
                allowed_extensions={".pdf", ".txt"},
            )

        self.assertEqual(2, added)
        self.assertEqual(
            [
                "attachment:a@example.com:m1:fb6d26e9d81218fafbcbe562680cfc2ed4e2b2b6fae879e8a445ea5d4d23da9f",
                "attachment:a@example.com:m1:799835831bee5f935190f4310495279fc917bb5b0a8e1947b9c44f8429a37366",
            ],
            [key for key, _payload in queue.enqueued],
        )
        self.assertEqual(["1", "2.0"], [payload["part_id"] for _key, payload in queue.enqueued])


class OrphanTempCleanupTest(unittest.TestCase):
    def test_removes_only_matching_temps_and_leaves_real_files_alone(self):
        # W2: this installation's temps go at once; another installation's
        # (other PC sharing the folder) and pre-install-id ".gmailad_*.tmp"
        # temps only once they are older than FOREIGN_TEMP_MAX_AGE.
        with tempfile.TemporaryDirectory() as td:
            dir_a = os.path.join(td, "A")
            dir_b = os.path.join(td, "B")
            os.makedirs(dir_a)
            os.makedirs(dir_b)
            own_id = gmail_monitor.installation_id()
            other_id = "zzzzzz" if own_id != "zzzzzz" else "yyyyyy"
            keep = os.path.join(dir_a, "real_document.pdf")
            with open(keep, "wb") as handle:
                handle.write(b"keep")
            own_a = os.path.join(dir_a, gmail_monitor.temp_file_name(1))
            own_b = os.path.join(dir_b, gmail_monitor.temp_file_name(2))
            foreign_live = os.path.join(dir_a, gmail_monitor.temp_file_name(3, other_id))
            foreign_old = os.path.join(dir_b, gmail_monitor.temp_file_name(4, other_id))
            legacy_live = os.path.join(dir_a, ".gmailad_5.tmp")
            legacy_old = os.path.join(dir_b, ".gmailad_6.tmp")
            unrelated = [
                os.path.join(dir_a, "unrelated.tmp"),
                os.path.join(dir_a, ".gad_notes.txt"),
                os.path.join(dir_a, f".gad{other_id}_7.pdf"),
            ]
            for path in (own_a, own_b, foreign_live, foreign_old, legacy_live, legacy_old, *unrelated):
                with open(path, "wb") as handle:
                    handle.write(b"junk")
            old = time.time() - gmail_monitor.FOREIGN_TEMP_MAX_AGE - 60
            for path in (foreign_old, legacy_old, *unrelated):
                os.utime(path, (old, old))

            # dir_a listed twice: the sweep must not double-count or error on a duplicate.
            removed = gmail_monitor.cleanup_orphan_temp_files([dir_a, dir_b, dir_a])

            self.assertEqual(4, removed)
            for path in (own_a, own_b, foreign_old, legacy_old):
                self.assertFalse(os.path.exists(path), path)
            for path in (keep, foreign_live, legacy_live, *unrelated):
                self.assertTrue(os.path.isfile(path), path)

    def test_missing_and_empty_directories_are_ignored_without_error(self):
        with tempfile.TemporaryDirectory() as td:
            missing = os.path.join(td, "does-not-exist")
            self.assertEqual(0, gmail_monitor.cleanup_orphan_temp_files([missing, "", None]))


if __name__ == "__main__":
    unittest.main()
