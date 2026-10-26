#!/usr/bin/env python3
# tests/test_w4_archived_visibility.py
#
# Wave-4 regression tests for D5 (reports/W3-8-interactive.md, HIGH).
#
# Every project-listing call hardcodes `archived=false`:
#
#     api.py:259  get_projects
#     api.py:275  get_group_projects_recursive   <- coverage.remote_projects
#     api.py:369  get_user_projects
#     api.py:405  get_user_namespace_projects
#
# W1 recorded this as "currently harmless — the instance has 0 archived
# projects". That was true of `globex` and false of the instance that is
# actually active: Hub has archived projects, several of them already cloned.
#
# FIXED IN W5-4. `archived` is now a tri-state the caller chooses
# (api.archived_param), and the DEFAULT is "list everything", so archived
# projects appear in the clone set and in coverage's denominator. The classes
# below were rewritten from "pin the defect" to "pin the fix", as the original
# file instructed.
#
# Consequence, straight from the live transcript: `vertex` prints `❌ 0/5`,
# is offered for cloning, actually has 10 projects of which 3 are on disk.
# Clone All reports "✔ finished" and 5 archived repos are still missing, with
# nothing on screen saying they exist. The 6 already-cloned archived repos are
# in neither the numerator nor the denominator — invisible in both directions,
# which is exactly the blind spot RC-2 was fixed to close.
#
# A mirroring tool that silently refuses to mirror archived projects loses
# precisely the history nobody else is keeping. `archived` must be a choice,
# and the count must be disclosed either way.
#
# Offline: api.get_paginated is stubbed, so no request is ever made.

import unittest

import _bootstrap  # noqa: F401  # puts the CLI's directory on sys.path
                   # so this module also runs standalone, not only via run_tests.py

import api
import coverage as cov


class _PaginatedSpy:
    """Replaces api.get_paginated and records the params it was handed."""

    def __init__(self, payload=None):
        self.payload = payload if payload is not None else []
        self.calls = []
        self._original = None

    def __enter__(self):
        self._original = api.get_paginated

        def fake(url, headers, params=None, **kwargs):
            self.calls.append({"url": url, "params": dict(params or {})})
            return list(self.payload)

        api.get_paginated = fake
        return self

    def __exit__(self, *exc):
        api.get_paginated = self._original
        return False

    @property
    def last_params(self):
        if not self.calls:
            raise AssertionError("get_paginated was never called")
        return self.calls[-1]["params"]


HEADERS = {"PRIVATE-TOKEN": "unused-offline"}
URL = "https://hub.example"


class ArchivedIsVisibleByDefaultTests(unittest.TestCase):
    """
    The default must be "everything". A listing that omits `archived` gets
    archived AND active projects from GitLab; a hardcoded archived="false" is
    what made five missing repositories invisible while the group read ✅.
    """

    def test_group_listing_does_not_filter_archived_out_by_default(self):
        with _PaginatedSpy() as spy:
            api.get_group_projects_recursive(URL, HEADERS, 42)
        self.assertNotIn(
            "archived", spy.last_params,
            "the recursive listing feeds coverage's denominator; filtering "
            "archived projects out of it is the defect itself")

    def test_direct_group_listing_does_not_filter_archived_out_by_default(self):
        with _PaginatedSpy() as spy:
            api.get_projects(URL, HEADERS, 42)
        self.assertNotIn("archived", spy.last_params)

    def test_user_namespace_listings_do_not_filter_archived_out_by_default(self):
        for call in (lambda: api.get_user_projects(URL, HEADERS, "someone"),
                     lambda: api.get_user_namespace_projects(
                         URL, HEADERS, scope="all")):
            with self.subTest(call=call):
                with _PaginatedSpy() as spy:
                    call()
                self.assertNotIn("archived", spy.last_params)

    def test_clone_call_shape_sees_archived_projects(self):
        """clone.py calls get_projects(url, headers, group_id) positionally and
        passes no archived argument. That exact shape must now see them."""
        payload = [{"id": 1, "path_with_namespace": "z/a", "archived": True}]
        with _PaginatedSpy(payload) as spy:
            out = api.get_projects(URL, HEADERS, 42)
        self.assertNotIn("archived", spy.last_params)
        self.assertEqual(len(out), 1)

    def test_boolean_params_are_lowercase_strings_not_python_bools(self):
        # Not the defect, but the invariant the fix must not break: requests
        # encodes a Python bool as "True"/"False", which GitLab accepts only by
        # leniency. `archived` is now a variable, and a variable is exactly
        # where a bool sneaks in — so every tri-state value is checked.
        for value in (None, True, False, "all", "active", "only"):
            with self.subTest(archived=value):
                with _PaginatedSpy() as spy:
                    api.get_group_projects_recursive(
                        URL, HEADERS, 42, archived=value)
                for key, v in spy.last_params.items():
                    self.assertIsInstance(
                        v, str, f"{key} must be a string, not a Python bool")
                    if v.lower() in ("true", "false"):
                        self.assertEqual(
                            v, v.lower(),
                            f"{key} must be the lowercase string GitLab "
                            f"documents, got {v!r}")


class ArchivedIsSelectableTests(unittest.TestCase):
    """A caller must be able to ask for each of the three answers."""

    def test_group_listing_can_include_archived(self):
        with _PaginatedSpy() as spy:
            api.get_group_projects_recursive(URL, HEADERS, 42, archived=True)
        self.assertEqual(spy.last_params.get("archived"), "true")

    def test_direct_group_listing_can_include_archived(self):
        with _PaginatedSpy() as spy:
            api.get_projects(URL, HEADERS, 42, archived=True)
        self.assertNotEqual(spy.last_params.get("archived"), "false")

    def test_active_only_is_still_available_as_an_opt_in(self):
        """The old behaviour is not deleted, only demoted from default."""
        for value in (False, "active", "false", "exclude"):
            with self.subTest(archived=value):
                with _PaginatedSpy() as spy:
                    api.get_projects(URL, HEADERS, 42, archived=value)
                self.assertEqual(spy.last_params.get("archived"), "false")

    def test_archived_only_is_available_for_auditing(self):
        for value in (True, "only", "archived", "true"):
            with self.subTest(archived=value):
                with _PaginatedSpy() as spy:
                    api.get_projects(URL, HEADERS, 42, archived=value)
                self.assertEqual(spy.last_params.get("archived"), "true")

    def test_everything_is_available_explicitly_too(self):
        for value in (None, "all", "any", "both"):
            with self.subTest(archived=value):
                with _PaginatedSpy() as spy:
                    api.get_projects(URL, HEADERS, 42, archived=value)
                self.assertNotIn("archived", spy.last_params)

    def test_an_unknown_value_is_refused_not_silently_degraded(self):
        """A typo must not quietly become a filter nobody asked for — that is
        how the original under-count stayed invisible for so long."""
        for value in ("maybe", "yes-please", "0", "1"):
            with self.subTest(archived=value):
                with self.assertRaises(ValueError):
                    api.get_projects(URL, HEADERS, 42, archived=value)

    def test_is_archived_labels_a_listed_project(self):
        self.assertTrue(api.is_archived({"archived": True}))
        self.assertFalse(api.is_archived({"archived": False}))
        self.assertFalse(api.is_archived({}))       # no key -> not archived
        self.assertFalse(api.is_archived(None))


class CoverageSeesArchivedProjectsTests(unittest.TestCase):
    """
    The end-to-end consequence of the fix, on coverage's own inputs: with the
    archived projects present in the listing, a group missing them reports ◐
    and names them, instead of reporting ✅ against a shortened truth.
    """

    def _cfg(self):
        return {"url": URL, "headers": HEADERS, "min_access": 0}

    REMOTE = [
        {"id": 1, "path_with_namespace": "vertex/live-a", "archived": False},
        {"id": 2, "path_with_namespace": "vertex/live-b", "archived": False},
        {"id": 3, "path_with_namespace": "vertex/old-a", "archived": True},
    ]

    def test_a_group_missing_only_archived_repos_is_not_reported_complete(self):
        index = {("hub.example", "vertex/live-a"): "/d/1",
                 ("hub.example", "vertex/live-b"): "/d/2"}
        with _PaginatedSpy(self.REMOTE):
            listing = api.get_group_projects_recursive(URL, HEADERS, 42)
        cloned, total = cov.group_coverage(
            self._cfg(), {"id": 7}, index, listing)
        self.assertEqual((cloned, total), (2, 3))
        self.assertEqual(cov.status_icon(cloned, total), "◐")
        self.assertEqual(
            [p["path_with_namespace"] for p in cov.missing_projects(
                self._cfg(), {"id": 7}, index, listing)],
            ["vertex/old-a"])

    def test_an_already_cloned_archived_repo_is_counted_in_both_halves(self):
        index = {("hub.example", "vertex/live-a"): "/d/1",
                 ("hub.example", "vertex/live-b"): "/d/2",
                 ("hub.example", "vertex/old-a"): "/d/3"}
        with _PaginatedSpy(self.REMOTE):
            listing = api.get_group_projects_recursive(URL, HEADERS, 42)
        self.assertEqual(
            cov.group_coverage(self._cfg(), {"id": 7}, index, listing), (3, 3))


class CoverageCountsWhatItListsTests(unittest.TestCase):
    """
    The deeper invariant, independent of how `archived` is spelled: a project
    that is ON DISK must appear in both the numerator and the denominator.

    Today an archived project appears in neither, so the icon reads ✅ complete
    on a group that is missing projects, and 6 clones are uncounted. This test
    works purely on coverage's own inputs, so it stays valid whatever the API
    signature becomes.
    """

    def _cfg(self):
        return {"url": URL, "headers": HEADERS, "min_access": 0}

    def test_a_cloned_project_is_counted_in_both_halves(self):
        projects = [
            {"id": 1, "path_with_namespace": "vertex/live-svc"},
            {"id": 2, "path_with_namespace": "vertex/archived-svc"},
        ]
        index = {
            ("hub.example", "vertex/live-svc"): "/somewhere/live-svc",
            ("hub.example", "vertex/archived-svc"): "/somewhere/archived-svc",
        }
        cloned, total = cov.group_coverage(
            self._cfg(), {"id": 7}, index, projects)
        self.assertEqual((cloned, total), (2, 2))
        self.assertEqual(cov.status_icon(cloned, total), "✅")

    def test_a_project_missing_from_the_listing_is_invisible_in_both_halves(self):
        # Exactly what `archived=false` does: the archived project is dropped
        # from `projects`, so its clone on disk is counted nowhere and the
        # group reports complete while it is not.
        listed = [{"id": 1, "path_with_namespace": "vertex/live-svc"}]
        index = {
            ("hub.example", "vertex/live-svc"): "/somewhere/live-svc",
            ("hub.example", "vertex/archived-svc"): "/somewhere/archived-svc",
        }
        cloned, total = cov.group_coverage(self._cfg(), {"id": 7}, index, listed)
        self.assertEqual(
            (cloned, total), (1, 1),
            "a clone whose project was filtered out of the listing is counted "
            "in neither half — the ✅ is computed against a shortened truth")
        self.assertEqual(cov.status_icon(cloned, total), "✅")

    def test_missing_projects_reports_the_filtered_gap(self):
        listed = [
            {"id": 1, "path_with_namespace": "vertex/live-svc"},
            {"id": 2, "path_with_namespace": "vertex/archived-svc"},
        ]
        index = {("hub.example", "vertex/live-svc"): "/somewhere/live-svc"}
        missing = cov.missing_projects(self._cfg(), {"id": 7}, index, listed)
        self.assertEqual([p["path_with_namespace"] for p in missing],
                         ["vertex/archived-svc"])

    def test_case_differences_never_double_count(self):
        # coverage keys are lowercased; a fix that re-lists projects must not
        # reintroduce a case-sensitive compare.
        listed = [{"id": 1, "path_with_namespace": "Vertex/Live-SVC"}]
        index = {("hub.example", "vertex/live-svc"): "/somewhere/live-svc"}
        cloned, total = cov.group_coverage(self._cfg(), {"id": 7}, index, listed)
        self.assertEqual((cloned, total), (1, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
