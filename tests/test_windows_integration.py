import csv
import io
import os
import unittest
from unittest import mock
from xml.sax.saxutils import escape

import windows_integration as win


def task_xml(executable, script, description="日本語 & safe"):
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{escape(description)}</Description></RegistrationInfo>
  <Actions Context="Author"><Exec><Command>{escape(executable)}</Command><Arguments>"{escape(script)}"</Arguments></Exec></Actions>
</Task>"""
    return b"\xff\xfe" + xml.encode("utf-16-le")


def task_listing(names):
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\r\n")
    for name in names:
        writer.writerow(["\\" + name.lstrip("\\"), "N/A", "準備完了"])
    return output.getvalue().encode("cp932")


class StatefulTaskRunner(win.Runner):
    def __init__(self, tasks=None, fail_create=None):
        self.tasks = dict(tasks or {})
        self.fail_create = fail_create
        self.commands = []

    def run(self, command):
        self.commands.append(command)
        if "/Query" in command and "/FO" in command:
            names = list(self.tasks) or ["System Placeholder"]
            return win.CommandResult(0, task_listing(names))
        if "/Query" in command and "/XML" in command:
            name = command[command.index("/TN") + 1]
            data = self.tasks.get(name)
            return win.CommandResult(0, data) if data else win.CommandResult(1, b"", "Nicht gefunden".encode())
        if "/Create" in command and "/XML" in command:
            name = command[command.index("/TN") + 1]
            path = command[command.index("/XML") + 1]
            with open(path, "rb") as source:
                self.tasks[name] = source.read()
            return win.CommandResult(0)
        if "/Create" in command:
            name = command[command.index("/TN") + 1]
            if name == self.fail_create:
                return win.CommandResult(1, b"", b"failure")
            action = command[command.index("/TR") + 1]
            executable, script = action.split('" "')
            executable = executable.strip('"')
            script = script.strip('"')
            self.tasks[name] = task_xml(executable, script)
            return win.CommandResult(0)
        if "/Delete" in command:
            name = command[command.index("/TN") + 1]
            self.tasks.pop(name, None)
            return win.CommandResult(0)
        raise AssertionError(command)


class ConcurrentReplacementRunner(StatefulTaskRunner):
    def __init__(self, tasks, fail_create, replace_name, replacement_xml):
        super().__init__(tasks, fail_create)
        self.replace_name = replace_name
        self.replacement_xml = replacement_xml
        self.create_failed = False
        self.replaced = False

    def run(self, command):
        if "/Create" in command and "/XML" not in command:
            name = command[command.index("/TN") + 1]
            if name == self.fail_create:
                self.create_failed = True
        if self.create_failed and not self.replaced and "/Query" in command and "/XML" in command:
            name = command[command.index("/TN") + 1]
            if name == self.replace_name:
                self.tasks[name] = self.replacement_xml
                self.replaced = True
        return super().run(command)


class ConcurrentAppearanceRunner(StatefulTaskRunner):
    def __init__(self, appeared_name, appeared_xml):
        super().__init__()
        self.appeared_name = appeared_name
        self.appeared_xml = appeared_xml
        self.listing_queries = 0

    def run(self, command):
        if "/Query" in command and "/FO" in command:
            self.listing_queries += 1
            if self.listing_queries == 3:
                self.tasks[self.appeared_name] = self.appeared_xml
        return super().run(command)


class ProcessRunner(win.Runner):
    def __init__(self, rows, query_code=0, session_id="1", owner_sid="S-1-5-21-100"):
        self.rows = list(rows)
        self.query_code = query_code
        self.session_id = session_id
        self.owner_sid = owner_sid
        self.killed = []

    def _csv(self, rows):
        output = io.StringIO()
        fieldnames = ["RecordType", "ProcessId", "Name", "CommandLine", "SessionId", "OwnerSid"]
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\r\n")
        writer.writeheader()
        writer.writerow({
            "RecordType": "Context",
            "ProcessId": "",
            "Name": "",
            "CommandLine": "",
            "SessionId": self.session_id,
            "OwnerSid": self.owner_sid,
        })
        for row in rows:
            item = dict(row)
            item.setdefault("RecordType", "Process")
            item.setdefault("SessionId", self.session_id)
            item.setdefault("OwnerSid", self.owner_sid)
            writer.writerow(item)
        return output.getvalue().encode("utf-8")

    def run(self, command):
        if command[0].endswith("powershell.exe"):
            if self.query_code:
                return win.CommandResult(self.query_code, b"", b"access denied")
            script = command[-1]
            if "ProcessId = " in script:
                pid = script.split("ProcessId = ", 1)[1].split(None, 1)[0]
                rows = [row for row in self.rows if row["ProcessId"] == pid]
            else:
                rows = self.rows
            return win.CommandResult(0, self._csv(rows))
        if command[0].endswith("taskkill.exe"):
            pid = command[-1]
            self.killed.append(pid)
            self.rows = [row for row in self.rows if row["ProcessId"] != pid]
            return win.CommandResult(0)
        raise AssertionError(command)


class DecodeAndTaskParsingTest(unittest.TestCase):
    def test_decodes_utf16_both_endiannesses_and_cp932(self):
        text = "準備完了"
        self.assertEqual(win.decode_windows_output(b"\xff\xfe" + text.encode("utf-16-le")), text)
        self.assertEqual(win.decode_windows_output(b"\xfe\xff" + text.encode("utf-16-be")), text)
        self.assertEqual(win.decode_windows_output(text.encode("cp932")), text)

    def test_cp932_wins_when_earlier_locale_candidate_contains_replacement(self):
        text = "準備完了"
        original_decode = win._decode_candidate

        def decode_candidate(data, encoding):
            if encoding.lower() == "mbcs":
                return "準備�了"
            return original_decode(data, encoding)

        with mock.patch.object(win.locale, "getpreferredencoding", return_value="mbcs"):
            with mock.patch.object(win, "_decode_candidate", side_effect=decode_candidate):
                self.assertEqual(win.decode_windows_output(text.encode("cp932")), text)

    def test_preferred_western_encoding_is_preserved_without_japanese_cp932_text(self):
        text = "Operación denegada"
        with mock.patch.object(win.locale, "getpreferredencoding", return_value="cp1252"):
            self.assertEqual(win.decode_windows_output(text.encode("cp1252")), text)

    def test_preferred_cp1252_wins_when_byte_is_valid_half_width_cp932(self):
        text = "Copyright © 2026"
        with mock.patch.object(win.locale, "getpreferredencoding", return_value="cp1252"):
            self.assertEqual(win.decode_windows_output(text.encode("cp1252")), text)

    def test_localized_error_text_is_not_mistaken_for_absent_listing(self):
        with self.assertRaises(win.IntegrationError):
            win.parse_task_names("FEHLER: Zugriff verweigert".encode("cp1252"))

    def test_escaped_xml_and_quoted_path_are_owned(self):
        executable = r"C:\Program Files\Python312\pythonw.exe"
        script = r"C:\日本語 & Work\gmail_app.py"
        self.assertTrue(win.action_is_owned(task_xml(executable, script), executable, script))

    def test_name_prefix_and_extra_arguments_are_foreign(self):
        executable = r"C:\Python\pythonw.exe"
        script = r"C:\App\gmail_app.py"
        self.assertFalse(win.action_is_owned(task_xml(executable, script + ".old"), executable, script))
        data = task_xml(executable, script).replace(
            ('"' + script + '"').encode("utf-16-le"),
            ('"' + script + '" --extra').encode("utf-16-le"),
        )
        self.assertFalse(win.action_is_owned(data, executable, script))


class TaskTransactionTest(unittest.TestCase):
    def specs(self):
        executable = r"C:\Python\pythonw.exe"
        return [
            {
                "name": "App Task",
                "executable": executable,
                "script": r"C:\App\gmail_app.py",
                "user": r"DOMAIN\user",
                "schedule": ["/SC", "ONLOGON"],
            },
            {
                "name": "Watchdog Task",
                "executable": executable,
                "script": r"C:\App\watchdog.py",
                "user": r"DOMAIN\user",
                "schedule": ["/SC", "MINUTE", "/MO", "5"],
            },
        ]

    def test_foreign_preflight_blocks_all_creates(self):
        specs = self.specs()
        runner = StatefulTaskRunner({"App Task": task_xml(r"C:\Other\pythonw.exe", r"C:\Other\app.py")})
        with self.assertRaises(win.IntegrationError):
            win.register_tasks_transaction(runner, "schtasks.exe", specs)
        self.assertFalse(any("/Create" in command for command in runner.commands))

    def test_absent_preflight_concurrent_appearance_is_not_deleted(self):
        specs = self.specs()
        appeared = task_xml(specs[0]["executable"], specs[0]["script"], description="other transaction")
        runner = ConcurrentAppearanceRunner("App Task", appeared)
        with self.assertRaisesRegex(win.IntegrationError, "changed after preflight"):
            win.register_tasks_transaction(runner, "schtasks.exe", specs)
        self.assertEqual(win._xml_signature(appeared), win._xml_signature(runner.tasks["App Task"]))
        self.assertFalse(any("/Delete" in command for command in runner.commands))
        self.assertFalse(any("/Create" in command for command in runner.commands))

    def test_second_create_failure_deletes_only_new_owned_first_task(self):
        specs = self.specs()
        runner = StatefulTaskRunner(fail_create="Watchdog Task")
        with self.assertRaises(win.IntegrationError):
            win.register_tasks_transaction(runner, "schtasks.exe", specs)
        self.assertNotIn("App Task", runner.tasks)
        self.assertNotIn("Watchdog Task", runner.tasks)

    def test_second_create_failure_restores_exact_owned_first_xml(self):
        specs = self.specs()
        original = task_xml(specs[0]["executable"], specs[0]["script"], description="original")
        runner = StatefulTaskRunner({"App Task": original}, fail_create="Watchdog Task")
        with self.assertRaises(win.IntegrationError):
            win.register_tasks_transaction(runner, "schtasks.exe", specs)
        self.assertEqual(win._xml_signature(runner.tasks["App Task"]), win._xml_signature(original))

    def test_rollback_refuses_to_overwrite_concurrent_foreign_replacement(self):
        specs = self.specs()
        original = task_xml(specs[0]["executable"], specs[0]["script"], description="original")
        foreign = task_xml(r"C:\Other\pythonw.exe", r"C:\Other\foreign.py", description="foreign")
        runner = ConcurrentReplacementRunner(
            {"App Task": original}, "Watchdog Task", "App Task", foreign
        )
        with self.assertRaisesRegex(win.IntegrationError, "rollback verification failed"):
            win.register_tasks_transaction(runner, "schtasks.exe", specs)
        self.assertEqual(win._xml_signature(runner.tasks["App Task"]), win._xml_signature(foreign))
        restore_commands = [command for command in runner.commands if "/Create" in command and "/XML" in command]
        self.assertEqual([], restore_commands)

    def test_rollback_refuses_same_command_definition_drift(self):
        specs = self.specs()
        original = task_xml(specs[0]["executable"], specs[0]["script"], description="original")
        drift = task_xml(specs[0]["executable"], specs[0]["script"], description="concurrent drift")
        runner = ConcurrentReplacementRunner(
            {"App Task": original}, "Watchdog Task", "App Task", drift
        )
        with self.assertRaisesRegex(win.IntegrationError, "rollback verification failed"):
            win.register_tasks_transaction(runner, "schtasks.exe", specs)
        self.assertEqual(win._xml_signature(runner.tasks["App Task"]), win._xml_signature(drift))
        restore_commands = [command for command in runner.commands if "/Create" in command and "/XML" in command]
        self.assertEqual([], restore_commands)


@unittest.skipUnless(os.name == "nt", "Windows command-line rules")
class ProcessOwnershipTest(unittest.TestCase):
    def test_no_matching_process_is_a_verified_zero(self):
        runner = ProcessRunner([])
        stopped = win.stop_owned_monitors(
            runner, "powershell.exe", "taskkill.exe", r"C:\App\gmail_monitor.py"
        )
        self.assertEqual(stopped, 0)
        self.assertEqual(runner.killed, [])

    def test_only_exact_canonical_script_is_stopped(self):
        script = os.path.abspath(r"C:\App Folder\gmail_monitor.py")
        rows = [
            {"ProcessId": "101", "Name": "pythonw.exe", "CommandLine": f'pythonw.exe "{script}"'},
            {"ProcessId": "102", "Name": "python.exe", "CommandLine": f'python.exe "{script}.old"'},
            {"ProcessId": "103", "Name": "python.exe", "CommandLine": "python.exe gmail_monitor.py"},
            {"ProcessId": "104", "Name": "python.exe", "CommandLine": "", "SessionId": "2"},
        ]
        runner = ProcessRunner(rows)
        stopped = win.stop_owned_monitors(runner, "powershell.exe", "taskkill.exe", script)
        self.assertEqual(stopped, 1)
        self.assertEqual(runner.killed, ["101"])

    def test_same_script_in_other_session_or_user_is_not_owned(self):
        script = os.path.abspath(r"C:\App\gmail_monitor.py")
        rows = [
            {
                "ProcessId": "301",
                "Name": "python.exe",
                "CommandLine": f'python.exe "{script}"',
                "SessionId": "2",
                "OwnerSid": "S-1-5-21-100",
            },
            {
                "ProcessId": "302",
                "Name": "python.exe",
                "CommandLine": f'python.exe "{script}"',
                "SessionId": "1",
                "OwnerSid": "S-1-5-21-999",
            },
        ]
        runner = ProcessRunner(rows)
        self.assertEqual([], win.owned_monitor_pids(runner, "powershell.exe", script))
        self.assertEqual(0, win.stop_owned_monitors(runner, "powershell.exe", "taskkill.exe", script))
        self.assertEqual([], runner.killed)

    def test_same_session_unknown_owner_is_fail_closed(self):
        script = os.path.abspath(r"C:\App\gmail_monitor.py")
        rows = [{
            "ProcessId": "303",
            "Name": "python.exe",
            "CommandLine": f'python.exe "{script}"',
            "SessionId": "1",
            "OwnerSid": "",
        }]
        runner = ProcessRunner(rows)
        with self.assertRaises(win.IntegrationError):
            win.owned_monitor_pids(runner, "powershell.exe", script)
        with self.assertRaises(win.IntegrationError):
            win.stop_owned_monitors(runner, "powershell.exe", "taskkill.exe", script)
        self.assertEqual([], runner.killed)

    def test_same_user_session_unknown_command_line_is_fail_closed(self):
        script = os.path.abspath(r"C:\App\gmail_monitor.py")
        rows = [{
            "ProcessId": "304",
            "Name": "python.exe",
            "CommandLine": "",
            "SessionId": "1",
            "OwnerSid": "S-1-5-21-100",
        }]
        runner = ProcessRunner(rows)
        with self.assertRaises(win.IntegrationError):
            win.owned_monitor_pids(runner, "powershell.exe", script)
        self.assertEqual([], runner.killed)

    def test_query_failure_is_unknown_and_kills_nothing(self):
        runner = ProcessRunner([], query_code=1)
        with self.assertRaises(win.IntegrationError):
            win.stop_owned_monitors(runner, "powershell.exe", "taskkill.exe", r"C:\App\gmail_monitor.py")
        self.assertEqual(runner.killed, [])

    def test_pid_identity_change_blocks_kill(self):
        script = os.path.abspath(r"C:\App\gmail_monitor.py")
        row = {"ProcessId": "201", "Name": "python.exe", "CommandLine": f'python.exe "{script}"'}
        runner = ProcessRunner([row])
        original_run = runner.run
        calls = {"queries": 0}

        def change_before_recheck(command):
            if command[0].endswith("powershell.exe"):
                calls["queries"] += 1
                if calls["queries"] == 2:
                    runner.rows[0]["CommandLine"] = "python.exe other.py"
            return original_run(command)

        with mock.patch.object(runner, "run", side_effect=change_before_recheck):
            with self.assertRaises(win.IntegrationError):
                win.stop_owned_monitors(runner, "powershell.exe", "taskkill.exe", script)
        self.assertEqual(runner.killed, [])


if __name__ == "__main__":
    unittest.main()
