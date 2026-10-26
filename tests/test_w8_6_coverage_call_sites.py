# tests/test_w8_6_coverage_call_sites.py — the census, pinned.
#
# W8-6 part 2. Finding the duplicate computation was the hard part; keeping it
# from coming back is this file's job. It pins WHERE a coverage fraction may be
# computed, and it pins the shared vocabulary between the CLI and the
# standalone verification script so the third consumer cannot drift either.
#
# These tests read source text. That is deliberate: the defect was never a
# wrong number, it was two numbers computed in two places, and only a
# structural assertion can catch a third one being added.
#
# Offline. No network, no token.

import os
import re
import unittest

import _bootstrap  # noqa: F401

import coverage as cov

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def src(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()

def code_only(text: str) -> str:
    """`text` with whole-line and trailing comments removed.

    These tests grep SOURCE, which means a comment that merely NAMES a banned
    construct ("no module may call caps.coverage_counts") would fail the very
    test it documents. Stripping comments keeps the assertion about code while
    letting the code stay explained. Strings are left alone: a banned call
    hidden in a string is still worth flagging.
    """
    out = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if "#" in line and line.count('"') % 2 == 0 and line.count("'") % 2 == 0:
            line = line.split("#", 1)[0]
        out.append(line)
    return "\n".join(out)


class SingleAccounting(unittest.TestCase):

    def test_coverage_exposes_the_one_accounting_api(self):
        # W10: the accounting moved from caps.py to coverage.py, beside
        # group_coverage(). caps.py answers "what may this token do?" and has
        # no business owning a project count.
        for name in ("coverage_counts", "coverage_line", "coverage_consistent",
                     "project_namespace_kind", "POLICY_USER_NAMESPACE"):
            self.assertTrue(hasattr(cov, name), name)

    def test_caps_keeps_no_copy_and_no_forwarding_shim(self):
        # Not merely "caps no longer defines it": caps must not RE-EXPORT it
        # either. Two live import paths for one function is how one address
        # gets fixed and the other drifts.
        import caps
        for name in ("coverage_counts", "coverage_line", "coverage_consistent",
                     "project_namespace_kind", "POLICY_USER_NAMESPACE"):
            self.assertFalse(hasattr(caps, name), f"caps still exposes {name}")

    def test_the_accounting_needs_no_network_or_api_module(self):
        # The accounting must stay pure data-in/string-out: no api, no caps,
        # no HTTP. The orphan helpers below it DO call the API, so slice the
        # section rather than reading to end of file.
        body = src("coverage.py").split("Coverage accounting", 1)[1]
        body = code_only(body.split("Orphaned clones", 1)[0])
        for forbidden in ("import api", "import caps", "import requests",
                          "requests.", "caps."):
            self.assertNotIn(forbidden, body)

    def test_the_two_denominators_are_each_documented(self):
        body = src("coverage.py")
        self.assertIn("user namespace", body.lower())
        self.assertIn("simple=true", body.lower())


class VocabularyIsShared(unittest.TestCase):

    def test_policy_string_matches_the_verification_script(self):
        # scripts/verify_mirror.py labels the same exclusion
        # "user_namespace_not_mirrored". The user-facing wording in caps.py
        # must describe the SAME policy, or the CLI and the script are once
        # again answering the same question two ways.
        script = src("scripts", "verify_mirror.py")
        self.assertIn("user_namespace_not_mirrored", script)
        self.assertIn("user-namespace", cov.POLICY_USER_NAMESPACE)
        self.assertIn("policy", cov.POLICY_USER_NAMESPACE)

    def test_verify_script_still_separates_present_missing_excluded(self):
        # The script's three-way split is the model the CLI now mirrors:
        # present + missing + excluded == remote total. If the script stops
        # excluding user namespaces, the CLI's clause becomes a lie.
        script = src("scripts", "verify_mirror.py")
        self.assertIn("exclude_user_namespace", script)
        self.assertIn("present_plus_missing_plus_excluded", script)


class CallSiteCensus(unittest.TestCase):
    """Every place a coverage fraction reaches the user, enumerated."""

    # file -> the marker that identifies its fraction-printing site
    SITES = {
        "coverage.py": "projects on disk",       # the canonical accounting
        "setup.py": "projects on disk",          # Doctor
        "gitlab_cli.py": "projects on disk",     # group list
    }

    def test_known_sites_still_exist(self):
        for path, marker in self.SITES.items():
            self.assertIn(marker, src(path), path)

    def test_no_new_module_grew_its_own_projects_on_disk_line(self):
        # A third bare fraction is the regression this whole wave exists to
        # prevent. Adding one means adding it to SITES deliberately -- and
        # routing it through coverage.coverage_line() first.
        offenders = []
        for name in sorted(os.listdir(ROOT)):
            if not name.endswith(".py") or name in self.SITES:
                continue
            path = os.path.join(ROOT, name)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            if "projects on disk" in text:
                offenders.append(name)
        self.assertEqual(offenders, [], f"new coverage print site(s): {offenders}")

    def test_coverage_module_still_returns_group_scoped_totals_only(self):
        # coverage.group_coverage's denominator is a GROUP's recursive project
        # list -- it can never see a user-namespace project. That is why the
        # menu's total is 450 and not 480, and it must stay explicit.
        body = src("coverage.py")
        self.assertIn("def group_coverage", body)
        self.assertIn("remote_projects", body)
        self.assertIn("get_group_projects_recursive", body)


class RenderedLineShape(unittest.TestCase):

    def test_line_is_one_line(self):
        c = cov.coverage_counts(
            [{"path_with_namespace": "a/b", "namespace": {"kind": "group"}},
             {"path_with_namespace": "u/c", "namespace": {"kind": "user"}}],
            {("h", "a/b")}, "h")
        line = cov.coverage_line(c)
        self.assertNotIn("\n", line)

    def test_line_contains_no_ansi_escapes(self):
        # Colour is the caller's business; the accounting must stay printable
        # into a log file, a report and a terminal alike.
        c = cov.coverage_counts([], set(), "h")
        self.assertIsNone(re.search(r"\x1b\[", cov.coverage_line(c)))


if __name__ == "__main__":
    unittest.main()
