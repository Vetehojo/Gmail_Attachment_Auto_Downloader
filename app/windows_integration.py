"""Fail-safe Windows Task Scheduler and process integration helpers."""
from __future__ import annotations

import argparse
import csv
import datetime
import io
import locale
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from xml.sax.saxutils import escape as _xml_escape


OWNED = "owned"
ABSENT = "absent"
FOREIGN = "foreign"
UNKNOWN = "unknown"

# Task kinds. The tray runs from a logon trigger with no time limit; the
# watchdog repeats every five minutes and is bounded.
LOGON_TRIGGER = "logon"
INTERVAL_TRIGGER = "interval"
EXECUTION_TIME_LIMITS = {LOGON_TRIGGER: "PT0S", INTERVAL_TRIGGER: "PT10M"}

TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_UTF16_BOM = b"\xff\xfe"
_SID_PATTERN = re.compile(r"^S-1-\d+(-\d+)+$", re.IGNORECASE)
_SID_TYPE_USER = 1  # SID_NAME_USE.SidTypeUser
_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>\s*")
# XML 1.0 forbids C0 controls and U+FFFE/U+FFFF; task fields never contain them.
_FORBIDDEN_XML_CHARS = re.compile("[\x00-\x1f" + chr(0xFFFE) + chr(0xFFFF) + "]")


class IntegrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


@dataclass(frozen=True)
class ProcessQuery:
    session_id: int
    owner_sid: str
    rows: list[dict[str, str]]


def _has_console() -> bool:
    if os.name != "nt":
        return True
    import ctypes

    return ctypes.windll.kernel32.GetConsoleCP() != 0


def _creation_flags() -> int:
    # pythonw hosts have no console, so each console child would open a window.
    # Never use DETACHED_PROCESS: PowerShell needs a (hidden) console for stdout.
    if _has_console():
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Runner:
    def run(self, command: list[str]) -> CommandResult:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            creationflags=_creation_flags(),
            stdin=subprocess.DEVNULL,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _decode_candidate(data: bytes, encoding: str) -> str | None:
    try:
        return data.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        return None


def decode_windows_output(data: bytes) -> str:
    """Decode BOM output first, then known pipe encodings without replacement."""
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le")
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be")

    candidates = ["utf-8-sig", locale.getpreferredencoding(False), "mbcs", "cp932"]
    tried = set()
    for encoding in candidates:
        key = encoding.lower()
        if key in tried:
            continue
        tried.add(key)
        text = _decode_candidate(data, encoding)
        if text is None:
            continue
        if "\ufffd" in text:
            continue
        return text
    raise IntegrationError("Windows command output has an unknown encoding")


def _canonical_path(path: str) -> str:
    value = os.path.expandvars(path.strip().strip('"'))
    if not value or "\x00" in value:
        raise IntegrationError("Invalid path in Windows integration state")
    return os.path.normcase(os.path.realpath(os.path.abspath(value)))


def _single_path_argument(arguments: str) -> str | None:
    value = (arguments or "").strip()
    if not value:
        return None
    if value.startswith('"'):
        if len(value) < 2 or not value.endswith('"') or '"' in value[1:-1]:
            return None
        return value[1:-1]
    if any(char.isspace() for char in value):
        return None
    return value


def parse_task_names(csv_bytes: bytes) -> set[str]:
    text = decode_windows_output(csv_bytes)
    names: set[str] = set()
    saw_row = False
    for row in csv.reader(io.StringIO(text)):
        if not row or not any(field.strip() for field in row):
            continue
        saw_row = True
        if len(row) < 3 or not row[0].startswith("\\"):
            raise IntegrationError("Task Scheduler listing is not structural CSV")
        names.add(row[0])
    if not saw_row:
        raise IntegrationError("Task Scheduler listing is empty")
    return names


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_task_action(xml_bytes: bytes) -> tuple[str, str]:
    try:
        root = ET.fromstring(decode_windows_output(xml_bytes))
    except (ET.ParseError, UnicodeError) as exc:
        raise IntegrationError("Task Scheduler XML is invalid") from exc
    # Every action of every Actions block counts: an extra ComHandler, e-mail
    # or message action makes the task something other than this app's task.
    actions = [
        action
        for block in root
        if _local_name(block.tag) == "Actions"
        for action in block
    ]
    if len(actions) != 1 or _local_name(actions[0].tag) != "Exec":
        raise IntegrationError("Task must contain exactly one executable action")
    command = ""
    arguments = ""
    for child in actions[0]:
        if _local_name(child.tag) == "Command":
            command = child.text or ""
        elif _local_name(child.tag) == "Arguments":
            arguments = child.text or ""
    if not command.strip():
        raise IntegrationError("Task must contain exactly one executable action")
    return command.strip(), arguments.strip()


def action_is_owned(xml_bytes: bytes, executable: str, script: str) -> bool:
    command, arguments = parse_task_action(xml_bytes)
    script_argument = _single_path_argument(arguments)
    if script_argument is None:
        return False
    try:
        return (
            _canonical_path(command) == _canonical_path(executable)
            and _canonical_path(script_argument) == _canonical_path(script)
        )
    except IntegrationError:
        return False


def _task_name(name: str) -> str:
    return "\\" + name.lstrip("\\")


def _run_tool(runner: Runner, command: list[str]) -> CommandResult:
    # A tool that cannot be started is an IntegrationError, so callers classify
    # it as unknown and the registration transaction still rolls back.
    try:
        return runner.run(command)
    except OSError as exc:
        raise IntegrationError(f"{os.path.basename(command[0])} could not be run: {exc}") from exc


def query_task_xml(runner: Runner, schtasks: str, name: str) -> bytes:
    result = _run_tool(runner, [schtasks, "/Query", "/TN", name, "/XML"])
    if result.returncode != 0 or not result.stdout:
        raise IntegrationError("Task XML query failed")
    parse_task_action(result.stdout)
    return result.stdout


def classify_task(
    runner: Runner, schtasks: str, name: str, executable: str, script: str
) -> tuple[str, bytes | None]:
    try:
        listing = _run_tool(runner, [schtasks, "/Query", "/FO", "CSV", "/NH"])
    except IntegrationError:
        return UNKNOWN, None
    if listing.returncode != 0:
        return UNKNOWN, None
    try:
        names = parse_task_names(listing.stdout)
    except IntegrationError:
        return UNKNOWN, None
    if _task_name(name).casefold() not in {item.casefold() for item in names}:
        return ABSENT, None
    try:
        xml_bytes = query_task_xml(runner, schtasks, name)
        return (OWNED if action_is_owned(xml_bytes, executable, script) else FOREIGN), xml_bytes
    except IntegrationError:
        return UNKNOWN, None


def _xml_signature(xml_bytes: bytes):
    try:
        root = ET.fromstring(decode_windows_output(xml_bytes))
    except (ET.ParseError, UnicodeError) as exc:
        raise IntegrationError("Task Scheduler XML is invalid") from exc

    def signature(element):
        text = (element.text or "").strip()
        attributes = tuple(sorted(element.attrib.items()))
        return (_local_name(element.tag), attributes, text, tuple(signature(child) for child in element))

    return signature(root)


def _xml_field(value: object, label: str, *, quoted: bool = False) -> str:
    text = str(value)
    if not text.strip():
        raise IntegrationError(f"Task {label} is empty")
    try:
        text.encode("utf-16-le")
    except UnicodeEncodeError as exc:
        raise IntegrationError(f"Task {label} is not valid Unicode") from exc
    if _FORBIDDEN_XML_CHARS.search(text):
        raise IntegrationError(f"Task {label} contains control characters")
    if quoted and '"' in text:
        raise IntegrationError(f"Task {label} contains a double quote")
    return _xml_escape(text)


def build_task_xml(spec: dict[str, object], now: datetime.datetime) -> bytes:
    """Return the UTF-16LE+BOM Task Scheduler 1.2 definition for one task spec.

    `now` is naive local time; it becomes the interval trigger's StartBoundary.
    """
    trigger = spec.get("trigger")
    if trigger not in EXECUTION_TIME_LIMITS:
        raise IntegrationError("Task trigger kind is unknown")
    if now.tzinfo is not None:
        raise IntegrationError("Task start time must be naive local time")
    user = _xml_field(spec["user"], "user")
    command = _xml_field(spec["executable"], "executable", quoted=True)
    script = _xml_field(spec["script"], "script", quoted=True)
    if trigger == LOGON_TRIGGER:
        trigger_lines = [
            "    <LogonTrigger>",
            "      <Enabled>true</Enabled>",
            f"      <UserId>{user}</UserId>",
            "      <Delay>PT1M</Delay>",
            "    </LogonTrigger>",
        ]
    else:
        start = now.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
        trigger_lines = [
            "    <TimeTrigger>",
            "      <Enabled>true</Enabled>",
            f"      <StartBoundary>{start}</StartBoundary>",
            "      <Repetition>",
            "        <Interval>PT5M</Interval>",
            "        <StopAtDurationEnd>false</StopAtDurationEnd>",
            "      </Repetition>",
            "    </TimeTrigger>",
        ]
    lines = [
        '<?xml version="1.0" encoding="UTF-16"?>',
        f'<Task version="1.2" xmlns="{TASK_NAMESPACE}">',
        "  <Triggers>",
        *trigger_lines,
        "  </Triggers>",
        "  <Principals>",
        '    <Principal id="Author">',
        f"      <UserId>{user}</UserId>",
        "      <LogonType>InteractiveToken</LogonType>",
        "      <RunLevel>LeastPrivilege</RunLevel>",
        "    </Principal>",
        "  </Principals>",
        "  <Settings>",
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>",
        "    <AllowHardTerminate>true</AllowHardTerminate>",
        "    <StartWhenAvailable>false</StartWhenAvailable>",
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>",
        "    <IdleSettings>",
        "      <StopOnIdleEnd>false</StopOnIdleEnd>",
        "      <RestartOnIdle>false</RestartOnIdle>",
        "    </IdleSettings>",
        "    <AllowStartOnDemand>true</AllowStartOnDemand>",
        "    <Enabled>true</Enabled>",
        "    <Hidden>false</Hidden>",
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>",
        "    <WakeToRun>false</WakeToRun>",
        f"    <ExecutionTimeLimit>{EXECUTION_TIME_LIMITS[trigger]}</ExecutionTimeLimit>",
        "    <Priority>7</Priority>",
        "  </Settings>",
        '  <Actions Context="Author">',
        "    <Exec>",
        f'      <Command>"{command}"</Command>',
        f'      <Arguments>"{script}"</Arguments>',
        "    </Exec>",
        "  </Actions>",
        "</Task>",
    ]
    return _UTF16_BOM + ("\r\n".join(lines) + "\r\n").encode("utf-16-le")


def _utf16_task_document(xml_bytes: bytes) -> bytes:
    """Re-encode a /Query /XML read-back as the UTF-16LE+BOM file /Create /XML expects.

    Piped read-back has no BOM, declares UTF-16 over code-page bytes and ends
    lines with CR CR LF, so it cannot be handed back to schtasks as-is.
    """
    try:
        text = decode_windows_output(xml_bytes)
    except UnicodeError as exc:
        raise IntegrationError("Task Scheduler XML is invalid") from exc
    text = text.replace("\r\r\n", "\r\n")
    text = re.sub(r"\r\n|\r|\n", "\r\n", text)
    text = '<?xml version="1.0" encoding="UTF-16"?>\r\n' + _XML_DECLARATION.sub("", text, count=1)
    try:
        ET.fromstring(text)
        data = text.encode("utf-16-le")
    except (ET.ParseError, UnicodeError) as exc:
        raise IntegrationError("Task Scheduler XML is invalid") from exc
    return _UTF16_BOM + data


def _run_create_xml(runner: Runner, schtasks: str, name: str, document: bytes) -> CommandResult:
    # The document is fully built before the private temp file exists, and the
    # file is closed before schtasks reads it and removed afterwards.
    try:
        handle, path = tempfile.mkstemp(prefix="gmail-task-", suffix=".xml")
    except OSError as exc:
        raise IntegrationError(f"Temporary task XML could not be created: {exc}") from exc
    try:
        try:
            with os.fdopen(handle, "wb") as output:
                output.write(document)
        except OSError as exc:
            raise IntegrationError(f"Temporary task XML could not be written: {exc}") from exc
        return _run_tool(runner, [schtasks, "/Create", "/F", "/TN", name, "/XML", path])
    finally:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[WARN] Temporary task XML could not be deleted: {exc}", file=sys.stderr)


def lookup_account_sid(account: str) -> str:
    """Resolve a Windows account name (DOMAIN\\user) to its string SID."""
    if os.name != "nt":
        raise IntegrationError("Windows account lookup is unavailable")
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    lookup = advapi32.LookupAccountNameW
    lookup.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(ctypes.c_int),
    ]
    lookup.restype = wintypes.BOOL
    convert = advapi32.ConvertSidToStringSidW
    convert.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    convert.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    sid_size = wintypes.DWORD(0)
    domain_size = wintypes.DWORD(0)
    use = ctypes.c_int(0)
    lookup(None, account, None, ctypes.byref(sid_size), None, ctypes.byref(domain_size), ctypes.byref(use))
    if not sid_size.value:
        raise IntegrationError(
            f"Windows account could not be resolved (error {ctypes.get_last_error()})"
        )
    sid = ctypes.create_string_buffer(sid_size.value)
    domain = ctypes.create_unicode_buffer(max(domain_size.value, 1))
    if not lookup(
        None, account, sid, ctypes.byref(sid_size), domain, ctypes.byref(domain_size), ctypes.byref(use)
    ):
        raise IntegrationError(
            f"Windows account could not be resolved (error {ctypes.get_last_error()})"
        )
    if use.value != _SID_TYPE_USER:
        raise IntegrationError(f"Windows account is not a user account (SID type {use.value})")
    string_sid = wintypes.LPWSTR()
    if not convert(sid, ctypes.byref(string_sid)):
        raise IntegrationError(
            f"Windows account SID could not be formatted (error {ctypes.get_last_error()})"
        )
    try:
        return string_sid.value or ""
    finally:
        kernel32.LocalFree(ctypes.cast(string_sid, ctypes.c_void_p))


def _account_sid(account: str, resolve_sid) -> str:
    value = account.strip()
    if _SID_PATTERN.match(value):
        return value.upper()
    sid = resolve_sid(value)
    if not isinstance(sid, str) or not _SID_PATTERN.match(sid.strip()):
        raise IntegrationError("Windows account SID could not be resolved")
    return sid.strip().upper()


def _parse_task_root(xml_bytes: bytes) -> ET.Element:
    try:
        return ET.fromstring(decode_windows_output(xml_bytes))
    except (ET.ParseError, UnicodeError) as exc:
        raise IntegrationError("Task Scheduler XML is invalid") from exc


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local_name(child.tag) == name]


def _single_child(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None
    matches = _children(element, name)
    if len(matches) > 1:
        raise IntegrationError(f"Task XML has more than one {name}")
    return matches[0] if matches else None


def _text_or_default(element: ET.Element | None, name: str, default: str) -> str:
    # An absent element means the Task Scheduler schema default applies.
    child = _single_child(element, name)
    return default if child is None else (child.text or "").strip()


def _xml_bool(value: str, name: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("true", "1"):
        return True
    if lowered in ("false", "0"):
        return False
    raise IntegrationError(f"Task XML has an invalid {name} value")


# (schema default, required value) for each boolean setting this app relies on.
_REQUIRED_BOOLEAN_SETTINGS = {
    "DisallowStartIfOnBatteries": ("true", False),
    "StopIfGoingOnBatteries": ("true", False),
    "AllowStartOnDemand": ("true", True),
    "Enabled": ("true", True),
}


def verify_task_definition(
    xml_bytes: bytes, spec: dict[str, object], expected_sid: str, resolve_sid
) -> None:
    """Check a read-back task's principal and settings; raise IntegrationError on mismatch."""
    root = _parse_task_root(xml_bytes)
    principal = _single_child(_single_child(root, "Principals"), "Principal")
    if principal is None:
        raise IntegrationError("Task has no principal")
    user_id = _text_or_default(principal, "UserId", "")
    if not user_id or _account_sid(user_id, resolve_sid) != expected_sid.upper():
        raise IntegrationError("Task principal is not the expected user")
    if _text_or_default(principal, "LogonType", "") != "InteractiveToken":
        raise IntegrationError("Task principal does not use the interactive logon")
    if _text_or_default(principal, "RunLevel", "LeastPrivilege") != "LeastPrivilege":
        raise IntegrationError("Task principal does not run with least privilege")
    settings = _single_child(root, "Settings")
    for name, (default, required) in _REQUIRED_BOOLEAN_SETTINGS.items():
        if _xml_bool(_text_or_default(settings, name, default), name) != required:
            raise IntegrationError(f"Task setting {name} was not applied")
    limit = EXECUTION_TIME_LIMITS.get(spec.get("trigger"))
    if limit is None:
        raise IntegrationError("Task trigger kind is unknown")
    if _text_or_default(settings, "ExecutionTimeLimit", "PT72H") != limit:
        raise IntegrationError("Task setting ExecutionTimeLimit was not applied")


def _create_task(
    runner: Runner,
    schtasks: str,
    spec: dict[str, object],
    document: bytes,
    original_state: str,
    original_xml: bytes | None,
) -> bytes:
    current_state, current_xml = classify_task(
        runner, schtasks, str(spec["name"]), str(spec["executable"]), str(spec["script"])
    )
    if current_state != original_state:
        raise IntegrationError("Task state changed after preflight")
    if original_state == OWNED:
        if original_xml is None or current_xml is None:
            raise IntegrationError("Owned task XML disappeared after preflight")
        if _xml_signature(current_xml) != _xml_signature(original_xml):
            raise IntegrationError("Owned task definition changed after preflight")
    result = _run_create_xml(runner, schtasks, str(spec["name"]), document)
    if result.returncode != 0:
        raise IntegrationError("Task creation failed")
    state, installed_xml = classify_task(
        runner, schtasks, str(spec["name"]), str(spec["executable"]), str(spec["script"])
    )
    if state != OWNED or installed_xml is None:
        raise IntegrationError(
            f"Created task '{spec['name']}' could not be verified as this install's task "
            "(install paths with characters outside the Windows code page cannot be registered)"
        )
    return installed_xml


def _restore_task(
    runner: Runner,
    schtasks: str,
    spec: dict[str, object],
    original_state: str,
    original_xml: bytes | None,
    installed_xml: bytes,
) -> None:
    state, _current = classify_task(
        runner, schtasks, str(spec["name"]), str(spec["executable"]), str(spec["script"])
    )
    if state != OWNED or _current is None:
        raise IntegrationError("Rollback refused to touch a task that is no longer owned")
    if _xml_signature(_current) != _xml_signature(installed_xml):
        raise IntegrationError("Rollback refused to overwrite task definition drift")
    if original_state == ABSENT:
        deleted = _run_tool(runner, [schtasks, "/Delete", "/F", "/TN", str(spec["name"])])
        if deleted.returncode != 0:
            raise IntegrationError("Rollback could not delete the newly-created task")
        state, _ = classify_task(
            runner, schtasks, str(spec["name"]), str(spec["executable"]), str(spec["script"])
        )
        if state != ABSENT:
            raise IntegrationError("Rollback deletion could not be verified")
        return

    if original_state != OWNED or original_xml is None:
        raise IntegrationError("Rollback has no verified original task state")
    restored = _run_create_xml(
        runner, schtasks, str(spec["name"]), _utf16_task_document(original_xml)
    )
    if restored.returncode != 0:
        raise IntegrationError("Rollback could not restore the original task")
    current_xml = query_task_xml(runner, schtasks, str(spec["name"]))
    if _xml_signature(current_xml) != _xml_signature(original_xml):
        raise IntegrationError("Rollback task state does not match the original XML")


def register_tasks_transaction(
    runner: Runner,
    schtasks: str,
    specs: list[dict[str, object]],
    *,
    resolve_sid=None,
    clock=None,
) -> None:
    resolve = resolve_sid or lookup_account_sid
    now = (clock or datetime.datetime.now)()
    # Everything that can be rejected without touching Task Scheduler is
    # rejected before the first query.
    documents = [build_task_xml(spec, now) for spec in specs]
    expected_sids = [_account_sid(str(spec["user"]), resolve) for spec in specs]

    originals: list[tuple[str, bytes | None]] = []
    for spec in specs:
        state, xml_bytes = classify_task(
            runner, schtasks, str(spec["name"]), str(spec["executable"]), str(spec["script"])
        )
        if state not in (OWNED, ABSENT):
            raise IntegrationError("Task preflight found foreign or unknown state")
        originals.append((state, xml_bytes))

    mutated: list[tuple[int, bytes]] = []
    try:
        for index, spec in enumerate(specs):
            installed_xml = _create_task(runner, schtasks, spec, documents[index], *originals[index])
            mutated.append((index, installed_xml))
            # Owned and recorded for rollback before its settings are judged.
            verify_task_definition(installed_xml, spec, expected_sids[index], resolve)
    except IntegrationError as original_error:
        rollback_errors = []
        for index, installed_xml in reversed(mutated):
            try:
                _restore_task(
                    runner, schtasks, specs[index], *originals[index], installed_xml
                )
            except IntegrationError as exc:
                rollback_errors.append(str(exc))
        if rollback_errors:
            raise IntegrationError(
                f"{original_error}; rollback verification failed: {'; '.join(rollback_errors)}"
            ) from original_error
        raise


def parse_process_csv(csv_bytes: bytes) -> ProcessQuery:
    text = decode_windows_output(csv_bytes)
    reader = csv.DictReader(io.StringIO(text))
    required = {"RecordType", "ProcessId", "Name", "CommandLine", "SessionId", "OwnerSid"}
    if reader.fieldnames is None or not required.issubset(reader.fieldnames):
        raise IntegrationError("Process query output is not structural CSV")
    rows = [{key: value or "" for key, value in row.items()} for row in reader]
    contexts = [row for row in rows if row["RecordType"] == "Context"]
    processes = [row for row in rows if row["RecordType"] == "Process"]
    if len(contexts) != 1 or len(contexts) + len(processes) != len(rows):
        raise IntegrationError("Process query context is missing or ambiguous")
    try:
        session_id = int(contexts[0]["SessionId"])
    except (TypeError, ValueError) as exc:
        raise IntegrationError("Current Windows session is unknown") from exc
    owner_sid = contexts[0]["OwnerSid"].strip()
    if not owner_sid.upper().startswith("S-"):
        raise IntegrationError("Current Windows owner SID is unknown")
    return ProcessQuery(session_id, owner_sid, processes)


def _command_line_args(command_line: str) -> list[str]:
    if os.name != "nt":
        raise IntegrationError("Windows command line parsing is unavailable")
    import ctypes
    from ctypes import wintypes

    count = ctypes.c_int()
    shell32 = ctypes.windll.shell32
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    argv = shell32.CommandLineToArgvW(command_line, ctypes.byref(count))
    if not argv:
        raise IntegrationError("Windows command line could not be parsed")
    try:
        return [argv[index] for index in range(count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)


def classify_monitor_process(row: dict[str, str], script: str, query: ProcessQuery) -> str:
    if row.get("Name", "").casefold() not in {"python.exe", "pythonw.exe"}:
        return FOREIGN
    try:
        session_id = int(row.get("SessionId", ""))
    except (TypeError, ValueError):
        return UNKNOWN
    if session_id != query.session_id:
        return FOREIGN
    owner_sid = row.get("OwnerSid", "").strip()
    if not owner_sid:
        return UNKNOWN
    if owner_sid.casefold() != query.owner_sid.casefold():
        return FOREIGN
    command_line = row.get("CommandLine", "")
    if not command_line:
        return UNKNOWN
    try:
        arguments = _command_line_args(command_line)
        if len(arguments) < 2:
            return FOREIGN
        return OWNED if _canonical_path(arguments[1]) == _canonical_path(script) else FOREIGN
    except IntegrationError:
        return UNKNOWN


def process_is_owned(row: dict[str, str], script: str, query: ProcessQuery) -> bool:
    return classify_monitor_process(row, script, query) == OWNED


def _query_processes(runner: Runner, powershell: str, pid: int | None = None) -> ProcessQuery:
    process_filter = "(Name = 'python.exe' OR Name = 'pythonw.exe')"
    if pid is not None:
        process_filter = f"ProcessId = {pid} AND {process_filter}"
    query = f'Get-CimInstance Win32_Process -Filter "{process_filter}"'
    script = (
        "$ErrorActionPreference='Stop'; "
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
        "$session=[System.Diagnostics.Process]::GetCurrentProcess().SessionId; "
        "$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
        "$context=[pscustomobject]@{RecordType='Context';ProcessId='';Name='';CommandLine='';"
        "SessionId=$session;OwnerSid=$sid}; "
        "$processes=@("
        + query
        + " | ForEach-Object { $ownerSid=''; "
        "try { $owner=Invoke-CimMethod -InputObject $_ -MethodName GetOwnerSid -ErrorAction Stop; "
        "if ($owner.ReturnValue -eq 0) { $ownerSid=$owner.Sid } } catch {} "
        "[pscustomobject]@{RecordType='Process';ProcessId=$_.ProcessId;Name=$_.Name;"
        "CommandLine=$_.CommandLine;SessionId=$_.SessionId;OwnerSid=$ownerSid} }); "
        "@($context)+@($processes) | ConvertTo-Csv -NoTypeInformation"
    )
    result = runner.run([powershell, "-NoProfile", "-Command", script])
    if result.returncode != 0:
        raise IntegrationError("Windows process query failed")
    return parse_process_csv(result.stdout)


def owned_monitor_pids(runner: Runner, powershell: str, script: str) -> list[int]:
    query = _query_processes(runner, powershell)
    pids = []
    for row in query.rows:
        state = classify_monitor_process(row, script, query)
        if state == UNKNOWN:
            raise IntegrationError("Python process ownership could not be classified")
        if state != OWNED:
            continue
        try:
            pids.append(int(row["ProcessId"]))
        except (KeyError, ValueError) as exc:
            raise IntegrationError("Process query returned an invalid PID") from exc
    return pids


def stop_owned_monitors(
    runner: Runner, powershell: str, taskkill: str, script: str
) -> int:
    candidate_pids = owned_monitor_pids(runner, powershell, script)
    stopped = 0
    for pid in candidate_pids:
        current_query = _query_processes(runner, powershell, pid)
        if not current_query.rows:
            continue
        if (
            len(current_query.rows) != 1
            or classify_monitor_process(current_query.rows[0], script, current_query) != OWNED
        ):
            raise IntegrationError("Process identity changed or became unreadable before stop")
        result = runner.run([taskkill, "/F", "/T", "/PID", str(pid)])
        if result.returncode != 0:
            remaining = _query_processes(runner, powershell, pid)
            if remaining.rows:
                raise IntegrationError("Owned monitor process could not be stopped")
        stopped += 1
    return stopped


def _system_tool(name: str) -> str:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    return os.path.join(system_root, "System32", name)


def powershell_path() -> str:
    return os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32",
        "WindowsPowerShell",
        "v1.0",
        "powershell.exe",
    )


def taskkill_path() -> str:
    return _system_tool("taskkill.exe")


def register_tasks(
    specs: list[dict[str, object]], *, runner: Runner | None = None, resolve_sid=None, clock=None
) -> None:
    """Register task specs through the same transaction register-tasks uses.

    Each spec is {"name", "executable", "script", "user", "trigger"}: trigger
    LOGON_TRIGGER is the tray task (logon + 1 minute, no time limit) and
    INTERVAL_TRIGGER the watchdog task (every 5 minutes, 10 minute limit).
    Uses the real schtasks.exe unless a runner is injected; raises
    IntegrationError after rolling back on failure.
    """
    register_tasks_transaction(
        runner or Runner(),
        _system_tool("schtasks.exe"),
        list(specs),
        resolve_sid=resolve_sid,
        clock=clock,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    register = subparsers.add_parser("register-tasks")
    register.add_argument("--pythonw", required=True)
    register.add_argument("--app-script", required=True)
    register.add_argument("--watchdog-script", required=True)
    register.add_argument("--user", required=True)

    stop = subparsers.add_parser("stop-monitor")
    stop.add_argument("--script", required=True)

    args = parser.parse_args(argv)
    runner = Runner()
    try:
        if args.command == "register-tasks":
            specs = [
                {
                    "name": "Gmail Auto Downloader Monitor",
                    "executable": args.pythonw,
                    "script": args.app_script,
                    "user": args.user,
                    "trigger": LOGON_TRIGGER,
                },
                {
                    "name": "Gmail Auto Downloader Watchdog",
                    "executable": args.pythonw,
                    "script": args.watchdog_script,
                    "user": args.user,
                    "trigger": INTERVAL_TRIGGER,
                },
            ]
            register_tasks(specs, runner=runner)
            print("Scheduled tasks registered and verified.")
        else:
            stopped = stop_owned_monitors(
                runner,
                powershell_path(),
                taskkill_path(),
                args.script,
            )
            print(f"Stopped {stopped} owned monitor process(es).")
        return 0
    except IntegrationError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
