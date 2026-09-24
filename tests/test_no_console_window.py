import os
import subprocess
import sys
import unittest
from unittest import mock

import watchdog
import windows_integration as win


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)


def completed(returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@unittest.skipUnless(os.name == "nt", "Windows console creation flags")
class RunnerConsoleWindowTest(unittest.TestCase):
    def test_console_less_host_hides_child_console(self):
        with mock.patch.object(win, "_has_console", return_value=False), \
             mock.patch.object(win.subprocess, "run", return_value=completed(3, b"out", b"err")) as run:
            result = win.Runner().run(["schtasks.exe", "/Query"])
        run.assert_called_once_with(
            ["schtasks.exe", "/Query"],
            capture_output=True,
            check=False,
            creationflags=CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
        )
        self.assertNotEqual(0, CREATE_NO_WINDOW)
        self.assertEqual(win.CommandResult(3, b"out", b"err"), result)

    def test_console_host_keeps_default_creation(self):
        with mock.patch.object(win, "_has_console", return_value=True), \
             mock.patch.object(win.subprocess, "run", return_value=completed()) as run:
            win.Runner().run(["schtasks.exe", "/Query"])
        run.assert_called_once_with(
            ["schtasks.exe", "/Query"],
            capture_output=True,
            check=False,
            creationflags=0,
            stdin=subprocess.DEVNULL,
        )

    def test_console_detection_uses_console_code_page(self):
        import ctypes

        kernel32 = ctypes.windll.kernel32
        with mock.patch.object(kernel32, "GetConsoleCP", return_value=0):
            self.assertFalse(win._has_console())
            self.assertEqual(CREATE_NO_WINDOW, win._creation_flags())
        with mock.patch.object(kernel32, "GetConsoleCP", return_value=65001):
            self.assertTrue(win._has_console())
            self.assertEqual(0, win._creation_flags())

    def test_hidden_child_output_and_exit_code_are_captured(self):
        code = "import sys; sys.stdout.write('out'); sys.stderr.write('err'); sys.exit(3)"
        with mock.patch.object(win, "_has_console", return_value=False):
            result = win.Runner().run([sys.executable, "-c", code])
        self.assertEqual(win.CommandResult(3, b"out", b"err"), result)

    def test_watchdog_process_query_uses_hidden_runner(self):
        listing = (
            "RecordType,ProcessId,Name,CommandLine,SessionId,OwnerSid\r\n"
            "Context,,,,1,S-1-5-21-100\r\n"
        ).encode("utf-8")
        with mock.patch.object(win, "_has_console", return_value=False), \
             mock.patch.object(win.subprocess, "run", return_value=completed(0, listing)) as run:
            self.assertEqual([], watchdog.monitor_pids())
        self.assertEqual(CREATE_NO_WINDOW, run.call_args.kwargs["creationflags"])


class WatchdogConsoleWindowTest(unittest.TestCase):
    def test_msg_eventcreate_and_powershell_hide_console(self):
        with mock.patch.object(watchdog.subprocess, "run", return_value=completed(0, " 42 \n", "")) as run:
            watchdog.notify("hello")
            watchdog.event_log("problem")
            self.assertEqual("42", watchdog.powershell("script"))
        self.assertEqual(["msg", "eventcreate", "powershell"], [c.args[0][0] for c in run.call_args_list])
        for call in run.call_args_list:
            self.assertEqual(CREATE_NO_WINDOW, call.kwargs["creationflags"])
            self.assertFalse(call.kwargs["creationflags"] & DETACHED_PROCESS)
            self.assertTrue(call.kwargs["capture_output"])
        self.assertEqual([10, 10, 20], [c.kwargs["timeout"] for c in run.call_args_list])
        self.assertTrue(run.call_args_list[2].kwargs["text"])

    def test_powershell_failure_still_raises_stderr(self):
        with mock.patch.object(watchdog.subprocess, "run", return_value=completed(1, "", " denied \n")):
            with self.assertRaisesRegex(RuntimeError, "^denied$"):
                watchdog.powershell("script")


if __name__ == "__main__":
    unittest.main()
