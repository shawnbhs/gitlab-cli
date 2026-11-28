"""Offline contract tests for canonical MCP paths and action outcomes."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _error_log_guard  # noqa: F401

import instances
from gitlab_mcp import actions, reporting


class Context:
    def __init__(self, base, host):
        self.url = f"https://{host}"
        self.name = host
        with patch.object(instances.creds, "resolve_token", return_value=""):
            self.cfg = instances.build_cfg({"name": host, "url": self.url,
                                            "master_dir": str(base)})
        self.master_dir = self.cfg["master_dir"]
        self.inventory = []
        self.total = None

    def cfg_for_git(self):
        return self.cfg.copy()

    def run_captured(self, fn, *args, **kwargs):
        return fn(*args, **kwargs), ""

    def get(self, path, params=None):
        if path != "/groups/team":
            raise AssertionError(path)
        return {"id": 1, "full_path": "team"}

    def paginate(self, path, params=None):
        if path != "/groups/1/projects":
            raise AssertionError(path)
        return self.inventory, False, self.total


class HostLayoutMCPTests(unittest.TestCase):
    def setUp(self):
        tree = tempfile.TemporaryDirectory()
        self.addCleanup(tree.cleanup)
        self.base = Path(tree.name)
        self.context = Context(self.base, "git.example.com")
        self.other = Context(self.base, "other.example.com")

    def repo(self, namespace, ctx=None, remote=None):
        ctx = ctx or self.context
        repo = Path(ctx.master_dir) / namespace
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                        remote or f"{ctx.url}/{namespace}.git"], check=True)
        return str(repo)

    def project(self, namespace):
        return {"path_with_namespace": namespace, "empty_repo": True,
                "id": int.from_bytes(namespace.encode()),
                "http_url_to_repo": f"{self.context.url}/{namespace}.git"}

    def test_sync_exact_namespace_and_host_in_both_phases(self):
        wanted = self.repo("team/one/app")
        self.repo("team/two/app")
        self.repo("team/one/app", ctx=self.other)
        preview = actions.preview_sync_local(self.context, project="team/one/app")
        self.assertEqual(preview["repos"]["sample"], [wanted])
        with patch.object(actions.gl_pull, "sync_many", return_value=({"pulled": 1}, [])) as sync:
            self.assertTrue(actions.do_sync_local(self.context, project="team/one/app")["ok"])
        self.assertEqual(sync.call_args.args[0], [wanted])
        self.assertEqual(sync.call_args.kwargs["host_root"], self.context.master_dir)
        self.assertEqual(sync.call_args.kwargs["instance_url"], self.context.url)

    def test_sync_rechecks_authority_after_preview(self):
        repo = self.repo("team/one/app")
        actions.preview_sync_local(self.context, project="team/one/app")
        for url in ("https://git.example.com:8443/team/one/app.git",
                    "http://git.example.com/team/one/app.git"):
            with self.subTest(url=url):
                subprocess.run(["git", "-C", repo, "remote", "set-url", "origin", url],
                               check=True)
                with patch.object(actions.gl_pull, "sync_many") as sync, self.assertRaises(ValueError):
                    actions.do_sync_local(self.context, project="team/one/app")
                sync.assert_not_called()

    def test_sync_rejects_basename_and_unresolved_current_source(self):
        repo = self.repo("team/one/app")
        with self.assertRaises(ValueError):
            actions.preview_sync_local(self.context, project="app")
        self.assertTrue(actions.preview_sync_local(self.context, group="team")["ok"])
        subprocess.run(["git", "-C", repo, "remote", "set-url", "origin",
                        f"{self.other.url}/team/one/app.git"], check=True)
        with patch.object(actions.gl_pull, "sync_many") as sync, self.assertRaises(ValueError):
            actions.do_sync_local(self.context, group="team")
        sync.assert_not_called()

    def test_local_status_canonical_scope_and_blocker(self):
        wanted = self.repo("team/one/app")
        self.repo("team/two/app", remote=f"{self.other.url}/team/two/app.git")
        self.repo("team/one/app", ctx=self.other)
        report = reporting.local_status(self.context, "team")
        self.assertEqual([row["path"] for row in report["repos"]], [wanted])
        self.assertEqual(len(report["blocked"]), 1)
        self.assertEqual(reporting.local_status(self.other, "team")["count"], 1)
        self.assertEqual(reporting.local_status(self.context, "one")["count"], 0)
        for group in ("", "../other.example.com/team"):
            with self.assertRaises(ValueError):
                reporting.local_status(self.context, group)

    def test_clone_preview_complete_inventory_and_exact_existing(self):
        self.repo("team/one/app")
        self.repo("team/two/app", ctx=self.other)
        self.context.inventory = [self.project("team/one/app"), self.project("team/two/app")]
        self.context.total = 2
        preview = actions.preview_clone_missing(self.context, "team")
        self.assertTrue(preview["ok"])
        self.assertEqual(preview["target"], str(Path(self.context.master_dir) / "team"))
        self.assertEqual(preview["would_clone"]["sample"], ["team/two/app"])
        self.context.total = 3
        with patch.object(actions.gl_clone, "cmd_clone") as clone:
            self.assertFalse(actions.preview_clone_missing(self.context, "team")["ok"])
            with self.assertRaises(ValueError):
                actions.do_clone_missing(self.context, "team")
        clone.assert_not_called()

    def test_clone_rechecks_plan_and_verifies_checkout_not_command_return(self):
        self.context.inventory = [self.project("team/one/app")]
        self.assertTrue(actions.preview_clone_missing(self.context, "team")["ok"])
        with patch.object(actions.gl_git, "clone_repo_managed", return_value={"status": "ok"}):
            with self.assertRaisesRegex(ValueError, "incomplete"):
                actions.do_clone_missing(self.context, "team")

        def transfer(*args, **kwargs):
            self.repo("team/one/app")
            return {"status": "ok"}

        with patch.object(actions.gl_git, "clone_repo_managed", side_effect=transfer) as clone, \
                patch.object(actions.gl_clone, "cmd_clone") as rediscover:
            result = actions.do_clone_missing(self.context, "team")
            rediscover.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(clone.call_args.kwargs["host_root"], self.context.master_dir)
        self.assertEqual(result["repos"]["sample"], ["one/app"])

    def test_clone_refuses_occupied_target_and_foreign_url(self):
        self.context.inventory = [self.project("team/one/app")]
        (Path(self.context.master_dir) / "team/one/app").mkdir(parents=True)
        self.assertFalse(actions.preview_clone_missing(self.context, "team")["ok"])
        self.context.inventory = [{**self.project("team/two/app"),
                                   "http_url_to_repo": f"{self.other.url}/team/two/app.git"}]
        self.assertFalse(actions.preview_clone_missing(self.context, "team")["ok"])


    def test_clone_refuses_same_hostname_with_different_authority(self):
        row = self.project("team/one/app")
        for url in ("http://git.example.com/team/one/app.git",
                    "https://git.example.com:8443/team/one/app.git"):
            with self.subTest(url=url), patch.object(actions.gl_clone, "cmd_clone") as clone:
                self.context.inventory = [{**row, "http_url_to_repo": url}]
                self.assertFalse(actions.preview_clone_missing(self.context, "team")["ok"])
                with self.assertRaises(ValueError):
                    actions.do_clone_missing(self.context, "team")
                clone.assert_not_called()

    def test_clone_reports_managed_retry_rows_when_no_targets_are_missing(self):
        namespace = "team/one/app"
        self.repo(namespace)
        self.context.inventory = [self.project(namespace)]
        for reason in ("shallow", actions.gl_clone.REASON_LFS, "managed_blocked"):
            with self.subTest(reason=reason):
                actions.gl_clone._managed_write_log(
                    actions.gl_clone._error_log_path(self.context.master_dir, "team", managed=True),
                    {"group": "team", "authority": ["https", "git.example.com", 443],
                     "errors": [{"namespace": namespace, "reason": reason}]})
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    actions.do_clone_missing(self.context, "team")

    def test_sync_confirmation_rejects_new_repository_in_same_group(self):
        from gitlab_mcp import server

        self.repo("team/one/app")
        server.reset_state()
        self.addCleanup(server.reset_state)
        with (patch.dict("os.environ", {"GITLAB_MCP_CONFIRM_MODE": "agent", "GITLAB_MCP_READ_ONLY": "0"}),
              patch.object(server, "_ctx", return_value=self.context),
              patch.object(actions.gl_pull, "sync_many", return_value=({"pulled": 2}, [])) as sync):
            preview = server._tool_sync_local(group="team")
            self.assertEqual(preview["status"], "needs_confirmation")
            self.repo("team/two/app")
            response = server._tool_sync_local(group="team", confirm_token=preview["confirm_token"])
            self.assertNotEqual(response["status"], "done")
            sync.assert_not_called()
