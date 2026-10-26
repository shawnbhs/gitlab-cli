#!/usr/bin/env python3
"""
tests/test_w9_7_exit_semantics.py — the referee's exit code must MEAN something.

THE FAILURE THIS PINS
---------------------
`scripts/verify_mirror.py` used to return exactly two non-zero codes: 1 for
"something missing or dirty" and 2 for an operational error. `1` therefore
conflated two unrelated states:

  * `missing` — the server has a project and the mirror does not. The mirror
    has failed at the only job it has.
  * `config_mode_not_0600`, duplicate clones, orphans deleted upstream — the
    mirror is COMPLETE and the user has a tidiness problem.

For most of this project's recent history the referee exited 1 while `missing`
was 0 on both instances — at one point driven entirely by hundreds of config-mode
flags on otherwise perfect repositories. An exit code that is always 1 is an
exit code nobody reads, and this tool exists specifically because a status
that cannot distinguish states teaches its user to ignore it.

WHAT IS ASSERTED HERE
---------------------
  * the five codes exist, are distinct, and 0 is never repurposed
  * correctness failures (missing / broken / shallow) -> 1
  * an operational failure (bad token, unreachable, missing clone root) -> 2,
    and is NEVER reportable as a clean mirror
  * a credential in a .git/config -> 3, and cannot be silenced by "the mirror
    is complete"
  * hygiene-only findings -> 4, distinguishable from a correctness failure
  * precedence is by SEVERITY, not by numeric value
  * the verdict line names the category that fired
  * --legacy-exit-codes collapses 3 and 4 back to 1 for old consumers
  * the group cross-check disagreement, and an instance that enumerates zero
    projects, are OPERATIONAL (exit 2) — the "silent empty list" failure
  * the referee is still independent: no CLI imports, stdlib only

No network, no token, no GitLab server. Every non-zero case below is
CONSTRUCTED from a temporary fixture tree and a stubbed api_request.
"""

import ast
import importlib.util
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(PROJECT_ROOT, "scripts", "verify_mirror.py")
WRAPPER = os.path.join(PROJECT_ROOT, "scripts", "verify-mirror")

sys.path.insert(0, HERE)
from _bootstrap import TempTreeTestCase  # noqa: E402


def load_module():
    """Import the referee by path, without putting the CLI root on sys.path."""
    spec = importlib.util.spec_from_file_location("w9_7_verify_mirror", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


VM = load_module()

# Token-SHAPED but entirely synthetic. Generated for this test file; it is not
# and never was a credential on any server.
FAKE_TOKEN = "glpat-w97FakeNotARealToken00"  # gitleaks:allow - fake test fixture


# ---------------------------------------------------------------------------
# shared fixture: one group project on one instance, stubbed API
# ---------------------------------------------------------------------------
class RefereeCase(TempTreeTestCase):
    """Drives main() end to end against a fixture tree and a stubbed API.

    The live clone trees are NEVER touched: every case is built in
    tempfile.mkdtemp() by TempTreeTestCase.
    """

    HOST = "git.example.net"
    URL = "http://git.example.net"

    def remote_projects(self):
        return [{"id": 1, "path_with_namespace": "grp/a",
                 "namespace": {"kind": "group", "full_path": "grp"},
                 "default_branch": "main", "empty_repo": False,
                 "archived": False}]

    def clean_repo(self, relpath="grp/a", origin=None, commit=True):
        p = self.make_repo(relpath, origin or f"{self.URL}/{relpath}.git",
                           commit=commit)
        # `git init` writes .git/config 0644. Left alone, the hygiene gate
        # fires in every case and hides whatever the case is testing.
        cfg = os.path.join(VM.git_common_dir(p), "config")
        os.chmod(cfg, 0o600)
        return p

    def write_cfg(self, url=None):
        p = os.path.join(self.tmp, "instances.json")
        with open(p, "w") as fh:
            json.dump({"instances": [{"name": "T", "url": url or self.URL,
                                      "master_dir": "MasterGroups",
                                      "token_env": "GITLAB_TOKEN_T"}],
                       "active": "T"}, fh)
        return p

    def write_env(self):
        p = os.path.join(self.tmp, "env")
        with open(p, "w") as fh:
            fh.write(f"export GITLAB_TOKEN_T={FAKE_TOKEN}\n")
        os.chmod(p, 0o600)
        return p

    def stub_api(self, projects=None, liveness=200, groups=None):
        """Return an api_request stub. groups=None -> no groups configured."""
        projects = self.remote_projects() if projects is None else projects
        groups = [] if groups is None else groups

        def fake(base, path, token, params=None):
            if path in ("/api/v4/metadata", "/api/v4/user"):
                if liveness != 200:
                    return liveness, {"_body": "unauthorized"}, {}
                return 200, {"version": "19.1.1", "username": "t",
                             "is_admin": True}, {}
            if path == "/api/v4/projects":
                return 200, list(projects), {}
            if path == "/api/v4/groups":
                return 200, list(groups), {}
            if path.startswith("/api/v4/groups/"):
                return 200, [], {}
            if path.startswith("/api/v4/projects/"):
                return 404, {"_body": "404"}, {}
            return 404, {"_body": "404"}, {}

        return fake

    def run_main(self, extra_args=(), api=None, cfg_url=None, dest=None):
        argv = ["--dest", dest or self.dest,
                "--config", self.write_cfg(cfg_url),
                "--env-file", self.write_env(),
                "--json", os.path.join(self.tmp, "out.json"),
                "--no-group-crosscheck", *extra_args]
        buf = io.StringIO()
        orig = VM.api_request
        if api is not None:
            VM.api_request = api
        try:
            with redirect_stdout(buf):
                code = VM.main(argv)
        finally:
            VM.api_request = orig
        return code, buf.getvalue()


# ---------------------------------------------------------------------------
# 1. the codes themselves
# ---------------------------------------------------------------------------
class ExitCodeVocabulary(unittest.TestCase):

    def test_five_distinct_codes_exist(self):
        codes = {
            "EXIT_OK": VM.EXIT_OK,
            "EXIT_INCOMPLETE": VM.EXIT_INCOMPLETE,
            "EXIT_ERROR": VM.EXIT_ERROR,
            "EXIT_INSECURE": VM.EXIT_INSECURE,
            "EXIT_HYGIENE": VM.EXIT_HYGIENE,
        }
        self.assertEqual(len(set(codes.values())), 5,
                         f"exit codes collide: {codes}")

    def test_zero_is_not_repurposed_and_one_and_two_keep_their_meaning(self):
        """Backward compatibility for the obvious consumer."""
        self.assertEqual(VM.EXIT_OK, 0)
        self.assertEqual(VM.EXIT_INCOMPLETE, 1)
        self.assertEqual(VM.EXIT_ERROR, 2)
        # the historical name for 1 must still resolve, to 1
        self.assertEqual(VM.EXIT_DIRTY, 1)

    def test_every_non_ok_code_is_non_zero(self):
        for c in (VM.EXIT_INCOMPLETE, VM.EXIT_ERROR, VM.EXIT_INSECURE,
                  VM.EXIT_HYGIENE):
            self.assertNotEqual(c, 0, "a finding must never exit 0")

    def test_categories_are_named_and_mapped_to_codes(self):
        """A number a user sees must be explainable without reading source."""
        for code in (VM.EXIT_OK, VM.EXIT_INCOMPLETE, VM.EXIT_ERROR,
                     VM.EXIT_INSECURE, VM.EXIT_HYGIENE):
            self.assertIn(code, VM.EXIT_CATEGORY)
            self.assertTrue(VM.EXIT_CATEGORY[code].strip())

    def test_severity_order_is_not_numeric_order(self):
        """OPERATIONAL(2) outranks CORRECTNESS(1) which outranks SECURITY(3).

        The codes are a vocabulary, not a scale. Pinning this stops a later
        refactor from 'tidying' the precedence into `max(codes)`.
        """
        order = VM.SEVERITY_ORDER
        self.assertLess(order.index("unknown"), order.index("incomplete"))
        self.assertLess(order.index("incomplete"), order.index("insecure"))
        self.assertLess(order.index("insecure"), order.index("hygiene"))
        self.assertLess(order.index("hygiene"), order.index("clean"))


# ---------------------------------------------------------------------------
# 2. correctness vs hygiene — the whole point
# ---------------------------------------------------------------------------
class CorrectnessFailuresExitOne(RefereeCase):

    def test_missing_project_exits_1(self):
        """CONSTRUCTED: the server has grp/a, the fixture tree has nothing."""
        code, out = self.run_main(api=self.stub_api())
        self.assertEqual(code, VM.EXIT_INCOMPLETE, out)
        self.assertIn("CORRECTNESS", out)
        self.assertIn("missing 1", out)

    def test_broken_repository_exits_1(self):
        """A directory that is not a usable git repo holds no mirrored data."""
        self.make_broken_clone("grp/a")
        code, out = self.run_main(api=self.stub_api())
        self.assertEqual(code, VM.EXIT_INCOMPLETE, out)
        self.assertIn("CORRECTNESS", out)

    def test_shallow_clone_exits_1(self):
        """History the server has is not on disk: a completeness failure."""
        p = self.clean_repo("grp/a")
        open(os.path.join(VM.git_common_dir(p), "shallow"), "w").close()
        code, out = self.run_main(api=self.stub_api())
        self.assertEqual(code, VM.EXIT_INCOMPLETE, out)
        self.assertIn("CORRECTNESS", out)

    def test_complete_and_tidy_still_exits_0(self):
        self.clean_repo("grp/a")
        code, out = self.run_main(api=self.stub_api())
        self.assertEqual(code, VM.EXIT_OK, out)
        self.assertIn(VM.VERDICT_CLEAN, out)


class HygieneFindingsExitFour(RefereeCase):

    def test_config_mode_not_0600_exits_4_not_1(self):
        """THE regression: config-mode flags used to exit 1 forever."""
        p = self.clean_repo("grp/a")
        os.chmod(os.path.join(VM.git_common_dir(p), "config"), 0o644)
        code, out = self.run_main(api=self.stub_api())
        self.assertEqual(code, VM.EXIT_HYGIENE,
                         f"mode-only finding must not look like a "
                         f"correctness failure\n{out}")
        self.assertIn("HYGIENE", out)
        # The per-category breakdown legitimately prints "CORRECTNESS ... none";
        # it is the VERDICT line that must not claim a correctness failure.
        verdict = [ln for ln in out.splitlines() if ln.startswith("VERDICT:")][0]
        self.assertNotIn("CORRECTNESS", verdict)
        self.assertIn("CORRECTNESS (exit 1) : none", out)

    def test_duplicate_clone_exits_4(self):
        """The content IS on disk — twice. Wasteful, not incorrect."""
        self.clean_repo("grp/a")
        self.clean_repo("copy/a", origin=f"{self.URL}/grp/a.git")
        code, out = self.run_main(api=self.stub_api())
        self.assertEqual(code, VM.EXIT_HYGIENE, out)
        self.assertIn("HYGIENE", out)

    def test_orphan_deleted_upstream_exits_4(self):
        """Deleted on the server: the mirror is complete, the copy is a
        leftover. Retention is a decision, not a completeness failure."""
        self.clean_repo("grp/a")
        self.clean_repo("grp/gone", origin=f"{self.URL}/grp/gone.git")
        code, out = self.run_main(api=self.stub_api())
        self.assertEqual(code, VM.EXIT_HYGIENE, out)

    def test_hygiene_and_missing_together_report_correctness(self):
        """Severity precedence: the worse category owns the exit code."""
        p = self.clean_repo("copy/a", origin=f"{self.URL}/grp/other.git")
        os.chmod(os.path.join(VM.git_common_dir(p), "config"), 0o644)
        code, out = self.run_main(api=self.stub_api())
        self.assertEqual(code, VM.EXIT_INCOMPLETE, out)
        self.assertIn("CORRECTNESS", out)


class CredentialExposureExitsThree(RefereeCase):

    def test_credential_in_config_exits_3_even_when_mirror_is_complete(self):
        """A secret on disk is not a tidiness issue and not a missing project.

        CONSTRUCTED: the origin URL carries synthetic userinfo.
        """
        self.clean_repo("grp/a",
                        origin=f"http://oauth2:{FAKE_TOKEN}@{self.HOST}/grp/a.git")
        code, out = self.run_main(api=self.stub_api())
        self.assertEqual(code, VM.EXIT_INSECURE, out)
        self.assertIn("SECURITY", out)
        self.assertNotIn(FAKE_TOKEN, out, "the referee printed the token")

    def test_credential_outranks_hygiene(self):
        p = self.clean_repo(
            "grp/a", origin=f"http://oauth2:{FAKE_TOKEN}@{self.HOST}/grp/a.git")
        os.chmod(os.path.join(VM.git_common_dir(p), "config"), 0o644)
        code, out = self.run_main(api=self.stub_api())
        self.assertEqual(code, VM.EXIT_INSECURE, out)

    def test_missing_outranks_credential(self):
        self.clean_repo(
            "grp/other",
            origin=f"http://oauth2:{FAKE_TOKEN}@{self.HOST}/grp/other.git")
        code, out = self.run_main(api=self.stub_api())
        self.assertEqual(code, VM.EXIT_INCOMPLETE, out)


# ---------------------------------------------------------------------------
# 3. operational failure is not a failed check
# ---------------------------------------------------------------------------
class OperationalFailuresExitTwo(RefereeCase):
    """A bad token, an unreachable server or a missing clone root must NEVER
    be reportable as a clean mirror. This is the exact failure this codebase
    hit repeatedly: a paginated fetch broke silently and returned an empty
    list, so a bad token yielded zero projects, no exception and no warning —
    indistinguishable from an instance that genuinely has no projects.
    """

    def test_bad_token_exits_2(self):
        """CONSTRUCTED: liveness answers 401 on both probes."""
        self.clean_repo("grp/a")
        code, out = self.run_main(api=self.stub_api(liveness=401))
        self.assertEqual(code, VM.EXIT_ERROR, out)
        self.assertIn("OPERATIONAL", out)
        self.assertNotIn(VM.VERDICT_CLEAN, out)

    def test_unreachable_server_exits_2(self):
        self.clean_repo("grp/a")

        def boom(*a, **k):
            raise VM.OperationalError("cannot reach http://git.example.net: refused")

        code, out = self.run_main(api=boom)
        self.assertEqual(code, VM.EXIT_ERROR, out)
        self.assertIn("OPERATIONAL", out)
        self.assertNotIn(VM.VERDICT_CLEAN, out)

    def test_missing_clone_root_exits_2(self):
        code, out = self.run_main(api=self.stub_api(),
                                  dest=os.path.join(self.tmp, "no-such-tree"))
        self.assertEqual(code, VM.EXIT_ERROR, out)
        self.assertIn("OPERATIONAL", out)
        self.assertNotIn(VM.VERDICT_CLEAN, out)

    def test_absent_token_variable_exits_2(self):
        self.clean_repo("grp/a")
        argv = ["--dest", self.dest, "--config", self.write_cfg(),
                "--env-file", os.path.join(self.tmp, "no-env-file"),
                "--json", os.path.join(self.tmp, "o.json"),
                "--no-group-crosscheck"]
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = VM.main(argv)
        self.assertEqual(code, VM.EXIT_ERROR, buf.getvalue())
        self.assertIn("OPERATIONAL", buf.getvalue())

    def test_zero_projects_with_crosscheck_enabled_is_operational(self):
        """A silently-empty listing must not read as 'nothing to mirror'."""
        self.clean_repo("grp/a")
        argv = ["--dest", self.dest, "--config", self.write_cfg(),
                "--env-file", self.write_env(),
                "--json", os.path.join(self.tmp, "o.json")]
        orig = VM.api_request
        VM.api_request = self.stub_api(projects=[], groups=[])
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = VM.main(argv)
        finally:
            VM.api_request = orig
        out = buf.getvalue()
        self.assertEqual(code, VM.EXIT_ERROR, out)
        self.assertIn("OPERATIONAL", out)

    def test_allow_empty_instance_opts_out_of_the_zero_guard(self):
        argv = ["--dest", self.dest, "--config", self.write_cfg(),
                "--env-file", self.write_env(),
                "--json", os.path.join(self.tmp, "o.json"),
                "--allow-empty-instance"]
        orig = VM.api_request
        VM.api_request = self.stub_api(projects=[], groups=[])
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = VM.main(argv)
        finally:
            VM.api_request = orig
        self.assertEqual(code, VM.EXIT_OK, buf.getvalue())

    def test_group_crosscheck_disagreement_is_operational(self):
        """The cross-check was computed but never affected the verdict.

        A flat listing that loses a project the group listing still sees is
        precisely the silent-truncation class of bug.
        """
        self.clean_repo("grp/a")
        groups = [{"id": 35, "full_path": "grp"}]

        def fake(base, path, token, params=None):
            if path in ("/api/v4/metadata", "/api/v4/user"):
                return 200, {"version": "19.1.1"}, {}
            if path == "/api/v4/projects":
                return 200, self.remote_projects(), {}
            if path == "/api/v4/groups":
                return 200, groups, {}
            if path == "/api/v4/groups/35/projects":
                # the group knows about a project the flat listing omitted
                return 200, [{"path_with_namespace": "grp/a"},
                             {"path_with_namespace": "grp/only-via-group"}], {}
            return 404, {"_body": "404"}, {}

        argv = ["--dest", self.dest, "--config", self.write_cfg(),
                "--env-file", self.write_env(),
                "--json", os.path.join(self.tmp, "o.json")]
        orig = VM.api_request
        VM.api_request = fake
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = VM.main(argv)
        finally:
            VM.api_request = orig
        out = buf.getvalue()
        self.assertEqual(code, VM.EXIT_ERROR, out)
        self.assertIn("OPERATIONAL", out)

    def test_operational_outranks_every_check_category(self):
        """Even with a credential leak AND a missing project on disk, an
        instance we could not determine reports OPERATIONAL."""
        self.clean_repo(
            "grp/x", origin=f"http://oauth2:{FAKE_TOKEN}@{self.HOST}/grp/x.git")
        code, out = self.run_main(api=self.stub_api(liveness=401))
        self.assertEqual(code, VM.EXIT_ERROR, out)


# ---------------------------------------------------------------------------
# 4. the human-readable verdict
# ---------------------------------------------------------------------------
class VerdictLineIsLegible(RefereeCase):

    def test_verdict_line_states_the_code_and_the_category(self):
        p = self.clean_repo("grp/a")
        os.chmod(os.path.join(VM.git_common_dir(p), "config"), 0o644)
        code, out = self.run_main(api=self.stub_api())
        line = [ln for ln in out.splitlines() if ln.startswith("VERDICT:")]
        self.assertTrue(line, out)
        self.assertIn(f"exit {code}", line[0])
        self.assertIn("HYGIENE", line[0],
                      "the verdict line must say WHICH category fired")

    def test_legend_explains_every_code_without_reading_source(self):
        self.clean_repo("grp/a")
        _code, out = self.run_main(api=self.stub_api())
        legend = [ln for ln in out.splitlines() if "EXIT CODE" in ln]
        self.assertTrue(legend, out)
        for n in ("0", "1", "2", "3", "4"):
            self.assertIn(n, legend[0])

    def test_output_still_reports_the_counts_it_always_did(self):
        self.clean_repo("grp/a")
        _code, out = self.run_main(api=self.stub_api())
        for label in ("remote projects", "on disk", "MISSING",
                      "excluded (policy)", "orphans", "hygiene",
                      "integrity", "accounting"):
            self.assertIn(label, out, f"lost the {label!r} line")

    def test_json_artifact_carries_the_category_and_the_code(self):
        p = self.clean_repo("grp/a")
        os.chmod(os.path.join(VM.git_common_dir(p), "config"), 0o644)
        code, _out = self.run_main(api=self.stub_api())
        data = json.load(open(os.path.join(self.tmp, "out.json"),
                              encoding="utf-8"))
        self.assertEqual(data["exit_code"], code)
        self.assertEqual(data["exit_category"], VM.EXIT_CATEGORY[code])
        self.assertIn("severity", data["instances"][0])
        self.assertIn("findings_by_category", data["instances"][0])

    def test_existing_status_field_keeps_its_three_values(self):
        """`status` is a pre-existing contract other readers depend on.

        The new granularity lands in `severity` / `verdict_detail`; widening
        `status` in place would silently break every existing consumer.
        """
        p = self.clean_repo("grp/a")
        os.chmod(os.path.join(VM.git_common_dir(p), "config"), 0o644)
        self.run_main(api=self.stub_api())
        inst = json.load(open(os.path.join(self.tmp, "out.json"),
                              encoding="utf-8"))["instances"][0]
        self.assertIn(inst["status"],
                      (VM.VERDICT_CLEAN, VM.VERDICT_DIRTY, VM.VERDICT_UNKNOWN))
        self.assertEqual(inst["status"], VM.VERDICT_DIRTY)
        self.assertEqual(inst["verdict_detail"], VM.VERDICT_UNTIDY)
        self.assertEqual(inst["severity"], "hygiene")


class LegacyExitCodes(RefereeCase):
    """An old consumer that only understands 0/1/2 can ask for that."""

    def test_hygiene_collapses_to_1_under_legacy_flag(self):
        p = self.clean_repo("grp/a")
        os.chmod(os.path.join(VM.git_common_dir(p), "config"), 0o644)
        code, out = self.run_main(["--legacy-exit-codes"],
                                  api=self.stub_api())
        self.assertEqual(code, 1, out)
        self.assertIn("HYGIENE", out, "legacy mode still names the category")

    def test_insecure_collapses_to_1_under_legacy_flag(self):
        self.clean_repo("grp/a",
                        origin=f"http://oauth2:{FAKE_TOKEN}@{self.HOST}/grp/a.git")
        code, _out = self.run_main(["--legacy-exit-codes"], api=self.stub_api())
        self.assertEqual(code, 1)

    def test_legacy_flag_does_not_touch_0_1_or_2(self):
        self.clean_repo("grp/a")
        self.assertEqual(
            self.run_main(["--legacy-exit-codes"], api=self.stub_api())[0], 0)
        self.assertEqual(
            self.run_main(["--legacy-exit-codes"],
                          api=self.stub_api(liveness=401))[0], 2)


# ---------------------------------------------------------------------------
# 5. independence is not traded away for any of this
# ---------------------------------------------------------------------------
class IndependencePreserved(unittest.TestCase):

    def test_still_imports_nothing_from_the_cli(self):
        cli_modules = {
            "coverage", "paths", "instances", "api", "clone", "pull", "setup",
            "errors", "remotes", "log", "caps", "credentials", "members",
            "printer", "gitlab_cli", "config",
        }
        tree = ast.parse(open(SCRIPT, encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & cli_modules, set())

    def test_no_sys_path_manipulation_and_no_cli_state_read(self):
        src = open(SCRIPT, encoding="utf-8").read()
        self.assertNotRegex(src, r"sys\.path\.(insert|append)")
        for cached in ("resume.json", ".gitlab-cli/state", "errors.log",
                       "coverage.json"):
            self.assertNotIn(cached, src,
                             f"the referee reads the CLI's cached {cached}")

    def test_wrapper_documents_the_codes_it_forwards(self):
        text = open(WRAPPER, encoding="utf-8").read()
        for n in ("0", "1", "2", "3", "4"):
            self.assertIn(n, text)
        code = "\n".join(ln for ln in text.splitlines()
                         if ln.strip() and not ln.lstrip().startswith("#"))
        self.assertIn("python3", code)
        self.assertNotIn("venv/bin/python", code)
        # `exec` hands the interpreter's status straight to the caller: no
        # shell wrapper may swallow or rewrite it.
        self.assertIn("exec", code)

    def test_docstring_documents_the_full_code_table(self):
        doc = ast.get_docstring(ast.parse(open(SCRIPT, encoding="utf-8").read()))
        for token in ("0", "1", "2", "3", "4", "CORRECTNESS", "OPERATIONAL",
                      "SECURITY", "HYGIENE"):
            self.assertIn(token, doc, f"{token!r} undocumented in the header")


if __name__ == "__main__":
    unittest.main(verbosity=2)
