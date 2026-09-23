import unittest
from unittest import mock

import gmail_auth


class FakeCredentials:
    def __init__(self):
        self.subject = None

    def with_subject(self, subject):
        self.subject = subject
        return self


class DwdAuthTest(unittest.TestCase):
    def test_dwd_uses_readonly_scope_and_subject(self):
        fake = FakeCredentials()
        with mock.patch.object(gmail_auth.os.path, "exists", return_value=True), \
             mock.patch.object(
                 gmail_auth.service_account.Credentials,
                 "from_service_account_file",
                 return_value=fake,
             ) as from_file, \
             mock.patch.object(gmail_auth, "build", return_value="SERVICE") as build:
            service = gmail_auth.get_dwd_service("User@Example.com")

        self.assertEqual("SERVICE", service)
        self.assertEqual("user@example.com", fake.subject)
        args, kwargs = from_file.call_args
        self.assertEqual(gmail_auth.SERVICE_ACCOUNT_FILE, args[0])
        self.assertEqual(["https://www.googleapis.com/auth/gmail.readonly"], kwargs["scopes"])
        build.assert_called_once_with("gmail", "v1", credentials=fake, cache_discovery=False)

    def test_dwd_requires_subject(self):
        with self.assertRaises(ValueError):
            gmail_auth.get_dwd_service("")

    def test_runtime_dwd_rejects_account_not_in_gui_config(self):
        with mock.patch.object(
            gmail_auth,
            "get_account_configs",
            return_value=[{"email": "allowed@example.com", "final_dir": "C:/tmp"}],
        ), mock.patch.object(gmail_auth, "load_settings", return_value={"auth_mode": "dwd"}):
            with self.assertRaises(gmail_auth.AccountNotConfiguredError) as ctx:
                gmail_auth.get_gmail_service(
                    account_email="other@example.com",
                    allow_interactive=False,
                    auth_mode="dwd",
                )
        self.assertIn("other@example.com", str(ctx.exception))
        self.assertIsInstance(ctx.exception, gmail_auth.AuthenticationRequiredError)


class VerifyProfileAccountTest(unittest.TestCase):
    def test_exact_match(self):
        ok, actual = gmail_auth.verify_profile_account(
            {"emailAddress": "user@example.com"}, "user@example.com"
        )
        self.assertTrue(ok)
        self.assertEqual("user@example.com", actual)

    def test_case_and_whitespace_difference_still_matches(self):
        ok, actual = gmail_auth.verify_profile_account(
            {"emailAddress": "  User@Example.com  "}, " user@example.com "
        )
        self.assertTrue(ok)
        self.assertEqual("User@Example.com", actual)

    def test_real_mismatch(self):
        ok, actual = gmail_auth.verify_profile_account(
            {"emailAddress": "someone-else@example.com"}, "user@example.com"
        )
        self.assertFalse(ok)
        self.assertEqual("someone-else@example.com", actual)

    def test_missing_email_address_is_ok(self):
        ok, actual = gmail_auth.verify_profile_account({}, "user@example.com")
        self.assertTrue(ok)
        self.assertEqual("", actual)

    def test_missing_expected_is_ok(self):
        ok, actual = gmail_auth.verify_profile_account(
            {"emailAddress": "user@example.com"}, ""
        )
        self.assertTrue(ok)
        self.assertEqual("user@example.com", actual)

    def test_none_profile_is_ok(self):
        ok, actual = gmail_auth.verify_profile_account(None, "user@example.com")
        self.assertTrue(ok)
        self.assertEqual("", actual)


if __name__ == "__main__":
    unittest.main()
