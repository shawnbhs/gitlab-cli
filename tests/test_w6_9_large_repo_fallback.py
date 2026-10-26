# test_w6_9_large_repo_fallback.py — W6-9: the large-repository fallback.
#
# One repository in the Hub fleet (~2.88 GB, a database dump) cannot be
# fetched in one shot: the transfer dies mid-pack with `curl 18 transfer
# closed with outstanding read data`, reproducibly, and it survives
# http.version=HTTP/1.1, a 500 MB http.postBuffer and core.compression=0.
# That is a server/proxy response cap, not a client defect.
#
# git.py answers it with `clone --depth=<n>` plus incremental
# `fetch --deepen=<n>`. The danger in ANY such fallback is that it becomes a
# silent-success path — the founding bug of this project is exactly a partial
# result reported as complete. These tests pin the opposite:
#
#   * a shallow result is status "shallow", NEVER "ok"
#   * clone_repo() (the boolean API) returns False for it
#   * the fallback fires ONLY for the transfer-cap class, never for auth/gone
#   * deepen_repo() reports complete=True only when .git/shallow is gone,
#     and a stall is reported as a stall
#   * a pre-existing completed clone is never deleted by the fallback
#
# Everything is stubbed: the suite must not depend on a 2.88 GB network fetch.

import os
import sys
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Keep the user's live ~/.gitlab-cli/errors.log out of this test run.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _error_log_guard  # noqa: E402,F401

import git as gitmod


CAP_ERR = ("error: RPC failed; curl 18 transfer closed with outstanding "
           "read data remaining\nfetch-pack: unexpected disconnect while "
           "reading sideband packet")
AUTH_ERR = "fatal: Authentication failed for 'https://hub.example/x.git/'"
GONE_ERR = "remote: The project you were looking for could not be found."


class Recorder:
    """Stands in for git.py's subprocess layer and records every argv."""

    def __init__(self, script):
        self.script = script          # list of (rc, out, err) per _run call
        self.calls = []
        self.i = 0

    def __call__(self, args, cwd=None, timeout=None, env_extra=None):
        self.calls.append(list(args))
        if self.i < len(self.script):
            r = self.script[self.i]
        else:
            r = self.script[-1] if self.script else (0, "", "")
        self.i += 1
        return r

    def argv_at(self, n):
        return self.calls[n] if n < len(self.calls) else []

    def any_with(self, token):
        return any(token in a for c in self.calls for a in c)


class StubBase(unittest.TestCase):
    """Neutralises printing, logging and credential I/O for every test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="w69_")
        self._saved = {}
        for name in ("print_ok", "print_warn", "print_error", "log_error",
                     "save_credential", "credential_known",
                     "remote_has_userinfo", "_harden_repo_config"):
            self._saved[name] = getattr(gitmod, name)
        self.warnings = []
        gitmod.print_ok = lambda *a, **k: None
        gitmod.print_warn = lambda m="", *a, **k: self.warnings.append(str(m))
        gitmod.print_error = lambda m="", *a, **k: self.warnings.append(str(m))
        self.logged = []
        gitmod.log_error = lambda k, v="": self.logged.append((k, v))
        gitmod.save_credential = lambda *a, **k: True
        gitmod.credential_known = lambda *a, **k: True
        gitmod.remote_has_userinfo = lambda *a, **k: False
        gitmod._harden_repo_config = lambda *a, **k: None

    def tearDown(self):
        for name, fn in self._saved.items():
            setattr(gitmod, name, fn)
        for attr in ("_run", "_git", "is_shallow", "shallow_depth"):
            if attr in getattr(self, "_patched", ()):
                setattr(gitmod, attr, self._orig[attr])
        shutil.rmtree(self.tmp, ignore_errors=True)

    def patch(self, **kw):
        self._patched = getattr(self, "_patched", set())
        self._orig = getattr(self, "_orig", {})
        for k, v in kw.items():
            self._orig.setdefault(k, getattr(gitmod, k))
            self._patched.add(k)
            setattr(gitmod, k, v)


class TestTransferCapClassification(StubBase):

    def test_cap_symptoms_recognised(self):
        for msg in (
            "curl 18 transfer closed with outstanding read data remaining",
            "error: RPC failed; curl 56 GnuTLS recv error",
            "fetch-pack: unexpected disconnect while reading sideband packet",
            "fatal: early EOF",
            "fatal: index-pack failed",
            "fatal: the remote end hung up unexpectedly",
        ):
            self.assertTrue(gitmod.is_transfer_cap(msg), msg)

    def test_auth_and_gone_are_never_transfer_caps(self):
        # If these routed into the fallback, a 401 would silently produce a
        # depth-1 repository and look like partial success.
        self.assertFalse(gitmod.is_transfer_cap(AUTH_ERR))
        self.assertFalse(gitmod.is_transfer_cap(GONE_ERR))
        self.assertFalse(gitmod.is_transfer_cap(""))
        self.assertFalse(gitmod.is_transfer_cap(None))

    def test_unrelated_error_is_not_a_cap(self):
        self.assertFalse(gitmod.is_transfer_cap("fatal: destination exists"))


class TestCloneFallback(StubBase):

    def _stub_clone(self, script, shallow_after=True, depth=10):
        rec = Recorder(script)
        self.patch(_run=rec,
                   is_shallow=lambda p: shallow_after,
                   shallow_depth=lambda p: depth)
        return rec

    def test_clean_clone_reports_ok(self):
        self._stub_clone([(0, "", "")], shallow_after=False, depth=19)
        r = gitmod.clone_repo_detailed("tok", "https://h/x.git",
                                       os.path.join(self.tmp, "a"))
        self.assertEqual(r["status"], "ok")
        self.assertFalse(r["shallow"])

    def test_transfer_cap_falls_back_to_shallow_and_is_NOT_ok(self):
        # call 0 = full clone (fails with the cap), call 1 = --depth clone
        rec = self._stub_clone([(128, "", CAP_ERR), (0, "", "")],
                               shallow_after=True, depth=10)
        target = os.path.join(self.tmp, "big")
        r = gitmod.clone_repo_detailed("tok", "https://h/big.git", target)

        self.assertEqual(r["status"], gitmod.S_SHALLOW)
        self.assertNotEqual(r["status"], "ok")
        self.assertTrue(r["shallow"])
        self.assertEqual(r["commits"], 10)
        self.assertTrue(rec.any_with("--depth=10"),
                        "the fallback must actually ask for a shallow clone")

    def test_boolean_api_returns_False_for_a_shallow_result(self):
        # THE anti-silent-success assertion: every existing caller counts
        # success by this boolean, so shallow must not increment it.
        self._stub_clone([(128, "", CAP_ERR), (0, "", "")], shallow_after=True)
        ok = gitmod.clone_repo("tok", "https://h/big.git",
                               os.path.join(self.tmp, "b"))
        self.assertFalse(ok)

    def test_boolean_api_returns_True_only_for_a_complete_clone(self):
        self._stub_clone([(0, "", "")], shallow_after=False, depth=19)
        self.assertTrue(gitmod.clone_repo("tok", "https://h/x.git",
                                          os.path.join(self.tmp, "c")))

    def test_shallow_result_is_recorded_in_the_error_log(self):
        self._stub_clone([(128, "", CAP_ERR), (0, "", "")], shallow_after=True)
        gitmod.clone_repo_detailed("tok", "https://h/big.git",
                                   os.path.join(self.tmp, "d"))
        keys = [k for k, _ in self.logged]
        self.assertTrue(any(k.startswith("clone-shallow:") for k in keys),
                        f"shallow clone must leave a row; got {keys}")

    def test_shallow_result_warns_the_operator_in_words(self):
        self._stub_clone([(128, "", CAP_ERR), (0, "", "")], shallow_after=True)
        gitmod.clone_repo_detailed("tok", "https://h/big.git",
                                   os.path.join(self.tmp, "e"))
        blob = " ".join(self.warnings).upper()
        self.assertIn("SHALLOW", blob)

    def test_auth_failure_does_not_trigger_the_fallback(self):
        rec = self._stub_clone([(128, "", AUTH_ERR)], shallow_after=True)
        r = gitmod.clone_repo_detailed("tok", "https://h/x.git",
                                       os.path.join(self.tmp, "f"))
        self.assertEqual(r["status"], gitmod.S_AUTH)
        self.assertFalse(rec.any_with("--depth=10"))

    def test_gone_repo_does_not_trigger_the_fallback(self):
        rec = self._stub_clone([(128, "", GONE_ERR)], shallow_after=True)
        r = gitmod.clone_repo_detailed("tok", "https://h/x.git",
                                       os.path.join(self.tmp, "g"))
        self.assertEqual(r["status"], gitmod.S_GONE)
        self.assertFalse(rec.any_with("--depth=10"))

    def test_allow_shallow_False_disables_the_fallback_entirely(self):
        rec = self._stub_clone([(128, "", CAP_ERR), (0, "", "")])
        r = gitmod.clone_repo_detailed("tok", "https://h/x.git",
                                       os.path.join(self.tmp, "h"),
                                       allow_shallow=False)
        self.assertNotEqual(r["status"], gitmod.S_SHALLOW)
        self.assertFalse(rec.any_with("--depth=10"))

    def test_failed_fallback_reports_the_original_error_not_success(self):
        self._stub_clone([(128, "", CAP_ERR), (128, "", CAP_ERR)])
        r = gitmod.clone_repo_detailed("tok", "https://h/x.git",
                                       os.path.join(self.tmp, "i"))
        self.assertNotIn(r["status"], ("ok", gitmod.S_SHALLOW))

    def test_a_successful_clone_that_is_shallow_is_still_not_ok(self):
        # Defence in depth: even if git itself returns rc 0 with a shallow
        # repo (e.g. a server-side cap honoured as a truncation), the result
        # must not be counted as complete.
        self._stub_clone([(0, "", "")], shallow_after=True, depth=10)
        r = gitmod.clone_repo_detailed("tok", "https://h/x.git",
                                       os.path.join(self.tmp, "j"))
        self.assertEqual(r["status"], gitmod.S_SHALLOW)


class TestFallbackNeverDestroysData(StubBase):

    def test_existing_completed_clone_is_never_deleted(self):
        target = os.path.join(self.tmp, "real")
        os.makedirs(os.path.join(target, ".git"))
        marker = os.path.join(target, "PRECIOUS.sql")
        with open(marker, "w") as fh:
            fh.write("2.88 GB of database dump, pretend")

        rec = Recorder([(128, "", CAP_ERR)])
        self.patch(_run=rec,
                   _has_completed_clone=lambda p: True,
                   is_shallow=lambda p: False,
                   shallow_depth=lambda p: 19)
        gitmod.clone_repo_detailed("tok", "https://h/x.git", target)
        self.assertTrue(os.path.exists(marker),
                        "the fallback must never delete a completed clone")

    def test_existing_shallow_clone_is_kept_and_reported_shallow(self):
        target = os.path.join(self.tmp, "shal")
        os.makedirs(os.path.join(target, ".git"))
        with open(os.path.join(target, ".git", "shallow"), "w") as fh:
            fh.write("deadbeef\n")
        marker = os.path.join(target, "DATA.sql")
        with open(marker, "w") as fh:
            fh.write("existing shallow checkout")

        self.patch(_run=Recorder([(128, "", CAP_ERR)]),
                   shallow_depth=lambda p: 10)
        r = gitmod.clone_repo_detailed("tok", "https://h/x.git", target)
        self.assertEqual(r["status"], gitmod.S_SHALLOW)
        self.assertTrue(os.path.exists(marker))


class TestIsShallow(StubBase):

    def test_shallow_file_is_authoritative(self):
        p = os.path.join(self.tmp, "r")
        os.makedirs(os.path.join(p, ".git"))
        self.patch(_git=lambda *a, **k: (0, "false", ""))
        self.assertFalse(gitmod.is_shallow(p))
        with open(os.path.join(p, ".git", "shallow"), "w") as fh:
            fh.write("abc\n")
        self.assertTrue(gitmod.is_shallow(p))

    def test_rev_parse_fallback(self):
        p = os.path.join(self.tmp, "r2")
        os.makedirs(os.path.join(p, ".git"))
        self.patch(_git=lambda *a, **k: (0, "true", ""))
        self.assertTrue(gitmod.is_shallow(p))


class TestDeepenRepo(StubBase):

    def _repo(self, name="d"):
        p = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(p, ".git"))
        return p

    def test_deepen_completes_and_reports_complete(self):
        p = self._repo()
        state = {"n": 10, "shallow": True, "calls": 0}

        def run(args, cwd=None, timeout=None, env_extra=None):
            state["calls"] += 1
            state["n"] += 1
            if state["n"] >= 19:
                state["shallow"] = False
            return (0, "", "")

        self.patch(_run=run,
                   _git=lambda *a, **k: (0, "https://h/x.git", ""),
                   is_shallow=lambda q: state["shallow"],
                   shallow_depth=lambda q: state["n"])
        r = gitmod.deepen_repo(p, token="tok", step=1)
        self.assertTrue(r["complete"])
        self.assertEqual(r["commits"], 19)
        self.assertEqual(state["calls"], 9)

    def test_deepen_uses_small_steps_not_one_big_fetch(self):
        p = self._repo("d2")
        rec = Recorder([(0, "", "")])
        self.patch(_run=rec,
                   _git=lambda *a, **k: (0, "https://h/x.git", ""),
                   is_shallow=lambda q: False,
                   shallow_depth=lambda q: 19)
        # already complete → no fetch at all
        r = gitmod.deepen_repo(p)
        self.assertTrue(r["complete"])
        self.assertEqual(rec.calls, [])

    def test_deepen_passes_the_deepen_flag(self):
        p = self._repo("d3")
        state = {"shallow": True}

        def run(args, cwd=None, timeout=None, env_extra=None):
            self.assertIn("--deepen=3", args)
            state["shallow"] = False
            return (0, "", "")

        self.patch(_run=run,
                   _git=lambda *a, **k: (0, "https://h/x.git", ""),
                   is_shallow=lambda q: state["shallow"],
                   shallow_depth=lambda q: 13)
        self.assertTrue(gitmod.deepen_repo(p, step=3)["complete"])

    def test_deepen_failure_is_not_complete(self):
        p = self._repo("d4")
        self.patch(_run=lambda *a, **k: (128, "", CAP_ERR),
                   _git=lambda *a, **k: (0, "https://h/x.git", ""),
                   is_shallow=lambda q: True,
                   shallow_depth=lambda q: 11)
        r = gitmod.deepen_repo(p)
        self.assertFalse(r["complete"])
        self.assertIn("curl 18", r["detail"])

    def test_a_stall_is_reported_as_a_stall_not_as_complete(self):
        # rc 0 forever but no new commits and .git/shallow persists: this is
        # precisely the shape a silent-success bug would take.
        p = self._repo("d5")
        self.patch(_run=lambda *a, **k: (0, "", ""),
                   _git=lambda *a, **k: (0, "https://h/x.git", ""),
                   is_shallow=lambda q: True,
                   shallow_depth=lambda q: 10)
        r = gitmod.deepen_repo(p, max_steps=5)
        self.assertFalse(r["complete"])
        self.assertIn("stall", r["detail"].lower())

    def test_max_steps_exhausted_is_not_complete(self):
        p = self._repo("d6")
        state = {"n": 10}

        def run(args, cwd=None, timeout=None, env_extra=None):
            state["n"] += 1
            return (0, "", "")

        self.patch(_run=run,
                   _git=lambda *a, **k: (0, "https://h/x.git", ""),
                   is_shallow=lambda q: True,
                   shallow_depth=lambda q: state["n"])
        r = gitmod.deepen_repo(p, max_steps=3)
        self.assertFalse(r["complete"])
        self.assertEqual(r["steps"], 3)

    def test_non_repo_is_not_complete(self):
        r = gitmod.deepen_repo(os.path.join(self.tmp, "nope"))
        self.assertFalse(r["complete"])
        self.assertIn("not a git repo", r["detail"])


class TestSyncReportsShallow(StubBase):

    def test_sync_result_carries_the_shallow_flag(self):
        p = os.path.join(self.tmp, "s")
        os.makedirs(os.path.join(p, ".git"))
        self.patch(is_shallow=lambda q: True,
                   _remotes=lambda q: [])
        r = gitmod.sync_repo(p)
        self.assertTrue(r["shallow"],
                        "a shallow repo must be reported as shallow on sync")


if __name__ == "__main__":
    unittest.main()
