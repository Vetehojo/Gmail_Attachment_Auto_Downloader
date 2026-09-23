"""Settings dialog connection test: it writes nothing and stages its result in
memory only (config.ini, credential copies, token.json, folders, cursors and
the monitor stay untouched). Every path is redirected to a temp folder, the
google libraries are mocked and no Tk window is created."""
import os
import unittest
from unittest import mock

import gmail_app
import gmail_auth
from tests.settings_support import (
    FakeButton,
    FakeCreds,
    FakeServiceAccountCreds,
    FakeWin,
    InlineThread,
    LiveStateTestBase,
    ProfileService,
    Root,
    Var,
)


class OAuthConnectionTestTest(LiveStateTestBase):
    def run_test(self, snapshot):
        before = self.snapshot()
        with mock.patch.object(gmail_auth, "_save_token", side_effect=AssertionError("token.json written")):
            result = gmail_app.run_connection_test(snapshot)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(os.path.exists(self.new_final))
        return result

    def test_installed_client_uses_the_live_token_read_only(self):
        live = self.live_token(valid=False, expired=True)

        result = self.run_test(self.oauth_snapshot(self.installed_client))

        self.from_client_file.assert_not_called()
        self.assertEqual(1, live.refreshed)  # refreshed in memory only
        self.assertEqual("info", result["level"])
        staged = result["staged"]
        self.assertIsNone(staged["creds"])
        self.assertEqual({self.TARGET: self.TARGET}, staged["identities"])
        self.assertIn(gmail_app.SAVED_LATER_TEXT, result["text"])

    def test_same_bytes_at_another_path_count_as_the_installed_client(self):
        self.live_token()
        copy = self.downloaded_client('{"installed": {"client_id": "old"}}')

        result = self.run_test(self.oauth_snapshot(copy))

        self.from_client_file.assert_not_called()
        self.assertIsNone(result["staged"]["creds"])

    def test_new_client_logs_in_and_keeps_the_login_in_memory(self):
        self.live_token()
        client = self.downloaded_client()

        result = self.run_test(self.oauth_snapshot(client, email="login@example.com"))

        self.from_client_file.assert_called_once_with(client, gmail_auth.SCOPES)
        staged = result["staged"]
        self.assertIs(self.flow.run_local_server.return_value, staged["creds"])
        self.assertEqual({"login@example.com": "login@example.com"}, staged["identities"])
        self.assertIn("「保存」を押したときに保存します", result["text"])

    def test_forced_relogin_skips_the_live_token(self):
        live = self.live_token()

        result = self.run_test(self.oauth_snapshot(self.installed_client, email="login@example.com", force_login=True))

        self.from_client_file.assert_called_once_with(self.installed_client, gmail_auth.SCOPES)
        self.assertEqual(0, live.refreshed)
        self.assertIsNotNone(result["staged"]["creds"])

    def test_unusable_live_token_falls_back_to_a_browser_login(self):
        self.live_token(valid=False, expired=True, refresh_error=RuntimeError("invalid_grant"))

        result = self.run_test(self.oauth_snapshot(self.installed_client, email="login@example.com"))

        self.from_client_file.assert_called_once()
        self.assertIn("invalid_grant", result["text"])
        self.assertIn("ブラウザでログインしました", result["text"])
        self.assertIsNotNone(result["staged"]["creds"])

    def test_every_fallback_reason_renders_as_natural_sentences(self):
        then = "そのため、ブラウザでログインしました。"
        installed = "保存済みの認証情報（token.json）"
        staged = "前回のテストでログインした認証情報"
        cases = {
            "no token.json": (None, None, f"{installed}がありません。{then}"),
            "unreadable token.json": (ValueError("bad json"), None,
                                      f"{installed}を読み込めませんでした（bad json）。{then}"),
            "refresh failed": (FakeCreds(self.TARGET, valid=False, expired=True,
                                         refresh_error=RuntimeError("invalid_grant")), None,
                               f"{installed}を更新できませんでした（invalid_grant）。{then}"),
            "invalid token": (FakeCreds(self.TARGET, valid=False, expired=False), None,
                              f"{installed}が無効です。{then}"),
            "earlier login expired": (None, FakeCreds("login@example.com", valid=False, expired=True,
                                                      refresh_error=RuntimeError("expired")),
                                      f"{staged}を更新できませんでした（expired）。{then}"),
            "earlier login invalid": (None, FakeCreds("login@example.com", valid=False, expired=False),
                                      f"{staged}が無効です。{then}"),
        }
        for name, (token, staged_creds, first_line) in cases.items():
            with self.subTest(name):
                self.from_client_file.reset_mock()
                moved = False
                if staged_creds is not None:
                    client = self.downloaded_client()  # not the installed client
                    load = mock.patch.object(gmail_auth.Credentials, "from_authorized_user_file")
                elif token is None:
                    client = self.installed_client
                    load = mock.patch.object(gmail_auth.Credentials, "from_authorized_user_file")
                    os.replace(self.token, self.token + ".away")
                    moved = True
                else:
                    client = self.installed_client
                    load = mock.patch.object(
                        gmail_auth.Credentials, "from_authorized_user_file",
                        **({"side_effect": token} if isinstance(token, Exception) else {"return_value": token}),
                    )
                try:
                    with load:
                        result = self.run_test(self.oauth_snapshot(
                            client, email="login@example.com", staged_creds=staged_creds,
                        ))
                finally:
                    if moved:
                        os.replace(self.token + ".away", self.token)
                self.from_client_file.assert_called_once()
                self.assertEqual(first_line, result["text"].splitlines()[0])
                self.assertNotIn("ませんため", result["text"])
                self.assertNotIn("ですため", result["text"])

    def test_a_staged_login_is_reused_without_another_browser_login(self):
        self.live_token()
        staged = FakeCreds("login@example.com")

        result = self.run_test(self.oauth_snapshot(self.installed_client, email="login@example.com", staged_creds=staged))

        self.from_client_file.assert_not_called()
        self.assertIs(staged, result["staged"]["creds"])

    def test_mismatch_warns_without_claiming_which_mailbox_will_be_read(self):
        self.live_token()

        result = self.run_test(self.oauth_snapshot(self.installed_client, email="other@example.com"))

        self.assertEqual("warning", result["level"])
        self.assertNotIn("今後読み込まれる", result["text"])
        self.assertIn("別のGoogleアカウントでログインし直す", result["text"])
        self.assertIn(gmail_app.SAVED_LATER_TEXT, result["text"])
        # An alias setup can still be saved: the verified mailbox is recorded.
        self.assertEqual({"other@example.com": self.TARGET}, result["staged"]["identities"])

    def test_missing_profile_address_is_an_error(self):
        self.live_token()
        with mock.patch.object(gmail_auth, "build", return_value=ProfileService("")):
            with self.assertRaises(RuntimeError):
                self.run_test(self.oauth_snapshot(self.installed_client))


class DwdConnectionTestTest(LiveStateTestBase):
    def test_uses_the_selected_file_and_the_dialog_accounts_and_writes_nothing(self):
        selected = os.path.join(self.downloads, "sa.json")
        self.write(selected, '{"type": "service_account", "client_email": "new"}')
        before = self.snapshot()
        with mock.patch.object(
            gmail_auth.service_account.Credentials, "from_service_account_file",
            return_value=FakeServiceAccountCreds(fail_for={"b@example.com"}),
        ) as from_file:
            # Neither account is in the saved allowlist (config.ini is still OAuth).
            result = gmail_app.run_connection_test({
                "mode": "dwd",
                "service_account": selected,
                "accounts": ["x@example.com", "b@example.com"],
            })
        self.assertEqual(before, self.snapshot())
        self.assertEqual({selected}, {call.args[0] for call in from_file.call_args_list})
        self.assertEqual("error", result["level"])
        self.assertIn("成功 1 / 失敗 1", result["text"])
        self.assertIn("b@example.com", result["text"])
        self.assertEqual({"x@example.com": "x@example.com"}, result["staged"]["identities"])
        self.assertEqual(selected, result["staged"]["service_account"])


class DialogConnectionTestFlowTest(LiveStateTestBase):
    """SettingsDialog's own test/cancel flow with every Tk object faked."""

    def make_dialog(self, root=None, **values):
        dialog = gmail_app.SettingsDialog.__new__(gmail_app.SettingsDialog)
        dialog.app = mock.Mock()
        dialog.app.root = root or Root()
        dialog.win = FakeWin()
        dialog.initial = False
        dialog.setup_only = False
        dialog.auth_mode = Var(values.get("auth_mode", "oauth"))
        dialog.email = Var(values.get("email", self.TARGET))
        dialog.credentials = Var(values.get("credentials", self.installed_client))
        dialog.service_account = Var(values.get("service_account", ""))
        dialog.dwd_accounts = values.get("dwd_accounts", [])
        dialog.force_login = Var(values.get("force_login", False))
        dialog._staged = None
        dialog._closed = False
        dialog._test_running = False
        dialog._test_generation = 0
        dialog.save_button = FakeButton()
        dialog.oauth_test_button = FakeButton()
        dialog.dwd_test_button = FakeButton()
        return dialog

    def run_dialog_test(self, dialog):
        with mock.patch.object(gmail_app.threading, "Thread", InlineThread), \
             mock.patch.object(gmail_auth, "_save_token", side_effect=AssertionError("token.json written")):
            dialog._connection_test()

    def test_test_then_cancel_leaves_the_live_configuration_byte_identical(self):
        self.live_token()
        client = self.downloaded_client()
        dialog = self.make_dialog(credentials=client, email="login@example.com")
        before = self.snapshot()

        self.run_dialog_test(dialog)

        self.assertIsNotNone(dialog._staged["creds"])
        self.assertEqual([], dialog.app.controller.mock_calls)
        self.assertEqual([("disabled",), ("!disabled",)], dialog.save_button.states)
        dialog.close()
        self.assertIsNone(dialog._staged)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(os.path.exists(self.new_final))

    def test_staged_login_is_dropped_when_the_client_or_address_changes(self):
        self.live_token()
        for change in ("email", "credentials"):
            with self.subTest(change):
                dialog = self.make_dialog(credentials=self.downloaded_client(), email="login@example.com")
                self.run_dialog_test(dialog)
                self.assertIsNotNone(dialog._staged)
                dialog._discard_stale_stage()  # an unchanged value keeps it
                self.assertIsNotNone(dialog._staged)
                getattr(dialog, change).set(os.path.join(self.downloads, "other.json") if change == "credentials"
                                            else "someone@example.com")
                dialog._discard_stale_stage()
                self.assertIsNone(dialog._staged)

    def test_dwd_stage_is_dropped_when_the_service_account_file_changes(self):
        selected = os.path.join(self.downloads, "sa.json")
        self.write(selected, "{}")
        dialog = self.make_dialog(
            auth_mode="dwd", service_account=selected,
            dwd_accounts=[{"email": "x@example.com", "final_dir": self.final}],
        )
        with mock.patch.object(
            gmail_auth.service_account.Credentials, "from_service_account_file",
            return_value=FakeServiceAccountCreds(),
        ):
            self.run_dialog_test(dialog)
        self.assertEqual({"x@example.com": "x@example.com"}, dialog._staged["identities"])
        dialog.service_account.set(self.installed_sa)
        dialog._discard_stale_stage()
        self.assertIsNone(dialog._staged)

    def test_a_result_arriving_after_cancel_is_dropped(self):
        self.live_token()
        root = Root(defer=True)
        dialog = self.make_dialog(root=root, credentials=self.downloaded_client(), email="login@example.com")
        self.run_dialog_test(dialog)
        # Save is disabled while the (browser) login is still running.
        self.assertEqual([("disabled",)], dialog.save_button.states)
        with mock.patch.object(dialog, "_save_plan", side_effect=AssertionError("saved during a test")):
            dialog.save()

        dialog.close()
        for callback in root.pending:
            callback()

        self.assertIsNone(dialog._staged)
        gmail_app.messagebox.showinfo.assert_not_called()
        gmail_app.messagebox.showwarning.assert_not_called()

    def test_a_result_for_changed_inputs_is_not_staged(self):
        self.live_token()
        root = Root(defer=True)
        dialog = self.make_dialog(root=root, credentials=self.downloaded_client(), email="login@example.com")
        self.run_dialog_test(dialog)
        dialog.email.set("someone@example.com")

        for callback in root.pending:
            callback()

        self.assertIsNone(dialog._staged)
        text = gmail_app.messagebox.showinfo.call_args.args[1]
        self.assertIn("この結果は保存に使いません", text)

    def test_a_failed_test_drops_an_earlier_stage(self):
        self.live_token()
        dialog = self.make_dialog(credentials=self.downloaded_client(), email="login@example.com")
        self.run_dialog_test(dialog)
        self.assertIsNotNone(dialog._staged)

        self.flow.run_local_server.side_effect = RuntimeError("browser closed")
        dialog.force_login.set(True)
        self.run_dialog_test(dialog)

        self.assertIsNone(dialog._staged)
        self.assertIn("browser closed", gmail_app.messagebox.showerror.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
