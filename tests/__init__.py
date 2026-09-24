"""Test package. Run from the repo root: python -m unittest discover -s tests -t . -v

The runtime modules live in app/ and import each other by top-level name
(app/ is sys.path[0] when app/*.py runs as a script), so the tests do the same.

%LOCALAPPDATA% points at a throwaway folder for the whole run, set before any
app module is imported: app_settings derives the private folder (credential
files, token.json, gmail_monitor's installation id) from it at import, and no
test may read or write the real %LOCALAPPDATA%\\GmailAutoDownloader.
gmail_monitor.INSTALL_ID_FILE is also pointed into that folder: off Windows,
app_settings.APP_DATA_DIR falls back under the repo's state/.
"""
import atexit
import os
import shutil
import sys
import tempfile

_LOCALAPPDATA = tempfile.mkdtemp(prefix="gad-tests-localappdata-")
os.environ["LOCALAPPDATA"] = _LOCALAPPDATA
atexit.register(shutil.rmtree, _LOCALAPPDATA, ignore_errors=True)

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import gmail_monitor  # only after LOCALAPPDATA and sys.path are set above

gmail_monitor.INSTALL_ID_FILE = os.path.join(_LOCALAPPDATA, "GmailAutoDownloader", "install_id")
