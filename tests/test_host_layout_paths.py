import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _error_log_guard  # noqa: F401

import paths


class HostLayoutPathsTests(unittest.TestCase):
    def test_host_is_canonical_dns_or_ipv4(self):
        for url, host in (
            ("HTTPS://LAB.Example.:443/", "lab.example"),
            ("https://bücher.example", "xn--bcher-kva.example"),
            ("https://faß.example", "xn--fa-hia.example"),
            ("http://lab.internal", "lab.internal"),
            ("http://192.0.2.1:8080", "192.0.2.1"),
        ):
            with self.subTest(host=host):
                self.assertEqual(paths.gitlab_host(url), host)

    def test_host_rejects_unclear_unsafe_or_non_instance_urls(self):
        for url in (
            "", "lab.example", "ssh://lab.example", "https://",
            "https://lab.example//", "https://lab.example/group",
            "https://user:placeholder@lab.example", "https://lab.example?",
            "https://lab.example/#", "https://lab.example:",
            "https://lab.example:0", "https://lab.example:65536",
            "https://[::1]", "https://lab..example", "https://lab_example",
            "https://127.1", "https://001.0.0.1", "https://0x7F000001",
            "https://%6Cab.example", " https://lab.example", "https://lab\n.example",
            "https://lab.example\\dir", "https://NUL.example",
        ):
            with self.subTest(url=url):
                with self.assertRaises(paths.UnsafePath) as raised:
                    paths.gitlab_host(url)
                self.assertNotIn("placeholder", str(raised.exception))

    def test_exact_path_is_anchored_and_host_isolated(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(paths, "INSTALL_ROOT", tmp):
            base = Path(tmp) / "MasterGroups"
            first = paths.managed_root("MasterGroups", "https://a.example")
            second = paths.managed_root("MasterGroups", "https://b.example")
            self.assertEqual(first, str(base / "a.example"))
            self.assertEqual(second, str(base / "b.example"))
            (Path(first) / "Repo").mkdir(parents=True)  # Legacy basename must not win.
            relative = "a.example/Sub/Repo"
            repo = paths.managed_path(first, relative)
            Path(repo).mkdir(parents=True)
            self.assertEqual(repo, str(base / "a.example/a.example/Sub/Repo"))
            self.assertEqual(paths.managed_namespace(first, repo), relative)
            with self.assertRaises(paths.UnsafePath):
                paths.managed_namespace(second, repo)
            with self.assertRaises(paths.UnsafePath):
                paths.managed_namespace(first, Path(first) / "Repo")

    def test_managed_path_never_collapses_invalid_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            for namespace in ("", "/x/y", "x/y/", "x//y", "x/./y", "x/../y",
                              "x/%2e%2e", "x/C:foo", "x\\y", "x/y ", "x/y\x00",
                              "x/aux.txt", "x/y.", "x/.git"):
                with self.subTest(namespace=namespace), self.assertRaises(paths.UnsafePath):
                    paths.managed_path(tmp, namespace)

    def test_managed_resolution_rejects_symlinks_even_inside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            real = base / "real"
            real.mkdir()
            (real / "repo").mkdir()
            host = base / "lab.example"
            host.symlink_to(real, target_is_directory=True)
            with self.assertRaises(paths.UnsafePath):
                paths.managed_root(base, "https://lab.example")
            with self.assertRaises(paths.UnsafePath):
                paths.managed_path(host, "team/repo")
            with self.assertRaises(paths.UnsafePath):
                paths.managed_root(host, "https://other.example")
            (base / "alias").symlink_to(real, target_is_directory=True)
            (base / "dangling").symlink_to(base / "missing", target_is_directory=True)
            for group in ("alias", "dangling"):
                repo = f"{group}/repo"
                with self.assertRaises(paths.UnsafePath):
                    paths.managed_path(base, repo)
                with self.assertRaises(paths.UnsafePath):
                    paths.managed_namespace(base, base / repo)
            with self.assertRaises(paths.UnsafePath):
                paths.managed_namespace(base, str(base / "alias/../real/repo"))

    def test_remote_identity_preserves_exact_namespace(self):
        expected = ("lab.example", "Group/Sub/Repo")
        for url in (
            "https://lab.example/Group/Sub/Repo.git",
            "http://user:p%40ss@LAB.EXAMPLE.:8080/Group/Sub/Repo",
            "ssh://git@lab.example:2222/Group/Sub/Repo.git",
            "git@lab.example:Group/Sub/Repo.git",
            "lab.example:Group/Sub/Repo.git",
        ):
            with self.subTest(url=url):
                self.assertEqual(paths.remote_identity(url), expected)
        self.assertEqual(paths.remote_identity("https://lab.example/group/Repo.git.git"),
                         ("lab.example", "group/Repo.git"))
        self.assertNotEqual(paths.remote_identity("https://lab.example/group/Sub/Repo.git"), expected)

    def test_remote_identity_rejects_unclear_or_unsupported_transport(self):
        for url in (
            None, "", "../Group/Repo", "C:/Group/Repo", "C:Group/Repo",
            "file:///Group/Repo", "git://lab.example/Group/Repo",
            "ext::helper Group/Repo", "https::lab.example/Group/Repo",
            "git@lab.example:/Group/Repo", "git@lab.example:~/Group/Repo",
            "https://lab.example/Group", "https://lab.example/Group//Repo",
            "https://lab.example/Group/Repo/", "https://lab.example//Group/Repo",
            "https://lab.example/Group/../Repo", "https://lab.example/Group/%252e%252e",
            "https://lab.example/Group%2fSub/Repo", "https://lab.example/Group/Repo?",
            "https://a@b@lab.example/Group/Repo", "https://lab.example:/Group/Repo",
            "ssh://-bad@lab.example/Group/Repo", "ssh://bad:pass@lab.example/Group/Repo",
            "https://[::1]/Group/Repo", "git@lab.example:Group/Repo\n",
        ):
            with self.subTest(url=url):
                self.assertIsNone(paths.remote_identity(url))


class HostLayoutConfigTests(unittest.TestCase):
    def setUp(self):
        import instances

        self.instances = instances
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.config_file = tmp / "config/instances.json"
        self.stack.enter_context(patch.object(instances, "CONFIG_DIR", str(self.config_file.parent)))
        self.stack.enter_context(patch.object(instances, "INSTANCES_FILE", str(self.config_file)))
        self.stack.enter_context(patch.object(paths, "INSTALL_ROOT", str(tmp)))
        self.token = self.stack.enter_context(patch.object(instances.creds, "resolve_token", return_value=""))
        self.record = {"name": "Friendly", "url": "https://lab.example/", "master_dir": "tree/team",
                       "folder": "team", "user_namespaces": ["example"], "min_access_level": 20}

    def test_config_round_trip_keeps_exact_base_and_legacy_folder(self):
        cfg = self.instances.build_cfg(self.record)
        base = Path(paths.INSTALL_ROOT) / "tree/team"
        self.assertEqual(cfg["layout_host"], "lab.example")
        self.assertEqual(cfg["base_dir"], str(base))
        self.assertEqual(cfg["master_dir"], str(base / "lab.example"))
        self.assertEqual(cfg["folder"], "team")
        self.assertEqual(cfg, self.instances.build_cfg(cfg))
        self.assertEqual(self.instances.unknown_instance_keys(cfg), [])
        self.instances._save({"instances": [dict(cfg)], "active": "Friendly"})
        persisted = self.instances._load()["instances"][0]
        self.assertEqual(persisted["master_dir"], "tree/team")
        self.assertEqual(persisted["folder"], "team")
        self.assertNotIn("layout_host", persisted)
        self.assertNotIn("base_dir", persisted)
        self.assertNotIn("token", persisted)
        self.assertEqual(self.instances.get_active_instance(), cfg)
        self.instances._save({"instances": [persisted], "active": "Friendly"})
        self.assertEqual(self.instances.get_active_instance(), cfg)
        without_folder = self.instances.build_cfg({"name": "Other", "url": "http://b.example"})
        self.assertEqual(without_folder["master_dir"], str(Path(paths.INSTALL_ROOT) / "MasterGroups/b.example"))

    def test_identical_authority_aliases_allowed_but_conflicting_ones_fail_closed(self):
        alias = dict(self.record, name="Alias", url="HTTPS://LAB.EXAMPLE.:443")
        data = {"instances": [dict(self.record), alias], "active": "Alias"}
        self.instances._save(data)
        self.assertEqual(self.instances.get_active_instance()["layout_host"], "lab.example")
        snapshot = self.config_file.read_bytes()
        for url in ("https://lab.example:8443", "http://lab.example:443", "http://lab.example"):
            with self.subTest(url=url), self.assertRaisesRegex(self.instances.InstancesFileError, "lab.example"):
                self.instances._save(dict(data, instances=[self.record, dict(alias, url=url)]))
            self.assertEqual(self.config_file.read_bytes(), snapshot)
        # Direct edits bypass _save; loading must still reject collisions.
        import json
        for url in ("https://lab.example:8443", "http://lab.example"):
            with self.subTest(load=url):
                self.config_file.write_text(json.dumps(dict(data, instances=[self.record, dict(alias, url=url)])),
                                            encoding="utf-8")
                self.assertIn("lab.example", self.instances.instances_file_problem())
                self.assertEqual(self.instances._read_instances()[0]["instances"], [])
                self.assertIsNone(self.instances.get_active_instance())

    def test_standalone_host_root_ignores_authority_so_config_guard_owns_it(self):
        urls = ("https://lab.example", "https://lab.example:8443", "http://lab.example")
        with tempfile.TemporaryDirectory() as tmp:
            roots = {paths.managed_root(tmp, url) for url in urls}
        self.assertEqual(roots, {str(Path(tmp) / "lab.example")})
        self.assertEqual(len({paths.gitlab_authority(url) for url in urls}), 3)
        for url in urls[1:]:
            with self.subTest(url=url):
                self.assertIn("lab.example", self.instances._authority_problem(
                    [{"url": urls[0]}, {"url": url}]))

    def test_invalid_url_is_not_echoed_and_never_resolves_token(self):
        bad = dict(self.record, url="https://user:placeholder@lab.example")
        with self.assertRaises(paths.UnsafePath) as exc:
            self.instances.build_cfg(bad)
        self.assertNotIn("placeholder", str(exc.exception))
        self.token.assert_not_called()
        with self.assertRaises(self.instances.InstancesFileError) as exc:
            self.instances._save({"instances": [bad]})
        self.assertNotIn("placeholder", str(exc.exception))
        self.assertFalse(self.config_file.exists())

    def test_add_conflicting_url_stops_before_token_or_probe(self):
        for url in ("https://lab.example:8443", "http://lab.example"):
            data = {"instances": [dict(self.record)], "active": "Friendly"}
            with (self.subTest(url=url),
                  patch.object(self.instances, "_ask", return_value=url),
                  patch.object(self.instances.creds, "prompt_token") as ask_token,
                  patch.object(self.instances.caps, "probe") as probe,
                  patch.object(self.instances, "print_warn") as warn):
                self.assertIsNone(self.instances.add_instance(data, quiet_header=True))
                ask_token.assert_not_called()
                probe.assert_not_called()
                self.assertIn("lab.example", warn.call_args.args[0])
                self.assertEqual(data["instances"], [self.record])
                self.assertFalse(self.config_file.exists())

    def test_edit_conflicting_authority_keeps_record_and_file(self):
        from contextlib import redirect_stdout
        from io import StringIO

        alias = dict(self.record, name="Alias")
        data = {"instances": [dict(self.record), alias], "active": "Friendly"}
        self.instances._save(data)
        snapshot = self.config_file.read_bytes()
        for url in ("https://lab.example:8443", "http://lab.example"):
            with (self.subTest(url=url),
                  patch("builtins.input", side_effect=["2", "3", url, "b"]),
                  patch.object(self.instances.caps, "clear_cache") as clear_cache,
                  patch.object(self.instances, "print_warn") as warn,
                  redirect_stdout(StringIO())):
                self.instances.edit_instance(data)
                self.assertIn("lab.example", warn.call_args.args[0])
                self.assertEqual(data["instances"][1]["url"], alias["url"])
                self.assertEqual(self.config_file.read_bytes(), snapshot)
                clear_cache.assert_not_called()

    def test_git_remote_scheme_matches_git_transport(self):
        for url in ("HTTP://lab.example/Group/Repo", "SSH://lab.example/Group/Repo"):
            with self.subTest(scheme=url.split(":", 1)[0]):
                self.assertIsNone(paths.remote_identity(url))
