"""Keep the affected emSign root out of the app's default TLS transports.

GHSA-6q48-6c7w-9gw2 identifies emSign ECC Root CA - G3; certifi
2026.10.19 removes that root while retaining the unaffected emSign roots.
All transport requests below stop before network I/O and use no credentials.
"""
import hashlib
import importlib.metadata
import io
from pathlib import Path
import ssl
import unittest
from unittest import mock

import certifi
from google.auth.credentials import AnonymousCredentials
import httplib2
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.response import HTTPResponse

import gmail_auth
from tools import lock_requirements


REPO_ROOT = Path(__file__).resolve().parents[1]
MIN_CERTIFI = "2026.10.19"
# SHA-256 of the DER certificate, independent of PEM formatting/comments:
# https://github.com/advisories/GHSA-6q48-6c7w-9gw2
# https://knowledge.workspace.google.com/admin/gmail/advanced/ca-certificates-trusted-by-gmail-for-s-mime
EMSIGN_ECC_G3_SHA256 = "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b"


class CaBundleTest(unittest.TestCase):
    def setUp(self):
        for target in ("socket.getaddrinfo", "socket.socket.connect"):
            patcher = mock.patch(target, side_effect=AssertionError("network access"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def assert_safe_context(self, context):
        self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)
        self.assertTrue(context.check_hostname)
        roots = context.get_ca_certs(binary_form=True)
        self.assertTrue(roots, "the selected trust store must not be empty")
        self.assertFalse(
            any(hashlib.sha256(der).hexdigest() == EMSIGN_ECC_G3_SHA256 for der in roots),
            "the trust store still contains emSign ECC Root CA - G3",
        )

    def test_certifi_floor_applies_to_direct_requirement_lock_and_install(self):
        requirements = lock_requirements.read_requirements(REPO_ROOT / "requirements.txt")
        certifi_requirements = [item for item in requirements if item[0] == "certifi"]
        self.assertEqual(1, len(certifi_requirements), "certifi needs an explicit security floor")
        _name, _extras, clauses, marker = certifi_requirements[0]
        self.assertFalse(marker, "the security floor must apply on every platform")
        self.assertIn((">=", MIN_CERTIFI), clauses)
        for old in ("2026.7.22", "2026.10.18"):
            self.assertFalse(lock_requirements.satisfies(old, clauses), old)
        pins = lock_requirements.parse_lock((REPO_ROOT / "requirements.lock").read_text("ascii"))
        for version in (pins["certifi"], importlib.metadata.version("certifi")):
            self.assertTrue(lock_requirements.satisfies(version, ((">=", MIN_CERTIFI),)), version)

    def test_installed_bundle_excludes_the_affected_root(self):
        self.assert_safe_context(ssl.create_default_context(cafile=certifi.where()))

    def test_google_auth_requests_uses_fixed_certifi_bundle(self):
        request = gmail_auth.Request()
        self.addCleanup(request.session.close)
        # Exercise default CA selection independently of local proxy/CA overrides.
        request.session.trust_env = False
        response = HTTPResponse(body=io.BytesIO(b""), status=200, preload_content=False)
        with mock.patch.object(HTTPSConnectionPool, "urlopen", autospec=True, return_value=response) as send:
            self.assertEqual(200, request("https://example.invalid/").status)
        pool = send.call_args.args[0]
        self.assertEqual("CERT_REQUIRED", pool.cert_reqs)
        self.assertTrue(Path(pool.ca_certs).samefile(certifi.where()))
        self.assert_safe_context(ssl.create_default_context(cafile=pool.ca_certs))

    def test_google_api_httplib2_loads_fixed_bundle_into_tls_context(self):
        service = gmail_auth.build(
            "gmail", "v1", credentials=AnonymousCredentials(), cache_discovery=False
        )
        self.addCleanup(service.close)
        http = service._http.http
        http.proxy_info = None
        response = httplib2.Response({"status": "200"})
        with mock.patch.object(httplib2.Http, "_conn_request", autospec=True,
                               return_value=(response, b"")) as send:
            self.assertEqual(200, service._http.request("https://example.invalid/")[0].status)
        connection = send.call_args.args[1]
        self.assertTrue(Path(connection.ca_certs).samefile(certifi.where()))
        self.assertFalse(connection.disable_ssl_certificate_validation)
        self.assert_safe_context(connection._context)


if __name__ == "__main__":
    unittest.main()
