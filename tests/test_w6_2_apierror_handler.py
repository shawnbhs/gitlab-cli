#!/usr/bin/env python3
# tests/test_w6_2_apierror_handler.py
#
# W6-2. An earlier wave made api.get_paginated RAISE ApiError instead of
# silently returning a short list. That was the right fix for the root cause
# (a bad token reading as "0 projects"), but nothing anywhere caught the
# exception: main() caught only KeyboardInterrupt, so a 500, a dropped VPN or
# an expired token printed a raw Python traceback.
#
# These tests pin the boundary added to gitlab_cli.main(). The central claim
# under test is NOT "the traceback is gone" -- it is that the failure stays
# LOUD in all three ways at once:
#
#     printed  +  logged  +  non-zero exit
#
# A handler that satisfies only the first is a new silent-failure path, which
# is the exact bug class this project exists to remove. Every assertion below
# checks all three together, on purpose.

import io
import os
import signal
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (HERE, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

# Keep the user's live ~/.gitlab-cli/errors.log out of this test run.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _error_log_guard  # noqa: E402,F401

import api                       # noqa: E402
import gitlab_cli                # noqa: E402


def _raise(exc):
    def _fn(args):
        raise exc
    return _fn


class _Boundary:
    """Run gitlab_cli.main() with _run replaced, capturing everything."""

    def __init__(self, exc, argv=("clone", "g"), env=None):
        self.exc = exc
        self.argv = list(argv)
        self.env = env or {}

    def run(self):
        out, err = io.StringIO(), io.StringIO()
        logged = []

        def fake_log_error(context, message, resolved=False):
            logged.append({"context": context, "message": message})

        env = {k: v for k, v in os.environ.items()
               if k != "GITLAB_CLI_DEBUG"}
        env.update(self.env)

        with mock.patch.object(gitlab_cli, "_run", _raise(self.exc)), \
                mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, "argv", ["gitlab-cli"] + self.argv), \
                mock.patch("errors.log_error", fake_log_error), \
                redirect_stdout(out), redirect_stderr(err):
            code = gitlab_cli.main()

        return code, out.getvalue(), err.getvalue(), logged


# ─── The three guarantees, together ───────────────────────────────────────────

class TestFailureStaysLoud(unittest.TestCase):

    def _assert_loud(self, exc, expect_code, argv=("clone", "g")):
        code, out, err, logged = _Boundary(exc, argv).run()
        self.assertEqual(code, expect_code,
                         f"{type(exc).__name__} must exit {expect_code}, "
                         f"not {code} -- a zero exit is indistinguishable "
                         f"from success to any caller")
        self.assertTrue(err.strip(),
                        "the failure must be PRINTED; a handler that prints "
                        "nothing is a silent failure")
        self.assertEqual(len(logged), 1,
                         "the failure must be LOGGED; the terminal scrolls, "
                         "the log is what survives")
        return code, out, err, logged

    def test_api_error_is_printed_logged_and_exits_two(self):
        self._assert_loud(api.ApiError(500, "boom on page 1",
                                       "http://h/api/v4/groups/1/projects"), 2)

    def test_unexpected_exception_is_printed_logged_and_exits_three(self):
        self._assert_loud(RuntimeError("unexpected"), 3)

    def test_exit_codes_are_distinct(self):
        api_code, _, _, _ = _Boundary(api.ApiError(500, "x")).run()
        int_code, _, _, _ = _Boundary(RuntimeError("x")).run()
        self.assertNotEqual(api_code, int_code,
                            "an API failure and an internal defect must be "
                            "distinguishable by exit code alone")
        self.assertNotIn(0, (api_code, int_code))

    def test_a_logging_failure_does_not_silence_the_error(self):
        """If errors.log cannot be written, the failure must STILL surface."""
        err_buf = io.StringIO()
        with mock.patch.object(gitlab_cli, "_run",
                               _raise(api.ApiError(500, "boom"))), \
                mock.patch.object(sys, "argv", ["gitlab-cli", "clone", "g"]), \
                mock.patch("errors.log_error",
                           side_effect=OSError("read-only fs")), \
                redirect_stderr(err_buf), redirect_stdout(io.StringIO()):
            code = gitlab_cli.main()
        text = err_buf.getvalue()
        self.assertEqual(code, 2, "still non-zero when the log write fails")
        self.assertIn("could not write the error log", text,
                      "a failed log write must itself be reported, never "
                      "swallowed")
        self.assertIn("500", text)


# ─── 401 must not read like an unreachable host ───────────────────────────────

class TestMessagesAreDistinguishable(unittest.TestCase):

    def _stderr_for(self, status):
        _, _, err, _ = _Boundary(
            api.ApiError(status, f"msg {status}",
                         "http://git.example.net/api/v4/groups/1/projects")).run()
        return err

    def test_401_and_unreachable_are_different_messages(self):
        unreachable = self._stderr_for(0)
        unauthorized = self._stderr_for(401)
        self.assertNotEqual(unreachable, unauthorized)
        self.assertIn("VPN", unreachable)
        self.assertNotIn("VPN", unauthorized.split("What to do")[0])
        self.assertIn("401", unauthorized)
        self.assertIn("token", unauthorized.lower())

    def test_401_says_it_is_not_a_network_problem(self):
        self.assertIn("NOT a network problem", self._stderr_for(401))

    def test_403_is_not_401(self):
        self.assertNotEqual(self._stderr_for(403), self._stderr_for(401))
        self.assertIn("Forbidden", self._stderr_for(403))

    def test_500_blames_the_server_not_the_user(self):
        text = self._stderr_for(500)
        self.assertIn("server", text.lower())
        self.assertIn("not your token", text)

    def test_every_status_names_host_endpoint_and_an_action(self):
        for status in (0, 401, 403, 404, 429, 500, 502, 418):
            text = self._stderr_for(status)
            with self.subTest(status=status):
                self.assertIn("What to do", text)
                if status != 0 or True:
                    self.assertIn("git.example.net", text,
                                  "the message must name the host")
                    self.assertIn("/api/v4/groups/1/projects", text,
                                  "the message must name the endpoint")

    def test_advice_exists_for_an_unmapped_status(self):
        headline, todo = gitlab_cli._api_error_advice(api.ApiError(418, "tea"))
        self.assertTrue(headline)
        self.assertTrue(todo, "an unknown status still gets actionable advice")


# ─── No traceback by default; a traceback on demand ───────────────────────────

_TB = "Traceback (most recent call last)"


class TestTracebackPolicy(unittest.TestCase):

    def test_no_traceback_by_default(self):
        for exc in (api.ApiError(500, "boom"), RuntimeError("boom"),
                    ValueError("boom"), KeyError("boom")):
            _, out, err, _ = _Boundary(exc).run()
            with self.subTest(exc=type(exc).__name__):
                self.assertNotIn(_TB, err + out)

    def test_debug_flag_prints_the_traceback(self):
        _, _, err, _ = _Boundary(RuntimeError("boom"),
                                 argv=("clone", "g", "--debug")).run()
        self.assertIn(_TB, err)

    def test_debug_env_var_prints_the_traceback(self):
        _, _, err, _ = _Boundary(RuntimeError("boom"),
                                 env={"GITLAB_CLI_DEBUG": "1"}).run()
        self.assertIn(_TB, err)

    def test_debug_does_not_change_the_exit_code(self):
        plain, _, _, _ = _Boundary(api.ApiError(500, "x")).run()
        dbg, _, _, _ = _Boundary(api.ApiError(500, "x"),
                                 argv=("clone", "g", "--debug")).run()
        self.assertEqual(plain, dbg, "--debug is diagnostics, not policy")

    def test_debug_flag_is_stripped_before_argument_parsing(self):
        """--debug must never be mistaken for a group name or a directory."""
        seen = []
        with mock.patch.object(gitlab_cli, "_run", lambda a: seen.append(a)), \
                mock.patch.object(sys, "argv",
                                  ["gitlab-cli", "clone", "g", "--debug"]), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            gitlab_cli.main()
        self.assertEqual(seen, [["clone", "g"]])

    def test_debug_env_var_only_accepts_truthy_values(self):
        for val, want in (("1", True), ("true", True), ("YES", True),
                          ("on", True), ("0", False), ("", False),
                          ("no", False)):
            with mock.patch.dict(os.environ, {"GITLAB_CLI_DEBUG": val}):
                with self.subTest(val=val):
                    self.assertEqual(gitlab_cli._debug_enabled([]), want)


# ─── Preserved behaviour ──────────────────────────────────────────────────────

class TestPreservedContracts(unittest.TestCase):

    def test_keyboardinterrupt_still_exits_zero(self):
        """The pre-existing contract: Ctrl-C is a decision, not a failure."""
        code, out, err, logged = _Boundary(KeyboardInterrupt()).run()
        self.assertEqual(code, 0)
        self.assertIn("Goodbye", out)
        self.assertEqual(logged, [],
                         "Ctrl-C is not an error and must not pollute the log")

    def test_keyboardinterrupt_does_not_print_a_traceback(self):
        _, out, err, _ = _Boundary(KeyboardInterrupt()).run()
        self.assertNotIn(_TB, out + err)

    def test_sigint_handler_is_not_disturbed_by_the_boundary(self):
        before = signal.getsignal(signal.SIGINT)
        for exc in (KeyboardInterrupt(), api.ApiError(500, "x"),
                    RuntimeError("x")):
            _Boundary(exc).run()
        self.assertIs(signal.getsignal(signal.SIGINT), before,
                      "the boundary must not touch the SIGINT handler that "
                      "clone.py restores in its own try/finally")

    def test_success_exits_zero(self):
        with mock.patch.object(gitlab_cli, "_run", lambda a: None), \
                mock.patch.object(sys, "argv", ["gitlab-cli", "instances"]), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(gitlab_cli.main(), 0)

    def test_systemexit_is_not_converted_into_a_failure(self):
        """_run() calls sys.exit(0) on several normal paths -- it must pass."""
        with mock.patch.object(gitlab_cli, "_run", _raise(SystemExit(0))), \
                mock.patch.object(sys, "argv", ["gitlab-cli"]), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                gitlab_cli.main()
        self.assertEqual(ctx.exception.code, 0)

    def test_systemexit_nonzero_still_propagates(self):
        with mock.patch.object(gitlab_cli, "_run", _raise(SystemExit(1))), \
                mock.patch.object(sys, "argv", ["gitlab-cli"]), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                gitlab_cli.main()
        self.assertEqual(ctx.exception.code, 1)


# ─── Credentials never reach the terminal or the log ──────────────────────────

class TestNoCredentialLeak(unittest.TestCase):

    FAKE = "glpat-" + "FAKEFAKEFAKEFAKEFAKE"      # generated here, not real

    def test_token_in_the_url_is_redacted_everywhere(self):
        exc = api.ApiError(
            401, f"denied for private_token={self.FAKE}",
            f"https://oauth2:{self.FAKE}@git.example.net/api/v4/groups/1/projects")
        code, out, err, logged = _Boundary(exc).run()
        blob = out + err + repr(logged)
        self.assertNotIn(self.FAKE, blob,
                         "a token must never reach stderr or errors.log")
        self.assertEqual(code, 2)
        self.assertIn("git.example.net", err,
                      "redaction must not destroy the diagnostic host name")

    def test_endpoint_parsing_drops_userinfo(self):
        host, endpoint = gitlab_cli._endpoint_of(
            f"https://user:{self.FAKE}@git.example.net/api/v4/groups/1/projects")
        self.assertEqual(host, "git.example.net")
        self.assertEqual(endpoint, "/api/v4/groups/1/projects")
        self.assertNotIn(self.FAKE, host + endpoint)

    def test_endpoint_parsing_survives_junk(self):
        for url in ("", None, "not a url", "http://", "://x"):
            with self.subTest(url=url):
                host, endpoint = gitlab_cli._endpoint_of(url)
                self.assertIsInstance(host, str)
                self.assertIsInstance(endpoint, str)


# ─── The log entry is useful, not a shrug ─────────────────────────────────────

class TestLogEntryContent(unittest.TestCase):

    def test_log_entry_carries_context_host_and_endpoint(self):
        exc = api.ApiError(500, "HTTP 500 on page 1",
                           "http://git.example.net/api/v4/groups/7/projects")
        _, _, _, logged = _Boundary(exc, argv=("clone", "globex")).run()
        entry = logged[0]
        self.assertIn("clone", entry["context"])
        self.assertIn("globex", entry["context"])
        self.assertIn("git.example.net", entry["message"])
        self.assertIn("/api/v4/groups/7/projects", entry["message"])
        self.assertIn("ApiError", entry["message"])

    def test_interactive_run_is_identifiable_in_the_log(self):
        _, _, _, logged = _Boundary(api.ApiError(0, "down"), argv=()).run()
        self.assertIn("interactive", logged[0]["context"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
