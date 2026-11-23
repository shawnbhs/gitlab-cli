"""Token-bearing member/probe requests must not follow redirects."""
import contextlib
import io
import unittest
from unittest import mock

import requests
from requests.adapters import HTTPAdapter

from tests import _home_guard
import caps
import members

HOST = "https://gitlab.example.invalid"
TOKEN = "synthetic-token-not-real"
CFG = {"url": HOST, "headers": {"PRIVATE-TOKEN": TOKEN}}
USER = {"id": 7, "name": "Ali", "username": "ali", "state": "active"}


def setUpModule():
    _home_guard.activate()


def tearDownModule():
    _home_guard.deactivate()


class Adapter(HTTPAdapter):
    def __init__(self, status, target="https://other.example.invalid/steal"):
        super().__init__()
        self.status = status
        self.target = target
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append((request.method, request.url, request.headers.get("PRIVATE-TOKEN")))
        response = requests.Response()
        response.status_code = self.status if len(self.sent) == 1 else 200
        response.url = request.url
        response.request = request
        response.headers["Location"] = self.target
        response._content = b'{"id": 7, "name": "Ali", "username": "ali"}'
        return response


class RedirectSafety(unittest.TestCase):
    def run_case(self, status, verb, invoke, answers=()):
        adapter = Adapter(status)
        session = requests.Session()
        session.mount("https://", adapter)
        output = io.StringIO()
        with mock.patch.object(requests, verb, getattr(session, verb)), \
             mock.patch("builtins.input", side_effect=answers), \
             contextlib.redirect_stdout(output):
            result = invoke()
        self.assertEqual(1, len(adapter.sent), adapter.sent)
        self.assertEqual(TOKEN, adapter.sent[0][2])
        self.assertTrue(adapter.sent[0][1].startswith(HOST))
        return result, output.getvalue()

    def test_caps_probe_and_member_fetch_reject_redirect(self):
        for status in (301, 302, 307, 308):
            with self.subTest(status=status):
                result, _ = self.run_case(status, "get", lambda: caps.probe(HOST, TOKEN))
                self.assertFalse(result["ok"])
                self.assertIn(f"HTTP {status}", result["error"])
                data, error = self.run_case(status, "get", lambda: members._fetch_one(CFG, "users/7"))[0]
                self.assertIsNone(data)
                self.assertIsNotNone(error)

    def test_same_host_redirect_is_not_followed(self):
        adapter = Adapter(302, HOST + "/another")
        session = requests.Session()
        session.mount("https://", adapter)
        with mock.patch.object(requests, "get", session.get):
            data, error = members._fetch_one(CFG, "users/7")
        self.assertIsNone(data)
        self.assertIsNotNone(error)
        self.assertEqual(1, len(adapter.sent))

    def test_group_conflict_update_redirect_is_not_followed(self):
        class ConflictThenRedirect(Adapter):
            def send(self, request, **kwargs):
                response = super().send(request, **kwargs)
                response.status_code = 409 if len(self.sent) == 1 else 307
                return response

        adapter = ConflictThenRedirect(409)
        session = requests.Session()
        session.mount("https://", adapter)
        output = io.StringIO()
        with mock.patch.object(requests, "post", session.post), \
             mock.patch.object(requests, "put", session.put), \
             contextlib.redirect_stdout(output):
            members._set_group_member(CFG, 3, 7, 20, "Ali", "org")
        self.assertEqual(["POST", "PUT"], [item[0] for item in adapter.sent])
        self.assertIn("Not changed", output.getvalue())

    def test_group_writes_reject_redirect(self):
        cases = (
            ("post", lambda: members._set_group_member(CFG, 3, 7, 20, "Ali", "org"), ()),
            ("delete", lambda: members._revoke_group_member(CFG, 3, 7, "Ali", "org"), ("y",)),
            ("put", lambda: members.cmd_edit_account(CFG, USER), ("1", "New name")),
            ("request", lambda: members.cmd_user_actions(CFG, USER), ("1", "y")),
        )
        for verb, invoke, answers in cases:
            with self.subTest(verb=verb):
                _result, text = self.run_case(307, verb, invoke, answers)
                self.assertIn("Not changed", text)

    def test_delete_user_rejects_redirect(self):
        _result, text = self.run_case(
            302, "delete", lambda: members.cmd_user_actions(CFG, USER),
            ("5", "yes", "ali"))
        self.assertIn("Not changed", text)

    def test_2xx_paths_remain_successful(self):
        response, error = self.run_case(200, "get", lambda: caps._request(HOST + "/api/v4/user", CFG["headers"]))[0]
        self.assertEqual(200, response.status_code)
        self.assertEqual("", error)
        data, error = self.run_case(200, "get", lambda: members._fetch_one(CFG, "users/7"))[0]
        self.assertEqual(7, data["id"])
        self.assertIsNone(error)
        for status, verb, invoke, answers in (
            (201, "post", lambda: members._set_group_member(CFG, 3, 7, 20, "Ali", "org"), ()),
            (204, "delete", lambda: members._revoke_group_member(CFG, 3, 7, "Ali", "org"), ("y",)),
            (200, "put", lambda: members.cmd_edit_account(CFG, USER), ("1", "New name")),
            (200, "request", lambda: members.cmd_user_actions(CFG, USER), ("1", "y")),
        ):
            with self.subTest(verb=verb):
                _result, text = self.run_case(status, verb, invoke, answers)
                self.assertNotIn("Not changed", text)
                self.assertIn("✔", text)


if __name__ == "__main__":
    unittest.main()
