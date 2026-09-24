"""Pause, exit, 期間を指定して再確認 and 今すぐGmailを確認 honor the monitor
controller's stop/start results. A configured install in a temp folder
(LiveStateTestBase); the controller is a mock and the rescan dialog's widgets
are mocked, so no monitor process or real dialog runs."""
import contextlib
import os
import sqlite3
from datetime import datetime, timedelta
from unittest import mock

import gmail_app
import runtime_state
import watchdog
from job_queue import JobQueue
from tests.settings_support import LiveStateTestBase


class StopResultTest(LiveStateTestBase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(gmail_app, "HEARTBEAT_FILE", os.path.join(self.state, "heartbeat.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.queue = JobQueue(self.queue_db)
        self.app = gmail_app.TrayApp()
        self.addCleanup(self.app.root.destroy)
        self.app.controller = mock.Mock()
        self.app.controller.stop_all.return_value = True
        self.app.controller.start.return_value = True
        self.app.controller.restart.return_value = True
        self.messagebox = gmail_app.messagebox

    def metadata(self):
        with contextlib.closing(sqlite3.connect(self.queue_db)) as conn:
            return conn.execute("SELECT key, value, updated_at FROM metadata ORDER BY key").fetchall()

    def run_rescan(self, start):
        """Open 期間を指定して再確認 with mocked widgets and press 実行; the dialog window."""
        with mock.patch.object(gmail_app, "tk") as tk, mock.patch.object(gmail_app, "ttk") as ttk, \
             mock.patch.object(gmail_app, "center_on_parent"), \
             mock.patch.object(gmail_app, "parse_scan_start", return_value=start):
            self.app.show_rescan_dialog()
            run = next(c.kwargs["command"] for c in ttk.Button.call_args_list if c.kwargs.get("text") == "実行")
            run()
        return tk.Toplevel.return_value

    def test_pause_is_undone_when_the_monitor_cannot_be_stopped(self):
        paused_while_stopping = []

        def stop_all():
            paused_while_stopping.append(self.app.paused())
            return False

        self.app.controller.stop_all.side_effect = stop_all

        self.app.toggle_pause()

        # Paused while stopping, so the watchdog stays idle meanwhile.
        self.assertEqual([True], paused_while_stopping)
        self.assertFalse(self.app.paused())
        self.assertIsNone(self.queue.get_metadata(gmail_app.PAUSE_REASON_KEY))
        self.assertEqual(gmail_app.PAUSE_STOP_FAILED_TEXT, self.messagebox.showerror.call_args.args[1])
        self.assertIn("一時停止しませんでした", gmail_app.PAUSE_STOP_FAILED_TEXT)
        self.messagebox.showinfo.assert_not_called()

    def test_resume_warns_when_the_monitor_cannot_be_started(self):
        self.app.toggle_pause()
        self.messagebox.reset_mock()
        self.app.controller.start.return_value = False

        self.app.toggle_pause()

        self.assertFalse(self.app.paused())
        text = self.messagebox.showwarning.call_args.args[1]
        self.assertTrue(text.startswith("自動取得を再開しました。"))
        self.assertIn(gmail_app.START_FAILED_TEXT, text)
        self.messagebox.showinfo.assert_not_called()

    def test_exit_warns_when_the_monitor_cannot_be_stopped_and_still_exits(self):
        self.app.controller.stop_all.return_value = False
        self.app.tray = mock.Mock()

        with mock.patch.object(self.app.root, "quit") as quit_:
            self.app.exit_app()

        self.assertEqual(gmail_app.EXIT_STOP_FAILED_TEXT, self.messagebox.showwarning.call_args.args[1])
        self.assertTrue(self.app.paused())
        self.assertEqual("exit", self.queue.get_metadata(gmail_app.PAUSE_REASON_KEY))
        self.app.tray.stop.assert_called_once()
        quit_.assert_called_once()

    def test_rescan_writes_nothing_when_the_monitor_cannot_be_stopped(self):
        self.app.controller.stop_all.return_value = False
        before = self.metadata()

        win = self.run_rescan(datetime.now() - timedelta(days=3))

        self.assertEqual(before, self.metadata())
        self.assertEqual(
            ("再確認できませんでした", gmail_app.RESCAN_STOP_FAILED_TEXT), self.messagebox.showerror.call_args.args
        )
        self.app.controller.start.assert_not_called()
        self.messagebox.showinfo.assert_not_called()
        win.destroy.assert_not_called()

    def test_rescan_holds_the_marker_so_nothing_restarts_the_monitor_meanwhile(self):
        self.queue.set_metadata(runtime_state.identity_key(self.TARGET), self.TARGET)
        seen = []

        def stop_all():
            seen.append(watchdog.is_settings_update())
            self.app._health_tick()  # the tray's own start attempt
            return True

        self.app.controller.stop_all.side_effect = stop_all
        start = datetime.now() - timedelta(days=3)

        with mock.patch.object(watchdog, "QUEUE_DB", self.queue_db):
            win = self.run_rescan(start)

        self.assertEqual([True], seen)
        # Only the rescan's own start, after the cursor was written.
        self.app.controller.start.assert_called_once()
        cursor = self.queue.get_metadata(f"{runtime_state.MAIL_CURSOR_KEY}:{self.TARGET}")
        self.assertAlmostEqual((start + timedelta(minutes=5)).timestamp(), float(cursor), delta=1)
        self.assertIsNone(self.queue.get_metadata(runtime_state.SETTINGS_UPDATE_KEY))
        self.assertEqual("再スキャン", self.messagebox.showinfo.call_args.args[0])
        win.destroy.assert_called_once()

    def test_rescan_write_failure_still_clears_the_marker(self):
        with mock.patch.object(gmail_app, "write_scan_start", side_effect=OSError("disk gone")):
            win = self.run_rescan(datetime.now() - timedelta(days=3))

        self.assertIsNone(self.queue.get_metadata(runtime_state.SETTINGS_UPDATE_KEY))
        self.assertIn("disk gone", self.messagebox.showerror.call_args.args[1])
        self.app.controller.start.assert_not_called()
        win.destroy.assert_not_called()

    def test_rescan_warns_when_the_monitor_cannot_be_started(self):
        self.app.controller.start.return_value = False

        self.run_rescan(datetime.now() - timedelta(days=3))

        text = self.messagebox.showwarning.call_args.args[1]
        self.assertIn("以降を再確認します。", text)
        self.assertIn(gmail_app.START_FAILED_TEXT, text)
        self.messagebox.showinfo.assert_not_called()

    def test_run_now_reports_a_failed_restart(self):
        self.app.controller.restart.return_value = False

        self.app.run_now()

        self.assertEqual(gmail_app.RUN_NOW_FAILED_TEXT, self.messagebox.showerror.call_args.args[1])
        self.messagebox.showinfo.assert_not_called()

        self.messagebox.reset_mock()
        self.app.controller.restart.return_value = True
        self.app.run_now()

        self.assertEqual("Gmail確認を開始しました。", self.messagebox.showinfo.call_args.args[1])
        self.messagebox.showerror.assert_not_called()
