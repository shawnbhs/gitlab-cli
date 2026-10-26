#!/usr/bin/env python3
# tests/test_prune_leftovers.py — T6: clean ONLY provably-empty leftovers.
#
# After Pull All followed the group-a/legacy-tools -> group-a/data-team rename,
# the old tree kept its skeleton (measured): an empty clone of a project the
# server has scheduled for deletion (legacy-tools/web-scraper-tools, 0 commits,
# nothing but .git) and empty folders the server answers 404 for
# (legacy-tools/geo-scrape/{maps-a,maps-b}, shop, industry/sso).
# clone.prune_leftovers() removes exactly those and nothing that holds data.
#
# Real directories and real git repos in a mkdtemp tree. The server (group,
# project and subgroup listings, the redirect lookup) is stubbed; nothing
# reaches the network and nothing outside the temp dir is touched.

import io
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

import _bootstrap  # noqa: F401  (path + error-log guard)
from _bootstrap import BASE, HOST, TempTreeTestCase, fake_cfg, git

from tests import _home_guard

import api
import clone
import gitlab_cli as G

GROUP = "group-a"


def setUpModule():
    _home_guard.activate()


def tearDownModule():
    _home_guard.deactivate()


class FakeServer:
    """Group/project/subgroup listings plus per-path redirect states."""

    def __init__(self, projects=(), subgroups=(), states=None,
                 projects_fail=None, subgroups_fail=None, group_missing=False):
        self.projects = list(projects)
        subs = list(subgroups)
        for p in self.projects:              # every project namespace is a group
            parts = p.split("/")[1:-1]
            for i in range(1, len(parts) + 1):
                ns = "/".join([GROUP] + parts[:i])
                if ns not in subs:
                    subs.append(ns)
        self.subgroups = subs
        self.states = dict(states or {})
        self.projects_fail = projects_fail
        self.subgroups_fail = subgroups_fail
        self.group_missing = group_missing
        self.lookups = []
        self.ids = {GROUP: 1}
        for i, sg in enumerate(self.subgroups, 2):
            self.ids[sg] = i

    def get_group(self, url, headers, path):
        if self.group_missing:
            return None
        return {"id": self.ids.get(path, 1), "full_path": path,
                "path": path.split("/")[-1]}

    def get_projects(self, url, headers, gid, *a, **k):
        if self.projects_fail == "raise":
            raise api.ApiError(500, "boom", "http://x")
        ns = next(p for p, i in self.ids.items() if i == gid)
        items = [{"id": i, "path": p.split("/")[-1], "path_with_namespace": p,
                  "http_url_to_repo": f"{BASE}/{p}.git"}
                 for i, p in enumerate(self.projects, 1)
                 if p.rsplit("/", 1)[0] == ns]
        if self.projects_fail == "partial":
            return api.PagedResult(items[:1], complete=False,
                                   error=api.ApiError(0, "partial"))
        if self.projects_fail == "none":
            return None
        return api.PagedResult(items, complete=True)

    def get_subgroups(self, url, headers, gid):
        if self.subgroups_fail == "raise":
            raise api.ApiError(403, "no", "http://x")
        parent = next(p for p, i in self.ids.items() if i == gid)
        kids = [{"id": self.ids[s], "full_path": s, "path": s.split("/")[-1]}
                for s in self.subgroups if s.rsplit("/", 1)[0] == parent]
        if self.subgroups_fail == "partial":
            return api.PagedResult(kids, complete=False,
                                   error=api.ApiError(0, "partial"))
        return api.PagedResult(kids, complete=True)

    def redirect_lookup(self, cfg, path):
        self.lookups.append(path)
        state, new = self.states.get(path, ("undetermined", ""))
        return {"state": state, "new_path": new, "detail": f"stub {state}"}


class PruneCase(TempTreeTestCase):

    def setUp(self):
        super().setUp()
        clone.reset_presence_cache()
        clone.reset_redirect_cache()
        self.cfg = fake_cfg(url=BASE, master_dir=self.dest)
        self.cfg["folder"] = None
        self.root = os.path.join(self.dest, GROUP)
        os.makedirs(self.root)

    def tearDown(self):
        clone.reset_presence_cache()
        clone.reset_redirect_cache()
        super().tearDown()

    # ── fixtures ──────────────────────────────────────────────────────────
    def at(self, rel):
        return os.path.join(self.root, *rel.split("/"))

    def mk(self, rel):
        os.makedirs(self.at(rel), exist_ok=True)
        return self.at(rel)

    def empty_clone(self, rel, origin_pwn=None):
        """What `git clone` of an empty project leaves: only .git, no refs."""
        path = self.mk(rel)
        self.assertEqual(git("init", "-q", path).returncode, 0)
        pwn = origin_pwn or f"{GROUP}/{rel}"
        self.assertEqual(git("remote", "add", "origin", f"{BASE}/{pwn}.git",
                             cwd=path).returncode, 0)
        self.assertEqual(os.listdir(path), [".git"])
        return path

    def full_clone(self, rel, origin_pwn=None):
        path = self.empty_clone(rel, origin_pwn)
        with open(os.path.join(path, "README.md"), "w") as fh:
            fh.write("x\n")
        git("add", "-A", cwd=path)
        self.assertEqual(git("commit", "-q", "-m", "c", cwd=path).returncode, 0)
        return path

    def run_prune(self, srv, quiet=False, real_redirect=False, request=None):
        patches = [
            mock.patch.object(clone, "get_group", side_effect=srv.get_group),
            mock.patch.object(clone, "get_subgroups",
                              side_effect=srv.get_subgroups),
            mock.patch.object(clone, "get_projects",
                              side_effect=srv.get_projects),
            # the recursive listing is NOT how prune lists: fail loudly if used
            mock.patch.object(clone.cov, "remote_projects",
                              side_effect=AssertionError("network")),
        ]
        if real_redirect:
            class _Api:
                ApiError = api.ApiError
            _Api.request = staticmethod(request)
            patches.append(mock.patch.object(clone, "gitlab_api", _Api))
        else:
            patches.append(mock.patch.object(clone, "redirect_lookup",
                                             side_effect=srv.redirect_lookup))
        buf = io.StringIO()
        for p in patches:
            p.start()
        try:
            with redirect_stdout(buf):
                rows = clone.prune_leftovers(self.cfg, GROUP, self.dest,
                                             quiet=quiet)
        finally:
            for p in reversed(patches):
                p.stop()
        self.out = buf.getvalue()
        return rows

    def gone(self, rel):
        return not os.path.lexists(self.at(rel))


# ─── Rule A: empty directories ────────────────────────────────────────────────

class EmptyDirTests(PruneCase):

    def test_empty_non_server_dirs_removed_bottom_up_with_emptied_parents(self):
        self.mk("a/b/c")
        self.mk("a/d")
        live = self.full_clone("live/p")
        srv = FakeServer(projects=[f"{GROUP}/live/p"], subgroups=[f"{GROUP}/live"])
        rows = self.run_prune(srv)
        for rel in ("a/b/c", "a/b", "a/d", "a"):
            self.assertTrue(self.gone(rel), rel)
        self.assertTrue(os.path.isfile(os.path.join(live, "README.md")))
        self.assertEqual({r["kind"] for r in rows}, {"empty_dir"})
        self.assertEqual(sorted(r["path"] for r in rows),
                         sorted(f"{GROUP}/{x}" for x in ("a", "a/b", "a/b/c", "a/d")))
        self.assertIn("🧹 Cleaned 4 leftover(s)", self.out)

    def test_empty_live_subgroup_and_live_project_path_kept(self):
        self.mk("empty-sub")                 # project-less live subgroup
        self.mk("Live/Deep/x")               # live project not cloned yet (case differs)
        srv = FakeServer(projects=[f"{GROUP}/live/deep/x"],
                         subgroups=[f"{GROUP}/empty-sub", f"{GROUP}/live",
                                    f"{GROUP}/live/deep"])
        rows = self.run_prune(srv)
        self.assertEqual(rows, [])
        for rel in ("empty-sub", "Live/Deep/x", "Live/Deep", "Live"):
            self.assertTrue(os.path.isdir(self.at(rel)), rel)
        self.assertEqual(self.out, "")

    def test_nested_live_subgroup_found_only_via_subgroup_listing(self):
        self.mk("s1/s2")
        srv = FakeServer(subgroups=[f"{GROUP}/s1", f"{GROUP}/s1/s2"])
        self.assertEqual(self.run_prune(srv), [])
        self.assertTrue(os.path.isdir(self.at("s1/s2")))

    def test_group_root_and_dest_never_removed(self):
        rows = self.run_prune(FakeServer())
        self.assertEqual(rows, [])
        self.assertTrue(os.path.isdir(self.root))
        self.assertTrue(os.path.isdir(self.dest))
        self.mk("x")
        self.run_prune(FakeServer())
        self.assertTrue(self.gone("x"))
        self.assertTrue(os.path.isdir(self.root))
        self.assertTrue(os.path.isdir(self.dest))

    def test_dir_with_a_stray_file_kept(self):
        self.mk("a/b")
        with open(self.at("a/b/note.txt"), "w") as fh:
            fh.write("keep me\n")
        self.mk("a/empty")
        rows = self.run_prune(FakeServer())
        self.assertTrue(os.path.isfile(self.at("a/b/note.txt")))
        self.assertTrue(self.gone("a/empty"))
        self.assertEqual([r["path"] for r in rows], [f"{GROUP}/a/empty"])

    def test_symlink_kept_and_not_followed(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(os.path.join(outside, "inner-empty"))
        os.symlink(outside, self.at("link"))
        os.makedirs(os.path.join(self.tmp, "dangling-target-empty"))
        os.symlink(os.path.join(self.tmp, "dangling-target-empty"),
                   self.at("link2"))
        rows = self.run_prune(FakeServer())
        self.assertEqual(rows, [])
        self.assertTrue(os.path.islink(self.at("link")))
        self.assertTrue(os.path.islink(self.at("link2")))
        self.assertTrue(os.path.isdir(os.path.join(outside, "inner-empty")))

    def test_clean_tree_asks_the_server_nothing(self):
        self.full_clone("live/p")
        os.makedirs(self.at("docs"))
        with open(self.at("docs/x.md"), "w") as fh:
            fh.write("x\n")
        gg = mock.MagicMock(side_effect=AssertionError("network"))
        with mock.patch.object(clone, "get_group", gg), \
                mock.patch.object(clone, "redirect_lookup", gg), \
                redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(clone.prune_leftovers(self.cfg, GROUP, self.dest), [])
        gg.assert_not_called()
        self.assertEqual(buf.getvalue(), "")

    def test_never_descends_into_a_repo(self):
        live = self.full_clone("live/p")
        os.makedirs(os.path.join(live, "build", "empty"))
        srv = FakeServer(projects=[f"{GROUP}/live/p"])
        self.assertEqual(self.run_prune(srv), [])
        self.assertTrue(os.path.isdir(os.path.join(live, "build", "empty")))


# ─── Rule B: dead, provably empty clones ──────────────────────────────────────

class DeadCloneTests(PruneCase):

    def test_pending_deletion_empty_clone_removed(self):
        self.empty_clone("old/proj")
        srv = FakeServer(states={f"{GROUP}/old/proj": (
            "pending_deletion", f"{GROUP}/old/proj-deletion_scheduled-9")})
        rows = self.run_prune(srv)
        self.assertTrue(self.gone("old/proj"))
        self.assertTrue(self.gone("old"))
        self.assertEqual(rows[0]["kind"], "empty_dead_clone")
        self.assertEqual(rows[0]["path"], f"{GROUP}/old/proj")
        self.assertEqual(srv.lookups, [f"{GROUP}/old/proj"])

    def test_gone_empty_clone_removed(self):
        self.empty_clone("dead")
        rows = self.run_prune(FakeServer(states={f"{GROUP}/dead": ("gone", "")}))
        self.assertTrue(self.gone("dead"))
        self.assertEqual([r["kind"] for r in rows], ["empty_dead_clone"])

    def test_renamed_clone_not_removed(self):
        p = self.empty_clone("old")
        rows = self.run_prune(FakeServer(
            states={f"{GROUP}/old": ("renamed", f"{GROUP}/new")}))
        self.assertEqual(rows, [])
        self.assertTrue(os.path.isdir(os.path.join(p, ".git")))

    def test_undetermined_or_same_or_unknown_state_kept(self):
        a = self.empty_clone("a")
        b = self.empty_clone("b")
        c = self.empty_clone("c")
        srv = FakeServer(states={f"{GROUP}/a": ("undetermined", ""),
                                 f"{GROUP}/b": ("same", f"{GROUP}/b"),
                                 f"{GROUP}/c": ("weird-new-state", "")})
        self.assertEqual(self.run_prune(srv), [])
        for p in (a, b, c):
            self.assertTrue(os.path.isdir(os.path.join(p, ".git")), p)

    def test_clone_with_one_commit_kept(self):
        p = self.full_clone("dead")
        os.remove(os.path.join(p, "README.md"))      # only .git left on disk
        self.assertEqual(os.listdir(p), [".git"])
        git("read-tree", "--empty", cwd=p)
        self.assertEqual(self.run_prune(
            FakeServer(states={f"{GROUP}/dead": ("gone", "")})), [])
        self.assertTrue(os.path.isdir(os.path.join(p, ".git")))

    def test_clone_with_only_an_untracked_file_kept(self):
        p = self.empty_clone("dead")
        with open(os.path.join(p, "notes.txt"), "w") as fh:
            fh.write("unsaved work\n")
        self.assertEqual(self.run_prune(
            FakeServer(states={f"{GROUP}/dead": ("gone", "")})), [])
        self.assertTrue(os.path.isfile(os.path.join(p, "notes.txt")))
        self.assertTrue(os.path.isdir(os.path.join(p, ".git")))

    def test_clone_with_only_a_staged_file_kept(self):
        p = self.empty_clone("dead")
        with open(os.path.join(p, "f.txt"), "w") as fh:
            fh.write("staged\n")
        git("add", "f.txt", cwd=p)
        os.remove(os.path.join(p, "f.txt"))
        self.assertEqual(os.listdir(p), [".git"])
        self.assertEqual(self.run_prune(
            FakeServer(states={f"{GROUP}/dead": ("gone", "")})), [])
        self.assertTrue(os.path.isdir(os.path.join(p, ".git")))

    def test_clone_with_stash_kept(self):
        p = self.full_clone("dead")
        with open(os.path.join(p, "README.md"), "w") as fh:
            fh.write("changed\n")
        self.assertEqual(git("stash", "-q", cwd=p).returncode, 0)
        branch = git("symbolic-ref", "--short", "HEAD", cwd=p).stdout.strip()
        git("update-ref", "-d", f"refs/heads/{branch}", cwd=p)
        git("read-tree", "--empty", cwd=p)
        os.remove(os.path.join(p, "README.md"))
        self.assertEqual(os.listdir(p), [".git"])
        self.assertTrue(git("stash", "list", cwd=p).stdout.strip())
        self.assertEqual(self.run_prune(
            FakeServer(states={f"{GROUP}/dead": ("gone", "")})), [])
        self.assertTrue(git("stash", "list", cwd=p).stdout.strip())

    def test_clone_with_reflog_entries_kept(self):
        p = self.empty_clone("dead")
        logs = os.path.join(p, ".git", "logs")
        os.makedirs(logs)
        with open(os.path.join(logs, "HEAD"), "w") as fh:
            fh.write("0" * 40 + " " + "1" * 40 + " t <t@t> 1 +0000\tcommit\n")
        self.assertEqual(self.run_prune(
            FakeServer(states={f"{GROUP}/dead": ("gone", "")})), [])
        self.assertTrue(os.path.isfile(os.path.join(logs, "HEAD")))

    def test_clone_with_linked_worktree_kept(self):
        p = self.empty_clone("dead")
        wt = os.path.join(p, ".git", "worktrees", "wt1")
        os.makedirs(wt)
        with open(os.path.join(wt, "gitdir"), "w") as fh:
            fh.write(os.path.join(self.tmp, "wt1", ".git") + "\n")
        self.assertEqual(self.run_prune(
            FakeServer(states={f"{GROUP}/dead": ("gone", "")})), [])
        self.assertTrue(os.path.isdir(wt))

    def test_clone_whose_git_is_a_gitfile_kept(self):
        real = os.path.join(self.tmp, "elsewhere.git")
        self.assertEqual(git("init", "-q", "--bare", real).returncode, 0)
        p = self.mk("dead")
        with open(os.path.join(p, ".git"), "w") as fh:
            fh.write(f"gitdir: {real}\n")
        git("config", "remote.origin.url", f"{BASE}/{GROUP}/dead.git", cwd=p)
        self.assertEqual(self.run_prune(
            FakeServer(states={f"{GROUP}/dead": ("gone", "")})), [])
        self.assertTrue(os.path.isfile(os.path.join(p, ".git")))
        self.assertTrue(os.path.isdir(real))

    def test_live_project_clone_never_touched_even_if_empty(self):
        p = self.empty_clone("live")
        srv = FakeServer(projects=[f"{GROUP}/live"],
                         states={f"{GROUP}/live": ("gone", "")})
        self.assertEqual(self.run_prune(srv), [])
        self.assertTrue(os.path.isdir(os.path.join(p, ".git")))
        self.assertEqual(srv.lookups, [])

    def test_other_host_clone_never_touched(self):
        p = self.mk("foreign")
        git("init", "-q", p)
        git("remote", "add", "origin", "http://other.host/x/y.git", cwd=p)
        srv = FakeServer(states={"x/y": ("gone", "")})
        self.assertEqual(self.run_prune(srv), [])
        self.assertTrue(os.path.isdir(os.path.join(p, ".git")))
        self.assertEqual(srv.lookups, [])


# ─── Uncertain server data: delete nothing, never raise ───────────────────────

class ListingFailureTests(PruneCase):

    def _fixture(self):
        self.mk("a/b")
        self.empty_clone("dead")
        return {f"{GROUP}/dead": ("gone", "")}

    def _assert_untouched(self):
        self.assertTrue(os.path.isdir(self.at("a/b")))
        self.assertTrue(os.path.isdir(self.at("dead/.git")))

    def test_partial_project_listing_removes_nothing(self):
        rows = self.run_prune(FakeServer(states=self._fixture(),
                                         projects_fail="partial"))
        self.assertEqual(rows, [])
        self._assert_untouched()
        self.assertIn("nothing was removed", self.out)

    def test_none_project_listing_removes_nothing(self):
        self.assertEqual(self.run_prune(FakeServer(
            states=self._fixture(), projects_fail="none")), [])
        self._assert_untouched()

    def test_partial_subgroup_listing_removes_nothing(self):
        self.assertEqual(self.run_prune(FakeServer(
            states=self._fixture(), subgroups_fail="partial")), [])
        self._assert_untouched()

    def test_listing_raises_returns_empty_and_does_not_raise(self):
        states = self._fixture()
        for kw in ({"projects_fail": "raise"}, {"subgroups_fail": "raise"}):
            rows = self.run_prune(FakeServer(states=states, **kw))
            self.assertEqual(rows, [])
            self._assert_untouched()
            self.assertIn("leftover cleanup skipped", self.out)

    def test_group_not_found_removes_nothing(self):
        self.assertEqual(self.run_prune(FakeServer(
            states=self._fixture(), group_missing=True)), [])
        self._assert_untouched()

    def test_get_group_raising_does_not_raise(self):
        self._fixture()
        with mock.patch.object(clone, "get_group",
                               side_effect=api.ApiError(401, "x", "u")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(clone.prune_leftovers(self.cfg, GROUP, self.dest), [])
        self._assert_untouched()

    def test_missing_group_folder_is_a_no_op(self):
        os.rmdir(self.root)
        self.assertEqual(self.run_prune(FakeServer()), [])
        self.assertTrue(os.path.isdir(self.dest))


# ─── The measured real layout ─────────────────────────────────────────────────

class MeasuredLayoutTests(PruneCase):

    def test_real_group_leftovers_cleaned_live_dirs_intact(self):
        wst_old = f"{GROUP}/legacy-tools/web-scraper-tools"
        wst_new = f"{GROUP}/data-team/web-scraper-tools-deletion_scheduled-47"
        self.empty_clone("legacy-tools/web-scraper-tools", wst_old)
        for rel in ("legacy-tools/geo-scrape/maps-a", "legacy-tools/geo-scrape/maps-b",
                    "shop", "industry/sso"):
            self.mk(rel)
        labeler = self.full_clone("data-team/data-team-labeler")
        other = self.full_clone("other-team/svc")

        class Resp:
            def __init__(self, code, body):
                self.status_code, self._b = code, body

            def json(self):
                return self._b

        calls = []

        def request(url, headers, params=None, quiet=False):
            from urllib.parse import unquote
            path = unquote(url.rsplit("/projects/", 1)[-1])
            calls.append(path)
            if path == wst_old:
                return Resp(200, {"id": 47, "path_with_namespace": wst_new})
            return Resp(404, {"message": "404 Project Not Found"})

        srv = FakeServer(
            projects=[f"{GROUP}/data-team/data-team-labeler", wst_new,
                      f"{GROUP}/other-team/svc"],
            subgroups=[f"{GROUP}/data-team", f"{GROUP}/other-team"])
        rows = self.run_prune(srv, real_redirect=True, request=request)

        for rel in ("legacy-tools", "shop", "industry"):
            self.assertTrue(self.gone(rel), rel)
        self.assertTrue(os.path.isfile(os.path.join(labeler, "README.md")))
        self.assertTrue(os.path.isfile(os.path.join(other, "README.md")))
        self.assertTrue(os.path.isdir(self.root))
        self.assertEqual(calls, [wst_old])
        got = sorted((r["kind"], r["path"]) for r in rows)
        want = sorted([("empty_dead_clone", wst_old)] + [
            ("empty_dir", f"{GROUP}/{x}") for x in (
                "legacy-tools/geo-scrape/maps-a", "legacy-tools/geo-scrape/maps-b",
                "legacy-tools/geo-scrape", "legacy-tools", "shop", "industry/sso",
                "industry")])
        self.assertEqual(got, want)
        self.assertIn("🧹 Cleaned 8 leftover(s) — nothing with data was removed",
                      self.out)

    def test_quiet_prints_nothing(self):
        self.mk("shop")
        rows = self.run_prune(FakeServer(), quiet=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.out, "")


# ─── Wiring ───────────────────────────────────────────────────────────────────

class SyncWiringTests(TempTreeTestCase):

    def setUp(self):
        super().setUp()
        self.cfg = fake_cfg(url=BASE, master_dir=self.dest)
        self.cfg["folder"] = None
        self.calls = []

    def _run(self, prune_exc=None):
        def reconcile(cfg, gp, dest, quiet=False):
            self.calls.append(("reconcile", gp))
            return []

        def pull(cfg, gp, dest=".", *a, **k):
            self.calls.append(("pull", gp))

        def prune(cfg, gp, dest, quiet=False):
            self.calls.append(("prune", gp, dest))
            if prune_exc:
                raise prune_exc
            return []

        buf = io.StringIO()
        with mock.patch.object(clone, "reconcile_renames", side_effect=reconcile), \
                mock.patch.object(clone, "prune_leftovers", side_effect=prune), \
                mock.patch.object(G, "cmd_pull", side_effect=pull), \
                redirect_stdout(buf):
            G._sync_group(self.cfg, "acme/data", self.dest, {})
        return buf.getvalue()

    def test_sync_group_prunes_after_cmd_pull(self):
        self._run()
        self.assertEqual([c[0] for c in self.calls], ["reconcile", "pull", "prune"])
        self.assertEqual(self.calls[2][1], "acme/data")
        self.assertEqual(self.calls[2][2],
                         G._clone_dest(self.cfg, "acme/data", self.dest, {}))

    def test_sync_group_survives_a_raising_prune(self):
        out = self._run(prune_exc=RuntimeError("kaput"))
        self.assertEqual([c[0] for c in self.calls], ["reconcile", "pull", "prune"])
        self.assertIn("Leftover cleanup skipped", out)


class CloneWiringTests(TempTreeTestCase):

    def setUp(self):
        super().setUp()
        self.cfg = fake_cfg(url=BASE, master_dir=self.dest)
        self.cfg["folder"] = None

    def _run(self, group):
        prune = mock.MagicMock(return_value=[])
        with mock.patch.object(clone, "get_group", return_value=group), \
                mock.patch.object(clone, "_process_group", return_value=[]), \
                mock.patch.object(clone, "presence_index", return_value={}), \
                mock.patch.object(clone, "prune_leftovers", prune), \
                redirect_stdout(io.StringIO()):
            clone.cmd_clone(self.cfg, GROUP, self.dest)
        return prune

    def test_cmd_clone_prunes_at_the_end(self):
        prune = self._run({"id": 1, "full_path": GROUP, "path": GROUP})
        prune.assert_called_once_with(self.cfg, GROUP, self.dest)

    def test_cmd_clone_does_not_prune_on_early_abort(self):
        prune = self._run(None)
        prune.assert_not_called()

    def test_cmd_clone_does_not_prune_when_processing_raises(self):
        prune = mock.MagicMock(return_value=[])
        with mock.patch.object(clone, "get_group",
                               return_value={"id": 1, "full_path": GROUP}), \
                mock.patch.object(clone, "_process_group",
                                  side_effect=api.ApiError(500, "x", "u")), \
                mock.patch.object(clone, "presence_index", return_value={}), \
                mock.patch.object(clone, "prune_leftovers", prune), \
                redirect_stdout(io.StringIO()), \
                self.assertRaises(api.ApiError):
            clone.cmd_clone(self.cfg, GROUP, self.dest)
        prune.assert_not_called()


if __name__ == "__main__":
    unittest.main()
