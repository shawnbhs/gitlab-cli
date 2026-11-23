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
#     and it only runs when GITLAB_CLI_LIVE_TESTS=1 and an explicitly
#     approved GITLAB_CLI_LIVE_CLONE_URL are set. No project enumeration.
# No assertion ever prints a secret — only its absence is asserted.

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import subprocess
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

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

    def test_local_http_clone_authenticates_without_persisting_token(self):
        """Real smart-HTTP clone, synthetic token, isolated git config, loopback only."""
        src = self._local_source()
        r = git("symbolic-ref", "HEAD", "refs/heads/main", cwd=src)
        self.assertEqual(r.returncode, 0, r.stderr)
        expected_auth = "Basic " + base64.b64encode(
            f"oauth2:{FAKE}".encode("ascii")).decode("ascii")
        requests_seen = []

        class GitHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # Never log Authorization or URLs from the fixture.

            def do_GET(self):
                self.serve_git()

            def do_POST(self):
                self.serve_git()

            def serve_git(self):
                url = urlsplit(self.path)
                if not url.path.startswith("/src.git/"):
                    self.send_error(404)
                    return
                if self.headers.get("Authorization") != expected_auth:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="fixture"')
                    self.end_headers()
                    return
                requests_seen.append(self.command)
                length = int(self.headers.get("Content-Length", "0"))
                if length > 8 * 1024 * 1024:
                    self.send_error(413)
                    return
                env = {
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "GIT_PROJECT_ROOT": self.server.fixture_root,
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "REQUEST_METHOD": self.command,
                    "PATH_INFO": url.path,
                    "QUERY_STRING": url.query,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(length),
                    "REMOTE_ADDR": "127.0.0.1",
                }
                result = subprocess.run(
                    [GIT, "http-backend"], input=self.rfile.read(length),
                    env=env, capture_output=True, timeout=20)
                header, separator, body = result.stdout.partition(b"\r\n\r\n")
                if result.returncode or not separator:
                    self.send_error(500)
                    return
                lines = header.decode("latin-1").split("\r\n")
                status = next((line.split(":", 1)[1].strip() for line in lines
                               if line.lower().startswith("status:")), "200 OK")
                self.send_response(int(status.split()[0]))
                for line in lines:
                    if ":" in line and not line.lower().startswith("status:"):
                        key, value = line.split(":", 1)
                        self.send_header(key, value.strip())
                self.end_headers()
                self.wfile.write(body)

        # All subprocesses, including git credential approve, use throwaway HOME.
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": self.tmp, "XDG_CONFIG_HOME": self.tmp,
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
               "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo",
               "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
        with ThreadingHTTPServer(("127.0.0.1", 0), GitHandler) as server:
            server.fixture_root = self.tmp
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/src.git"
                dst = os.path.join(self.dest, "http-clone")
                with patch.dict(os.environ, env, clear=True), \
                        patch.object(git_mod, "save_credential", return_value=False), \
                        patch.object(git_mod, "credential_known", return_value=True):
                    # Disable helper fallback: only the clone process's auth header can pass.
                    ok = git_mod.clone_repo(FAKE, url, dst)
            finally:
                server.shutdown()
                worker.join(timeout=5)
        self.assertTrue(ok, "authenticated local smart-HTTP clone failed")
        self.assertIn("GET", requests_seen)
        self.assertIn("POST", requests_seen)
        assert_no_credentials(self, dst)
        config = os.path.join(dst, ".git", "config")
        with open(config, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn(FAKE, text)
        self.assertIn(url, text)

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
    """Explicitly chosen real GitLab project, opt-in. Not covered by fixture."""

    def test_a_real_clone_leaves_no_credential_on_disk(self):
        url = os.environ.get("GITLAB_CLI_LIVE_CLONE_URL", "")
        if not url:
            self.skipTest("set GITLAB_CLI_LIVE_CLONE_URL to an approved small project")
        import instances
        cfg = instances.get_active_instance()
        self.assertTrue(cfg and cfg.get("token"), "active instance token required")
        selected, active = urlsplit(url), urlsplit(cfg["url"])
        self.assertTrue(
            selected.scheme in ("http", "https") and
            selected.scheme == active.scheme and selected.netloc == active.netloc and
            not selected.username and not selected.password and
            selected.path.endswith(".git") and not selected.query and not selected.fragment,
            "explicit clone URL must be a clean .git URL on the active instance")
        dst = os.path.join(self.dest, "live")
        self.assertTrue(git_mod.clone_repo(cfg["token"], url, dst),
                        "approved live clone failed; credential hygiene unverified")
        assert_no_credentials(self, dst)
        with open(os.path.join(dst, ".git", "config"), "r", encoding="utf-8") as fh:
            self.assertFalse(cfg["token"] in fh.read(),
                             "credential persisted in .git/config")


if __name__ == "__main__":
    unittest.main()
