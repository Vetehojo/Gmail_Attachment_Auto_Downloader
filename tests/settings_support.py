"""Shared fakes for the settings dialog tests: Tk variable/button/root
stand-ins, mocked google credentials and services, and a configured install
in a temp folder (LiveStateTestBase) with every live path redirected."""
import os
import tempfile
import unittest
from unittest import mock

import app_settings
import gmail_app
import gmail_auth
from job_queue import JobQueue


class Var:
    """tk.StringVar / BooleanVar stand-in."""

    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeButton:
    def __init__(self):
        self.states = []

    def state(self, spec):
        self.states.append(tuple(spec))


class FakeWin:
    def winfo_exists(self):
        return False


class Root:
    """root.after stand-in; runs callbacks at once unless deferred."""

    def __init__(self, defer=False):
        self.defer = defer
        self.pending = []

    def after(self, _ms, callback):
        if self.defer:
            self.pending.append(callback)
        else:
            callback()


class InlineThread:
    def __init__(self, target, daemon=None):
        self.target = target

    def start(self):
        self.target()


class FakeCreds:
    def __init__(self, email, valid=True, expired=False, refresh_token="refresh", refresh_error=None):
        self.email = email
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.refresh_error = refresh_error
        self.refreshed = 0

    def refresh(self, _request):
        if self.refresh_error is not None:
            raise self.refresh_error
        self.refreshed += 1
        self.valid = True
        self.expired = False

    def to_json(self):
        return '{"token": "%s"}' % self.email


class Execute:
    def __init__(self, value):
        self.value = value

    def execute(self, num_retries=None):
        return self.value


class ProfileService:
    def __init__(self, email):
        self.email = email

    def users(self):
        return self

    def getProfile(self, userId):
        return Execute({"emailAddress": self.email} if self.email else {})


def fake_build(*_args, credentials=None, **_kwargs):
    return ProfileService(credentials.email)


class FakeServiceAccountCreds:
    def __init__(self, fail_for=()):
        self.fail_for = set(fail_for)

    def with_subject(self, subject):
        if subject in self.fail_for:
            raise ValueError("unauthorized_client")
        return FakeCreds(subject)


class LiveStateTestBase(unittest.TestCase):
    """A configured install in a temp folder: config.ini, the private
    credential folder (client JSON, token.json, service account) and the
    queue with a cursor."""

    TARGET = "a@example.com"

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = temp.name
        self.appdata = os.path.join(self.root, "appdata")
        self.downloads = os.path.join(self.root, "downloads")
        self.state = os.path.join(self.root, "state")
        for folder in (self.appdata, self.downloads, self.state):
            os.makedirs(folder)
        self.config = os.path.join(self.root, "config.ini")
        self.installed_client = os.path.join(self.appdata, "credentials.json")
        self.installed_sa = os.path.join(self.appdata, "service_account.json")
        self.token = os.path.join(self.appdata, "token.json")
        self.queue_db = os.path.join(self.state, "jobs.sqlite3")
        self.final = os.path.join(self.root, "final")
        self.new_final = os.path.join(self.root, "new-final")
        patches = [
            mock.patch.object(app_settings, "CONFIG_PATH", self.config),
            mock.patch.object(app_settings, "APP_DATA_DIR", self.appdata),
            mock.patch.object(app_settings, "CREDENTIALS_PATH", self.installed_client),
            mock.patch.object(app_settings, "SERVICE_ACCOUNT_PATH", self.installed_sa),
            mock.patch.object(app_settings, "TOKEN_PATH", self.token),
            mock.patch.object(app_settings, "_secure_private_path", lambda *a, **k: None),
            mock.patch.object(gmail_auth, "CREDENTIALS_FILE", self.installed_client),
            mock.patch.object(gmail_auth, "SERVICE_ACCOUNT_FILE", self.installed_sa),
            mock.patch.object(gmail_auth, "TOKEN_FILE", self.token),
            mock.patch.object(gmail_app, "CREDENTIALS_PATH", self.installed_client),
            mock.patch.object(gmail_app, "SERVICE_ACCOUNT_PATH", self.installed_sa),
            mock.patch.object(gmail_app, "QUEUE_DB", self.queue_db),
            mock.patch.object(gmail_app, "_QUEUE_INSTANCE", None),
            mock.patch.object(gmail_app, "TRAY_LOG", os.path.join(self.root, "log", "tray_log.txt")),
            mock.patch.object(gmail_app, "messagebox"),
            mock.patch.object(gmail_auth, "build", side_effect=fake_build),
            mock.patch.object(gmail_auth, "Request", return_value="request"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(lambda: setattr(gmail_app, "_QUEUE_INSTANCE", None))
        self.write(self.installed_client, '{"installed": {"client_id": "old"}}')
        self.write(self.installed_sa, '{"type": "service_account", "client_email": "old"}')
        self.write(self.token, '{"token": "live"}')
        app_settings.save_settings({
            "auth_mode": "oauth",
            "target_email": self.TARGET,
            "final_dir": self.final,
        })
        queue = JobQueue(self.queue_db)
        queue.set_metadata(gmail_app.MAIL_CURSOR_KEY, "1700000000")
        self.flow = mock.Mock()
        self.flow.run_local_server.return_value = FakeCreds("login@example.com")
        flow_patch = mock.patch.object(
            gmail_auth.InstalledAppFlow, "from_client_secrets_file", return_value=self.flow
        )
        self.from_client_file = flow_patch.start()
        self.addCleanup(flow_patch.stop)

    @staticmethod
    def write(path, text):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def snapshot(self):
        files = {}
        for folder, _dirs, names in os.walk(self.root):
            for name in names:
                path = os.path.join(folder, name)
                with open(path, "rb") as handle:
                    files[os.path.relpath(path, self.root)] = (handle.read(), os.stat(path).st_mtime_ns)
        return files

    def live_token(self, **kwargs):
        creds = FakeCreds(self.TARGET, **kwargs)
        patcher = mock.patch.object(gmail_auth.Credentials, "from_authorized_user_file", return_value=creds)
        patcher.start()
        self.addCleanup(patcher.stop)
        return creds

    def downloaded_client(self, text='{"installed": {"client_id": "new"}}'):
        path = os.path.join(self.downloads, "client_secret.json")
        self.write(path, text)
        return path

    def oauth_snapshot(self, client, email=None, force_login=False, staged_creds=None):
        return {
            "mode": "oauth",
            "email": email or self.TARGET,
            "client": client,
            "force_login": force_login,
            "staged_creds": staged_creds,
        }
