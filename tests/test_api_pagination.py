#!/usr/bin/env python3
# tests/test_api_pagination.py — W3 hardening.
#
# Pins the fix for the silent-truncation defect in api.get_paginated.
#
# The original code did `break` on any HTTP error and returned the pages it had
# collected so far as if that were the whole set. Measured consequence: a bad
# token produced "0 projects", no exception, no warning — indistinguishable
# from an empty group. get_subgroups() was additionally quiet=True, so a failed
# subgroup listing read as "this group has no subgroups" and dropped an entire
# subtree from a clone. That is the same failure class as the 51-project skip.
#
# No network: api.requests is replaced by a shim for the duration of each test,
# so the real get_paginated logic (paging, X-Next-Page, retries, strict) runs
# against scripted responses. api.time is replaced too, so the retry backoff
# does not actually sleep.

import unittest

from _bootstrap import TempTreeTestCase  # noqa: F401  (path bootstrap)

import api


class _Resp:
    def __init__(self, status=200, payload=None, next_page="", bad_json=False):
        self.status_code = status
        self._payload = [] if payload is None else payload
        self._bad = bad_json
        self.headers = {"X-Next-Page": next_page}

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._payload


class _FakeRequests:
    """Stands in for the `requests` module inside api.py."""

    Timeout = api.requests.Timeout
    ConnectionError = api.requests.ConnectionError
    utils = api.requests.utils

    def __init__(self, script):
        # script: list of _Resp, or an exception instance to raise
        self.script = list(script)
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        item = self.script.pop(0) if self.script else _Resp(200, [])
        if isinstance(item, BaseException):
            raise item
        return item


class _NoSleep:
    @staticmethod
    def sleep(_):
        return None


class ApiTestCase(unittest.TestCase):
    def install(self, script):
        self.fake = _FakeRequests(script)
        api.requests = self.fake
        return self.fake

    def setUp(self):
        self._real_requests = api.requests
        self._real_time = api.time
        api.time = _NoSleep()

    def tearDown(self):
        api.requests = self._real_requests
        api.time = self._real_time


def _page(n, count=100):
    return [{"id": i, "path_with_namespace": f"g/p{i}"} for i in range(n, n + count)]


class PaginationCompletenessTests(ApiTestCase):
    """A partial fetch may never be presented as a complete one."""

    def test_multiple_pages_are_all_collected_and_marked_complete(self):
        self.install([_Resp(200, _page(0), next_page="2"),
                      _Resp(200, _page(100), next_page="3"),
                      _Resp(200, _page(200, 5), next_page="")])
        out = api.get_paginated("http://h/api/v4/projects", {}, quiet=True)
        self.assertEqual(len(out), 205)
        self.assertTrue(out.complete)
        self.assertIsNone(out.error)

    def test_an_empty_result_is_complete_not_failed(self):
        """0 items with complete=True is how a caller tells 'nothing here'
        apart from 'the request failed' — the distinction the old code denied."""
        self.install([_Resp(200, [], next_page="")])
        out = api.get_paginated("http://h/api/v4/projects", {}, quiet=True)
        self.assertEqual(len(out), 0)
        self.assertTrue(out.complete)

    def test_http_error_raises_instead_of_returning_an_empty_list(self):
        self.install([_Resp(401, [])])
        with self.assertRaises(api.ApiError) as ctx:
            api.get_paginated("http://h/api/v4/projects", {}, quiet=True)
        self.assertEqual(ctx.exception.status, 401)

    def test_failure_on_a_later_page_raises_rather_than_truncating(self):
        """The dangerous shape: page 1 succeeded, so the old code returned 100
        projects and every caller treated them as the whole group.

        500 is a retryable status, so the failure is scripted for every
        attempt — otherwise the retry would fall through to the default empty
        page and the fetch would end cleanly."""
        self.install([_Resp(200, _page(0), next_page="2")]
                     + [_Resp(500, [])] * 3)
        with self.assertRaises(api.ApiError):
            api.get_paginated("http://h/api/v4/projects", {}, quiet=True,
                              max_pages=200)

    def test_non_strict_returns_a_flagged_partial_result(self):
        # 403 is not retryable, so one scripted failure is enough.
        self.install([_Resp(200, _page(0), next_page="2"), _Resp(403, [])])
        out = api.get_paginated("http://h/api/v4/projects", {}, quiet=True,
                                strict=False)
        self.assertEqual(len(out), 100)
        self.assertFalse(out.complete)
        self.assertTrue(out.partial)
        self.assertIsNotNone(out.error)
        self.assertEqual(out.error.status, 403)

    def test_partial_result_is_still_a_real_list(self):
        """Every caller does len()/sorted()/iteration on this value."""
        self.install([_Resp(200, _page(0, 3), next_page="2"), _Resp(500, [])])
        out = api.get_paginated("http://h/x", {}, quiet=True, strict=False)
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["id"], 0)
        self.assertEqual(len(sorted(out, key=lambda p: p["id"])), 3)

    def test_transport_failure_raises(self):
        self.install([api.requests.ConnectionError("down"),
                      api.requests.ConnectionError("down"),
                      api.requests.ConnectionError("down")])
        with self.assertRaises(api.ApiError) as ctx:
            api.get_paginated("http://h/x", {}, quiet=True)
        self.assertEqual(ctx.exception.status, 0)

    def test_unparseable_body_raises(self):
        self.install([_Resp(200, bad_json=True)])
        with self.assertRaises(api.ApiError):
            api.get_paginated("http://h/x", {}, quiet=True)

    def test_a_json_object_where_a_list_was_expected_raises(self):
        """GitLab returns {"message": ...} on some error paths; iterating that
        silently yields key strings instead of projects."""
        self.install([_Resp(200, {"message": "403 Forbidden"})])
        with self.assertRaises(api.ApiError):
            api.get_paginated("http://h/x", {}, quiet=True)

    def test_unfollowable_cursor_raises_rather_than_claiming_completeness(self):
        self.install([_Resp(200, _page(0), next_page="eyJpZCI6MX0=")])
        with self.assertRaises(api.ApiError) as ctx:
            api.get_paginated("http://h/x", {}, quiet=True)
        self.assertIn("X-Next-Page", str(ctx.exception))

    def test_max_pages_ceiling_is_reported_not_silently_truncated(self):
        script = [_Resp(200, _page(i * 100), next_page=str(i + 2))
                  for i in range(5)]
        self.install(script)
        with self.assertRaises(api.ApiError) as ctx:
            api.get_paginated("http://h/x", {}, quiet=True, max_pages=2)
        self.assertIn("truncated", str(ctx.exception))
        self.install([_Resp(200, _page(0), next_page="2"),
                      _Resp(200, _page(100), next_page="3")])
        out = api.get_paginated("http://h/x", {}, quiet=True, max_pages=2,
                                strict=False)
        self.assertEqual(len(out), 200)
        self.assertFalse(out.complete)

    def test_a_transient_status_is_retried_and_then_succeeds(self):
        self.install([_Resp(503, []), _Resp(200, _page(0, 4), next_page="")])
        out = api.get_paginated("http://h/x", {}, quiet=True)
        self.assertEqual(len(out), 4)
        self.assertTrue(out.complete)

    def test_a_short_page_ends_the_walk_without_an_error(self):
        self.install([_Resp(200, _page(0, 7), next_page="")])
        out = api.get_paginated("http://h/x", {}, quiet=True,
                                params={"per_page": 50})
        self.assertEqual(len(out), 7)
        self.assertTrue(out.complete)
        self.assertEqual(self.fake.calls[0]["params"]["per_page"], 50)


class QuietSubtreeLossTests(ApiTestCase):
    """A failed listing must never read as 'there is nothing here'."""

    def test_get_subgroups_raises_instead_of_reporting_no_subgroups(self):
        self.install([_Resp(401, [])])
        with self.assertRaises(api.ApiError):
            api.get_subgroups("http://h", {}, 35)

    def test_get_projects_raises_instead_of_reporting_an_empty_group(self):
        self.install([_Resp(500, [])] * 3)
        with self.assertRaises(api.ApiError):
            api.get_projects("http://h", {}, 35)

    def test_recursive_project_fetch_raises(self):
        self.install([_Resp(404, [])])
        with self.assertRaises(api.ApiError):
            api.get_group_projects_recursive("http://h", {}, 35)

    def test_a_genuinely_empty_group_still_returns_an_empty_list(self):
        self.install([_Resp(200, [], next_page="")])
        self.assertEqual(list(api.get_projects("http://h", {}, 35)), [])


class QueryParameterEncodingTests(ApiTestCase):
    """Booleans must reach GitLab as the documented lowercase strings.

    requests encodes a Python bool as "True"/"False". This GitLab accepts that
    — server leniency, not a contract — and `include_subgroups=False` really
    does return 0 of 22 projects, so getting this wrong is a silent
    under-count, not an error.
    """

    def _params_of(self, fn, *args):
        self.install([_Resp(200, [], next_page="")])
        fn(*args)
        return self.fake.calls[0]["params"]

    def test_get_projects_sends_lowercase_strings(self):
        """Default listing omits `archived` so archived projects stay visible.

        The param used to be hardcoded to "false", which hid archived
        projects from both halves of the coverage fraction. It is now
        tri-state and only sent when the caller asks for one side of it.
        """
        p = self._params_of(api.get_projects, "http://h", {}, 35)
        self.assertNotIn("archived", p)
        self.assertEqual(p["with_shared"], "false")
        self.assertIsInstance(p["with_shared"], str)
        self.assertNotIsInstance(p["with_shared"], bool)

    def test_get_projects_sends_archived_as_a_lowercase_string_when_asked(self):
        p = self._params_of(api.get_projects, "http://h", {}, 35, None, False)
        self.assertEqual(p["archived"], "false")
        self.assertIsInstance(p["archived"], str)
        self.assertNotIsInstance(p["archived"], bool)

    def test_recursive_fetch_asks_for_subgroups_as_a_lowercase_string(self):
        p = self._params_of(api.get_group_projects_recursive, "http://h", {}, 35)
        self.assertEqual(p["include_subgroups"], "true")
        self.assertIsInstance(p["include_subgroups"], str)

    def test_top_level_group_listing_uses_string_flags(self):
        p = self._params_of(api.get_top_level_groups, "http://h", {})
        for key, value in p.items():
            if key in ("page", "per_page", "min_access_level"):
                continue
            self.assertNotIsInstance(value, bool,
                                     f"{key} is a Python bool on the wire")


if __name__ == "__main__":
    unittest.main(verbosity=2)
