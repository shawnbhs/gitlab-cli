"""Tests for :mod:`gitlab_mcp.reporting` — offline, no GitLab server.

Two kinds of double are used, and the choice is deliberate:

* **A fake context** (:class:`FakeCtx`) stands in for ``GitLabContext``. It is
  a working in-memory implementation keyed by request path, not a mock with
  scripted expectations, so the assertions below are about the *report* a
  function returns rather than about which calls it made. Where the request
  itself is the behaviour under test — ``with_stats=true``, ``per_page=100``,
  an events window — the recorded parameters are asserted directly.
* **Real git repositories** for :func:`reporting.local_status`. Faking
  ``subprocess`` there would only prove that the fake works; the thing worth
  testing is that the argv we build actually produces the branch, dirty count
  and ahead/behind numbers git reports. Every repository is created under the
  process temp directory and removed in ``tearDown``.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from gitlab_mcp import reporting
from tests import _home_guard


def setUpModule() -> None:
    """Point HOME and every gitlab-cli config path at a temporary directory."""
    _home_guard.activate()


def tearDownModule() -> None:
    """Restore HOME; fail if any test reached into the real home."""
    _home_guard.deactivate()


GIT = shutil.which("git")

PROJECT = "group-a/project-a"
PROJECT_ID = 42
PROJECT_PATH = f"/projects/{PROJECT_ID}"


class FakeCtx:
    """An in-memory stand-in for ``GitLabContext``.

    ``responses`` maps a request path to a payload. A value may be an
    ``Exception`` instance, which is raised instead of returned — that is how
    a Community Edition 404 on ``/approvals`` is simulated.
    """

    def __init__(self, responses: dict | None = None,
                 master_dir: str = "/srv/MasterGroups",
                 totals: dict | None = None):
        self.name = "example"
        self.url = "https://gitlab.example.com"
        self.master_dir = master_dir
        self._responses = dict(responses or {})
        self._totals = dict(totals or {})
        self.gets: list[tuple[str, dict | None]] = []
        self.paginations: list[tuple[str, dict | None, int | None]] = []
        self.captured: list[tuple] = []

    # -- read ---------------------------------------------------------------
    def get(self, path, params=None):
        self.gets.append((path, params))
        if path not in self._responses:
            raise LookupError(f"404 no fake response for GET {path}")
        value = self._responses[path]
        if isinstance(value, Exception):
            raise value
        return value

    def paginate(self, path, params=None, limit=None):
        self.paginations.append((path, params, limit))
        items = self._responses.get(path, [])
        if isinstance(items, Exception):
            raise items
        items = list(items)
        total = self._totals.get(path, len(items))
        truncated = limit is not None and total > limit
        return items[:limit] if limit is not None else items, truncated, total

    # -- writes are a contract violation in this module ----------------------
    def post(self, path, json=None):  # pragma: no cover - must never run
        raise AssertionError(f"reporting must not POST ({path})")

    def put(self, path, json=None):  # pragma: no cover - must never run
        raise AssertionError(f"reporting must not PUT ({path})")

    # -- helpers ------------------------------------------------------------
    def project_id(self, ref):
        return PROJECT_ID if not str(ref).isdigit() else int(ref)

    def user_id(self, ref):
        return 7

    def cfg_public(self):
        return {"name": self.name, "url": self.url,
                "master_dir": self.master_dir, "folder": "MasterGroups"}

    def git_env(self):
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def run_captured(self, fn, *args, **kwargs):
        self.captured.append((fn, args, kwargs))
        return fn(*args, **kwargs), "captured output"

    # -- assertion sugar -----------------------------------------------------
    def params_for(self, path):
        """Query parameters of the first paginate/get recorded for ``path``."""
        for recorded, params, _limit in self.paginations:
            if recorded == path:
                return params or {}
        for recorded, params in self.gets:
            if recorded == path:
                return params or {}
        raise AssertionError(f"no request recorded for {path}")


def commit(sha="a" * 40, title="Add a thing", name="Alice Example",
           email="alice@example.com", date_="2026-10-05T10:00:00.000+00:00",
           additions=10, deletions=2):
    """A GitLab commit payload with stats; override any field."""
    return {
        "id": sha,
        "short_id": sha[:8],
        "title": title,
        "message": f"{title}\n\nbody\n",
        "author_name": name,
        "author_email": email,
        "created_at": date_,
        "committed_date": date_,
        "web_url": f"https://gitlab.example.com/{PROJECT}/-/commit/{sha}",
        "stats": {"additions": additions, "deletions": deletions,
                  "total": additions + deletions},
    }


def mr(iid=7, state="opened", author="alice", **over):
    """A merge request payload; override any field."""
    base = {
        "iid": iid,
        "project_id": PROJECT_ID,
        "title": "Add a thing",
        "state": state,
        "author": {"username": author},
        "assignees": [{"username": "bob"}],
        "reviewers": [{"username": "bob"}],
        "source_branch": "feature/thing",
        "target_branch": "develop",
        "created_at": "2026-10-01T09:00:00Z",
        "updated_at": "2026-10-05T09:00:00Z",
        "merged_at": "2026-10-05T09:30:00Z" if state == "merged" else None,
        "sha": "b" * 40,
        "labels": ["backend"],
        "web_url": f"https://gitlab.example.com/{PROJECT}/-/merge_requests/{iid}",
    }
    base.update(over)
    return base


# ─── resolve_since ────────────────────────────────────────────────────────────

class ResolveSinceTests(unittest.TestCase):
    """Every accepted time-window form, and the rejection of the rest."""

    NOW = datetime(2026, 10, 11, 15, 30, 0, tzinfo=timezone.utc)

    def test_none_and_empty_mean_no_lower_bound(self):
        self.assertIsNone(reporting.resolve_since(None))
        self.assertIsNone(reporting.resolve_since(""))
        self.assertIsNone(reporting.resolve_since("   "))

    def test_days_weeks_months_years(self):
        self.assertEqual(reporting.resolve_since("7d", self.NOW),
                         "2026-10-04T15:30:00Z")
        self.assertEqual(reporting.resolve_since("2w", self.NOW),
                         "2026-09-27T15:30:00Z")
        self.assertEqual(reporting.resolve_since("1m", self.NOW),
                         "2026-09-11T15:30:00Z")
        self.assertEqual(reporting.resolve_since("1y", self.NOW),
                         "2025-10-11T15:30:00Z")

    def test_today_is_midnight_utc(self):
        self.assertEqual(reporting.resolve_since("today", self.NOW),
                         "2026-10-11T00:00:00Z")

    def test_yesterday_is_the_previous_midnight(self):
        self.assertEqual(reporting.resolve_since("yesterday", self.NOW),
                         "2026-10-10T00:00:00Z")

    def test_plain_date_becomes_midnight_utc(self):
        self.assertEqual(reporting.resolve_since("2026-01-02", self.NOW),
                         "2026-01-02T00:00:00Z")

    def test_iso_timestamp_is_normalised_to_utc(self):
        self.assertEqual(
            reporting.resolve_since("2026-01-02T12:00:00+02:00", self.NOW),
            "2026-01-02T10:00:00Z",
        )

    def test_zulu_timestamp_round_trips(self):
        self.assertEqual(reporting.resolve_since("2026-01-02T10:00:00Z", self.NOW),
                         "2026-01-02T10:00:00Z")

    def test_case_is_ignored_for_keywords_and_units(self):
        self.assertEqual(reporting.resolve_since("TODAY", self.NOW),
                         "2026-10-11T00:00:00Z")
        self.assertEqual(reporting.resolve_since("7D", self.NOW),
                         "2026-10-04T15:30:00Z")

    def test_zero_days_is_now(self):
        self.assertEqual(reporting.resolve_since("0d", self.NOW),
                         "2026-10-11T15:30:00Z")

    def test_garbage_raises_rather_than_guessing(self):
        for bad in ("last tuesday", "7x", "d7", "2026-13-40", "-3d"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    reporting.resolve_since(bad, self.NOW)

    def test_default_clock_is_used_when_now_is_omitted(self):
        self.assertTrue(reporting.resolve_since("1d").endswith("Z"))


# ─── Diff accounting ──────────────────────────────────────────────────────────

class DiffAccountingTests(unittest.TestCase):
    """Line counting and the shared diff-text budget."""

    DIFF = ("--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,2 +1,3 @@\n"
            " keep\n"
            "+added one\n"
            "+added two\n"
            "-removed one\n")

    def test_file_headers_are_not_counted_as_content(self):
        self.assertEqual(reporting._count_diff_lines(self.DIFF), (2, 1))

    def test_empty_diff_counts_zero(self):
        self.assertEqual(reporting._count_diff_lines(""), (0, 0))
        self.assertEqual(reporting._count_diff_lines(None), (0, 0))

    def test_budget_is_spent_in_order_and_reported(self):
        entries = [
            {"new_path": "a.py", "diff": "+" * 30},
            {"new_path": "b.py", "diff": "+" * 30},
        ]
        files, truncated = reporting._pack_diffs(entries, 40)
        self.assertTrue(truncated)
        self.assertFalse(files[0]["diff_truncated"])
        self.assertEqual(len(files[0]["diff"]), 30)
        self.assertTrue(files[1]["diff_truncated"])
        self.assertEqual(len(files[1]["diff"]), 10)
        self.assertEqual(files[1]["diff_omitted_chars"], 20)

    def test_line_counts_survive_truncation(self):
        """A truncated body must not understate how much code changed."""
        entries = [{"new_path": "a.py", "diff": "+one\n+two\n+three\n"}]
        files, truncated = reporting._pack_diffs(entries, 2)
        self.assertTrue(truncated)
        self.assertEqual(files[0]["additions"], 3)

    def test_flags_and_rename_path_are_preserved(self):
        entries = [{"old_path": "old.py", "new_path": "new.py",
                    "renamed_file": True, "diff": ""}]
        files, truncated = reporting._pack_diffs(entries, 100)
        self.assertFalse(truncated)
        self.assertEqual(files[0]["path"], "new.py")
        self.assertEqual(files[0]["old_path"], "old.py")
        self.assertTrue(files[0]["renamed_file"])

    def test_totals_roll_up_every_file(self):
        files = [{"additions": 3, "deletions": 1}, {"additions": 2, "deletions": 0}]
        self.assertEqual(reporting._diff_totals(files),
                         {"files_changed": 2, "additions": 5,
                          "deletions": 1, "total": 6})


# ─── Catalogue ────────────────────────────────────────────────────────────────

class CatalogueTests(unittest.TestCase):
    """Groups, projects, users, branches, pipelines, file content."""

    def test_instances_lists_names_without_tokens(self):
        # Real context + real gitlab-cli reader, against the fake instances.json
        # the home guard wrote. A token in the environment must not leak in.
        with mock.patch.dict(os.environ,
                             {"GITLAB_TOKEN_EXAMPLE": "glpat-...TEST"}):
            rows = reporting.instances()
        self.assertEqual([r["name"] for r in rows], ["example"])
        self.assertEqual(rows[0]["url"], "https://gitlab.example.com")
        self.assertTrue(rows[0]["active"])
        self.assertNotIn("token", rows[0])
        self.assertNotIn("glpat-", repr(rows))

    def test_groups_requests_all_available_and_flattens(self):
        ctx = FakeCtx({"/groups": [
            {"id": 1, "name": "Group A", "full_path": "group-a",
             "visibility": "private", "web_url": "https://gitlab.example.com/group-a"},
        ]})
        rows = reporting.groups(ctx, search="group")
        self.assertEqual(rows[0]["full_path"], "group-a")
        params = ctx.params_for("/groups")
        self.assertEqual(params["all_available"], "true")
        self.assertEqual(params["search"], "group")
        self.assertEqual(params["per_page"], 100)

    def test_projects_in_a_group_includes_subgroups(self):
        ctx = FakeCtx({"/groups/group-a/projects": [
            {"id": 42, "name": "Project A",
             "path_with_namespace": PROJECT, "default_branch": "main"},
        ]})
        rows = reporting.projects(ctx, group="group-a")
        self.assertEqual(rows[0]["path_with_namespace"], PROJECT)
        self.assertEqual(
            ctx.params_for("/groups/group-a/projects")["include_subgroups"],
            "true")

    def test_group_path_with_slash_is_encoded_into_one_segment(self):
        ctx = FakeCtx({"/groups/group-a%2Fsub/projects": []})
        reporting.projects(ctx, group="group-a/sub")
        self.assertEqual(ctx.paginations[0][0], "/groups/group-a%2Fsub/projects")

    def test_membership_only_applies_to_the_instance_wide_listing(self):
        ctx = FakeCtx({"/projects": []})
        reporting.projects(ctx, membership=True)
        self.assertEqual(ctx.params_for("/projects")["membership"], "true")

    def test_membership_is_not_sent_for_a_group_listing(self):
        ctx = FakeCtx({"/groups/group-a/projects": []})
        reporting.projects(ctx, group="group-a", membership=True)
        self.assertNotIn("membership",
                         ctx.params_for("/groups/group-a/projects"))

    def test_users_search_returns_public_fields_only(self):
        ctx = FakeCtx({"/users": [
            {"id": 7, "username": "alice", "name": "Alice Example",
             "state": "active", "public_email": "alice@example.com",
             "password": "nope"},
        ]})
        rows = reporting.users(ctx, "alice")
        self.assertEqual(rows[0]["username"], "alice")
        self.assertNotIn("password", rows[0])

    def test_branches_report_protection_and_tip(self):
        ctx = FakeCtx({f"{PROJECT_PATH}/repository/branches": [
            {"name": "main", "default": True, "protected": True,
             "commit": {"id": "c" * 40, "title": "Init",
                        "committed_date": "2026-10-01T00:00:00Z"}},
        ]})
        rows = reporting.branches(ctx, PROJECT)
        self.assertTrue(rows[0]["protected"])
        self.assertEqual(rows[0]["commit_sha"], "c" * 40)

    def test_pipelines_are_flattened(self):
        ctx = FakeCtx({f"{PROJECT_PATH}/pipelines": [
            {"id": 9, "status": "success", "ref": "main", "sha": "d" * 40},
        ]})
        rows = reporting.pipelines(ctx, PROJECT, ref="main", limit=5)
        self.assertEqual(rows[0]["status"], "success")
        self.assertEqual(ctx.params_for(f"{PROJECT_PATH}/pipelines")["ref"], "main")

    def test_file_content_decodes_base64(self):
        body = "print('hello')\n"
        ctx = FakeCtx({
            f"{PROJECT_PATH}/repository/files/src%2Fapp.py": {
                "file_path": "src/app.py", "ref": "main", "size": len(body),
                "content": base64.b64encode(body.encode()).decode(),
            },
        })
        result = reporting.file_content(ctx, PROJECT, "src/app.py", ref="main")
        self.assertEqual(result["content"], body)
        self.assertFalse(result["truncated"])
        self.assertFalse(result["binary"])

    def test_file_content_truncates_and_reports_the_real_size(self):
        body = "x" * 100
        ctx = FakeCtx({
            f"{PROJECT_PATH}/repository/files/big.txt": {
                "content": base64.b64encode(body.encode()).decode(),
            },
        })
        result = reporting.file_content(ctx, PROJECT, "big.txt", max_chars=10)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["content"]), 10)
        self.assertEqual(result["total"], 100)

    def test_binary_file_is_reported_not_mangled(self):
        ctx = FakeCtx({
            f"{PROJECT_PATH}/repository/files/logo.png": {
                "content": base64.b64encode(b"\xff\xfe\x00\x01").decode(),
            },
        })
        result = reporting.file_content(ctx, PROJECT, "logo.png")
        self.assertTrue(result["binary"])
        self.assertEqual(result["content"], "")
        self.assertEqual(result["bytes"], 4)


# ─── Commits ──────────────────────────────────────────────────────────────────

class CommitsTests(unittest.TestCase):
    """Commit listing, totals, and the request shape the contract names."""

    def test_rows_and_summary(self):
        ctx = FakeCtx({f"{PROJECT_PATH}/repository/commits": [
            commit(sha="a" * 40, additions=10, deletions=2),
            commit(sha="b" * 40, additions=5, deletions=5, name="Bob Example",
                   email="bob@example.com"),
        ]})
        result = reporting.commits(ctx, PROJECT, since="7d")
        self.assertEqual(result["summary"],
                         {"commits": 2, "merge_commits": 0, "additions": 15,
                          "deletions": 7, "total": 22})
        self.assertEqual(result["rows"][0]["sha"], "a" * 40)
        self.assertEqual(result["rows"][0]["total"], 12)

    def test_requests_stats_and_the_maximum_page_size(self):
        ctx = FakeCtx({f"{PROJECT_PATH}/repository/commits": []})
        reporting.commits(ctx, PROJECT, since="7d")
        params = ctx.params_for(f"{PROJECT_PATH}/repository/commits")
        self.assertEqual(params["with_stats"], "true")
        self.assertEqual(params["per_page"], 100)
        self.assertIn("since", params)

    def test_with_stats_false_omits_the_parameter(self):
        ctx = FakeCtx({f"{PROJECT_PATH}/repository/commits": []})
        reporting.commits(ctx, PROJECT, with_stats=False)
        self.assertNotIn("with_stats",
                         ctx.params_for(f"{PROJECT_PATH}/repository/commits"))

    def test_branch_becomes_ref_name_and_author_passes_through(self):
        ctx = FakeCtx({f"{PROJECT_PATH}/repository/commits": []})
        reporting.commits(ctx, PROJECT, branch="develop", author="alice")
        params = ctx.params_for(f"{PROJECT_PATH}/repository/commits")
        self.assertEqual(params["ref_name"], "develop")
        self.assertEqual(params["author"], "alice")

    def test_until_is_resolved_too(self):
        ctx = FakeCtx({f"{PROJECT_PATH}/repository/commits": []})
        result = reporting.commits(ctx, PROJECT, since="2026-10-01",
                                   until="2026-10-08")
        self.assertEqual(result["until"], "2026-10-08T00:00:00Z")

    def test_truncation_is_explicit_with_a_total(self):
        path = f"{PROJECT_PATH}/repository/commits"
        ctx = FakeCtx({path: [commit(sha=str(i) * 40) for i in range(5)]},
                      totals={path: 500})
        result = reporting.commits(ctx, PROJECT, limit=3)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["total"], 500)
        self.assertEqual(len(result["rows"]), 3)

    def test_empty_history_is_zero_not_an_error(self):
        ctx = FakeCtx({f"{PROJECT_PATH}/repository/commits": []})
        result = reporting.commits(ctx, PROJECT)
        self.assertEqual(result["summary"]["commits"], 0)
        self.assertEqual(result["rows"], [])


class CommitDetailTests(unittest.TestCase):
    """One commit with its diff."""

    def setUp(self):
        sha = "a" * 40
        self.sha = sha
        self.ctx = FakeCtx({
            f"{PROJECT_PATH}/repository/commits/{sha}": commit(sha=sha),
            f"{PROJECT_PATH}/repository/commits/{sha}/diff": [
                {"new_path": "app.py", "old_path": "app.py",
                 "diff": "+one\n+two\n-gone\n"},
            ],
        })

    def test_message_stats_and_per_file_counts(self):
        result = reporting.commit_detail(self.ctx, PROJECT, self.sha)
        self.assertEqual(result["sha"], self.sha)
        self.assertIn("body", result["message"])
        self.assertEqual(result["files"][0]["additions"], 2)
        self.assertEqual(result["files"][0]["deletions"], 1)
        self.assertEqual(result["diff_summary"]["files_changed"], 1)
        self.assertFalse(result["truncated"])

    def test_diff_budget_sets_the_truncated_flag(self):
        result = reporting.commit_detail(self.ctx, PROJECT, self.sha,
                                         max_diff_chars=4)
        self.assertTrue(result["truncated"])
        self.assertTrue(result["files"][0]["diff_truncated"])

    def test_a_missing_diff_endpoint_is_a_note_not_a_crash(self):
        sha = "a" * 40
        ctx = FakeCtx({f"{PROJECT_PATH}/repository/commits/{sha}": commit(sha=sha)})
        result = reporting.commit_detail(ctx, PROJECT, sha)
        self.assertEqual(result["files"], [])
        self.assertTrue(any("commit diff" in n for n in result["notes"]))


class CompareTests(unittest.TestCase):
    """Branch-to-branch comparison."""

    def test_commits_and_diff_summary(self):
        ctx = FakeCtx({f"{PROJECT_PATH}/repository/compare": {
            "commits": [commit(), commit(sha="b" * 40)],
            "diffs": [{"new_path": "app.py", "diff": "+added\n"}],
        }})
        result = reporting.compare(ctx, PROJECT, "main", "feature/thing")
        self.assertEqual(result["commit_count"], 2)
        self.assertEqual(result["diff_summary"]["additions"], 1)

    def test_refs_travel_as_query_parameters(self):
        """A ref with a slash needs no escaping when it is not in the path."""
        ctx = FakeCtx({f"{PROJECT_PATH}/repository/compare":
                       {"commits": [], "diffs": []}})
        reporting.compare(ctx, PROJECT, "main", "feature/thing")
        path, params = ctx.gets[0]
        self.assertEqual(path, f"{PROJECT_PATH}/repository/compare")
        self.assertEqual(params, {"from": "main", "to": "feature/thing"})

    def test_server_side_timeout_is_surfaced(self):
        ctx = FakeCtx({f"{PROJECT_PATH}/repository/compare": {
            "commits": [], "diffs": [], "compare_timeout": True,
        }})
        self.assertTrue(reporting.compare(ctx, PROJECT, "main", "x")["timed_out"])


class ContributorsTests(unittest.TestCase):
    """Per-author aggregation over a date window."""

    def setUp(self):
        self.ctx = FakeCtx({f"{PROJECT_PATH}/repository/commits": [
            commit(sha="a" * 40, additions=10, deletions=1,
                   date_="2026-10-01T10:00:00+00:00"),
            commit(sha="b" * 40, additions=4, deletions=0,
                   date_="2026-10-05T10:00:00+00:00"),
            commit(sha="c" * 40, name="Bob Example", email="bob@example.com",
                   additions=1, deletions=1, date_="2026-10-03T10:00:00+00:00"),
        ]})

    def test_sorted_by_commit_count_with_line_totals(self):
        result = reporting.contributors(self.ctx, PROJECT, since="30d")
        self.assertEqual(result["author_count"], 2)
        top = result["authors"][0]
        self.assertEqual(top["email"], "alice@example.com")
        self.assertEqual(top["commits"], 2)
        self.assertEqual(top["additions"], 14)
        self.assertEqual(top["deletions"], 1)

    def test_first_and_last_dates_bound_the_activity(self):
        top = reporting.contributors(self.ctx, PROJECT)["authors"][0]
        self.assertEqual(top["first_date"], "2026-10-01T10:00:00+00:00")
        self.assertEqual(top["last_date"], "2026-10-05T10:00:00+00:00")

    def test_one_identity_per_email_regardless_of_name_spelling(self):
        ctx = FakeCtx({f"{PROJECT_PATH}/repository/commits": [
            commit(sha="a" * 40, name="alice", email="Alice@Example.com"),
            commit(sha="b" * 40, name="Alice Example", email="alice@example.com"),
        ]})
        result = reporting.contributors(ctx, PROJECT)
        self.assertEqual(result["author_count"], 1)
        self.assertEqual(result["authors"][0]["commits"], 2)


# ─── Project-level reports ────────────────────────────────────────────────────

class ProjectOverviewTests(unittest.TestCase):
    """The one-screen project answer."""

    def setUp(self):
        self.responses = {
            PROJECT_PATH: {
                "id": PROJECT_ID, "name": "Project A",
                "path_with_namespace": PROJECT, "default_branch": "main",
                "visibility": "private", "last_activity_at": "2026-10-10T08:00:00Z",
            },
            f"{PROJECT_PATH}/repository/commits": [
                commit(sha="a" * 40, additions=10, deletions=2),
                commit(sha="b" * 40, name="Bob Example",
                       email="bob@example.com", additions=3, deletions=3),
            ],
            f"{PROJECT_PATH}/merge_requests": [mr(), mr(iid=8)],
            f"{PROJECT_PATH}/languages": {"Python": 90.0, "Shell": 10.0},
        }

    def test_identity_activity_and_contributors(self):
        result = reporting.project_overview(FakeCtx(self.responses), PROJECT)
        self.assertEqual(result["project"], PROJECT)
        self.assertEqual(result["default_branch"], "main")
        self.assertEqual(result["activity"],
                         {"commits": 2, "merge_commits": 0, "additions": 13,
                          "deletions": 5, "commits_truncated": False})
        self.assertEqual(result["contributor_count"], 2)
        self.assertEqual(result["open_merge_requests"], 2)
        self.assertEqual(result["languages"]["Python"], 90.0)

    def test_top_contributors_are_capped(self):
        result = reporting.project_overview(FakeCtx(self.responses), PROJECT, top=1)
        self.assertEqual(len(result["top_contributors"]), 1)
        self.assertEqual(result["contributor_count"], 2)

    def test_missing_languages_endpoint_is_noted(self):
        responses = dict(self.responses)
        del responses[f"{PROJECT_PATH}/languages"]
        result = reporting.project_overview(FakeCtx(responses), PROJECT)
        self.assertEqual(result["languages"], {})
        self.assertTrue(any("languages" in n for n in result["notes"]))


class ProjectProgressTests(unittest.TestCase):
    """Weekly cadence, merge throughput, pipeline health, tags."""

    def setUp(self):
        self.ctx = FakeCtx({
            f"{PROJECT_PATH}/repository/commits": [
                # 2026-10-05 and 2026-10-07 are both ISO week 41.
                commit(sha="a" * 40, date_="2026-10-05T10:00:00+00:00",
                       additions=5, deletions=1),
                commit(sha="b" * 40, date_="2026-10-07T10:00:00+00:00",
                       additions=2, deletions=0),
                # 2026-09-28 is ISO week 40.
                commit(sha="c" * 40, date_="2026-09-28T10:00:00+00:00",
                       name="Bob Example", email="bob@example.com",
                       additions=7, deletions=7),
            ],
            f"{PROJECT_PATH}/merge_requests": [mr(state="merged")],
            f"{PROJECT_PATH}/pipelines": [
                {"id": 1, "status": "success"},
                {"id": 2, "status": "failed"},
                {"id": 3, "status": "success"},
                {"id": 4, "status": "running"},
            ],
            f"{PROJECT_PATH}/repository/tags": [
                {"name": "v1.1", "commit": {"committed_date": "2026-10-09T00:00:00Z"}},
                {"name": "v1.0", "commit": {"committed_date": "2026-09-01T00:00:00Z"}},
            ],
        })

    def test_commits_bucketed_per_iso_week(self):
        weeks = reporting.project_progress(self.ctx, PROJECT)["commits_per_week"]
        self.assertEqual([w["week"] for w in weeks], ["2026-W40", "2026-W41"])
        self.assertEqual(weeks[1]["commits"], 2)
        self.assertEqual(weeks[1]["additions"], 7)

    def test_commits_bucketed_per_author(self):
        authors = reporting.project_progress(self.ctx, PROJECT)["commits_per_author"]
        self.assertEqual(authors[0]["email"], "alice@example.com")
        self.assertEqual(authors[0]["commits"], 2)

    def test_pipeline_success_rate_ignores_unfinished_runs(self):
        result = reporting.project_progress(self.ctx, PROJECT)
        self.assertEqual(result["pipelines_finished"], 3)
        self.assertEqual(result["pipeline_success_rate"], 0.6667)

    def test_success_rate_is_none_when_nothing_finished(self):
        ctx = FakeCtx({
            f"{PROJECT_PATH}/repository/commits": [],
            f"{PROJECT_PATH}/merge_requests": [],
            f"{PROJECT_PATH}/pipelines": [{"id": 1, "status": "running"}],
            f"{PROJECT_PATH}/repository/tags": [],
        })
        self.assertIsNone(
            reporting.project_progress(ctx, PROJECT)["pipeline_success_rate"])

    def test_latest_five_tags_only(self):
        ctx = FakeCtx({
            f"{PROJECT_PATH}/repository/commits": [],
            f"{PROJECT_PATH}/merge_requests": [],
            f"{PROJECT_PATH}/pipelines": [],
            f"{PROJECT_PATH}/repository/tags": [
                {"name": f"v{i}"} for i in range(9)
            ],
        })
        self.assertEqual(len(reporting.project_progress(ctx, PROJECT)["latest_tags"]), 5)

    def test_merged_merge_requests_are_listed(self):
        result = reporting.project_progress(self.ctx, PROJECT)
        self.assertEqual(result["merged_count"], 1)
        self.assertEqual(result["merge_requests_merged"][0]["author"], "alice")


# ─── Merge requests ───────────────────────────────────────────────────────────

class MergeRequestsListTests(unittest.TestCase):
    """Listing merge requests across the three scopes."""

    def test_project_scope(self):
        ctx = FakeCtx({f"{PROJECT_PATH}/merge_requests": [mr()]})
        rows = reporting.merge_requests(ctx, project=PROJECT)
        self.assertEqual(rows[0]["iid"], 7)
        self.assertEqual(rows[0]["assignees"], ["bob"])

    def test_group_scope(self):
        ctx = FakeCtx({"/groups/group-a/merge_requests": [mr()]})
        reporting.merge_requests(ctx, group="group-a")
        self.assertEqual(ctx.paginations[0][0], "/groups/group-a/merge_requests")

    def test_instance_scope_asks_for_all_visible(self):
        ctx = FakeCtx({"/merge_requests": [mr()]})
        reporting.merge_requests(ctx)
        self.assertEqual(ctx.params_for("/merge_requests")["scope"], "all")

    def test_author_username_is_resolved_to_an_id(self):
        ctx = FakeCtx({f"{PROJECT_PATH}/merge_requests": []})
        reporting.merge_requests(ctx, project=PROJECT, author="alice")
        params = ctx.params_for(f"{PROJECT_PATH}/merge_requests")
        self.assertEqual(params["author_id"], 7)
        self.assertNotIn("author", params)

    def test_since_becomes_created_after(self):
        ctx = FakeCtx({f"{PROJECT_PATH}/merge_requests": []})
        reporting.merge_requests(ctx, project=PROJECT, since="2026-10-01")
        self.assertEqual(
            ctx.params_for(f"{PROJECT_PATH}/merge_requests")["created_after"],
            "2026-10-01T00:00:00Z")

    def test_draft_is_read_from_either_field_name(self):
        ctx = FakeCtx({f"{PROJECT_PATH}/merge_requests": [
            mr(iid=1, work_in_progress=True), mr(iid=2, draft=True),
        ]})
        rows = reporting.merge_requests(ctx, project=PROJECT)
        self.assertTrue(rows[0]["draft"])
        self.assertTrue(rows[1]["draft"])


class MergeRequestDetailTests(unittest.TestCase):
    """One merge request, in review detail."""

    BASE = f"{PROJECT_PATH}/merge_requests/7"

    def responses(self, **over):
        base = {
            self.BASE: mr(
                detailed_merge_status="mergeable",
                diff_refs={"base_sha": "1" * 40, "head_sha": "2" * 40,
                           "start_sha": "1" * 40},
                head_pipeline={"id": 9, "status": "success", "ref": "feature/thing"},
            ),
            f"{self.BASE}/diffs": [
                {"new_path": "app.py", "diff": "+added\n-removed\n"},
            ],
            f"{self.BASE}/discussions": [
                {"id": "abc", "notes": [
                    {"author": {"username": "bob"}, "body": "Please rename this",
                     "resolvable": True, "resolved": False,
                     "position": {"new_path": "app.py", "new_line": 12}},
                ]},
            ],
            f"{self.BASE}/approvals": {
                "approvals_required": 1, "approvals_left": 0,
                "approved_by": [{"user": {"username": "bob"}}],
            },
        }
        base.update(over)
        return base

    def test_full_detail(self):
        result = reporting.merge_request(FakeCtx(self.responses()), PROJECT, 7)
        self.assertEqual(result["iid"], 7)
        self.assertEqual(result["detailed_merge_status"], "mergeable")
        self.assertEqual(result["diff_summary"]["additions"], 1)
        self.assertEqual(result["diff_summary"]["deletions"], 1)
        self.assertEqual(result["discussion_count"], 1)
        self.assertEqual(result["discussions"][0]["notes"][0]["path"], "app.py")
        self.assertFalse(result["discussions"][0]["resolved"])
        self.assertEqual(result["approvals"]["approved_by"], ["bob"])
        self.assertEqual(result["head_pipeline"]["status"], "success")

    def test_falls_back_to_changes_on_older_gitlab(self):
        responses = self.responses()
        del responses[f"{self.BASE}/diffs"]
        responses[f"{self.BASE}/changes"] = {
            "changes": [{"new_path": "app.py", "diff": "+added\n"}],
        }
        result = reporting.merge_request(FakeCtx(responses), PROJECT, 7)
        self.assertEqual(result["files"][0]["path"], "app.py")
        self.assertTrue(any("mr diffs" in n for n in result["notes"]))

    def test_changes_overflow_sets_truncated(self):
        responses = self.responses()
        del responses[f"{self.BASE}/diffs"]
        responses[f"{self.BASE}/changes"] = {"changes": [], "overflow": True}
        result = reporting.merge_request(FakeCtx(responses), PROJECT, 7)
        self.assertTrue(result["truncated"])

    def test_community_edition_404_on_approvals_is_explained(self):
        responses = self.responses()
        del responses[f"{self.BASE}/approvals"]
        result = reporting.merge_request(FakeCtx(responses), PROJECT, 7)
        self.assertIsNone(result["approvals"])
        self.assertTrue(any("Premium" in n for n in result["notes"]))

    def test_with_diff_false_skips_the_diff_requests(self):
        ctx = FakeCtx(self.responses())
        result = reporting.merge_request(ctx, PROJECT, 7, with_diff=False)
        self.assertEqual(result["files"], [])
        self.assertFalse(any(path.endswith("/diffs") for path, _ in ctx.gets))

    def test_diff_budget_truncates_and_flags(self):
        ctx = FakeCtx(self.responses())
        result = reporting.merge_request(ctx, PROJECT, 7, max_diff_chars=3)
        self.assertTrue(result["truncated"])

    def test_resolved_is_none_for_a_non_resolvable_thread(self):
        responses = self.responses()
        responses[f"{self.BASE}/discussions"] = [
            {"id": "sys", "notes": [{"system": True, "body": "merged"}]},
        ]
        result = reporting.merge_request(FakeCtx(responses), PROJECT, 7)
        self.assertIsNone(result["discussions"][0]["resolved"])


# ─── person_report ────────────────────────────────────────────────────────────

def event(project_id=PROJECT_ID, action="pushed to", target=None):
    """A GitLab contribution event."""
    return {"project_id": project_id, "action_name": action,
            "target_type": target, "created_at": "2026-10-05T10:00:00Z"}


class PersonReportTests(unittest.TestCase):
    """The person-centric report and its honesty fields."""

    def base_responses(self, **over):
        base = {
            "/users": [{"id": 7, "username": "alice", "name": "Alice Example",
                        "state": "active", "public_email": "alice@example.com"}],
            "/users/7/events": [
                event(), event(action="commented on", target="Note"),
                event(action="opened", target="MergeRequest"),
                event(action="accepted", target="MergeRequest"),
                event(action="opened", target="Issue"),
            ],
            f"/projects/{PROJECT_ID}": {
                "id": PROJECT_ID, "path_with_namespace": PROJECT,
                "default_branch": "main",
            },
            f"{PROJECT_PATH}/repository/commits": [
                commit(sha="a" * 40, additions=10, deletions=2,
                       title="Add the invoice exporter"),
                commit(sha="b" * 40, additions=4, deletions=0,
                       title="Fix the exporter rounding"),
                commit(sha="c" * 40, name="Bob Example",
                       email="bob@example.com", additions=99, deletions=99),
            ],
            f"{PROJECT_PATH}/merge_requests": [mr(state="merged"), mr(iid=8)],
        }
        base.update(over)
        return base

    def test_per_project_commits_lines_and_samples(self):
        result = reporting.person_report(FakeCtx(self.base_responses()), ["alice"])
        person = result["people"][0]
        self.assertTrue(person["found"])
        work = person["projects"][0]
        self.assertEqual(work["project"], PROJECT)
        self.assertEqual(work["commits"], 2)
        self.assertEqual(work["additions"], 14)
        self.assertEqual(work["deletions"], 2)
        self.assertIn("Add the invoice exporter", work["sample_commit_titles"])

    def test_other_authors_commits_are_not_attributed(self):
        work = reporting.person_report(
            FakeCtx(self.base_responses()), ["alice"])["people"][0]["projects"][0]
        self.assertEqual(work["commits_scanned"], 3)
        self.assertEqual(work["commits"], 2)

    def test_name_equality_is_a_fallback_when_the_email_is_hidden(self):
        responses = self.base_responses(**{
            f"{PROJECT_PATH}/repository/commits": [
                commit(sha="a" * 40, name="Alice Example",
                       email="alice@laptop.local"),
            ],
        })
        responses["/users"] = [{"id": 7, "username": "alice",
                                "name": "Alice Example", "state": "active"}]
        work = reporting.person_report(
            FakeCtx(responses), ["alice"])["people"][0]["projects"][0]
        self.assertEqual(work["commits"], 1)

    def test_name_matching_is_exact_not_a_substring(self):
        responses = self.base_responses(**{
            f"{PROJECT_PATH}/repository/commits": [
                commit(sha="a" * 40, name="Alice Example Jr",
                       email="other@example.com"),
            ],
        })
        work = reporting.person_report(
            FakeCtx(responses), ["alice"])["people"][0]["projects"][0]
        self.assertEqual(work["commits"], 0)

    def test_private_email_is_used_when_the_api_exposes_it(self):
        responses = self.base_responses(**{
            f"{PROJECT_PATH}/repository/commits": [
                commit(sha="a" * 40, name="a.example",
                       email="alice.private@example.com"),
            ],
        })
        responses["/users"] = [{"id": 7, "username": "alice",
                                "name": "Alice Example",
                                "email": "alice.private@example.com"}]
        work = reporting.person_report(
            FakeCtx(responses), ["alice"])["people"][0]["projects"][0]
        self.assertEqual(work["commits"], 1)

    def test_event_buckets(self):
        person = reporting.person_report(
            FakeCtx(self.base_responses()), ["alice"])["people"][0]
        summary = person["event_summary"]
        self.assertEqual(summary["pushes"], 1)
        self.assertEqual(summary["comments"], 1)
        self.assertEqual(summary["mrs_opened"], 1)
        self.assertEqual(summary["mrs_merged"], 1)
        self.assertEqual(summary["issues_opened"], 1)
        self.assertEqual(summary["events"], 5)

    def test_events_window_is_sent_as_dates(self):
        ctx = FakeCtx(self.base_responses())
        reporting.person_report(ctx, ["alice"], since="2026-10-01",
                                until="2026-10-08")
        params = ctx.params_for("/users/7/events")
        self.assertEqual(params["after"], "2026-10-01")
        self.assertEqual(params["before"], "2026-10-08")

    def test_merge_requests_opened_and_merged_per_project(self):
        work = reporting.person_report(
            FakeCtx(self.base_responses()), ["alice"])["people"][0]["projects"][0]
        self.assertEqual(work["merge_requests_opened"], 2)
        self.assertEqual(work["merge_requests_merged"], 1)

    def test_touched_projects_come_from_events(self):
        responses = self.base_responses()
        responses["/users/7/events"] = [event(project_id=99), event(project_id=99)]
        responses["/projects/99"] = {"id": 99,
                                     "path_with_namespace": "group-a/project-b"}
        responses["/projects/99/repository/commits"] = []
        responses["/projects/99/merge_requests"] = []
        person = reporting.person_report(FakeCtx(responses), ["alice"])["people"][0]
        self.assertEqual([p["project"] for p in person["projects"]],
                         ["group-a/project-b"])

    def test_explicit_projects_are_reported_even_without_events(self):
        responses = self.base_responses()
        responses["/users/7/events"] = []
        person = reporting.person_report(
            FakeCtx(responses), ["alice"], projects=[PROJECT])["people"][0]
        self.assertEqual(len(person["projects"]), 1)
        self.assertEqual(person["projects"][0]["commits"], 2)

    def test_explicit_project_is_not_duplicated_by_an_event(self):
        person = reporting.person_report(
            FakeCtx(self.base_responses()), ["alice"],
            projects=[PROJECT])["people"][0]
        self.assertEqual(len(person["projects"]), 1)

    def test_max_projects_caps_and_sets_truncated(self):
        responses = self.base_responses()
        responses["/users/7/events"] = [event(project_id=1), event(project_id=2)]
        for pid in (1, 2):
            responses[f"/projects/{pid}"] = {
                "id": pid, "path_with_namespace": f"group-a/p{pid}"}
            responses[f"/projects/{pid}/repository/commits"] = []
            responses[f"/projects/{pid}/merge_requests"] = []
        result = reporting.person_report(FakeCtx(responses), ["alice"],
                                         max_projects=1)
        self.assertEqual(len(result["people"][0]["projects"]), 1)
        self.assertTrue(result["people"][0]["truncated"])
        self.assertTrue(result["truncated"])

    def test_unknown_person_is_reported_not_raised(self):
        responses = self.base_responses()
        responses["/users"] = []
        result = reporting.person_report(FakeCtx(responses), ["nobody"])
        person = result["people"][0]
        self.assertFalse(person["found"])
        self.assertEqual(person["projects"], [])
        self.assertEqual(result["totals"]["commits"], 0)

    def test_several_people_are_summed_into_totals(self):
        responses = self.base_responses()
        responses["/users"] = [{"id": 7, "username": "alice",
                                "name": "Alice Example",
                                "public_email": "alice@example.com"}]
        result = reporting.person_report(FakeCtx(responses), ["alice", "alice"])
        self.assertEqual(result["totals"]["people"], 2)
        self.assertEqual(result["totals"]["commits"], 4)
        self.assertEqual(result["totals"]["additions"], 28)

    def test_method_and_limitations_are_always_present(self):
        result = reporting.person_report(FakeCtx(self.base_responses()), ["alice"])
        self.assertIn("author_email", result["method"])
        joined = " ".join(result["limitations"])
        self.assertIn("missed", joined)
        self.assertIn("retention", joined)
        self.assertIn("max_projects", joined)

    def test_an_unreadable_project_is_a_note_not_a_failure(self):
        responses = self.base_responses()
        responses[f"{PROJECT_PATH}/repository/commits"] = PermissionError("403")
        result = reporting.person_report(FakeCtx(responses), ["alice"])
        self.assertEqual(result["people"][0]["projects"][0]["commits"], 0)
        self.assertTrue(any("unavailable" in n for n in result["notes"]))

    def test_reporting_never_writes(self):
        """The fake raises on POST/PUT, so a passing report proves read-only."""
        result = reporting.person_report(FakeCtx(self.base_responses()), ["alice"])
        self.assertTrue(result["people"][0]["found"])


# ─── local_status ─────────────────────────────────────────────────────────────

@unittest.skipIf(GIT is None, "git is not installed")
class LocalStatusTests(unittest.TestCase):
    """Real temporary repositories; git's own answers, not a faked subprocess.

    Every repository lives under one temp directory created in ``setUp`` and
    removed in ``tearDown``, with ``HOME`` pointed into it for the duration so
    the user's real git configuration is never read or written.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="gitlab-mcp-local-")
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        self.master = os.path.join(self.root, "MasterGroups")
        os.makedirs(self.master)
        self._saved_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home

    def tearDown(self):
        if self._saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._saved_home
        shutil.rmtree(self.root, ignore_errors=True)

    # -- fixture helpers ----------------------------------------------------
    def _git(self, repo, *args):
        env = dict(os.environ)
        env.update({
            "HOME": self.home,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Alice Example",
            "GIT_AUTHOR_EMAIL": "alice@example.com",
            "GIT_COMMITTER_NAME": "Alice Example",
            "GIT_COMMITTER_EMAIL": "alice@example.com",
        })
        done = subprocess.run([GIT, *args], cwd=repo, env=env,
                              capture_output=True, text=True, timeout=30,
                              check=False)
        self.assertEqual(done.returncode, 0,
                         f"git {' '.join(args)} failed: {done.stderr}")
        return (done.stdout or "").strip()

    def _make_repo(self, relative, origin=None, branch="main"):
        repo = os.path.join(self.master, *relative.split("/"))
        os.makedirs(repo)
        self._git(repo, "init", "-q", "-b", branch)
        with open(os.path.join(repo, "README.md"), "w", encoding="utf-8") as fh:
            fh.write("# project\n")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "-q", "-m", "Initial commit")
        if origin:
            self._git(repo, "remote", "add", "origin", origin)
        return repo

    def _ctx(self):
        return FakeCtx(master_dir=self.master)

    # -- tests --------------------------------------------------------------
    def test_clean_repo_reports_branch_and_no_dirt(self):
        self._make_repo("group-a/project-a",
                        origin="https://gitlab.example.com/group-a/project-a.git")
        result = reporting.local_status(self._ctx())
        self.assertEqual(result["count"], 1)
        repo = result["repos"][0]
        self.assertEqual(repo["branch"], "main")
        self.assertFalse(repo["dirty"])
        self.assertEqual(repo["dirty_entries"], 0)
        self.assertEqual(repo["relative_path"],
                         os.path.join("group-a", "project-a"))

    def test_dirty_repo_counts_its_entries(self):
        repo = self._make_repo("group-a/project-a")
        with open(os.path.join(repo, "new.txt"), "w", encoding="utf-8") as fh:
            fh.write("scratch\n")
        with open(os.path.join(repo, "README.md"), "a", encoding="utf-8") as fh:
            fh.write("changed\n")
        row = reporting.local_status(self._ctx())["repos"][0]
        self.assertTrue(row["dirty"])
        self.assertEqual(row["dirty_entries"], 2)
        self.assertEqual(reporting.local_status(self._ctx())["dirty"], 1)

    def test_no_upstream_is_reported_as_unknown_not_as_in_sync(self):
        self._make_repo("group-a/project-a")
        row = reporting.local_status(self._ctx())["repos"][0]
        self.assertIsNone(row["ahead"])
        self.assertIsNone(row["behind"])
        self.assertFalse(row["has_upstream"])

    def test_ahead_and_behind_are_counted_without_fetching(self):
        upstream = os.path.join(self.root, "upstream.git")
        os.makedirs(upstream)
        self._git(upstream, "init", "-q", "--bare", "-b", "main")
        repo = self._make_repo("group-a/project-a", origin=upstream)
        self._git(repo, "push", "-q", "-u", "origin", "main")

        # One local commit ahead of the recorded upstream ref.
        with open(os.path.join(repo, "feature.txt"), "w", encoding="utf-8") as fh:
            fh.write("work\n")
        self._git(repo, "add", "feature.txt")
        self._git(repo, "commit", "-q", "-m", "Add a feature")

        row = reporting.local_status(self._ctx())["repos"][0]
        self.assertEqual(row["ahead"], 1)
        self.assertEqual(row["behind"], 0)
        self.assertTrue(row["has_upstream"])
        self.assertFalse(reporting.local_status(self._ctx())["fetched"])

    def test_credentials_in_the_origin_url_are_stripped(self):
        self._make_repo(
            "group-a/project-a",
            origin="https://oauth2:glpat-SECRETVALUE@gitlab.example.com/group-a/project-a.git",
        )
        row = reporting.local_status(self._ctx())["repos"][0]
        self.assertEqual(
            row["origin"],
            "https://gitlab.example.com/group-a/project-a.git")
        self.assertNotIn("glpat", str(row))

    def test_nested_repositories_are_all_found(self):
        self._make_repo("group-a/project-a")
        self._make_repo("group-a/sub/project-b")
        result = reporting.local_status(self._ctx())
        self.assertEqual(result["count"], 2)
        self.assertEqual([r["branch"] for r in result["repos"]], ["main", "main"])

    def test_group_narrows_the_walk(self):
        self._make_repo("group-a/project-a")
        self._make_repo("group-b/project-c")
        result = reporting.local_status(self._ctx(), group="group-b")
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["base"].endswith(os.path.join("MasterGroups",
                                                             "group-b")))

    def test_missing_directory_is_a_note_not_a_crash(self):
        ctx = FakeCtx(master_dir=os.path.join(self.root, "absent"))
        result = reporting.local_status(ctx)
        self.assertEqual(result["repos"], [])
        self.assertIn("does not exist", result["note"])

    def test_discovery_goes_through_run_captured(self):
        """gitlab-cli prints; MCP speaks its protocol on stdout."""
        self._make_repo("group-a/project-a")
        ctx = self._ctx()
        result = reporting.local_status(ctx)
        self.assertEqual(len(ctx.captured), 1)
        self.assertEqual(result["output"], "captured output")

    def test_detached_head_is_flagged(self):
        repo = self._make_repo("group-a/project-a")
        sha = self._git(repo, "rev-parse", "HEAD")
        self._git(repo, "checkout", "-q", sha)
        row = reporting.local_status(self._ctx())["repos"][0]
        self.assertTrue(row["detached"])


class GitInvocationTests(unittest.TestCase):
    """The hardening properties of the local git wrapper."""

    def test_binary_is_resolved_absolutely(self):
        self.assertTrue(reporting.GIT_BIN)

    def test_timeout_is_bounded(self):
        self.assertEqual(reporting.GIT_TIMEOUT_SECONDS, 10)

    def test_env_disables_credential_prompts(self):
        env = reporting._git_env(FakeCtx())
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_env_falls_back_when_the_context_has_no_git_env(self):
        class Bare:
            name = "example"

        env = reporting._git_env(Bare())
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_a_timeout_is_reported_not_raised(self):
        original = reporting.subprocess.run

        def boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=10)

        reporting.subprocess.run = boom
        try:
            code, out = reporting._git(".", ["status"], {})
        finally:
            reporting.subprocess.run = original
        self.assertEqual(code, 124)
        self.assertEqual(out, "")

    def test_a_missing_binary_is_reported_not_raised(self):
        original = reporting.subprocess.run

        def boom(*args, **kwargs):
            raise OSError("no such file")

        reporting.subprocess.run = boom
        try:
            code, out = reporting._git(".", ["status"], {})
        finally:
            reporting.subprocess.run = original
        self.assertEqual(code, 1)
        self.assertIn("no such file", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
