"""Offline checks for managed Git transport and sync."""

from contextlib import contextmanager
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from _bootstrap import git as fixture_git, TempTreeTestCase
import git


HOST = "git.example.net"
NS = "team/deep/project"
URL = f"https://{HOST}/{NS}.git"


class ManagedGitTests(TempTreeTestCase):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.dest, HOST)
        self.repo = self.make_repo(f"{HOST}/{NS}", URL)
        self.cmd("branch", "-M", "main")

    def cmd(self, *args, cwd=None):
        result = fixture_git(*args, cwd=cwd or self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def set_upstream(self, remote, ref):
        self.cmd("config", "branch.main.remote", remote)
        self.cmd("config", "branch.main.merge", ref)

    def test_count_selects_host_and_full_namespace_without_mutation(self):
        self.cmd("remote", "add", "foreign", f"https://other.example.net/{NS}.git")
        self.cmd("remote", "add", "different", f"https://{HOST}/other/project.git")
        config = Path(self.repo, ".git", "config")
        before = config.read_bytes()
        self.assertEqual(git.managed_source(self.repo, self.root),
                         ("origin", ["different", "foreign", "origin"]))
        self.assertEqual(config.read_bytes(), before)
        self.assertFalse(Path(self.repo, ".git", "FETCH_HEAD").exists())

    def test_configured_upstream_wins_among_matching_remotes(self):
        self.cmd("remote", "add", "mirror", f"git@{HOST}:{NS}.git")
        self.set_upstream("mirror", "refs/heads/develop")
        self.assertEqual(git.managed_source(self.repo, self.root),
                         ("mirror", ["mirror", "origin"]))

    def test_ambiguous_match_is_not_a_zero_count(self):
        self.cmd("remote", "add", "mirror", URL)
        with self.assertRaises(ValueError):
            git.managed_source(self.repo, self.root)

    def test_foreign_upstream_blocks_even_if_only_one_remote_matches(self):
        self.cmd("remote", "add", "foreign", f"https://other.example.net/{NS}.git")
        self.set_upstream("foreign", "refs/heads/main")
        with self.assertRaises(ValueError):
            git.managed_source(self.repo, self.root)

    def test_rewrite_to_another_host_is_refused(self):
        self.cmd("config", "url.https://other.example.net/.insteadOf", f"https://{HOST}/")
        with self.assertRaises(ValueError) as exc:
            git.managed_source(self.repo, self.root)
        self.assertNotIn(URL, str(exc.exception))

    def test_rewrite_to_verified_fetch_url_is_allowed(self):
        self.cmd("remote", "set-url", "origin", f"shortcut:{NS}.git")
        self.cmd("config", f"url.https://{HOST}/.insteadOf", "shortcut:")
        self.assertEqual(git.managed_source(self.repo, self.root), ("origin", ["origin"]))

    def test_rewrite_to_unsupported_scheme_is_refused(self):
        self.cmd("config", "url.file:///offline/.insteadOf", f"https://{HOST}/")
        with self.assertRaises(ValueError):
            git.managed_source(self.repo, self.root)

    def test_option_like_remote_is_refused(self):
        self.cmd("config", "--", "remote.-bad.url", URL)
        with self.assertRaises(ValueError):
            git.managed_source(self.repo, self.root)

    def test_non_literal_upstream_ref_is_refused(self):
        for ref in ("refs/tags/release", "refs/heads/*", "refs/heads/-bad"):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                self.set_upstream("origin", ref)
                git.managed_source(self.repo, self.root)

    def test_configured_multi_url_remote_is_not_a_verified_source(self):
        self.cmd("config", "--add", "remote.origin.url", f"https://other.example.net/{NS}.git")
        with self.assertRaises(ValueError):
            git.managed_source(self.repo, self.root)

    def test_missing_or_non_repository_target_refused(self):
        for path in (os.path.join(self.root, "missing", "project"), self.tmp):
            with self.subTest(path=path), self.assertRaises(ValueError):
                git.managed_source(path, self.root)

    def test_detached_head_has_count_but_does_not_fetch(self):
        self.cmd("checkout", "--detach", "HEAD")
        self.assertEqual(git.managed_source(self.repo, self.root), ("origin", ["origin"]))
        with self.local_transport() as fetches:
            result = git.sync_repo(self.repo, host_root=self.root, instance_url=f"https://{HOST}")
        self.assertEqual(result["status"], git.S_DETACHED)
        self.assertEqual(fetches, [])

    def advance(self, branch):
        self.seed = os.path.join(self.tmp, "upstream")
        self.cmd("clone", "--quiet", "--", self.repo, self.seed)
        self.cmd("checkout", "-B", branch, cwd=self.seed)
        Path(self.seed, "new.txt").write_text("remote", encoding="utf-8")
        self.cmd("add", "new.txt", cwd=self.seed)
        self.cmd("commit", "--quiet", "-m", "upstream change", cwd=self.seed)
        return self.cmd("rev-parse", "HEAD", cwd=self.seed)

    def local_transport(self, *, failure=None, after_fetch=None):
        real_run = git._run

        @contextmanager
        def transport():
            fetches = []

            def run(args, *pos, **kw):
                if args[3:4] != ["fetch"]:
                    return real_run(args, *pos, **kw)
                fetches.append(args[4:])
                self.assertIn("--", args)
                boundary = args.index("--")
                env = kw["env_extra"]
                self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "http:https:ssh")
                alias = args[boundary + 1]
                settings = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
                            for i in range(int(env["GIT_CONFIG_COUNT"]))}
                self.assertEqual(settings[f"url.{URL}.insteadOf"], alias)
                checked = subprocess.run(
                    [git.GIT_BIN, "-C", self.repo, "config", "--get-urlmatch",
                     "http.followRedirects", URL], env={**os.environ, **env},
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True)
                self.assertEqual(checked.stdout.strip(), "false")
                if failure is not None:
                    return failure
                kw["env_extra"] = {**kw["env_extra"], "GIT_ALLOW_PROTOCOL": "file"}
                result = real_run([*args[:boundary + 1], self.seed, *args[boundary + 2:]], *pos, **kw)
                if after_fetch is not None:
                    after_fetch()
                return result

            with patch.object(git, "_run", side_effect=run):
                yield fetches

        return transport()

    def test_sync_fetches_only_tracked_branch_and_fast_forwards(self):
        self.cmd("remote", "add", "foreign", f"https://other.example.net/{NS}.git")
        self.set_upstream("origin", "refs/heads/develop")
        tip = self.advance("develop")
        self.cmd("tag", "remote-tag", cwd=self.seed)
        self.cmd("config", "fetch.prune", "true")
        self.cmd("config", "fetch.pruneTags", "true")
        self.cmd("config", "fetch.recurseSubmodules", "true")
        self.cmd("config", f"http.{URL}.followRedirects", "true")
        self.cmd("tag", "keep-local-tag")
        config = Path(self.repo, ".git", "config")
        before = config.read_bytes()
        with self.local_transport() as fetches:
            result = git.sync_repo(self.repo, host_root=self.root, instance_url=f"https://{HOST}")
        self.assertEqual(result["status"], git.S_PULLED, result)
        self.assertEqual(result["behind"], 1)
        self.assertEqual(self.cmd("rev-parse", "HEAD"), tip)
        self.assertEqual(self.cmd("branch", "--show-current"), "main")
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(self.cmd("tag", "--list"), "keep-local-tag")
        self.assertFalse(Path(self.repo, ".git", "FETCH_HEAD").exists())
        self.assertEqual(self.cmd("for-each-ref", "--format=%(refname)", "refs/gitlab-cli"), "")
        self.assertEqual(len(fetches), 1)
        self.assertTrue({"--no-tags", "--no-prune", "--no-prune-tags", "--no-recurse-submodules"} <= set(fetches[0]))
        self.assertEqual(len(fetches[0][fetches[0].index("--") + 2:]), 1)
        self.assertEqual(fetches[0][-1].partition(":")[0], "refs/heads/develop")

    def test_sync_without_upstream_never_sets_one(self):
        tip = self.advance("main")
        config = Path(self.repo, ".git", "config")
        before = config.read_bytes()
        with self.local_transport():
            result = git.sync_repo(self.repo, host_root=self.root, instance_url=f"https://{HOST}")
        self.assertEqual(result["status"], git.S_PULLED, result)
        self.assertEqual(self.cmd("rev-parse", "HEAD"), tip)
        self.assertEqual(config.read_bytes(), before)

    def test_sync_rejects_wrong_http_authority_before_fetch(self):
        for url in (f"https://{HOST}:8443/{NS}.git", f"http://{HOST}/{NS}.git"):
            with self.subTest(url=url):
                self.cmd("remote", "set-url", "origin", url)
                with patch.object(git, "_managed_fetch") as fetch:
                    result = git.sync_repo(self.repo, host_root=self.root,
                                           instance_url=f"https://{HOST}")
                self.assertEqual(result["status"], git.S_ERROR, result)
                fetch.assert_not_called()
                self.assertNotIn(url, result["detail"])

    def test_sync_accepts_matching_http_authority(self):
        self.advance("main")
        with self.local_transport() as fetches:
            result = git.sync_repo(self.repo, host_root=self.root,
                                   instance_url=f"https://{HOST}:443")
        self.assertEqual(result["status"], git.S_PULLED, result)
        self.assertEqual(len(fetches), 1)

    def test_sync_rechecks_http_authority_after_fetch(self):
        self.advance("main")
        head = self.cmd("rev-parse", "HEAD")
        with self.local_transport(after_fetch=lambda: self.cmd(
                "remote", "set-url", "origin", f"https://{HOST}:8443/{NS}.git")) as fetches:
            result = git.sync_repo(self.repo, host_root=self.root,
                                   instance_url=f"https://{HOST}")
        self.assertEqual(result["status"], git.S_ERROR, result)
        self.assertEqual(len(fetches), 1)
        self.assertEqual(self.cmd("rev-parse", "HEAD"), head)

    def test_sync_preserves_ssh_remote_selection(self):
        self.cmd("remote", "set-url", "origin", f"git@{HOST}:{NS}.git")
        with patch.object(git, "_managed_fetch", return_value=(1, "", "offline")) as fetch:
            result = git.sync_repo(self.repo, host_root=self.root,
                                   instance_url=f"https://{HOST}")
        self.assertEqual(result["status"], git.S_ERROR, result)
        self.assertTrue(fetch.called)

    def test_untracked_file_blocks_sync_without_network(self):
        marker = Path(self.repo, "new.txt")
        marker.write_text("local", encoding="utf-8")
        head = self.cmd("rev-parse", "HEAD")
        with self.local_transport() as fetches:
            result = git.sync_repo(self.repo, host_root=self.root, instance_url=f"https://{HOST}")
        self.assertEqual(result["status"], git.S_DIRTY, result)
        self.assertEqual(fetches, [])
        self.assertEqual(marker.read_text(encoding="utf-8"), "local")
        self.assertEqual(self.cmd("rev-parse", "HEAD"), head)

    def test_ignored_file_is_not_overwritten(self):
        tip = self.advance("main")
        head = self.cmd("rev-parse", "HEAD")
        Path(self.repo, ".git", "info", "exclude").write_text("new.txt\n", encoding="utf-8")
        marker = Path(self.repo, "new.txt")
        marker.write_text("local", encoding="utf-8")
        with self.local_transport():
            result = git.sync_repo(self.repo, host_root=self.root, instance_url=f"https://{HOST}")
        self.assertNotEqual(result["status"], git.S_PULLED, result)
        self.assertNotEqual(head, tip)
        self.assertEqual(self.cmd("rev-parse", "HEAD"), head)
        self.assertEqual(marker.read_text(encoding="utf-8"), "local")

    def test_branch_changed_during_fetch_is_not_merged(self):
        self.advance("main")
        head = self.cmd("rev-parse", "HEAD")
        with self.local_transport(after_fetch=lambda: self.cmd("checkout", "-b", "other")):
            result = git.sync_repo(self.repo, host_root=self.root, instance_url=f"https://{HOST}")
        self.assertEqual(result["status"], git.S_ERROR, result)
        self.assertEqual(self.cmd("rev-parse", "HEAD"), head)
        self.assertEqual(self.cmd("branch", "--show-current"), "other")

    def test_changed_remote_during_fetch_is_not_merged(self):
        self.advance("main")
        head = self.cmd("rev-parse", "HEAD")
        with self.local_transport(after_fetch=lambda: self.cmd(
                "remote", "set-url", "origin", f"https://other.example.net/{NS}.git")):
            result = git.sync_repo(self.repo, host_root=self.root, instance_url=f"https://{HOST}")
        self.assertEqual(result["status"], git.S_ERROR, result)
        self.assertEqual(self.cmd("rev-parse", "HEAD"), head)

    def test_promisor_repo_does_not_sync(self):
        self.advance("main")
        self.cmd("config", "remote.origin.promisor", "true")
        head = self.cmd("rev-parse", "HEAD")
        with self.local_transport() as fetches:
            result = git.sync_repo(self.repo, host_root=self.root, instance_url=f"https://{HOST}")
        self.assertEqual(result["status"], git.S_ERROR, result)
        self.assertEqual(fetches, [])
        self.assertEqual(self.cmd("rev-parse", "HEAD"), head)
        self.assertEqual(git.managed_source(self.repo, self.root), ("origin", ["origin"]))

    def test_fetch_error_does_not_merge_prefilled_fetch_head(self):
        tip = self.advance("main")
        head = self.cmd("rev-parse", "HEAD")
        self.cmd("fetch", "--quiet", "--", self.seed, "refs/heads/main")
        self.assertEqual(self.cmd("rev-parse", "FETCH_HEAD"), tip)
        with self.local_transport(failure=(1, "", "fatal: Authentication failed")):
            result = git.sync_repo(self.repo, host_root=self.root, instance_url=f"https://{HOST}")
        self.assertEqual(result["status"], git.S_AUTH, result)
        self.assertEqual(self.cmd("rev-parse", "HEAD"), head)

    def test_missing_fetched_ref_does_not_fall_back_to_fetch_head(self):
        tip = self.advance("main")
        head = self.cmd("rev-parse", "HEAD")
        self.cmd("fetch", "--quiet", "--", self.seed, "refs/heads/main")
        self.assertEqual(self.cmd("rev-parse", "FETCH_HEAD"), tip)
        with self.local_transport(failure=(0, "", "")):
            result = git.sync_repo(self.repo, host_root=self.root, instance_url=f"https://{HOST}")
        self.assertEqual(result["status"], git.S_ERROR, result)
        self.assertEqual(self.cmd("rev-parse", "HEAD"), head)

    def test_missing_branch_does_not_change_head(self):
        self.advance("other")
        self.set_upstream("origin", "refs/heads/missing")
        head = self.cmd("rev-parse", "HEAD")
        with self.local_transport():
            result = git.sync_repo(self.repo, host_root=self.root, instance_url=f"https://{HOST}")
        self.assertEqual(result["status"], git.S_NO_REMOTE, result)
        self.assertEqual(self.cmd("rev-parse", "HEAD"), head)

    def test_local_commits_are_not_pushed_or_reset(self):
        self.advance("other")
        self.cmd("commit", "--allow-empty", "-m", "local only")
        head = self.cmd("rev-parse", "HEAD")
        with self.local_transport():
            result = git.sync_repo(self.repo, host_root=self.root, instance_url=f"https://{HOST}")
        self.assertEqual(result["status"], git.S_AHEAD, result)
        self.assertEqual(self.cmd("rev-parse", "HEAD"), head)

    def test_divergence_does_not_change_head(self):
        self.advance("main")
        self.cmd("commit", "--allow-empty", "-m", "local change")
        head = self.cmd("rev-parse", "HEAD")
        with self.local_transport():
            result = git.sync_repo(self.repo, host_root=self.root, instance_url=f"https://{HOST}")
        self.assertEqual(result["status"], git.S_DIVERGED, result)
        self.assertEqual(self.cmd("rev-parse", "HEAD"), head)



class ManagedCloneTests(TempTreeTestCase):
    def clone(self, url, target, instance_url, root, token=""):
        return git.clone_repo_managed(token, url, str(target), instance_url=instance_url,
                                      host_root=str(root))

    def test_loopback_http_clone_keeps_origin_and_reflog_clean(self):
        from base64 import b64encode
        from functools import partial
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread

        seed = self.make_repo("seed")
        storage = Path(self.tmp, "server")
        project = storage / f"{NS}.git"
        project.parent.mkdir(parents=True)
        for args in (("clone", "--quiet", "--bare", "--", seed, str(project)),
                     ("-C", str(project), "update-server-info")):
            result = fixture_git(*args, cwd=self.tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
        secret = "fixture-only-password"
        basic = b64encode(f"oauth2:{secret}".encode()).decode()
        headers_ok = []

        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                authorized = self.headers.get("Authorization") == f"Basic {basic}"
                headers_ok.append(authorized)
                if authorized:
                    super().do_GET()
                else:
                    self.send_error(403)

        with ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=storage)) as server:
            worker = Thread(target=server.serve_forever)
            worker.start()
            try:
                root = Path(self.dest, "127.0.0.1")
                target = root / NS
                instance = f"http://127.0.0.1:{server.server_port}"
                url = f"{instance}/{NS}.git"
                with patch.object(git, "save_credential") as store:
                    result = self.clone(url, target, instance, root, secret)
                store.assert_not_called()
            finally:
                server.shutdown()
                worker.join()
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["commits"], 1)
        self.assertTrue(headers_ok and all(headers_ok))
        self.assertEqual((target / "README.md").read_text(encoding="utf-8"), "x\n")
        self.assertEqual(git.managed_source(str(target), str(root)), ("origin", ["origin"]))
        self.assertEqual((target / ".git/config").stat().st_mode & 0o077, 0)
        for rel in ("config", "logs/HEAD"):
            text = (target / ".git" / rel).read_text(encoding="utf-8")
            self.assertIn(url, text)
            self.assertNotIn(secret, text)
            self.assertNotIn(basic, text)

    def test_clone_rejects_mismatched_inputs_without_transport(self):
        root = Path(self.dest, HOST)
        target = root / NS
        instance = f"https://{HOST}"
        for url, path, base, host_root in (
            (f"https://other.example.net/{NS}.git", target, instance, root),
            (f"https://{HOST}/other/project.git", target, instance, root),
            (f"https://{HOST}:8443/{NS}.git", target, instance, root),
            (f"http://{HOST}/{NS}.git", target, instance, root),
            (f"git@{HOST}:{NS}.git", target, instance, root),
            (f"https://fixture-user:fixture-password@{HOST}/{NS}.git", target, instance, root),
            (URL, root / "project", instance, root),
            (URL, target, instance, Path(self.dest, "other.example.net")),
        ):
            with self.subTest(url=url, path=path, instance=base, root=host_root):
                with patch.object(git, "_run") as run:
                    result = self.clone(url, path, base, host_root)
                run.assert_not_called()
                self.assertEqual(result["status"], git.S_ERROR)
                self.assertNotIn(url, result["detail"])
                self.assertNotIn("fixture-password", result["detail"])
        self.assertEqual(list(Path(self.dest).iterdir()), [])

    def test_clone_rejects_bad_credential_before_transport(self):
        root = Path(self.dest, HOST)
        with patch.object(git, "_run") as run:
            result = self.clone(URL, root / NS, f"https://{HOST}", root, "sample\nvalue")
        run.assert_not_called()
        self.assertEqual(result["status"], git.S_ERROR)
        self.assertNotIn("sample", result["detail"])
        self.assertFalse(root.exists())

    def test_clone_leaves_occupied_destination_untouched(self):
        root = Path(self.dest, HOST)
        target = root / NS
        target.mkdir(parents=True)
        marker = target / "keep.txt"
        marker.write_text("local data", encoding="utf-8")
        with patch.object(git, "_run") as run:
            result = self.clone(URL, target, f"https://{HOST}", root)
        run.assert_not_called()
        self.assertEqual(result["status"], git.S_ERROR)
        self.assertEqual(marker.read_text(encoding="utf-8"), "local data")

    def test_clone_refuses_destination_inside_other_repository(self):
        root = Path(self.dest, HOST)
        project = self.make_repo(f"{HOST}/team/deep")
        target = root / NS
        self.assertEqual(target.parent, Path(project))
        with patch.object(git, "_run") as run:
            result = self.clone(URL, target, f"https://{HOST}", root)
        run.assert_not_called()
        self.assertEqual(result["status"], git.S_ERROR)
        self.assertFalse(target.exists())

    def test_clone_failure_is_classified_without_child_diagnostics(self):
        root = Path(self.dest, HOST)
        secret = "fixture-private-value"
        with patch.object(git, "_run", return_value=(1, "", f"Authentication failed: {secret}")) as run:
            result = self.clone(URL, root / NS, f"https://{HOST}", root, secret)
        run.assert_called_once()
        self.assertEqual(result["status"], git.S_AUTH)
        self.assertNotIn(secret, repr(result))

    def test_clone_settings_ignore_ambient_git_config(self):
        root = Path(self.dest, HOST)

        def transport(args, *, cwd, env_base, umask):
            self.assertEqual(args[-3:], ["--", URL, str(root / NS)])
            self.assertEqual(env_base["GIT_ALLOW_PROTOCOL"], "https")
            self.assertNotIn("GIT_CONFIG_PARAMETERS", env_base)
            self.assertNotIn("GIT_TRACE_CURL", env_base)
            self.assertNotIn("https_proxy", env_base)
            for query, expected in (
                (("config", "--get-urlmatch", "http.followRedirects", URL), "false"),
                (("ls-remote", "--get-url", "--", URL), URL),
            ):
                checked = subprocess.run([git.GIT_BIN, *query], env=env_base, cwd=cwd,
                                         stdin=subprocess.DEVNULL, capture_output=True,
                                         text=True, check=True, timeout=10)
                self.assertEqual(checked.stdout.strip(), expected)
            return 1, "", "offline fixture"

        with patch.dict(os.environ, {"GIT_CONFIG_PARAMETERS": "'http.followRedirects'='true'",
                                     "GIT_TRACE_CURL": "1", "https_proxy": "http://127.0.0.1:9"}):
            with patch.object(git, "_run", side_effect=transport) as run:
                result = self.clone(URL, root / NS, f"https://{HOST}", root)
        run.assert_called_once()
        self.assertEqual(result["status"], git.S_ERROR)

    def test_clone_retries_shallow_only_with_same_transport(self):
        root = Path(self.dest, HOST)
        target = root / NS
        with patch.object(git, "_run", side_effect=[
            (1, "", "curl 18 transfer closed with outstanding read data"),
            (1, "", "offline fixture"),
        ]) as run:
            result = self.clone(URL, target, f"https://{HOST}", root)
        self.assertEqual(run.call_count, 2)
        first, second = run.call_args_list
        self.assertEqual(first.kwargs, second.kwargs)
        self.assertEqual(first.args[0][-3:], second.args[0][-3:])
        self.assertEqual(second.args[0][-4], "--depth=10")
        self.assertEqual(result["status"], git.S_ERROR)

    def test_clone_shallow_retry_does_not_remove_residual_files(self):
        root = Path(self.dest, HOST)
        target = root / NS
        marker = target / "keep.txt"

        def incomplete(*args, **kwargs):
            target.mkdir()
            marker.write_text("keep", encoding="utf-8")
            return 1, "", "curl 18 transfer closed with outstanding read data"

        with patch.object(git, "_run", side_effect=incomplete) as run:
            result = self.clone(URL, target, f"https://{HOST}", root)
        run.assert_called_once()
        self.assertEqual(result["status"], git.S_ERROR)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_clone_rejects_invalid_depth_before_transport(self):
        root = Path(self.dest, HOST)
        with patch.object(git, "_run") as run:
            result = git.clone_repo_managed("", URL, str(root / NS),
                                            instance_url=f"https://{HOST}", host_root=str(root),
                                            shallow_depth_hint=0)
        run.assert_not_called()
        self.assertEqual(result["status"], git.S_ERROR)
        self.assertFalse(root.exists())

    def test_clone_rejects_linked_destination_before_transport(self):
        root = Path(self.dest, HOST)
        outside = Path(self.tmp, "outside")
        outside.mkdir()
        root.mkdir()
        (root / "team").symlink_to(outside, target_is_directory=True)
        with patch.object(git, "_run") as run:
            result = self.clone(URL, root / NS, f"https://{HOST}", root)
        run.assert_not_called()
        self.assertEqual(result["status"], git.S_ERROR)
        self.assertEqual(list(outside.iterdir()), [])

    def test_lfs_clone_reports_incomplete_without_second_transport(self):
        seed = self.make_repo("seed")
        Path(seed, ".gitattributes").write_text("*.bin filter=lfs\n", encoding="utf-8")
        for args in (("add", ".gitattributes"), ("commit", "-qm", "LFS attributes")):
            checked = fixture_git(*args, cwd=seed)
            self.assertEqual(checked.returncode, 0, checked.stderr)
        root = Path(self.dest, HOST)
        target = root / NS
        real_run = git._run
        transport_calls = []

        def fixture(args, **kwargs):
            if args[1] == "clone":
                transport_calls.append(args)
                self.assertEqual(args[-2], URL)
                args = [*args[:-2], seed, args[-1]]
                kwargs["env_base"] = {**kwargs["env_base"], "GIT_ALLOW_PROTOCOL": "file"}
            else:
                self.assertEqual(kwargs["env_base"]["GIT_ALLOW_PROTOCOL"], "")
                self.assertNotIn("lfs", args)
            return real_run(args, **kwargs)

        with patch.object(git, "_run", side_effect=fixture):
            result = self.clone(URL, target, f"https://{HOST}", root)
        self.assertEqual(result["status"], git.S_LFS, result)
        self.assertEqual(len(transport_calls), 1)
        self.assertTrue((target / "README.md").exists())
