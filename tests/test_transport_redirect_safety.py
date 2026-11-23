"""Redirects must not carry GitLab credentials to a second request."""

import unittest
from unittest import mock

import requests
from requests.adapters import HTTPAdapter

from tests import _home_guard
import api
from gitlab_mcp import context as ctxmod


SOURCE = "https://gitlab.example.invalid/api/v4/projects"
TOKEN = "synthetic-token-not-real"


def setUpModule():
    _home_guard.activate()


def tearDownModule():
    _home_guard.deactivate()


class RedirectAdapter(HTTPAdapter):
    def __init__(self, target, status):
        super().__init__()
        self.target, self.status = target, status
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append((request.url, request.headers.get("PRIVATE-TOKEN")))
        response = requests.Response()
        response.status_code = self.status if len(self.sent) == 1 else 200
        response.url = request.url
        response.request = request
        response.headers["Location"] = self.target
        response._content = b"[]"
        return response


class RedirectSafety(unittest.TestCase):
    def setUp(self):
        api.reset_failures()
        self.addCleanup(api.reset_failures)

    def _session(self, adapter):
        session = requests.Session()
        session.mount("https://", adapter)
        return session

    def _context(self, adapter):
        cfg = {"name": "fixture", "url": "https://gitlab.example.invalid",
               "token": TOKEN, "master_dir": "/nonexistent"}
        with mock.patch.object(ctxmod.gl_instances, "get_active_instance", return_value=cfg):
            context = ctxmod.GitLabContext()
        context._GitLabContext__session.mount("https://", adapter)
        return context

    def test_api_never_follows_cross_host_or_same_host_redirect(self):
        for target in ("https://other.example.invalid/steal", SOURCE + "/elsewhere"):
            for status in (301, 302, 307, 308):
                with self.subTest(target=target, status=status):
                    adapter = RedirectAdapter(target, status)
                    session = self._session(adapter)
                    with mock.patch.object(api.requests, "get", side_effect=session.get):
                        result = api.request(SOURCE, {"PRIVATE-TOKEN": TOKEN}, quiet=True)
                    self.assertEqual(status, result.status_code)
                    self.assertEqual([(SOURCE, TOKEN)], adapter.sent)

    def test_context_never_follows_cross_host_or_same_host_redirect(self):
        for target in ("https://other.example.invalid/steal", SOURCE + "/elsewhere"):
            for status in (301, 302, 307, 308):
                with self.subTest(target=target, status=status):
                    adapter = RedirectAdapter(target, status)
                    context = self._context(adapter)
                    with self.assertRaises(ctxmod.GitLabHTTPError) as error:
                        context.get("/projects")
                    self.assertEqual(status, error.exception.status)
                    self.assertEqual([(SOURCE, TOKEN)], adapter.sent)

    def test_context_private_request_rejects_redirect(self):
        adapter = RedirectAdapter("https://other.example.invalid/steal", 303)
        context = self._context(adapter)
        with self.assertRaises(ctxmod.GitLabHTTPError) as error:
            context._request("GET", "/projects")
        self.assertEqual(303, error.exception.status)
        self.assertEqual([(SOURCE, TOKEN)], adapter.sent)

    def test_api_redirect_is_incomplete_under_strict_pagination(self):
        adapter = RedirectAdapter("https://other.example.invalid/steal", 302)
        with mock.patch.object(api.requests, "get", side_effect=self._session(adapter).get):
            with self.assertRaises(api.ApiError) as error:
                api.get_paginated(SOURCE, {"PRIVATE-TOKEN": TOKEN}, quiet=True)
        self.assertEqual(302, error.exception.status)
        self.assertEqual([(SOURCE + "?per_page=100&page=1", TOKEN)], adapter.sent)

    def test_both_transports_accept_2xx(self):
        adapter = RedirectAdapter("https://other.example.invalid/unused", 200)
        with mock.patch.object(api.requests, "get", side_effect=self._session(adapter).get):
            self.assertEqual(200, api.request(SOURCE, {"PRIVATE-TOKEN": TOKEN}).status_code)
        self.assertEqual([(SOURCE, TOKEN)], adapter.sent)
        adapter = RedirectAdapter("https://other.example.invalid/unused", 200)
        self.assertEqual([], self._context(adapter).get("/projects"))
        self.assertEqual([(SOURCE, TOKEN)], adapter.sent)


if __name__ == "__main__":
    unittest.main()
