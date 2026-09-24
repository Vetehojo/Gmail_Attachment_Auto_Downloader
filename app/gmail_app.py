"""Windows tray UI for the Gmail attachment downloader."""
import argparse
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, ttk

from app_settings import (
    AUTH_DWD,
    AUTH_OAUTH,
    BASE_DIR,
    CREDENTIALS_PATH,
    SERVICE_ACCOUNT_PATH,
    STATE_DIR,
    copy_credentials,
    copy_service_account,
    encode_dwd_accounts,
    get_account_configs,
    is_configured,
    load_dwd_accounts,
    load_settings,
    normalize_auth_mode,
    normalize_dwd_accounts,
    parse_csv_setting,
    parse_scan_start,
    save_settings,
)
from filename_rules import preview_filename, validate_template
from job_queue import JobQueue
from runtime_state import (
    AUTH_ISSUES_KEY,
    MAIL_CURSOR_KEY,
    WORKER_HEARTBEAT_KEY,
    WORKER_IDLE_STALL_SECONDS,
    WORKER_STALL_SECONDS,
    SingleInstance,
    append_log,
    begin_settings_update,
    cursor_key,
    end_settings_update,
    format_auth_issues,
    identity_key,
    read_auth_issues,
    read_heartbeat,
    settings_update_in_progress,
)
import windows_integration

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
QUEUE_DB = os.path.join(STATE_DIR, "jobs.sqlite3")
HEARTBEAT_FILE = os.path.join(STATE_DIR, "heartbeat.json")
APP_LOCK_FILE = os.path.join(STATE_DIR, "app.lock")
TRAY_LOG = os.path.join(BASE_DIR, "log", "tray_log.txt")
MONITOR_SCRIPT = os.path.join(SCRIPT_DIR, "gmail_monitor.py")
PAUSED_KEY = "monitor_paused"
# Why the monitor is paused: "exit" (set by exit_app) is cleared automatically
# on the next start, "user" (set by toggle_pause) survives restarts/logon (I6).
# A missing reason (older versions, or a pause set before this key existed) is
# treated the same as "user" - stay paused rather than guess it was exit-only.
PAUSE_REASON_KEY = "monitor_pause_reason"
PAUSE_STARTUP_NOTICE = "自動取得は一時停止中です。トレイメニューの「一時停止 / 再開」で再開できます。"
LAST_ERROR_KEY = "monitor_last_error"
# Written by gmail_monitor's worker thread (see C3 in PR32 contract); the
# heartbeat key comes from runtime_state. Redefined here rather than imported
# so the tray process never has to pull in gmail_monitor's heavier
# dependencies (google-api-python-client) just to read a metadata key name.
WORKER_ERROR_KEY = "worker_last_error"
_QUEUE_INSTANCE = None


def queue():
    global _QUEUE_INSTANCE
    if _QUEUE_INSTANCE is None:
        _QUEUE_INSTANCE = JobQueue(QUEUE_DB)
    return _QUEUE_INSTANCE


def log(message, procedure="tray"):
    append_log(TRAY_LOG, message, procedure)


def format_time(value):
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y/%m/%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "-"


def check_scan_start(start):
    if start > datetime.now():
        raise ValueError("未来の日付は指定できません。")


def write_scan_start(q, start, settings, identities=None):
    """Set every account's mail cursor in `settings` to just after `start`.

    lookback_days only bounds a cursor-less first scan (see gmail_monitor's
    scan_gmail). This always writes an explicit cursor for every account, so
    lookback_days is never consulted here; bumping it used to just leak a
    permanently larger window into later cursor-less scans for no benefit.

    `identities` are mailboxes a Save is recording (see cursor_key). An OAuth
    account with no mailbox recorded yet gets the pending MAIL_CURSOR_KEY,
    which the first scan of its recorded mailbox adopts.
    """
    dwd = normalize_auth_mode(settings.get("auth_mode")) == AUTH_DWD
    timestamp = (start + timedelta(minutes=5)).timestamp()
    for account in get_account_configs(settings):
        q.set_metadata(cursor_key(q, account["email"], dwd, identities) or MAIL_CURSOR_KEY, timestamp)


SAVED_LATER_TEXT = "設定はまだ保存していません。「保存」を押すと反映します。"
STOP_FAILED_TEXT = (
    "動作中の自動取得（監視プロセス）を特定できなかったか、停止できなかったため、設定を保存しませんでした。"
    "設定は変更していません。自動取得は以前の設定のまま動いている可能性があります。"
    "詳しい原因は log\\tray_log.txt に記録しました。"
)
START_FAILED_TEXT = (
    "ただし、自動取得（監視プロセス）を開始できませんでした。"
    "トレイアプリが定期的に開始を再試行します。「状態を表示」で状態を確認してください。"
)
PAUSE_STOP_FAILED_TEXT = (
    "動作中の自動取得（監視プロセス）を特定できなかったか、停止できなかったため、一時停止しませんでした。"
    "自動取得は動いたままの可能性があります。"
    "詳しい原因は log\\tray_log.txt に記録しました。"
)
EXIT_STOP_FAILED_TEXT = (
    "動作中の自動取得（監視プロセス）を特定できなかったか、停止できなかったため、"
    "自動取得は動いたままの可能性があります。トレイアプリはこのまま終了します。"
    "詳しい原因は log\\tray_log.txt に記録しました。"
)
RESCAN_STOP_FAILED_TEXT = (
    "動作中の自動取得（監視プロセス）を特定できなかったか、停止できなかったため、再確認の期間を設定しませんでした。"
    "自動取得は以前の確認位置から続けている可能性があります。"
    "詳しい原因は log\\tray_log.txt に記録しました。"
)
RUN_NOW_FAILED_TEXT = (
    "自動取得（監視プロセス）の停止または開始に失敗したため、Gmail確認を開始できませんでした。"
    "「状態を表示」で状態を確認してください。停止に失敗した場合の原因は log\\tray_log.txt に記録しました。"
)


def _path_key(path):
    path = str(path or "").strip()
    return os.path.normcase(os.path.abspath(path)) if path else ""


def stage_matches(staged, inputs):
    """Whether a connection-test result still describes the dialog's inputs:
    OAuth by client JSON and target address, DWD by service-account JSON."""
    if not staged:
        return False
    if staged["mode"] == AUTH_DWD:
        return _path_key(staged["service_account"]) == _path_key(inputs["service_account"])
    return (
        _path_key(staged["client"]) == _path_key(inputs["client"])
        and staged["email"] == inputs["email"]
    )


def run_connection_test(snapshot):
    """The settings dialog's connection test on a snapshot of its unsaved values.

    Writes nothing: no config.ini, credential copy, token.json, folder, cursor
    or queue metadata, and the monitor keeps running. Returns the message box
    ("level", "title", "text") and "staged": what Save may commit - the
    verified mailbox identity per account and, after a new OAuth login, its
    in-memory credentials ("creds") - or None.
    """
    from gmail_auth import dwd_connection_test, oauth_connection_test, verify_profile_account

    if snapshot["mode"] == AUTH_DWD:
        identities = {}
        failures = []
        for email, profile, error in dwd_connection_test(snapshot["service_account"], snapshot["accounts"]):
            actual = str((profile or {}).get("emailAddress") or "").strip().lower()
            if error is not None:
                failures.append(f"{email}: {error}")
            elif not actual:
                failures.append(f"{email}: Gmailがメールアドレスを返しませんでした。")
            else:
                identities[email] = actual
        staged = None
        if identities:
            staged = {"mode": AUTH_DWD, "service_account": snapshot["service_account"], "identities": identities}
        if failures:
            text = f"成功 {len(identities)} / 失敗 {len(failures)}\n\n" + "\n".join(failures[:10])
            return {"level": "error", "title": "接続失敗", "text": text, "staged": staged}
        text = f"DWD接続成功: {len(identities)}アカウント\n{SAVED_LATER_TEXT}"
        return {"level": "info", "title": "接続成功", "text": text, "staged": staged}

    expected = snapshot["email"]
    profile, login, note = oauth_connection_test(
        snapshot["client"], snapshot["force_login"], snapshot["staged_creds"]
    )
    actual = str((profile or {}).get("emailAddress") or "").strip().lower()
    if not actual:
        raise RuntimeError("Gmailがログインしたアカウントのメールアドレスを返しませんでした。")
    staged = {
        "mode": AUTH_OAUTH,
        "client": snapshot["client"],
        "email": expected,
        "creds": login,
        "identities": {expected: actual},
    }
    lines = [f"{note}そのため、ブラウザでログインしました。"] if note else []
    ok, _actual = verify_profile_account(profile, expected)
    if not ok:
        lines += [
            "Gmail APIには接続できましたが、ログインしたGoogleアカウントのメールアドレスが入力値と異なります。",
            f"入力したメールアドレス: {expected}",
            f"ログインしたアカウント: {actual}",
            "入力したアドレスがこのアカウントの別名（エイリアス）でなければ、"
            "「別のGoogleアカウントでログインし直す」をオンにして、もう一度テストしてください。"
            "このまま保存すると、このアカウントのメールボックスを入力したメールアドレスの取得先として記録します。",
            SAVED_LATER_TEXT,
        ]
        return {"level": "warning", "title": "メールアドレス不一致", "text": "\n".join(lines), "staged": staged}
    lines += ["Gmail APIに接続できました。", actual]
    if login is not None:
        lines.append("ログインした認証情報は「保存」を押したときに保存します。")
    lines.append(SAVED_LATER_TEXT)
    return {"level": "info", "title": "接続成功", "text": "\n".join(lines), "staged": staged}


def identity_mismatches(identity_writes):
    """(account, verified mailbox) pairs Save is about to record where the
    mailbox is not the typed address itself: an alias, or another account."""
    return [
        (account, value)
        for account, value in identity_writes.items()
        if value and value.strip().lower() != account.strip().lower()
    ]


def mismatch_confirmation_text(mismatches):
    lines = ["接続テストで確認したメールボックスが、入力したメールアドレスと異なります。"]
    for account, mailbox in mismatches:
        lines += ["", f"入力したメールアドレス: {account}", f"確認したメールボックス: {mailbox}"]
    lines += [
        "",
        "「はい」を選ぶと、確認したメールボックスの添付を、入力したメールアドレスの保存先へ保存します。"
        "入力したアドレスがそのメールボックスの別名（エイリアス）の場合だけ「はい」を選んでください。"
        "「いいえ」を選ぶと保存を中止します。",
    ]
    return "\n".join(lines)


def plan_identity_writes(account_ids, previous_ids, verified, stored):
    """Mailbox identities Save records, as {account: value}.

    An address a connection test verified is recorded. An account this Save
    adds without one gets "" - the monitor refuses it until it is tested and
    saved - unless an identity is already stored for it. Accounts configured
    before keep what they have, so an install from before identities were
    recorded stays trust-on-first-use. The caller passes no previous accounts
    when the auth mode changes: a token for another mailbox must not be
    trusted on first use.
    """
    previous = set(previous_ids)
    writes = {}
    for account in account_ids:
        if verified.get(account):
            writes[account] = verified[account]
        elif account not in previous and stored(account) is None:
            writes[account] = ""
    return writes


def commit_settings(plan, q):
    """Write a validated Save plan while the monitor is stopped: credential
    files, token, identities and cursors first, config.ini last."""
    if plan["create_dir"]:
        os.makedirs(plan["create_dir"], exist_ok=True)
    if plan["mode"] == AUTH_DWD:
        copy_service_account(plan["source"])
    else:
        copy_credentials(plan["source"])
    if plan["token"] is not None:
        from gmail_auth import install_oauth_token
        install_oauth_token(plan["token"])
    # Before the identities, so a new mailbox can never find it.
    if plan["drop_pending_cursor"]:
        q.delete_metadata(MAIL_CURSOR_KEY)
    for account, value in plan["identity_writes"].items():
        q.set_metadata(identity_key(account), value)
    # Deferrals recorded under the old settings; a job still failing re-records one.
    q.delete_metadata(AUTH_ISSUES_KEY)
    if plan["scan_start"] is not None:
        write_scan_start(q, plan["scan_start"], plan["values"], plan["identity_writes"])
    save_settings(plan["values"])


def _start_monitor(controller):
    """controller.start(); "" when it started, else what went wrong."""
    try:
        if controller.start():
            return ""
        detail = "開始の条件を満たしていないか、監視プロセスを確認できませんでした。"
    except Exception as exc:
        detail = str(exc)
    log(f"Monitor start after a settings save failed: {detail}", "settings")
    return detail


def apply_settings_update(plan, controller, q, restart):
    """Save's stop -> commit -> restart. Returns (saved, problem): saved is
    True once the commit completed; problem is "" or the message to show.

    The settings-update marker keeps the watchdog and the tray's health tick
    from starting the monitor meanwhile. Nothing is written unless the monitor
    is confirmed stopped. A failed commit still restarts it (when `restart`)
    and is reported; so is a restart that fails after a successful commit.
    """
    begin_settings_update(q)
    error = None
    try:
        if not controller.stop_all():
            log("Settings save aborted: the monitor could not be stopped", "settings")
            return False, STOP_FAILED_TEXT
        try:
            commit_settings(plan, q)
        except Exception as exc:
            log(f"Settings commit failed: {exc}", "settings")
            error = exc
    finally:
        try:
            end_settings_update(q)
        except Exception as exc:
            # It expires on its own after SETTINGS_UPDATE_SECONDS.
            log(f"Settings update marker could not be cleared: {exc}", "settings")
    start_problem = _start_monitor(controller) if restart else ""
    if error is not None:
        if not restart:
            resumed = ""
        elif start_problem:
            resumed = f"自動取得（監視プロセス）も開始できませんでした（{start_problem}）。"
        else:
            resumed = "自動取得は再開しています。"
        return False, (
            "設定の保存中にエラーが発生しました。一部の認証情報だけが保存された可能性があります。"
            f"内容を確認して、もう一度保存してください。{resumed}\n{error}"
        )
    if start_problem:
        return True, f"{START_FAILED_TEXT}\n{start_problem}"
    return True, ""


class MonitorController:
    def __init__(self):
        self.process = None

    def _pids(self):
        if os.name != "nt":
            if self.process is not None and self.process.poll() is None:
                return [self.process.pid]
            return []
        return windows_integration.owned_monitor_pids(
            windows_integration.Runner(), windows_integration.powershell_path(), MONITOR_SCRIPT
        )

    def start(self):
        if not is_configured():
            return False
        try:
            if self._pids():
                return True
        except windows_integration.IntegrationError:
            return False
        self.process = subprocess.Popen(
            [sys.executable, MONITOR_SCRIPT],
            cwd=BASE_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True

    def stop_all(self):
        """False when the monitor could not be confirmed stopped (cause logged).
        Used by settings save, pause, exit, 期間を指定して再確認 and 今すぐGmailを確認."""
        if os.name == "nt":
            try:
                windows_integration.stop_owned_monitors(
                    windows_integration.Runner(),
                    windows_integration.powershell_path(),
                    windows_integration.taskkill_path(),
                    MONITOR_SCRIPT,
                )
            except (windows_integration.IntegrationError, OSError) as exc:
                log(f"Monitor stop failed: {exc}", "monitor")
                return False
        elif self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
            except Exception:
                pass
        self.process = None
        return True

    def restart(self):
        if not self.stop_all():
            return False
        time.sleep(0.4)
        return self.start()


def center_on_parent(win, parent=None):
    """Place `win` over its parent, or on the screen centre when the parent is hidden.

    Every dialog here is a Toplevel of a withdrawn root, and Tk then puts it at the
    screen origin. On a multi-monitor desk that lands the dialog far away from the
    window the user just clicked, so position it explicitly.
    """
    win.update_idletasks()
    width = win.winfo_width()
    height = win.winfo_height()
    if parent is None:
        parent = win.master
    if parent is not None and parent.winfo_exists() and parent.winfo_viewable():
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
    else:
        x = (win.winfo_screenwidth() - width) // 2
        y = (win.winfo_screenheight() - height) // 2
    win.geometry("+%d+%d" % (max(0, min(x, win.winfo_screenwidth() - width)),
                             max(0, min(y, win.winfo_screenheight() - height))))


def build_scrollable_tree(parent, columns, headings, height=None):
    """Grid a Treeview with both scrollbars inside a new frame.

    Column widths are fixed pixels sized for the headings, so Japanese subjects,
    file names and error strings overflow. Without a horizontal scrollbar the
    overflowing text is unreachable, which is why the failure reason used to be
    unreadable in the list.
    """
    container = ttk.Frame(parent)
    kwargs = {"columns": columns, "show": "headings", "selectmode": "extended"}
    if height is not None:
        kwargs["height"] = height
    tree = ttk.Treeview(container, **kwargs)
    for key, label, width in headings:
        tree.heading(key, text=label)
        tree.column(key, width=width, anchor="w", stretch=False)
    vsb = ttk.Scrollbar(container, orient="vertical", command=tree.yview)
    hsb = ttk.Scrollbar(container, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    hsb.grid(row=1, column=0, sticky="ew")
    container.rowconfigure(0, weight=1)
    container.columnconfigure(0, weight=1)
    return container, tree


def build_detail_pane(parent, height=8):
    """Scrollable read-only text area holding the full record of the selected row."""
    frame = ttk.Frame(parent)
    text = tk.Text(frame, height=height, wrap="word")
    vsb = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=vsb.set)
    text.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    return frame, text


def build_filename_preview(parent, variable):
    """Bound preview width while keeping the complete filename horizontally readable."""
    preview = ttk.Entry(parent, textvariable=variable, state="readonly", width=60)
    preview.grid(row=4, column=1, columnspan=2, sticky="ew", padx=8, pady=5)
    return preview


def format_job_detail(job):
    """Human-readable summary of one queue job, error first.

    The previous raw json.dumps put "error" last, so the one line the user needs
    was below the fold of a five-line box with no scrollbar.
    """
    payload = job.get("payload") or {}
    rows = []
    if job.get("last_error"):
        rows.append(("エラー内容", job["last_error"]))
    rows += [
        ("アカウント", payload.get("account_email", "-")),
        ("件名", payload.get("mail_subject", "-")),
        ("添付ファイル名", payload.get("filename", "-")),
        ("保存先フォルダー", payload.get("final_dir", "-")),
        ("受信日", payload.get("received_date", "-")),
        ("送信元", payload.get("sender_email", "-")),
        ("試行回数", str(job.get("attempts", "-"))),
        ("無視設定", "無視中" if job.get("ignored") else "無視していない"),
        ("メールID", payload.get("message_id", "-")),
    ]
    return "\n".join("%s: %s" % (label, value) for label, value in rows)


class DwdAccountEditor:
    def __init__(self, parent, title, initial=None):
        self.value = None
        self.win = tk.Toplevel(parent)
        self.win.title(title)
        self.win.resizable(False, False)
        initial = initial or {}
        self.email = tk.StringVar(value=initial.get("email", ""))
        self.folder = tk.StringVar(value=initial.get("final_dir", ""))
        # Both entries sit in column 1 with the same sticky so their edges line up.
        # Letting the first one span the browse-button column made grid centre it
        # across the wider span, which is what pushed that row to the right.
        ttk.Label(self.win, text="対象メールアドレス").grid(row=0, column=0, sticky="w", padx=10, pady=7)
        ttk.Entry(self.win, textvariable=self.email, width=48).grid(row=0, column=1, sticky="ew", padx=10, pady=7)
        ttk.Label(self.win, text="添付ファイルの保存先").grid(row=1, column=0, sticky="w", padx=10, pady=7)
        ttk.Entry(self.win, textvariable=self.folder, width=48).grid(row=1, column=1, sticky="ew", padx=10, pady=7)
        ttk.Button(self.win, text="参照...", command=self._browse).grid(row=1, column=2, padx=10, pady=7)
        self.win.columnconfigure(1, weight=1)
        bar = ttk.Frame(self.win)
        bar.grid(row=2, column=0, columnspan=3, sticky="e", padx=10, pady=10)
        ttk.Button(bar, text="キャンセル", command=self.win.destroy).pack(side="right", padx=4)
        ttk.Button(bar, text="OK", command=self._ok).pack(side="right", padx=4)
        self.win.transient(parent)
        self.win.grab_set()
        center_on_parent(self.win, parent)

    def _browse(self):
        path = filedialog.askdirectory(parent=self.win, initialdir=self.folder.get() or BASE_DIR)
        if path:
            self.folder.set(path)

    def _ok(self):
        email = self.email.get().strip().lower()
        folder = self.folder.get().strip()
        if "@" not in email:
            messagebox.showerror(
                "入力内容の確認",
                "対象メールアドレスを正しく入力してください。（例: office@example.com）",
                parent=self.win,
            )
            return
        if not folder:
            messagebox.showerror("入力内容の確認", "添付ファイルの保存先を指定してください。", parent=self.win)
            return
        self.value = {"email": email, "final_dir": os.path.abspath(folder)}
        self.win.destroy()

    def show(self):
        self.win.wait_window()
        return self.value


class SettingsDialog:
    def __init__(self, app, initial=False, setup_only=False):
        self.app = app
        self.initial = initial
        self.setup_only = setup_only
        self.settings = load_settings()
        self.win = tk.Toplevel(app.root)
        self.win.title("Gmail Attachment Downloader - 初期設定" if initial else "Gmail Attachment Downloader - 設定")
        # Resizable so long values (the extension list, DWD save paths) can be
        # widened; the old fixed size clipped them with no way to see the rest.
        self.win.resizable(True, True)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.columnconfigure(0, weight=1)
        self.win.rowconfigure(0, weight=1)

        self.auth_mode = tk.StringVar(value=normalize_auth_mode(self.settings.get("auth_mode")))
        self.final = tk.StringVar(value=self.settings.get("final_dir", ""))
        self.email = tk.StringVar(value=self.settings.get("target_email", ""))
        self.credentials = tk.StringVar(value=CREDENTIALS_PATH if os.path.exists(CREDENTIALS_PATH) else "")
        self.service_account = tk.StringVar(value=SERVICE_ACCOUNT_PATH if os.path.exists(SERVICE_ACCOUNT_PATH) else "")
        self.dwd_accounts = load_dwd_accounts(self.settings)
        self.template = tk.StringVar(value=self.settings.get("rename_template", "{original}_{date}_{subject}_{sender}"))
        self.subject_max = tk.StringVar(value=self.settings.get("rename_subject_max", "50"))
        self.excluded_labels = tk.StringVar(value=self.settings.get("excluded_labels", ""))
        self.allowed_extensions = tk.StringVar(value=self.settings.get("allowed_extensions", ""))
        self.preview = tk.StringVar()
        self.period = tk.StringVar(value="過去7日")
        self.custom_date = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        self.force_login = tk.BooleanVar(value=False)
        # The last connection test's result (see run_connection_test), kept in
        # memory only until Save; dropped on Cancel or when it no longer
        # matches the inputs it was made for.
        self._staged = None
        self._closed = False
        self._test_running = False
        self._test_generation = 0
        self._build()
        for variable in (self.email, self.credentials, self.service_account):
            variable.trace_add("write", self._discard_stale_stage)
        self._refresh_preview()
        self._refresh_auth_mode()
        self.win.update_idletasks()
        self.win.minsize(self.win.winfo_reqwidth(), self.win.winfo_reqheight())
        center_on_parent(self.win, app.root)

    def _entry_row(self, frame, row, label, variable, browse=None, width=54):
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=5)
        ttk.Entry(frame, textvariable=variable, width=width).grid(row=row, column=1, sticky="ew", padx=8, pady=5)
        if browse:
            ttk.Button(frame, text="参照...", command=browse).grid(row=row, column=2, padx=8, pady=5)

    def _build(self):
        notebook = ttk.Notebook(self.win)
        self.notebook = notebook
        basic = ttk.Frame(notebook)
        rename = ttk.Frame(notebook)
        notebook.add(basic, text="基本設定")
        notebook.add(rename, text="ファイル名")
        basic.columnconfigure(0, weight=1)
        rename.columnconfigure(1, weight=1)

        auth = ttk.LabelFrame(basic, text="アカウントの種類")
        auth.grid(row=0, column=0, columnspan=3, sticky="ew", padx=8, pady=8)
        ttk.Radiobutton(
            auth,
            text="個人Gmail・単独アカウント（OAuth認証）",
            value=AUTH_OAUTH,
            variable=self.auth_mode,
            command=self._refresh_auth_mode,
        ).pack(anchor="w", padx=8, pady=4)
        ttk.Radiobutton(
            auth,
            text="Google Workspace の複数アカウント（ドメイン全体の委任）",
            value=AUTH_DWD,
            variable=self.auth_mode,
            command=self._refresh_auth_mode,
        ).pack(anchor="w", padx=8, pady=4)
        ttk.Label(basic, text="処理対象はGmailの添付ファイルのみです。メール本文中のURLは処理しません。").grid(
            row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 8)
        )

        self.oauth_frame = ttk.LabelFrame(basic, text="OAuth認証の設定")
        self.oauth_frame.grid(row=2, column=0, columnspan=3, sticky="ew", padx=8, pady=8)
        self.oauth_frame.columnconfigure(1, weight=1)
        self._entry_row(self.oauth_frame, 0, "対象メールアドレス", self.email)
        self._entry_row(self.oauth_frame, 1, "添付ファイルの保存先", self.final, lambda: self._pick_dir(self.final))
        self._entry_row(self.oauth_frame, 2, "OAuthクライアントJSONファイル", self.credentials, self._pick_credentials)
        test_bar = ttk.Frame(self.oauth_frame)
        test_bar.grid(row=3, column=1, sticky="w", padx=8, pady=8)
        self.oauth_test_button = ttk.Button(test_bar, text="Googleに接続してテスト", command=self._connection_test)
        self.oauth_test_button.pack(side="left")
        ttk.Checkbutton(
            test_bar, text="別のGoogleアカウントでログインし直す", variable=self.force_login
        ).pack(side="left", padx=(12, 0))

        self.dwd_frame = ttk.LabelFrame(basic, text="Google Workspace（ドメイン全体の委任）の設定")
        self.dwd_frame.grid(row=2, column=0, columnspan=3, sticky="ew", padx=8, pady=8)
        self.dwd_frame.columnconfigure(1, weight=1)
        self._entry_row(self.dwd_frame, 0, "サービスアカウントJSONファイル", self.service_account, self._pick_service_account)
        ttk.Label(
            self.dwd_frame,
            text=(
                "Google管理コンソールで、このサービスアカウントに gmail.readonly の権限を承認してください。"
                "指定したJSONファイルはこのPCのユーザー専用フォルダーへコピーし、"
                "他のユーザーから読めないよう保護します。"
            ),
            wraplength=740,
            justify="left",
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 6))
        self.account_tree = ttk.Treeview(
            self.dwd_frame,
            columns=("email", "folder"),
            show="headings",
            height=7,
            selectmode="browse",
        )
        self.account_tree.heading("email", text="対象メールアドレス")
        self.account_tree.heading("folder", text="添付ファイルの保存先")
        self.account_tree.column("email", width=250, anchor="w")
        self.account_tree.column("folder", width=390, anchor="w")
        self.account_tree.grid(row=2, column=0, columnspan=3, padx=8, pady=6, sticky="ew")
        controls = ttk.Frame(self.dwd_frame)
        controls.grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=5)
        ttk.Button(controls, text="追加", command=self._add_dwd_account).pack(side="left", padx=3)
        ttk.Button(controls, text="編集", command=self._edit_dwd_account).pack(side="left", padx=3)
        ttk.Button(controls, text="削除", command=self._delete_dwd_account).pack(side="left", padx=3)
        self.dwd_test_button = ttk.Button(
            controls, text="登録した全アカウントに接続してテスト", command=self._connection_test
        )
        self.dwd_test_button.pack(side="left", padx=(18, 3))
        self._refresh_account_tree()

        common_frame = ttk.LabelFrame(basic, text="取り込み条件（共通）")
        common_frame.grid(row=3, column=0, columnspan=3, sticky="ew", padx=8, pady=8)
        common_frame.columnconfigure(1, weight=1)
        self._entry_row(common_frame, 0, "取り込まないGmailラベル（カンマ区切り／空欄なら除外しない）", self.excluded_labels)
        self._entry_row(common_frame, 1, "保存する添付ファイルの拡張子（カンマ区切り）", self.allowed_extensions, width=70)

        ttk.Label(
            rename,
            text="使える置き換え文字： {original}=元のファイル名 / {date}=受信日 / {subject}=件名 / {sender}=送信元",
            wraplength=620,
            justify="left",
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=5)
        ttk.Label(rename, text="保存するファイル名の形式").grid(row=1, column=0, sticky="w", padx=8, pady=5)
        template_entry = ttk.Entry(rename, textvariable=self.template, width=60)
        template_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=8, pady=5)
        template_entry.bind("<KeyRelease>", lambda _event: self._refresh_preview())
        tokens = ttk.Frame(rename)
        tokens.grid(row=2, column=1, columnspan=2, sticky="w", padx=8, pady=5)
        for label, token in [
            ("元ファイル名", "{original}"),
            ("受信日", "{date}"),
            ("件名", "{subject}"),
            ("送信元", "{sender}"),
        ]:
            ttk.Button(tokens, text=label, command=lambda value=token: self._append_token(value)).pack(side="left", padx=3)
        ttk.Label(rename, text="ファイル名に使う件名の最大文字数").grid(row=3, column=0, sticky="w", padx=8, pady=5)
        subject_spin = ttk.Spinbox(rename, from_=0, to=200, textvariable=self.subject_max, width=8)
        subject_spin.grid(row=3, column=1, sticky="w", padx=8, pady=5)
        subject_spin.bind("<KeyRelease>", lambda _event: self._refresh_preview())
        ttk.Label(rename, text="保存されるファイル名の例").grid(row=4, column=0, sticky="w", padx=8, pady=5)
        build_filename_preview(rename, self.preview)

        if self.initial:
            scan = ttk.Frame(notebook)
            notebook.add(scan, text="初回の取り込み範囲")
            scan.columnconfigure(2, weight=1)
            ttk.Label(
                scan,
                text=(
                    "最初の取り込みで、どこまで遡ってGmailを確認するかを指定します。"
                    "Google Workspace の複数アカウントでは、登録した全アカウントに同じ範囲を適用します。"
                ),
                wraplength=560,
                justify="left",
            ).grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=8)
            period_box = ttk.Combobox(
                scan,
                textvariable=self.period,
                values=["今日", "過去3日", "過去7日", "過去30日", "日付指定"],
                state="readonly",
                width=18,
            )
            period_box.grid(row=1, column=0, sticky="w", padx=8, pady=5)
            period_box.bind("<<ComboboxSelected>>", lambda _event: self._refresh_period_state())
            ttk.Label(scan, text="開始日").grid(row=1, column=1, sticky="e", padx=(8, 0), pady=5)
            self.custom_date_entry = ttk.Entry(scan, textvariable=self.custom_date, width=14)
            self.custom_date_entry.grid(row=1, column=2, sticky="w", padx=8, pady=5)
            ttk.Label(
                scan,
                text="開始日は「日付指定」を選んだときだけ入力できます。形式は YYYY-MM-DD です。",
                wraplength=560,
                justify="left",
            ).grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=5)
            self._refresh_period_state()

        bar = ttk.Frame(self.win)
        self.footer = bar
        ttk.Button(bar, text="キャンセル", command=self.close).pack(side="right", padx=4)
        self.save_button = ttk.Button(bar, text="保存", command=self.save)
        self.save_button.pack(side="right", padx=4)
        # Reserve the footer first. The notebook may request a taller page when
        # OAuth/DWD or tabs change, but it can only consume the remaining client
        # area and therefore cannot push Save/Cancel below the window edge.
        bar.pack(side="bottom", fill="x", padx=10, pady=(0, 10))
        notebook.pack(side="top", fill="both", expand=True, padx=10, pady=(10, 0))

    def _refresh_period_state(self):
        """Enable the start-date box only for 日付指定.

        parse_scan_start ignores the date for every other choice, so leaving the
        box editable made a typed date disappear without a word.
        """
        entry = getattr(self, "custom_date_entry", None)
        if entry is not None:
            entry.configure(state="normal" if self.period.get() == "日付指定" else "disabled")

    def _refresh_auth_mode(self):
        if normalize_auth_mode(self.auth_mode.get()) == AUTH_DWD:
            self.oauth_frame.grid_remove()
            self.dwd_frame.grid()
        else:
            self.dwd_frame.grid_remove()
            self.oauth_frame.grid()

    def _pick_dir(self, variable):
        path = filedialog.askdirectory(parent=self.win, initialdir=variable.get() or BASE_DIR)
        if path:
            variable.set(path)

    def _pick_credentials(self):
        path = filedialog.askopenfilename(parent=self.win, title="OAuthクライアントJSONを選択", filetypes=[("JSON", "*.json")])
        if path:
            self.credentials.set(path)

    def _pick_service_account(self):
        path = filedialog.askopenfilename(parent=self.win, title="サービスアカウントJSONを選択", filetypes=[("JSON", "*.json")])
        if path:
            self.service_account.set(path)

    def _refresh_account_tree(self):
        for item in self.account_tree.get_children():
            self.account_tree.delete(item)
        for index, account in enumerate(self.dwd_accounts):
            self.account_tree.insert("", "end", iid=str(index), values=(account["email"], account["final_dir"]))

    def _add_dwd_account(self):
        value = DwdAccountEditor(self.win, "処理対象アカウントの追加").show()
        if not value:
            return
        if any(item["email"].lower() == value["email"].lower() for item in self.dwd_accounts):
            messagebox.showerror("登録済みのアカウント", "同じメールアドレスがすでに登録されています。", parent=self.win)
            return
        self.dwd_accounts.append(value)
        self._refresh_account_tree()

    def _selected_account_index(self):
        selected = self.account_tree.selection()
        return int(selected[0]) if selected else None

    def _edit_dwd_account(self):
        index = self._selected_account_index()
        if index is None:
            return
        value = DwdAccountEditor(self.win, "処理対象アカウントの編集", self.dwd_accounts[index]).show()
        if not value:
            return
        if any(
            i != index and item["email"].lower() == value["email"].lower()
            for i, item in enumerate(self.dwd_accounts)
        ):
            messagebox.showerror("登録済みのアカウント", "同じメールアドレスがすでに登録されています。", parent=self.win)
            return
        self.dwd_accounts[index] = value
        self._refresh_account_tree()

    def _delete_dwd_account(self):
        index = self._selected_account_index()
        if index is not None:
            del self.dwd_accounts[index]
            self._refresh_account_tree()

    def _append_token(self, token):
        self.template.set(self.template.get() + token)
        self._refresh_preview()

    def _refresh_preview(self):
        try:
            maximum = int(self.subject_max.get() or 50)
        except ValueError:
            maximum = 50
        self.preview.set(preview_filename(self.template.get(), maximum))

    @staticmethod
    def _looks_like_extension(entry):
        body = entry[1:] if entry.startswith(".") else entry
        return 1 <= len(body) <= 10 and body.isalnum()

    def _common_values(self):
        excluded_labels = self.excluded_labels.get().strip()
        allowed_extensions_raw = self.allowed_extensions.get().strip()
        extension_entries = parse_csv_setting(allowed_extensions_raw)
        if not extension_entries:
            raise ValueError("保存する添付ファイルの拡張子を1つ以上指定してください。")
        for entry in extension_entries:
            if not self._looks_like_extension(entry):
                raise ValueError(f"拡張子の形式が正しくありません: {entry}（例: .pdf）")
        values = {
            "auth_mode": normalize_auth_mode(self.auth_mode.get()),
            "lookback_days": self.settings.get("lookback_days", "7"),
            "polling_interval": self.settings.get("polling_interval", "60"),
            "rename_template": self.template.get().strip(),
            "rename_subject_max": self.subject_max.get().strip(),
            "excluded_labels": excluded_labels,
            "allowed_extensions": allowed_extensions_raw,
        }
        ok, reason = validate_template(values["rename_template"])
        if not ok:
            raise ValueError(reason)
        try:
            maximum = int(values["rename_subject_max"])
        except ValueError as exc:
            raise ValueError("ファイル名に使う件名の最大文字数は数値で指定してください。") from exc
        if not 0 <= maximum <= 200:
            raise ValueError("ファイル名に使う件名の最大文字数は0～200で指定してください。")
        return values

    def _inputs(self):
        return {
            "email": self.email.get().strip().lower(),
            "client": self.credentials.get().strip(),
            "service_account": self.service_account.get().strip(),
        }

    def _discard_stale_stage(self, *_args):
        if self._staged is not None and not stage_matches(self._staged, self._inputs()):
            self._staged = None

    def _save_plan(self):
        """Validate every value and describe what Save commits. No side effects."""
        values = self._common_values()
        mode = values["auth_mode"]
        inputs = self._inputs()
        staged = self._staged if stage_matches(self._staged, inputs) and self._staged["mode"] == mode else None
        create_dir = None
        if mode == AUTH_DWD:
            if not self.dwd_accounts:
                raise ValueError("処理対象のアカウントを1件以上登録してください。")
            source = inputs["service_account"]
            if not source or not os.path.isfile(source):
                raise ValueError("サービスアカウントJSONファイルを指定してください。")
            # target_email/final_dir are OAuth-only fields (see get_account_configs
            # and is_configured). Copying the first DWD account into them used to be
            # a "keep legacy modules loadable" hack; the legacy module is gone, so
            # doing this now only pre-fills the OAuth form with stale DWD data if
            # the user switches auth mode back. Leave them untouched here.
            values["dwd_accounts"] = encode_dwd_accounts(self.dwd_accounts)
            accounts = normalize_dwd_accounts(self.dwd_accounts)
        else:
            email = inputs["email"]
            final_dir = self.final.get().strip()
            source = inputs["client"]
            if "@" not in email:
                raise ValueError("対象メールアドレスを正しく入力してください。（例: office@example.com）")
            if not final_dir:
                raise ValueError("添付ファイルの保存先を指定してください。")
            if not source or not os.path.isfile(source):
                raise ValueError("OAuthクライアントJSONファイルを指定してください。")
            create_dir = os.path.abspath(final_dir)
            values.update({
                "target_email": email,
                "final_dir": create_dir,
                "dwd_accounts": encode_dwd_accounts(self.dwd_accounts) if self.dwd_accounts else "[]",
            })
            accounts = [{"email": email, "final_dir": create_dir}]
        scan_start = None
        if self.initial:
            scan_start = parse_scan_start(self.period.get(), self.custom_date.get())
            check_scan_start(scan_start)
        q = queue()
        current = load_settings()
        mode_changed = normalize_auth_mode(current.get("auth_mode")) != mode
        # After an auth mode switch the credentials that will open each mailbox
        # are new, so no account keeps trust-on-first-use.
        previous = [] if mode_changed else get_account_configs(current)
        identity_writes = plan_identity_writes(
            [account["email"] for account in accounts],
            [account["email"] for account in previous],
            (staged or {}).get("identities", {}),
            lambda account: q.get_metadata(identity_key(account)),
        )
        # The pending OAuth cursor (MAIL_CURSOR_KEY) must not pass to another
        # mailbox: drop it when the auth mode changes or Save records a
        # different mailbox for an account - except when an account saved
        # untested ("") is now verified, which is what it waits for. A legacy
        # install (nothing recorded) that saves a verified mailbox drops it too;
        # the next scan then starts from lookback_days, which re-lists mail
        # but saves no duplicate files (job keys per account, kept 730 days).
        drop_pending_cursor = mode_changed
        for account, value in identity_writes.items():
            stored = q.get_metadata(identity_key(account))
            if value != stored and not (stored == "" and value):
                drop_pending_cursor = True
        return {
            "mode": mode,
            "values": values,
            "source": source,
            "create_dir": create_dir,
            "token": (staged or {}).get("creds"),
            "identity_writes": identity_writes,
            "drop_pending_cursor": drop_pending_cursor,
            "scan_start": scan_start,
        }

    def save(self):
        if self._test_running:
            return
        try:
            plan = self._save_plan()
        except Exception as exc:
            messagebox.showerror("設定内容の確認", str(exc), parent=self.win)
            return
        mismatches = identity_mismatches(plan["identity_writes"])
        if mismatches and not messagebox.askyesno(
            "メールアドレスの確認", mismatch_confirmation_text(mismatches), parent=self.win
        ):
            return
        # 1_setup.bat never starts automatic fetching, so --setup only stops a
        # running monitor (a registered watchdog starts it again unless paused).
        restart = not self.setup_only and not self.app.paused()
        try:
            saved, problem = apply_settings_update(plan, self.app.controller, queue(), restart)
        except Exception as exc:
            # Raised before anything was committed (e.g. the marker or the stop).
            log(f"Settings save failed: {exc}", "settings")
            saved, problem = False, str(exc)
        if not saved:
            messagebox.showerror("設定を保存できませんでした", problem, parent=self.win)
            return
        text = "設定を保存しました。"
        unverified = [account for account, value in plan["identity_writes"].items() if not value]
        if unverified:
            text += (
                "\n\n接続テストで確認していないアカウントがあります: " + ", ".join(unverified)
                + "\n接続テストを実行して保存し直すまで、これらのアカウントの取得は保留します。"
            )
        if problem:
            messagebox.showwarning("保存", f"{text}\n\n{problem}", parent=self.win)
        else:
            messagebox.showinfo("保存", text, parent=self.win)
        self.close()

    def _test_snapshot(self):
        """The test's inputs, read here on the Tk main thread."""
        mode = normalize_auth_mode(self.auth_mode.get())
        inputs = self._inputs()
        if mode == AUTH_DWD:
            if not self.dwd_accounts:
                raise ValueError("処理対象のアカウントを1件以上登録してください。")
            source = inputs["service_account"]
            if not source or not os.path.isfile(source):
                raise ValueError("サービスアカウントJSONファイルを指定してください。")
            return {
                "mode": AUTH_DWD,
                "service_account": os.path.abspath(source),
                "accounts": [account["email"] for account in self.dwd_accounts],
            }
        if "@" not in inputs["email"]:
            raise ValueError("対象メールアドレスを正しく入力してください。（例: office@example.com）")
        source = inputs["client"]
        if not source or not os.path.isfile(source):
            raise ValueError("OAuthクライアントJSONファイルを指定してください。")
        staged = self._staged if stage_matches(self._staged, inputs) and self._staged["mode"] == AUTH_OAUTH else None
        return {
            "mode": AUTH_OAUTH,
            "email": inputs["email"],
            "client": os.path.abspath(source),
            "force_login": bool(self.force_login.get()),
            "staged_creds": (staged or {}).get("creds"),
        }

    def _set_testing(self, running):
        # Save stays disabled while a browser login may still be completing.
        self._test_running = running
        for button in (self.save_button, self.oauth_test_button, self.dwd_test_button):
            button.state(["disabled"] if running else ["!disabled"])

    def _connection_test(self):
        if self._test_running:
            return
        try:
            snapshot = self._test_snapshot()
        except Exception as exc:
            messagebox.showerror("設定内容の確認", str(exc), parent=self.win)
            return
        self._test_generation += 1
        generation = self._test_generation
        self._set_testing(True)

        def work():
            try:
                result = run_connection_test(snapshot)
            except Exception as exc:
                result = {"level": "error", "title": "接続失敗", "text": str(exc), "staged": None}
            self.app.root.after(0, lambda: self._finish_test(generation, result))

        threading.Thread(target=work, daemon=True).start()

    def _finish_test(self, generation, result):
        if self._closed or generation != self._test_generation:
            return  # Cancelled meanwhile: the result, including any login, is dropped.
        self._set_testing(False)
        staged = result.get("staged")
        text = result["text"]
        if staged is not None and not stage_matches(staged, self._inputs()):
            staged = None
            text += "\n\nテスト中に入力内容が変わったため、この結果は保存に使いません。もう一度テストしてください。"
        # Save commits only what the latest test showed.
        self._staged = staged
        show = {"info": messagebox.showinfo, "warning": messagebox.showwarning}.get(result["level"], messagebox.showerror)
        show(result["title"], text, parent=self.win)

    def close(self):
        self._closed = True
        self._staged = None
        if self.win.winfo_exists():
            self.win.destroy()
        if self.setup_only:
            self.app.root.quit()
        elif self.initial and not is_configured():
            self.app.root.after(0, lambda: self.app.exit_app(stop_monitor=False))


class TrayApp:
    def __init__(self, setup_only=False):
        self.setup_only = setup_only
        self.root = tk.Tk()
        self.root.withdraw()
        self.controller = MonitorController()
        self.tray = None
        self.normal_icon = None
        self.error_icon = None
        self.status_window = None
        self.failed_window = None
        self.redownload_window = None
        self.last_failed_count = -1
        self.last_monitor_error = ""
        self.last_worker_issue = ""
        self.last_auth_issue = ""
        self._startup_notice = ""
        self.instance = SingleInstance("Local\\GmailAutoDownloaderTray", APP_LOCK_FILE)

    def start(self):
        if not self.instance.acquire():
            if self.setup_only:
                messagebox.showinfo("Gmail Attachment Downloader", "アプリはすでに起動しています。")
            return 0
        try:
            if self.setup_only:
                SettingsDialog(self, initial=not is_configured(), setup_only=True)
                self.root.mainloop()
                return 0
            self._prepare_startup()
            self._start_tray()
            self.root.after(3000, self._health_tick)
            self.root.mainloop()
            return 0
        finally:
            self.instance.release()

    def paused(self):
        return queue().get_metadata(PAUSED_KEY, "0") == "1"

    def _prepare_startup(self):
        """Resolve pause state and start the monitor if applicable, before the
        tray icon is created (I6). Split out from start() so it can run
        without opening a real tray icon or entering the Tk mainloop.
        """
        self._resume_from_exit_pause()
        configured = is_configured()
        if not configured:
            SettingsDialog(self, initial=True)
        elif not self.paused():
            self.controller.start()
        self._startup_notice = PAUSE_STARTUP_NOTICE if configured and self.paused() else ""

    def _resume_from_exit_pause(self):
        """Clear a pause the tray itself set on exit; a user's own (or legacy,
        reason-less) pause must survive a restart/logon instead (I6).
        """
        q = queue()
        if q.get_metadata(PAUSED_KEY, "0") == "1" and q.get_metadata(PAUSE_REASON_KEY, "") == "exit":
            q.delete_metadata(PAUSE_REASON_KEY)
            q.set_metadata(PAUSED_KEY, "0")

    def apply_scan_start(self, start):
        """期間を指定して再確認: write every account's cursor while the monitor
        is stopped. False, with nothing written, when it could not be
        confirmed stopped. The settings-update marker keeps the watchdog and
        the tray's health tick from starting it meanwhile."""
        check_scan_start(start)
        q = queue()
        begin_settings_update(q)
        try:
            if not self.controller.stop_all():
                log("Rescan aborted: the monitor could not be stopped", "rescan")
                return False
            write_scan_start(q, start, load_settings())
            return True
        finally:
            try:
                end_settings_update(q)
            except Exception as exc:
                # It expires on its own after SETTINGS_UPDATE_SECONDS.
                log(f"Settings update marker could not be cleared: {exc}", "rescan")

    @staticmethod
    def _make_icon(error=False):
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (64, 64), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((8, 14, 56, 50), outline="black", width=4)
        draw.line((8, 16, 32, 35, 56, 16), fill="black", width=4)
        if error:
            draw.ellipse((42, 2, 62, 22), fill="black")
            draw.line((52, 6, 52, 15), fill="white", width=3)
            draw.point((52, 18), fill="white")
        return image

    def _start_tray(self):
        try:
            import pystray
            from PIL import Image  # noqa: F401
        except ImportError:
            messagebox.showerror(
                "Gmail Attachment Downloader",
                "必要なライブラリ（pystray / Pillow）が見つかりません。1_setup.bat を実行してから起動し直してください。",
            )
            self.show_status()
            return
        self.normal_icon = self._make_icon(False)
        self.error_icon = self._make_icon(True)

        def call(fn):
            return lambda _icon=None, _item=None: self.root.after(0, fn)

        self.tray = pystray.Icon(
            "gmail_attachment_downloader",
            self.normal_icon,
            "Gmail Attachment Downloader",
            pystray.Menu(
                pystray.MenuItem("状態を表示", call(self.show_status), default=True),
                pystray.MenuItem("今すぐGmailを確認", call(self.run_now)),
                pystray.MenuItem("一時停止 / 再開", call(self.toggle_pause)),
                pystray.MenuItem("失敗した添付", call(self.show_failed_jobs)),
                pystray.MenuItem("添付を取り直す", call(self.show_redownload_jobs)),
                pystray.MenuItem("期間を指定して再確認", call(self.show_rescan_dialog)),
                pystray.MenuItem("設定", call(lambda: SettingsDialog(self))),
                pystray.MenuItem("添付ファイルの保存先を開く", call(self.open_final_dir)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("終了（自動取得を停止）", call(self.exit_app)),
            ),
        )
        # run_detached() returns as soon as the backend thread is scheduled,
        # before the icon is actually live; notifying right after that call
        # can race the platform backend and be silently dropped. pystray's own
        # setup callback is guaranteed to run once the icon is live, so the
        # startup pause notice is sent from there instead.
        self.tray.run_detached(setup=self._on_tray_ready)

    def _on_tray_ready(self, icon):
        icon.visible = True
        if self._startup_notice:
            try:
                icon.notify(self._startup_notice, "Gmail Attachment Downloader")
            except Exception as exc:
                log(f"startup notification failed: {exc}", "tray")

    def _worker_issue(self, counts):
        """Reason string when the attachment worker thread looks dead/stalled, else "".

        Mirrors contract C3: WORKER_ERROR_KEY non-empty, or jobs are waiting
        or processing while WORKER_HEARTBEAT_KEY is missing or too old. This
        catches a worker thread that died silently or hangs in one job, which
        otherwise leaves counts["failed"] == 0 and monitor_error empty forever.
        While a job is processing, one download may legitimately take up to
        WORKER_STALL_SECONDS without a heartbeat; with jobs only waiting, the
        idle loop keeps it within WORKER_IDLE_STALL_SECONDS. The text depends
        only on the last heartbeat, so it stays the same (one notification)
        while the counts change during one stall.
        """
        q = queue()
        worker_error = q.get_metadata(WORKER_ERROR_KEY, "") or ""
        if worker_error:
            return worker_error
        if counts.get("processing", 0) > 0:
            limit = WORKER_STALL_SECONDS
        elif counts.get("pending", 0) > 0:
            limit = WORKER_IDLE_STALL_SECONDS
        else:
            return ""
        raw = q.get_metadata(WORKER_HEARTBEAT_KEY)
        try:
            age = time.time() - float(raw)
        except (TypeError, ValueError):
            return "添付の保存処理が応答していません（応答の記録がありません）"
        if age > limit:
            return f"添付の保存処理が応答していません（最後の応答: {format_time(raw)}）"
        return ""

    def _health_tick(self):
        try:
            q = queue()
            # A Save in progress stops the monitor on purpose and restarts it itself.
            if is_configured() and not self.paused() and not settings_update_in_progress(q):
                heartbeat = read_heartbeat(HEARTBEAT_FILE)
                heartbeat_fresh = False
                if heartbeat:
                    try:
                        heartbeat_fresh = (time.time() - float(heartbeat.get("timestamp", 0))) < 180
                    except (TypeError, ValueError):
                        heartbeat_fresh = False
                # The PowerShell Win32_Process probe inside controller.start() is
                # expensive; skip it while the heartbeat file already proves the
                # monitor process is alive and recent, and only pay for the probe
                # when the heartbeat looks stale or missing.
                if not heartbeat_fresh:
                    self.controller.start()
            counts = q.counts()
            monitor_error = q.get_metadata(LAST_ERROR_KEY, "") or ""
            # While paused the monitor is deliberately stopped, so pending jobs
            # and a stale worker heartbeat are expected. Checking anyway would
            # turn the tray red and fire a notification every time the user
            # pauses or exits, which is exactly when they know it is stopped.
            worker_issue = "" if self.paused() else self._worker_issue(counts)
            auth_issue = "" if self.paused() else format_auth_issues(read_auth_issues(q))
            has_error = counts["failed"] > 0 or bool(monitor_error) or bool(worker_issue) or bool(auth_issue)
            if self.tray is not None:
                if self.paused():
                    # Every tick otherwise overwrites this with the plain title
                    # below, which used to erase the only sign that a pause
                    # (not a crash) is why nothing is being fetched. Existing
                    # failed jobs are still real failures while paused, so the
                    # error icon/count must not disappear just because the
                    # pause also explains why nothing new is being fetched.
                    if counts["failed"] > 0:
                        self.tray.icon = self.error_icon
                        self.tray.title = f"Gmail Attachment Downloader - 一時停止中 / 失敗 {counts['failed']}件"
                    else:
                        self.tray.icon = self.normal_icon
                        self.tray.title = "Gmail Attachment Downloader - 一時停止中"
                else:
                    self.tray.icon = self.error_icon if has_error else self.normal_icon
                    self.tray.title = (
                        f"Gmail Attachment Downloader - 失敗 {counts['failed']}件"
                        if has_error else "Gmail Attachment Downloader"
                    )
                if self.last_failed_count >= 0 and counts["failed"] > self.last_failed_count:
                    try:
                        self.tray.notify(f"添付ファイル取得失敗が {counts['failed']} 件あります。", "Gmail Attachment Downloader")
                    except Exception:
                        pass
                if monitor_error and monitor_error != self.last_monitor_error:
                    try:
                        self.tray.notify(monitor_error[:180], "Gmail接続エラー")
                    except Exception:
                        pass
                if worker_issue and worker_issue != self.last_worker_issue:
                    try:
                        self.tray.notify(worker_issue[:180], "添付の保存処理が停止しています")
                    except Exception:
                        pass
                if auth_issue and auth_issue != self.last_auth_issue:
                    try:
                        self.tray.notify(auth_issue[:180], "Gmail認証の確認が必要です")
                    except Exception:
                        pass
            self.last_failed_count = counts["failed"]
            self.last_monitor_error = monitor_error
            self.last_worker_issue = worker_issue
            self.last_auth_issue = auth_issue
        finally:
            if self.root.winfo_exists():
                self.root.after(30000, self._health_tick)

    def run_now(self):
        if self.paused():
            messagebox.showinfo(
                "Gmail Attachment Downloader",
                "一時停止中です。トレイメニューの「一時停止 / 再開」で再開してから実行してください。",
            )
            return
        if not self.controller.restart():
            messagebox.showerror("Gmail Attachment Downloader", RUN_NOW_FAILED_TEXT)
            return
        messagebox.showinfo("Gmail Attachment Downloader", "Gmail確認を開始しました。")

    def toggle_pause(self):
        q = queue()
        if self.paused():
            # Clear the reason before the flag: if this is interrupted between
            # the two writes, the flag stays "1" and the pause is kept (with no
            # reason, i.e. treated like a user/legacy pause) rather than
            # silently resuming.
            q.delete_metadata(PAUSE_REASON_KEY)
            q.set_metadata(PAUSED_KEY, "0")
            if not self.controller.start():
                messagebox.showwarning("Gmail Attachment Downloader", f"自動取得を再開しました。\n{START_FAILED_TEXT}")
                return
            messagebox.showinfo("Gmail Attachment Downloader", "自動取得を再開しました。")
        else:
            q.set_metadata(PAUSE_REASON_KEY, "user")
            q.set_metadata(PAUSED_KEY, "1")
            if not self.controller.stop_all():
                # Not paused after all: the monitor may still be running.
                q.delete_metadata(PAUSE_REASON_KEY)
                q.set_metadata(PAUSED_KEY, "0")
                messagebox.showerror("Gmail Attachment Downloader", PAUSE_STOP_FAILED_TEXT)
                return
            messagebox.showinfo("Gmail Attachment Downloader", "自動取得を一時停止しました。")

    def _cursor_summary(self):
        settings = load_settings()
        dwd = normalize_auth_mode(settings.get("auth_mode")) == AUTH_DWD
        accounts = get_account_configs(settings)
        q = queue()
        values = []
        for account in accounts:
            raw = q.get_metadata(cursor_key(q, account["email"], dwd) or MAIL_CURSOR_KEY)
            if raw is not None:
                try:
                    values.append(float(raw))
                except (TypeError, ValueError):
                    pass
        if not accounts:
            return "-"
        if not values:
            return f"未確認（0/{len(accounts)}アカウント）"
        return f"確認済み {len(values)}/{len(accounts)}アカウント / 最も古い確認時点 {format_time(min(values))}"

    def show_status(self):
        if self.status_window is not None and self.status_window.winfo_exists():
            self.status_window.lift()
            return
        win = tk.Toplevel(self.root)
        self.status_window = win
        win.title("Gmail Attachment Downloader - 状態")
        values = {key: tk.StringVar() for key in ("mode", "health", "detail", "cursor", "jobs", "error")}
        for row, (label, key) in enumerate([
            ("接続方式", "mode"),
            ("状態", "health"),
            ("状態の理由", "detail"),
            ("メール確認の状況", "cursor"),
            ("添付ファイルの処理状況", "jobs"),
            ("直近のエラー", "error"),
        ]):
            ttk.Label(win, text=label).grid(row=row, column=0, sticky="nw", padx=10, pady=6)
            ttk.Label(win, textvariable=values[key], width=80, wraplength=650).grid(row=row, column=1, sticky="w", padx=10, pady=6)
        bar = ttk.Frame(win)
        bar.grid(row=6, column=0, columnspan=2, sticky="e", padx=10, pady=10)
        ttk.Button(bar, text="今すぐGmailを確認", command=self.run_now).pack(side="left", padx=4)
        ttk.Button(bar, text="失敗した添付", command=self.show_failed_jobs).pack(side="left", padx=4)
        ttk.Button(bar, text="添付を取り直す", command=self.show_redownload_jobs).pack(side="left", padx=4)
        ttk.Button(bar, text="閉じる", command=win.destroy).pack(side="left", padx=4)

        def refresh():
            if not win.winfo_exists():
                return
            q = queue()
            counts = q.counts()
            heartbeat = read_heartbeat(HEARTBEAT_FILE)
            settings = load_settings()
            mode = normalize_auth_mode(settings.get("auth_mode"))
            accounts = get_account_configs(settings)
            monitor_error = q.get_metadata(LAST_ERROR_KEY, "") or ""
            # Paused is an intended state, not a worker fault - see _health_tick.
            worker_issue = "" if self.paused() else self._worker_issue(counts)
            auth_issue = format_auth_issues(read_auth_issues(q))
            values["mode"].set(("Google Workspace DWD" if mode == AUTH_DWD else "OAuth") + f" / {len(accounts)}アカウント")
            if self.paused():
                health = "一時停止中"
            elif monitor_error or counts["failed"] or worker_issue or auth_issue:
                health = "要確認"
            elif heartbeat:
                age = time.time() - float(heartbeat.get("timestamp", 0))
                health = "正常" if age < 180 and heartbeat.get("status") == "ok" else "要確認"
            else:
                health = "未起動（監視プロセスの応答がありません）"
            values["health"].set(health)
            # "要確認" used to leave 詳細 and 直近エラー at "-" whenever the trigger
            # was a failed job, so the screen never said what needed checking.
            reasons = []
            if self.paused():
                reasons.append("トレイメニューの「一時停止 / 再開」で再開できます。")
            if counts["failed"]:
                reasons.append(
                    f"取得できなかった添付が {counts['failed']}件あります。"
                    "「失敗した添付」から内容の確認と再取得ができます。"
                )
            if monitor_error:
                reasons.append(f"Gmailへの接続でエラーが出ています: {monitor_error}")
            if worker_issue:
                reasons.append(worker_issue)
            if auth_issue:
                reasons.append(f"認証の確認が必要なため、保存を保留している添付があります: {auth_issue}")
            heartbeat_detail = (heartbeat or {}).get("detail", "")
            if not reasons and heartbeat_detail:
                reasons.append(heartbeat_detail)
            values["detail"].set(" / ".join(reasons) if reasons else "問題は見つかっていません。")
            values["cursor"].set(self._cursor_summary())
            values["jobs"].set(
                "待機 {pending} / 処理中 {processing} / 成功 {success} / 失敗 {failed} / 無視 {ignored}".format(**counts)
            )
            values["error"].set(" / ".join(filter(None, [monitor_error, worker_issue, auth_issue])) or "-")
            win.after(3000, refresh)

        refresh()
        center_on_parent(win, self.root)

    def show_failed_jobs(self):
        if self.failed_window is not None and self.failed_window.winfo_exists():
            self.failed_window.lift()
            return
        win = tk.Toplevel(self.root)
        self.failed_window = win
        win.title("Gmail Attachment Downloader - 失敗した添付")
        win.geometry("1050x600")
        win.minsize(720, 420)
        show_ignored = tk.BooleanVar(value=False)
        ttk.Checkbutton(win, text="無視した添付も表示", variable=show_ignored).pack(anchor="w", padx=10, pady=(8, 0))
        columns = ("id", "ignored", "account", "subject", "filename", "updated", "error")
        container, tree = build_scrollable_tree(win, columns, [
            ("id", "ID", 55),
            ("ignored", "無視", 50),
            ("account", "アカウント", 200),
            ("subject", "件名", 260),
            ("filename", "添付ファイル名", 220),
            ("updated", "失敗日時", 140),
            ("error", "エラー内容", 420),
        ])
        container.pack(fill="both", expand=True, padx=10, pady=10)
        detail_frame, detail = build_detail_pane(win, height=9)
        detail_frame.pack(fill="both", padx=10, pady=(0, 8))

        def jobs():
            return queue().list_failed(include_ignored=show_ignored.get())

        def refresh():
            for item in tree.get_children():
                tree.delete(item)
            for job in jobs():
                payload = job["payload"]
                tree.insert("", "end", iid=str(job["id"]), values=(
                    job["id"],
                    "済" if job["ignored"] else "",
                    payload.get("account_email", ""),
                    payload.get("mail_subject", ""),
                    payload.get("filename", ""),
                    format_time(job["updated_at"]),
                    (job["last_error"] or "")[:180],
                ))

        def selected_ids():
            return [int(item) for item in tree.selection()]

        def show_detail(_event=None):
            detail.delete("1.0", "end")
            selected = tree.selection()
            if not selected:
                return
            job = queue().get_job(int(selected[0]))
            if job:
                detail.insert("end", format_job_detail(job))

        def retry():
            ids = selected_ids()
            for job_id in ids:
                queue().retry_job(job_id)
            if ids and not self.paused():
                self.controller.start()
            refresh()

        def ignore():
            ids = selected_ids()
            if ids and messagebox.askyesno(
                "無視の確認",
                f"選択した {len(ids)}件を無視します。よろしいですか？\n"
                "無視した添付は失敗一覧に表示されなくなります。",
                parent=win,
            ):
                for job_id in ids:
                    queue().ignore_job(job_id)
                refresh()

        def unignore():
            for job_id in selected_ids():
                queue().unignore_job(job_id)
            refresh()

        tree.bind("<<TreeviewSelect>>", show_detail)
        show_ignored.trace_add("write", lambda *_args: refresh())
        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bar, text="もう一度取得する", command=retry).pack(side="left", padx=4)
        ttk.Button(bar, text="無視する", command=ignore).pack(side="left", padx=4)
        ttk.Button(bar, text="無視を解除", command=unignore).pack(side="left", padx=4)
        ttk.Button(bar, text="一覧を更新", command=refresh).pack(side="left", padx=4)
        ttk.Button(bar, text="閉じる", command=win.destroy).pack(side="right", padx=4)
        refresh()
        center_on_parent(win, self.root)

    def show_redownload_jobs(self):
        if self.redownload_window is not None and self.redownload_window.winfo_exists():
            self.redownload_window.lift()
            return
        win = tk.Toplevel(self.root)
        self.redownload_window = win
        win.title("Gmail Attachment Downloader - 添付を取り直す")
        win.geometry("980x560")
        win.minsize(720, 420)
        ttk.Label(
            win,
            text="直近90日以内に取得できた添付を、もう一度ダウンロードします。保存済みファイルを誤って削除した場合などに使用します。",
            wraplength=940,
            justify="left",
        ).pack(anchor="w", padx=10, pady=8)
        columns = ("id", "account", "subject", "filename", "updated")
        container, tree = build_scrollable_tree(win, columns, [
            ("id", "ID", 60),
            ("account", "アカウント", 220),
            ("subject", "件名", 320),
            ("filename", "添付ファイル名", 240),
            ("updated", "取得日時", 150),
        ])
        container.pack(fill="both", expand=True, padx=10, pady=8)
        # Same reason as the failure list: the columns clip Japanese values, so the
        # selected row is repeated in full here instead of being unreachable.
        detail_frame, detail = build_detail_pane(win, height=8)
        detail_frame.pack(fill="both", padx=10, pady=(0, 8))

        def show_detail(_event=None):
            detail.delete("1.0", "end")
            selected = tree.selection()
            if not selected:
                return
            job = queue().get_job(int(selected[0]))
            if job:
                detail.insert("end", format_job_detail(job))

        tree.bind("<<TreeviewSelect>>", show_detail)

        def refresh():
            for item in tree.get_children():
                tree.delete(item)
            for job in queue().list_recent_success():
                payload = job["payload"]
                tree.insert("", "end", iid=str(job["id"]), values=(
                    job["id"],
                    payload.get("account_email", ""),
                    payload.get("mail_subject", ""),
                    payload.get("filename", ""),
                    format_time(job["updated_at"]),
                ))

        def redownload():
            ids = [int(item) for item in tree.selection()]
            if not ids:
                return
            if not messagebox.askyesno(
                "取り直し",
                f"{len(ids)}件の添付を再取得しますか？\n"
                "元のファイルが保存先に残っている場合、新しいファイルは別名（連番付き）で保存されます。",
                parent=win,
            ):
                return
            changed = sum(1 for job_id in ids if queue().retry_success_job(job_id))
            if changed and not self.paused():
                self.controller.start()
            refresh()

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bar, text="選択した添付を取り直す", command=redownload).pack(side="left", padx=4)
        ttk.Button(bar, text="一覧を更新", command=refresh).pack(side="left", padx=4)
        ttk.Button(bar, text="閉じる", command=win.destroy).pack(side="right", padx=4)
        refresh()
        center_on_parent(win, self.root)

    def show_rescan_dialog(self):
        win = tk.Toplevel(self.root)
        win.title("期間を指定して再確認")
        win.transient(self.root)
        period = tk.StringVar(value="過去7日")
        custom = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        ttk.Label(
            win,
            text=(
                "どこまで遡ってGmailを確認し直しますか？ "
                "Google Workspace の複数アカウントでは、登録した全アカウントに同じ範囲を適用します。"
            ),
            wraplength=420,
            justify="left",
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=10)
        period_box = ttk.Combobox(
            win,
            textvariable=period,
            values=["今日", "過去3日", "過去7日", "過去30日", "日付指定"],
            state="readonly",
        )
        period_box.grid(row=1, column=0, sticky="w", padx=10, pady=6)
        ttk.Label(win, text="開始日").grid(row=1, column=1, sticky="e", padx=(10, 0), pady=6)
        custom_entry = ttk.Entry(win, textvariable=custom, width=14)
        custom_entry.grid(row=1, column=2, sticky="w", padx=10, pady=6)
        ttk.Label(
            win,
            text="開始日は「日付指定」を選んだときだけ入力できます。形式は YYYY-MM-DD です。",
            wraplength=420,
            justify="left",
        ).grid(row=2, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 6))

        def refresh_state(_event=None):
            custom_entry.configure(state="normal" if period.get() == "日付指定" else "disabled")

        period_box.bind("<<ComboboxSelected>>", refresh_state)
        refresh_state()

        def apply():
            try:
                start = parse_scan_start(period.get(), custom.get())
                applied = self.apply_scan_start(start)
            except Exception as exc:
                messagebox.showerror("期間エラー", str(exc), parent=win)
                return
            if not applied:
                messagebox.showerror("再確認できませんでした", RESCAN_STOP_FAILED_TEXT, parent=win)
                return
            started = True
            if not self.paused():
                started = self.controller.start()
            win.destroy()
            text = f"{start:%Y/%m/%d %H:%M} 以降を再確認します。\n同一メール・同一添付は重複登録されません。"
            if started:
                messagebox.showinfo("再スキャン", text)
            else:
                messagebox.showwarning("再スキャン", f"{text}\n{START_FAILED_TEXT}")

        bar = ttk.Frame(win)
        bar.grid(row=3, column=0, columnspan=3, sticky="e", padx=10, pady=10)
        ttk.Button(bar, text="キャンセル", command=win.destroy).pack(side="right", padx=4)
        ttk.Button(bar, text="実行", command=apply).pack(side="right", padx=4)
        win.grab_set()
        center_on_parent(win, self.root)

    def open_final_dir(self):
        accounts = get_account_configs(load_settings())
        if not accounts:
            messagebox.showerror("保存先", "添付ファイルの保存先が設定されていません。設定画面で指定してください。")
            return
        if len(accounts) == 1:
            return self._open_folder(accounts[0]["final_dir"])
        win = tk.Toplevel(self.root)
        win.title("添付ファイルの保存先を開く")
        container, tree = build_scrollable_tree(
            win,
            ("email", "folder"),
            [("email", "アカウント", 240), ("folder", "添付ファイルの保存先", 460)],
            height=min(12, len(accounts)),
        )
        for index, account in enumerate(accounts):
            tree.insert("", "end", iid=str(index), values=(account["email"], account["final_dir"]))
        container.pack(fill="both", expand=True, padx=10, pady=10)

        def open_selected():
            selected = tree.selection()
            if selected:
                self._open_folder(accounts[int(selected[0])]["final_dir"])

        ttk.Button(win, text="フォルダーを開く", command=open_selected).pack(anchor="e", padx=10, pady=(0, 10))
        center_on_parent(win, self.root)

    def _open_folder(self, path):
        if not path or not os.path.isdir(path):
            messagebox.showerror("保存先", "添付ファイルの保存先フォルダーが見つかりません。")
            return
        if os.name == "nt":
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])

    def exit_app(self, stop_monitor=True):
        if stop_monitor:
            q = queue()
            if not self.paused():
                # Only a not-paused -> paused transition here is "exit" and
                # gets auto-cleared on the next start; an already-paused
                # instance (user pause) keeps its existing reason (I6).
                q.set_metadata(PAUSE_REASON_KEY, "exit")
                q.set_metadata(PAUSED_KEY, "1")
            # The exit pause stays either way, so the watchdog stays idle
            # until the next start clears it.
            if not self.controller.stop_all():
                messagebox.showwarning("Gmail Attachment Downloader", EXIT_STOP_FAILED_TEXT)
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.quit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", action="store_true", help="設定GUIだけ開く")
    args = parser.parse_args()
    return TrayApp(setup_only=args.setup).start()


if __name__ == "__main__":
    raise SystemExit(main())
