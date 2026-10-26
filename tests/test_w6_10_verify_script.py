#!/usr/bin/env python3
"""
tests/test_w6_10_verify_script.py — offline regression tests for the
independent referee at scripts/verify_mirror.py.

These tests pin the NON-NEGOTIABLE design rules, each of which encodes a real
failure on this project:

  * the referee imports nothing from the CLI (rule 1)
  * identity is the normalised (host, path_with_namespace), never a basename
    (rule 2) — 30 basenames repeat across namespaces as different projects
  * group listings pass include_subgroups=true (rule 3)
  * a failed/partial fetch is UNKNOWN, never zero and never success (rule 4)
  * exit codes 0 / 1 / 2 mean complete-clean / missing-or-dirty / error (r5)
  * no token ever appears in output or in the JSON artifact (rule 6)
  * liveness uses /api/v4/metadata or /api/v4/user, never /api/v4/version (r7)
  * the filesystem walk does NOT prune at the first .git (rule 8)

No network, no token, no GitLab server is touched. The only thing written is
the per-test tempfile.mkdtemp() tree, and that is the only thing removed.
"""

import ast
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(PROJECT_ROOT, "scripts", "verify_mirror.py")

sys.path.insert(0, HERE)
from _bootstrap import TempTreeTestCase, git  # noqa: E402


def string_literals(path):
    """Every string constant in the module EXCEPT docstrings.

    Assertions about what the script CALLS must not be satisfied — or broken —
    by prose in a docstring: the docstring legitimately names /api/v4/version
    in order to explain why it is never used.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            d = ast.get_docstring(node, clean=False)
            if d is not None:
                docs.add(d)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value not in docs]


def load_module():
    """Import the referee by path, without putting the CLI root on sys.path."""
    spec = importlib.util.spec_from_file_location("w6_10_verify_mirror", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


VM = load_module()

# Token-SHAPED but entirely synthetic. Generated for this test file; it is not
# and never was a credential on any server.
FAKE_TOKEN = "glpat-w610TESTonlyNOTREAL0000"  # gitleaks:allow - fake test fixture


class ScriptIsIndependent(unittest.TestCase):
    """Rule 1: do not verify the tool with the tool."""

    def test_script_exists_and_is_executable(self):
        self.assertTrue(os.path.isfile(SCRIPT), SCRIPT)
        self.assertTrue(os.access(SCRIPT, os.X_OK), "verify_mirror.py not +x")

    def test_imports_nothing_from_the_cli(self):
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
        self.assertEqual(
            imported & cli_modules, set(),
            "the referee imports CLI modules; a checker that reuses the "
            "subject's modules cannot detect the subject's bugs")

    def test_never_appends_cli_root_to_sys_path(self):
        src = open(SCRIPT, encoding="utf-8").read()
        self.assertNotRegex(src, r"sys\.path\.(insert|append)")

    def test_third_party_imports_absent(self):
        """Must run under the SYSTEM python3, which has no requests/dotenv."""
        src = open(SCRIPT, encoding="utf-8").read()
        for pkg in ("requests", "dotenv", "yaml", "rich", "click"):
            self.assertNotRegex(src, rf"^\s*(import|from)\s+{pkg}\b",
                                msg=f"non-stdlib import: {pkg}")

    def test_runs_under_system_python3(self):
        for py in ("/usr/bin/python3", "/usr/local/bin/python3"):
            if os.path.exists(py):
                r = subprocess.run([py, SCRIPT, "--help"],
                                   capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("verify_mirror.py", r.stdout)
                return
        self.skipTest("no system python3 found")


class Normalisation(unittest.TestCase):
    """Rule 2: identity is the full normalised origin, never a basename."""

    def test_strips_git_suffix_slash_and_case(self):
        for url in ("http://git.example.net/Group/Sub/Proj.git",
                    "http://git.example.net/group/sub/proj/",
                    "HTTP://GIT.EXAMPLE.NET/group/sub/proj"):
            self.assertEqual(VM.normalize(url),
                             ("git.example.net", "group/sub/proj"), url)

    def test_strips_embedded_credentials(self):
        n = VM.normalize(f"https://oauth2:{FAKE_TOKEN}@hub.example.com/a/b.git")
        self.assertEqual(n, ("hub.example.com", "a/b"))
        self.assertNotIn(FAKE_TOKEN, str(n))

    def test_scp_style_and_ssh_url(self):
        self.assertEqual(VM.normalize("git@git.example.net:group/proj.git"),
                         ("git.example.net", "group/proj"))
        self.assertEqual(VM.normalize("ssh://git@git.example.net:22/group/proj.git"),
                         ("git.example.net", "group/proj"))

    def test_same_basename_in_two_namespaces_is_two_identities(self):
        a = VM.normalize("http://git.example.net/teamA/feed-ms.git")
        b = VM.normalize("http://git.example.net/teamB/feed-ms.git")
        self.assertNotEqual(a, b,
                            "basename collapse — 30 basenames repeat here")

    def test_same_path_on_two_hosts_is_two_identities(self):
        self.assertNotEqual(VM.normalize("http://git.example.net/g/p"),
                            VM.normalize("https://hub.example.com/g/p"))

    def test_garbage_returns_none(self):
        for bad in ("", None, "not a url", "/plain/path"):
            self.assertIsNone(VM.normalize(bad), repr(bad))


class TreeScan(TempTreeTestCase):
    """Rule 8: the walk must NOT prune at the first .git."""

    def test_finds_nested_checkouts(self):
        outer = self.make_repo("ns/outer", "http://git.example.net/ns/outer.git")
        inner = os.path.join(outer, "vendor", "inner")
        os.makedirs(inner, exist_ok=True)
        git("init", "-q", inner)
        git("remote", "add", "origin",
            "http://git.example.net/other/inner.git", cwd=inner)

        repos, _ = VM.scan_tree(self.dest)
        paths = {r["path"] for r in repos}
        self.assertIn(outer, paths)
        self.assertIn(inner, paths,
                      "nested checkout missed — a sweep that misses them is "
                      "worse than none")
        ids = {r["path_with_namespace"] for r in repos}
        self.assertEqual(ids, {"ns/outer", "other/inner"})

    def test_missing_destination_is_operational_error_not_empty(self):
        with self.assertRaises(VM.OperationalError):
            VM.scan_tree(os.path.join(self.tmp, "does-not-exist"))

    def test_duplicate_identity_at_two_paths_is_detected(self):
        self.make_repo("a/dup", "http://git.example.net/ns/dup.git")
        self.make_repo("b/dup", "http://git.example.net/ns/dup.git")
        repos, _ = VM.scan_tree(self.dest)
        keys = [(r["host"], r["path_with_namespace"]) for r in repos]
        self.assertEqual(keys.count(("git.example.net", "ns/dup")), 2)

    def test_shallow_clone_flagged(self):
        p = self.make_repo("ns/shallow", "http://git.example.net/ns/shallow.git")
        gd = VM.git_common_dir(p)
        open(os.path.join(gd, "shallow"), "w").close()
        repos, _ = VM.scan_tree(self.dest)
        rec = [r for r in repos if r["path"] == p][0]
        self.assertTrue(rec["health"]["shallow"])

    def test_broken_clone_is_invalid(self):
        p = self.make_broken_clone("ns/broken")
        repos, _ = VM.scan_tree(self.dest)
        rec = [r for r in repos if r["path"] == p][0]
        self.assertFalse(rec["health"]["valid"])

    def test_unborn_head_is_not_broken(self):
        """A clone of a server-side EMPTY project is not debris."""
        p = self.make_repo("ns/empty", "http://git.example.net/ns/empty.git",
                           commit=False)
        repos, _ = VM.scan_tree(self.dest)
        rec = [r for r in repos if r["path"] == p][0]
        self.assertTrue(rec["health"]["valid"])
        self.assertTrue(rec["health"]["unborn"])


class CredentialHygiene(TempTreeTestCase):

    def test_embedded_credential_in_config_is_found_without_printing_it(self):
        p = self.make_repo(
            "ns/leak", f"https://oauth2:{FAKE_TOKEN}@hub.example.com/ns/leak.git")
        info = VM.inspect_config(VM.git_common_dir(p))
        kinds = {f["kind"] for f in info["credential_findings"]}
        self.assertTrue(kinds & {"userinfo_in_url", "gitlab_pat", "oauth2_literal"})
        self.assertNotIn(FAKE_TOKEN, json.dumps(info),
                         "the finding leaked the credential value")

    def test_clean_config_has_no_findings(self):
        p = self.make_repo("ns/clean", "http://git.example.net/ns/clean.git")
        info = VM.inspect_config(VM.git_common_dir(p))
        self.assertEqual(info["credential_findings"], [])

    def test_mode_check_is_exactly_0600(self):
        p = self.make_repo("ns/mode", "http://git.example.net/ns/mode.git")
        gd = VM.git_common_dir(p)
        cfg = os.path.join(gd, "config")
        os.chmod(cfg, 0o644)
        self.assertFalse(VM.inspect_config(gd)["mode_ok"])
        os.chmod(cfg, 0o600)
        self.assertTrue(VM.inspect_config(gd)["mode_ok"])

    def test_git_common_dir_used_not_absolute_git_dir(self):
        """A linked worktree's gitdir holds no config; --git-common-dir does."""
        lits = string_literals(SCRIPT)
        self.assertIn("--git-common-dir", lits)
        self.assertNotIn("--absolute-git-dir", lits)


class Redaction(unittest.TestCase):
    """Rule 6: no token, ever, in output or in the JSON artifact."""

    def test_redactor_scrubs_registered_secret_everywhere(self):
        r = VM.Redactor()
        r.add(FAKE_TOKEN)
        self.assertNotIn(FAKE_TOKEN, r(f"url = https://x:{FAKE_TOKEN}@h/a.git"))
        nested = {"a": [{"b": f"tok {FAKE_TOKEN}"}], FAKE_TOKEN: "k"}
        self.assertNotIn(FAKE_TOKEN, json.dumps(r(nested)))

    def test_token_shaped_literal_scrubbed_even_if_unregistered(self):
        r = VM.Redactor()
        self.assertNotIn(FAKE_TOKEN, r(f"stray {FAKE_TOKEN} in a message"))

    def test_env_parser_reads_value_but_registers_it_for_redaction(self):
        d = tempfile.mkdtemp(prefix="w610_env_")
        path = os.path.join(d, "env")
        with open(path, "w") as fh:
            fh.write("# comment\n")
            fh.write(f"export GITLAB_TOKEN_X={FAKE_TOKEN}\n")
            fh.write('export GITLAB_TOKEN_Y="quoted-value-1234567"\n')
        toks = VM.read_env_tokens(path)
        self.assertEqual(toks["GITLAB_TOKEN_X"], FAKE_TOKEN)
        self.assertEqual(toks["GITLAB_TOKEN_Y"], "quoted-value-1234567")
        self.assertNotIn(FAKE_TOKEN, VM.REDACT(f"leaked {FAKE_TOKEN}"))


class ApiContract(unittest.TestCase):

    def test_liveness_never_uses_api_v4_version(self):
        """/api/v4/version 401s on a valid admin PAT on the Hub instance."""
        lits = string_literals(SCRIPT)
        self.assertNotIn("/api/v4/version", lits,
                         "a /version-based check declares a working "
                         "instance dead")
        self.assertIn("/api/v4/metadata", lits)
        self.assertIn("/api/v4/user", lits)

    def test_group_listing_passes_include_subgroups(self):
        """group 35 returns 0 projects without the flag and 22 with it."""
        lits = string_literals(SCRIPT)
        self.assertIn("include_subgroups", lits)
        src = open(SCRIPT, encoding="utf-8").read()
        self.assertIn('"include_subgroups": "true"', src)

    def test_pagination_follows_x_next_page_header(self):
        src = open(SCRIPT, encoding="utf-8").read()
        self.assertIn("x-next-page", src.lower())

    def test_pagination_failure_raises_unknown_not_empty_list(self):
        """Rule 4: a partial or failed fetch is UNKNOWN, never zero."""
        calls = {"n": 0}

        def fake(base, path, token, params=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return 200, [{"id": 1}], {"X-Next-Page": "2"}
            return 502, {"_body": "bad gateway"}, {}

        orig = VM.api_request
        VM.api_request = fake
        try:
            with self.assertRaises(VM.OperationalError) as ctx:
                VM.paginate("http://h", "/api/v4/projects", "t", {}, "probe")
            self.assertIn("UNKNOWN", str(ctx.exception))
        finally:
            VM.api_request = orig

    def test_rename_probe_url_encodes_the_full_path(self):
        seen = {}

        def fake(base, path, token, params=None):
            seen["path"] = path
            return 200, {"id": 7, "path_with_namespace": "ns/new-name"}, {}

        orig = VM.api_request
        VM.api_request = fake
        try:
            out = VM.probe_rename("http://h", "t", "ns/old-name")
        finally:
            VM.api_request = orig
        self.assertEqual(seen["path"], "/api/v4/projects/ns%2Fold-name")
        self.assertEqual(out["verdict"], "renamed")
        self.assertEqual(out["current_path"], "ns/new-name")

    def test_rename_probe_404_is_deleted_and_non_404_error_is_unknown(self):
        orig = VM.api_request
        try:
            VM.api_request = lambda *a, **k: (404, {"_body": "404"}, {})
            self.assertEqual(VM.probe_rename("http://h", "t", "a/b")["verdict"],
                             "deleted")
            VM.api_request = lambda *a, **k: (500, {"_body": "boom"}, {})
            self.assertEqual(VM.probe_rename("http://h", "t", "a/b")["verdict"],
                             "unknown")
        finally:
            VM.api_request = orig


class RenameAccounting(TempTreeTestCase):
    """A renamed project reads as missing AND orphan simultaneously.

    A prior audit reported 212 missing when 16 of them were renames already
    on disk. verify_instance must net them out of the missing list.
    """

    def _run(self, remote_projects, rename_map):
        self.make_repo("ns/old-name", "http://git.example.net/ns/old-name.git")
        local, _ = VM.scan_tree(self.dest)

        def fake(base, path, token, params=None):
            if path in ("/api/v4/metadata", "/api/v4/user"):
                return 200, {"version": "19.1.1", "username": "t",
                             "is_admin": True}, {}
            if path == "/api/v4/projects":
                return 200, remote_projects, {}
            if path.startswith("/api/v4/projects/"):
                enc = path.rsplit("/", 1)[-1]
                old = enc.replace("%2F", "/")
                if old in rename_map:
                    return 200, {"id": 9,
                                 "path_with_namespace": rename_map[old]}, {}
                return 404, {"_body": "404"}, {}
            return 200, [], {}

        args = VM.build_parser().parse_args(["--no-group-crosscheck"])
        orig = VM.api_request
        VM.api_request = fake
        try:
            return VM.verify_instance(
                {"name": "T", "url": "http://git.example.net",
                 "token_env": "GITLAB_TOKEN_T"},
                {"GITLAB_TOKEN_T": FAKE_TOKEN}, local, args)
        finally:
            VM.api_request = orig

    def test_renamed_project_is_not_counted_missing(self):
        remote = [{"id": 9, "path_with_namespace": "ns/new-name",
                   "namespace": {"kind": "group", "full_path": "ns"},
                   "default_branch": "main", "empty_repo": False,
                   "archived": False}]
        res = self._run(remote, {"ns/old-name": "ns/new-name"})
        self.assertEqual(res["counts"]["missing"], 0,
                         "rename inflated the missing count")
        self.assertEqual(res["counts"]["orphans_renamed"], 1)
        self.assertEqual(res["orphans"][0]["classification"], "renamed_upstream")
        self.assertEqual(res["orphans"][0]["current_path_with_namespace"],
                         "ns/new-name")

    def test_rename_does_not_double_count_present(self):
        """present + missing + excluded must equal the remote total.

        The first live run inflated `present` by exactly the number of renamed
        orphans, because the new path was appended even when it was already
        matched on disk.
        """
        remote = [{"id": 9, "path_with_namespace": "ns/new-name",
                   "namespace": {"kind": "group", "full_path": "ns"},
                   "default_branch": "main", "empty_repo": False,
                   "archived": False}]
        # both the stale-origin clone AND a clone at the new path exist
        self.make_repo("ns/new-name", "http://git.example.net/ns/new-name.git")
        res = self._run(remote, {"ns/old-name": "ns/new-name"})
        self.assertTrue(res["accounting"]["balances"],
                        f"accounting: {res['accounting']}")
        self.assertEqual(res["counts"]["present"], 1)

    def test_genuinely_deleted_orphan_is_classified_deleted_and_is_dirty(self):
        res = self._run([], {})
        self.assertEqual(res["counts"]["orphans_deleted"], 1)
        self.assertEqual(res["status"], VM.VERDICT_DIRTY)

    def test_unreachable_instance_is_unknown_not_zero(self):
        self.make_repo("ns/x", "http://git.example.net/ns/x.git")
        local, _ = VM.scan_tree(self.dest)
        args = VM.build_parser().parse_args(["--no-group-crosscheck"])
        orig = VM.api_request

        def boom(*a, **k):
            raise VM.OperationalError("cannot reach http://git.example.net: refused")

        VM.api_request = boom
        try:
            res = VM.verify_instance(
                {"name": "T", "url": "http://git.example.net",
                 "token_env": "GITLAB_TOKEN_T"},
                {"GITLAB_TOKEN_T": FAKE_TOKEN}, local, args)
        finally:
            VM.api_request = orig
        self.assertEqual(res["status"], VM.VERDICT_UNKNOWN)
        self.assertEqual(res["counts"], {},
                         "an unreachable instance must not report 0 missing")
        self.assertTrue(res["unknown_reasons"])

    def test_missing_token_is_unknown(self):
        local, _ = VM.scan_tree(self.dest)
        args = VM.build_parser().parse_args([])
        res = VM.verify_instance(
            {"name": "T", "url": "http://git.example.net",
             "token_env": "GITLAB_TOKEN_ABSENT"}, {}, local, args)
        self.assertEqual(res["status"], VM.VERDICT_UNKNOWN)
        self.assertEqual(res["counts"], {})


class PolicyExclusion(TempTreeTestCase):

    def _remote(self):
        return [
            {"id": 1, "path_with_namespace": "grp/a",
             "namespace": {"kind": "group", "full_path": "grp"},
             "default_branch": "main", "empty_repo": False, "archived": False},
            {"id": 2, "path_with_namespace": "someuser/personal",
             "namespace": {"kind": "user", "full_path": "someuser"},
             "default_branch": "main", "empty_repo": False, "archived": False},
        ]

    def _run(self, extra_args):
        p = self.make_repo("grp/a", "http://git.example.net/grp/a.git")
        # git init writes .git/config 0644; the hygiene gate would then make
        # every case DIRTY and hide what this class is actually testing.
        os.chmod(os.path.join(VM.git_common_dir(p), "config"), 0o600)
        local, _ = VM.scan_tree(self.dest)
        remote = self._remote()

        def fake(base, path, token, params=None):
            if path in ("/api/v4/metadata", "/api/v4/user"):
                return 200, {"version": "17.5.5"}, {}
            if path == "/api/v4/projects":
                return 200, remote, {}
            return 404, {"_body": "404"}, {}

        args = VM.build_parser().parse_args(["--no-group-crosscheck"] + extra_args)
        orig = VM.api_request
        VM.api_request = fake
        try:
            return VM.verify_instance(
                {"name": "T", "url": "http://git.example.net",
                 "token_env": "GITLAB_TOKEN_T"},
                {"GITLAB_TOKEN_T": FAKE_TOKEN}, local, args)
        finally:
            VM.api_request = orig

    def test_user_namespace_excluded_by_default_and_separated_from_missing(self):
        res = self._run([])
        self.assertEqual(res["counts"]["missing"], 0)
        self.assertEqual(res["counts"]["excluded_by_policy"], 1)
        self.assertEqual(res["excluded_by_policy"][0]["policy"],
                         "user_namespace_not_mirrored")
        self.assertEqual(res["status"], VM.VERDICT_CLEAN)

    def test_include_user_namespace_moves_it_to_missing(self):
        res = self._run(["--include-user-namespace"])
        self.assertEqual(res["counts"]["missing"], 1)
        self.assertEqual(res["counts"]["excluded_by_policy"], 0)
        self.assertEqual(res["status"], VM.VERDICT_DIRTY)


class ExitCodes(TempTreeTestCase):
    """Rule 5: 0 complete+clean, 1 missing/dirty, 2 operational error."""

    def test_constants(self):
        self.assertEqual((VM.EXIT_OK, VM.EXIT_DIRTY, VM.EXIT_ERROR), (0, 1, 2))

    def _main(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = VM.main(argv)
        return code, buf.getvalue()

    def test_nonexistent_destination_exits_2_with_unknown(self):
        code, out = self._main([
            "--dest", os.path.join(self.tmp, "no-such-tree"),
            "--config", self._write_cfg(), "--env-file", self._write_env(),
            "--json", os.path.join(self.tmp, "o.json")])
        self.assertEqual(code, 2)
        self.assertIn("UNKNOWN", out)
        self.assertNotIn(VM.VERDICT_CLEAN, out)

    def test_missing_config_exits_2(self):
        code, out = self._main([
            "--dest", self.dest,
            "--config", os.path.join(self.tmp, "nope.json"),
            "--env-file", self._write_env()])
        self.assertEqual(code, 2)
        self.assertIn("UNKNOWN", out)

    def test_unreachable_host_exits_2_and_prints_no_clean_verdict(self):
        cfg = self._write_cfg(url="http://127.0.0.1:9/")
        code, out = self._main([
            "--dest", self.dest, "--config", cfg,
            "--env-file", self._write_env(),
            "--json", os.path.join(self.tmp, "o.json")])
        self.assertEqual(code, 2)
        self.assertIn("UNKNOWN", out)
        self.assertNotIn("COMPLETE_AND_CLEAN", out)
        self.assertNotIn(FAKE_TOKEN, out)

    def test_json_artifact_contains_no_token(self):
        out_json = os.path.join(self.tmp, "o.json")
        self._main(["--dest", self.dest,
                    "--config", self._write_cfg(url="http://127.0.0.1:9/"),
                    "--env-file", self._write_env(), "--json", out_json])
        if os.path.exists(out_json):
            text = open(out_json, encoding="utf-8").read()
            self.assertNotIn(FAKE_TOKEN, text)
            self.assertFalse(re.search(r"glpat-[A-Za-z0-9_\-]{16,}", text))

    # helpers -------------------------------------------------------------
    def _write_cfg(self, url="http://git.example.net/"):
        p = os.path.join(self.tmp, "instances.json")
        with open(p, "w") as fh:
            json.dump({"instances": [{"name": "T", "url": url,
                                      "master_dir": "MasterGroups",
                                      "token_env": "GITLAB_TOKEN_T"}],
                       "active": "T"}, fh)
        return p

    def _write_env(self):
        p = os.path.join(self.tmp, "env")
        with open(p, "w") as fh:
            fh.write(f"export GITLAB_TOKEN_T={FAKE_TOKEN}\n")
        os.chmod(p, 0o600)
        return p


class WrapperScript(unittest.TestCase):

    def test_wrapper_exists_and_uses_system_python(self):
        w = os.path.join(PROJECT_ROOT, "scripts", "verify-mirror")
        self.assertTrue(os.path.isfile(w), w)
        self.assertTrue(os.access(w, os.X_OK))
        # Judge the EXECUTED lines, not the comments: the header legitimately
        # names ./venv/bin/python in order to say it must not be used.
        code = "\n".join(ln for ln in open(w, encoding="utf-8").read().splitlines()
                         if ln.strip() and not ln.lstrip().startswith("#"))
        self.assertIn("python3", code)
        self.assertNotIn("venv/bin/python", code,
                         "the referee must not run in the CLI's venv")
        self.assertIn("LC_ALL=C", code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
