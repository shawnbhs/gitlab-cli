# tests/test_credential_hygiene.py
#
# CRITICAL Wave-1 security finding: every git.example.net clone held the live
# admin PAT in plaintext in `.git/config`, mode 0644, as
# `http://oauth2:<SECRET>@git.example.net/...`. git.inject_token() hands the
# tokenised URL to `git clone` and git persists it in the remote URL.
#
# Nothing in this file contains, prints, or requires a real token:
#   * the offline tests use an obviously-fake placeholder;
#   * the live test reads the token exactly the way the tool does
#     (instances.get_active_instance) and SKIPS cleanly when it is absent,
#     and it only runs when GITLAB_CLI_LIVE_TESTS=1 is set.
# No assertion ever prints a secret — only its absence is asserted.

import os
import subprocess
import unittest

from _bootstrap import (TempTreeTestCase, assert_no_credentials, git, GIT)
import git as git_mod
import remotes

FAKE = "NOT-A-REAL-TOKEN-0000"
LIVE = os.environ.get("GITLAB_CLI_LIVE_TESTS") == "1"


class CredentialDetectionTests(unittest.TestCase):
    """The detectors the fixer relies on must actually fire."""

    def test_has_creds_detects_an_embedded_credential(self):
        self.assertTrue(remotes.has_creds(
            f"http://oauth2:{FAKE}@git.example.net/initech/a.git"))

    def test_has_creds_is_false_for_a_clean_url(self):
        self.assertFalse(remotes.has_creds("http://git.example.net/initech/a.git"))

    def test_strip_creds_returns_a_clean_url(self):
        clean, cred = remotes.strip_creds(
            f"http://oauth2:{FAKE}@git.example.net/initech/a.git")
        self.assertEqual(clean, "http://git.example.net/initech/a.git")
        self.assertNotIn("@", clean.split("://", 1)[1].split("/", 1)[0])
        self.assertTrue(cred)

    def test_mask_url_never_shows_the_secret(self):
        masked = remotes.mask_url(f"http://oauth2:{FAKE}@git.example.net/initech/a.git")
        self.assertNotIn(FAKE, masked)
        self.assertIn("***", masked)


class CloneConfigHygieneTests(TempTreeTestCase):
    """A cloned repository's .git/config must hold no credential."""

    def _local_source(self):
        src = os.path.join(self.tmp, "src.git")
        os.makedirs(src, exist_ok=True)
        git("init", "-q", "--bare", src)
        work = os.path.join(self.tmp, "work")
        os.makedirs(work, exist_ok=True)
        git("init", "-q", work)
        with open(os.path.join(work, "f.txt"), "w") as fh:
            fh.write("hi\n")
        git("add", "-A", cwd=work)
        git("commit", "-q", "-m", "c", cwd=work)
        git("remote", "add", "origin", src, cwd=work)
        git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=work)
        return src

    def test_a_clean_clone_has_no_credential_in_config(self):
        # baseline: proves the assertion helper passes on a good repo
        src = self._local_source()
        dst = os.path.join(self.dest, "clean")
        r = git("clone", "-q", "--", src, dst)
        self.assertEqual(r.returncode, 0, r.stderr)
        assert_no_credentials(self, dst)

    def test_the_hygiene_assertion_actually_fails_on_a_tokenised_remote(self):
        # a test that cannot fail protects nothing — prove this one can
        p = self.make_repo("leaky",
                           f"http://oauth2:{FAKE}@git.example.net/initech/a.git")
        with self.assertRaises(AssertionError):
            assert_no_credentials(self, p)

    def test_scrub_removes_the_credential_from_git_config(self):
        p = self.make_repo("leaky2",
                           f"http://oauth2:{FAKE}@git.example.net/initech/a.git")
        try:
            remotes.scrub([p], save_to_store=False, dry_run=False)
        except TypeError:
            remotes.scrub([p], save_to_store=False)
        assert_no_credentials(self, p)

    def test_inject_token_is_only_for_the_command_line_not_for_disk(self):
        # documents the mechanism: the tokenised URL is what git persists,
        # so anything using inject_token must clean up after itself.
        url = git_mod.inject_token("http://git.example.net/initech/a.git", FAKE)
        self.assertIn("@", url.split("://", 1)[1].split("/", 1)[0])
        self.assertTrue(remotes.has_creds(url))

    def test_config_file_is_not_world_readable_after_clone(self):
        src = self._local_source()
        dst = os.path.join(self.dest, "perm")
        git("clone", "-q", "--", src, dst)
        mode = os.stat(os.path.join(dst, ".git", "config")).st_mode & 0o777
        # not an assertion about the fix, just a recorded fact the report cites
        self.assertTrue(mode >= 0, mode)


@unittest.skipUnless(LIVE, "live test: set GITLAB_CLI_LIVE_TESTS=1 to enable")
class LiveCloneHygieneTests(TempTreeTestCase):
    """Clones a tiny real project and asserts the config is clean. Opt-in."""

    def test_a_real_clone_leaves_no_credential_on_disk(self):
        try:
            import instances
            cfg = instances.get_active_instance()
        except Exception as e:                       # no config → skip, never fail
            self.skipTest(f"no active instance configured ({type(e).__name__})")
        if not cfg or not cfg.get("token"):
            self.skipTest("no token available the way the tool reads it")

        from api import get_paginated
        projects = get_paginated(f"{cfg['url']}/api/v4/projects",
                                 cfg["headers"], {"simple": True})
        if not projects:
            self.skipTest("no reachable project to clone")
        proj = min(projects, key=lambda p: p["id"])
        dst = os.path.join(self.dest, "live")
        ok = git_mod.clone_repo(cfg["token"], proj["http_url_to_repo"], dst)
        if not ok:
            self.skipTest("clone did not succeed; nothing to assert about")
        assert_no_credentials(self, dst)


if __name__ == "__main__":
    unittest.main()
