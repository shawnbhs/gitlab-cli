#!/usr/bin/env python3
"""
tests/test_w7_2_credential_pattern.py — regression guard for the W7-2
false-positive fix in scripts/verify_mirror.py.

THE BUG THIS PINS
-----------------
The referee's `gitlab_pat` pattern was `\\bgl[a-z]{2,6}-[A-Za-z0-9_\\-]{16,}`,
an OPEN-ENDED prefix class. GitLab's token prefixes are a closed, documented
set; "gl" + any-2-to-6-letters is not. The pattern therefore matched ordinary
PROJECT NAMES. The completely clean, credential-free remote URL

    url = https://hub.example.com/atlas/glint-management-system.git

(zero "@" characters, no userinfo, no oauth2 literal) matched on the path
segment `glint-management-system`, and `verify-mirror` exited 1 forever on a
tree that had nothing wrong with it. A security check that cries wolf on clean
input gets ignored, which is strictly worse than no check.

WHAT MUST STAY TRUE
-------------------
  * a clean URL whose PROJECT NAME starts with a gl-ish prefix is NOT a
    credential finding -- whatever the name spells;
  * a genuinely tokenised URL IS still a credential finding;
  * the prefix set stays CLOSED: no open-ended `gl[a-z]{n,m}-` character class
    may reappear in the source. This is a source-text assertion on purpose --
    it is the guard against someone re-widening the pattern later, which is
    exactly how the bug was introduced.

No network, no token, no GitLab server. Every token-shaped string below is
synthetic and locally generated; none is or ever was a real credential.
"""

import importlib.util
import io
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(PROJECT_ROOT, "scripts", "verify_mirror.py")

# Project rule (pinned by tests/test_w7_live_error_log_isolation.py): every test
# module must reach the guard, so that running it standalone cannot append to
# the developer's live ~/.gitlab-cli/errors.log.
sys.path.insert(0, HERE)
import _error_log_guard  # noqa: E402,F401


def load_referee():
    """Import verify_mirror.py by path, without executing its main()."""
    spec = importlib.util.spec_from_file_location("w7_2_verify_mirror", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


VM = load_referee()

# Synthetic secret bodies -- never real credentials.
FAKE_BODY = "AAAAAAAAAAAAAAAAAAAAAAA"


def findings_for(text):
    """Run every CRED_PATTERN over `text`, return the set of kinds that fire."""
    kinds = set()
    for name, pat in VM.CRED_PATTERNS:
        if pat.search(text):
            kinds.add(name)
    return kinds


class CleanProjectNamesAreNotCredentials(unittest.TestCase):
    """The false positive itself, plus its whole family."""

    # The exact line from the real config that broke verify-mirror.
    REAL_CASE = ('[remote "origin"]\n'
                 '\turl = https://hub.example.com/atlas/'
                 'glint-management-system.git\n')

    def test_the_actual_false_positive_is_clean(self):
        self.assertEqual(
            set(), findings_for(self.REAL_CASE),
            "the clean glint-management-system URL must produce NO credential "
            "finding: it contains no '@', no userinfo and no token")

    def test_url_has_no_userinfo_at_all(self):
        self.assertNotIn("@", self.REAL_CASE,
                         "sanity: the regression case genuinely has no userinfo")

    def test_family_of_gl_prefixed_project_names_is_clean(self):
        # Every one of these is a plausible project slug that the old
        # open-ended pattern would have flagged.
        for name in ("glint-management-system",
                     "globe-trotter-frontend-service",
                     "gluon-data-pipeline-worker",
                     "glacier-archive-storage-svc",
                     "glossary-translation-backend",
                     "glider-telemetry-ingest-api"):
            line = "\turl = https://hub.example.invalid/grp/%s.git\n" % name
            with self.subTest(project=name):
                self.assertEqual(
                    set(), findings_for(line),
                    "project name %r is not a credential" % name)

    def test_clean_ssh_remote_is_not_flagged(self):
        # `git@host:path` is SSH key auth -- no secret in the config.
        line = "\turl = git@hub.example.invalid:atlas/glint-management-system.git\n"
        self.assertEqual(set(), findings_for(line))


class RealCredentialsAreStillCaught(unittest.TestCase):
    """The fix must not have blinded the check."""

    def test_tokenised_url_is_flagged(self):
        line = ("\turl = https://oauth2:glpat-%s@hub.example.invalid/"
                "atlas/glint-management-system.git\n" % FAKE_BODY)
        kinds = findings_for(line)
        self.assertIn("userinfo_in_url", kinds)
        self.assertIn("gitlab_pat", kinds)
        self.assertIn("oauth2_literal", kinds)

    def test_userinfo_without_a_known_prefix_is_still_flagged(self):
        # An opaque secret in the userinfo position -- no recognisable prefix.
        line = ("\turl = https://user:s3cr3t-opaque-value@hub.example.invalid/"
                "grp/proj.git\n")
        self.assertIn("userinfo_in_url", findings_for(line))

    def test_every_documented_gitlab_prefix_is_caught(self):
        for prefix in VM.GITLAB_TOKEN_PREFIXES:
            line = "\turl = https://oauth2:%s%s@h.invalid/g/p.git\n" % (
                prefix, FAKE_BODY)
            with self.subTest(prefix=prefix):
                self.assertIn("gitlab_pat", findings_for(line),
                              "prefix %r must be detected" % prefix)

    def test_extraheader_is_flagged(self):
        text = '[http "https://h.invalid/"]\n\textraheader = AUTHORIZATION: basic QUJD\n'
        self.assertIn("extraheader", findings_for(text))

    def test_credential_password_value_is_flagged(self):
        text = "[credential]\n\tpassword = something-secret-here\n"
        self.assertIn("credential_value", findings_for(text))

    def test_github_token_is_flagged(self):
        line = "\turl = https://ghp_%s@github.com/o/r.git\n" % ("A" * 30)
        self.assertIn("github_pat", findings_for(line))


def source_without_comments(path):
    """Return the source with COMMENTS stripped.

    The guard below must test live code, not prose. The fix's own explanatory
    comment necessarily quotes the bad pattern in order to explain it, and a
    naive full-text search would flag that forever.
    """
    import tokenize

    with tokenize.open(path) as fh:
        toks = list(tokenize.generate_tokens(fh.readline))
    return "".join(
        t.string for t in toks if t.type not in (tokenize.COMMENT,))


class PrefixSetStaysClosed(unittest.TestCase):
    """Source-text guard: the open-ended class must not come back."""

    SOURCE = source_without_comments(SCRIPT)

    def test_no_open_ended_gl_character_class(self):
        bad = re.search(r"gl\[a-z\]\{\d+,\d+\}-", self.SOURCE)
        self.assertIsNone(
            bad,
            "an open-ended `gl[a-z]{n,m}-` prefix class is back in "
            "verify_mirror.py; that is the exact pattern that flagged the "
            "clean project name glint-management-system. Enumerate the real "
            "prefixes in GITLAB_TOKEN_PREFIXES instead.")

    def test_prefix_tuple_is_explicit_and_hyphen_terminated(self):
        self.assertTrue(VM.GITLAB_TOKEN_PREFIXES,
                        "the prefix set must not be empty")
        for p in VM.GITLAB_TOKEN_PREFIXES:
            with self.subTest(prefix=p):
                self.assertTrue(p.startswith("gl"), p)
                self.assertTrue(p.endswith("-"),
                                "%r must end with '-' so it cannot match a "
                                "bare word" % p)

    def test_glint_is_not_a_prefix(self):
        self.assertNotIn("glint-", VM.GITLAB_TOKEN_PREFIXES)


class RedactorDoesNotCorruptCleanOutput(unittest.TestCase):
    """The Redactor's belt-and-braces pass uses VALUE patterns only."""

    def test_clean_url_survives_redaction_intact(self):
        url = "https://hub.example.com/atlas/glint-management-system.git"
        self.assertEqual(
            url, VM.REDACT(url),
            "redaction must not mangle a clean URL -- positional patterns "
            "match key names, not secrets, and must stay out of REDACT_PATTERNS")

    def test_real_token_literal_is_still_redacted(self):
        tok = "glpat-" + FAKE_BODY
        out = VM.REDACT("url = https://oauth2:%s@h.invalid/g/p.git" % tok)
        self.assertNotIn(tok, out)
        self.assertIn("<redacted>", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
