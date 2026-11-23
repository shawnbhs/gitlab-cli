import contextlib
import io
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
import api
import members

CFG = {"url": "https://git.example.net", "headers": {"PRIVATE-TOKEN": "fake"}}
USER = {"id": 7, "name": "Ali"}
PROJECT = {"id": 42, "path_with_namespace": "org/app"}


class Response:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text
        self.reason = "Error" if status_code >= 400 else "OK"


def drive(fn, answers, *args):
    output = io.StringIO()
    with patch("builtins.input", side_effect=answers), contextlib.redirect_stdout(output):
        fn(*args)
    return output.getvalue()


class ProjectMembershipTests(unittest.TestCase):
    def setUp(self):
        self.requests = []

    def listing(self, cfg, path, params=None, quiet=False, strict=True):
        self.requests.append((path, strict))
        if path == "projects":
            return api.PagedResult([PROJECT])
        if path == "projects/42/members":
            return api.PagedResult([])
        raise AssertionError(path)

    def test_grant_uses_direct_members_endpoint_after_confirmation(self):
        with patch.object(members, "_get_paginated", side_effect=self.listing), patch.object(
            members.requests, "post", return_value=Response(201)
        ) as post:
            text = drive(members.cmd_project_permissions, ["1", "1", "2", "y"], CFG, USER)
        self.assertIn("direct", text.lower())
        self.assertEqual(self.requests, [("projects", True), ("projects/42/members", True)])
        self.assertEqual(post.call_args.args[0], "https://git.example.net/api/v4/projects/42/members")
        self.assertEqual(post.call_args.kwargs["json"], {"user_id": 7, "access_level": 20})
        self.assertFalse(post.call_args.kwargs["allow_redirects"])

    def test_change_role_uses_put_not_post(self):
        def listing(cfg, path, params=None, quiet=False, strict=True):
            if path == "projects":
                return api.PagedResult([PROJECT])
            return api.PagedResult([{"id": 7, "access_level": 20}])
        with patch.object(members, "_get_paginated", side_effect=listing), patch.object(
            members.requests, "put", return_value=Response(200)
        ) as put, patch.object(members.requests, "post") as post:
            drive(members.cmd_project_permissions, ["1", "1", "4", "y"], CFG, USER)
        self.assertEqual(put.call_args.args[0], "https://git.example.net/api/v4/projects/42/members/7")
        self.assertEqual(put.call_args.kwargs["json"], {"access_level": 40})
        self.assertFalse(put.call_args.kwargs["allow_redirects"])
        post.assert_not_called()

    def test_revoke_only_direct_project_membership(self):
        def listing(cfg, path, params=None, quiet=False, strict=True):
            if path == "projects":
                return api.PagedResult([PROJECT])
            return api.PagedResult([{"id": 7, "access_level": 30}])
        with patch.object(members, "_get_paginated", side_effect=listing), patch.object(
            members.requests, "delete", return_value=Response(204)
        ) as delete:
            drive(members.cmd_project_permissions, ["1", "2", "y"], CFG, USER)
        self.assertEqual(delete.call_args.args[0], "https://git.example.net/api/v4/projects/42/members/7")
        self.assertFalse(delete.call_args.kwargs["allow_redirects"])

    def test_cancellation_and_owner_choice_do_not_write(self):
        with patch.object(members, "_get_paginated", side_effect=self.listing), patch.object(
            members.requests, "post"
        ) as post:
            text = drive(members.cmd_project_permissions, ["1", "1", "5"], CFG, USER)
            drive(members.cmd_project_permissions, ["1", "1", "2", "n"], CFG, USER)
        self.assertIn("'5' is not one of", text)
        post.assert_not_called()

    def test_partial_listing_aborts_before_write(self):
        with patch.object(members, "_get_paginated", side_effect=api.ApiError(500, "page failed")), patch.object(
            members.requests, "post"
        ) as post:
            text = drive(members.cmd_project_permissions, [], CFG, USER)
        self.assertIn("cannot be shown", text)
        post.assert_not_called()

    def test_invalid_ids_abort_before_write(self):
        with patch.object(members, "_get_paginated") as listing, patch.object(members.requests, "post") as post:
            text = drive(members.cmd_project_permissions, [], CFG, {"id": "7/2", "name": "Ali"})
        self.assertIn("Invalid user ID", text)
        listing.assert_not_called()
        post.assert_not_called()

    def test_partial_direct_members_listing_aborts_before_write(self):
        def listing(cfg, path, params=None, quiet=False, strict=True):
            if path == "projects":
                return api.PagedResult([PROJECT])
            raise api.ApiError(500, "page 2 failed")
        with patch.object(members, "_get_paginated", side_effect=listing), patch.object(
            members.requests, "post"
        ) as post, patch.object(members.requests, "delete") as delete:
            text = drive(members.cmd_project_permissions, ["1"], CFG, USER)
        self.assertIn("cannot be shown", text)
        post.assert_not_called()
        delete.assert_not_called()

    def test_invalid_project_id_aborts_before_write(self):
        with patch.object(members, "_get_paginated", return_value=api.PagedResult([{"id": "42/../7"}])), patch.object(
            members.requests, "post"
        ) as post:
            text = drive(members.cmd_project_permissions, [], CFG, USER)
        self.assertIn("invalid project ID", text)
        post.assert_not_called()

    def test_rejected_redirect_is_not_success(self):
        with patch.object(members, "_get_paginated", side_effect=self.listing), patch.object(
            members.requests, "post", return_value=Response(302)
        ), patch.object(members, "_report_write_failure") as failure:
            text = drive(members.cmd_project_permissions, ["1", "1", "2", "y"], CFG, USER)
        self.assertNotIn("succeeded", text)
        failure.assert_called_once()

    def test_inherited_access_is_not_a_direct_member(self):
        with patch.object(members, "_get_paginated", side_effect=self.listing), patch.object(
            members.requests, "get"
        ) as get:
            text = drive(members.cmd_project_permissions, ["1", "b"], CFG, USER)
        self.assertIn("inherited", text.lower())
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
