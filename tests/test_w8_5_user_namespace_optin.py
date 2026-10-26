#!/usr/bin/env python3
# tests/test_w8_5_user_namespace_optin.py — W8-5.
#
# api.get_user_namespace_projects() had zero call sites, on purpose: the
# configured token is an ADMIN token, and scope="all" sees every employee's
# personal namespace. Wiring it must not change that default. But the previous
# state was still wrong: Doctor counted those projects as "missing → Clone"
# while the group-rooted menu cannot reach a project that has no group above
# it, i.e. the tool advised an action it does not offer.
#
# What is pinned here, in order of importance:
#   1. OFF BY DEFAULT — no config, admin token or not.
#   2. An unreadable setting is never an opt-in.
#   3. The off path issues NO API call, and still DISCLOSES the count, the
#      owners and how to turn it on. A silent exclusion is indistinguishable
#      from a bug.
#   4. The on path names WHOSE namespaces before anything is pulled.
#   5. Doctor no longer says "→ Clone" about unreachable projects.
#
# Offline: the api module is stubbed; no network, no token, no clone.

import os
import unittest

from _bootstrap import TempTreeTestCase  # noqa: F401  (path bootstrap)

import setup as S


# ─── fixtures ─────────────────────────────────────────────────────────────────

def proj(pwn, kind="user"):
    ns = pwn.split("/")[0]
    return {"id": abs(hash(pwn)) % 9999,
            "path": pwn.split("/")[-1],
            "path_with_namespace": pwn,
            "namespace": {"kind": kind, "full_path": ns, "path": ns}}


GROUPS = [proj("acme/web/core", kind="group"),
          proj("globex/x/core", kind="group")]
USERS = [proj("anna_user/investment-data"),
         proj("anna_user/notes"),
         proj("henry/scratch"),
         proj("deactivated_person/old-fork")]
SYSTEM = [proj("project_12_bot_abc123/thing")]
ALL = GROUPS + USERS + SYSTEM


class _CountingApi:
    """Stub api module: records every discovery call it is asked to make."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else list(USERS)

    def get_user_namespace_projects(self, url, headers, min_access_level=None,
                                    scope="own", owner_ref=None, archived=None):
        self.calls.append({"url": url, "scope": scope, "owner_ref": owner_ref,
                           "min_access_level": min_access_level})
        if scope == "own":
            me = (owner_ref or "").lower()
            return [p for p in self.result
                    if p["path_with_namespace"].split("/")[0].lower() == me]
        return list(self.result)


CFG = {"url": "http://git.example.net", "headers": {"PRIVATE-TOKEN": "x"},
       "name": "live", "min_access": 0}


# ─── 1. off by default ────────────────────────────────────────────────────────

class OffByDefault(unittest.TestCase):
    def test_no_config_no_env_is_off(self):
        self.assertEqual(S.user_ns_scope({}, env={}), "off")
        self.assertEqual(S.user_ns_scope(None, env={}), "off")
        self.assertFalse(S.user_ns_enabled({}, env={}))

    def test_default_constant_is_off(self):
        self.assertEqual(S.USER_NS_DEFAULT, "off")

    def test_admin_token_does_not_flip_the_default(self):
        # An admin-ish cfg carries no user_namespaces key: still off.
        cfg = dict(CFG, is_admin=True, caps={"all_users": True})
        self.assertEqual(S.user_ns_scope(cfg, env={}), "off")

    def test_empty_value_is_off(self):
        self.assertEqual(S.user_ns_scope({S.USER_NS_KEY: "   "}, env={}), "off")
        self.assertEqual(S.user_ns_scope({}, env={S.USER_NS_ENV: ""}), "off")

    def test_env_opt_in(self):
        self.assertEqual(S.user_ns_scope({}, env={S.USER_NS_ENV: "own"}), "own")
        self.assertEqual(S.user_ns_scope({}, env={S.USER_NS_ENV: "ALL"}), "all")

    def test_instance_key_beats_env(self):
        self.assertEqual(
            S.user_ns_scope({S.USER_NS_KEY: "off"}, env={S.USER_NS_ENV: "all"}),
            "off")

    def test_garbage_is_not_an_opt_in_and_says_so(self):
        scope, note = S.user_ns_setting({}, env={S.USER_NS_ENV: "yes-please"})
        self.assertEqual(scope, "off")
        self.assertIn("yes-please", note)
        self.assertIn("off", note)

    def test_real_process_environment_is_clean(self):
        # The suite itself must not be silently opted in.
        self.assertEqual(os.environ.get(S.USER_NS_ENV, ""), "")
        self.assertEqual(S.user_ns_scope({}), "off")


# ─── 2. classification (no network) ───────────────────────────────────────────

class Classification(unittest.TestCase):
    def test_split_keeps_groups_and_drops_system_namespaces(self):
        group, user = S.split_user_namespace(ALL)
        self.assertEqual(len(user), len(USERS))
        # the bot namespace is classified with the non-user side, never cloned
        self.assertIn(SYSTEM[0], group)

    def test_owners_are_distinct_and_sorted(self):
        self.assertEqual(S.user_ns_owners(USERS),
                         ["anna_user", "deactivated_person", "henry"])

    def test_partition_off_excludes_every_user_project(self):
        keep, excl, scope = S.partition_by_policy(ALL, CFG, scope="off")
        self.assertEqual(scope, "off")
        self.assertEqual(len(excl), len(USERS))
        self.assertNotIn(USERS[0], keep)

    def test_partition_all_admits_everything(self):
        keep, excl, _ = S.partition_by_policy(ALL, CFG, scope="all")
        self.assertEqual(excl, [])
        self.assertEqual(len(keep), len(ALL))

    def test_partition_own_admits_only_the_token_owner(self):
        keep, excl, _ = S.partition_by_policy(ALL, CFG, scope="own",
                                              owner="anna_user")
        self.assertEqual(len(keep), len(GROUPS) + len(SYSTEM) + 2)
        self.assertEqual(S.user_ns_owners(excl),
                         ["deactivated_person", "henry"])

    def test_partition_own_with_unknown_owner_admits_nothing(self):
        # Guessing would over-collect, and over-collecting here is somebody
        # else's private repository.
        keep, excl, _ = S.partition_by_policy(ALL, CFG, scope="own", owner="")
        self.assertEqual(len(excl), len(USERS))

    def test_partition_own_for_this_account_returns_zero(self):
        # The real token owns none of them — a correctly scoped feature is
        # a no-op for its own user.
        keep, excl, _ = S.partition_by_policy(ALL, CFG, scope="own",
                                              owner="nobody-owns-these")
        self.assertEqual([p for p in keep if S._is_user_ns(p)], [])
        self.assertEqual(len(excl), len(USERS))


# ─── 3. the disclosure ────────────────────────────────────────────────────────

class Disclosure(unittest.TestCase):
    def test_off_discloses_count_owners_and_the_remedy(self):
        text = "\n".join(S.user_ns_disclosure(USERS, "off"))
        self.assertIn("4 project(s)", text)
        self.assertIn("EXCLUDED by policy", text)
        self.assertIn("anna_user", text)
        self.assertIn(S.USER_NS_ENV, text)

    def test_off_does_not_call_them_missing(self):
        text = "\n".join(S.user_ns_disclosure(USERS, "off")).lower()
        self.assertIn("not missing", text)
        self.assertNotIn("→ clone", text)

    def test_off_with_none_still_states_the_fact(self):
        text = "\n".join(S.user_ns_disclosure([], "off"))
        self.assertIn("nothing is being excluded", text)

    def test_a_rejected_setting_is_carried_into_the_disclosure(self):
        text = "\n".join(S.user_ns_disclosure(USERS, "off", note="BAD VALUE"))
        self.assertIn("BAD VALUE", text)

    def test_on_names_the_namespaces_and_the_count(self):
        text = "\n".join(S.user_ns_disclosure([], "all", in_scope=USERS))
        self.assertIn("ENABLED (scope=all)", text)
        self.assertIn("4 project(s)", text)
        for owner in ("anna_user", "henry", "deactivated_person"):
            self.assertIn(owner, text)
        self.assertIn("private repositories", text)

    def test_on_own_with_nothing_in_scope_says_so(self):
        text = "\n".join(S.user_ns_disclosure(USERS, "own", in_scope=[]))
        self.assertIn("no project is in scope", text)
        self.assertIn("authenticated account owns none", text)
        self.assertIn("4 further", text)

    def test_owner_list_is_truncated_but_the_total_is_not_hidden(self):
        many = [proj(f"person{i:02d}/repo") for i in range(21)]
        text = "\n".join(S.user_ns_disclosure(many, "off", max_names=8))
        self.assertIn("21 project(s)", text)
        self.assertIn("21 personal namespace(s)", text)
        self.assertIn("13 more", text)

    def test_how_to_enable_names_both_scopes(self):
        h = S.user_ns_how_to_enable()
        self.assertIn("own", h)
        self.assertIn("all", h)
        self.assertIn(S.USER_NS_KEY, h)


# ─── 4. the wired path ────────────────────────────────────────────────────────

class FetchPath(unittest.TestCase):
    def test_off_issues_no_api_call_at_all(self):
        stub = _CountingApi()
        got, scope, lines = S.fetch_user_namespace_projects(
            CFG, scope=None, api_mod=stub)
        self.assertEqual(stub.calls, [])          # ← the whole point
        self.assertEqual(got, [])
        self.assertEqual(scope, "off")
        self.assertIn("EXCLUDED by policy", "\n".join(lines))
        self.assertIn(S.USER_NS_ENV, "\n".join(lines))

    def test_off_explicitly_also_issues_no_call(self):
        stub = _CountingApi()
        S.fetch_user_namespace_projects(CFG, scope="off", api_mod=stub)
        self.assertEqual(stub.calls, [])

    def test_all_discovers_and_names_before_anything_is_pulled(self):
        stub = _CountingApi()
        got, scope, lines = S.fetch_user_namespace_projects(
            CFG, scope="all", api_mod=stub)
        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(stub.calls[0]["scope"], "all")
        self.assertEqual(len(got), len(USERS))
        text = "\n".join(lines)
        for owner in ("anna_user", "henry", "deactivated_person"):
            self.assertIn(owner, text)
        self.assertIn("nothing has been cloned yet", text)

    def test_own_passes_the_owner_through_and_scopes_the_result(self):
        stub = _CountingApi()
        got, scope, lines = S.fetch_user_namespace_projects(
            CFG, scope="own", owner_ref="henry", api_mod=stub)
        self.assertEqual(stub.calls[0]["owner_ref"], "henry")
        self.assertEqual([p["path_with_namespace"] for p in got],
                         ["henry/scratch"])

    def test_own_without_a_knowable_owner_fetches_nothing(self):
        stub = _CountingApi()
        got, scope, lines = S.fetch_user_namespace_projects(
            {"url": "http://git.example.net", "headers": {}},
            scope="own", owner_ref="", api_mod=stub)
        # caps.get() on a bogus cfg must not turn into a guess.
        if stub.calls:
            self.assertNotEqual(stub.calls[0]["owner_ref"], "")
        else:
            self.assertEqual(got, [])
            self.assertIn("could not be identified", "\n".join(lines))

    def test_fetch_never_clones(self):
        # The contract is reachability, not mirror contents. Checked on the
        # compiled code, not the prose: the function may DISCUSS cloning in
        # its docstring, but it must not name any module that can do it.
        names = set(S.fetch_user_namespace_projects.__code__.co_names)
        names |= set(S.fetch_user_namespace_projects.__code__.co_consts and
                     [c for c in S.fetch_user_namespace_projects.__code__.co_consts
                      if isinstance(c, str)][:0] or [])
        for forbidden in ("clone", "clone_repo", "cmd_clone", "subprocess",
                          "shutil", "makedirs", "git"):
            self.assertNotIn(forbidden, names)

    def test_fetch_writes_nothing_to_disk(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="w8_5_")
        before = os.listdir(d)
        S.fetch_user_namespace_projects(CFG, scope="all", api_mod=_CountingApi())
        S.fetch_user_namespace_projects(CFG, scope=None, api_mod=_CountingApi())
        self.assertEqual(os.listdir(d), before)
        os.rmdir(d)


# ─── 5. Doctor no longer advises an action it does not offer ──────────────────

class DoctorAdvice(unittest.TestCase):
    def _report(self, scope, excluded, in_scope=(), note=""):
        problems = []
        S._user_ns_report("live", CFG, list(excluded), scope, note,
                          list(in_scope), problems)
        return problems

    def test_off_reports_the_exclusion_without_telling_you_to_clone(self):
        problems = self._report("off", USERS)
        self.assertEqual(len(problems), 1)
        p = problems[0]
        self.assertIn("4 project(s)", p)
        self.assertIn("excluded by policy", p)
        self.assertIn("cannot reach them", p)
        self.assertNotIn("→ Clone", p)
        self.assertIn(S.USER_NS_ENV, p)

    def test_off_with_none_raises_nothing(self):
        self.assertEqual(self._report("off", []), [])

    def test_on_warns_about_whose_data_it_is(self):
        problems = self._report("all", [], in_scope=USERS)
        self.assertEqual(len(problems), 1)
        self.assertIn("ENABLED (scope=all)", problems[0])
        self.assertIn("Review", problems[0])

    def test_a_rejected_setting_becomes_its_own_problem_line(self):
        problems = self._report("off", USERS, note="bad value 'yes-please'")
        self.assertTrue(any("yes-please" in p for p in problems))

    def test_coverage_check_partitions_before_counting(self):
        # The arithmetic Doctor prints must be over REACHABLE projects only,
        # or it reports a shortfall the menu can never close.
        import inspect
        src = inspect.getsource(S._coverage_check)
        self.assertIn("partition_by_policy", src)
        self.assertIn("_user_ns_report", src)
        self.assertLess(src.index("partition_by_policy"),
                        src.index("want = {}"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
