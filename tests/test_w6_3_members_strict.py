#!/usr/bin/env python3
# tests/test_w6_3_members_strict.py — W6-3.
#
# members.py was the last holdout of the founding defect: a private
# `_get_paginated` that, on 401/403/500/exception, RETURNED THE ROWS IT HAD.
# A 40-member group whose page 2 failed rendered as a clean list of 20 with no
# error line; a token without scope rendered as "No users found."
#
# These tests pin the conversion to the api.get_paginated contract that the
# rest of the codebase already uses. Every one of them FAILS against the
# pre-fix members.py.
#
# No network: api.requests is replaced with a scripted shim, so the real
# pagination logic runs against real failures.

import io
import unittest
from contextlib import redirect_stdout

from _bootstrap import TempTreeTestCase  # noqa: F401  (path bootstrap)

import api
import members


# ─── HTTP shim ────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status=200, payload=None, next_page=""):
        self.status_code = status
        self._payload = [] if payload is None else payload
        self.headers = {"X-Next-Page": next_page}
        self.text = ""

    def json(self):
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


CFG = {"url": "http://git.example.net", "headers": {"PRIVATE-TOKEN": "x"}}


def _users(start, count):
    return [{"id": i, "name": f"u{i:03d}", "username": f"u{i}", "state": "active"}
            for i in range(start, start + count)]


class MembersFetchTestCase(unittest.TestCase):
    def setUp(self):
        self._real_requests = api.requests
        self._real_time = api.time
        api.time = _NoSleep()
        # errors.log_error writes to disk; neutralise it for these tests.
        self._real_log = members.log_error
        members.log_error = lambda *a, **k: None

    def tearDown(self):
        api.requests = self._real_requests
        api.time = self._real_time
        members.log_error = self._real_log

    def install(self, script):
        api.requests = _FakeRequests(script)
        return api.requests


# ─── The four scripted failures, at the members.py seam ───────────────────────

class PaginationContractTests(MembersFetchTestCase):
    """_get_paginated must obey api.py's strict / PagedResult contract."""

    def test_normal_multipage_success_is_complete_and_whole(self):
        self.install([_Resp(200, _users(0, 100), next_page="2"),
                      _Resp(200, _users(100, 100), next_page="3"),
                      _Resp(200, _users(200, 40), next_page="")])
        out = members._get_paginated(CFG, "groups/7/members/all",
                                     quiet=True, strict=False)
        self.assertEqual(len(out), 240)
        self.assertTrue(out.complete)
        self.assertFalse(members._is_partial(out))

    def test_empty_but_successful_is_empty_AND_complete(self):
        # The distinction the whole project exists for.
        self.install([_Resp(200, [], next_page="")])
        out = members._get_paginated(CFG, "groups/7/members/all",
                                     quiet=True, strict=False)
        self.assertEqual(len(out), 0)
        self.assertTrue(out.complete,
                        "a genuinely empty group must read as COMPLETE, "
                        "otherwise it cannot be told apart from a failure")

    def test_500_on_page_2_of_3_raises_in_strict_mode(self):
        self.install([_Resp(200, _users(0, 100), next_page="2"),
                      _Resp(500), _Resp(500), _Resp(500)])
        with self.assertRaises(api.ApiError) as ctx:
            members._get_paginated(CFG, "groups/7/members/all", quiet=True)
        self.assertEqual(ctx.exception.status, 500)

    def test_500_on_page_2_of_3_is_flagged_partial_in_nonstrict_mode(self):
        self.install([_Resp(200, _users(0, 100), next_page="2"),
                      _Resp(500), _Resp(500), _Resp(500)])
        out = members._get_paginated(CFG, "groups/7/members/all",
                                     quiet=True, strict=False)
        self.assertEqual(len(out), 100)
        self.assertFalse(out.complete,
                         "100 of ~240 rows were returned as if complete — "
                         "this is the founding bug")
        self.assertTrue(members._is_partial(out))
        self.assertEqual(out.error.status, 500)

    def test_401_on_page_1_raises_rather_than_returning_empty(self):
        self.install([_Resp(401)])
        with self.assertRaises(api.ApiError) as ctx:
            members._get_paginated(CFG, "users", quiet=True)
        self.assertEqual(ctx.exception.status, 401)

    def test_401_on_page_1_is_partial_not_a_clean_empty_list(self):
        self.install([_Resp(401)])
        out = members._get_paginated(CFG, "users", quiet=True, strict=False)
        self.assertEqual(len(out), 0)
        self.assertFalse(out.complete,
                         "an unauthorised listing must never read as "
                         "'there are zero members'")

    def test_403_is_a_failure_not_an_empty_answer(self):
        # The pre-fix code special-cased 403 and RETURNED what it had.
        self.install([_Resp(403)])
        out = members._get_paginated(CFG, "users", quiet=True, strict=False)
        self.assertFalse(out.complete)

    def test_result_is_still_a_real_list_for_downstream_callers(self):
        self.install([_Resp(200, _users(0, 3), next_page="")])
        out = members._get_paginated(CFG, "users", quiet=True, strict=False)
        self.assertIsInstance(out, list)
        self.assertEqual(out[0]["id"], 0)
        self.assertEqual(len(sorted(out, key=lambda u: u["id"])), 3)


# ─── What the user actually sees ──────────────────────────────────────────────

class ScreenVisibilityTests(MembersFetchTestCase):
    """A number on screen may never silently under-report."""

    def test_partial_banner_is_loud_and_names_the_shortfall(self):
        self.install([_Resp(200, _users(0, 100), next_page="2"), _Resp(500),
                      _Resp(500), _Resp(500)])
        out = members._get_paginated(CFG, "users", quiet=True, strict=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            flagged = members._partial_banner(out, "Members")
        text = buf.getvalue()
        self.assertTrue(flagged)
        self.assertIn("INCOMPLETE", text)
        self.assertIn("UNKNOWN", text)

    def test_complete_result_prints_no_banner(self):
        self.install([_Resp(200, _users(0, 5), next_page="")])
        out = members._get_paginated(CFG, "users", quiet=True, strict=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            flagged = members._partial_banner(out, "Members")
        self.assertFalse(flagged)
        self.assertEqual(buf.getvalue(), "")

    def test_count_label_marks_a_partial_count(self):
        self.install([_Resp(401)])
        bad = members._get_paginated(CFG, "users", quiet=True, strict=False)
        self.assertIn("INCOMPLETE", members._count_label(bad, len(bad)))
        self.install([_Resp(200, _users(0, 4), next_page="")])
        good = members._get_paginated(CFG, "users", quiet=True, strict=False)
        self.assertEqual(members._count_label(good, 4), "4 total")

    def test_members_list_screen_shouts_when_the_list_is_short(self):
        # 3 members shown out of an unknown real total -> must be visible.
        members._can_list_all_users = lambda cfg: True
        self.install([_Resp(200, _users(0, 100), next_page="2"),
                      _Resp(500), _Resp(500), _Resp(500),   # active, truncated
                      _Resp(200, [], next_page=""),          # blocked
                      _Resp(200, [], next_page="")])         # fallback groups
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                members.cmd_members_list(CFG)
        except (EOFError, OSError):
            pass
        text = buf.getvalue()
        self.assertIn("INCOMPLETE", text,
                      "the Members screen printed a count with no warning that "
                      "it is short — the exact founding defect")

    def test_access_map_partial_membership_is_labelled(self):
        self.install([_Resp(200, [{"source_type": "Namespace",
                                   "source_id": 1,
                                   "source_full_name": "grp",
                                   "access_level": 30}] * 100,
                            next_page="2"),
                      _Resp(500), _Resp(500), _Resp(500)])
        buf = io.StringIO()
        with redirect_stdout(buf):
            members.cmd_member_access(CFG, {"name": "T", "id": 9})
        self.assertIn("INCOMPLETE", buf.getvalue())


class MutatingScreenRefusesPartialTests(MembersFetchTestCase):
    """Group Permissions grants/revokes — it must abort, not guess."""

    def test_group_permissions_aborts_instead_of_drawing_a_short_tree(self):
        self.install([_Resp(200, [{"id": i, "name": f"g{i}",
                                   "full_path": f"g{i}"}
                                  for i in range(100)], next_page="2"),
                      _Resp(500), _Resp(500), _Resp(500)])
        buf = io.StringIO()
        with redirect_stdout(buf):
            members.cmd_group_permissions(CFG, {"name": "T", "id": 9})
        text = buf.getvalue()
        self.assertNotIn("Groups (100 total)", text,
                         "a truncated group tree was rendered as authoritative "
                         "on a screen that grants and revokes access")
        self.assertTrue("did not complete" in text or "Refusing" in text,
                        f"no refusal was shown; got:\n{text}")


if __name__ == "__main__":
    unittest.main()
