"""Folder layout invariants: code lives in app/, runtime data stays at the repo root."""
import os
import re
import unittest

import app_settings
import filename_rules
import gmail_app
import gmail_auth
import gmail_monitor
import watchdog
import windows_integration


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(REPO_ROOT, "app")
# Script paths each bat must pass, exactly as written in the bat.
BAT_SCRIPT_PATHS = {
    "setup.bat": {r"%~dp0app\gmail_app.py", r"%~dp0app\gmail_monitor.py"},
    "trial_download.bat": {r"%~dp0app\gmail_monitor.py"},
    "register_logon_task.bat": {
        r"%~dp0app\gmail_app.py",
        r"%~dp0app\watchdog.py",
        r"%~dp0app\windows_integration.py",
    },
    os.path.join("tools", "start_monitor.bat"): {r"%~dp0..\app\gmail_monitor.py"},
    os.path.join("tools", "stop_monitor.bat"): {
        r"%~dp0..\app\gmail_monitor.py",
        r"%~dp0..\app\windows_integration.py",
    },
}


class RuntimePathLayoutTest(unittest.TestCase):
    def test_base_dir_is_repo_root(self):
        self.assertEqual(os.path.normcase(REPO_ROOT), os.path.normcase(app_settings.BASE_DIR))

    def test_every_runtime_path_is_derived_from_base_dir(self):
        base = app_settings.BASE_DIR
        state = os.path.join(base, "state")
        log = os.path.join(base, "log")
        expected = {
            "app_settings.CONFIG_PATH": (app_settings.CONFIG_PATH, os.path.join(base, "config.ini")),
            "filename_rules._CONFIG_PATH": (filename_rules._CONFIG_PATH, os.path.join(base, "config.ini")),
            "app_settings.STATE_DIR": (app_settings.STATE_DIR, state),
            "gmail_monitor.STATE_DIR": (gmail_monitor.STATE_DIR, state),
            "watchdog.STATE_DIR": (watchdog.STATE_DIR, state),
            "gmail_monitor.QUEUE_DB": (gmail_monitor.QUEUE_DB, os.path.join(state, "jobs.sqlite3")),
            "gmail_app.QUEUE_DB": (gmail_app.QUEUE_DB, os.path.join(state, "jobs.sqlite3")),
            "watchdog.QUEUE_DB": (watchdog.QUEUE_DB, os.path.join(state, "jobs.sqlite3")),
            "gmail_monitor.HEARTBEAT_FILE": (gmail_monitor.HEARTBEAT_FILE, os.path.join(state, "heartbeat.json")),
            "gmail_app.HEARTBEAT_FILE": (gmail_app.HEARTBEAT_FILE, os.path.join(state, "heartbeat.json")),
            "watchdog.HEARTBEAT_FILE": (watchdog.HEARTBEAT_FILE, os.path.join(state, "heartbeat.json")),
            "gmail_monitor.LOCK_FILE": (gmail_monitor.LOCK_FILE, os.path.join(state, "monitor.lock")),
            "gmail_app.APP_LOCK_FILE": (gmail_app.APP_LOCK_FILE, os.path.join(state, "app.lock")),
            "gmail_monitor.TRAY_LOCK_FILE": (gmail_monitor.TRAY_LOCK_FILE, os.path.join(state, "app.lock")),
            "watchdog.STATE_FILE": (watchdog.STATE_FILE, os.path.join(state, "watchdog_state.json")),
            "gmail_monitor.LOG_FILE": (gmail_monitor.LOG_FILE, os.path.join(log, "mail_log.txt")),
            "gmail_app.TRAY_LOG": (gmail_app.TRAY_LOG, os.path.join(log, "tray_log.txt")),
            "watchdog.WATCHDOG_LOG": (watchdog.WATCHDOG_LOG, os.path.join(log, "watchdog_log.txt")),
            "gmail_auth.LEGACY_TOKEN_FILE": (gmail_auth.LEGACY_TOKEN_FILE, os.path.join(base, "token.pickle")),
        }
        for name, (actual, wanted) in expected.items():
            with self.subTest(name):
                self.assertEqual(wanted, actual)

    def test_monitor_script_is_the_app_copy(self):
        wanted = os.path.normcase(os.path.join(APP_DIR, "gmail_monitor.py"))
        for module in (gmail_app, watchdog):
            with self.subTest(module.__name__):
                self.assertEqual(wanted, os.path.normcase(module.MONITOR_SCRIPT))
                self.assertTrue(os.path.isfile(module.MONITOR_SCRIPT))


@unittest.skipUnless(os.name == "nt", "Windows path forms and command lines")
class MonitorPathIdentityTest(unittest.TestCase):
    # %~dp0 expands to the bat's own folder with a trailing backslash.
    ROOT_DP0 = REPO_ROOT + "\\"
    TOOLS_DP0 = os.path.join(REPO_ROOT, "tools") + "\\"

    def test_bat_path_forms_canonicalize_to_the_tray_monitor_path(self):
        canonical = windows_integration._canonical_path
        tray = canonical(gmail_app.MONITOR_SCRIPT)
        self.assertEqual(tray, canonical(watchdog.MONITOR_SCRIPT))
        self.assertEqual(tray, canonical(self.TOOLS_DP0 + r"..\app\gmail_monitor.py"))
        self.assertEqual(tray, canonical(self.ROOT_DP0 + r"app\gmail_monitor.py"))

    def test_start_monitor_bat_process_is_owned(self):
        # Command line as cmd passes it for: python "%MONITOR_PY%"
        command_line = f'python  "{self.TOOLS_DP0}..\\app\\gmail_monitor.py"'
        query = windows_integration.ProcessQuery(1, "S-1-5-21-100", [])
        row = {
            "ProcessId": "501",
            "Name": "python.exe",
            "CommandLine": command_line,
            "SessionId": "1",
            "OwnerSid": "S-1-5-21-100",
        }
        for module in (gmail_app, watchdog):
            with self.subTest(module.__name__):
                self.assertEqual(
                    windows_integration.OWNED,
                    windows_integration.classify_monitor_process(row, module.MONITOR_SCRIPT, query),
                )


class BatScriptPathTest(unittest.TestCase):
    def _read(self, rel):
        with open(os.path.join(REPO_ROOT, rel), "rb") as handle:
            return handle.read()

    def _command_lines(self, rel):
        # One logical command per item: "^" continuations joined, echo/rem skipped.
        text = re.sub(r"\^\r?\n", " ", self._read(rel).decode("ascii"))
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or re.match(r"(?i)(echo|rem)\b", stripped):
                continue
            yield stripped

    def test_bat_set_is_complete(self):
        found = {
            os.path.relpath(os.path.join(folder, name), REPO_ROOT)
            for folder in (REPO_ROOT, os.path.join(REPO_ROOT, "tools"))
            for name in os.listdir(folder)
            if name.lower().endswith(".bat")
        }
        self.assertEqual(set(BAT_SCRIPT_PATHS), found)

    def test_bats_are_ascii_crlf_without_bom(self):
        for rel in BAT_SCRIPT_PATHS:
            with self.subTest(rel):
                data = self._read(rel)
                self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
                data.decode("ascii")
                self.assertNotIn(b"\n", data.replace(b"\r\n", b""))

    def test_bats_pass_quoted_absolute_app_script_paths(self):
        for rel, wanted in BAT_SCRIPT_PATHS.items():
            with self.subTest(rel):
                bat_dir = os.path.dirname(os.path.join(REPO_ROOT, rel))
                found = set()
                for line in self._command_lines(rel):
                    for match in re.finditer(r'[^\s"=]+\.py\b', line):
                        token = match.group(0)
                        self.assertTrue(token.startswith("%~dp0"), f"relative script path: {line}")
                        self.assertEqual(1, line[: match.start()].count('"') % 2, f"unquoted: {line}")
                        found.add(token)
                self.assertEqual(wanted, found)
                for token in found:
                    relative = token[len("%~dp0"):].replace("\\", os.sep)
                    resolved = os.path.normpath(os.path.join(bat_dir, relative))
                    self.assertEqual(os.path.normcase(APP_DIR), os.path.normcase(os.path.dirname(resolved)))
                    self.assertTrue(os.path.isfile(resolved), resolved)

    def test_program_arguments_never_end_with_backslash(self):
        for rel in BAT_SCRIPT_PATHS:
            with self.subTest(rel):
                values = {}

                def expand(text):
                    # A "\ at the end of an argument escapes the quote for Python's argv.
                    text = text.replace("%~dp0", "C:\\install\\")
                    return re.sub(r"%(\w+)%", lambda m: values.get(m.group(1).upper(), m.group(0)), text)

                for line in self._command_lines(rel):
                    assignment = re.match(r'(?i)set "(\w+)=(.*)"$', line)
                    if assignment:
                        values[assignment.group(1).upper()] = expand(assignment.group(2))
                        continue
                    if not re.match(r"(?i)(call\s+)?(python|start)\b", line):
                        continue
                    for quoted in re.findall(r'"([^"]*)"', expand(line)):
                        self.assertFalse(quoted.endswith("\\"), line)

    def test_utf8_mode_only_on_pythonw_lookup(self):
        for rel in BAT_SCRIPT_PATHS:
            with self.subTest(rel):
                for line in self._command_lines(rel):
                    if "-X utf8" in line:
                        self.assertIn("pythonw.exe", line)
                        self.assertNotIn("_PY%", line)

    def test_dp0_is_quoted_in_echo_lines(self):
        # An unquoted path with ")" or "&" breaks an echo inside a ( ) block.
        for rel in BAT_SCRIPT_PATHS:
            with self.subTest(rel):
                for line in self._read(rel).decode("ascii").splitlines():
                    if not re.match(r"(?i)\s*echo\b", line):
                        continue
                    for match in re.finditer(r"%~dp0", line):
                        self.assertEqual(1, line[: match.start()].count('"') % 2, f"unquoted: {line}")

    def test_trial_runs_the_monitor_with_trial_3(self):
        for rel in ("setup.bat", "trial_download.bat"):
            with self.subTest(rel):
                self.assertIn('python "%MONITOR_PY%" --trial 3', list(self._command_lines(rel)))
        self.assertEqual("exit /b %EXIT_CODE%", list(self._command_lines("trial_download.bat"))[-1])

    def test_no_bat_starts_the_tray_directly(self):
        for rel in BAT_SCRIPT_PATHS:
            with self.subTest(rel):
                self.assertEqual([], [line for line in self._command_lines(rel) if re.match(r"(?i)start\b", line)])

    def test_windows_integration_calls_fail_on_any_nonzero_exit_code(self):
        # "if errorlevel 1" is false for the negative exit code of a Ctrl+C
        # or a crash, which would then be reported as success.
        for rel, command in (
            ("register_logon_task.bat", 'python "%INTEGRATION_PY%" register-tasks'),
            (os.path.join("tools", "stop_monitor.bat"), 'python "%INTEGRATION_PY%" stop-monitor'),
        ):
            with self.subTest(rel):
                lines = list(self._command_lines(rel))
                index = next(i for i, line in enumerate(lines) if line.startswith(command))
                self.assertEqual('if not "%ERRORLEVEL%"=="0" goto :END_ERROR', lines[index + 1])

    def test_register_runs_the_registered_monitor_task_only_after_success(self):
        rel = "register_logon_task.bat"
        lines = list(self._command_lines(rel))
        run = '"%SCHTASKS%" /Run /TN "Gmail Auto Downloader Monitor" >nul'
        self.assertIn(r'set "SCHTASKS=%SystemRoot%\System32\schtasks.exe"', lines)
        self.assertEqual(1, sum("/Run" in line for line in lines))
        register = next(i for i, line in enumerate(lines) if line.startswith('python "%INTEGRATION_PY%" register-tasks'))
        # Not "if errorlevel 1": the negative exit code of a Ctrl+C would pass it.
        self.assertEqual('if not "%ERRORLEVEL%"=="0" goto :END_ERROR', lines[register + 1])
        # Only the success path reaches the run: after the check, before :END_OK / :END_ERROR.
        self.assertLess(register + 1, lines.index(run))
        self.assertLess(lines.index(run), lines.index(":END_OK"))
        self.assertLess(lines.index(":END_OK"), lines.index(":END_ERROR"))
        text = self._read(rel).decode("ascii")
        # Only a reported error is rolled back; an interrupted run is not.
        self.assertNotIn("echo Any partial update was rolled back", text)
        self.assertIn("echo If registration reported an error, any partial update was rolled back", text)
        self.assertIn("echo Run this file again to finish or repair the registration.", text)
        self.assertNotIn("IsUserAnAdmin", text)
        # The task XML sets the run level; schtasks /RL is no longer used.
        self.assertNotIn("/RL", text)
        self.assertIn("RunLevel LeastPrivilege", text)
        with open(os.path.join(APP_DIR, "windows_integration.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('"name": "Gmail Auto Downloader Monitor"', source)
        self.assertIn("<RunLevel>LeastPrivilege</RunLevel>", source)


if __name__ == "__main__":
    unittest.main()
