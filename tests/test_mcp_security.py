"""Security regression tests for the W3-13 audit findings #1, #2 and #7.

* **#1 path traversal** — ``local_status(group=...)`` must never walk a
  directory outside the instance's managed directory.
* **#2 repo-config code execution** — a repository found on disk is
  untrusted; its ``.git/config`` (``core.fsmonitor``, a ``filter`` driver)
  must not be able to run a program when a *read* tool inspects it.
* **#7 read-only fails open** — any ``GITLAB_MCP_READ_ONLY`` value that is not
  clearly "off" must mean read-only.

The exploit tests use real git and real hook scripts, each of which only
touches a marker file inside the test's own temporary directory. Every
exploit test has a *control* that runs plain git on the same fixture and
proves the payload is live, so "marker absent" cannot pass vacuously.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from gitlab_mcp import context as ctxmod
from gitlab_mcp import reporting
from tests import _home_guard

GIT = shutil.which("git")


def setUpModule() -> None:
    """Point HOME and every gitlab-cli config path at a temporary directory."""
    _home_guard.activate()


def tearDownModule() -> None:
    """Restore HOME; fail if any test reached into the real home."""
    _home_guard.deactivate()


class FakeCtx:
    """The slice of ``GitLabContext`` that ``local_status`` uses."""

    def __init__(self, master_dir: str) -> None:
        self.name = "example"
        self.master_dir = master_dir

    def git_env(self) -> dict:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def run_captured(self, fn, *args, **kwargs):
        return fn(*args, **kwargs), ""


class _Sandbox(unittest.TestCase):
    """A temp root holding ``MasterGroups/`` and an ``outside/`` sibling."""

    def setUp(self) -> None:
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="gitlab-mcp-sec-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.master = os.path.join(self.root, "MasterGroups")
        self.outside = os.path.join(self.root, "outside")
        os.makedirs(self.master)
        os.makedirs(self.outside)
        self.home = _home_guard.fake_home() or self.root

    def git(self, repo: str, *args: str) -> str:
        """Fixture git: plain, unhardened, isolated HOME."""
        env = dict(os.environ)
        env.update({
            "HOME": self.home,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Alice Example",
            "GIT_AUTHOR_EMAIL": "alice@example.com",
            "GIT_COMMITTER_NAME": "Alice Example",
            "GIT_COMMITTER_EMAIL": "alice@example.com",
        })
        done = subprocess.run([GIT, *args], cwd=repo, env=env,
                              capture_output=True, text=True, timeout=30,
                              check=False)
        return (done.stdout or "").strip()

    def make_repo(self, path: str) -> str:
        os.makedirs(path)
        self.git(path, "init", "-q", "-b", "main")
        with open(os.path.join(path, "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("hello\n")
        self.git(path, "add", "a.txt")
        self.git(path, "commit", "-q", "-m", "Initial commit")
        return path

    def script(self, name: str, marker: str, body: str = "exit 1") -> str:
        """An executable that touches ``marker`` — the exploit's payload."""
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"#!/bin/sh\ntouch '{marker}'\n{body}\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path


# ─── #1 path traversal ────────────────────────────────────────────────────────

@unittest.skipIf(GIT is None, "git is not installed")
class LocalStatusContainmentTests(_Sandbox):

    def setUp(self) -> None:
        super().setUp()
        self.make_repo(os.path.join(self.outside, "secret-project"))
        self.make_repo(os.path.join(self.master, "group-a", "project-a"))
        self.ctx = FakeCtx(self.master)

    def assert_rejected(self, group: str) -> None:
        with self.assertRaises(ValueError):
            reporting.local_status(self.ctx, group=group)

    def test_dot_dot_sibling_is_rejected(self) -> None:
        self.assert_rejected("../outside")

    def test_dot_dot_inside_a_longer_path_is_rejected(self) -> None:
        self.assert_rejected("group-a/../../outside")

    def test_absolute_path_is_rejected(self) -> None:
        self.assert_rejected(self.outside)

    def test_root_is_rejected(self) -> None:
        self.assert_rejected("/")

    def test_nul_byte_is_rejected(self) -> None:
        self.assert_rejected("group-a\x00/../../outside")

    def test_backslash_traversal_is_rejected(self) -> None:
        self.assert_rejected("..\\outside")

    def test_symlink_pointing_outside_is_rejected(self) -> None:
        os.symlink(self.outside, os.path.join(self.master, "escape"))
        self.assert_rejected("escape")

    def test_symlinked_repo_inside_a_group_is_not_reported(self) -> None:
        os.symlink(os.path.join(self.outside, "secret-project"),
                   os.path.join(self.master, "group-a", "linked"))
        result = reporting.local_status(self.ctx, group="group-a")
        paths = [r["path"] for r in result["repos"]]
        self.assertFalse(any("secret-project" in p or "linked" in p
                             for p in paths), paths)

    def test_a_valid_group_still_works(self) -> None:
        result = reporting.local_status(self.ctx, group="group-a")
        self.assertEqual(1, result["count"])
        self.assertEqual(os.path.join(self.master, "group-a"), result["base"])

    def test_trailing_slash_is_accepted(self) -> None:
        self.assertEqual(
            1, reporting.local_status(self.ctx, group="group-a/")["count"])

    def test_no_group_lists_only_the_managed_directory(self) -> None:
        result = reporting.local_status(self.ctx)
        self.assertEqual(1, result["count"])
        self.assertNotIn("secret-project", str(result))


# ─── #2 repository config must not execute ───────────────────────────────────

@unittest.skipIf(GIT is None, "git is not installed")
class RepoConfigExecutionTests(_Sandbox):

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.make_repo(
            os.path.join(self.master, "group-a", "project-a"))
        self.ctx = FakeCtx(self.master)
        self.marker = os.path.join(self.root, "PWNED")

    def arm_fsmonitor(self) -> None:
        hook = self.script("fsmonitor.sh", self.marker)
        self.git(self.repo, "config", "core.fsmonitor", hook)

    def arm_clean_filter(self) -> None:
        clean = self.script("clean.sh", self.marker, body="cat")
        self.git(self.repo, "config", "filter.evil.clean", clean)
        self.git(self.repo, "config", "filter.evil.required", "true")
        with open(os.path.join(self.repo, ".git", "info", "attributes"), "w",
                  encoding="utf-8") as fh:
            fh.write("*.txt filter=evil\n")
        # Same content, new mtime: git must re-read the file — through the
        # clean filter — to decide whether it changed.
        path = os.path.join(self.repo, "a.txt")
        st = os.stat(path)
        os.utime(path, (st.st_atime + 100, st.st_mtime + 100))

    def test_control_fsmonitor_payload_is_live_for_plain_git(self) -> None:
        self.arm_fsmonitor()
        self.git(self.repo, "status", "--porcelain")
        self.assertTrue(os.path.exists(self.marker))

    def test_local_status_does_not_run_core_fsmonitor(self) -> None:
        self.arm_fsmonitor()
        result = reporting.local_status(self.ctx)
        self.assertEqual(1, result["count"])
        self.assertFalse(os.path.exists(self.marker))

    def test_local_status_does_not_run_core_fsmonitor_in_read_only_mode(
            self) -> None:
        self.arm_fsmonitor()
        with mock.patch.dict(os.environ, {ctxmod.READ_ONLY_ENV: "1"}):
            reporting.local_status(self.ctx)
        self.assertFalse(os.path.exists(self.marker))

    def test_control_clean_filter_payload_is_live_for_plain_git(self) -> None:
        self.arm_clean_filter()
        self.git(self.repo, "status", "--porcelain")
        self.assertTrue(os.path.exists(self.marker))

    def test_local_status_does_not_run_a_clean_filter(self) -> None:
        self.arm_clean_filter()
        reporting.local_status(self.ctx)
        self.assertFalse(os.path.exists(self.marker))

    def test_clean_filter_repo_is_still_reported(self) -> None:
        self.arm_clean_filter()
        row = reporting.local_status(self.ctx)["repos"][0]
        self.assertEqual("main", row["branch"])

    def test_inherited_git_env_cannot_reenable_fsmonitor(self) -> None:
        hook = self.script("fsmonitor.sh", self.marker)
        env = {"GIT_CONFIG_COUNT": "1",
               "GIT_CONFIG_KEY_0": "core.fsmonitor",
               "GIT_CONFIG_VALUE_0": hook}
        with mock.patch.dict(os.environ, env):
            reporting.local_status(self.ctx)
        self.assertFalse(os.path.exists(self.marker))


class GitInvocationHardeningTests(unittest.TestCase):
    """Every read-tool git process gets the overrides; the argv IS the fix."""

    def run_git(self, env: dict | None = None) -> tuple[list, dict]:
        seen: dict = {}

        def fake_run(argv, **kwargs):
            seen["argv"], seen["env"] = list(argv), kwargs["env"]
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(reporting.subprocess, "run", fake_run):
            reporting._git(".", ["status", "--porcelain"], env or {})
        return seen["argv"], seen["env"]

    def test_overrides_precede_the_subcommand(self) -> None:
        argv, _ = self.run_git()
        status_at = argv.index("status")
        for setting in ("core.fsmonitor=false", "core.untrackedCache=false",
                        "core.hooksPath=/dev/null", "diff.external=",
                        "core.pager=cat", "protocol.file.allow=never"):
            self.assertIn(setting, argv[:status_at])
            self.assertEqual("-c", argv[argv.index(setting) - 1])
        self.assertEqual("--no-optional-locks", argv[1])

    def test_environment_is_forced_even_if_the_caller_omits_it(self) -> None:
        _, env = self.run_git({"GIT_SSH_COMMAND": "touch /nonexistent/x"})
        self.assertEqual("1", env["GIT_CONFIG_NOSYSTEM"])
        self.assertEqual("0", env["GIT_TERMINAL_PROMPT"])
        self.assertEqual("0", env["GIT_OPTIONAL_LOCKS"])
        self.assertNotIn("GIT_SSH_COMMAND", env)

    @unittest.skipIf(GIT is None, "git is not installed")
    def test_installed_git_accepts_every_override(self) -> None:
        workdir = tempfile.mkdtemp(prefix="gitlab-mcp-sec-")
        self.addCleanup(shutil.rmtree, workdir, True)
        done = subprocess.run(
            [GIT, "--no-optional-locks", *reporting.SAFE_GIT_CONFIG,
             "init", "-q", workdir],
            env=reporting._safe_env(dict(os.environ)), capture_output=True,
            text=True, timeout=30, check=False)
        self.assertEqual(0, done.returncode, done.stderr)


# ─── #7 read-only fails closed ───────────────────────────────────────────────

class ReadOnlyFailClosedTests(unittest.TestCase):

    def read_only_for(self, value: str | None) -> bool:
        env = dict(os.environ)
        env.pop(ctxmod.READ_ONLY_ENV, None)
        if value is not None:
            env[ctxmod.READ_ONLY_ENV] = value
        with mock.patch.dict(os.environ, env, clear=True):
            return ctxmod.read_only()

    def test_unset_allows_writes(self) -> None:
        self.assertFalse(self.read_only_for(None))

    def test_clear_off_values_allow_writes(self) -> None:
        for value in ("", "0", "false", "FALSE", "no", "off", " Off "):
            with self.subTest(value=value):
                self.assertFalse(self.read_only_for(value))

    def test_on_values_disable_writes(self) -> None:
        for value in ("1", "true", "yes", "on"):
            with self.subTest(value=value):
                self.assertTrue(self.read_only_for(value))

    def test_unrecognised_values_disable_writes(self) -> None:
        for value in ("enabled", "y", "2", "ture", "read-only"):
            with self.subTest(value=value):
                self.assertTrue(self.read_only_for(value))


class ContextGitEnvTests(unittest.TestCase):

    def test_git_env_ignores_system_config_and_optional_locks(self) -> None:
        ctx = object.__new__(ctxmod.GitLabContext)
        with mock.patch.dict(os.environ, {"GIT_SSH_COMMAND": "evil",
                                          "GIT_CONFIG_PARAMETERS": "'a.b=c'"}):
            env = ctxmod.GitLabContext.git_env(ctx)
        self.assertEqual("1", env["GIT_CONFIG_NOSYSTEM"])
        self.assertEqual("0", env["GIT_OPTIONAL_LOCKS"])
        self.assertEqual("0", env["GIT_TERMINAL_PROMPT"])
        self.assertNotIn("GIT_SSH_COMMAND", env)
        self.assertNotIn("GIT_CONFIG_PARAMETERS", env)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
