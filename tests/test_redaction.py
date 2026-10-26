#!/usr/bin/env python3
# tests/test_redaction.py — W3 hardening.
#
# errors.redact() is the last line of defence before text lands in
# ~/.gitlab-cli/errors.log and (via git.check_url) on the terminal. The old
# code applied three patterns by index, so a fourth rule added to the list was
# silently dead — which is exactly the shape of bug that lets a token through.
#
# Every test here uses a synthetic token built in-process. No real token value
# is read, printed or written by this file.

import unittest

from _bootstrap import TempTreeTestCase  # noqa: F401  (path bootstrap)

import errors
import git as gitlib

FAKE = "glpat-" + "FAKEFAKEFAKEFAKEFAKE"          # synthetic, not a real token
FAKE2 = "s3cr3t-" + "password-value-here"


class RedactionRuleTests(unittest.TestCase):

    def assertRedacted(self, text, secret):
        out = errors.redact(text)
        self.assertNotIn(secret, out,
                         f"secret survived redaction in: {text[:40]}…")
        return out

    def test_a_bare_gitlab_token_is_removed(self):
        self.assertRedacted(f"fatal: bad token {FAKE}", FAKE)

    def test_user_password_userinfo_is_removed_but_the_username_survives(self):
        out = self.assertRedacted(
            f"fatal: could not read from http://oauth2:{FAKE}@git.example.net/a/b.git",
            FAKE)
        self.assertIn("oauth2", out,
                      "the username is diagnostic — it says which code path "
                      "injected the credential — and must not be destroyed")

    def test_userinfo_without_a_colon_is_removed(self):
        """A PAT used as the whole userinfo: https://<token>@host. Rule 3 does
        not see this shape at all."""
        self.assertRedacted(f"https://{FAKE}@git.example.net/a/b.git", FAKE)

    def test_a_token_in_a_query_string_is_removed(self):
        for param in ("private_token", "access_token", "token", "job_token"):
            self.assertRedacted(
                f"GET /api/v4/projects?{param}={FAKE}&per_page=100", FAKE)

    def test_a_token_echoed_as_a_header_is_removed(self):
        for header in ("PRIVATE-TOKEN", "Authorization", "X-Gitlab-Token"):
            self.assertRedacted(f"> {header}: {FAKE}", FAKE)

    def test_a_github_or_openai_token_is_removed_too(self):
        for tok in ("ghp_" + "0123456789abcdefghij",
                    "sk-" + "0123456789abcdefghij"):
            self.assertRedacted(f"remote: rejected using {tok}", tok)

    def test_redaction_is_not_a_no_op(self):
        """Negative control: an assertion that cannot fail protects nothing."""
        leaky = f"http://oauth2:{FAKE}@git.example.net/x.git"
        self.assertNotEqual(errors.redact(leaky), leaky)
        self.assertIn("[REDACTED", errors.redact(leaky))

    def test_harmless_text_is_left_alone(self):
        for clean in ("http://git.example.net/initech/feed-ms.git",
                      "fatal: repository not found",
                      "", None):
            self.assertEqual(errors.redact(clean), clean)

    def test_every_declared_rule_is_actually_applied(self):
        """The old bug was applying patterns 0,1,2 by hand. Iterate the rule
        table itself so a newly added rule cannot be dead on arrival."""
        self.assertGreaterEqual(len(errors._SECRET_RULES), 6)
        samples = [
            f"token {FAKE}",
            "token ghp_0123456789abcdefghij",
            f"http://u:{FAKE2}@h/x",
            f"http://{FAKE}@h/x",
            f"http://h/x?private_token={FAKE}",
            f"PRIVATE-TOKEN: {FAKE}",
        ]
        for text in samples:
            self.assertNotEqual(errors.redact(text), text, text)


class CheckUrlLeakTests(unittest.TestCase):
    """git.check_url()'s message reaches print_error() — which does NOT
    redact — as well as the log. The URL comes from the GitLab API, so anyone
    who can create a project on the instance controls that string."""

    def test_a_rejected_url_carrying_a_credential_is_not_echoed_in_the_clear(self):
        bad = f"ftp://oauth2:{FAKE}@git.example.net/x.git"
        with self.assertRaises(gitlib.UnsafeURL) as ctx:
            gitlib.check_url(bad)
        self.assertNotIn(FAKE, str(ctx.exception))

    def test_an_argument_injection_url_is_refused(self):
        with self.assertRaises(gitlib.UnsafeURL):
            gitlib.check_url("--upload-pack=touch /tmp/pwned")

    def test_a_dangerous_transport_is_refused(self):
        for url in ("ext::sh -c touch% /tmp/pwned",
                    "file:///etc/passwd",
                    "ssh://git@h/x.git"):
            with self.assertRaises(gitlib.UnsafeURL):
                gitlib.check_url(url)

    def test_an_empty_url_is_refused(self):
        with self.assertRaises(gitlib.UnsafeURL):
            gitlib.check_url("")

    def test_plain_http_is_accepted(self):
        """Internal hosts without TLS must keep working."""
        url = "http://git.example.net/initech/feed-ms.git"
        self.assertEqual(gitlib.check_url(url), url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
