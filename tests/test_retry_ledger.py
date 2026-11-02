#!/usr/bin/env python3
# tests/test_retry_ledger.py — a retry that recovers is not a failure.
#
# Seen live: Clone All on roboshan printed
#   "✖  Host name did not resolve (git.internal) ... (groups/112/subgroups)"
# and then "✔ Clone All finished — 1/1 groups" with every project accounted
# for. Both were true: the first attempt hit a DNS blip, the in-loop retry in
# get_paginated() succeeded. But request() had already printed the error and
# appended it to the failure ledger, so the user saw a red ✖ for a fetch that
# was complete, and every ledger-driven count ("N API failures", the menu's
# failure diff) over-reported by one per recovered blip.
#
# Contract pinned here: only the FINAL attempt of a retry loop may print or
# record. A recovered fetch leaves the ledger untouched and prints nothing; an
# exhausted one records exactly once (plus the "Incomplete fetch" summary).

import contextlib
import io
import unittest

import _bootstrap  # noqa: F401  (path bootstrap + error-log guard)

import api


class _Ok:
    status_code = 200
    headers = {"X-Total": "1"}

    def json(self):
        return [{"id": 1}]


class RetryLedgerTests(unittest.TestCase):

    def setUp(self):
        self._get = api.requests.get
        self._backoff = api._RETRY_BACKOFF
        self.addCleanup(setattr, api.requests, "get", self._get)
        self.addCleanup(setattr, api, "_RETRY_BACKOFF", self._backoff)
        api._RETRY_BACKOFF = 0
        api.reset_failures()
        self.addCleanup(api.reset_failures)

    def _serve(self, fail_first: int):
        calls = []

        def fake(url, **_kw):
            calls.append(url)
            if len(calls) <= fail_first:
                raise api.requests.ConnectionError(
                    "Failed to resolve 'git.internal'")
            return _Ok()

        api.requests.get = fake
        return calls

    def _run(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            res = api.get_paginated("http://git.internal/api/v4/groups/112/"
                                    "subgroups", {}, strict=False)
        return res, out.getvalue()

    def test_recovered_blip_is_not_recorded(self):
        calls = self._serve(fail_first=1)
        res, _ = self._run()
        self.assertEqual(len(calls), 2)
        self.assertTrue(res.complete)
        self.assertEqual(len(res), 1)
        self.assertEqual(api.failure_count(), 0)

    def test_recovered_blip_prints_nothing(self):
        self._serve(fail_first=api._RETRIES)   # recovers on the last attempt
        _, printed = self._run()
        self.assertNotIn("did not resolve", printed)

    def test_exhausted_retries_still_record_and_print(self):
        self._serve(fail_first=api._RETRIES + 1)
        res, printed = self._run()
        self.assertFalse(res.complete)
        self.assertIn("did not resolve", printed)
        msgs = [f.message for f in api.failures()]
        # one transport failure (final attempt) + one "Incomplete fetch"
        self.assertEqual(len(msgs), 2, msgs)
        self.assertTrue(any("Incomplete fetch" in m for m in msgs))

    def test_legacy_stub_without_record_kwarg_still_works(self):
        seen = []

        def stub(url, headers, params=None, quiet=False):
            seen.append(quiet)
            return None if len(seen) == 1 else _Ok()

        orig = api.request
        self.addCleanup(setattr, api, "request", orig)
        api.request = stub
        res, _ = self._run()
        self.assertTrue(res.complete)
        self.assertEqual(seen[0], True)   # non-final attempt is quiet


if __name__ == "__main__":
    unittest.main()
