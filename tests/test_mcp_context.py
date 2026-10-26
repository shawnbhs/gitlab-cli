"""Offline tests for ``gitlab_mcp.context``.

Nothing here touches a network, a real ``~/.gitlab-cli`` or a terminal: the
instance registry is patched to in-memory records and every HTTP call goes
through a fake session that returns canned responses.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gitlab_mcp import context as ctxmod           # noqa: E402

from tests import _home_guard                       # noqa: E402


def setUpModule() -> None:
    """Point HOME and every gitlab-cli config path at a temporary directory."""
    _home_guard.activate()


def tearDownModule() -> None:
    """Restore HOME; fail if any test reached into the real home."""
    _home_guard.deactivate()

TOKEN = "glpat-AAAABBBBCCCCDDDDEEEE"  # gitleaks:allow - fake test fixture
OTHER_TOKEN = "glpat-ZZZZYYYYXXXXWWWWVVVV"  # gitleaks:allow - fake test fixture

INSTANCES = [
    {"name": "example", "url": "https://gitlab.example.com/",
     "master_dir": "/srv/MasterGroups", "folder": "example",
     "token_env": "GITLAB_TOKEN_EXAMPLE"},
    {"name": "second", "url": "https://gitlab2.example.com",
     "master_dir": "/srv/Second", "folder": "second",
     "token_env": "GITLAB_TOKEN_SECOND"},
]


# ─── Fakes ────────────────────────────────────────────────────────────────────

class FakeResponse:
    """The slice of ``requests.Response`` the context actually reads."""

    def __init__(self, status_code: int = 200, json_body=None,
                 headers: dict | None = None, text: str = "",
                 content: bytes | None = None) -> None:
        self.status_code = status_code
        self._json = json_body
        self.headers = headers or {}
        self.text = text
        if content is not None:
            self.content = content
        elif json_body is not None:
            self.content = b"{}"
        else:
            self.content = text.encode() if text else b""

    def json(self):
        if self._json is None:
            raise ValueError("no JSON body")
        return self._json


class FakeSession:
    """Records requests and replays a scripted list of responses."""

    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self.headers: dict = {}
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def request(self, method, url, params=None, json=None, timeout=None,
                verify=None):
        self.calls.append({"method": method, "url": url,
                           "params": dict(params) if isinstance(params, dict)
                           else params,
                           "json": json, "timeout": timeout,
                           "verify": verify})
        if not self.responses:
            raise AssertionError(f"no scripted response for {method} {url}")
        res = self.responses.pop(0)
        if isinstance(res, Exception):
            raise res
        return res


def build_cfg(inst: dict, allow_generic: bool = False) -> dict:
    """``instances.build_cfg`` with a fixed token and no file access."""
    token = (OTHER_TOKEN if inst["name"] == "second" else TOKEN)
    return {
        "token": token,
        "token_env": inst["token_env"],
        "url": inst["url"].rstrip("/"),
        "master_dir": inst["master_dir"],
        "base_dir": inst["master_dir"],
        "folder": inst["folder"],
        "name": inst["name"],
        "min_access": 0,
        "headers": {"PRIVATE-TOKEN": token},
    }


def make_context(instance: str | None = None, active: str = "example",
                 responses: list | None = None):
    """A ``GitLabContext`` on fake records, with its session replaced."""
    with mock.patch.object(ctxmod.gl_instances, "list_instances",
                           return_value=INSTANCES), \
         mock.patch.object(ctxmod.gl_instances, "build_cfg",
                           side_effect=build_cfg), \
         mock.patch.object(ctxmod.gl_instances, "_load",
                           return_value={"instances": INSTANCES,
                                         "active": active}), \
         mock.patch.object(
             ctxmod.gl_instances, "get_active_instance",
             side_effect=lambda: next(
                 (build_cfg(i) for i in INSTANCES if i["name"] == active),
                 None)):
        ctx = ctxmod.GitLabContext(instance)
    session = FakeSession(responses)
    # Name-mangled private attribute: the test reaches in deliberately,
    # because the point of the design is that nothing else can.
    ctx._GitLabContext__session = session
    ctx.session = session
    return ctx


# ─── redact ───────────────────────────────────────────────────────────────────

class TestRedact(unittest.TestCase):

    def test_masks_a_gitlab_personal_access_token(self):
        out = ctxmod.redact(f"fatal: auth failed with {TOKEN}")
        self.assertNotIn(TOKEN, out)
        self.assertIn("[REDACTED-TOKEN]", out)

    def test_masks_oauth2_userinfo_but_keeps_the_username(self):
        out = ctxmod.redact(
            f"remote: https://oauth2:{TOKEN}@gitlab.example.com/a/b.git")
        self.assertNotIn(TOKEN, out)
        self.assertIn("oauth2:[REDACTED", out)
        self.assertIn("@gitlab.example.com/a/b.git", out)

    def test_masks_a_user_password_pair_in_a_url(self):
        out = ctxmod.redact("https://alice:s3cr3t-pass@gitlab.example.com/x")
        self.assertNotIn("s3cr3t-pass", out)
        self.assertIn("alice:[REDACTED]@", out)

    def test_masks_bare_userinfo(self):
        out = ctxmod.redact(
            "https://verysecretvalue1234@gitlab.example.com/x")
        self.assertNotIn("verysecretvalue1234", out)
        self.assertIn("[REDACTED-USERINFO]@gitlab.example.com", out)

    def test_masks_a_private_token_header_value(self):
        out = ctxmod.redact(f"> PRIVATE-TOKEN: {TOKEN}")
        self.assertNotIn(TOKEN, out)
        self.assertIn("PRIVATE-TOKEN: [REDACTED", out)

    def test_masks_a_private_token_header_whose_value_is_not_token_shaped(self):
        out = ctxmod.redact("> PRIVATE-TOKEN: plainlookingvalue")
        self.assertNotIn("plainlookingvalue", out)
        self.assertIn("PRIVATE-TOKEN: [REDACTED]", out)

    def test_masks_an_authorization_bearer_value_keeping_the_scheme(self):
        out = ctxmod.redact("Authorization: Bearer abcdefghijklmnopqrstuvwx")
        self.assertNotIn("abcdefghijklmnopqrstuvwx", out)
        self.assertIn("Bearer [REDACTED]", out)

    def test_masks_a_token_query_parameter(self):
        out = ctxmod.redact(
            "GET /api/v4/projects?private_token=abcdef123456 failed")  # gitleaks:allow - fake test fixture
        self.assertNotIn("abcdef123456", out)

    def test_masks_an_env_style_assignment(self):
        out = ctxmod.redact("GITLAB_TOKEN=notatokenshape12345")
        self.assertNotIn("notatokenshape12345", out)

    def test_masks_a_literal_secret_that_matches_no_pattern(self):
        out = ctxmod.redact("value is plainsecret", secrets=("plainsecret",))
        self.assertEqual("value is [REDACTED]", out)

    def test_leaves_ordinary_text_alone(self):
        text = "cloned 12 repositories into /srv/MasterGroups"
        self.assertEqual(text, ctxmod.redact(text))

    def test_empty_input_passes_through(self):
        self.assertEqual("", ctxmod.redact(""))


# ─── Instance selection ───────────────────────────────────────────────────────

class TestInstanceSelection(unittest.TestCase):

    def test_none_selects_the_active_instance(self):
        ctx = make_context(None, active="second")
        self.assertEqual("second", ctx.name)
        self.assertEqual("https://gitlab2.example.com", ctx.url)

    def test_a_named_instance_is_selected(self):
        ctx = make_context("example")
        self.assertEqual("example", ctx.name)

    def test_the_url_loses_its_trailing_slash(self):
        self.assertEqual("https://gitlab.example.com",
                         make_context("example").url)

    def test_unknown_instance_raises_valueerror_listing_the_names(self):
        with self.assertRaises(ValueError) as caught:
            make_context("nope")
        message = str(caught.exception)
        self.assertIn("nope", message)
        self.assertIn("example", message)
        self.assertIn("second", message)

    def test_no_active_instance_raises_valueerror(self):
        with self.assertRaises(ValueError) as caught:
            make_context(None, active="")
        self.assertIn("no active GitLab instance", str(caught.exception))

    def test_master_dir_is_absolute(self):
        ctx = make_context("example")
        self.assertTrue(os.path.isabs(ctx.master_dir), ctx.master_dir)

    def test_a_relative_master_dir_is_anchored_not_cwd_relative(self):
        inst = dict(INSTANCES[0], master_dir="MasterGroups")
        with mock.patch.object(ctxmod.gl_instances, "list_instances",
                               return_value=[inst]), \
             mock.patch.object(ctxmod.gl_instances, "build_cfg",
                               side_effect=build_cfg):
            # Run from a foreign cwd: inside the install dir the anchored and
            # the cwd-relative path coincide and the assertion proves nothing.
            here = os.getcwd()
            foreign = tempfile.mkdtemp(prefix="gitlab-mcp-cwd-")
            try:
                os.chdir(foreign)
                ctx = ctxmod.GitLabContext("example")
                cwd_relative = os.path.abspath("MasterGroups")
            finally:
                os.chdir(here)
                os.rmdir(foreign)
        self.assertTrue(os.path.isabs(ctx.master_dir))
        self.assertNotEqual(cwd_relative, ctx.master_dir)

    def test_list_instances_public_has_no_tokens(self):
        with mock.patch.object(ctxmod.gl_instances, "list_instances",
                               return_value=INSTANCES), \
             mock.patch.object(ctxmod.gl_instances, "_load",
                               return_value={"instances": INSTANCES,
                                             "active": "second"}):
            rows = ctxmod.list_instances_public()
        self.assertEqual([{"name": "example",
                           "url": "https://gitlab.example.com",
                           "active": False},
                          {"name": "second",
                           "url": "https://gitlab2.example.com",
                           "active": True}], rows)
        self.assertNotIn("token", repr(rows))


# ─── Secrecy of the token ─────────────────────────────────────────────────────

class TestTokenIsPrivate(unittest.TestCase):

    def setUp(self):
        self.ctx = make_context("example")

    def test_repr_has_no_token(self):
        self.assertNotIn(TOKEN, repr(self.ctx))
        self.assertIn("GitLabContext", repr(self.ctx))

    def test_str_has_no_token(self):
        self.assertNotIn(TOKEN, str(self.ctx))

    def test_no_public_attribute_exposes_the_token(self):
        for name in dir(self.ctx):
            if name.startswith("_"):
                continue
            value = getattr(self.ctx, name)
            self.assertNotIn(TOKEN, repr(value), name)

    def test_vars_has_no_token(self):
        self.assertNotIn(TOKEN, repr(vars(self.ctx)["_GitLabContext__cfg_public"]))

    def test_cfg_public_has_neither_token_nor_headers(self):
        cfg = self.ctx.cfg_public()
        self.assertNotIn("token", cfg)
        self.assertNotIn("headers", cfg)
        self.assertEqual("example", cfg["name"])
        self.assertNotIn(TOKEN, repr(cfg))

    def test_cfg_public_returns_a_copy(self):
        self.ctx.cfg_public()["name"] = "tampered"
        self.assertEqual("example", self.ctx.cfg_public()["name"])

    def test_cfg_for_git_carries_the_header_clone_needs(self):
        cfg = self.ctx.cfg_for_git()
        self.assertEqual(TOKEN, cfg["token"])
        self.assertEqual({"PRIVATE-TOKEN": TOKEN}, cfg["headers"])
        self.assertEqual(self.ctx.master_dir, cfg["master_dir"])

    def test_the_session_carries_the_private_token_header(self):
        fresh = make_context("example")
        # The real session, before the test replaced it, is what matters; build
        # one without replacing it.
        with mock.patch.object(ctxmod.gl_instances, "list_instances",
                               return_value=INSTANCES), \
             mock.patch.object(ctxmod.gl_instances, "build_cfg",
                               side_effect=build_cfg):
            real = ctxmod.GitLabContext("example")
        session = real._GitLabContext__session
        self.assertEqual(TOKEN, session.headers["PRIVATE-TOKEN"])
        self.assertEqual("example", fresh.name)

    def test_git_env_blocks_prompts_and_carries_no_token(self):
        env = self.ctx.git_env()
        self.assertEqual("0", env["GIT_TERMINAL_PROMPT"])
        self.assertNotIn(TOKEN, repr(env))

    def test_context_redact_removes_its_own_token(self):
        self.assertNotIn(TOKEN, self.ctx.redact(f"used {TOKEN} ok"))


# ─── Requests ─────────────────────────────────────────────────────────────────

class TestRequests(unittest.TestCase):

    def test_get_parses_json_and_builds_the_api_url(self):
        ctx = make_context("example",
                           responses=[FakeResponse(200, {"id": 7})])
        self.assertEqual({"id": 7}, ctx.get("/projects/7"))
        call = ctx.session.calls[0]
        self.assertEqual("https://gitlab.example.com/api/v4/projects/7",
                         call["url"])
        self.assertEqual("GET", call["method"])

    def test_timeout_is_twenty_seconds_and_tls_is_verified(self):
        ctx = make_context("example", responses=[FakeResponse(200, {})])
        ctx.get("/user")
        self.assertEqual(20, ctx.session.calls[0]["timeout"])
        self.assertIs(True, ctx.session.calls[0]["verify"])
        self.assertEqual(20, ctxmod.TIMEOUT)

    def test_a_path_already_carrying_the_api_prefix_is_not_doubled(self):
        ctx = make_context("example", responses=[FakeResponse(200, {})])
        ctx.get("/api/v4/projects")
        self.assertEqual("https://gitlab.example.com/api/v4/projects",
                         ctx.session.calls[0]["url"])

    def test_a_path_without_a_leading_slash_still_works(self):
        ctx = make_context("example", responses=[FakeResponse(200, {})])
        ctx.get("projects")
        self.assertEqual("https://gitlab.example.com/api/v4/projects",
                         ctx.session.calls[0]["url"])

    def test_an_absolute_url_is_refused(self):
        ctx = make_context("example")
        with self.assertRaises(ValueError):
            ctx.get("https://evil.example.com/api/v4/user")

    def test_post_put_delete_send_their_method(self):
        ctx = make_context("example",
                           responses=[FakeResponse(201, {"id": 1}),
                                      FakeResponse(200, {"id": 1}),
                                      FakeResponse(204)])
        self.assertEqual({"id": 1}, ctx.post("/x", {"a": 1}))
        self.assertEqual({"id": 1}, ctx.put("/x", {"a": 2}))
        self.assertIsNone(ctx.delete("/x"))
        self.assertEqual(["POST", "PUT", "DELETE"],
                         [c["method"] for c in ctx.session.calls])

    def test_a_204_returns_none(self):
        ctx = make_context("example", responses=[FakeResponse(204)])
        self.assertIsNone(ctx.get("/nothing"))

    def test_401_raises_an_auth_error(self):
        ctx = make_context("example",
                           responses=[FakeResponse(401, {"message": "401"})])
        with self.assertRaises(ctxmod.GitLabAuthError) as caught:
            ctx.get("/user")
        self.assertEqual(401, caught.exception.status)

    def test_403_404_429_map_to_their_own_subclasses(self):
        for status, cls in ((403, ctxmod.GitLabPermissionError),
                            (404, ctxmod.GitLabNotFoundError),
                            (429, ctxmod.GitLabRateLimitError)):
            ctx = make_context(
                "example", responses=[FakeResponse(status, {"message": "x"})])
            with self.subTest(status=status):
                with self.assertRaises(cls):
                    ctx.get("/user")

    def test_another_status_raises_the_generic_http_error(self):
        ctx = make_context("example",
                           responses=[FakeResponse(500, None, text="boom")])
        with self.assertRaises(ctxmod.GitLabHTTPError) as caught:
            ctx.get("/user")
        self.assertEqual(500, caught.exception.status)
        self.assertIn("boom", str(caught.exception))

    def test_every_error_is_a_gitlaberror(self):
        for cls in (ctxmod.GitLabAuthError, ctxmod.GitLabPermissionError,
                    ctxmod.GitLabNotFoundError, ctxmod.GitLabRateLimitError,
                    ctxmod.GitLabHTTPError, ctxmod.GitLabTransportError,
                    ctxmod.GitLabReadOnlyError):
            self.assertTrue(issubclass(cls, ctxmod.GitLabError), cls.__name__)

    def test_an_error_message_is_redacted(self):
        ctx = make_context(
            "example",
            responses=[FakeResponse(403, {"message":
                                          f"denied for {TOKEN}"})])
        with self.assertRaises(ctxmod.GitLabError) as caught:
            ctx.get("/user")
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertNotIn(TOKEN, repr(caught.exception))
        self.assertNotIn(TOKEN, caught.exception.message)

    def test_a_transport_failure_raises_a_transport_error(self):
        import requests
        ctx = make_context("example")
        ctx.session.responses = [requests.ConnectionError(
            f"https://oauth2:{TOKEN}@gitlab.example.com refused")]
        with self.assertRaises(ctxmod.GitLabTransportError) as caught:
            ctx.get("/user")
        self.assertIsNone(caught.exception.status)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_a_non_json_body_on_success_raises(self):
        ctx = make_context("example",
                           responses=[FakeResponse(200, None, text="<html>")])
        with self.assertRaises(ctxmod.GitLabHTTPError):
            ctx.get("/user")

    def test_read_only_blocks_writes_but_not_reads(self):
        ctx = make_context("example", responses=[FakeResponse(200, {})])
        with mock.patch.dict(os.environ,
                             {ctxmod.READ_ONLY_ENV: "1"}, clear=False):
            self.assertTrue(ctxmod.read_only())
            with self.assertRaises(ctxmod.GitLabReadOnlyError):
                ctx.post("/x", {})
            with self.assertRaises(ctxmod.GitLabReadOnlyError):
                ctx.put("/x", {})
            with self.assertRaises(ctxmod.GitLabReadOnlyError):
                ctx.delete("/x")
            self.assertEqual({}, ctx.get("/user"))
        self.assertEqual(1, len(ctx.session.calls))


# ─── Pagination ───────────────────────────────────────────────────────────────

def page(items, next_page=None, total=None):
    headers = {}
    if next_page is not None:
        headers["X-Next-Page"] = str(next_page)
    if total is not None:
        headers["X-Total"] = str(total)
    return FakeResponse(200, items, headers=headers)


class TestPaginate(unittest.TestCase):

    def test_follows_x_next_page_until_it_is_empty(self):
        ctx = make_context("example", responses=[
            page([{"id": 1}], next_page=2, total=3),
            page([{"id": 2}], next_page=3),
            page([{"id": 3}], next_page=""),
        ])
        items, truncated, total = ctx.paginate("/projects")
        self.assertEqual([1, 2, 3], [i["id"] for i in items])
        self.assertFalse(truncated)
        self.assertEqual(3, total)
        self.assertEqual([1, 2, 3],
                         [c["params"]["page"] for c in ctx.session.calls])

    def test_default_per_page_is_a_hundred(self):
        ctx = make_context("example", responses=[page([])])
        ctx.paginate("/projects")
        self.assertEqual(100, ctx.session.calls[0]["params"]["per_page"])

    def test_caller_params_are_preserved(self):
        ctx = make_context("example", responses=[page([])])
        ctx.paginate("/projects", {"search": "project-a", "per_page": 5})
        params = ctx.session.calls[0]["params"]
        self.assertEqual("project-a", params["search"])
        self.assertEqual(5, params["per_page"])

    def test_total_is_none_when_the_server_sends_no_x_total(self):
        ctx = make_context("example", responses=[page([{"id": 1}])])
        _, _, total = ctx.paginate("/projects")
        self.assertIsNone(total)

    def test_max_items_truncates_and_reports_it(self):
        ctx = make_context("example", responses=[
            page([{"id": 1}, {"id": 2}, {"id": 3}], next_page=2, total=9),
        ])
        items, truncated, total = ctx.paginate("/projects", max_items=2)
        self.assertEqual([1, 2], [i["id"] for i in items])
        self.assertTrue(truncated)
        self.assertEqual(9, total)
        self.assertEqual(1, len(ctx.session.calls),
                         "must stop fetching once the ceiling is reached")

    def test_exactly_max_items_with_nothing_left_is_not_truncated(self):
        ctx = make_context("example", responses=[
            page([{"id": 1}, {"id": 2}], next_page=""),
        ])
        items, truncated, _ = ctx.paginate("/projects", max_items=2)
        self.assertEqual(2, len(items))
        self.assertFalse(truncated)

    def test_the_limit_alias_behaves_like_max_items(self):
        ctx = make_context("example", responses=[
            page([{"id": 1}, {"id": 2}], next_page=2),
        ])
        items, truncated, _ = ctx.paginate("/projects", limit=1)
        self.assertEqual(1, len(items))
        self.assertTrue(truncated)

    def test_an_empty_first_page_returns_an_empty_complete_result(self):
        ctx = make_context("example", responses=[page([], total=0)])
        self.assertEqual(([], False, 0), ctx.paginate("/projects"))

    def test_a_failing_page_raises(self):
        ctx = make_context("example", responses=[
            page([{"id": 1}], next_page=2),
            FakeResponse(500, None, text="boom"),
        ])
        with self.assertRaises(ctxmod.GitLabHTTPError):
            ctx.paginate("/projects")

    def test_a_non_list_body_raises(self):
        ctx = make_context("example",
                           responses=[FakeResponse(200, {"id": 1})])
        with self.assertRaises(ctxmod.GitLabHTTPError):
            ctx.paginate("/projects")


# ─── id lookups ───────────────────────────────────────────────────────────────

class TestProjectId(unittest.TestCase):

    def test_an_int_is_returned_without_a_request(self):
        ctx = make_context("example")
        self.assertEqual(42, ctx.project_id(42))
        self.assertEqual([], ctx.session.calls)

    def test_a_numeric_string_is_returned_without_a_request(self):
        ctx = make_context("example")
        self.assertEqual(42, ctx.project_id("42"))
        self.assertEqual([], ctx.session.calls)

    def test_a_path_is_url_encoded_and_looked_up(self):
        ctx = make_context("example",
                           responses=[FakeResponse(200, {"id": 101})])
        self.assertEqual(101, ctx.project_id("group-a/project-a"))
        self.assertEqual(
            "https://gitlab.example.com/api/v4/projects/group-a%2Fproject-a",
            ctx.session.calls[0]["url"])

    def test_a_nested_path_is_fully_encoded(self):
        ctx = make_context("example",
                           responses=[FakeResponse(200, {"id": 5})])
        ctx.project_id("group-a/sub/project-a")
        self.assertIn("group-a%2Fsub%2Fproject-a", ctx.session.calls[0]["url"])

    def test_the_lookup_is_cached(self):
        ctx = make_context("example",
                           responses=[FakeResponse(200, {"id": 101})])
        self.assertEqual(101, ctx.project_id("group-a/project-a"))
        self.assertEqual(101, ctx.project_id("group-a/project-a"))
        self.assertEqual(1, len(ctx.session.calls))

    def test_a_missing_project_raises_not_found(self):
        ctx = make_context("example",
                           responses=[FakeResponse(404, {"message": "404"})])
        with self.assertRaises(ctxmod.GitLabNotFoundError):
            ctx.project_id("group-a/nope")

    def test_an_empty_reference_raises_valueerror(self):
        with self.assertRaises(ValueError):
            make_context("example").project_id("  ")

    def test_user_id_looks_a_username_up_and_caches_it(self):
        ctx = make_context("example",
                           responses=[FakeResponse(200, [{"id": 9}])])
        self.assertEqual(9, ctx.user_id("alice"))
        self.assertEqual(9, ctx.user_id("alice"))
        self.assertEqual(1, len(ctx.session.calls))
        self.assertEqual({"username": "alice"},
                         ctx.session.calls[0]["params"])

    def test_an_unknown_username_raises_not_found(self):
        ctx = make_context("example", responses=[FakeResponse(200, [])])
        with self.assertRaises(ctxmod.GitLabNotFoundError):
            ctx.user_id("nobody")


# ─── run_captured ─────────────────────────────────────────────────────────────

class TestRunCaptured(unittest.TestCase):

    def test_stdout_and_stderr_are_captured_not_printed(self):
        ctx = make_context("example")

        def noisy():
            print("cloning group-a")
            print("warning: slow", file=sys.stderr)
            return {"cloned": 1}

        result, text = ctx.run_captured(noisy)
        self.assertEqual({"cloned": 1}, result)
        self.assertIn("cloning group-a", text)
        self.assertIn("warning: slow", text)

    def test_the_captured_text_is_redacted(self):
        ctx = make_context("example")

        def leaky():
            print(f"remote: https://oauth2:{TOKEN}@gitlab.example.com/a.git")
            return None

        _, text = ctx.run_captured(leaky)
        self.assertNotIn(TOKEN, text)
        self.assertIn("[REDACTED]", text)

    def test_arguments_are_passed_through(self):
        ctx = make_context("example")
        result, text = ctx.run_captured(
            lambda a, b=0: a + b, 2, b=3)
        self.assertEqual(5, result)
        self.assertEqual("", text)

    def test_an_exception_propagates_and_stdout_is_restored(self):
        ctx = make_context("example")
        before = sys.stdout

        def explode():
            print("about to fail")
            raise RuntimeError("nope")

        with self.assertRaises(RuntimeError):
            ctx.run_captured(explode)
        self.assertIs(before, sys.stdout)


# ─── Import hygiene ───────────────────────────────────────────────────────────

class TestImportHygiene(unittest.TestCase):
    """Importing gitlab-cli modules must not touch the network or a terminal."""

    RELOADED = ("instances", "credentials", "api", "paths", "git",
                "pull", "clone", "errors")

    def test_importing_the_gitlab_cli_modules_is_inert(self):
        import importlib
        import requests
        modules = {name: importlib.import_module(name)
                   for name in self.RELOADED}
        for name, module in modules.items():
            self.assertTrue(hasattr(module, "__file__"), name)
        original_api_error = modules["api"].ApiError
        # A reload re-executes the module body in the SAME module object and
        # rebinds ApiError & co. to new classes, which would break every
        # ``except ApiError`` imported earlier in this process (the whole
        # gitlab-cli suite when the two run together). Snapshot both the
        # sys.modules entries and each namespace, and put them back.
        saved_entries = {name: sys.modules[name] for name in self.RELOADED}
        saved_namespaces = {name: dict(module.__dict__)
                            for name, module in modules.items()}
        try:
            with mock.patch.object(requests, "get",
                                   side_effect=AssertionError("network!")), \
                 mock.patch.object(requests, "post",
                                   side_effect=AssertionError("network!")), \
                 mock.patch("builtins.input",
                            side_effect=AssertionError("prompt!")):
                for module in modules.values():
                    importlib.reload(module)
        finally:
            sys.modules.update(saved_entries)
            for name, namespace in saved_namespaces.items():
                modules[name].__dict__.clear()
                modules[name].__dict__.update(namespace)
            # A reload recomputes CONFIG_DIR from ``~``; repoint the sandbox.
            _home_guard.refresh()
        self.assertIs(original_api_error, sys.modules["api"].ApiError)

    def test_constructing_a_context_makes_no_request(self):
        import requests
        with mock.patch.object(requests.Session, "request",
                               side_effect=AssertionError("network!")):
            ctx = make_context("example")
        self.assertEqual("example", ctx.name)


# ─── Sandbox guard ────────────────────────────────────────────────────────────

class TestHomeGuard(unittest.TestCase):
    """The suite's isolation from the real ``~`` is itself under test."""

    def tearDown(self):
        _home_guard.clear_violations()

    def test_home_is_a_temporary_directory(self):
        home = _home_guard.fake_home()
        self.assertIsNotNone(home)
        self.assertEqual(os.path.realpath(os.environ["HOME"]),
                         os.path.realpath(home))
        self.assertNotEqual(os.path.realpath(home), _home_guard.REAL_HOME)

    def test_every_gitlab_cli_config_path_is_inside_the_sandbox(self):
        import credentials
        import instances
        import paths
        home = os.path.realpath(_home_guard.fake_home())
        for value in (credentials.CONFIG_DIR, credentials.ENV_FILE,
                      instances.CONFIG_DIR, instances.INSTANCES_FILE,
                      paths.INSTANCES_FILE):
            self.assertTrue(os.path.realpath(value).startswith(home + os.sep),
                            value)

    def test_token_env_file_is_never_the_real_one(self):
        import credentials
        real_env = os.path.join(_home_guard.REAL_HOME, ".gitlab-cli", "env")
        self.assertNotEqual(os.path.realpath(credentials.ENV_FILE), real_env)
        self.assertEqual({}, credentials.read_env_file())

    def test_the_registry_reads_only_the_fake_instance(self):
        rows = ctxmod.list_instances_public()
        self.assertEqual([{"name": "example",
                           "url": "https://gitlab.example.com",
                           "active": True}], rows)

    def test_opening_a_path_under_the_real_home_is_refused(self):
        target = os.path.join(_home_guard.REAL_HOME, ".gitlab-cli",
                              "instances.json")
        with self.assertRaises(PermissionError):
            open(target, encoding="utf-8")           # noqa: SIM115
        self.assertEqual(1, len(_home_guard.violations()))

    def test_reading_git_credentials_is_refused(self):
        target = os.path.join(_home_guard.REAL_HOME, ".git-credentials")
        with self.assertRaises(PermissionError):
            open(target, encoding="utf-8")           # noqa: SIM115
        self.assertIn("credential store", _home_guard.violations()[0])

    def test_reading_an_ssh_key_is_refused(self):
        target = os.path.join(_home_guard.REAL_HOME, ".ssh", "id_ed25519")
        with self.assertRaises(PermissionError):
            open(target, "rb")                       # noqa: SIM115
        self.assertIn("credential store", _home_guard.violations()[0])

    def test_listing_the_ssh_directory_is_refused(self):
        target = os.path.join(_home_guard.REAL_HOME, ".ssh")
        for lister in (os.listdir, os.scandir):
            with self.subTest(lister=lister.__name__), \
                 self.assertRaises(PermissionError):
                lister(target)
        self.assertEqual(2, len(_home_guard.violations()))

    def test_secret_paths_are_judged_by_their_resolved_location(self):
        home = _home_guard.REAL_HOME
        self.assertTrue(_home_guard.is_secret_path(
            os.path.join(home, "x", "..", ".ssh", "config")))
        self.assertFalse(_home_guard.is_secret_path(
            os.path.join(_home_guard.fake_home(), ".ssh", "config")))
        self.assertFalse(_home_guard.is_secret_path(3))

    def test_a_remote_connection_is_refused(self):
        import socket
        with socket.socket() as sock, self.assertRaises(PermissionError):
            sock.connect(("192.0.2.1", 443))         # TEST-NET-1, never routed
        self.assertTrue(_home_guard.violations())


if __name__ == "__main__":        # pragma: no cover
    unittest.main()
