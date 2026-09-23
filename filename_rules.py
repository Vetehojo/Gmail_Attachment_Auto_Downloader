import configparser
import os
import re

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_SCRIPT_DIR, "config.ini")
DEFAULT_TEMPLATE = "{original}_{date}_{subject}_{sender}"
DEFAULT_SUBJECT_MAX = 50
_ALLOWED_FIELDS = {"original", "date", "subject", "sender"}

MAX_WINDOWS_PATH = 259          # MAX_PATH (260) minus the terminating NUL
TEMP_NAME_RESERVE = 24          # worst case ".gmailad_<job id>.tmp"
RESERVED_STEMS = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def _load_settings():
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(_CONFIG_PATH, encoding="utf-8")
    if not cfg.has_section("settings"):
        return {}
    return dict(cfg["settings"])


def sanitize_component(value):
    value = str(value or "")
    for char in '<>:"/\\|?*':
        value = value.replace(char, "_")
    value = re.sub(r"[\x00-\x1f]", "_", value)
    return value.strip(" .")


def normalize_date(received_date):
    digits = re.sub(r"\D", "", str(received_date or ""))
    if re.fullmatch(r"\d{8}", digits):
        return digits
    return digits


def validate_template(template):
    template = (template or "").strip()
    if not template:
        return False, "リネームテンプレートが空です。"
    fields = re.findall(r"\{([^{}]+)\}", template)
    unknown = sorted(set(fields) - _ALLOWED_FIELDS)
    if unknown:
        return False, "使用できないプレースホルダー: " + ", ".join(unknown)
    if not fields:
        return False, "少なくとも1つのプレースホルダーを指定してください。"
    return True, ""


def filename_budget(final_dir, reserve=TEMP_NAME_RESERVE, limit=MAX_WINDOWS_PATH):
    """Characters a filename may use inside final_dir and still fit MAX_PATH.

    Accounts for the separator and for the temp name written next to it.
    Never returns less than 16 and never more than 255.
    """
    dir_length = len(os.path.abspath(str(final_dir or "")))
    # -1 for the path separator between final_dir and the filename itself.
    available = limit - dir_length - 1 - reserve
    return max(16, min(255, available))


def render_filename(
    original_filename,
    received_date="",
    sender_email="",
    mail_subject="",
    template=None,
    subject_max=None,
    max_filename_length=255,
):
    settings = _load_settings()
    template = (template if template is not None else settings.get("rename_template", DEFAULT_TEMPLATE)).strip()
    ok, _reason = validate_template(template)
    if not ok:
        template = DEFAULT_TEMPLATE

    try:
        subject_max = int(
            subject_max if subject_max is not None
            else settings.get("rename_subject_max", DEFAULT_SUBJECT_MAX)
        )
    except (TypeError, ValueError):
        subject_max = DEFAULT_SUBJECT_MAX
    subject_max = max(0, min(subject_max, 200))

    name, ext = os.path.splitext(str(original_filename or "file"))
    values = {
        "original": sanitize_component(name),
        "date": sanitize_component(normalize_date(received_date)),
        "subject": sanitize_component(mail_subject)[:subject_max],
        "sender": sanitize_component(sender_email),
    }

    try:
        rendered = template.format(**values)
    except (KeyError, ValueError):
        rendered = DEFAULT_TEMPLATE.format(**values)

    rendered = sanitize_component(rendered)
    rendered = re.sub(r"_{2,}", "_", rendered).strip("_ .")
    if not rendered:
        rendered = values["original"] or "file"

    safe_ext = str(ext or "")
    for char in '<>:"/\\|?*':
        safe_ext = safe_ext.replace(char, "_")
    safe_ext = re.sub(r"[\x00-\x1f]", "_", safe_ext).rstrip(" .")
    if safe_ext and not safe_ext.startswith("."):
        safe_ext = "." + safe_ext.lstrip(".")

    try:
        max_filename_length = int(max_filename_length)
    except (TypeError, ValueError):
        max_filename_length = 255
    max_filename_length = max(16, min(255, max_filename_length))

    max_stem = max(1, max_filename_length - len(safe_ext))
    stem = rendered[:max_stem]

    if stem.upper() in RESERVED_STEMS:
        stem = "_" + stem

    return stem + safe_ext


def preview_filename(template, subject_max=50):
    return render_filename(
        "物件資料.pdf",
        "20260825",
        "sample@example.com",
        "○○マンション売買契約書送付の件",
        template=template,
        subject_max=subject_max,
    )
