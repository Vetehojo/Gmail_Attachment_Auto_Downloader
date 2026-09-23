"""W: several installations (PCs) saving into one shared folder.

Reproduces the external review's probes shared_target_overwrite (both
installations pick the same free name; the later move used to replace the
earlier, already verified file) and cleanup_of_other_installation (startup
cleanup used to delete another installation's live temp), and pins the
no-overwrite placement, the per-installation temp names and the install id.
"""
import base64
import hashlib
import json
import os
import re
import tempfile
import unittest
from unittest import mock

import app_settings
import filename_rules
import gmail_monitor


class Execute:
    def __init__(self, value):
        self.value = value

    def execute(self, num_retries=None):
        return self.value


class AttachmentService:
    def __init__(self, data):
        self.data = data

    def users(self):
        return self

    def messages(self):
        return self

    def attachments(self):
        return self

    def get(self, **kwargs):
        return Execute({"data": base64.urlsafe_b64encode(self.data).decode("ascii")})


def old_cleanup_matches(name):
    """The predicate of cleanup_orphan_temp_files before installation ids."""
    return name.startswith(".gmailad_") and name.endswith(".tmp")


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = temp.name
        self.state = os.path.join(self.root, "state")
        self.final = os.path.join(self.root, "final")
        os.makedirs(self.state)
        os.makedirs(self.final)
        self.log_file = os.path.join(self.root, "log", "mail_log.txt")
        for patcher in (
            mock.patch.object(gmail_monitor, "STATE_DIR", self.state),
            mock.patch.object(gmail_monitor, "LOG_FILE", self.log_file),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def read_log(self):
        if not os.path.exists(self.log_file):
            return ""
        with open(self.log_file, encoding="utf-8") as handle:
            return handle.read()

    @staticmethod
    def write(path, data):
        with open(path, "wb") as handle:
            handle.write(data)

    @staticmethod
    def read(path):
        with open(path, "rb") as handle:
            return handle.read()


class InstallIdTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.id_file = os.path.join(self.root, "localappdata", "GmailAutoDownloader", "install_id")
        for patcher in (
            mock.patch.object(gmail_monitor, "INSTALL_ID_FILE", self.id_file),
            mock.patch.dict(gmail_monitor._INSTALL_IDS, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    @unittest.skipUnless(os.name == "nt", "APP_DATA_DIR is %LOCALAPPDATA% only on Windows")
    def test_default_location_is_the_private_localappdata_folder_not_state(self):
        # tests/__init__.py points LOCALAPPDATA at a throwaway folder.
        self.assertEqual(
            os.path.join(os.environ["LOCALAPPDATA"], "GmailAutoDownloader", "install_id"),
            os.path.join(app_settings.APP_DATA_DIR, "install_id"),
        )
        with mock.patch.dict(gmail_monitor._INSTALL_IDS, clear=True):
            self.assertTrue(re.fullmatch("[0-9a-z]{6}", gmail_monitor.installation_id()))
        state_dir = os.path.normcase(os.path.join(app_settings.BASE_DIR, "state"))
        self.assertFalse(os.path.normcase(app_settings.APP_DATA_DIR).startswith(state_dir))

    def test_created_once_then_reused_by_later_processes(self):
        first = gmail_monitor.installation_id()
        self.assertTrue(re.fullmatch("[0-9a-z]{6}", first))
        with open(self.id_file, encoding="ascii") as handle:
            self.assertEqual(first, handle.read().strip())
        self.assertEqual(first, gmail_monitor.installation_id())
        gmail_monitor._INSTALL_IDS.clear()  # a new process
        self.assertEqual(first, gmail_monitor.installation_id())

    def test_an_invalid_id_file_is_replaced_and_logged(self):
        os.makedirs(os.path.dirname(self.id_file))
        with open(self.id_file, "w", encoding="ascii") as handle:
            handle.write("not an id")
        value = gmail_monitor.installation_id()
        self.assertTrue(re.fullmatch("[0-9a-z]{6}", value))
        with open(self.id_file, encoding="ascii") as handle:
            self.assertEqual(value, handle.read().strip())
        self.assertIn("Replacing an invalid installation id file", self.read_log())


class TempNameTest(unittest.TestCase):
    def test_worst_case_fits_the_filename_budget_reserve(self):
        # The largest SQLite row id: 13 base-36 digits.
        name = gmail_monitor.temp_file_name(2 ** 63 - 1, "zzzzzz")
        self.assertEqual(".gadzzzzzz_1y2p0ij32e8e7", name)
        self.assertLessEqual(len(name), filename_rules.TEMP_NAME_RESERVE)

    def test_never_matched_by_the_cleanup_of_older_versions(self):
        for job_id in (0, 1, 7, 36, 10 ** 11, 2 ** 63 - 1):
            name = gmail_monitor.temp_file_name(job_id, "abc123")
            self.assertFalse(old_cleanup_matches(name), name)
            self.assertTrue(gmail_monitor._TEMP_NAME_RE.fullmatch(name), name)

    def test_installations_never_share_a_temp_name(self):
        self.assertNotEqual(
            gmail_monitor.temp_file_name(5, "aaaaaa"), gmail_monitor.temp_file_name(5, "bbbbbb")
        )
        self.assertEqual(".gadaaaaaa_z", gmail_monitor.temp_file_name(35, "aaaaaa"))


class RenameNoReplaceTest(TempDirTestCase):
    def test_existing_target_raises_and_both_files_stay(self):
        source = os.path.join(self.final, "source.part")
        target = os.path.join(self.final, "report.pdf")
        self.write(source, b"new")
        self.write(target, b"existing")
        with self.assertRaises(FileExistsError):
            gmail_monitor._rename_no_replace(source, target)
        self.assertEqual(b"existing", self.read(target))
        self.assertEqual(b"new", self.read(source))

    @unittest.skipUnless(os.name == "nt", "case-insensitive names are a Windows property")
    def test_a_name_differing_only_in_case_is_the_same_file(self):
        source = os.path.join(self.final, "source.part")
        self.write(source, b"new")
        self.write(os.path.join(self.final, "Report.PDF"), b"existing")
        with self.assertRaises(FileExistsError):
            gmail_monitor._rename_no_replace(source, os.path.join(self.final, "report.pdf"))


class SharedTargetProbeTest(TempDirTestCase):
    def test_shared_target_overwrite_probe_is_fixed(self):
        # Both installations pass their exists check before either writes,
        # then B writes and verifies, then A writes.
        target = os.path.join(self.final, "report.pdf")
        a_target = gmail_monitor._unique_path(target)
        b_target = gmail_monitor._unique_path(target)
        self.assertEqual(a_target, b_target)
        with mock.patch.object(gmail_monitor, "installation_id", return_value="bbbbbb"):
            gmail_monitor._write_bytes_atomic(b"B content", b_target, 2)
        self.assertEqual(b"B content", self.read(b_target))
        with mock.patch.object(gmail_monitor, "installation_id", return_value="aaaaaa"):
            with self.assertRaises(FileExistsError):
                gmail_monitor._write_bytes_atomic(b"A content", a_target, 1)
        self.assertEqual(b"B content", self.read(target))
        self.assertEqual(["report.pdf"], os.listdir(self.final))

    def test_cleanup_of_other_installation_probe_is_fixed(self):
        # A live temp of another installation, whether it runs this version
        # or one from before installation ids, survives this one's startup.
        live_legacy = os.path.join(self.final, ".gmailad_123.tmp")
        live_other = os.path.join(self.final, gmail_monitor.temp_file_name(123, "otherx"))
        for path in (live_legacy, live_other):
            self.write(path, b"Other installation finished writing; rename not yet run")
        with mock.patch.object(gmail_monitor, "installation_id", return_value="mineid"):
            removed = gmail_monitor.cleanup_orphan_temp_files([self.final])
        self.assertEqual(0, removed)
        self.assertTrue(os.path.exists(live_legacy))
        self.assertTrue(os.path.exists(live_other))


class CleanupWithoutInstallIdTest(TempDirTestCase):
    def test_an_unreadable_install_id_does_not_stop_the_startup_cleanup(self):
        # Every temp is then treated as another installation's: only old ones go.
        fresh = os.path.join(self.final, gmail_monitor.temp_file_name(1, "abcdef"))
        old_legacy = os.path.join(self.final, ".gmailad_2.tmp")
        for path in (fresh, old_legacy):
            self.write(path, b"partial")
        old = os.path.getmtime(old_legacy) - gmail_monitor.FOREIGN_TEMP_MAX_AGE - 60
        os.utime(old_legacy, (old, old))
        with mock.patch.object(gmail_monitor, "installation_id", side_effect=PermissionError(13, "denied")):
            self.assertEqual(1, gmail_monitor.cleanup_orphan_temp_files([self.final]))
        self.assertTrue(os.path.exists(fresh))
        self.assertFalse(os.path.exists(old_legacy))
        self.assertIn("Installation id unavailable", self.read_log())


class NoOverwritePlacementTest(TempDirTestCase):
    DATA = b"job-bytes"

    def payload(self):
        return {
            "account_email": "a@example.com",
            "final_dir": self.final,
            "message_id": "m1",
            "attachment_id": "att1",
            "part_id": "",
            "inline": False,
            "filename": "document.pdf",
            "received_date": "20260825",
            "sender_email": "sender@example.com",
            "mail_subject": "subject",
        }

    def journal_path(self, job_id):
        return os.path.join(self.state, f"attachmentjob_{job_id}.json")

    def journal(self, job_id):
        with open(self.journal_path(job_id), encoding="utf-8") as handle:
            return json.load(handle)

    def pinned_name(self):
        return gmail_monitor._fresh_target_path(self.payload(), os.path.abspath(self.final))

    def other_installation_takes(self, targets, job_id, taken_count):
        """_rename_no_replace spy: records (target, journal target) for every
        move and, for the first `taken_count` moves, lets another
        installation place its file at that name just before."""
        real = gmail_monitor._rename_no_replace
        calls = []

        def rename(source, target):
            calls.append((target, self.journal(job_id)["result"]["target_path"]))
            if len(calls) <= taken_count:
                self.write(target, b"other installation %d" % len(calls))
                targets.append(target)
            return real(source, target)

        return calls, mock.patch.object(gmail_monitor, "_rename_no_replace", side_effect=rename)

    def assert_no_temp_left(self):
        self.assertEqual([], [name for name in os.listdir(self.final) if name.startswith(".")])

    def test_name_taken_by_another_installation_moves_on_and_keeps_both(self):
        pinned = self.pinned_name()
        taken = []
        calls, patcher = self.other_installation_takes(taken, 1, 1)
        with patcher:
            result = gmail_monitor.run_attachment_job(1, AttachmentService(self.DATA), self.payload())

        self.assertEqual([pinned], taken)
        self.assertEqual(b"other installation 1", self.read(pinned))
        stem, ext = os.path.splitext(pinned)
        self.assertEqual(f"{stem}(1){ext}", result["target_path"])
        self.assertEqual(os.path.basename(result["target_path"]), result["saved_filename"])
        self.assertEqual(self.DATA, self.read(result["target_path"]))
        self.assertEqual(hashlib.sha256(self.DATA).hexdigest(), result["hash"])
        self.assertEqual({"success": True, "phase": "committed", "result": result}, self.journal(1))
        self.assertIn("was taken meanwhile", self.read_log())
        self.assert_no_temp_left()

    def test_the_journal_names_each_target_before_it_is_tried(self):
        pinned = self.pinned_name()
        taken = []
        calls, patcher = self.other_installation_takes(taken, 2, 2)
        with patcher:
            result = gmail_monitor.run_attachment_job(2, AttachmentService(self.DATA), self.payload())

        self.assertEqual(3, len(calls))
        for target, journaled in calls:
            self.assertEqual(target, journaled)
        stem, ext = os.path.splitext(pinned)
        self.assertEqual([pinned, f"{stem}(1){ext}", f"{stem}(2){ext}"], [target for target, _ in calls])
        self.assertEqual(f"{stem}(2){ext}", result["target_path"])
        self.assertEqual(b"other installation 1", self.read(pinned))
        self.assertEqual(b"other installation 2", self.read(f"{stem}(1){ext}"))
        self.assert_no_temp_left()

    def test_other_move_errors_retry_the_job_without_moving_on(self):
        pinned = self.pinned_name()
        with mock.patch.object(gmail_monitor, "_rename_no_replace", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(PermissionError):
                gmail_monitor.run_attachment_job(3, AttachmentService(self.DATA), self.payload())
        journal = self.journal(3)
        self.assertEqual("prepared", journal["phase"])
        self.assertEqual(pinned, journal["result"]["target_path"])
        self.assertEqual([], os.listdir(self.final))

    def test_committed_or_legacy_journal_still_raises_when_the_name_is_taken_at_the_move(self):
        for label, phase, success in (
            ("committed", "committed", True),
            ("legacy without phase", None, False),
        ):
            with self.subTest(label):
                job_id = 4 if phase else 5
                pinned = os.path.join(os.path.abspath(self.final), f"{label}.pdf")
                marker = {
                    "success": success,
                    "phase": phase,
                    "result": {
                        "hash": hashlib.sha256(self.DATA).hexdigest(),
                        "filename": "document.pdf",
                        "saved_filename": os.path.basename(pinned),
                        "target_path": pinned,
                        "account_email": "a@example.com",
                        "final_dir": os.path.abspath(self.final),
                    },
                }
                if phase is None:
                    del marker["phase"]
                with open(self.journal_path(job_id), "w", encoding="utf-8") as handle:
                    json.dump(marker, handle)
                before = self.read(self.journal_path(job_id))
                taken = []
                _calls, patcher = self.other_installation_takes(taken, job_id, 1)
                with patcher:
                    with self.assertRaises(RuntimeError):
                        gmail_monitor.run_attachment_job(job_id, AttachmentService(self.DATA), self.payload())
                self.assertEqual([pinned], taken)
                self.assertEqual(b"other installation 1", self.read(pinned))
                self.assertEqual(before, self.read(self.journal_path(job_id)))
                self.assert_no_temp_left()

    def test_placement_attempts_are_bounded(self):
        taken = []
        calls, patcher = self.other_installation_takes(taken, 6, 100)
        with patcher:
            with self.assertRaises(RuntimeError) as ctx:
                gmail_monitor.run_attachment_job(6, AttachmentService(self.DATA), self.payload())
        self.assertEqual(gmail_monitor.MAX_PLACEMENT_ATTEMPTS, len(calls))
        self.assertIn("保存できませんでした", str(ctx.exception))
        self.assertEqual("prepared", self.journal(6)["phase"])
        for target in taken:
            self.assertTrue(self.read(target).startswith(b"other installation"))
        self.assert_no_temp_left()

    def test_the_hash_is_still_verified_after_the_move(self):
        real = gmail_monitor._rename_no_replace

        def rename_then_corrupt(source, target):
            real(source, target)
            self.write(target, b"changed on the share")

        with mock.patch.object(gmail_monitor, "_rename_no_replace", side_effect=rename_then_corrupt):
            with self.assertRaises(RuntimeError) as ctx:
                gmail_monitor.run_attachment_job(7, AttachmentService(self.DATA), self.payload())
        self.assertIn("SHA-256", str(ctx.exception))
        self.assertEqual("prepared", self.journal(7)["phase"])


if __name__ == "__main__":
    unittest.main()
