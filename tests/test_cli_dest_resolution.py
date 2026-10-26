#!/usr/bin/env python3
# tests/test_cli_dest_resolution.py — W3 hardening.
#
# The Wave-0 fix anchored master_dir inside instances.build_cfg — a CALLER, not
# the boundary. gitlab_cli.py still handed raw argv to cmd_clone/cmd_pull/
# cmd_log, and cmd_clone's own signature defaults to dest=".", so a relative
# destination survived into clone.py and was resolved against whatever cwd the
# shell happened to have. `gitlab-cli clone globex ./here` wrote the error log
# into the user's cwd; `gitlab-cli clone globex` from ~ built ~/MasterGroups.
#
# The existing suite pins paths.anchor(). This file pins the BOUNDARY: every
# destination that enters the program now goes through _resolve_dest, comes out
# absolute, and is announced when it lands outside the managed folder.
#
# Every test chdirs into a temp directory first — a cwd that is not the install
# dir is the only place this defect is visible.

import io
import os
import unittest
from contextlib import redirect_stdout

from _bootstrap import TempTreeTestCase

import gitlab_cli
import paths


class DestResolutionTests(TempTreeTestCase):

    def setUp(self):
        super().setUp()
        self.managed = paths.anchor("MasterGroups")
        self.cfg = {"url": "http://git.example.net", "headers": {},
                    "token": "unused-offline", "min_access": 0,
                    "master_dir": "MasterGroups"}
        os.chdir(self.tmp)          # NOT the install dir

    def resolve(self, raw=None, what="destination"):
        buf = io.StringIO()
        with redirect_stdout(buf):
            out = gitlab_cli._resolve_dest(self.cfg, raw, what)
        return out, buf.getvalue()

    # ── the default: no argument at all ────────────────────────────────────

    def test_no_argument_resolves_to_the_managed_folder_not_the_cwd(self):
        """The original bug was the DEFAULT, not the argument: the user never
        typed a path and the tool still resolved one against their cwd."""
        dest, _ = self.resolve(None)
        self.assertEqual(dest, self.managed)
        self.assertFalse(dest.startswith(os.path.realpath(self.tmp)))

    def test_an_empty_or_whitespace_argument_behaves_like_no_argument(self):
        for raw in ("", "   ", "\t"):
            self.assertEqual(self.resolve(raw)[0], self.managed, repr(raw))

    def test_the_resolved_default_is_absolute(self):
        self.assertTrue(os.path.isabs(self.resolve(None)[0]))

    def test_the_default_is_identical_from_two_different_cwds(self):
        first = self.resolve(None)[0]
        sub = os.path.join(self.tmp, "deeper")
        os.makedirs(sub, exist_ok=True)
        os.chdir(sub)
        self.assertEqual(self.resolve(None)[0], first)

    # ── explicit destinations ──────────────────────────────────────────────

    def test_an_explicit_relative_path_never_leaves_this_function_relative(self):
        dest, _ = self.resolve("./here")
        self.assertTrue(os.path.isabs(dest))
        self.assertNotIn("./", dest.replace(os.sep, "/").lstrip("/"))

    def test_an_explicit_relative_path_resolves_against_the_users_cwd(self):
        """Every other Unix tool (cp, git clone, tar -C) does this; anchoring
        it to the install dir would silently write somewhere the user did not
        name AND that the menu does not read."""
        dest, _ = self.resolve("./here")
        self.assertEqual(os.path.realpath(dest),
                         os.path.realpath(os.path.join(self.tmp, "here")))

    def test_a_nested_relative_path_resolves_against_the_cwd(self):
        dest, _ = self.resolve("sub/dir")
        self.assertEqual(os.path.realpath(dest),
                         os.path.realpath(os.path.join(self.tmp, "sub", "dir")))

    def test_an_absolute_path_is_normalised_and_kept(self):
        target = os.path.join(self.tmp, "mirror")
        dest, _ = self.resolve(target)
        self.assertEqual(os.path.realpath(dest), os.path.realpath(target))

    def test_resolution_is_idempotent(self):
        once, _ = self.resolve("./here")
        twice, _ = self.resolve(once)
        self.assertEqual(once, twice)

    # ── the property that actually matters: nothing is silent ──────────────

    def test_an_off_tree_destination_is_announced(self):
        _, out = self.resolve("./here", "clone destination")
        self.assertIn("outside the managed folder", out)
        self.assertIn(os.path.realpath(self.tmp).split(os.sep)[-1], out)

    def test_an_absolute_off_tree_destination_is_announced_too(self):
        """The hazard is 'invisible to the menu', not 'relative' — so an
        absolute path one keystroke away must warn identically."""
        _, out = self.resolve(os.path.join(self.tmp, "mirror"))
        self.assertIn("outside the managed folder", out)

    def test_the_managed_folder_itself_is_not_warned_about(self):
        _, out = self.resolve(self.managed)
        self.assertNotIn("outside the managed folder", out)

    def test_a_subdirectory_of_the_managed_folder_is_not_warned_about(self):
        _, out = self.resolve(os.path.join(self.managed, "initech"))
        self.assertNotIn("outside the managed folder", out)

    def test_the_warning_names_the_command_it_belongs_to(self):
        for label in ("clone destination", "sync destination", "log folder",
                      "remotes folder"):
            _, out = self.resolve("./here", label)
            self.assertIn(label, out)

    def test_the_offsite_warning_is_not_a_no_op(self):
        """Negative control — a warning that never fires protects nothing."""
        quiet = self.resolve(self.managed)[1]
        loud = self.resolve("./here")[1]
        self.assertNotEqual(quiet, loud)
        self.assertTrue(loud.strip())


class DestContainmentTests(TempTreeTestCase):
    """_is_inside is what decides whether the user is warned."""

    def test_a_path_is_inside_itself(self):
        self.assertTrue(gitlab_cli._is_inside(self.dest, self.dest))

    def test_a_child_is_inside(self):
        child = os.path.join(self.dest, "initech", "feed-ms")
        os.makedirs(child, exist_ok=True)
        self.assertTrue(gitlab_cli._is_inside(self.dest, child))

    def test_a_sibling_with_a_shared_prefix_is_not_inside(self):
        """String prefix matching would call MasterGroups-old a child of
        MasterGroups."""
        sibling = self.dest + "-old"
        os.makedirs(sibling, exist_ok=True)
        self.assertFalse(gitlab_cli._is_inside(self.dest, sibling))

    def test_a_parent_is_not_inside_its_child(self):
        self.assertFalse(gitlab_cli._is_inside(
            os.path.join(self.dest, "initech"), self.dest))


if __name__ == "__main__":
    unittest.main(verbosity=2)
