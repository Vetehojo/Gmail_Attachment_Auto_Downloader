import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import app_settings
from app_settings import (
    AUTH_DWD,
    AUTH_OAUTH,
    encode_dwd_accounts,
    get_account_configs,
    load_allowed_extensions,
    load_excluded_labels,
    normalize_auth_mode,
    parse_csv_setting,
    parse_scan_start,
)


class AppSettingsTest(unittest.TestCase):
    def test_custom_date(self):
        value = parse_scan_start("日付指定", "2026-08-01")
        self.assertEqual(datetime(2026, 8, 1), value)

    def test_relative_periods_are_in_the_past(self):
        now = datetime.now()
        for label in ("今日", "過去3日", "過去7日", "過去30日"):
            with self.subTest(label=label):
                self.assertLessEqual(parse_scan_start(label), now)

    def test_invalid_custom_date_raises(self):
        with self.assertRaises(ValueError):
            parse_scan_start("日付指定", "2026/08/01")

    def test_auth_mode_defaults_to_oauth(self):
        self.assertEqual(AUTH_OAUTH, normalize_auth_mode("anything"))
        self.assertEqual(AUTH_DWD, normalize_auth_mode("DWD"))

    def test_dwd_accounts_round_trip_and_normalize_email(self):
        with tempfile.TemporaryDirectory() as td:
            raw = encode_dwd_accounts([
                {"email": "A@Example.com", "final_dir": os.path.join(td, "A")},
                {"email": "b@example.com", "final_dir": os.path.join(td, "B")},
            ])
            settings = {"auth_mode": "dwd", "dwd_accounts": raw}
            accounts = get_account_configs(settings)
            self.assertEqual(["a@example.com", "b@example.com"], [a["email"] for a in accounts])
            self.assertTrue(all(os.path.isabs(a["final_dir"]) for a in accounts))
            self.assertEqual(2, len(json.loads(raw)))

    def test_duplicate_dwd_account_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                encode_dwd_accounts([
                    {"email": "a@example.com", "final_dir": td},
                    {"email": "A@example.com", "final_dir": td},
                ])

    def test_oauth_account_config_remains_single_account(self):
        settings = {"auth_mode": "oauth", "target_email": "User@Example.com", "final_dir": tempfile.gettempdir()}
        accounts = get_account_configs(settings)
        self.assertEqual(1, len(accounts))
        self.assertEqual("user@example.com", accounts[0]["email"])

    def test_percent_is_literal_not_config_interpolation(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.ini")
            with mock.patch.object(app_settings, "CONFIG_PATH", path):
                app_settings.save_settings({
                    "auth_mode": "oauth",
                    "target_email": "a@example.com",
                    "final_dir": os.path.join(td, "100%complete"),
                    "rename_template": "{original}_100%_{date}",
                })
                loaded = app_settings.load_settings()
            self.assertIn("100%complete", loaded["final_dir"])
            self.assertEqual("{original}_100%_{date}", loaded["rename_template"])

    def test_parse_csv_setting_trims_dedupes_and_drops_blanks(self):
        self.assertEqual(
            ["FAX", "税務"],
            parse_csv_setting(" FAX , 税務 ,FAX,  ,税務 "),
        )

    def test_parse_csv_setting_handles_empty_and_none(self):
        self.assertEqual([], parse_csv_setting(""))
        self.assertEqual([], parse_csv_setting(None))

    def test_load_excluded_labels_defaults_to_empty(self):
        self.assertEqual([], load_excluded_labels({"excluded_labels": ""}))

    def test_load_excluded_labels_parses_csv_and_preserves_order(self):
        self.assertEqual(
            ["FAX", "税務"],
            load_excluded_labels({"excluded_labels": "FAX, 税務, FAX"}),
        )

    def test_load_allowed_extensions_normalizes_case_and_leading_dot(self):
        extensions = load_allowed_extensions({"allowed_extensions": "PDF, .JPG, jpg, .PDF"})
        self.assertEqual({".pdf", ".jpg"}, extensions)

    def test_load_allowed_extensions_falls_back_to_defaults_when_setting_parses_empty(self):
        extensions = load_allowed_extensions({"allowed_extensions": " , ,"})
        expected = set(parse_csv_setting(app_settings.DEFAULTS["allowed_extensions"]))
        self.assertEqual(expected, extensions)
        self.assertTrue(extensions)

    def test_load_allowed_extensions_reads_from_config_file_when_no_settings_given(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.ini")
            with mock.patch.object(app_settings, "CONFIG_PATH", path):
                app_settings.save_settings({"allowed_extensions": "txt, PDF"})
                extensions = load_allowed_extensions()
            self.assertEqual({".txt", ".pdf"}, extensions)


if __name__ == "__main__":
    unittest.main()
