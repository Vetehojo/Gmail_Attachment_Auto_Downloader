import json
import os
import tempfile
import time
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest import mock

import app_settings
import gmail_app
import watchdog
import windows_integration


class FilenamePreviewLayoutTest(unittest.TestCase):
    def test_preview_is_readonly_bounded_entry_inside_expandable_column(self):
        parent = object()
        variable = object()
        widget = mock.Mock()
        with mock.patch.object(gmail_app.ttk, "Entry", return_value=widget) as entry:
            result = gmail_app.build_filename_preview(parent, variable)
        self.assertIs(result, widget)
        entry.assert_called_once_with(parent, textvariable=variable, state="readonly", width=60)
        widget.grid.assert_called_once_with(
            row=4, column=1, columnspan=2, sticky="ew", padx=8, pady=5
        )

    @unittest.skipUnless(os.name == "nt", "Windows Tk layout geometry")
    def test_footer_remains_inside_client_area_across_auth_and_tab_switches(self):
        root = tk.Tk()
        root.geometry("1x1+0+0")
        root.update()
        settings = dict(app_settings.DEFAULTS)
        settings.update({
            "auth_mode": "oauth",
            "target_email": "fixture@example.com",
            "final_dir": os.path.abspath(os.curdir),
        })
        dialog = None
        try:
            with mock.patch.object(gmail_app, "load_settings", return_value=settings):
                dialog = gmail_app.SettingsDialog(
                    SimpleNamespace(root=root), initial=True, setup_only=True
                )
            dialog.win.deiconify()
            dialog.win.geometry("815x508")

            transitions = [
                (gmail_app.AUTH_OAUTH, 0),
                (gmail_app.AUTH_DWD, 0),
                (gmail_app.AUTH_DWD, 1),
                (gmail_app.AUTH_DWD, 2),
                (gmail_app.AUTH_OAUTH, 0),
                (gmail_app.AUTH_OAUTH, 1),
            ]
            for auth_mode, tab_index in transitions:
                with self.subTest(auth_mode=auth_mode, tab_index=tab_index):
                    dialog.auth_mode.set(auth_mode)
                    dialog._refresh_auth_mode()
                    dialog.notebook.select(tab_index)
                    dialog.win.update_idletasks()
                    dialog.win.update()
                    client_bottom = dialog.win.winfo_rooty() + dialog.win.winfo_height()
                    footer_bottom = dialog.footer.winfo_rooty() + dialog.footer.winfo_height()
                    self.assertTrue(dialog.footer.winfo_ismapped())
                    self.assertGreater(dialog.footer.winfo_height(), 1)
                    self.assertLessEqual(footer_bottom, client_bottom)
        finally:
            if dialog is not None and dialog.win.winfo_exists():
                dialog.win.destroy()
            root.destroy()


@unittest.skipUnless(os.name == "nt", "Windows process ownership paths")
class MonitorControllerOwnershipTest(unittest.TestCase):
    def test_unknown_query_blocks_duplicate_start(self):
        controller = gmail_app.MonitorController()
        query = windows_integration.ProcessQuery(
            1,
            "S-1-5-21-100",
            [{
                "ProcessId": "401",
                "Name": "python.exe",
                "CommandLine": f'python.exe "{gmail_app.MONITOR_SCRIPT}"',
                "SessionId": "1",
                "OwnerSid": "",
            }],
        )
        with mock.patch.object(gmail_app, "is_configured", return_value=True), \
             mock.patch.object(
                 gmail_app.windows_integration,
                 "_query_processes",
                 return_value=query,
             ), \
             mock.patch.object(gmail_app.subprocess, "Popen") as popen:
            self.assertFalse(controller.start())
        popen.assert_not_called()

    def test_stop_uses_canonical_shared_helper(self):
        controller = gmail_app.MonitorController()
        with mock.patch.object(
            gmail_app.windows_integration, "stop_owned_monitors", return_value=1
        ) as stop:
            self.assertTrue(controller.stop_all())
        self.assertEqual(gmail_app.MONITOR_SCRIPT, stop.call_args.args[3])

    def test_restart_does_not_start_after_unknown_stop(self):
        controller = gmail_app.MonitorController()
        with mock.patch.object(controller, "stop_all", return_value=False), \
             mock.patch.object(controller, "start") as start:
            self.assertFalse(controller.restart())
        start.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Windows process ownership paths")
class WatchdogOwnershipTest(unittest.TestCase):
    def test_unknown_query_blocks_duplicate_start(self):
        query = windows_integration.ProcessQuery(
            1,
            "S-1-5-21-100",
            [{
                "ProcessId": "402",
                "Name": "pythonw.exe",
                "CommandLine": f'pythonw.exe "{watchdog.MONITOR_SCRIPT}"',
                "SessionId": "1",
                "OwnerSid": "",
            }],
        )
        with mock.patch.object(watchdog.os.path, "exists", return_value=True), \
             mock.patch.object(watchdog, "is_paused", return_value=False), \
             mock.patch.object(
                 watchdog.windows_integration,
                 "_query_processes",
                 return_value=query,
             ), \
             mock.patch.object(watchdog, "log_event"), \
             mock.patch.object(watchdog.subprocess, "Popen") as popen:
            self.assertFalse(watchdog.start_monitor())
        popen.assert_not_called()

    def test_stop_uses_canonical_shared_helper(self):
        with mock.patch.object(
            watchdog.windows_integration, "stop_owned_monitors", return_value=1
        ) as stop:
            self.assertTrue(watchdog.stop_monitor())
        self.assertEqual(watchdog.MONITOR_SCRIPT, stop.call_args.args[3])

    def test_stop_that_cannot_run_is_a_failed_stop(self):
        error = FileNotFoundError(2, "The system cannot find the file specified", "powershell.exe")
        with mock.patch.object(watchdog.windows_integration, "stop_owned_monitors", side_effect=error), \
             mock.patch.object(watchdog, "log_event") as log_event:
            self.assertFalse(watchdog.stop_monitor())
        self.assertIn("Monitor stop failed", log_event.call_args.args[0])

    def test_state_file_is_saved_when_the_stop_cannot_run(self):
        with tempfile.TemporaryDirectory() as root:
            state_dir = os.path.join(root, "state")
            state_file = os.path.join(state_dir, "watchdog_state.json")
            watchdog_log = os.path.join(root, "log", "watchdog_log.txt")
            with mock.patch.object(watchdog, "STATE_DIR", state_dir), \
                 mock.patch.object(watchdog, "QUEUE_DB", os.path.join(state_dir, "jobs.sqlite3")), \
                 mock.patch.object(watchdog, "HEARTBEAT_FILE", os.path.join(state_dir, "heartbeat.json")), \
                 mock.patch.object(watchdog, "STATE_FILE", state_file), \
                 mock.patch.object(watchdog, "WATCHDOG_LOG", watchdog_log), \
                 mock.patch.object(watchdog, "boot_timestamp", return_value=time.time() - 86400), \
                 mock.patch.object(watchdog, "monitor_pids", return_value=[4242, 4343]), \
                 mock.patch.object(watchdog.windows_integration, "stop_owned_monitors",
                                   side_effect=OSError(5, "Access is denied")), \
                 mock.patch.object(watchdog, "start_monitor") as start, \
                 mock.patch.object(watchdog, "notify"), \
                 mock.patch.object(watchdog, "event_log"):
                # Two monitor processes: a restart that kills them first.
                self.assertEqual(0, watchdog.main())
            start.assert_not_called()
            with open(state_file, encoding="utf-8") as handle:
                self.assertIn("last_run", json.load(handle))
            with open(watchdog_log, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("Monitor stop failed", text)
            self.assertIn("restart aborted", text)

    def test_restart_aborts_when_stop_identity_is_unknown(self):
        state = {"restart_times": [], "stale_count": 3}
        with mock.patch.object(watchdog, "is_paused", return_value=False), \
             mock.patch.object(watchdog, "is_settings_update", return_value=False), \
             mock.patch.object(watchdog, "stop_monitor", return_value=False), \
             mock.patch.object(watchdog, "start_monitor") as start, \
             mock.patch.object(watchdog, "log_event"), \
             mock.patch.object(watchdog, "notify"), \
             mock.patch.object(watchdog, "event_log"):
            watchdog.perform_restart(state, "test", 1000, kill_existing=True)
        start.assert_not_called()
        self.assertEqual([], state["restart_times"])
        self.assertEqual(0, state["stale_count"])


if __name__ == "__main__":
    unittest.main()
