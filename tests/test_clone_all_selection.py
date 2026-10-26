# tests/test_clone_all_selection.py
#
# RC-2's consequence, not its cause. Even with a correct ◐ status, "Clone All"
# is only fixed if the selection actually INCLUDES partial groups, and if the
# per-group menu still offers Clone for a partial group. Excluding ◐ from the
# ca list is the exact behaviour that froze 51 projects: globex read as done,
# was never re-entered, and the only offered action was Sync — which can never
# fetch a project that was never cloned.
#
# Offline: get_top_level_groups and cov.remote_projects are stubbed, the local
# tree is real git repos, no token and no server are touched.

import builtins
import io
import unittest
from contextlib import redirect_stdout

from _bootstrap import TempTreeTestCase, fake_cfg, fake_projects
import coverage as cov
import gitlab_cli


GROUPS = [
    {"id": 1, "path": "initech", "full_path": "initech", "visibility": "private"},
    {"id": 2, "path": "legacy_transfer",
     "full_path": "legacy_transfer", "visibility": "private"},
    {"id": 35, "path": "globex", "full_path": "globex", "visibility": "private"},
    {"id": 114, "path": "agent-knowledge", "full_path": "agent-knowledge",
     "visibility": "private"},
]

REMOTE = {
    1:   fake_projects("initech/a", "initech/b"),                      # will be 2/2 ✅
    2:   fake_projects(*[f"legacy_transfer/p{i}"
                         for i in range(1, 30)]),                # 0/29 ❌
    35:  fake_projects(*[f"globex/s{i}/p{i}" for i in range(1, 23)]),  # 1/22 ◐
    114: [],                                                     # 0/0 ✅ (empty)
}


class CloneAllSelectionTests(TempTreeTestCase):

    def setUp(self):
        super().setUp()
        self._orig_groups = gitlab_cli.get_top_level_groups
        self._orig_remote = cov.remote_projects
        self._orig_input = builtins.input
        gitlab_cli.get_top_level_groups = lambda url, headers: list(GROUPS)
        cov.remote_projects = lambda cfg, group: REMOTE[group["id"]]

    def tearDown(self):
        gitlab_cli.get_top_level_groups = self._orig_groups
        cov.remote_projects = self._orig_remote
        builtins.input = self._orig_input
        super().tearDown()

    def _build_world(self):
        """initech complete, transfer nothing, globex 1 of 22, agent-knowledge empty."""
        self.make_project_clone("initech/a")
        self.make_project_clone("initech/b")
        self.make_project_clone("globex/s1/p1")

    def _pick(self, answer):
        builtins.input = lambda *a, **k: answer
        buf = io.StringIO()
        with redirect_stdout(buf):
            return gitlab_cli.pick_group(fake_cfg(), self.dest), buf.getvalue()

    def test_clone_all_includes_the_partial_group(self):
        self._build_world()
        result, _ = self._pick("ca")
        self.assertEqual(result["_bulk"], "clone_all")
        picked = {g["full_path"] for g in result["_groups"]}
        self.assertIn("globex", picked,
                      "partial group excluded from Clone All — this is the "
                      "bug that left 51 projects unfetched")
        self.assertIn("legacy_transfer", picked)

    def test_clone_all_excludes_complete_and_empty_groups(self):
        self._build_world()
        result, _ = self._pick("ca")
        picked = {g["full_path"] for g in result["_groups"]}
        self.assertNotIn("initech", picked)
        self.assertNotIn("agent-knowledge", picked)
        self.assertEqual(len(result["_groups"]), 2)

    def test_menu_renders_partial_as_partial_with_a_real_count(self):
        self._build_world()
        _, out = self._pick("ca")
        self.assertIn("1/22", out)
        self.assertIn("◐", out)
        # 2 (initech) + 0 (transfer) + 1 (globex) of 2+29+22
        self.assertIn("3/53 projects on disk", out)

    def test_pull_all_includes_partial_groups_too(self):
        # a partial group does have clones; refusing to sync them is wrong
        self._build_world()
        result, _ = self._pick("pa")
        self.assertEqual(result["_bulk"], "pull_all")
        picked = {g["full_path"] for g in result["_groups"]}
        self.assertIn("globex", picked)
        self.assertIn("initech", picked)

    def test_nothing_on_disk_puts_every_non_empty_group_in_clone_all(self):
        result, _ = self._pick("ca")
        picked = {g["full_path"] for g in result["_groups"]}
        self.assertEqual(picked, {"initech", "legacy_transfer", "globex"})

    def test_selecting_the_partial_group_carries_the_partial_status(self):
        self._build_world()
        result, _ = self._pick("globex")
        self.assertEqual(result["_status"], "◐")
        # subset, not equality: other keys (e.g. 'orphans') may legitimately be
        # added to the coverage dict without this behaviour changing.
        cov_info = result["_cov"]
        self.assertEqual(cov_info["icon"], "◐")
        self.assertEqual(cov_info["cloned"], 1)
        self.assertEqual(cov_info["total"], 22)


class PickActionTests(unittest.TestCase):
    """Clone must stay reachable for a partial group."""

    def setUp(self):
        self._orig_input = builtins.input

    def tearDown(self):
        builtins.input = self._orig_input

    def _action(self, status, answer, cov_info):
        builtins.input = lambda *a, **k: answer
        buf = io.StringIO()
        with redirect_stdout(buf):
            return gitlab_cli.pick_action(status, False, {}, cov_info), buf.getvalue()

    def test_partial_group_offers_clone(self):
        choice, out = self._action("◐", "5", {"cloned": 1, "total": 22})
        self.assertIn("Clone", out)
        self.assertIn("21 missing", out)
        self.assertEqual(choice, "5")

    def test_empty_group_offers_clone_as_option_one(self):
        choice, out = self._action("❌", "1", {"cloned": 0, "total": 29})
        self.assertIn("Clone", out)
        self.assertEqual(choice, "1")

    def test_complete_group_still_exposes_clone_but_reports_nothing_missing(self):
        _, out = self._action("✅", "1", {"cloned": 2, "total": 2})
        self.assertIn("nothing missing", out)


if __name__ == "__main__":
    unittest.main()
