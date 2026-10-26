# tests/test_path_anchoring.py
#
# RC-1 / RC-4. The launcher is an alias that runs gitlab_cli.py by absolute
# path without cd-ing, so a cwd-relative destination silently built a SECOND
# MasterGroups tree wherever the shell happened to be (`~/MasterGroups`). The
# menu then kept reading the install-dir tree and reported every group as not
# cloned while a full copy existed elsewhere.
#
# Every test here runs from a working directory that is NOT the install dir —
# that is the only way this class of bug is visible.

import os
import unittest

from _bootstrap import TempTreeTestCase, PROJECT_ROOT
import paths


class AnchorTests(TempTreeTestCase):

    def setUp(self):
        super().setUp()
        self.foreign = os.path.join(self.tmp, "foreign_cwd")
        os.makedirs(self.foreign, exist_ok=True)
        os.chdir(self.foreign)          # never the install dir

    def test_install_root_is_the_cli_directory_not_the_cwd(self):
        self.assertEqual(os.path.realpath(paths.INSTALL_ROOT),
                         os.path.realpath(PROJECT_ROOT))
        self.assertNotEqual(os.path.realpath(paths.INSTALL_ROOT),
                            os.path.realpath(os.getcwd()))

    def test_dot_resolves_to_the_install_dir_not_the_cwd(self):
        self.assertEqual(paths.anchor("."), os.path.normpath(PROJECT_ROOT))

    def test_empty_and_none_behave_like_dot(self):
        self.assertEqual(paths.anchor(""), os.path.normpath(PROJECT_ROOT))
        self.assertEqual(paths.anchor(None), os.path.normpath(PROJECT_ROOT))

    def test_relative_dest_hangs_off_the_install_dir(self):
        self.assertEqual(paths.anchor("MasterGroups"),
                         os.path.join(PROJECT_ROOT, "MasterGroups"))
        self.assertEqual(paths.anchor("./MasterGroups"),
                         os.path.join(PROJECT_ROOT, "MasterGroups"))

    def test_a_relative_dest_never_lands_under_the_current_directory(self):
        # the exact RC-1 symptom: `gitlab-cli clone globex ./here` from ~
        anchored = paths.anchor("./here")
        self.assertFalse(anchored.startswith(os.path.realpath(self.foreign)),
                         f"relative dest scattered into the cwd: {anchored}")
        self.assertTrue(anchored.startswith(os.path.normpath(PROJECT_ROOT)))

    def test_absolute_dest_is_left_alone(self):
        self.assertEqual(paths.anchor(self.dest), os.path.normpath(self.dest))

    def test_anchor_is_idempotent(self):
        once = paths.anchor("MasterGroups")
        self.assertEqual(paths.anchor(once), once)

    def test_anchor_from_two_different_cwds_gives_the_same_answer(self):
        a = paths.anchor("MasterGroups")
        other = os.path.join(self.tmp, "another")
        os.makedirs(other, exist_ok=True)
        os.chdir(other)
        self.assertEqual(paths.anchor("MasterGroups"), a)

    def test_build_cfg_master_dir_is_absolute_and_anchored(self):
        import instances
        cfg = instances.build_cfg({"name": "t", "url": "http://git.example.net",
                                   "token": "unused", "master_dir": "MasterGroups",
                                   "folder": ""})
        self.assertTrue(os.path.isabs(cfg["master_dir"]), cfg["master_dir"])
        self.assertFalse(
            os.path.realpath(cfg["master_dir"]).startswith(
                os.path.realpath(self.foreign)),
            "cfg['master_dir'] resolved against the cwd — RC-1 regression")


class SanitizeAndCandidateTests(TempTreeTestCase):
    """A destination must never be escaped, and a group dir must stay inside it."""

    def test_parent_traversal_is_rejected(self):
        with self.assertRaises(paths.UnsafePath):
            paths.sanitize("../../etc")
        with self.assertRaises(paths.UnsafePath):
            paths.sanitize("a/../../b")

    def test_windows_separator_traversal_is_rejected(self):
        with self.assertRaises(paths.UnsafePath):
            paths.sanitize(r"..\..\etc")

    def test_absolute_group_path_is_rejected(self):
        with self.assertRaises(paths.UnsafePath):
            paths.sanitize("/etc/passwd")

    def test_nested_group_keeps_every_component(self):
        self.assertEqual(paths.sanitize("globex/legacy-tools/geo-scrape"),
                         ["globex", "legacy-tools", "geo-scrape"])

    def test_no_candidate_escapes_the_destination(self):
        for gp in ("initech", "globex/mcps", "globex/legacy-tools/geo-scrape"):
            for c in paths.candidates(gp, self.dest):
                real = os.path.realpath(c)
                self.assertTrue(
                    real == os.path.realpath(self.dest)
                    or real.startswith(os.path.realpath(self.dest) + os.sep),
                    f"{c} escapes {self.dest}")

    def test_clone_target_of_a_nested_group_is_the_full_nested_path(self):
        target = paths.target_for_clone("globex/mcps", self.dest)
        self.assertEqual(target, os.path.join(self.dest, "globex", "mcps"))

    def test_clone_target_is_absolute_when_dest_is_absolute(self):
        self.assertTrue(os.path.isabs(paths.target_for_clone("initech", self.dest)))

    def test_two_groups_sharing_a_last_component_get_different_dirs(self):
        # initech/core vs globex/x/core — the .core_errors.json collision shape
        a = paths.target_for_clone("initech/core", self.dest)
        b = paths.target_for_clone("globex/x/core", self.dest)
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
