"""Tests for ``gitlab_mcp.confirm`` and ``gitlab_mcp.server``.

Everything here is offline. No GitLab instance is contacted and no git command
runs: ``gitlab_mcp.context``, ``gitlab_mcp.reporting`` and
``gitlab_mcp.actions`` are replaced with fakes before the server module is
imported, and the tools see the fakes because the server imports those three
as *modules* rather than pulling names out of them.

``gitlab_mcp.context`` and ``gitlab_mcp.reporting`` are written by another task
in parallel with this one, so when they are absent the stubs below are injected
into ``sys.modules`` instead. That keeps this file runnable on its own; when
the real modules exist they are still not used, because every test patches the
attributes the server actually touches.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest
from unittest import mock


from tests import _home_guard


def setUpModule() -> None:
    """Point HOME and every gitlab-cli config path at a temporary directory."""
    _home_guard.activate()


def tearDownModule() -> None:
    """Restore HOME; fail if any test reached into the real home."""
    _home_guard.deactivate()

# ── Stand-ins for the modules written in parallel ─────────────────────────────

_REPORTING_FUNCTIONS = (
    "instances", "groups", "projects", "project_overview", "commits",
    "commit_detail", "compare", "contributors", "person_report",
    "project_progress", "merge_requests", "merge_request", "pipelines",
    "branches", "users", "file_content", "local_status",
)

_ACTION_NAMES = (
    "sync_local", "clone_missing", "mr_comment", "mr_review_comment",
    "mr_approve", "mr_unapprove", "mr_create", "mr_merge", "issue_comment",
    "create_branch",
)


def _stub_context() -> types.ModuleType:
    """A ``gitlab_mcp.context`` substitute with the attributes the server uses."""
    mod = types.ModuleType("gitlab_mcp.context")

    class GitLabError(Exception):
        """Stand-in for the real GitLabError."""

    class GitLabContext:
        """Records which instance it was built for; performs no I/O."""

        def __init__(self, instance: str | None = None) -> None:
            self.instance = instance
            self.name = instance or "Hub"
            self.url = "https://gitlab.example.com"
            self.master_dir = "/tmp/gitlab-mcp-test"

    mod.GitLabContext = GitLabContext
    mod.GitLabError = GitLabError
    mod.list_instances_public = lambda: [
        {"name": "Hub", "url": "https://gitlab.example.com", "active": True}]
    mod.redact = lambda text: str(text).replace("glpat-SECRET123456", "***")
    return mod


def _stub_reporting() -> types.ModuleType:
    mod = types.ModuleType("gitlab_mcp.reporting")
    for fn_name in _REPORTING_FUNCTIONS:
        setattr(mod, fn_name, mock.Mock(name=fn_name, return_value={}))
    return mod


def _ensure(name: str, factory) -> None:
    """Import ``name``; inject ``factory()`` when it is not there yet."""
    try:
        __import__(name)
    except Exception:                                        # noqa: BLE001
        sys.modules[name] = factory()


_ensure("gitlab_mcp.context", _stub_context)
_ensure("gitlab_mcp.reporting", _stub_reporting)

from gitlab_mcp import confirm as confirm_mod          # noqa: E402
from gitlab_mcp import server as server_mod            # noqa: E402


# ── Fakes used by the server tests ────────────────────────────────────────────

class FakeContext:
    """Minimal GitLabContext: remembers its instance, does nothing else."""

    def __init__(self, instance: str | None = None) -> None:
        self.instance = instance
        self.name = instance or "Hub"
        self.url = "https://gitlab.example.com"
        self.master_dir = "/tmp/gitlab-mcp-test"


class ServerTestCase(unittest.TestCase):
    """Base class: fresh server state and a fake context for every test."""

    def setUp(self) -> None:
        server_mod.reset_state()
        self.addCleanup(server_mod.reset_state)
        ctx_patch = mock.patch.object(
            server_mod.context, "GitLabContext", FakeContext)
        ctx_patch.start()
        self.addCleanup(ctx_patch.stop)
        env_patch = mock.patch.dict(
            os.environ, {server_mod.READ_ONLY_ENV: "0"}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def patch_action(self, name: str, *, preview=None, do=None):
        """Patch ``preview_<name>`` / ``do_<name>`` on the actions module."""
        mocks = {}
        if preview is not None:
            p = mock.patch.object(server_mod.actions, f"preview_{name}",
                                  mock.Mock(return_value=preview))
            mocks["preview"] = p.start()
            self.addCleanup(p.stop)
        if do is not None:
            d = mock.patch.object(server_mod.actions, f"do_{name}",
                                  mock.Mock(return_value=do))
            mocks["do"] = d.start()
            self.addCleanup(d.stop)
        return mocks


def ok_preview(action: str = "mr_comment", **extra) -> dict:
    """A preview that permits the operation."""
    out = {"action": action, "ok": True, "danger": False,
           "summary": f"would {action}", "reasons": [], "warnings": []}
    out.update(extra)
    return out


# ── ConfirmStore semantics ────────────────────────────────────────────────────

class ConfirmStoreTests(unittest.TestCase):
    """The token store: binding, expiry, single use, cap."""

    def setUp(self) -> None:
        self.now = 1000.0
        self.store = confirm_mod.ConfirmStore(clock=lambda: self.now)

    def test_issue_returns_token_and_ttl(self) -> None:
        issued = self.store.issue("mr_merge", {"project": "group-a/project-a"})
        self.assertEqual(issued["expires_in"], confirm_mod.DEFAULT_TTL_SECONDS)
        self.assertEqual(confirm_mod.DEFAULT_TTL_SECONDS, 900)
        self.assertIsInstance(issued["confirm_token"], str)
        self.assertGreaterEqual(len(issued["confirm_token"]), 16)

    def test_tokens_are_unique(self) -> None:
        tokens = {self.store.issue("mr_approve", {"iid": 1})["confirm_token"]
                  for _ in range(50)}
        self.assertEqual(len(tokens), 50)

    def test_check_accepts_identical_args(self) -> None:
        args = {"project": "group-a/project-a", "iid": 7, "body": "ship it"}
        token = self.store.issue("mr_comment", args)["confirm_token"]
        ok, extra = self.store.check("mr_comment", args, token)
        self.assertTrue(ok)
        self.assertIsNone(extra)

    def test_key_order_does_not_matter(self) -> None:
        token = self.store.issue(
            "mr_comment", {"a": 1, "b": 2})["confirm_token"]
        ok, _ = self.store.check("mr_comment", {"b": 2, "a": 1}, token)
        self.assertTrue(ok)

    def test_embedded_confirm_token_is_ignored_in_the_fingerprint(self) -> None:
        args = {"project": "group-a/project-a", "iid": 7}
        token = self.store.issue("mr_comment", args)["confirm_token"]
        with_token = dict(args, confirm_token=token)
        ok, _ = self.store.check("mr_comment", with_token, token)
        self.assertTrue(ok)

    def test_check_rejects_changed_args(self) -> None:
        token = self.store.issue(
            "mr_comment", {"iid": 7, "body": "ship it"})["confirm_token"]
        ok, extra = self.store.check(
            "mr_comment", {"iid": 7, "body": "rm -rf"}, token)
        self.assertFalse(ok)
        self.assertIsNone(extra)

    def test_check_rejects_a_different_tool(self) -> None:
        args = {"project": "group-a/project-a", "iid": 7}
        token = self.store.issue("mr_approve", args)["confirm_token"]
        self.assertFalse(self.store.check("mr_merge", args, token)[0])

    def test_token_is_single_use(self) -> None:
        args = {"iid": 7}
        token = self.store.issue("mr_approve", args)["confirm_token"]
        self.assertTrue(self.store.check("mr_approve", args, token)[0])
        self.assertFalse(self.store.check("mr_approve", args, token)[0])

    def test_a_failed_check_also_burns_the_token(self) -> None:
        """A token must not survive a wrong-args attempt and be retried."""
        token = self.store.issue("mr_approve", {"iid": 7})["confirm_token"]
        self.assertFalse(self.store.check("mr_approve", {"iid": 8}, token)[0])
        self.assertFalse(self.store.check("mr_approve", {"iid": 7}, token)[0])

    def test_token_expires_after_the_ttl(self) -> None:
        args = {"iid": 7}
        token = self.store.issue("mr_merge", args)["confirm_token"]
        self.now += confirm_mod.DEFAULT_TTL_SECONDS - 1
        self.now += 2
        self.assertFalse(self.store.check("mr_merge", args, token)[0])

    def test_token_still_valid_just_before_expiry(self) -> None:
        args = {"iid": 7}
        token = self.store.issue("mr_merge", args)["confirm_token"]
        self.now += confirm_mod.DEFAULT_TTL_SECONDS - 1
        self.assertTrue(self.store.check("mr_merge", args, token)[0])

    def test_unknown_empty_and_non_string_tokens_are_rejected(self) -> None:
        for bad in (None, "", "not-a-token", 12345, b"bytes"):
            self.assertFalse(self.store.check("mr_merge", {}, bad)[0])

    def test_extra_round_trips(self) -> None:
        token = self.store.issue(
            "mr_merge", {"iid": 7}, {"sha": "deadbeef"})["confirm_token"]
        ok, extra = self.store.check("mr_merge", {"iid": 7}, token)
        self.assertTrue(ok)
        self.assertEqual(extra, {"sha": "deadbeef"})

    def test_pending_is_capped(self) -> None:
        store = confirm_mod.ConfirmStore(max_pending=3,
                                         clock=lambda: self.now)
        tokens = [store.issue("mr_approve", {"iid": i})["confirm_token"]
                  for i in range(5)]
        self.assertEqual(store.pending_count(), 3)
        self.assertFalse(store.check("mr_approve", {"iid": 0}, tokens[0])[0])
        self.assertTrue(store.check("mr_approve", {"iid": 4}, tokens[4])[0])

    def test_default_cap_is_256(self) -> None:
        self.assertEqual(confirm_mod.MAX_PENDING, 256)

    def test_expired_entries_are_dropped(self) -> None:
        self.store.issue("mr_approve", {"iid": 1})
        self.assertEqual(self.store.pending_count(), 1)
        self.now += confirm_mod.DEFAULT_TTL_SECONDS + 1
        self.assertEqual(self.store.pending_count(), 0)

    def test_invalid_construction_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            confirm_mod.ConfirmStore(ttl_seconds=0)
        with self.assertRaises(ValueError):
            confirm_mod.ConfirmStore(max_pending=0)

    def test_fingerprint_is_sha256_hex(self) -> None:
        digest = confirm_mod.fingerprint("mr_merge", {"iid": 1})
        self.assertEqual(len(digest), 64)
        int(digest, 16)

    def test_canonical_args_is_sorted_and_token_free(self) -> None:
        text = confirm_mod.canonical_args(
            {"b": 1, "a": 2, "confirm_token": "x"})
        self.assertEqual(text, '{"a":2,"b":1}')


# ── Two-step write flow ───────────────────────────────────────────────────────

class TwoStepFlowTests(ServerTestCase):
    """First call previews, second call executes, and never both at once."""

    def test_first_call_previews_and_does_not_execute(self) -> None:
        mocks = self.patch_action(
            "mr_comment",
            preview=ok_preview("mr_comment", body="ship it"),
            do={"action": "mr_comment", "ok": True, "note_id": 5})

        res = server_mod._tool_mr_comment(
            "group-a/project-a", 7, "ship it")

        self.assertEqual(res["status"], "needs_confirmation")
        self.assertEqual(res["preview"]["action"], "mr_comment")
        self.assertEqual(res["expires_in"], 900)
        self.assertIn("confirm_token", res)
        self.assertFalse(res["danger"])
        self.assertIn("approval", res["message"].lower())
        mocks["preview"].assert_called_once()
        mocks["do"].assert_not_called()

    def test_second_call_with_the_token_executes(self) -> None:
        mocks = self.patch_action(
            "mr_comment", preview=ok_preview("mr_comment"),
            do={"action": "mr_comment", "ok": True, "note_id": 5})

        first = server_mod._tool_mr_comment("group-a/project-a", 7, "ship it")
        second = server_mod._tool_mr_comment(
            "group-a/project-a", 7, "ship it",
            confirm_token=first["confirm_token"])

        self.assertEqual(second["status"], "done")
        self.assertEqual(second["result"]["note_id"], 5)
        mocks["do"].assert_called_once()
        _, kwargs = mocks["do"].call_args
        self.assertEqual(kwargs["iid"], 7)
        self.assertEqual(kwargs["body"], "ship it")

    def test_a_confirmed_call_does_not_mint_a_new_token(self) -> None:
        """A token must never be issued and consumed by the same call."""
        self.patch_action("mr_comment", preview=ok_preview("mr_comment"),
                          do={"ok": True})
        first = server_mod._tool_mr_comment("group-a/project-a", 7, "hi")
        second = server_mod._tool_mr_comment(
            "group-a/project-a", 7, "hi",
            confirm_token=first["confirm_token"])
        self.assertNotIn("confirm_token", second)
        self.assertEqual(server_mod.confirm_store.pending_count(), 0)

    def test_token_cannot_be_replayed(self) -> None:
        self.patch_action("mr_approve", preview=ok_preview("mr_approve"),
                          do={"ok": True})
        first = server_mod._tool_mr_approve("group-a/project-a", 7)
        token = first["confirm_token"]
        self.assertEqual(
            server_mod._tool_mr_approve(
                "group-a/project-a", 7, confirm_token=token)["status"], "done")
        replay = server_mod._tool_mr_approve(
            "group-a/project-a", 7, confirm_token=token)
        self.assertEqual(replay["status"], "rejected")

    def test_tampered_args_reject_the_token(self) -> None:
        mocks = self.patch_action(
            "mr_comment", preview=ok_preview("mr_comment"), do={"ok": True})
        first = server_mod._tool_mr_comment("group-a/project-a", 7, "ship it")

        tampered = server_mod._tool_mr_comment(
            "group-a/project-a", 7, "delete everything",
            confirm_token=first["confirm_token"])

        self.assertEqual(tampered["status"], "rejected")
        self.assertIn("reason", tampered)
        mocks["do"].assert_not_called()

    def test_token_from_one_tool_is_useless_on_another(self) -> None:
        self.patch_action("mr_approve", preview=ok_preview("mr_approve"),
                          do={"ok": True})
        self.patch_action("mr_merge",
                          preview=ok_preview("mr_merge", sha="a" * 40),
                          do={"ok": True})
        approve = server_mod._tool_mr_approve("group-a/project-a", 7)
        res = server_mod._tool_mr_merge(
            "group-a/project-a", 7, confirm_token=approve["confirm_token"])
        self.assertEqual(res["status"], "rejected")

    def test_instance_is_part_of_the_binding(self) -> None:
        self.patch_action("mr_approve", preview=ok_preview("mr_approve"),
                          do={"ok": True})
        first = server_mod._tool_mr_approve("group-a/project-a", 7,
                                            instance="Hub")
        res = server_mod._tool_mr_approve(
            "group-a/project-a", 7, instance="other",
            confirm_token=first["confirm_token"])
        self.assertEqual(res["status"], "rejected")

    def test_unusable_preview_is_rejected_with_the_preview_attached(self) -> None:
        bad = {"action": "mr_merge", "ok": False, "danger": True,
               "summary": "would merge", "warnings": [],
               "reasons": ["the merge request has conflicts with 'main'"]}
        mocks = self.patch_action("mr_merge", preview=bad, do={"ok": True})

        res = server_mod._tool_mr_merge("group-a/project-a", 7)

        self.assertEqual(res["status"], "rejected")
        self.assertIn("conflicts", res["reason"])
        self.assertEqual(res["preview"], bad)
        self.assertNotIn("confirm_token", res)
        mocks["do"].assert_not_called()

    def test_danger_is_surfaced_at_the_top_level(self) -> None:
        self.patch_action(
            "mr_merge",
            preview=ok_preview("mr_merge", danger=True, sha="b" * 40,
                               warnings=["DANGER: 'main' is protected"]),
            do={"ok": True})
        res = server_mod._tool_mr_merge("group-a/project-a", 7)
        self.assertTrue(res["danger"])

    def test_preview_exception_becomes_an_error_result(self) -> None:
        patch = mock.patch.object(
            server_mod.actions, "preview_create_branch",
            mock.Mock(side_effect=ValueError("branch must not be empty")))
        patch.start()
        self.addCleanup(patch.stop)

        res = server_mod._tool_create_branch("group-a/project-a", "", "main")

        self.assertEqual(res["status"], "error")
        self.assertEqual(res["type"], "ValueError")
        self.assertIn("must not be empty", res["error"])


class MergeShaPinningTests(ServerTestCase):
    """``mr_merge`` merges the commit the human previewed, or nothing."""

    def test_preview_sha_is_passed_to_the_doer(self) -> None:
        head = "c" * 40
        mocks = self.patch_action(
            "mr_merge", preview=ok_preview("mr_merge", sha=head),
            do={"action": "mr_merge", "ok": True, "sha": head})

        first = server_mod._tool_mr_merge("group-a/project-a", 7, squash=True)
        second = server_mod._tool_mr_merge(
            "group-a/project-a", 7, squash=True,
            confirm_token=first["confirm_token"])

        self.assertEqual(second["status"], "done")
        _, kwargs = mocks["do"].call_args
        self.assertEqual(kwargs["sha"], head)
        self.assertEqual(kwargs["iid"], 7)
        self.assertTrue(kwargs["squash"])

    def test_sha_is_not_a_tool_argument(self) -> None:
        """The agent cannot choose which commit gets merged."""
        self.assertNotIn("sha", server_mod._tool_mr_merge.__code__.co_varnames[
            :server_mod._tool_mr_merge.__code__.co_argcount])

    def test_moved_head_merges_the_previewed_sha_only(self) -> None:
        previewed = "d" * 40
        mocks = self.patch_action(
            "mr_merge", preview=ok_preview("mr_merge", sha=previewed),
            do={"ok": True})
        first = server_mod._tool_mr_merge("group-a/project-a", 7)

        # The source branch moves: a later preview would report a new head.
        mocks["preview"].return_value = ok_preview("mr_merge", sha="e" * 40)
        server_mod._tool_mr_merge(
            "group-a/project-a", 7, confirm_token=first["confirm_token"])

        _, kwargs = mocks["do"].call_args
        self.assertEqual(kwargs["sha"], previewed)


class ReadOnlyModeTests(ServerTestCase):
    """``GITLAB_MCP_READ_ONLY=1`` refuses writes without even previewing."""

    def test_write_tool_is_rejected_without_a_preview(self) -> None:
        mocks = self.patch_action("mr_merge", preview=ok_preview("mr_merge"),
                                  do={"ok": True})
        with mock.patch.dict(os.environ,
                             {server_mod.READ_ONLY_ENV: "1"}, clear=False):
            res = server_mod._tool_mr_merge("group-a/project-a", 7)

        self.assertEqual(res["status"], "rejected")
        self.assertIn("read-only", res["reason"])
        self.assertNotIn("preview", res)
        self.assertNotIn("confirm_token", res)
        mocks["preview"].assert_not_called()
        mocks["do"].assert_not_called()

    def test_every_write_tool_honours_read_only(self) -> None:
        with mock.patch.dict(os.environ,
                             {server_mod.READ_ONLY_ENV: "1"}, clear=False):
            calls = {
                "sync_local": lambda: server_mod._tool_sync_local(),
                "clone_missing": lambda: server_mod._tool_clone_missing("g"),
                "mr_comment": lambda: server_mod._tool_mr_comment("p", 1, "b"),
                "mr_review_comment": lambda: server_mod._tool_mr_review_comment(
                    "p", 1, "b", "f.py", new_line=3),
                "mr_approve": lambda: server_mod._tool_mr_approve("p", 1),
                "mr_unapprove": lambda: server_mod._tool_mr_unapprove("p", 1),
                "mr_create": lambda: server_mod._tool_mr_create(
                    "p", "feature", "main", "title"),
                "mr_merge": lambda: server_mod._tool_mr_merge("p", 1),
                "issue_comment": lambda: server_mod._tool_issue_comment(
                    "p", 1, "b"),
                "create_branch": lambda: server_mod._tool_create_branch(
                    "p", "feature", "main"),
            }
            self.assertEqual(sorted(calls), sorted(_ACTION_NAMES))
            for name, call in calls.items():
                with self.subTest(tool=name):
                    self.assertEqual(call()["status"], "rejected")

    def test_a_valid_token_does_not_bypass_read_only(self) -> None:
        self.patch_action("mr_approve", preview=ok_preview("mr_approve"),
                          do={"ok": True})
        first = server_mod._tool_mr_approve("group-a/project-a", 7)
        with mock.patch.dict(os.environ,
                             {server_mod.READ_ONLY_ENV: "1"}, clear=False):
            res = server_mod._tool_mr_approve(
                "group-a/project-a", 7,
                confirm_token=first["confirm_token"])
        self.assertEqual(res["status"], "rejected")

    def test_read_tools_still_work_in_read_only_mode(self) -> None:
        with mock.patch.object(server_mod.reporting, "branches",
                               mock.Mock(return_value=[{"name": "main"}])):
            with mock.patch.dict(os.environ,
                                 {server_mod.READ_ONLY_ENV: "1"}, clear=False):
                res = server_mod._tool_branches("group-a/project-a")
        self.assertEqual(res["total"], 1)


# ── Read tools ────────────────────────────────────────────────────────────────

class ReadToolTests(ServerTestCase):
    """Reads execute straight away, are capped, and need no token."""

    def test_read_tool_calls_reporting_with_the_context(self) -> None:
        fake = mock.Mock(return_value={"default_branch": "main"})
        with mock.patch.object(server_mod.reporting, "project_overview", fake):
            res = server_mod._tool_project_overview("group-a/project-a")
        self.assertEqual(res, {"default_branch": "main"})
        args, kwargs = fake.call_args
        self.assertIsInstance(args[0], FakeContext)
        self.assertEqual(args[1], "group-a/project-a")
        self.assertEqual(kwargs["since"], "30d")

    def test_contexts_are_cached_per_instance(self) -> None:
        fake = mock.Mock(return_value=[])
        with mock.patch.object(server_mod.reporting, "branches", fake):
            server_mod._tool_branches("p")
            server_mod._tool_branches("p")
            server_mod._tool_branches("p", instance="other")
        contexts = [call.args[0] for call in fake.call_args_list]
        self.assertIs(contexts[0], contexts[1])
        self.assertIsNot(contexts[0], contexts[2])
        self.assertEqual(contexts[2].instance, "other")

    def test_list_results_are_capped_with_total_and_truncated(self) -> None:
        rows = [{"name": f"branch-{i}"}
                for i in range(server_mod.MAX_ITEMS + 10)]
        with mock.patch.object(server_mod.reporting, "branches",
                               mock.Mock(return_value=rows)):
            res = server_mod._tool_branches("group-a/project-a")
        self.assertEqual(len(res["items"]), server_mod.MAX_ITEMS)
        self.assertEqual(res["total"], server_mod.MAX_ITEMS + 10)
        self.assertTrue(res["truncated"])

    def test_short_lists_are_not_marked_truncated(self) -> None:
        with mock.patch.object(server_mod.reporting, "branches",
                               mock.Mock(return_value=[{"name": "main"}])):
            res = server_mod._tool_branches("group-a/project-a")
        self.assertFalse(res["truncated"])
        self.assertEqual(res["total"], 1)

    def test_person_report_accepts_a_comma_separated_string(self) -> None:
        fake = mock.Mock(return_value={"people": []})
        with mock.patch.object(server_mod.reporting, "person_report", fake):
            server_mod._tool_person_report("alice, bob", projects="g/a, g/b")
        args, kwargs = fake.call_args
        self.assertEqual(args[1], ["alice", "bob"])
        self.assertEqual(kwargs["projects"], ["g/a", "g/b"])

    def test_person_report_accepts_a_list(self) -> None:
        fake = mock.Mock(return_value={"people": []})
        with mock.patch.object(server_mod.reporting, "person_report", fake):
            server_mod._tool_person_report(["alice", "bob"])
        self.assertEqual(fake.call_args.args[1], ["alice", "bob"])
        self.assertIsNone(fake.call_args.kwargs["projects"])

    def test_instances_tool_returns_no_tokens(self) -> None:
        listing = [{"name": "Hub", "url": "https://gitlab.example.com",
                    "active": True}]
        with mock.patch.object(server_mod.context, "list_instances_public",
                               mock.Mock(return_value=listing)):
            res = server_mod._tool_instances()
        self.assertEqual(res["items"], listing)
        self.assertNotIn("token", str(res).lower())


# ── Error handling and redaction ──────────────────────────────────────────────

class ErrorRedactionTests(ServerTestCase):
    """Failures come back as redacted error dicts, never as tracebacks."""

    def test_read_failure_becomes_an_error_dict(self) -> None:
        with mock.patch.object(
                server_mod.reporting, "commits",
                mock.Mock(side_effect=RuntimeError("GitLab said 500"))):
            res = server_mod._tool_commits("group-a/project-a")
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["type"], "RuntimeError")
        self.assertIn("500", res["error"])

    def test_bad_instance_becomes_an_error_dict(self) -> None:
        with mock.patch.object(
                server_mod.context, "GitLabContext",
                mock.Mock(side_effect=ValueError("unknown instance 'nope'"))):
            res = server_mod._tool_branches("group-a/project-a",
                                            instance="nope")
        self.assertEqual(res["status"], "error")
        self.assertIn("unknown instance", res["error"])

    def test_token_is_redacted_out_of_a_read_error(self) -> None:
        leak = "401 for https://oauth2:glpat-SECRET123456@gitlab.example.com"
        with mock.patch.object(server_mod.context, "redact",
                               lambda t: t.replace("glpat-SECRET123456",
                                                   "***")):
            with mock.patch.object(server_mod.reporting, "commits",
                                   mock.Mock(side_effect=RuntimeError(leak))):
                res = server_mod._tool_commits("group-a/project-a")
        self.assertNotIn("glpat-SECRET123456", res["error"])
        self.assertIn("gitlab.example.com", res["error"])

    def test_token_is_redacted_out_of_a_write_error(self) -> None:
        leak = "403 PRIVATE-TOKEN=glpat-SECRET123456"  # gitleaks:allow - fake test fixture
        with mock.patch.object(server_mod.context, "redact",
                               lambda t: t.replace("glpat-SECRET123456",
                                                   "***")):
            with mock.patch.object(
                    server_mod.actions, "preview_mr_merge",
                    mock.Mock(side_effect=RuntimeError(leak))):
                res = server_mod._tool_mr_merge("group-a/project-a", 7)
        self.assertEqual(res["status"], "error")
        self.assertNotIn("glpat-SECRET123456", res["error"])

    def test_error_result_carries_no_traceback(self) -> None:
        with mock.patch.object(server_mod.reporting, "users",
                               mock.Mock(side_effect=KeyError("boom"))):
            res = server_mod._tool_users("alice")
        self.assertEqual(set(res), {"status", "error", "type"})

    def test_a_non_dict_preview_is_an_error_not_a_token(self) -> None:
        with mock.patch.object(server_mod.actions, "preview_mr_approve",
                               mock.Mock(return_value="not a dict")):
            res = server_mod._tool_mr_approve("group-a/project-a", 7)
        self.assertEqual(res["status"], "error")
        self.assertNotIn("confirm_token", res)


# ── Tool registration ─────────────────────────────────────────────────────────

EXPECTED_READ_TOOLS = tuple(f"gitlab_{n}" for n in _REPORTING_FUNCTIONS)
EXPECTED_WRITE_TOOLS = tuple(f"gitlab_{n}" for n in _ACTION_NAMES)


class ToolRegistrationTests(unittest.TestCase):
    """Every contracted tool is registered, described, and correctly shaped."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tools = asyncio.run(server_mod.mcp.list_tools())
        cls.by_name = {t.name: t for t in cls.tools}

    def test_server_is_named_gitlab(self) -> None:
        self.assertEqual(server_mod.SERVER_NAME, "gitlab")
        self.assertEqual(server_mod.mcp.name, "gitlab")

    def test_every_read_tool_is_registered(self) -> None:
        for name in EXPECTED_READ_TOOLS:
            with self.subTest(tool=name):
                self.assertIn(name, self.by_name)

    def test_every_write_tool_is_registered(self) -> None:
        for name in EXPECTED_WRITE_TOOLS:
            with self.subTest(tool=name):
                self.assertIn(name, self.by_name)

    def test_there_are_no_unexpected_tools(self) -> None:
        self.assertEqual(
            sorted(self.by_name),
            sorted(EXPECTED_READ_TOOLS + EXPECTED_WRITE_TOOLS))

    def test_every_tool_has_a_description(self) -> None:
        for name, tool in self.by_name.items():
            with self.subTest(tool=name):
                self.assertTrue((tool.description or "").strip(),
                                f"{name} has no description")
                self.assertGreater(len(tool.description.strip()), 30)

    def test_every_tool_name_is_prefixed(self) -> None:
        for name in self.by_name:
            self.assertTrue(name.startswith("gitlab_"))

    def test_read_tools_take_an_optional_instance(self) -> None:
        for name in EXPECTED_READ_TOOLS:
            if name == "gitlab_instances":
                continue
            props = self.by_name[name].inputSchema.get("properties", {})
            required = self.by_name[name].inputSchema.get("required", [])
            with self.subTest(tool=name):
                self.assertIn("instance", props)
                self.assertNotIn("instance", required)

    def test_read_tools_take_no_confirm_token(self) -> None:
        for name in EXPECTED_READ_TOOLS:
            props = self.by_name[name].inputSchema.get("properties", {})
            with self.subTest(tool=name):
                self.assertNotIn("confirm_token", props)

    def test_write_tools_take_an_optional_confirm_token(self) -> None:
        for name in EXPECTED_WRITE_TOOLS:
            schema = self.by_name[name].inputSchema
            props = schema.get("properties", {})
            with self.subTest(tool=name):
                self.assertIn("confirm_token", props)
                self.assertNotIn("confirm_token", schema.get("required", []))
                self.assertIn("instance", props)

    def test_write_tool_descriptions_demand_confirmation(self) -> None:
        for name in EXPECTED_WRITE_TOOLS:
            text = self.by_name[name].description.lower()
            with self.subTest(tool=name):
                self.assertIn("confirm", text)

    def test_mr_merge_does_not_expose_a_sha_argument(self) -> None:
        props = self.by_name["gitlab_mr_merge"].inputSchema.get(
            "properties", {})
        self.assertNotIn("sha", props)


# ── Central scrubbing (literal tokens, not only glpat- shapes) ───────────────

#: A token that matches no credential pattern: only literal scrubbing can
#: catch it. It must never appear in anything a tool returns.
LITERAL_TOKEN = "abcd1234secret"  # gitleaks:allow - fake test fixture
#: The configured instance's token (fake home: instance "example").
CONFIGURED_TOKEN = "zzzz9876configured"


class SecretContext(FakeContext):
    """A fake context that, like the real one, knows its literal token."""

    def __init__(self, instance: str | None = None) -> None:
        super().__init__(instance)
        self._token = LITERAL_TOKEN

    def redact(self, text: str) -> str:
        return str(text).replace(self._token, "[REDACTED]")


def _leaky(*_args, **_kwargs) -> dict:
    """A reporting/action stand-in that echoes the token everywhere."""
    return {
        "note": f"echo {LITERAL_TOKEN}",
        "nested": [{"remote": f"https://oauth2:{LITERAL_TOKEN}@"
                              "gitlab.example.com/group-a/project-a.git"}],
        LITERAL_TOKEN: "as a key",
        "count": 3,
    }


class LiteralTokenScrubbingTests(ServerTestCase):
    """The context's literal token never reaches the agent, on any path."""

    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.object(server_mod.context, "GitLabContext",
                                  SecretContext)
        patch.start()
        self.addCleanup(patch.stop)

    def assertClean(self, result) -> None:
        self.assertNotIn(LITERAL_TOKEN, repr(result))

    def test_read_error_with_a_bare_literal_token_is_scrubbed(self) -> None:
        with mock.patch.object(
                server_mod.reporting, "commits",
                mock.Mock(side_effect=RuntimeError(
                    f"401 Unauthorized using {LITERAL_TOKEN}"))):
            res = server_mod._tool_commits("group-a/project-a")
        self.assertEqual(res["status"], "error")
        self.assertIn("401 Unauthorized", res["error"])
        self.assertClean(res)

    def test_exception_with_an_empty_str_falls_back_to_a_scrubbed_repr(
            self) -> None:
        class Opaque(Exception):
            def __str__(self) -> str:
                return ""

            def __repr__(self) -> str:
                return f"Opaque(token={LITERAL_TOKEN!r})"

        with mock.patch.object(server_mod.reporting, "users",
                               mock.Mock(side_effect=Opaque())):
            res = server_mod._tool_users("alice")
        self.assertEqual(res["type"], "Opaque")
        self.assertClean(res)

    def test_read_result_data_is_scrubbed_recursively(self) -> None:
        with mock.patch.object(server_mod.reporting, "project_overview",
                               mock.Mock(side_effect=_leaky)):
            res = server_mod._tool_project_overview("group-a/project-a")
        self.assertClean(res)
        self.assertEqual(res["count"], 3)
        self.assertIn("gitlab.example.com", res["nested"][0]["remote"])

    def test_preview_error_is_scrubbed(self) -> None:
        with mock.patch.object(
                server_mod.actions, "preview_mr_merge",
                mock.Mock(side_effect=ValueError(f"bad {LITERAL_TOKEN}"))):
            res = server_mod._tool_mr_merge("group-a/project-a", 7)
        self.assertEqual(res["status"], "error")
        self.assertClean(res)

    def test_preview_payload_is_scrubbed(self) -> None:
        self.patch_action("mr_comment",
                          preview=ok_preview("mr_comment",
                                             body=f"x {LITERAL_TOKEN}"),
                          do={"ok": True})
        res = server_mod._tool_mr_comment("group-a/project-a", 7, "hi")
        self.assertEqual(res["status"], "needs_confirmation")
        self.assertClean(res)

    def test_rejected_preview_reasons_are_scrubbed(self) -> None:
        bad = ok_preview("mr_merge", ok=False,
                         reasons=[f"token {LITERAL_TOKEN} lacks api scope"])
        self.patch_action("mr_merge", preview=bad, do={"ok": True})
        res = server_mod._tool_mr_merge("group-a/project-a", 7)
        self.assertEqual(res["status"], "rejected")
        self.assertClean(res)

    def test_done_result_is_scrubbed(self) -> None:
        self.patch_action("create_branch",
                          preview=ok_preview("create_branch"),
                          do=_leaky())
        first = server_mod._tool_create_branch("group-a/project-a", "f",
                                               "main")
        res = server_mod._tool_create_branch(
            "group-a/project-a", "f", "main",
            confirm_token=first["confirm_token"])
        self.assertEqual(res["status"], "done")
        self.assertClean(res)

    def test_doer_exception_is_scrubbed(self) -> None:
        self.patch_action("mr_unapprove", preview=ok_preview("mr_unapprove"))
        patch = mock.patch.object(
            server_mod.actions, "do_mr_unapprove",
            mock.Mock(side_effect=RuntimeError(
                f"Authorization: Bearer {LITERAL_TOKEN}")))
        patch.start()
        self.addCleanup(patch.stop)
        first = server_mod._tool_mr_unapprove("group-a/project-a", 7)
        res = server_mod._tool_mr_unapprove(
            "group-a/project-a", 7, confirm_token=first["confirm_token"])
        self.assertEqual(res["status"], "error")
        self.assertClean(res)

    def test_every_tool_scrubs_its_error_path(self) -> None:
        """Sweep: every registered tool, forced to fail with the token."""
        boom = mock.Mock(side_effect=RuntimeError(f"leak {LITERAL_TOKEN}"))
        targets = [(server_mod.reporting, n) for n in _REPORTING_FUNCTIONS]
        targets += [(server_mod.actions, f"preview_{n}")
                    for n in _ACTION_NAMES]
        targets.append((server_mod.context, "list_instances_public"))
        for owner, attr in targets:
            patch = mock.patch.object(owner, attr, boom)
            patch.start()
            self.addCleanup(patch.stop)
        # The token belongs to a context; build it up front so tools that
        # never build one themselves (gitlab_instances) are covered too.
        # Without any context, the configured-instance lookup is what
        # applies (ConfiguredInstanceTokenTests).
        server_mod._ctx(None)
        tools = asyncio.run(server_mod.mcp.list_tools())
        self.assertEqual(len(tools),
                         len(_REPORTING_FUNCTIONS) + len(_ACTION_NAMES))
        for tool in tools:
            props = tool.inputSchema.get("properties", {})
            args = {name: (1 if props[name].get("type") == "integer"
                           else "x")
                    for name in tool.inputSchema.get("required", [])}
            with self.subTest(tool=tool.name):
                fn = server_mod.mcp._tool_manager.get_tool(tool.name).fn
                res = fn(**args)
                self.assertEqual(res["status"], "error")
                self.assertClean(res)


class ConfiguredInstanceTokenTests(ServerTestCase):
    """Tokens of configured instances are scrubbed with no context built."""

    def test_token_from_the_instance_env_var_is_scrubbed(self) -> None:
        with mock.patch.dict(os.environ,
                             {"GITLAB_TOKEN_EXAMPLE": CONFIGURED_TOKEN}):
            out = server_mod.scrub_text(f"failed with {CONFIGURED_TOKEN}")
        self.assertNotIn(CONFIGURED_TOKEN, out)
        self.assertIn("failed with", out)

    def test_token_from_the_gitlab_cli_env_file_is_scrubbed(self) -> None:
        home = _home_guard.fake_home()
        self.assertTrue(home, "home guard is not active")
        env_file = os.path.join(home, ".gitlab-cli", "env")
        with open(env_file, "w", encoding="utf-8") as fh:
            fh.write(f"GITLAB_TOKEN_EXAMPLE={CONFIGURED_TOKEN}\n")
        self.addCleanup(os.remove, env_file)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITLAB_TOKEN_EXAMPLE", None)
            with mock.patch.object(
                    server_mod.reporting, "groups",
                    mock.Mock(side_effect=RuntimeError(
                        f"rejected {CONFIGURED_TOKEN}"))):
                res = server_mod._tool_groups()
        self.assertEqual(res["status"], "error")
        self.assertIn("rejected", res["error"])
        self.assertNotIn(CONFIGURED_TOKEN, repr(res))

    def test_a_short_value_is_not_treated_as_a_secret(self) -> None:
        with mock.patch.dict(os.environ, {"GITLAB_TOKEN_EXAMPLE": "ab"}):
            self.assertEqual(server_mod.scrub_text("ab cd"), "ab cd")


class FragmentScrubbingTests(unittest.TestCase):
    """Credential-shaped fragments are removed whatever the token is."""

    def scrub(self, text: str, error: bool = True) -> str:
        return server_mod.scrub_text(text, error=error)

    def test_oauth2_userinfo_in_a_url(self) -> None:
        out = self.scrub("https://oauth2:qwerty-not-gl@gitlab.example.com/a")
        self.assertNotIn("qwerty-not-gl", out)
        self.assertIn("gitlab.example.com/a", out)

    def test_oauth2_userinfo_without_a_scheme(self) -> None:
        out = self.scrub("remote oauth2:qwerty-not-gl@gitlab.example.com")
        self.assertNotIn("qwerty-not-gl", out)

    def test_private_token_header(self) -> None:
        out = self.scrub("PRIVATE-TOKEN: qwerty-not-gl")
        self.assertNotIn("qwerty-not-gl", out)
        self.assertIn("PRIVATE-TOKEN", out)

    def test_private_token_in_a_headers_dict_repr(self) -> None:
        out = self.scrub(str({"PRIVATE-TOKEN": "qwerty-not-gl"}))
        self.assertNotIn("qwerty-not-gl", out)

    def test_authorization_bearer_header(self) -> None:
        out = self.scrub("Authorization: Bearer qwerty-not-gl")
        self.assertNotIn("qwerty-not-gl", out)
        self.assertIn("Authorization", out)

    def test_bare_bearer_in_error_text(self) -> None:
        self.assertNotIn("qwerty-not-gl",
                         self.scrub("sent bearer qwerty-not-gl upstream"))

    def test_gitlab_token_shape(self) -> None:
        token = "gl" + "pat-" + "ABCDEFGHIJKL"
        self.assertNotIn(token, self.scrub(f"token {token}"))

    def test_query_string_token(self) -> None:
        out = self.scrub("GET /api/v4/x?private_token=qwerty-not-gl&a=1")
        self.assertNotIn("qwerty-not-gl", out)
        self.assertIn("a=1", out)

    def test_ordinary_text_is_left_alone(self) -> None:
        text = "Merged feature into main; 3 files changed."
        self.assertEqual(self.scrub(text, error=False), text)

    def test_none_and_empty_pass_through(self) -> None:
        self.assertEqual(server_mod.scrub_text(None), "")
        self.assertEqual(server_mod.scrub_text(""), "")

    def test_scrub_keeps_non_string_scalars(self) -> None:
        value = {"n": 1, "f": 1.5, "b": True, "z": None, "l": [2, "x"]}
        self.assertEqual(server_mod.scrub(value, ()), value)


class ToolDescriptionLabelTests(unittest.TestCase):
    """Every description says whether the tool writes and what it needs."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.by_name = {t.name: t for t in asyncio.run(
            server_mod.mcp.list_tools())}

    def test_read_tools_are_labelled_read_only(self) -> None:
        for name in EXPECTED_READ_TOOLS:
            text = self.by_name[name].description
            with self.subTest(tool=name):
                self.assertTrue(text.startswith(server_mod.READ_TOOL_LABEL))
                self.assertNotIn("[WRITE tool]", text)

    def test_write_tools_are_labelled_and_require_confirm_token(self) -> None:
        for name in EXPECTED_WRITE_TOOLS:
            text = self.by_name[name].description
            with self.subTest(tool=name):
                self.assertTrue(text.startswith(server_mod.WRITE_TOOL_LABEL))
                self.assertIn("REQUIRES confirm_token", text)

    def test_the_tool_specific_docstring_follows_the_label(self) -> None:
        self.assertIn("most destructive",
                      self.by_name["gitlab_mr_merge"].description)


class QuickActionWiringTests(ServerTestCase):
    """``allow_quick_actions`` reaches the action and is part of the binding."""

    BODY_TOOLS = ("gitlab_mr_comment", "gitlab_mr_review_comment",
                  "gitlab_issue_comment", "gitlab_mr_create")

    def test_body_tools_expose_an_optional_allow_quick_actions(self) -> None:
        by_name = {t.name: t
                   for t in asyncio.run(server_mod.mcp.list_tools())}
        for name in self.BODY_TOOLS:
            schema = by_name[name].inputSchema
            with self.subTest(tool=name):
                self.assertIn("allow_quick_actions", schema["properties"])
                self.assertNotIn("allow_quick_actions",
                                 schema.get("required", []))
                self.assertIn("quick action", by_name[name].description)

    def test_default_is_false_and_is_passed_through(self) -> None:
        mocks = self.patch_action("mr_comment", preview=ok_preview(),
                                  do={"ok": True})
        server_mod._tool_mr_comment("group-a/project-a", 7, "/merge")
        self.assertIs(
            mocks["preview"].call_args.kwargs["allow_quick_actions"], False)

    def test_flipping_it_after_the_preview_invalidates_the_token(self) -> None:
        mocks = self.patch_action("issue_comment",
                                  preview=ok_preview("issue_comment"),
                                  do={"ok": True})
        first = server_mod._tool_issue_comment("group-a/project-a", 7,
                                               "/close")
        res = server_mod._tool_issue_comment(
            "group-a/project-a", 7, "/close", allow_quick_actions=True,
            confirm_token=first["confirm_token"])
        self.assertEqual(res["status"], "rejected")
        mocks["do"].assert_not_called()

    def test_confirmed_call_forwards_allow_quick_actions(self) -> None:
        mocks = self.patch_action("mr_review_comment",
                                  preview=ok_preview("mr_review_comment"),
                                  do={"ok": True})
        first = server_mod._tool_mr_review_comment(
            "group-a/project-a", 7, "/approve", "a.py", new_line=1,
            allow_quick_actions=True)
        server_mod._tool_mr_review_comment(
            "group-a/project-a", 7, "/approve", "a.py", new_line=1,
            allow_quick_actions=True, confirm_token=first["confirm_token"])
        self.assertIs(mocks["do"].call_args.kwargs["allow_quick_actions"],
                      True)


class PreviewPinningTests(ServerTestCase):
    """Approve and mr_create are pinned to what the human previewed."""

    def test_approve_is_pinned_to_the_previewed_sha(self) -> None:
        head = "f" * 40
        mocks = self.patch_action("mr_approve",
                                  preview=ok_preview("mr_approve", sha=head),
                                  do={"ok": True})
        first = server_mod._tool_mr_approve("group-a/project-a", 7)
        mocks["preview"].return_value = ok_preview("mr_approve",
                                                   sha="0" * 40)
        server_mod._tool_mr_approve("group-a/project-a", 7,
                                    confirm_token=first["confirm_token"])
        self.assertEqual(mocks["do"].call_args.kwargs["sha"], head)

    def test_mr_create_is_pinned_to_the_description_hash(self) -> None:
        mocks = self.patch_action(
            "mr_create",
            preview=ok_preview("mr_create", description_sha256="ab" * 32),
            do={"ok": True})
        first = server_mod._tool_mr_create("group-a/project-a", "f", "dev",
                                           "t", description="d")
        server_mod._tool_mr_create("group-a/project-a", "f", "dev", "t",
                                   description="d",
                                   confirm_token=first["confirm_token"])
        self.assertEqual(mocks["do"].call_args.kwargs["description_sha256"],
                         "ab" * 32)


class CliScrubbingTests(unittest.TestCase):
    """cli.py prints through the same scrubber, with its contexts' tokens."""

    def setUp(self) -> None:
        from gitlab_mcp import cli
        self.cli = cli
        patch = mock.patch.object(cli, "_CONTEXTS", [])
        patch.start()
        self.addCleanup(patch.stop)

    def run_cli(self, argv) -> tuple[int, str, str]:
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.cli, "make_context",
                               side_effect=self._make_context), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = self.cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _make_context(self, instance):
        ctx = SecretContext(instance)
        self.cli._CONTEXTS.append(ctx)
        return ctx

    def test_error_line_never_carries_the_literal_token(self) -> None:
        with mock.patch.object(
                server_mod.reporting, "groups", create=True,
                side_effect=RuntimeError(f"401 using {LITERAL_TOKEN}")):
            code, out, err = self.run_cli(["groups"])
        self.assertEqual(code, 1)
        self.assertIn("401 using", err)
        self.assertNotIn(LITERAL_TOKEN, out + err)

    def test_rendered_output_never_carries_the_literal_token(self) -> None:
        with mock.patch.object(server_mod.reporting, "groups", create=True,
                               side_effect=_leaky):
            code, out, err = self.run_cli(["groups", "--json"])
        self.assertEqual(code, 0)
        self.assertNotIn(LITERAL_TOKEN, out + err)

    def test_preview_output_never_carries_the_literal_token(self) -> None:
        leaky = ok_preview("mr_approve", sha="a" * 40,
                           summary=f"approve with {LITERAL_TOKEN}")
        with mock.patch.object(server_mod.actions, "preview_mr_approve",
                               return_value=leaky), \
                mock.patch.object(server_mod.actions, "do_mr_approve") as do, \
                mock.patch.object(self.cli, "_ask", return_value="n"):
            code, out, err = self.run_cli(
                ["mr-approve", "group-a/project-a", "7"])
        self.assertEqual(code, 0)
        do.assert_not_called()
        self.assertNotIn(LITERAL_TOKEN, out + err)

    def test_approve_passes_the_previewed_sha(self) -> None:
        with mock.patch.object(server_mod.actions, "preview_mr_approve",
                               return_value=ok_preview("mr_approve",
                                                       sha="b" * 40)), \
                mock.patch.object(server_mod.actions, "do_mr_approve",
                                  return_value={"ok": True}) as do, \
                mock.patch.object(self.cli, "_ask", return_value="y"):
            code, _, _ = self.run_cli(["mr-approve", "group-a/project-a", "7"])
        self.assertEqual(code, 0)
        self.assertEqual(do.call_args.kwargs["sha"], "b" * 40)

    def test_allow_quick_actions_flag_reaches_the_action(self) -> None:
        with mock.patch.object(server_mod.actions, "preview_mr_comment",
                               return_value=ok_preview()) as pv, \
                mock.patch.object(self.cli, "_ask", return_value="n"):
            self.run_cli(["mr-comment", "group-a/project-a", "7", "/merge",
                          "--allow-quick-actions"])
            self.assertIs(pv.call_args.kwargs["allow_quick_actions"], True)
            self.run_cli(["mr-comment", "group-a/project-a", "7", "/merge"])
            self.assertNotIn("allow_quick_actions", pv.call_args.kwargs)


class HumanConfirmModeTests(ServerTestCase):
    """GITLAB_MCP_CONFIRM_MODE=human keeps the token out of the result."""

    def setUp(self) -> None:
        super().setUp()
        import tempfile
        self.token_dir = tempfile.mkdtemp(prefix="gitlab-mcp-confirm-")
        self.addCleanup(__import__("shutil").rmtree, self.token_dir, True)
        store = confirm_mod.ConfirmStore(mode="human",
                                         token_dir=self.token_dir)
        patch = mock.patch.object(server_mod, "confirm_store", store)
        patch.start()
        self.addCleanup(patch.stop)

    def test_preview_returns_the_token_file_not_the_token(self) -> None:
        self.patch_action("mr_approve",
                          preview=ok_preview("mr_approve", sha="a" * 40),
                          do={"ok": True})
        res = server_mod._tool_mr_approve("group-a/project-a", 7)
        self.assertEqual(res["status"], "needs_confirmation")
        self.assertEqual(res["confirm_mode"], "human")
        self.assertNotIn("confirm_token", res)
        with open(res["token_file"], encoding="ascii") as fh:
            token = fh.read().strip()
        self.assertNotIn(token, repr(res))
        self.assertIn("paste", res["message"])

    def test_the_pasted_token_confirms(self) -> None:
        mocks = self.patch_action(
            "mr_approve", preview=ok_preview("mr_approve", sha="a" * 40),
            do={"ok": True})
        first = server_mod._tool_mr_approve("group-a/project-a", 7)
        with open(first["token_file"], encoding="ascii") as fh:
            token = fh.read().strip()
        res = server_mod._tool_mr_approve("group-a/project-a", 7,
                                          confirm_token=token)
        self.assertEqual(res["status"], "done")
        mocks["do"].assert_called_once()

    def test_agent_mode_reports_its_mode(self) -> None:
        with mock.patch.object(server_mod, "confirm_store",
                               confirm_mod.ConfirmStore(mode="agent")):
            self.patch_action("mr_unapprove",
                              preview=ok_preview("mr_unapprove"),
                              do={"ok": True})
            res = server_mod._tool_mr_unapprove("group-a/project-a", 7)
        self.assertEqual(res["confirm_mode"], "agent")
        self.assertIn("confirm_token", res)

    def test_an_unwritable_token_dir_is_an_error_not_a_token(self) -> None:
        self.patch_action("mr_unapprove", preview=ok_preview("mr_unapprove"))
        with mock.patch.object(server_mod.confirm_store, "issue",
                               side_effect=PermissionError("refusing dir")):
            res = server_mod._tool_mr_unapprove("group-a/project-a", 7)
        self.assertEqual(res["status"], "error")
        self.assertNotIn("confirm_token", res)


class EntryPointTests(unittest.TestCase):
    """``main`` serves over stdio and nothing is printed."""

    def test_main_runs_stdio(self) -> None:
        with mock.patch.object(server_mod.mcp, "run") as run:
            server_mod.main()
        run.assert_called_once_with(transport="stdio")

    def test_dunder_main_calls_server_main(self) -> None:
        import gitlab_mcp.__main__ as entry
        with mock.patch.object(entry.server, "main") as main:
            entry._run()
        main.assert_called_once_with()

    def test_version_is_declared(self) -> None:
        import gitlab_mcp
        import version
        # One version for the whole tree: the MCP package re-exports it.
        self.assertEqual(gitlab_mcp.__version__, version.__version__)
        self.assertRegex(gitlab_mcp.__version__, r"^\d+\.\d+\.\d+$")
        self.assertTrue(gitlab_mcp.__doc__)

    def test_no_module_prints_to_stdout_on_import(self) -> None:
        import io
        import contextlib
        import importlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            importlib.reload(confirm_mod)
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
