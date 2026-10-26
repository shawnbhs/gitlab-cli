#!/usr/bin/env python3
# tests/test_w4_action_folder_gate.py
#
# Wave-4 regression tests for D1/D2/D3 (reports/W3-8-interactive.md).
#
# ONE root cause, three seams: the group's local directory was computed
# WITHOUT the active instance's `folder`, while the group's ✅/◐/❌ icon came
# from coverage.scan(), which is layout-agnostic and finds a clone wherever it
# sits. Status was computed one way and the action taken another — RC-2
# reintroduced at a different seam.
#
#   D2  pull.py / log.py   — Sync and Log reported "folder does not exist" on
#                            20 of the 23 groups the list showed as cloned.
#   D3  clone.py           — Clone All would have written a SECOND copy of 407
#                            already-present repositories.
#   D1  gitlab_cli.py:575  — the per-group *Remotes* screen bypassed the
#                            resolver entirely (`os.path.join(dest, gpath
#                            .split("/")[-1])`) and inspected 91 repositories
#                            belonging to a DIFFERENT GitLab instance, because
#                            both instances share `master_dir`. That screen is
#                            the one that offers to rewrite remotes and move
#                            credentials into the credential store, so it was
#                            a mutation of the wrong data behind a mislabelled
#                            prompt.
#
# These tests assert on OUTCOMES — which directory an action lands on, whether
# a second clone is attempted — not on which helper was called, so they
# survive a restructuring of the resolver.
#
# Offline: api/git are stubbed, no token, the real MasterGroups is never read.

import json
import os
import re
import unittest

from _bootstrap import TempTreeTestCase, fake_cfg, git

import clone as clone_mod
import coverage as cov
import gitlab_cli
import paths

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The live shape this was found on: instance `Hub`, whose config lost its
# `folder` key while 442 clones sit under `MasterGroups/acme/`.
FOLDER = "acme"
HUB = "https://hub.example"
OTHER = "http://git.example.net"


class DriftedLayoutTestCase(TempTreeTestCase):
    """
    Builds the exact disk shape that broke: the group's clones live under
    <dest>/acme/<group>/ while the config produces <dest>/<group>/.
    """

    def setUp(self):
        super().setUp()
        # An instances.json with NO folder key anywhere — the real state of
        # this machine, and the reason the value has to come from cfg.
        self.instances_file = os.path.join(self.tmp, "instances.json")
        with open(self.instances_file, "w", encoding="utf-8") as fh:
            json.dump({"instances": [{"name": "Hub", "url": HUB}]}, fh)
        self._real_instances_file = paths.INSTANCES_FILE
        paths.INSTANCES_FILE = self.instances_file
        paths._folder_cache.update(key=None, folders=frozenset())

        self.cfg = fake_cfg(url=HUB, master_dir=self.dest)
        self.cfg["folder"] = FOLDER
        clone_mod.reset_presence_cache()

    def tearDown(self):
        paths.INSTANCES_FILE = self._real_instances_file
        paths._folder_cache.update(key=None, folders=frozenset())
        clone_mod.reset_presence_cache()
        super().tearDown()

    def clone_at(self, relpath, origin):
        """A real git repo at <dest>/<relpath> with the given origin."""
        path = os.path.join(self.dest, *relpath.split("/"))
        os.makedirs(path, exist_ok=True)
        self.assertEqual(git("init", "-q", path).returncode, 0)
        git("remote", "add", "origin", origin, cwd=path)
        return path


class ActionLandsWhereTheClonesAreTests(DriftedLayoutTestCase):
    """
    D1/D2. The invariant: the directory an action operates on must be the
    directory the coverage index found the group's clones in. If the icon can
    see them, so must Sync, Log and Remotes.
    """

    def test_action_dir_follows_the_drifted_layout(self):
        real = self.clone_at(f"{FOLDER}/web/web-frontend", f"{HUB}/web/web-frontend.git")
        index = cov.scan(self.dest)

        gdir = gitlab_cli._group_dir(self.cfg, "web", self.dest, index)

        self.assertTrue(
            os.path.isdir(gdir),
            f"the action directory {gdir!r} does not exist, while the group's "
            f"clone sits at {real!r} and the list shows the group as complete")
        self.assertEqual(os.path.normpath(gdir),
                         os.path.normpath(os.path.dirname(real)))

    def test_action_dir_is_not_the_other_instances_tree(self):
        # The cross-instance collision: both instances share master_dir, and
        # the OTHER instance's `globex` tree sits exactly where the naive
        # last-component join points.
        for i in range(3):
            self.clone_at(f"globex/svc{i}", f"{OTHER}/globex/svc{i}.git")
        mine = self.clone_at(f"{FOLDER}/globex/hooli-front",
                             f"{HUB}/globex/hooli-front.git")
        index = cov.scan(self.dest)

        gdir = os.path.normpath(
            gitlab_cli._group_dir(self.cfg, "globex", self.dest, index))
        foreign = os.path.normpath(os.path.join(self.dest, "globex"))

        self.assertNotEqual(
            gdir, foreign,
            "the per-group action pointed at the other instance's tree — the "
            "Remotes screen would have offered to rewrite 3 remotes the user "
            "never selected")
        self.assertEqual(gdir, os.path.normpath(os.path.dirname(mine)))

    def test_action_dir_never_escapes_the_managed_folder(self):
        self.clone_at(f"{FOLDER}/web/web-frontend", f"{HUB}/web/web-frontend.git")
        index = cov.scan(self.dest)
        gdir = os.path.realpath(
            gitlab_cli._group_dir(self.cfg, "web", self.dest, index))
        dest = os.path.realpath(self.dest)
        self.assertTrue(gdir == dest or gdir.startswith(dest + os.sep))

    def test_action_dir_for_a_nested_group_keeps_its_namespace(self):
        real = self.clone_at(f"{FOLDER}/globex/hooli/hooli-front",
                             f"{HUB}/globex/hooli/hooli-front.git")
        index = cov.scan(self.dest)
        gdir = os.path.normpath(
            gitlab_cli._group_dir(self.cfg, "globex/hooli", self.dest, index))
        self.assertEqual(gdir, os.path.normpath(os.path.dirname(real)))
        self.assertNotEqual(
            gdir, os.path.normpath(os.path.join(self.dest, "hooli")),
            "a nested group must not collapse to its last path component")

    def test_uncloned_group_still_gets_a_sane_suggestion(self):
        index = cov.scan(self.dest)
        gdir = os.path.normpath(
            gitlab_cli._group_dir(self.cfg, "brand-new", self.dest, index))
        self.assertTrue(gdir.startswith(os.path.normpath(self.dest)))
        self.assertIn("brand-new", gdir)


class CloneDoesNotDuplicateTests(DriftedLayoutTestCase):
    """
    D3. The outcome that matters is not which resolver ran — it is whether a
    second copy gets written. `_process_group` must recognise a project that
    is already on disk ANYWHERE under dest.
    """

    def _stub_api(self, projects, subgroups=()):
        saved = (clone_mod.get_projects, clone_mod.get_subgroups,
                 clone_mod.clone_repo)
        self.cloned = []

        def fake_clone(token, url, target):
            self.cloned.append(target)
            os.makedirs(target, exist_ok=True)
            git("init", "-q", target)
            git("remote", "add", "origin", url, cwd=target)
            return True

        clone_mod.get_projects = lambda *a, **k: list(projects)
        clone_mod.get_subgroups = lambda *a, **k: list(subgroups)
        clone_mod.clone_repo = fake_clone
        self.addCleanup(lambda: (
            setattr(clone_mod, "get_projects", saved[0]),
            setattr(clone_mod, "get_subgroups", saved[1]),
            setattr(clone_mod, "clone_repo", saved[2])))

    def test_a_project_cloned_under_the_drifted_layout_is_not_cloned_again(self):
        self.clone_at(f"{FOLDER}/web/web-frontend", f"{HUB}/web/web-frontend.git")
        self._stub_api([{
            "id": 1, "path": "web-frontend",
            "path_with_namespace": "web/web-frontend",
            "http_url_to_repo": f"{HUB}/web/web-frontend.git",
        }])

        index = clone_mod.build_presence_index(self.dest)
        clone_mod._process_group(
            self.cfg, {"id": 1, "full_path": "web", "path": "web"},
            os.path.join(self.dest, "web"), index=index)

        self.assertEqual(
            self.cloned, [],
            "Clone All would have written a second copy of a repository that "
            "is already on disk under the instance's folder")
        self.assertFalse(
            os.path.isdir(os.path.join(self.dest, "web", "web-frontend")),
            "a duplicate tree was created beside the existing clone")

    def test_a_genuinely_missing_project_is_still_cloned(self):
        # Negative control: the dedupe must not turn into "never clone".
        self.clone_at(f"{FOLDER}/web/present", f"{HUB}/web/present.git")
        self._stub_api([
            {"id": 1, "path": "present",
             "path_with_namespace": "web/present",
             "http_url_to_repo": f"{HUB}/web/present.git"},
            {"id": 2, "path": "absent",
             "path_with_namespace": "web/absent",
             "http_url_to_repo": f"{HUB}/web/absent.git"},
        ])

        index = clone_mod.build_presence_index(self.dest)
        clone_mod._process_group(
            self.cfg, {"id": 1, "full_path": "web", "path": "web"},
            os.path.join(self.dest, "web"), index=index)

        self.assertEqual(len(self.cloned), 1)
        self.assertTrue(self.cloned[0].endswith("absent"))

    def test_a_same_named_project_on_another_instance_is_not_mistaken_for_ours(self):
        # `globex/hooli/hooli-front` genuinely exists on BOTH instances.
        # Identity is the origin URL, never the path or the basename.
        self.clone_at("globex/hooli-front", f"{OTHER}/globex/hooli-front.git")
        self._stub_api([{
            "id": 1, "path": "hooli-front",
            "path_with_namespace": "globex/hooli-front",
            "http_url_to_repo": f"{HUB}/globex/hooli-front.git",
        }])

        index = clone_mod.build_presence_index(self.dest)
        clone_mod._process_group(
            self.cfg, {"id": 1, "full_path": "globex", "path": "globex"},
            os.path.join(self.dest, FOLDER, "globex"), index=index)

        self.assertEqual(
            len(self.cloned), 1,
            "a project with the same path on a DIFFERENT instance was treated "
            "as already present — the Hub project would never be cloned")


class FolderValueChangesTheAnswerTests(DriftedLayoutTestCase):
    """
    Passing the folder is not cosmetic: with a folder-less instances.json it
    is the ONLY thing that distinguishes an instance folder from a top-level
    group that happens to share its name. Pinned so nobody "simplifies" the
    call sites back to two arguments.
    """

    def test_folder_named_like_a_group_does_not_swallow_the_namespace(self):
        folder_dest = os.path.join(self.dest, FOLDER)

        with_folder = paths.target_for_clone(FOLDER, folder_dest, FOLDER)
        without_folder = paths.target_for_clone(FOLDER, folder_dest)

        self.assertEqual(with_folder, os.path.join(folder_dest, FOLDER))
        self.assertEqual(without_folder, folder_dest)
        self.assertNotEqual(
            with_folder, without_folder,
            "the folder argument must change the answer — otherwise the call "
            "sites are free to keep omitting it")

    def test_nested_group_never_collapses_to_its_last_component(self):
        naive = os.path.join(self.dest, "globex/hooli".split("/")[-1])
        correct = paths.resolve("globex/hooli", self.dest)
        self.assertNotEqual(
            naive, correct,
            "the last-component shortcut and the resolver must not be "
            "interchangeable — they are exactly the D1 bug")
        self.assertEqual(correct, os.path.join(self.dest, "globex", "hooli"))

    def test_legacy_shape_stays_reachable_but_never_first(self):
        # Hundreds of repos already sit in the legacy shape; the gate must not
        # orphan them, it must only stop NEW clones taking that shape.
        folder_dest = os.path.join(self.dest, FOLDER)
        cands = paths.candidates(FOLDER, folder_dest, FOLDER)
        self.assertEqual(cands[0], os.path.join(folder_dest, FOLDER))
        self.assertIn(folder_dest, cands)


class NoResolverBypassTests(unittest.TestCase):
    """
    D1 is a *structural* defect: a command re-implemented path resolution
    inline instead of calling the one door. Two string expressions can be
    equally valid to a behavioural test, so this one reads the source.

    Rule: no module may join a destination to a group's last path component by
    hand. `paths.resolve` / `paths.target_for_clone` / `_group_dir` exist for it.
    """

    BYPASS = re.compile(
        r"os\.path\.join\(\s*dest\s*,[^)\n]*?\.split\(\s*[\"']/[\"']\s*\)\s*\[\s*-1\s*\]")

    def _source(self, name):
        with open(os.path.join(PROJECT_ROOT, name), encoding="utf-8") as fh:
            return fh.read()

    def test_no_module_builds_a_group_dir_by_hand(self):
        for name in ("gitlab_cli.py", "pull.py", "log.py", "clone.py"):
            with self.subTest(module=name):
                self.assertEqual(
                    self.BYPASS.findall(self._source(name)), [],
                    f"{name} joins dest to a group's last path component "
                    f"directly. On a nested group that points at the wrong "
                    f"directory; with two instances sharing master_dir it "
                    f"points at the OTHER instance's tree.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
