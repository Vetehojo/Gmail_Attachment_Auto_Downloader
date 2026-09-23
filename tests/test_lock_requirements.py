"""D3: requirements.lock pins the whole runtime dependency closure.

Covers tools/lock_requirements.py (marker evaluation, closure walk, output),
the committed lock's shape, and that 1_setup.bat and CI install from it.
"""
import importlib.util
import io
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "lock_requirements", os.path.join(REPO_ROOT, "tools", "lock_requirements.py")
)
lock_requirements = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lock_requirements)

WINDOWS_314 = {
    "implementation_name": "cpython",
    "implementation_version": "3.14.3",
    "os_name": "nt",
    "platform_machine": "AMD64",
    "platform_python_implementation": "CPython",
    "platform_release": "11",
    "platform_system": "Windows",
    "platform_version": "10.0.26200",
    "python_full_version": "3.14.3",
    "python_version": "3.14",
    "sys_platform": "win32",
}


def evaluate(marker, extra=""):
    return lock_requirements.evaluate_marker(marker, dict(WINDOWS_314, extra=extra))


class MarkerTest(unittest.TestCase):
    def test_markers_found_in_the_dependency_metadata(self):
        cases = {
            'python_version >= "3.14"': True,
            'python_version < "3.14"': False,
            'python_full_version < "3.11"': False,
            'sys_platform == "darwin"': False,
            'sys_platform == "linux"': False,
            'platform_python_implementation != "PyPy"': True,
            'implementation_name != "PyPy"': True,
            'platform_python_implementation == "CPython" and extra == "brotli"': False,
            'python_version >= "3.14" and extra == "testing"': False,
            'extra == "docs"': False,
        }
        for marker, expected in cases.items():
            with self.subTest(marker):
                self.assertIs(expected, evaluate(marker))

    def test_extras(self):
        self.assertTrue(evaluate('extra == "socks"', "socks"))
        self.assertTrue(evaluate('extra == "Use_Chardet.on-py3"', "use-chardet-on-py3"))
        self.assertFalse(evaluate('python_version < "3.14" and extra == "socks"', "socks"))

    def test_grammar(self):
        cases = {
            '(sys_platform == "linux" or os_name == "nt") and python_version >= "3.9"': True,
            'sys_platform == "linux" or os_name == "nt" and python_version < "3"': False,
            '"3.14" <= python_version': True,
            'python_version == "3.14.0"': True,
            'python_version == "3.*"': True,
            'python_full_version != "3.14.*"': False,
            'python_full_version ~= "3.14.0"': True,
            'python_full_version ~= "3.13.0"': False,
            'python_version>"3.10"': True,
            "sys_platform in 'win32 cygwin'": True,
            'sys_platform not in "win32 cygwin"': False,
            'platform_system === "Windows"': True,
            'platform_release >= "10"': True,
        }
        for marker, expected in cases.items():
            with self.subTest(marker):
                self.assertIs(expected, evaluate(marker))

    def test_anything_unknown_fails_loudly(self):
        for marker in (
            'python_version >= "3.14" and',
            'unknown_variable == "x"',
            'python_version >= "3.14" python_version',
            '(python_version >= "3.14"',
            'sys_platform < "win32"',
            'python_version ~= "3"',
            'python_version = "3.14"',
            'python_version >= "3.14" and nonsense',
        ):
            with self.subTest(marker):
                with self.assertRaises(ValueError):
                    evaluate(marker)


class ParseTest(unittest.TestCase):
    def test_requirement_forms(self):
        parse = lock_requirements.parse_requirement
        self.assertEqual(("pyasn1-modules", set(), ((">=", "0.2.1"),), ""), parse("pyasn1_modules>=0.2.1"))
        self.assertEqual(
            ("pyobjc-framework-quartz", set(), ((">=", "7.0"),), 'sys_platform == "darwin"'),
            parse('pyobjc-framework-Quartz (>=7.0) ; sys_platform == "darwin"'),
        )
        self.assertEqual(
            ("requests", {"socks", "use-chardet-on-py3"}, ((">=", "2"),), ""),
            parse("requests[socks, use_chardet_on_py3]>=2"),
        )
        self.assertEqual(("pillow", set(), ((">=", "12"), ("<", "13")), ""), parse("Pillow>=12,<13"))
        self.assertEqual(("oauthlib", set(), ((">=", "3.0.0"),), ""), parse("oauthlib >=3.0.0"))
        self.assertEqual(("six", set(), (), ""), parse("six"))
        self.assertEqual(
            (("!=", "2.0.*"), ("!=", "2.3.0"), ("<", "3.0.0"), (">=", "1.31.5")),
            parse("google-api-core!=2.0.*,!=2.3.0,<3.0.0,>=1.31.5")[2],
        )
        for bad in (">=1.0", "name @ https://example.com/x.whl", "name>=", "name=1.0", "name>=1,,<2"):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    parse(bad)

    def test_satisfies(self):
        satisfies = lock_requirements.satisfies
        clauses = lock_requirements.parse_requirement("x!=2.0.*,!=2.3.0,<3.0.0,>=1.31.5")[2]
        for version, expected in (("2.34.0", True), ("1.31.5", True), ("1.31.4", False), ("2.0.9", False),
                                  ("2.3.0", False), ("2.3", False), ("3.0", False), ("2.99", True)):
            with self.subTest(version):
                self.assertIs(expected, satisfies(version, clauses))
        self.assertTrue(satisfies("1.4.1", (("~=", "1.4"),)))
        self.assertFalse(satisfies("2.0.0", (("~=", "1.4"),)))
        self.assertTrue(satisfies("anything", ()))
        with self.assertRaises(ValueError):
            satisfies("2.0.0rc1", ((">=", "1"),))

    def test_prefix_ranges_pad_the_version_with_zeros(self):
        satisfies = lock_requirements.satisfies
        for version, spec, expected in (
            ("2", "!=2.0.*", False), ("2", "==2.0.*", True), ("2", "==2.0.0.*", True),
            ("2.0", "!=2.0.*", False), ("2.1", "!=2.0.*", True), ("2.1", "==2.0.*", False),
            ("2", "==2.*", True), ("20", "==2.*", False), ("3", "!=2.0.*", True),
        ):
            with self.subTest(version=version, spec=spec):
                self.assertIs(expected, satisfies(version, lock_requirements.parse_requirement("x" + spec)[2]))
        self.assertIs(True, evaluate('python_version == "3.14.*"'))
        self.assertIs(False, evaluate('python_version != "3.14.0.*"'))

    def test_prefix_ranges_refuse_versions_they_cannot_order(self):
        for version, spec in (("2.0rc1", "!=2.0.*"), ("2.0rc1", "==2.0.*"), ("2.0", "==2.x.*")):
            with self.subTest(version=version, spec=spec):
                with self.assertRaisesRegex(ValueError, "cannot compare"):
                    lock_requirements.satisfies(version, lock_requirements.parse_requirement("x" + spec)[2])
        with self.assertRaisesRegex(ValueError, "cannot compare"):
            evaluate('platform_system == "Windows.*"')

    def test_requirements_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "requirements.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("# 日本語のコメント\n\nPillow>=12,<13  # inline\npystray>=0.19.5,<1\n")
            self.assertEqual(
                [
                    ("pillow", set(), ((">=", "12"), ("<", "13")), ""),
                    ("pystray", set(), ((">=", "0.19.5"), ("<", "1")), ""),
                ],
                lock_requirements.read_requirements(path),
            )
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("-r other.txt\n")
            with self.assertRaises(ValueError):
                lock_requirements.read_requirements(path)

    def test_the_real_requirements_file_parses_with_ranges(self):
        requirements = lock_requirements.read_requirements(os.path.join(REPO_ROOT, "requirements.txt"))
        self.assertEqual(6, len(requirements))
        for name, _extras, clauses, _marker in requirements:
            with self.subTest(name):
                self.assertEqual([">=", "<"], [op for op, _version in clauses])

    def test_lock_text(self):
        self.assertEqual(
            {"pyasn1-modules": "0.4.2", "six": "1.17.0"},
            lock_requirements.parse_lock("# header\n\npyasn1_modules==0.4.2\r\nsix==1.17.0\n"),
        )
        for bad in ("six>=1.17.0", "six==", "-r x.txt"):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    lock_requirements.parse_lock(bad)


def req(name, spec="", marker=""):
    """A read_requirements tuple."""
    return lock_requirements.parse_requirement(f"{name}{spec}" + (f"; {marker}" if marker else ""))


class FakeDistributions:
    def __init__(self, packages):
        self.packages = packages

    def __call__(self, name):
        if name not in self.packages:
            raise LookupError(f"{name} is not installed")
        return self.packages[name]


class ClosureTest(unittest.TestCase):
    def resolve(self, top_level, packages):
        return lock_requirements.resolve_closure(top_level, WINDOWS_314, FakeDistributions(packages))

    def versions(self, top_level, packages):
        return self.resolve(top_level, packages)[0]

    def test_markers_decide_what_is_included(self):
        packages = {
            "app-lib": ("1.0", [
                "shared>=1",
                'mac-only>=1 ; sys_platform == "darwin"',
                'old-python-only; python_version < "3.14"',
                'new-python-only; python_version >= "3.14"',
                'docs-tool; extra == "docs"',
            ]),
            "shared": ("2.0", ["app-lib"]),  # a cycle ends
            "new-python-only": ("3.0", []),
        }
        self.assertEqual(
            {"app-lib": "1.0", "shared": "2.0", "new-python-only": "3.0"},
            self.versions([req("app-lib")], packages),
        )

    def test_edges_carry_every_requirer_and_range(self):
        packages = {
            "top": ("1.2", ["dep>=3,<4", 'mac-only; sys_platform == "darwin"']),
            "dep": ("3.4", []),
        }
        _versions, edges = self.resolve([req("top", ">=1")], packages)
        self.assertEqual(
            {
                ("requirements.txt", "top", ((">=", "1"),)),
                ("top 1.2", "dep", ((">=", "3"), ("<", "4"))),
            },
            edges,
        )

    def test_requested_extras_add_their_dependencies(self):
        packages = {
            "top": ("1", ["client[socks]"]),
            "client": ("2", ['pysocks; extra == "socks"', 'brotli; extra == "brotli"']),
            "pysocks": ("3", []),
        }
        self.assertEqual({"top": "1", "client": "2", "pysocks": "3"}, self.versions([req("top")], packages))

    def test_an_extra_requested_after_the_plain_package_is_still_walked(self):
        # Both orders, so one of them walks "client" first without the extra.
        for order in (["client", "b"], ["b", "client"]):
            with self.subTest(order):
                packages = {
                    "a": ("1", order),
                    "b": ("1", ["client[socks]"]),
                    "client": ("2", ['pysocks; extra == "socks"']),
                    "pysocks": ("3", []),
                }
                self.assertIn("pysocks", self.versions([req("a")], packages))

    def test_top_level_markers_are_evaluated(self):
        packages = {"win": ("1", [])}
        top = [req("win", marker='os_name == "nt"'), req("mac", marker='sys_platform == "darwin"')]
        self.assertEqual({"win": "1"}, self.versions(top, packages))

    def test_a_missing_package_is_an_error(self):
        with self.assertRaises(LookupError):
            self.resolve([req("top")], {"top": ("1", ["absent"])})

    def test_unsatisfied(self):
        edges = {
            ("requirements.txt", "top", ((">=", "2"),)),
            ("top 1.2", "dep", ((">=", "3"), ("<", "4"))),
        }
        self.assertEqual([], lock_requirements.unsatisfied(edges, {"top": "2.0", "dep": "3.9"}, "pinned in the lock"))
        self.assertEqual(
            [
                "requirements.txt requires top>=2, but top 1.2 is pinned in the lock",
                "top 1.2 requires dep>=3,<4, but dep is not pinned in the lock",
            ],
            lock_requirements.unsatisfied(edges, {"top": "1.2"}, "pinned in the lock"),
        )

    def test_render_is_sorted_pinned_and_headed(self):
        text = lock_requirements.render_lock({"zeta": "1.0", "alpha-beta": "2.0"})
        lines = text.splitlines()
        self.assertEqual(list(lock_requirements.HEADER), lines[:len(lock_requirements.HEADER)])
        self.assertEqual(["alpha-beta==2.0", "zeta==1.0"], lines[len(lock_requirements.HEADER):])
        self.assertTrue(text.endswith("\n"))


class MainTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.requirements = os.path.join(temp.name, "requirements.txt")
        self.lock = os.path.join(temp.name, "requirements.lock")
        self.write_requirements("top>=1,<2\n")
        self.packages = {"top": ("1.2", ["dep>=3,<4"]), "dep": ("3.4", [])}
        for patcher in (
            mock.patch.object(lock_requirements, "REQUIREMENTS_TXT", self.requirements),
            mock.patch.object(lock_requirements, "REQUIREMENTS_LOCK", self.lock),
            mock.patch.object(lock_requirements, "current_environment", return_value=dict(WINDOWS_314)),
            mock.patch.object(lock_requirements, "installed_distribution", side_effect=self.distribution),
            # Whatever runs the tests counts as supported here.
            mock.patch.object(lock_requirements, "SUPPORTED", (sys.platform, tuple(sys.version_info[:2]))),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_requirements(self, text):
        with open(self.requirements, "w", encoding="utf-8") as handle:
            handle.write(text)

    def distribution(self, name):
        if name not in self.packages:
            raise LookupError(f"{name} is not installed")
        return self.packages[name]

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = lock_requirements.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def read_lock(self):
        with open(self.lock, "rb") as handle:
            return handle.read()

    def write_lock(self, data):
        with open(self.lock, "wb") as handle:
            handle.write(data)

    def test_write_then_check(self):
        self.assertEqual(1, self.run_main("--check")[0])  # no lock yet
        self.assertEqual(0, self.run_main()[0])
        data = self.read_lock()
        self.assertNotIn(b"\r", data)
        self.assertTrue(data.endswith(b"dep==3.4\ntop==1.2\n"))
        code, out, _err = self.run_main("--check")
        self.assertEqual(0, code)
        self.assertIn("within every range", out)

    def test_check_fails_on_a_missing_stale_or_changed_pin_and_writes_nothing(self):
        self.run_main()
        good = self.read_lock()
        for label, text in (
            ("missing dependency", good.replace(b"dep==3.4\n", b"")),
            ("extra pin", good + b"unrelated==1.0\n"),
            ("other version", good.replace(b"dep==3.4", b"dep==3.3")),
        ):
            with self.subTest(label):
                self.write_lock(text)
                code, out, err = self.run_main("--check")
                self.assertEqual(1, code)
                self.assertIn("not the dependency closure", err)
                self.assertTrue(out.startswith("--- requirements.lock"))
                self.assertEqual(text, self.read_lock())

    def test_installed_version_outside_the_requirements_range_fails_both_modes(self):
        # The review's range probe: requirements.txt raised, lock not regenerated.
        self.run_main()
        before = self.read_lock()
        self.write_requirements("top>=2,<3\n")
        for argv in ((), ("--check",)):
            with self.subTest(argv):
                code, _out, err = self.run_main(*argv)
                self.assertEqual(1, code)
                self.assertIn("requirements.txt requires top>=2,<3, but top 1.2 is installed", err)
        self.assertEqual(before, self.read_lock())
        _code, _out, err = self.run_main("--check")
        self.assertIn("requirements.txt requires top>=2,<3, but top 1.2 is pinned in requirements.lock", err)

    def test_installed_dependency_outside_its_requirers_range_is_not_written(self):
        self.packages["dep"] = ("4.0", [])
        code, _out, err = self.run_main()
        self.assertEqual(1, code)
        self.assertIn("top 1.2 requires dep>=3,<4, but dep 4.0 is installed", err)
        self.assertIn("requirements.lock was not written", err)
        self.assertFalse(os.path.exists(self.lock))

    def test_a_lock_pin_outside_a_requirers_range_fails_the_check(self):
        self.run_main()
        good = self.read_lock()
        for label, text, message in (
            ("transitive", good.replace(b"dep==3.4", b"dep==4.1"),
             "top 1.2 requires dep>=3,<4, but dep 4.1 is pinned in requirements.lock"),
            ("top level", good.replace(b"top==1.2", b"top==2.0"),
             "requirements.txt requires top>=1,<2, but top 2.0 is pinned in requirements.lock"),
            ("missing", good.replace(b"dep==3.4\n", b""),
             "top 1.2 requires dep>=3,<4, but dep is not pinned in requirements.lock"),
        ):
            with self.subTest(label):
                self.write_lock(text)
                code, _out, err = self.run_main("--check")
                self.assertEqual(1, code)
                self.assertIn(message, err)

    def test_a_malformed_lock_line_fails_the_check(self):
        self.run_main()
        self.write_lock(self.read_lock() + b"dep>=3\n")
        code, _out, err = self.run_main("--check")
        self.assertEqual(1, code)
        self.assertIn("not name==version", err)

    def test_crlf_checkout_still_matches(self):
        self.run_main()
        crlf = self.read_lock().replace(b"\n", b"\r\n")
        self.write_lock(crlf)
        self.assertEqual(0, self.run_main("--check")[0])

    def test_uninstalled_package_is_reported(self):
        del self.packages["dep"]
        code, _out, err = self.run_main()
        self.assertEqual(1, code)
        self.assertIn("dep is not installed", err)
        self.assertFalse(os.path.exists(self.lock))

    def test_other_python_is_refused(self):
        with mock.patch.object(lock_requirements, "SUPPORTED", ("win32", (2, 7))):
            code, _out, err = self.run_main()
        self.assertEqual(2, code)
        self.assertIn("Python 3.14 on Windows", err)
        self.assertFalse(os.path.exists(self.lock))


class CommittedLockTest(unittest.TestCase):
    def test_lock_shape(self):
        with open(os.path.join(REPO_ROOT, "requirements.lock"), "rb") as handle:
            data = handle.read()
        text = data.decode("ascii").replace("\r\n", "\n")
        lines = text.splitlines()
        self.assertEqual(list(lock_requirements.HEADER), lines[:len(lock_requirements.HEADER)])
        names = []
        for line in lines[len(lock_requirements.HEADER):]:
            match = re.fullmatch(r"([a-z0-9]+(?:-[a-z0-9]+)*)==([0-9][0-9A-Za-z.+!-]*)", line)
            self.assertIsNotNone(match, line)
            names.append(match.group(1))
        self.assertEqual(sorted(set(names)), names)

    def test_every_requirements_range_holds_for_the_locked_version(self):
        with open(os.path.join(REPO_ROOT, "requirements.lock"), encoding="ascii") as handle:
            pins = lock_requirements.parse_lock(handle.read())
        edges = {
            ("requirements.txt", name, clauses)
            for name, _extras, clauses, _marker in
            lock_requirements.read_requirements(os.path.join(REPO_ROOT, "requirements.txt"))
        }
        self.assertEqual([], lock_requirements.unsatisfied(edges, pins, "pinned in requirements.lock"))


class InstallCommandsTest(unittest.TestCase):
    def read(self, rel):
        with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as handle:
            return handle.read()

    def test_1_setup_bat_installs_from_the_lock(self):
        lines = [line.strip() for line in self.read("1_setup.bat").splitlines()]
        pip = [line for line in lines if " pip " in f" {line} "]
        self.assertEqual(["python -m pip install -r requirements.lock"], pip)
        index = lines.index(pip[0])
        self.assertEqual('set "PIP_RESULT=%ERRORLEVEL%"', lines[index + 1])

    def test_ci_installs_the_lock_without_resolving_first_and_checks_it(self):
        runs = [line.strip()[len("run:"):].strip() for line in self.read(".github/workflows/tests.yml").splitlines()
                if line.strip().startswith("run:")]
        # One command per step: a failing command must fail its step.
        self.assertEqual(
            [
                "python -m pip install --no-deps -r requirements.lock",
                "python -m pip check",
                "python tools/lock_requirements.py --check",
                "python -m pip install -r requirements.lock",
                "python -m compileall -q app",
                "python -m unittest discover -s tests -t . -v",
            ],
            runs,
        )


if __name__ == "__main__":
    unittest.main()
