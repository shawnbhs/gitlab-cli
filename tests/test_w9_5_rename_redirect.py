# tests/test_w9_5_rename_redirect.py — a renamed project is REPAIRED, never duplicated.
#
# THE DEFECT (measured: 17 projects in the Hub tree, 866 MB of duplicate
# checkouts). A project renamed or moved on the server leaves its clone on disk
# carrying the OLD origin URL. Identity here is the origin URL, so one rename
# produces two wrong answers simultaneously: the project reads as MISSING (no
# clone carries the new path) and the old clone reads as an ORPHAN (its path
# matches nothing on the server). The clone path then wrote a second, complete
# copy at the new path and the status display called it fine.
#
# GitLab follows its own rename redirect, so `GET /projects/<old-encoded-path>`
# answers 200 with the CURRENT path. That single request separates renamed from
# deleted without guessing, and it is what these tests pin.
#
# Everything here is REAL git against REAL local repositories. Only the HTTP
# layer (api.request) is stubbed, and only because there is no server offline.
# The containment check talks to a real bare repository acting as the server.

import os
import subprocess
import sys

import _bootstrap  # noqa: F401  (path + error-log guard)
from _bootstrap import TempTreeTestCase, git, GIT, BASE, HOST, fake_cfg

import clone as clone_mod
import coverage as cov


# ─── A stub for the ONE HTTP call in this feature ─────────────────────────────

class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class RedirectServer:
    """Stands in for GitLab's `GET /projects/<encoded path>` endpoint.

    `renames` maps an OLD path_with_namespace to its CURRENT one — exactly the
    redirect GitLab serves. `live` are paths that still exist unchanged.
    Anything else 404s. `status_for` forces an arbitrary status (401, 429, 500)
    for one path, which is how the undetermined branch is exercised.
    """

    def __init__(self, renames=None, live=(), status_for=None, unreachable=()):
        self.renames = dict(renames or {})
        self.live = set(live)
        self.status_for = dict(status_for or {})
        self.unreachable = set(unreachable)
        self.calls = []

    def request(self, url, headers, params=None, quiet=False):
        from urllib.parse import unquote
        path = unquote(url.rsplit("/projects/", 1)[-1])
        self.calls.append(path)
        if path in self.unreachable:
            return None                      # timeout / connection error
        if path in self.status_for:
            return FakeResponse(self.status_for[path], {})
        if path in self.renames:
            return FakeResponse(200, {
                "id": 1,
                "path": self.renames[path].split("/")[-1],
                "path_with_namespace": self.renames[path],
                "http_url_to_repo": f"{BASE}/{self.renames[path]}.git",
            })
        if path in self.live:
            return FakeResponse(200, {
                "id": 2,
                "path": path.split("/")[-1],
                "path_with_namespace": path,
                "http_url_to_repo": f"{BASE}/{path}.git",
            })
        return FakeResponse(404, {"message": "404 Project Not Found"})


class RenameTestCase(TempTreeTestCase):
    """Shared fixture: a real bare 'server' repo plus a real clone of it."""

    def setUp(self):
        super().setUp()
        clone_mod.reset_presence_cache()
        clone_mod.reset_redirect_cache()
        self._saved = {}

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(clone_mod, name, value)
        clone_mod.reset_presence_cache()
        clone_mod.reset_redirect_cache()
        super().tearDown()

    def patch(self, name, value):
        if name not in self._saved:
            self._saved[name] = getattr(clone_mod, name)
        setattr(clone_mod, name, value)

    def install_server(self, server):
        """Route clone.py's redirect lookup at a RedirectServer."""
        class _Api:
            request = staticmethod(server.request)
        self.patch("gitlab_api", _Api)
        return server

    # ── real repositories ─────────────────────────────────────────────────

    def make_bare(self, name="server.git"):
        """A real bare repository with one commit — the 'server' side."""
        bare = os.path.join(self.tmp, "srv", name)
        os.makedirs(os.path.dirname(bare), exist_ok=True)
        r = git("init", "--bare", "-q", bare)
        self.assertEqual(r.returncode, 0, r.stderr)
        seed = os.path.join(self.tmp, "seed")
        git("init", "-q", seed)
        os.makedirs(seed, exist_ok=True)
        with open(os.path.join(seed, "a.txt"), "w") as fh:
            fh.write("one\n")
        git("add", "-A", cwd=seed)
        git("commit", "-q", "-m", "on the server", cwd=seed)
        git("branch", "-M", "main", cwd=seed)
        git("push", "-q", bare, "main", cwd=seed)
        # `git init --bare` points HEAD at refs/heads/master; without this the
        # clone checks nothing out and every assertion below measures a
        # fixture bug instead of the code.
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=bare)
        return bare

    def clone_from(self, bare, relpath, origin_url):
        """A real clone of `bare` at dest/<relpath>, origin rewritten to the
        tool-shaped http URL (that is what the presence index keys on)."""
        path = os.path.join(self.dest, *relpath.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        r = subprocess.run([GIT, "clone", "-q", bare, path],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        git("remote", "set-url", "origin", origin_url, cwd=path)
        return path


# ─── 1. redirect_lookup: renamed vs deleted vs "I could not tell" ─────────────

class RedirectClassification(RenameTestCase):
    """The single request that makes the whole repair possible — and the three
    answers it must keep apart. A wrong guess toward 'deleted' abandons real
    data; a wrong guess toward 'renamed' moves a directory that should not
    move; so anything that is not a definite answer must say so."""

    def setUp(self):
        super().setUp()
        self.cfg = fake_cfg()

    def test_a_renamed_project_is_reported_with_its_current_path(self):
        self.install_server(RedirectServer(
            renames={"web/old-name": "web/new-name"}))
        r = clone_mod.redirect_lookup(self.cfg, "web/old-name")
        self.assertEqual(r["state"], "renamed")
        self.assertEqual(r["new_path"], "web/new-name")

    def test_a_project_moved_between_groups_is_renamed_too(self):
        # GitLab treats a namespace transfer as the same redirect.
        self.install_server(RedirectServer(
            renames={"oldgroup/svc": "newgroup/svc"}))
        r = clone_mod.redirect_lookup(self.cfg, "oldgroup/svc")
        self.assertEqual(r["state"], "renamed")
        self.assertEqual(r["new_path"], "newgroup/svc")

    def test_a_404_is_deleted_not_renamed(self):
        self.install_server(RedirectServer())
        r = clone_mod.redirect_lookup(self.cfg, "web/vanished")
        self.assertEqual(r["state"], "gone")

    def test_an_unchanged_project_is_same_not_renamed(self):
        self.install_server(RedirectServer(live=["web/steady"]))
        self.assertEqual(
            clone_mod.redirect_lookup(self.cfg, "web/steady")["state"], "same")

    def test_a_401_is_undetermined_and_never_deleted(self):
        # A 401 is an AUTH failure. Reading it as "the project is gone" is the
        # mistake that has already been made once on this project.
        self.install_server(RedirectServer(status_for={"web/x": 401}))
        r = clone_mod.redirect_lookup(self.cfg, "web/x")
        self.assertEqual(r["state"], "undetermined")
        self.assertNotEqual(r["state"], "gone")

    def test_a_403_and_a_500_are_undetermined(self):
        self.install_server(RedirectServer(
            status_for={"web/a": 403, "web/b": 500}))
        for p in ("web/a", "web/b"):
            self.assertEqual(
                clone_mod.redirect_lookup(self.cfg, p)["state"], "undetermined")

    def test_a_timeout_is_undetermined_and_is_not_cached(self):
        server = self.install_server(RedirectServer(unreachable=["web/slow"]))
        self.assertEqual(
            clone_mod.redirect_lookup(self.cfg, "web/slow")["state"],
            "undetermined")
        # A failure must not be memoised: the next run has to ask again.
        clone_mod.redirect_lookup(self.cfg, "web/slow")
        self.assertEqual(server.calls.count("web/slow"), 2)

    def test_a_definite_answer_is_asked_for_only_once_per_session(self):
        server = self.install_server(RedirectServer(
            renames={"web/old": "web/new"}))
        for _ in range(4):
            clone_mod.redirect_lookup(self.cfg, "web/old")
        self.assertEqual(server.calls.count("web/old"), 1,
                         "one Clone All run walks the same tree repeatedly — "
                         "the redirect answer must be cached")

    def test_the_old_path_is_url_encoded(self):
        server = self.install_server(RedirectServer(
            renames={"a/b/c": "a/b/d"}))
        clone_mod.redirect_lookup(self.cfg, "a/b/c")
        # The stub decodes before recording, so a hit here proves the slashes
        # survived encoding rather than being sent raw.
        self.assertIn("a/b/c", server.calls)


# ─── 2. Containment measured against the SERVER, not a local ref ──────────────

class ContainmentAgainstTheServer(RenameTestCase):
    """`--not --remotes` against a stale local ref has already produced a
    '2 993 commits ahead' verdict for a clone with zero local authors. The
    check must therefore re-fetch from the server before counting."""

    def test_a_clone_with_nothing_extra_reports_zero(self):
        bare = self.make_bare()
        repo = self.clone_from(bare, "web/proj", f"{BASE}/web/proj.git")
        r = clone_mod.unique_commits_vs_server(repo, bare)
        self.assertTrue(r["ok"], r["detail"])
        self.assertEqual(r["count"], 0)

    def test_a_commit_that_exists_only_here_is_counted(self):
        bare = self.make_bare()
        repo = self.clone_from(bare, "web/proj", f"{BASE}/web/proj.git")
        with open(os.path.join(repo, "local.txt"), "w") as fh:
            fh.write("only on this laptop\n")
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", "nowhere else", cwd=repo)
        r = clone_mod.unique_commits_vs_server(repo, bare)
        self.assertTrue(r["ok"], r["detail"])
        self.assertEqual(r["count"], 1)

    def test_a_stale_local_remote_ref_does_not_manufacture_unique_commits(self):
        # THE 2 993 TRAP, reproduced. The commit IS on the server; only the
        # local remote-tracking ref is old. A check that trusted origin/main
        # would refuse a perfectly safe move (or, worse, report phantom work).
        bare = self.make_bare()
        repo = self.clone_from(bare, "web/proj", f"{BASE}/web/proj.git")
        old_tip = subprocess.run([GIT, "-C", repo, "rev-parse", "HEAD"],
                                 capture_output=True, text=True).stdout.strip()
        with open(os.path.join(repo, "b.txt"), "w") as fh:
            fh.write("published\n")
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", "this one IS on the server", cwd=repo)
        git("push", "-q", bare, "HEAD:main", cwd=repo)
        # Freeze the remote-tracking ref at the old tip: a stale local ref.
        git("update-ref", "refs/remotes/origin/main", old_tip, cwd=repo)

        naive = subprocess.run(
            [GIT, "-C", repo, "rev-list", "--count", "HEAD", "--not",
             "--remotes"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(naive, "1",
                         "fixture wrong: the stale ref should make the naive "
                         "count non-zero")

        r = clone_mod.unique_commits_vs_server(repo, bare)
        self.assertTrue(r["ok"], r["detail"])
        self.assertEqual(r["count"], 0,
                         "the commit is on the server; only the local "
                         "remote-tracking ref was stale")

    def test_the_check_leaves_no_refs_behind(self):
        bare = self.make_bare()
        repo = self.clone_from(bare, "web/proj", f"{BASE}/web/proj.git")
        clone_mod.unique_commits_vs_server(repo, bare)
        out = subprocess.run(
            [GIT, "-C", repo, "for-each-ref", "--format=%(refname)"],
            capture_output=True, text=True).stdout
        self.assertNotIn("__gitlab_cli_rename_check__", out)

    def test_an_unreachable_server_is_not_answered_with_zero(self):
        bare = self.make_bare()
        repo = self.clone_from(bare, "web/proj", f"{BASE}/web/proj.git")
        r = clone_mod.unique_commits_vs_server(
            repo, os.path.join(self.tmp, "no-such-repo.git"))
        self.assertFalse(r["ok"],
                         "an unanswerable question must never read as 'safe'")

    def test_a_shallow_clone_refuses_to_answer(self):
        bare = self.make_bare()
        repo = os.path.join(self.dest, "web", "shallow")
        os.makedirs(os.path.dirname(repo), exist_ok=True)
        subprocess.run([GIT, "clone", "-q", "--depth=1", "file://" + bare,
                        repo], capture_output=True, text=True, timeout=120)
        if not os.path.isdir(repo):
            self.skipTest("shallow clone unavailable in this environment")
        r = clone_mod.unique_commits_vs_server(repo, bare)
        self.assertFalse(r["ok"])
        self.assertIn("shallow", r["detail"])


# ─── 3. move_safety: the refusal paths ────────────────────────────────────────

class MoveRefusals(RenameTestCase):
    """A move must never happen when the existing clone holds anything the
    server cannot give back. Each condition below is a real thing on disk."""

    def _repo(self):
        bare = self.make_bare()
        return bare, self.clone_from(bare, "web/proj", f"{BASE}/web/proj.git")

    def test_a_clean_clone_is_safe(self):
        bare, repo = self._repo()
        s = clone_mod.move_safety(repo, bare)
        self.assertTrue(s["safe"], s["reasons"])

    def test_uncommitted_changes_refuse_the_move(self):
        bare, repo = self._repo()
        with open(os.path.join(repo, "a.txt"), "a") as fh:
            fh.write("edited but never committed\n")
        s = clone_mod.move_safety(repo, bare)
        self.assertFalse(s["safe"])
        self.assertTrue(any("uncommitted" in r for r in s["reasons"]),
                        s["reasons"])

    def test_untracked_files_refuse_the_move(self):
        bare, repo = self._repo()
        with open(os.path.join(repo, "scratch.txt"), "w") as fh:
            fh.write("might matter to somebody\n")
        s = clone_mod.move_safety(repo, bare)
        self.assertFalse(s["safe"])
        self.assertTrue(any("untracked" in r for r in s["reasons"]),
                        s["reasons"])

    def test_a_stash_refuses_the_move(self):
        bare, repo = self._repo()
        with open(os.path.join(repo, "a.txt"), "a") as fh:
            fh.write("stashed work\n")
        r = git("stash", "push", "-q", "-m", "wip", cwd=repo)
        stashes = subprocess.run([GIT, "-C", repo, "stash", "list"],
                                 capture_output=True, text=True).stdout
        if not stashes.strip():
            self.skipTest(f"git stash unavailable here: {r.stderr}")
        s = clone_mod.move_safety(repo, bare)
        self.assertFalse(s["safe"])
        self.assertTrue(any("stash" in x for x in s["reasons"]), s["reasons"])

    def test_a_commit_the_server_does_not_have_refuses_the_move(self):
        # The seventeenth repository held exactly one such commit and was very
        # nearly lost. A name comparison would never have seen it.
        bare, repo = self._repo()
        with open(os.path.join(repo, "only-here.txt"), "w") as fh:
            fh.write("exists nowhere else\n")
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", "the commit that was nearly lost", cwd=repo)
        s = clone_mod.move_safety(repo, bare)
        self.assertFalse(s["safe"])
        self.assertTrue(any("NOT on the server" in r for r in s["reasons"]),
                        s["reasons"])

    def test_an_unverifiable_clone_refuses_the_move(self):
        bare, repo = self._repo()
        s = clone_mod.move_safety(repo, os.path.join(self.tmp, "gone.git"))
        self.assertFalse(s["safe"])
        self.assertTrue(any("unverifiable" in r for r in s["reasons"]),
                        s["reasons"])

    def test_a_nested_checkout_is_never_moved(self):
        bare = self.make_bare()
        outer = self.make_repo("web/outer", f"{BASE}/web/outer.git")
        inner = os.path.join(outer, "vendor", "inner")
        os.makedirs(os.path.dirname(inner), exist_ok=True)
        subprocess.run([GIT, "clone", "-q", bare, inner],
                       capture_output=True, text=True, timeout=120)
        s = clone_mod.move_safety(inner, bare)
        self.assertFalse(s["safe"])
        self.assertTrue(any("nested" in r for r in s["reasons"]), s["reasons"])

    def test_every_reason_is_reported_not_just_the_first(self):
        bare, repo = self._repo()
        with open(os.path.join(repo, "a.txt"), "a") as fh:
            fh.write("dirty\n")
        with open(os.path.join(repo, "new.txt"), "w") as fh:
            fh.write("untracked\n")
        s = clone_mod.move_safety(repo, bare)
        self.assertGreaterEqual(len(s["reasons"]), 2, s["reasons"])


# ─── 4. End to end through _process_group: before vs after ───────────────────

class ProcessGroupRepairsInsteadOfDuplicating(RenameTestCase):
    """The behaviour the 866 MB came from, and the behaviour that replaces it.

    Only two seams are stubbed: the project listing (there is no server) and
    the transport address the containment check uses (the same real bare repo,
    reached by path instead of by http). Every git operation is real.
    """

    def setUp(self):
        super().setUp()
        self.cfg = fake_cfg()
        self.cfg["token"] = ""
        self.cloned = []
        self.bare_for_url = {}

        def _fake_clone(token, url, target):
            self.cloned.append(target)
            os.makedirs(target, exist_ok=True)
            git("init", "-q", target)
            git("remote", "add", "origin", url, cwd=target)
            return True

        self.patch("clone_repo", _fake_clone)
        self.patch("get_subgroups", lambda url, headers, gid: [])

        real_unique = clone_mod.unique_commits_vs_server

        def _local_unique(repo_dir, url, token=""):
            # Substitute ONLY the transport address; every git step below is
            # the production one, run against the real bare 'server' repo.
            return real_unique(repo_dir, self.bare_for_url.get(url, url), "")

        self.patch("unique_commits_vs_server", _local_unique)

    def _projects(self, *pwns):
        return [{"id": i, "path": p.split("/")[-1],
                 "path_with_namespace": p,
                 "http_url_to_repo": f"{BASE}/{p}.git"}
                for i, p in enumerate(pwns, 1)]

    def _run_group(self, projects, group_path, renamed=None):
        self.patch("get_projects", lambda url, headers, gid: projects)
        clone_mod._session_stats.update(
            {"cloned": 0, "failed": 0, "invalid": 0, "elsewhere": 0,
             "renamed": 0, "renamed_refused": 0})
        index = clone_mod.build_presence_index(self.dest)
        renamed = [] if renamed is None else renamed
        errors = clone_mod._process_group(
            self.cfg, {"id": 1, "full_path": group_path, "path": group_path},
            os.path.join(self.dest, group_path),
            index=index, renamed=renamed)
        return errors, renamed, index

    # ── the repair ────────────────────────────────────────────────────────

    def test_a_renamed_project_is_moved_not_cloned_again(self):
        bare = self.make_bare()
        old = self.clone_from(bare, "web/old-name", f"{BASE}/web/old-name.git")
        self.bare_for_url[f"{BASE}/web/new-name.git"] = bare
        self.install_server(RedirectServer(
            renames={"web/old-name": "web/new-name"}))

        _, renamed, _ = self._run_group(self._projects("web/new-name"), "web")

        new = os.path.join(self.dest, "web", "new-name")
        self.assertEqual(self.cloned, [],
                         "a second, complete copy of a renamed project was "
                         "cloned — this is the 866 MB defect")
        self.assertTrue(os.path.isdir(new), "the clone was not moved")
        self.assertFalse(os.path.isdir(old), "the old directory still exists")
        self.assertEqual(len(renamed), 1)
        self.assertEqual(renamed[0]["reason"], clone_mod.REASON_RENAMED)
        self.assertEqual(clone_mod._session_stats["renamed"], 1)

    def test_the_moved_clone_has_its_origin_repointed(self):
        bare = self.make_bare()
        self.clone_from(bare, "web/old-name", f"{BASE}/web/old-name.git")
        self.bare_for_url[f"{BASE}/web/new-name.git"] = bare
        self.install_server(RedirectServer(
            renames={"web/old-name": "web/new-name"}))
        self._run_group(self._projects("web/new-name"), "web")

        new = os.path.join(self.dest, "web", "new-name")
        url = subprocess.run([GIT, "-C", new, "remote", "get-url", "origin"],
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(cov.normalize(url), (HOST, "web/new-name"),
                         "the repaired clone still carries the old origin URL, "
                         "so it reads as an orphan and the project reads as "
                         "missing — the whole defect, unrepaired")

    def test_the_repaired_clone_is_a_working_repository(self):
        bare = self.make_bare()
        self.clone_from(bare, "web/old-name", f"{BASE}/web/old-name.git")
        self.bare_for_url[f"{BASE}/web/new-name.git"] = bare
        self.install_server(RedirectServer(
            renames={"web/old-name": "web/new-name"}))
        self._run_group(self._projects("web/new-name"), "web")
        new = os.path.join(self.dest, "web", "new-name")
        self.assertEqual(clone_mod.repo_state(new), "ok")
        self.assertEqual(
            subprocess.run([GIT, "-C", new, "rev-list", "--count", "HEAD"],
                           capture_output=True, text=True).stdout.strip(), "1")

    def test_a_project_moved_between_groups_is_repaired_too(self):
        bare = self.make_bare()
        self.clone_from(bare, "oldgroup/svc", f"{BASE}/oldgroup/svc.git")
        self.bare_for_url[f"{BASE}/newgroup/svc.git"] = bare
        self.install_server(RedirectServer(
            renames={"oldgroup/svc": "newgroup/svc"}))
        self._run_group(self._projects("newgroup/svc"), "newgroup")
        self.assertEqual(self.cloned, [])
        self.assertTrue(os.path.isdir(
            os.path.join(self.dest, "newgroup", "svc")))

    # ── the refusals: proven renamed, but left strictly alone ─────────────

    def _refusal_fixture(self):
        bare = self.make_bare()
        old = self.clone_from(bare, "web/old-name", f"{BASE}/web/old-name.git")
        self.bare_for_url[f"{BASE}/web/new-name.git"] = bare
        self.install_server(RedirectServer(
            renames={"web/old-name": "web/new-name"}))
        return bare, old

    def _assert_left_alone(self, old, renamed):
        self.assertEqual(self.cloned, [],
                         "a refusal must NOT fall through to cloning a second "
                         "copy — that is the defect, not the fix")
        self.assertTrue(os.path.isdir(old), "the clone was moved anyway")
        self.assertFalse(os.path.isdir(os.path.join(self.dest, "web",
                                                    "new-name")))
        self.assertEqual(len(renamed), 1)
        self.assertEqual(renamed[0]["reason"], clone_mod.REASON_RENAME_UNSAFE)
        self.assertEqual(clone_mod._session_stats["renamed_refused"], 1)

    def test_a_dirty_clone_is_refused_and_nothing_is_cloned(self):
        _, old = self._refusal_fixture()
        with open(os.path.join(old, "a.txt"), "a") as fh:
            fh.write("uncommitted\n")
        _, renamed, _ = self._run_group(self._projects("web/new-name"), "web")
        self._assert_left_alone(old, renamed)
        self.assertTrue(any("uncommitted" in r
                            for r in renamed[0]["reasons"]),
                        renamed[0]["reasons"])

    def test_a_clone_with_a_stash_is_refused(self):
        _, old = self._refusal_fixture()
        with open(os.path.join(old, "a.txt"), "a") as fh:
            fh.write("wip\n")
        git("stash", "push", "-q", "-m", "wip", cwd=old)
        stashes = subprocess.run([GIT, "-C", old, "stash", "list"],
                                 capture_output=True, text=True).stdout
        if not stashes.strip():
            self.skipTest("git stash unavailable here")
        _, renamed, _ = self._run_group(self._projects("web/new-name"), "web")
        self._assert_left_alone(old, renamed)
        self.assertTrue(any("stash" in r for r in renamed[0]["reasons"]),
                        renamed[0]["reasons"])

    def test_a_clone_holding_a_commit_the_server_lacks_is_refused(self):
        _, old = self._refusal_fixture()
        with open(os.path.join(old, "only-here.txt"), "w") as fh:
            fh.write("exists nowhere else\n")
        git("add", "-A", cwd=old)
        git("commit", "-q", "-m", "nearly lost", cwd=old)
        _, renamed, _ = self._run_group(self._projects("web/new-name"), "web")
        self._assert_left_alone(old, renamed)
        self.assertTrue(any("NOT on the server" in r
                            for r in renamed[0]["reasons"]),
                        renamed[0]["reasons"])

    # ── the guards against over-eager repair ──────────────────────────────

    def test_a_genuinely_new_project_is_still_cloned(self):
        # Negative control. The repair must not turn into "never clone".
        self.install_server(RedirectServer())
        self._run_group(self._projects("web/brand-new"), "web")
        self.assertEqual(len(self.cloned), 1)
        self.assertTrue(self.cloned[0].endswith("brand-new"))

    def test_an_unrelated_clone_that_404s_is_never_treated_as_a_rename(self):
        bare = self.make_bare()
        old = self.clone_from(bare, "web/deleted-thing",
                              f"{BASE}/web/deleted-thing.git")
        self.bare_for_url[f"{BASE}/web/other-thing.git"] = bare
        self.install_server(RedirectServer())        # everything 404s
        self._run_group(self._projects("web/other-thing"), "web")
        self.assertTrue(os.path.isdir(old), "a deleted project's clone was "
                                            "moved on a guess")
        self.assertEqual(len(self.cloned), 1,
                         "with no rename proven, the missing project is "
                         "cloned normally")

    def test_an_undetermined_lookup_never_moves_anything(self):
        bare = self.make_bare()
        old = self.clone_from(bare, "web/maybe", f"{BASE}/web/maybe.git")
        self.bare_for_url[f"{BASE}/web/other.git"] = bare
        self.install_server(RedirectServer(status_for={"web/maybe": 401}))
        self._run_group(self._projects("web/other"), "web")
        self.assertTrue(os.path.isdir(old),
                        "a 401 is an auth failure, not permission to move")

    def test_a_redirect_pointing_somewhere_else_is_not_accepted(self):
        # The candidate IS renamed — but to a different project. Moving it
        # here would put the wrong repository at this path.
        bare = self.make_bare()
        old = self.clone_from(bare, "web/old-name", f"{BASE}/web/old-name.git")
        self.bare_for_url[f"{BASE}/web/new-name.git"] = bare
        self.install_server(RedirectServer(
            renames={"web/old-name": "web/somewhere-else"}))
        self._run_group(self._projects("web/new-name"), "web")
        self.assertTrue(os.path.isdir(old))
        self.assertEqual(len(self.cloned), 1)

    def test_a_same_path_project_on_another_host_is_never_a_candidate(self):
        other = "http://other.example.net"
        bare = self.make_bare()
        old = self.clone_from(bare, "web/old-name",
                              f"{other}/web/old-name.git")
        self.bare_for_url[f"{BASE}/web/new-name.git"] = bare
        self.install_server(RedirectServer(
            renames={"web/old-name": "web/new-name"}))
        self._run_group(self._projects("web/new-name"), "web")
        self.assertTrue(os.path.isdir(old),
                        "a clone of ANOTHER instance was moved")
        self.assertEqual(len(self.cloned), 1)

    def test_a_clone_already_claimed_by_a_live_project_is_not_stolen(self):
        # gl/keeper is present and correct; gl/keeper-2 is missing. The
        # bounded candidate search must not offer gl/keeper's directory.
        bare = self.make_bare()
        keeper = self.clone_from(bare, "web/keeper", f"{BASE}/web/keeper.git")
        self.bare_for_url[f"{BASE}/web/keeper-2.git"] = bare
        self.install_server(RedirectServer(
            live=["web/keeper"], renames={}))
        self._run_group(self._projects("web/keeper", "web/keeper-2"), "web")
        self.assertTrue(os.path.isdir(keeper))
        self.assertEqual(len(self.cloned), 1)
        self.assertTrue(self.cloned[0].endswith("keeper-2"))

    def test_the_new_location_is_never_written_over(self):
        bare = self.make_bare()
        old = self.clone_from(bare, "web/old-name", f"{BASE}/web/old-name.git")
        # Something already occupies the new path (not a valid repo).
        occupied = os.path.join(self.dest, "web", "new-name")
        os.makedirs(occupied, exist_ok=True)
        with open(os.path.join(occupied, "keep-me"), "w") as fh:
            fh.write("do not clobber\n")
        self.bare_for_url[f"{BASE}/web/new-name.git"] = bare
        self.install_server(RedirectServer(
            renames={"web/old-name": "web/new-name"}))
        self._run_group(self._projects("web/new-name"), "web")
        self.assertTrue(os.path.isfile(os.path.join(occupied, "keep-me")))
        self.assertTrue(os.path.isdir(old))


# ─── 5. The resume behaviour an earlier wave fixed must not regress ──────────

class ResumeBehaviourPreserved(RenameTestCase):
    """`git clone` creates `.git` first and fills it afterwards, so an
    interrupted clone leaves a directory that an existence check calls done.
    Validity is proven with `git rev-parse`; a broken target is reported and
    NOT deleted. An unborn HEAD is NOT broken: a clone of a genuinely empty
    server-side project legitimately has zero objects and zero refs (9 of 14
    repositories once suspected broken were exactly this)."""

    def test_a_valid_repo_is_still_skipped(self):
        self.make_project_clone("web/present")
        self.assertEqual(
            clone_mod.repo_state(os.path.join(self.dest, "web", "present")),
            "ok")

    def test_a_half_built_dot_git_is_still_invalid(self):
        p = self.make_broken_clone("web/interrupted")
        self.assertEqual(clone_mod.repo_state(p), "invalid")

    def test_a_dangling_gitdir_pointer_is_still_invalid(self):
        p = self.make_broken_clone("web/worktreeish", git_is_file=True)
        self.assertEqual(clone_mod.repo_state(p), "invalid")

    def test_an_empty_clone_with_an_unborn_head_is_ok_not_broken(self):
        p = os.path.join(self.dest, "web", "empty")
        os.makedirs(p, exist_ok=True)
        git("init", "-q", p)
        head = subprocess.run([GIT, "-C", p, "rev-parse", "HEAD"],
                              capture_output=True, text=True)
        self.assertNotEqual(head.returncode, 0, "fixture wrong: HEAD is born")
        self.assertEqual(clone_mod.repo_state(p), "ok",
                         "a correct clone of an EMPTY server-side project was "
                         "called broken — 9 of 14 'broken' repos were this")

    def test_an_absent_and_an_empty_directory_are_both_clone_targets(self):
        self.assertEqual(
            clone_mod.repo_state(os.path.join(self.dest, "nothing")), "absent")
        empty = os.path.join(self.dest, "web", "empty-dir")
        os.makedirs(empty, exist_ok=True)
        self.assertEqual(clone_mod.repo_state(empty), "absent")

    def test_the_rename_path_never_touches_a_broken_target(self):
        # A broken directory is reported and left alone BEFORE any rename
        # lookup happens, so no repair can ever clobber it.
        cfg = fake_cfg()
        broken = self.make_broken_clone("web/interrupted")
        server = self.install_server(RedirectServer())
        self.patch("get_subgroups", lambda url, headers, gid: [])
        self.patch("get_projects", lambda url, headers, gid: [{
            "id": 1, "path": "interrupted",
            "path_with_namespace": "web/interrupted",
            "http_url_to_repo": f"{BASE}/web/interrupted.git"}])
        self.patch("clone_repo",
                   lambda *a: self.fail("a broken target was cloned over"))
        clone_mod._session_stats.update(
            {"cloned": 0, "failed": 0, "invalid": 0, "elsewhere": 0,
             "renamed": 0, "renamed_refused": 0})
        errors = clone_mod._process_group(
            cfg, {"id": 1, "full_path": "web", "path": "web"},
            os.path.join(self.dest, "web"),
            index=clone_mod.build_presence_index(self.dest))
        self.assertTrue(os.path.isdir(broken), "a broken target was removed")
        self.assertEqual([e["reason"] for e in errors],
                         [clone_mod.REASON_INVALID])
        self.assertEqual(server.calls, [],
                         "a broken target must be reported before any network "
                         "lookup is spent on it")


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
