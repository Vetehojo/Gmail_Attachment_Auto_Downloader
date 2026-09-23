"""Fail-safe Windows Task Scheduler and process integration helpers."""
from __future__ import annotations

import argparse
import csv
import io
import locale
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass


OWNED = "owned"
ABSENT = "absent"
FOREIGN = "foreign"
UNKNOWN = "unknown"


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


class Runner:
    def run(self, command: list[str]) -> CommandResult:
        completed = subprocess.run(command, capture_output=True, check=False)
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
    actions = []
    for element in root.iter():
        if _local_name(element.tag) != "Exec":
            continue
        command = ""
        arguments = ""
        for child in element:
            if _local_name(child.tag) == "Command":
                command = child.text or ""
            elif _local_name(child.tag) == "Arguments":
                arguments = child.text or ""
        actions.append((command.strip(), arguments.strip()))
    if len(actions) != 1 or not actions[0][0]:
        raise IntegrationError("Task must contain exactly one executable action")
    return actions[0]


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


def query_task_xml(runner: Runner, schtasks: str, name: str) -> bytes:
    result = runner.run([schtasks, "/Query", "/TN", name, "/XML"])
    if result.returncode != 0 or not result.stdout:
        raise IntegrationError("Task XML query failed")
    parse_task_action(result.stdout)
    return result.stdout


def classify_task(
    runner: Runner, schtasks: str, name: str, executable: str, script: str
) -> tuple[str, bytes | None]:
    listing = runner.run([schtasks, "/Query", "/FO", "CSV", "/NH"])
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


def _create_task(
    runner: Runner,
    schtasks: str,
    spec: dict[str, object],
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
    command = [
        schtasks,
        "/Create",
        "/F",
        "/TN",
        str(spec["name"]),
        "/TR",
        '"%s" "%s"' % (spec["executable"], spec["script"]),
        "/RU",
        str(spec["user"]),
        "/IT",
        "/RL",
        "LIMITED",
    ] + list(spec["schedule"])
    result = runner.run(command)
    if result.returncode != 0:
        raise IntegrationError("Task creation failed")
    state, installed_xml = classify_task(
        runner, schtasks, str(spec["name"]), str(spec["executable"]), str(spec["script"])
    )
    if state != OWNED or installed_xml is None:
        raise IntegrationError("Created task identity could not be verified")
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
        deleted = runner.run([schtasks, "/Delete", "/F", "/TN", str(spec["name"])])
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
    handle, path = tempfile.mkstemp(prefix="gmail-task-", suffix=".xml")
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(original_xml)
        restored = runner.run(
            [schtasks, "/Create", "/F", "/TN", str(spec["name"]), "/XML", path]
        )
        if restored.returncode != 0:
            raise IntegrationError("Rollback could not restore the original task")
        current_xml = query_task_xml(runner, schtasks, str(spec["name"]))
        if _xml_signature(current_xml) != _xml_signature(original_xml):
            raise IntegrationError("Rollback task state does not match the original XML")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def register_tasks_transaction(runner: Runner, schtasks: str, specs: list[dict[str, object]]) -> None:
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
            installed_xml = _create_task(runner, schtasks, spec, *originals[index])
            mutated.append((index, installed_xml))
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
                    "schedule": ["/SC", "ONLOGON", "/DELAY", "0001:00"],
                },
                {
                    "name": "Gmail Auto Downloader Watchdog",
                    "executable": args.pythonw,
                    "script": args.watchdog_script,
                    "user": args.user,
                    "schedule": ["/SC", "MINUTE", "/MO", "5"],
                },
            ]
            register_tasks_transaction(runner, _system_tool("schtasks.exe"), specs)
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
