#!/usr/bin/env python3
# tests/test_create_interactive.py — interactive Create (main menu 6).
#
# Pins create_argv (answers -> `report create-group/create-project` argv),
# cmd_create (scripted input(), report CLI main() captured, never run for
# real) and that the main menu accepts "6". Offline: no network, no token.

import builtins
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

import _bootstrap  # noqa: F401  (puts the CLI directory on sys.path)

import gitlab_cli as G
from gitlab_mcp import cli as report_cli

CFG = {"name": "lab", "url": "https://gitlab.example"}


class CreateArgvTests(unittest.TestCase):
    def test_group_minimal(self):
        self.assertEqual(G.create_argv("g", {"name": "Team A"}),
                         ["create-group", "--", "Team A"])

    def test_subgroup_has_parent_and_path(self):
        argv = G.create_argv("s", {"name": "Sub", "path": "sub",
                                   "parent": "team/a", "visibility": "internal"})
        self.assertEqual(argv, ["create-group", "--path=sub", "--parent=team/a",
                                "--visibility=internal", "--", "Sub"])

    def test_project_full(self):
        argv = G.create_argv("p", {"name": "api", "namespace": "team/a",
                                   "description": "d", "readme": True,
                                   "visibility": "public"})
        self.assertEqual(argv, ["create-project", "--namespace=team/a",
                                "--visibility=public", "--description=d",
                                "--readme", "--", "api"])

    def test_empty_answers_dropped(self):
        argv = G.create_argv("p", {"name": "api", "path": "", "namespace": "",
                                   "description": "", "visibility": "",
                                   "readme": False})
        self.assertEqual(argv, ["create-project", "--", "api"])

    def test_readme_only_for_projects(self):
        for kind in ("g", "s"):
            argv = G.create_argv(kind, {"name": "x", "readme": True})
            self.assertNotIn("--readme", argv, kind)

    def test_no_instance_flag(self):
        # The active instance is resolved by the report CLI itself, with the
        # same token lookup the menu used.
        self.assertNotIn("--instance", G.create_argv("g", {"name": "A"}))

    def test_argv_parses_in_report_cli(self):
        # Every shape create_argv emits must be accepted by the real parser,
        # including values that start with "-".
        p = report_cli.build_parser()
        for kind, ans in (("g", {"name": "A", "visibility": "public"}),
                          ("s", {"name": "-B", "parent": "a", "path": "b"}),
                          ("p", {"name": "--yes", "readme": True,
                                 "namespace": "a", "description": "-d"})):
            args = p.parse_args(G.create_argv(kind, ans))
            self.assertEqual(args.name, ans["name"])
            self.assertFalse(args.yes)


class CmdCreateTests(unittest.TestCase):
    def run_create(self, answers, cfg=CFG, main=None):
        script = list(answers)

        def fake_input(_prompt=""):
            if not script:
                raise EOFError
            item = script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        captured = []

        def fake_main(argv):
            captured.append(argv)
            return 7

        buf = io.StringIO()
        with mock.patch.object(builtins, "input", fake_input), \
                mock.patch.object(report_cli, "main", main or fake_main), \
                redirect_stdout(buf):
            rc = G.cmd_create(cfg)
        return rc, captured, buf.getvalue()

    def test_group(self):
        rc, calls, _ = self.run_create(["g", "Team A", "", ""])
        self.assertEqual(rc, 7)  # report main()'s exit code passes through
        self.assertEqual(calls, [["create-group", "--", "Team A"]])

    def test_subgroup(self):
        _, calls, _ = self.run_create(["S", "Sub", "sub", "team/a", "Internal"])
        self.assertEqual(calls, [["create-group", "--path=sub", "--parent=team/a",
                                  "--visibility=internal", "--", "Sub"]])

    def test_project(self):
        _, calls, _ = self.run_create(["p", "api", "", "team/a", "desc", "Y", ""])
        self.assertEqual(calls, [["create-project", "--namespace=team/a",
                                  "--description=desc", "--readme", "--", "api"]])

    def test_project_readme_no(self):
        _, calls, _ = self.run_create(["p", "api", "", "", "", "n", ""])
        self.assertEqual(calls, [["create-project", "--", "api"]])

    def test_bad_visibility_cancels(self):
        self.assert_cancelled(["g", "A", "", "pub"])

    def test_argparse_exit_does_not_end_menu(self):
        def boom(argv):
            raise SystemExit(2)
        rc, _, _ = self.run_create(["g", "A", "", ""], main=boom)
        self.assertEqual(rc, 2)

    def assert_cancelled(self, answers):
        rc, calls, out = self.run_create(answers)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])
        self.assertIn("Cancelled", out)

    def test_bad_kind_cancels(self):
        self.assert_cancelled(["x"])

    def test_empty_kind_cancels(self):
        self.assert_cancelled([""])

    def test_empty_name_cancels(self):
        self.assert_cancelled(["g", "  "])

    def test_subgroup_without_parent_cancels(self):
        self.assert_cancelled(["s", "Sub", "", ""])

    def test_eof_at_first_prompt_cancels(self):
        self.assert_cancelled([])

    def test_eof_at_name_cancels(self):
        self.assert_cancelled(["p", EOFError()])

    def test_ctrl_c_cancels(self):
        self.assert_cancelled([KeyboardInterrupt()])


class MainMenuTests(unittest.TestCase):
    def test_accepts_6(self):
        # "6" once, then EOF: if "6" were rejected, the menu re-prompts, hits
        # EOF and exits (SystemExit) instead of looping forever.
        answers = iter(["6"])

        def fake_input(_p=""):
            try:
                return next(answers)
            except StopIteration:
                raise EOFError from None

        buf = io.StringIO()
        with mock.patch.object(G.errors, "unresolved_count", return_value=0), \
                mock.patch.object(G.caps, "can", return_value=False), \
                mock.patch.object(builtins, "input", fake_input), \
                redirect_stdout(buf):
            self.assertEqual(G.pick_main_menu(CFG, {"ok": False}), "6")
        self.assertIn("Create", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
