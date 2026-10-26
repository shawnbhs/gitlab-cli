# tests/test_w9_2_seam_wiring.py — W9-2: the four seams gitlab_cli.py owns.
#
# Four earlier subagents each built a correct, tested function and named
# gitlab_cli.py as its missing call site. The functions were never the gap;
# the CALL was. These tests pin the call, not the callee — each one fails
# against the pre-W9-2 gitlab_cli.py and passes against the wired one.
#
# Nothing here touches the live clone tree, the live instances.json or the
# live error log: _bootstrap pulls in _error_log_guard, every fixture is a
# tempdir, and every remote/API interaction is a stub.

import io
import contextlib
import os
import stat
import sys
import tempfile
import shutil
import unittest

import _bootstrap  # noqa: F401  (path + live-error-log isolation)
from _bootstrap import PROJECT_ROOT

import caps  # noqa: F401  (still imported by gitlab_cli)
import coverage as cov
import gitlab_cli as G
import printer
import remotes
import setup as setup_mod
from api import ApiError


def _src(name="gitlab_cli.py"):
    with open(os.path.join(PROJECT_ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def _run(fn, *a, _input=None, **kw):
    """Call fn capturing stdout+stderr, optionally feeding input()."""
    import builtins
    out, err = io.StringIO(), io.StringIO()
    real = builtins.input
    if _input is not None:
        builtins.input = lambda *x, **y: _input
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            res = fn(*a, **kw)
    finally:
        builtins.input = real
    return res, out.getvalue(), err.getvalue()


SCOPE = {"name": "globex", "folder": "", "base": "/base", "root": "/base",
         "others": [{"name": "Hub", "folder": "kz", "root": "/base/kz"}]}


# ─── Seam 1 · remotes scope (W8-2 H1/H2/H3) ──────────────────────────────────

class RemotesScreenStatesItsScope(unittest.TestCase):
    """The count on screen must be the count the screen covers."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="w9_2_rem_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = {"name": "globex", "url": "http://git.example.net",
                    "headers": {}, "master_dir": self.tmp, "folder": ""}
        # 10 repos walked, 3 of them this instance's.
        self.walked = [f"/base/kz/foreign{i}" for i in range(7)] + \
                      [f"/base/own{i}" for i in range(3)]
        self._patch()

    def _patch(self):
        import pull
        self._orig = (pull._find_git_repos, remotes.active_scope,
                      remotes.audit, remotes.scrub)
        pull._find_git_repos = lambda d: list(self.walked)
        remotes.active_scope = lambda: SCOPE
        self.audit_calls, self.scrub_calls = [], []

        def fake_audit(repos, check_network=False, scope=None,
                       enforce_scope=True):
            self.audit_calls.append({"repos": list(repos), "scope": scope})
            rows = remotes.AuditRows()
            rows.scope, rows.foreign = scope, {"Hub": 7}
            for p in repos:
                rows.append({"path": p, "name": os.path.basename(p),
                             "remotes": [], "count": 1, "leaks": ["origin"],
                             "has_creds": True, "hosts": [],
                             "unreachable": []})
            return rows

        def fake_scrub(repos, save_to_store=True, dry_run=False, scope=None,
                       enforce_scope=True):
            self.scrub_calls.append({"repos": list(repos), "scope": scope})
            return {"scanned": len(repos), "cleaned": 0, "saved": 0,
                    "failed": 0, "instance": remotes.scope_label(scope),
                    "skipped_other_instance": 0, "skipped_by_instance": {}}

        remotes.audit, remotes.scrub = fake_audit, fake_scrub
        self.addCleanup(self._restore)

    def _restore(self):
        import pull
        (pull._find_git_repos, remotes.active_scope,
         remotes.audit, remotes.scrub) = self._orig

    def test_the_printed_count_is_the_in_scope_count_not_the_walk(self):
        _r, out, _e = _run(G._run_remotes, self.cfg, _input="n")
        self.assertIn("3 repos", out,
                      "the screen must print the IN-SCOPE count")
        self.assertNotIn("10 repos found", out,
                         "the unfiltered walk count must never be printed as "
                         "'repos found' — it is the larger, wrong number")

    def test_the_printed_count_names_the_instance(self):
        _r, out, _e = _run(G._run_remotes, self.cfg, _input="n")
        self.assertIn("globex", out)

    def test_audit_receives_only_in_scope_repos_and_the_scope(self):
        _run(G._run_remotes, self.cfg, _input="n")
        self.assertEqual(1, len(self.audit_calls))
        call = self.audit_calls[0]
        self.assertEqual(3, len(call["repos"]))
        self.assertTrue(all("kz" not in p for p in call["repos"]))
        self.assertIs(SCOPE, call["scope"])

    def test_confirmation_states_instance_and_count_before_asking(self):
        _r, out, _e = _run(G._run_remotes, self.cfg, _input="n")
        expected_fragment = "About to rewrite remote URLs in 3 of 3"
        self.assertIn(expected_fragment, out)
        self.assertIn("globex", out.split(expected_fragment)[1][:200]
                      + out.split(expected_fragment)[0][-200:])

    def test_the_confirmation_text_comes_from_the_shared_helper(self):
        """Wording must not be re-invented here: the helper owns it."""
        src = _src()
        self.assertIn("confirm_scrub_message", src)

    def test_declining_runs_no_scrub_at_all(self):
        _run(G._run_remotes, self.cfg, _input="n")
        self.assertEqual([], self.scrub_calls)

    def test_confirming_passes_the_scope_through_to_scrub(self):
        _run(G._run_remotes, self.cfg, _input="y")
        self.assertEqual(1, len(self.scrub_calls))
        self.assertIs(SCOPE, self.scrub_calls[0]["scope"],
                      "scrub must be told the scope explicitly — the write "
                      "path may not depend on it re-reading the config")
        self.assertEqual(3, len(self.scrub_calls[0]["repos"]))

    def test_empty_scope_says_which_instance_is_empty(self):
        self.walked = ["/base/kz/a", "/base/kz/b"]
        _r, out, _e = _run(G._run_remotes, self.cfg, _input="n")
        self.assertIn("globex", out)
        self.assertIn("Hub", out, "the excluded owner must still be named")


class RemotesCliFixDiscloses(unittest.TestCase):
    """--fix may skip the PROMPT. It may not skip the DISCLOSURE."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="w9_2_fix_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = {"name": "globex", "url": "http://git.example.net",
                    "headers": {}, "master_dir": self.tmp, "folder": ""}
        import pull
        self._orig = (pull._find_git_repos, remotes.active_scope,
                      remotes.audit, remotes.scrub)
        self.walked = [f"/base/kz/f{i}" for i in range(4)] + \
                      [f"/base/own{i}" for i in range(2)]
        pull._find_git_repos = lambda d: list(self.walked)
        remotes.active_scope = lambda: SCOPE
        self.scrub_calls = []

        def fake_audit(repos, check_network=False, scope=None,
                       enforce_scope=True):
            rows = remotes.AuditRows()
            rows.scope, rows.foreign = scope, {"Hub": 4}
            for p in repos:
                rows.append({"path": p, "name": os.path.basename(p),
                             "remotes": [], "count": 1, "leaks": ["origin"],
                             "has_creds": True, "hosts": [],
                             "unreachable": []})
            return rows

        def fake_scrub(repos, save_to_store=True, dry_run=False, scope=None,
                       enforce_scope=True):
            self.scrub_calls.append({"repos": list(repos), "scope": scope})
            return {"scanned": len(repos), "cleaned": 1, "saved": 1,
                    "failed": 0, "instance": remotes.scope_label(scope),
                    "skipped_other_instance": 4, "skipped_by_instance": {}}

        remotes.audit, remotes.scrub = fake_audit, fake_scrub
        self.addCleanup(self._restore)

    def _restore(self):
        import pull
        (pull._find_git_repos, remotes.active_scope,
         remotes.audit, remotes.scrub) = self._orig

    def test_fix_states_instance_and_count_before_rewriting(self):
        def boom(*a, **k):
            raise AssertionError("input() must NOT be called by --fix")
        import builtins
        real, builtins.input = builtins.input, boom
        try:
            _r, out, _e = _run(G._cmd_remotes_cli, self.cfg, ["--fix"])
        finally:
            builtins.input = real
        self.assertIn("About to rewrite remote URLs in 2 of 2", out)
        self.assertIn("globex", out)

    def test_fix_still_scrubs_and_still_passes_the_scope(self):
        _run(G._cmd_remotes_cli, self.cfg, ["--fix"])
        self.assertEqual(1, len(self.scrub_calls))
        self.assertIs(SCOPE, self.scrub_calls[0]["scope"])
        self.assertEqual(2, len(self.scrub_calls[0]["repos"]))

    def test_without_fix_nothing_is_rewritten(self):
        _run(G._cmd_remotes_cli, self.cfg, [])
        self.assertEqual([], self.scrub_calls)


# ─── Seam 2 · one coverage sentence (W8-6 §5.2) ──────────────────────────────

FLAT = (
    [{"path_with_namespace": f"g/p{i}",
      "namespace": {"kind": "group", "full_path": "g"}} for i in range(5)]
    + [{"path_with_namespace": f"u{i}/p",
        "namespace": {"kind": "user", "full_path": f"u{i}"}}
       for i in range(3)]
)


class CoverageIsOneLabelledSentence(unittest.TestCase):

    def setUp(self):
        self.cfg = {"name": "x", "url": "http://git.example.net", "headers": {}}
        self._orig = setup_mod._remote_projects
        self.seen = []

        def fake(cfg):
            self.seen.append("called")
            return list(FLAT), ""
        setup_mod._remote_projects = fake
        self.addCleanup(setattr, setup_mod, "_remote_projects", self._orig)

    def _index(self, n_group, n_user=0):
        idx = {("git.example.net", f"g/p{i}"): "/d" for i in range(n_group)}
        idx.update({("git.example.net", f"u{i}/p"): "/d" for i in range(n_user)})
        return idx

    def test_counts_come_from_the_PRE_partition_flat_list(self):
        """W8-6's warning: the filtered list zeroes excluded_user."""
        counts = G._instance_coverage_counts(self.cfg, self._index(5))
        self.assertEqual(5, counts["group_total"])
        self.assertEqual(3, counts["user_total"],
                         "user-namespace projects must still be COUNTED; a "
                         "post-partition list would report 0 here and put an "
                         "unexplained number back on screen")
        self.assertEqual(3, counts["excluded_user"])
        self.assertEqual(8, counts["all_total"])

    def test_it_refuses_to_guess_when_the_answer_is_unknown(self):
        setup_mod._remote_projects = lambda cfg: (None, "VPN down")
        self.assertIsNone(G._instance_coverage_counts(self.cfg, {}))

    def test_it_refuses_when_the_accounting_does_not_reconcile(self):
        # W10: the accounting now lives in coverage.py, and
        # _instance_coverage_counts() calls it there. Patching caps would
        # patch nothing, so this test would pass while asserting nothing.
        self._orig_cc = cov.coverage_counts
        cov.coverage_counts = lambda *a, **k: {
            "group_total": 1, "user_total": 1, "unknown_total": 0,
            "all_total": 9}
        self.addCleanup(setattr, cov, "coverage_counts", self._orig_cc)
        self.assertIsNone(G._instance_coverage_counts(self.cfg, {}))

    def test_an_api_failure_never_takes_the_menu_down(self):
        def boom(cfg):
            raise ApiError(500, "nope", "http://git.example.net/api/v4/projects")
        setup_mod._remote_projects = boom
        self.assertIsNone(G._instance_coverage_counts(self.cfg, {}))

    def test_the_rendered_line_explains_both_denominators(self):
        counts = G._instance_coverage_counts(self.cfg, self._index(5))
        line = cov.coverage_line(counts)
        self.assertIn("5/5 group projects on disk", line)
        self.assertIn("8 on the instance", line)
        self.assertIn(cov.POLICY_USER_NAMESPACE, line)

    def test_the_group_list_no_longer_prints_an_unqualified_fraction(self):
        """The fraction may stay; the silent ambiguity may not.

        A bare `3/53 projects on disk` reads as instance coverage, which is
        the number Doctor prints against a LARGER denominator. Every render
        path must carry a qualifier saying which population it counts.
        """
        body = _src().split("def pick_group", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("coverage_line", body)
        self.assertIn("(group namespaces)", body,
                      "the fallback fraction must name its population")

    def test_every_fallback_still_carries_the_noun(self):
        """Even with no instance total, the fraction is never unqualified."""
        body = _src().split("def pick_group", 1)[1].split("\ndef ", 1)[0]
        import re
        for m in re.finditer(r'\{done\}/\{total\}[^"\']*', body):
            self.assertIn("group namespaces", m.group(0))


# ─── Seam 3 · user namespaces (W8-5 §10.1) ───────────────────────────────────

def _uprojects(n):
    return [{"path_with_namespace": f"person{i}/p",
             "namespace": {"kind": "user", "full_path": f"person{i}"}}
            for i in range(n)]


class UserNamespaceGate(unittest.TestCase):

    def setUp(self):
        self.cfg = {"name": "x", "url": "http://git.example.net", "headers": {}}
        self._orig = setup_mod.fetch_user_namespace_projects
        self.addCleanup(setattr, setup_mod, "fetch_user_namespace_projects",
                        self._orig)

    def _stub(self, projects, scope, lines=("disclosure line",)):
        setup_mod.fetch_user_namespace_projects = \
            lambda cfg, *a, **k: (list(projects), scope, list(lines))

    def test_there_is_a_caller_at_all(self):
        """The whole point of this seam: the function had none."""
        src = _src()
        self.assertIn("fetch_user_namespace_projects", src)

    def test_the_clone_all_path_calls_the_gate(self):
        src = _src()
        body = src.split("clone_all", 2)[2]
        self.assertIn("_user_namespace_gate", body.split("pull_all")[0])

    def test_scope_off_STILL_prints_the_disclosure(self):
        """A silent exclusion is the bug class this project exists to kill."""
        self._stub([], "off", ["EXCLUDED by policy", "to include them: ..."])
        got, out, _e = _run(G._user_namespace_gate, self.cfg)
        self.assertEqual([], got)
        self.assertIn("EXCLUDED by policy", out)
        self.assertIn("to include them", out)
        self.assertIn("scope=off", out)

    def test_scope_all_requires_an_explicit_y(self):
        self._stub(_uprojects(4), "all")
        got, out, _e = _run(G._user_namespace_gate, self.cfg, _input="y")
        self.assertEqual(4, len(got))
        self.assertIn("confirmed", out)

    def test_scope_all_excludes_on_anything_that_is_not_y(self):
        for answer in ("", "n", "N", "yes please", "Y ES", "1", "ok"):
            self._stub(_uprojects(4), "all")
            got, out, _e = _run(G._user_namespace_gate, self.cfg,
                                _input=answer)
            self.assertEqual([], got, f"{answer!r} must not mean yes")
            self.assertIn("EXCLUDED", out)

    def test_scope_all_warns_that_these_are_other_peoples_repositories(self):
        self._stub(_uprojects(4), "all")
        _got, out, _e = _run(G._user_namespace_gate, self.cfg, _input="n")
        self.assertIn("private", out.lower())
        self.assertIn("4 project(s)", out)

    def test_scope_own_needs_no_confirmation(self):
        """`own` is your own namespace; the y/N gate is for `all`."""
        self._stub(_uprojects(2), "own")
        def boom(*a, **k):
            raise AssertionError("scope=own must not prompt")
        import builtins
        real, builtins.input = builtins.input, boom
        try:
            got, out, _e = _run(G._user_namespace_gate, self.cfg)
        finally:
            builtins.input = real
        self.assertEqual(2, len(got))

    def test_ctrl_c_at_the_prompt_means_no(self):
        self._stub(_uprojects(4), "all")
        import builtins
        real = builtins.input

        def interrupt(*a, **k):
            raise KeyboardInterrupt
        builtins.input = interrupt
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                got = G._user_namespace_gate(self.cfg)
        finally:
            builtins.input = real
        self.assertEqual([], got)
        self.assertIn("EXCLUDED", out.getvalue())

    def test_a_discovery_failure_is_UNKNOWN_and_is_logged_not_swallowed(self):
        def boom(cfg, *a, **k):
            raise ApiError(500, "nope", "http://git.example.net/api/v4/projects")
        setup_mod.fetch_user_namespace_projects = boom
        import errors
        before = len(errors._read_errors())
        got, out, _e = _run(G._user_namespace_gate, self.cfg)
        self.assertEqual([], got)
        self.assertIn("UNKNOWN", out)
        self.assertGreater(len(errors._read_errors()), before,
                           "a failed discovery must reach the error log")

    def test_the_gate_never_clones(self):
        names = G._user_namespace_gate.__code__.co_names
        for forbidden in ("cmd_clone", "clone_repo", "subprocess", "rmtree",
                          "makedirs"):
            self.assertNotIn(forbidden, names)


# ─── Seam 4 · harden-perms (W8-4 §4.2/§11.1) ─────────────────────────────────

class HardenPermsCommand(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="w9_2_perm_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "r1")
        os.makedirs(self.repo)
        _bootstrap.git("init", "-q", cwd=self.repo)
        self.gitdir = os.path.join(self.repo, ".git")
        self.cfgfile = os.path.join(self.gitdir, "config")
        os.chmod(self.cfgfile, 0o644)
        os.chmod(self.gitdir, 0o755)
        self.cfg = {"name": "x", "url": "http://git.example.net", "headers": {},
                    "master_dir": self.tmp, "folder": ""}

    def _census(self):
        import git as git_mod
        out = {}
        for _wt, gd in git_mod.iter_repos(self.tmp):
            for name in ("",) + git_mod._SENSITIVE_GIT_FILES:
                p = os.path.join(gd, name) if name else gd
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                out[p] = (stat.S_IMODE(st.st_mode), st.st_mtime_ns,
                          st.st_ctime_ns)
        return out

    def test_the_command_is_dispatched(self):
        src = _src()
        self.assertIn('args[0] == "harden-perms"', src)
        self.assertIn("harden-perms", src.split("def print_usage", 1)[1]
                      .split("\ndef ", 1)[0], "the command must be documented")

    def test_dry_run_changes_no_mode_no_mtime_and_no_ctime(self):
        """A chmod bumps ctime even when the mode already matches — so ctime
        is the assertion that actually proves nothing was written."""
        before = self._census()
        self.assertTrue(before)
        _r, out, _e = _run(G._cmd_harden_perms, self.cfg, ["--dry-run"])
        after = self._census()
        self.assertEqual(before, after,
                         "the dry run modified something on disk")
        self.assertEqual(0o644, stat.S_IMODE(os.lstat(self.cfgfile).st_mode))

    def test_dry_run_reports_what_it_would_change(self):
        _r, out, _e = _run(G._cmd_harden_perms, self.cfg, ["--dry-run"])
        self.assertIn("DRY RUN", out)
        self.assertIn("would change", out)
        self.assertIn("nothing was modified", out)

    def test_a_real_run_does_change_it(self):
        """The control: without --dry-run the same command tightens."""
        _r, out, _e = _run(G._cmd_harden_perms, self.cfg, [])
        self.assertEqual(0o600, stat.S_IMODE(os.lstat(self.cfgfile).st_mode))
        self.assertEqual(0o700, stat.S_IMODE(os.lstat(self.gitdir).st_mode))
        self.assertNotIn("DRY RUN", out)

    def test_dry_run_and_real_run_agree_on_the_count(self):
        _r, dry_out, _e = _run(G._cmd_harden_perms, self.cfg, ["--dry-run"])
        _r, real_out, _e = _run(G._cmd_harden_perms, self.cfg, [])
        import re
        dry_n = int(re.search(r"files would change: (\d+)", dry_out).group(1))
        real_n = int(re.search(r"files changed: (\d+)", real_out).group(1))
        self.assertEqual(dry_n, real_n)

    def test_an_extra_root_can_be_named_on_the_command_line(self):
        other = tempfile.mkdtemp(prefix="w9_2_other_")
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        r2 = os.path.join(other, "r2")
        os.makedirs(r2)
        _bootstrap.git("init", "-q", cwd=r2)
        os.chmod(os.path.join(r2, ".git", "config"), 0o644)
        _r, out, _e = _run(G._cmd_harden_perms, self.cfg, ["--dry-run", other])
        self.assertIn(other, out)
        self.assertNotIn(self.tmp + "\n", out.replace(other, ""))


# ─── Point 5 · ApiError is an error, not a crash ─────────────────────────────

class ApiErrorIsHandled(unittest.TestCase):

    def test_the_process_boundary_still_catches_it(self):
        src = _src()
        body = src.split("def main()", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("except ApiError", body)

    def test_the_menu_now_survives_one(self):
        def boom():
            raise ApiError(500, "server on fire",
                           "http://git.example.net/api/v4/groups")
        res, out, err = _run(G._guarded, "w9-2 test", boom)
        self.assertIs(G._GUARD_FAILED, res)
        self.assertIn("HTTP 500", err)
        self.assertIn("menu is still open", err)

    def test_a_recovered_failure_still_reaches_the_error_log(self):
        import errors
        before = len(errors._read_errors())

        def boom():
            raise ApiError(401, "bad token",
                           "http://git.example.net/api/v4/groups")
        _run(G._guarded, "w9-2 test :: log", boom)
        after = errors._read_errors()
        self.assertGreater(len(after), before)
        self.assertTrue(any("w9-2 test :: log" == r.get("context")
                            for r in after))

    def test_a_recovered_failure_never_prints_a_traceback(self):
        def boom():
            raise ApiError(500, "x", "http://git.example.net/api/v4/g")
        _res, _out, err = _run(G._guarded, "w9-2 test", boom)
        self.assertNotIn("Traceback (most recent call last)", err)

    def test_it_says_401_is_not_a_network_problem(self):
        def boom():
            raise ApiError(401, "unauthorized",
                           "http://git.example.net/api/v4/groups")
        _res, _out, err = _run(G._guarded, "w9-2 test", boom)
        self.assertIn("401", err)
        self.assertNotIn("Could not reach", err)

    def test_keyboard_interrupt_and_sysexit_pass_straight_through(self):
        def kb():
            raise KeyboardInterrupt

        def se():
            raise SystemExit(0)
        with self.assertRaises(KeyboardInterrupt):
            G._guarded("c", kb)
        with self.assertRaises(SystemExit):
            G._guarded("c", se)

    def test_a_logging_failure_is_reported_not_hidden(self):
        import errors
        orig = errors.log_error

        def broken(*a, **k):
            raise OSError("disk full")
        errors.log_error = broken
        self.addCleanup(setattr, errors, "log_error", orig)

        def boom():
            raise ApiError(500, "x", "http://git.example.net/api/v4/g")
        _res, _out, err = _run(G._guarded, "w9-2 test", boom)
        self.assertIn("could not write the error log", err)

    def test_the_interactive_sections_are_guarded(self):
        src = _src()
        body = src.split("def interactive_mode", 1)[1].split("\ndef ", 1)[0]
        for target in ("_run_remotes", "doctor", "pick_group",
                       "cmd_members_list"):
            self.assertIn(target, body)
        self.assertGreaterEqual(body.count("_guarded"), 4,
                                "every API-touching menu section must be "
                                "guarded, not just one")


# ─── Papercut · pick_action was the only case-sensitive prompt ───────────────

class PickActionNormalisesCase(unittest.TestCase):

    def _answer(self, text, status="✅"):
        import builtins
        real, builtins.input = builtins.input, lambda *a, **k: text
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                return G.pick_action(status, False, {"ok": True}, {})
        finally:
            builtins.input = real

    def test_uppercase_B_goes_back_like_every_other_screen(self):
        self.assertEqual("back", self._answer("B"))
        self.assertEqual("back", self._answer("  b  "))

    def test_uppercase_Q_exits(self):
        with self.assertRaises(SystemExit):
            self._answer("Q")

    def test_it_uses_the_shared_helper(self):
        self.assertEqual("c", printer.menu_key("  C "))
        src = _src()
        body = src.split("def pick_action", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("menu_key", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
