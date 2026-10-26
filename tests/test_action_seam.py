#!/usr/bin/env python3
# tests/test_action_seam.py — W4.
#
# RC-2 was fixed in the LISTING code and nowhere else.
#
# The icon next to a group comes from coverage.scan(), which indexes clones by
# their ORIGIN URL and is therefore layout-agnostic: it finds the clone wherever
# it sits. The ACTIONS behind that icon each rebuilt the directory a second way,
# with a weaker rule, and so disagreed with the icon:
#
#   * Sync / Log   paths.resolve(gpath, dest) with the instance `folder`
#                  dropped, so on a drifted layout a group rendered as
#                  "complete" answered "Folder does not exist. Clone it first."
#   * Remotes      os.path.join(dest, gpath.split("/")[-1]) — no resolver at
#                  all. With two instances sharing one master_dir that walked
#                  into the OTHER instance's tree and offered to rewrite its
#                  remotes. Remotes is the credential screen; pointing it at
#                  the wrong tree is a mutation of the wrong data.
#   * Clone        target_for_clone() without `folder`, so a group already on
#                  disk got a second copy written beside the first.
#   * Error log    the menu spelled the filename with the group's LAST path
#                  component while clone._group_slug keys on the FULL path.
#
# Every test below fixes the defect's shape in a throwaway tree of real git
# repositories. `folder` is always passed explicitly, so none of this reads the
# user's ~/.gitlab-cli/instances.json.

import os
import unittest

from _bootstrap import TempTreeTestCase, fake_cfg

import coverage as cov
import gitlab_cli as G
import paths
from clone import _error_log_path


def cfg_for(host, dest, folder=None):
    c = fake_cfg(url=f"http://{host}", master_dir=dest)
    c["folder"] = folder
    return c


class ActionSeamTests(TempTreeTestCase):

    # ── helpers ────────────────────────────────────────────────────────────

    def clone_of(self, relpath, host, path_with_namespace):
        return self.make_repo(relpath,
                              f"http://{host}/{path_with_namespace}.git")

    def sync_lands_on(self, cfg, gpath, dest, index):
        """Where cmd_pull / cmd_log will really look, callee unfixed AND fixed.

        The action seam cannot change pull.py's signature, so it chooses the
        (group_path, dest) pair whose OWN resolver lands on the proven
        directory — and it must hold whether or not the callee later starts
        passing `folder` itself. Both are asserted.
        """
        gdir    = G._group_dir(cfg, gpath, dest, index)
        gp, dst = G._action_args(cfg, gpath, gdir, dest)
        a = os.path.normpath(paths.resolve(gp, dst))
        b = os.path.normpath(paths.resolve(gp, dst, cfg.get("folder")))
        self.assertEqual(a, b, "sync target depends on whether the callee "
                               "passes folder — the seam is not pinned")
        return a

    def clone_writes_to(self, cfg, gpath, dest, index):
        cdest = G._clone_dest(cfg, gpath, dest, index)
        a = os.path.normpath(paths.target_for_clone(gpath, cdest))
        b = os.path.normpath(paths.target_for_clone(gpath, cdest,
                                                    cfg.get("folder")))
        self.assertEqual(a, b)
        return a

    # ── 1. drifted layout: clones under a folder shape the config dropped ──

    def test_sync_finds_a_group_cloned_under_a_drifted_layout(self):
        real = self.clone_of("acme/web/web-frontend",
                             "hub.example", "web/web-frontend")
        want = os.path.dirname(real)
        cfg  = cfg_for("hub.example", self.dest, folder=None)
        idx  = cov.scan(self.dest)

        # precondition: the old, unfixed answer does not exist on disk
        self.assertFalse(os.path.isdir(paths.resolve("web", self.dest)))
        # the icon says the group is present...
        self.assertIn(("hub.example", "web/web-frontend"), idx)
        # ...and the action now agrees with it
        self.assertEqual(self.sync_lands_on(cfg, "web", self.dest, idx), want)

    def test_clone_reuses_a_drifted_tree_instead_of_duplicating_it(self):
        real = self.clone_of("acme/web/web-frontend",
                             "hub.example", "web/web-frontend")
        want = os.path.dirname(real)
        cfg  = cfg_for("hub.example", self.dest, folder=None)
        idx  = cov.scan(self.dest)

        self.assertNotEqual(
            os.path.normpath(paths.target_for_clone("web", self.dest)), want,
            "fixture no longer reproduces the duplication defect")
        self.assertEqual(self.clone_writes_to(cfg, "web", self.dest, idx), want)

    # ── 2. cross-instance collision on a shared master_dir ─────────────────

    def test_remotes_never_audits_another_instances_repos(self):
        """The security screen must not be handed the wrong tree."""
        for i in range(4):                       # the other instance
            self.clone_of(f"globex/p{i}", "git.other", f"globex/p{i}")
        for i in range(2):                       # ours
            self.clone_of(f"acme/globex/q{i}",
                          "hub.example", f"globex/q{i}")

        cfg  = cfg_for("hub.example", self.dest, folder="acme")
        idx  = cov.scan(self.dest)
        gdir = G._group_dir(cfg, "globex", self.dest, idx)

        old  = os.path.join(self.dest, "globex")    # the pre-fix expression
        self.assertTrue(os.path.isdir(old), "fixture broken")
        self.assertNotEqual(os.path.normpath(gdir), os.path.normpath(old))

        mine = foreign = 0
        for r in cov.find_repos(gdir):
            host, path = cov.normalize(cov._origin(r))
            if host == "hub.example" and path.startswith("globex"):
                mine += 1
            else:
                foreign += 1
        self.assertEqual((mine, foreign), (2, 0),
                         "an action reached repos of another instance")

    # ── 3. nested groups ───────────────────────────────────────────────────

    def test_a_nested_group_resolves_to_its_own_directory(self):
        self.clone_of("globex/hooli/svc", "git.x", "globex/hooli/svc")
        self.clone_of("hooli", "git.x", "hooli")      # decoy: another group
        cfg  = cfg_for("git.x", self.dest)
        idx  = cov.scan(self.dest)
        want = os.path.join(self.dest, "globex", "hooli")

        self.assertEqual(G._group_dir(cfg, "globex/hooli", self.dest, idx),
                         want)
        self.assertEqual(
            self.sync_lands_on(cfg, "globex/hooli", self.dest, idx), want)

    def test_the_menu_reads_the_error_log_clone_actually_writes(self):
        self.clone_of("globex/hooli/svc", "git.x", "globex/hooli/svc")
        cfg   = cfg_for("git.x", self.dest)
        idx   = cov.scan(self.dest)
        cdest = G._clone_dest(cfg, "globex/hooli", self.dest, idx)

        self.assertEqual(
            os.path.basename(G._err_log_path(cfg, "globex/hooli",
                                             self.dest, idx)),
            os.path.basename(_error_log_path(cdest, "globex/hooli")))
        # and it is keyed on the full path, not the last component
        self.assertNotEqual(
            os.path.basename(G._err_log_path(cfg, "globex/hooli",
                                             self.dest, idx)),
            ".hooli_errors.json")

    def test_an_existing_error_log_in_the_master_dir_is_still_found(self):
        """Logs written before this change live at dest, not in the group dir."""
        self.clone_of("globex/hooli/svc", "git.x", "globex/hooli/svc")
        cfg    = cfg_for("git.x", self.dest)
        idx    = cov.scan(self.dest)
        legacy = _error_log_path(self.dest, "globex/hooli")
        with open(legacy, "w") as fh:
            fh.write('{"errors": []}')
        self.assertEqual(
            G._err_log_path(cfg, "globex/hooli", self.dest, idx), legacy)

    # ── 4. the cases that must NOT change ──────────────────────────────────

    def test_an_empty_destination_keeps_the_historic_shaping(self):
        cfg = cfg_for("git.x", self.dest)
        self.assertEqual(G._group_dir(cfg, "brand-new", self.dest, {}),
                         paths.resolve("brand-new", self.dest))
        self.assertEqual(self.clone_writes_to(cfg, "brand-new", self.dest, {}),
                         os.path.normpath(
                             paths.target_for_clone("brand-new", self.dest)))

    def test_a_normally_laid_out_group_is_unaffected(self):
        self.clone_of("initech/cart-ms", "git.x", "initech/cart-ms")
        cfg  = cfg_for("git.x", self.dest)
        idx  = cov.scan(self.dest)
        want = os.path.join(self.dest, "initech")
        self.assertEqual(G._group_dir(cfg, "initech", self.dest, idx), want)
        self.assertEqual(self.sync_lands_on(cfg, "initech", self.dest, idx), want)
        self.assertEqual(self.clone_writes_to(cfg, "initech", self.dest, idx),
                         want)

    def test_the_resolvers_answer_wins_when_it_holds_the_group(self):
        """Two identical trees on disk (the RC-3 duplication): the action must
        keep using the one the resolver names, not whichever copy a scan
        happened to record."""
        self.clone_of("initech/cart-ms", "git.x", "initech/cart-ms")
        self.clone_of("globex/initech/cart-ms", "git.x", "initech/cart-ms")
        cfg = cfg_for("git.x", self.dest)
        idx = cov.scan(self.dest)          # one origin -> exactly one of them
        self.assertEqual(G._group_dir(cfg, "initech", self.dest, idx),
                         paths.resolve("initech", self.dest, cfg.get("folder")))

    # ── 5. safety rails ────────────────────────────────────────────────────

    def test_an_action_is_never_widened_to_the_whole_managed_folder(self):
        """A clone sitting directly in dest must not hand an action the entire
        tree — that is how Remotes would reach every other group's repos."""
        self.clone_of("flat-repo", "git.x", "flatgroup")
        self.clone_of("other/r", "git.x", "othergroup/r")
        cfg  = cfg_for("git.x", self.dest)
        idx  = cov.scan(self.dest)
        gdir = G._group_dir(cfg, "flatgroup", self.dest, idx)
        self.assertNotEqual(os.path.normpath(gdir),
                            os.path.normpath(self.dest))

    def test_no_action_directory_escapes_the_destination(self):
        self.clone_of("initech/cart-ms", "git.x", "initech/cart-ms")
        cfg = cfg_for("git.x", self.dest)
        idx = cov.scan(self.dest)
        for gpath in ("initech", "initech/sub", "weird", "a/b/c"):
            for d in (G._group_dir(cfg, gpath, self.dest, idx),
                      G._clone_dest(cfg, gpath, self.dest, idx),
                      G._err_log_path(cfg, gpath, self.dest, idx)):
                self.assertTrue(G._is_inside(self.dest, d) or d == self.dest,
                                f"{d} escaped {self.dest}")

    def test_a_hostile_group_path_cannot_traverse(self):
        cfg = cfg_for("git.x", self.dest)
        for bad in ("../../etc", "/etc/passwd", "a/../../b"):
            d = G._group_dir(cfg, bad, self.dest, {})
            self.assertTrue(G._is_inside(self.dest, d) or
                            os.path.normpath(d) == os.path.normpath(self.dest),
                            f"{bad!r} escaped to {d}")

    def test_an_index_from_another_host_is_ignored(self):
        self.clone_of("acme/web/x", "other.host", "web/x")
        cfg = cfg_for("hub.example", self.dest, folder=None)
        idx = cov.scan(self.dest)
        # nothing of OURS is on disk: fall back to the normal shaping
        self.assertEqual(G._group_dir(cfg, "web", self.dest, idx),
                         paths.resolve("web", self.dest, cfg.get("folder")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
