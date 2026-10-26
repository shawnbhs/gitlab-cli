# tests/test_w8_1_since_validation.py — W8-1.
#
# THE BUG
# -------
# The Log screen prompted `Since:` and handed the answer straight to
# `git log --since=<value>`. git's approxidate parser has NO error channel: an
# unparseable string silently becomes "now", git exits 0 and prints nothing.
# So typing `b` on a repository with 19 commits produced
#
#     No commits found since 'b'
#
# — a confident false negative about the user's own data. Same shape as every
# other bug in this project: computed one way, reported as if verified another.
#
# These tests pin the three outcomes apart:
#   1. garbage            -> refused, nothing queried, no "no commits" claim
#   2. real filter, empty -> says so AND reports how many repos DO have history
#   3. real filter, hits  -> unchanged tree output
#
# Every fixture is a REAL git repository (via _bootstrap), and the date probe
# is the REAL git binary — the bug was about what git actually does.

import io
import os
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stdout

import _bootstrap  # noqa: F401  (path setup + error-log isolation)
from _bootstrap import TempTreeTestCase, git

import log


ANSI = re.compile(r"\033\[[0-9;]*m")


def strip(text):
    return ANSI.sub("", text)


def make_old_repo(case, relpath, when="2020-01-01T00:00:00"):
    """A real repo whose single commit is OLD in both author and commit date.

    The fixtures in _bootstrap commit at 'now', which makes 'now'-shaped
    filters indistinguishable from real ones — the exact confusion under test.
    """
    saved = {k: os.environ.get(k)
             for k in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE")}
    os.environ["GIT_AUTHOR_DATE"] = when
    os.environ["GIT_COMMITTER_DATE"] = when
    try:
        return case.make_repo(relpath)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def result_lines(out):
    """The lines that are a CLAIM ABOUT THE USER'S COMMITS.

    The refusal message quotes the old wording ("No commits found since 'b'")
    while explaining it, so a naive substring check is not enough: what must
    never appear is that sentence PRESENTED AS THE RESULT, i.e. as its own
    warning line.
    """
    return [ln.strip() for ln in out.splitlines()
            if ln.strip().startswith(("⚠", "📊"))]


def claims_no_commits(out):
    return any(ln.lstrip("⚠ ").startswith("No commits")
               for ln in result_lines(out))


def run_cmd_log(dest, since, author=None, group="grp"):
    buf = io.StringIO()
    with redirect_stdout(buf):
        log.cmd_log({"folder": None}, group, dest, since, author)
    return strip(buf.getvalue())


class SinceValidationTests(TempTreeTestCase):
    """validate_since() separates 'not a date' from 'no results'."""

    GARBAGE = ["b", "not a date", "banana", "qq", "zzz nope"]

    LEGITIMATE = [
        "2 weeks ago",
        "yesterday",
        "2026-01-01",
        "3.days",
        "2026-01-01T10:00:00",
        "1 day ago",
        "10 days",
        "1d", "7d", "2w", "1m", "1y",
        "now",
        "today",
        "midnight",
        "@1700000000",
        "last monday",
        "1 year 2 months ago",
    ]

    def test_garbage_is_refused_not_answered(self):
        for value in self.GARBAGE:
            with self.subTest(value=value):
                with self.assertRaises(log.SinceError):
                    log.validate_since(value)

    def test_legitimate_git_syntax_is_never_rejected(self):
        """A false rejection is just the false negative wearing a hat."""
        for value in self.LEGITIMATE:
            with self.subTest(value=value):
                parsed, epoch, note = log.validate_since(value)
                self.assertTrue(parsed.strip())
                self.assertTrue(note)

    def test_empty_and_whitespace_are_refused(self):
        for value in ("", "   ", "\t"):
            with self.subTest(value=value):
                with self.assertRaises(log.SinceError):
                    log.validate_since(value)

    def test_option_looking_value_is_refused(self):
        for value in ("--all", "-n", "--since=now", "--pretty=x"):
            with self.subTest(value=value):
                with self.assertRaises(log.SinceError):
                    log.validate_since(value)

    def test_control_characters_are_refused(self):
        for value in ("2 weeks ago\nrm -rf", "a\0b"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(log.SinceError):
                    log.validate_since(value)

    def test_shell_metacharacters_cannot_reach_a_shell(self):
        """`;` and a backtick are either refused or inert — never executed."""
        canary = os.path.join(self.tmp, "PWNED")
        payloads = [
            "x;y`id`",
            "`touch %s`" % canary,
            "$(touch %s)" % canary,
            "; touch %s" % canary,
            "2 weeks ago; touch %s" % canary,
        ]
        for value in payloads:
            with self.subTest(value=value):
                try:
                    log.validate_since(value)
                except log.SinceError:
                    pass
                self.assertFalse(
                    os.path.exists(canary),
                    "a shell metacharacter reached a shell: %r" % value)

    def test_the_resolved_absolute_date_is_returned_for_echoing(self):
        """Requirement 4: the user must see what was actually applied."""
        parsed, epoch, note = log.validate_since("2 weeks ago")
        self.assertIsNotNone(epoch)
        # note carries a real YYYY-MM-DD HH:MM stamp
        self.assertRegex(note, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")

    def test_a_loosely_interpreted_value_is_flagged_as_such(self):
        """Accepted-but-odd input is accepted WITH a visible caveat."""
        parsed, epoch, note = log.validate_since("2026-13-45")
        self.assertIn("loosely", note.lower())

    def test_shortcut_expansion_still_works(self):
        self.assertEqual(log.parse_since("2w"), "2 weeks ago")
        self.assertEqual(log.parse_since("1d"), "1 day ago")
        parsed, _e, _n = log.validate_since("2w")
        self.assertEqual(parsed, "2 weeks ago")


class GitReallyIsSilentTests(TempTreeTestCase):
    """The premise, measured — not assumed."""

    def test_git_exits_zero_and_prints_nothing_for_garbage(self):
        repo = make_old_repo(self, "g/p")
        r = subprocess.run(
            ["git", "-C", repo, "log", "--all", "--since=b", "--format=%H"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0,
                         "premise changed: git now errors on a bad date")
        self.assertEqual(r.stdout.strip(), "",
                         "premise changed: git now returns commits for 'b'")
        # ...on a repository that demonstrably HAS a commit.
        r2 = subprocess.run(
            ["git", "-C", repo, "log", "--all", "--format=%H"],
            capture_output=True, text=True, timeout=60)
        self.assertTrue(r2.stdout.strip())

    def test_garbage_resolves_to_exactly_now(self):
        """Door B's mechanism, verified against the real binary."""
        repo = self.make_repo("g/p")
        gitdir = os.path.join(repo, ".git")
        epoch, now = log._resolve_since("b", gitdir)
        self.assertIsNotNone(epoch)
        self.assertLessEqual(abs(epoch - now), 1)

    def test_a_real_date_does_not_resolve_to_now(self):
        repo = self.make_repo("g/p")
        gitdir = os.path.join(repo, ".git")
        epoch, now = log._resolve_since("2 weeks ago", gitdir)
        self.assertIsNotNone(epoch)
        self.assertGreater(now - epoch, 60 * 60 * 24 * 13)


class AuthorValidationTests(TempTreeTestCase):
    """--author is a REGEX; a malformed one exited 128 and became 'no commits'."""

    def test_a_malformed_regex_is_refused(self):
        for bad in ("[", "a\\", "*[a-"):
            with self.subTest(author=bad):
                with self.assertRaises(log.AuthorError):
                    log.validate_author(bad)

    def test_a_normal_author_is_accepted(self):
        for ok in ("alex", "t@t", "Jane Doe", None):
            with self.subTest(author=ok):
                log.validate_author(ok)  # must not raise


class CmdLogOutputTests(TempTreeTestCase):
    """What the USER sees — the actual defect surface."""

    def setUp(self):
        super().setUp()
        self.repo = make_old_repo(self, "grp/proj")

    def test_garbage_never_claims_no_commits(self):
        out = run_cmd_log(self.dest, "b")
        self.assertFalse(claims_no_commits(out),
                         "a garbage date still produced a commit claim:\n" + out)
        self.assertNotIn("Total:", out)
        self.assertIn("does not understand", out)
        self.assertIn("Nothing was searched", out)

    def test_garbage_message_is_distinguishable_from_an_empty_result(self):
        bad = run_cmd_log(self.dest, "not a date")
        empty = run_cmd_log(self.dest, "2021-01-01")   # valid; matches nothing
        self.assertNotEqual(bad, empty)
        self.assertIn("Nothing was searched", bad)
        self.assertNotIn("Nothing was searched", empty)
        self.assertTrue(claims_no_commits(empty))

    def test_empty_since_never_claims_no_commits(self):
        out = run_cmd_log(self.dest, "")
        self.assertFalse(claims_no_commits(out))
        self.assertIn("Nothing was searched", out)

    def test_option_looking_since_never_claims_no_commits(self):
        out = run_cmd_log(self.dest, "--all")
        self.assertIn("command-line option", out)
        self.assertNotIn("Total:", out)

    def test_a_real_filter_that_matches_still_prints_the_tree(self):
        self.make_repo("grp/recent")          # committed at 'now'
        out = run_cmd_log(self.dest, "2 weeks ago")
        self.assertIn("recent", out)
        self.assertIn("Total:", out)

    def test_the_resolved_date_is_echoed_to_the_user(self):
        out = run_cmd_log(self.dest, "2 weeks ago")
        self.assertRegex(out, r"commits after \d{4}-\d{2}-\d{2} \d{2}:\d{2}")

    def test_an_empty_result_reports_the_verified_history_count(self):
        """'your filter matched nothing' is now backed by a second check."""
        out = run_cmd_log(self.dest, "2021-01-01")
        self.assertTrue(claims_no_commits(out), out)
        self.assertIn("Verified:", out)
        self.assertIn("DO have history", out)

    def test_repos_with_no_history_are_reported_as_such(self):
        """Third outcome: nothing to exclude, so do not blame the filter."""
        empty_tree_dest = os.path.join(self.tmp, "EmptyGroups")
        os.makedirs(os.path.join(empty_tree_dest, "grp"), exist_ok=True)
        path = os.path.join(empty_tree_dest, "grp", "bare")
        os.makedirs(path, exist_ok=True)
        git("init", "-q", path)
        out = run_cmd_log(empty_tree_dest, "1y")
        self.assertIn("none of them has any commit at all", out)

    def test_a_bad_author_never_claims_no_commits(self):
        out = run_cmd_log(self.dest, "2 weeks ago", "[")
        self.assertFalse(claims_no_commits(out))
        self.assertIn("regular expression", out)
        self.assertIn("Nothing was searched", out)

    def test_no_case_raises(self):
        """Validation must never turn a bad prompt answer into a traceback."""
        for since in ("b", "", "--all", "x;y`id`", "2 weeks ago",
                      "yesterday", "2026-01-01", "not a date", "3.days"):
            with self.subTest(since=since):
                run_cmd_log(self.dest, since)


class ProbeHygieneTests(TempTreeTestCase):
    """The probe must not touch the user's tree or hang the screen."""

    def test_the_probe_prefers_a_repo_we_already_have(self):
        repo = self.make_repo("grp/proj")
        self.assertEqual(log._probe_gitdir([repo]),
                         os.path.join(repo, ".git"))

    def test_the_probe_falls_back_to_a_temp_bare_repo_outside_the_tree(self):
        import tempfile
        gitdir = log._probe_gitdir([])
        self.assertIsNotNone(gitdir)
        self.assertTrue(
            os.path.realpath(gitdir).startswith(
                os.path.realpath(tempfile.gettempdir())),
            "the date probe created a repository outside the temp dir: %s"
            % gitdir)

    def test_validation_works_with_no_repos_at_all(self):
        """The Since prompt is answered before any repo is walked."""
        with self.assertRaises(log.SinceError):
            log.validate_since("b", [])
        parsed, epoch, note = log.validate_since("2 weeks ago", [])
        self.assertIsNotNone(epoch)


if __name__ == "__main__":
    unittest.main(verbosity=2)
