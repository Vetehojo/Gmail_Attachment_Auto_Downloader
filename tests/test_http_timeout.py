"""K1: every Gmail service the monitor builds has a finite socket timeout.

googleapiclient's build() wraps the credentials in httplib2.Http(timeout=60)
(googleapiclient.http.build_http) when no http object is passed, so a hung
attachment download fails and is retried instead of stalling the worker for
good. Pins that for the OAuth and the DWD build paths; no network access
(the Gmail discovery document is bundled).
"""
import os
import socket
import tempfile
import unittest
from unittest import mock

import httplib2
from google.oauth2.credentials import Credentials

import gmail_auth

MAX_TIMEOUT_SECONDS = 300


class ServiceAccountStub:
    def with_subject(self, subject):
        return Credentials(token="dwd-token")


class HttpTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNone(socket.getdefaulttimeout())
        patcher = mock.patch.object(httplib2.Http, "request", side_effect=AssertionError("network access"))
        patcher.start()
        self.addCleanup(patcher.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = temp.name

    def assert_finite_timeout(self, service):
        timeout = service._http.http.timeout
        self.assertIsNotNone(timeout)
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, MAX_TIMEOUT_SECONDS)

    def test_oauth_service(self):
        token = os.path.join(self.root, "token.json")
        with open(token, "w", encoding="utf-8") as handle:
            handle.write("{}")
        with mock.patch.object(gmail_auth, "TOKEN_FILE", token), \
             mock.patch.object(gmail_auth.Credentials, "from_authorized_user_file",
                               return_value=Credentials(token="oauth-token")):
            service = gmail_auth.get_oauth_service(allow_interactive=False)
        self.assert_finite_timeout(service)

    def test_dwd_service(self):
        key = os.path.join(self.root, "service_account.json")
        with open(key, "w", encoding="utf-8") as handle:
            handle.write("{}")
        with mock.patch.object(gmail_auth.service_account.Credentials, "from_service_account_file",
                               return_value=ServiceAccountStub()):
            service = gmail_auth.build_dwd_service(key, "user@example.com")
        self.assert_finite_timeout(service)


if __name__ == "__main__":
    unittest.main()
