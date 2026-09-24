import hashlib
import os
import time

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from app_settings import (
    AUTH_DWD,
    AUTH_OAUTH,
    BASE_DIR,
    CREDENTIALS_PATH,
    SERVICE_ACCOUNT_PATH,
    TOKEN_PATH,
    ensure_private_dir,
    get_account_configs,
    load_settings,
    normalize_auth_mode,
    protect_private_file,
)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = CREDENTIALS_PATH
SERVICE_ACCOUNT_FILE = SERVICE_ACCOUNT_PATH
TOKEN_FILE = TOKEN_PATH
LEGACY_TOKEN_FILE = os.path.join(BASE_DIR, "token.pickle")


class AuthenticationRequiredError(RuntimeError):
    """Configuration/user action is required before Gmail access can resume."""


class AccountNotConfiguredError(AuthenticationRequiredError):
    """The requested mailbox is not in the app's configured account list."""


def _backup_bad_token():
    if not os.path.exists(TOKEN_FILE):
        return
    backup = f"{TOKEN_FILE}.invalid.{int(time.time())}"
    try:
        os.replace(TOKEN_FILE, backup)
    except OSError:
        pass


def _save_token(creds):
    ensure_private_dir()
    temp = TOKEN_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(creds.to_json())
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    os.replace(temp, TOKEN_FILE)
    protect_private_file(TOKEN_FILE)


def get_oauth_service(allow_interactive=True):
    creds = None
    refresh_error = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except Exception:
            _backup_bad_token()
            creds = None

    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
        except Exception as exc:
            refresh_error = exc
            creds = None

    if not creds or not creds.valid:
        if not allow_interactive:
            legacy_note = " token.pickle exists but is intentionally not loaded;" if os.path.exists(LEGACY_TOKEN_FILE) else ""
            detail = f" ({refresh_error})" if refresh_error else ""
            raise AuthenticationRequiredError(
                "Gmail authentication is required;" + legacy_note
                + " open Settings, run the connection test and save to create/refresh token.json."
                + detail
            )
        if not os.path.exists(CREDENTIALS_FILE):
            raise AuthenticationRequiredError(f"credentials.json not found: {CREDENTIALS_FILE}")
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
        creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
        _save_token(creds)

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def install_oauth_token(creds):
    """Write credentials from the settings dialog's login as token.json (on Save only)."""
    _save_token(creds)


def build_dwd_service(service_account_path, account_email):
    """DWD service from an explicit service-account JSON; no allowlist check."""
    account_email = str(account_email or "").strip().lower()
    if not account_email:
        raise ValueError("DWD requires an account email to impersonate.")
    if not os.path.exists(service_account_path):
        raise AuthenticationRequiredError(f"service_account.json not found: {service_account_path}")
    try:
        creds = service_account.Credentials.from_service_account_file(
            service_account_path,
            scopes=SCOPES,
        ).with_subject(account_email)
    except Exception as exc:
        raise AuthenticationRequiredError(f"DWD credentials could not be loaded: {exc}") from exc
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_dwd_service(account_email):
    return build_dwd_service(SERVICE_ACCOUNT_FILE, account_email)


def _assert_configured_dwd_account(account_email):
    account_email = str(account_email or "").strip().lower()
    allowed = {item["email"].lower() for item in get_account_configs(load_settings())}
    if account_email not in allowed:
        raise AccountNotConfiguredError(f"DWD account is not configured in this app: {account_email}")
    return account_email


def get_gmail_service(account_email=None, allow_interactive=True, auth_mode=None):
    mode = normalize_auth_mode(auth_mode or load_settings().get("auth_mode"))
    if mode == AUTH_DWD:
        return get_dwd_service(_assert_configured_dwd_account(account_email))
    if mode != AUTH_OAUTH:
        raise ValueError(f"Unsupported auth mode: {mode}")
    return get_oauth_service(allow_interactive=allow_interactive)


# --- Settings dialog connection test ------------------------------------------
# These take the dialog's unsaved values explicitly and write nothing: no
# config.ini, no credential copy and no token.json. A browser login's
# credentials stay in memory and are installed by the dialog's Save.

def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_installed_oauth_client(client_path):
    """True when client_path is the installed client JSON: the same file or the same bytes."""
    if not client_path or not os.path.isfile(CREDENTIALS_FILE):
        return False
    if os.path.normcase(os.path.abspath(client_path)) == os.path.normcase(os.path.abspath(CREDENTIALS_FILE)):
        return True
    return _file_sha256(client_path) == _file_sha256(CREDENTIALS_FILE)


INSTALLED_CREDENTIALS_LABEL = "保存済みの認証情報（token.json）"
STAGED_CREDENTIALS_LABEL = "前回のテストでログインした認証情報"


def _refreshed_in_memory(creds, label):
    """(creds, "") when usable, refreshing only in memory; else (None, reason).
    A reason is one complete sentence about `label`."""
    try:
        if not creds.valid and creds.expired and creds.refresh_token:
            creds.refresh(Request())
    except Exception as exc:
        return None, f"{label}を更新できませんでした（{exc}）。"
    if not creds.valid:
        return None, f"{label}が無効です。"
    return creds, ""


def _installed_credentials_read_only():
    """token.json as installed, refreshed in memory only (never saved back)."""
    if not os.path.exists(TOKEN_FILE):
        return None, f"{INSTALLED_CREDENTIALS_LABEL}がありません。"
    try:
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    except Exception as exc:
        return None, f"{INSTALLED_CREDENTIALS_LABEL}を読み込めませんでした（{exc}）。"
    return _refreshed_in_memory(creds, INSTALLED_CREDENTIALS_LABEL)


def run_oauth_login(client_path):
    flow = InstalledAppFlow.from_client_secrets_file(client_path, SCOPES)
    return flow.run_local_server(port=0, access_type="offline", prompt="consent")


def oauth_connection_test(client_path, force_login=False, staged_creds=None):
    """OAuth test on the dialog's values. Writes nothing.

    Unless force_login, the dialog's earlier login (staged_creds) is reused,
    then the installed token.json read-only when client_path is the installed
    client. Otherwise a browser login runs and its credentials are kept in
    memory. Returns (profile, login_creds, note): login_creds are what Save
    installs as token.json (None when the installed token.json was used) and
    note is one sentence saying why a browser login replaced the stored or
    earlier credentials, if it did.
    """
    creds = login = None
    note = ""
    if not force_login and staged_creds is not None:
        creds, note = _refreshed_in_memory(staged_creds, STAGED_CREDENTIALS_LABEL)
        login = creds
    if creds is None and not force_login and is_installed_oauth_client(client_path):
        creds, note = _installed_credentials_read_only()
    if creds is None:
        creds = login = run_oauth_login(client_path)
    else:
        note = ""
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    profile = service.users().getProfile(userId="me").execute(num_retries=3)
    return profile, login, note


def dwd_connection_test(service_account_path, account_emails):
    """DWD test of the dialog's account list (not the saved allowlist) with the
    selected service-account JSON. Writes nothing. Returns [(email, profile, error)]."""
    results = []
    for email in account_emails:
        try:
            service = build_dwd_service(service_account_path, email)
            profile = service.users().getProfile(userId="me").execute(num_retries=3)
        except Exception as exc:
            results.append((email, None, exc))
            continue
        results.append((email, profile, None))
    return results


def test_gmail_service(account_email=None, allow_interactive=True, auth_mode=None):
    service = get_gmail_service(
        account_email=account_email,
        allow_interactive=allow_interactive,
        auth_mode=auth_mode,
    )
    profile = service.users().getProfile(userId="me").execute(num_retries=3)
    return service, profile


def verify_profile_account(profile, expected_email):
    """Return (ok, actual_email).

    ok is False only when both the profile's emailAddress and expected_email
    are present and, after trimming and case-folding, differ. If either side
    is missing, there is nothing to contradict, so ok is True.
    """
    actual_email = ""
    if isinstance(profile, dict):
        actual_email = str(profile.get("emailAddress") or "").strip()
    expected = str(expected_email or "").strip()
    if not actual_email or not expected:
        return True, actual_email
    return actual_email.lower() == expected.lower(), actual_email


if __name__ == "__main__":
    try:
        settings = load_settings()
        mode = normalize_auth_mode(settings.get("auth_mode"))
        if mode == AUTH_DWD:
            accounts = get_account_configs(settings)
            if not accounts:
                raise RuntimeError("No DWD account is configured.")
            account = accounts[0]["email"]
        else:
            account = settings.get("target_email", "")
        _service, profile = test_gmail_service(
            account_email=account,
            allow_interactive=(mode == AUTH_OAUTH),
            auth_mode=mode,
        )
        print("Gmail API authentication: OK")
        print(f"Mode: {mode}")
        print(f"Account: {profile.get('emailAddress', account or '-')}")
    except Exception as exc:
        print(f"Authentication failed: {exc}")
        raise SystemExit(1)
