"""ServicePool identity guard: rebuild on auth mode / installed credential
change, verify every new service's mailbox against the stored identity, keep
auth deferrals visible, and fail jobs of accounts no longer configured.
Gmail is faked; queue, state and credential files live in a temp folder."""
import base64
import contextlib
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import app_settings
import gmail_monitor
import runtime_state
from job_queue import JobQueue


class Execute:
    def __init__(self, value):
        self.value = value

    def execute(self, num_retries=None):
        return self.value


class FakeGmail:
    """getProfile answers `mailbox`; attachments().get returns fixed bytes."""

    def __init__(self, mailbox):
        self.mailbox = mailbox

    def users(self):
        return self

    def getProfile(self, userId):
        return Execute({"emailAddress": self.mailbox} if self.mailbox else {})

    def messages(self):
        if self.mailbox is None:
            raise AssertionError("mailbox used before its identity was checked")
        return self

    def attachments(self):
        return self

    def list(self, **kwargs):
        raise AssertionError("scan reached Gmail with an unverified service")

    def get(self, **kwargs):
        return Execute({"data": base64.urlsafe_b64encode(b"attachment-bytes").decode("ascii")})


class IdentityGuardTestBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = temp.name
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.state)
        self.client = os.path.join(self.root, "credentials.json")
        self.service_account = os.path.join(self.root, "service_account.json")
        self.token = os.path.join(self.root, "token.json")
        for path in (self.client, self.service_account, self.token):
            self.write(path, "{}")
        self.queue = JobQueue(os.path.join(self.state, "jobs.sqlite3"))
        self.settings = self.oauth_settings("a@example.com")
        # What a built service really opens: OAuth -> the token's mailbox,
        # DWD -> the impersonated account unless overridden here.
        self.oauth_mailbox = "a@example.com"
        self.dwd_mailboxes = {}
        self.builds = []
        patches = [
            mock.patch.object(app_settings, "CREDENTIALS_PATH", self.client),
            mock.patch.object(app_settings, "SERVICE_ACCOUNT_PATH", self.service_account),
            mock.patch.object(app_settings, "TOKEN_PATH", self.token),
            mock.patch.object(gmail_monitor, "load_settings", lambda: dict(self.settings)),
            mock.patch.object(gmail_monitor, "get_gmail_service", side_effect=self.fake_service),
            mock.patch.object(gmail_monitor, "STATE_DIR", self.state),
            mock.patch.object(gmail_monitor, "HEARTBEAT_FILE", os.path.join(self.state, "heartbeat.json")),
            mock.patch.object(gmail_monitor, "LOG_FILE", os.path.join(self.root, "log", "mail_log.txt")),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def write(path, text):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def final_dir(self, account):
        return os.path.join(self.root, "final", account.split("@")[0])

    def oauth_settings(self, email):
        return {"auth_mode": "oauth", "target_email": email, "final_dir": self.final_dir(email), "lookback_days": "7"}

    def dwd_settings(self, *emails):
        accounts = [{"email": email, "final_dir": self.final_dir(email)} for email in emails]
        return {"auth_mode": "dwd", "dwd_accounts": app_settings.encode_dwd_accounts(accounts), "lookback_days": "7"}

    def fake_service(self, account_email=None, allow_interactive=False, auth_mode=None):
        self.builds.append((account_email, auth_mode))
        if auth_mode == "dwd":
            return FakeGmail(self.dwd_mailboxes.get(account_email, account_email))
        return FakeGmail(self.oauth_mailbox)

    def stored(self, account):
        return self.queue.get_metadata(runtime_state.identity_key(account))

    def store(self, account, value):
        self.queue.set_metadata(runtime_state.identity_key(account), value)

    def pool(self):
        return gmail_monitor.ServicePool(self.queue)


class ServicePoolRebuildTest(IdentityGuardTestBase):
    def test_probe_pool_after_persisted_mode_switch(self):
        # The external review's probe: a pool built for OAuth kept serving the
        # OAuth mailbox after DWD was saved, so account b's scan read mailbox a.
        pool = self.pool()
        pool.get("a@example.com")
        self.settings = self.dwd_settings("b@example.com")

        service = pool.get("b@example.com")

        self.assertEqual("b@example.com", service.mailbox)
        self.assertEqual("dwd", pool.auth_mode)
        self.assertEqual(("b@example.com", "dwd"), self.builds[-1])

    def test_rebuilds_when_the_installed_credential_file_changes(self):
        pool = self.pool()
        pool.get("a@example.com")
        pool.get("a@example.com")
        self.write(self.token, '{"token": "refreshed by the monitor"}')
        pool.get("a@example.com")
        self.assertEqual(1, len(self.builds))  # token.json is not part of the identity

        self.write(self.client, '{"installed": {"client_id": "other"}}')
        pool.get("a@example.com")
        self.assertEqual(2, len(self.builds))

    def test_oauth_target_switch_with_the_old_token_is_refused(self):
        # config.ini now says b, but token.json still opens a's mailbox.
        pool = self.pool()
        pool.get("a@example.com")
        self.settings = self.oauth_settings("b@example.com")
        for stored in ("", "b@example.com"):  # saved untested / verified as b
            with self.subTest(stored=stored):
                self.store("b@example.com", stored)
                with self.assertRaises(gmail_monitor.AuthenticationRequiredError):
                    pool.get("b@example.com")

    def test_account_no_longer_configured_is_refused(self):
        with self.assertRaises(gmail_monitor.AccountNotConfiguredError):
            self.pool().get("gone@example.com")
        self.assertEqual([], self.builds)


class VerifyIdentityTest(IdentityGuardTestBase):
    def test_first_use_records_the_identity_then_enforces_it(self):
        pool = self.pool()
        pool.get("a@example.com")
        self.assertEqual("a@example.com", self.stored("a@example.com"))

        pool.invalidate("a@example.com")
        self.oauth_mailbox = "intruder@example.com"
        with self.assertRaises(gmail_monitor.AuthenticationRequiredError) as ctx:
            pool.get("a@example.com")
        self.assertIn("intruder@example.com", str(ctx.exception))
        self.assertEqual("a@example.com", self.stored("a@example.com"))

    def test_missing_email_address_fails_closed(self):
        self.oauth_mailbox = ""
        with self.assertRaises(gmail_monitor.AuthenticationRequiredError):
            self.pool().get("a@example.com")
        self.assertIsNone(self.stored("a@example.com"))

    def test_account_saved_without_a_test_is_refused(self):
        self.store("a@example.com", "")
        with self.assertRaises(gmail_monitor.AuthenticationRequiredError) as ctx:
            self.pool().get("a@example.com")
        self.assertIn("接続テスト", str(ctx.exception))

    def test_alias_target_is_checked_against_the_verified_mailbox(self):
        self.settings = self.oauth_settings("info@example.com")
        self.oauth_mailbox = "owner@example.com"
        self.store("info@example.com", "Owner@Example.com")

        service = self.pool().get("info@example.com")

        self.assertEqual("owner@example.com", service.mailbox)


class GuardedScanAndJobTest(IdentityGuardTestBase):
    MODES = ("oauth", "dwd")

    def use_mode(self, mode):
        if mode == "dwd":
            self.settings = self.dwd_settings("a@example.com")
            self.dwd_mailboxes["a@example.com"] = "intruder@example.com"
        else:
            self.settings = self.oauth_settings("a@example.com")
            self.oauth_mailbox = "intruder@example.com"
        self.store("a@example.com", "a@example.com")

    def fix_mailbox(self):
        self.oauth_mailbox = "a@example.com"
        self.dwd_mailboxes.clear()

    def metadata_keys(self):
        with contextlib.closing(sqlite3.connect(self.queue.db_path)) as conn:
            return {row[0] for row in conn.execute("SELECT key FROM metadata")}

    def payload(self, account="a@example.com"):
        return {
            "account_email": account,
            "final_dir": self.final_dir(account),
            "message_id": "m1",
            "attachment_id": "att1",
            "part_id": "",
            "inline": False,
            "filename": "document.pdf",
            "received_date": "20260825",
            "sender_email": "sender@example.com",
            "mail_subject": "subject",
        }

    def test_scan_mismatch_leaves_the_cursor_and_queue_untouched(self):
        for mode in self.MODES:
            with self.subTest(mode=mode):
                self.use_mode(mode)
                with mock.patch.object(gmail_monitor, "log", lambda *a, **k: None):
                    result = gmail_monitor.scan_all_accounts(self.queue, self.pool())
                self.assertEqual(1, len(result["errors"]))
                self.assertIn("intruder@example.com", result["errors"][0]["error"])
                self.assertFalse(any(key.startswith(gmail_monitor.MAIL_CURSOR_KEY) for key in self.metadata_keys()))
                self.assertEqual({"pending": 0, "processing": 0, "success": 0, "failed": 0, "ignored": 0},
                                 self.queue.counts())
                self.assertIn("intruder@example.com", self.queue.get_metadata(gmail_monitor.LAST_ERROR_KEY))

    def test_job_mismatch_is_deferred_visibly_and_cleared_on_success(self):
        for index, mode in enumerate(self.MODES):
            with self.subTest(mode=mode):
                self.use_mode(mode)
                self.queue.enqueue(f"attachment:{mode}", self.payload())
                job_id = self.queue.get_job_by_key(f"attachment:{mode}")["id"]
                pool = self.pool()
                with mock.patch.object(gmail_monitor, "log", lambda *a, **k: None):
                    self.assertTrue(gmail_monitor.process_one_job(self.queue, pool))

                job = self.queue.get_job(job_id)
                self.assertEqual("pending", job["status"])
                self.assertEqual(0, job["attempts"])
                self.assertGreater(job["next_attempt_at"], time.time())
                self.assertFalse(os.path.exists(os.path.join(self.final_dir("a@example.com"), "document.pdf")))
                issues = runtime_state.read_auth_issues(self.queue)
                self.assertIn("intruder@example.com", issues["a@example.com"])

                self.fix_mailbox()
                with contextlib.closing(sqlite3.connect(self.queue.db_path)) as conn, conn:
                    conn.execute("UPDATE jobs SET next_attempt_at = 0 WHERE id = ?", (job_id,))
                with mock.patch.object(gmail_monitor, "log", lambda *a, **k: None):
                    self.assertTrue(gmail_monitor.process_one_job(self.queue, pool))
                self.assertEqual("success", self.queue.get_job(job_id)["status"])
                self.assertEqual({}, runtime_state.read_auth_issues(self.queue))
                self.assertEqual(index + 1, len(os.listdir(self.final_dir("a@example.com"))))

    def test_job_for_an_account_no_longer_configured_fails_visibly(self):
        self.queue.enqueue("attachment:gone", self.payload("gone@example.com"))
        with mock.patch.object(gmail_monitor, "log", lambda *a, **k: None):
            self.assertTrue(gmail_monitor.process_one_job(self.queue, self.pool()))

        [failed] = self.queue.list_failed()
        self.assertEqual("gone@example.com", failed["payload"]["account_email"])
        self.assertIn("現在の設定に含まれていません", failed["last_error"])
        self.assertEqual(1, failed["attempts"])
        self.assertFalse(os.path.exists(self.final_dir("gone@example.com")))
        self.assertEqual({}, runtime_state.read_auth_issues(self.queue))
        self.assertEqual([], self.builds)


class AuthIssueHelpersTest(unittest.TestCase):
    def test_set_clear_and_format(self):
        with tempfile.TemporaryDirectory() as td:
            queue = JobQueue(os.path.join(td, "jobs.sqlite3"))
            runtime_state.clear_auth_issue(queue, "a@example.com")
            self.assertIsNone(queue.get_metadata(runtime_state.AUTH_ISSUES_KEY))
            runtime_state.set_auth_issue(queue, "b@example.com", "expired")
            runtime_state.set_auth_issue(queue, "a@example.com", "mismatch")
            self.assertEqual(
                "a@example.com: mismatch / b@example.com: expired",
                runtime_state.format_auth_issues(runtime_state.read_auth_issues(queue)),
            )
            runtime_state.clear_auth_issue(queue, "a@example.com")
            runtime_state.clear_auth_issue(queue, "b@example.com")
            self.assertIsNone(queue.get_metadata(runtime_state.AUTH_ISSUES_KEY))
            queue.set_metadata(runtime_state.AUTH_ISSUES_KEY, "not json")
            self.assertEqual("not json", runtime_state.format_auth_issues(runtime_state.read_auth_issues(queue)))


if __name__ == "__main__":
    unittest.main()
