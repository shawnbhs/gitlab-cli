# tests/test_rename_reconcile.py — upstream renames are FOLLOWED, safely.
#
# Measured on the real machine: the server renamed group group-a/legacy-tools
# -> group-a/data-team and project legacy-tools/crawler-panel ->
# data-team/data-team-labeler. The clones on disk kept their old paths and old
# origins, and the existing repair could not fix any of them, for four reasons
# these tests pin:
#
#   D1  the clone root (MasterGroups/) lives INSIDE another git repository, so
#       the unbounded nested-repo walk read every clone as nested;
#   D2  a project whose name AND namespace changed matched no candidate tier;
#   D3  a same-device rename was refused for dirty trees / stashes / local
#       commits — none of which a rename can lose;
#   +   now-empty old parents were never pruned, and Pull All had no way to
#       call the repair (reconcile_renames).
#
# Real git, real directories, temp trees only. The ONE HTTP call (the redirect
# lookup) and the project listings are stubbed; nothing reaches a server.

import contextlib
import io
import os
import subprocess
import unittest

import _bootstrap  # noqa: F401  (path + error-log guard)
from _bootstrap import TempTreeTestCase, git, GIT, BASE, HOST, fake_cfg

from tests import _home_guard

import api
import clone as clone_mod
import coverage as cov


def setUpModule():
    _home_guard.activate()


def tearDownModule():
    _home_guard.deactivate()


# ─── stubs ────────────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class RedirectServer:
    """GitLab's `GET /projects/<encoded path>`: renames map OLD -> CURRENT."""

    def __init__(self, renames=None, live=()):
        self.renames = dict(renames or {})
        self.live = set(live)
        self.calls = []

    def request(self, url, headers, params=None, quiet=False):
        from urllib.parse import unquote
        path = unquote(url.rsplit("/projects/", 1)[-1])
        self.calls.append(path)
        if path in self.renames:
            cur = self.renames[path]
            return FakeResponse(200, {"id": 1, "path": cur.split("/")[-1],
                                      "path_with_namespace": cur})
        if path in self.live:
            return FakeResponse(200, {"id": 2, "path": path.split("/")[-1],
                                      "path_with_namespace": path})
        return FakeResponse(404, {"message": "404 Project Not Found"})


def proj(pwn, pid=1, url=None):
    return {"id": pid, "path": pwn.split("/")[-1], "path_with_namespace": pwn,
            "http_url_to_repo": url or f"{BASE}/{pwn}.git"}


def projs(*pwns):
    return [proj(p, i) for i, p in enumerate(pwns, 1)]


def keys(*pwns):
    return {(HOST, p.lower()) for p in pwns}


def out_of(*args, cwd=None):
    r = subprocess.run([GIT, *args], cwd=cwd, capture_output=True, text=True,
                       timeout=60)
    return r.returncode, r.stdout


# ─── shared fixture ───────────────────────────────────────────────────────────

class ReconcileCase(TempTreeTestCase):

    def setUp(self):
        super().setUp()
        clone_mod.reset_presence_cache()
        clone_mod.reset_redirect_cache()
        self._saved = {}
        self.cfg = fake_cfg()
        self.cfg["token"] = ""
        self.cloned = []

        def _no_clone(*a, **k):
            self.cloned.append(a)
            raise AssertionError("a clone was attempted")

        self.patch("clone_repo", _no_clone)
        self.patch("_clone", _no_clone)

        def _no_network_check(*a, **k):
            raise AssertionError("unique_commits_vs_server must not run for "
                                 "a same-device rename")
        self.patch("unique_commits_vs_server", _no_network_check)
        self.bare = self.make_bare()

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(clone_mod, name, value)
        clone_mod.reset_presence_cache()
        clone_mod.reset_redirect_cache()
        super().tearDown()

    def patch(self, name, value):
        if name not in self._saved:
            self._saved[name] = getattr(clone_mod, name)
        setattr(clone_mod, name, value)

    def server(self, renames=None, live=()):
        srv = RedirectServer(renames, live)

        class _Api:
            request = staticmethod(srv.request)
            ApiError = api.ApiError
        self.patch("gitlab_api", _Api)
        return srv

    def make_bare(self):
        bare = os.path.join(self.tmp, "srv", "server.git")
        os.makedirs(os.path.dirname(bare), exist_ok=True)
        self.assertEqual(git("init", "--bare", "-q", bare).returncode, 0)
        seed = os.path.join(self.tmp, "seed")
        git("init", "-q", seed)
        with open(os.path.join(seed, "a.txt"), "w") as fh:
            fh.write("one\n")
        git("add", "-A", cwd=seed)
        git("commit", "-q", "-m", "on the server", cwd=seed)
        git("branch", "-M", "main", cwd=seed)
        git("push", "-q", bare, "main", cwd=seed)
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=bare)
        return bare

    def clone_old(self, old_pwn):
        """A real clone at dest/<old_pwn> whose origin is the OLD URL."""
        path = os.path.join(self.dest, *old_pwn.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        r = subprocess.run([GIT, "clone", "-q", self.bare, path],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        git("remote", "set-url", "origin", f"{BASE}/{old_pwn}.git", cwd=path)
        return path

    def parent_repo_around_dest(self):
        """D1: the clone root lives inside another git work tree."""
        self.assertEqual(git("init", "-q", self.tmp).returncode, 0)
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, ".git")))

    def at(self, pwn):
        return os.path.join(self.dest, *pwn.split("/"))

    def repair(self, p, known=None, root="dest"):
        index = clone_mod.build_presence_index(self.dest)
        root = self.dest if root == "dest" else root
        return clone_mod.try_rename_repair(
            self.cfg, p, self.at(p["path_with_namespace"]), index, HOST,
            set(), known=known, root=root)

    def origin(self, path):
        return out_of("-C", path, "remote", "get-url", "origin")[1].strip()


# ─── D1 ───────────────────────────────────────────────────────────────────────

class D1BoundedNestedCheck(ReconcileCase):

    def test_clone_root_inside_a_parent_repo_still_moves(self):
        self.parent_repo_around_dest()
        old = self.clone_old("group-a/legacy-tools/svc")
        self.server({"group-a/legacy-tools/svc": "group-a/data-team/svc"})
        res = self.repair(proj("group-a/data-team/svc"))
        self.assertEqual(res["action"], "moved", res)
        self.assertFalse(os.path.exists(old))
        new = self.at("group-a/data-team/svc")
        self.assertEqual(clone_mod.repo_state(new), "ok")
        self.assertEqual(cov.normalize(self.origin(new)),
                         (HOST, "group-a/data-team/svc"))

    def test_without_a_root_the_old_unbounded_walk_is_kept(self):
        self.parent_repo_around_dest()
        old = self.clone_old("group-a/legacy-tools/svc")
        self.assertTrue(clone_mod._nested_inside_another_repo(old))
        self.assertFalse(clone_mod._nested_inside_another_repo(old, self.dest))

    def test_a_repo_nested_strictly_inside_dest_is_still_refused(self):
        outer = self.make_repo("group-a/outer", f"{BASE}/group-a/outer.git")
        inner = os.path.join(outer, "vendor", "svc")
        os.makedirs(os.path.dirname(inner), exist_ok=True)
        subprocess.run([GIT, "clone", "-q", self.bare, inner],
                       capture_output=True, text=True, timeout=120)
        git("remote", "set-url", "origin",
            f"{BASE}/group-a/outer/vendor/svc.git", cwd=inner)
        self.server({"group-a/outer/vendor/svc": "group-a/data-team/svc"})
        res = self.repair(proj("group-a/data-team/svc"))
        self.assertEqual(res["action"], "refused", res)
        self.assertTrue(any("nested" in r for r in res["reasons"]), res)
        self.assertTrue(os.path.isdir(inner))
        self.assertFalse(os.path.exists(self.at("group-a/data-team/svc")))


# ─── D2 ───────────────────────────────────────────────────────────────────────

class D2NameAndNamespaceChanged(ReconcileCase):
    OLD = "group-a/legacy-tools/crawler-panel"
    NEW = "group-a/data-team/data-team-labeler"

    def test_found_only_via_the_known_set(self):
        old = self.clone_old(self.OLD)
        self.server({self.OLD: self.NEW})
        res = self.repair(proj(self.NEW),
                          known=keys(self.NEW, "group-a/data-team/other"))
        self.assertEqual(res["action"], "moved", res)
        self.assertFalse(os.path.exists(old))
        self.assertEqual(clone_mod.repo_state(self.at(self.NEW)), "ok")

    def test_known_none_turns_tier_three_off(self):
        old = self.clone_old(self.OLD)
        srv = self.server({self.OLD: self.NEW})
        res = self.repair(proj(self.NEW), known=None)
        self.assertEqual(res["action"], "none", res)
        self.assertTrue(os.path.isdir(old))
        self.assertNotIn(self.OLD, srv.calls)

    def test_a_clone_of_a_current_project_is_never_an_orphan(self):
        # The old path is itself a CURRENT project: not an orphan, not asked.
        old = self.clone_old(self.OLD)
        srv = self.server({self.OLD: self.NEW})
        res = self.repair(proj(self.NEW), known=keys(self.NEW, self.OLD))
        self.assertEqual(res["action"], "none")
        self.assertTrue(os.path.isdir(old))
        self.assertNotIn(self.OLD, srv.calls)

    def test_orphans_outside_the_top_level_namespace_are_not_asked(self):
        self.clone_old("otherco/legacy-tools/crawler-panel")
        srv = self.server({"otherco/legacy-tools/crawler-panel": self.NEW})
        res = self.repair(proj(self.NEW), known=keys(self.NEW))
        self.assertEqual(res["action"], "none")
        self.assertEqual(srv.calls, [])

    def test_a_callable_known_is_fetched_lazily_once(self):
        self.clone_old(self.OLD)
        self.server({self.OLD: self.NEW})
        calls = []

        def known():
            calls.append(1)
            return keys(self.NEW)
        res = self.repair(proj(self.NEW), known=known)
        self.assertEqual(res["action"], "moved", res)
        self.assertEqual(len(calls), 1)


# ─── D3 ───────────────────────────────────────────────────────────────────────

class D3SameDeviceRenameCarriesEverything(ReconcileCase):

    def _dirty_everything(self, repo):
        with open(os.path.join(repo, "only-here.txt"), "w") as fh:
            fh.write("local commit\n")
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", "local only", cwd=repo)
        with open(os.path.join(repo, "a.txt"), "a") as fh:
            fh.write("stashed\n")
        git("stash", "push", "-q", "-m", "wip", cwd=repo)
        with open(os.path.join(repo, "a.txt"), "a") as fh:
            fh.write("dirty\n")
        with open(os.path.join(repo, "scratch.txt"), "w") as fh:
            fh.write("untracked\n")

    def test_dirty_untracked_stash_and_local_commit_survive(self):
        self.parent_repo_around_dest()
        old = self.clone_old("group-a/legacy-tools/work")
        self._dirty_everything(old)
        before = {
            "status": out_of("-C", old, "status", "--porcelain")[1],
            "stash": out_of("-C", old, "stash", "list")[1],
            "local": out_of("-C", old, "rev-list", "--count", "HEAD",
                            "--not", "--remotes")[1].strip(),
            "head": out_of("-C", old, "rev-parse", "HEAD")[1].strip(),
        }
        self.assertTrue(before["stash"].strip(), "fixture: no stash")
        self.assertEqual(before["local"], "1")
        self.server({"group-a/legacy-tools/work": "group-a/data-team/work"})

        res = self.repair(proj("group-a/data-team/work"))

        self.assertEqual(res["action"], "moved", res)
        new = self.at("group-a/data-team/work")
        self.assertFalse(os.path.exists(old))
        self.assertEqual(out_of("-C", new, "status", "--porcelain")[1],
                         before["status"])
        self.assertEqual(out_of("-C", new, "stash", "list")[1],
                         before["stash"])
        self.assertEqual(out_of("-C", new, "rev-list", "--count", "HEAD",
                                "--not", "--remotes")[1].strip(), "1")
        self.assertEqual(out_of("-C", new, "rev-parse", "HEAD")[1].strip(),
                         before["head"])
        self.assertIn("carried along", res["detail"])
        self.assertIn("1 local-only commit", res["detail"])
        self.assertIn("1 stash", res["detail"])
        self.assertIn("untracked", res["detail"])

    def test_a_linked_worktree_outside_dest_works_after_the_move(self):
        old = self.clone_old("group-a/legacy-tools/wtrepo")
        wt = os.path.join(self.tmp, "wt-outside", "feature")
        r = git("-C", old, "worktree", "add", "-q", "-b", "feature", wt)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.server({"group-a/legacy-tools/wtrepo": "group-a/data-team/wtrepo"})

        res = self.repair(proj("group-a/data-team/wtrepo"))

        self.assertEqual(res["action"], "moved", res)
        self.assertTrue(res["worktrees"]["had"])
        self.assertTrue(res["worktrees"]["ok"], res["worktrees"])
        self.assertEqual(res["worktrees"]["linked"], 1)
        self.assertIn("1 linked worktree(s) repaired", res["detail"])
        rc, _ = out_of("-C", wt, "status")
        self.assertEqual(rc, 0, "the linked worktree is broken after the move")
        rc, lst = out_of("-C", self.at("group-a/data-team/wtrepo"),
                         "worktree", "list", "--porcelain")
        self.assertEqual(rc, 0)
        self.assertNotIn("prunable", lst)

    def test_cross_device_falls_back_to_the_strict_rules(self):
        self.patch("_same_device", lambda a, b: False)
        old = self.clone_old("group-a/legacy-tools/far")
        with open(os.path.join(old, "a.txt"), "a") as fh:
            fh.write("dirty\n")
        self.patch("unique_commits_vs_server",
                   lambda *a, **k: {"ok": True, "count": 0, "detail": ""})
        self.server({"group-a/legacy-tools/far": "group-a/data-team/far"})
        res = self.repair(proj("group-a/data-team/far"))
        self.assertEqual(res["action"], "refused", res)
        self.assertTrue(any("uncommitted" in r for r in res["reasons"]))
        self.assertTrue(os.path.isdir(old))

    def test_cross_device_clean_and_verified_is_moved(self):
        self.patch("_same_device", lambda a, b: False)
        old = self.clone_old("group-a/legacy-tools/far")
        self.patch("unique_commits_vs_server",
                   lambda *a, **k: {"ok": True, "count": 0, "detail": ""})
        self.server({"group-a/legacy-tools/far": "group-a/data-team/far"})
        res = self.repair(proj("group-a/data-team/far"))
        self.assertEqual(res["action"], "moved", res)
        self.assertFalse(os.path.exists(old))
        self.assertEqual(clone_mod.repo_state(self.at("group-a/data-team/far")),
                         "ok")

    def test_credentials_in_the_new_url_are_never_written(self):
        old = self.clone_old("group-a/legacy-tools/cred")
        self.server({"group-a/legacy-tools/cred": "group-a/data-team/cred"})
        p = proj("group-a/data-team/cred",
                 url=f"http://someone:not-a-real-secret@{HOST}/group-a/data-team/cred.git")
        res = self.repair(p)
        self.assertEqual(res["action"], "moved", res)
        url = self.origin(self.at("group-a/data-team/cred"))
        self.assertNotIn("@", url)
        self.assertNotIn("not-a-real-secret", url)
        self.assertFalse(os.path.exists(old))


# ─── refusals ─────────────────────────────────────────────────────────────────

class Refusals(ReconcileCase):

    def test_target_exists_refused_nothing_moved(self):
        old = self.clone_old("group-a/legacy-tools/svc")
        occupied = self.at("group-a/data-team/svc")
        os.makedirs(occupied)
        with open(os.path.join(occupied, "keep-me"), "w") as fh:
            fh.write("x\n")
        self.server({"group-a/legacy-tools/svc": "group-a/data-team/svc"})
        res = self.repair(proj("group-a/data-team/svc"))
        self.assertEqual(res["action"], "refused", res)
        self.assertTrue(any("already exists" in r for r in res["reasons"]))
        self.assertTrue(os.path.isdir(old))
        self.assertEqual(os.listdir(occupied), ["keep-me"])

    def test_pending_deletion_target_is_not_moved(self):
        old = self.clone_old("group-a/legacy-tools/svc")
        gone = "group-a/data-team-deletion_scheduled-42/svc"
        self.server({"group-a/legacy-tools/svc": gone})
        res = self.repair(proj(gone))
        self.assertEqual(res["action"], "pending")
        self.assertTrue(os.path.isdir(old))
        # And a redirect INTO a pending path never moves a clone either.
        res = self.repair(proj("group-a/data-team/svc"))
        self.assertEqual(res["action"], "none")
        self.assertTrue(os.path.isdir(old))

    def test_malicious_new_path_with_dotdot_is_refused(self):
        old = self.clone_old("group-a/legacy-tools/escape")
        evil = "group-a/../../escape"
        self.server({"group-a/legacy-tools/escape": evil})
        p = proj(evil)
        index = clone_mod.build_presence_index(self.dest)
        target = os.path.join(self.dest, "group-a", "..", "..", "escape")
        res = clone_mod.try_rename_repair(self.cfg, p, target, index, HOST,
                                          set(), known=None, root=self.dest)
        self.assertEqual(res["action"], "refused", res)
        self.assertTrue(os.path.isdir(old))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escape")))

    def test_a_target_outside_dest_is_refused(self):
        old = self.clone_old("group-a/legacy-tools/svc")
        self.server({"group-a/legacy-tools/svc": "group-a/data-team/svc"})
        index = clone_mod.build_presence_index(self.dest)
        outside = os.path.join(self.tmp, "elsewhere", "svc")
        res = clone_mod.try_rename_repair(
            self.cfg, proj("group-a/data-team/svc"), outside, index, HOST,
            set(), root=self.dest)
        self.assertEqual(res["action"], "refused", res)
        self.assertTrue(any("escapes" in r for r in res["reasons"]), res)
        self.assertTrue(os.path.isdir(old))
        self.assertFalse(os.path.exists(outside))

    def test_set_url_failure_rolls_the_directory_back(self):
        old = self.clone_old("group-a/legacy-tools/svc")
        self.server({"group-a/legacy-tools/svc": "group-a/data-team/svc"})
        real = clone_mod.gitlib

        class _Shim:
            def __getattr__(self, name):
                return getattr(real, name)

            @staticmethod
            def _run(args, *a, **k):
                if "set-url" in args:
                    return 1, "", "simulated set-url failure"
                return real._run(args, *a, **k)
        self.patch("gitlib", _Shim())
        res = self.repair(proj("group-a/data-team/svc"))
        self.assertEqual(res["action"], "refused", res)
        self.assertIn("renamed back", res["detail"])
        self.assertTrue(os.path.isdir(old))
        self.assertEqual(cov.normalize(self.origin(old)),
                         (HOST, "group-a/legacy-tools/svc"))
        self.assertFalse(os.path.exists(self.at("group-a/data-team/svc")))
        self.assertFalse(os.path.exists(self.at("group-a/data-team")),
                         "the parent created for the rename was not pruned")


# ─── prune ────────────────────────────────────────────────────────────────────

class PruneEmptyParents(ReconcileCase):

    def test_empty_chain_removed_but_dest_root_kept(self):
        leaf = os.path.join(self.dest, "a", "b", "c")
        os.makedirs(leaf)
        clone_mod._prune_empty_parents(os.path.join(leaf, "moved-away"),
                                       self.dest)
        self.assertFalse(os.path.exists(os.path.join(self.dest, "a")))
        self.assertTrue(os.path.isdir(self.dest))

    def test_stops_at_the_first_non_empty_dir(self):
        leaf = os.path.join(self.dest, "a", "b", "c")
        os.makedirs(leaf)
        with open(os.path.join(self.dest, "a", "keep"), "w") as fh:
            fh.write("x\n")
        clone_mod._prune_empty_parents(os.path.join(leaf, "gone"), self.dest)
        self.assertFalse(os.path.exists(os.path.join(self.dest, "a", "b")))
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "a", "keep")))

    def test_the_root_is_never_removed_even_when_empty(self):
        sub = os.path.join(self.dest, "only")
        os.makedirs(sub)
        clone_mod._prune_empty_parents(os.path.join(sub, "x"), self.dest)
        self.assertFalse(os.path.exists(sub))
        self.assertTrue(os.path.isdir(self.dest))
        self.assertEqual(os.listdir(self.dest), [])


# ─── reconcile_renames (the public API Pull All calls) ───────────────────────

class ReconcileRenames(ReconcileCase):

    GROUP = "group-a/data-team"

    def _listing(self, by_group, raise_for=()):
        def get_group(url, headers, gp):
            if gp in raise_for:
                raise api.ApiError(401, "Invalid or expired token")
            if gp not in by_group:
                return None
            return {"id": hash(gp) & 0xffff, "full_path": gp,
                    "path": gp.split("/")[-1]}

        def recursive(cfg, group):
            gp = group["full_path"]
            if gp in raise_for:
                raise api.ApiError(503, "unavailable")
            return by_group[gp]
        self.patch("get_group", get_group)
        self.patch("_recursive_projects", recursive)

    def _run(self, quiet=False):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rows = clone_mod.reconcile_renames(self.cfg, self.GROUP, self.dest,
                                               quiet=quiet)
        return rows, buf.getvalue()

    def test_group_rename_moves_all_children_incl_nested_subgroup(self):
        self.parent_repo_around_dest()
        old_a = self.clone_old("group-a/legacy-tools/a")
        old_b = self.clone_old("group-a/legacy-tools/harvester/b")
        old_c = self.clone_old("group-a/legacy-tools/harvester/c")
        # legacy-tools keeps a pending-deletion clone, so it must survive.
        keep = self.clone_old("group-a/legacy-tools/old-deletion_scheduled-7")
        self.server({
            "group-a/legacy-tools/a": "group-a/data-team/a",
            "group-a/legacy-tools/harvester/b": "group-a/data-team/harvester/b",
            "group-a/legacy-tools/harvester/c": "group-a/data-team/harvester/c",
        })
        current = projs("group-a/data-team/a", "group-a/data-team/harvester/b",
                        "group-a/data-team/harvester/c")
        self._listing({self.GROUP: current, "group-a": current + projs(
            "group-a/legacy-tools/old-deletion_scheduled-7")})

        rows, out = self._run()

        self.assertEqual(self.cloned, [])
        self.assertEqual(sorted(r["reason"] for r in rows),
                         [clone_mod.REASON_RENAMED] * 3, rows)
        for pwn in ("group-a/data-team/a", "group-a/data-team/harvester/b",
                    "group-a/data-team/harvester/c"):
            self.assertEqual(clone_mod.repo_state(self.at(pwn)), "ok", pwn)
            self.assertEqual(cov.normalize(self.origin(self.at(pwn))),
                             (HOST, pwn))
        for gone in (old_a, old_b, old_c):
            self.assertFalse(os.path.exists(gone))
        self.assertFalse(os.path.exists(self.at("group-a/legacy-tools/harvester")),
                         "the emptied old subgroup dir was not pruned")
        self.assertTrue(os.path.isdir(keep))
        self.assertTrue(os.path.isdir(self.at("group-a/legacy-tools")))
        self.assertTrue(os.path.isdir(self.dest))
        self.assertIn("renamed ✓", out)
        self.assertIn("group-a/data-team/harvester/b", out)
        self.assertIn("3 renamed upstream", out)

    def test_the_d2_rename_is_found_through_the_top_level_listing(self):
        old = self.clone_old("group-a/legacy-tools/crawler-panel")
        self.server({"group-a/legacy-tools/crawler-panel":
                     "group-a/data-team/data-team-labeler"})
        current = projs("group-a/data-team/data-team-labeler")
        self._listing({self.GROUP: current,
                       "group-a": current + projs("group-a/misc/tool")})
        rows, _ = self._run(quiet=True)
        self.assertEqual([r["reason"] for r in rows],
                         [clone_mod.REASON_RENAMED])
        self.assertFalse(os.path.exists(old))
        self.assertEqual(
            clone_mod.repo_state(self.at("group-a/data-team/data-team-labeler")),
            "ok")

    def test_nothing_to_do_prints_nothing_and_never_clones(self):
        self.clone_old("group-a/data-team/a")          # already in place
        self.server({})
        current = projs("group-a/data-team/a", "group-a/data-team/brand-new")
        self._listing({self.GROUP: current, "group-a": current})
        rows, out = self._run()
        self.assertEqual(rows, [])
        self.assertEqual(out, "")
        self.assertEqual(self.cloned, [])
        self.assertFalse(os.path.exists(self.at("group-a/data-team/brand-new")))

    def test_api_error_returns_empty_and_never_raises(self):
        self.clone_old("group-a/legacy-tools/a")
        self.server({"group-a/legacy-tools/a": "group-a/data-team/a"})
        self._listing({self.GROUP: []}, raise_for=(self.GROUP,))
        rows, out = self._run()
        self.assertEqual(rows, [])
        self.assertIn("rename check skipped", out)
        self.assertTrue(os.path.isdir(self.at("group-a/legacy-tools/a")))
        self.assertEqual(self.cloned, [])

    def test_missing_group_returns_empty(self):
        self._listing({})
        rows, out = self._run()
        self.assertEqual(rows, [])
        self.assertIn("not found", out)

    def test_top_level_listing_failure_turns_tier_three_off(self):
        old = self.clone_old("group-a/legacy-tools/crawler-panel")
        self.server({"group-a/legacy-tools/crawler-panel":
                     "group-a/data-team/data-team-labeler"})
        self._listing({self.GROUP: projs("group-a/data-team/data-team-labeler"),
                       "group-a": []}, raise_for=("group-a",))
        rows, out = self._run()
        self.assertEqual(rows, [])
        self.assertTrue(os.path.isdir(old))
        self.assertIn("rename search across 'group-a' is off", out)


# ─── cmd_clone threads root + known ──────────────────────────────────────────

class CmdCloneFollowsRenames(ReconcileCase):

    def _stub_group(self, current, top_listing=None, top_raises=False):
        def get_group(url, headers, gp):
            return {"id": 1 if gp == "group-a/data-team" else 2,
                    "full_path": gp, "path": gp.split("/")[-1]}

        def recursive(cfg, group):
            if top_raises:
                raise api.ApiError(503, "unavailable")
            return top_listing if top_listing is not None else current
        self.patch("get_group", get_group)
        self.patch("get_projects", lambda url, h, gid, *a, **k:
                   current if gid == 1 else [])
        self.patch("get_subgroups", lambda url, h, gid, *a, **k: [])
        self.patch("_recursive_projects", recursive)

    def test_cmd_clone_moves_a_d1_d2_rename_instead_of_cloning(self):
        self.parent_repo_around_dest()
        old = self.clone_old("group-a/legacy-tools/crawler-panel")
        self.server({"group-a/legacy-tools/crawler-panel":
                     "group-a/data-team/data-team-labeler"})
        self._stub_group(projs("group-a/data-team/data-team-labeler"))
        with contextlib.redirect_stdout(io.StringIO()):
            clone_mod.cmd_clone(self.cfg, "group-a/data-team", self.dest)
        self.assertEqual(self.cloned, [])
        self.assertFalse(os.path.exists(old))
        self.assertEqual(
            clone_mod.repo_state(self.at("group-a/data-team/data-team-labeler")),
            "ok")

    def test_cmd_clone_survives_a_failed_known_fetch(self):
        self.clone_old("group-a/legacy-tools/crawler-panel")
        self.server({"group-a/legacy-tools/crawler-panel":
                     "group-a/data-team/data-team-labeler"})
        self._stub_group(projs("group-a/data-team/data-team-labeler"),
                         top_raises=True)
        made = []

        def fake_clone(token, url, target):
            made.append(target)
            return {"status": "ok", "detail": ""}
        self.patch("_clone", fake_clone)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            clone_mod.cmd_clone(self.cfg, "group-a/data-team", self.dest)
        self.assertEqual(len(made), 1, "tier 3 off -> the project is cloned")
        self.assertIn("is off this run", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
