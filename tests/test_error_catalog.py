#!/usr/bin/env python3
# tests/test_error_catalog.py — the error catalog (error_catalog.py).
#
# Pins CONTRACT.md "error_catalog.py" and defects D2, D3, D8:
#
#   D3  the old catalog matched bare "401"/"403"/"404"/"500" ANYWHERE, so a
#       hex LFS oid or a path like group/project-500 was reported as an HTTP
#       failure, while the failures users actually hit (DNS, timeout, LFS
#       smudge, unsafe url, unsafe remote name, empty repo) all fell through
#       to "Unknown error — follow up with your admin".
#   D2  stderr was cut HEAD-first, so the real cause (the LAST fatal:/error:
#       line) never reached the user. summarize() must pick that line.
#   D8  the curl-18 advice recommended http.postBuffer, which only affects
#       pushes and can never fix a fetch.
#
# Pure module: no I/O, no network. The strings below are the real ones
# measured against the live instances (CONTRACT.md D1-D8), not paraphrases.

import unittest

import _bootstrap  # noqa: F401  (path bootstrap + error-log guard)

import error_catalog as ec


CONTRACT_CODES = (
    "unknown", "dns", "timeout", "connection_refused", "ssl", "proxy", "auth",
    "forbidden", "not_found", "pending_deletion", "rate_limited",
    "server_error", "transfer_cap", "rpc_failed", "early_eof", "empty_repo",
    "lfs_missing_object", "lfs_auth", "lfs_not_installed", "unsafe_url",
    "unsafe_remote", "unsafe_arg", "dirty_tree", "diverged", "detached",
    "no_upstream", "not_a_repo", "lock_file", "disk_full",
    "permission_denied", "filename_too_long", "token_missing",
    "config_missing", "api_truncated",
)

CONTRACT_TYPES = ("Network", "Auth", "Access", "NotFound", "Server", "Git",
                  "LFS", "Disk", "Config", "Safety", "Local", "Unknown")

# D1, verbatim shape of the lfs-repo checkout failure.
OID = "9f2c4e1b7a3d5f60812c9e4b7d1a3f5e6c8b0a2d4f6e8a0c2b4d6f8e0a2c4b6d"
D1_LFS_STDERR = (
    "Cloning into 'lfs-repo'...\n"
    "Downloading assets/model.bin (36 MB)\n"
    "Error downloading object: assets/model.bin (9f2c4e1): Smudge error: "
    "Error downloading assets/model.bin (" + OID + "): LFS: Client error: "
    "http://gitlab.example.com/group-a/platform/lfs-repo.git/gitlab-lfs/objects/"
    + OID + "\n"
    "\n"
    "Errors logged to '/home/user/lfs-repo/.git/lfs/logs/20261022.log'.\n"
    "Use `git lfs logs last` to view the log.\n"
    "error: external filter 'git-lfs filter-process' failed\n"
    "fatal: assets/model.bin: smudge filter lfs failed\n"
    "warning: Clone succeeded, but checkout failed.\n"
)

# A 64-hex oid that contains every bare status the old catalog matched.
HEX_WITH_STATUSES = ("ab401cd403ef404aa500bb" + "0123456789abcdef" * 3)[:64]


class CatalogShapeTests(unittest.TestCase):

    def test_every_contract_code_is_reachable_by_code(self):
        for code in CONTRACT_CODES:
            with self.subTest(code=code):
                info = ec.by_code(code)
                self.assertIsNotNone(info, f"by_code({code!r}) returned None")
                self.assertEqual(info.code, code)

    def test_by_code_returns_none_for_an_unknown_code(self):
        self.assertIsNone(ec.by_code("no_such_code_ever"))

    def test_types_tuple_matches_contract(self):
        self.assertEqual(tuple(ec.TYPES), CONTRACT_TYPES)

    def test_every_entry_has_a_contract_type(self):
        for info in self._entries():
            with self.subTest(code=info.code):
                self.assertIn(info.type, CONTRACT_TYPES)

    def test_error_info_is_frozen(self):
        info = ec.by_code("dns")
        # A frozen dataclass raises FrozenInstanceError, an AttributeError.
        with self.assertRaises(AttributeError):
            info.code = "changed"

    def test_codes_are_unique(self):
        codes = [i.code for i in self._entries()]
        self.assertEqual(len(codes), len(set(codes)),
                         "duplicate codes in CATALOG")

    def test_title_fits_40_chars(self):
        for info in self._entries():
            with self.subTest(code=info.code):
                self.assertTrue(info.title.strip())
                self.assertLessEqual(len(info.title), 40, info.title)

    def test_cause_fits_110_chars(self):
        for info in self._entries():
            with self.subTest(code=info.code):
                self.assertTrue(info.cause.strip())
                self.assertLessEqual(len(info.cause), 110, info.cause)

    def test_fix_fits_110_chars(self):
        for info in self._entries():
            with self.subTest(code=info.code):
                self.assertTrue(info.fix.strip())
                self.assertLessEqual(len(info.fix), 110, info.fix)

    def test_no_fix_says_follow_up_with_your_admin(self):
        # D4: the old fallback told the user nothing actionable.
        for info in self._entries():
            with self.subTest(code=info.code):
                self.assertNotIn("follow up with your admin", info.fix.lower())

    def test_no_fix_recommends_http_postbuffer(self):
        # D8: postBuffer affects pushes only; it can never fix a fetch.
        for info in self._entries():
            with self.subTest(code=info.code):
                self.assertNotIn("postbuffer", info.fix.lower())

    def _entries(self):
        # The contract fixes CATALOG's order, not its row type: a row may be
        # the ErrorInfo itself or a wrapper carrying it as `.info`.
        entries = [getattr(e, "info", e) for e in ec.CATALOG]
        self.assertTrue(entries, "CATALOG is empty")
        for info in entries:
            self.assertIsInstance(info, ec.ErrorInfo)
        # The fallback must be describable even if it is not a CATALOG row.
        if not any(e.code == "unknown" for e in entries):
            entries.append(ec.by_code("unknown"))
        return entries


class ExplainNeverNoneTests(unittest.TestCase):

    def test_none_message_falls_back_to_unknown(self):
        info = ec.explain(None)
        self.assertIsNotNone(info)
        self.assertEqual(info.code, "unknown")

    def test_empty_message_falls_back_to_unknown(self):
        self.assertEqual(ec.explain("").code, "unknown")

    def test_garbage_falls_back_to_unknown(self):
        self.assertEqual(ec.explain("zzqx \x00 \u2603 lorem ipsum").code,
                         "unknown")

    def test_unknown_fallback_is_a_full_error_info(self):
        info = ec.explain("zzqx")
        for field in ("code", "type", "title", "cause", "fix"):
            self.assertTrue(getattr(info, field), f"empty {field}")


class ExplainRealStringsTests(unittest.TestCase):

    def assertCode(self, message, code):
        info = ec.explain(message)
        self.assertEqual(info.code, code,
                         f"explain({message[:90]!r}) -> {info.code!r}, "
                         f"expected {code!r}")

    def test_d1_lfs_smudge_failure_is_lfs_missing_object(self):
        self.assertCode(D1_LFS_STDERR, "lfs_missing_object")

    def test_could_not_resolve_host_is_dns(self):
        self.assertCode(
            "fatal: unable to access 'http://gitlab.example.com/a/b.git/': "
            "Could not resolve host: gitlab.example.com", "dns")

    def test_timed_out_is_timeout(self):
        self.assertCode("timed out after 120s", "timeout")

    def test_page_404_is_not_found(self):
        self.assertCode(
            "404 — Not found (or you don't have access) on page 1",
            "not_found")

    def test_404_on_deletion_scheduled_path_is_pending_deletion(self):
        self.assertCode(
            "HTTP 404 for http://gitlab.example.com/api/v4/projects/"
            "app-x-deletion_scheduled-98%2Fapp-x-chat", "pending_deletion")

    def test_unsafe_url_is_unsafe_url(self):
        self.assertCode(
            "unsafe url: only http/https are accepted, not: ext::sh -c id",
            "unsafe_url")

    def test_unsafe_remote_name_is_unsafe_remote(self):
        self.assertCode(
            "unsafe remote name(s) ignored: --upload-pack=touch /tmp/pwned",
            "unsafe_remote")

    def test_http_401_is_auth(self):
        self.assertCode(
            "fatal: unable to access 'http://gitlab.example.com/a/b.git/': "
            "The requested URL returned error: 401", "auth")

    def test_ssh_publickey_is_auth(self):
        self.assertCode(
            "git@gitlab.example.com: Permission denied (publickey).\n"
            "fatal: Could not read from remote repository.", "auth")

    def test_not_possible_to_fast_forward_is_diverged(self):
        self.assertCode("fatal: Not possible to fast-forward, aborting.",
                        "diverged")

    def test_no_space_left_is_disk_full(self):
        self.assertCode(
            "error: unable to write file x.pack: No space left on device",
            "disk_full")

    def test_cloned_empty_repository_is_empty_repo(self):
        self.assertCode(
            "warning: You appear to have cloned an empty repository.",
            "empty_repo")


class ExplainNoFalseHttpMatchTests(unittest.TestCase):
    """D3: a status number inside a word, hash or path is not a status."""

    def test_hex_oid_containing_statuses_is_unknown(self):
        self.assertEqual(len(HEX_WITH_STATUSES), 64)
        for s in ("401", "403", "404", "500"):
            self.assertIn(s, HEX_WITH_STATUSES)
        self.assertEqual(ec.explain(HEX_WITH_STATUSES).code, "unknown")

    def test_path_with_500_is_not_server_error(self):
        self.assertNotEqual(ec.explain("group/project-500/x").code,
                            "server_error")

    def test_path_with_401_is_not_auth(self):
        self.assertNotEqual(ec.explain("group/project-401/x").code, "auth")

    def test_path_with_404_is_not_not_found(self):
        self.assertNotEqual(ec.explain("group/release-404/x").code,
                            "not_found")


class SummarizeTests(unittest.TestCase):

    def test_picks_last_fatal_line_not_the_download_head(self):
        out = ec.summarize(D1_LFS_STDERR)
        self.assertIn("smudge filter lfs failed", out)
        self.assertNotIn("Downloading", out)
        self.assertNotIn("36 MB", out)

    def test_picks_last_error_line_when_there_is_no_fatal(self):
        text = ("remote: Enumerating objects: 5\n"
                "error: first problem\n"
                "some noise\n"
                "error: the real final problem\n"
                "trailing noise without a prefix\n")
        out = ec.summarize(text)
        self.assertIn("the real final problem", out)
        self.assertNotIn("first problem", out)

    def test_strips_ansi(self):
        out = ec.summarize("\x1b[31mfatal: \x1b[1mrepository not found\x1b[0m")
        self.assertNotIn("\x1b", out)
        self.assertIn("repository not found", out)

    def test_respects_limit_with_ellipsis(self):
        long_line = "fatal: " + "x" * 400
        out = ec.summarize(long_line, limit=60)
        self.assertLessEqual(len(out), 60)
        self.assertTrue(out.endswith("\u2026"), repr(out))

    def test_short_text_is_not_truncated(self):
        out = ec.summarize("fatal: short", limit=60)
        self.assertNotIn("\u2026", out)
        self.assertIn("short", out)

    def test_empty_input_returns_a_string(self):
        self.assertIsInstance(ec.summarize(""), str)
        self.assertIsInstance(ec.summarize(None), str)

    def test_single_line_input_is_returned(self):
        self.assertIn("Could not resolve host",
                      ec.summarize("Could not resolve host: gitlab.example.com"))


class OneLineTests(unittest.TestCase):

    def test_contains_title_and_respects_limit(self):
        msg = "Could not resolve host: gitlab.example.com"
        info = ec.explain(msg)
        out = ec.one_line(msg, limit=160)
        self.assertIsInstance(out, str)
        self.assertLessEqual(len(out), 160)
        self.assertIn(info.title, out)

    def test_never_empty_for_garbage(self):
        self.assertTrue(ec.one_line(None).strip())


if __name__ == "__main__":
    unittest.main()
