"""Task Scheduler XML generation, settings verification and real read-back fixtures."""
import datetime
import os
import re
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

import windows_integration as win
from tests.fixtures import task_xml_readback as fixtures


NS = {"t": win.TASK_NAMESPACE}
USER = r"PC\user"
SID = "S-1-5-21-1000-2000-3000-1001"
NOW = datetime.datetime(2026, 9, 24, 1, 2, 3, 456789)


def spec(trigger, executable=r"C:\Python\pythonw.exe", script=r"C:\App\app\gmail_app.py", user=USER):
    return {"name": "Task", "executable": executable, "script": script, "user": user, "trigger": trigger}


def resolve_sid(account):
    if account.casefold() == USER.casefold():
        return SID
    raise win.IntegrationError("fake account lookup failed")


def refuse_lookup(account):
    raise AssertionError(f"SID-form principal must not be looked up: {account}")


def parse(data):
    return ET.fromstring(data[2:].decode("utf-16-le"))


def text_of(root, path):
    node = root.find(path, NS)
    return None if node is None else node.text


def edit_settings(data, old, new):
    text = data[2:].decode("utf-16-le")
    if old not in text:
        raise AssertionError(old)
    return b"\xff\xfe" + text.replace(old, new).encode("utf-16-le")


class BuildTaskXmlTest(unittest.TestCase):
    def test_document_is_utf16le_with_bom_and_task_1_2(self):
        data = win.build_task_xml(spec(win.LOGON_TRIGGER), NOW)
        self.assertTrue(data.startswith(b"\xff\xfe"))
        text = data[2:].decode("utf-16-le")
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-16"?>\r\n'))
        self.assertNotIn("\r\r\n", text)
        self.assertIsNone(re.search(r"(?<!\r)\n", text))
        root = parse(data)
        self.assertEqual("{%s}Task" % win.TASK_NAMESPACE, root.tag)
        self.assertEqual("1.2", root.get("version"))
        self.assertEqual(["Triggers", "Principals", "Settings", "Actions"], [win._local_name(c.tag) for c in root])
        for absent in ("URI", "WorkingDirectory", "RestartOnFailure", "Duration", "DisallowStartOnRemoteAppSession"):
            self.assertNotIn(f"<{absent}>", text)

    def test_principal_and_action(self):
        root = parse(win.build_task_xml(spec(win.LOGON_TRIGGER), NOW))
        principal = root.find("t:Principals/t:Principal", NS)
        self.assertEqual("Author", principal.get("id"))
        self.assertEqual(["UserId", "LogonType", "RunLevel"], [win._local_name(c.tag) for c in principal])
        self.assertEqual(USER, text_of(principal, "t:UserId"))
        self.assertEqual("InteractiveToken", text_of(principal, "t:LogonType"))
        self.assertEqual("LeastPrivilege", text_of(principal, "t:RunLevel"))
        actions = root.find("t:Actions", NS)
        self.assertEqual("Author", actions.get("Context"))
        exec_action = actions.find("t:Exec", NS)
        self.assertEqual(["Command", "Arguments"], [win._local_name(c.tag) for c in exec_action])
        self.assertEqual(r'"C:\Python\pythonw.exe"', text_of(exec_action, "t:Command"))
        self.assertEqual(r'"C:\App\app\gmail_app.py"', text_of(exec_action, "t:Arguments"))

    def test_settings_for_both_task_kinds(self):
        expected = {
            "MultipleInstancesPolicy": "IgnoreNew",
            "DisallowStartIfOnBatteries": "false",
            "StopIfGoingOnBatteries": "false",
            "AllowHardTerminate": "true",
            "StartWhenAvailable": "false",
            "RunOnlyIfNetworkAvailable": "false",
            "IdleSettings": None,
            "AllowStartOnDemand": "true",
            "Enabled": "true",
            "Hidden": "false",
            "RunOnlyIfIdle": "false",
            "WakeToRun": "false",
            "ExecutionTimeLimit": None,
            "Priority": "7",
        }
        for trigger, limit in ((win.LOGON_TRIGGER, "PT0S"), (win.INTERVAL_TRIGGER, "PT10M")):
            with self.subTest(trigger):
                settings = parse(win.build_task_xml(spec(trigger), NOW)).find("t:Settings", NS)
                self.assertEqual(list(expected), [win._local_name(c.tag) for c in settings])
                for name, value in expected.items():
                    if value is not None:
                        self.assertEqual(value, text_of(settings, f"t:{name}"), name)
                self.assertEqual(limit, text_of(settings, "t:ExecutionTimeLimit"))
                idle = settings.find("t:IdleSettings", NS)
                self.assertEqual(
                    [("StopOnIdleEnd", "false"), ("RestartOnIdle", "false")],
                    [(win._local_name(c.tag), c.text) for c in idle],
                )

    def test_logon_trigger_is_scoped_to_the_user_with_one_minute_delay(self):
        triggers = parse(win.build_task_xml(spec(win.LOGON_TRIGGER), NOW)).find("t:Triggers", NS)
        self.assertEqual(1, len(triggers))
        logon = triggers.find("t:LogonTrigger", NS)
        self.assertEqual(
            [("Enabled", "true"), ("UserId", USER), ("Delay", "PT1M")],
            [(win._local_name(c.tag), c.text) for c in logon],
        )

    def test_interval_trigger_repeats_every_five_minutes_from_the_injected_clock(self):
        triggers = parse(win.build_task_xml(spec(win.INTERVAL_TRIGGER), NOW)).find("t:Triggers", NS)
        self.assertEqual(1, len(triggers))
        trigger = triggers.find("t:TimeTrigger", NS)
        self.assertEqual(["Enabled", "StartBoundary", "Repetition"], [win._local_name(c.tag) for c in trigger])
        self.assertEqual("2026-09-24T01:02:03", text_of(trigger, "t:StartBoundary"))
        repetition = trigger.find("t:Repetition", NS)
        self.assertEqual(
            [("Interval", "PT5M"), ("StopAtDurationEnd", "false")],
            [(win._local_name(c.tag), c.text) for c in repetition],
        )

    def test_aware_clock_is_rejected(self):
        aware = NOW.replace(tzinfo=datetime.timezone.utc)
        with self.assertRaises(win.IntegrationError):
            win.build_task_xml(spec(win.INTERVAL_TRIGGER), aware)

    def test_generated_task_is_owned_only_by_this_install(self):
        data = win.build_task_xml(spec(win.LOGON_TRIGGER), NOW)
        self.assertTrue(win.action_is_owned(data, r"C:\Python\pythonw.exe", r"C:\App\app\gmail_app.py"))
        self.assertFalse(win.action_is_owned(data, r"C:\Python\pythonw.exe", r"C:\Other\gmail_app.py"))
        self.assertFalse(win.action_is_owned(data, r"C:\Other\pythonw.exe", r"C:\App\app\gmail_app.py"))

    def test_hostile_user_and_paths_are_escaped(self):
        user = r"PC\O'Brien&Co"
        executable = r"C:\Program Files (x86)\日本語 & Co\pythonw.exe"
        script = r"C:\O'Brien & <Co> ]]> 作業\gmail_app.py"
        data = win.build_task_xml(spec(win.LOGON_TRIGGER, executable, script, user), NOW)
        text = data[2:].decode("utf-16-le")
        self.assertIn(r"<UserId>PC\O'Brien&amp;Co</UserId>", text)
        self.assertIn("&lt;Co&gt; ]]&gt;", text)
        root = parse(data)
        self.assertEqual(user, text_of(root, "t:Principals/t:Principal/t:UserId"))
        self.assertEqual(user, text_of(root, "t:Triggers/t:LogonTrigger/t:UserId"))
        self.assertEqual(f'"{executable}"', text_of(root, "t:Actions/t:Exec/t:Command"))
        self.assertEqual(f'"{script}"', text_of(root, "t:Actions/t:Exec/t:Arguments"))
        self.assertTrue(win.action_is_owned(data, executable, script))

    def test_unencodable_or_ambiguous_fields_are_rejected(self):
        cases = [
            ("user", "PC\\\udc80user"),
            ("executable", "C:\\Py\\\ud83d.exe"),
            ("script", "C:\\App\\\udfff.py"),
            ("script", "C:\\App\\a\x00b.py"),
            ("script", "C:\\App\\line\nbreak.py"),
            ("user", "PC\\user\uffff"),
            ("script", 'C:\\App\\"quoted".py'),
            ("executable", "   "),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=repr(value)):
                item = spec(win.INTERVAL_TRIGGER)
                item[field] = value
                with self.assertRaises(win.IntegrationError):
                    win.build_task_xml(item, NOW)

    def test_unknown_trigger_is_rejected(self):
        for trigger in ("ONLOGON", None, ""):
            with self.subTest(trigger=trigger):
                with self.assertRaises(win.IntegrationError):
                    win.build_task_xml(spec(trigger), NOW)


class VerifyTaskDefinitionTest(unittest.TestCase):
    def verify(self, data, trigger=win.LOGON_TRIGGER, resolver=resolve_sid):
        win.verify_task_definition(data, spec(trigger), SID, resolver)

    def test_generated_documents_verify_with_name_form_principal(self):
        for trigger in (win.LOGON_TRIGGER, win.INTERVAL_TRIGGER):
            with self.subTest(trigger):
                self.verify(win.build_task_xml(spec(trigger), NOW), trigger)

    def test_real_readbacks_verify_with_sid_principal_and_schema_defaults(self):
        # RunLevel and AllowStartOnDemand are absent from real read-back.
        self.verify(fixtures.XML_MONITOR, win.LOGON_TRIGGER, refuse_lookup)
        self.verify(fixtures.XML_WATCHDOG, win.INTERVAL_TRIGGER, refuse_lookup)

    def test_time_limit_must_match_the_task_kind(self):
        with self.assertRaisesRegex(win.IntegrationError, "ExecutionTimeLimit"):
            self.verify(fixtures.XML_MONITOR, win.INTERVAL_TRIGGER, refuse_lookup)
        with self.assertRaisesRegex(win.IntegrationError, "ExecutionTimeLimit"):
            self.verify(fixtures.XML_WATCHDOG, win.LOGON_TRIGGER, refuse_lookup)

    def test_legacy_flag_readbacks_fail_on_battery_defaults(self):
        for data, trigger in (
            (fixtures.LEGACY_WATCHDOG, win.INTERVAL_TRIGGER),
            (fixtures.LEGACY_MONITOR, win.LOGON_TRIGGER),
        ):
            with self.subTest(trigger):
                with self.assertRaisesRegex(win.IntegrationError, "DisallowStartIfOnBatteries"):
                    self.verify(data, trigger, refuse_lookup)

    def test_absent_elements_take_schema_defaults(self):
        base = win.build_task_xml(spec(win.LOGON_TRIGGER), NOW)
        cases = [
            ("    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\r\n", "DisallowStartIfOnBatteries"),
            ("    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\r\n", "StopIfGoingOnBatteries"),
            ("    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\r\n", "ExecutionTimeLimit"),
        ]
        for line, message in cases:
            with self.subTest(message):
                with self.assertRaisesRegex(win.IntegrationError, message):
                    self.verify(edit_settings(base, line, ""))
        for line in (
            "    <AllowStartOnDemand>true</AllowStartOnDemand>\r\n",
            "    <Enabled>true</Enabled>\r\n",
            "      <RunLevel>LeastPrivilege</RunLevel>\r\n",
        ):
            with self.subTest(line.strip()):
                self.verify(edit_settings(base, line, ""))

    def test_missing_settings_element_means_all_defaults(self):
        root_text = win.build_task_xml(spec(win.LOGON_TRIGGER), NOW)[2:].decode("utf-16-le")
        stripped = re.sub(r"  <Settings>.*?</Settings>\r\n", "", root_text, flags=re.S)
        with self.assertRaisesRegex(win.IntegrationError, "DisallowStartIfOnBatteries"):
            self.verify(b"\xff\xfe" + stripped.encode("utf-16-le"))

    def test_wrong_values_fail(self):
        base = win.build_task_xml(spec(win.LOGON_TRIGGER), NOW)
        cases = [
            ("<StopIfGoingOnBatteries>false<", "<StopIfGoingOnBatteries>true<", "StopIfGoingOnBatteries"),
            ("<AllowStartOnDemand>true<", "<AllowStartOnDemand>false<", "AllowStartOnDemand"),
            ("<Enabled>true</Enabled>\r\n    <Hidden>", "<Enabled>0</Enabled>\r\n    <Hidden>", "Enabled"),
            ("<ExecutionTimeLimit>PT0S<", "<ExecutionTimeLimit>PT72H<", "ExecutionTimeLimit"),
            ("<RunLevel>LeastPrivilege<", "<RunLevel>HighestAvailable<", "least privilege"),
            ("<LogonType>InteractiveToken<", "<LogonType>Password<", "interactive logon"),
            ("      <LogonType>InteractiveToken</LogonType>\r\n", "", "interactive logon"),
            (f"<UserId>{USER}</UserId>\r\n      <LogonType>", "<UserId>S-1-5-21-1-2-3-500</UserId>\r\n      <LogonType>",
             "expected user"),
            (f"<UserId>{USER}</UserId>\r\n      <LogonType>", "<LogonType>", "expected user"),
            (f"<UserId>{USER}</UserId>\r\n      <LogonType>", "<UserId>PC\\other</UserId>\r\n      <LogonType>",
             "fake account lookup failed"),
        ]
        for old, new, message in cases:
            with self.subTest(message=message, new=new):
                with self.assertRaisesRegex(win.IntegrationError, message):
                    self.verify(edit_settings(base, old, new))

    def test_numeric_booleans_are_accepted_and_garbage_is_not(self):
        base = win.build_task_xml(spec(win.LOGON_TRIGGER), NOW)
        numeric = edit_settings(
            edit_settings(base, "<DisallowStartIfOnBatteries>false<", "<DisallowStartIfOnBatteries>0<"),
            "<AllowStartOnDemand>true<",
            "<AllowStartOnDemand>1<",
        )
        self.verify(numeric)
        garbage = edit_settings(base, "<StopIfGoingOnBatteries>false<", "<StopIfGoingOnBatteries>no<")
        with self.assertRaisesRegex(win.IntegrationError, "invalid StopIfGoingOnBatteries"):
            self.verify(garbage)

    def test_duplicate_elements_are_ambiguous(self):
        base = win.build_task_xml(spec(win.LOGON_TRIGGER), NOW)
        duplicated_setting = edit_settings(
            base,
            "    <Priority>7</Priority>\r\n",
            "    <Priority>7</Priority>\r\n    <DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>\r\n",
        )
        duplicated_principal = edit_settings(
            base,
            "  </Principals>",
            '    <Principal id="Other"><UserId>S-1-5-18</UserId></Principal>\r\n  </Principals>',
        )
        for data in (duplicated_setting, duplicated_principal):
            with self.assertRaisesRegex(win.IntegrationError, "more than one"):
                self.verify(data)

    def test_sid_principal_is_compared_case_insensitively(self):
        lowered = fixtures.XML_MONITOR.replace(SID.encode("ascii"), SID.lower().encode("ascii"))
        self.verify(lowered, win.LOGON_TRIGGER, refuse_lookup)


class ReadbackFixtureTest(unittest.TestCase):
    def test_every_fixture_classifies_owned_for_its_script(self):
        cases = [
            (fixtures.LEGACY_MONITOR, fixtures.FIXTURE_APP_SCRIPT),  # quoted Command
            (fixtures.LEGACY_WATCHDOG, fixtures.FIXTURE_WATCHDOG_SCRIPT),  # quoted Command
            (fixtures.XML_MONITOR, fixtures.FIXTURE_APP_SCRIPT),  # unquoted Command
            (fixtures.XML_WATCHDOG, fixtures.FIXTURE_WATCHDOG_SCRIPT),  # unquoted Command
        ]
        for data, script in cases:
            with self.subTest(script=script):
                self.assertFalse(data.startswith(b"\xff\xfe"))
                self.assertIn(b"\r\r\n", data)
                self.assertTrue(win.action_is_owned(data, fixtures.FIXTURE_PYTHONW, script))
                self.assertFalse(win.action_is_owned(data, fixtures.FIXTURE_PYTHONW, script + ".old"))
                self.assertFalse(win.action_is_owned(data, r"C:\Other\pythonw.exe", script))

    def test_legacy_readback_lacks_the_settings_the_old_flags_never_set(self):
        text = fixtures.LEGACY_WATCHDOG.decode("ascii")
        for absent in ("<ExecutionTimeLimit>", "<RunLevel>", "<AllowStartOnDemand>"):
            self.assertNotIn(absent, text)
        self.assertIn("<DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>", text)

    def test_readback_is_reencoded_as_utf16le_with_bom_for_restore(self):
        for data in (fixtures.LEGACY_MONITOR, fixtures.LEGACY_WATCHDOG, fixtures.XML_MONITOR, fixtures.XML_WATCHDOG):
            document = win._utf16_task_document(data)
            self.assertTrue(document.startswith(b"\xff\xfe"))
            text = document[2:].decode("utf-16-le")
            self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-16"?>\r\n<Task '))
            self.assertNotIn("\r\r\n", text)
            self.assertIsNone(re.search(r"\r(?!\n)|(?<!\r)\n", text))
            self.assertEqual(win._xml_signature(data), win._xml_signature(document))

    def test_cp932_readback_with_kanji_is_reencoded(self):
        data = fixtures.LEGACY_WATCHDOG.replace(rb"C:\App\app", "C:\\業務\\app".encode("cp932"))
        with mock.patch.object(win.locale, "getpreferredencoding", return_value="cp932"):
            document = win._utf16_task_document(data)
            self.assertEqual(win._xml_signature(data), win._xml_signature(document))
        self.assertIn("C:\\業務\\app\\watchdog.py", document[2:].decode("utf-16-le"))

    def test_bom_readback_and_other_declarations_are_normalized(self):
        text = fixtures.XML_MONITOR.decode("ascii").replace("\r\r\n", "\n")
        for data in (
            b"\xff\xfe" + text.encode("utf-16-le"),
            text.replace('encoding="UTF-16"', 'encoding="utf-8"').encode("utf-8"),
            text.replace('<?xml version="1.0" encoding="UTF-16"?>\n', "").encode("ascii"),
        ):
            document = win._utf16_task_document(data)
            self.assertEqual(1, document[2:].decode("utf-16-le").count("<?xml"))
            self.assertEqual(win._xml_signature(fixtures.XML_MONITOR), win._xml_signature(document))

    def test_invalid_readback_is_rejected(self):
        for data in (b"<Task><Unclosed></Task>", b"\xff\xfe\x00\xd8<\x00"):
            with self.subTest(data=data):
                with self.assertRaises(win.IntegrationError):
                    win._utf16_task_document(data)


@unittest.skipUnless(os.name == "nt", "Windows account lookup")
class LookupAccountSidTest(unittest.TestCase):
    def test_current_user_resolves_to_a_sid(self):
        account = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
        self.assertRegex(win.lookup_account_sid(account), r"^S-1-5-\d+(-\d+)+$")

    def test_unknown_local_account_fails_loud(self):
        with self.assertRaises(win.IntegrationError):
            win.lookup_account_sid(f"{os.environ['COMPUTERNAME']}\\gad-no-such-user-7f3c9e")

    def test_machine_domain_name_is_not_a_user_account(self):
        # The computer name resolves to the local account domain (SidTypeDomain).
        with self.assertRaisesRegex(win.IntegrationError, "not a user account"):
            win.lookup_account_sid(os.environ["COMPUTERNAME"])


class FakeApi:
    def __init__(self, impl):
        self.impl = impl

    def __call__(self, *args):
        return self.impl(*args)


def fake_windll(sid_type, calls):
    """advapi32/kernel32 stand-ins: LookupAccountNameW reports `sid_type`."""

    def lookup(system, account, sid, sid_size, domain, domain_size, use):
        calls.append(("lookup", account))
        sid_size._obj.value = 16
        domain_size._obj.value = 3
        use._obj.value = sid_type
        return 0 if sid is None else 1

    def convert(sid, string_sid):
        calls.append(("convert",))
        string_sid._obj.value = SID
        return 1

    def factory(name, use_last_error=False):
        return mock.Mock(
            LookupAccountNameW=FakeApi(lookup),
            ConvertSidToStringSidW=FakeApi(convert),
            LocalFree=FakeApi(lambda pointer: None),
        )

    return factory


@unittest.skipUnless(os.name == "nt", "Windows account lookup")
class LookupAccountSidTypeTest(unittest.TestCase):
    def test_user_account_resolves(self):
        calls = []
        with mock.patch("ctypes.WinDLL", side_effect=fake_windll(1, calls)):
            self.assertEqual(SID, win.lookup_account_sid(USER))
        self.assertEqual([("lookup", USER), ("lookup", USER), ("convert",)], calls)

    def test_non_user_sid_types_are_rejected(self):
        # Group, Domain, Alias, WellKnownGroup, DeletedAccount, Invalid, Unknown, Computer, Label.
        for sid_type in (2, 3, 4, 5, 6, 7, 8, 9, 10):
            with self.subTest(sid_type=sid_type):
                calls = []
                with mock.patch("ctypes.WinDLL", side_effect=fake_windll(sid_type, calls)):
                    with self.assertRaisesRegex(win.IntegrationError, f"not a user account \\(SID type {sid_type}\\)"):
                        win.lookup_account_sid(r"PC\Administrators")
                self.assertNotIn(("convert",), calls)

    def test_group_account_blocks_registration_before_any_task_scheduler_call(self):
        runner = mock.Mock(spec=win.Runner)
        with mock.patch("ctypes.WinDLL", side_effect=fake_windll(4, [])):
            with self.assertRaisesRegex(win.IntegrationError, "not a user account"):
                win.register_tasks_transaction(
                    runner, "schtasks.exe", [spec(win.LOGON_TRIGGER, user=r"PC\Administrators")], clock=lambda: NOW
                )
        runner.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
