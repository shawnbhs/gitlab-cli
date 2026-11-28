import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import _error_log_guard  # noqa: F401

import api
import clone


class ManagedCloneTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "git.example.test"
        self.cfg = dict(url="https://git.example.test", layout_host=self.root.name,
                        base_dir=str(self.base), master_dir=str(self.root),
                        folder="old", token="", headers={})
        self.project = self.project_at("team/sub/repo")
        self.target = self.root / "team/sub/repo"
        self.output = io.StringIO()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.groups = self.stack.enter_context(patch.object(clone, "get_group", return_value={"id": 1, "full_path": "team/sub"}))
        self.projects = self.stack.enter_context(patch.object(clone, "get_projects", return_value=[self.project]))
        self.subgroups = self.stack.enter_context(patch.object(clone, "get_subgroups", return_value=[]))
        self.lookup = self.stack.enter_context(patch.object(clone.gitlab_api, "get_project", return_value=None))
        self.transfer = self.stack.enter_context(patch.object(clone.gitlib, "clone_repo_managed", autospec=True))
        self.transfer.return_value = {"status": "ok"}
        self.legacy = self.stack.enter_context(patch.object(clone, "_clone", return_value={"status": "ok"}))
        self.move = self.stack.enter_context(patch.object(clone, "try_rename_repair", return_value={"action": "none"}))

    def project_at(self, namespace, number=42):
        return dict(id=number, path=namespace.rsplit("/", 1)[-1],
                    path_with_namespace=namespace,
                    http_url_to_repo=f"{self.cfg['url']}/{namespace}.git")

    def run_clone(self, dest=None):
        result = clone.cmd_clone(self.cfg, "team/sub", str(dest or self.root))
        self.legacy.assert_not_called()
        self.move.assert_not_called()
        return result

    def git(self, directory, *args):
        return subprocess.run([clone.gitlib.GIT_BIN, "-C", str(directory), *args],
                              check=True, capture_output=True).stdout

    def repo(self, directory, url):
        directory.mkdir(parents=True)
        self.git(directory, "init", "-q")
        self.git(directory, "remote", "add", "origin", url)
        (directory / "untouched").write_text("local work")
        return self.git(directory, "config", "--local", "--list")

    def rows(self, root=None):
        return clone.load_errors(str(root or self.root), "team/sub", managed=True)

    def test_uses_full_namespace(self):
        self.run_clone()
        self.transfer.assert_called_once_with(
            "", self.project["http_url_to_repo"], str(self.target),
            instance_url=self.cfg["url"], host_root=str(self.root))

    def test_explicit_destination_is_the_transport_root(self):
        dest = self.base / "explicit" / self.root.name
        self.cfg["token"] = "fixture-token"
        self.assertIsNone(self.run_clone(dest))
        self.transfer.assert_called_once_with(
            "fixture-token", self.project["http_url_to_repo"],
            str(dest / "team/sub/repo"), instance_url=self.cfg["url"], host_root=str(dest))

    def test_managed_outcomes_never_report_incomplete_or_failed_as_success(self):
        self.stack.enter_context(patch.object(clone, "_session_stats", {"cloned": 0}))
        for status, field, value in (
            (clone.gitlib.S_SHALLOW, "reason", clone.gitlib.S_SHALLOW),
            (clone.gitlib.S_LFS, "reason", clone.gitlib.S_LFS),
            (clone.gitlib.S_AUTH, "code", "auth"),
            (clone.gitlib.S_GONE, "code", "not_found"),
            (clone.gitlib.S_ERROR, "code", "unknown"),
        ):
            with self.subTest(status=status):
                self.transfer.return_value = {"status": status}
                self.run_clone()
                row, = self.rows()
                self.assertEqual(row[field], value)
                self.assertEqual(clone._session_stats["cloned"], 0)
                self.assertNotIn("cloned into the canonical destination", self.output.getvalue())

    def test_occupied_destination_is_not_overwritten(self):
        self.target.mkdir(parents=True)
        marker = self.target / "notes.txt"
        marker.write_text("keep local notes")
        self.run_clone()
        self.assertTrue(self.rows())
        self.transfer.assert_not_called()
        self.assertEqual(marker.read_text(), "keep local notes")

    def test_two_hosts_and_repeated_basename_have_distinct_targets(self):
        subgroups = [dict(id=2, full_path="team/sub/a", path="a"),
                     dict(id=3, full_path="team/sub/b", path="b")]
        self.subgroups.side_effect = [subgroups, [], []] * 2
        for host in ("git.example.test", "other.example.test"):
            root = self.base / host
            self.cfg.update(url=f"https://{host}", layout_host=host, master_dir=str(root))
            by_id = {1: [], 2: [self.project_at("team/sub/a/repo")],
                     3: [self.project_at("team/sub/b/repo", 43)]}
            self.projects.side_effect = (
                lambda _url, _headers, group_id, by_id=by_id: by_id[group_id])
            self.run_clone(root)
        self.assertCountEqual([call.args[2] for call in self.transfer.call_args_list], [
            str(self.base / host / f"team/sub/{branch}/repo")
            for host in ("git.example.test", "other.example.test") for branch in ("a", "b")])

    def test_wrong_scope_or_unsafe_namespace_blocks(self):
        for namespace in ("team/other/repo", "team/sub/deeper/repo", "team/sub/../repo"):
            with self.subTest(namespace=namespace):
                self.projects.return_value = [self.project_at(namespace)]
                self.run_clone()
                self.transfer.assert_not_called()
                self.assertTrue(self.rows())

    def test_invalid_group_or_url_cannot_reach_transport(self):
        for url in ("https://other.example.test/team/sub/repo.git",
                    "http://git.example.test/team/sub/repo.git",
                    "https://git.example.test:8443/team/sub/repo.git",
                    "https://user@git.example.test/team/sub/repo.git",
                    "https://git.example.test/team/sub/other.git"):
            with self.subTest(url=url):
                self.project["http_url_to_repo"] = url
                self.run_clone()
                self.transfer.assert_not_called()
                self.assertNotIn(url, self.output.getvalue())
        self.groups.reset_mock()
        clone.cmd_clone(self.cfg, "", str(self.root))
        self.groups.assert_not_called()
        self.transfer.assert_not_called()

    def test_existing_target_requires_exact_project_identity(self):
        wrong = self.project_at("team/Sub/repo")["http_url_to_repo"]
        before = self.repo(self.target, wrong)
        self.run_clone()
        self.assertTrue(self.rows())
        self.assertEqual(self.git(self.target, "config", "--local", "--list"), before)
        self.git(self.target, "remote", "set-url", "origin", self.project["http_url_to_repo"])
        self.run_clone()
        self.assertFalse(self.rows())
        self.transfer.assert_not_called()

    def test_legacy_match_blocked_even_for_explicit_root(self):
        legacy = self.base / "old/team/sub/repo"
        before = self.repo(legacy, self.project["http_url_to_repo"])
        dest = self.base / "explicit" / self.root.name
        self.run_clone(dest)
        self.transfer.assert_not_called()
        self.assertIn("elsewhere", self.rows(dest)[0]["detail"])
        self.assertEqual(self.git(legacy, "config", "--local", "--list"), before)

    def test_unresolved_or_renamed_copy_never_recloned(self):
        old = self.project_at("previous-group/old-repo")
        legacy = self.base / "old/previous-group/old-repo"
        before = self.repo(legacy, old["http_url_to_repo"])
        for response, message in ((None, "Unresolved local clone inventory"),
                                  (self.project, "under an old namespace")):
            self.lookup.return_value = response
            self.run_clone()
            self.transfer.assert_not_called()
            self.assertIn(message, self.rows()[0]["detail"])
        self.assertEqual(self.git(legacy, "config", "--local", "--list"), before)
        self.assertFalse(self.target.exists())

    def test_known_distinct_copy_does_not_block_new_project(self):
        other = self.project_at("team/sub/other", 43)
        self.repo(self.root / "team/sub/other", other["http_url_to_repo"])
        self.projects.return_value = [self.project, other]
        self.run_clone()
        self.transfer.assert_called_once_with(
            "", self.project["http_url_to_repo"], str(self.target),
            instance_url=self.cfg["url"], host_root=str(self.root))

    def test_retry_uses_fresh_identity_and_ignores_logged_path_and_url(self):
        self.transfer.return_value = {"status": "error"}
        self.run_clone()
        log, = self.root.glob(".*_errors.json")
        saved = json.loads(log.read_text())
        saved["errors"][0].update(path=str(self.base / "unrelated"), url="https://elsewhere.test/repo.git")
        log.write_text(json.dumps(saved))
        self.transfer.reset_mock()
        self.transfer.return_value = {"status": "ok"}
        self.lookup.return_value = self.project
        clone.cmd_retry_failed(self.cfg, "team/sub", str(self.root))
        self.lookup.assert_called_once_with(self.cfg["url"], {}, 42)
        self.transfer.assert_called_once_with(
            "", self.project["http_url_to_repo"], str(self.target),
            instance_url=self.cfg["url"], host_root=str(self.root))
        self.assertFalse(self.rows())

    def test_retry_refuses_log_from_previous_instance_port(self):
        self.transfer.return_value = {"status": "error"}
        self.run_clone()
        log, = self.root.glob(".managed-*_errors.json")
        saved = json.loads(log.read_text())
        saved["authority"] = ["https", self.root.name, 8443]
        log.write_text(json.dumps(saved))
        before = log.read_bytes()
        self.groups.reset_mock()
        self.transfer.reset_mock()

        self.assertIsNone(clone.cmd_retry_failed(self.cfg, "team/sub", str(self.root)))

        self.groups.assert_not_called()
        self.lookup.assert_not_called()
        self.transfer.assert_not_called()
        self.assertEqual(log.read_bytes(), before)
        self.assertIn("different GitLab authority", self.output.getvalue())

    def test_retry_defers_incomplete_saved_namespace_without_lookup(self):
        self.transfer.return_value = {"status": "error"}
        self.run_clone()
        log, = self.root.glob(".managed-*_errors.json")
        saved = json.loads(log.read_text())
        saved["errors"][0]["namespace"] = "team/sub/"
        log.write_text(json.dumps(saved))
        self.transfer.reset_mock()

        clone.cmd_retry_failed(self.cfg, "team/sub", str(self.root))

        self.lookup.assert_not_called()
        self.transfer.assert_not_called()
        self.assertTrue(self.rows())

    def test_retry_changed_or_unavailable_project_stays_blocked(self):
        self.transfer.return_value = {"status": "error"}
        self.run_clone()
        self.transfer.reset_mock()
        for response in (None, self.project_at("team/sub/renamed"), self.project_at("team/sub/repo", 43)):
            self.lookup.return_value = response
            clone.cmd_retry_failed(self.cfg, "team/sub", str(self.root))
            self.assertTrue(self.rows())
        self.transfer.assert_not_called()
        self.legacy.assert_not_called()

    def test_incomplete_listing_blocks_even_listed_project(self):
        self.projects.return_value = api.PagedResult([self.project], complete=False)
        self.run_clone()
        self.transfer.assert_not_called()
        self.assertTrue(self.rows())

    def test_symlink_target_is_not_followed(self):
        other = self.base / "elsewhere"
        other.mkdir()
        self.target.parent.mkdir(parents=True)
        self.target.symlink_to(other, target_is_directory=True)
        self.run_clone()
        self.transfer.assert_not_called()
        self.assertTrue(self.rows())
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(list(other.iterdir()), [])

    def test_missing_strict_transport_does_not_fall_back(self):
        with patch.object(clone.gitlib, "clone_repo_managed", None):
            self.run_clone()
        self.assertTrue(self.rows())
        self.transfer.assert_not_called()

    def test_managed_rename_and_prune_do_not_reach_legacy(self):
        clone.reconcile_renames(self.cfg, "team/sub", str(self.root))
        clone.prune_leftovers(self.cfg, "team/sub", str(self.root))
        self.groups.assert_not_called()
        self.move.assert_not_called()

    def test_alternate_remote_on_legacy_copy_blocks_duplicate(self):
        legacy = self.base / "old/team/sub/repo"
        self.repo(legacy, "https://other.example.test/team/sub/repo.git")
        self.git(legacy, "remote", "add", "gitlab", self.project["http_url_to_repo"])
        self.run_clone()
        self.assertTrue(self.rows())
        self.transfer.assert_not_called()

    def test_retry_keeps_incomplete_reason_after_repeated_deferrals(self):
        self.transfer.return_value = {"status": clone.S_LFS}
        self.run_clone()
        self.repo(self.target, self.project["http_url_to_repo"])
        self.lookup.return_value = self.project
        self.transfer.reset_mock()
        for _ in range(2):
            clone.cmd_retry_failed(self.cfg, "team/sub", str(self.root))
            self.assertEqual(self.rows()[0]["reason"], clone.S_LFS)
        self.transfer.assert_not_called()

    def test_resume_defers_changed_namespace_even_without_old_clone(self):
        self.transfer.return_value = {"status": "error"}
        self.run_clone()
        self.transfer.reset_mock()
        self.projects.return_value = [self.project_at("team/sub/renamed")]
        self.run_clone()
        self.assertTrue(any(row.get("namespace") == "team/sub/repo" for row in self.rows()))
        self.assertIn("namespace changed", self.output.getvalue())
        self.transfer.assert_not_called()

    def test_existing_checkout_with_symlink_git_metadata_is_not_present(self):
        actual = self.base / "independent"
        self.repo(actual, self.project["http_url_to_repo"])
        self.target.mkdir(parents=True)
        (self.target / ".git").symlink_to(actual / ".git", target_is_directory=True)
        self.run_clone()
        self.assertTrue(self.rows())
        self.transfer.assert_not_called()
