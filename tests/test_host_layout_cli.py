"""Offline integration checks for canonical CLI paths."""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import io
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

import _error_log_guard  # noqa: F401

import coverage as cov
import gitlab_cli as cli
import instances
import log
import paths
import pull
import setup


CLI = Path(__file__).resolve().parents[1] / "gitlab_cli.py"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True)


def checkout(repo, *remotes):
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
        "commit", "-qm", "fixture", "--allow-empty")
    for name, url in remotes:
        git(repo, "remote", "add", name, url)
    return repo


class HostLayoutCLI(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp_path = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.config = instances.build_cfg({
            "name": "One", "url": "https://one.example.test",
            "master_dir": str(self.tmp_path / "clones"), "folder": "legacy",
        })

    def test_remote_count_is_exact_offline_and_read_only(self):
        home = self.tmp_path / "home"
        config_dir = home / ".gitlab-cli"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "instances.json"
        config_file.write_text(json.dumps({"active": "One", "instances": [
            {"name": "One", "url": self.config["url"], "master_dir": self.config["base_dir"],
             "token": "synthetic-inline-token"}]}), encoding="utf-8")
        root = Path(self.config["master_dir"])
        repo = checkout(root / "group/sub/repo",
                        ("mirror", "https://other.example.test/group/sub/repo.git"),
                        ("source", "https://fixture:placeholder@one.example.test/group/sub/repo.git"))
        checkout(root / "group/other/repo",
                 ("source", "https://one.example.test/group/other/repo.git"))
        checkout(root.parent / "other.example.test/group/missing/repo",
                 ("source", "https://other.example.test/group/missing/repo.git"))
        git(repo, "config", "branch.main.remote", "source")
        git(repo, "config", "branch.main.merge", "refs/heads/develop")
        tracked = [config_file, repo / ".git/config", repo / ".git/HEAD", repo / ".git/FETCH_HEAD"]

        def snapshot():
            return ({p: (p.read_bytes(), p.stat().st_mtime_ns) if p.exists() else None for p in tracked},
                    sorted(config_dir.iterdir()))

        def run(*projects):
            return subprocess.run(
                [sys.executable, str(CLI), "remote-count", *projects],
                env={**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / "xdg"),
                     "GIT_ALLOW_PROTOCOL": "", "GIT_TERMINAL_PROMPT": "0"},
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5,
            )

        before = snapshot()
        one = run("group/sub/repo")
        assert one.returncode == 0, one.stdout + one.stderr
        assert "group/sub/repo: 1" in one.stdout
        two = run("group/sub/repo", "group/other/repo")
        assert two.returncode == 0, two.stdout + two.stderr
        assert "group/other/repo: 0" in two.stdout
        mixed = run("group/other/repo", "group/missing/repo", "repo", "../repo")
        assert mixed.returncode != 0
        assert "group/other/repo: 0" in mixed.stdout
        assert "unresolved" in mixed.stdout
        for result in (one, two, mixed):
            assert "https://" not in result.stdout + result.stderr
            assert "placeholder" not in result.stdout + result.stderr
        assert snapshot() == before
        assert not (home / "xdg").exists()


    def test_cmd_pull_resolves_exact_group_and_forwards_host_root(self):
        root = Path(self.config["master_dir"])
        repo = checkout(root / "group/sub/repo",
                        ("source", "https://one.example.test/group/sub/repo.git"))
        checkout(root / "sub/wrong", ("source", "https://one.example.test/sub/wrong.git"))
        sync = Mock(return_value={"path": str(repo), "status": pull.S_UP_TO_DATE, "other": []})
        self.stack.enter_context(patch.object(pull, "sync_repo", sync))
        pull.cmd_pull(self.config, "group/sub")
        sync.assert_called_once_with(str(repo), host_root=str(root), instance_url=self.config["url"])
        pull.cmd_pull(self.config, "unrelated/sub")
        sync.assert_called_once()
        with self.assertRaises(paths.UnsafePath):
            pull.cmd_pull(self.config, "")
        with self.assertRaises(paths.UnsafePath):
            pull.cmd_pull(self.config, "group", str(root.parent / "other.example.test"))


    def test_managed_cli_sync_does_not_reconcile_or_prune(self):
        pull_group = Mock()
        reconcile = Mock(side_effect=AssertionError("must not reconcile"))
        prune = Mock(side_effect=AssertionError("must not prune"))
        self.stack.enter_context(patch.object(cli, "cmd_pull", pull_group))
        self.stack.enter_context(patch.object(cli, "_reconcile_renames", reconcile))
        self.stack.enter_context(patch.object(cli, "_prune_leftovers", prune))
        cli._sync_group(self.config, "group/sub", self.config["master_dir"], {})
        pull_group.assert_called_once_with(self.config, "group/sub", self.config["master_dir"])
        reconcile.assert_not_called()
        prune.assert_not_called()


    def test_managed_log_filters_foreign_and_wrong_namespace(self):
        root = Path(self.config["master_dir"])
        checkout(root / "group/sub/repo", ("site", "https://one.example.test/group/sub/repo.git"))
        checkout(root / "group/sub/foreign", ("site", "https://other.example.test/group/sub/foreign.git"))
        checkout(root / "group/sub/wrong", ("site", "https://one.example.test/group/other/wrong.git"))
        captured = io.StringIO()
        with redirect_stdout(captured):
            log.cmd_log(self.config, "group/sub", since="1d")
        output = captured.getvalue()
        assert "1 commit" in output
        assert "2 " in output and "unresolved" in output
        assert "one.example.test" in output
        with self.assertRaises(paths.UnsafePath):
            log.cmd_log(self.config, "")


    def test_coverage_keeps_exact_path_and_host(self):
        root = Path(self.config["master_dir"])
        good = checkout(root / "Group/sub/repo", ("site", "https://one.example.test/Group/sub/repo.git"))
        checkout(root / "Group/other/repo", ("site", "https://one.example.test/Group/sub/repo.git"))
        checkout(root / "Group/sub/foreign", ("site", "https://other.example.test/Group/sub/foreign.git"))
        checkout(root / "Group/sub/deep/repo", ("site", "https://one.example.test/Group/sub/deep/repo.git"))
        checkout(root.parent / "other.example.test/Group/sub/repo",
                 ("site", "https://other.example.test/Group/sub/repo.git"))
        index = cli.scan_dest(str(root), self.config)
        assert index == {("one.example.test", "Group/sub/repo"): str(good),
                         ("one.example.test", "Group/sub/deep/repo"): str(root / "Group/sub/deep/repo")}
        projects = [{"path_with_namespace": p, "namespace": {"kind": "group"}} for p in (
            "Group/sub/repo", "Group/sub/deep/repo", "Group/other/repo", "Group/sub/foreign", "group/sub/repo")]
        group = {"full_path": "Group"}
        assert cov.group_coverage(self.config, group, index, projects) == (2, 5)
        assert cov.missing_projects(self.config, group, index, projects) == projects[2:]
        assert cli._instance_coverage_counts(self.config, index, projects)["all_present"] == 2
        # Doctor uses the same canonical index and does not infer that "folder" is missing.
        marked = Mock()
        self.stack.enter_context(patch.object(setup, "_mark", marked))
        setup._config_completeness_check("One", self.config, {"ok": True, "by_key": {
            ("one.example.test", "Group/sub/repo"): [str(good)]}}, "one.example.test", [])
        assert marked.call_args.args[1] == "ok"


    def test_clone_destination_is_host_qualified_or_refused(self):
        others = {"instances": [{"name": "Other", "url": "https://other.example.test",
                                 "master_dir": self.config["base_dir"]}]}
        self.stack.enter_context(patch.object(instances, "_read_instances", lambda: (others, "")))
        self.stack.enter_context(patch.object(cli.setup_mod, "ensure_setup", lambda: self.config))
        clone = Mock()
        self.stack.enter_context(patch.object(cli, "cmd_clone", clone))
        cli._run(["clone", "group/sub"])
        clone.assert_called_once_with(self.config, "group/sub", self.config["master_dir"])
        clone.reset_mock()
        cli._run(["clone", "group/sub", str(self.tmp_path / "offsite")])
        clone.assert_called_once_with(self.config, "group/sub", str(self.tmp_path / "offsite/one.example.test"))
        clone.reset_mock()
        with self.assertRaises(paths.UnsafePath):
            cli._run(["clone", "group/sub", str(Path(self.config["base_dir"]) / "other.example.test/inside")])
        clone.assert_not_called()

    def test_remotes_cleanup_respects_audit_only_rows(self):
        import remotes

        root = self.config["master_dir"]
        scope = {"name": "One", "root": root}
        managed = str(Path(root) / "group/managed")
        offsite = str(self.tmp_path / "offsite/group/repo")
        repos = [offsite, managed]
        self.stack.enter_context(patch.object(cli, "_remotes_scope_and_repos",
                                       return_value=(scope, repos, {})))
        self.stack.enter_context(patch.object(cli.setup_mod, "ensure_setup", return_value=self.config))
        self.stack.enter_context(patch.object(cli, "_activate_instance"))

        for interactive in (True, False):
            for managed_leak in (False, True):
                with self.subTest(interactive=interactive, managed_leak=managed_leak):
                    rows = [
                        {"path": offsite, "has_creds": True, "leaks": ["origin"], "local_only": True},
                        {"path": managed, "has_creds": managed_leak,
                         "leaks": ["origin"] if managed_leak else [], "local_only": False},
                    ]
                    with (patch.object(remotes, "audit", return_value=rows) as audit,
                          patch.object(remotes, "print_audit") as report,
                          patch.object(remotes, "scrub", return_value={"cleaned": 0, "saved": 0, "failed": 0}) as scrub,
                          patch("builtins.input", return_value="y") as prompt,
                          redirect_stdout(io.StringIO()) as output):
                        if interactive:
                            cli._run_remotes(self.config)
                        else:
                            cli._run(["remotes", "--fix", str(self.tmp_path)])
                    audit.assert_called_once_with(repos, check_network=False, scope=scope)
                    report.assert_called_once_with(rows)
                    self.assertEqual((prompt.call_count, scrub.call_count),
                                     (int(interactive and managed_leak), int(managed_leak)))
                    if managed_leak:
                        scrub.assert_called_once_with([managed], save_to_store=True, scope=scope)
                    else:
                        self.assertNotIn("About to rewrite remote URLs", output.getvalue())

    def test_retry_menu_reads_managed_log_only(self):
        import clone

        root = Path(self.config["master_dir"])
        group = "group/sub"
        (root / group).mkdir(parents=True)
        legacy = Path(clone._error_log_path(str(root), group))
        legacy.write_text(json.dumps({"group": group, "errors": [{}]}))
        self.assertEqual(cli._retry_count(self.config, group, str(root)), 0)
        managed = Path(clone._error_log_path(str(root), group, managed=True))
        managed.write_text(json.dumps({"group": group, "errors": [{}, {}]}))
        self.assertEqual(cli._retry_count(self.config, group, str(root)), 2)

    def test_retry_is_selectable_without_existing_clones(self):
        with patch("builtins.input", side_effect=["4", "b"]):
            self.assertEqual(cli.pick_action("❌", has_errors=True), "4")

    def test_partial_group_inventory_does_not_label_omitted_projects_orphan(self):
        class PartialProjects(list):
            complete = False

        index = {("one.example.test", "Group/sub/repo"): "/unused"}
        with patch.object(cov, "classify_orphans") as classify:
            self.assertEqual(cli._instance_orphans(self.config, index, PartialProjects()), [])
            classify.assert_not_called()

    def test_doctor_layout_is_unknown_with_unresolved_worktrees(self):
        with patch.object(setup, "_mark") as mark:
            setup._layout_check("One", self.config, {
                "ok": True, "by_key": {}, "unresolved": 1,
            }, "one.example.test", [])
        self.assertTrue(mark.call_args_list)
        self.assertTrue(all(call.args[1] == "unknown" for call in mark.call_args_list))
