"""Offline regression for managed remote ownership."""

import json
from pathlib import Path
from unittest.mock import patch

import _bootstrap
import paths
import remotes


class TestHostLayoutRemotes(_bootstrap.TempTreeTestCase):
    def setUp(self):
        super().setUp()
        self.config = Path(self.tmp, "instances.json")
        self.instances = [
            {"name": "One", "url": "https://ONE.example", "master_dir": self.dest,
             "folder": "legacy"},
            {"name": "Two", "url": "https://two.example", "master_dir": self.dest},
        ]
        config = patch.object(paths, "INSTANCES_FILE", str(self.config))
        config.start()
        self.addCleanup(config.stop)
        self.write_instances()

    def write_instances(self):
        self.config.write_text(json.dumps({"active": "One", "instances": self.instances}),
                               encoding="utf-8")

    def add_remote(self, path, name, host, project):
        result = _bootstrap.git("remote", "add", name,
                                f"https://fixture:example@{host}/{project}.git", cwd=path)
        self.assertEqual(result.returncode, 0)

    def repo(self, relative, host, project="team/sub/app"):
        path = self.make_repo(relative)
        self.add_remote(path, "origin", host, project)
        return path

    def test_audit_and_scrub_own_only_exact_host_and_namespace(self):
        good = self.repo("one.example/team/sub/app", "one.example")
        self.add_remote(good, "mirror", "two.example", "team/sub/app")
        self.add_remote(good, "sibling", "one.example", "team/other/app")
        self.add_remote(good, "other-port", "one.example:8443", "team/sub/app")
        foreign = self.repo("two.example/team/sub/app", "two.example")
        legacy = self.repo("legacy/team/sub/app", "one.example")
        misplaced = self.repo("one.example/team/other/app", "one.example")
        wrong_host = self.repo("one.example/team/sub/other", "two.example", "team/sub/other")
        repos = [good, foreign, legacy, misplaced, wrong_host]
        protected = {Path(p, ".git", "config") for p in repos[1:]}
        before = {p: p.read_bytes() for p in protected}

        self.assertEqual(remotes.active_scope()["root"], str(Path(self.dest, "one.example")))
        rows = remotes.audit(repos)
        self.assertEqual([row["path"] for row in rows], [good])
        self.assertEqual(rows[0]["leaks"], ["origin"])
        self.assertEqual(rows.dropped, 4)
        preview = remotes.scrub(repos, save_to_store=False, dry_run=True)
        self.assertEqual((preview["scanned"], preview["cleaned"]), (1, 1))
        with patch.object(remotes, "harden_config", return_value=True):
            actual = remotes.scrub(repos, save_to_store=False)
        self.assertEqual((actual["scanned"], actual["cleaned"]), (1, 1))
        self.assertEqual({p: p.read_bytes() for p in protected}, before)
        urls = remotes.read_remote_config(good)
        self.assertFalse(remotes.has_creds(urls["origin"]["url"]))
        for name in ("mirror", "sibling", "other-port"):
            self.assertTrue(remotes.has_creds(urls[name]["url"]))

    def test_invalid_config_never_broadens_scope(self):
        repo = self.repo("one.example/team/sub/app", "one.example")
        config = Path(repo, ".git", "config")
        before = config.read_bytes()
        invalid = ["{", json.dumps({"active": "missing", "instances": self.instances}),
                   json.dumps({"active": "One", "instances": [*self.instances, {
                       "name": "Conflict", "url": "https://one.example:8443",
                       "master_dir": self.dest,
                   }]})]
        for index, text in enumerate(invalid):
            with self.subTest(case=index):
                self.config.write_text(text, encoding="utf-8")
                self.assertEqual(remotes.audit([repo]), [])
                stats = remotes.scrub([repo], save_to_store=False, enforce_scope=False)
                self.assertEqual(stats["scanned"], 0)
                self.assertEqual(config.read_bytes(), before)

    def test_audit_cannot_use_previously_active_scope(self):
        repo = self.repo("one.example/team/sub/app", "one.example")
        previous = remotes.active_scope()
        self.config.write_text(json.dumps({"active": "Two", "instances": self.instances}),
                               encoding="utf-8")
        rows = remotes.audit([repo], scope=previous, enforce_scope=False)
        self.assertEqual(len(rows), 0)
        self.assertEqual(rows.dropped, 1)

    def test_audit_rejects_incomplete_explicit_scope(self):
        repo = self.repo("two.example/team/sub/app", "two.example")
        rows = remotes.audit([repo], scope={"name": "One"})
        self.assertEqual(len(rows), 0)
        self.assertEqual(rows.dropped, 1)

    def test_invalid_base_does_not_read_as_default_root(self):
        repo = self.repo("one.example/team/sub/app", "one.example")
        for bad_base in (False, [], " "):
            with self.subTest(base_type=type(bad_base).__name__):
                self.instances[0]["master_dir"] = bad_base
                self.write_instances()
                self.assertTrue(remotes.active_scope().get("blocked"))
                self.assertEqual(len(remotes.audit([repo])), 0)

    def test_scope_rechecks_remote_row_on_repair(self):
        repo = self.repo("one.example/team/sub/app", "one.example")
        before = Path(repo, ".git", "config").read_bytes()
        # Model settings changed between the identity lookup and reading rows.
        original = remotes.list_remotes(repo, reveal=True)
        original[0]["url"] = original[0]["url"].replace("one.example", "two.example")
        with patch.object(remotes, "list_remotes", return_value=original):
            with patch.object(remotes, "harden_config") as harden:
                stats = remotes.scrub([repo], save_to_store=False)
        self.assertEqual(stats["cleaned"], 0)
        self.assertFalse(harden.called)
        self.assertTrue(Path(repo, ".git", "config").read_bytes() == before)

    def test_missing_configuration_after_audit_blocks_repair(self):
        repo = self.repo("one.example/team/sub/app", "one.example")
        rows = remotes.audit([repo])
        self.config.unlink()
        before = Path(repo, ".git", "config").read_bytes()
        stats = remotes.scrub([repo], save_to_store=False, scope=rows.scope)
        self.assertEqual(stats["scanned"], 0)
        self.assertTrue(Path(repo, ".git", "config").read_bytes() == before)

    def test_offsite_audit_does_not_allow_probe_or_repair(self):
        repo = self.repo("../offsite/team/sub/app", "one.example")
        before = Path(repo, ".git", "config").read_bytes()
        with patch.object(remotes, "_probe") as probe:
            rows = remotes.audit([repo], check_network=True)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["has_creds"])
        self.assertIn("0 of 1", remotes.confirm_scrub_message(rows))
        probe.assert_not_called()
        stats = remotes.scrub([repo], save_to_store=False)
        self.assertEqual(stats["scanned"], 0)
        self.assertTrue(Path(repo, ".git", "config").read_bytes() == before)

    def test_scrub_stops_before_rewrite_if_instance_switches_while_saving(self):
        repo = self.repo("one.example/team/sub/app", "one.example")
        scope = remotes.active_scope()
        config = Path(repo, ".git", "config")
        before = config.read_bytes()

        def save(*_):
            self.config.write_text(json.dumps({"active": "Two", "instances": self.instances}),
                                   encoding="utf-8")
            return True

        with (patch.object(remotes, "_credential_approve", side_effect=save) as approve,
              patch.object(remotes, "harden_config") as harden):
            stats = remotes.scrub([repo], scope=scope)

        approve.assert_called_once()
        harden.assert_not_called()
        self.assertEqual((stats["saved"], stats["cleaned"], stats["failed"]), (1, 0, 1))
        self.assertEqual(config.read_bytes(), before)

    def test_scope_rechecks_identity_on_repair(self):
        repo = self.repo("one.example/team/sub/app", "one.example")
        rows = remotes.audit([repo])
        self.assertEqual(len(rows), 1)
        result = _bootstrap.git("remote", "set-url", "origin",
                                "https://fixture:example@two.example/team/sub/app.git", cwd=repo)
        self.assertEqual(result.returncode, 0)
        config = Path(repo, ".git", "config")
        before = config.read_bytes()
        stats = remotes.scrub([repo], save_to_store=False, scope=rows.scope)
        self.assertEqual(stats["scanned"], 0)
        self.assertEqual(config.read_bytes(), before)
