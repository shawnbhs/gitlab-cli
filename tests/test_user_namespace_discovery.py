#!/usr/bin/env python3
# tests/test_user_namespace_discovery.py — W3 hardening.
#
# The group-rooted menu structurally cannot reach a project that lives in a
# USER namespace (anna_user/investment-data — the one the group walk missed). The fix
# added get_user_namespace_projects(), and the dangerous part of it is not the
# discovery, it is the DEFAULT: an admin token asked for "all user namespaces"
# pulls every employee's private repositories. So the safety property is the
# thing worth pinning — opt-in, and never guessing an owner.
#
# The old get_my_projects() is also the wrong basis and is documented as dead:
# membership=true returns only projects the token is a member of and EXCLUDES
# the user-namespace project it looks like it solves.
#
# Offline: api.requests is replaced for the duration of each test.

import unittest

from _bootstrap import TempTreeTestCase  # noqa: F401  (path bootstrap)

import api


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.headers = {"X-Next-Page": ""}

    def json(self):
        return self._payload


class _FakeRequests:
    Timeout = api.requests.Timeout
    ConnectionError = api.requests.ConnectionError
    utils = api.requests.utils

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        return _Resp(self.payload)


def project(path_with_namespace, kind="user", ns_path=None):
    ns = ns_path or path_with_namespace.split("/")[0]
    return {"id": abs(hash(path_with_namespace)) % 10_000,
            "path": path_with_namespace.split("/")[-1],
            "path_with_namespace": path_with_namespace,
            "http_url_to_repo": f"http://git.example.net/{path_with_namespace}.git",
            "namespace": {"kind": kind, "full_path": ns, "path": ns}}


class ApiStubCase(unittest.TestCase):
    def install(self, payload):
        self.fake = _FakeRequests(payload)
        api.requests = self.fake
        return self.fake

    def setUp(self):
        self._real = api.requests

    def tearDown(self):
        api.requests = self._real


class ScopeSafetyTests(ApiStubCase):
    """Default-off is the whole point: an admin token must not sweep up every
    user's private repositories because a menu item existed."""

    def test_the_default_scope_returns_nothing_without_an_explicit_owner(self):
        self.install([project("anna_user/investment-data")])
        self.assertEqual(
            api.get_user_namespace_projects("http://h", {}), [])

    def test_the_default_scope_makes_no_request_at_all(self):
        self.install([project("anna_user/investment-data")])
        api.get_user_namespace_projects("http://h", {})
        self.assertEqual(self.fake.calls, [],
                         "the instance was queried with no owner specified")

    def test_an_explicitly_disabled_scope_returns_nothing(self):
        self.install([project("a/b")])
        for scope in ("", "off", "none", "false"):
            self.assertEqual(
                api.get_user_namespace_projects("http://h", {}, scope=scope), [],
                scope)
        self.assertEqual(self.fake.calls, [])

    def test_scope_own_queries_only_that_users_namespace_endpoint(self):
        self.install([project("anna_user/investment-data")])
        out = api.get_user_namespace_projects("http://h", {}, scope="own",
                                              owner_ref="anna_user")
        self.assertEqual([p["path_with_namespace"] for p in out],
                         ["anna_user/investment-data"])
        self.assertIn("/users/anna_user/projects", self.fake.calls[0]["url"])

    def test_scope_all_is_reachable_but_must_be_asked_for_by_name(self):
        self.install([project("anna_user/investment-data"),
                      project("someone_else/private-notes")])
        out = api.get_user_namespace_projects("http://h", {}, scope="all")
        self.assertEqual(len(out), 2)
        self.assertIn("/projects", self.fake.calls[0]["url"])


class NamespaceFilteringTests(ApiStubCase):

    def test_group_projects_are_excluded(self):
        self.install([project("initech/feed-ms", kind="group"),
                      project("anna_user/investment-data")])
        out = api.get_user_namespace_projects("http://h", {}, scope="all")
        self.assertEqual([p["path_with_namespace"] for p in out],
                         ["anna_user/investment-data"])

    def test_service_principals_are_excluded(self):
        """Bot namespaces are not people and their repos are not a mirror."""
        self.install([project("ghost/x"), project("support-bot/y"),
                      project("group_35_bot_ab12cd/z"),
                      project("anna_user/investment-data")])
        out = api.get_user_namespace_projects("http://h", {}, scope="all")
        self.assertEqual([p["path_with_namespace"] for p in out],
                         ["anna_user/investment-data"])

    def test_the_system_namespace_predicate_is_conservative_about_real_users(self):
        for name in ("anna_user", "alex_doe", "robot-lover"):
            self.assertFalse(api.is_system_namespace(name), name)
        for name in ("ghost", "support-bot", "GitLabDuo",
                     "project_9_bot_deadbeef", "alert-bot", ""):
            self.assertTrue(api.is_system_namespace(name), name)

    def test_results_are_sorted_by_path_with_namespace(self):
        self.install([project("zeta_user/b"), project("alpha_user/a")])
        out = api.get_user_namespace_projects("http://h", {}, scope="all")
        self.assertEqual([p["path_with_namespace"] for p in out],
                         ["alpha_user/a", "zeta_user/b"])

    def test_a_user_endpoint_returning_group_projects_is_still_filtered(self):
        """/users/<id>/projects also returns contributed group projects on some
        GitLab versions."""
        self.install([project("initech/feed-ms", kind="group"),
                      project("anna_user/investment-data")])
        out = api.get_user_projects("http://h", {}, "anna_user")
        self.assertEqual([p["path_with_namespace"] for p in out],
                         ["anna_user/investment-data"])


class DeadCodeTests(unittest.TestCase):
    """get_my_projects is the WRONG basis and must stay documented as such, so
    nobody wires it into the menu believing it solves the user-namespace gap."""

    def test_it_is_still_marked_unused(self):
        doc = (api.get_my_projects.__doc__ or "")
        self.assertTrue(doc.strip().upper().startswith("UNUSED"))

    def test_it_has_no_caller_in_the_codebase(self):
        """Comment lines are skipped: the module documents WHY it is the wrong
        basis, and that prose must not read as a call site."""
        import os
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        hits = []
        for name in sorted(os.listdir(root)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(root, name), encoding="utf-8",
                      errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    stripped = line.lstrip()
                    if stripped.startswith("#") or stripped.startswith("def "):
                        continue
                    if re.search(r"\bget_my_projects\s*\(", line):
                        hits.append(f"{name}:{lineno}")
        self.assertEqual(hits, [], f"get_my_projects gained a caller: {hits}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
