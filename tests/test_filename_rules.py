import os
import tempfile
import unittest

from filename_rules import (
    RESERVED_STEMS,
    filename_budget,
    preview_filename,
    render_filename,
    validate_template,
)


class FilenameRulesTest(unittest.TestCase):
    def test_template_order_and_omission(self):
        name = render_filename(
            "document.pdf",
            "20260825",
            "sender@example.com",
            "件名",
            template="{date}-{original}-{sender}",
            subject_max=50,
        )
        self.assertEqual("20260825-document-sender@example.com.pdf", name)

    def test_invalid_placeholder_is_rejected(self):
        ok, reason = validate_template("{original}_{unknown}")
        self.assertFalse(ok)
        self.assertIn("unknown", reason)

    def test_subject_limit_and_windows_sanitizing(self):
        name = render_filename(
            "a.pdf",
            "20260825",
            "sender@example.com",
            "AB:CDE",
            template="{original}_{subject}",
            subject_max=4,
        )
        self.assertEqual("a_AB_C.pdf", name)

    def test_preview(self):
        value = preview_filename("{date}_{original}")
        self.assertTrue(value.startswith("20260825_物件資料"))
        self.assertTrue(value.endswith(".pdf"))


class FilenameBudgetTest(unittest.TestCase):
    def test_budget_shrinks_for_deep_final_dir(self):
        shallow_dir = os.path.join(tempfile.gettempdir(), "short")
        deep_dir = os.path.join(
            tempfile.gettempdir(),
            *[f"segment_{i:02d}_" + "x" * 20 for i in range(6)],
        )
        self.assertLess(filename_budget(deep_dir), filename_budget(shallow_dir))
        self.assertLess(filename_budget(deep_dir), 255)

    def test_budget_never_below_16(self):
        # An impossibly small limit forces the budget to go negative before
        # the floor is applied.
        self.assertEqual(16, filename_budget(tempfile.gettempdir(), reserve=0, limit=1))

    def test_budget_never_above_255(self):
        # A tiny dir plus a huge limit would otherwise produce a budget far
        # above the Windows filename cap.
        self.assertEqual(
            255,
            filename_budget(tempfile.gettempdir(), reserve=0, limit=100000),
        )


class MaxFilenameLengthTest(unittest.TestCase):
    def test_long_name_truncated_to_exact_budget(self):
        long_subject = "あ" * 300
        name = render_filename(
            "document.pdf",
            "",
            "",
            long_subject,
            template="{subject}",
            subject_max=300,
            max_filename_length=40,
        )
        self.assertEqual(40, len(name))
        self.assertTrue(name.endswith(".pdf"))

    def test_max_filename_length_is_clamped_to_16_255(self):
        # Drive the length from {original}. {subject} cannot reach the 255
        # ceiling because subject_max separately clamps it to 200 characters.
        long_name = "x" * 300 + ".pdf"
        too_small = render_filename(long_name, template="{original}", max_filename_length=1)
        self.assertEqual(16, len(too_small))

        too_large = render_filename(long_name, template="{original}", max_filename_length=100000)
        self.assertEqual(255, len(too_large))

    def test_default_behaviour_unchanged_for_names_that_already_fit(self):
        name = render_filename(
            "invoice.pdf",
            "20260825",
            "sender@example.com",
            "件名",
            template="{original}",
        )
        self.assertEqual("invoice.pdf", name)


class ReservedDeviceNameTest(unittest.TestCase):
    def test_nul_reserved_name_gets_prefixed(self):
        name = render_filename("NUL.pdf", template="{original}")
        self.assertEqual("_NUL.pdf", name)

    def test_every_reserved_stem_is_escaped(self):
        for stem in sorted(RESERVED_STEMS):
            for variant in (stem, stem.lower(), stem.capitalize()):
                name = render_filename(f"{variant}.txt", template="{original}")
                self.assertEqual(f"_{variant}.txt", name)

    def test_non_reserved_stem_is_untouched(self):
        name = render_filename("CONTRACT.txt", template="{original}")
        self.assertEqual("CONTRACT.txt", name)


if __name__ == "__main__":
    unittest.main()
