"""Watchdog for Gmail Auto Downloader.

Scheduled every five minutes. It supervises the monitor process, the
separate heartbeat file and the attachment worker's heartbeat in the queue.
It does not use the Gmail mail cursor as health data.
"""
import json
import os
import subprocess
import sys
import time

import app_settings
from job_queue import JobQueue
from runtime_state import (
    WORKER_HEARTBEAT_KEY,
    WORKER_STALL_SECONDS,
    append_log,
    read_heartbeat,
    settings_update_in_progress,
)
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
# Consecutive checks (5 minutes apart) that must see the worker heartbeat
# older than WORKER_STALL_SECONDS before the monitor is restarted.
WORKER_STALL_CHECKS = 2
# watchdog_state.json: whether this incident already produced a customer
# notice (the !監視停止 file) / a restart-cap alert. Cleared once the monitor is
# seen healthy again, or paused.
NOTICE_SENT_KEY = "customer_notice_sent"
CAP_ALERTED_KEY = "restart_cap_alerted"


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


def claim_customer_notice(state):
    """True for the first customer notice of an incident: later alerts of the
    same incident still go to the log, msg and the event log, but add no line
    to the !監視停止 files."""
    if state.get(NOTICE_SENT_KEY):
        return False
    state[NOTICE_SENT_KEY] = True
    return True


def end_incident(state):
    state.pop(NOTICE_SENT_KEY, None)
    state.pop(CAP_ALERTED_KEY, None)


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


def worker_stall(now):
    """Age text when attachment jobs are waiting or processing while the
    worker heartbeat is missing or older than WORKER_STALL_SECONDS, else "".
    The heartbeat.json the scanner keeps fresh says nothing about the worker."""
    try:
        queue = JobQueue(QUEUE_DB)
        counts = queue.counts()
        if not counts["pending"] and not counts["processing"]:
            return ""
        raw = queue.get_metadata(WORKER_HEARTBEAT_KEY)
    except Exception as exc:
        log_event(f"Failed to read the attachment worker state: {exc}")
        return ""
    try:
        age = now - float(raw)
    except (TypeError, ValueError):
        return "missing"
    return f"{int(age / 60)} min old" if age >= WORKER_STALL_SECONDS else ""


def release_stalled_jobs(reason):
    """After a stall kill: the killed attempt of each processing job counts
    (JobQueue.fail_stalled_jobs), so a job that hangs on every try ends as a
    visible failure instead of being retried forever."""
    try:
        outcomes = JobQueue(QUEUE_DB).fail_stalled_jobs(
            "添付の保存処理が応答しなくなったため、監視プロセスを再起動して中断しました。"
        )
    except Exception as exc:
        log_event(f"Failed to record the stalled attachment job attempt: {exc}")
        return
    if outcomes:
        log_event("Stalled attachment job attempt recorded: " + ", ".join(
            f"job {job_id} {outcome}" for job_id, outcome in sorted(outcomes.items())
        ))


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
    except OSError as exc:
        # e.g. PowerShell or taskkill could not be run: a failed stop, so the
        # caller aborts the restart and main() still saves its state file.
        log_event(f"Monitor stop failed: {exc}")
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


def perform_restart(state, reason, now, kill_existing=False, worker_stalled=False):
    """worker_stalled: the kill interrupts a hung attachment job, whose
    attempt then counts (release_stalled_jobs)."""
    if is_paused() or is_settings_update():
        state["stale_count"] = 0
        return
    if not can_restart(state, now):
        message = f"Gmail monitor restart suppressed: {MAX_RESTARTS} restarts within 30 minutes. Reason: {reason}"
        log_event(message)
        # This repeats every run until the window allows a restart again.
        if not state.get(CAP_ALERTED_KEY):
            state[CAP_ALERTED_KEY] = True
            notify(message, customer_alert=claim_customer_notice(state))
            event_log(message)
        state["stale_count"] = 0
        return
    if kill_existing:
        if not stop_monitor():
            message = f"Gmail monitor restart aborted because process ownership could not be verified. Reason: {reason}"
            log_event(message)
            notify(message, customer_alert=claim_customer_notice(state))
            event_log(message)
            state["stale_count"] = 0
            return
        if worker_stalled:
            release_stalled_jobs(reason)
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
        notify(message, customer_alert=claim_customer_notice(state))
        event_log(message)


def check_worker(state, now, worker_checks):
    """The monitor process and heartbeat.json look fine; is its attachment
    worker stuck? worker_checks: consecutive stalled checks before this one."""
    age_text = worker_stall(now)
    if not age_text:
        end_incident(state)
        return
    count = worker_checks + 1
    if count < WORKER_STALL_CHECKS:
        state["worker_stall_count"] = count
        log_event(
            f"Attachment worker heartbeat is {age_text} while jobs are waiting; "
            f"observation {count}/{WORKER_STALL_CHECKS}."
        )
        return
    perform_restart(
        state, f"attachment worker stalled (heartbeat {age_text})", now,
        kill_existing=True, worker_stalled=True,
    )


def main():
    now = time.time()
    state = load_state()
    previous_run = float(state.get("last_run", 0) or 0)
    state["last_run"] = now
    # Kept only by a run that saw the worker stalled; any other outcome of
    # this run starts the count again.
    worker_checks = int(state.pop("worker_stall_count", 0) or 0)

    if is_paused():
        state["stale_count"] = 0
        state["paused"] = True
        end_incident(state)
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
        # The scanner keeps heartbeat.json fresh on its own; check the worker too.
        check_worker(state, now, worker_checks)
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
        notify(message, customer_alert=claim_customer_notice(state))
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
