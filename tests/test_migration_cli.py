"""Offline migration CLI contract tests."""
import io
import json
import os
import stat
import tempfile
import types
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from gitlab_mcp import cli
from tests import _home_guard

TOKEN = "glpat-" + "TESTSENTINEL"  # gitleaks:allow - synthetic sentinel


def fixture(status="identical", complete=True):
    return {
        "repositories": [{"source_path": "a/repo", "target_path": "a/repo",
                          "status": status, "differences": ["branch changed"] if status != "identical" else []}],
        "access": [{"project": "a/repo", "username": "alice",
                    "source_role": "Developer", "target_role": "Developer",
                    "source_state": "active", "target_state": "active",
                    "source_expires_at": None, "target_expires_at": None,
                    "source_origin": TOKEN, "target_origin": "direct", "status": "identical"}],
        "summary": {"complete": complete, "source_instance": "src", "target_instance": "dst",
                    "source_group": "a", "target_group": "a",
                    "repositories": {"identical": 1}, "access": {"identical": 1}},
        "limitations": ["Does not verify Git objects"], "complete": complete,
    }


def setUpModule():
    """Keep configured-secret lookups away from the real home."""
    _home_guard.activate()


def tearDownModule():
    _home_guard.deactivate()


class MigrationCliTests(unittest.TestCase):
    def setUp(self):
        self.previous_contexts = list(cli._CONTEXTS)
        self.addCleanup(lambda: cli._CONTEXTS.__setitem__(slice(None), self.previous_contexts))
        self.result = fixture()
        self.calls = []
        def context(name):
            ctx = types.SimpleNamespace(name=name, redact=lambda text: str(text).replace(TOKEN, "[REDACTED]"))
            cli._CONTEXTS.append(ctx)
            return ctx
        def audit(*args):
            self.calls.append(args)
            return self.result
        for name, value in (("make_context", context), ("_migration", lambda: types.SimpleNamespace(audit=audit))):
            patcher = mock.patch.object(cli, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["migration", "--source-instance", "src", "--target-instance", "dst",
                             "--source-group", "a", *extra])
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def test_parser_flags_and_default_target_group(self):
        args = cli.build_parser().parse_args(["migration", "--source-instance", "src",
                                              "--target-instance", "dst", "--source-group", "a"])
        self.assertEqual((args.source_instance, args.target_instance, args.source_group), ("src", "dst", "a"))
        self.assertIsNone(args.target_group)
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(self.calls[-1][2:], ("a", "a"))
        self.assertEqual(self.run_cli("--target-group", "b")[0], 0)
        self.assertEqual(self.calls[-1][2:], ("a", "b"))

    def test_instance_rejected(self):
        code, _, err = self.run_cli("--instance", "old")
        self.assertEqual(code, 2)
        self.assertIn("migration takes --source-instance/--target-instance, not --instance", err)
        self.assertFalse(self.calls)

    def test_complete_identical_tables_and_summary(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("repositories", out)
        self.assertIn("access", out)
        self.assertIn("summary", out)
        self.assertIn("limitations", out)

    def test_nonidentical_statuses_exit_one(self):
        for status in ("different", "missing", "extra"):
            with self.subTest(status=status):
                self.result = fixture(status)
                code, out, _ = self.run_cli("--format", "md")
                self.assertEqual(code, 1)
                self.assertIn("branch changed", out)

    def test_incomplete_exit_two(self):
        self.result = fixture(complete=False)
        self.assertEqual(self.run_cli()[0], 2)

    def test_audit_error_redacted_exit_two(self):
        with mock.patch.object(cli, "_migration", return_value=types.SimpleNamespace(
                audit=lambda *_: (_ for _ in ()).throw(RuntimeError("failed " + TOKEN)))):
            code, _, err = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertIn("[REDACTED]", err)

    def test_json_exact_keys(self):
        code, out, _ = self.run_cli("--json")
        self.assertEqual(code, 0)
        self.assertEqual(set(json.loads(out)), {"repositories", "access", "summary", "limitations", "complete"})
        self.assertNotIn(TOKEN, out)

    def test_out_refuses_existing_and_symlink_then_creates_mode_0600(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = os.path.join(directory, "existing")
            with open(existing, "w", encoding="utf-8") as handle:
                handle.write("untouched")
            symlink = os.path.join(directory, "link")
            os.symlink(existing, symlink)
            for path in (existing, symlink):
                code, _, err = self.run_cli("--out", path)
                self.assertEqual(code, 2)
                self.assertIn("exists", err)
            with open(existing, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "untouched")
            new = os.path.join(directory, "new")
            code, _, _ = self.run_cli("--json", "--out", new)
            self.assertEqual(code, 0)
            self.assertEqual(stat.S_IMODE(os.stat(new).st_mode), 0o600)
            with open(new, encoding="utf-8") as handle:
                written = handle.read()
            self.assertNotIn(TOKEN, written)
            self.assertEqual(set(json.loads(written)),
                             {"repositories", "access", "summary", "limitations", "complete"})

    def test_debug_traceback_redacted_and_terminal_safe_for_all_commands(self):
        def fail(*_):
            try:
                raise ValueError("inner " + TOKEN + "\x1b]52;c;eA==\x07\u202e")
            except ValueError as exc:
                raise RuntimeError("outer") from exc
        with mock.patch.dict(os.environ, {"GITLAB_MCP_DEBUG": "1"}), \
                mock.patch.object(cli, "_migration", return_value=types.SimpleNamespace(audit=fail)), \
                mock.patch.dict(cli.READ_HANDLERS, {"instances": fail}):
            code, _, err = self.run_cli()
            self.assertEqual(code, 2)
            out, errio = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(errio):
                self.assertEqual(cli.main(["instances"]), 1)
            for text in (err, errio.getvalue()):
                self.assertIn("Traceback", text)
                self.assertIn("inner [REDACTED]", text)
                for unsafe in (TOKEN, "\x1b", "\x07", "\u202e"):
                    self.assertNotIn(unsafe, text)

    def test_out_fdopen_failure_closes_descriptor_and_removes_new_empty_file(self):
        seen = []
        def bad_fdopen(fd, *_args, **_kwargs):
            seen.append(fd)
            raise OSError("fdopen failed")
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "report.json")
            with mock.patch.object(cli.os, "fdopen", bad_fdopen), \
                    mock.patch.object(cli.os, "close", wraps=os.close) as close:
                code, out, _ = self.run_cli("--out", path)
            self.assertEqual(code, 2)
            self.assertEqual(out, "")
            self.assertEqual(len(seen), 1)
            close.assert_any_call(seen[0])
            self.assertFalse(os.path.lexists(path))

    def test_out_nonprivate_mode_fails_closed_without_report_bytes(self):
        real_fstat = os.fstat
        def loose_fstat(fd):
            values = list(real_fstat(fd))
            values[0] = stat.S_IFREG | 0o777
            return os.stat_result(values)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "report.json")
            with mock.patch.object(cli.os, "fstat", loose_fstat):
                code, out, err = self.run_cli("--json", "--out", path)
            self.assertEqual(code, 2)
            self.assertIn("private", err)
            self.assertEqual(out, "")
            self.assertFalse(os.path.lexists(path))

    def test_out_windows_backed_filesystem_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "report.json")
            with mock.patch.object(cli, "_filesystem_type", return_value="9p"):
                code, out, err = self.run_cli("--json", "--out", path)
            self.assertEqual(code, 2)
            self.assertIn("private", err)
            self.assertEqual(out, "")
            self.assertFalse(os.path.lexists(path))

    def test_out_native_windows_refused_but_stdout_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "report.json")
            with mock.patch.object(cli, "_NATIVE_WINDOWS", True):
                code, _, err = self.run_cli("--out", path)
                self.assertEqual(code, 2)
                self.assertIn("Windows", err)
                self.assertFalse(os.path.lexists(path))
                code, out, _ = self.run_cli("--json")
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out)["complete"])

    def test_out_failure_never_deletes_preexisting_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "report.json")
            with open(path, "w", encoding="utf-8"):
                pass
            with mock.patch.object(cli.os, "fdopen", side_effect=OSError("boom")):
                code, _, err = self.run_cli("--out", path)
            self.assertEqual(code, 2)
            self.assertIn("exists", err)
            self.assertTrue(os.path.exists(path))

    def test_general_write_output_still_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "existing")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("old")
            with redirect_stdout(io.StringIO()):
                cli.write_output("new", path)
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "new\n")
