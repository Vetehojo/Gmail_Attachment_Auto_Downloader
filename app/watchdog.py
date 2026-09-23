"""Watchdog for Gmail Auto Downloader.

Scheduled every five minutes. It supervises the monitor process and the
separate heartbeat file. It does not use the Gmail mail cursor as health data.
"""
import json
import os
import subprocess
import sys
import time

import app_settings
from job_queue import JobQueue
from runtime_state import append_log, read_heartbeat, settings_update_in_progress
import windows_integration


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(app_settings.BASE_DIR, "state")
HEARTBEAT_FILE = os.path.join(STATE_DIR, "heartbeat.json")
WATCHDOG_LOG = os.path.join(app_settings.BASE_DIR, "log", "watchdog_log.txt")
STATE_FILE = os.path.join(STATE_DIR, "watchdog_state.json")
QUEUE_DB = os.path.join(STATE_DIR, "jobs.sqlite3")
MONITOR_SCRIPT = os.path.join(SCRIPT_DIR, "gmail_monitor.py")
PAUSED_KEY = "monitor_paused"

STALE_SECONDS = 15 * 60
BOOT_PROCESS_GRACE_SECONDS = 5 * 60
BOOT_STALE_GRACE_SECONDS = 10 * 60
RESUME_GAP_SECONDS = 10 * 60
RESUME_GRACE_SECONDS = 10 * 60
MAX_RESTARTS = 3
RESTART_WINDOW_SECONDS = 30 * 60


def log_event(message):
    append_log(WATCHDOG_LOG, message)


def _run_hidden(command, **kwargs):
    # The watchdog runs under pythonw; hide console children. Never use
    # DETACHED_PROCESS: PowerShell needs a (hidden) console for stdout.
    return subprocess.run(command, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kwargs)


def _write_customer_notice(message):
    """Fallback for machines without msg.exe (Windows Home): leave a file the
    customer will actually see in every configured save folder, matching the
    naming/append convention of gmail_monitor's !取得エラー_YYYYMMDD.txt.
    """
    try:
        accounts = app_settings.get_account_configs(app_settings.load_settings())
    except Exception:
        # notify() sits on the watchdog's control path; it must never raise.
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    date_tag = time.strftime("%Y%m%d")
    for account in accounts:
        final_dir = (account or {}).get("final_dir", "")
        if not final_dir:
            continue
        try:
            os.makedirs(final_dir, exist_ok=True)
            path = os.path.join(final_dir, f"!監視停止_{date_tag}.txt")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"[{stamp}] {message}\n")
        except OSError:
            pass


def notify(message, customer_alert=False):
    try:
        _run_hidden(["msg", os.environ.get("USERNAME", "*"), message], capture_output=True, timeout=10)
    except Exception:
        pass
    if customer_alert:
        _write_customer_notice(message)


def event_log(message):
    try:
        _run_hidden(
            ["eventcreate", "/T", "ERROR", "/ID", "100", "/L", "APPLICATION",
             "/SO", "GmailAutoDownloader", "/D", message[:300]],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    temp = STATE_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(temp, STATE_FILE)


def is_paused():
    try:
        return JobQueue(QUEUE_DB).get_metadata(PAUSED_KEY, "0") == "1"
    except Exception as exc:
        log_event(f"Failed to read pause state: {exc}")
        return False


def is_settings_update():
    """The settings dialog is stopping the monitor to commit new settings."""
    try:
        return settings_update_in_progress(JobQueue(QUEUE_DB))
    except Exception as exc:
        log_event(f"Failed to read settings update state: {exc}")
        return False


def powershell(script):
    result = _run_hidden(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"PowerShell exit {result.returncode}")
    return result.stdout.strip()


def monitor_pids():
    if os.name != "nt":
        return []
    return windows_integration.owned_monitor_pids(
        windows_integration.Runner(), windows_integration.powershell_path(), MONITOR_SCRIPT
    )


def boot_timestamp():
    if os.name != "nt":
        return time.time() - 86400
    output = powershell("(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToFileTimeUtc()")
    filetime = int(output)
    return (filetime - 116444736000000000) / 10000000.0


def stop_monitor():
    if os.name != "nt":
        return True
    try:
        windows_integration.stop_owned_monitors(
            windows_integration.Runner(),
            windows_integration.powershell_path(),
            windows_integration.taskkill_path(),
            MONITOR_SCRIPT,
        )
        return True
    except windows_integration.IntegrationError as exc:
        log_event(f"Monitor stop identity check failed: {exc}")
        return False


def start_monitor():
    if not os.path.exists(MONITOR_SCRIPT) or is_paused():
        return False
    if os.name == "nt":
        try:
            if monitor_pids():
                return True
        except windows_integration.IntegrationError as exc:
            log_event(f"Monitor start identity check failed: {exc}")
            return False
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [sys.executable, MONITOR_SCRIPT], cwd=app_settings.BASE_DIR,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    time.sleep(8)
    if os.name != "nt":
        return True
    try:
        return bool(monitor_pids())
    except windows_integration.IntegrationError as exc:
        log_event(f"Monitor start verification failed: {exc}")
        return False


def can_restart(state, now):
    times = [float(x) for x in state.get("restart_times", []) if now - float(x) <= RESTART_WINDOW_SECONDS]
    state["restart_times"] = times
    return len(times) < MAX_RESTARTS


def perform_restart(state, reason, now, kill_existing=False):
    if is_paused() or is_settings_update():
        state["stale_count"] = 0
        return
    if not can_restart(state, now):
        message = f"Gmail monitor restart suppressed: {MAX_RESTARTS} restarts within 30 minutes. Reason: {reason}"
        log_event(message)
        notify(message, customer_alert=True)
        event_log(message)
        state["stale_count"] = 0
        return
    if kill_existing:
        if not stop_monitor():
            message = f"Gmail monitor restart aborted because process ownership could not be verified. Reason: {reason}"
            log_event(message)
            notify(message, customer_alert=True)
            event_log(message)
            state["stale_count"] = 0
            return
    log_event(f"Restarting Gmail monitor. Reason: {reason}")
    ok = start_monitor()
    state.setdefault("restart_times", []).append(now)
    state["stale_count"] = 0
    if ok:
        log_event("Gmail monitor restart succeeded.")
        notify(f"Gmail Auto Downloader was restarted. Reason: {reason}")
    else:
        message = f"Gmail monitor restart FAILED. Reason: {reason}"
        log_event(message)
        notify(message, customer_alert=True)
        event_log(message)


def main():
    now = time.time()
    state = load_state()
    previous_run = float(state.get("last_run", 0) or 0)
    state["last_run"] = now

    if is_paused():
        state["stale_count"] = 0
        state["paused"] = True
        save_state(state)
        return 0
    state.pop("paused", None)

    # A Save stops the monitor on purpose and restarts it itself.
    if is_settings_update():
        state["stale_count"] = 0
        save_state(state)
        return 0

    if previous_run and now - previous_run > RESUME_GAP_SECONDS:
        state["grace_until"] = now + RESUME_GRACE_SECONDS
        state["stale_count"] = 0

    try:
        boot_ts = boot_timestamp()
        pids = monitor_pids()
    except Exception as exc:
        log_event(f"Watchdog self-check failed: {exc}")
        save_state(state)
        return 1

    boot_age = max(0, now - boot_ts)
    grace_until = float(state.get("grace_until", 0) or 0)

    if not pids:
        if boot_age < BOOT_PROCESS_GRACE_SECONDS or now < grace_until:
            save_state(state)
            return 0
        perform_restart(state, "monitor process not found", now)
        save_state(state)
        return 0

    if len(pids) > 1:
        perform_restart(state, f"multiple monitor processes detected ({len(pids)})", now, kill_existing=True)
        save_state(state)
        return 0

    if boot_age < BOOT_STALE_GRACE_SECONDS or now < grace_until:
        state["stale_count"] = 0
        save_state(state)
        return 0

    heartbeat = read_heartbeat(HEARTBEAT_FILE)
    heartbeat_ts = None
    if heartbeat:
        try:
            heartbeat_ts = float(heartbeat.get("timestamp"))
        except (TypeError, ValueError):
            heartbeat_ts = None

    stale = heartbeat_ts is None or now - heartbeat_ts >= STALE_SECONDS
    if not stale:
        state["stale_count"] = 0
        save_state(state)
        return 0

    state["stale_count"] = int(state.get("stale_count", 0) or 0) + 1
    count = state["stale_count"]
    age_text = "missing" if heartbeat_ts is None else f"{int((now - heartbeat_ts) / 60)} min old"

    if count == 1:
        log_event(f"Stale heartbeat detected ({age_text}); observation 1/3.")
    elif count == 2:
        message = f"Gmail monitor may be stalled: heartbeat is {age_text} (2/3)."
        log_event(message)
        notify(message, customer_alert=True)
    else:
        perform_restart(state, f"heartbeat remained stale ({age_text}) for 3 checks", now, kill_existing=True)

    save_state(state)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:
        log_event(f"Watchdog fatal error: {exc}")
        notify("Gmail Auto Downloader watchdog failed. Check watchdog_log.txt.")
        code = 1
    raise SystemExit(code)
