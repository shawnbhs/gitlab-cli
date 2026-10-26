"""Tests for ``gitlab_mcp.cli`` — the manual bash CLI.

``gitlab_mcp.context`` and ``gitlab_mcp.reporting`` are written in parallel
with this task (CONTRACT section 7), so this module installs stub modules under
those names *before* importing the CLI when the real ones are absent. The CLI
imports them lazily, so a stub injected into ``sys.modules`` is what it sees.
``gitlab_mcp.actions`` is finished and is imported for real, then patched per
test with ``unittest.mock.patch.object``.

No network, no filesystem outside ``tempfile``, no real GitLab.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock


from tests import _home_guard


def setUpModule() -> None:
    """Point HOME and every gitlab-cli config path at a temporary directory."""
    _home_guard.activate()


def tearDownModule() -> None:
    """Restore HOME; fail if any test reached into the real home."""
    _home_guard.deactivate()


# ─── Stubs for the modules written in parallel ────────────────────────────────

class FakeContext:
    """Stand-in for ``GitLabContext``: records which instance it was made for."""

    instances_created: list[str | None] = []

    def __init__(self, instance: str | None = None) -> None:
        self.instance = instance
        self.name = instance or "Hub"
        self.url = "https://gitlab.example.com"
        self.master_dir = "/tmp/does-not-exist"
        FakeContext.instances_created.append(instance)


def _install_stub(name: str, build) -> None:
    """Put a stub at ``gitlab_mcp.<name>`` only if the real module is absent."""
    full = f"gitlab_mcp.{name}"
    if full in sys.modules:
        return
    try:
        __import__(full)
        return
    except Exception:                                # noqa: BLE001, S110
        pass
    module = types.ModuleType(full)
    build(module)
    sys.modules[full] = module


def _build_context(module: types.ModuleType) -> None:
    module.GitLabContext = FakeContext

    class GitLabError(Exception):
        """Stub mirroring ``context.GitLabError``."""

        def __init__(self, message: str = "", status: int | None = None):
            super().__init__(message)
            self.status = status
            self.message = message

    module.GitLabError = GitLabError
    module.list_instance_names = lambda: []
    module.redact = lambda text: str(text)


#: Every reporting function the CLI routes to, with its (args, kwargs) recorder.
REPORTING_FUNCTIONS = (
    "instances", "groups", "projects", "project_overview", "commits",
    "commit_detail", "compare", "contributors", "person_report",
    "project_progress", "merge_requests", "merge_request", "pipelines",
    "branches", "users", "file_content", "local_status",
)


def _build_reporting(module: types.ModuleType) -> None:
    module.calls = []

    def make(name: str):
        def fn(*args, **kwargs):
            module.calls.append((name, args, kwargs))
            return {"fn": name}
        fn.__name__ = name
        return fn

    for name in REPORTING_FUNCTIONS:
        setattr(module, name, make(name))


_install_stub("context", _build_context)
_install_stub("reporting", _build_reporting)

from gitlab_mcp import actions, cli                          # noqa: E402


def _reporting_module():
    import gitlab_mcp.reporting as reporting
    return reporting


def run_cli(argv, answers=None, prompts=None):
    """Run ``cli.main(argv)`` with stdio captured and prompts scripted.

    Returns ``(exit_code, stdout, stderr)``. ``answers`` is the queue
    ``_ask`` pops from; an empty queue means the user pressed Enter.
    ``prompts``, when given a list, collects the prompt strings the user was
    actually shown — the only way to assert on a question, since the prompt
    goes to ``input()`` rather than to stdout.
    """
    queue = list(answers or [])
    out, err = io.StringIO(), io.StringIO()

    def fake_ask(prompt: str) -> str:
        if prompts is not None:
            prompts.append(prompt)
        return queue.pop(0) if queue else ""

    with mock.patch.object(cli, "_ask", fake_ask):
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def preview(action: str, **extra) -> dict:
    """A preview dict shaped like the ones ``actions`` produces."""
    base = {"action": action, "ok": True, "danger": False,
            "summary": f"would {action}", "reasons": [], "warnings": []}
    base.update(extra)
    return base


# ─── Parsing ──────────────────────────────────────────────────────────────────

class TestParsing(unittest.TestCase):
    """Every advertised subcommand parses, with the flags it documents."""

    def setUp(self) -> None:
        self.parser = cli.build_parser()

    def parse(self, argv):
        return self.parser.parse_args(argv)

    def test_no_command_prints_help_and_exits_zero(self) -> None:
        code, out, _ = run_cli([])
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)

    def test_every_read_command_is_a_subcommand(self) -> None:
        for name in cli.READ_HANDLERS:
            with self.subTest(command=name):
                self.assertIn(name, self.parser._subparsers._group_actions[0].choices)

    def test_every_write_command_is_a_subcommand(self) -> None:
        for name in cli.WRITE_SPECS:
            with self.subTest(command=name):
                self.assertIn(name, self.parser._subparsers._group_actions[0].choices)

    def test_write_specs_name_real_action_pairs(self) -> None:
        for command, (base, _) in cli.WRITE_SPECS.items():
            with self.subTest(command=command):
                self.assertTrue(hasattr(actions, f"preview_{base}"))
                self.assertTrue(hasattr(actions, f"do_{base}"))

    def test_instances(self) -> None:
        self.assertEqual(self.parse(["instances"]).command, "instances")

    def test_groups_flags(self) -> None:
        args = self.parse(["groups", "--search", "team", "--limit", "5"])
        self.assertEqual((args.search, args.limit), ("team", 5))

    def test_projects_flags(self) -> None:
        args = self.parse(["projects", "--group", "group-a", "--limit", "9"])
        self.assertEqual((args.group, args.limit), ("group-a", 9))

    def test_overview_positional(self) -> None:
        args = self.parse(["overview", "group-a/project-a", "--since", "30d"])
        self.assertEqual(args.project, "group-a/project-a")
        self.assertEqual(args.since, "30d")

    def test_commits_flags(self) -> None:
        args = self.parse(["commits", "group-a/project-a", "--author", "alice",
                           "--branch", "main", "--no-stats", "--limit", "3"])
        self.assertEqual(args.author, "alice")
        self.assertEqual(args.branch, "main")
        self.assertTrue(args.no_stats)
        self.assertEqual(args.limit, 3)

    def test_commit_positional(self) -> None:
        args = self.parse(["commit", "group-a/project-a", "deadbeef"])
        self.assertEqual(args.sha, "deadbeef")

    def test_compare_positionals(self) -> None:
        args = self.parse(["compare", "group-a/project-a", "main", "feature"])
        self.assertEqual((args.base, args.head), ("main", "feature"))

    def test_contributors(self) -> None:
        args = self.parse(["contributors", "group-a/project-a", "--branch", "dev"])
        self.assertEqual(args.branch, "dev")

    def test_person_positional_and_users_flag(self) -> None:
        self.assertEqual(self.parse(["person", "alice", "bob"]).people,
                         ["alice", "bob"])
        self.assertEqual(self.parse(["person", "--users", "alice,bob"]).users,
                         "alice,bob")

    def test_progress(self) -> None:
        self.assertEqual(self.parse(["progress", "group-a/project-a"]).project,
                         "group-a/project-a")

    def test_mrs_flags(self) -> None:
        args = self.parse(["mrs", "--group", "group-a", "--state", "merged",
                           "--author", "alice", "--target-branch", "main"])
        self.assertEqual((args.group, args.state, args.author),
                         ("group-a", "merged", "alice"))
        self.assertEqual(args.target_branch, "main")

    def test_mr_flags(self) -> None:
        args = self.parse(["mr", "group-a/project-a", "7", "--no-diff"])
        self.assertEqual(args.iid, 7)
        self.assertTrue(args.no_diff)

    def test_pipelines_flags(self) -> None:
        args = self.parse(["pipelines", "group-a/project-a", "--ref", "main"])
        self.assertEqual(args.ref, "main")

    def test_branches_flags(self) -> None:
        args = self.parse(["branches", "group-a/project-a", "--search", "rel"])
        self.assertEqual(args.search, "rel")

    def test_users_positional(self) -> None:
        self.assertEqual(self.parse(["users", "alice"]).search, "alice")

    def test_file_positionals(self) -> None:
        args = self.parse(["file", "group-a/project-a", "src/app.py",
                           "--ref", "main"])
        self.assertEqual((args.path, args.ref), ("src/app.py", "main"))

    def test_local_status_flags(self) -> None:
        self.assertEqual(self.parse(["local-status", "--group", "g"]).group, "g")

    def test_sync_flags(self) -> None:
        args = self.parse(["sync", "--group", "group-a", "--project", "p"])
        self.assertEqual((args.group, args.project), ("group-a", "p"))

    def test_clone_missing_positional(self) -> None:
        self.assertEqual(self.parse(["clone-missing", "group-a"]).group,
                         "group-a")

    def test_mr_comment_positionals(self) -> None:
        args = self.parse(["mr-comment", "group-a/project-a", "7", "hello"])
        self.assertEqual((args.iid, args.body), (7, "hello"))

    def test_mr_review_comment_requires_path(self) -> None:
        args = self.parse(["mr-review-comment", "group-a/project-a", "7", "x",
                           "--path", "src/app.py", "--new-line", "12"])
        self.assertEqual((args.path, args.new_line), ("src/app.py", 12))
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                self.parse(["mr-review-comment", "group-a/project-a", "7", "x"])

    def test_mr_approve_and_unapprove(self) -> None:
        self.assertEqual(self.parse(["mr-approve", "p", "1"]).iid, 1)
        self.assertEqual(self.parse(["mr-unapprove", "p", "2"]).iid, 2)

    def test_mr_create_positionals(self) -> None:
        args = self.parse(["mr-create", "group-a/project-a", "feature", "main",
                           "Add the thing", "--draft"])
        self.assertEqual(args.source_branch, "feature")
        self.assertEqual(args.target_branch, "main")
        self.assertEqual(args.title, "Add the thing")
        self.assertTrue(args.draft)

    def test_mr_merge_flags(self) -> None:
        args = self.parse(["mr-merge", "group-a/project-a", "7", "--squash",
                           "--remove-source-branch"])
        self.assertTrue(args.squash)
        self.assertTrue(args.remove_source_branch)

    def test_issue_comment_positionals(self) -> None:
        args = self.parse(["issue-comment", "group-a/project-a", "3", "ack"])
        self.assertEqual((args.iid, args.body), (3, "ack"))

    def test_create_branch_positionals(self) -> None:
        args = self.parse(["create-branch", "group-a/project-a", "feature",
                           "main"])
        self.assertEqual((args.branch, args.ref), ("feature", "main"))

    def test_common_flags_accepted_before_the_command(self) -> None:
        args = self.parse(["--instance", "example", "--json", "--yes",
                           "groups"])
        self.assertEqual(args.instance, "example")
        self.assertTrue(args.json)
        self.assertTrue(args.yes)

    def test_common_flags_accepted_after_the_command(self) -> None:
        args = self.parse(["groups", "--instance", "example2", "--format", "md",
                           "--out", "x.md"])
        self.assertEqual(args.instance, "example2")
        self.assertEqual(args.format, "md")
        self.assertEqual(args.out, "x.md")

    def test_pre_command_flag_survives_the_subparser(self) -> None:
        # The subparser re-declares --json; its default must not wipe the
        # value the main parser already stored.
        self.assertTrue(self.parse(["--json", "groups"]).json)

    def test_unknown_command_exits_two(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            with redirect_stderr(io.StringIO()):
                self.parse(["nope"])
        self.assertEqual(caught.exception.code, 2)


class TestSignatureConformance(unittest.TestCase):
    """The CLI's calls must bind against the real reporting/actions signatures.

    This is the test that catches a sibling module growing or renaming a
    parameter: the stub accepts anything, the real function does not. It is
    skipped when ``reporting`` is still a stub, so it never reports a pass it
    did not earn.
    """

    def setUp(self) -> None:
        self.reporting = _reporting_module()
        if getattr(self.reporting, "calls", None) is not None:
            self.skipTest("gitlab_mcp.reporting is still a stub")

    def test_read_handlers_bind_against_reporting(self) -> None:
        import inspect

        parser = cli.build_parser()
        sample = {
            "instances": ["instances"],
            "groups": ["groups"],
            "projects": ["projects"],
            "overview": ["overview", "group-a/project-a"],
            "commits": ["commits", "group-a/project-a"],
            "commit": ["commit", "group-a/project-a", "abc123"],
            "compare": ["compare", "group-a/project-a", "main", "dev"],
            "contributors": ["contributors", "group-a/project-a"],
            "person": ["person", "alice"],
            "progress": ["progress", "group-a/project-a"],
            "mrs": ["mrs"],
            "mr": ["mr", "group-a/project-a", "7"],
            "pipelines": ["pipelines", "group-a/project-a"],
            "branches": ["branches", "group-a/project-a"],
            "users": ["users", "alice"],
            "file": ["file", "group-a/project-a", "README.md"],
            "local-status": ["local-status"],
        }
        self.assertEqual(set(sample), set(cli.READ_HANDLERS))

        for command, argv in sample.items():
            with self.subTest(command=command):
                bound: list[tuple] = []

                def spy(*args, **kwargs):
                    bound.append((args, kwargs))  # noqa: B023 - called in this iteration
                    return []

                # Find which reporting function this command reaches by
                # patching them all, then bind the recorded call against the
                # REAL signature.
                patchers = [mock.patch.object(self.reporting, name, spy)
                            for name in REPORTING_FUNCTIONS
                            if hasattr(self.reporting, name)]
                for p in patchers:
                    p.start()
                try:
                    args = parser.parse_args(argv)
                    with mock.patch.object(cli, "make_context",
                                           side_effect=FakeContext):
                        cli.READ_HANDLERS[command](args)
                finally:
                    for p in patchers:
                        p.stop()

                self.assertEqual(len(bound), 1, msg=command)
                call_args, call_kwargs = bound[0]
                fn = getattr(self.reporting,
                             {"overview": "project_overview",
                              "commit": "commit_detail",
                              "person": "person_report",
                              "progress": "project_progress",
                              "mrs": "merge_requests",
                              "mr": "merge_request",
                              "file": "file_content",
                              "local-status": "local_status"}.get(
                                  command, command.replace("-", "_")))
                inspect.signature(fn).bind(*call_args, **call_kwargs)

    def test_write_kwargs_bind_against_actions(self) -> None:
        import inspect

        parser = cli.build_parser()
        sample = {
            "sync": ["sync"],
            "clone-missing": ["clone-missing", "group-a"],
            "mr-comment": ["mr-comment", "group-a/project-a", "7", "hi"],
            "mr-review-comment": ["mr-review-comment", "group-a/project-a",
                                  "7", "hi", "--path", "a.py",
                                  "--new-line", "3"],
            "mr-approve": ["mr-approve", "group-a/project-a", "7"],
            "mr-unapprove": ["mr-unapprove", "group-a/project-a", "7"],
            "mr-create": ["mr-create", "group-a/project-a", "feature",
                          "develop", "Title"],
            "mr-merge": ["mr-merge", "group-a/project-a", "7"],
            "issue-comment": ["issue-comment", "group-a/project-a", "3", "ok"],
            "create-branch": ["create-branch", "group-a/project-a", "f", "main"],
        }
        self.assertEqual(set(sample), set(cli.WRITE_SPECS))

        for command, argv in sample.items():
            with self.subTest(command=command):
                base, build = cli.WRITE_SPECS[command]
                kwargs = build(parser.parse_args(argv))
                ctx = FakeContext(None)
                inspect.signature(
                    getattr(actions, f"preview_{base}")).bind(ctx, **kwargs)
                if command == "mr-merge":
                    kwargs["sha"] = "abc123"
                inspect.signature(
                    getattr(actions, f"do_{base}")).bind(ctx, **kwargs)


# ─── Read routing ─────────────────────────────────────────────────────────────

class TestReadRouting(unittest.TestCase):
    """Each read command calls exactly its reporting function, with a context."""

    def setUp(self) -> None:
        self.reporting = _reporting_module()
        self.recorded: list[tuple] = []
        self.patchers = []
        for name in REPORTING_FUNCTIONS:
            patcher = mock.patch.object(
                self.reporting, name,
                side_effect=self._recorder(name), create=True)
            patcher.start()
            self.patchers.append(patcher)
        self.addCleanup(lambda: [p.stop() for p in self.patchers])
        self.ctx_patcher = mock.patch.object(
            cli, "make_context", side_effect=FakeContext)
        self.ctx_patcher.start()
        self.addCleanup(self.ctx_patcher.stop)

    def _recorder(self, name: str):
        def fn(*args, **kwargs):
            self.recorded.append((name, args, kwargs))
            return [{"name": name, "value": 1}]
        return fn

    def assert_routed(self, argv, fn_name):
        code, out, err = run_cli(argv)
        self.assertEqual(code, 0, msg=f"stderr: {err}")
        self.assertEqual([c[0] for c in self.recorded], [fn_name])
        return self.recorded[0]

    def test_instances_takes_no_context(self) -> None:
        name, args, kwargs = self.assert_routed(["instances"], "instances")
        self.assertEqual(args, ())
        self.assertEqual(kwargs, {})

    def test_groups(self) -> None:
        _, args, kwargs = self.assert_routed(
            ["groups", "--search", "x", "--limit", "7"], "groups")
        self.assertIsInstance(args[0], FakeContext)
        self.assertEqual(kwargs, {"search": "x", "limit": 7})

    def test_projects(self) -> None:
        _, _, kwargs = self.assert_routed(
            ["projects", "--group", "group-a"], "projects")
        self.assertEqual(kwargs["group"], "group-a")

    def test_overview_default_since(self) -> None:
        _, args, kwargs = self.assert_routed(
            ["overview", "group-a/project-a"], "project_overview")
        self.assertEqual(args[1], "group-a/project-a")
        self.assertEqual(kwargs["since"], "30d")

    def test_commits_passes_with_stats_and_since(self) -> None:
        _, _, kwargs = self.assert_routed(
            ["commits", "group-a/project-a", "--since", "3d", "--no-stats"],
            "commits")
        self.assertEqual(kwargs["since"], "3d")
        self.assertFalse(kwargs["with_stats"])

    def test_commit(self) -> None:
        _, args, _ = self.assert_routed(
            ["commit", "group-a/project-a", "abc123"], "commit_detail")
        self.assertEqual(args[2], "abc123")

    def test_compare(self) -> None:
        _, args, _ = self.assert_routed(
            ["compare", "group-a/project-a", "main", "dev"], "compare")
        self.assertEqual(args[2:], ("main", "dev"))

    def test_contributors(self) -> None:
        _, _, kwargs = self.assert_routed(
            ["contributors", "group-a/project-a"], "contributors")
        self.assertEqual(kwargs["since"], "30d")

    def test_person_positional_names(self) -> None:
        _, args, _ = self.assert_routed(["person", "alice", "bob"],
                                        "person_report")
        self.assertEqual(args[1], ["alice", "bob"])

    def test_person_users_flag_and_dedup(self) -> None:
        _, args, _ = self.assert_routed(
            ["person", "alice", "--users", "bob, alice"], "person_report")
        self.assertEqual(args[1], ["alice", "bob"])

    def test_person_without_a_name_is_an_error(self) -> None:
        code, _, err = run_cli(["person"])
        self.assertEqual(code, 1)
        self.assertIn("error:", err)
        self.assertEqual(self.recorded, [])

    def test_progress(self) -> None:
        self.assert_routed(["progress", "group-a/project-a"],
                           "project_progress")

    def test_mrs(self) -> None:
        _, _, kwargs = self.assert_routed(
            ["mrs", "--group", "group-a", "--state", "merged"],
            "merge_requests")
        self.assertEqual(kwargs["state"], "merged")
        self.assertEqual(kwargs["group"], "group-a")

    def test_mr(self) -> None:
        _, args, kwargs = self.assert_routed(
            ["mr", "group-a/project-a", "7"], "merge_request")
        self.assertEqual(args[2], 7)
        self.assertTrue(kwargs["with_diff"])

    def test_pipelines(self) -> None:
        _, _, kwargs = self.assert_routed(
            ["pipelines", "group-a/project-a", "--limit", "4"], "pipelines")
        self.assertEqual(kwargs["limit"], 4)

    def test_branches(self) -> None:
        self.assert_routed(["branches", "group-a/project-a"], "branches")

    def test_users(self) -> None:
        _, args, _ = self.assert_routed(["users", "alice"], "users")
        self.assertEqual(args[1], "alice")

    def test_file(self) -> None:
        _, args, kwargs = self.assert_routed(
            ["file", "group-a/project-a", "README.md", "--ref", "main"], "file_content")
        self.assertEqual(args[2], "README.md")
        self.assertEqual(kwargs["ref"], "main")

    def test_local_status(self) -> None:
        _, _, kwargs = self.assert_routed(["local-status"], "local_status")
        self.assertIsNone(kwargs["group"])

    def test_instance_flag_reaches_the_context(self) -> None:
        self.ctx_patcher.stop()
        created = []

        def spy(instance):
            created.append(instance)
            return FakeContext(instance)

        with mock.patch.object(cli, "make_context", side_effect=spy):
            run_cli(["groups", "--instance", "example2"])
        self.assertEqual(created, ["example2"])
        self.ctx_patcher.start()


# ─── Output formats ───────────────────────────────────────────────────────────

class TestOutput(unittest.TestCase):

    def setUp(self) -> None:
        self.reporting = _reporting_module()
        self.rows = [{"name": "group-a", "id": 1},
                     {"name": "group-b", "id": 2}]
        patcher = mock.patch.object(self.reporting, "groups",
                                    return_value=self.rows, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        ctx = mock.patch.object(cli, "make_context", side_effect=FakeContext)
        ctx.start()
        self.addCleanup(ctx.stop)

    def test_json_flag_emits_parseable_json(self) -> None:
        code, out, _ = run_cli(["groups", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), self.rows)

    def test_format_json_matches_the_json_flag(self) -> None:
        _, out, _ = run_cli(["groups", "--format", "json"])
        self.assertEqual(json.loads(out), self.rows)

    def test_table_is_the_default(self) -> None:
        _, out, _ = run_cli(["groups"])
        self.assertIn("group-a", out)
        self.assertNotIn("{", out)

    def test_markdown_has_a_header_row(self) -> None:
        _, out, _ = run_cli(["groups", "--format", "md"])
        self.assertIn("| name | id |", out)
        self.assertIn("| --- | --- |", out)

    def test_markdown_escapes_pipes(self) -> None:
        text = cli.render_markdown(["title"], [{"title": "fix a|b"}])
        self.assertIn(r"fix a\|b", text)

    def test_table_width_is_display_aware(self) -> None:
        text = cli.render_table(["title", "n"],
                                [{"title": "日本語", "n": 1},
                                 {"title": "abcdef", "n": 2}])
        lines = [ln for ln in text.splitlines() if ln.strip()]
        widths = {cli.display_width(ln) for ln in lines}
        self.assertEqual(len(widths), 1, msg=text)

    def test_display_width_counts_wide_characters_twice(self) -> None:
        self.assertEqual(cli.display_width("日本"), 4)
        self.assertEqual(cli.display_width("ab"), 2)

    def test_out_file_receives_the_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "groups.json")
            code, out, _ = run_cli(["groups", "--json", "--out", target])
            self.assertEqual(code, 0)
            self.assertIn("wrote", out)
            with open(target, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), self.rows)

    def test_out_into_a_missing_directory_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "nope", "groups.json")
            code, _, err = run_cli(["groups", "--out", target])
        self.assertEqual(code, 1)
        self.assertIn("error:", err)


# ─── Write flow ───────────────────────────────────────────────────────────────

class TestWriteFlow(unittest.TestCase):
    """preview → print → ask → do, and every way that must not reach ``do``."""

    def setUp(self) -> None:
        ctx = mock.patch.object(cli, "make_context", side_effect=FakeContext)
        ctx.start()
        self.addCleanup(ctx.stop)

    def patch_pair(self, base: str, prev: dict,
                   result: dict | None = None):
        """Patch ``preview_<base>``/``do_<base>`` and return both mocks."""
        p = mock.patch.object(actions, f"preview_{base}", return_value=prev)
        d = mock.patch.object(actions, f"do_{base}",
                              return_value=result or {"action": base,
                                                      "ok": True})
        preview_mock = p.start()
        do_mock = d.start()
        self.addCleanup(p.stop)
        self.addCleanup(d.stop)
        return preview_mock, do_mock

    def test_preview_is_printed_before_anything_runs(self) -> None:
        _, do_mock = self.patch_pair(
            "mr_comment", preview("mr_comment", summary="Post a comment."))
        code, out, _ = run_cli(
            ["mr-comment", "group-a/project-a", "7", "hi"], answers=["n"])
        self.assertEqual(code, 0)
        self.assertIn("preview: mr_comment", out)
        self.assertIn("Post a comment.", out)
        do_mock.assert_not_called()

    def test_decline_does_not_call_do(self) -> None:
        _, do_mock = self.patch_pair("mr_approve", preview("mr_approve"))
        code, out, _ = run_cli(["mr-approve", "group-a/project-a", "7"],
                               answers=["n"])
        self.assertEqual(code, 0)
        self.assertIn("cancelled", out)
        do_mock.assert_not_called()

    def test_empty_answer_is_a_no(self) -> None:
        _, do_mock = self.patch_pair("mr_approve", preview("mr_approve"))
        code, out, _ = run_cli(["mr-approve", "group-a/project-a", "7"],
                               answers=[""])
        self.assertEqual(code, 0)
        self.assertIn("cancelled", out)
        do_mock.assert_not_called()

    def test_accept_calls_do_exactly_once(self) -> None:
        preview_mock, do_mock = self.patch_pair(
            "mr_comment", preview("mr_comment"),
            {"action": "mr_comment", "ok": True, "note_id": 42})
        code, out, err = run_cli(
            ["mr-comment", "group-a/project-a", "7", "hi"], answers=["y"])
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(do_mock.call_count, 1)
        self.assertEqual(preview_mock.call_count, 1)
        _, kwargs = do_mock.call_args
        self.assertEqual(kwargs["project"], "group-a/project-a")
        self.assertEqual(kwargs["iid"], 7)
        self.assertEqual(kwargs["body"], "hi")
        self.assertIn("42", out)

    def test_yes_flag_skips_the_question(self) -> None:
        _, do_mock = self.patch_pair("mr_approve", preview("mr_approve"))

        def explode(prompt):
            raise AssertionError("--yes must not ask")

        with mock.patch.object(cli, "_ask", explode):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.main(["mr-approve", "group-a/project-a", "7", "--yes"])
        self.assertEqual(code, 0)
        self.assertEqual(do_mock.call_count, 1)

    def test_not_ok_preview_exits_one_without_asking(self) -> None:
        _, do_mock = self.patch_pair(
            "mr_merge",
            preview("mr_merge", ok=False,
                    reasons=["the merge request has conflicts with 'main'"],
                    target_branch="main", danger=True, sha="abc123"))

        def explode(prompt):
            raise AssertionError("an ok=false preview must never ask")

        with mock.patch.object(cli, "_ask", explode):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["mr-merge", "group-a/project-a", "7"])
        self.assertEqual(code, 1)
        self.assertIn("conflicts", err.getvalue())
        do_mock.assert_not_called()

    def test_not_ok_preview_ignores_yes(self) -> None:
        _, do_mock = self.patch_pair(
            "create_branch",
            preview("create_branch", ok=False,
                    reasons=["branch 'feature' already exists"]))
        code, _, err = run_cli(
            ["create-branch", "group-a/project-a", "feature", "main", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        do_mock.assert_not_called()

    def test_danger_preview_prints_a_loud_uppercase_line(self) -> None:
        self.patch_pair("mr_approve",
                        preview("mr_approve", danger=True,
                                target_branch="main",
                                warnings=["DANGER: 'main' is protected"]))
        _, out, _ = run_cli(["mr-approve", "group-a/project-a", "7"],
                            answers=["n"])
        loud = [ln for ln in out.splitlines() if "DANGER" in ln
                and "PROTECTED/PRODUCTION" in ln]
        self.assertEqual(len(loud), 1, msg=out)
        # The sentence shouts; the branch name keeps its real spelling.
        without_target = loud[0].replace("main", "")
        self.assertEqual(without_target, without_target.upper())
        self.assertIn("main", loud[0])

    def test_mr_merge_passes_the_sha_from_the_preview(self) -> None:
        _, do_mock = self.patch_pair(
            "mr_merge", preview("mr_merge", sha="feedface1234",
                                target_branch="feature-collect"))
        code, _, err = run_cli(["mr-merge", "group-a/project-a", "7"],
                               answers=["y"])
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(do_mock.call_args.kwargs["sha"], "feedface1234")

    def test_danger_merge_asks_for_the_branch_name_even_with_yes(self) -> None:
        _, do_mock = self.patch_pair(
            "mr_merge", preview("mr_merge", danger=True, target_branch="main",
                                sha="abc123def456"))
        prompts: list[str] = []
        code, out, _ = run_cli(
            ["mr-merge", "group-a/project-a", "7", "--yes"], answers=["main"],
            prompts=prompts)
        self.assertEqual(code, 0)
        self.assertIn("--yes does not apply here", out)
        self.assertEqual(len(prompts), 1)
        self.assertIn("Type the target branch name", prompts[0])
        self.assertIn("main", prompts[0])
        self.assertEqual(do_mock.call_count, 1)
        self.assertEqual(do_mock.call_args.kwargs["sha"], "abc123def456")

    def test_danger_merge_refuses_a_bare_y_even_with_yes(self) -> None:
        _, do_mock = self.patch_pair(
            "mr_merge", preview("mr_merge", danger=True, target_branch="main",
                                sha="abc123"))
        code, out, _ = run_cli(
            ["mr-merge", "group-a/project-a", "7", "--yes"], answers=["y"])
        self.assertEqual(code, 0)
        self.assertIn("did not match", out)
        do_mock.assert_not_called()

    def test_danger_merge_refuses_a_wrong_branch_name(self) -> None:
        _, do_mock = self.patch_pair(
            "mr_merge", preview("mr_merge", danger=True, target_branch="main",
                                sha="abc123"))
        code, out, _ = run_cli(["mr-merge", "group-a/project-a", "7"],
                               answers=["mian"])
        self.assertEqual(code, 0)
        self.assertIn("did not match", out)
        do_mock.assert_not_called()

    def test_non_danger_merge_accepts_yes_without_a_prompt(self) -> None:
        _, do_mock = self.patch_pair(
            "mr_merge", preview("mr_merge", danger=False,
                                target_branch="develop", sha="abc123"))

        def explode(prompt):
            raise AssertionError("a non-protected merge must honour --yes")

        with mock.patch.object(cli, "_ask", explode):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.main(["mr-merge", "group-a/project-a", "7", "--yes"])
        self.assertEqual(code, 0)
        self.assertEqual(do_mock.call_count, 1)

    def test_sync_routes_to_sync_local(self) -> None:
        _, do_mock = self.patch_pair("sync_local", preview("sync_local"))
        code, _, _ = run_cli(["sync", "--group", "group-a"], answers=["y"])
        self.assertEqual(code, 0)
        self.assertEqual(do_mock.call_args.kwargs,
                         {"group": "group-a", "project": None})

    def test_clone_missing_routes(self) -> None:
        _, do_mock = self.patch_pair("clone_missing", preview("clone_missing"))
        run_cli(["clone-missing", "group-a"], answers=["y"])
        self.assertEqual(do_mock.call_args.kwargs, {"group": "group-a"})

    def test_mr_create_passes_every_field(self) -> None:
        _, do_mock = self.patch_pair("mr_create", preview("mr_create"))
        run_cli(["mr-create", "group-a/project-a", "feature", "develop",
                 "Add the thing", "--description", "why", "--draft"],
                answers=["y"])
        self.assertEqual(do_mock.call_args.kwargs, {
            "project": "group-a/project-a", "source_branch": "feature",
            "target_branch": "develop", "title": "Add the thing",
            "description": "why", "draft": True})

    def test_mr_review_comment_passes_the_position(self) -> None:
        _, do_mock = self.patch_pair("mr_review_comment",
                                     preview("mr_review_comment"))
        run_cli(["mr-review-comment", "group-a/project-a", "7", "nit",
                 "--path", "src/app.py", "--new-line", "12"], answers=["y"])
        kwargs = do_mock.call_args.kwargs
        self.assertEqual(kwargs["path"], "src/app.py")
        self.assertEqual(kwargs["new_line"], 12)
        self.assertIsNone(kwargs["old_line"])

    def test_issue_comment_routes(self) -> None:
        _, do_mock = self.patch_pair("issue_comment", preview("issue_comment"))
        run_cli(["issue-comment", "group-a/project-a", "3", "ack"],
                answers=["y"])
        self.assertEqual(do_mock.call_args.kwargs["body"], "ack")

    def test_mr_unapprove_routes(self) -> None:
        _, do_mock = self.patch_pair("mr_unapprove", preview("mr_unapprove"))
        run_cli(["mr-unapprove", "group-a/project-a", "7"], answers=["y"])
        self.assertEqual(do_mock.call_args.kwargs["iid"], 7)

    def test_squash_is_tri_state(self) -> None:
        for argv_extra, expected in ((["--squash"], True),
                                     (["--no-squash"], False),
                                     ([], None)):
            with self.subTest(flags=argv_extra):
                _, do_mock = self.patch_pair(
                    "mr_merge", preview("mr_merge", sha="abc", 
                                        target_branch="develop"))
                run_cli(["mr-merge", "group-a/project-a", "7", *argv_extra],
                        answers=["y"])
                self.assertEqual(do_mock.call_args.kwargs["squash"], expected)

    def test_body_file_supplies_the_comment(self) -> None:
        _, do_mock = self.patch_pair("mr_comment", preview("mr_comment"))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "note.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("from a file")
            run_cli(["mr-comment", "group-a/project-a", "7",
                     "--body-file", path], answers=["y"])
        self.assertEqual(do_mock.call_args.kwargs["body"], "from a file")

    def test_missing_body_is_an_error_before_any_preview(self) -> None:
        preview_mock, do_mock = self.patch_pair("mr_comment",
                                                preview("mr_comment"))
        code, _, err = run_cli(["mr-comment", "group-a/project-a", "7"])
        self.assertEqual(code, 1)
        self.assertIn("error:", err)
        preview_mock.assert_not_called()
        do_mock.assert_not_called()

    def test_json_format_prints_the_preview_as_json(self) -> None:
        self.patch_pair("mr_approve", preview("mr_approve"))
        code, out, _ = run_cli(["mr-approve", "group-a/project-a", "7",
                                "--json"], answers=["n"])
        self.assertEqual(code, 0)
        first = out.split("cancelled")[0]
        self.assertEqual(json.loads(first)["action"], "mr_approve")


# ─── Errors and interruption ──────────────────────────────────────────────────

class TestErrors(unittest.TestCase):

    def setUp(self) -> None:
        ctx = mock.patch.object(cli, "make_context", side_effect=FakeContext)
        ctx.start()
        self.addCleanup(ctx.stop)
        self.reporting = _reporting_module()

    def test_reporting_failure_prints_one_error_line_and_exits_one(self) -> None:
        with mock.patch.object(self.reporting, "groups", create=True,
                               side_effect=RuntimeError("boom")):
            code, out, err = run_cli(["groups"])
        self.assertEqual(code, 1)
        self.assertIn("error: boom", err)
        self.assertEqual(out, "")

    def test_error_message_is_redacted(self) -> None:
        secret = "glpat-AAAABBBBCCCCDDDDEEEE"  # gitleaks:allow - fake test fixture
        with mock.patch.object(
                self.reporting, "groups", create=True,
                side_effect=RuntimeError(f"401 with token {secret}")):
            code, _, err = run_cli(["groups"])
        self.assertEqual(code, 1)
        self.assertNotIn(secret, err)

    def test_keyboard_interrupt_exits_130(self) -> None:
        with mock.patch.object(self.reporting, "groups", create=True,
                               side_effect=KeyboardInterrupt):
            code, _, err = run_cli(["groups"])
        self.assertEqual(code, 130)
        self.assertIn("cancelled", err)

    def test_keyboard_interrupt_at_the_prompt_exits_130(self) -> None:
        with mock.patch.object(actions, "preview_mr_approve",
                               return_value=preview("mr_approve")):
            with mock.patch.object(actions, "do_mr_approve") as do_mock:
                def interrupt(prompt):
                    raise KeyboardInterrupt

                with mock.patch.object(cli, "_ask", interrupt):
                    out, err = io.StringIO(), io.StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        code = cli.main(["mr-approve", "group-a/project-a", "7"])
        self.assertEqual(code, 130)
        do_mock.assert_not_called()

    def test_action_failure_exits_one(self) -> None:
        with mock.patch.object(actions, "preview_mr_approve",
                               return_value=preview("mr_approve")):
            with mock.patch.object(actions, "do_mr_approve",
                                   side_effect=RuntimeError("403 forbidden")):
                code, _, err = run_cli(
                    ["mr-approve", "group-a/project-a", "7"], answers=["y"])
        self.assertEqual(code, 1)
        self.assertIn("error: 403 forbidden", err)


class TestRedaction(unittest.TestCase):
    """The local fallback redactor, exercised without the context module."""

    def redact(self, text: str) -> str:
        with mock.patch.object(cli, "_module",
                               side_effect=ImportError("absent")):
            return cli.redact(text)

    def test_personal_access_token(self) -> None:
        self.assertNotIn("AAAABBBBCCCC",
                         self.redact("token glpat-AAAABBBBCCCCDDDD"))

    def test_private_token_header(self) -> None:
        self.assertNotIn("supersecret",
                         self.redact("PRIVATE-TOKEN: supersecret"))

    def test_bearer_header(self) -> None:
        self.assertNotIn("abc123", self.redact("Authorization: Bearer abc123"))

    def test_url_userinfo(self) -> None:
        out = self.redact("https://oauth2:glpat-xyz@gitlab.example.com/a.git")
        self.assertNotIn("glpat-xyz", out)
        self.assertIn("gitlab.example.com", out)


# ─── Terminal safety ──────────────────────────────────────────────────────────

#: Server-controlled payloads that must never reach a terminal raw.
PAYLOADS = {
    "osc52": "\x1b]52;c;ZWNobyBwd25lZA==\x07",
    "csi_clear": "\x1b[2K\x1b[1A",
    "cr_rewrite": "safe\rEVIL",
    "c1_csi": "\x9b31m",
    "bidi": "\u202eevil",
}
_FORBIDDEN = ("\x1b", "\x9b", "\x07", "\r", "\u202e")


class TestTerminalSafety(unittest.TestCase):
    """GitLab text is attacker-controlled: no control sequence is printed."""

    def setUp(self) -> None:
        ctx = mock.patch.object(cli, "make_context", side_effect=FakeContext)
        ctx.start()
        self.addCleanup(ctx.stop)
        self.reporting = _reporting_module()
        self.rows = [{"title": f"mr {name} {text}", "author": text}
                     for name, text in PAYLOADS.items()]

    def assert_clean(self, text: str) -> None:
        for char in _FORBIDDEN:
            self.assertNotIn(char, text, msg=repr(text))

    def test_terminal_safe_keeps_newlines_and_unicode(self) -> None:
        self.assertEqual(cli.terminal_safe("a\nb\tc 日本 سلام"), "a\nb\tc 日本 سلام")
        self.assertEqual(cli.terminal_safe_line("a\nb\tc"), "a b c")
        self.assertEqual(cli.terminal_safe(None), "")

    def test_every_payload_is_stripped(self) -> None:
        for name, text in PAYLOADS.items():
            with self.subTest(name):
                self.assert_clean(cli.terminal_safe(text))

    def test_table_output_is_clean(self) -> None:
        with mock.patch.object(self.reporting, "groups", create=True,
                               return_value=self.rows):
            code, out, _ = run_cli(["groups"])
        self.assertEqual(code, 0)
        self.assert_clean(out)
        self.assertIn("EVIL", out)

    def test_markdown_output_is_clean(self) -> None:
        with mock.patch.object(self.reporting, "groups", create=True,
                               return_value=self.rows):
            _, out, _ = run_cli(["groups", "--format", "md"])
        self.assert_clean(out)

    def test_json_output_is_clean_and_lossless(self) -> None:
        with mock.patch.object(self.reporting, "groups", create=True,
                               return_value=self.rows):
            _, out, _ = run_cli(["groups", "--json"])
        self.assert_clean(out)
        self.assertEqual(json.loads(out), self.rows)

    def test_preview_and_reasons_are_clean(self) -> None:
        prev = preview("mr_comment", summary=PAYLOADS["osc52"] + "summary",
                       warnings=[PAYLOADS["csi_clear"]], title=PAYLOADS["c1_csi"])
        with mock.patch.object(actions, "preview_mr_comment", return_value=prev), \
                mock.patch.object(actions, "do_mr_comment") as do_mock:
            _, out, err = run_cli(["mr-comment", "group-a/project-a", "7", "hi"],
                                  answers=["n"])
        do_mock.assert_not_called()
        self.assert_clean(out + err)
        refused = preview("mr_comment", ok=False, reasons=[PAYLOADS["bidi"]])
        with mock.patch.object(actions, "preview_mr_comment", return_value=refused):
            _, out, err = run_cli(["mr-comment", "group-a/project-a", "7", "hi"])
        self.assert_clean(out + err)

    def test_error_message_is_clean(self) -> None:
        with mock.patch.object(self.reporting, "groups", create=True,
                               side_effect=RuntimeError(PAYLOADS["osc52"] + "boom")):
            code, _, err = run_cli(["groups"])
        self.assertEqual(code, 1)
        self.assert_clean(err)
        self.assertIn("boom", err)


if __name__ == "__main__":
    unittest.main()
