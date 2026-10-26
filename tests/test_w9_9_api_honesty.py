#!/usr/bin/env python3
# tests/test_w9_9_api_honesty.py — W9-9.
#
# The founding defect of this project lives in api.py: get_paginated() used to
# `break` out of its loop on a None response, a non-200 status or a JSON parse
# error and return whatever it had collected as if it were the whole set. A bad
# token therefore produced ZERO PROJECTS, no exception and no warning — and the
# callers passed quiet=True, so not even an error line printed. Most of the
# projects were skipped and the tool printed "Clone All: done".
#
# W3 introduced ApiError. These tests finish that work and pin it EVERYWHERE,
# as a property of every function in the module rather than of the one that was
# patched:
#
#   A. Every collection-returning function raises on failure and never hands
#      back a short list  (CollectionFunctionsNeverLieTests).
#   B. Completeness metadata SURVIVES the wrappers' sorted()/filter, because a
#      flag that sorted() throws away is silence with extra steps
#      (CompletenessSurvivesPostProcessingTests).
#   C. `quiet` suppresses the console line, NEVER the knowledge of the failure
#      (QuietCannotHideAFailureTests).
#   D. A complete-looking walk is cross-checked against the server's own
#      X-Total, so an invisible truncation is still caught
#      (XTotalCrossCheckTests).
#   E. The recursive-listing trap: include_subgroups=true can never be dropped
#      (RecursiveListingTrapTests). Measured live in W9: group 'globex'
#      returns 0 projects flat and 23 recursive; group 'vega' 1 vs 149.
#   F. The archived filter is explicit and DISCLOSED, never a silent default
#      (ArchivedIsExplicitAndDisclosedTests).
#   G. The singular getters distinguish "does not exist" (None) from "I could
#      not find out" (raise)  (SingularGettersTests, AccessLevelTests).
#   H. get_user_namespace_projects still refuses to guess an owner
#      (UserNamespaceContractTests).
#
# No network: api.requests is replaced by a shim, so the real logic runs
# against scripted responses. api.time is replaced too, so retries do not sleep.

import io
import unittest
from contextlib import redirect_stdout

from _bootstrap import TempTreeTestCase  # noqa: F401  (path bootstrap)

import api


URL = "http://h"
HEADERS = {"PRIVATE-TOKEN": "x"}


class _Resp:
    def __init__(self, status=200, payload=None, next_page="",
                 bad_json=False, total=None, not_a_list=False):
        self.status_code = status
        self._payload = [] if payload is None else payload
        self._bad = bad_json
        self._not_a_list = not_a_list
        self.headers = {"X-Next-Page": next_page}
        if total is not None:
            self.headers["X-Total"] = str(total)

    def json(self):
        if self._bad:
            raise ValueError("not json")
        if self._not_a_list:
            return {"message": "403 Forbidden"}
        return self._payload


class _FakeRequests:
    Timeout = api.requests.Timeout
    ConnectionError = api.requests.ConnectionError
    utils = api.requests.utils

    def __init__(self, script):
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
        api.reset_failures()

    def tearDown(self):
        api.requests = self._real_requests
        api.time = self._real_time
        api.reset_failures()

    def params_of(self, i=0):
        return self.fake.calls[i]["params"]


def _proj(n, count=1, kind="group", ns="grp"):
    return [{"id": i, "path_with_namespace": f"{ns}/p{i}",
             "namespace": {"kind": kind, "full_path": ns, "path": ns}}
            for i in range(n, n + count)]


# Every api.py function that returns a collection, and how to call it offline.
# If a new one is added without an entry here, add it — that is the point.
COLLECTION_FUNCS = {
    "get_top_level_groups":
        lambda: api.get_top_level_groups(URL, HEADERS),
    "get_all_groups":
        lambda: api.get_all_groups(URL, HEADERS),
    "get_subgroups":
        lambda: api.get_subgroups(URL, HEADERS, 7),
    "get_projects":
        lambda: api.get_projects(URL, HEADERS, 7),
    "get_group_projects_recursive":
        lambda: api.get_group_projects_recursive(URL, HEADERS, 7),
    "get_my_projects":
        lambda: api.get_my_projects(URL, HEADERS),
    "get_user_projects":
        lambda: api.get_user_projects(URL, HEADERS, "someone"),
    "get_user_namespace_projects_all":
        lambda: api.get_user_namespace_projects(URL, HEADERS, scope="all"),
    "get_user_namespace_projects_own":
        lambda: api.get_user_namespace_projects(URL, HEADERS, scope="own",
                                                owner_ref="someone"),
}


# ─── A. No collection function may return a short list ────────────────────────

class CollectionFunctionsNeverLieTests(ApiTestCase):
    """
    THE founding defect, asserted across the whole module rather than at the
    one place it was first patched.
    """

    FAILURE_MODES = {
        "invalid token (401)":  [_Resp(401)],
        "forbidden (403)":      [_Resp(403)],
        "server error (500)":   [_Resp(500), _Resp(500), _Resp(500)],
        "unparseable body":     [_Resp(200, bad_json=True)],
        "not a JSON list":      [_Resp(200, not_a_list=True)],
        # Transport failures are retried _RETRIES times, so the script must
        # keep failing — one failure followed by a success is a success.
        "connection refused":   [api.requests.ConnectionError("refused")] * 4,
        "timeout":              [api.requests.Timeout("slow")] * 4,
    }

    def test_every_collection_function_raises_on_every_failure_mode(self):
        for fname, call in COLLECTION_FUNCS.items():
            for mode, script in self.FAILURE_MODES.items():
                with self.subTest(func=fname, mode=mode):
                    self.install(list(script))
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        with self.assertRaises(api.ApiError):
                            call()

    def test_a_failure_never_yields_an_empty_collection(self):
        """
        The precise shape of the bug: 401 -> [] with no exception. If any of
        these ever *returns*, the caller cannot tell it from an empty group.
        """
        for fname, call in COLLECTION_FUNCS.items():
            with self.subTest(func=fname):
                self.install([_Resp(401)])
                buf = io.StringIO()
                with redirect_stdout(buf):
                    try:
                        out = call()
                    except api.ApiError:
                        continue
                self.fail(f"{fname} returned {out!r} on a 401 instead of "
                          f"raising — a failure indistinguishable from empty")

    def test_a_truncated_multipage_fetch_never_looks_complete(self):
        """Page 1 works, page 2 500s. The 100 rows in hand are NOT the set."""
        for fname, call in COLLECTION_FUNCS.items():
            with self.subTest(func=fname):
                self.install([_Resp(200, _proj(0, 100, kind="user"),
                                    next_page="2"),
                              _Resp(500), _Resp(500), _Resp(500)])
                buf = io.StringIO()
                with redirect_stdout(buf):
                    with self.assertRaises(api.ApiError) as ctx:
                        call()
                self.assertEqual(ctx.exception.status, 500)

    def test_a_genuinely_empty_result_is_complete_and_does_not_raise(self):
        """The other half of the contract: empty must still be sayable."""
        for fname, call in COLLECTION_FUNCS.items():
            with self.subTest(func=fname):
                self.install([_Resp(200, [], next_page="")])
                out = call()
                self.assertEqual(len(out), 0)
                self.assertTrue(out.complete,
                                f"{fname}: empty result must be complete")
                self.assertIsNone(out.error)


# ─── B. Completeness must survive sorted()/filtering ──────────────────────────

class CompletenessSurvivesPostProcessingTests(ApiTestCase):
    """
    A flag that sorted() erases is not a mechanism, it is a comment.

    Before W9-9 every wrapper ended in `return sorted(...)` or
    `return _user_ns_projects(...)`, both of which produce a PLAIN LIST. A
    caller doing `getattr(result, "complete", True)` therefore saw True on the
    exact path that had failed — the same silence, one layer up.
    """

    def test_every_collection_function_returns_a_PagedResult(self):
        for fname, call in COLLECTION_FUNCS.items():
            with self.subTest(func=fname):
                self.install([_Resp(200, _proj(0, 3, kind="user"),
                                    next_page="")])
                out = call()
                self.assertIsInstance(
                    out, api.PagedResult,
                    f"{fname} returned a plain {type(out).__name__}; "
                    f"`.complete` cannot reach the caller")
                self.assertTrue(hasattr(out, "complete"))
                self.assertTrue(hasattr(out, "error"))

    def test_sorting_wrappers_keep_the_incomplete_flag(self):
        """strict=False partiality must survive the sort inside the wrapper."""
        real = api.get_paginated

        def partial_fetch(url, headers, params=None, **kw):
            err = api.ApiError(500, "scripted partial", url)
            return api.PagedResult(_proj(0, 2, kind="user"), complete=False,
                                   error=err, pages=1)

        api.get_paginated = partial_fetch
        try:
            for fname, call in COLLECTION_FUNCS.items():
                if fname == "get_user_namespace_projects_own":
                    continue  # delegates to get_user_projects, covered there
                with self.subTest(func=fname):
                    out = call()
                    self.assertFalse(
                        getattr(out, "complete", True),
                        f"{fname} laundered a partial fetch into a result that "
                        f"looks complete")
                    self.assertIsNotNone(getattr(out, "error", None))
        finally:
            api.get_paginated = real

    def test_require_complete_raises_on_a_partial_and_returns_on_a_whole(self):
        self.install([_Resp(200, _proj(0, 100), next_page="2"),
                      _Resp(500), _Resp(500), _Resp(500)])
        out = api.get_paginated(f"{URL}/api/v4/projects", HEADERS,
                                quiet=True, strict=False)
        with self.assertRaises(api.ApiError):
            out.require_complete("projects")

        self.install([_Resp(200, _proj(0, 3), next_page="")])
        ok = api.get_paginated(f"{URL}/api/v4/projects", HEADERS, quiet=True)
        self.assertIs(ok.require_complete(), ok)

    def test_PagedResult_like_inherits_every_honesty_field(self):
        err = api.ApiError(403, "nope")
        src = api.PagedResult([1, 2], complete=False, error=err, pages=1,
                              total=99, note="n")
        out = api.PagedResult.like([2, 1], src)
        self.assertFalse(out.complete)
        self.assertIs(out.error, err)
        self.assertEqual(out.pages, 1)
        self.assertEqual(out.total, 99)
        self.assertTrue(out.partial)


# ─── C. quiet hides the line, never the failure ───────────────────────────────

class QuietCannotHideAFailureTests(ApiTestCase):
    """
    Half of the original bug was quiet=True at the call sites. Suppressing
    output is a reasonable thing to want; suppressing the KNOWLEDGE that
    something failed is what made the tool lie.
    """

    def test_quiet_true_still_records_the_failure(self):
        self.install([_Resp(401)])
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(api.ApiError):
                api.get_paginated(f"{URL}/api/v4/projects", HEADERS,
                                  quiet=True)
        self.assertEqual(buf.getvalue(), "",
                         "quiet=True must print nothing")
        self.assertGreater(api.failure_count(), 0,
                           "quiet=True suppressed the KNOWLEDGE of a failure")
        # Both the raw 401 and the "Incomplete fetch" summary are on record.
        self.assertTrue(any(f.status == 401 for f in api.failures()))
        self.assertIn("Incomplete fetch", api.last_failure().message)

    def test_quiet_false_prints_and_records(self):
        self.install([_Resp(401)])
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(api.ApiError):
                api.get_paginated(f"{URL}/api/v4/projects", HEADERS,
                                  quiet=False)
        self.assertIn("Invalid or expired token", buf.getvalue())
        self.assertGreater(api.failure_count(), 0)

    def test_a_quiet_success_records_nothing(self):
        """The ledger must stay clean on the happy path, or it means nothing."""
        self.install([_Resp(200, _proj(0, 3), next_page="")])
        out = api.get_paginated(f"{URL}/api/v4/projects", HEADERS, quiet=True)
        self.assertTrue(out.complete)
        self.assertEqual(api.failure_count(), 0)
        self.assertIsNone(api.last_failure())

    def test_quiet_non_strict_failure_is_still_recorded(self):
        """strict=False + quiet=True is the maximum-silence combination."""
        self.install([_Resp(401)])
        buf = io.StringIO()
        with redirect_stdout(buf):
            out = api.get_paginated(f"{URL}/api/v4/projects", HEADERS,
                                    quiet=True, strict=False)
        self.assertEqual(buf.getvalue(), "")
        self.assertFalse(out.complete)
        self.assertGreater(api.failure_count(), 0)

    def test_quiet_singular_getters_record_their_failures(self):
        for label, call in (
            ("get_group", lambda: api.get_group(URL, HEADERS, "g")),
            ("get_project", lambda: api.get_project(URL, HEADERS, "p")),
            ("my_access_level",
             lambda: api.my_access_level(URL, HEADERS, "groups", 1)),
        ):
            with self.subTest(func=label):
                api.reset_failures()
                self.install([_Resp(500), _Resp(500), _Resp(500)])
                buf = io.StringIO()
                with redirect_stdout(buf):
                    with self.assertRaises(api.ApiError):
                        call()
                self.assertGreater(api.failure_count(), 0,
                                   f"{label} failed silently and unrecorded")

    def test_reset_failures_clears_the_ledger(self):
        self.install([_Resp(401)])
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(api.ApiError):
                api.get_paginated(f"{URL}/api/v4/x", HEADERS, quiet=True)
        self.assertGreater(api.failure_count(), 0)
        api.reset_failures()
        self.assertEqual(api.failure_count(), 0)
        self.assertEqual(api.failures(), [])


# ─── D. Cross-check the walk against the server's own count ───────────────────

class XTotalCrossCheckTests(ApiTestCase):
    """
    We can walk every page we are told about and STILL be short: a proxy drops
    a page, a rate limiter answers with an empty body, X-Next-Page is rewritten.
    Nothing in the loop fails, so the old contract would call that complete.
    GitLab hands us its own record count on page 1; disagreeing with it means
    we could not find out, not that the server has none.
    """

    def test_a_shortfall_against_X_Total_is_an_incomplete_fetch(self):
        # Server says 50 records; page 1 returns 3 and claims to be the last.
        self.install([_Resp(200, _proj(0, 3), next_page="", total=50)])
        with self.assertRaises(api.ApiError) as ctx:
            api.get_paginated(f"{URL}/api/v4/projects", HEADERS, quiet=True)
        self.assertIn("50", ctx.exception.message)
        self.assertIn("3", ctx.exception.message)

    def test_the_zero_case_is_caught_too(self):
        """The exact silent-skip shape: the server has records, we received none."""
        self.install([_Resp(200, [], next_page="", total=45)])
        with self.assertRaises(api.ApiError) as ctx:
            api.get_paginated(f"{URL}/api/v4/projects", HEADERS, quiet=True)
        self.assertIn("45", ctx.exception.message)

    def test_matching_X_Total_is_complete(self):
        self.install([_Resp(200, _proj(0, 3), next_page="", total=3)])
        out = api.get_paginated(f"{URL}/api/v4/projects", HEADERS, quiet=True)
        self.assertTrue(out.complete)
        self.assertEqual(out.total, 3)
        self.assertEqual(len(out), 3)

    def test_a_genuine_zero_with_X_Total_zero_stays_complete(self):
        """"The server really has none" must remain expressible."""
        self.install([_Resp(200, [], next_page="", total=0)])
        out = api.get_paginated(f"{URL}/api/v4/projects", HEADERS, quiet=True)
        self.assertTrue(out.complete)
        self.assertEqual(len(out), 0)
        self.assertEqual(out.total, 0)

    def test_an_overshoot_is_not_an_error(self):
        """A project created mid-walk makes us hold MORE than X-Total. Not a
        loss of data, so not a failure — otherwise a live instance is unusable."""
        self.install([_Resp(200, _proj(0, 5), next_page="", total=3)])
        out = api.get_paginated(f"{URL}/api/v4/projects", HEADERS, quiet=True)
        self.assertTrue(out.complete)
        self.assertEqual(len(out), 5)

    def test_no_X_Total_header_falls_back_to_the_page_walk(self):
        """Not every endpoint/version sends X-Total (the two live instances run
        GitLab 19.1.1 and 17.5.5). Absence must not become a failure."""
        self.install([_Resp(200, _proj(0, 3), next_page="")])
        out = api.get_paginated(f"{URL}/api/v4/projects", HEADERS, quiet=True)
        self.assertTrue(out.complete)
        self.assertIsNone(out.total)

    def test_a_non_numeric_X_Total_is_ignored_not_fatal(self):
        self.install([_Resp(200, _proj(0, 3), next_page="")])
        self.fake.script = []
        resp = _Resp(200, _proj(0, 3), next_page="")
        resp.headers["X-Total"] = "lots"
        self.install([resp])
        out = api.get_paginated(f"{URL}/api/v4/projects", HEADERS, quiet=True)
        self.assertTrue(out.complete)
        self.assertIsNone(out.total)

    def test_a_caller_starting_mid_set_is_not_cross_checked(self):
        """page=3 legitimately holds fewer rows than X-Total by construction."""
        self.install([_Resp(200, _proj(0, 2), next_page="", total=500)])
        out = api.get_paginated(f"{URL}/api/v4/projects", HEADERS,
                                {"page": 3}, quiet=True)
        self.assertTrue(out.complete)

    def test_the_shortfall_is_reported_not_raised_under_strict_false(self):
        self.install([_Resp(200, _proj(0, 3), next_page="", total=50)])
        out = api.get_paginated(f"{URL}/api/v4/projects", HEADERS,
                                quiet=True, strict=False)
        self.assertFalse(out.complete)
        self.assertEqual(out.total, 50)
        self.assertEqual(len(out), 3)


# ─── E. The recursive-listing trap ────────────────────────────────────────────

class RecursiveListingTrapTests(ApiTestCase):
    """
    GET /groups/<id>/projects returns an EMPTY LIST for a group whose projects
    all live in subgroups. HTTP 200, well-formed, complete — and wrong, because
    it answers a different question. No error handling can catch this; the only
    defence is that include_subgroups=true is never dropped.

    Measured live, W9-9, on the two instances:
        group 'globex'  flat=0    recursive=23
        group 'vega'      flat=1    recursive=149
        group 'acme-one'  flat=0    recursive=103
    An empty denominator renders as ✅ "complete".
    """

    def test_recursive_listing_always_sends_include_subgroups_true(self):
        self.install([_Resp(200, [], next_page="")])
        api.get_group_projects_recursive(URL, HEADERS, 7)
        self.assertEqual(self.params_of()["include_subgroups"], "true")

    def test_the_flag_is_sent_on_every_archived_variant(self):
        for archived in (None, True, False, "all", "active", "only"):
            with self.subTest(archived=archived):
                self.install([_Resp(200, [], next_page="")])
                api.get_group_projects_recursive(URL, HEADERS, 7,
                                                 archived=archived)
                self.assertEqual(self.params_of()["include_subgroups"], "true")

    def test_the_flag_is_sent_with_a_min_access_level(self):
        self.install([_Resp(200, [], next_page="")])
        api.get_group_projects_recursive(URL, HEADERS, 7, 30)
        self.assertEqual(self.params_of()["include_subgroups"], "true")

    def test_the_flag_is_a_lowercase_string_never_a_python_bool(self):
        """requests encodes True as 'True'; GitLab's contract is 'true'."""
        self.install([_Resp(200, [], next_page="")])
        api.get_group_projects_recursive(URL, HEADERS, 7)
        v = self.params_of()["include_subgroups"]
        self.assertIsInstance(v, str)
        self.assertEqual(v, "true")

    def test_dropping_the_flag_is_detected_rather_than_silently_zero(self):
        """
        Simulates the regression directly: if a future edit removes or falsifies
        include_subgroups, the function must fail loudly instead of returning
        the empty list that renders as a complete group.
        """
        real = api.archived_param
        api.archived_param = lambda a=None: {"include_subgroups": "false"}
        try:
            self.install([_Resp(200, [], next_page="")])
            with self.assertRaises(api.ApiError) as ctx:
                api.get_group_projects_recursive(URL, HEADERS, 7)
            self.assertIn("include_subgroups", ctx.exception.message)
        finally:
            api.archived_param = real

    def test_the_flat_listing_deliberately_does_not_send_it(self):
        """get_projects is 'directly owned' by contract; the two must differ."""
        self.install([_Resp(200, [], next_page="")])
        api.get_projects(URL, HEADERS, 7)
        self.assertNotIn("include_subgroups", self.params_of())

    def test_flat_and_recursive_disagreeing_is_the_documented_trap(self):
        """Reproduces the measured 0-vs-23 shape offline."""
        self.install([_Resp(200, [], next_page="")])
        flat = api.get_projects(URL, HEADERS, 7)
        self.install([_Resp(200, _proj(0, 23), next_page="")])
        rec = api.get_group_projects_recursive(URL, HEADERS, 7)
        self.assertEqual(len(flat), 0)
        self.assertEqual(len(rec), 23)
        self.assertTrue(flat.complete, "the flat zero is a TRUE answer to the "
                                       "wrong question — that is the trap")

    def test_the_recursive_result_discloses_that_it_includes_subgroups(self):
        self.install([_Resp(200, [], next_page="")])
        out = api.get_group_projects_recursive(URL, HEADERS, 7)
        self.assertIn("subgroup", out.note.lower())


# ─── F. The archived filter is explicit and disclosed ─────────────────────────

class ArchivedIsExplicitAndDisclosedTests(ApiTestCase):
    """
    archived=False as a silent default is a mirroring tool quietly refusing to
    mirror. Measured: one live instance holds 9 archived projects, the other 0
    — so the old default was harmless on ONE of them as a property of the DATA,
    never of the code.
    """

    def test_the_default_omits_the_parameter_on_every_listing(self):
        for label, call in (
            ("get_projects", lambda: api.get_projects(URL, HEADERS, 7)),
            ("recursive",
             lambda: api.get_group_projects_recursive(URL, HEADERS, 7)),
            ("get_user_projects",
             lambda: api.get_user_projects(URL, HEADERS, "u")),
            ("uns scope=all",
             lambda: api.get_user_namespace_projects(URL, HEADERS,
                                                     scope="all")),
            ("get_my_projects", lambda: api.get_my_projects(URL, HEADERS)),
        ):
            with self.subTest(func=label):
                self.install([_Resp(200, [], next_page="")])
                call()
                self.assertNotIn("archived", self.params_of(),
                                 f"{label} silently filtered archived projects")

    def test_every_archived_choice_is_disclosed_in_words(self):
        self.assertIn("includes archived", api.archived_disclosure(None))
        self.assertIn("EXCLUDED", api.archived_disclosure(False))
        self.assertIn("EXCLUDED", api.archived_disclosure(True))
        # A filtered listing may never disclose nothing.
        for value in (True, False, "active", "only", "exclude"):
            self.assertTrue(api.archived_disclosure(value).strip(),
                            f"archived={value!r} disclosed nothing")

    def test_a_filtered_listing_carries_the_disclosure_on_the_result(self):
        self.install([_Resp(200, [], next_page="")])
        out = api.get_projects(URL, HEADERS, 7, archived=False)
        self.assertIn("EXCLUDED", out.note)
        self.install([_Resp(200, [], next_page="")])
        out = api.get_projects(URL, HEADERS, 7)
        self.assertIn("includes archived", out.note)

    def test_an_unknown_archived_value_raises_rather_than_guessing(self):
        with self.assertRaises(ValueError):
            api.archived_param("maybe")
        self.install([_Resp(200, [], next_page="")])
        with self.assertRaises(ValueError):
            api.get_projects(URL, HEADERS, 7, archived="sometimes")

    def test_the_wire_value_is_never_a_python_bool(self):
        for value in (True, False, "true", "false", "active", "only"):
            with self.subTest(value=value):
                out = api.archived_param(value)
                self.assertIsInstance(out["archived"], str)
                self.assertIn(out["archived"], ("true", "false"))


# ─── G. "does not exist" vs "I could not find out" ────────────────────────────

class SingularGettersTests(ApiTestCase):
    """
    get_group/get_project returning None for BOTH a 404 and a dead token is the
    founding defect in singular form: "Group 'x' not found." was printed when
    the real answer was "your token expired". gitlab_cli._orphans() uses
    get_project as its confirming lookup, so a None meaning "the server is
    down" would mass-label every healthy clone an orphan.
    """

    def test_a_real_404_returns_None(self):
        self.install([_Resp(404)])
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(api.get_group(URL, HEADERS, "nope"))
        self.install([_Resp(404)])
        self.assertIsNone(api.get_project(URL, HEADERS, "nope"))

    def test_everything_that_is_not_a_404_raises(self):
        for status in (401, 403, 500, 502):
            for label, call in (
                ("get_group", lambda: api.get_group(URL, HEADERS, "g")),
                ("get_project", lambda: api.get_project(URL, HEADERS, "p")),
            ):
                with self.subTest(func=label, status=status):
                    self.install([_Resp(status)] * 4)
                    with redirect_stdout(io.StringIO()):
                        with self.assertRaises(api.ApiError) as ctx:
                            call()
                    self.assertEqual(ctx.exception.status, status)

    def test_a_transport_failure_raises_rather_than_reading_as_absent(self):
        for label, call in (
            ("get_group", lambda: api.get_group(URL, HEADERS, "g")),
            ("get_project", lambda: api.get_project(URL, HEADERS, "p")),
        ):
            for exc in (api.requests.ConnectionError("x"),
                        api.requests.Timeout("x")):
                with self.subTest(func=label, exc=type(exc).__name__):
                    self.install([exc])
                    with redirect_stdout(io.StringIO()):
                        with self.assertRaises(api.ApiError):
                            call()

    def test_an_unparseable_200_raises_rather_than_returning_None(self):
        self.install([_Resp(200, bad_json=True)])
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(api.ApiError):
                api.get_project(URL, HEADERS, "p")

    def test_a_good_lookup_still_returns_the_payload(self):
        self.install([_Resp(200, {"id": 5, "full_path": "g"})])
        self.assertEqual(api.get_group(URL, HEADERS, "g")["id"], 5)
        self.install([_Resp(200, {"id": 9})])
        self.assertEqual(api.get_project(URL, HEADERS, "p")["id"], 9)


class AccessLevelTests(ApiTestCase):
    """
    my_access_level returning None for both "the server is down" and "you are
    not a member" under-reports the operator's rights — the founding defect
    applied to a permission check, in its more dangerous direction.
    """

    def test_a_failed_lookup_raises(self):
        for status in (401, 403, 404, 500):
            with self.subTest(status=status):
                self.install([_Resp(status)] * 4)
                with redirect_stdout(io.StringIO()):
                    with self.assertRaises(api.ApiError):
                        api.my_access_level(URL, HEADERS, "groups", 1)

    def test_a_transport_failure_raises(self):
        self.install([api.requests.ConnectionError("x")])
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(api.ApiError):
                api.my_access_level(URL, HEADERS, "projects", 1)

    def test_None_means_only_no_access_level_in_a_real_payload(self):
        self.install([_Resp(200, {"permissions": {}})])
        self.assertIsNone(api.my_access_level(URL, HEADERS, "groups", 1))

    def test_the_highest_level_is_returned(self):
        self.install([_Resp(200, {"permissions": {
            "group_access": {"access_level": 30},
            "project_access": {"access_level": 40}}})])
        self.assertEqual(api.my_access_level(URL, HEADERS, "projects", 1), 40)

    def test_an_invalid_kind_is_rejected_not_sent(self):
        self.install([_Resp(200, {})])
        with self.assertRaises(ValueError):
            api.my_access_level(URL, HEADERS, "users", 1)
        self.assertEqual(self.fake.calls, [],
                         "an invalid kind must not reach the network")


# ─── H. get_user_namespace_projects keeps refusing to guess ───────────────────

class UserNamespaceContractTests(ApiTestCase):
    """
    W8 wired this to its first call site behind a setting that defaults to off.
    Its refusal to guess an owner is load-bearing: guessing with an ADMIN token
    sweeps up every employee's private repositories. Do not loosen.
    """

    def test_scope_own_without_owner_ref_collects_nothing_and_makes_no_request(self):
        self.install([_Resp(200, _proj(0, 5, kind="user"), next_page="")])
        out = api.get_user_namespace_projects(URL, HEADERS, scope="own")
        self.assertEqual(len(out), 0)
        self.assertEqual(self.fake.calls, [],
                         "scope='own' guessed an owner and hit the network")

    def test_scope_off_collects_nothing_and_makes_no_request(self):
        for scope in ("", "off", "none", "false", "OFF", " None "):
            with self.subTest(scope=scope):
                self.install([_Resp(200, _proj(0, 5, kind="user"))])
                out = api.get_user_namespace_projects(URL, HEADERS, scope=scope)
                self.assertEqual(len(out), 0)
                self.assertEqual(self.fake.calls, [])

    def test_a_skipped_scope_says_it_was_skipped_not_that_none_exist(self):
        """
        The distinction this whole wave is about, in its subtlest form: 0
        because we did not look is not 0 because there are none.
        """
        for scope, kwargs in (("own", {}), ("off", {})):
            with self.subTest(scope=scope):
                self.install([])
                out = api.get_user_namespace_projects(URL, HEADERS,
                                                      scope=scope, **kwargs)
                self.assertTrue(out.complete)
                self.assertIn("NOT COLLECTED", out.note)
                self.assertIn("not a statement that none exist",
                              out.note.lower())

    def test_scope_own_with_owner_ref_queries_only_that_users_namespace(self):
        self.install([_Resp(200, _proj(0, 2, kind="user", ns="alice"),
                            next_page="")])
        out = api.get_user_namespace_projects(URL, HEADERS, scope="own",
                                              owner_ref="alice")
        self.assertIn("/users/alice/projects", self.fake.calls[0]["url"])
        self.assertEqual(len(out), 2)
        self.assertNotIn("NOT COLLECTED", out.note)

    def test_scope_all_hits_the_instance_wide_project_list(self):
        self.install([_Resp(200, _proj(0, 2, kind="user"), next_page="")])
        out = api.get_user_namespace_projects(URL, HEADERS, scope="all")
        self.assertTrue(self.fake.calls[0]["url"].endswith("/api/v4/projects"))
        self.assertIn("scope=all", out.note)
        self.assertEqual(len(out), 2)

    def test_group_namespaces_and_system_bots_are_filtered_out(self):
        payload = (_proj(0, 2, kind="group", ns="grp")
                   + _proj(10, 1, kind="user", ns="ghost")
                   + _proj(20, 1, kind="user", ns="project_5_bot_abc123")
                   + _proj(30, 1, kind="user", ns="realperson"))
        self.install([_Resp(200, payload, next_page="")])
        out = api.get_user_namespace_projects(URL, HEADERS, scope="all")
        self.assertEqual([p["path_with_namespace"] for p in out],
                         ["realperson/p30"])

    def test_a_failed_scope_all_listing_raises_rather_than_returning_zero(self):
        self.install([_Resp(401)])
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(api.ApiError):
                api.get_user_namespace_projects(URL, HEADERS, scope="all")


# ─── Module-wide structural guards ────────────────────────────────────────────

class NoSilentTruncationRemainsTests(unittest.TestCase):
    """Source-level guards, so the shape cannot come back by a new route."""

    def setUp(self):
        import inspect
        self.src = inspect.getsource(api)

    def test_the_failure_ledger_exists_and_is_the_single_choke_point(self):
        for name in ("record_failure", "failures", "failure_count",
                     "reset_failures", "last_failure"):
            self.assertTrue(hasattr(api, name), f"api.{name} is missing")

    def test_no_collection_wrapper_returns_a_bare_sorted_call(self):
        """
        `return sorted(...)` is exactly how `.complete` used to be erased.
        Every wrapper must go through PagedResult.like().
        """
        self.assertNotIn("return sorted(", self.src,
                         "a wrapper returns a plain sorted list, which erases "
                         "`.complete` on the path that failed")

    def test_get_paginated_still_defaults_to_strict(self):
        import inspect
        sig = inspect.signature(api.get_paginated)
        self.assertIs(sig.parameters["strict"].default, True,
                      "the loud default is the mechanism; a flag nobody reads "
                      "is not much better than silence")

    def test_PagedResult_is_a_real_list_so_existing_callers_keep_working(self):
        r = api.PagedResult([3, 1, 2])
        self.assertIsInstance(r, list)
        self.assertEqual(sorted(r), [1, 2, 3])
        self.assertEqual(len(r), 3)
        self.assertEqual(r[0], 3)

    def test_no_token_value_can_leak_through_an_ApiError(self):
        """Errors carry URLs; URLs here never carry credentials, and the
        headers dict is never interpolated into a message."""
        e = api.ApiError(401, "Invalid or expired token",
                         "http://h/api/v4/projects")
        self.assertNotIn("PRIVATE-TOKEN", str(e))
        self.assertNotIn("headers", str(e))


if __name__ == "__main__":
    unittest.main(verbosity=2)
