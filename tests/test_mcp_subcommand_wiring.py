# tests/test_mcp_subcommand_wiring.py — `gitlab-cli mcp` / `gitlab-cli report`.
#
# Pins the two integration seams between gitlab_cli.py and gitlab_mcp/:
#   * `mcp` must reach gitlab_mcp.server.main() WITHOUT printing the banner —
#     stdout is the MCP protocol, one stray byte corrupts the session.
#   * `report ...` must hand its argv through unchanged to gitlab_mcp.cli.main
#     and propagate that exit code.
# Offline: server.main and cli.main are patched; nothing touches a network.

import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

if HERE not in sys.path:
    sys.path.insert(0, HERE)
import _error_log_guard  # noqa: E402,F401  (never touch the live errors.log)
import gitlab_cli  # noqa: E402


class McpSubcommandWiring(unittest.TestCase):

    def test_mcp_calls_server_main_and_prints_nothing(self):
        from gitlab_mcp import server
        buf = io.StringIO()
        with mock.patch.object(server, "main") as fake, \
                mock.patch.object(gitlab_cli, "print_banner") as banner, \
                redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                gitlab_cli._run(["mcp"])
        self.assertEqual(cm.exception.code, 0)
        fake.assert_called_once_with()
        banner.assert_not_called()
        self.assertEqual(buf.getvalue(), "")

    def test_report_passes_argv_through_and_propagates_exit_code(self):
        from gitlab_mcp import cli
        with mock.patch.object(cli, "main", return_value=7) as fake, \
                mock.patch.object(gitlab_cli, "print_banner") as banner:
            with self.assertRaises(SystemExit) as cm:
                gitlab_cli._run(["report", "commits", "grp/proj", "--json"])
        self.assertEqual(cm.exception.code, 7)
        fake.assert_called_once_with(["commits", "grp/proj", "--json"])
        banner.assert_not_called()

    def test_classic_commands_do_not_import_gitlab_mcp(self):
        self.assertIsNone(gitlab_cli._run_sidecar([]))
        self.assertIsNone(gitlab_cli._run_sidecar(["doctor"]))
        self.assertIsNone(gitlab_cli._run_sidecar(["clone", "mcp"]))

    def test_usage_lists_both_commands(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            gitlab_cli.print_usage()
        self.assertIn("gitlab-cli mcp", buf.getvalue())
        self.assertIn("gitlab-cli report", buf.getvalue())

    def test_report_help_subprocess_exits_zero_without_banner(self):
        env = dict(os.environ, HOME=os.environ.get("HOME", "/tmp"))
        r = subprocess.run([sys.executable, os.path.join(ROOT, "gitlab_cli.py"),
                            "report", "--help"], capture_output=True, text=True,
                           timeout=60, cwd=ROOT, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("usage", r.stdout.lower())
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
