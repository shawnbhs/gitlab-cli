#!/usr/bin/env python3
# tests/test_sync_empty_purged.py — WAVE 2: empty remotes and purged clones.
#
# Evidence (live `pa` run, 2026-10-22, group-a):
#   * 7 repos printed `∅ no remote — no remote has branch 'main'` with the fix
#     `--set-upstream-to`. They were EMPTY projects on the server (remote
#     `ls-remote --heads` = 0 lines, local `rev-list --all --count` = 0). The
#     advice was wrong: nothing is wrong with them.
#   * 4 clones under `<name>-deletion_scheduled-<n>` printed `⊘ gone` under
#     "Not found (404)" with fix "open in web UI". The server had purged them,
#     and every `pa` re-logged them (git.py `log_error(f"fetch:{path}")`), so
#     the Error Log grew 10 -> 14 per run.
#
# Pins CONTRACT.md "WAVE 2 CONTRACT":
#   git.S_EMPTY  = "empty_remote"  remote has no branches AND local has no
#                                  commits. Remote empty + local commits stays
#                                  S_NO_REMOTE (real unpushed work).
#   git.S_PURGED = "purged"        fetch says not found AND the clone's path or
#                                  remote URL path has a `-deletion_scheduled-<d>`
#                                  segment suffix.
#   Neither calls log_error. pull.py keeps both out of "Needs attention";
#   purged's one fix line is error_catalog.by_code("purged").fix.
#
# Fixtures are REAL git repositories (bare remote + clone, plain filesystem
# paths, no network) under one tempfile.mkdtemp() per test. git runs with a
# hermetic HOME so the developer's global config cannot leak in. The error log
# is redirected per test into that temp dir and READ BACK: "no log entry" is
# asserted on the file errors.log_error() actually writes, not on a stub.

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401  (path bootstrap + error-log guard)
from _bootstrap import git as hgit

import errors
import error_catalog
import git as gitmod
import pull


def _status(name, fallback):
    """Contract constant from git.py, or its contract value if not landed yet
    (so a missing constant fails the behaviour assertion, not the import)."""
    return getattr(gitmod, name, fallback)


S_EMPTY = _status("S_EMPTY", "empty_remote")
S_PURGED = _status("S_PURGED", "purged")


class RealRepoBase(unittest.TestCase):
    """Throwaway bare remotes and clones; per-test error log; hermetic git."""

    PRINTERS = ("print_ok", "print_warn", "print_error")
    ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t23_sync_")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        # hermetic git for the code under test (git._run copies os.environ)
        self._env = {k: os.environ.get(k) for k in
                     list(self.ENV) + ["HOME", "GIT_CONFIG_GLOBAL"]}
        os.environ.update(self.ENV)
        os.environ["HOME"] = self.home
        os.environ["GIT_CONFIG_GLOBAL"] = os.path.join(self.home, "gitconfig")
        # per-test error log, read back by the assertions
        self._err = (errors.ERROR_DIR, errors.ERROR_FILE)
        errors.ERROR_DIR = os.path.join(self.tmp, "errlog")
        errors.ERROR_FILE = os.path.join(errors.ERROR_DIR, "errors.log")
        # silence console output only — logging stays real
        self._printers = {n: getattr(gitmod, n) for n in self.PRINTERS}
        for n in self.PRINTERS:
            setattr(gitmod, n, lambda *a, **k: None)

    def tearDown(self):
        for n, f in self._printers.items():
            setattr(gitmod, n, f)
        errors.ERROR_DIR, errors.ERROR_FILE = self._err
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        assert self.tmp.startswith(tempfile.gettempdir())
        assert os.path.realpath(self.tmp) != os.path.realpath(
            _bootstrap.REAL_MASTER_GROUPS)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── fixtures ──────────────────────────────────────────────────────────

    def g(self, *args, cwd=None):
        r = hgit(*args, cwd=cwd or self.home)
        self.assertEqual(r.returncode, 0, f"git {args}: {r.stderr}")
        return r

    def bare_remote(self, name="remote.git", populated=False):
        remote = os.path.join(self.tmp, "server", name)
        os.makedirs(os.path.dirname(remote), exist_ok=True)
        self.g("init", "-q", "--bare", "--initial-branch=main", remote)
        if populated:
            seed = os.path.join(self.tmp, "seed-" + os.path.basename(remote))
            self.g("init", "-q", "--initial-branch=main", seed)
            self.commit(seed)
            self.g("push", "-q", remote, "main", cwd=seed)
        return remote

    def clone(self, remote, relpath):
        work = os.path.join(self.tmp, "MasterGroups", *relpath.split("/"))
        os.makedirs(os.path.dirname(work), exist_ok=True)
        self.g("clone", "-q", remote, work)
        return work

    def commit(self, work, name="README.md"):
        with open(os.path.join(work, name), "w") as fh:
            fh.write("x\n")
        self.g("add", "-A", cwd=work)
        self.g("commit", "-q", "-m", "c", cwd=work)

    def purge(self, remote):
        """What GitLab's final purge looks like from a clone: the repo is gone."""
        assert remote.startswith(self.tmp)
        shutil.rmtree(remote)

    def log_entries(self):
        if not os.path.exists(errors.ERROR_FILE):
            return []
        out = []
        with open(errors.ERROR_FILE, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        out.append({"raw": line})
        return out

    def log_mentions(self, path):
        return [e for e in self.log_entries() if path in json.dumps(e)]


# ── git.py: names ──────────────────────────────────────────────────────────

class ContractNameTests(unittest.TestCase):

    def test_s_empty_constant_value(self):
        self.assertEqual(getattr(gitmod, "S_EMPTY", None), "empty_remote")

    def test_s_purged_constant_value(self):
        self.assertEqual(getattr(gitmod, "S_PURGED", None), "purged")


# ── git.py: empty remotes ──────────────────────────────────────────────────

class EmptyRemoteTests(RealRepoBase):

    def test_empty_remote_and_empty_clone_is_s_empty(self):
        work = self.clone(self.bare_remote(), "grp/fresh")
        res = gitmod.sync_repo(work)
        self.assertEqual(res["status"], S_EMPTY,
                         f"empty project reported as {res['status']!r}: "
                         f"{res.get('detail')!r}")

    def test_empty_remote_and_empty_clone_writes_no_log_entry(self):
        work = self.clone(self.bare_remote(), "grp/fresh")
        gitmod.sync_repo(work)
        self.assertEqual(self.log_entries(), [],
                         "an empty project is an expected state, not an error")

    def test_empty_detail_does_not_advise_set_upstream(self):
        work = self.clone(self.bare_remote(), "grp/fresh")
        res = gitmod.sync_repo(work)
        self.assertNotIn("set-upstream", res.get("detail") or "")
        self.assertNotIn("no remote has branch", res.get("detail") or "")

    def test_empty_remote_with_local_commit_stays_no_remote(self):
        work = self.clone(self.bare_remote(), "grp/started")
        self.commit(work)
        res = gitmod.sync_repo(work)
        self.assertEqual(res["status"], gitmod.S_NO_REMOTE,
                         "local commits on an empty remote are unpushed work")

    def test_empty_remote_with_local_commit_is_not_s_empty(self):
        work = self.clone(self.bare_remote(), "grp/started")
        self.commit(work)
        self.assertNotEqual(gitmod.sync_repo(work)["status"], S_EMPTY)

    def test_populated_remote_up_to_date_is_unaffected(self):
        work = self.clone(self.bare_remote(populated=True), "grp/normal")
        res = gitmod.sync_repo(work)
        self.assertEqual(res["status"], gitmod.S_UP_TO_DATE, res.get("detail"))


# ── git.py: purged clones ──────────────────────────────────────────────────

class PurgedCloneTests(RealRepoBase):

    def _purged_clone(self, relpath, remote_name="remote.git"):
        remote = self.bare_remote(remote_name, populated=True)
        work = self.clone(remote, relpath)
        self.purge(remote)
        return work

    def test_deletion_scheduled_clone_writes_no_log_entry(self):
        work = self._purged_clone("grp/x-deletion_scheduled-12")
        gitmod.sync_repo(work)
        self.assertEqual(self.log_entries(), [],
                         "a purged deletion-scheduled clone is re-logged on "
                         "every sync (Error Log grew 10 -> 14 per run)")

    def test_deletion_scheduled_clone_is_s_purged(self):
        work = self._purged_clone("grp/x-deletion_scheduled-12")
        res = gitmod.sync_repo(work)
        self.assertEqual(res["status"], S_PURGED, res.get("detail"))

    def test_clone_inside_deletion_scheduled_group_is_s_purged(self):
        # GitLab renames whole groups too: app-x-deletion_scheduled-98/...
        work = self._purged_clone("app-x-deletion_scheduled-98/app")
        res = gitmod.sync_repo(work)
        self.assertEqual(res["status"], S_PURGED, res.get("detail"))
        self.assertEqual(self.log_entries(), [])

    def test_remote_url_path_with_deletion_scheduled_is_s_purged(self):
        # local folder has the old name; the remote URL carries the suffix
        work = self._purged_clone("grp/plainname",
                                  remote_name="x-deletion_scheduled-34")
        res = gitmod.sync_repo(work)
        self.assertEqual(res["status"], S_PURGED, res.get("detail"))
        self.assertEqual(self.log_entries(), [])

    def test_ordinary_clone_with_remote_removed_is_still_gone(self):
        work = self._purged_clone("grp/ordinary")
        res = gitmod.sync_repo(work)
        self.assertEqual(res["status"], gitmod.S_GONE, res.get("detail"))

    def test_ordinary_clone_with_remote_removed_is_still_logged(self):
        work = self._purged_clone("grp/ordinary")
        gitmod.sync_repo(work)
        self.assertTrue(self.log_mentions(work),
                        f"an unexplained 404 must still be logged; log="
                        f"{self.log_entries()!r}")

    def test_suffix_not_ending_in_digits_is_not_purged(self):
        # `-deletion_scheduled-\d+` must be the whole segment suffix
        work = self._purged_clone("grp/x-deletion_scheduled-12b")
        res = gitmod.sync_repo(work)
        self.assertEqual(res["status"], gitmod.S_GONE, res.get("detail"))
        self.assertTrue(self.log_mentions(work))

    def test_deletion_scheduled_clone_that_still_exists_syncs_normally(self):
        # pending (not yet purged): the remote is alive, nothing special
        remote = self.bare_remote("live.git", populated=True)
        work = self.clone(remote, "grp/y-deletion_scheduled-5")
        res = gitmod.sync_repo(work)
        self.assertEqual(res["status"], gitmod.S_UP_TO_DATE, res.get("detail"))


# ── error_catalog ──────────────────────────────────────────────────────────

class CatalogTests(unittest.TestCase):

    def test_purged_entry_exists(self):
        self.assertIsNotNone(error_catalog.by_code("purged"))

    def test_purged_entry_fields(self):
        info = error_catalog.by_code("purged")
        self.assertEqual(info.code, "purged")
        self.assertEqual(info.type, "NotFound")
        self.assertEqual(info.title, "Purged from the server")

    def test_purged_entry_respects_limits(self):
        info = error_catalog.by_code("purged")
        self.assertLessEqual(len(info.title), 40)
        self.assertLessEqual(len(info.cause), 110)
        self.assertLessEqual(len(info.fix), 110)

    def test_purged_fix_says_archive_and_nothing_to_sync(self):
        fix = error_catalog.by_code("purged").fix.lower()
        self.assertIn("archive", fix)
        self.assertIn("nothing to sync", fix)

    def test_purged_cause_warns_last_copy(self):
        cause = error_catalog.by_code("purged").cause.lower()
        self.assertIn("deletion", cause)
        self.assertIn("last copy", cause)

    def test_empty_repo_fix_says_nothing_to_sync_until_push(self):
        fix = error_catalog.by_code("empty_repo").fix.lower()
        self.assertIn("nothing to sync", fix)
        self.assertIn("push", fix)

    def test_no_upstream_fix_mentions_push_u(self):
        self.assertIn("git push -u", error_catalog.by_code("no_upstream").fix)


# ── pull.py: summary ───────────────────────────────────────────────────────

def _res(path, status, detail=""):
    return {"path": path, "status": status, "detail": detail,
            "branch": "main", "remote": "origin", "ahead": 0, "behind": 0,
            "other": [], "shallow": False}


class PullSummaryTests(unittest.TestCase):

    BASE = "/nonexistent/t23/base"

    def setUp(self):
        self._sync = pull.sync_repo
        self.results = {
            f"{self.BASE}/grp/fresh": _res(f"{self.BASE}/grp/fresh", S_EMPTY,
                                           "empty repository on the server"),
            f"{self.BASE}/grp/x-deletion_scheduled-12":
                _res(f"{self.BASE}/grp/x-deletion_scheduled-12", S_PURGED,
                     "purged from the server"),
        }
        pull.sync_repo = lambda p: self.results[p]

    def tearDown(self):
        pull.sync_repo = self._sync

    def run_pull(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            stats, attention = pull.sync_many(sorted(self.results), self.BASE,
                                              workers=1)
            pull._print_summary(stats, attention, self.BASE)
        return stats, attention, buf.getvalue()

    def test_empty_and_purged_are_counted_in_stats(self):
        stats, _, _ = self.run_pull()
        self.assertEqual(stats.get(S_EMPTY), 1)
        self.assertEqual(stats.get(S_PURGED), 1)

    def test_no_needs_attention_block_for_empty_and_purged(self):
        _, _, out = self.run_pull()
        self.assertNotIn("Needs attention", out,
                         "empty/purged repos are not problems:\n" + out)

    def test_empty_is_not_a_problem_result(self):
        _, attention, _ = self.run_pull()
        problems = [r for r in attention
                    if r["status"] not in pull._WARNINGS]
        self.assertNotIn(S_EMPTY, [r["status"] for r in problems])

    def test_purged_is_not_a_problem_result(self):
        _, attention, _ = self.run_pull()
        problems = [r for r in attention
                    if r["status"] not in pull._WARNINGS]
        self.assertNotIn(S_PURGED, [r["status"] for r in problems])

    def test_purged_fix_line_comes_from_catalog(self):
        _, _, out = self.run_pull()
        info = error_catalog.by_code("purged")
        self.assertIsNotNone(info)
        self.assertIn(info.fix, out)
        self.assertEqual(out.count(info.fix), 1, "exactly one fix line")

    def test_empty_summary_row_says_empty(self):
        _, _, out = self.run_pull()
        self.assertIn("○ empty", out)

    def test_empty_row_never_suggests_set_upstream(self):
        _, _, out = self.run_pull()
        self.assertNotIn("set-upstream", out)

    def test_purged_not_shown_as_not_found_404(self):
        _, _, out = self.run_pull()
        self.assertNotIn("open in web UI", out)
        nf = error_catalog.by_code("not_found")
        self.assertNotIn(nf.title, out)


if __name__ == "__main__":
    unittest.main()
