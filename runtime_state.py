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
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    os.replace(temp, path)


def write_heartbeat(path, status="ok", detail=""):
    atomic_write_json(path, {
        "timestamp": time.time(),
        "pid": os.getpid(),
        "status": status,
        "detail": detail[:500],
    })


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
