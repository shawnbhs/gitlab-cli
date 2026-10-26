#!/usr/bin/env python3
# tests/test_w10_error_artifact_privacy.py — W10.
#
# Two disk artifacts carry credentials out of this tool:
#
#   1. ~/.gitlab-cli/errors.log        — written by errors.log_error()
#   2. <dest>/.<group>_errors.json     — written by clone._save_errors()
#
# Both quote raw git stderr and raw remote URLs, which is where a token
# actually appears. Before W10:
#
#   * errors.redact() stopped the header rule at one \S+, so
#     'Authorization: Bearer <token>' destroyed the word 'Bearer' and wrote
#     the token verbatim;
#   * log_error() redacted only `message`, never `context` — and a caller
#     builds the context from an API path, which can carry ?private_token=…;
#   * clone._save_errors() used plain open()+json.dump: no redaction at all,
#     and mode 0644 from the umask, on a file written INTO the clone tree.
#
# Every token in this file is synthetic and built in-process. Nothing here
# reads, prints or writes a real credential.

import json
import os
import stat
import unittest

from _bootstrap import TempTreeTestCase  # noqa: F401  (path bootstrap)

import errors
import clone

# Synthetic values. Assembled from fragments so the literal never appears
# whole in the source and no scanner flags this file.
TOKEN = "glpat-" + "FAKEFAKEFAKEFAKEFAKE000"
OPAQUE = "Zm9vYmFyYmF6cXV1eA" + "0123456789abcdef"       # no gl-prefix shape


class RedactionEscapeTests(unittest.TestCase):
    """Shapes that reached the log in the clear before W10."""

    def assertScrubbed(self, text, secret):
        out = errors.redact(text)
        self.assertNotIn(secret, out,
                         f"secret survived redaction: {text[:60]}…")
        return out

    def test_bearer_credential_after_an_auth_header_is_destroyed(self):
        """The regression: the old rule ate 'Bearer' and kept the token."""
        out = self.assertScrubbed(f"> Authorization: Bearer {OPAQUE}", OPAQUE)
        self.assertIn("Bearer", out,
                      "the auth scheme is diagnostic and must survive — it "
                      "says which code path built the request")

    def test_a_bare_bearer_credential_in_a_message_body_is_destroyed(self):
        self.assertScrubbed(f"remote: rejected: Bearer {OPAQUE}", OPAQUE)

    def test_scheme_less_userinfo_is_destroyed(self):
        """A .netrc / credential-helper echo has no '://' for rules 3 and 4."""
        self.assertScrubbed(
            f"fatal: x-access-token:{OPAQUE}@gitlab.example.com refused",
            OPAQUE)

    def test_an_env_var_style_secret_assignment_is_destroyed(self):
        for key in ("GITLAB_TOKEN", "CI_JOB_TOKEN", "api_key",
                    "MY_PASSWORD", "client_secret"):
            self.assertScrubbed(f"env: {key}={OPAQUE}", OPAQUE)

    def test_a_credential_flag_on_an_echoed_command_line_is_destroyed(self):
        self.assertScrubbed(f"ran: curl -u oauth2:{OPAQUE} https://h/x", OPAQUE)  # gitleaks:allow - fake test fixture
        self.assertScrubbed(f"ran: git ... --password={OPAQUE}", OPAQUE)

    def test_gits_access_denied_line_does_not_quote_the_credential(self):
        self.assertScrubbed(
            f"remote: HTTP Basic: Access denied for {OPAQUE}", OPAQUE)

    def test_ordinary_diagnostics_are_still_readable(self):
        """Negative control: over-redaction destroys the log's whole purpose."""
        for clean in ("fatal: repository not found",
                      "http://git.example.net/initech/feed-ms.git",
                      "error: token expired",
                      "remote: HTTP Basic: Access denied"):
            self.assertEqual(errors.redact(clean), clean, clean)

    def test_redaction_is_idempotent(self):
        """Re-scrubbing an already-scrubbed string must not corrode it, or a
        re-read/re-write cycle of the log degrades into mush."""
        once = errors.redact(f"Authorization: Bearer {OPAQUE}")
        self.assertEqual(errors.redact(once), once)


class RedactObjTests(unittest.TestCase):
    """redact_obj() is what makes a whole artifact safe, not one field."""

    def test_every_nested_string_is_scrubbed(self):
        data = {"errors": [{"url": f"https://oauth2:{TOKEN}@h/x.git",
                            "nested": {"detail": f"PRIVATE-TOKEN: {TOKEN}"}}]}
        out = json.dumps(errors.redact_obj(data))
        self.assertNotIn(TOKEN, out)

    def test_dict_keys_are_scrubbed_too(self):
        out = errors.redact_obj({f"PRIVATE-TOKEN: {TOKEN}": "v"})
        self.assertNotIn(TOKEN, json.dumps(out))

    def test_non_strings_survive_unchanged(self):
        data = {"n": 3, "f": 1.5, "b": True, "z": None, "l": [1, 2]}
        self.assertEqual(errors.redact_obj(data), data)

    def test_recursion_is_depth_bounded(self):
        node = {}
        cur = node
        for _ in range(60):
            nxt = {}
            cur["k"] = nxt
            cur = nxt
        errors.redact_obj(node)          # must not raise RecursionError


class WritePrivateJsonTests(TempTreeTestCase):

    def test_the_artifact_is_created_0600(self):
        path = os.path.join(self.tmp, "a.json")
        errors.write_private_json(path, {"x": 1})
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600, f"artifact is {oct(mode)}, not 0600")

    def test_a_pre_existing_0644_artifact_is_repaired(self):
        """The real-world case: a file written by the older code is still on
        disk. A re-run must fix its mode, not inherit it."""
        path = os.path.join(self.tmp, "b.json")
        with open(path, "w") as fh:
            fh.write("{}")
        os.chmod(path, 0o644)
        errors.write_private_json(path, {"x": 1})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_the_content_is_redacted(self):
        path = os.path.join(self.tmp, "c.json")
        errors.write_private_json(
            path, {"url": f"https://oauth2:{TOKEN}@h/x.git"})
        with open(path) as fh:
            text = fh.read()
        self.assertNotIn(TOKEN, text)
        self.assertIn("[REDACTED", text)

    def test_it_is_still_valid_json_and_round_trips(self):
        path = os.path.join(self.tmp, "d.json")
        errors.write_private_json(path, {"group": "initech/core", "errors": []})
        with open(path) as fh:
            self.assertEqual(json.load(fh),
                             {"group": "initech/core", "errors": []})

    def test_no_temp_file_is_left_behind(self):
        path = os.path.join(self.tmp, "e.json")
        errors.write_private_json(path, {"x": 1})
        leftovers = [n for n in os.listdir(self.tmp) if ".tmp-" in n]
        self.assertEqual(leftovers, [], f"temp artifact left on disk: {leftovers}")


class LogErrorContextTests(TempTreeTestCase):
    """log_error() scrubbed `message` and wrote `context` raw."""

    def setUp(self):
        super().setUp()
        self._dir, self._file = errors.ERROR_DIR, errors.ERROR_FILE
        errors.ERROR_DIR = os.path.join(self.tmp, "cfg")
        errors.ERROR_FILE = os.path.join(errors.ERROR_DIR, "errors.log")

    def tearDown(self):
        errors.ERROR_DIR, errors.ERROR_FILE = self._dir, self._file
        super().tearDown()

    def _log_text(self):
        with open(errors.ERROR_FILE) as fh:
            return fh.read()

    def test_a_token_in_the_context_does_not_reach_the_log(self):
        """The context is built by callers from an API path — a URL fragment
        that can carry ?private_token=…  (members.py: log_error(f"api:{path}"))."""
        errors.log_error(f"api:groups/7/members?private_token={TOKEN}",
                         "500 — server error")
        self.assertNotIn(TOKEN, self._log_text())

    def test_a_token_in_the_message_still_does_not_reach_the_log(self):
        errors.log_error("clone:/tmp/x", f"fatal: Bearer {OPAQUE} rejected")
        self.assertNotIn(OPAQUE, self._log_text())

    def test_the_record_is_still_a_usable_diagnostic(self):
        errors.log_error("clone:/srv/MasterGroups/initech/core",
                         "fatal: repository not found")
        entry = json.loads(self._log_text().splitlines()[-1])
        self.assertIn("initech/core", entry["context"])
        self.assertIn("repository not found", entry["message"])

    def test_the_log_file_is_0600(self):
        errors.log_error("clone:/tmp/x", "boom")
        self.assertEqual(
            stat.S_IMODE(os.stat(errors.ERROR_FILE).st_mode), 0o600)


class GroupArtifactTests(TempTreeTestCase):
    """clone._save_errors() is the per-group artifact's only writer."""

    def _artifact(self, group):
        return clone._error_log_path(self.dest, group)

    def test_the_group_artifact_is_written_0600(self):
        clone._save_errors(self.dest, "initech/core", [
            {"project": "p", "path": "/x", "url": "http://h/p.git"}])
        path = self._artifact("initech/core")
        self.assertTrue(os.path.exists(path), path)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600,
                         f"per-group error artifact is {oct(mode)}; it sits in "
                         "the clone tree and quotes git stderr")

    def test_a_credential_in_a_project_url_never_reaches_the_artifact(self):
        clone._save_errors(self.dest, "initech/core", [{
            "project": "core",
            "path":    os.path.join(self.dest, "initech", "core"),
            "url":     f"https://oauth2:{TOKEN}@git.example.net/initech/core.git",
            "detail":  f"remote: HTTP Basic: Access denied for {OPAQUE}",
        }])
        with open(self._artifact("initech/core")) as fh:
            text = fh.read()
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(OPAQUE, text)

    def test_the_artifact_still_round_trips_through_load_errors(self):
        """Redacting must not break the retry path that reads it back."""
        rows = [{"project": "core", "path": "/x",
                 "url": "http://git.example.net/initech/core.git",
                 "reason": "failed"}]
        clone._save_errors(self.dest, "initech/core", rows)
        self.assertEqual(clone.load_errors(self.dest, "initech/core"), rows)

    def test_a_pre_existing_world_readable_artifact_is_repaired_on_rewrite(self):
        path = self._artifact("initech/core")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"group": "initech/core", "errors": []}, fh)
        os.chmod(path, 0o644)
        clone._save_errors(self.dest, "initech/core",
                           [{"project": "p", "path": "/x", "url": "http://h/p.git"}])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_a_clean_run_still_removes_the_artifact(self):
        """Negative control: the empty-list branch must keep working."""
        clone._save_errors(self.dest, "initech/core",
                           [{"project": "p", "path": "/x", "url": "http://h/p.git"}])
        self.assertTrue(os.path.exists(self._artifact("initech/core")))
        clone._save_errors(self.dest, "initech/core", [])
        self.assertFalse(os.path.exists(self._artifact("initech/core")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
