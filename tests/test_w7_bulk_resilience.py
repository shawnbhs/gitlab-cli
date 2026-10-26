#!/usr/bin/env python3
# tests/test_w7_bulk_resilience.py
#
# W7. Clone All / Sync All used to catch ONLY KeyboardInterrupt inside their
# per-group loop. An ApiError from ONE group (a 403 on a group the token
# cannot read, a 404 after a rename, a three-second VPN blink) therefore
# escaped the loop entirely, escaped interactive_mode(), and was handled by
# main()'s PROCESS-level fatal boundary — which ends the run. "Clone all 12
# groups" stopped at group 3 and the other 9 were never attempted.
#
# The repair must not create the opposite bug. A swallowed error is worse
# than an abort, because it is invisible. So every test below asserts the
# failure stays LOUD IN THREE WAYS AT ONCE:
#
#     printed  +  written to the error log  +  named in the final summary
#
# ...while the remaining groups still run, and while Ctrl-C still stops
# everything immediately.

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (HERE, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

# Keep the user's live ~/.gitlab-cli/errors.log out of this test run:
# errors.py binds ERROR_FILE at import time, and these tests assert that
# failures ARE logged, so without this they append to the real log.
import _error_log_guard  # noqa: E402,F401

import api                       # noqa: E402
import gitlab_cli                # noqa: E402


def _groups(*names):
    return [{"id": i, "full_path": n} for i, n in enumerate(names, 1)]


class _Bulk:
    """Run gitlab_cli._run_bulk capturing stdout and every log_error call."""

    def __init__(self, kind, groups, behaviour):
        self.kind = kind
        self.groups = groups
        self.behaviour = behaviour      # full_path -> None | exception
        self.visited = []
        self.logged = []

    def run(self):
        def run_one(g):
            self.visited.append(g["full_path"])
            outcome = self.behaviour.get(g["full_path"])
            if isinstance(outcome, BaseException):
                raise outcome

        def fake_log_error(context, message, resolved=False):
            self.logged.append({"context": context, "message": message})

        buf = io.StringIO()
        with mock.patch("errors.log_error", fake_log_error):
            with redirect_stdout(buf):
                result = gitlab_cli._run_bulk(self.kind, self.groups, run_one)
        self.out = buf.getvalue()
        self.done, self.failed, self.interrupted = result
        return self


class BulkLoopSurvivesOneGroup(unittest.TestCase):
    """The core claim: one group's ApiError does not end the run."""

    def test_api_error_on_one_group_does_not_stop_the_others(self):
        gs = _groups("a", "b", "c")
        b = _Bulk("Clone All", gs,
                  {"b": api.ApiError(403, "Forbidden",
                                     "http://git.example.net/api/v4/groups/b")}).run()

        # Every group was attempted, including the ones AFTER the failure.
        self.assertEqual(b.visited, ["a", "b", "c"])
        self.assertEqual(b.done, 2)
        self.assertEqual([f["group"] for f in b.failed], ["b"])
        self.assertFalse(b.interrupted)

    def test_the_failure_is_printed_logged_and_summarised(self):
        gs = _groups("a", "b", "c")
        b = _Bulk("Clone All", gs,
                  {"b": api.ApiError(403, "Forbidden",
                                     "http://git.example.net/api/v4/groups/b")}).run()

        # 1. printed, at the point of failure, naming the group
        self.assertIn("b", b.out)
        self.assertIn("FAILED", b.out)

        # 2. logged, exactly once, with the group in the context
        self.assertEqual(len(b.logged), 1)
        self.assertIn("b", b.logged[0]["context"])
        self.assertIn("Clone All", b.logged[0]["context"])
        self.assertTrue(b.failed[0]["logged"])

        # 3. named again in the closing summary, with its HTTP status
        tail = b.out[b.out.rindex("finished") if "finished" in b.out else 0:]
        self.assertIn("b", tail)
        self.assertIn("403", tail)

    def test_success_banner_is_withheld_when_anything_failed(self):
        gs = _groups("a", "b")
        b = _Bulk("Sync All", gs,
                  {"b": api.ApiError(500, "boom", "")}).run()
        # The word that used to appear unconditionally must not claim success.
        self.assertNotIn("\u2714 Sync All finished \u2014", b.out)
        self.assertIn("with failures", b.out)
        self.assertIn("1/2", b.out)

    def test_clean_run_still_reports_success(self):
        gs = _groups("a", "b", "c")
        b = _Bulk("Clone All", gs, {}).run()
        self.assertEqual(b.done, 3)
        self.assertEqual(b.failed, [])
        self.assertIn("\u2714", b.out)
        self.assertIn("3/3", b.out)
        self.assertEqual(b.logged, [])

    def test_every_group_failing_still_visits_every_group(self):
        gs = _groups("a", "b", "c")
        b = _Bulk("Clone All", gs,
                  {n: api.ApiError(404, "gone", "") for n in ("a", "b", "c")}).run()
        self.assertEqual(b.visited, ["a", "b", "c"])
        self.assertEqual(b.done, 0)
        self.assertEqual(len(b.failed), 3)
        self.assertEqual(len(b.logged), 3)

    def test_non_api_exception_is_handled_the_same_way(self):
        """A plain bug in one group must not be more fatal than an ApiError."""
        gs = _groups("a", "b", "c")
        b = _Bulk("Sync All", gs, {"b": RuntimeError("bad path")}).run()
        self.assertEqual(b.visited, ["a", "b", "c"])
        self.assertEqual(b.done, 2)
        self.assertEqual(b.failed[0]["exc"], "RuntimeError")
        self.assertEqual(len(b.logged), 1)


class InterruptStillStops(unittest.TestCase):
    """Ctrl-C means 'I want out', not 'skip this one'."""

    def test_keyboard_interrupt_breaks_the_loop(self):
        gs = _groups("a", "b", "c")
        b = _Bulk("Clone All", gs, {"b": KeyboardInterrupt()}).run()
        self.assertEqual(b.visited, ["a", "b"])     # "c" never started
        self.assertTrue(b.interrupted)
        self.assertEqual(b.failed, [])
        self.assertIn("interrupted", b.out)
        self.assertIn("not attempted", b.out)

    def test_interrupt_is_not_logged_as_a_group_failure(self):
        gs = _groups("a", "b")
        b = _Bulk("Sync All", gs, {"a": KeyboardInterrupt()}).run()
        self.assertEqual(b.logged, [])
        self.assertNotIn("\u2714 Sync All finished \u2014", b.out)


class FailureHandlerIsItselfSafe(unittest.TestCase):
    """A broken error log must not take down the remaining groups."""

    def test_logging_failure_is_reported_not_propagated(self):
        gs = _groups("a", "b", "c")

        def boom_log(*a, **k):
            raise OSError("read-only file system")

        visited = []

        def run_one(g):
            visited.append(g["full_path"])
            if g["full_path"] == "b":
                raise api.ApiError(401, "bad token", "")

        buf = io.StringIO()
        with mock.patch("errors.log_error", boom_log):
            with redirect_stdout(buf):
                done, failed, interrupted = gitlab_cli._run_bulk(
                    "Clone All", gs, run_one)
        out = buf.getvalue()

        self.assertEqual(visited, ["a", "b", "c"])   # loop survived
        self.assertEqual(done, 2)
        self.assertEqual(len(failed), 1)
        self.assertFalse(failed[0]["logged"])
        self.assertIn("could not write the error log", out)
        self.assertIn("b", out)


class TokenNeverLeaks(unittest.TestCase):
    """Bulk failure output goes through the same redaction as the boundary."""

    def test_token_in_the_url_is_redacted(self):
        secret = "glpat-" + "A" * 20
        gs = _groups("a")
        b = _Bulk("Clone All", gs,
                  {"a": api.ApiError(401, f"denied for {secret}",
                                     "http://git.example.net/api/v4/x")}).run()
        self.assertNotIn(secret, b.out)
        self.assertNotIn(secret, b.logged[0]["message"])


class WiringIsActuallyUsed(unittest.TestCase):
    """Guard against the helpers existing but the loops not calling them."""

    def test_bulk_branches_call_run_bulk(self):
        with io.open(os.path.join(ROOT, "gitlab_cli.py"),
                     encoding="utf-8") as fh:
            src = fh.read()
        body = src[src.index("def interactive_mode"):]
        self.assertIn('_run_bulk("Clone All"', body)
        self.assertIn('_run_bulk("Sync All"', body)
        # The old fragile pattern must be gone from both bulk branches.
        self.assertNotIn("\u26a0 Clone All interrupted.{RESET}\\n", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
