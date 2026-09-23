import os
import tempfile
import unittest
from unittest import mock

import pystray

import gmail_app
from job_queue import JobQueue


class PauseReasonTestCase(unittest.TestCase):
    """F5: an exit-time pause must not survive a restart, but a user-chosen
    (or reason-less legacy) pause must (I6). Exercises gmail_app.TrayApp
    directly against a temp queue DB - never the repo's state/ - with the
    controller/tray/messagebox mocked so no monitor process or real GUI runs.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = os.path.join(self._tmpdir.name, "jobs.sqlite3")
        self.addCleanup(lambda: setattr(gmail_app, "_QUEUE_INSTANCE", None))
        patch_db = mock.patch.object(gmail_app, "QUEUE_DB", self.db_path)
        patch_instance = mock.patch.object(gmail_app, "_QUEUE_INSTANCE", None)
        patch_messagebox = mock.patch.object(gmail_app, "messagebox")
        patch_configured = mock.patch.object(gmail_app, "is_configured", return_value=True)
        for patcher in (patch_db, patch_instance, patch_messagebox, patch_configured):
            patcher.start()
            self.addCleanup(patcher.stop)

        self.queue = JobQueue(self.db_path)
        self.app = gmail_app.TrayApp()
        self.app.controller = mock.Mock()
        # Sentinels distinct from None so icon assertions below are meaningful
        # without needing a real pystray/PIL icon.
        self.app.normal_icon = "normal-icon"
        self.app.error_icon = "error-icon"
        self.addCleanup(self.app.root.destroy)

    def _add_failed_job(self):
        self.queue.enqueue("job-1", {"foo": "bar"}, max_attempts=1)
        job = self.queue.claim_next()
        self.queue.mark_failure(job["id"], "boom")

    def test_exit_then_start_resumes(self):
        self.app.exit_app()
        self.assertTrue(self.app.paused())
        self.assertEqual("exit", self.queue.get_metadata(gmail_app.PAUSE_REASON_KEY))
        self.app.controller.stop_all.assert_called_once()

        self.app.controller.reset_mock()
        self.app._prepare_startup()

        self.assertFalse(self.app.paused())
        self.assertIsNone(self.queue.get_metadata(gmail_app.PAUSE_REASON_KEY))
        self.app.controller.start.assert_called_once()
        self.assertEqual("", self.app._startup_notice)

    def test_user_pause_then_start_stays_paused_and_notifies_via_setup_callback(self):
        self.app.toggle_pause()
        self.assertTrue(self.app.paused())
        self.assertEqual("user", self.queue.get_metadata(gmail_app.PAUSE_REASON_KEY))

        self.app.controller.reset_mock()
        self.app._prepare_startup()

        self.assertTrue(self.app.paused())
        self.app.controller.start.assert_not_called()
        self.assertEqual(gmail_app.PAUSE_STARTUP_NOTICE, self.app._startup_notice)

        # The notice must actually be wired through pystray's run_detached
        # setup callback - notifying right after run_detached() returns can
        # race the platform backend and be silently dropped.
        with mock.patch.object(pystray.Icon, "run_detached") as run_detached:
            self.app._start_tray()
        run_detached.assert_called_once()
        setup_callback = run_detached.call_args.kwargs["setup"]

        fake_icon = mock.Mock()
        setup_callback(fake_icon)
        self.assertTrue(fake_icon.visible)
        fake_icon.notify.assert_called_once_with(
            gmail_app.PAUSE_STARTUP_NOTICE, "Gmail Attachment Downloader"
        )

    def test_user_pause_then_exit_then_start_stays_paused(self):
        self.app.toggle_pause()
        self.assertEqual("user", self.queue.get_metadata(gmail_app.PAUSE_REASON_KEY))

        self.app.exit_app()
        # Already paused before exit_app ran, so the existing "user" reason
        # must be kept rather than overwritten with "exit".
        self.assertEqual("user", self.queue.get_metadata(gmail_app.PAUSE_REASON_KEY))

        self.app.controller.reset_mock()
        self.app._prepare_startup()

        self.assertTrue(self.app.paused())
        self.app.controller.start.assert_not_called()

    def test_legacy_pause_without_reason_stays_paused(self):
        self.queue.set_metadata(gmail_app.PAUSED_KEY, "1")

        self.app._prepare_startup()

        self.assertTrue(self.app.paused())
        self.app.controller.start.assert_not_called()
        self.assertEqual(gmail_app.PAUSE_STARTUP_NOTICE, self.app._startup_notice)

    def test_resume_clears_reason(self):
        self.app.toggle_pause()
        self.assertEqual("user", self.queue.get_metadata(gmail_app.PAUSE_REASON_KEY))

        self.app.toggle_pause()

        self.assertFalse(self.app.paused())
        self.assertIsNone(self.queue.get_metadata(gmail_app.PAUSE_REASON_KEY))

    def test_health_tick_paused_without_failures_shows_plain_paused_title(self):
        self.app.toggle_pause()
        self.app.tray = mock.Mock()

        self.app._health_tick()

        self.assertEqual("Gmail Attachment Downloader - 一時停止中", self.app.tray.title)
        self.assertEqual(self.app.normal_icon, self.app.tray.icon)
        self.app.controller.start.assert_not_called()

    def test_health_tick_paused_with_failures_keeps_error_icon_and_count(self):
        """A pause must not hide pre-existing failed jobs (regression: paused
        used to always force the normal icon/plain title, even with failures
        that showed the error icon and count before the pause)."""
        self.app.toggle_pause()
        self._add_failed_job()
        self.app.tray = mock.Mock()

        self.app._health_tick()

        self.assertEqual(
            "Gmail Attachment Downloader - 一時停止中 / 失敗 1件", self.app.tray.title
        )
        self.assertEqual(self.app.error_icon, self.app.tray.icon)
        self.app.controller.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
