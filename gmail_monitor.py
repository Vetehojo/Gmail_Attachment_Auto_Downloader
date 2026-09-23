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
import threading
import time
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from app_settings import (
    AUTH_DWD,
    get_account_configs,
    load_allowed_extensions,
    load_excluded_labels,
    load_settings,
    normalize_auth_mode,
)
from filename_rules import filename_budget, render_filename
from gmail_auth import AuthenticationRequiredError, get_gmail_service
from job_queue import JobQueue
from runtime_state import SingleInstance, append_log, atomic_write_json, write_heartbeat

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(_SCRIPT_DIR, "state")
QUEUE_DB = os.path.join(STATE_DIR, "jobs.sqlite3")
HEARTBEAT_FILE = os.path.join(STATE_DIR, "heartbeat.json")
LOCK_FILE = os.path.join(STATE_DIR, "monitor.lock")
LOG_FILE = os.path.join(_SCRIPT_DIR, "log", "mail_log.txt")
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
    query = f"after:{int(start_time.timestamp())} in:inbox"
    for label in excluded_labels or []:
        query += f' -label:"{label}"'
    return query


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
        write_heartbeat(HEARTBEAT_FILE, "ok", f"gmail pagination: {account_email}")


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

    visit((payload or {}).get("parts", []))
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
        if queue.enqueue(_attachment_job_key(account_email, message_id, attachment), job_payload):
            added += 1
    return added


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
    write_heartbeat(HEARTBEAT_FILE, "ok", f"gmail scan: {account_email}")
    for message_id in iter_message_ids(service, query, account_email):
        attachment_count += enqueue_message_jobs(
            queue, service, message_id, account_email, final_dir, allowed_extensions=allowed_extensions
        )
        message_count += 1
        if message_count % 10 == 0:
            write_heartbeat(HEARTBEAT_FILE, "ok", f"gmail scan {account_email}: {message_count} messages")

    # Advance only after pagination and all enqueue operations complete.
    queue.set_metadata(cursor_key(account_email, mode), scan_started.timestamp())
    log(
        f"Scan complete [{account_email}]: messages={message_count}, "
        f"new_attachment_jobs={attachment_count}, start={start_time:%Y/%m/%d %H:%M:%S}",
        "scan",
    )
    return {"account": account_email, "messages": message_count, "attachments": attachment_count}


class ServicePool:
    def __init__(self, auth_mode=None, allow_interactive=False):
        self.auth_mode = normalize_auth_mode(auth_mode or load_settings().get("auth_mode"))
        self.allow_interactive = allow_interactive
        self._services = {}

    def get(self, account_email):
        key = _account_id(account_email) if self.auth_mode == AUTH_DWD else "oauth"
        if key not in self._services:
            self._services[key] = get_gmail_service(
                account_email=account_email,
                allow_interactive=self.allow_interactive,
                auth_mode=self.auth_mode,
            )
        return self._services[key]

    def invalidate(self, account_email):
        key = _account_id(account_email) if self.auth_mode == AUTH_DWD else "oauth"
        self._services.pop(key, None)


def scan_all_accounts(queue, pool=None):
    settings = load_settings()
    mode = normalize_auth_mode(settings.get("auth_mode"))
    accounts = get_account_configs(settings)
    if not accounts:
        raise RuntimeError("No Gmail account is configured.")
    pool = pool or ServicePool(mode)
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
        write_heartbeat(HEARTBEAT_FILE, "error", f"gmail scan failed: {errors[0]['account']}")
    else:
        queue.delete_metadata(LAST_ERROR_KEY)
        write_heartbeat(HEARTBEAT_FILE, "ok", "gmail scan complete")
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

    part_id = payload.get("part_id", "")
    if not part_id:
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
    else:
        filename = render_filename(
            payload["filename"],
            payload.get("received_date", ""),
            payload.get("sender_email", ""),
            payload.get("mail_subject", ""),
            max_filename_length=filename_budget(final_dir),
        )
        target_path = _unique_path(os.path.join(final_dir, filename))
        result = {
            "hash": file_hash,
            "filename": payload["filename"],
            "saved_filename": os.path.basename(target_path),
            "target_path": target_path,
            "account_email": payload.get("account_email", ""),
            "final_dir": final_dir,
        }
        atomic_write_json(marker_path, {"success": False, "phase": "prepared", "result": result})

    if _file_matches(target_path, file_hash):
        atomic_write_json(marker_path, {"success": True, "phase": "committed", "result": result})
        return result
    if os.path.exists(target_path):
        raise RuntimeError(f"保存予定先に別内容のファイルが存在します: {target_path}")

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
    job = queue.claim_next()
    if job is None:
        return False
    payload = job.get("payload") or {}
    account_email = _account_id(payload.get("account_email"))
    write_heartbeat(HEARTBEAT_FILE, "ok", f"attachment job {job['id']}: {account_email}")
    try:
        service = pool.get(account_email)
        result = run_attachment_job(job["id"], service, payload)
        if not queue.mark_success(job["id"], result):
            raise RuntimeError("job state changed before success could be recorded")
        try:
            os.remove(_attachment_marker_path(job["id"]))
        except OSError:
            pass
        log(f"Attachment job {job['id']} succeeded [{account_email}]", "worker")
    except Exception as exc:
        if _is_auth_error(exc):
            pool.invalidate(account_email)
            queue.defer_job(job["id"], exc, AUTH_DEFER_SECONDS)
            log(f"Attachment job {job['id']} deferred for authentication [{account_email}]: {exc}", "worker")
        else:
            disposition = queue.mark_failure(job["id"], exc)
            log(f"Attachment job {job['id']} {disposition} [{account_email}]: {exc}", "worker")
            if disposition == "failed":
                _write_failure_notice(job, exc)
        return True
    return True


def process_jobs(queue, pool=None, limit=MAX_JOBS_PER_ONCE):
    pool = pool or ServicePool(load_settings().get("auth_mode"))
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
        pool = ServicePool(load_settings().get("auth_mode"))
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
    settings = load_settings()
    scan = scan_all_accounts(queue, ServicePool(settings.get("auth_mode")))
    jobs = process_jobs(queue, ServicePool(settings.get("auth_mode")))
    maybe_apply_retention(queue)
    return {"scan": scan, "processed_jobs": jobs, "queue": queue.counts()}


def test_recent_emails(max_results):
    settings = load_settings()
    mode = normalize_auth_mode(settings.get("auth_mode"))
    pool = ServicePool(mode)
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--test-recent", type=int, default=0)
    args = parser.parse_args()
    if args.test_recent:
        test_recent_emails(args.test_recent)
        return 0

    instance = SingleInstance("Local\\GmailAutoDownloaderMonitor", LOCK_FILE)
    if not instance.acquire():
        log("Another gmail_monitor.py instance is already running; exiting", "main")
        return 0

    stop_event = threading.Event()
    worker = None
    try:
        queue = JobQueue(QUEUE_DB)
        reconciled = reconcile_completed_jobs(queue)
        if reconciled:
            log(f"Reconciled {reconciled} completed attachment job(s) after interruption", "queue")
        recovered = queue.recover_processing_jobs()
        if recovered:
            log(f"Recovered {recovered} interrupted attachment job(s)", "queue")
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
        scan_pool = ServicePool(settings.get("auth_mode"))
        while not stop_event.is_set():
            try:
                scan_all_accounts(queue, scan_pool)
            except Exception as exc:
                queue.set_metadata(LAST_ERROR_KEY, str(exc)[:4000])
                log(f"Gmail scan cycle failed: {exc}", "scan")
                write_heartbeat(HEARTBEAT_FILE, "error", f"gmail scan cycle failed: {exc}")
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
