import csv
import datetime
import io
import os
import re
import subprocess
import unittest
import xml.etree.ElementTree as ET
from unittest import mock
from xml.sax.saxutils import escape, unescape

import windows_integration as win
from tests.fixtures import task_xml_readback as fixtures


SIDS = {
    r"domain\user": "S-1-5-21-100-200-300-1001",
    fixtures.FIXTURE_USER.casefold(): fixtures.FIXTURE_SID,
    r"pc\o'brien&co": "S-1-5-21-100-200-300-1002",
}


def resolve_sid(account):
    try:
        return SIDS[account.casefold()]
    except KeyError:
        raise win.IntegrationError("fake account lookup failed") from None


def fixed_clock():
    return datetime.datetime(2026, 9, 24, 1, 2, 3, 456789)


def register(runner, specs, **kwargs):
    kwargs.setdefault("resolve_sid", resolve_sid)
    kwargs.setdefault("clock", fixed_clock)
    win.register_tasks_transaction(runner, "schtasks.exe", specs, **kwargs)


def simulate_readback(text):
    """What `schtasks /Query /XML` pipes back for a registered definition (see fixtures)."""

    def principal_sid(match):
        user = unescape(match.group(2))
        return match.group(1) + escape(SIDS.get(user.casefold(), user)) + match.group(3)

    text = re.sub(r'(<Principal id="Author">\s*<UserId>)(.*?)(</UserId>)', principal_sid, text, flags=re.S)
    text = text.replace("      <RunLevel>LeastPrivilege</RunLevel>\r\n", "")
    text = text.replace("    <AllowStartOnDemand>true</AllowStartOnDemand>\r\n", "")
    return text.replace("\r\n", "\r\r\n").encode("cp932", errors="replace")


def task_xml(executable, script, description="日本語 & safe"):
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{escape(description)}</Description></RegistrationInfo>
  <Actions Context="Author"><Exec><Command>{escape(executable)}</Command><Arguments>"{escape(script)}"</Arguments></Exec></Actions>
</Task>"""
    return b"\xff\xfe" + xml.encode("utf-16-le")


EXEC_ACTION = r'<Exec><Command>C:\Python\pythonw.exe</Command><Arguments>"C:\App\gmail_app.py"</Arguments></Exec>'
COM_ACTION = "<ComHandler><ClassId>{00000000-0000-0000-0000-000000000000}</ClassId></ComHandler>"
MESSAGE_ACTION = "<ShowMessage><Title>t</Title><Body>b</Body></ShowMessage>"
EMAIL_ACTION = "<SendEmail><Server>smtp.invalid</Server><To>a@b.invalid</To><From>c@d.invalid</From></SendEmail>"


def task_xml_with_actions(*blocks):
    actions = "".join(f'<Actions Context="Author">{block}</Actions>' for block in blocks)
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  {actions}
</Task>"""
    return b"\xff\xfe" + xml.encode("utf-16-le")


def task_listing(names):
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\r\n")
    for name in names:
        writer.writerow(["\\" + name.lstrip("\\"), "N/A", "準備完了"])
    return output.getvalue().encode("cp932")


class StatefulTaskRunner(win.Runner):
    """Fake Task Scheduler: /Create reads the temp XML file like schtasks does.

    `creates` records (name, file bytes, temp path) for every /Create, in call
    order; the first /Create for `fail_create` fails, later ones (restores)
    succeed. `tamper(name, text)` may rewrite a definition before it is stored.
    """

    def __init__(self, tasks=None, fail_create=None, tamper=None):
        self.tasks = dict(tasks or {})
        self.fail_create = fail_create
        self.tamper = tamper
        self.commands = []
        self.creates = []
        self.failed_once = False

    def run(self, command):
        self.commands.append(command)
        if "/Query" in command and "/FO" in command:
            names = list(self.tasks) or ["System Placeholder"]
            return win.CommandResult(0, task_listing(names))
        if "/Query" in command and "/XML" in command:
            name = command[command.index("/TN") + 1]
            data = self.tasks.get(name)
            return win.CommandResult(0, data) if data else win.CommandResult(1, b"", "Nicht gefunden".encode())
        if "/Create" in command:
            if len(command) != 7 or command[1:4] != ["/Create", "/F", "/TN"] or command[5] != "/XML":
                raise AssertionError(command)
            name, path = command[4], command[6]
            with open(path, "rb") as source:
                data = source.read()
            self.creates.append((name, data, path))
            if not data.startswith(b"\xff\xfe"):
                return win.CommandResult(1, b"", b"not UTF-16LE with BOM")
            text = data[2:].decode("utf-16-le")
            if "\r\r\n" in text:
                return win.CommandResult(1, b"", b"stray CR")
            ET.fromstring(text)
            if name == self.fail_create and not self.failed_once:
                self.failed_once = True
                return win.CommandResult(1, b"", b"failure")
            if self.tamper:
                text = self.tamper(name, text)
            self.tasks[name] = simulate_readback(text)
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
        if "/Create" in command:
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
    def test_decodes_utf16_both_endiannesses(self):
        text = "準備完了"
        self.assertEqual(win.decode_windows_output(b"\xff\xfe" + text.encode("utf-16-le")), text)
        self.assertEqual(win.decode_windows_output(b"\xfe\xff" + text.encode("utf-16-be")), text)

    def test_decodes_cp932_on_japanese_locale_preferred_encoding(self):
        # On an en-US machine, locale.getpreferredencoding() is cp1252 and the
        # Windows "mbcs" alias also resolves to cp1252, which decodes every
        # byte without U+FFFD and wins ahead of cp932 in the candidate list.
        # Real en-US tools never emit cp932, so this test pins the locale to
        # a Japanese one (matching the neighboring locale-patched tests) to
        # exercise the cp932 fallback deterministically on any machine/CI.
        text = "準備完了"
        with mock.patch.object(win.locale, "getpreferredencoding", return_value="cp932"):
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

    def test_only_a_single_exec_action_can_be_owned(self):
        executable = r"C:\Python\pythonw.exe"
        script = r"C:\App\gmail_app.py"
        self.assertTrue(win.action_is_owned(task_xml_with_actions(EXEC_ACTION), executable, script))
        for blocks in (
            (EXEC_ACTION + COM_ACTION,),
            (COM_ACTION + EXEC_ACTION,),
            (EXEC_ACTION + MESSAGE_ACTION,),
            (EXEC_ACTION + EMAIL_ACTION,),
            (EXEC_ACTION + EXEC_ACTION,),
            (COM_ACTION,),
            (EXEC_ACTION, EXEC_ACTION),
            (EXEC_ACTION, COM_ACTION),
            (),
        ):
            with self.subTest(blocks=blocks):
                data = task_xml_with_actions(*blocks)
                with self.assertRaisesRegex(win.IntegrationError, "exactly one executable action"):
                    win.parse_task_action(data)
                runner = StatefulTaskRunner({"App Task": data})
                self.assertEqual(
                    (win.UNKNOWN, None), win.classify_task(runner, "schtasks.exe", "App Task", executable, script)
                )

    def test_exec_outside_the_actions_block_is_not_counted(self):
        data = task_xml_with_actions(COM_ACTION).replace(
            "</Task>".encode("utf-16-le"), f"<Data>{EXEC_ACTION}</Data></Task>".encode("utf-16-le")
        )
        with self.assertRaisesRegex(win.IntegrationError, "exactly one executable action"):
            win.parse_task_action(data)


class TaskTransactionTest(unittest.TestCase):
    def specs(self):
        executable = r"C:\Python\pythonw.exe"
        return [
            {
                "name": "App Task",
                "executable": executable,
                "script": r"C:\App\gmail_app.py",
                "user": r"DOMAIN\user",
                "trigger": win.LOGON_TRIGGER,
            },
            {
                "name": "Watchdog Task",
                "executable": executable,
                "script": r"C:\App\watchdog.py",
                "user": r"DOMAIN\user",
                "trigger": win.INTERVAL_TRIGGER,
            },
        ]

    def fixture_specs(self):
        return [
            {
                "name": "App Task",
                "executable": fixtures.FIXTURE_PYTHONW,
                "script": fixtures.FIXTURE_APP_SCRIPT,
                "user": fixtures.FIXTURE_USER,
                "trigger": win.LOGON_TRIGGER,
            },
            {
                "name": "Watchdog Task",
                "executable": fixtures.FIXTURE_PYTHONW,
                "script": fixtures.FIXTURE_WATCHDOG_SCRIPT,
                "user": fixtures.FIXTURE_USER,
                "trigger": win.INTERVAL_TRIGGER,
            },
        ]

    def assert_verified(self, runner, specs):
        for spec in specs:
            installed = runner.tasks[spec["name"]]
            self.assertEqual(
                win.OWNED,
                win.classify_task(runner, "schtasks.exe", spec["name"], spec["executable"], spec["script"])[0],
            )
            win.verify_task_definition(installed, spec, resolve_sid(spec["user"]), resolve_sid)

    def test_foreign_preflight_blocks_all_creates(self):
        specs = self.specs()
        runner = StatefulTaskRunner({"App Task": task_xml(r"C:\Other\pythonw.exe", r"C:\Other\app.py")})
        with self.assertRaises(win.IntegrationError):
            register(runner, specs)
        self.assertFalse(any("/Create" in command for command in runner.commands))

    def test_unknown_listing_blocks_all_creates(self):
        specs = self.specs()
        runner = StatefulTaskRunner()
        original_run = runner.run

        def denied_listing(command):
            if "/FO" in command:
                runner.commands.append(command)
                return win.CommandResult(1, b"", "Zugriff verweigert".encode("cp1252"))
            return original_run(command)

        with mock.patch.object(runner, "run", side_effect=denied_listing):
            with self.assertRaisesRegex(win.IntegrationError, "foreign or unknown"):
                register(runner, specs)
        self.assertFalse(any("/Create" in command for command in runner.commands))

    def test_absent_preflight_concurrent_appearance_is_not_deleted(self):
        specs = self.specs()
        appeared = task_xml(specs[0]["executable"], specs[0]["script"], description="other transaction")
        runner = ConcurrentAppearanceRunner("App Task", appeared)
        with self.assertRaisesRegex(win.IntegrationError, "changed after preflight"):
            register(runner, specs)
        self.assertEqual(win._xml_signature(appeared), win._xml_signature(runner.tasks["App Task"]))
        self.assertFalse(any("/Delete" in command for command in runner.commands))
        self.assertFalse(any("/Create" in command for command in runner.commands))

    def test_owned_definition_drift_after_preflight_blocks_create(self):
        specs = self.specs()
        original = task_xml(specs[0]["executable"], specs[0]["script"], description="original")
        drift = task_xml(specs[0]["executable"], specs[0]["script"], description="drift")
        runner = StatefulTaskRunner({"App Task": original})
        original_run = runner.run
        calls = {"listings": 0}

        def drift_before_create(command):
            if "/FO" in command:
                calls["listings"] += 1
                if calls["listings"] == 3:
                    runner.tasks["App Task"] = drift
            return original_run(command)

        with mock.patch.object(runner, "run", side_effect=drift_before_create):
            with self.assertRaisesRegex(win.IntegrationError, "changed after preflight"):
                register(runner, specs)
        self.assertEqual(drift, runner.tasks["App Task"])
        self.assertEqual([], runner.creates)

    def test_create_passes_only_the_xml_file_and_removes_it(self):
        specs = self.specs()
        runner = StatefulTaskRunner()
        register(runner, specs)
        creates = [command for command in runner.commands if "/Create" in command]
        self.assertEqual(2, len(creates))
        for spec, command, (name, data, path) in zip(specs, creates, runner.creates):
            self.assertEqual(["schtasks.exe", "/Create", "/F", "/TN", spec["name"], "/XML", path], command)
            self.assertEqual(spec["name"], name)
            self.assertEqual(win.build_task_xml(spec, fixed_clock()), data)
            self.assertFalse(os.path.exists(path))
        flags = {"/RU", "/RP", "/IT", "/RL", "/TR", "/SC", "/MO", "/DELAY", "/ST"}
        self.assertFalse(any(flags & set(command) for command in runner.commands))
        self.assert_verified(runner, specs)

    def test_temp_xml_is_removed_when_create_fails(self):
        specs = self.specs()
        runner = StatefulTaskRunner(fail_create="App Task")
        with self.assertRaisesRegex(win.IntegrationError, "Task creation failed"):
            register(runner, specs)
        self.assertEqual(["App Task"], [name for name, _data, _path in runner.creates])
        self.assertFalse(os.path.exists(runner.creates[0][2]))
        self.assertEqual({}, runner.tasks)

    def test_temp_xml_is_removed_when_the_runner_raises(self):
        seen = []

        class ExplodingRunner(win.Runner):
            def run(self, command):
                path = command[-1]
                with open(path, "rb") as source:
                    seen.append((path, source.read()))
                raise OSError("spawn failed")

        with self.assertRaisesRegex(win.IntegrationError, "^schtasks.exe could not be run: spawn failed$") as caught:
            win._run_create_xml(ExplodingRunner(), "schtasks.exe", "App Task", b"\xff\xfedata")
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertEqual(b"\xff\xfedata", seen[0][1])
        self.assertFalse(os.path.exists(seen[0][0]))

    def test_temp_xml_creation_failure_is_an_integration_error(self):
        runner = StatefulTaskRunner()
        with mock.patch.object(win.tempfile, "mkstemp", side_effect=OSError(28, "No space left")):
            with self.assertRaisesRegex(win.IntegrationError, "Temporary task XML could not be created") as caught:
                win._run_create_xml(runner, "schtasks.exe", "App Task", b"\xff\xfedata")
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertEqual([], runner.commands)

    def test_temp_xml_write_failure_is_an_integration_error_and_removes_the_file(self):
        runner = StatefulTaskRunner()
        created = []
        real_mkstemp = win.tempfile.mkstemp
        real_fdopen = win.os.fdopen

        def recording_mkstemp(*args, **kwargs):
            handle, path = real_mkstemp(*args, **kwargs)
            created.append(path)
            return handle, path

        class FullDisk:
            def __init__(self, handle):
                self.file = real_fdopen(handle, "wb")

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                self.file.close()

            def write(self, data):
                raise OSError(28, "No space left")

        with mock.patch.object(win.tempfile, "mkstemp", side_effect=recording_mkstemp), \
             mock.patch.object(win.os, "fdopen", side_effect=lambda handle, mode: FullDisk(handle)):
            with self.assertRaisesRegex(win.IntegrationError, "Temporary task XML could not be written"):
                win._run_create_xml(runner, "schtasks.exe", "App Task", b"\xff\xfedata")
        self.assertEqual([], runner.commands)
        self.assertEqual(1, len(created))
        self.assertFalse(os.path.exists(created[0]))

    def failing_runner(self, runner, predicate, error=None):
        """Patch runner.run so commands matching predicate raise OSError (or return `error`)."""
        original_run = runner.run

        def run(command):
            if predicate(command):
                runner.commands.append(command)
                if error is not None:
                    return error
                raise OSError(5, "Access is denied")
            return original_run(command)

        return mock.patch.object(runner, "run", side_effect=run)

    def test_os_error_on_second_create_rolls_back_the_first(self):
        specs = self.specs()
        for original in (None, task_xml(specs[0]["executable"], specs[0]["script"], description="original")):
            with self.subTest(original_owned=original is not None):
                runner = StatefulTaskRunner({"App Task": original} if original else {})
                with self.failing_runner(runner, lambda c: "/Create" in c and c[4] == "Watchdog Task"):
                    with self.assertRaises(win.IntegrationError) as caught:
                        register(runner, specs)
                self.assertEqual("schtasks.exe could not be run: [Errno 5] Access is denied", str(caught.exception))
                self.assertNotIn("Watchdog Task", runner.tasks)
                if original is None:
                    self.assertNotIn("App Task", runner.tasks)
                else:
                    self.assertEqual(win._xml_signature(original), win._xml_signature(runner.tasks["App Task"]))

    def test_os_error_during_restore_is_reported_as_rollback_failure(self):
        specs = self.specs()
        original = task_xml(specs[0]["executable"], specs[0]["script"], description="original")
        cases = (
            ({"App Task": original}, lambda c: "/Create" in c and c[4] == "App Task" and len(runner.creates) == 2),
            ({}, lambda c: "/Delete" in c),
        )
        for tasks, predicate in cases:
            with self.subTest(original_owned=bool(tasks)):
                runner = StatefulTaskRunner(tasks, fail_create="Watchdog Task")
                with self.failing_runner(runner, predicate):
                    with self.assertRaises(win.IntegrationError) as caught:
                        register(runner, specs)
                self.assertEqual(
                    "Task creation failed; rollback verification failed: "
                    "schtasks.exe could not be run: [Errno 5] Access is denied",
                    str(caught.exception),
                )
                # The rollback could not act, so the new definition is still installed.
                win.verify_task_definition(runner.tasks["App Task"], specs[0], resolve_sid(specs[0]["user"]), resolve_sid)

    def test_xml_query_spawn_failure_is_unknown_or_a_rollback_failure(self):
        specs = self.specs()
        original = task_xml(specs[0]["executable"], specs[0]["script"], description="original")
        is_xml_query = lambda c: "/Query" in c and "/XML" in c and c[3] == "App Task"

        runner = StatefulTaskRunner({"App Task": original})
        with self.failing_runner(runner, is_xml_query):
            with self.assertRaisesRegex(win.IntegrationError, "^Task preflight found foreign or unknown state$"):
                register(runner, specs)
        self.assertEqual([], runner.creates)

        runner = StatefulTaskRunner({"App Task": original}, fail_create="Watchdog Task")
        with self.failing_runner(runner, lambda c: is_xml_query(c) and len(runner.creates) == 3):
            with self.assertRaises(win.IntegrationError) as caught:
                register(runner, specs)
        self.assertEqual(
            "Task creation failed; rollback verification failed: "
            "schtasks.exe could not be run: [Errno 5] Access is denied",
            str(caught.exception),
        )
        self.assertEqual(["App Task", "Watchdog Task", "App Task"], [name for name, _data, _path in runner.creates])

    def test_listing_spawn_failure_is_unknown_and_blocks_all_creates(self):
        specs = self.specs()
        runner = StatefulTaskRunner()
        with self.failing_runner(runner, lambda c: "/FO" in c):
            with self.assertRaisesRegex(win.IntegrationError, "foreign or unknown"):
                register(runner, specs)
        self.assertEqual([], runner.creates)

    def test_restore_create_failure_is_reported(self):
        specs = self.specs()
        original = task_xml(specs[0]["executable"], specs[0]["script"], description="original")
        runner = StatefulTaskRunner({"App Task": original}, fail_create="Watchdog Task")
        refused = win.CommandResult(1, b"", b"refused")
        restore = lambda c: "/Create" in c and c[4] == "App Task" and len(runner.creates) == 2
        with self.failing_runner(runner, restore, error=refused):
            with self.assertRaises(win.IntegrationError) as caught:
                register(runner, specs)
        self.assertEqual(
            "Task creation failed; rollback verification failed: Rollback could not restore the original task",
            str(caught.exception),
        )
        self.assertEqual(["App Task", "Watchdog Task"], [name for name, _data, _path in runner.creates])
        restore_commands = [c for c in runner.commands if "/Create" in c and c[4] == "App Task"]
        self.assertEqual(2, len(restore_commands))
        self.assertFalse(os.path.exists(restore_commands[1][6]))
        win.verify_task_definition(runner.tasks["App Task"], specs[0], resolve_sid(specs[0]["user"]), resolve_sid)

    def test_restore_readback_mismatch_is_reported(self):
        specs = self.specs()
        original = task_xml(specs[0]["executable"], specs[0]["script"], description="original")
        app_creates = []

        def tamper_restore(name, text):
            if name == "App Task":
                app_creates.append(name)
                if len(app_creates) == 2:
                    return text.replace("<Description>original</Description>", "<Description>changed</Description>")
            return text

        runner = StatefulTaskRunner({"App Task": original}, fail_create="Watchdog Task", tamper=tamper_restore)
        with self.assertRaises(win.IntegrationError) as caught:
            register(runner, specs)
        self.assertEqual(
            "Task creation failed; rollback verification failed: "
            "Rollback task state does not match the original XML",
            str(caught.exception),
        )
        self.assertEqual(
            ["App Task", "Watchdog Task", "App Task"], [name for name, _data, _path in runner.creates]
        )
        self.assertIn(b"changed", runner.tasks["App Task"])

    UNVERIFIED_APP_TASK = (
        "Created task 'App Task' could not be verified as this install's task "
        "(install paths with characters outside the Windows code page cannot be registered)"
    )
    ROLLBACK_REFUSED = "; rollback verification failed: Rollback refused to touch a task that is no longer owned"

    def test_unverifiable_created_task_is_named_with_the_code_page_hint(self):
        specs = self.specs()
        specs[0]["script"] = r"C:\Café\gmail_app.py"
        runner = StatefulTaskRunner()
        with self.assertRaises(win.IntegrationError) as caught:
            register(runner, specs)
        self.assertEqual(self.UNVERIFIED_APP_TASK + self.ROLLBACK_REFUSED, str(caught.exception))
        # Recorded for rollback, but not provably ours, so it is not deleted.
        self.assertIn(rb"C:\Caf?\gmail_app.py", runner.tasks["App Task"])
        self.assertEqual(["App Task"], [name for name, _data, _path in runner.creates])
        self.assertFalse(any("/Delete" in command for command in runner.commands))

    def test_foreign_readback_after_create_is_not_overwritten_with_the_original(self):
        specs = self.specs()
        original = task_xml(specs[0]["executable"], specs[0]["script"], description="original")
        foreign_script = r"C:\Other\foreign.py"

        def foreign_after_create(name, text):
            # Another writer replaced the definition before the read-back.
            return text.replace(specs[0]["script"], foreign_script)

        runner = StatefulTaskRunner({"App Task": original}, tamper=foreign_after_create)
        with self.assertRaises(win.IntegrationError) as caught:
            register(runner, specs)
        self.assertEqual(self.UNVERIFIED_APP_TASK + self.ROLLBACK_REFUSED, str(caught.exception))
        self.assertTrue(win.action_is_owned(runner.tasks["App Task"], specs[0]["executable"], foreign_script))
        # Only the create: no restore reached schtasks.
        self.assertEqual(["App Task"], [name for name, _data, _path in runner.creates])
        self.assertFalse(any("/Delete" in command for command in runner.commands))

    def test_readback_failure_after_create_still_rolls_back_the_task(self):
        specs = self.specs()
        original = task_xml(specs[0]["executable"], specs[0]["script"], description="original")
        failures = (
            ("listing spawn", lambda c: "/FO" in c, None),
            ("xml query", lambda c: "/Query" in c and "/XML" in c, win.CommandResult(1, b"", b"busy")),
        )
        for label, matches, error in failures:
            for tasks in ({}, {"App Task": original}):
                with self.subTest(failure=label, original_owned=bool(tasks)):
                    runner = StatefulTaskRunner(tasks)
                    failed = []

                    def first_readback(command):
                        # Only the read-back right after /Create fails; the rollback re-query works.
                        if len(runner.creates) == 1 and not failed and matches(command):
                            failed.append(command)
                            return True
                        return False

                    with self.failing_runner(runner, first_readback, error):
                        with self.assertRaises(win.IntegrationError) as caught:
                            register(runner, specs)
                    self.assertEqual(self.UNVERIFIED_APP_TASK, str(caught.exception))
                    self.assertEqual(1, len(failed))
                    self.assertNotIn("Watchdog Task", runner.tasks)
                    deletes = [c[c.index("/TN") + 1] for c in runner.commands if "/Delete" in c]
                    if tasks:
                        self.assertEqual(win._xml_signature(original), win._xml_signature(runner.tasks["App Task"]))
                        self.assertEqual(["App Task", "App Task"], [name for name, _data, _path in runner.creates])
                        self.assertEqual(win._utf16_task_document(original), runner.creates[1][1])
                        self.assertEqual([], deletes)
                    else:
                        self.assertNotIn("App Task", runner.tasks)
                        self.assertEqual(["App Task"], [name for name, _data, _path in runner.creates])
                        self.assertEqual(["App Task"], deletes)

    def test_unverified_task_is_left_and_reported_when_the_rollback_requery_fails(self):
        specs = self.specs()
        original = task_xml(specs[0]["executable"], specs[0]["script"], description="original")
        for tasks in ({}, {"App Task": original}):
            with self.subTest(original_owned=bool(tasks)):
                runner = StatefulTaskRunner(tasks)
                with self.failing_runner(runner, lambda c: "/FO" in c and len(runner.creates) == 1):
                    with self.assertRaises(win.IntegrationError) as caught:
                        register(runner, specs)
                self.assertEqual(self.UNVERIFIED_APP_TASK + self.ROLLBACK_REFUSED, str(caught.exception))
                # The accepted definition is still installed; nothing touched it afterwards.
                win.verify_task_definition(runner.tasks["App Task"], specs[0], resolve_sid(specs[0]["user"]), resolve_sid)
                self.assertEqual(["App Task"], [name for name, _data, _path in runner.creates])
                self.assertFalse(any("/Delete" in command for command in runner.commands))

    def test_second_create_failure_deletes_only_new_owned_first_task(self):
        specs = self.specs()
        runner = StatefulTaskRunner(fail_create="Watchdog Task")
        with self.assertRaises(win.IntegrationError):
            register(runner, specs)
        self.assertNotIn("App Task", runner.tasks)
        self.assertNotIn("Watchdog Task", runner.tasks)
        self.assertEqual(["App Task", "Watchdog Task"], [name for name, _data, _path in runner.creates])

    def test_second_create_failure_restores_exact_owned_first_xml(self):
        specs = self.specs()
        original = task_xml(specs[0]["executable"], specs[0]["script"], description="original")
        runner = StatefulTaskRunner({"App Task": original}, fail_create="Watchdog Task")
        with self.assertRaises(win.IntegrationError):
            register(runner, specs)
        self.assertEqual(win._xml_signature(runner.tasks["App Task"]), win._xml_signature(original))
        self.assertEqual(
            ["App Task", "Watchdog Task", "App Task"], [name for name, _data, _path in runner.creates]
        )
        self.assertEqual(win._utf16_task_document(original), runner.creates[2][1])

    def test_legacy_flag_tasks_are_owned_and_upgraded(self):
        specs = self.fixture_specs()
        runner = StatefulTaskRunner(
            {"App Task": fixtures.LEGACY_MONITOR, "Watchdog Task": fixtures.LEGACY_WATCHDOG}
        )
        for spec in specs:
            with self.subTest(spec["name"]):
                state, _xml = win.classify_task(
                    runner, "schtasks.exe", spec["name"], spec["executable"], spec["script"]
                )
                self.assertEqual(win.OWNED, state)
                with self.assertRaisesRegex(win.IntegrationError, "DisallowStartIfOnBatteries"):
                    win.verify_task_definition(runner.tasks[spec["name"]], spec, fixtures.FIXTURE_SID, resolve_sid)
        register(runner, specs)
        self.assert_verified(runner, specs)

    def test_legacy_rollback_restores_the_exact_flag_created_xml(self):
        specs = self.fixture_specs()
        runner = StatefulTaskRunner(
            {"App Task": fixtures.LEGACY_MONITOR, "Watchdog Task": fixtures.LEGACY_WATCHDOG},
            fail_create="Watchdog Task",
        )
        with self.assertRaisesRegex(win.IntegrationError, "Task creation failed"):
            register(runner, specs)
        self.assertEqual(win._xml_signature(fixtures.LEGACY_MONITOR), win._xml_signature(runner.tasks["App Task"]))
        self.assertEqual(fixtures.LEGACY_WATCHDOG, runner.tasks["Watchdog Task"])
        self.assertEqual(
            ["App Task", "Watchdog Task", "App Task"], [name for name, _data, _path in runner.creates]
        )
        restored = runner.creates[2][1]
        self.assertTrue(restored.startswith(b"\xff\xfe"))
        text = restored[2:].decode("utf-16-le")
        self.assertNotIn("\r\r\n", text)
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-16"?>\r\n<Task version="1.2"'))
        self.assertIn("<DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>", text)
        for _name, _data, path in runner.creates:
            self.assertFalse(os.path.exists(path))

    def test_settings_mismatch_after_create_rolls_back_that_task_too(self):
        specs = self.specs()

        def ignore_battery_setting(name, text):
            if name != "Watchdog Task":
                return text
            return text.replace(
                "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
                "<DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>",
            )

        runner = StatefulTaskRunner(tamper=ignore_battery_setting)
        with self.assertRaisesRegex(win.IntegrationError, "DisallowStartIfOnBatteries"):
            register(runner, specs)
        self.assertEqual({}, runner.tasks)
        deletes = [command[command.index("/TN") + 1] for command in runner.commands if "/Delete" in command]
        self.assertEqual(["Watchdog Task", "App Task"], deletes)

    def test_settings_mismatch_restores_owned_original(self):
        specs = self.fixture_specs()

        def drop_time_limit(name, text):
            return re.sub(r"\s*<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", "", text)

        runner = StatefulTaskRunner({"App Task": fixtures.LEGACY_MONITOR}, tamper=drop_time_limit)
        with self.assertRaisesRegex(win.IntegrationError, "ExecutionTimeLimit"):
            register(runner, specs)
        self.assertEqual(win._xml_signature(fixtures.LEGACY_MONITOR), win._xml_signature(runner.tasks["App Task"]))
        self.assertNotIn("Watchdog Task", runner.tasks)
        self.assertEqual(["App Task", "App Task"], [name for name, _data, _path in runner.creates])

    def test_principal_mismatch_rolls_back(self):
        specs = self.specs()

        def other_user(name, text):
            return text.replace(r"<UserId>DOMAIN\user</UserId>", "<UserId>S-1-5-21-100-200-300-9999</UserId>")

        runner = StatefulTaskRunner(tamper=other_user)
        with self.assertRaisesRegex(win.IntegrationError, "principal is not the expected user"):
            register(runner, specs)
        self.assertEqual({}, runner.tasks)
        self.assertEqual(["App Task"], [name for name, _data, _path in runner.creates])

    def test_unresolvable_user_blocks_before_any_query(self):
        specs = self.specs()
        for spec in specs:
            spec["user"] = r"NOWHERE\nobody"
        runner = StatefulTaskRunner()
        with self.assertRaisesRegex(win.IntegrationError, "fake account lookup failed"):
            register(runner, specs)
        self.assertEqual([], runner.commands)

    def test_invalid_spec_blocks_before_any_query(self):
        for field, value in (
            ("script", "C:\\App\\\ud800.py"),
            ("executable", 'C:\\Py"thon\\pythonw.exe'),
            ("user", "DOMAIN\\us\x07er"),
            ("trigger", "ONLOGON"),
        ):
            with self.subTest(field):
                specs = self.specs()
                specs[1][field] = value
                runner = StatefulTaskRunner()
                with self.assertRaises(win.IntegrationError):
                    register(runner, specs)
                self.assertEqual([], runner.commands)

    def test_hostile_user_and_paths_round_trip(self):
        specs = self.specs()
        for spec in specs:
            spec["user"] = r"PC\O'Brien&Co"
            spec["executable"] = r"C:\Program Files (x86)\日本語 & Co\pythonw.exe"
            spec["script"] = spec["script"].replace(r"C:\App", r"C:\O'Brien & <Co> 作業")
        runner = StatefulTaskRunner()
        # simulate_readback() models schtasks piping back cp932 bytes; pin the
        # Japanese locale it assumes so the kanji paths round-trip
        # deterministically on any machine/CI (see
        # test_decodes_cp932_on_japanese_locale_preferred_encoding above).
        with mock.patch.object(win.locale, "getpreferredencoding", return_value="cp932"):
            register(runner, specs)
            self.assert_verified(runner, specs)

    def test_rollback_refuses_to_overwrite_concurrent_foreign_replacement(self):
        specs = self.specs()
        original = task_xml(specs[0]["executable"], specs[0]["script"], description="original")
        foreign = task_xml(r"C:\Other\pythonw.exe", r"C:\Other\foreign.py", description="foreign")
        runner = ConcurrentReplacementRunner(
            {"App Task": original}, "Watchdog Task", "App Task", foreign
        )
        with self.assertRaisesRegex(win.IntegrationError, "rollback verification failed"):
            register(runner, specs)
        self.assertEqual(win._xml_signature(runner.tasks["App Task"]), win._xml_signature(foreign))
        # Only the create and the failed create: no restore reached schtasks.
        self.assertEqual(["App Task", "Watchdog Task"], [name for name, _data, _path in runner.creates])
        self.assertFalse(any("/Delete" in command for command in runner.commands))

    def test_rollback_refuses_same_command_definition_drift(self):
        specs = self.specs()
        original = task_xml(specs[0]["executable"], specs[0]["script"], description="original")
        drift = task_xml(specs[0]["executable"], specs[0]["script"], description="concurrent drift")
        runner = ConcurrentReplacementRunner(
            {"App Task": original}, "Watchdog Task", "App Task", drift
        )
        with self.assertRaisesRegex(win.IntegrationError, "rollback verification failed"):
            register(runner, specs)
        self.assertEqual(win._xml_signature(runner.tasks["App Task"]), win._xml_signature(drift))
        self.assertEqual(["App Task", "Watchdog Task"], [name for name, _data, _path in runner.creates])
        self.assertFalse(any("/Delete" in command for command in runner.commands))


class RegisterEntryPointTest(unittest.TestCase):
    def test_register_tasks_runs_synthetic_specs_through_the_transaction(self):
        specs = [
            {
                "name": "GAD Synthetic Monitor",
                "executable": r"C:\Synthetic\pythonw.exe",
                "script": r"C:\Synthetic\dummy_monitor.py",
                "user": r"DOMAIN\user",
                "trigger": win.LOGON_TRIGGER,
            },
            {
                "name": "GAD Synthetic Watchdog",
                "executable": r"C:\Synthetic\pythonw.exe",
                "script": r"C:\Synthetic\dummy_watchdog.py",
                "user": r"DOMAIN\user",
                "trigger": win.INTERVAL_TRIGGER,
            },
        ]
        runner = StatefulTaskRunner()
        win.register_tasks(specs, runner=runner, resolve_sid=resolve_sid, clock=fixed_clock)
        self.assertEqual({"GAD Synthetic Monitor", "GAD Synthetic Watchdog"}, set(runner.tasks))
        schtasks = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "schtasks.exe")
        self.assertTrue(all(command[0] == schtasks for command in runner.commands))
        for spec in specs:
            win.verify_task_definition(runner.tasks[spec["name"]], spec, resolve_sid(spec["user"]), resolve_sid)

    def test_main_registers_the_tray_and_watchdog_tasks(self):
        argv = [
            "register-tasks",
            "--pythonw", r"C:\Py\pythonw.exe",
            "--app-script", r"C:\App\app\gmail_app.py",
            "--watchdog-script", r"C:\App\app\watchdog.py",
            "--user", r"PC\user",
        ]
        with mock.patch.object(win, "register_tasks") as register_tasks, \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(0, win.main(argv))
        specs = register_tasks.call_args.args[0]
        self.assertEqual(
            [
                ("Gmail Auto Downloader Monitor", r"C:\App\app\gmail_app.py", win.LOGON_TRIGGER),
                ("Gmail Auto Downloader Watchdog", r"C:\App\app\watchdog.py", win.INTERVAL_TRIGGER),
            ],
            [(spec["name"], spec["script"], spec["trigger"]) for spec in specs],
        )
        self.assertTrue(all(spec["executable"] == r"C:\Py\pythonw.exe" for spec in specs))
        self.assertTrue(all(spec["user"] == r"PC\user" for spec in specs))


class RunnerStdinTest(unittest.TestCase):
    def test_child_stdin_is_devnull(self):
        completed = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch.object(win.subprocess, "run", return_value=completed) as run:
            win.Runner().run(["schtasks.exe", "/Query"])
        self.assertIs(subprocess.DEVNULL, run.call_args.kwargs["stdin"])


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
