"""Trial download (gmail_monitor --trial N): newest N attachment mails per account."""
import base64
import contextlib
import functools
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from googleapiclient.errors import HttpError

import gmail_monitor
from job_queue import JobQueue


class Execute:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    def execute(self, num_retries=None):
        if self.error is not None:
            raise self.error
        return self.value


def not_found():
    return HttpError(mock.Mock(status=404, reason="Not Found"), b"")


def message_payload(subject, attachments, date="Tue, 22 Sep 2026 09:00:00 +0900"):
    """attachments: list of (filename, attachment_id)."""
    return {
        "headers": [
            {"name": "subject", "value": subject},
            {"name": "from", "value": "sender@example.com"},
            {"name": "date", "value": date},
        ],
        "parts": [
            {"partId": str(index), "filename": filename, "body": {"attachmentId": attachment_id, "size": 10}}
            for index, (filename, attachment_id) in enumerate(attachments, 1)
        ],
    }


class FakeGmail:
    """users().messages() with list (newest first), get and attachments().get."""

    def __init__(self, messages, gone=(), list_error=None, internal_dates=None):
        self.order = [message_id for message_id, _payload in messages]
        self.payloads = dict(messages)
        self.internal_dates = internal_dates or {}
        self.gone = set(gone)
        self.list_error = list_error
        self.list_calls = []
        self.get_calls = []
        self.attachment_calls = []

    def users(self):
        return self

    def messages(self):
        return self

    def attachments(self):
        return FakeAttachments(self)

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        if self.list_error is not None:
            return Execute(error=self.list_error)
        return Execute({"messages": [{"id": message_id} for message_id in self.order]})

    def get(self, **kwargs):
        self.get_calls.append(kwargs["id"])
        if kwargs["id"] in self.gone:
            return Execute(error=not_found())
        message = {"payload": self.payloads[kwargs["id"]]}
        if kwargs["id"] in self.internal_dates:
            message["internalDate"] = self.internal_dates[kwargs["id"]]
        return Execute(message)


class FakeAttachments:
    def __init__(self, gmail):
        self.gmail = gmail

    def get(self, **kwargs):
        self.gmail.attachment_calls.append(kwargs["id"])
        data = f"bytes-of-{kwargs['id']}".encode("ascii")
        return Execute({"data": base64.urlsafe_b64encode(data).decode("ascii")})


class FakePool:
    def __init__(self, services, errors=None):
        self.services = services
        self.errors = errors or {}
        self.invalidated = []

    def get(self, account_email):
        if account_email in self.errors:
            raise self.errors[account_email]
        return self.services[account_email]

    def invalidate(self, account_email):
        self.invalidated.append(account_email)


class FakeInstance:
    """SingleInstance stand-in; never touches a real Local\\ mutex."""

    held = set()
    raising = set()
    events = []

    def __init__(self, name, lock_path):
        self.name = name
        self.lock_path = lock_path

    def acquire(self):
        FakeInstance.events.append(("acquire", self.name))
        if self.name in FakeInstance.raising:
            raise OSError("CreateMutexW failed")
        return self.name not in FakeInstance.held

    def release(self):
        FakeInstance.events.append(("release", self.name))


class TrialTestBase(unittest.TestCase):
    ACCOUNT = "a@example.com"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = self.temp.name
        self.state = os.path.join(root, "state")
        self.final = os.path.join(root, "final")
        self.queue_db = os.path.join(self.state, "jobs.sqlite3")
        self.settings = {
            "auth_mode": "oauth",
            "target_email": self.ACCOUNT,
            "final_dir": self.final,
            "lookback_days": "7",
            "excluded_labels": "",
            "allowed_extensions": ".pdf",
        }
        patches = [
            mock.patch.object(gmail_monitor, "STATE_DIR", self.state),
            mock.patch.object(gmail_monitor, "QUEUE_DB", self.queue_db),
            mock.patch.object(gmail_monitor, "HEARTBEAT_FILE", os.path.join(self.state, "heartbeat.json")),
            mock.patch.object(gmail_monitor, "LOCK_FILE", os.path.join(self.state, "monitor.lock")),
            mock.patch.object(gmail_monitor, "TRAY_LOCK_FILE", os.path.join(self.state, "app.lock")),
            mock.patch.object(gmail_monitor, "LOG_FILE", os.path.join(root, "log", "mail_log.txt")),
            mock.patch.object(gmail_monitor, "load_settings", lambda: dict(self.settings)),
            mock.patch.object(gmail_monitor, "is_configured", return_value=True),
            mock.patch.object(gmail_monitor, "SingleInstance", FakeInstance),
            # The trial must never reuse the cursor-writing scan paths.
            mock.patch.object(gmail_monitor, "scan_gmail", side_effect=AssertionError("scan_gmail called")),
            mock.patch.object(gmail_monitor, "scan_all_accounts", side_effect=AssertionError("scan_all_accounts")),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        FakeInstance.held = set()
        FakeInstance.raising = set()
        FakeInstance.events = []
        os.makedirs(self.state)

    def use_pool(self, pool):
        patcher = mock.patch.object(gmail_monitor, "ServicePool", return_value=pool)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, count=3):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = gmail_monitor.run_trial_cli(count)
        return code, out.getvalue()

    def queue(self):
        return JobQueue(self.queue_db)

    def saved_files(self, folder=None):
        folder = folder or self.final
        if not os.path.isdir(folder):
            return []
        return sorted(name for name in os.listdir(folder) if not name.startswith("."))

    def job_key(self, message_id, attachment_id, account=None):
        return gmail_monitor._attachment_job_key(
            account or self.ACCOUNT, message_id, {"attachment_id": attachment_id}
        )

    def standard_inbox(self):
        # Newest first, as Gmail lists by default.
        return FakeGmail([
            ("m1", message_payload("newest", [("a.pdf", "att1")])),
            ("m2", message_payload("exe only", [("tool.exe", "att2")])),
            ("m3", message_payload("third", [("c.pdf", "att3"), ("c.exe", "att3x")])),
            ("m4", message_payload("fourth", [("d.pdf", "att4")])),
            ("m5", message_payload("fifth", [("e.pdf", "att5")])),
        ])


class TrialSelectionTest(TrialTestBase):
    def test_saves_the_newest_n_messages_with_allowed_attachments(self):
        gmail = self.standard_inbox()
        self.use_pool(FakePool({self.ACCOUNT: gmail}))

        code, out = self.run_cli(3)

        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, code)
        self.assertEqual(["m1", "m2", "m3", "m4"], gmail.get_calls)  # m5 is never fetched
        self.assertEqual(["att1", "att3", "att4"], gmail.attachment_calls)
        self.assertEqual(3, len(self.saved_files()))
        self.assertTrue(any(name.startswith("a_") for name in self.saved_files()))
        self.assertIn("保存しました", out)
        self.assertIn("tool.exe（拡張子 .exe は保存対象外）", out)
        self.assertIn("c.exe（拡張子 .exe は保存対象外）", out)
        self.assertIn("結果: 保存 3件 / 保存済み 0件 / 保存できなかった添付 0件", out)
        self.assertIn("「添付を取り直す」", out)

    def test_query_is_exactly_inbox_has_attachment_plus_excluded_labels(self):
        self.settings["excluded_labels"] = "FAX,税務"
        gmail = FakeGmail([])
        self.use_pool(FakePool({self.ACCOUNT: gmail}))

        code, out = self.run_cli(3)

        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, code)
        self.assertEqual('in:inbox has:attachment -label:"FAX" -label:"税務"', gmail.list_calls[0]["q"])
        self.assertEqual('in:inbox has:attachment', gmail_monitor.build_trial_query([]))
        self.assertIn("見つかりませんでした", out)

    def test_scan_cap_stops_early_and_reports_the_shortfall(self):
        gmail = FakeGmail([(f"m{i}", message_payload(f"s{i}", [("x.exe", f"a{i}")])) for i in range(1, 6)])
        report = self.empty_report()
        gmail_monitor.select_trial_messages(
            self.queue(), gmail, {"email": self.ACCOUNT, "final_dir": self.final}, 3, [], {".pdf"}, report,
            scan_cap=3,
        )
        self.assertEqual(["m1", "m2", "m3"], gmail.get_calls)
        self.assertTrue(report["capped"])
        self.assertEqual(3, report["scanned"])
        self.assertEqual([], report["messages"])
        lines = "\n".join(gmail_monitor.format_trial_report([report], 3, gmail_monitor.trial_exit_code([report])))
        self.assertIn("見つかりませんでした（新しい順に3件を確認）", lines)
        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, gmail_monitor.trial_exit_code([report]))

    def test_default_scan_cap_is_100_messages(self):
        gmail = FakeGmail([(f"m{i}", message_payload(f"s{i}", [("x.exe", f"a{i}")])) for i in range(1, 151)])
        self.use_pool(FakePool({self.ACCOUNT: gmail}))
        code, out = self.run_cli(3)
        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, code)
        self.assertEqual(100, len(gmail.get_calls))

    def test_file_name_date_is_the_received_time_in_the_pc_timezone(self):
        # 2026-08-24T15:30Z is already the 25th in Japan; the Date header says 2030.
        received = str(int(datetime(2026, 8, 24, 15, 30, tzinfo=timezone.utc).timestamp() * 1000))
        gmail = FakeGmail(
            [("m1", message_payload("newest", [("a.pdf", "att1")], date="Tue, 1 Jan 2030 12:00:00 +0000"))],
            internal_dates={"m1": received},
        )
        self.use_pool(FakePool({self.ACCOUNT: gmail}))
        real = gmail_monitor.get_received_date_yyyymmdd
        jst = timezone(timedelta(hours=9))
        with mock.patch.object(gmail_monitor, "get_received_date_yyyymmdd", functools.partial(real, tz=jst)):
            code, out = self.run_cli(1)

        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, code)
        [name] = self.saved_files()
        self.assertIn("_20260825_", name)
        self.assertIn("2026/08/25 件名「newest」", out)
        job = self.queue().get_job_by_key(self.job_key("m1", "att1"))
        self.assertEqual("20260825", job["payload"]["received_date"])

    def test_fewer_than_n_when_the_inbox_runs_out_is_not_an_error(self):
        gmail = FakeGmail([("m1", message_payload("only", [("a.pdf", "att1")]))])
        self.use_pool(FakePool({self.ACCOUNT: gmail}))
        code, out = self.run_cli(3)
        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, code)
        self.assertIn("保存対象の添付があるメールは1件だけでした。", out)

    def test_message_deleted_between_list_and_get_is_skipped(self):
        gmail = FakeGmail(
            [("m1", {}), ("m2", message_payload("kept", [("b.pdf", "att2")]))],
            gone={"m1"},
        )
        self.use_pool(FakePool({self.ACCOUNT: gmail}))
        code, out = self.run_cli(1)
        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, code)
        self.assertEqual(["att2"], gmail.attachment_calls)
        self.assertIn("確認中に削除されたメール 1件を飛ばしました。", out)

    def test_other_http_errors_fail_the_account(self):
        gmail = FakeGmail([("m1", {})])
        gmail.get =lambda **kwargs: Execute(error=HttpError(mock.Mock(status=500, reason="boom"), b""))
        self.use_pool(FakePool({self.ACCOUNT: gmail}))
        code, out = self.run_cli(1)
        self.assertEqual(gmail_monitor.TRIAL_EXIT_FAILED, code)
        self.assertIn("エラー:", out)

    def empty_report(self):
        return {
            "account": self.ACCOUNT, "final_dir": self.final, "mode": "oauth", "messages": [], "skipped": [],
            "scanned": 0, "gone": 0, "capped": False, "error": "", "auth_error": False,
        }


class TrialIsolationTest(TrialTestBase):
    def test_only_trial_jobs_are_processed(self):
        queue = self.queue()
        queue.enqueue("attachment:a@example.com:other:1", {"account_email": self.ACCOUNT, "filename": "z.pdf"})
        gmail = FakeGmail([("m1", message_payload("newest", [("a.pdf", "att1")]))])
        self.use_pool(FakePool({self.ACCOUNT: gmail}))

        code, _out = self.run_cli(3)

        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, code)
        other = queue.get_job_by_key("attachment:a@example.com:other:1")
        self.assertEqual("pending", other["status"])
        self.assertEqual(0, other["attempts"])
        self.assertEqual(["att1"], gmail.attachment_calls)

    def test_metadata_is_unchanged_except_the_worker_heartbeat(self):
        queue = self.queue()
        for key, value in {
            gmail_monitor.MAIL_CURSOR_KEY: "1700000000",
            gmail_monitor.LAST_ERROR_KEY: "old error",
            gmail_monitor.WORKER_HEARTBEAT_KEY: "1700000001",
            gmail_monitor.WORKER_ERROR_KEY: "worker error",
            "monitor_paused": "1",
            "monitor_pause_reason": "exit",
        }.items():
            queue.set_metadata(key, value)

        def snapshot():
            with contextlib.closing(sqlite3.connect(self.queue_db)) as conn:
                return conn.execute(
                    "SELECT key, value, updated_at FROM metadata WHERE key != ? ORDER BY key",
                    (gmail_monitor.WORKER_HEARTBEAT_KEY,),
                ).fetchall()

        before = snapshot()
        gmail = self.standard_inbox()
        self.use_pool(FakePool({self.ACCOUNT: gmail}))
        code, _out = self.run_cli(3)
        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, code)
        self.assertEqual(before, snapshot())
        # The watchdog counts the trial process as the monitor: its worker
        # heartbeat must be fresh, or a running trial would look stalled.
        self.assertGreater(float(queue.get_metadata(gmail_monitor.WORKER_HEARTBEAT_KEY)), time.time() - 60)

    def journal_phase(self):
        names = [name for name in os.listdir(self.state) if name.startswith("attachmentjob_")]
        if not names:
            return None
        with open(os.path.join(self.state, names[0]), encoding="utf-8") as handle:
            return json.load(handle)["phase"]

    def test_worker_heartbeat_at_the_start_and_at_every_job_step(self):
        gmail = FakeGmail([("m1", message_payload("newest", [("a.pdf", "att1")]))])
        self.use_pool(FakePool({self.ACCOUNT: gmail}))
        real_beat = gmail_monitor._worker_beat
        beats = []

        def beat(queue):
            beats.append((len(gmail.attachment_calls), len(self.saved_files()), self.journal_phase()))
            real_beat(queue)

        with mock.patch.object(gmail_monitor, "_worker_beat", side_effect=beat):
            code, _out = self.run_cli(1)

        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, code)
        self.assertEqual(
            [
                (0, 0, None),        # trial start
                (0, 0, None),        # claimed
                (1, 0, None),        # fetched
                (1, 1, "prepared"),  # placed
                (1, 1, "prepared"),  # verified
            ],
            beats,
        )

    def test_failed_ignored_and_backoff_jobs_are_reported_not_claimed(self):
        queue = self.queue()
        payload = {"account_email": self.ACCOUNT, "filename": "x.pdf"}
        queue.enqueue(self.job_key("m1", "att1"), payload, max_attempts=1)
        failed = queue.claim_next()
        queue.mark_failure(failed["id"], "broken before")
        queue.enqueue(self.job_key("m2", "att2"), payload, max_attempts=1)
        ignored = queue.claim_next()
        queue.mark_failure(ignored["id"], "ignored before")
        queue.ignore_job(ignored["id"])
        queue.enqueue(self.job_key("m3", "att3"), payload, max_attempts=5)
        backoff = queue.claim_next()
        queue.mark_failure(backoff["id"], "wait", retry_delays=(3600,))
        before = {job_id: queue.get_job(job_id) for job_id in (failed["id"], ignored["id"], backoff["id"])}

        gmail = FakeGmail([
            ("m1", message_payload("one", [("a.pdf", "att1")])),
            ("m2", message_payload("two", [("b.pdf", "att2")])),
            ("m3", message_payload("three", [("c.pdf", "att3")])),
        ])
        self.use_pool(FakePool({self.ACCOUNT: gmail}))
        code, out = self.run_cli(3)

        self.assertEqual(gmail_monitor.TRIAL_EXIT_FAILED, code)
        self.assertEqual([], gmail.attachment_calls)
        for job_id, job in before.items():
            self.assertEqual(job, queue.get_job(job_id))
        self.assertIn("以前の取得で失敗しています", out)
        self.assertIn("「無視」に設定された添付です", out)
        self.assertIn("再試行待ちです", out)
        self.assertIn("保存できなかった添付 3件", out)

    def test_download_failure_goes_through_the_normal_failure_path(self):
        gmail = FakeGmail([("m1", message_payload("one", [("a.pdf", "att1")]))])
        self.use_pool(FakePool({self.ACCOUNT: gmail}))
        with mock.patch.object(gmail_monitor, "run_attachment_job", side_effect=RuntimeError("disk full")):
            code, out = self.run_cli(3)
        self.assertEqual(gmail_monitor.TRIAL_EXIT_FAILED, code)
        job = self.queue().get_job_by_key(self.job_key("m1", "att1"))
        self.assertEqual("pending", job["status"])
        self.assertEqual(1, job["attempts"])
        self.assertIn("disk full", job["last_error"])
        self.assertIn("自動取得の開始後に再試行します", out)


class TrialRerunTest(TrialTestBase):
    def test_rerun_is_idempotent(self):
        gmail = self.standard_inbox()
        self.use_pool(FakePool({self.ACCOUNT: gmail}))
        first_code, _ = self.run_cli(3)
        files = self.saved_files()
        calls = list(gmail.attachment_calls)

        second_code, out = self.run_cli(3)

        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, first_code)
        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, second_code)
        self.assertEqual(files, self.saved_files())
        self.assertEqual(calls, gmail.attachment_calls)
        self.assertIn("結果: 保存 0件 / 保存済み 3件", out)

    def test_later_normal_scan_does_not_create_new_jobs(self):
        gmail = self.standard_inbox()
        self.use_pool(FakePool({self.ACCOUNT: gmail}))
        self.run_cli(3)
        queue = self.queue()
        added = sum(
            gmail_monitor.enqueue_message_jobs(queue, gmail, message_id, self.ACCOUNT, self.final, {".pdf"})
            for message_id in ("m1", "m3", "m4")
        )
        self.assertEqual(0, added)

    def test_missing_saved_file_is_flagged_and_not_resaved(self):
        gmail = self.standard_inbox()
        self.use_pool(FakePool({self.ACCOUNT: gmail}))
        self.run_cli(1)
        [name] = self.saved_files()
        os.remove(os.path.join(self.final, name))

        code, out = self.run_cli(1)

        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, code)
        self.assertEqual([], self.saved_files())
        self.assertEqual(["att1"], gmail.attachment_calls)
        self.assertIn("ファイルが見つかりません", out)

    def test_changed_save_folder_is_not_applied_to_saved_jobs_and_is_noted(self):
        gmail = self.standard_inbox()
        self.use_pool(FakePool({self.ACCOUNT: gmail}))
        code, out = self.run_cli(1)
        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, code)
        self.assertNotIn("別の場所に記録された添付", out)
        [name] = self.saved_files()

        new_final = os.path.join(self.temp.name, "moved")
        self.settings["final_dir"] = new_final
        code, out = self.run_cli(1)

        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, code)
        self.assertEqual([], self.saved_files(new_final))
        self.assertEqual([name], self.saved_files())
        self.assertIn(f"保存済みです（前回までに保存）: {os.path.join(self.final, name)}", out)
        self.assertEqual(1, out.count("※ 現在の保存先とは別の場所に記録された添付があります。"))

    def test_is_under_compares_whole_path_components(self):
        base = os.path.join(self.temp.name, "final")
        self.assertTrue(gmail_monitor._is_under(os.path.join(base, "a.pdf"), base))
        self.assertTrue(gmail_monitor._is_under(os.path.join(base.upper(), "sub", "a.pdf"), base))
        self.assertFalse(gmail_monitor._is_under(os.path.join(base + "2", "a.pdf"), base))
        self.assertFalse(gmail_monitor._is_under(r"Z:\elsewhere\a.pdf", r"C:\final"))

    def test_compacted_success_counts_as_already_saved(self):
        queue = self.queue()
        queue.enqueue(self.job_key("m1", "att1"), {"account_email": self.ACCOUNT, "filename": "a.pdf"})
        job = queue.claim_next()
        queue.mark_success(job["id"], {"target_path": "gone"})
        queue.apply_retention(now=time.time() + 100 * 86400, full_days=90, dedupe_days=730)
        gmail = FakeGmail([("m1", message_payload("one", [("a.pdf", "att1")]))])
        self.use_pool(FakePool({self.ACCOUNT: gmail}))

        code, out = self.run_cli(1)

        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, code)
        self.assertIn("古い記録のため保存先の記録はありません", out)

    def test_stale_processing_job_is_recovered_before_the_trial(self):
        queue = self.queue()
        queue.enqueue(self.job_key("m1", "att1"), {
            "account_email": self.ACCOUNT, "final_dir": self.final, "message_id": "m1",
            "attachment_id": "att1", "part_id": "1", "inline": False, "filename": "a.pdf",
            "received_date": "20260922", "sender_email": "sender@example.com", "mail_subject": "newest",
        })
        self.assertEqual("processing", queue.claim_next()["status"])
        gmail = FakeGmail([("m1", message_payload("newest", [("a.pdf", "att1")]))])
        self.use_pool(FakePool({self.ACCOUNT: gmail}))

        code, out = self.run_cli(1)

        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, code)
        self.assertEqual("success", queue.get_job_by_key(self.job_key("m1", "att1"))["status"])
        self.assertIn("保存しました", out)

    def test_ctrl_c_after_the_file_write_then_rerun_saves_once(self):
        gmail = FakeGmail([("m1", message_payload("newest", [("a.pdf", "att1")]))])
        self.use_pool(FakePool({self.ACCOUNT: gmail}))
        real_write = gmail_monitor._write_bytes_atomic

        def write_then_interrupt(data, target_path, tag):
            real_write(data, target_path, tag)
            raise KeyboardInterrupt

        with mock.patch.object(gmail_monitor, "_write_bytes_atomic", side_effect=write_then_interrupt):
            code, out = self.run_cli(1)
        self.assertEqual(gmail_monitor.TRIAL_EXIT_FAILED, code)
        self.assertIn("中断しました", out)
        self.assertEqual("processing", self.queue().get_job_by_key(self.job_key("m1", "att1"))["status"])
        self.assertIn(("release", gmail_monitor.MONITOR_MUTEX_NAME), FakeInstance.events)

        code, out = self.run_cli(1)

        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, code)
        self.assertEqual(1, len(self.saved_files()))
        self.assertEqual("success", self.queue().get_job_by_key(self.job_key("m1", "att1"))["status"])
        self.assertIn("保存済みです（前回までに保存）", out)

    def test_orphan_temps_in_configured_folders_are_removed_after_recovery(self):
        other = os.path.join(self.temp.name, "not_configured")
        os.makedirs(self.final)
        os.makedirs(other)
        temp_name = gmail_monitor.temp_file_name(7)
        for folder in (self.final, other):
            with open(os.path.join(folder, temp_name), "wb") as handle:
                handle.write(b"partial")
        gmail = FakeGmail([])
        self.use_pool(FakePool({self.ACCOUNT: gmail}))
        order = []
        real_recover = gmail_monitor.recover_interrupted_jobs
        real_cleanup = gmail_monitor.cleanup_orphan_temp_files

        def recover(queue):
            order.append("recover")
            return real_recover(queue)

        def cleanup(final_dirs):
            order.append(("cleanup", list(final_dirs)))
            return real_cleanup(final_dirs)

        with mock.patch.object(gmail_monitor, "recover_interrupted_jobs", side_effect=recover), \
             mock.patch.object(gmail_monitor, "cleanup_orphan_temp_files", side_effect=cleanup):
            code, _out = self.run_cli(3)

        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, code)
        self.assertEqual(["recover", ("cleanup", [os.path.abspath(self.final)])], order)
        self.assertFalse(os.path.exists(os.path.join(self.final, temp_name)))
        self.assertTrue(os.path.exists(os.path.join(other, temp_name)))

    def test_ctrl_c_before_the_file_write_then_rerun_saves_once(self):
        gmail = FakeGmail([("m1", message_payload("newest", [("a.pdf", "att1")]))])
        self.use_pool(FakePool({self.ACCOUNT: gmail}))
        with mock.patch.object(gmail_monitor, "_write_bytes_atomic", side_effect=KeyboardInterrupt):
            code, _out = self.run_cli(1)
        self.assertEqual(gmail_monitor.TRIAL_EXIT_FAILED, code)

        code, out = self.run_cli(1)

        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, code)
        self.assertEqual(1, len(self.saved_files()))
        self.assertIn("保存しました", out)


class TrialAccountsTest(TrialTestBase):
    def use_dwd(self):
        self.osaka = os.path.join(self.final, "Osaka")
        self.nara = os.path.join(self.final, "Nara")
        self.settings.update({
            "auth_mode": "dwd",
            "dwd_accounts": json.dumps([
                {"email": "osaka@example.jp", "final_dir": self.osaka},
                {"email": "nara@example.jp", "final_dir": self.nara},
            ]),
        })

    def test_dwd_selects_per_account(self):
        self.use_dwd()
        osaka = FakeGmail([(f"o{i}", message_payload(f"o{i}", [(f"o{i}.pdf", f"oa{i}")])) for i in range(1, 5)])
        nara = FakeGmail([(f"n{i}", message_payload(f"n{i}", [(f"n{i}.pdf", f"na{i}")])) for i in range(1, 5)])
        self.use_pool(FakePool({"osaka@example.jp": osaka, "nara@example.jp": nara}))

        code, out = self.run_cli(2)

        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, code)
        self.assertEqual(["oa1", "oa2"], osaka.attachment_calls)
        self.assertEqual(["na1", "na2"], nara.attachment_calls)
        self.assertEqual(2, len(self.saved_files(self.osaka)))
        self.assertEqual(2, len(self.saved_files(self.nara)))
        self.assertIn("[osaka@example.jp]", out)
        self.assertIn("[nara@example.jp]", out)

    def test_one_failing_dwd_account_is_reported_and_the_other_continues(self):
        self.use_dwd()
        osaka = FakeGmail([], list_error=RuntimeError("mailbox unavailable"))
        nara = FakeGmail([("n1", message_payload("n1", [("n1.pdf", "na1")]))])
        pool = FakePool({"osaka@example.jp": osaka, "nara@example.jp": nara})
        self.use_pool(pool)

        code, out = self.run_cli(1)

        self.assertEqual(gmail_monitor.TRIAL_EXIT_FAILED, code)
        self.assertEqual(["na1"], nara.attachment_calls)
        self.assertEqual(["osaka@example.jp"], pool.invalidated)
        self.assertIn("エラー: mailbox unavailable", out)

    def test_authentication_failure_everywhere_exits_2_with_a_setup_hint(self):
        error = gmail_monitor.AuthenticationRequiredError("Gmail authentication is required")
        self.use_pool(FakePool({}, errors={self.ACCOUNT: error}))
        code, out = self.run_cli(3)
        self.assertEqual(gmail_monitor.TRIAL_EXIT_BLOCKED, code)
        self.assertIn("「Googleに接続してテスト」", out)

    def test_api_access_denied_everywhere_exits_2_with_an_api_hint(self):
        self.use_dwd()
        denied = HttpError(mock.Mock(status=403, reason="Gmail API has not been used"), b"")
        osaka = FakeGmail([], list_error=denied)
        nara = FakeGmail([], list_error=denied)
        self.use_pool(FakePool({"osaka@example.jp": osaka, "nara@example.jp": nara}))
        code, out = self.run_cli(1)
        self.assertEqual(gmail_monitor.TRIAL_EXIT_BLOCKED, code)
        self.assertEqual(2, out.count("Gmail API が有効か"))
        self.assertIn("ドメイン全体の委任", out)

    def test_partial_dwd_authentication_failure_exits_1(self):
        self.use_dwd()
        error = gmail_monitor.AuthenticationRequiredError("not delegated")
        nara = FakeGmail([("n1", message_payload("n1", [("n1.pdf", "na1")]))])
        self.use_pool(FakePool({"nara@example.jp": nara}, errors={"osaka@example.jp": error}))
        code, out = self.run_cli(1)
        self.assertEqual(gmail_monitor.TRIAL_EXIT_FAILED, code)
        self.assertIn("「登録した全アカウントに接続してテスト」", out)


class TrialLockTest(TrialTestBase):
    def assert_refused(self, code, out):
        self.assertEqual(gmail_monitor.TRIAL_EXIT_BLOCKED, code)
        self.assertIn("トレイアプリを終了してから実行してください", out)
        self.assertFalse(os.path.exists(self.queue_db))

    def test_monitor_lock_held_refuses(self):
        FakeInstance.held = {gmail_monitor.MONITOR_MUTEX_NAME}
        self.assert_refused(*self.run_cli(3))

    def test_tray_running_refuses_and_releases_the_monitor_lock(self):
        FakeInstance.held = {gmail_monitor.TRAY_MUTEX_NAME}
        self.assert_refused(*self.run_cli(3))
        self.assertEqual(("release", gmail_monitor.MONITOR_MUTEX_NAME), FakeInstance.events[-1])

    def test_mutex_access_denied_counts_as_held(self):
        for name in (gmail_monitor.MONITOR_MUTEX_NAME, gmail_monitor.TRAY_MUTEX_NAME):
            with self.subTest(name):
                FakeInstance.raising = {name}
                self.assert_refused(*self.run_cli(3))

    def test_tray_probe_is_released_and_monitor_lock_held_during_the_run(self):
        gmail = FakeGmail([("m1", message_payload("one", [("a.pdf", "att1")]))])
        self.use_pool(FakePool({self.ACCOUNT: gmail}))
        code, _out = self.run_cli(1)
        self.assertEqual(gmail_monitor.TRIAL_EXIT_OK, code)
        self.assertEqual(
            [
                ("acquire", gmail_monitor.MONITOR_MUTEX_NAME),
                ("acquire", gmail_monitor.TRAY_MUTEX_NAME),
                ("release", gmail_monitor.TRAY_MUTEX_NAME),
                ("release", gmail_monitor.MONITOR_MUTEX_NAME),
            ],
            FakeInstance.events,
        )

    def test_not_configured_refuses(self):
        with mock.patch.object(gmail_monitor, "is_configured", return_value=False):
            code, out = self.run_cli(3)
        self.assertEqual(gmail_monitor.TRIAL_EXIT_BLOCKED, code)
        self.assertIn("1_setup.bat", out)
        self.assertFalse(os.path.exists(self.queue_db))


class TrialCommandLineTest(unittest.TestCase):
    def run_main(self, *argv):
        stdout = mock.Mock()
        with mock.patch("sys.argv", ["gmail_monitor.py", *argv]), \
             mock.patch("sys.stdout", stdout), \
             mock.patch.object(gmail_monitor, "run_trial_cli", return_value=7) as trial:
            code = gmail_monitor.main()
        return code, trial, stdout

    def test_trial_count_is_passed_and_output_errors_are_replaced(self):
        code, trial, stdout = self.run_main("--trial", "3")
        self.assertEqual(7, code)
        trial.assert_called_once_with(3)
        stdout.reconfigure.assert_called_once_with(errors="replace")

    def test_trial_without_a_number_defaults_to_3(self):
        _code, trial, _stdout = self.run_main("--trial")
        trial.assert_called_once_with(3)

    def test_trial_count_out_of_range_is_rejected(self):
        for value in ("0", "21", "x"):
            with self.subTest(value):
                with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as raised:
                    self.run_main("--trial", value)
                self.assertEqual(2, raised.exception.code)

    def test_mutex_names_match_the_tray_and_monitor(self):
        app_dir = os.path.dirname(gmail_monitor.__file__)
        with open(os.path.join(app_dir, "gmail_app.py"), encoding="utf-8") as handle:
            tray_source = handle.read()
        self.assertIn(f'SingleInstance("{gmail_monitor.TRAY_MUTEX_NAME}"'.replace("\\", "\\\\"), tray_source)
        self.assertEqual("Local\\GmailAutoDownloaderMonitor", gmail_monitor.MONITOR_MUTEX_NAME)


if __name__ == "__main__":
    unittest.main()
