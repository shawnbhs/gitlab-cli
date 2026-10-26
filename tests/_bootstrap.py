# tests/_bootstrap.py — shared offline harness for the gitlab-cli regression suite.
#
# Design rules (see tests/README.md):
#   * fixtures are REAL git repositories created by `git init` in a fresh
#     tempfile.mkdtemp() — no mocking of git, because every bug this suite pins
#     was a bug about what git/the filesystem actually did.
#   * nothing is ever written outside that temp dir, and the temp dir is the
#     only thing ever removed.
#   * no network, no token, no GitLab server for anything in this module.

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

# The CLI lives one level above tests/.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

GIT = shutil.which("git") or "git"

# Never touch the user's live error log. Importing this redirects
# errors.ERROR_DIR / errors.ERROR_FILE into a per-process temp dir before any
# test runs, so a production code path that calls errors.log_error() (e.g.
# git.clone_repo refusing an unsafe URL) cannot append to the real
# ~/.gitlab-cli/errors.log. Must run before anything imports `errors`.
import _error_log_guard  # noqa: E402,F401

# Never touch the real clone tree.
REAL_MASTER_GROUPS = os.path.join(PROJECT_ROOT, "MasterGroups")

HOST = "git.example.net"
BASE = f"http://{HOST}"


def git(*args, cwd=None):
    """Run git with a hermetic environment (no user config, no credential helper)."""
    env = dict(os.environ)
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": cwd or tempfile.gettempdir(),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "echo",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    })
    return subprocess.run([GIT, *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60)


class TempTreeTestCase(unittest.TestCase):
    """Base class: one throwaway tree per test, removed on teardown."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gitlabcli_test_")
        self.dest = os.path.join(self.tmp, "MasterGroups")
        os.makedirs(self.dest, exist_ok=True)
        self._cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self._cwd)
        # Guard rail: only ever delete our own mkdtemp directory.
        assert self.tmp.startswith(tempfile.gettempdir())
        assert os.path.realpath(self.tmp) != os.path.realpath(REAL_MASTER_GROUPS)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── fixture builders ───────────────────────────────────────────────────

    def make_repo(self, relpath, origin=None, commit=True):
        """A real git work tree at dest/<relpath> with origin set to `origin`."""
        path = os.path.join(self.dest, *relpath.split("/"))
        os.makedirs(path, exist_ok=True)
        r = git("init", "-q", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        if commit:
            with open(os.path.join(path, "README.md"), "w") as fh:
                fh.write("x\n")
            git("add", "-A", cwd=path)
            git("commit", "-q", "-m", "init", cwd=path)
        if origin:
            r = git("remote", "add", "origin", origin, cwd=path)
            self.assertEqual(r.returncode, 0, r.stderr)
        return path

    def make_project_clone(self, path_with_namespace):
        """A clone that looks exactly like one the tool produced (clean URL)."""
        return self.make_repo(path_with_namespace,
                              f"{BASE}/{path_with_namespace}.git")

    def make_broken_clone(self, relpath, git_is_file=False):
        """An interrupted clone: `.git` exists but is not a usable repository."""
        path = os.path.join(self.dest, *relpath.split("/"))
        os.makedirs(path, exist_ok=True)
        dotgit = os.path.join(path, ".git")
        if git_is_file:
            with open(dotgit, "w") as fh:
                fh.write("gitdir: /nonexistent/elsewhere/.git\n")
        else:
            os.makedirs(dotgit, exist_ok=True)
            # what `git clone` leaves behind when it dies during transfer
            os.makedirs(os.path.join(dotgit, "objects"), exist_ok=True)
            with open(os.path.join(dotgit, "HEAD"), "w") as fh:
                fh.write("ref: refs/heads/main\n")
        return path


def fake_projects(*paths_with_namespace):
    """The subset of the GitLab project payload the code actually reads."""
    return [{"id": i,
             "path": p.split("/")[-1],
             "path_with_namespace": p,
             "http_url_to_repo": f"{BASE}/{p}.git"}
            for i, p in enumerate(paths_with_namespace, 1)]


def fake_cfg(url=BASE, master_dir=None):
    return {"url": url, "headers": {"PRIVATE-TOKEN": "unused-offline"},
            "token": "unused-offline", "min_access": 0,
            "master_dir": master_dir or "."}


def assert_no_credentials(testcase, repo_path):
    """A clone must never persist a credential in .git/config."""
    cfg = os.path.join(repo_path, ".git", "config")
    testcase.assertTrue(os.path.isfile(cfg), f"no .git/config at {repo_path}")
    with open(cfg, "r", errors="replace") as fh:
        text = fh.read()
    for url_line in [ln.strip() for ln in text.splitlines()
                     if ln.strip().startswith("url =")]:
        value = url_line.split("=", 1)[1].strip()
        authority = value.split("://", 1)[-1].split("/", 1)[0]
        testcase.assertNotIn(
            "@", authority,
            "credential persisted in .git/config "
            f"({repo_path}) — remote authority contains an embedded userinfo")
    testcase.assertNotIn("oauth2:", text,
                         f"credential persisted in .git/config ({repo_path})")
    testcase.assertNotIn("glpat-", text,
                         f"credential persisted in .git/config ({repo_path})")
