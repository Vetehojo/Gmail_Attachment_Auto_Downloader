"""Durable multi-account Gmail attachment downloader.

The general-purpose build processes Gmail attachments only. Message-body URLs
and browser automation are intentionally absent. Gmail scanning and attachment
downloading run independently so a slow download cannot stop mailbox cursor
advancement.
"""
import argparse
import base64
import hashlib
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from app_settings import (
    AUTH_DWD,
    BASE_DIR,
    credential_file_digest,
    get_account_configs,
    is_configured,
    load_allowed_extensions,
    load_excluded_labels,
    load_settings,
    normalize_auth_mode,
)
from filename_rules import filename_budget, render_filename
from gmail_auth import AccountNotConfiguredError, AuthenticationRequiredError, get_gmail_service
from job_queue import JobQueue
from runtime_state import (
    SingleInstance,
    append_log,
    atomic_write_json,
    clear_auth_issue,
    identity_key,
    set_auth_issue,
    write_heartbeat,
)

STATE_DIR = os.path.join(BASE_DIR, "state")
QUEUE_DB = os.path.join(STATE_DIR, "jobs.sqlite3")
HEARTBEAT_FILE = os.path.join(STATE_DIR, "heartbeat.json")
LOCK_FILE = os.path.join(STATE_DIR, "monitor.lock")
# The tray's single-instance lock (gmail_app.APP_LOCK_FILE); the trial checks it.
TRAY_LOCK_FILE = os.path.join(STATE_DIR, "app.lock")
LOG_FILE = os.path.join(BASE_DIR, "log", "mail_log.txt")
MONITOR_MUTEX_NAME = "Local\\GmailAutoDownloaderMonitor"
TRAY_MUTEX_NAME = "Local\\GmailAutoDownloaderTray"
MAIL_CURSOR_KEY = "mail_cursor_timestamp"
RETENTION_KEY = "jobs_retention_last_run"
LAST_ERROR_KEY = "monitor_last_error"
WORKER_HEARTBEAT_KEY = "worker_heartbeat"
WORKER_ERROR_KEY = "worker_last_error"
WORKER_HEARTBEAT_INTERVAL = 15
QUERY_OVERLAP = timedelta(minutes=5)
AUTH_DEFER_SECONDS = 15 * 60
MAX_JOBS_PER_ONCE = 20
MAX_ATTACHMENT_SIZE = 100 * 1024 * 1024
TEMP_PREFIX = ".gmailad_"
TEMP_SUFFIX = ".tmp"
TRIAL_DEFAULT_MESSAGES = 3
TRIAL_MAX_MESSAGES = 20
TRIAL_SCAN_CAP = 100
TRIAL_EXIT_OK = 0
TRIAL_EXIT_FAILED = 1
TRIAL_EXIT_BLOCKED = 2
_INLINE_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".svg"}


def log(message, procedure="monitor"):
    append_log(LOG_FILE, message, procedure)


def _account_id(account_email):
    return str(account_email or "").strip().lower()


def cursor_key(account_email, auth_mode=None):
    mode = normalize_auth_mode(auth_mode or load_settings().get("auth_mode"))
    if mode == AUTH_DWD:
        return f"{MAIL_CURSOR_KEY}:{_account_id(account_email)}"
    return MAIL_CURSOR_KEY


def get_mail_cursor(queue, account_email, auth_mode=None):
    raw = queue.get_metadata(cursor_key(account_email, auth_mode))
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(float(raw))
    except (TypeError, ValueError, OSError):
        return None


def build_mail_query(start_time, target_email=None, auth_mode=None, excluded_labels=None):
    """Search the authenticated/impersonated mailbox, not a To: header value.

    OAuth and DWD both already select a concrete mailbox. A To: filter would
    drop BCC, aliases, Groups/ML delivery and forwarded messages.
    """
    del target_email, auth_mode
    return f"after:{int(start_time.timestamp())} in:inbox" + _excluded_label_terms(excluded_labels)


def build_trial_query(excluded_labels=None):
    """The trial ignores the setup period: no after:, Gmail's default order (newest first)."""
    return "in:inbox has:attachment" + _excluded_label_terms(excluded_labels)


def _excluded_label_terms(excluded_labels):
    return "".join(f' -label:"{label}"' for label in excluded_labels or [])


def iter_message_ids(service, query, account_email=""):
    page_token = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute(num_retries=3)
        for item in result.get("messages", []):
            message_id = item.get("id")
            if message_id:
                yield message_id
        page_token = result.get("nextPageToken")
        if not page_token:
            break
        write_heartbeat(HEARTBEAT_FILE, "ok", f"gmail pagination: {account_email}", log_path=LOG_FILE)


def _header(headers, name, default=""):
    name = name.lower()
    return next((h.get("value", default) for h in headers if h.get("name", "").lower() == name), default)


def decode_mime_header_value(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def extract_sender_email(sender_header):
    _name, address = parseaddr(sender_header or "")
    return (address or sender_header or "unknown@sender.com").strip()


def get_received_date_yyyymmdd(date_header):
    try:
        return parsedate_to_datetime(date_header).strftime("%Y%m%d")
    except Exception:
        return datetime.now().strftime("%Y%m%d")


def _is_inline_image_part(part):
    """A signature logo / embedded banner referenced from the message body, not a document.

    A real attachment carries ``Content-Disposition: attachment``, and a
    sender who inlines a PDF has no image extension, so both keep working.
    """
    headers = part.get("headers", []) or []
    content_id = _header(headers, "Content-ID", "")
    disposition = _header(headers, "Content-Disposition", "")
    if not content_id or not disposition.strip().lower().startswith("inline"):
        return False
    filename = decode_mime_header_value(part.get("filename", ""))
    extension = os.path.splitext(filename)[1].lower()
    return extension in _INLINE_IMAGE_EXTENSIONS


def get_attachments_from_payload(payload):
    """Return (attachments, inline_image_skips).

    ``attachments`` includes MIME attachments, including small parts whose
    bytes are inline. Parts identified as inline signature logos/banners by
    ``_is_inline_image_part`` are excluded, and their count is returned
    separately so a caller can log one summary line per message instead of
    one line per logo.
    """
    attachments = []
    inline_image_skips = 0

    def visit(parts):
        nonlocal inline_image_skips
        for part in parts or []:
            if _is_inline_image_part(part):
                inline_image_skips += 1
                visit(part.get("parts", []))
                continue
            filename = decode_mime_header_value(part.get("filename", ""))
            body = part.get("body", {}) or {}
            attachment_id = body.get("attachmentId", "")
            inline_data = body.get("data", "")
            if filename and (attachment_id or inline_data):
                attachments.append({
                    "filename": filename,
                    "attachment_id": attachment_id,
                    "inline_data": inline_data,
                    "part_id": str(part.get("partId", "")),
                    "size": int(body.get("size", 0) or 0),
                })
            visit(part.get("parts", []))

    # Start at the payload itself: a single-part message (e.g. a bare PDF)
    # carries its filename and attachmentId/data on the top-level payload
    # (partId ""). A multipart top level has no filename, so it adds nothing
    # and its children produce exactly the jobs they did before.
    visit([payload] if payload else [])
    return attachments, inline_image_skips


def is_allowed_attachment(filename, size, allowed_extensions):
    if not filename:
        return False, "filename is empty"
    extension = os.path.splitext(filename)[1].lower()
    if extension not in allowed_extensions:
        return False, f"extension {extension} is not allowed"
    if int(size or 0) > MAX_ATTACHMENT_SIZE:
        return False, "attachment exceeds the 100MB limit"
    return True, "ok"


def _attachment_identity(attachment):
    if attachment.get("attachment_id"):
        return f"id:{attachment['attachment_id']}"
    inline_data = attachment.get("inline_data", "")
    inline_hash = hashlib.sha256(inline_data.encode("ascii", errors="ignore")).hexdigest()
    return f"inline:{attachment.get('part_id', '')}:{inline_hash}"


def _attachment_job_key(account_email, message_id, attachment):
    digest = hashlib.sha256(_attachment_identity(attachment).encode("utf-8")).hexdigest()
    return f"attachment:{_account_id(account_email)}:{message_id}:{digest}"


def enqueue_message_jobs(queue, service, message_id, account_email="", final_dir=None, allowed_extensions=None):
    """Returns how many new jobs were created (scan_gmail's count)."""
    return enqueue_message(queue, service, message_id, account_email, final_dir, allowed_extensions)["added"]


def enqueue_message(queue, service, message_id, account_email="", final_dir=None, allowed_extensions=None):
    """Enqueue one message's allowed attachments and describe the result.

    Returns a dict: "added" (new jobs), "job_keys" (every allowed attachment's
    key, new or already queued), "skipped" (disallowed attachments with
    filename/size/reason), "subject" and "received_date".
    """
    settings = load_settings()
    account_email = _account_id(account_email) or _account_id(settings.get("target_email"))
    final_dir = os.path.abspath(final_dir or settings.get("final_dir") or ".")
    if allowed_extensions is None:
        allowed_extensions = load_allowed_extensions(settings)
    message = service.users().messages().get(
        userId="me", id=message_id, format="full"
    ).execute(num_retries=3)
    payload = message.get("payload", {})
    headers = payload.get("headers", [])
    subject = decode_mime_header_value(_header(headers, "subject", "No Subject"))
    sender_email = extract_sender_email(decode_mime_header_value(_header(headers, "from", "")))
    received_date = get_received_date_yyyymmdd(_header(headers, "date", ""))

    common = {
        "account_email": account_email,
        "final_dir": final_dir,
        "message_id": message_id,
        "mail_subject": subject,
        "received_date": received_date,
        "sender_email": sender_email,
    }

    added = 0
    job_keys = []
    skipped = []
    attachments, inline_image_skips = get_attachments_from_payload(payload)
    if inline_image_skips:
        log(f"Skipped {inline_image_skips} inline image part(s): subject={subject}", "scan")
    for attachment in attachments:
        allowed, reason = is_allowed_attachment(attachment["filename"], attachment["size"], allowed_extensions)
        if not allowed:
            log(
                f"Skipped attachment: subject={subject} file={attachment['filename']} reason={reason}",
                "scan",
            )
            skipped.append({"filename": attachment["filename"], "size": attachment["size"], "reason": reason})
            continue
        # Attachment bytes never enter the job payload persisted to SQLite;
        # the worker re-fetches them via attachment_id or (message_id, part_id).
        job_payload = dict(common)
        job_payload.update({
            "attachment_id": attachment.get("attachment_id", ""),
            "part_id": attachment.get("part_id", ""),
            "inline": bool(not attachment.get("attachment_id") and attachment.get("inline_data")),
            "filename": attachment["filename"],
        })
        job_key = _attachment_job_key(account_email, message_id, attachment)
        job_keys.append(job_key)
        if queue.enqueue(job_key, job_payload):
            added += 1
    return {
        "added": added,
        "job_keys": job_keys,
        "skipped": skipped,
        "subject": subject,
        "received_date": received_date,
    }


def scan_gmail(queue, service, account_email=None, final_dir=None, auth_mode=None):
    settings = load_settings()
    mode = normalize_auth_mode(auth_mode or settings.get("auth_mode"))
    account_email = _account_id(account_email or settings.get("target_email"))
    final_dir = os.path.abspath(final_dir or settings.get("final_dir") or ".")
    if not account_email:
        raise ValueError("account email is required")

    excluded_labels = load_excluded_labels(settings)
    allowed_extensions = load_allowed_extensions(settings)

    scan_started = datetime.now()
    cursor = get_mail_cursor(queue, account_email, mode)
    try:
        lookback_days = max(1, int(settings.get("lookback_days", "7")))
    except ValueError:
        lookback_days = 7

    # lookback_days is an initial-scan bound only. Once the durable cursor
    # exists, downtime of any length resumes from cursor - overlap.
    start_time = scan_started - timedelta(days=lookback_days) if cursor is None else cursor - QUERY_OVERLAP
    query = build_mail_query(start_time, account_email, mode, excluded_labels=excluded_labels)

    message_count = 0
    attachment_count = 0
    write_heartbeat(HEARTBEAT_FILE, "ok", f"gmail scan: {account_email}", log_path=LOG_FILE)
    for message_id in iter_message_ids(service, query, account_email):
        attachment_count += enqueue_message_jobs(
            queue, service, message_id, account_email, final_dir, allowed_extensions=allowed_extensions
        )
        message_count += 1
        if message_count % 10 == 0:
            write_heartbeat(
                HEARTBEAT_FILE, "ok", f"gmail scan {account_email}: {message_count} messages", log_path=LOG_FILE
            )

    # Advance only after pagination and all enqueue operations complete.
    queue.set_metadata(cursor_key(account_email, mode), scan_started.timestamp())
    log(
        f"Scan complete [{account_email}]: messages={message_count}, "
        f"new_attachment_jobs={attachment_count}, start={start_time:%Y/%m/%d %H:%M:%S}",
        "scan",
    )
    return {"account": account_email, "messages": message_count, "attachments": attachment_count}


def verify_mailbox_identity(queue, account_email, service):
    """Check the mailbox a service really opens against the identity stored for
    the account (see runtime_state.IDENTITY_KEY_PREFIX), not against the typed
    address, so an alias typed in the settings keeps working. Raises
    AuthenticationRequiredError, so the caller defers instead of advancing a
    cursor or saving into the account's folder."""
    account_id = _account_id(account_email)
    profile = service.users().getProfile(userId="me").execute(num_retries=3)
    actual = _account_id((profile or {}).get("emailAddress") if isinstance(profile, dict) else "")
    if not actual:
        raise AuthenticationRequiredError(
            f"Gmailが認証したメールボックスのアドレスを返さなかったため、取得を止めています（{account_id}）。"
        )
    key = identity_key(account_id)
    stored = queue.get_metadata(key)
    if stored is None:
        # Installed before identities were recorded: trust on first use.
        queue.set_metadata(key, actual)
        log(f"Recorded mailbox identity on first use [{account_id}]: {actual}", "auth")
        return actual
    stored = _account_id(stored)
    if not stored:
        raise AuthenticationRequiredError(
            f"{account_id} は接続テストで確認されていないため、取得を保留しています。"
            "設定画面で接続テストを実行してから保存してください。"
        )
    if stored != actual:
        raise AuthenticationRequiredError(
            f"{account_id} で確認済みのメールボックス（{stored}）と、"
            f"認証されたメールボックス（{actual}）が異なるため、取得を止めています。"
            "設定画面で接続テストを実行し直してから保存してください。"
        )
    return actual


class ServicePool:
    """Gmail services per account. Every get() re-reads the settings: the pool
    drops its services when the auth mode or the installed credential file
    (SHA-256) changed since they were built, refuses accounts that are no
    longer configured, and uses a newly built service only after
    verify_mailbox_identity accepted it."""

    def __init__(self, queue, allow_interactive=False):
        self.queue = queue
        self.allow_interactive = allow_interactive
        self.auth_mode = None
        self._fingerprint = None
        self._services = {}
        self._digest_failing = False

    def _credential_digest(self, mode):
        """credential_file_digest(mode); on a read error (e.g. the file is
        locked while it is being replaced) the digest this pool already has for
        the mode, i.e. "unchanged", logged once until a read succeeds again."""
        try:
            digest = credential_file_digest(mode)
        except OSError as exc:
            if not self._digest_failing:
                log(f"Could not read the installed credential file; keeping the current Gmail services: {exc}", "auth")
                self._digest_failing = True
            return self._fingerprint[1] if self._fingerprint and self._fingerprint[0] == mode else None
        if self._digest_failing:
            log("Installed credential file is readable again", "auth")
            self._digest_failing = False
        return digest

    def _sync(self, settings):
        mode = normalize_auth_mode(settings.get("auth_mode"))
        fingerprint = (mode, self._credential_digest(mode))
        if fingerprint != self._fingerprint:
            if self._services:
                log(f"Authentication settings changed (mode={mode}); rebuilding Gmail services", "auth")
            self._services.clear()
            self._fingerprint = fingerprint
            self.auth_mode = mode
        return mode

    def get(self, account_email):
        settings = load_settings()
        mode = self._sync(settings)
        account_id = _account_id(account_email)
        if account_id not in {account["email"] for account in get_account_configs(settings)}:
            raise AccountNotConfiguredError(f"このアカウントは現在の設定に含まれていません: {account_id or '(空欄)'}")
        service = self._services.get(account_id)
        if service is None:
            service = get_gmail_service(
                account_email=account_id,
                allow_interactive=self.allow_interactive,
                auth_mode=mode,
            )
            verify_mailbox_identity(self.queue, account_id, service)
            self._services[account_id] = service
        return service

    def invalidate(self, account_email):
        self._services.pop(_account_id(account_email), None)


def scan_all_accounts(queue, pool=None):
    settings = load_settings()
    mode = normalize_auth_mode(settings.get("auth_mode"))
    accounts = get_account_configs(settings)
    if not accounts:
        raise RuntimeError("No Gmail account is configured.")
    pool = pool or ServicePool(queue)
    results, errors = [], []
    for account in accounts:
        email = account["email"]
        try:
            results.append(scan_gmail(queue, pool.get(email), email, account["final_dir"], mode))
        except Exception as exc:
            pool.invalidate(email)
            errors.append({"account": email, "error": str(exc)})
            log(f"Gmail scan failed [{email}]: {exc}", "scan")
    if errors:
        queue.set_metadata(LAST_ERROR_KEY, " / ".join(f"{e['account']}: {e['error']}" for e in errors)[:4000])
        write_heartbeat(HEARTBEAT_FILE, "error", f"gmail scan failed: {errors[0]['account']}", log_path=LOG_FILE)
    else:
        queue.delete_metadata(LAST_ERROR_KEY)
        write_heartbeat(HEARTBEAT_FILE, "ok", "gmail scan complete", log_path=LOG_FILE)
    return {"accounts": results, "errors": errors}


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_matches(path, expected_hash):
    return bool(path and expected_hash and os.path.isfile(path) and _sha256_file(path) == expected_hash)


def _unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    counter = 1
    while os.path.exists(f"{base}({counter}){ext}"):
        counter += 1
    return f"{base}({counter}){ext}"


def _write_bytes_atomic(data, target_path, tag):
    target_dir = os.path.dirname(target_path)
    os.makedirs(target_dir, exist_ok=True)
    # Fixed-length temp name: never scales with the (already budgeted) final
    # filename, so it cannot push a legal final path over MAX_PATH.
    temp = os.path.join(target_dir, f"{TEMP_PREFIX}{tag}{TEMP_SUFFIX}")
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temp, target_path)
    finally:
        try:
            os.remove(temp)
        except OSError:
            pass


def cleanup_orphan_temp_files(final_dirs):
    """Remove *.tmp left by a killed monitor. Returns how many were removed.

    SingleInstance guarantees one monitor, and this runs before any write, so
    deleting every matching temp at startup is safe. Only entries matching
    TEMP_PREFIX + TEMP_SUFFIX are touched; every per-file OSError is ignored
    so one locked/missing file cannot abort the sweep.
    """
    removed = 0
    seen = set()
    for final_dir in final_dirs or []:
        if not final_dir:
            continue
        final_dir = os.path.abspath(str(final_dir))
        if final_dir in seen:
            continue
        seen.add(final_dir)
        try:
            entries = os.listdir(final_dir)
        except OSError:
            continue
        for name in entries:
            if not (name.startswith(TEMP_PREFIX) and name.endswith(TEMP_SUFFIX)):
                continue
            try:
                os.remove(os.path.join(final_dir, name))
                removed += 1
            except OSError:
                pass
    return removed


def _attachment_marker_path(job_id):
    return os.path.join(STATE_DIR, f"attachmentjob_{job_id}.json")


def _find_part_by_id(payload, part_id):
    # Gmail gives the top-level payload partId "" (single-part messages).
    if payload and str(payload.get("partId", "")) == part_id:
        return payload

    def visit(parts):
        for part in parts or []:
            if str(part.get("partId", "")) == part_id:
                return part
            found = visit(part.get("parts", []))
            if found is not None:
                return found
        return None

    return visit((payload or {}).get("parts", []))


def _attachment_bytes(service, payload):
    """Attachment bytes never live in SQLite; always re-fetch them from Gmail."""
    attachment_id = payload.get("attachment_id", "")
    if attachment_id:
        attachment = service.users().messages().attachments().get(
            userId="me",
            messageId=payload["message_id"],
            id=attachment_id,
        ).execute(num_retries=3)
        return base64.urlsafe_b64decode(attachment["data"])

    # "" is a valid part_id: the top-level payload of a single-part message.
    part_id = payload.get("part_id")
    if part_id is None:
        raise RuntimeError("Gmail attachment job has neither attachment_id nor part_id.")
    message = service.users().messages().get(
        userId="me", id=payload["message_id"], format="full"
    ).execute(num_retries=3)
    part = _find_part_by_id(message.get("payload", {}), part_id)
    if part is None:
        raise RuntimeError(f"Attachment part {part_id} is no longer present in message {payload['message_id']}.")
    inline_data = (part.get("body", {}) or {}).get("data", "")
    if not inline_data:
        raise RuntimeError(f"Attachment part {part_id} in message {payload['message_id']} has no inline data.")
    return base64.urlsafe_b64decode(inline_data)


def _fresh_target_path(payload, final_dir):
    filename = render_filename(
        payload["filename"],
        payload.get("received_date", ""),
        payload.get("sender_email", ""),
        payload.get("mail_subject", ""),
        max_filename_length=filename_budget(final_dir),
    )
    return _unique_path(os.path.join(final_dir, filename))


def run_attachment_job(job_id, service, payload):
    file_data = _attachment_bytes(service, payload)
    if len(file_data) > MAX_ATTACHMENT_SIZE:
        raise RuntimeError("添付ファイルが100MB上限を超えています。")
    file_hash = hashlib.sha256(file_data).hexdigest()
    final_dir = os.path.abspath(payload["final_dir"])
    os.makedirs(final_dir, exist_ok=True)
    marker_path = _attachment_marker_path(job_id)
    marker = _load_json(marker_path)

    if marker and marker.get("result"):
        result = marker["result"]
        if result.get("hash") != file_hash:
            raise RuntimeError("同じGmail添付jobの再試行中に添付内容が変化しました。")
        target_path = os.path.abspath(result.get("target_path", ""))
        if not target_path:
            raise RuntimeError("添付ファイルcommit journalが不正です。")
        prepared = marker.get("phase") == "prepared" and marker.get("success") is False
    else:
        target_path = _fresh_target_path(payload, final_dir)
        result = {
            "hash": file_hash,
            "filename": payload["filename"],
            "saved_filename": os.path.basename(target_path),
            "target_path": target_path,
            "account_email": payload.get("account_email", ""),
            "final_dir": final_dir,
        }
        atomic_write_json(marker_path, {"success": False, "phase": "prepared", "result": result})
        prepared = True

    if _file_matches(target_path, file_hash):
        atomic_write_json(marker_path, {"success": True, "phase": "committed", "result": result})
        return result
    if os.path.exists(target_path):
        # A save interrupted after "prepared" never wrote the pinned name, so
        # another job may have taken it since. Only when _file_matches really
        # hashed that file (a read error propagates and the job retries) move
        # to a fresh name from the rendered base -- not name(1)(1) -- and
        # persist it before writing. Committed or legacy journals still stop.
        if not (prepared and os.path.isfile(target_path)):
            raise RuntimeError(f"保存予定先に別内容のファイルが存在します: {target_path}")
        target_path = _fresh_target_path(payload, final_dir)
        result = dict(result, saved_filename=os.path.basename(target_path), target_path=target_path)
        atomic_write_json(marker_path, {"success": False, "phase": "prepared", "result": result})

    _write_bytes_atomic(file_data, target_path, job_id)
    if not _file_matches(target_path, file_hash):
        raise RuntimeError("添付ファイル保存後のSHA-256検証に失敗しました。")
    atomic_write_json(marker_path, {"success": True, "phase": "committed", "result": result})
    return result


def reconcile_completed_jobs(queue):
    """Reconcile filesystem side effects without resurrecting failed/ignored jobs."""
    os.makedirs(STATE_DIR, exist_ok=True)
    reconciled = 0
    for name in os.listdir(STATE_DIR):
        if not (name.startswith("attachmentjob_") and name.endswith(".json")):
            continue
        id_text = name[len("attachmentjob_"):-len(".json")]
        if not id_text.isdigit():
            continue
        path = os.path.join(STATE_DIR, name)
        job_id = int(id_text)
        job = queue.get_job(job_id)
        if job is None or job["status"] == "success":
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        if job["status"] == "failed" or job["ignored"]:
            continue
        data = _load_json(path) or {}
        result = data.get("result") or {}
        if _file_matches(result.get("target_path", ""), result.get("hash", "")):
            result.setdefault("account_email", job.get("payload", {}).get("account_email", ""))
            if queue.mark_success(job_id, result):
                reconciled += 1
                try:
                    os.remove(path)
                except OSError:
                    pass
    return reconciled


def _is_auth_error(exc):
    if isinstance(exc, (AuthenticationRequiredError, RefreshError)):
        return True
    if isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
        return status in {401, 403}
    return False


def _write_failure_notice(job, error):
    payload = job.get("payload") or {}
    final_dir = payload.get("final_dir")
    if not final_dir:
        return
    try:
        os.makedirs(final_dir, exist_ok=True)
        path = os.path.join(final_dir, f"!取得エラー_{datetime.now():%Y%m%d}.txt")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(
                f"[{datetime.now():%Y-%m-%d %H:%M:%S}] job={job['id']} "
                f"account={payload.get('account_email', '')} "
                f"subject={payload.get('mail_subject', '')} "
                f"file={payload.get('filename', '')}\n{str(error)[:1000]}\n\n"
            )
    except OSError:
        pass


def process_one_job(queue, pool):
    """The monitor's worker path. Besides the job itself it keeps the tray's
    authentication issues current (runtime_state.AUTH_ISSUES_KEY); the trial
    calls run_claimed_job directly and leaves them alone."""
    job = queue.claim_next()
    if job is None:
        return False
    outcome, detail = run_claimed_job(queue, pool, job)
    account_email = _account_id((job.get("payload") or {}).get("account_email"))
    if outcome == "deferred":
        set_auth_issue(queue, account_email, detail)
    elif outcome == "success":
        clear_auth_issue(queue, account_email)
    return True


def run_claimed_job(queue, pool, job):
    """Save one claimed job and record the outcome through the normal
    success/failure path. Returns (outcome, detail): ("success", result),
    ("deferred", error) for authentication, ("failed", error) at once for an
    account that is no longer configured, or (mark_failure's disposition,
    error)."""
    payload = job.get("payload") or {}
    account_email = _account_id(payload.get("account_email"))
    try:
        # Inside the try: whatever a heartbeat write raises must still release
        # the claimed job through mark_failure instead of leaving it processing.
        write_heartbeat(HEARTBEAT_FILE, "ok", f"attachment job {job['id']}: {account_email}", log_path=LOG_FILE)
        service = pool.get(account_email)
        result = run_attachment_job(job["id"], service, payload)
        if not queue.mark_success(job["id"], result):
            raise RuntimeError("job state changed before success could be recorded")
        try:
            os.remove(_attachment_marker_path(job["id"]))
        except OSError:
            pass
        log(f"Attachment job {job['id']} succeeded [{account_email}]", "worker")
        return "success", result
    except Exception as exc:
        if isinstance(exc, AccountNotConfiguredError):
            # Deferring would retry every 15 minutes forever; fail it visibly
            # instead (the failed list can retry it once the account is back).
            # No notice file: the account's old folder may be gone for good.
            queue.fail_job(job["id"], exc)
            log(f"Attachment job {job['id']} failed: account not configured [{account_email}]", "worker")
            return "failed", exc
        if _is_auth_error(exc):
            pool.invalidate(account_email)
            queue.defer_job(job["id"], exc, AUTH_DEFER_SECONDS)
            log(f"Attachment job {job['id']} deferred for authentication [{account_email}]: {exc}", "worker")
            return "deferred", exc
        disposition = queue.mark_failure(job["id"], exc)
        log(f"Attachment job {job['id']} {disposition} [{account_email}]: {exc}", "worker")
        if disposition == "failed":
            _write_failure_notice(job, exc)
        return disposition, exc


def process_jobs(queue, pool=None, limit=MAX_JOBS_PER_ONCE):
    pool = pool or ServicePool(queue)
    processed = 0
    while processed < limit and process_one_job(queue, pool):
        processed += 1
    return processed


def worker_loop(queue, stop_event):
    """Everything, including ServicePool construction, is inside the try.

    Without that, a construction-time exception (e.g. a misconfigured
    account) would silently kill this thread while the scanner kept writing
    heartbeats and pending jobs piled up with every indicator still green.
    WORKER_HEARTBEAT_KEY is refreshed at most once per
    WORKER_HEARTBEAT_INTERVAL seconds, far inside the 300s staleness budget
    the tray checks against; WORKER_ERROR_KEY is cleared on a normal start
    and set only when this function is about to exit.
    """
    try:
        pool = ServicePool(queue)
        queue.delete_metadata(WORKER_ERROR_KEY)
        last_beat = 0.0
        while not stop_event.is_set():
            now = time.time()
            # Idle iterations are 2s apart. Writing the heartbeat on every one
            # would be ~43k pointless SQLite writes a day and needless lock
            # contention with the scanner thread's enqueue; 15s is far inside
            # the 300s staleness budget the tray checks against.
            if now - last_beat >= WORKER_HEARTBEAT_INTERVAL:
                queue.set_metadata(WORKER_HEARTBEAT_KEY, now)
                last_beat = now
            try:
                worked = process_one_job(queue, pool)
            except Exception as exc:
                log(f"Worker loop failed: {exc}", "worker")
                worked = False
            if not worked:
                stop_event.wait(2)
    except Exception as exc:
        queue.set_metadata(WORKER_ERROR_KEY, str(exc)[:4000])
        log(f"Worker thread stopped unexpectedly: {exc}", "worker")


def maybe_apply_retention(queue, force=False):
    now = time.time()
    try:
        last = float(queue.get_metadata(RETENTION_KEY, "0") or 0)
    except (TypeError, ValueError):
        last = 0
    if not force and now - last < 86400:
        return {"compacted": 0, "deleted": 0}
    result = queue.apply_retention(now=now, full_days=90, dedupe_days=730)
    queue.set_metadata(RETENTION_KEY, now)
    if result["compacted"] or result["deleted"]:
        log(f"Job retention: compacted={result['compacted']}, deleted={result['deleted']}", "retention")
    return result


def run_once(queue=None):
    queue = queue or JobQueue(QUEUE_DB)
    scan = scan_all_accounts(queue, ServicePool(queue))
    jobs = process_jobs(queue, ServicePool(queue))
    maybe_apply_retention(queue)
    return {"scan": scan, "processed_jobs": jobs, "queue": queue.counts()}


def test_recent_emails(max_results):
    settings = load_settings()
    mode = normalize_auth_mode(settings.get("auth_mode"))
    pool = ServicePool(JobQueue(QUEUE_DB))
    excluded_labels = load_excluded_labels(settings)
    results = []
    for account in get_account_configs(settings):
        start = datetime.now() - timedelta(days=max(1, int(settings.get("lookback_days", "7"))))
        response = pool.get(account["email"]).users().messages().list(
            userId="me",
            q=build_mail_query(start, account["email"], mode, excluded_labels=excluded_labels),
            maxResults=max_results,
        ).execute(num_retries=3)
        results.append({"account": account["email"], "messages": len(response.get("messages", []))})
    print(json.dumps(results, ensure_ascii=False, indent=2))


# --- Trial download (--trial N) ----------------------------------------------
# Saves only the newest N attachment mails of each account so a first-time
# user can check the save folder and file names before automatic fetching
# starts. It never calls scan_gmail/scan_all_accounts, which advance the mail
# cursors and write LAST_ERROR_KEY: the trial writes no cursor, pause,
# LAST_ERROR or WORKER_* metadata, and a job failure is recorded only on the
# job by the normal job path. It reuses iter_message_ids and run_claimed_job
# as they are, so heartbeat.json and mail_log.txt are written as usual. Like
# the monitor, its ServicePool records a mailbox identity on first use for an
# install from before identities were recorded.

TRIAL_TRAY_RUNNING_TEXT = (
    "自動取得（トレイアプリまたは監視プロセス）が動作中です。トレイアプリを終了してから実行してください。\n"
    "トレイメニューの「終了（自動取得を停止）」で終了できます。"
)
TRIAL_REDOWNLOAD_NOTE = (
    "※ 保存済みの添付は、もう一度実行しても、保存先やファイル名の設定を変えても保存し直しません"
    "（自動取得でも同じです）。トレイメニューの「添付を取り直す」は、記録した保存先へ"
    "現在のファイル名の設定で保存し直します。"
)


def _lock_is_free(name, lock_path):
    """True when no other process holds this single-instance lock. An OSError
    from CreateMutexW (e.g. the owner runs elevated) counts as held."""
    probe = SingleInstance(name, lock_path)
    try:
        if not probe.acquire():
            return False
    except OSError:
        return False
    probe.release()
    return True


def select_trial_messages(queue, service, account, count, excluded_labels, allowed_extensions, report,
                          scan_cap=TRIAL_SCAN_CAP):
    """Fill report["messages"] with the newest `count` messages that have at
    least one allowed attachment job (new or already queued), scanning at most
    scan_cap messages. Messages without one go to report["skipped"]; a message
    deleted between list and get (404) is only counted in report["gone"]."""
    email = account["email"]
    for message_id in iter_message_ids(service, build_trial_query(excluded_labels), email):
        report["scanned"] += 1
        try:
            detail = enqueue_message(queue, service, message_id, email, account["final_dir"], allowed_extensions)
        except HttpError as exc:
            if getattr(getattr(exc, "resp", None), "status", None) != 404:
                raise
            report["gone"] += 1
            detail = None
        if detail is not None:
            entry = {
                "subject": detail["subject"],
                "received_date": detail["received_date"],
                "job_keys": detail["job_keys"],
                "skipped": detail["skipped"],
                "jobs": [],
            }
            if detail["job_keys"]:
                report["messages"].append(entry)
            else:
                report["skipped"].append(entry)
        if len(report["messages"]) >= count:
            break
        if report["scanned"] >= scan_cap:
            report["capped"] = True
            break


def _describe_trial_job(job):
    if job is None:
        return {"state": "missing", "filename": ""}
    info = {"filename": (job.get("payload") or {}).get("filename", ""), "error": job.get("last_error") or ""}
    if job["status"] == "success":
        path = (job.get("result") or {}).get("target_path", "")
        info.update(state="already", path=path, file_missing=bool(path) and not os.path.isfile(path))
    elif job["ignored"]:
        info["state"] = "ignored"
    elif job["status"] == "failed":
        info["state"] = "failed_before"
    elif job["status"] == "processing":
        info["state"] = "processing"
    else:
        info.update(state="waiting", next_attempt_at=job["next_attempt_at"])
    return info


def run_trial_job(queue, pool, job_key):
    """Process one of the trial's own jobs, never another queued job. A saved
    job is never saved again; failed, ignored and not-yet-due jobs are only
    reported."""
    job = queue.get_job_by_key(job_key)
    if job is not None and job["status"] == "pending" and not job["ignored"]:
        claimed = queue.claim_job(job["id"])
        if claimed is not None:
            outcome, detail = run_claimed_job(queue, pool, claimed)
            info = {"filename": (claimed.get("payload") or {}).get("filename", "")}
            if outcome == "success":
                path = detail.get("target_path", "")
                info.update(state="saved", path=path, file_missing=not os.path.isfile(path))
            else:
                info.update(state=outcome, error=str(detail))
            return info
        job = queue.get_job_by_key(job_key)
    return _describe_trial_job(job)


def run_trial(queue, count=TRIAL_DEFAULT_MESSAGES, pool=None, scan_cap=TRIAL_SCAN_CAP):
    settings = load_settings()
    mode = normalize_auth_mode(settings.get("auth_mode"))
    pool = pool or ServicePool(queue)
    excluded_labels = load_excluded_labels(settings)
    allowed_extensions = load_allowed_extensions(settings)
    reports = []
    for account in get_account_configs(settings):
        report = {
            "account": account["email"],
            "final_dir": account["final_dir"],
            "mode": mode,
            "messages": [],
            "skipped": [],
            "scanned": 0,
            "gone": 0,
            "capped": False,
            "error": "",
            "auth_error": False,
        }
        try:
            service = pool.get(account["email"])
            select_trial_messages(
                queue, service, account, count, excluded_labels, allowed_extensions, report, scan_cap
            )
        except Exception as exc:
            # One failing account (e.g. a DWD mailbox without delegation) is
            # reported; the other accounts still run.
            pool.invalidate(account["email"])
            report["error"] = str(exc) or type(exc).__name__
            report["auth_error"] = _is_auth_error(exc)
            log(f"Trial failed [{account['email']}]: {exc}", "trial")
        # Messages selected before an error are still processed.
        for message in report["messages"]:
            message["jobs"] = [run_trial_job(queue, pool, key) for key in message["job_keys"]]
        reports.append(report)
    return reports


def trial_exit_code(reports):
    if reports and all(report["error"] and report["auth_error"] for report in reports):
        return TRIAL_EXIT_BLOCKED
    for report in reports:
        if report["error"]:
            return TRIAL_EXIT_FAILED
        for message in report["messages"]:
            if any(job["state"] not in ("saved", "already") for job in message["jobs"]):
                return TRIAL_EXIT_FAILED
    return TRIAL_EXIT_OK


def _trial_date(received_date):
    text = str(received_date or "")
    return f"{text[:4]}/{text[4:6]}/{text[6:]}" if len(text) == 8 and text.isdigit() else text


def _trial_skip_reason(item):
    reason = item.get("reason", "")
    extension = os.path.splitext(item.get("filename") or "")[1].lower()
    if reason == f"extension {extension} is not allowed":
        return f"拡張子 {extension or '(なし)'} は保存対象外"
    if reason == "attachment exceeds the 100MB limit":
        return "100MBの上限を超えています"
    return reason


def _trial_job_text(job):
    name = job.get("filename") or "添付"
    error = job.get("error", "")
    state = job["state"]
    if state in ("saved", "already") and job.get("file_missing"):
        return f"保存済みと記録されていますが、ファイルが見つかりません: {job['path']}"
    if state == "saved":
        return f"保存しました: {job['path']}"
    if state == "already":
        if job.get("path"):
            return f"保存済みです（前回までに保存）: {job['path']}"
        return f"保存済みです（{name}。古い記録のため保存先の記録はありません）"
    if state == "failed_before":
        return f"以前の取得で失敗しています（もう一度は取得しません）: {name} - {error}"
    if state == "ignored":
        return f"「無視」に設定された添付です（取得しません）: {name}"
    if state == "waiting":
        when = datetime.fromtimestamp(job.get("next_attempt_at") or 0).strftime("%Y/%m/%d %H:%M")
        return f"再試行待ちです（{when}以降）: {name} - {error}"
    if state == "processing":
        return f"処理中のままです: {name}"
    if state == "retry":
        return f"保存できませんでした（自動取得の開始後に再試行します）: {name} - {error}"
    if state == "deferred":
        return f"認証エラーのため保存を保留しました: {name} - {error}"
    if state == "missing":
        return f"処理キューに見つかりません: {name}"
    return f"保存できませんでした: {name} - {error}"


def _trial_auth_hint(mode):
    # _is_auth_error also covers HTTP 403 such as a Gmail API that is not enabled.
    if mode == AUTH_DWD:
        return (
            "Gmailの認証またはAPIアクセスに失敗しました。Google Cloud プロジェクトで Gmail API が有効か、"
            "Google管理コンソールでドメイン全体の委任（gmail.readonly）が承認されているかを確認し、"
            "設定画面の「登録した全アカウントに接続してテスト」で確かめてから「保存」してください。"
        )
    return (
        "Gmailの認証またはAPIアクセスに失敗しました。Google Cloud プロジェクトで Gmail API が有効かを確認し、"
        "設定画面の「Googleに接続してテスト」で認証を済ませて「保存」してから、もう一度実行してください。"
    )


def _is_under(path, folder):
    path = os.path.normcase(os.path.abspath(path))
    folder = os.path.normcase(os.path.abspath(folder))
    try:
        return os.path.commonpath([path, folder]) == folder
    except ValueError:  # different drives
        return False


def format_trial_report(reports, count, exit_code):
    lines = []
    saved = already = problems = 0
    file_missing = False
    for report in reports:
        lines.append("")
        lines.append(f"[{report['account']}]  保存先: {report['final_dir']}")
        elsewhere = False
        for index, message in enumerate(report["messages"], 1):
            lines.append(f"  {index}. {_trial_date(message['received_date'])} 件名「{message['subject']}」")
            for job in message["jobs"]:
                lines.append("     " + _trial_job_text(job))
                file_missing = file_missing or bool(job.get("file_missing"))
                if job.get("path") and not _is_under(job["path"], report["final_dir"]):
                    elsewhere = True
                if job["state"] == "saved":
                    saved += 1
                elif job["state"] == "already":
                    already += 1
                else:
                    problems += 1
            for item in message["skipped"]:
                lines.append(f"     対象外: {item['filename']}（{_trial_skip_reason(item)}）")
        if elsewhere:
            lines.append(
                "  ※ 現在の保存先とは別の場所に記録された添付があります。"
                "保存先の設定を変えても、保存済みの添付は移動も再保存もされません。"
            )
        if report["skipped"]:
            lines.append(f"  保存対象の添付が無いため数えなかったメール: {len(report['skipped'])}件")
            for message in report["skipped"][:5]:
                reasons = "、".join(
                    f"{item['filename']}（{_trial_skip_reason(item)}）" for item in message["skipped"]
                ) or "本文に埋め込まれた画像のみ"
                lines.append(f"     {_trial_date(message['received_date'])} 件名「{message['subject']}」: {reasons}")
            if len(report["skipped"]) > 5:
                lines.append(f"     ほか{len(report['skipped']) - 5}件")
        if report["gone"]:
            lines.append(f"  確認中に削除されたメール {report['gone']}件を飛ばしました。")
        found = len(report["messages"])
        if report["error"]:
            lines.append(f"  エラー: {report['error']}")
            if report["auth_error"]:
                lines.append("  " + _trial_auth_hint(report["mode"]))
        elif found == 0:
            lines.append(
                f"  保存対象の添付があるメールは見つかりませんでした（新しい順に{report['scanned']}件を確認）。"
            )
        elif found < count and report["capped"]:
            lines.append(
                f"  新しい順に{report['scanned']}件まで確認しましたが、"
                f"保存対象の添付があるメールは{found}件でした。"
            )
        elif found < count:
            lines.append(f"  保存対象の添付があるメールは{found}件だけでした。")
    lines.append("")
    lines.append(f"結果: 保存 {saved}件 / 保存済み {already}件 / 保存できなかった添付 {problems}件")
    if file_missing:
        lines.append(
            "※ 見つからないファイルは、自動取得の開始後にトレイメニューの「添付を取り直す」で、"
            "記録した保存先へ保存し直せます。"
        )
    lines.append(TRIAL_REDOWNLOAD_NOTE)
    if exit_code == TRIAL_EXIT_OK:
        lines.append(
            "保存先のファイル名と内容を確認してください。問題なければ register_logon_task.bat を実行して"
            "自動取得を開始してください。"
        )
    else:
        lines.append("お試し取得を完了できませんでした。上の内容を確認してください。")
    return lines


def run_trial_cli(count=TRIAL_DEFAULT_MESSAGES):
    """Exit codes: TRIAL_EXIT_OK when every selected job is saved or already
    saved; TRIAL_EXIT_BLOCKED when the tray/monitor is running, the app is not
    configured, or authentication/API access failed for every account;
    TRIAL_EXIT_FAILED otherwise."""
    instance = SingleInstance(MONITOR_MUTEX_NAME, LOCK_FILE)
    try:
        acquired = instance.acquire()
    except OSError:
        acquired = False
    if not acquired:
        print(TRIAL_TRAY_RUNNING_TEXT)
        return TRIAL_EXIT_BLOCKED
    try:
        if not _lock_is_free(TRAY_MUTEX_NAME, TRAY_LOCK_FILE):
            print(TRIAL_TRAY_RUNNING_TEXT)
            return TRIAL_EXIT_BLOCKED
        if not is_configured():
            print("設定が完了していません。setup.bat を実行して設定を保存してから、もう一度実行してください。")
            return TRIAL_EXIT_BLOCKED
        queue = JobQueue(QUEUE_DB)
        # An interrupted trial (Ctrl+C, closed window) is recovered like a monitor
        # restart, including its temp files; safe while the monitor lock is held.
        recover_interrupted_jobs(queue)
        removed = cleanup_orphan_temp_files(
            [account["final_dir"] for account in get_account_configs(load_settings())]
        )
        if removed:
            log(f"Removed {removed} orphan temp file(s) left by a previous run", "trial")
        print(
            f"お試し取得: 各アカウントの受信トレイから、保存対象の添付があるメールを新しい順に最大{count}件保存します。\n"
            "設定画面の取込期間は使わず、自動取得の確認位置も変更しません。しばらくお待ちください。",
            flush=True,
        )
        log(f"Trial started: newest {count} attachment message(s) per account", "trial")
        reports = run_trial(queue, count)
        exit_code = trial_exit_code(reports)
        for line in format_trial_report(reports, count, exit_code):
            print(line)
        log(f"Trial finished: exit={exit_code}", "trial")
        return exit_code
    except KeyboardInterrupt:
        print(
            "\n中断しました。途中だった添付は、もう一度実行したときにそのメールがまだ新しい順の対象に入っていれば、"
            "続きから保存します。入っていなければ、自動取得の開始後に保存されます。"
        )
        log("Trial interrupted by user", "trial")
        return TRIAL_EXIT_FAILED
    finally:
        instance.release()


def recover_interrupted_jobs(queue):
    """Run at startup while holding the monitor lock (monitor and trial)."""
    reconciled = reconcile_completed_jobs(queue)
    if reconciled:
        log(f"Reconciled {reconciled} completed attachment job(s) after interruption", "queue")
    recovered = queue.recover_processing_jobs()
    if recovered:
        log(f"Recovered {recovered} interrupted attachment job(s)", "queue")


def _trial_count(text):
    try:
        value = int(text)
    except ValueError:
        value = 0
    if not 1 <= value <= TRIAL_MAX_MESSAGES:
        raise argparse.ArgumentTypeError(f"1～{TRIAL_MAX_MESSAGES}の数を指定してください: {text}")
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--test-recent", type=int, default=0)
    parser.add_argument(
        "--trial", nargs="?", const=TRIAL_DEFAULT_MESSAGES, type=_trial_count, metavar="N",
        help="各アカウントの最新N件（既定3）の添付付きメールだけを保存するお試し取得",
    )
    args = parser.parse_args()
    if args.trial is not None:
        # Redirected output must never crash the run after files are saved.
        sys.stdout.reconfigure(errors="replace")
        return run_trial_cli(args.trial)
    if args.test_recent:
        test_recent_emails(args.test_recent)
        return 0

    instance = SingleInstance(MONITOR_MUTEX_NAME, LOCK_FILE)
    if not instance.acquire():
        log("Another gmail_monitor.py instance is already running; exiting", "main")
        return 0

    stop_event = threading.Event()
    worker = None
    try:
        queue = JobQueue(QUEUE_DB)
        recover_interrupted_jobs(queue)
        maybe_apply_retention(queue, force=True)

        settings = load_settings()

        # A killed monitor (taskkill /F /T from stop_all(), which runs on
        # every 今すぐ確認 / 一時停止 / 設定保存 / 終了, not just crashes)
        # can leave a temp file behind in a customer's case folder. Sweep
        # every configured final_dir before any write happens this run.
        removed = cleanup_orphan_temp_files(
            [account["final_dir"] for account in get_account_configs(settings)]
        )
        if removed:
            log(f"Removed {removed} orphan temp file(s) left by a previous run", "main")

        if args.once:
            print(json.dumps(run_once(queue), ensure_ascii=False, indent=2))
            return 0

        try:
            polling_interval = max(15, int(settings.get("polling_interval", "60")))
        except ValueError:
            polling_interval = 60
        log(
            f"=== Gmail Monitor Started (mode={normalize_auth_mode(settings.get('auth_mode'))}, "
            f"accounts={len(get_account_configs(settings))}, attachments-only) ===",
            "main",
        )
        worker = threading.Thread(
            target=worker_loop,
            args=(queue, stop_event),
            daemon=True,
            name="attachment-worker",
        )
        worker.start()
        scan_pool = ServicePool(queue)
        while not stop_event.is_set():
            try:
                scan_all_accounts(queue, scan_pool)
            except Exception as exc:
                queue.set_metadata(LAST_ERROR_KEY, str(exc)[:4000])
                log(f"Gmail scan cycle failed: {exc}", "scan")
                write_heartbeat(HEARTBEAT_FILE, "error", f"gmail scan cycle failed: {exc}", log_path=LOG_FILE)
            maybe_apply_retention(queue)
            stop_event.wait(polling_interval)
    except KeyboardInterrupt:
        log("Monitor stopped by user", "main")
        return 0
    finally:
        stop_event.set()
        if worker is not None:
            worker.join(timeout=5)
        instance.release()


if __name__ == "__main__":
    raise SystemExit(main())
