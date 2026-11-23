"""Offline contract checks for migration inventory and access provenance."""
import unittest
from unittest.mock import patch

import _error_log_guard  # noqa: F401 - isolate the live error log

import api
from api import ApiError, PagedResult
from gitlab_mcp import migration
from gitlab_mcp.context import GitLabNotFoundError, GitLabPermissionError

TOKEN = "synthetic-secret-value"


class FakeCtx:
    def __init__(self, name, root="org"):
        self.name, self.url, self.root = name, "https://example.invalid", root
        self.calls = []
        self.direct = {}
        self.effective = {}
        self.shared = {}
        self.projects = {"org/sub/repo": 11}
        self.groups = {"org": 1, "org/sub": 2}
        self.project_changes = {}
        self.group_changes = {}
        self.shared_groups = {5: {"id": 5, "full_path": "outside/team", "parent_id": None,
                                  "shared_with_groups": []}}
        self.branches = None
        self.tags = None
        self.denied = set()
        self.missing = set()

    def cfg_for_git(self):
        return {"headers": {"PRIVATE-TOKEN": TOKEN}}

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if path in self.missing:
            raise GitLabNotFoundError("not found " + TOKEN, status=404, url=path)
        if path in self.denied:
            raise GitLabPermissionError("forbidden " + TOKEN, status=403, url=path)
        if path.startswith("/groups/") and path[8:].isdigit() and int(path[8:]) in self.shared_groups:
            return {**self.shared_groups[int(path[8:])], **self.group_changes}
        if path == "/groups/org":
            return {"id": 1, "full_path": "org", "parent_id": None, "shared_with_groups": []}
        if path == "/groups/org%2Fsub":
            return {"id": 2, "full_path": "org/sub", "parent_id": 1, "shared_with_groups": []}
        if path == "/groups/1":
            return {"id": 1, "full_path": "org", "parent_id": None, "shared_with_groups": []}
        if path == "/groups/2":
            return {"id": 2, "full_path": "org/sub", "parent_id": 1, "shared_with_groups": []}
        if path.startswith("/projects/"):
            n = int(path.split("/")[2])
            key = next(k for k, v in self.projects.items() if v == n)
            return {"id": n, "path_with_namespace": key, "default_branch": "main",
                    "archived": True, "visibility": "private", "empty_repo": False,
                    "shared_with_groups": self.shared.get(key, []), **self.project_changes}
        raise AssertionError(path)

    def pages(self, url, params):
        self.calls.append(("GET", url, params))
        path = url.split("/api/v4", 1)[1]
        if path == "/groups/1/descendant_groups":
            return [{"id": 2, "full_path": "org/sub", "parent_id": 1}]
        if path == "/groups/2/descendant_groups":
            return []
        if path in ("/groups/1/projects", "/groups/2/projects"):
            assert params.get("include_subgroups") is True
            assert params.get("with_shared") is False
            assert "archived" not in params
            return [{"id": n, "path_with_namespace": k} for k, n in self.projects.items()]
        if path in self.denied:
            raise ApiError(403, "forbidden", "https://example.invalid/api/v4" + path)
        if path.endswith("/repository/branches"):
            return self.branches or [{"name": "main", "commit": {"id": "abc"}}]
        if path.endswith("/repository/tags"):
            return self.tags or [{"name": "v1", "commit": {"id": "abc"}, "target": "abc",
                                  "message": "release"}]
        if path.endswith("/members/all"):
            return self.effective.get(path, [])
        if path.endswith("/members"):
            return self.direct.get(path, [])
        raise AssertionError(path)


def member(username="alice", role=30, expiry=None, state="active", id=7):
    return {"id": id, "username": username, "access_level": role,
            "expires_at": expiry, "state": state}


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.source, self.target = FakeCtx("source"), FakeCtx("target")
        self.contexts = (self.source, self.target)
        self.patch = patch.object(migration.api, "get_paginated", side_effect=self.paginate)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def paginate(self, url, headers, params=None, **kwargs):
        self.assertTrue(kwargs["strict"])
        self.assertIs(kwargs.get("quiet"), True)  # never print onto JSON stdout
        self.assertEqual(headers["PRIVATE-TOKEN"], TOKEN)
        ctx = self.source if "source.invalid" in url else self.target
        return PagedResult(ctx.pages(url, params), complete=True)

    def test_complete_repo_and_project_only_user(self):
        self.source.url, self.target.url = "https://source.invalid", "https://target.invalid"
        self.source.effective["/projects/11/members/all"] = [member()]
        self.target.effective["/projects/11/members/all"] = [member(id=77)]
        result = migration.audit(self.source, self.target, "org")
        self.assertEqual(result["repositories"][0]["status"], "identical")
        self.assertEqual(result["access"][0]["username"], "alice")
        self.assertEqual(result["access"][0]["source_origin"], "indeterminate")
        self.assertFalse(result["complete"])
        self.assertNotIn(TOKEN, repr(result))
        for ctx in self.contexts:
            self.assertTrue(all(method == "GET" and "?private_token" not in path
                                for method, path, _ in ctx.calls))

    def test_inherited_higher_role_outweighs_direct(self):
        self.source.url, self.target.url = "https://source.invalid", "https://target.invalid"
        for ctx in self.contexts:
            ctx.direct["/projects/11/members"] = [member(role=20)]
            ctx.direct["/groups/2/members"] = [member(role=40)]
            ctx.effective["/projects/11/members/all"] = [member(role=40)]
        result = migration.audit(self.source, self.target, "org")
        self.assertEqual(result["access"][0]["source_origin"], "inherited")
        self.assertTrue(result["complete"])

    def test_shared_cap_and_expiry_must_explain_effective_grant(self):
        self.source.url, self.target.url = "https://source.invalid", "https://target.invalid"
        for ctx in self.contexts:
            ctx.shared["org/sub/repo"] = [{"group_id": 5, "group_access_level": 20,
                                            "expires_at": "2027-01-01"}]
            ctx.effective["/groups/5/members/all"] = [member(role=40)]
            ctx.effective["/projects/11/members/all"] = [member(role=40)]
        result = migration.audit(self.source, self.target, "org")
        self.assertEqual(result["access"][0]["source_origin"], "indeterminate")
        self.assertFalse(result["complete"])

    def test_partial_page_aborts_without_result(self):
        self.source.url, self.target.url = "https://source.invalid", "https://target.invalid"
        original = self.paginate
        def partial(url, headers, params=None, **kwargs):
            if url.endswith("/projects/11/members/all"):
                return PagedResult([], complete=False)
            return original(url, headers, params, **kwargs)
        with patch.object(migration.api, "get_paginated", side_effect=partial):
            with self.assertRaises(migration.MigrationAuditError) as raised:
                migration.audit(self.source, self.target, "org")
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_members_all_permission_failure_aborts(self):
        self.source.url, self.target.url = "https://source.invalid", "https://target.invalid"
        def denied(url, headers, params=None, **kwargs):
            if url.endswith("/projects/11/members/all"):
                raise GitLabPermissionError("forbidden " + TOKEN)
            return self.paginate(url, headers, params, **kwargs)
        with patch.object(migration.api, "get_paginated", side_effect=denied):
            with self.assertRaises(Exception) as raised:
                migration.audit(self.source, self.target, "org")
        self.assertNotIn(TOKEN, str(raised.exception))

    def urls(self):
        self.source.url, self.target.url = "https://source.invalid", "https://target.invalid"

    def test_legacy_api_403_on_members_all_aborts(self):
        self.urls()
        self.source.denied.add("/projects/11/members/all")
        with self.assertRaises(migration.MigrationAuditError) as raised:
            migration.audit(self.source, self.target, "org")
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_shared_group_403_is_indeterminate_not_guessed(self):
        self.urls()
        for ctx in self.contexts:
            ctx.shared["org/sub/repo"] = [{"group_id": 5, "group_access_level": 40,
                                            "expires_at": None}]
            ctx.denied.add("/groups/5/members/all")
            ctx.effective["/projects/11/members/all"] = [member(role=40)]
        result = migration.audit(self.source, self.target, "org")
        self.assertEqual(result["access"][0]["source_origin"], "indeterminate")
        self.assertFalse(result["complete"])
        self.assertFalse(result["summary"]["complete"])

    def test_shared_grant_matches_only_with_cap_and_expiry(self):
        self.urls()
        for ctx in self.contexts:
            ctx.shared["org/sub/repo"] = [{"group_id": 5, "group_access_level": 30,
                                            "expires_at": "2027-01-01"}]
            ctx.effective["/groups/5/members/all"] = [member(role=50, expiry="2028-01-01")]
            ctx.effective["/projects/11/members/all"] = [member(role=30, expiry="2027-01-01")]
        result = migration.audit(self.source, self.target, "org")
        self.assertEqual(result["access"][0]["source_origin"], "shared")
        self.assertTrue(result["complete"])

    def test_ancestor_outside_nested_root_explains_inheritance(self):
        self.urls()
        for ctx in self.contexts:
            ctx.direct["/groups/1/members"] = [member(role=50)]
            ctx.effective["/projects/11/members/all"] = [member(role=50)]
        result = migration.audit(self.source, self.target, "org/sub")
        self.assertEqual(result["repositories"][0]["source_path"], "repo")
        self.assertEqual(result["access"][0]["source_origin"], "inherited")
        self.assertEqual(result["access"][0]["source_role"], "owner")
        self.assertTrue(result["complete"])

    def test_equal_direct_and_inherited_grants_are_both_reported(self):
        self.urls()
        for ctx in self.contexts:
            ctx.direct["/projects/11/members"] = [member(role=30)]
            ctx.direct["/groups/1/members"] = [member(role=30)]
            ctx.effective["/projects/11/members/all"] = [member(role=30)]
        result = migration.audit(self.source, self.target, "org")
        # Two grants that each exactly explain the row: provenance is known,
        # just redundant. Only *no* explanation is indeterminate.
        self.assertEqual(result["access"][0]["source_origin"], "direct+inherited")
        self.assertTrue(result["complete"])

    def test_direct_grant_with_matching_role_and_expiry(self):
        self.urls()
        for ctx in self.contexts:
            ctx.direct["/projects/11/members"] = [member(role=40, expiry="2027-02-02")]
            ctx.effective["/projects/11/members/all"] = [member(role=40, expiry="2027-02-02")]
        result = migration.audit(self.source, self.target, "org")
        self.assertEqual(result["access"][0]["source_origin"], "direct")
        self.assertEqual(result["access"][0]["status"], "identical")

    def test_blocked_user_and_role_difference_are_kept(self):
        self.urls()
        self.source.direct["/projects/11/members"] = [member(role=40, state="blocked")]
        self.source.effective["/projects/11/members/all"] = [member(role=40, state="blocked")]
        self.target.direct["/projects/11/members"] = [member(role=15)]
        self.target.effective["/projects/11/members/all"] = [member(role=15)]
        row = migration.audit(self.source, self.target, "org")["access"][0]
        self.assertEqual((row["source_state"], row["source_role"], row["target_role"]),
                         ("blocked", "maintainer", "planner"))
        self.assertEqual(row["status"], "different")

    def test_repo_missing_extra_and_different(self):
        self.urls()
        self.source.projects = {"org/sub/repo": 11, "org/old": 12}
        self.target.projects = {"org/sub/repo": 11, "org/new": 13}
        self.target.project_changes = {"default_branch": "trunk"}
        rows = {r["source_path"] or r["target_path"]: r
                for r in migration.audit(self.source, self.target, "org")["repositories"]}
        self.assertEqual(rows["old"]["status"], "missing")
        self.assertEqual(rows["new"]["status"], "extra")
        self.assertEqual(rows["sub/repo"]["status"], "different")
        self.assertIn("default_branch", rows["sub/repo"]["differences"])

    def test_missing_required_project_metadata_raises(self):
        self.urls()
        self.source.project_changes = {"empty_repo": None}
        with self.assertRaises(migration.MigrationAuditError):
            migration.audit(self.source, self.target, "org")

    def test_project_outside_root_raises(self):
        self.urls()
        self.source.projects = {"elsewhere/repo": 11}
        with self.assertRaises(migration.MigrationAuditError):
            migration.audit(self.source, self.target, "org")

    def test_grant_state_mismatch_is_indeterminate(self):
        self.urls()
        for ctx in self.contexts:
            ctx.direct["/projects/11/members"] = [member(state="blocked")]
            ctx.direct["/groups/2/members"] = [member(state="blocked")]
            ctx.effective["/projects/11/members/all"] = [member()]
        result = migration.audit(self.source, self.target, "org")
        self.assertEqual(result["access"][0]["source_origin"], "indeterminate")
        self.assertFalse(result["complete"])

    def test_shared_group_detail_404_is_indeterminate(self):
        self.urls()
        for ctx in self.contexts:
            ctx.shared["org/sub/repo"] = [{"group_id": 5, "group_access_level": 30,
                                           "expires_at": None}]
            ctx.missing.add("/groups/5")
            ctx.effective["/projects/11/members/all"] = [member()]
        result = migration.audit(self.source, self.target, "org")
        self.assertEqual(result["access"][0]["source_origin"], "indeterminate")
        self.assertFalse(result["complete"])

    def test_shared_group_inherited_member_explains_grant(self):
        self.urls()
        for ctx in self.contexts:
            ctx.shared["org/sub/repo"] = [{"group_id": 5, "group_access_level": 30,
                                           "expires_at": None}]
            ctx.effective["/groups/5/members/all"] = [member()]
            ctx.effective["/projects/11/members/all"] = [member()]
        result = migration.audit(self.source, self.target, "org")
        self.assertEqual(result["access"][0]["source_origin"], "shared")
        self.assertTrue(result["complete"])
        self.assertIn(("GET", "/groups/5", None), self.source.calls)

    def test_shared_state_mismatch_is_indeterminate(self):
        self.urls()
        for ctx in self.contexts:
            ctx.shared["org/sub/repo"] = [{"group_id": 5, "group_access_level": 30,
                                           "expires_at": None}]
            ctx.effective["/groups/5/members/all"] = [member(state="blocked")]
            ctx.effective["/projects/11/members/all"] = [member()]
        result = migration.audit(self.source, self.target, "org")
        self.assertEqual(result["access"][0]["source_origin"], "indeterminate")
        self.assertFalse(result["complete"])

    def test_shared_group_metadata_mismatch_fails_loudly(self):
        self.urls()
        self.source.shared["org/sub/repo"] = [{"group_id": 5, "group_access_level": 30,
                                                "expires_at": None}]
        self.source.effective["/projects/11/members/all"] = [member()]
        self.source.shared_groups[5]["id"] = 6
        with self.assertRaises(migration.MigrationAuditError):
            migration.audit(self.source, self.target, "org")

    def test_shared_group_missing_detail_metadata_fails_loudly(self):
        self.urls()
        self.source.shared["org/sub/repo"] = [{"group_id": 5, "group_access_level": 30,
                                                "expires_at": None}]
        self.source.effective["/projects/11/members/all"] = [member()]
        del self.source.shared_groups[5]["parent_id"]
        with self.assertRaises(migration.MigrationAuditError):
            migration.audit(self.source, self.target, "org")

    def test_shared_group_expansion_limit_fails_not_truncates(self):
        self.urls()
        self.source.shared["org/sub/repo"] = [
            {"group_id": i, "group_access_level": 30, "expires_at": None}
            for i in range(100, 170)]
        self.source.effective["/projects/11/members/all"] = [member()]
        for i in range(100, 170):
            self.source.shared_groups[i] = {"id": i, "full_path": f"outside/team-{i}",
                                            "parent_id": None, "shared_with_groups": []}
        with self.assertRaisesRegex(migration.MigrationAuditError, "limit"):
            migration.audit(self.source, self.target, "org")

    def test_noncanonical_expiry_rejected(self):
        self.urls()
        for expiry in ("2027-2-01", "2027-02-30", "", "2027-01-01T00:00:00"):
            with self.subTest(expiry=expiry):
                self.source.effective["/projects/11/members/all"] = [member(expiry=expiry)]
                with self.assertRaises(migration.MigrationAuditError):
                    migration.audit(self.source, self.target, "org")

    def test_tag_target_and_message_only_differences(self):
        self.urls()
        for key, value in (("target", "other"), ("message", "changed")):
            with self.subTest(key=key):
                self.source.tags = [{"name": "v1", "commit": {"id": "abc"},
                                     "target": "abc", "message": "release"}]
                self.target.tags = [{**self.source.tags[0], key: value}]
                row = migration.audit(self.source, self.target, "org")["repositories"][0]
                self.assertEqual(row["status"], "different")
                self.assertIn("tags", row["differences"])

    def test_same_basename_different_full_paths_are_not_matched(self):
        self.urls()
        self.source.projects = {"org/sub/repo": 11}
        self.target.projects = {"org/repo": 12}
        rows = migration.audit(self.source, self.target, "org")["repositories"]
        self.assertEqual([(r["source_path"], r["target_path"], r["status"]) for r in rows],
                         [(None, "repo", "extra"), ("sub/repo", None, "missing")])

    def test_expired_members_retained(self):
        self.urls()
        for ctx in self.contexts:
            ctx.direct["/projects/11/members"] = [member(expiry="2020-01-01")]
            ctx.effective["/projects/11/members/all"] = [member(expiry="2020-01-01")]
        result = migration.audit(self.source, self.target, "org")
        self.assertEqual(result["access"][0]["source_expires_at"], "2020-01-01")
        self.assertEqual(result["access"][0]["source_origin"], "direct")

    def test_legacy_lister_cycle_is_bounded_and_raises(self):
        class Response:
            status_code = 200
            headers = {"X-Next-Page": "1"}
            def json(self):
                return [{"id": 1}]
        self.patch.stop()  # exercise the real legacy lister, not the fake
        calls = []
        def fake_request(url, headers, params, quiet=False, record=True):
            calls.append(dict(params))
            return Response()
        with patch.object(api, "request", side_effect=fake_request), \
                patch.object(api, "record_failure"):
            with self.assertRaises(ApiError):
                api.get_paginated("https://x.invalid/api/v4/groups/1/members",
                                  {}, strict=True, max_pages=200)
        self.assertEqual(len(calls), 200)


if __name__ == "__main__":
    unittest.main()
