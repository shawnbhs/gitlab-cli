#!/usr/bin/env python3
# tests/test_remotes_hardening.py — W3 hardening.
#
# The scrub is the remediation path for the leaked credentials. Two fixed
# defects around it had no test:
#
#   * config_path() used to be assumed as <repo>/.git/config. For a submodule
#     or a linked worktree, `.git` is a FILE and that path does not exist — so
#     the hardening silently no-opped on exactly the repos the discovery fix
#     had just made visible.
#   * git creates .git/config at 0644. After a scrub the file is chmodded to
#     0600, so a future re-injection is not instantly world-readable.
#
# Plus the scrub's own end-to-end property: a credential really leaves a real
# .git/config on disk, and a credential-free repo is left alone.

import os
import unittest

from _bootstrap import TempTreeTestCase, git, assert_no_credentials

import remotes

FAKE_TOKEN = "glpat-" + "FAKEsecretFAKEsecret"
CLEAN = "http://git.example.net/initech/feed-ms.git"
DIRTY = f"http://oauth2:{FAKE_TOKEN}@git.example.net/initech/feed-ms.git"


class CredentialDetectionTests(unittest.TestCase):

    def test_an_embedded_credential_is_detected_and_stripped(self):
        clean, cred = remotes.strip_creds(DIRTY)
        self.assertEqual(clean, CLEAN)
        self.assertTrue(cred.startswith("oauth2:"))
        self.assertTrue(remotes.has_creds(DIRTY))

    def test_a_clean_url_is_untouched(self):
        self.assertEqual(remotes.strip_creds(CLEAN), (CLEAN, ""))
        self.assertFalse(remotes.has_creds(CLEAN))

    def test_the_masked_url_never_contains_the_secret(self):
        masked = remotes.mask_url(DIRTY)
        self.assertNotIn(FAKE_TOKEN, masked)
        self.assertIn("oauth2", masked)      # the username stays diagnostic
        self.assertIn("***", masked)


class ConfigPathTests(TempTreeTestCase):

    def test_a_normal_repository_resolves_to_its_own_config(self):
        repo = self.make_project_clone("initech/feed-ms")
        self.assertEqual(os.path.realpath(remotes.config_path(repo)),
                         os.path.realpath(os.path.join(repo, ".git", "config")))

    def test_a_linked_worktree_resolves_to_the_real_git_dir(self):
        """`.git` is a FILE here; assuming <repo>/.git/config would point at a
        path that does not exist and the hardening would silently no-op.

        NOTE — this currently FAILS and the failure is a real defect, not an
        over-strict test. `git rev-parse --absolute-git-dir` in a linked
        worktree returns `<main>/.git/worktrees/<name>`, which holds no
        `config` file: git reads config from the COMMON dir
        (`--git-common-dir`). So `harden_config()` chmods nothing for every
        worktree/submodule repo — precisely the repos the discovery fix
        (`.git` as a file now counts) just made visible to the scrub.
        One-line fix in remotes.config_path: use `--git-common-dir`.
        """
        main = self.make_project_clone("initech/feed-ms")
        wt = os.path.join(self.tmp, "worktree")
        r = git("worktree", "add", "--detach", wt, cwd=main)
        if r.returncode != 0:
            self.skipTest(f"git worktree unavailable: {r.stderr.strip()[:80]}")
        self.assertTrue(os.path.isfile(os.path.join(wt, ".git")),
                        "expected .git to be a file in a linked worktree")
        cfg = remotes.config_path(wt)
        self.assertTrue(os.path.isfile(cfg),
                        f"config_path returned a non-existent path: {cfg}")

    def test_hardening_a_linked_worktree_actually_changes_a_mode(self):
        """The behavioural half of the same defect: harden_config reports
        success/failure, and for a worktree it used to be unable to succeed:
        config_path pointed at the linked worktree's private git dir, which has
        no config file. Fixed; this is now the regression guard."""
        main = self.make_project_clone("initech/feed-ms")
        wt = os.path.join(self.tmp, "worktree2")
        r = git("worktree", "add", "--detach", wt, cwd=main)
        if r.returncode != 0:
            self.skipTest("git worktree unavailable")
        os.chmod(os.path.join(main, ".git", "config"), 0o644)
        self.assertTrue(remotes.harden_config(wt),
                        "a worktree's config was never hardened")
        mode = os.stat(os.path.join(main, ".git", "config")).st_mode & 0o777
        self.assertEqual(mode, 0o600)


class HardeningTests(TempTreeTestCase):

    def mode(self, path):
        return os.stat(path).st_mode & 0o777

    def test_a_world_readable_config_is_tightened(self):
        repo = self.make_project_clone("initech/feed-ms")
        cfg = os.path.join(repo, ".git", "config")
        os.chmod(cfg, 0o644)
        self.assertTrue(remotes.harden_config(repo))
        self.assertEqual(self.mode(cfg), 0o600)

    def test_hardening_is_idempotent(self):
        repo = self.make_project_clone("initech/feed-ms")
        remotes.harden_config(repo)
        self.assertTrue(remotes.harden_config(repo))
        self.assertEqual(self.mode(os.path.join(repo, ".git", "config")), 0o600)

    def test_hardening_a_non_repository_fails_without_raising(self):
        plain = os.path.join(self.dest, "not-a-repo")
        os.makedirs(plain, exist_ok=True)
        self.assertFalse(remotes.harden_config(plain))


class ScrubTests(TempTreeTestCase):
    """End-to-end on real repositories. The credential store is stubbed so the
    test never touches the user's ~/.git-credentials."""

    def setUp(self):
        super().setUp()
        self._approve = remotes._credential_approve
        self.approved = []
        remotes._credential_approve = self._record

    def tearDown(self):
        remotes._credential_approve = self._approve
        super().tearDown()

    def _record(self, clean_url, cred):
        self.approved.append(clean_url)
        return True

    def test_a_credential_is_removed_from_a_real_config_on_disk(self):
        repo = self.make_repo("initech/feed-ms", origin=DIRTY)
        stats = remotes.scrub([repo], save_to_store=True)
        self.assertEqual(stats["cleaned"], 1)
        assert_no_credentials(self, repo)
        url = git("remote", "get-url", "origin", cwd=repo).stdout.strip()
        self.assertEqual(url, CLEAN)

    def test_the_token_is_stored_before_it_is_stripped(self):
        """Order matters: strip first and the repo can no longer authenticate."""
        repo = self.make_repo("initech/feed-ms", origin=DIRTY)
        remotes.scrub([repo], save_to_store=True)
        self.assertEqual(self.approved, [CLEAN])

    def test_the_token_is_saved_once_per_host_not_once_per_repo(self):
        repos = [self.make_repo(f"initech/p{i}", origin=DIRTY) for i in range(3)]
        stats = remotes.scrub(repos, save_to_store=True)
        self.assertEqual(stats["cleaned"], 3)
        self.assertEqual(stats["saved"], 1)

    def test_a_scrubbed_config_is_no_longer_world_readable(self):
        repo = self.make_repo("initech/feed-ms", origin=DIRTY)
        os.chmod(os.path.join(repo, ".git", "config"), 0o644)
        remotes.scrub([repo], save_to_store=True)
        mode = os.stat(os.path.join(repo, ".git", "config")).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_a_credential_free_repository_is_left_completely_alone(self):
        """Blast radius: only repos that were actually modified are chmodded."""
        repo = self.make_project_clone("initech/clean")
        os.chmod(os.path.join(repo, ".git", "config"), 0o644)
        stats = remotes.scrub([repo], save_to_store=True)
        self.assertEqual(stats["cleaned"], 0)
        self.assertEqual(self.approved, [])
        mode = os.stat(os.path.join(repo, ".git", "config")).st_mode & 0o777
        self.assertEqual(mode, 0o644)

    def test_a_dry_run_reports_without_changing_anything(self):
        repo = self.make_repo("initech/feed-ms", origin=DIRTY)
        stats = remotes.scrub([repo], save_to_store=True, dry_run=True)
        self.assertEqual(stats["cleaned"], 1)
        url = git("remote", "get-url", "origin", cwd=repo).stdout.strip()
        self.assertEqual(url, DIRTY, "a dry run modified the remote")
        self.assertEqual(self.approved, [])

    def test_the_audit_flags_a_leaky_remote_and_clears_after_the_scrub(self):
        """Negative control on the audit itself: it must be capable of
        reporting a credential, or a green audit means nothing."""
        repo = self.make_repo("initech/feed-ms", origin=DIRTY)
        before = remotes.audit([repo], check_network=False)
        self.assertTrue(any(r.get("has_creds") for r in before),
                        "the audit did not notice a tokenised remote")
        remotes.scrub([repo], save_to_store=True)
        after = remotes.audit([repo], check_network=False)
        self.assertFalse(any(r.get("has_creds") for r in after))

    def test_no_audit_or_scrub_output_contains_the_secret(self):
        repo = self.make_repo("initech/feed-ms", origin=DIRTY)
        rows = remotes.audit([repo], check_network=False)
        stats = remotes.scrub([repo], save_to_store=True, dry_run=True)
        blob = repr(rows) + repr(stats)
        self.assertNotIn(FAKE_TOKEN, blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
