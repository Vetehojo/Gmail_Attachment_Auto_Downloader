import json
import os
import threading
import time


ERROR_ALREADY_EXISTS = 183

# gmail_monitor runs a scanner thread and a worker thread in one process, and
# both call append_log -> rotate_file -> os.replace. Guard rotate+append so a
# concurrent rename cannot collide (the OSError from that collision is still
# swallowed below, but without the lock rotation could silently stop working
# under contention and the log would grow past its cap).
_LOG_LOCK = threading.Lock()

# The scanner and worker threads both write heartbeat.json through the same
# fixed "<path>.tmp". Without serialization one thread can truncate or rename
# the other's temp, so every atomic_write_json writer takes this lock.
_JSON_WRITE_LOCK = threading.Lock()

# Heartbeat paths whose last write failed; logs only the first failure and the
# recovery instead of one line per attempt.
_HEARTBEAT_FAILING = set()
_HEARTBEAT_STATE_LOCK = threading.Lock()

# Queue metadata shared by the tray, the monitor and the watchdog.
# Set by the settings dialog's Save while it stops the monitor and commits the
# new settings: an expiry timestamp, so a tray that dies mid-save cannot block
# monitor starts for longer than SETTINGS_UPDATE_SECONDS.
SETTINGS_UPDATE_KEY = "settings_update_until"
SETTINGS_UPDATE_SECONDS = 120
# Per-account mailbox identity (users.getProfile emailAddress) the monitor
# compares every newly built Gmail service against. Absent: an install from
# before this check, trusted on first use. "": saved without a connection
# test, refused until one is saved. Otherwise the verified address.
IDENTITY_KEY_PREFIX = "verified_identity:"
# JSON {account: message} of attachment jobs deferred for authentication,
# shown by the tray as 要確認; an account's entry is cleared when one of its
# jobs succeeds, and every entry when settings are saved.
AUTH_ISSUES_KEY = "monitor_auth_issues"


def begin_settings_update(queue, now=None):
    now = time.time() if now is None else float(now)
    queue.set_metadata(SETTINGS_UPDATE_KEY, now + SETTINGS_UPDATE_SECONDS)


def end_settings_update(queue):
    queue.delete_metadata(SETTINGS_UPDATE_KEY)


def settings_update_in_progress(queue, now=None):
    """True while a Save holds the marker and it has not expired. A value
    further ahead than SETTINGS_UPDATE_SECONDS (e.g. the clock moved back)
    counts as expired rather than blocking starts indefinitely."""
    now = time.time() if now is None else float(now)
    raw = queue.get_metadata(SETTINGS_UPDATE_KEY)
    if raw is None:
        return False
    try:
        remaining = float(raw) - now
    except (TypeError, ValueError):
        return False
    return 0 < remaining <= SETTINGS_UPDATE_SECONDS


def identity_key(account_email):
    return IDENTITY_KEY_PREFIX + str(account_email or "").strip().lower()


def read_auth_issues(queue):
    raw = queue.get_metadata(AUTH_ISSUES_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"": raw}
    return data if isinstance(data, dict) else {"": raw}


def set_auth_issue(queue, account, message):
    issues = read_auth_issues(queue)
    issues[account] = str(message)[:1000]
    queue.set_metadata(AUTH_ISSUES_KEY, json.dumps(issues, ensure_ascii=False))


def clear_auth_issue(queue, account):
    """No write at all when the account has no entry."""
    issues = read_auth_issues(queue)
    if account not in issues:
        return
    del issues[account]
    if issues:
        queue.set_metadata(AUTH_ISSUES_KEY, json.dumps(issues, ensure_ascii=False))
    else:
        queue.delete_metadata(AUTH_ISSUES_KEY)


def format_auth_issues(issues):
    return " / ".join(
        f"{account}: {message}" if account else str(message)
        for account, message in sorted(issues.items())
    )


class SingleInstance:
    def __init__(self, name, lock_path):
        self.name = name
        self.lock_path = lock_path
        self.handle = None
        self.file_handle = None

    def acquire(self):
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.CreateMutexW(None, False, self.name)
            if not handle:
                raise OSError("CreateMutexW failed")
            if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                return False
            self.handle = handle
            return True

        import fcntl
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        self.file_handle = open(self.lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(self.file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            self.file_handle.close()
            self.file_handle = None
            return False

    def release(self):
        if os.name == "nt" and self.handle:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None
        elif self.file_handle:
            import fcntl
            try:
                fcntl.flock(self.file_handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.file_handle.close()
                self.file_handle = None


def atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp"
    with _JSON_WRITE_LOCK:
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            os.replace(temp, path)
        except BaseException:
            # The handle is closed by now; drop the temp and re-raise so
            # callers (the commit journal) still see the failure.
            try:
                os.remove(temp)
            except OSError:
                pass
            raise


def write_heartbeat(path, status="ok", detail="", log_path=None):
    """Best-effort: a failed write (e.g. os.replace denied while a reader holds
    the file) is logged once and never raised, so it cannot abort a scan or
    strand a claimed job. The first failure and the recovery go to log_path.
    """
    try:
        atomic_write_json(path, {
            "timestamp": time.time(),
            "pid": os.getpid(),
            "status": status,
            "detail": detail[:500],
        })
    except OSError as exc:
        with _HEARTBEAT_STATE_LOCK:
            first = path not in _HEARTBEAT_FAILING
            _HEARTBEAT_FAILING.add(path)
        if first and log_path:
            append_log(log_path, f"Heartbeat write failed: {exc}", "heartbeat")
        return
    with _HEARTBEAT_STATE_LOCK:
        recovered = path in _HEARTBEAT_FAILING
        _HEARTBEAT_FAILING.discard(path)
    if recovered and log_path:
        append_log(log_path, "Heartbeat write recovered", "heartbeat")


def read_heartbeat(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def rotate_file(path, max_bytes=5 * 1024 * 1024, generations=3):
    try:
        if not os.path.exists(path) or os.path.getsize(path) < max_bytes:
            return
        oldest = f"{path}.{generations}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for index in range(generations - 1, 0, -1):
            src = f"{path}.{index}"
            dst = f"{path}.{index + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(path, f"{path}.1")
    except OSError:
        pass


def append_log(path, message, procedure=""):
    try:
        with _LOG_LOCK:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            rotate_file(path)
            stamp = time.strftime("%Y/%m/%d %H:%M:%S")
            text = f"{stamp} - {procedure}: {message}" if procedure else f"{stamp} - {message}"
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(text + "\n")
    except OSError:
        pass


def sleep_with_heartbeat(seconds, heartbeat_path, detail="sleep"):
    end = time.time() + max(0, seconds)
    while time.time() < end:
        write_heartbeat(heartbeat_path, "ok", detail)
        time.sleep(min(5, max(0, end - time.time())))
