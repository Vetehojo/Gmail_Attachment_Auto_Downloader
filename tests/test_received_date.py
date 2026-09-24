"""D2: the file-name date is Gmail's received time (internalDate) in the PC's timezone."""
import functools
import tempfile
import unittest
from datetime import datetime, timedelta, timezone, tzinfo
from unittest import mock

import gmail_monitor

JST = timezone(timedelta(hours=9))
UTC = timezone.utc
# 2026-08-24T15:30:00Z: still the 24th in UTC, already the 25th in Japan.
LATE_EVENING_UTC_MS = str(int(datetime(2026, 8, 24, 15, 30, tzinfo=UTC).timestamp() * 1000))
# A Date header whose own date differs from both, so the source is visible.
DATE_HEADER = "Tue, 1 Jan 2030 12:00:00 +0000"


class RaisingTz(tzinfo):
    """A timezone whose offset lookup fails, as localtime() can on Windows."""

    def utcoffset(self, dt):
        raise OSError(22, "Invalid argument")

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "broken"


class ReceivedDateTest(unittest.TestCase):
    def setUp(self):
        self.logged = []
        patcher = mock.patch.object(
            gmail_monitor, "log", lambda message, procedure="monitor": self.logged.append((procedure, message))
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_internal_date_is_converted_to_the_given_timezone(self):
        self.assertEqual("20260825", gmail_monitor.get_received_date_yyyymmdd(DATE_HEADER, LATE_EVENING_UTC_MS, JST))
        self.assertEqual("20260824", gmail_monitor.get_received_date_yyyymmdd(DATE_HEADER, LATE_EVENING_UTC_MS, UTC))
        self.assertEqual([], self.logged)

    def test_midnight_boundary_in_japan(self):
        midnight_jst = int(datetime(2026, 8, 25, tzinfo=JST).timestamp() * 1000)
        self.assertEqual("20260824", gmail_monitor.get_received_date_yyyymmdd("", str(midnight_jst - 1), JST))
        self.assertEqual("20260825", gmail_monitor.get_received_date_yyyymmdd("", str(midnight_jst), JST))
        self.assertEqual("20260824", gmail_monitor.get_received_date_yyyymmdd("", str(midnight_jst), UTC))

    def test_an_integer_internal_date_is_accepted_too(self):
        self.assertEqual("20260825", gmail_monitor.get_received_date_yyyymmdd("", int(LATE_EVENING_UTC_MS), JST))

    def test_default_is_the_pc_local_timezone(self):
        expected = datetime.fromtimestamp(int(LATE_EVENING_UTC_MS) / 1000).strftime("%Y%m%d")
        self.assertEqual(expected, gmail_monitor.get_received_date_yyyymmdd(DATE_HEADER, LATE_EVENING_UTC_MS))

    def test_missing_internal_date_uses_the_date_header_as_before(self):
        self.assertEqual("20300101", gmail_monitor.get_received_date_yyyymmdd(DATE_HEADER, None, JST))
        # The header's own date, not converted to tz (unchanged behavior).
        self.assertEqual(
            "20260825", gmail_monitor.get_received_date_yyyymmdd("Tue, 25 Aug 2026 00:00:00 +0900", None, UTC)
        )
        self.assertEqual([], self.logged)

    def test_unusable_internal_date_falls_back_to_the_date_header_and_is_logged(self):
        for value in ("", "abc", "12.5", "0", "-1", "9" * 40, float("inf"), [], {"ms": 1}):
            with self.subTest(value=value):
                self.logged.clear()
                self.assertEqual("20300101", gmail_monitor.get_received_date_yyyymmdd(DATE_HEADER, value, JST))
                self.assertEqual(1, len(self.logged))
                self.assertEqual("scan", self.logged[0][0])
                self.assertIn("Unusable internalDate", self.logged[0][1])

    def test_os_error_from_the_timezone_conversion_falls_back(self):
        self.assertEqual("20300101", gmail_monitor.get_received_date_yyyymmdd(DATE_HEADER, LATE_EVENING_UTC_MS, RaisingTz()))
        self.assertIn("Invalid argument", self.logged[0][1])

    def test_no_usable_date_at_all_is_today(self):
        with mock.patch.object(gmail_monitor, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 9, 1, 8, 0)
            self.assertEqual("20260901", gmail_monitor.get_received_date_yyyymmdd("not a date", "x", JST))


class Execute:
    def __init__(self, value):
        self.value = value

    def execute(self, num_retries=None):
        return self.value


class MessageService:
    """users().messages().get(format="full") returning internalDate and a payload."""

    def __init__(self, message):
        self.message = message
        self.formats = []

    def users(self):
        return self

    def messages(self):
        return self

    def get(self, **kwargs):
        self.formats.append(kwargs.get("format"))
        return Execute(self.message)


class RecordingQueue:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, job_key, payload):
        self.enqueued.append(payload)
        return True


class MonitorPathTest(unittest.TestCase):
    def enqueue(self, message, tz):
        queue = RecordingQueue()
        service = MessageService(message)
        settings = {"auth_mode": "oauth", "target_email": "a@example.com", "final_dir": tempfile.gettempdir()}
        real = gmail_monitor.get_received_date_yyyymmdd
        with mock.patch.object(gmail_monitor, "load_settings", return_value=settings), \
             mock.patch.object(gmail_monitor, "log", lambda *a, **k: None), \
             mock.patch.object(gmail_monitor, "get_received_date_yyyymmdd", functools.partial(real, tz=tz)):
            added = gmail_monitor.enqueue_message_jobs(
                queue, service, "m1", "a@example.com", tempfile.gettempdir(), allowed_extensions={".pdf"}
            )
        self.assertEqual(["full"], service.formats)
        self.assertEqual(1, added)
        return queue.enqueued[0]

    def message(self, **extra):
        return dict(extra, payload={
            "headers": [
                {"name": "subject", "value": "subject"},
                {"name": "from", "value": "sender@example.com"},
                {"name": "date", "value": DATE_HEADER},
            ],
            "parts": [{"partId": "1", "filename": "report.pdf", "body": {"attachmentId": "att-1", "size": 10}}],
        })

    def test_job_payload_carries_the_received_date_in_the_pc_timezone(self):
        message = self.message(internalDate=LATE_EVENING_UTC_MS)
        self.assertEqual("20260825", self.enqueue(message, JST)["received_date"])
        self.assertEqual("20260824", self.enqueue(message, UTC)["received_date"])

    def test_without_internal_date_the_date_header_is_used(self):
        self.assertEqual("20300101", self.enqueue(self.message(), JST)["received_date"])


if __name__ == "__main__":
    unittest.main()
