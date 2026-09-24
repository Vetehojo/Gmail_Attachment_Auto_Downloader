"""Settings dialog Save: validate first, then marker -> stop -> commit
(config.ini last) -> restart; the watchdog and the tray's health tick honor
the marker. Paths go to a temp folder; the monitor controller is a mock."""
import contextlib
import os
import re
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

import app_settings
import gmail_app
import gmail_auth
import gmail_monitor
import runtime_state
import watchdog
from job_queue import JobQueue
from tests.settings_support import FakeButton, FakeCreds, FakeWin, LiveStateTestBase, ProfileService, Root, Var


class SaveTestBase(LiveStateTestBase):
    def setUp(self):
        super().setUp()
        self.events = []
        self.queue = JobQueue(self.queue_db)
        self.controller = mock.Mock()
        self.controller.stop_all.side_effect = self._stop_all
        self.controller.start.side_effect = self._start
        self.start_result = True
        self.stop_result = True
        self.paused = False
        real_copy_credentials = app_settings.copy_credentials
        real_copy_service_account = app_settings.copy_service_account
        real_save_settings = app_settings.save_settings

        def record(name, real):
            def wrapper(*args, **kwargs):
                self.events.append(name)
                if name == "save_settings":
                    self.marker_during_config = runtime_state.settings_update_in_progress(self.queue)
                    self.identities_before_config = self.identities()
                return real(*args, **kwargs)
            return wrapper

        patches = [
            mock.patch.object(gmail_app, "copy_credentials", record("copy_credentials", real_copy_credentials)),
            mock.patch.object(gmail_app, "copy_service_account", record("copy_service_account", real_copy_service_account)),
            mock.patch.object(gmail_app, "save_settings", record("save_settings", real_save_settings)),
            mock.patch.object(gmail_auth, "install_oauth_token", side_effect=self._install_token),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _start(self):
        self.events.append("start")
        if isinstance(self.start_result, Exception):
            raise self.start_result
        return self.start_result

    def _stop_all(self):
        self.events.append(("stop_all", runtime_state.settings_update_in_progress(self.queue)))
        return self.stop_result

    def _install_token(self, creds):
        self.events.append("install_token")
        self.write(self.token, creds.to_json())

    def identities(self):
        with contextlib.closing(sqlite3.connect(self.queue_db)) as conn:
            rows = conn.execute(
                "SELECT key, value FROM metadata WHERE key LIKE ?", (runtime_state.IDENTITY_KEY_PREFIX + "%",)
            ).fetchall()
        return {key[len(runtime_state.IDENTITY_KEY_PREFIX):]: value for key, value in rows}

    def make_dialog(self, **values):
        dialog = gmail_app.SettingsDialog.__new__(gmail_app.SettingsDialog)
        dialog.app = mock.Mock()
        dialog.app.root = Root()
        dialog.app.controller = self.controller
        dialog.app.paused.side_effect = lambda: self.paused
        dialog.win = FakeWin()
        dialog.initial = values.get("initial", False)
        dialog.setup_only = values.get("setup_only", False)
        dialog.settings = app_settings.load_settings()
        dialog.auth_mode = Var(values.get("auth_mode", "oauth"))
        dialog.email = Var(values.get("email", self.TARGET))
        dialog.final = Var(values.get("final", self.new_final))
        dialog.credentials = Var(values.get("credentials", self.installed_client))
        dialog.service_account = Var(values.get("service_account", self.installed_sa))
        dialog.dwd_accounts = values.get("dwd_accounts", [])
        dialog.template = Var("{original}_{date}_{subject}_{sender}")
        dialog.subject_max = Var("50")
        dialog.excluded_labels = Var("")
        dialog.allowed_extensions = Var(values.get("allowed_extensions", ".pdf"))
        dialog.period = Var(values.get("period", "過去7日"))
        dialog.custom_date = Var("")
        dialog.force_login = Var(False)
        dialog._staged = values.get("staged")
        dialog._closed = False
        dialog._test_running = False
        dialog._test_generation = 0
        dialog.save_button = FakeButton()
        dialog.oauth_test_button = FakeButton()
        dialog.dwd_test_button = FakeButton()
        return dialog

    def staged_login(self, client, email, identity):
        return {
            "mode": "oauth",
            "client": client,
            "email": email,
            "creds": FakeCreds(identity),
            "identities": {email: identity},
        }


class SaveOrderTest(SaveTestBase):
    def test_invalid_values_abort_before_anything_is_stopped(self):
        dialog = self.make_dialog(allowed_extensions="")
        before = self.snapshot()

        dialog.save()

        self.controller.stop_all.assert_not_called()
        self.controller.start.assert_not_called()
        self.assertEqual(before, self.snapshot())
        self.assertIn("拡張子", gmail_app.messagebox.showerror.call_args.args[1])
        self.assertFalse(dialog._closed)

    def test_missing_client_file_aborts_before_anything_is_stopped(self):
        dialog = self.make_dialog(credentials=os.path.join(self.downloads, "missing.json"))
        before = self.snapshot()

        dialog.save()

        self.controller.stop_all.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_save_stops_commits_config_last_and_restarts(self):
        client = self.downloaded_client()
        email = "login@example.com"
        dialog = self.make_dialog(
            credentials=client, email=email, staged=self.staged_login(client, email, email),
        )

        dialog.save()

        self.assertEqual(
            [("stop_all", True), "copy_credentials", "install_token", "save_settings", "start"],
            self.events,
        )
        self.assertTrue(self.marker_during_config)
        self.assertEqual({email: email}, self.identities_before_config)
        self.assertFalse(runtime_state.settings_update_in_progress(self.queue))
        self.assertIsNone(self.queue.get_metadata(runtime_state.SETTINGS_UPDATE_KEY))
        settings = app_settings.load_settings()
        self.assertEqual(email, settings["target_email"])
        self.assertEqual(os.path.abspath(self.new_final), settings["final_dir"])
        self.assertTrue(os.path.isdir(self.new_final))
        with open(self.installed_client, encoding="utf-8") as handle:
            self.assertIn('"new"', handle.read())
        with open(self.token, encoding="utf-8") as handle:
            self.assertIn(email, handle.read())
        self.assertTrue(dialog._closed)
        self.assertIsNone(dialog._staged)
        gmail_app.messagebox.askyesno.assert_not_called()
        gmail_app.messagebox.showwarning.assert_not_called()

    def test_save_without_a_login_keeps_the_live_token(self):
        dialog = self.make_dialog(final=self.final)

        dialog.save()

        self.assertNotIn("install_token", self.events)
        with open(self.token, encoding="utf-8") as handle:
            self.assertEqual('{"token": "live"}', handle.read())
        self.assertEqual("start", self.events[-1])
        # Same auth mode, account configured before: stays trust-on-first-use.
        self.assertEqual({}, self.identities())

    def test_auth_mode_switch_trusts_no_account_on_first_use(self):
        # A legacy DWD install (no stored identity) switched to OAuth without a
        # test: the old token.json may open another mailbox, so the account
        # must be refused until it is tested and saved.
        app_settings.save_settings({
            "auth_mode": "dwd",
            "dwd_accounts": app_settings.encode_dwd_accounts([{"email": self.TARGET, "final_dir": self.final}]),
        })
        dialog = self.make_dialog(final=self.final)

        dialog.save()

        self.assertEqual({self.TARGET: ""}, self.identities())
        self.assertIn(f"接続テストで確認していないアカウントがあります: {self.TARGET}",
                      gmail_app.messagebox.showinfo.call_args.args[1])
        with mock.patch.object(gmail_monitor, "log"):
            with self.assertRaises(gmail_monitor.AuthenticationRequiredError):
                gmail_monitor.verify_mailbox_identity(self.queue, self.TARGET, ProfileService("other@example.com"))

    def test_alias_mismatch_is_confirmed_before_anything_is_stopped(self):
        client = self.downloaded_client()
        staged = self.staged_login(client, "info@example.com", "owner@example.com")
        gmail_app.messagebox.askyesno.return_value = False
        dialog = self.make_dialog(credentials=client, email="info@example.com", staged=staged)
        before = self.snapshot()

        dialog.save()

        text = gmail_app.messagebox.askyesno.call_args.args[1]
        self.assertIn("info@example.com", text)
        self.assertIn("owner@example.com", text)
        self.controller.stop_all.assert_not_called()
        self.assertEqual([], self.events)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(dialog._closed)
        self.assertIs(staged, dialog._staged)

        gmail_app.messagebox.askyesno.return_value = True
        dialog.save()

        self.assertEqual({"info@example.com": "owner@example.com"}, self.identities())
        self.assertEqual("start", self.events[-1])
        self.assertTrue(dialog._closed)

    def test_failed_restart_after_a_commit_is_reported_as_saved(self):
        for failure in (False, OSError("spawn failed")):
            with self.subTest(failure=failure):
                self.events.clear()
                gmail_app.messagebox.reset_mock()
                self.start_result = failure
                dialog = self.make_dialog(final=self.new_final)

                dialog.save()

                self.assertEqual("save_settings", self.events[-2])
                self.assertEqual("start", self.events[-1])
                gmail_app.messagebox.showerror.assert_not_called()
                text = gmail_app.messagebox.showwarning.call_args.args[1]
                self.assertTrue(text.startswith("設定を保存しました。"))
                self.assertIn(gmail_app.START_FAILED_TEXT, text)
                if isinstance(failure, Exception):
                    self.assertIn("spawn failed", text)
                self.assertTrue(dialog._closed)
                self.assertEqual(os.path.abspath(self.new_final), app_settings.load_settings()["final_dir"])

    def test_stale_stage_is_not_committed(self):
        client = self.downloaded_client()
        staged = self.staged_login(client, "login@example.com", "login@example.com")
        dialog = self.make_dialog(credentials=client, email="someone@example.com", staged=staged)

        dialog.save()

        self.assertNotIn("install_token", self.events)
        self.assertNotIn("someone@example.com", {k for k, v in self.identities().items() if v})

    def test_stop_failure_aborts_without_writing_anything(self):
        self.stop_result = False
        client = self.downloaded_client()
        dialog = self.make_dialog(
            credentials=client, email="login@example.com",
            staged=self.staged_login(client, "login@example.com", "login@example.com"),
        )
        before = self.snapshot()
        metadata_before = self.metadata()

        dialog.save()

        self.assertEqual([("stop_all", True)], self.events)
        self.assertEqual(metadata_before, self.metadata())
        # state\ holds the queue (compared above); log\ gets the abort line.
        self.assertEqual(
            {k: v for k, v in before.items() if not k.startswith(("state", "log"))},
            {k: v for k, v in self.snapshot().items() if not k.startswith(("state", "log"))},
        )
        self.assertEqual(gmail_app.STOP_FAILED_TEXT, gmail_app.messagebox.showerror.call_args.args[1])
        self.assertIn("特定できなかったか、停止できなかった", gmail_app.STOP_FAILED_TEXT)
        self.assertNotIn("しばらく待って", gmail_app.STOP_FAILED_TEXT)
        self.assertFalse(dialog._closed)
        self.assertIsNotNone(dialog._staged)

    def test_monitor_stop_failure_cause_is_logged(self):
        integration = gmail_app.windows_integration
        with mock.patch.object(gmail_app.os, "name", "nt"), \
             mock.patch.object(integration, "Runner"), \
             mock.patch.object(integration, "stop_owned_monitors",
                               side_effect=integration.IntegrationError("identity changed")):
            self.assertFalse(gmail_app.MonitorController().stop_all())
        with open(os.path.join(self.root, "log", "tray_log.txt"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Monitor stop failed: identity changed", text)
        # stop_all also serves pause, exit and 今すぐGmailを確認: a neutral label.
        self.assertIn("- monitor: Monitor stop failed", text)

    def test_monitor_stop_os_error_is_logged_and_reported_as_not_stopped(self):
        integration = gmail_app.windows_integration
        with mock.patch.object(gmail_app.os, "name", "nt"), \
             mock.patch.object(integration, "Runner"), \
             mock.patch.object(integration, "stop_owned_monitors",
                               side_effect=FileNotFoundError(2, "powershell.exe not found")):
            self.assertFalse(gmail_app.MonitorController().stop_all())
        with open(os.path.join(self.root, "log", "tray_log.txt"), encoding="utf-8") as handle:
            self.assertIn("- monitor: Monitor stop failed: [Errno 2] powershell.exe not found", handle.read())

    def test_commit_failure_restarts_and_reports(self):
        dialog = self.make_dialog(final=self.final)
        with mock.patch.object(gmail_app, "copy_credentials", side_effect=OSError("disk gone")):
            dialog.save()

        self.assertEqual([("stop_all", True), "start"], self.events)
        self.assertIsNone(self.queue.get_metadata(runtime_state.SETTINGS_UPDATE_KEY))
        message = gmail_app.messagebox.showerror.call_args.args[1]
        self.assertIn("disk gone", message)
        self.assertIn("自動取得は再開しています", message)
        self.assertFalse(dialog._closed)
        self.assertEqual(self.TARGET, app_settings.load_settings()["target_email"])

    def test_commit_and_restart_failures_are_both_reported(self):
        self.start_result = False
        dialog = self.make_dialog(final=self.final)
        with mock.patch.object(gmail_app, "copy_credentials", side_effect=OSError("disk gone")):
            dialog.save()

        message = gmail_app.messagebox.showerror.call_args.args[1]
        self.assertIn("disk gone", message)
        self.assertIn("自動取得（監視プロセス）も開始できませんでした", message)
        self.assertNotIn("自動取得は再開しています", message)

    def test_paused_save_stops_and_commits_without_restarting(self):
        self.paused = True
        dialog = self.make_dialog(final=self.final)

        dialog.save()

        self.assertEqual([("stop_all", True), "copy_credentials", "save_settings"], self.events)

    def test_setup_only_uses_the_same_stop_and_commit_path_without_starting(self):
        dialog = self.make_dialog(setup_only=True, final=self.final)
        dialog.app.root = mock.Mock()

        dialog.save()

        self.assertEqual([("stop_all", True), "copy_credentials", "save_settings"], self.events)
        dialog.app.root.quit.assert_called_once()

    def test_initial_save_writes_the_scan_start_cursor_before_config(self):
        dialog = self.make_dialog(initial=True, period="過去3日", final=self.final)
        cursors = []
        real = gmail_app.save_settings

        def save_settings(values):
            cursors.append(self.queue.get_metadata(gmail_app.MAIL_CURSOR_KEY))
            return real(values)

        with mock.patch.object(gmail_app, "save_settings", side_effect=save_settings), \
             mock.patch.object(gmail_app, "is_configured", return_value=True):
            dialog.save()

        expected = (datetime.now() - timedelta(days=3) + timedelta(minutes=5)).timestamp()
        self.assertAlmostEqual(expected, float(cursors[0]), delta=60)

    def test_future_scan_start_is_rejected_before_stopping(self):
        dialog = self.make_dialog(initial=True, period="日付指定", final=self.final)
        dialog.custom_date = Var((datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d"))

        dialog.save()

        self.controller.stop_all.assert_not_called()
        self.assertIn("未来", gmail_app.messagebox.showerror.call_args.args[1])

    def test_dwd_save_records_verified_and_untested_accounts(self):
        selected = os.path.join(self.downloads, "sa.json")
        self.write(selected, '{"type": "service_account", "client_email": "new"}')
        accounts = [
            {"email": "x@example.com", "final_dir": os.path.join(self.root, "X")},
            {"email": "y@example.com", "final_dir": os.path.join(self.root, "Y")},
        ]
        staged = {"mode": "dwd", "service_account": selected, "identities": {"x@example.com": "x@example.com"}}
        dialog = self.make_dialog(auth_mode="dwd", service_account=selected, dwd_accounts=accounts, staged=staged)

        dialog.save()

        self.assertEqual([("stop_all", True), "copy_service_account", "save_settings", "start"], self.events)
        self.assertEqual({"x@example.com": "x@example.com", "y@example.com": ""}, self.identities())
        text = gmail_app.messagebox.showinfo.call_args.args[1]
        self.assertIn("接続テストで確認していないアカウントがあります: y@example.com", text)
        settings = app_settings.load_settings()
        self.assertEqual("dwd", settings["auth_mode"])
        self.assertEqual(["x@example.com", "y@example.com"],
                         [a["email"] for a in app_settings.get_account_configs(settings)])

    def test_save_clears_recorded_auth_issues(self):
        runtime_state.set_auth_issue(self.queue, self.TARGET, "token expired")
        dialog = self.make_dialog(final=self.final)

        dialog.save()

        self.assertEqual({}, runtime_state.read_auth_issues(self.queue))

    def metadata(self):
        with contextlib.closing(sqlite3.connect(self.queue_db)) as conn:
            return conn.execute("SELECT key, value, updated_at FROM metadata ORDER BY key").fetchall()


class MailboxCursorTest(SaveTestBase):
    """OAuth mail cursors per recorded mailbox across Saves, rescans and scans
    (runtime_state.MAIL_CURSOR_KEY). LiveStateTestBase is an older version's
    install: OAuth a@example.com, no mailbox recorded, the bare cursor key."""

    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(gmail_monitor, "log"), mock.patch.object(gmail_monitor, "write_heartbeat")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def cursor(self, mailbox=None):
        return self.queue.get_metadata(gmail_app.MAIL_CURSOR_KEY + (f":{mailbox}" if mailbox else ""))

    def record(self, account, mailbox, checked=None):
        """`account` scanned before: `mailbox` recorded, its cursor at `checked`."""
        self.queue.delete_metadata(gmail_app.MAIL_CURSOR_KEY)
        self.queue.set_metadata(runtime_state.identity_key(account), mailbox)
        if checked is not None:
            self.queue.set_metadata(f"{gmail_app.MAIL_CURSOR_KEY}:{mailbox}", checked.timestamp())

    def first_scan(self, account):
        """Where the next scan of `account` under the saved settings starts."""
        seen = {}

        def ids(_service, query, _account):
            seen["query"] = query
            return iter([])

        with mock.patch.object(gmail_monitor, "iter_message_ids", side_effect=ids):
            gmail_monitor.scan_gmail(self.queue, object(), account, self.final)
        return datetime.fromtimestamp(int(re.search(r"after:(\d+)", seen["query"]).group(1)))

    def login_dialog(self, email, mailbox, **values):
        client = self.downloaded_client()
        return self.make_dialog(
            credentials=client, email=email, final=self.final, staged=self.staged_login(client, email, mailbox), **values
        )

    @staticmethod
    def yesterday():
        return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)

    def test_mailbox_change_scans_the_new_mailbox_from_lookback_days(self):
        # External review scenario: a@ was just checked, then the user logs in
        # to mailbox b@ and saves. b@'s mail from yesterday must be in its
        # first scan, not skipped by a@'s cursor.
        self.record(self.TARGET, self.TARGET)
        self.first_scan(self.TARGET)
        checked = self.cursor(self.TARGET)

        self.login_dialog("b@example.com", "b@example.com").save()

        start = self.first_scan("b@example.com")
        self.assertLessEqual(start, self.yesterday())
        self.assertAlmostEqual((datetime.now() - timedelta(days=7)).timestamp(), start.timestamp(), delta=60)
        self.assertIsNotNone(checked)
        self.assertEqual(checked, self.cursor(self.TARGET))

    def test_relogin_to_another_mailbox_does_not_inherit_an_older_versions_cursor(self):
        # An older version kept a@'s cursor under the bare key; the typed
        # address stays a@ while the login changes to mailbox b@.
        self.record(self.TARGET, self.TARGET)
        self.queue.set_metadata(gmail_app.MAIL_CURSOR_KEY, (datetime.now() - timedelta(hours=1)).timestamp())
        gmail_app.messagebox.askyesno.return_value = True

        self.login_dialog(self.TARGET, "b@example.com").save()

        self.assertIsNone(self.cursor())
        self.assertLessEqual(self.first_scan(self.TARGET), self.yesterday())
        self.assertIsNotNone(self.cursor("b@example.com"))

    def test_alias_of_the_same_mailbox_keeps_its_cursor(self):
        checked = datetime.now() - timedelta(hours=1)
        self.record(self.TARGET, "owner@example.com", checked)
        gmail_app.messagebox.askyesno.return_value = True

        self.login_dialog("info@example.com", "owner@example.com").save()

        start = self.first_scan("info@example.com")
        self.assertEqual(int((checked - gmail_monitor.QUERY_OVERLAP).timestamp()), int(start.timestamp()))

    def test_auth_mode_round_trip_passes_no_cursor_to_another_mailbox(self):
        # OAuth a@ recorded on first use but not scanned yet: its cursor is
        # still the pending bare key. b@ was recorded before, so switching to
        # DWD writes no identity and only the mode change drops the cursor.
        self.queue.set_metadata(runtime_state.identity_key(self.TARGET), self.TARGET)
        self.queue.set_metadata(runtime_state.identity_key("b@example.com"), "b@example.com")
        self.make_dialog(auth_mode="dwd", dwd_accounts=[{"email": "b@example.com", "final_dir": self.final}]).save()
        self.assertEqual("dwd", app_settings.load_settings()["auth_mode"])
        self.assertEqual({self.TARGET: self.TARGET, "b@example.com": "b@example.com"}, self.identities())
        self.assertIsNone(self.cursor())
        # DWD checked the typed address b@; back in OAuth, b@ logs in to
        # mailbox mailbox-b@ (b@ being its alias), which has no cursor yet.
        self.queue.set_metadata(f"{gmail_app.MAIL_CURSOR_KEY}:b@example.com", time.time())
        gmail_app.messagebox.askyesno.return_value = True

        self.login_dialog("b@example.com", "mailbox-b@example.com").save()

        self.assertIsNone(self.cursor())
        start = self.first_scan("b@example.com")
        self.assertAlmostEqual((datetime.now() - timedelta(days=7)).timestamp(), start.timestamp(), delta=60)

    def test_untested_initial_period_waits_for_the_mailbox_verified_later(self):
        with mock.patch.object(gmail_app, "is_configured", return_value=True):
            self.make_dialog(initial=True, period="過去3日", email="new@example.com", final=self.final).save()
        self.assertEqual({"new@example.com": ""}, self.identities())
        chosen = datetime.now() - timedelta(days=3)

        self.login_dialog("new@example.com", "new@example.com").save()

        self.assertAlmostEqual(chosen.timestamp(), self.first_scan("new@example.com").timestamp(), delta=60)
        self.assertIsNone(self.cursor())

    def test_rescan_before_an_older_installs_first_scan_is_kept_through_first_use(self):
        app = gmail_app.TrayApp.__new__(gmail_app.TrayApp)
        app.controller = self.controller
        chosen = datetime.now() - timedelta(days=20)

        self.assertTrue(app.apply_scan_start(chosen))
        gmail_monitor.verify_mailbox_identity(self.queue, self.TARGET, ProfileService(self.TARGET))

        self.assertEqual([("stop_all", True)], self.events)
        self.assertAlmostEqual(chosen.timestamp(), self.first_scan(self.TARGET).timestamp(), delta=2)
        self.assertIsNone(self.cursor())

    def test_rescan_for_a_new_untested_alias_is_honored_after_test_and_save(self):
        # Mailbox m@ was checked an hour ago under alias a1@. The user types a
        # new alias a2@ without a test, rescans from 10 days ago, then tests
        # and saves a2@ -> m@: the first scan starts at the rescan date.
        self.record("a1@example.com", "m@example.com", datetime.now() - timedelta(hours=1))
        self.make_dialog(email="a2@example.com", final=self.final).save()
        self.assertEqual("", self.identities()["a2@example.com"])
        app = gmail_app.TrayApp.__new__(gmail_app.TrayApp)
        app.controller = self.controller
        chosen = datetime.now() - timedelta(days=10)
        self.assertTrue(app.apply_scan_start(chosen))
        gmail_app.messagebox.askyesno.return_value = True

        self.login_dialog("a2@example.com", "m@example.com").save()

        start = self.first_scan("a2@example.com")
        self.assertAlmostEqual((chosen + timedelta(minutes=5)).timestamp(),
                               (start + gmail_monitor.QUERY_OVERLAP).timestamp(), delta=2)
        self.assertIsNone(self.cursor())

    def test_mailbox_change_with_a_period_writes_it_for_the_new_mailbox_only(self):
        checked = datetime.now() - timedelta(hours=1)
        self.record(self.TARGET, self.TARGET, checked)
        gmail_app.messagebox.askyesno.return_value = True

        with mock.patch.object(gmail_app, "is_configured", return_value=True):
            self.login_dialog(self.TARGET, "b@example.com", initial=True, period="過去30日").save()

        expected = (datetime.now() - timedelta(days=30) + timedelta(minutes=5)).timestamp()
        self.assertAlmostEqual(expected, float(self.cursor("b@example.com")), delta=60)
        self.assertEqual(str(checked.timestamp()), self.cursor(self.TARGET))
        self.assertIsNone(self.cursor())

    def test_rescan_and_status_use_the_recorded_mailbox_cursor(self):
        summary = gmail_app.TrayApp.__new__(gmail_app.TrayApp)._cursor_summary
        # Nothing recorded yet: the pending cursor.
        self.assertIn("確認済み 1/1アカウント", summary())
        self.queue.set_metadata(runtime_state.identity_key(self.TARGET), "owner@example.com")
        self.assertEqual("未確認（0/1アカウント）", summary())
        chosen = datetime.now() - timedelta(days=2)

        gmail_app.write_scan_start(self.queue, chosen, app_settings.load_settings())

        expected = (chosen + timedelta(minutes=5)).timestamp()
        self.assertAlmostEqual(expected, float(self.cursor("owner@example.com")), delta=1)
        self.assertEqual("1700000000", self.cursor())
        self.assertIn(gmail_app.format_time(expected), summary())

    def test_dwd_cursors_stay_per_typed_address(self):
        app_settings.save_settings({
            "auth_mode": "dwd",
            "dwd_accounts": app_settings.encode_dwd_accounts([
                {"email": "x@example.com", "final_dir": self.final},
                {"email": "y@example.com", "final_dir": self.final},
            ]),
        })
        self.queue.set_metadata(runtime_state.identity_key("x@example.com"), "other@example.com")

        gmail_app.write_scan_start(self.queue, datetime.now() - timedelta(days=2), app_settings.load_settings())

        self.assertIsNotNone(self.cursor("x@example.com"))
        self.assertIsNotNone(self.cursor("y@example.com"))
        self.assertIsNone(self.cursor("other@example.com"))
        self.assertIn("確認済み 2/2アカウント", gmail_app.TrayApp.__new__(gmail_app.TrayApp)._cursor_summary())


class IdentityMismatchTest(unittest.TestCase):
    def test_only_a_different_mailbox_is_a_mismatch(self):
        writes = {
            "info@example.com": "owner@example.com",
            "same@example.com": "Same@Example.com",
            "untested@example.com": "",
        }
        mismatches = gmail_app.identity_mismatches(writes)
        self.assertEqual([("info@example.com", "owner@example.com")], mismatches)
        text = gmail_app.mismatch_confirmation_text(mismatches)
        self.assertIn("入力したメールアドレス: info@example.com", text)
        self.assertIn("確認したメールボックス: owner@example.com", text)
        self.assertIn("「いいえ」を選ぶと保存を中止します。", text)


class PlanIdentityWritesTest(unittest.TestCase):
    def test_rules(self):
        stored = {"old@example.com": "old@example.com", "back@example.com": "primary@example.com"}
        writes = gmail_app.plan_identity_writes(
            ["tested@example.com", "legacy@example.com", "new@example.com", "back@example.com", "old@example.com"],
            ["legacy@example.com", "old@example.com"],
            {"tested@example.com": "primary@example.com"},
            stored.get,
        )
        # Tested: recorded. Legacy (configured before, nothing stored): left for
        # trust on first use. New and untested: refused until tested. Already
        # stored: kept.
        self.assertEqual({"tested@example.com": "primary@example.com", "new@example.com": ""}, writes)


class SettingsUpdateMarkerTest(unittest.TestCase):
    def setUp(self):
        self.queue = mock.Mock()
        self.store = {}
        self.queue.get_metadata.side_effect = lambda key, default=None: self.store.get(key, default)
        self.queue.set_metadata.side_effect = lambda key, value: self.store.__setitem__(key, str(value))
        self.queue.delete_metadata.side_effect = lambda key: self.store.pop(key, None)

    def test_marker_is_short_lived(self):
        runtime_state.begin_settings_update(self.queue, now=1000)
        self.assertTrue(runtime_state.settings_update_in_progress(self.queue, now=1001))
        self.assertFalse(runtime_state.settings_update_in_progress(
            self.queue, now=1000 + runtime_state.SETTINGS_UPDATE_SECONDS + 1))
        runtime_state.end_settings_update(self.queue)
        self.assertFalse(runtime_state.settings_update_in_progress(self.queue, now=1001))

    def test_a_marker_too_far_ahead_is_ignored(self):
        self.store[runtime_state.SETTINGS_UPDATE_KEY] = str(1000 + 10 * runtime_state.SETTINGS_UPDATE_SECONDS)
        self.assertFalse(runtime_state.settings_update_in_progress(self.queue, now=1000))
        self.store[runtime_state.SETTINGS_UPDATE_KEY] = "garbage"
        self.assertFalse(runtime_state.settings_update_in_progress(self.queue, now=1000))


class WatchdogSettingsUpdateTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = temp.name
        self.db = os.path.join(self.temp, "jobs.sqlite3")
        runtime_state.begin_settings_update(JobQueue(self.db))
        for patcher in (
            mock.patch.object(watchdog, "QUEUE_DB", self.db),
            mock.patch.object(watchdog, "STATE_DIR", self.temp),
            mock.patch.object(watchdog, "STATE_FILE", os.path.join(self.temp, "watchdog_state.json")),
            mock.patch.object(watchdog, "WATCHDOG_LOG", os.path.join(self.temp, "watchdog_log.txt")),
            mock.patch.object(watchdog, "notify"),
            mock.patch.object(watchdog, "event_log"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_main_does_not_start_or_stop_the_monitor(self):
        with mock.patch.object(watchdog, "monitor_pids", side_effect=AssertionError("probed")), \
             mock.patch.object(watchdog, "start_monitor") as start, \
             mock.patch.object(watchdog, "stop_monitor") as stop:
            self.assertEqual(0, watchdog.main())
        start.assert_not_called()
        stop.assert_not_called()

    def test_restart_is_suppressed(self):
        state = {"restart_times": [], "stale_count": 2}
        with mock.patch.object(watchdog, "start_monitor") as start, \
             mock.patch.object(watchdog, "stop_monitor") as stop:
            watchdog.perform_restart(state, "test", time.time(), kill_existing=True)
        start.assert_not_called()
        stop.assert_not_called()
        self.assertEqual(0, state["stale_count"])


class TrayHealthTickTest(unittest.TestCase):
    """Same isolation as test_pause_reason: temp queue DB, mocked controller,
    tray and messagebox; the withdrawn Tk root is the only Tk object."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db_path = os.path.join(temp.name, "jobs.sqlite3")
        self.addCleanup(lambda: setattr(gmail_app, "_QUEUE_INSTANCE", None))
        for patcher in (
            mock.patch.object(gmail_app, "QUEUE_DB", self.db_path),
            mock.patch.object(gmail_app, "_QUEUE_INSTANCE", None),
            mock.patch.object(gmail_app, "HEARTBEAT_FILE", os.path.join(temp.name, "heartbeat.json")),
            mock.patch.object(gmail_app, "messagebox"),
            mock.patch.object(gmail_app, "is_configured", return_value=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.queue = JobQueue(self.db_path)
        self.app = gmail_app.TrayApp()
        self.app.controller = mock.Mock()
        self.app.normal_icon = "normal-icon"
        self.app.error_icon = "error-icon"
        self.app.tray = mock.Mock()
        self.addCleanup(self.app.root.destroy)

    def test_no_monitor_start_during_a_settings_update(self):
        runtime_state.begin_settings_update(self.queue)
        self.app._health_tick()
        self.app.controller.start.assert_not_called()

        runtime_state.end_settings_update(self.queue)
        self.app._health_tick()
        self.app.controller.start.assert_called_once()

    def test_auth_deferral_shows_as_needs_attention_until_cleared(self):
        runtime_state.set_auth_issue(self.queue, "a@example.com", "mailbox mismatch")
        self.app._health_tick()
        self.assertEqual("error-icon", self.app.tray.icon)
        self.app.tray.notify.assert_called_once()
        self.assertIn("mailbox mismatch", self.app.tray.notify.call_args.args[0])

        runtime_state.clear_auth_issue(self.queue, "a@example.com")
        self.app._health_tick()
        self.assertEqual("normal-icon", self.app.tray.icon)


if __name__ == "__main__":
    unittest.main()
