#!/usr/bin/env python3
# tests/test_git_credential_mechanics.py — W3 hardening.
#
# The leak: clone_repo() handed `http://oauth2:<token>@host/…` to `git clone`,
# and git writes the clone URL verbatim into .git/config — every cloned repository held
# the live admin PAT at mode 0644. The existing suite asserts the SHAPE of a
# clean .git/config; this file pins the MECHANISM that keeps it clean, because
# the shape assertion passes for a clone that never authenticated at all.
#
# Four properties, each a separate way the fix could be undone by a later edit:
#   * the token is not in argv (/proc/<pid>/cmdline is world-readable)
#   * the token reaches git as env-only config, which git does NOT copy into
#     the new repo — unlike `git clone -c key=value`, which it deliberately
#     persists, and which would reintroduce the exact bug
#   * a URL that already embeds a credential is stripped before we store it
#   * credential fields carrying CR/LF/NUL are refused (git-credential is a
#     newline-delimited protocol; a newline injects extra fields)
#
# Offline: git._run is replaced with a capture. No token value is real — the
# strings below are constructed in-process.

import base64
import os
import unittest

from _bootstrap import TempTreeTestCase, assert_no_credentials

import git as gitlib

FAKE_TOKEN = "glpat-" + "FAKEtokenFAKEtokenFK"
CLEAN_URL = "http://git.example.net/initech/feed-ms.git"


class UrlHygieneTests(unittest.TestCase):

    def test_userinfo_is_stripped_from_an_api_supplied_url(self):
        """Anyone who can create a project on the instance controls this
        string, so a project whose http_url_to_repo already embeds a
        credential would otherwise have it written into .git/config by us."""
        dirty = f"http://oauth2:{FAKE_TOKEN}@git.example.net/initech/feed-ms.git"
        self.assertEqual(gitlib.strip_userinfo(dirty), CLEAN_URL)

    def test_stripping_is_idempotent_and_leaves_a_clean_url_alone(self):
        self.assertEqual(gitlib.strip_userinfo(CLEAN_URL), CLEAN_URL)
        self.assertEqual(
            gitlib.strip_userinfo(gitlib.strip_userinfo(
                f"http://u:p@git.example.net/x.git")), "http://git.example.net/x.git")

    def test_base_url_is_the_scheme_host_pair_git_matches_config_on(self):
        self.assertEqual(gitlib.base_url(CLEAN_URL), "http://git.example.net/")
        self.assertEqual(gitlib.base_url("https://h:8929/a/b.git"),
                         "https://h:8929/")

    def test_inject_token_still_works_for_its_remaining_callers(self):
        """remotes.py still uses it for one-shot, non-persisting operations —
        but it must never be handed to a command that stores a URL."""
        out = gitlib.inject_token(CLEAN_URL, FAKE_TOKEN)
        self.assertIn("oauth2:", out)
        self.assertIn("DANGEROUS", gitlib.inject_token.__doc__ or "")

    def test_inject_token_does_not_double_inject(self):
        once = gitlib.inject_token(CLEAN_URL, FAKE_TOKEN)
        self.assertEqual(gitlib.inject_token(once, FAKE_TOKEN), once)


class AuthEnvTests(unittest.TestCase):

    def test_the_token_is_carried_as_an_env_only_http_header(self):
        env = gitlib._auth_env(CLEAN_URL, FAKE_TOKEN)
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"],
                         "http.http://git.example.net/.extraHeader")
        self.assertTrue(env["GIT_CONFIG_VALUE_0"].startswith(
            "Authorization: Basic "))

    def test_the_header_actually_authenticates_as_oauth2(self):
        """A header that decodes to the wrong thing would fail closed at clone
        time, which is exactly what a silent refactor here would cause."""
        env = gitlib._auth_env(CLEAN_URL, FAKE_TOKEN)
        blob = env["GIT_CONFIG_VALUE_0"].split("Basic ", 1)[1]
        self.assertEqual(base64.b64decode(blob).decode(),
                         f"oauth2:{FAKE_TOKEN}")

    def test_the_header_is_scoped_to_one_exact_host(self):
        """Unscoped, a redirect to another host would carry the credential."""
        key = gitlib._auth_env(CLEAN_URL, FAKE_TOKEN)["GIT_CONFIG_KEY_0"]
        self.assertIn("git.example.net", key)
        self.assertNotEqual(
            key, gitlib._auth_env("http://elsewhere/x.git",
                                  FAKE_TOKEN)["GIT_CONFIG_KEY_0"])

    def test_an_existing_git_config_count_is_appended_to_not_clobbered(self):
        os.environ["GIT_CONFIG_COUNT"] = "2"
        try:
            env = gitlib._auth_env(CLEAN_URL, FAKE_TOKEN)
            self.assertEqual(env["GIT_CONFIG_COUNT"], "3")
            self.assertIn("GIT_CONFIG_KEY_2", env)
        finally:
            os.environ.pop("GIT_CONFIG_COUNT", None)

    def test_a_token_containing_a_newline_is_refused(self):
        """An HTTP header cannot contain CR/LF — this is a header-injection
        guard, not cosmetics."""
        for bad in ("tok\nen", "tok\ren", "tok\x00en"):
            self.assertEqual(gitlib._auth_env(CLEAN_URL, bad), {})

    def test_no_token_yields_no_auth_environment(self):
        self.assertEqual(gitlib._auth_env(CLEAN_URL, ""), {})


class CloneArgvTests(TempTreeTestCase):
    """git._run is captured, so this runs offline and asserts what would have
    been handed to git."""

    def setUp(self):
        super().setUp()
        self.calls = []
        self._run = gitlib._run
        self._save = gitlib.save_credential
        self._known = gitlib.credential_known
        gitlib.save_credential = lambda *a, **k: True
        gitlib.credential_known = lambda *a, **k: True

        def fake_run(args, cwd=None, timeout=None, env_extra=None):
            self.calls.append({"args": list(args), "env": dict(env_extra or {})})
            if "clone" in args:
                target = args[-1]
                os.makedirs(os.path.join(target, ".git"), exist_ok=True)
                with open(os.path.join(target, ".git", "config"), "w") as fh:
                    fh.write('[remote "origin"]\n\turl = '
                             f'{args[-2]}\n')
            return 0, "", ""
        gitlib._run = fake_run

    def tearDown(self):
        gitlib._run = self._run
        gitlib.save_credential = self._save
        gitlib.credential_known = self._known
        super().tearDown()

    def clone(self, url=CLEAN_URL, token=FAKE_TOKEN):
        target = os.path.join(self.dest, "initech", "feed-ms")
        ok = gitlib.clone_repo(token, url, target)
        # clone_repo also runs post-clone checks through _run; pick the clone.
        clone_calls = [c for c in self.calls if "clone" in c["args"]]
        self.assertTrue(clone_calls, "git clone was never invoked")
        return ok, target, clone_calls[-1]

    def test_the_token_never_appears_in_the_command_line(self):
        """/proc/<pid>/cmdline is readable by every local account; the old
        tokenised URL argument exposed the PAT to all of them."""
        self.clone()
        for call in self.calls:
            self.assertNotIn(FAKE_TOKEN, " ".join(call["args"]))

    def test_the_url_handed_to_git_is_the_clean_one(self):
        _, _, call = self.clone()
        self.assertIn(CLEAN_URL, call["args"])

    def test_a_credential_bearing_api_url_is_cleaned_before_git_sees_it(self):
        dirty = f"http://oauth2:{FAKE_TOKEN}@git.example.net/initech/feed-ms.git"
        _, _, call = self.clone(url=dirty)
        self.assertNotIn(FAKE_TOKEN, " ".join(call["args"]))
        self.assertIn(CLEAN_URL, call["args"])

    def test_the_double_dash_separator_is_present(self):
        """Without `--`, a URL beginning with `-` is consumed as an option;
        `--upload-pack=<cmd>` is a real RCE."""
        _, _, call = self.clone()
        self.assertIn("--", call["args"])
        self.assertLess(call["args"].index("--"), call["args"].index(CLEAN_URL))

    def test_the_credential_travels_in_the_environment_instead(self):
        _, _, call = self.clone()
        self.assertTrue(any(k.startswith("GIT_CONFIG_") for k in call["env"]))

    def test_the_clone_is_not_given_persisting_dash_c_config(self):
        """`git clone -c http.…extraHeader=…` is persisted into .git/config by
        design — using it here would reintroduce the whole defect."""
        _, _, call = self.clone()
        self.assertNotIn("-c", call["args"])

    def test_the_resulting_config_holds_no_credential(self):
        _, target, _ = self.clone()
        assert_no_credentials(self, target)

    def test_the_new_repository_config_is_not_world_readable(self):
        _, target, _ = self.clone()
        mode = os.stat(os.path.join(target, ".git", "config")).st_mode & 0o777
        self.assertEqual(mode, 0o600, f"config left at {oct(mode)}")

    def test_a_refused_url_is_never_cloned(self):
        ok = gitlib.clone_repo(FAKE_TOKEN, "ext::sh -c id",
                               os.path.join(self.dest, "x"))
        self.assertFalse(ok)
        self.assertEqual(self.calls, [], "an unsafe URL reached git")


class CredentialProtocolTests(unittest.TestCase):
    """git-credential is a newline-delimited key=value protocol."""

    def test_a_field_containing_a_newline_is_refused(self):
        for bad_url in ("http://git.int\nernal/x.git",):
            self.assertFalse(gitlib.save_credential(bad_url, FAKE_TOKEN))
        self.assertFalse(gitlib.save_credential(CLEAN_URL, "tok\nen"))
        self.assertFalse(gitlib.save_credential(CLEAN_URL, "tok\x00en"))

    def test_a_non_http_scheme_is_refused(self):
        self.assertFalse(gitlib.save_credential("ssh://git@h/x.git", FAKE_TOKEN))

    def test_an_empty_token_is_refused(self):
        self.assertFalse(gitlib.save_credential(CLEAN_URL, ""))


class PersistedCredentialDetectionTests(TempTreeTestCase):
    """The belt-and-braces post-clone check must actually detect a credential,
    otherwise it is decoration."""

    def test_a_tokenised_origin_is_detected(self):
        repo = self.make_repo(
            "initech/leaky",
            origin=f"http://oauth2:{FAKE_TOKEN}@git.example.net/initech/leaky.git")
        self.assertTrue(gitlib.remote_has_userinfo(repo))

    def test_a_clean_origin_is_not_flagged(self):
        repo = self.make_project_clone("initech/feed-ms")
        self.assertFalse(gitlib.remote_has_userinfo(repo))

    def test_a_directory_with_no_origin_is_not_flagged(self):
        repo = self.make_repo("initech/no-origin")
        self.assertFalse(gitlib.remote_has_userinfo(repo))


if __name__ == "__main__":
    unittest.main(verbosity=2)
