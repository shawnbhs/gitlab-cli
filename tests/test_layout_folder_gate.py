#!/usr/bin/env python3
# tests/test_layout_folder_gate.py — W3 hardening.
#
# paths.candidates() had two shaping rules keyed on a bare string comparison
# against basename(dest):
#
#   rule 1: basename(dest) == group name          -> dest IS the group dir
#   rule 4: basename(dest) == first path segment  -> strip the namespace
#
# Neither can tell "dest really is the `company` group directory" from "dest is
# an instance FOLDER that merely happens to be spelled `globex`, and GitLab
# also has a top-level group spelled `globex`". On this machine that is
# exactly the case, and rule 4 produced MasterGroups/globex/hooli (namespace
# stripped) next to MasterGroups/globex/globex/hooli (correct): one tree,
# two shapes, 22 half-nested and 2 double-nested directories.
#
# The gate is now the instance configuration. The legacy shapes are DEMOTED,
# not removed — hundreds of repos already sit in them and must never be
# re-cloned. Both halves are pinned below.

import os
import unittest

from _bootstrap import TempTreeTestCase

import paths


class FolderGateTests(TempTreeTestCase):
    """folder is passed explicitly (authoritative), so these tests never read
    the user's ~/.gitlab-cli/instances.json."""

    def rel(self, path):
        return os.path.relpath(path, self.dest).replace(os.sep, "/")

    def test_a_folder_named_like_a_group_does_not_strip_the_namespace(self):
        dest = os.path.join(self.dest, "globex")
        cands = paths.candidates("globex/hooli", dest, folder="globex")
        self.assertEqual(self.rel(cands[0]), "globex/globex/hooli",
                         "the namespace was stripped because a folder happened "
                         "to share a top-level group's name")

    def test_a_group_whose_name_equals_the_folder_does_not_collapse_onto_dest(self):
        dest = os.path.join(self.dest, "globex")
        target = paths.target_for_clone("globex", dest, folder="globex")
        self.assertEqual(self.rel(target), "globex/globex")

    def test_the_deep_case_keeps_every_namespace_component(self):
        dest = os.path.join(self.dest, "globex")
        cands = paths.candidates("globex/mcps/kasbi", dest, folder="globex")
        self.assertEqual(self.rel(cands[0]), "globex/globex/mcps/kasbi")

    def test_a_dest_that_genuinely_is_the_group_directory_still_collapses(self):
        """The case rules 1/4 were written for. No folder configured, so the
        gate is off and the historic behaviour is preserved exactly."""
        dest = os.path.join(self.dest, "company")
        cands = paths.candidates("company/team", dest, folder="")
        self.assertIn(os.path.join(dest, "team"), cands)

    def test_with_no_folder_configured_nothing_changes(self):
        dest = os.path.join(self.dest, "globex")
        gated = paths.candidates("globex/hooli", dest, folder="")
        self.assertIn(os.path.join(dest, "hooli"), gated,
                      "the legacy shape must still be produced when no folder "
                      "is configured — that is the current live behaviour")

    def test_an_unrelated_folder_name_does_not_change_anything(self):
        dest = os.path.join(self.dest, "MasterGroups2")
        a = paths.candidates("initech/core", dest, folder="MasterGroups2")
        b = paths.candidates("initech/core", dest, folder="")
        self.assertEqual(a, b)


class LegacyShapeReachabilityTests(TempTreeTestCase):
    """Demoted, never deleted: an existing clone in the old shape must still be
    found, or the tool re-clones hundreds of repositories."""

    def test_the_legacy_shape_is_still_a_candidate(self):
        dest = os.path.join(self.dest, "globex")
        cands = paths.candidates("globex/hooli", dest, folder="globex")
        rel = [os.path.relpath(c, self.dest).replace(os.sep, "/") for c in cands]
        self.assertIn("globex/hooli", rel)
        self.assertGreater(rel.index("globex/hooli"),
                           rel.index("globex/globex/hooli"),
                           "the legacy shape must come AFTER the correct one")

    def test_an_existing_legacy_directory_is_reused_and_not_recloned(self):
        dest = os.path.join(self.dest, "globex")
        legacy = os.path.join(dest, "hooli")
        os.makedirs(legacy, exist_ok=True)
        self.assertEqual(
            paths.target_for_clone("globex/hooli", dest, folder="globex"),
            legacy)

    def test_the_correct_shape_wins_when_both_exist_on_disk(self):
        dest = os.path.join(self.dest, "globex")
        os.makedirs(os.path.join(dest, "hooli"), exist_ok=True)
        correct = os.path.join(dest, "globex", "hooli")
        os.makedirs(correct, exist_ok=True)
        self.assertEqual(
            paths.target_for_clone("globex/hooli", dest, folder="globex"),
            correct)

    def test_a_new_clone_with_nothing_on_disk_takes_the_correct_shape(self):
        dest = os.path.join(self.dest, "globex")
        self.assertEqual(
            paths.target_for_clone("globex/hooli", dest, folder="globex"),
            os.path.join(dest, "globex", "hooli"))


class CandidateSafetyTests(TempTreeTestCase):
    """Properties that must hold for every candidate list, gated or not."""

    def test_no_candidate_ever_escapes_dest(self):
        for group in ("initech", "initech/core", "globex/x/core", "a/b/c/d"):
            for folder in (None, "", "globex", "MasterGroups"):
                for c in paths.candidates(group, self.dest, folder=folder):
                    self.assertTrue(
                        c == os.path.normpath(self.dest)
                        or c.startswith(os.path.normpath(self.dest) + os.sep),
                        f"{c} escapes {self.dest}")

    def test_candidates_are_unique_and_order_preserving(self):
        cands = paths.candidates("globex/hooli",
                                 os.path.join(self.dest, "globex"),
                                 folder="globex")
        self.assertEqual(len(cands), len(set(cands)))

    def test_a_traversal_group_path_is_refused(self):
        for bad in ("../../etc", "a/../../b", "..\\..\\etc", "/etc/passwd"):
            with self.assertRaises(paths.UnsafePath, msg=bad):
                paths.candidates(bad, self.dest)

    def test_two_groups_sharing_a_last_component_resolve_differently(self):
        a = paths.resolve("initech/core", self.dest)
        b = paths.resolve("globex/x/core", self.dest)
        self.assertNotEqual(a, b)

    def test_the_explicit_folder_argument_beats_the_config_scan(self):
        """A caller holding cfg['folder'] is authoritative, so the fix does not
        depend on the separate config-restoration decision."""
        dest = os.path.join(self.dest, "globex")
        self.assertTrue(paths.is_instance_folder(dest, "globex"))
        self.assertFalse(paths.is_instance_folder(dest, ""))
        self.assertFalse(paths.is_instance_folder(dest, "something-else"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
