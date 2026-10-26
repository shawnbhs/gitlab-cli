#!/usr/bin/env python3
# tests/test_w4_error_log_parity.py
#
# Wave-4 regression test for D11 (reports/W3-8-interactive.md), RECONCILED in
# W6 (reports/W6-5-test-reconcile.md).
#
# ─── The defect this file pins ────────────────────────────────────────────────
#
# The per-group error log had TWO spellings of its filename:
#
#   clone._error_log_path -> _group_slug(group_path)   FULL path,
#                 'globex/x/core' -> '.globex__x__core_errors.json'
#   gitlab_cli.py (menu)  -> gpath.split("/")[-1]      LAST component,
#                 'globex/x/core' -> '.core_errors.json'
#
# clone WROTE one name and the menu READ the other, so the `⚠ has errors`
# marker and the `4 Retry` action looked at a file clone never writes: a
# nested group's failures became invisible and Retry reported "no errors to
# retry" on a group that had them. This is the project's recurring bug class —
# status computed one way, the action taken another.
#
# ─── What changed in W6 ───────────────────────────────────────────────────────
#
# Two tests below carried `@unittest.expectedFailure` "until the fix lands".
# The fix HAS landed: gitlab_cli._err_log_path now delegates to
# clone._error_log_path. The decorators were nevertheless not stale-by-accident
# — they could NEVER have flipped, because the tests asserted
#
#     clone._error_log_path(dest, nested) == <a local copy of the OLD formula>
#
# i.e. they asserted that the full-path slug equals the last-component name.
# That is precisely the collision `_group_slug` exists to prevent, and
# SlugIsCollisionFreeTests in this same file asserts the opposite. The pair
# encoded the history of the bug, not a requirement, and pinned a target that
# a correct implementation must never hit.
#
# They now assert the real requirement against the real menu function:
# gitlab_cli._err_log_path and clone._error_log_path must agree, and must NOT
# be the last-component spelling. No decorator.
#
# Offline: pure path arithmetic plus a real temp tree. No network, no token.

import json
import os
import unittest

from _bootstrap import TempTreeTestCase, BASE, fake_cfg

import clone as clone_mod
import gitlab_cli as G


def naive_last_component_name(dest, group_full_path):
    """
    The OLD, collision-prone filename: last path component only.

    Kept as a NEGATIVE CONTROL, not as a target. It is what the menu used to
    compute and what clone must never produce; every assertion involving it
    below is an assertNotEqual or a demonstration that it collides.
    """
    return os.path.join(dest, f".{group_full_path.split('/')[-1]}_errors.json")


def _menu_cfg(dest):
    """A cfg the menu resolver accepts, with no instance `folder` layout."""
    cfg = fake_cfg(url=BASE, master_dir=dest)
    cfg["folder"] = None
    return cfg


class TopLevelGroupsAgreeTests(TempTreeTestCase):
    """The case that works today — pinned so a fix does not break it."""

    def test_top_level_group_has_one_filename(self):
        for group in ("industry", "initech", "globex"):
            with self.subTest(group=group):
                self.assertEqual(
                    clone_mod._error_log_path(self.dest, group),
                    naive_last_component_name(self.dest, group),
                    "a top-level group's slug IS its last component; existing "
                    "top-level logs must keep working unchanged")

    def test_existing_top_level_log_is_still_found(self):
        path = clone_mod._error_log_path(self.dest, "industry")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"group": "industry",
                       "errors": [{"project": "p", "path": "x"}]}, fh)
        self.assertEqual(
            G._err_log_path(_menu_cfg(self.dest), "industry", self.dest, {}),
            path)


class NestedGroupParityTests(TempTreeTestCase):
    """
    D11 proper: the MENU must compute the same filename CLONE writes.

    Asserted against gitlab_cli._err_log_path — the function the menu really
    calls — not against a reproduction of the old formula. A reproduction can
    only ever pin the bug; the real function is the only thing that can pin
    the requirement.
    """

    NESTED = "globex/metarepo/meta-repo"

    def test_writer_and_reader_agree_on_a_nested_group(self):
        self.assertEqual(
            os.path.basename(
                G._err_log_path(_menu_cfg(self.dest), self.NESTED,
                                self.dest, {})),
            os.path.basename(clone_mod._error_log_path(self.dest, self.NESTED)),
            "clone writes '.globex__metarepo__meta-repo_errors.json'; "
            "the menu must look for that same name, or the ⚠ marker and Retry "
            "read a file that is never written")

    def test_the_menu_finds_a_log_that_clone_actually_wrote(self):
        written = clone_mod._error_log_path(self.dest, self.NESTED)
        with open(written, "w", encoding="utf-8") as fh:
            json.dump({"group": self.NESTED,
                       "errors": [{"project": "p", "path": "x"}]}, fh)
        found = G._err_log_path(_menu_cfg(self.dest), self.NESTED,
                                self.dest, {})
        self.assertTrue(
            os.path.exists(found),
            "a group with recorded failures shows no ⚠ and Retry reports "
            "'there are no errors to retry'")
        self.assertEqual(os.path.normpath(found), os.path.normpath(written))

    def test_the_menu_never_uses_the_last_component_spelling(self):
        """The regression guard: reverting the menu to `split('/')[-1]` fails here."""
        self.assertNotEqual(
            os.path.basename(
                G._err_log_path(_menu_cfg(self.dest), self.NESTED,
                                self.dest, {})),
            os.path.basename(
                naive_last_component_name(self.dest, self.NESTED)),
            "the menu is back on the collision-prone last-component name")

    def test_two_nested_groups_get_distinct_menu_log_paths(self):
        cfg = _menu_cfg(self.dest)
        a = G._err_log_path(cfg, "initech/core", self.dest, {})
        b = G._err_log_path(cfg, "globex/x/core", self.dest, {})
        self.assertNotEqual(
            a, b,
            "the menu must not show one group's ⚠ on another group that "
            "merely shares a last path component")


class SlugIsCollisionFreeTests(TempTreeTestCase):
    """
    The property the full-path slug exists for. This is the W2 fix; re-pinned
    here because D11's repair must not be done by reverting to the last
    component on the clone side.
    """

    def test_two_groups_sharing_a_last_component_get_different_files(self):
        a = clone_mod._error_log_path(self.dest, "initech/core")
        b = clone_mod._error_log_path(self.dest, "globex/x/core")
        self.assertNotEqual(
            a, b,
            "'initech/core' and 'globex/x/core' must not share an error log — "
            "a clean run of one used to os.remove() the other's pending errors")

    def test_the_naive_last_component_name_does_collide(self):
        # Negative control: proves the assertion above can fail, i.e. the slug
        # is doing real work.
        self.assertEqual(
            naive_last_component_name(self.dest, "initech/core"),
            naive_last_component_name(self.dest, "globex/x/core"))

    def test_slug_stays_a_single_path_component(self):
        for group in ("globex/x/core", "a/b/c/d", "initech/core"):
            with self.subTest(group=group):
                name = os.path.basename(clone_mod._error_log_path(self.dest, group))
                self.assertNotIn("/", name)
                self.assertEqual(
                    os.path.dirname(clone_mod._error_log_path(self.dest, group)),
                    os.path.normpath(self.dest),
                    "the error log must stay directly under dest")

    def test_traversal_in_a_group_path_cannot_escape_dest(self):
        path = clone_mod._error_log_path(self.dest, "../../etc/evil")
        self.assertEqual(os.path.dirname(path), os.path.normpath(self.dest))
        name = os.path.basename(path)
        self.assertNotIn("/", name)
        self.assertNotIn(os.sep, name)
        self.assertEqual(
            os.path.normpath(path), path,
            "the traversal must be flattened into one filename component, not "
            "normalised away into a parent directory")

    def test_empty_group_path_still_produces_a_file_under_dest(self):
        path = clone_mod._error_log_path(self.dest, "")
        self.assertEqual(os.path.dirname(path), os.path.normpath(self.dest))
        self.assertTrue(os.path.basename(path).endswith("_errors.json"))


class LegacyLogMigrationTests(TempTreeTestCase):
    """
    ─── SPEC (decided in W6; see reports/W6-5-test-reconcile.md) ──────────────

    A legacy (last-component) error log is claimed by a group ONLY when it is
    provably that group's:

      S1  ATTRIBUTABLE — the file's recorded `group` equals the group being
          processed, or the legacy and slug names are identical anyway (a
          top-level group, where the last component IS the full path).
          => READ as ours, and DELETED/superseded when we write or clear.

      S2  UNATTRIBUTABLE — the file records a DIFFERENT `group`, or records no
          `group` at all.
          => NEITHER READ NOR DELETED. Strictly left alone.

    Why "read it, never delete it" was REJECTED for S2:

      load_errors does not only feed a display. Its result drives the `⚠ N
      errors` marker AND `4 Retry`, and Retry re-clones the recorded projects
      into THIS group's destination. Reading `initech/core`'s `.core_errors.json`
      while processing `globex/x/core` therefore does not merely confuse the
      user — it clones another group's projects into the wrong tree, which is
      the same status-here / action-there seam D11 and RC-2 were about. The
      damage is not symmetric with deletion, but it is not zero, and an
      unattributable log is exactly the case where we cannot tell.

    Deleting an S2 file is the original data-loss bug: `initech/core` and
    `globex/x/core` both wrote `.core_errors.json`, so a clean run of one
    os.remove()d the other's pending errors.

    The current source (clone._owns_legacy) already implements this spec; the
    tests below assert it, including the previously-unpinned "no `group` field
    is never deleted" half.
    """

    def test_legacy_path_is_the_last_component_spelling(self):
        self.assertEqual(
            clone_mod._legacy_error_log_path(self.dest, "globex/x/core"),
            naive_last_component_name(self.dest, "globex/x/core"))

    def _write_legacy(self, group_path, recorded_group):
        legacy = clone_mod._legacy_error_log_path(self.dest, group_path)
        payload = {"errors": [{"project": "p", "path": "x"}]}
        if recorded_group is not None:
            payload["group"] = recorded_group
        with open(legacy, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return legacy

    # ── S1: attributable ──────────────────────────────────────────────────

    def test_load_errors_reads_an_attributable_legacy_log(self):
        self._write_legacy("globex/x/core", "globex/x/core")
        loaded = clone_mod.load_errors(self.dest, "globex/x/core")
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["project"], "p")

    def test_an_attributable_legacy_log_is_superseded_on_a_clean_run(self):
        legacy = self._write_legacy("globex/x/core", "globex/x/core")
        clone_mod._save_errors(self.dest, "globex/x/core", [])
        self.assertFalse(
            os.path.exists(legacy),
            "our own legacy log must be cleaned up once the group runs clean")

    # ── S2: unattributable — never read ───────────────────────────────────

    def test_load_errors_ignores_a_legacy_log_belonging_to_another_group(self):
        # '.core_errors.json' written by initech/core must never be served to
        # globex/x/core — that collision is the defect the slug fixed.
        self._write_legacy("initech/core", "initech/core")
        self.assertEqual(clone_mod.load_errors(self.dest, "globex/x/core"), [])

    def test_load_errors_ignores_a_legacy_log_with_an_empty_group_field(self):
        self._write_legacy("globex/x/core", "")
        self.assertEqual(
            clone_mod.load_errors(self.dest, "globex/x/core"), [],
            "SPEC S2: a log we cannot attribute is not ours to show; Retry "
            "would re-clone its projects into this group's tree")

    def test_load_errors_ignores_a_legacy_log_with_no_group_field_at_all(self):
        self._write_legacy("globex/x/core", None)
        self.assertEqual(
            clone_mod.load_errors(self.dest, "globex/x/core"), [],
            "SPEC S2: a missing `group` key is as unattributable as an empty "
            "one")

    # ── S2: unattributable — never deleted ────────────────────────────────

    def test_an_unattributable_legacy_log_survives_a_clean_run(self):
        legacy = self._write_legacy("globex/x/core", None)
        clone_mod._save_errors(self.dest, "globex/x/core", [])
        self.assertTrue(
            os.path.exists(legacy),
            "SPEC S2: deleting a log we could not attribute is the original "
            "data-loss bug — initech/core's pending errors vanished when "
            "globex/x/core ran clean")

    def test_an_unattributable_legacy_log_survives_our_own_log_being_written(self):
        legacy = self._write_legacy("globex/x/core", "")
        clone_mod._save_errors(self.dest, "globex/x/core",
                               [{"project": "mine", "path": "y"}])
        self.assertTrue(os.path.exists(legacy),
                        "SPEC S2: migration must not consume a stranger's log")
        self.assertEqual(
            [e["project"] for e in
             clone_mod.load_errors(self.dest, "globex/x/core")],
            ["mine"])

    def test_a_foreign_legacy_log_survives_a_clean_run(self):
        legacy = self._write_legacy("initech/core", "initech/core")
        clone_mod._save_errors(self.dest, "globex/x/core", [])
        self.assertTrue(os.path.exists(legacy))
        self.assertEqual(
            [e["project"] for e in clone_mod.load_errors(self.dest, "initech/core")],
            ["p"], "the other group's errors must still be readable BY IT")

    # ── precedence ────────────────────────────────────────────────────────

    def test_the_new_log_wins_over_a_legacy_one(self):
        self._write_legacy("globex/x/core", "globex/x/core")
        with open(clone_mod._error_log_path(self.dest, "globex/x/core"),
                  "w", encoding="utf-8") as fh:
            json.dump({"group": "globex/x/core",
                       "errors": [{"project": "new-one", "path": "y"}]}, fh)
        loaded = clone_mod.load_errors(self.dest, "globex/x/core")
        self.assertEqual([e["project"] for e in loaded], ["new-one"])

    def test_a_top_level_group_is_never_treated_as_a_legacy_claim(self):
        """Legacy == slug for a top-level group, so no attribution is needed."""
        path = clone_mod._error_log_path(self.dest, "industry")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"errors": [{"project": "p", "path": "x"}]}, fh)
        self.assertEqual(
            [e["project"] for e in clone_mod.load_errors(self.dest, "industry")],
            ["p"],
            "a top-level group's own log must not need a `group` field to be "
            "readable — it cannot collide with anything")


if __name__ == "__main__":
    unittest.main(verbosity=2)
