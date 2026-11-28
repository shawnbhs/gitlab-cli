"""Offline migration preview: no real Git configuration or remote access."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _error_log_guard  # noqa: F401


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "preview_host_layout.py"


def stamp(path):
    stat = path.lstat()
    return [stat.st_dev, stat.st_ino, stat.st_ctime_ns, stat.st_mtime_ns, stat.st_size]


def inventory_rows(*rows):
    return {"complete": True, "count": len(rows), "rows": list(rows)}


def snapshot(root):
    return {
        path.relative_to(root).as_posix(): (
            path.lstat().st_mode,
            stamp(path),
            os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None,
        )
        for path in root.rglob("*")
    }


class HostLayoutPreviewTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.base = self.root / "MasterGroups"
        self.base.mkdir()
        self.manifest = self.root / "inventory.json"

    def project(self, host="first.example", project_id=7, namespace="group/team/repo"):
        return {"host": host, "id": project_id, "path_with_namespace": namespace}

    def source(self, relative, project, origin_namespace=None):
        repo = self.base / relative
        git_dir = repo / ".git"
        git_dir.mkdir(parents=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        (git_dir / "config").write_text("[branch \"main\"]\n\tremote = origin\n\tmerge = refs/heads/main\n")
        (repo / "local-changes").write_bytes(b"untracked work")
        return {"path": relative, "host": project["host"], "id": project["id"], "proof": {
            "kind": "gitlab_project_lookup",
            "host": project["host"],
            "id": project["id"],
            "origin_namespace": origin_namespace or project["path_with_namespace"],
            "lookup_namespace": project["path_with_namespace"],
            "checkout": stamp(repo),
            "git_dir": stamp(git_dir),
            "git_config": stamp(git_dir / "config"),
        }}

    def preview(self, projects, sources, *, digest=None):
        inventory = {"version": 1, "base": str(self.base), "projects": projects, "sources": sources}
        data = json.dumps(inventory).encode()
        self.manifest.write_bytes(data)
        before = snapshot(self.root)
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--base", str(self.base), "--inventory", str(self.manifest),
             "--inventory-sha256", hashlib.sha256(data).hexdigest() if digest is None else digest],
            cwd=self.root, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(result.stderr, "")
        return result.returncode, json.loads(result.stdout)

    def test_complete_inventory_keeps_hosts_distinct_and_reconciles_rename_by_id(self):
        first = self.project()
        second = self.project(host="second.example")
        third = self.project(project_id=8, namespace="other/repo")
        sources = inventory_rows(
            self.source("legacy/renamed", first, "old/team/renamed"),
            self.source("legacy/second", second),
            self.source("first.example/other/repo", third),
        )
        code, report = self.preview(inventory_rows(first, second, third), sources)
        self.assertEqual(code, 0)
        self.assertEqual(report["counts"], {"eligible": 2, "already_canonical": 1, "blocked": 0})
        self.assertEqual([(row["host"], row["id"], row["destination"]) for row in report["rows"]], [
            ("first.example", 7, "first.example/group/team/repo"),
            ("first.example", 8, "first.example/other/repo"),
            ("second.example", 7, "second.example/group/team/repo"),
        ])
        self.assertEqual(report["rows"][0]["origin_namespace"], "old/team/renamed")

    def test_incomplete_inventory_blocks_entire_preview(self):
        project = self.project()
        sources = inventory_rows(self.source("legacy/repo", project))
        for section in ({**sources, "complete": False}, {**sources, "count": 2}):
            with self.subTest(section=section):
                code, report = self.preview(inventory_rows(project), section)
                self.assertEqual((code, report["blockers"]), (2, ["incomplete_source_inventory"]))
                self.assertEqual(report["counts"]["eligible"], 0)

    def test_inventory_must_match_trusted_digest(self):
        code, report = self.preview(inventory_rows(self.project()), inventory_rows(), digest="0" * 64)
        self.assertEqual((code, report["blockers"]), (2, ["inventory_digest_mismatch"]))

    def test_unlisted_source_is_unproven_not_asserted_missing(self):
        code, report = self.preview(inventory_rows(self.project()), inventory_rows())
        self.assertEqual(code, 1)
        self.assertEqual(report["counts"], {"eligible": 0, "already_canonical": 0, "blocked": 1})
        self.assertEqual(report["rows"], [{"host": "first.example", "id": 7, "status": "blocked",
                                          "blockers": ["source_unproven"]}])

    def test_unknown_malformed_and_mismatched_source_identity_is_blocked(self):
        project = self.project()
        source = self.source("legacy/repo", project)
        mismatched_host = {**source, "proof": {**source["proof"], "host": "second.example"}}
        mislabeled_origin = {**source, "proof": {**source["proof"], "lookup_namespace": "wrong/repo"}}
        for item, reason in (({**source, "id": 999}, "unknown_project_identity"),
                             ({**source, "id": True}, "invalid_source_identity"),
                             ({**source, "proof": None}, "source_provenance_unverified"),
                             (mismatched_host, "source_provenance_unverified"),
                             (mislabeled_origin, "source_provenance_unverified")):
            with self.subTest(reason=reason):
                code, report = self.preview(inventory_rows(project), inventory_rows(item))
                self.assertEqual(code, 1)
                row = next(row for row in report["rows"] if row.get("source_row") == 1)
                self.assertIn(reason, row["blockers"])
                self.assertEqual(report["counts"]["eligible"], 0)

    def test_recycled_or_duplicate_origin_blocks_both_sources(self):
        first = self.project()
        second = self.project(project_id=8, namespace="new/repo")
        sources = inventory_rows(self.source("legacy/first", first),
                                 self.source("legacy/second", second, first["path_with_namespace"]))
        code, report = self.preview(inventory_rows(first, second), sources)
        self.assertEqual((code, report["counts"]["blocked"]), (1, 2))
        for row in report["rows"]:
            self.assertIn("conflicting_origin_identity", row["blockers"])

    def test_duplicate_source_id_blocks_both_sources(self):
        project = self.project()
        code, report = self.preview(inventory_rows(project), inventory_rows(
            self.source("legacy/first", project), self.source("legacy/second", project)))
        self.assertEqual((code, report["counts"]["blocked"]), (1, 2))
        self.assertTrue(all("duplicate_project_identity" in row["blockers"] for row in report["rows"]))

    def test_casefold_duplicate_destination_blocks_both_sources(self):
        first = self.project()
        second = self.project(project_id=8, namespace="Group/team/repo")
        code, report = self.preview(inventory_rows(first, second), inventory_rows(
            self.source("legacy/first", first), self.source("legacy/second", second)))
        self.assertEqual((code, report["counts"]["blocked"]), (1, 2))
        self.assertTrue(all("duplicate_destination" in row["blockers"] for row in report["rows"]))

    def test_config_change_invalidates_proof_without_reading_credentials(self):
        project = self.project()
        source = self.source("legacy/repo", project)
        config = self.base / "legacy/repo/.git/config"
        secret = "fixture-no-disclosure"
        config.write_text(f"[remote \"origin\"]\nurl = https://user:{secret}@example.test/g/p.git\n")
        code, report = self.preview(inventory_rows(project), inventory_rows(source))
        self.assertEqual(code, 1)
        self.assertIn("stale_source_provenance", report["rows"][0]["blockers"])
        self.assertNotIn(secret, json.dumps(report))

    def test_traversal_source_is_rejected_without_echoing_payload(self):
        project = self.project()
        source = {**self.source("legacy/repo", project), "path": "../https://fixture-secret@example.test/g/p"}
        code, report = self.preview(inventory_rows(project), inventory_rows(source))
        self.assertEqual(code, 1)
        self.assertIn("invalid_source_path", report["rows"][0]["blockers"])
        self.assertNotIn("fixture-secret", json.dumps(report))

    def test_symlink_parent_does_not_select_source_outside_base(self):
        project = self.project()
        source = self.source("actual/repo", project)
        (self.base / "link").symlink_to(self.base / "actual", target_is_directory=True)
        source["path"] = "link/repo"
        code, report = self.preview(inventory_rows(project), inventory_rows(source))
        self.assertEqual(code, 1)
        self.assertIn("path_not_directory_or_symlink", report["rows"][0]["blockers"])

    def test_existing_dangling_destination_blocks_rename(self):
        project = self.project()
        source = self.source("legacy/repo", project)
        destination = self.base / project["host"] / project["path_with_namespace"]
        destination.parent.mkdir(parents=True)
        destination.symlink_to(self.root / "nonexistent")
        code, report = self.preview(inventory_rows(project), inventory_rows(source))
        self.assertEqual(code, 1)
        self.assertIn("destination_exists", report["rows"][0]["blockers"])

    def test_source_under_unlisted_checkout_is_rejected(self):
        project = self.project()
        source = self.source("legacy/parent/nested", project)
        (self.base / "legacy/parent/.git").mkdir()
        code, report = self.preview(inventory_rows(project), inventory_rows(source))
        self.assertEqual(code, 1)
        self.assertIn("nested_checkout", report["rows"][0]["blockers"])

    def test_worktree_gitdir_and_main_with_linked_worktree_are_blocked(self):
        project = self.project()
        source = self.source("legacy/main", project)
        (self.base / "legacy/main/.git/worktrees").mkdir()
        linked = self.base / "legacy/linked"
        linked.mkdir()
        (linked / ".git").write_text("gitdir: ../main/.git/worktrees/linked\n")
        for item in (source, {**source, "path": "legacy/linked"}):
            with self.subTest(path=item["path"]):
                code, report = self.preview(inventory_rows(project), inventory_rows(item))
                self.assertEqual(code, 1)
                self.assertIn("linked_or_indirect_git_metadata", report["rows"][0]["blockers"])

    def test_unrecognized_arguments_do_not_echo_value(self):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--base", str(self.base),
                                 "--inventory", str(self.manifest), "--inventory-sha256", "0" * 64,
                                 "--apply=https://fixture-secret@example.test/"],
                                cwd=self.root, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("fixture-secret", result.stdout + result.stderr)


    def test_other_hosts_canonical_subtree_is_not_a_legacy_source(self):
        first = self.project()
        second = self.project(host="second.example", namespace="other/repo")
        sources = inventory_rows(self.source("second.example/old/repo", first, "old/repo"),
                                 self.source("second.example/other/repo", second))
        code, report = self.preview(inventory_rows(first, second), sources)
        self.assertEqual((code, report["counts"]), (1, {"eligible": 0, "already_canonical": 1, "blocked": 1}))
        self.assertIn("source_under_other_host", report["rows"][0]["blockers"])

    def test_malformed_duplicate_project_row_cannot_make_valid_peer_eligible(self):
        project = self.project()
        malformed = {**project, "unexpected": "ignored"}
        code, report = self.preview(inventory_rows(project, malformed), inventory_rows(self.source("legacy/repo", project)))
        self.assertEqual((code, report["counts"]["eligible"]), (1, 0))
        row = next(row for row in report["rows"] if row.get("source_row") == 1)
        self.assertIn("duplicate_project_identity", row["blockers"])

    def test_unreachable_destination_with_overlong_leaf_is_blocked(self):
        project = self.project(namespace="group/" + "x" * 256)
        code, report = self.preview(inventory_rows(project), inventory_rows(self.source("legacy/repo", project)))
        self.assertEqual(code, 1)
        self.assertIn("destination_path_too_long", report["rows"][0]["blockers"])

    def test_mount_device_mismatch_is_refused(self):
        # Boundary substitute for a mount: no root, mount or external tree needed.
        import importlib.util
        from types import SimpleNamespace
        from unittest.mock import patch

        spec = importlib.util.spec_from_file_location("preview_host_layout", SCRIPT)
        preview = importlib.util.module_from_spec(spec)
        old_sys_path = sys.path[:]
        previous_bytecode = sys.dont_write_bytecode
        try:
            sys.dont_write_bytecode = True
            spec.loader.exec_module(preview)
        finally:
            sys.path[:] = old_sys_path
            sys.dont_write_bytecode = previous_bytecode
        project = self.project()
        source = self.source("legacy/repo", project)
        host_root = self.base / project["host"]
        host_root.mkdir()
        manifest = {"version": 1, "base": str(self.base), "projects": inventory_rows(project),
                    "sources": inventory_rows(source)}
        before = snapshot(self.root)
        original_lstat = Path.lstat

        def lstat(path):
            entry = original_lstat(path)
            if path == host_root:
                return SimpleNamespace(st_dev=entry.st_dev + 1, st_mode=entry.st_mode)
            return entry

        with patch.object(Path, "lstat", lstat):
            row, = preview.build_rows(manifest, self.base)
        self.assertEqual(row["blockers"], ["cross_device"])
        self.assertEqual(snapshot(self.root), before)

    def test_base_inside_parent_checkout_does_not_hide_nesting(self):
        project = self.project()
        source = self.source("legacy/repo", project)
        (self.root / ".git").mkdir()
        code, report = self.preview(inventory_rows(project), inventory_rows(source))
        self.assertEqual(code, 1)
        self.assertIn("nested_checkout", report["rows"][0]["blockers"])
