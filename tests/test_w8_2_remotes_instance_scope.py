# tests/test_w8_2_remotes_instance_scope.py
#
# W8-2. The instance-wide Remotes screen walked the whole shared clone root
# instead of the active instance's subtree. Remotes is the screen that offers
# to REWRITE remote URLs, so with globex (folder="") active it audited and
# offered to scrub Hub's clones under MasterGroups/acme.
#
# The two instances are asymmetric by design:
#     Hub      folder = "acme"  -> root/acme
#     globex folder = ""             -> root itself
# A fix that assumes symmetry ("walk root/folder") silently empties the walk
# for the instance without a folder. Every test below therefore exercises BOTH
# instances plus the empty-folder edge case explicitly.
#
# All fixtures are real `git init` repositories in a temp dir, and the
# instances.json used is a temp file — the user's live config is never read
# or written.

import json
import os
import unittest

import _bootstrap  # noqa: F401  (sys.path + error-log guard)

import paths
import remotes


def _write_instances(tmp: str, active: str, instances: list) -> str:
    """A temp instances.json, pointed at by paths.INSTANCES_FILE."""
    p = os.path.join(tmp, "instances.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"active": active, "instances": instances}, fh)
    return p


class _ScopeCase(_bootstrap.TempTreeTestCase):
    """Two instances sharing one root, exactly like the live machine."""

    def setUp(self):
        super().setUp()
        self.root = self.dest          # <tmp>/MasterGroups, the shared root

        # Hub's subtree (folder="acme") and globex's (folder="").
        self.hub_repos = [
            self.make_repo("acme/atlas/atlas"),
            self.make_repo("acme/vertex/vertex-backend"),
        ]
        self.rob_repos = [
            self.make_repo("initech/accounts-ms"),
            self.make_repo("globex/meta-repo"),
            self.make_repo("anna_user/investment-data"),
        ]

        self.instances = [
            {"name": "Hub", "url": "https://hub.example.com",
             "master_dir": self.root, "folder": "acme"},
            {"name": "globex", "url": "http://git.example.net/",
             "master_dir": self.root, "folder": ""},
        ]

        self._real_instances_file = paths.INSTANCES_FILE
        self.addCleanup(self._restore)

    def _restore(self):
        paths.INSTANCES_FILE = self._real_instances_file
        remotes._scope_cache.update(key=None, scope=None)
        paths._folder_cache.update(key=None, folders=frozenset())

    def activate(self, name: str, instances: list | None = None):
        """Point remotes/paths at a temp config with `name` active."""
        paths.INSTANCES_FILE = _write_instances(
            self.tmp, name, instances if instances is not None
            else self.instances)
        # The caches key on (mtime_ns, size); two writes within one tick can
        # collide, so they are cleared explicitly.
        remotes._scope_cache.update(key=None, scope=None)
        paths._folder_cache.update(key=None, folders=frozenset())
        return remotes.active_scope()

    def walk(self):
        """Every work tree under the shared root — the pre-fix input."""
        from pull import _find_git_repos
        return _find_git_repos(self.root)


# ─── The defect itself ────────────────────────────────────────────────────────

class TestActiveInstanceScoping(_ScopeCase):

    def test_root_walk_really_does_see_both_instances(self):
        """Guards the premise: without scoping the walk is cross-instance."""
        found = self.walk()
        self.assertEqual(len(found), 5)
        self.assertTrue(any("acme" in p.split(os.sep) for p in found))

    def test_globex_active_excludes_every_hub_repo(self):
        """THE BUG. Empty folder => subtree is the root, minus Hub's folder."""
        scope = self.activate("globex")
        keep, foreign = remotes.split_by_instance(self.walk(), scope)

        self.assertEqual(len(keep), len(self.rob_repos))
        for p in keep:
            self.assertNotIn("acme", p.split(os.sep),
                             f"Hub clone leaked into globex's scope: {p}")
        self.assertEqual(foreign, {"Hub": len(self.hub_repos)})

    def test_hub_active_excludes_every_globex_only_repo(self):
        scope = self.activate("Hub")
        # Hub's dest is already root/acme; nothing outside it is walked,
        # and nothing inside it belongs to globex.
        from pull import _find_git_repos
        found = _find_git_repos(os.path.join(self.root, "acme"))
        keep, foreign = remotes.split_by_instance(found, scope)

        self.assertEqual(len(keep), len(self.hub_repos))
        for p in keep:
            self.assertIn("acme", p.split(os.sep))
        self.assertEqual(foreign, {},
                         "an instance WITH a folder must exclude nothing")

    def test_empty_folder_instance_is_never_turned_into_an_exclusion(self):
        """The asymmetry trap.

        globex's folder is "" so its subtree IS the root. If that were
        treated as an exclusion, Hub's walk (a strict subset of the root)
        would be emptied.
        """
        scope = self.activate("Hub")
        self.assertEqual(
            [o["name"] for o in scope["others"]], [],
            "the folder-less instance must not become an excluded subtree")

        keep, _ = remotes.split_by_instance(self.hub_repos, scope)
        self.assertEqual(len(keep), len(self.hub_repos),
                         "symmetry assumption emptied the folder-ed instance")

    def test_scope_root_of_each_instance(self):
        self.assertEqual(self.activate("globex")["root"],
                         os.path.normpath(self.root))
        self.assertEqual(self.activate("Hub")["root"],
                         os.path.normpath(os.path.join(self.root,
                                                       "acme")))


# ─── audit() honours the scope ────────────────────────────────────────────────

class TestAuditScoped(_ScopeCase):

    def test_audit_with_globex_active_returns_only_globex_repos(self):
        self.activate("globex")
        rows = remotes.audit(self.walk())

        self.assertEqual(len(rows), len(self.rob_repos))
        for r in rows:
            self.assertNotIn("acme", r["path"].split(os.sep))
        self.assertEqual(rows.scope["name"], "globex")
        self.assertEqual(rows.foreign, {"Hub": len(self.hub_repos)})
        self.assertEqual(rows.dropped, len(self.hub_repos))

    def test_audit_with_hub_active_returns_only_hub_repos(self):
        self.activate("Hub")
        rows = remotes.audit(self.walk())

        self.assertEqual(len(rows), len(self.hub_repos))
        for r in rows:
            self.assertIn("acme", r["path"].split(os.sep))
        self.assertEqual(rows.scope["name"], "Hub")

    def test_audit_result_is_still_a_plain_list_for_old_callers(self):
        self.activate("globex")
        rows = remotes.audit(self.walk())
        self.assertIsInstance(rows, list)
        self.assertIsInstance(rows[0], dict)
        self.assertIn("path", rows[0])
        self.assertIn("has_creds", rows[0])

    def test_enforce_scope_false_restores_the_whole_walk(self):
        """The escape hatch stays explicit, never the default."""
        self.activate("globex")
        rows = remotes.audit(self.walk(), enforce_scope=False)
        self.assertEqual(len(rows), 5)

    def test_no_config_means_no_filtering(self):
        """An unconfigured machine must behave exactly as before."""
        paths.INSTANCES_FILE = os.path.join(self.tmp, "does-not-exist.json")
        remotes._scope_cache.update(key=None, scope=None)
        self.assertIsNone(remotes.active_scope())
        rows = remotes.audit(self.walk())
        self.assertEqual(len(rows), 5)

    def test_active_name_not_in_instances_means_no_filtering(self):
        self.activate("ghost-instance")
        self.assertIsNone(remotes.active_scope())
        self.assertEqual(len(remotes.audit(self.walk())), 5)


# ─── scrub() filters independently of audit() ─────────────────────────────────

class TestScrubScoped(_ScopeCase):
    """The write path must not trust the read path to have filtered."""

    def test_scrub_dry_run_never_scans_the_other_instance(self):
        self.activate("globex")
        stats = remotes.scrub(self.walk(), save_to_store=False, dry_run=True)

        self.assertEqual(stats["scanned"], len(self.rob_repos))
        self.assertEqual(stats["instance"], "globex")
        self.assertEqual(stats["skipped_other_instance"], len(self.hub_repos))
        self.assertEqual(stats["skipped_by_instance"], {"Hub": len(self.hub_repos)})

    def test_scrub_filters_even_when_handed_the_raw_unfiltered_list(self):
        """Callers pass the same raw list to audit() and scrub()."""
        self.activate("globex")
        raw = self.walk()
        self.assertEqual(len(raw), 5)
        stats = remotes.scrub(raw, save_to_store=False, dry_run=True)
        self.assertEqual(stats["scanned"], 3)

    def test_scrub_with_hub_active_scans_hub_repos(self):
        """Hand Hub the ROOT walk: its own repos are scanned, the rest are not.

        The 3 globex-only repos sit under the shared root but outside
        root/acme, so they are excluded by containment and reported as
        unassigned-to-Hub rather than scrubbed.
        """
        self.activate("Hub")
        stats = remotes.scrub(self.walk(), save_to_store=False, dry_run=True)
        self.assertEqual(stats["scanned"], len(self.hub_repos))
        self.assertEqual(stats["instance"], "Hub")
        self.assertEqual(stats["skipped_other_instance"], len(self.rob_repos))
        self.assertEqual(stats["skipped_by_instance"],
                         {remotes.UNASSIGNED: len(self.rob_repos)})

    def test_scrub_on_hubs_own_subtree_excludes_nothing(self):
        """The normal path: Hub's dest is already root/acme."""
        self.activate("Hub")
        from pull import _find_git_repos
        found = _find_git_repos(os.path.join(self.root, "acme"))
        stats = remotes.scrub(found, save_to_store=False, dry_run=True)
        self.assertEqual(stats["scanned"], len(self.hub_repos))
        self.assertEqual(stats["skipped_other_instance"], 0)

    def test_scrub_reports_zero_scanned_rather_than_touching_foreign_repos(self):
        """Hand scrub ONLY the other instance's repos: nothing is scanned."""
        self.activate("globex")
        stats = remotes.scrub(self.hub_repos, save_to_store=False, dry_run=True)
        self.assertEqual(stats["scanned"], 0)
        self.assertEqual(stats["cleaned"], 0)
        self.assertEqual(stats["skipped_other_instance"], len(self.hub_repos))


# ─── Repos belonging to no instance, and out-of-tree destinations ────────────

class TestUnassignedAndOffsite(_ScopeCase):

    def test_repo_under_shared_root_but_no_instance_is_reported_not_scrubbed(self):
        """A hand-made tree in the root is counted, never silently scrubbed."""
        stray = self.make_repo("acme/atlas/atlas")  # already Hub's
        self.activate("Hub")
        stats = remotes.scrub(self.rob_repos + [stray],
                              save_to_store=False, dry_run=True)
        self.assertEqual(stats["scanned"], 1)
        self.assertEqual(stats["skipped_by_instance"],
                         {remotes.UNASSIGNED: len(self.rob_repos)})

    def test_unassigned_bucket_is_named_on_screen(self):
        scope = self.activate("Hub")
        line = remotes.scope_line(scope, 2, {remotes.UNASSIGNED: 3})
        self.assertIn("3", line)
        self.assertIn("no configured instance", line)
        self.assertIn("NOT", line)

    def test_destination_outside_the_managed_root_is_never_foreign(self):
        """`gitlab-cli remotes /elsewhere` must keep auditing /elsewhere."""
        outside = self.make_repo("../outside-tree/some-repo")
        scope = self.activate("Hub")
        self.assertEqual(remotes.owner_of(outside, scope), "")
        keep, foreign = remotes.split_by_instance([outside], scope)
        self.assertEqual(keep, [outside])
        self.assertEqual(foreign, {})

    def test_folderless_instance_treats_unmatched_root_repos_as_its_own(self):
        """globex's subtree IS the root, so nothing there is 'unassigned'."""
        scope = self.activate("globex")
        self.assertEqual(scope["base"], scope["root"])
        for p in self.rob_repos:
            self.assertEqual(remotes.owner_of(p, scope), "")


# ─── The screen must state instance + count ───────────────────────────────────

class TestScopeIsStatedOnScreen(_ScopeCase):

    def test_scope_line_names_instance_and_count(self):
        scope = self.activate("globex")
        line = remotes.scope_line(scope, 156, {"Hub": 628})
        self.assertIn("globex", line)
        self.assertIn("156", line)
        self.assertIn("628", line)
        self.assertIn("Hub", line)
        self.assertIn("NOT audited", line)

    def test_scope_line_without_foreign_still_names_instance_and_count(self):
        scope = self.activate("Hub")
        line = remotes.scope_line(scope, 628, {})
        self.assertIn("Hub", line)
        self.assertIn("628", line)

    def test_print_audit_states_the_instance_and_the_covered_count(self):
        self.activate("globex")
        rows = remotes.audit(self.walk())
        out = _capture(remotes.print_audit, rows)

        self.assertIn("globex", out)
        self.assertIn(str(len(self.rob_repos)), out)
        self.assertIn("Hub", out, "the excluded instance must be reported")
        self.assertNotIn("acme/atlas", out)

    def test_print_audit_for_hub_states_hub(self):
        self.activate("Hub")
        rows = remotes.audit(self.walk())
        out = _capture(remotes.print_audit, rows)
        self.assertIn("Hub", out)
        self.assertIn(str(len(self.hub_repos)), out)


class TestScrubConfirmationWording(_ScopeCase):

    def test_confirmation_states_instance_and_count_before_asking(self):
        scope = self.activate("globex")
        rows = remotes.AuditRows()
        rows.scope = scope
        rows.foreign = {"Hub": 628}
        rows.extend([{"path": p, "leaks": ["origin"]} for p in self.rob_repos])

        msg = remotes.confirm_scrub_message(rows, scope)
        self.assertIn("globex", msg)
        self.assertIn(str(len(self.rob_repos)), msg)
        self.assertIn("628", msg)
        self.assertIn("Hub", msg)
        self.assertIn("not be touched", msg)

    def test_confirmation_counts_only_leaky_repos(self):
        scope = self.activate("Hub")
        rows = remotes.AuditRows()
        rows.scope = scope
        rows.extend([{"path": "a", "leaks": ["origin"]},
                     {"path": "b", "leaks": []},
                     {"path": "c", "leaks": []}])
        msg = remotes.confirm_scrub_message(rows, scope)
        self.assertIn("1 of 3", msg)
        self.assertIn("Hub", msg)

    def test_confirmation_is_reachable_from_print_audit_output(self):
        """A leaky audit prints the wording, so no caller can skip it."""
        scope = self.activate("globex")
        rows = remotes.AuditRows()
        rows.scope = scope
        rows.foreign = {"Hub": 628}
        rows.extend([{"path": p, "name": os.path.basename(p), "remotes": [],
                      "count": 1, "leaks": ["origin"], "has_creds": True,
                      "hosts": ["git.example.net"], "unreachable": []}
                     for p in self.rob_repos])
        out = _capture(remotes.print_audit, rows)
        self.assertIn("About to rewrite remote URLs", out)
        self.assertIn("globex", out)


# ─── helpers ─────────────────────────────────────────────────────────────────

def _capture(fn, *a, **kw) -> str:
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a, **kw)
    return buf.getvalue()


if __name__ == "__main__":
    unittest.main()
