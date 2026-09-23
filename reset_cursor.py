import os
import sqlite3

from runtime_state import SingleInstance

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "state", "jobs.sqlite3")
LEGACY_PATH = os.path.join(SCRIPT_DIR, "log", "last_check_time.txt")
LOCK_PATH = os.path.join(SCRIPT_DIR, "state", "monitor.lock")
CURSOR_KEY = "mail_cursor_timestamp"


def main():
    # A running monitor writes set_metadata(cursor_key, scan_started) when its
    # in-flight scan finishes, silently undoing the delete below. The GUI path
    # (apply_scan_start) is safe because it stops the monitor first; this .bat
    # path is not, so refuse instead of pretending to succeed.
    instance = SingleInstance("Local\\GmailAutoDownloaderMonitor", LOCK_PATH)
    if not instance.acquire():
        print("Gmail監視プロセスが実行中のため、カーソルをリセットできません。")
        print("トレイの「一時停止」または「終了（監視停止）」でGmail監視を停止してから、再度実行してください。")
        print("期間を指定して再スキャンしたい場合は、トレイの「再スキャン期間指定」を使用してください。")
        return 1

    try:
        changed = False
        if os.path.exists(DB_PATH):
            with sqlite3.connect(DB_PATH, timeout=30) as conn:
                cur = conn.execute(
                    "DELETE FROM metadata WHERE key = ? OR key LIKE ?",
                    (CURSOR_KEY, CURSOR_KEY + ":%"),
                )
                changed = changed or cur.rowcount > 0
        if os.path.exists(LEGACY_PATH):
            os.remove(LEGACY_PATH)
            changed = True
        print("Mail cursor reset." if changed else "Mail cursor was already empty.")
        print("OAuth and DWD per-account cursors were cleared; queued job history was preserved.")
        return 0
    finally:
        instance.release()


if __name__ == "__main__":
    raise SystemExit(main())
