"""Tests for gitlab_mcp.actions create_group / create_project and their helpers.

Offline: a tiny fake context answers GET from a dict and raises
GitLabNotFoundError (what the real GitLabContext raises on a 404) for any
path it does not know, so ``_get_optional`` sees a missing object exactly as
it would against a live instance.
"""

from __future__ import annotations

import unittest

from gitlab_mcp import actions
from gitlab_mcp.context import GitLabNotFoundError

from tests import _home_guard  # noqa: E402  (isolates ~ incl. errors.ERROR_FILE)


def setUpModule():
    _home_guard.activate()


def tearDownModule():
    _home_guard.deactivate()


class FakeCtx:
    def __init__(self, responses=None, post_result=None):
        self._responses = responses or {}
        self._post_result = post_result
        self.gets = []
        self.posts = []

    def get(self, path, params=None):
        self.gets.append(path)
        if path not in self._responses:
            raise GitLabNotFoundError("404 Not Found", status=404)
        return self._responses[path]

    def post(self, path, json=None):
        self.posts.append((path, json))
        return self._post_result if self._post_result is not None else {"id": 99}


TEAM = {"id": 12, "full_path": "acme/team"}
USERNS = {"id": 5, "full_path": "shawn"}


# ─── helpers ─────────────────────────────────────────────────────────────────

class SlugFrom(unittest.TestCase):
    def test_spaces_and_case(self):
        self.assertEqual(actions._slug_from("My Team"), "my-team")

    def test_runs_of_punctuation_collapse(self):
        self.assertEqual(actions._slug_from("Foo  &  Bar!"), "foo-bar")

    def test_edges_stripped(self):
        self.assertEqual(actions._slug_from("  -.Hello.-  "), "hello")

    def test_keeps_dot_underscore(self):
        self.assertEqual(actions._slug_from("api_v2.core"), "api_v2.core")

    def test_persian_yields_empty(self):
        self.assertEqual(actions._slug_from("تیم بانک"), "")

    def test_mixed_persian_keeps_latin(self):
        self.assertEqual(actions._slug_from("تیم api"), "api")


class RequireSlug(unittest.TestCase):
    def test_valid(self):
        for s in ("a", "my-team", "_x", "v1.2", "A_b-C", "9lives"):
            self.assertEqual(actions._require_slug(s), s)

    def test_strips_whitespace(self):
        self.assertEqual(actions._require_slug("  ok  "), "ok")

    def test_invalid(self):
        bad = ("", " ", "-lead", ".lead", "trail-", "trail.", "has space",
               "repo.git", "REPO.GIT", "feed.atom", "a/b", "ü", "a\nb")
        for s in bad:
            with self.subTest(s=s), self.assertRaises(ValueError):
                actions._require_slug(s)

    def test_too_long(self):
        actions._require_slug("a" * actions.MAX_NAME_CHARS)
        with self.assertRaises(ValueError):
            actions._require_slug("a" * (actions.MAX_NAME_CHARS + 1))

    def test_non_string(self):
        for v in (None, 5, ["a"]):
            with self.subTest(v=v), self.assertRaises(ValueError):
                actions._require_slug(v)

    def test_field_name_in_message(self):
        with self.assertRaisesRegex(ValueError, "^namespace "):
            actions._require_slug("-x", "namespace")


class RequireVisibility(unittest.TestCase):
    def test_default_private(self):
        self.assertEqual(actions._require_visibility(None), "private")
        self.assertEqual(actions._require_visibility(""), "private")

    def test_normalized(self):
        self.assertEqual(actions._require_visibility(" PUBLIC "), "public")
        self.assertEqual(actions._require_visibility("Internal"), "internal")

    def test_invalid(self):
        for v in ("secret", "  ", "privat", "1"):
            with self.subTest(v=v), self.assertRaises(ValueError):
                actions._require_visibility(v)


# ─── create_group ────────────────────────────────────────────────────────────

class PreviewCreateGroup(unittest.TestCase):
    def test_top_level_derived_path(self):
        ctx = FakeCtx()
        p = actions.preview_create_group(ctx, "My Team")
        self.assertTrue(p["ok"])
        self.assertFalse(p["danger"])
        self.assertEqual(p["path"], "my-team")
        self.assertEqual(p["full_path"], "my-team")
        self.assertIsNone(p["parent"])
        self.assertIsNone(p["parent_id"])
        self.assertEqual(p["visibility"], "private")
        self.assertEqual(p["warnings"], [])
        self.assertIn("top-level group", p["summary"])
        self.assertEqual(ctx.gets, ["/groups/my-team"])
        self.assertEqual(ctx.posts, [], "a preview must never write")

    def test_name_stripped(self):
        p = actions.preview_create_group(FakeCtx(), "  Ops  ")
        self.assertEqual(p["name"], "Ops")

    def test_persian_name_without_path_refused(self):
        ctx = FakeCtx()
        with self.assertRaisesRegex(ValueError, "pass path explicitly"):
            actions.preview_create_group(ctx, "تیم بانک")
        self.assertEqual(ctx.gets, [])

    def test_persian_name_with_path(self):
        p = actions.preview_create_group(FakeCtx(), "تیم بانک", path="bank")
        self.assertEqual((p["name"], p["path"]), ("تیم بانک", "bank"))

    def test_blank_path_falls_back_to_name(self):
        p = actions.preview_create_group(FakeCtx(), "Core", path="   ")
        self.assertEqual(p["path"], "core")

    def test_empty_name_refused(self):
        for n in ("", "   ", None):
            with self.subTest(n=n), self.assertRaises(ValueError):
                actions.preview_create_group(FakeCtx(), n, path="x")

    def test_invalid_explicit_path(self):
        for s in ("x.git", "-x", "a b"):
            with self.subTest(s=s), self.assertRaises(ValueError):
                actions.preview_create_group(FakeCtx(), "Name", path=s)

    def test_name_deriving_dot_git_refused(self):
        with self.assertRaises(ValueError):
            actions.preview_create_group(FakeCtx(), "repo.git")

    def test_invalid_visibility(self):
        with self.assertRaises(ValueError):
            actions.preview_create_group(FakeCtx(), "x", visibility="world")

    def test_existing_group(self):
        ctx = FakeCtx({"/groups/my-team": {"id": 3}})
        p = actions.preview_create_group(ctx, "My Team")
        self.assertFalse(p["ok"])
        self.assertEqual(p["reasons"], ["group 'my-team' already exists"])

    def test_public_is_danger(self):
        p = actions.preview_create_group(FakeCtx(), "x", visibility="public")
        self.assertTrue(p["danger"])
        self.assertTrue(p["ok"])
        self.assertEqual(len(p["warnings"]), 1)
        self.assertIn("public", p["warnings"][0])

    def test_internal_not_danger(self):
        p = actions.preview_create_group(FakeCtx(), "x", visibility="internal")
        self.assertFalse(p["danger"])
        self.assertEqual(p["warnings"], [])

    def test_subgroup(self):
        ctx = FakeCtx({"/groups/acme%2Fteam": TEAM})
        p = actions.preview_create_group(ctx, "Backend", parent="/acme/team/")
        self.assertTrue(p["ok"])
        self.assertEqual(p["full_path"], "acme/team/backend")
        self.assertEqual((p["parent"], p["parent_id"]), ("acme/team", 12))
        self.assertIn("subgroup", p["summary"])
        self.assertEqual(ctx.gets, ["/groups/acme%2Fteam",
                                    "/groups/acme%2Fteam%2Fbackend"])

    def test_existing_subgroup(self):
        ctx = FakeCtx({"/groups/acme%2Fteam": TEAM,
                       "/groups/acme%2Fteam%2Fbackend": {"id": 8}})
        p = actions.preview_create_group(ctx, "Backend", parent="acme/team")
        self.assertFalse(p["ok"])
        self.assertIn("acme/team/backend", p["reasons"][0])

    def test_parent_not_found_404(self):
        with self.assertRaises(GitLabNotFoundError):
            actions.preview_create_group(FakeCtx(), "x", parent="nope")

    def test_parent_without_id(self):
        ctx = FakeCtx({"/groups/nope": {}})
        with self.assertRaisesRegex(ValueError, "parent 'nope' not found"):
            actions.preview_create_group(ctx, "x", parent="nope")


class DoCreateGroup(unittest.TestCase):
    def test_top_level_body(self):
        ctx = FakeCtx(post_result={"id": 4, "full_path": "my-team",
                                   "visibility": "private",
                                   "web_url": "https://g/my-team"})
        r = actions.do_create_group(ctx, " My Team ")
        self.assertEqual(ctx.posts, [("/groups", {"name": "My Team",
                                                  "path": "my-team",
                                                  "visibility": "private"})])
        self.assertEqual(r, {"action": "create_group", "ok": True, "id": 4,
                             "full_path": "my-team", "visibility": "private",
                             "web_url": "https://g/my-team"})

    def test_subgroup_sends_parent_id(self):
        ctx = FakeCtx({"/groups/acme%2Fteam": TEAM})
        actions.do_create_group(ctx, "Backend", parent="acme/team",
                                visibility="Internal")
        self.assertEqual(ctx.posts[0][1], {"name": "Backend",
                                           "path": "backend",
                                           "visibility": "internal",
                                           "parent_id": 12})

    def test_parent_missing_never_posts(self):
        ctx = FakeCtx()
        with self.assertRaises(GitLabNotFoundError):
            actions.do_create_group(ctx, "x", parent="nope")
        self.assertEqual(ctx.posts, [])

    def test_validation_before_post(self):
        for kw in ({"name": "تیم"}, {"name": "x", "path": "x.git"},
                   {"name": "x", "visibility": "nope"}):
            ctx = FakeCtx()
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                actions.do_create_group(ctx, **kw)
            self.assertEqual(ctx.posts, [])

    def test_empty_response_falls_back(self):
        ctx = FakeCtx(post_result={})
        r = actions.do_create_group(ctx, "x", visibility="public")
        self.assertEqual((r["id"], r["full_path"], r["visibility"]),
                         (None, "x", "public"))


# ─── create_project ──────────────────────────────────────────────────────────

class PreviewCreateProject(unittest.TestCase):
    def test_personal_namespace(self):
        ctx = FakeCtx()
        p = actions.preview_create_project(ctx, "My Repo")
        self.assertTrue(p["ok"])
        self.assertEqual(p["full_path"], "<your personal namespace>/my-repo")
        self.assertIsNone(p["namespace"])
        self.assertIsNone(p["namespace_id"])
        self.assertFalse(p["initialize_with_readme"])
        self.assertIn("(empty)", p["summary"])
        self.assertEqual(ctx.gets, [], "no lookup without a namespace")
        self.assertEqual(ctx.posts, [])

    def test_in_namespace(self):
        ctx = FakeCtx({"/namespaces/acme%2Fteam": TEAM})
        p = actions.preview_create_project(ctx, "Api", namespace="acme/team",
                                           initialize_with_readme=True,
                                           description="d")
        self.assertTrue(p["ok"])
        self.assertEqual(p["full_path"], "acme/team/api")
        self.assertEqual((p["namespace"], p["namespace_id"]), ("acme/team", 12))
        self.assertTrue(p["initialize_with_readme"])
        self.assertEqual(p["description"], "d")
        self.assertIn("with a README", p["summary"])
        self.assertEqual(ctx.gets, ["/namespaces/acme%2Fteam",
                                    "/projects/acme%2Fteam%2Fapi"])
        self.assertEqual(ctx.posts, [])

    def test_existing_project(self):
        ctx = FakeCtx({"/namespaces/acme%2Fteam": TEAM,
                       "/projects/acme%2Fteam%2Fapi": {"id": 77}})
        p = actions.preview_create_project(ctx, "Api", namespace="acme/team")
        self.assertFalse(p["ok"])
        self.assertEqual(p["reasons"], ["project 'acme/team/api' already exists"])

    def test_namespace_not_found(self):
        with self.assertRaises(GitLabNotFoundError):
            actions.preview_create_project(FakeCtx(), "x", namespace="ghost")
        ctx = FakeCtx({"/namespaces/ghost": None})
        with self.assertRaisesRegex(ValueError, "namespace 'ghost' not found"):
            actions.preview_create_project(ctx, "x", namespace="ghost")

    def test_public_is_danger(self):
        p = actions.preview_create_project(FakeCtx(), "x", visibility="PUBLIC")
        self.assertTrue(p["danger"])
        self.assertEqual(p["visibility"], "public")
        self.assertEqual(len(p["warnings"]), 1)

    def test_persian_name_requires_path(self):
        with self.assertRaises(ValueError):
            actions.preview_create_project(FakeCtx(), "پروژه")
        p = actions.preview_create_project(FakeCtx(), "پروژه", path="proj")
        self.assertEqual(p["path"], "proj")

    def test_invalid_path_and_visibility(self):
        for kw in ({"path": "-x"}, {"path": "x y"}, {"path": "x.atom"},
                   {"visibility": "everyone"}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                actions.preview_create_project(FakeCtx(), "x", **kw)

    def test_description_too_long(self):
        with self.assertRaises(ValueError):
            actions.preview_create_project(
                FakeCtx(), "x", description="a" * (actions.MAX_BODY_CHARS + 1))


class DoCreateProject(unittest.TestCase):
    def test_personal_namespace_body(self):
        ctx = FakeCtx(post_result={"id": 9, "path_with_namespace": "shawn/my-repo",
                                   "visibility": "private",
                                   "web_url": "w", "http_url_to_repo": "h",
                                   "ssh_url_to_repo": "s"})
        r = actions.do_create_project(ctx, "My Repo")
        self.assertEqual(ctx.posts, [("/projects", {
            "name": "My Repo", "path": "my-repo", "visibility": "private",
            "initialize_with_readme": False})])
        self.assertEqual(ctx.gets, [])
        self.assertEqual(r, {"action": "create_project", "ok": True, "id": 9,
                             "full_path": "shawn/my-repo",
                             "visibility": "private", "web_url": "w",
                             "http_url_to_repo": "h", "ssh_url_to_repo": "s"})

    def test_namespace_id_readme_description(self):
        ctx = FakeCtx({"/namespaces/shawn": USERNS})
        actions.do_create_project(ctx, "Api", namespace="shawn", path="api2",
                                  visibility="internal", description="desc",
                                  initialize_with_readme=1)
        self.assertEqual(ctx.posts[0][1], {
            "name": "Api", "path": "api2", "visibility": "internal",
            "initialize_with_readme": True, "description": "desc",
            "namespace_id": 5})

    def test_namespace_missing_never_posts(self):
        ctx = FakeCtx()
        with self.assertRaises(GitLabNotFoundError):
            actions.do_create_project(ctx, "x", namespace="ghost")
        self.assertEqual(ctx.posts, [])

    def test_validation_before_post(self):
        for kw in ({"name": "پروژه"}, {"name": "x", "path": "x.git"},
                   {"name": "x", "visibility": "nope"}):
            ctx = FakeCtx()
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                actions.do_create_project(ctx, **kw)
            self.assertEqual(ctx.posts, [])


if __name__ == "__main__":
    unittest.main()


# ─── review fixes (1.3.0) ────────────────────────────────────────────────────

class ReviewFixes(unittest.TestCase):
    def test_slug_folds_accents(self):
        self.assertEqual(actions._slug_from("Café Team"), "cafe-team")

    def test_slug_collapses_special_runs(self):
        self.assertEqual(actions._slug_from("a - b"), "a-b")
        self.assertNotRegex(actions._slug_from("a - b . c"), r"[-_.]{2,}")

    def test_dot_segments_refused_before_any_request(self):
        for ref in ("..", "a/../b", "./a", "a//b"):
            ctx = FakeCtx()
            with self.assertRaises(ValueError, msg=ref):
                actions.preview_create_group(ctx, "x", parent=ref)
            self.assertEqual(ctx.gets, [], ref)

    def test_existence_probe_propagates_non_404(self):
        from gitlab_mcp.context import GitLabError

        class Denied(FakeCtx):
            def get(self, path, params=None):
                raise GitLabError("403 Forbidden")

        with self.assertRaises(GitLabError):
            actions.preview_create_group(Denied(), "x")

    def test_pinned_parent_id_skips_lookup(self):
        ctx = FakeCtx()
        actions.do_create_group(ctx, "Sub", parent="acme/team", parent_id=12)
        self.assertEqual(ctx.gets, [])
        self.assertEqual(ctx.posts[0][1]["parent_id"], 12)

    def test_pinned_namespace_id_skips_lookup(self):
        ctx = FakeCtx()
        actions.do_create_project(ctx, "api", namespace="acme/team",
                                  namespace_id=12)
        self.assertEqual(ctx.gets, [])
        self.assertEqual(ctx.posts[0][1]["namespace_id"], 12)


class CliPublicCreateAlwaysAsks(unittest.TestCase):
    def test_yes_does_not_skip_public_create(self):
        from unittest import mock
        from gitlab_mcp import cli
        with mock.patch.object(cli, "_ask", return_value="n") as ask:
            self.assertFalse(cli.confirm_write(
                "create-project", {"danger": True}, assume_yes=True))
        ask.assert_called_once()

    def test_pins_registered(self):
        from gitlab_mcp import cli
        self.assertEqual(cli._PINNED_FROM_PREVIEW["create-group"](
            {"parent_id": 3}), {"parent_id": 3})
        self.assertEqual(cli._PINNED_FROM_PREVIEW["create-project"]({}), {})
