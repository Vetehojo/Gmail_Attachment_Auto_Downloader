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
                + " open Settings and run Google authentication to create/refresh token.json."
                + detail
            )
        if not os.path.exists(CREDENTIALS_FILE):
            raise AuthenticationRequiredError(f"credentials.json not found: {CREDENTIALS_FILE}")
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
        creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
        _save_token(creds)

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_dwd_service(account_email):
    account_email = str(account_email or "").strip().lower()
    if not account_email:
        raise ValueError("DWD requires an account email to impersonate.")
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise AuthenticationRequiredError(f"service_account.json not found: {SERVICE_ACCOUNT_FILE}")
    try:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE,
            scopes=SCOPES,
        ).with_subject(account_email)
    except Exception as exc:
        raise AuthenticationRequiredError(f"DWD credentials could not be loaded: {exc}") from exc
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


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
