# tests/test_w9_1_members_validation.py — W9-1
#
# The Members/Activity screen used to pass whatever the user typed straight
# into GitLab's `?after=` query parameter and then report the empty answer as
# a fact about a person's work:
#
#     _parse_since('b')           -> 'b'           -> ?after=b
#     _parse_since('2 weeks ago') -> '2 weeks ago' -> ?after=2 weeks ago
#     -> "⚠ No activity found since b."
#
# That is the project's founding defect: a result computed one way (GitLab
# silently returning nothing for an unparseable filter) reported as if it had
# been verified another (a claim about this person's activity). Note it is
# BROADER than the identical bug in log.py — there `2 weeks ago` was a correct
# git filter; here it was garbage too, because `?after=` is not approxidate.
#
# These tests pin, in order:
#   1. the premise (what the pre-fix parser did, and that GitLab's param needs
#      a calendar date);
#   2. that the validators are REUSED from log.py, not re-implemented;
#   3. what the user actually sees for each required value;
#   4. that an empty result is verified rather than assumed;
#   5. that every prompt accepts an uppercase answer;
#   6. that an unrecognised answer is never silently treated as a default.
#
# No network, no token, no real GitLab: api.get_paginated is replaced by a
# recorder so the exact query parameters can be asserted on.

import builtins
import contextlib
import io
import os
import re
import unittest

import _bootstrap  # noqa: F401  (path + error-log isolation)

import api
from api import PagedResult
import members
import log
from printer import menu_key

CFG = {"url": "http://git.example.net", "headers": {}}
USER = {"name": "Ali", "id": 7}

EVENT = {
    "created_at": "2026-09-20T10:00:00Z",
    "project_id": 1,
    "action_name": "pushed",
    "push_data": {"ref": "main", "commit_count": 2, "commit_title": "x"},
}


class _Recorder:
    """Stands in for api.get_paginated and records every query it was given."""

    def __init__(self, filtered=(), unfiltered=(),
                 filtered_complete=True, unfiltered_complete=True):
        self.calls = []
        self.filtered = list(filtered)
        self.unfiltered = list(unfiltered)
        self.filtered_complete = filtered_complete
        self.unfiltered_complete = unfiltered_complete

    def __call__(self, url, headers, params=None, quiet=False,
                 strict=True, **kw):
        p = dict(params or {})
        self.calls.append((url.split("/api/v4/")[-1], p))
        if "after" in p:
            return PagedResult(self.filtered, complete=self.filtered_complete,
                               error=None, pages=1)
        return PagedResult(self.unfiltered, complete=self.unfiltered_complete,
                           error=None, pages=1)

    @property
    def afters(self):
        return [p["after"] for _u, p in self.calls if "after" in p]


class MembersScreenTestCase(unittest.TestCase):
    """Isolates api.get_paginated and the project-info lookup."""

    def setUp(self):
        self._real_paginated = api.get_paginated
        self._real_projinfo = members._get_project_info
        members._get_project_info = lambda cfg, pid, cache: {
            "name": "p", "path": "g/p"}

    def tearDown(self):
        api.get_paginated = self._real_paginated
        members._get_project_info = self._real_projinfo

    def run_activity(self, since, author=None, rec=None, user=None):
        """Drive cmd_member_activity and return (stdout, recorder)."""
        rec = rec or _Recorder(filtered=[], unfiltered=[EVENT])
        api.get_paginated = rec
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            members.cmd_member_activity(CFG, dict(user or USER), since, author)
        return buf.getvalue(), rec


def drive(fn, answers, *a, **kw):
    """Run `fn`, feeding `answers` to input(). Returns (value, stdout)."""
    it = iter(answers)
    old = builtins.input
    builtins.input = lambda *_a, **_k: next(it)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            return fn(*a, **kw), buf.getvalue()
    except StopIteration:
        return "<ran out of answers>", buf.getvalue()
    except SystemExit as exc:
        return "SystemExit(%s)" % exc.code, buf.getvalue()
    finally:
        builtins.input = old


# ─── 1. The premise ───────────────────────────────────────────────────────────

class PremiseTests(unittest.TestCase):
    """Pin what the OLD parser did, so the fix cannot rot silently.

    _parse_since is kept as the offline shortcut expander and is deliberately
    still permissive; the point of these tests is that its output is NOT
    trustworthy on its own, which is why nothing calls it directly any more.
    """

    def test_parse_since_returns_garbage_unchanged(self):
        for bad in ("b", "", "not a date", "--all", "x;y`id`"):
            self.assertEqual(members._parse_since(bad), bad.strip().lower(),
                             msg="_parse_since must still be the raw, "
                                 "UNVALIDATED expander it always was")

    def test_parse_since_leaves_relative_english_unresolved(self):
        # This is the extra mile members.py has over log.py: '2 weeks ago' is
        # a perfectly good git date but NOT a value GitLab's ?after= accepts.
        self.assertEqual(members._parse_since("2 weeks ago"), "2 weeks ago")
        self.assertFalse(re.match(r"^\d{4}-\d{2}-\d{2}$",
                                  members._parse_since("2 weeks ago")))

    def test_parse_since_resolves_only_the_shortcut_form(self):
        self.assertRegex(members._parse_since("1w"), r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(members._parse_since("3d"), r"^\d{4}-\d{2}-\d{2}$")


# ─── 2. Reuse, not reimplementation ──────────────────────────────────────────

class ReuseTests(unittest.TestCase):
    """The rule must exist in ONE place. Three disagreeing directory walkers
    in this same project are what that costs when it does not."""

    def test_validators_are_the_ones_from_log(self):
        self.assertIs(members._log_validate_since, log.validate_since)
        self.assertIs(members._log_validate_author, log.validate_author)
        self.assertIs(members.SinceError, log.SinceError)
        self.assertIs(members.AuthorError, log.AuthorError)

    def test_members_defines_no_second_date_judge(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(os.path.dirname(here), "members.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        for copied in ("_syntactically_known", "_resolve_since",
                       "_is_calendar_date", "_DATE_FORMATS", "rev-parse"):
            self.assertNotIn(copied, src,
                             msg=f"{copied} looks copied out of log.py — "
                                 f"import it instead")

    def test_validated_since_delegates(self):
        seen = {}
        real = members._log_validate_since

        def spy(raw, repos=None):
            seen["raw"] = raw
            return real(raw, repos)

        members._log_validate_since = spy
        try:
            members.validated_since("2 weeks ago")
        finally:
            members._log_validate_since = real
        self.assertEqual(seen["raw"], "2 weeks ago")


# ─── 3. validated_since itself ───────────────────────────────────────────────

class ValidatedSinceTests(unittest.TestCase):

    def test_garbage_is_refused(self):
        for bad in ("b", "", "   ", "not a date", "--all", "x;y`id`"):
            with self.assertRaises(members.SinceError, msg=repr(bad)):
                members.validated_since(bad)

    def test_none_is_refused(self):
        with self.assertRaises(members.SinceError):
            members.validated_since(None)

    def test_legitimate_values_yield_a_calendar_date(self):
        for good in ("2 weeks ago", "yesterday", "2026-01-01", "1d", "7d",
                     "2w", "1m", "1y", "3.days", "last monday"):
            after, spoken, note = members.validated_since(good)
            self.assertRegex(after, r"^\d{4}-\d{2}-\d{2}$",
                             msg=f"{good!r} must resolve to an ISO day for "
                                 f"?after=, not to the words the user typed")
            self.assertTrue(note)

    def test_after_is_never_the_raw_words(self):
        after, _s, _n = members.validated_since("2 weeks ago")
        self.assertNotIn("weeks", after)
        self.assertNotIn("ago", after)

    def test_explicit_date_round_trips_within_the_buffer(self):
        after, _s, _n = members.validated_since("2026-01-01")
        # The one-day inclusion buffer is deliberately preserved.
        self.assertEqual(after, "2025-12-31")

    def test_iso_day_helper_keeps_the_one_day_buffer(self):
        import datetime
        epoch = int(datetime.datetime(2026, 5, 10, 12, 0).timestamp())
        self.assertEqual(members._iso_day_for_after(epoch), "2026-05-09")


class ValidatedAuthorTests(unittest.TestCase):

    def test_invalid_regex_is_refused(self):
        for bad in ("[", "a\\", "*[a-"):
            with self.assertRaises(members.AuthorError, msg=repr(bad)):
                members.validated_author(bad)

    def test_valid_patterns_accepted(self):
        for good in ("alex", "t@t", "Jane Doe", None):
            members.validated_author(good)   # must not raise


# ─── 4. What the user actually sees ──────────────────────────────────────────

CLAIM = ("⚠", "📊")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text):
    """The text as a human reads it, with the colour codes removed."""
    return _ANSI.sub("", text)


def claim_lines(text):
    """Only the lines that make a CLAIM about the data.

    Asserted on instead of raw substrings because a refusal deliberately
    QUOTES the old wording while explaining why it was wrong. ANSI colour
    codes are stripped first — they sit BEFORE the marker character, so a
    naive startswith() would match nothing and quietly pass every test.
    """
    return [ln.strip() for ln in plain(text).splitlines()
            if any(ln.strip().startswith(c) for c in CLAIM)]


class ActivityOutputTests(MembersScreenTestCase):

    REQUIRED = ["b", "", "2 weeks ago", "yesterday", "2026-01-01",
                "not a date", "--all", "x;y`id`"]

    def test_garbage_queries_nothing_at_all(self):
        for bad in ("b", "", "not a date", "--all", "x;y`id`"):
            out, rec = self.run_activity(bad)
            self.assertEqual(rec.calls, [],
                             msg=f"{bad!r} must not reach the API")
            self.assertIn("Nothing was searched", out)

    def test_garbage_never_claims_an_absence_of_activity(self):
        for bad in ("b", "", "not a date", "--all", "x;y`id`"):
            out, _rec = self.run_activity(bad)
            for ln in claim_lines(out):
                self.assertNotIn("No activity after", ln,
                                 msg=f"{bad!r} produced a data claim")

    def test_refusal_differs_from_an_empty_result(self):
        refused, _ = self.run_activity("b")
        empty, _ = self.run_activity("1d")
        self.assertNotEqual(claim_lines(refused), claim_lines(empty))

    def test_option_looking_value_is_named_as_such(self):
        out, rec = self.run_activity("--all")
        self.assertEqual(rec.calls, [])
        self.assertIn("looks like a command-line option", out)

    def test_every_required_value_round_trips_without_a_traceback(self):
        for v in self.REQUIRED:
            out, _rec = self.run_activity(v)
            self.assertNotIn("Traceback", out)

    def test_accepted_value_echoes_the_interpretation_applied(self):
        out, rec = self.run_activity("2 weeks ago")
        self.assertIn("Since : 2 weeks ago", out)
        self.assertIn("git read this as", out)
        self.assertIn("querying GitLab with after=", out)
        # filtered query + the unfiltered verification query
        self.assertEqual(len(rec.calls), 2)
        self.assertEqual(len(rec.afters), 1)
        self.assertRegex(rec.afters[0], r"^\d{4}-\d{2}-\d{2}$")

    def test_the_echoed_after_is_the_after_that_was_sent(self):
        out, rec = self.run_activity("2026-01-01")
        self.assertIn(f"after={rec.afters[0]}", out)

    def test_raw_words_never_reach_the_query(self):
        _out, rec = self.run_activity("2 weeks ago")
        for after in rec.afters:
            self.assertRegex(after, r"^\d{4}-\d{2}-\d{2}$")

    def test_shell_metacharacters_reach_no_shell(self):
        canary = os.path.join(os.environ.get("TMPDIR", "/tmp"),
                              "w9_1_canary_PWNED")
        if os.path.exists(canary):
            os.remove(canary)
        for payload in (f"$(touch {canary})", f"`touch {canary}`",
                        f"; touch {canary}", f"x;y`touch {canary}`"):
            out, _rec = self.run_activity(payload)
            self.assertNotIn("Traceback", out)
        self.assertFalse(os.path.exists(canary),
                         "a shell metacharacter was executed")

    def test_bad_author_queries_nothing_and_claims_nothing(self):
        out, rec = self.run_activity("1w", author="[")
        self.assertEqual(rec.calls, [])
        self.assertIn("regular expression", out)
        for ln in claim_lines(out):
            self.assertNotIn("No activity after", ln)

    def test_good_author_is_echoed_and_queried(self):
        rec = _Recorder(filtered=[EVENT], unfiltered=[EVENT])
        out, rec = self.run_activity("1w", author="alex", rec=rec)
        self.assertIn("Author: alex", out)
        self.assertIn("📊", out)


# ─── 5. An empty result is verified, not assumed ─────────────────────────────

class EmptyResultVerificationTests(MembersScreenTestCase):

    def test_empty_filter_triggers_an_unfiltered_check(self):
        rec = _Recorder(filtered=[], unfiltered=[EVENT])
        _out, rec = self.run_activity("1d", rec=rec)
        self.assertEqual(len(rec.calls), 2)
        self.assertNotIn("after", rec.calls[1][1],
                         "the verification query must drop the date filter")

    def test_filter_excluded_them_says_so_with_a_number(self):
        rec = _Recorder(filtered=[], unfiltered=[EVENT, EVENT])
        out, _rec = self.run_activity("1d", rec=rec)
        self.assertIn("Verified:", out)
        self.assertIn("2 recorded event", out)
        self.assertIn("the filter is what excluded them", out)

    def test_genuinely_no_activity_is_a_different_sentence(self):
        rec = _Recorder(filtered=[], unfiltered=[])
        out, _rec = self.run_activity("1d", rec=rec)
        self.assertIn("NO recorded activity at all", out)
        self.assertNotIn("Verified:", out)

    def test_the_two_empty_cases_do_not_look_alike(self):
        a, _ = self.run_activity("1d", rec=_Recorder([], [EVENT]))
        b, _ = self.run_activity("1d", rec=_Recorder([], []))
        self.assertNotEqual(claim_lines(a), claim_lines(b))

    def test_unverifiable_empty_result_is_labelled_unconfirmed(self):
        rec = _Recorder(filtered=[], unfiltered=[], unfiltered_complete=False)
        out, _rec = self.run_activity("1d", rec=rec)
        self.assertIn("Could NOT verify", out)
        self.assertNotIn("NO recorded activity at all", out)

    def test_a_failed_filtered_fetch_is_an_error_not_an_absence(self):
        rec = _Recorder(filtered=[], unfiltered=[EVENT],
                        filtered_complete=False)
        out, rec = self.run_activity("1d", rec=rec)
        self.assertIn("this is an ERROR", out)
        self.assertEqual(len(rec.calls), 1,
                         "a failed fetch must not trigger the empty-result "
                         "verification; it is not an empty result")

    def test_a_non_empty_result_pays_for_no_extra_query(self):
        rec = _Recorder(filtered=[EVENT], unfiltered=[EVENT])
        _out, rec = self.run_activity("1d", rec=rec)
        self.assertEqual(len(rec.calls), 1)


# ─── 6. Prompts are case-insensitive ─────────────────────────────────────────

class PromptCaseTests(MembersScreenTestCase):
    """13 of 17 prompts here used to reject an uppercase answer that worked in
    lowercase. Every one now goes through printer.menu_key."""

    def test_members_py_uses_the_shared_helper(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(os.path.dirname(here), "members.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("menu_key", src)
        # No hand-rolled lowercasing of an ANSWER survives. `.strip().lower()`
        # appears exactly once, inside the legacy _parse_since expander, which
        # normalises a DATE STRING and not a menu key.
        self.assertEqual(src.count(".strip().lower()"), 1)
        for ln in src.splitlines():
            if "input(" in ln:
                self.assertNotIn(".lower()", ln,
                                 msg="a prompt is lowercasing by hand instead "
                                     "of using printer.menu_key: " + ln.strip())

    def test_pick_role_accepts_padded_and_uppercase(self):
        self.assertEqual(drive(members._pick_role, ["3"])[0], 30)
        self.assertEqual(drive(members._pick_role, [" 3 "])[0], 30)
        self.assertIsNone(drive(members._pick_role, ["B"])[0])
        self.assertIsNone(drive(members._pick_role, ["b"])[0])

    def test_member_picker_matches_name_case_insensitively(self):
        users = [{"id": 1, "name": "ali reza", "username": "alireza",
                  "state": "active"}]
        members._get_all_users = lambda cfg: PagedResult(users, complete=True,
                                                         pages=1)
        for answer in ("ALI", "AliReza", "ali"):
            picked, _out = drive(members.cmd_members_list, [answer], CFG)
            self.assertEqual(picked["username"], "alireza", msg=answer)

    def test_member_picker_quit_key_is_case_insensitive(self):
        users = [{"id": 1, "name": "ali", "username": "ali", "state": "active"}]
        members._get_all_users = lambda cfg: PagedResult(users, complete=True,
                                                         pages=1)
        for answer in ("q", "Q", " Q "):
            picked, _out = drive(members.cmd_members_list, [answer], CFG)
            self.assertIsNone(picked, msg=answer)

    def test_revoke_confirmation_accepts_uppercase_y(self):
        import requests
        real = requests.delete
        hits = []

        class R:
            status_code = 204

        requests.delete = lambda *a, **k: (hits.append(1), R())[1]
        try:
            for answer, expected in (("Y", True), ("y", True), ("n", False),
                                     ("N", False), ("", False)):
                hits.clear()
                drive(members._revoke_group_member, [answer],
                      CFG, 1, 2, "Ali", "g/p")
                self.assertEqual(bool(hits), expected, msg=repr(answer))
        finally:
            requests.delete = real

    def test_delete_confirmation_accepts_uppercase_yes_and_username(self):
        import requests
        real = requests.delete
        hits = []

        class R:
            status_code = 204

        requests.delete = lambda *a, **k: (hits.append(1), R())[1]
        user = {"name": "Ali", "id": 7, "username": "alireza",
                "state": "active"}
        try:
            for c1, c2, expected in (("YES", "ALIREZA", True),
                                     ("yes", "alireza", True),
                                     ("Yes", "AliReza", True),
                                     ("yes", "someone", False),
                                     ("no", "alireza", False)):
                hits.clear()
                drive(members.cmd_user_actions, ["5", c1, c2], CFG, dict(user))
                self.assertEqual(bool(hits), expected,
                                 msg=f"{c1!r}/{c2!r}")
        finally:
            requests.delete = real

    def test_menu_key_is_what_normalises(self):
        self.assertEqual(menu_key("  Q "), "q")
        self.assertEqual(menu_key("YES"), "yes")


# ─── 7. An unrecognised input is never a silent default ──────────────────────

class NoSilentDefaultTests(MembersScreenTestCase):

    def _put_spy(self):
        import requests
        real = requests.put
        sent = []

        class R:
            status_code = 200
            text = ""

        requests.put = lambda *a, **k: (sent.append(k.get("json")), R())[1]
        return requests, real, sent

    def test_boolean_field_never_writes_false_for_an_unknown_key(self):
        """The worst instance: `payload = {field: sel == "1"}` turned ANY
        unrecognised answer into False and wrote it to the account."""
        requests, real, sent = self._put_spy()
        user = {"name": "Ali", "id": 7, "username": "a",
                "can_create_group": True}
        try:
            for answer, expected in (("1", [{"can_create_group": True}]),
                                     ("2", [{"can_create_group": False}]),
                                     ("9", []), ("", []), ("yes", [])):
                sent.clear()
                _out, txt = drive(members.cmd_edit_account, ["6", answer],
                                  CFG, dict(user))
                self.assertEqual(sent, expected, msg=repr(answer))
                if expected == []:
                    # Refused OUT LOUD, either way printer words it.
                    self.assertTrue(
                        "is not one of" in plain(txt)
                        or "Nothing entered" in plain(txt),
                        msg=f"{answer!r} was refused silently")
        finally:
            requests.put = real

    def test_unknown_field_number_is_refused_out_loud(self):
        user = {"name": "Ali", "id": 7, "username": "a"}
        _out, txt = drive(members.cmd_edit_account, ["99"], CFG, dict(user))
        self.assertTrue("is not one of" in plain(txt)
                        or "Nothing entered" in plain(txt))

    def test_access_level_menu_refuses_an_unknown_key(self):
        requests, real, sent = self._put_spy()
        user = {"name": "Ali", "id": 7, "username": "a", "is_admin": False}
        try:
            sent.clear()
            _out, txt = drive(members.cmd_edit_account, ["8", "9"],
                              CFG, dict(user))
            self.assertEqual(sent, [])
            self.assertIn("is not one of", plain(txt))
        finally:
            requests.put = real

    def test_pick_role_names_the_rejected_key(self):
        picked, txt = drive(members._pick_role, ["z"])
        self.assertIsNone(picked)
        self.assertIn("'z' is not one of", plain(txt))

    def test_blocked_user_cannot_be_banned_by_an_un_offered_key(self):
        """The blocked-user screen offers 1, 2, 5, 6 — but the action map was
        built unconditionally, so '3' silently BANNED the account."""
        import requests
        real = requests.request
        calls = []

        class R:
            status_code = 200
            text = ""

        requests.request = lambda m, u, **k: (calls.append(u), R())[1]
        user = {"name": "Ali", "id": 7, "username": "a", "state": "blocked"}
        try:
            for answer in ("3", "4"):
                calls.clear()
                _out, txt = drive(members.cmd_user_actions, [answer],
                                  CFG, dict(user))
                self.assertEqual(calls, [],
                                 msg=f"key {answer!r} was not offered on the "
                                     f"blocked-user screen but acted anyway")
                self.assertIn("is not one of", plain(txt))
        finally:
            requests.request = real

    def test_blocked_user_keys_that_ARE_offered_still_work(self):
        import requests
        real = requests.request
        calls = []

        class R:
            status_code = 200
            text = ""

        requests.request = lambda m, u, **k: (calls.append(u), R())[1]
        user = {"name": "Ali", "id": 7, "username": "a", "state": "blocked"}
        try:
            drive(members.cmd_user_actions, ["1", "y"], CFG, dict(user))
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0].endswith("/unblock"))
        finally:
            requests.request = real

    def test_manage_submenu_refuses_an_unknown_key(self):
        import caps
        real_get, real_can = caps.get, caps.can
        caps.get = lambda cfg: {}
        caps.can = lambda info, what: True
        user = {"name": "Ali", "id": 7, "username": "a", "state": "active"}
        try:
            _out, txt = drive(members.cmd_member_actions,
                              ["4", "9", "b", "q"], CFG, dict(user))
            self.assertIn("is not one of", plain(txt))
        finally:
            caps.get, caps.can = real_get, real_can

    def test_top_menu_refuses_an_unknown_key_instead_of_continuing(self):
        user = {"name": "Ali", "id": 7, "username": "a", "state": "active"}
        # "5" is not on the menu. It must be refused and the menu re-shown,
        # never fall through to the "Enter = continue" prompt.
        _out, txt = drive(members.cmd_member_actions, ["5", "q"], CFG,
                          dict(user))
        self.assertIn("is not one of", plain(txt))

    def test_empty_member_choice_is_not_a_default(self):
        users = [{"id": 1, "name": "ali", "username": "ali", "state": "active"}]
        members._get_all_users = lambda cfg: PagedResult(users, complete=True,
                                                         pages=1)
        picked, txt = drive(members.cmd_members_list, ["", "q"], CFG)
        self.assertIsNone(picked)
        self.assertIn("Nothing entered", plain(txt))


if __name__ == "__main__":
    unittest.main(verbosity=2)
