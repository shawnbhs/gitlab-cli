"""Tests for gitlab_mcp.actions — preview/do pairs for every mutating action.

Everything here runs offline against a fake GitLabContext. The fake records
every call, which is how the tests prove the two properties that matter most:
a preview never writes, and a merge is always pinned to a sha.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from gitlab_mcp import actions
from gitlab_mcp import confirm

from tests import _home_guard  # noqa: E402  (isolates ~ incl. errors.ERROR_FILE)


def setUpModule():
    _home_guard.activate()


def tearDownModule():
    _home_guard.deactivate()


# ─── Fakes ────────────────────────────────────────────────────────────────────

class FakeContext:
    """A GitLabContext stand-in that records calls instead of making them."""

    def __init__(self, responses: dict | None = None,
                 master_dir: str = "/srv/MasterGroups",
                 folder: str = "MasterGroups"):
        self.name = "example"
        self.url = "https://gitlab.example.com"
        self.master_dir = master_dir
        self._folder = folder
        self._responses = responses or {}
        self.gets: list[tuple[str, dict | None]] = []
        self.posts: list[tuple[str, object]] = []
        self.puts: list[tuple[str, object]] = []
        self.captured: list[tuple] = []

    # -- read ---------------------------------------------------------------
    def get(self, path, params=None):
        self.gets.append((path, params))
        if path not in self._responses:
            raise KeyError(f"no fake response for GET {path}")
        value = self._responses[path]
        if isinstance(value, Exception):
            raise value
        return value

    def paginate(self, path, params=None, limit=None):
        return self.get(path, params)

    # -- write --------------------------------------------------------------
    def post(self, path, json=None):
        self.posts.append((path, json))
        return self._responses.get(f"POST {path}", {"id": 1})

    def put(self, path, json=None):
        self.puts.append((path, json))
        return self._responses.get(f"PUT {path}", {"state": "merged"})

    # -- helpers ------------------------------------------------------------
    def project_id(self, ref):
        return 42

    def user_id(self, ref):
        return 7

    def cfg_public(self):
        return {"name": self.name, "url": self.url,
                "master_dir": self.master_dir, "folder": self._folder}

    def run_captured(self, fn, *args, **kwargs):
        self.captured.append((fn, args, kwargs))
        return fn(*args, **kwargs), "captured output"

    # -- assertions ---------------------------------------------------------
    @property
    def wrote(self):
        return bool(self.posts or self.puts)


def mr(**over):
    """A mergeable merge request payload; override any field."""
    base = {
        "iid": 7,
        "title": "Add a thing",
        "state": "opened",
        "author": {"username": "alice"},
        "source_branch": "feature/thing",
        "target_branch": "develop",
        "sha": "a" * 40,
        "detailed_merge_status": "mergeable",
        "has_conflicts": False,
        "draft": False,
        "blocking_discussions_resolved": True,
        "web_url": "https://gitlab.example.com/group-a/project-a/-/merge_requests/7",
        "diff_refs": {"base_sha": "b" * 40, "start_sha": "s" * 40,
                      "head_sha": "a" * 40},
        "head_pipeline": {"id": 99, "status": "success", "sha": "a" * 40},
    }
    base.update(over)
    return base


MR_PATH = "/projects/42/merge_requests/7"
APPROVALS = {"approvals_required": 1, "approvals_left": 0,
             "approved_by": [{"user": {"username": "bob"}}],
             "user_has_approved": True}


def ctx_for_mr(**over):
    return FakeContext({
        MR_PATH: mr(**over),
        f"{MR_PATH}/approvals": APPROVALS,
    })


# ─── Protected-target detection ───────────────────────────────────────────────

class ProtectedBranchTests(unittest.TestCase):

    def test_production_names_are_protected_case_insensitively(self):
        for name in ("main", "MAIN", "master", "Master", "production",
                     "prod", "PROD"):
            self.assertTrue(actions.is_protected_branch(name), name)

    def test_release_prefix_is_protected(self):
        for name in ("release", "release/1.2", "Release-24.10"):
            self.assertTrue(actions.is_protected_branch(name), name)

    def test_ordinary_branches_are_not_protected(self):
        for name in ("develop", "feature/main-menu", "mainline-fix", "", None):
            self.assertFalse(actions.is_protected_branch(name), name)


# ─── mr_merge: danger, refusal, sha ───────────────────────────────────────────

class MergePreviewDangerTests(unittest.TestCase):

    def test_protected_target_sets_danger_and_warns_loudly(self):
        ctx = ctx_for_mr(target_branch="main")
        out = actions.preview_mr_merge(ctx, "group-a/project-a", 7)
        self.assertTrue(out["danger"])
        self.assertTrue(out["ok"])
        self.assertTrue(any("DANGER" in w and "main" in w
                            for w in out["warnings"]), out["warnings"])

    def test_release_target_is_danger_case_insensitively(self):
        ctx = ctx_for_mr(target_branch="Release/24.10")
        out = actions.preview_mr_merge(ctx, "group-a/project-a", 7)
        self.assertTrue(out["danger"])

    def test_ordinary_target_is_not_danger(self):
        out = actions.preview_mr_merge(ctx_for_mr(), "group-a/project-a", 7)
        self.assertFalse(out["danger"])
        self.assertEqual([], out["warnings"])

    def test_preview_reports_the_state_a_human_needs(self):
        out = actions.preview_mr_merge(ctx_for_mr(), "group-a/project-a", 7)
        for key in ("state", "target_branch", "source_branch", "sha",
                    "merge_status", "has_conflicts", "draft",
                    "blocking_discussions_resolved", "head_pipeline",
                    "approvals"):
            self.assertIn(key, out)
        self.assertEqual("a" * 40, out["sha"])
        self.assertEqual("success", out["head_pipeline"]["status"])
        self.assertEqual(0, out["approvals"]["left"])


class MergeRefusalTests(unittest.TestCase):

    def _refused(self, **over):
        out = actions.preview_mr_merge(ctx_for_mr(**over),
                                       "group-a/project-a", 7)
        self.assertFalse(out["ok"])
        return " | ".join(out["reasons"])

    def test_conflicts_refuse_the_merge(self):
        self.assertIn("conflicts", self._refused(has_conflicts=True))

    def test_conflict_merge_status_refuses_the_merge(self):
        reasons = self._refused(detailed_merge_status="conflict")
        self.assertIn("conflicts", reasons)

    def test_draft_refuses_the_merge(self):
        self.assertIn("draft", self._refused(draft=True))

    def test_the_draft_field_wins_over_the_legacy_wip_flag(self):
        ctx = FakeContext({MR_PATH: {**mr(), "draft": False,
                                     "work_in_progress": True},
                           f"{MR_PATH}/approvals": APPROVALS})
        out = actions.preview_mr_merge(ctx, "group-a/project-a", 7)
        # draft is authoritative when present; work_in_progress only fills a
        # gap on older GitLab versions that do not send `draft`.
        self.assertTrue(out["ok"])

    def test_the_legacy_wip_flag_refuses_when_draft_is_absent(self):
        payload = mr()
        payload.pop("draft")
        payload["work_in_progress"] = True
        ctx = FakeContext({MR_PATH: payload,
                           f"{MR_PATH}/approvals": APPROVALS})
        out = actions.preview_mr_merge(ctx, "group-a/project-a", 7)
        self.assertFalse(out["ok"])
        self.assertIn("draft", " ".join(out["reasons"]))

    def test_not_mergeable_refuses_the_merge(self):
        reasons = self._refused(detailed_merge_status="ci_still_running")
        self.assertIn("mergeable", reasons)

    def test_unresolved_blocking_discussions_refuse_the_merge(self):
        reasons = self._refused(blocking_discussions_resolved=False,
                                detailed_merge_status="discussions_not_resolved")
        self.assertIn("blocking discussions", reasons)

    def test_closed_merge_request_is_refused(self):
        self.assertIn("not opened", self._refused(state="merged"))

    def test_missing_head_sha_is_refused(self):
        self.assertIn("head sha", self._refused(sha=None))

    def test_legacy_merge_status_field_is_accepted(self):
        ctx = FakeContext({
            MR_PATH: mr(detailed_merge_status="",
                        merge_status="can_be_merged"),
            f"{MR_PATH}/approvals": APPROVALS})
        out = actions.preview_mr_merge(ctx, "group-a/project-a", 7)
        self.assertTrue(out["ok"])
        self.assertEqual("merge_status", out["merge_status_field"])


class MergeShaTests(unittest.TestCase):

    def test_do_mr_merge_requires_a_sha(self):
        ctx = ctx_for_mr()
        for bad in (None, "", "   "):
            with self.assertRaises(ValueError) as caught:
                actions.do_mr_merge(ctx, "group-a/project-a", 7, sha=bad)
            self.assertIn("sha is required", str(caught.exception))
        self.assertFalse(ctx.wrote)

    def test_do_mr_merge_puts_the_sha_and_the_options(self):
        ctx = ctx_for_mr()
        out = actions.do_mr_merge(ctx, "group-a/project-a", 7, squash=True,
                                  remove_source_branch=True, sha="a" * 40)
        self.assertEqual([], ctx.posts)
        self.assertEqual(1, len(ctx.puts))
        path, payload = ctx.puts[0]
        self.assertEqual(f"{MR_PATH}/merge", path)
        self.assertEqual("a" * 40, payload["sha"])
        self.assertIs(True, payload["squash"])
        self.assertIs(True, payload["should_remove_source_branch"])
        self.assertTrue(out["ok"])

    def test_squash_is_omitted_when_not_chosen(self):
        ctx = ctx_for_mr()
        actions.do_mr_merge(ctx, "group-a/project-a", 7, sha="a" * 40)
        payload = ctx.puts[0][1]
        self.assertNotIn("squash", payload)
        self.assertIs(False, payload["should_remove_source_branch"])

    def test_preview_warns_when_a_stale_sha_is_offered(self):
        out = actions.preview_mr_merge(ctx_for_mr(), "group-a/project-a", 7,
                                       sha="c" * 40)
        self.assertTrue(any("not the current head" in w
                            for w in out["warnings"]), out["warnings"])


# ─── mr_review_comment: positioned notes ──────────────────────────────────────

class ReviewCommentTests(unittest.TestCase):

    def test_position_payload_uses_the_diff_refs(self):
        ctx = ctx_for_mr()
        out = actions.do_mr_review_comment(
            ctx, "group-a/project-a", 7, body="Needs a guard clause.",
            path="src/app.py", new_line=12)

        self.assertEqual(1, len(ctx.posts))
        path, payload = ctx.posts[0]
        self.assertEqual(f"{MR_PATH}/discussions", path)
        self.assertEqual("Needs a guard clause.", payload["body"])
        self.assertEqual({
            "position_type": "text",
            "base_sha": "b" * 40,
            "start_sha": "s" * 40,
            "head_sha": "a" * 40,
            "new_path": "src/app.py",
            "old_path": "src/app.py",
            "new_line": 12,
        }, payload["position"])
        self.assertTrue(out["ok"])

    def test_old_line_places_the_note_on_the_old_side(self):
        ctx = ctx_for_mr()
        actions.do_mr_review_comment(ctx, "group-a/project-a", 7, body="?",
                                     path="src/app.py", old_line=4)
        position = ctx.posts[0][1]["position"]
        self.assertEqual(4, position["old_line"])
        self.assertNotIn("new_line", position)

    def test_refs_fall_back_to_the_newest_version(self):
        ctx = FakeContext({
            MR_PATH: mr(diff_refs={}),
            f"{MR_PATH}/versions": [{"base_commit_sha": "1" * 40,
                                     "start_commit_sha": "2" * 40,
                                     "head_commit_sha": "3" * 40}],
        })
        out = actions.preview_mr_review_comment(
            ctx, "group-a/project-a", 7, body="x", path="a.py", new_line=1)
        self.assertEqual("merge_request.versions[0]", out["refs_source"])
        self.assertEqual("1" * 40, out["position"]["base_sha"])
        self.assertEqual("3" * 40, out["position"]["head_sha"])
        self.assertFalse(ctx.wrote)

    def test_a_line_is_required(self):
        ctx = ctx_for_mr()
        with self.assertRaises(ValueError) as caught:
            actions.preview_mr_review_comment(
                ctx, "group-a/project-a", 7, body="x", path="a.py")
        self.assertIn("needs a line", str(caught.exception))
        self.assertFalse(ctx.wrote)

    def test_missing_diff_refs_are_refused_rather_than_guessed(self):
        ctx = FakeContext({MR_PATH: mr(diff_refs={}),
                           f"{MR_PATH}/versions": []})
        with self.assertRaises(ValueError) as caught:
            actions.preview_mr_review_comment(
                ctx, "group-a/project-a", 7, body="x", path="a.py", new_line=1)
        self.assertIn("diff refs", str(caught.exception))


# ─── Bodies and refs ──────────────────────────────────────────────────────────

class ValidationTests(unittest.TestCase):

    def test_empty_bodies_are_refused(self):
        ctx = ctx_for_mr()
        for body in ("", "   ", "\n"):
            with self.assertRaises(ValueError):
                actions.preview_mr_comment(ctx, "group-a/project-a", 7, body)
        self.assertFalse(ctx.wrote)

    def test_oversized_bodies_are_refused(self):
        ctx = ctx_for_mr()
        with self.assertRaises(ValueError) as caught:
            actions.do_mr_comment(ctx, "group-a/project-a", 7,
                                  "x" * (actions.MAX_BODY_CHARS + 1))
        self.assertIn(str(actions.MAX_BODY_CHARS), str(caught.exception))
        self.assertFalse(ctx.wrote)

    def test_a_body_at_the_limit_is_accepted(self):
        ctx = ctx_for_mr()
        actions.do_mr_comment(ctx, "group-a/project-a", 7,
                              "x" * actions.MAX_BODY_CHARS)
        self.assertEqual(1, len(ctx.posts))

    def test_option_like_refs_are_refused(self):
        ctx = FakeContext({})
        for bad in ("--upload-pack=touch /tmp/pwn", "-u evil", "a\nb"):
            with self.assertRaises(ValueError):
                actions.preview_create_branch(ctx, "group-a/project-a",
                                              "feature/x", bad)
            with self.assertRaises(ValueError):
                actions.preview_create_branch(ctx, "group-a/project-a", bad,
                                              "main")
        self.assertFalse(ctx.wrote)

    def test_mr_create_refuses_a_self_targeting_merge_request(self):
        ctx = FakeContext({})
        with self.assertRaises(ValueError):
            actions.preview_mr_create(ctx, "group-a/project-a", "main", "main",
                                      "t")


# ─── Comments, approvals, branches ────────────────────────────────────────────

class NoteAndApprovalTests(unittest.TestCase):

    def test_mr_comment_posts_to_notes(self):
        ctx = ctx_for_mr()
        actions.do_mr_comment(ctx, "group-a/project-a", 7, "Looks good.")
        self.assertEqual([(f"{MR_PATH}/notes", {"body": "Looks good."})],
                         ctx.posts)

    def test_issue_comment_posts_to_issue_notes(self):
        ctx = FakeContext({"/projects/42/issues/3": {
            "iid": 3, "title": "Bug", "state": "opened"}})
        actions.do_issue_comment(ctx, "group-a/project-a", 3, "On it.")
        self.assertEqual([("/projects/42/issues/3/notes", {"body": "On it."})],
                         ctx.posts)

    def test_issue_comment_preview_warns_on_a_closed_issue(self):
        ctx = FakeContext({"/projects/42/issues/3": {
            "iid": 3, "title": "Bug", "state": "closed"}})
        out = actions.preview_issue_comment(ctx, "group-a/project-a", 3, "hi")
        self.assertTrue(any("closed" in w for w in out["warnings"]))
        self.assertFalse(ctx.wrote)

    def test_approve_flags_a_protected_target(self):
        ctx = ctx_for_mr(target_branch="production")
        out = actions.preview_mr_approve(ctx, "group-a/project-a", 7)
        self.assertTrue(out["danger"])
        self.assertFalse(ctx.wrote)

    def test_approve_and_unapprove_hit_the_right_endpoints(self):
        ctx = ctx_for_mr()
        actions.do_mr_approve(ctx, "group-a/project-a", 7, sha="a" * 40)
        actions.do_mr_unapprove(ctx, "group-a/project-a", 7)
        self.assertEqual([f"{MR_PATH}/approve", f"{MR_PATH}/unapprove"],
                         [p for p, _ in ctx.posts])

    def test_approvals_unavailable_is_reported_not_fatal(self):
        ctx = FakeContext({MR_PATH: mr()})          # no /approvals response
        out = actions.preview_mr_merge(ctx, "group-a/project-a", 7)
        self.assertFalse(out["approvals"]["available"])
        self.assertTrue(out["ok"])

    def test_create_branch_danger_on_a_protected_name(self):
        ctx = FakeContext({
            "/projects/42/repository/branches/production": {},
            "/projects/42/repository/branches/develop": {"name": "develop"},
        })
        out = actions.preview_create_branch(ctx, "group-a/project-a",
                                            "production", "develop")
        self.assertTrue(out["danger"])
        self.assertTrue(out["ok"])
        self.assertFalse(ctx.wrote)

    def test_create_branch_refuses_an_existing_branch(self):
        ctx = FakeContext({
            "/projects/42/repository/branches/develop": {"name": "develop"},
        })
        out = actions.preview_create_branch(ctx, "group-a/project-a",
                                            "develop", "develop")
        self.assertFalse(out["ok"])
        self.assertIn("already exists", " ".join(out["reasons"]))

    def test_do_create_branch_posts_branch_and_ref(self):
        ctx = FakeContext({})
        actions.do_create_branch(ctx, "group-a/project-a", "feature/x", "main")
        self.assertEqual([("/projects/42/repository/branches",
                           {"branch": "feature/x", "ref": "main"})], ctx.posts)

    def test_mr_create_prefixes_a_draft_title_and_flags_the_target(self):
        ctx = FakeContext({
            "/projects/42/repository/branches/feature%2Fx": {"name": "x"},
            "/projects/42/repository/branches/main": {"name": "main"},
            "/projects/42/merge_requests": [],
        })
        out = actions.preview_mr_create(ctx, "group-a/project-a", "feature/x",
                                        "main", "Add a thing", draft=True)
        self.assertTrue(out["danger"])
        self.assertEqual("Draft: Add a thing", out["title"])
        self.assertFalse(ctx.wrote)

        actions.do_mr_create(ctx, "group-a/project-a", "feature/x", "main",
                             "Add a thing", draft=True)
        self.assertEqual("Draft: Add a thing", ctx.posts[0][1]["title"])

    def test_mr_create_refuses_a_missing_source_branch(self):
        ctx = FakeContext({
            "/projects/42/repository/branches/feature%2Fx": {},
            "/projects/42/repository/branches/develop": {"name": "develop"},
            "/projects/42/merge_requests": [],
        })
        out = actions.preview_mr_create(ctx, "group-a/project-a", "feature/x",
                                        "develop", "t")
        self.assertFalse(out["ok"])
        self.assertIn("source branch", " ".join(out["reasons"]))


# ─── Local sync and clone ─────────────────────────────────────────────────────

class SyncLocalTests(unittest.TestCase):

    def setUp(self):
        self.repos = [os.path.join("/srv/MasterGroups/group-a", n)
                      for n in ("project-a", "project-b")]
        self.resolved = "/srv/MasterGroups/group-a"
        self.resolve_calls: list[tuple] = []

        def fake_resolve(group_path, dest, folder=None):
            self.resolve_calls.append((group_path, dest, folder))
            return self.resolved

        self.patches = [
            mock.patch.object(actions.gl_paths, "resolve", fake_resolve),
            mock.patch.object(actions.gl_pull, "_find_git_repos",
                              lambda base: list(self.repos)),
            mock.patch.object(os.path, "isdir", lambda p: True),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def test_preview_resolves_under_master_dir_never_the_cwd(self):
        ctx = FakeContext(master_dir="/srv/MasterGroups")
        out = actions.preview_sync_local(ctx, group="group-a")

        self.assertEqual(1, len(self.resolve_calls))
        group_path, dest, folder = self.resolve_calls[0]
        self.assertEqual("group-a", group_path)
        self.assertTrue(os.path.isabs(dest), dest)
        self.assertEqual(actions.gl_paths.anchor("/srv/MasterGroups"), dest)
        self.assertEqual("MasterGroups", folder)
        self.assertEqual(self.resolved, out["base"])

    def test_a_relative_master_dir_is_anchored_not_left_cwd_relative(self):
        ctx = FakeContext(master_dir="MasterGroups")
        actions.preview_sync_local(ctx, group="group-a")
        dest = self.resolve_calls[0][1]
        self.assertTrue(os.path.isabs(dest), dest)
        self.assertEqual(actions.gl_paths.anchor("MasterGroups"), dest)

    def test_preview_counts_the_repos_and_states_fast_forward_only(self):
        out = actions.preview_sync_local(FakeContext(), group="group-a")
        self.assertTrue(out["ok"])
        self.assertEqual(2, out["repo_count"])
        self.assertEqual(2, out["repos"]["total"])
        self.assertEqual(self.repos, out["repos"]["sample"])
        self.assertTrue(out["fast_forward_only"])
        self.assertIn("fast-forward", out["policy"].lower())

    def test_preview_is_side_effect_free(self):
        ctx = FakeContext()
        actions.preview_sync_local(ctx, group="group-a")
        self.assertFalse(ctx.wrote)
        self.assertEqual([], ctx.captured)

    def test_project_filter_narrows_to_one_repo(self):
        out = actions.preview_sync_local(FakeContext(), group="group-a",
                                         project="group-a/project-b")
        self.assertEqual(1, out["repo_count"])
        self.assertTrue(out["repos"]["sample"][0].endswith("project-b"))

    def test_do_sync_uses_run_captured_with_pull_sync_many(self):
        ctx = FakeContext()
        stats = {"pulled": 1, "dirty": 1}
        attention = [{"path": self.repos[1], "status": "dirty",
                      "branch": "develop", "detail": "uncommitted changes"}]
        with mock.patch.object(actions.gl_pull, "sync_many",
                               return_value=(stats, attention)) as sync:
            out = actions.do_sync_local(ctx, group="group-a")
            # Asserted inside the patch: outside it, gl_pull.sync_many is the
            # real function again and the identity check is vacuous.
            self.assertIs(sync, ctx.captured[0][0])

        self.assertEqual(1, len(ctx.captured))
        fn_args = ctx.captured[0][1]
        self.assertEqual(self.repos, fn_args[0])
        self.assertEqual(self.resolved, fn_args[1])
        self.assertFalse(ctx.captured[0][2]["show_all"])
        sync.assert_called_once()

        self.assertTrue(out["ok"])
        self.assertEqual(stats, out["counts"])
        self.assertEqual(1, out["needs_attention"])
        self.assertEqual("project-b", out["outcomes"][0]["path"])
        self.assertEqual("dirty", out["outcomes"][0]["status"])
        self.assertEqual("captured output", out["output"])
        self.assertFalse(ctx.wrote)


class SyncLocalEmptyTests(unittest.TestCase):

    def test_missing_directory_is_refused_in_the_preview(self):
        ctx = FakeContext()
        with mock.patch.object(actions.gl_paths, "resolve",
                               return_value="/srv/MasterGroups/nope"), \
             mock.patch.object(os.path, "isdir", lambda p: False):
            out = actions.preview_sync_local(ctx, group="nope")
        self.assertFalse(out["ok"])
        self.assertIn("does not exist", " ".join(out["reasons"]))

    def test_do_sync_without_repos_never_calls_run_captured(self):
        ctx = FakeContext()
        with mock.patch.object(actions.gl_paths, "resolve",
                               return_value="/srv/MasterGroups/nope"), \
             mock.patch.object(os.path, "isdir", lambda p: False):
            out = actions.do_sync_local(ctx, group="nope")
        self.assertFalse(out["ok"])
        self.assertEqual([], ctx.captured)


class CloneMissingTests(unittest.TestCase):

    def test_preview_targets_the_managed_dir_and_writes_nothing(self):
        ctx = FakeContext({
            "/groups/group-a/projects": [
                {"path_with_namespace": "group-a/project-a"},
                {"path_with_namespace": "group-a/project-b"},
            ],
        })
        calls = []

        def fake_target(group_path, dest, folder=None):
            calls.append((group_path, dest, folder))
            return "/srv/MasterGroups/group-a"

        with mock.patch.object(actions.gl_paths, "target_for_clone",
                               fake_target), \
             mock.patch.object(os.path, "isdir", lambda p: True), \
             mock.patch.object(os.path, "exists", lambda p: True), \
             mock.patch.object(actions.gl_clone, "repo_state",
                               return_value="ok"), \
             mock.patch.object(
                 actions.gl_pull, "_find_git_repos",
                 return_value=["/srv/MasterGroups/group-a/project-a"]):
            out = actions.preview_clone_missing(ctx, "group-a")

        self.assertEqual(actions.gl_paths.anchor("/srv/MasterGroups"),
                         calls[0][1])
        self.assertEqual("MasterGroups", calls[0][2])
        self.assertEqual(1, out["existing_count"])
        self.assertEqual(["group-a/project-b"], out["would_clone"]["sample"])
        self.assertFalse(ctx.wrote)

    def test_do_clone_runs_cmd_clone_through_run_captured(self):
        ctx = FakeContext()
        with mock.patch.object(actions.gl_clone, "cmd_clone",
                               return_value=None) as cmd, \
             mock.patch.object(actions.gl_paths, "resolve",
                               return_value="/srv/MasterGroups/group-a"), \
             mock.patch.object(os.path, "isdir", lambda p: True), \
             mock.patch.object(actions.gl_pull, "_find_git_repos",
                               return_value=[]):
            out = actions.do_clone_missing(ctx, "group-a")
            # Identity is checked inside the patch; outside it the attribute
            # is the real cmd_clone again.
            self.assertIs(cmd, ctx.captured[0][0])

        self.assertEqual(1, len(ctx.captured))
        fn_args = ctx.captured[0][1]
        self.assertEqual("group-a", fn_args[1])
        self.assertEqual(actions.gl_paths.anchor("/srv/MasterGroups"),
                         fn_args[2])
        self.assertTrue(out["ok"])
        cmd.assert_called_once()

    def test_an_empty_group_is_refused(self):
        for bad in ("", "   "):
            with self.assertRaises(ValueError):
                actions.preview_clone_missing(FakeContext(), bad)


# ─── mr_approve is pinned to the previewed head ───────────────────────────────

class ApprovePinTests(unittest.TestCase):

    def test_preview_reports_the_head_sha_to_pin(self):
        ctx = ctx_for_mr(sha="c" * 40)
        out = actions.preview_mr_approve(ctx, "group-a/project-a", 7)
        self.assertEqual("c" * 40, out["sha"])
        self.assertTrue(out["ok"])

    def test_preview_refuses_when_the_head_sha_is_missing(self):
        ctx = ctx_for_mr(sha=None)
        out = actions.preview_mr_approve(ctx, "group-a/project-a", 7)
        self.assertFalse(out["ok"])
        self.assertIn("no head sha", " ".join(out["reasons"]))

    def test_do_approve_sends_the_sha_to_gitlab(self):
        ctx = ctx_for_mr()
        res = actions.do_mr_approve(ctx, "group-a/project-a", 7, sha="c" * 40)
        self.assertEqual([(f"{MR_PATH}/approve", {"sha": "c" * 40})],
                         ctx.posts)
        self.assertEqual("c" * 40, res["sha"])

    def test_do_approve_refuses_without_a_sha(self):
        for bad in (None, "", "   "):
            with self.subTest(sha=bad):
                ctx = ctx_for_mr()
                with self.assertRaises(ValueError):
                    actions.do_mr_approve(ctx, "group-a/project-a", 7, sha=bad)
                self.assertFalse(ctx.wrote)

    def test_unapprove_preview_carries_no_sha_pin(self):
        out = actions.preview_mr_unapprove(ctx_for_mr(), "group-a/project-a", 7)
        self.assertNotIn("sha", out)


# ─── GitLab quick actions in bodies ───────────────────────────────────────────

INJECTED = "LGTM\n/merge\n  /approve\n/assign @bob\nnot /close here\n"
ISSUE_PATH = "/projects/42/issues/3"


def issue_ctx():
    return FakeContext({ISSUE_PATH: {"iid": 3, "title": "Bug",
                                     "state": "opened"}})


class QuickActionDetectionTests(unittest.TestCase):

    def test_finds_commands_at_line_start_in_order_without_duplicates(self):
        self.assertEqual(["/merge", "/approve", "/assign"],
                         actions.find_quick_actions(INJECTED + "/merge\n"))

    def test_ignores_slashes_that_are_not_line_starting_commands(self):
        for text in ("see /merge in the docs", "path a/b", "/ merge",
                     "//comment", "/1abc", "https://x/merge", ""):
            with self.subTest(text=text):
                self.assertEqual([], actions.find_quick_actions(text))

    def test_detects_every_named_command_and_crlf_lines(self):
        text = "\r\n".join(["x", "/close", "/label ~bug", "/unassign",
                            "/rebase", "/submit_review", "/Merge"])
        self.assertEqual(["/close", "/label", "/unassign", "/rebase",
                          "/submit_review", "/merge"],
                         actions.find_quick_actions(text))

    def test_neutralised_text_contains_no_quick_action(self):
        out = actions.neutralize_quick_actions(INJECTED)
        self.assertEqual([], actions.find_quick_actions(out))
        self.assertIn("`/merge`", out)
        self.assertIn("  `/approve`", out)
        self.assertIn("`/assign @bob`", out)
        self.assertIn("not /close here", out)
        self.assertTrue(out.startswith("LGTM\n"))

    def test_neutralisation_survives_inner_backticks(self):
        out = actions.neutralize_quick_actions("/label `x`\r\n")
        self.assertEqual("`` /label `x` ``\r\n", out)
        self.assertEqual([], actions.find_quick_actions(out))

    def test_plain_text_is_unchanged(self):
        text = "Looks good.\nShip it.\n"
        self.assertEqual(text, actions.neutralize_quick_actions(text))


class QuickActionGateTests(unittest.TestCase):

    def test_mr_comment_preview_flags_and_neutralises_by_default(self):
        ctx = ctx_for_mr()
        out = actions.preview_mr_comment(ctx, "group-a/project-a", 7, INJECTED)
        self.assertTrue(out["danger"])
        self.assertEqual(["/merge", "/approve", "/assign"],
                         out["quick_actions"])
        self.assertFalse(out["quick_actions_allowed"])
        self.assertIn("/merge, /approve, /assign", " ".join(out["warnings"]))
        self.assertEqual([], actions.find_quick_actions(out["body"]))
        self.assertFalse(ctx.wrote)

    def test_mr_comment_do_posts_the_neutralised_body(self):
        ctx = ctx_for_mr()
        preview = actions.preview_mr_comment(ctx, "group-a/project-a", 7,
                                             INJECTED)
        actions.do_mr_comment(ctx, "group-a/project-a", 7, INJECTED)
        sent = ctx.posts[0][1]["body"]
        self.assertEqual(preview["body"], sent)
        self.assertEqual([], actions.find_quick_actions(sent))

    def test_allow_quick_actions_true_sends_verbatim_and_still_warns(self):
        ctx = ctx_for_mr()
        out = actions.preview_mr_comment(ctx, "group-a/project-a", 7,
                                         INJECTED, allow_quick_actions=True)
        self.assertTrue(out["danger"])
        self.assertTrue(out["quick_actions_allowed"])
        self.assertIn("WILL EXECUTE", " ".join(out["warnings"]))
        actions.do_mr_comment(ctx, "group-a/project-a", 7, INJECTED,
                              allow_quick_actions=True)
        self.assertEqual(INJECTED, ctx.posts[0][1]["body"])

    def test_a_truthy_non_bool_does_not_allow_quick_actions(self):
        ctx = ctx_for_mr()
        actions.do_mr_comment(ctx, "group-a/project-a", 7, INJECTED,
                              allow_quick_actions="yes")
        self.assertEqual([], actions.find_quick_actions(ctx.posts[0][1]["body"]))

    def test_plain_comment_is_not_dangerous(self):
        out = actions.preview_mr_comment(ctx_for_mr(), "group-a/project-a", 7,
                                         "Looks good.")
        self.assertFalse(out["danger"])
        self.assertEqual([], out["quick_actions"])
        self.assertEqual([], out["warnings"])

    def test_issue_comment_is_gated_too(self):
        ctx = issue_ctx()
        out = actions.preview_issue_comment(ctx, "group-a/project-a", 3,
                                            "/close\n")
        self.assertTrue(out["danger"])
        self.assertEqual(["/close"], out["quick_actions"])
        actions.do_issue_comment(ctx, "group-a/project-a", 3, "/close\n")
        self.assertEqual("`/close`\n", ctx.posts[0][1]["body"])

    def test_issue_comment_closed_warning_is_kept_alongside(self):
        ctx = FakeContext({ISSUE_PATH: {"iid": 3, "title": "Bug",
                                        "state": "closed"}})
        out = actions.preview_issue_comment(ctx, "group-a/project-a", 3,
                                            "/reopen")
        joined = " ".join(out["warnings"])
        self.assertIn("closed", joined)
        self.assertIn("/reopen", joined)

    def test_review_comment_is_gated_too(self):
        ctx = ctx_for_mr()
        out = actions.preview_mr_review_comment(
            ctx, "group-a/project-a", 7, "nit\n/approve", "a.py", 3)
        self.assertTrue(out["danger"])
        self.assertEqual(["/approve"], out["quick_actions"])
        actions.do_mr_review_comment(ctx, "group-a/project-a", 7,
                                     "nit\n/approve", "a.py", 3)
        self.assertEqual("nit\n`/approve`", ctx.posts[0][1]["body"])


# ─── mr_create shows the description that will be sent ───────────────────────

def create_ctx():
    return FakeContext({
        "/projects/42/repository/branches/feature%2Fx": {"name": "x"},
        "/projects/42/repository/branches/develop": {"name": "develop"},
        "/projects/42/merge_requests": [],
    })


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class MrCreateDescriptionTests(unittest.TestCase):

    def test_preview_shows_the_full_description_and_its_hash(self):
        desc = "Fixes the thing.\n\nSee https://gitlab.example.com/x"
        out = actions.preview_mr_create(create_ctx(), "group-a/project-a",
                                        "feature/x", "develop", "T", desc)
        self.assertEqual(desc, out["description"])
        self.assertFalse(out["description_truncated_for_display"])
        self.assertEqual(sha256(desc), out["description_sha256"])
        self.assertFalse(out["danger"])

    def test_description_at_the_display_limit_is_not_cut(self):
        desc = "x" * actions.MAX_DESCRIPTION_DISPLAY_CHARS
        out = actions.preview_mr_create(create_ctx(), "group-a/project-a",
                                        "feature/x", "develop", "T", desc)
        self.assertEqual(desc, out["description"])
        self.assertFalse(out["description_truncated_for_display"])

    def test_long_description_is_cut_for_display_only_with_a_marker(self):
        desc = "x" * (actions.MAX_DESCRIPTION_DISPLAY_CHARS + 1) + "TAIL"
        ctx = create_ctx()
        out = actions.preview_mr_create(ctx, "group-a/project-a",
                                        "feature/x", "develop", "T", desc)
        self.assertTrue(out["description_truncated_for_display"])
        self.assertIn("TRUNCATED FOR DISPLAY ONLY: 5 more characters",
                      out["description"])
        self.assertNotIn("TAIL", out["description"])
        self.assertEqual(len(desc), out["description_chars"])
        self.assertEqual(sha256(desc), out["description_sha256"])
        actions.do_mr_create(ctx, "group-a/project-a", "feature/x",
                             "develop", "T", desc)
        self.assertEqual(desc, ctx.posts[0][1]["description"])

    def test_quick_actions_in_description_are_flagged_and_neutralised(self):
        ctx = create_ctx()
        desc = "Body\n/merge\n"
        out = actions.preview_mr_create(ctx, "group-a/project-a",
                                        "feature/x", "develop", "T", desc)
        self.assertTrue(out["danger"])
        self.assertEqual(["/merge"], out["quick_actions"])
        self.assertEqual("Body\n`/merge`\n", out["description"])
        self.assertEqual(sha256("Body\n`/merge`\n"), out["description_sha256"])
        actions.do_mr_create(ctx, "group-a/project-a", "feature/x",
                             "develop", "T", desc)
        self.assertEqual("Body\n`/merge`\n", ctx.posts[0][1]["description"])

    def test_quick_actions_allowed_in_description_are_sent_verbatim(self):
        ctx = create_ctx()
        actions.do_mr_create(ctx, "group-a/project-a", "feature/x",
                             "develop", "T", "/draft", allow_quick_actions=True)
        self.assertEqual("/draft", ctx.posts[0][1]["description"])

    def test_do_create_rejects_a_description_that_does_not_match_the_pin(self):
        ctx = create_ctx()
        with self.assertRaises(ValueError):
            actions.do_mr_create(ctx, "group-a/project-a", "feature/x",
                                 "develop", "T", "changed",
                                 description_sha256=sha256("original"))
        self.assertFalse(ctx.wrote)

    def test_do_create_accepts_the_matching_pin(self):
        ctx = create_ctx()
        actions.do_mr_create(ctx, "group-a/project-a", "feature/x",
                             "develop", "T", "same",
                             description_sha256=sha256("same"))
        self.assertEqual("same", ctx.posts[0][1]["description"])

    def test_preview_without_description_still_works(self):
        out = actions.preview_mr_create(create_ctx(), "group-a/project-a",
                                        "feature/x", "develop", "T")
        self.assertEqual("", out["description"])
        self.assertEqual(sha256(""), out["description_sha256"])


# ─── Confirmation mode: agent vs human ────────────────────────────────────────

class ConfirmModeTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gitlab-mcp-confirm-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = os.path.join(self.tmp, "confirm")
        self.now = 1000.0

    def store(self, mode):
        return confirm.ConfirmStore(clock=lambda: self.now, mode=mode,
                                    token_dir=self.dir)

    def read(self, path):
        with open(path, encoding="ascii") as fh:
            return fh.read().strip()

    def test_mode_parsing_defaults_to_agent_and_fails_closed(self):
        for raw, want in ((None, "agent"), ("", "agent"), ("agent", "agent"),
                          (" AGENT ", "agent"), ("human", "human"),
                          ("Human", "human"), ("humn", "human"),
                          ("0", "human")):
            with self.subTest(raw=raw):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop(confirm.CONFIRM_MODE_ENV, None)
                    if raw is None:
                        self.assertEqual(want, confirm.confirm_mode())
                    else:
                        self.assertEqual(want, confirm.confirm_mode(raw))

    def test_env_var_selects_the_mode_at_issue_time(self):
        store = confirm.ConfirmStore(token_dir=self.dir)
        with mock.patch.dict(os.environ, {confirm.CONFIRM_MODE_ENV: "human"}):
            self.assertIsNone(store.issue("mr_approve", {"iid": 1})
                              ["confirm_token"])
        with mock.patch.dict(os.environ, {confirm.CONFIRM_MODE_ENV: "agent"}):
            self.assertTrue(store.issue("mr_approve", {"iid": 1})
                            ["confirm_token"])

    def test_agent_mode_returns_the_token_and_writes_no_file(self):
        out = self.store("agent").issue("mr_approve", {"iid": 1})
        self.assertTrue(out["confirm_token"])
        self.assertEqual("agent", out["confirm_mode"])
        self.assertNotIn("token_file", out)
        self.assertFalse(os.path.exists(self.dir))

    def test_human_mode_hides_the_token_and_writes_a_private_file(self):
        store = self.store("human")
        out = store.issue("mr_approve", {"iid": 1})
        self.assertIsNone(out["confirm_token"])
        self.assertEqual("human", out["confirm_mode"])
        path = out["token_file"]
        self.assertEqual(self.dir, os.path.dirname(path))
        self.assertIn(path, out["message"])
        self.assertIn("paste", out["message"])
        self.assertEqual(0o600, stat.S_IMODE(os.stat(path).st_mode))
        self.assertEqual(0o700, stat.S_IMODE(os.stat(self.dir).st_mode))
        self.assertNotIn(self.read(path), repr(out))

    def test_human_token_is_bound_and_single_use_and_file_is_removed(self):
        store = self.store("human")
        path = store.issue("mr_approve", {"iid": 7})["token_file"]
        token = self.read(path)
        self.assertFalse(store.check("mr_approve", {"iid": 8}, token)[0])
        self.assertFalse(os.path.exists(path))       # burned by the attempt
        path = store.issue("mr_approve", {"iid": 7})["token_file"]
        token = self.read(path)
        self.assertTrue(store.check("mr_approve", {"iid": 7}, token)[0])
        self.assertFalse(store.check("mr_approve", {"iid": 7}, token)[0])
        self.assertFalse(os.path.exists(path))

    def test_human_token_expires_like_an_agent_token(self):
        store = self.store("human")
        out = store.issue("mr_approve", {"iid": 7})
        token = self.read(out["token_file"])
        self.assertEqual(confirm.DEFAULT_TTL_SECONDS, out["expires_in"])
        self.now += confirm.DEFAULT_TTL_SECONDS
        self.assertFalse(store.check("mr_approve", {"iid": 7}, token)[0])
        self.assertFalse(os.path.exists(out["token_file"]))

    def test_evicted_and_cleared_tokens_lose_their_files(self):
        store = confirm.ConfirmStore(clock=lambda: self.now, mode="human",
                                     token_dir=self.dir, max_pending=1)
        first = store.issue("mr_approve", {"iid": 1})["token_file"]
        second = store.issue("mr_approve", {"iid": 2})["token_file"]
        self.assertFalse(os.path.exists(first))
        self.assertTrue(os.path.exists(second))
        store.clear()
        self.assertFalse(os.path.exists(second))

    def test_a_symlinked_token_dir_is_refused(self):
        target = os.path.join(self.tmp, "elsewhere")
        os.mkdir(target)
        os.symlink(target, self.dir)
        with self.assertRaises(PermissionError):
            self.store("human").issue("mr_approve", {"iid": 1})
        self.assertEqual([], os.listdir(target))

    def test_default_token_dir_is_under_the_user_cache(self):
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.tmp}):
            self.assertEqual(os.path.join(self.tmp, "gitlab-mcp", "confirm"),
                             confirm.default_token_dir())
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": "relative"}):
            self.assertEqual(
                os.path.join(os.path.expanduser("~"), ".cache", "gitlab-mcp",
                             "confirm"),
                confirm.default_token_dir())


# ─── The invariant that holds for every preview ───────────────────────────────

class PreviewsAreReadOnlyTests(unittest.TestCase):

    def test_no_preview_issues_a_post_or_a_put(self):
        cases = [
            (actions.preview_mr_comment,
             ctx_for_mr(), ("group-a/project-a", 7, "hello")),
            (actions.preview_mr_review_comment,
             ctx_for_mr(), ("group-a/project-a", 7, "hi", "a.py", 3)),
            (actions.preview_mr_approve,
             ctx_for_mr(), ("group-a/project-a", 7)),
            (actions.preview_mr_unapprove,
             ctx_for_mr(), ("group-a/project-a", 7)),
            (actions.preview_mr_merge,
             ctx_for_mr(), ("group-a/project-a", 7)),
            (actions.preview_issue_comment,
             FakeContext({"/projects/42/issues/3": {
                 "iid": 3, "title": "Bug", "state": "opened"}}),
             ("group-a/project-a", 3, "hi")),
            (actions.preview_create_branch,
             FakeContext({}), ("group-a/project-a", "feature/x", "main")),
            (actions.preview_mr_create,
             FakeContext({"/projects/42/merge_requests": []}),
             ("group-a/project-a", "feature/x", "develop", "Title")),
        ]
        for fn, ctx, args in cases:
            with self.subTest(fn.__name__):
                fn(ctx, *args)
                self.assertEqual([], ctx.posts)
                self.assertEqual([], ctx.puts)


if __name__ == "__main__":        # pragma: no cover
    unittest.main()
