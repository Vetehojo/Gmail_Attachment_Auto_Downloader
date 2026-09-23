import configparser
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta

# Install folder = parent of app/. config.ini, state/ and log/ live here, and
# every module derives its data paths from this one value.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")
STATE_DIR = os.path.join(BASE_DIR, "state")
APP_DATA_DIR = (
    os.path.join(os.environ.get("LOCALAPPDATA", STATE_DIR), "GmailAutoDownloader")
    if os.name == "nt"
    else os.path.join(STATE_DIR, "private")
)
CREDENTIALS_PATH = os.path.join(APP_DATA_DIR, "credentials.json")
SERVICE_ACCOUNT_PATH = os.path.join(APP_DATA_DIR, "service_account.json")
TOKEN_PATH = os.path.join(APP_DATA_DIR, "token.json")

AUTH_OAUTH = "oauth"
AUTH_DWD = "dwd"

DEFAULTS = {
    "auth_mode": AUTH_OAUTH,
    "final_dir": "",
    "target_email": "",
    "lookback_days": "7",
    "polling_interval": "60",
    "rename_template": "{original}_{date}_{subject}_{sender}",
    "rename_subject_max": "50",
    "dwd_accounts": "[]",
    "excluded_labels": "",
    "allowed_extensions": ".pdf,.jpg,.jpeg,.png,.tiff,.doc,.docx,.xls,.xlsx,.xlsm,.ppt,.pptx,.txt,.csv,.zip",
}

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _new_parser():
    return configparser.ConfigParser(interpolation=None)


def normalize_auth_mode(value):
    return AUTH_DWD if str(value or "").strip().lower() == AUTH_DWD else AUTH_OAUTH


def load_settings():
    result = dict(DEFAULTS)
    cfg = _new_parser()
    cfg.read(CONFIG_PATH, encoding="utf-8")
    if cfg.has_section("settings"):
        result.update({k: v for k, v in cfg["settings"].items()})
    result["auth_mode"] = normalize_auth_mode(result.get("auth_mode"))
    return result


def save_settings(values):
    current = load_settings()
    current.update({k: str(v) for k, v in values.items()})
    current["auth_mode"] = normalize_auth_mode(current.get("auth_mode"))
    cfg = _new_parser()
    cfg["settings"] = current
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        cfg.write(handle)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    os.replace(tmp, CONFIG_PATH)


def parse_csv_setting(raw):
    """Split a comma separated setting into a trimmed, de-duplicated list."""
    items = []
    seen = set()
    for part in str(raw or "").split(","):
        value = part.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        items.append(value)
    return items


def load_excluded_labels(settings=None):
    """-> list[str], order preserved, empties dropped."""
    settings = settings or load_settings()
    return parse_csv_setting(settings.get("excluded_labels", ""))


def load_allowed_extensions(settings=None):
    """-> set[str], lowercased, each guaranteed to start with '.'.

    Falls back to the DEFAULTS value when the setting is present but parses empty.
    """
    settings = settings or load_settings()
    raw = settings.get("allowed_extensions", DEFAULTS["allowed_extensions"])

    def _normalize(csv_value):
        result = set()
        for item in parse_csv_setting(csv_value):
            value = item.lower()
            if not value.startswith("."):
                value = "." + value
            result.add(value)
        return result

    extensions = _normalize(raw)
    if not extensions:
        extensions = _normalize(DEFAULTS["allowed_extensions"])
    return extensions


def _current_windows_identity():
    result = subprocess.run(
        ["whoami"],
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    identity = result.stdout.strip()
    if result.returncode != 0 or not identity:
        raise RuntimeError("Windowsユーザーを特定できないため認証情報を安全に保存できません。")
    return identity


def _secure_private_path(path, is_dir=False):
    if os.name == "nt":
        identity = _current_windows_identity()
        permission = "(OI)(CI)F" if is_dir else "F"
        result = subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", f"{identity}:{permission}"],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            raise RuntimeError(
                "認証情報のWindows ACL設定に失敗しました。"
                + (f"\n{result.stderr.strip()}" if result.stderr.strip() else "")
            )
        return
    os.chmod(path, 0o700 if is_dir else 0o600)


def ensure_private_dir():
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    _secure_private_path(APP_DATA_DIR, is_dir=True)


def protect_private_file(path):
    ensure_private_dir()
    _secure_private_path(path, is_dir=False)


def _copy_private_json(source_path, target_path):
    if not source_path:
        return
    source = os.path.abspath(source_path)
    target = os.path.abspath(target_path)
    ensure_private_dir()
    if source != target:
        tmp = target + ".tmp"
        shutil.copy2(source, tmp)
        os.replace(tmp, target)
    protect_private_file(target)


def copy_credentials(source_path):
    _copy_private_json(source_path, CREDENTIALS_PATH)


def copy_service_account(source_path):
    _copy_private_json(source_path, SERVICE_ACCOUNT_PATH)


def _valid_email(value):
    return bool(_EMAIL_RE.fullmatch(str(value or "").strip()))


def normalize_dwd_accounts(accounts):
    normalized = []
    seen = set()
    for item in accounts or []:
        if not isinstance(item, dict):
            continue
        email = str(item.get("email", "")).strip().lower()
        final_dir = str(item.get("final_dir", "")).strip()
        if not email and not final_dir:
            continue
        if not _valid_email(email):
            raise ValueError(f"メールアドレスを確認してください: {email or '(空欄)'}")
        if not final_dir:
            raise ValueError(f"保存先が未設定です: {email}")
        if email in seen:
            raise ValueError(f"同じメールアドレスが重複しています: {email}")
        seen.add(email)
        normalized.append({"email": email, "final_dir": os.path.abspath(final_dir)})
    return normalized


def encode_dwd_accounts(accounts):
    return json.dumps(normalize_dwd_accounts(accounts), ensure_ascii=False, separators=(",", ":"))


def load_dwd_accounts(settings=None):
    settings = settings or load_settings()
    raw = settings.get("dwd_accounts", "[]")
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    try:
        return normalize_dwd_accounts(data)
    except ValueError:
        return []


def get_account_configs(settings=None):
    settings = settings or load_settings()
    mode = normalize_auth_mode(settings.get("auth_mode"))
    if mode == AUTH_DWD:
        return load_dwd_accounts(settings)
    email = settings.get("target_email", "").strip().lower()
    final_dir = settings.get("final_dir", "").strip()
    if not email or not final_dir:
        return []
    return [{"email": email, "final_dir": os.path.abspath(final_dir)}]


def is_configured():
    settings = load_settings()
    if not os.path.exists(CONFIG_PATH):
        return False
    mode = normalize_auth_mode(settings.get("auth_mode"))
    if mode == AUTH_DWD:
        return os.path.isfile(SERVICE_ACCOUNT_PATH) and bool(get_account_configs(settings))
    return (
        bool(settings.get("final_dir", "").strip())
        and _valid_email(settings.get("target_email", ""))
        and os.path.isfile(CREDENTIALS_PATH)
    )


def parse_scan_start(choice, custom_date=""):
    now = datetime.now()
    if choice == "今日":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    mapping = {
        "過去3日": 3,
        "過去7日": 7,
        "過去30日": 30,
    }
    if choice in mapping:
        return now - timedelta(days=mapping[choice])
    if choice == "日付指定":
        return datetime.strptime(custom_date.strip(), "%Y-%m-%d")
    raise ValueError("期間を選択してください。")
