#!/usr/bin/env python3
# tests/test_w7_live_error_log_isolation.py — W7.
#
# The bug this pins: the regression suite wrote to the DEVELOPER'S OWN error
# log. `errors.py` binds its log path at import time
#
#     ERROR_DIR  = os.path.expanduser("~/.gitlab-cli")
#     ERROR_FILE = os.path.join(ERROR_DIR, "errors.log")
#
# and `log_error()` appends to it unconditionally. Any test that drove a
# production path which logs — `git.clone_repo()` rejecting an unsafe URL was
# the confirmed one — appended a junk record to ~/.gitlab-cli/errors.log on
# every single run. The user's real operational log accumulated entries like
# `clone:/tmp/gitlabcli_test_.../MasterGroups/x` that never came from a real
# clone, which is both noise and a (small) information leak of the test tree.
#
# Two modules solved it locally by rebinding ERROR_DIR/ERROR_FILE in setUp.
# That protects only the module that remembers. tests/_error_log_guard.py makes
# it a process-wide invariant; this file proves the invariant actually holds.
#
# Offline. Touches nothing outside a temp dir. Never writes the real log.

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _error_log_guard as guard          # noqa: E402
from _bootstrap import TempTreeTestCase   # noqa: E402,F401  (import-order check)

import errors as errors_mod               # noqa: E402
import git as gitlib                      # noqa: E402


class GuardIsInstalledTests(unittest.TestCase):
    """The redirect must be in force merely because the harness was imported."""

    def test_errors_module_points_at_the_sandbox_not_the_home_directory(self):
        self.assertIsNotNone(guard.SANDBOX_FILE, "guard never installed")
        self.assertEqual(errors_mod.ERROR_FILE, guard.SANDBOX_FILE)
        self.assertEqual(errors_mod.ERROR_DIR, guard.SANDBOX_DIR)

    def test_the_sandbox_is_not_the_real_log(self):
        self.assertNotEqual(
            os.path.realpath(errors_mod.ERROR_FILE),
            os.path.realpath(guard.REAL_ERROR_FILE),
            "the suite is still bound to the user's live error log")

    def test_the_sandbox_lives_under_a_temp_directory(self):
        import tempfile
        self.assertTrue(
            os.path.realpath(guard.SANDBOX_DIR).startswith(
                os.path.realpath(tempfile.gettempdir())),
            f"sandbox escaped the temp dir: {guard.SANDBOX_DIR}")

    def test_installing_twice_is_a_no_op(self):
        before = (guard.SANDBOX_DIR, guard.SANDBOX_FILE)
        guard.install()
        self.assertEqual((guard.SANDBOX_DIR, guard.SANDBOX_FILE), before)


class LiveLogIsNeverWrittenTests(TempTreeTestCase):
    """Drive the exact production seam that was leaking, and prove the real
    log is byte-identical afterwards."""

    def setUp(self):
        super().setUp()
        self.before = guard.real_log_digest()

    def assertLiveLogUnchanged(self, what):
        self.assertEqual(
            guard.real_log_digest(), self.before,
            f"{what} modified the live log at {guard.REAL_ERROR_FILE}")

    def test_a_refused_clone_url_does_not_reach_the_live_log(self):
        # git.clone_repo -> check_url raises UnsafeURL -> log_error(...)
        ok = gitlib.clone_repo("glpat-" + "FAKEtokenFAKEtokenFK",
                               "ext::sh -c id",
                               os.path.join(self.dest, "x"))
        self.assertFalse(ok, "an unsafe URL was cloned")
        self.assertLiveLogUnchanged("clone_repo(unsafe url)")

    def test_a_direct_log_error_call_does_not_reach_the_live_log(self):
        errors_mod.log_error("w7-isolation-probe", "synthetic failure")
        self.assertLiveLogUnchanged("errors.log_error")

    def test_the_record_really_was_written_somewhere(self):
        """Isolation must not be achieved by silently dropping the write —
        that would hide a real logging regression behind a green suite."""
        errors_mod.log_error("w7-sink-probe", "synthetic failure")
        self.assertTrue(os.path.exists(errors_mod.ERROR_FILE),
                        "log_error wrote nothing at all")
        with open(errors_mod.ERROR_FILE, encoding="utf-8") as fh:
            self.assertIn("w7-sink-probe", fh.read(),
                          "log_error did not record the entry in the sandbox")
        self.assertLiveLogUnchanged("errors.log_error (sink probe)")

    def test_the_sandbox_log_keeps_owner_only_permissions(self):
        """The 0600 property of the real log must hold in the sandbox too, or
        the permission tests would be passing against a different regime."""
        errors_mod.log_error("w7-mode-probe", "synthetic failure")
        mode = os.stat(errors_mod.ERROR_FILE).st_mode & 0o777
        self.assertEqual(mode, 0o600, f"sandbox log left at {oct(mode)}")


class NonHarnessModulesAreCoveredTests(unittest.TestCase):
    """Modules that do not import _bootstrap must still import the guard, or
    running them standalone would write to the live log again."""

    HERE = os.path.dirname(os.path.abspath(__file__))

    def test_every_test_module_reaches_the_guard(self):
        offenders = []
        for name in sorted(os.listdir(self.HERE)):
            if not name.startswith("test_") or not name.endswith(".py"):
                continue
            with open(os.path.join(self.HERE, name), encoding="utf-8") as fh:
                src = fh.read()
            # _home_guard (gitlab_mcp tests) repoints errors.ERROR_FILE too,
            # along with every other ~-derived path constant.
            if ("_bootstrap" in src or "_error_log_guard" in src
                    or "_home_guard" in src):
                continue
            offenders.append(name)
        self.assertEqual(
            offenders, [],
            "these modules import neither _bootstrap nor _error_log_guard, so "
            "run standalone they would append to the user's live error log: "
            f"{offenders}")

    def test_the_suite_entry_point_installs_the_guard(self):
        with open(os.path.join(self.HERE, "run_tests.py"), encoding="utf-8") as fh:
            self.assertIn("_error_log_guard", fh.read(),
                          "run_tests.py does not install the error-log guard")


if __name__ == "__main__":
    unittest.main(verbosity=2)
