#!/usr/bin/env python3
# tests/test_pull_rename_wiring.py — T2 (follow upstream renames on Sync).
#
# cmd_pull only looks at a group's CURRENT path. A project or group renamed on
# the server left its clone under the OLD path, so Sync All / Sync / `pull`
# skipped it or answered "Folder does not exist. Clone it first." The fix is a
# pre-step: clone.reconcile_renames() moves proven-renamed clones first, and
# the directory handed to cmd_pull is derived AFTER it, from a fresh scan when
# something moved.
#
# Clone (Clone All, single Clone, `clone`) must NOT get the pre-step: cmd_clone
# repairs renames itself inside _process_group, and a second pass would double
# the API cost.
#
# reconcile_renames is owned by clone.py and is mocked here (create=True), so
# these tests pin the WIRING whether or not that function has landed yet. No
# network: every server-facing call is stubbed; fixtures are real git repos in
# a mkdtemp tree.

import io
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from _bootstrap import HOST, TempTreeTestCase, fake_cfg, git

import clone
import gitlab_cli as G
from pull import _resolve_local_path

MOVED = {"reason": "renamed_upstream", "name": "proj"}


def cfg_for(dest):
    c = fake_cfg(url=f"http://{HOST}", master_dir=dest)
    c["folder"] = None
    return c


class _MenuHarness(TempTreeTestCase):
    """Drive interactive_mode through exactly one menu round, then exit."""

    def setUp(self):
        super().setUp()
        self.cfg = cfg_for(self.dest)
        self.calls = []          # ordered record of reconcile / cmd_pull / cmd_clone

    def rec_reconcile(self, rows=None, exc=None):
        def _fn(cfg, group_path, dest, quiet=False):
            self.calls.append(("reconcile", group_path, dest))
            if exc is not None:
                raise exc
            return list(rows or [])
        return _fn

    def rec_pull(self, cfg, group_path, dest=".", *a, **k):
        self.calls.append(("pull", group_path, dest))

    def rec_clone(self, cfg, group_path, dest="."):
        self.calls.append(("clone", group_path, dest))

    def run_menu(self, picked, action=None, reconcile=None, extra=()):
        reconcile = reconcile or self.rec_reconcile()
        patches = [
            mock.patch.object(G, "pick_instance", return_value=None),
            mock.patch.object(G, "_activate_instance"),
            mock.patch.object(G, "_resolve_dest", return_value=self.dest),
            mock.patch.object(G, "_cap_info", return_value={}),
            mock.patch.object(G, "_show_instance_banner"),
            mock.patch.object(G, "pick_main_menu", return_value="1"),
            mock.patch.object(G, "pick_group",
                              side_effect=[picked, SystemExit(0)]),
            mock.patch.object(G, "pick_action", return_value=action or "1"),
            mock.patch.object(G, "_ask_next"),
            mock.patch.object(G, "_user_namespace_gate", return_value=[]),
            mock.patch.object(G, "cmd_pull", side_effect=self.rec_pull),
            mock.patch.object(G, "cmd_clone", side_effect=self.rec_clone),
            mock.patch.object(clone, "reconcile_renames", side_effect=reconcile,
                              create=True),
            *extra,
        ]
        buf = io.StringIO()
        mocks = [p.start() for p in patches]
        try:
            with redirect_stdout(buf), self.assertRaises(SystemExit):
                G.interactive_mode(self.cfg)
        finally:
            for p in reversed(patches):
                p.stop()
        return mocks, buf.getvalue()

    def kinds(self):
        return [c[0] for c in self.calls]


class PullAllWiringTests(_MenuHarness):

    def pull_all(self, *paths, index=None):
        return {"_bulk": "pull_all",
                "_groups": [{"id": i, "full_path": p}
                            for i, p in enumerate(paths, 1)],
                "_index": index if index is not None else {}}

    def test_reconcile_runs_before_cmd_pull_with_the_full_group_path(self):
        self.run_menu(self.pull_all("acme/data", "acme/web"))
        self.assertEqual(self.kinds(), ["reconcile", "pull", "reconcile", "pull"])
        self.assertEqual([c[1] for c in self.calls if c[0] == "reconcile"],
                         ["acme/data", "acme/web"])
        # dest is exactly what cmd_clone would have received for that group
        for c in self.calls:
            if c[0] == "reconcile":
                self.assertEqual(c[2], G._clone_dest(self.cfg, c[1], self.dest, {}))

    def test_no_rescan_when_nothing_moved(self):
        scan = mock.patch.object(G, "scan_dest", return_value={})
        mocks, _ = self.run_menu(
            self.pull_all("acme/data"),
            reconcile=self.rec_reconcile(rows=[{"reason": "skip"}]),
            extra=(scan,))
        mocks[-1].assert_not_called()
        self.assertEqual(self.kinds(), ["reconcile", "pull"])

    def test_rescan_once_when_moved_and_later_groups_use_the_fresh_index(self):
        fresh = {("x", "y"): "/nowhere"}
        seen = []
        real_group_dir = G._group_dir

        def spy(cfg, gpath, dest, index=None):
            seen.append((gpath, index))
            return real_group_dir(cfg, gpath, dest, index)

        moved_first = iter([[MOVED], []])

        def reconcile(cfg, group_path, dest, quiet=False):
            self.calls.append(("reconcile", group_path, dest))
            return next(moved_first)

        scan = mock.patch.object(G, "scan_dest", return_value=fresh)
        gd = mock.patch.object(G, "_group_dir", side_effect=spy)
        mocks, _ = self.run_menu(self.pull_all("acme/data", "acme/web"),
                                 reconcile=reconcile, extra=(scan, gd))
        mocks[-2].assert_called_once_with(self.dest)
        # _group_dir is also used for _clone_dest inside the reconcile call;
        # the ones that matter are the action-side lookups after each reconcile.
        after = [ix for gp, ix in seen if ix is fresh]
        self.assertTrue(after, "the refreshed index was never used")
        self.assertIs([ix for gp, ix in seen if gp == "acme/web"][-1], fresh)

    def test_cmd_pull_lands_on_the_moved_location(self):
        old = self.make_repo("oldname/proj", f"http://{HOST}/oldname/proj.git")
        G_index = G.scan_dest(self.dest)          # the menu's stale view
        new = os.path.join(self.dest, "newname", "proj")

        def reconcile(cfg, group_path, dest, quiet=False):
            self.calls.append(("reconcile", group_path, dest))
            os.makedirs(os.path.dirname(new), exist_ok=True)
            os.rename(old, new)
            r = git("-C", new, "remote", "set-url", "origin",
                    f"http://{HOST}/newname/proj.git")
            self.assertEqual(r.returncode, 0, r.stderr)
            return [MOVED]

        self.run_menu(self.pull_all("newname", index=G_index),
                      reconcile=reconcile)
        self.assertEqual(self.kinds(), ["reconcile", "pull"])
        _, gp, dst = self.calls[1]
        self.assertEqual(os.path.normpath(_resolve_local_path(gp, dst, None)),
                         os.path.normpath(os.path.join(self.dest, "newname")))
        self.assertTrue(os.path.isdir(os.path.join(self.dest, "newname")))

    def test_reconcile_raising_does_not_stop_the_bulk_loop(self):
        _, out = self.run_menu(
            self.pull_all("acme/data", "acme/web"),
            reconcile=self.rec_reconcile(exc=RuntimeError("boom")))
        self.assertEqual(self.kinds(), ["reconcile", "pull", "reconcile", "pull"])
        self.assertIn("Rename check skipped for acme/data", out)

    def test_missing_reconcile_is_skipped_silently(self):
        with mock.patch.object(clone, "reconcile_renames", None, create=True):
            G_calls = []
            with mock.patch.object(G, "cmd_pull",
                                   side_effect=lambda *a, **k: G_calls.append(a)), \
                    redirect_stdout(io.StringIO()):
                G._sync_group(self.cfg, "acme/data", self.dest, {})
        self.assertEqual(len(G_calls), 1)


class SingleGroupWiringTests(_MenuHarness):

    def group(self, status):
        return {"id": 7, "full_path": "acme/data", "_status": status,
                "_index": {}, "_cov": {"cloned": 1, "total": 2},
                "_orphans": []}

    def test_single_group_sync_reconciles_first(self):
        self.run_menu(self.group("◐"), action="1")
        self.assertEqual(self.kinds(), ["reconcile", "pull"])
        self.assertEqual(self.calls[0][1], "acme/data")

    def test_single_group_clone_does_not_reconcile(self):
        self.run_menu(self.group("◐"), action="5")
        self.assertEqual(self.kinds(), ["clone"])

    def test_clone_all_does_not_reconcile(self):
        self.run_menu({"_bulk": "clone_all", "_index": {},
                       "_groups": [{"id": 1, "full_path": "acme/data"},
                                   {"id": 2, "full_path": "acme/web"}]})
        self.assertEqual(self.kinds(), ["clone", "clone"])


class CliPullWiringTests(TempTreeTestCase):

    def _run(self, argv, reconcile):
        cfg = cfg_for(self.dest)
        calls = []
        with mock.patch.object(G, "print_banner"), \
                mock.patch.object(G.setup_mod, "ensure_setup", return_value=cfg), \
                mock.patch.object(G, "_activate_instance"), \
                mock.patch.object(G, "_resolve_dest", return_value=self.dest), \
                mock.patch.object(clone, "reconcile_renames", create=True,
                                  side_effect=lambda *a, **k: (
                                      calls.append(("reconcile", a[1])),
                                      reconcile(*a, **k))[1]), \
                mock.patch.object(G, "cmd_pull",
                                  side_effect=lambda c, gp, d, *a, **k:
                                  calls.append(("pull", gp))), \
                mock.patch.object(G, "cmd_clone",
                                  side_effect=lambda c, gp, d:
                                  calls.append(("clone", gp))), \
                redirect_stdout(io.StringIO()):
            G._run(argv)
        return calls

    def test_cli_pull_reconciles_the_given_group_first(self):
        calls = self._run(["pull", "acme/data"], lambda *a, **k: [])
        self.assertEqual(calls[0], ("reconcile", "acme/data"))
        self.assertEqual([c[0] for c in calls], ["reconcile", "pull"])

    def test_cli_sync_alias_reconciles_and_survives_an_exception(self):
        def boom(*a, **k):
            raise RuntimeError("api down")
        calls = self._run(["sync", "acme/data"], boom)
        self.assertEqual([c[0] for c in calls], ["reconcile", "pull"])

    def test_cli_clone_does_not_reconcile(self):
        calls = self._run(["clone", "acme/data"], lambda *a, **k: [])
        self.assertEqual([c[0] for c in calls], ["clone"])


if __name__ == "__main__":
    unittest.main()
