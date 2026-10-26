# tests/test_url_normalization.py
#
# coverage.scan matches a local clone to a remote project by normalizing the
# origin URL. If normalization is too weak, a cloned project reads as missing
# (re-clone storms, group stuck at ◐). If it is too aggressive — e.g. keying on
# the basename — two different projects collapse into one and "clone all"
# reports complete while a project was never fetched. This instance has 30
# basename collisions (initech/feed-ms vs legacy_transfer/feed-ms), so the
# collision half of this file is not hypothetical.

import unittest

from _bootstrap import TempTreeTestCase
import coverage as cov

CANON = ("git.example.net", "initech/feed-ms")


class NormalizeEquivalenceTests(unittest.TestCase):
    """Every spelling of the same remote must produce one key."""

    def test_plain_http(self):
        self.assertEqual(cov.normalize("http://git.example.net/initech/feed-ms"), CANON)

    def test_dot_git_suffix(self):
        self.assertEqual(cov.normalize("http://git.example.net/initech/feed-ms.git"), CANON)

    def test_trailing_slash(self):
        self.assertEqual(cov.normalize("http://git.example.net/initech/feed-ms/"), CANON)

    def test_dot_git_and_trailing_slash(self):
        self.assertEqual(cov.normalize("http://git.example.net/initech/feed-ms.git/"), CANON)

    def test_mixed_case_host_and_path(self):
        self.assertEqual(cov.normalize("http://GIT.EXAMPLE.NET/Initech/Feed-MS.git"), CANON)

    def test_https_vs_http_same_key(self):
        self.assertEqual(cov.normalize("https://git.example.net/initech/feed-ms.git"), CANON)

    def test_embedded_credential_is_stripped(self):
        # this is exactly what the tool persisted into every .git/config file
        self.assertEqual(
            cov.normalize("http://oauth2:SECRET-NOT-A-REAL-TOKEN@git.example.net/initech/feed-ms.git"),
            CANON)

    def test_embedded_user_password_is_stripped(self):
        self.assertEqual(
            cov.normalize("https://user:pa%40ss@git.example.net/initech/feed-ms.git"),
            CANON)

    def test_ssh_scp_style_remote(self):
        self.assertEqual(cov.normalize("git@git.example.net:initech/feed-ms.git"), CANON)

    def test_ssh_url_scheme(self):
        self.assertEqual(cov.normalize("ssh://git@git.example.net/initech/feed-ms.git"), CANON)

    def test_non_standard_port_is_ignored(self):
        self.assertEqual(cov.normalize("http://git.example.net:8929/initech/feed-ms.git"), CANON)

    def test_ssh_url_with_port(self):
        self.assertEqual(cov.normalize("ssh://git@git.example.net:2222/initech/feed-ms.git"), CANON)

    def test_all_spellings_collapse_to_exactly_one_key(self):
        spellings = [
            "http://git.example.net/initech/feed-ms",
            "http://git.example.net/initech/feed-ms.git",
            "http://git.example.net/initech/feed-ms/",
            "http://GIT.EXAMPLE.NET/INITECH/FEED-MS.git",
            "https://git.example.net/initech/feed-ms.git",
            "http://oauth2:SECRET-NOT-A-REAL-TOKEN@git.example.net/initech/feed-ms.git",
            "https://user:pw@git.example.net/initech/feed-ms.git",
            "git@git.example.net:initech/feed-ms.git",
            "ssh://git@git.example.net/initech/feed-ms.git",
            "http://git.example.net:8929/initech/feed-ms.git",
            "ssh://git@git.example.net:2222/initech/feed-ms.git",
        ]
        self.assertEqual({cov.normalize(s) for s in spellings}, {CANON})

    def test_deep_namespace_is_preserved_whole(self):
        self.assertEqual(
            cov.normalize("http://git.example.net/globex/legacy-tools/geo-scrape/maps-a.git"),
            ("git.example.net", "globex/legacy-tools/geo-scrape/maps-a"))

    def test_garbage_returns_none_rather_than_a_bogus_key(self):
        for bad in ("", "   ", None, "not a url", "http://git.example.net",
                    "http://git.example.net/", "git@git.example.net"):
            self.assertIsNone(cov.normalize(bad), repr(bad))


class CollisionSafetyTests(TempTreeTestCase):
    """Different projects that share a basename must never collapse."""

    def test_same_basename_two_namespaces_are_distinct_keys(self):
        a = cov.normalize("http://git.example.net/initech/feed-ms.git")
        b = cov.normalize("http://git.example.net/legacy_transfer/feed-ms.git")
        self.assertNotEqual(a, b)
        self.assertEqual(a[1], "initech/feed-ms")
        self.assertEqual(b[1], "legacy_transfer/feed-ms")

    def test_same_basename_different_hosts_are_distinct_keys(self):
        self.assertNotEqual(cov.normalize("http://git.example.net/initech/feed-ms.git"),
                            cov.normalize("http://git.example.org/initech/feed-ms.git"))

    def test_scan_keeps_both_colliding_clones(self):
        self.make_project_clone("initech/feed-ms")
        self.make_project_clone("legacy_transfer/feed-ms")
        index = cov.scan(self.dest)
        self.assertEqual(len(index), 2, index)
        self.assertIn(("git.example.net", "initech/feed-ms"), index)
        self.assertIn(("git.example.net", "legacy_transfer/feed-ms"), index)

    def test_cloning_one_of_a_colliding_pair_leaves_the_other_missing(self):
        # the failure this pins: dedupe-by-basename would report 2/2.
        from _bootstrap import fake_cfg, fake_projects
        projects = fake_projects("initech/feed-ms", "legacy_transfer/feed-ms")
        self.make_project_clone("initech/feed-ms")
        index = cov.scan(self.dest)
        cloned, total = cov.group_coverage(
            fake_cfg(), {"id": 0, "full_path": "x"}, index, projects)
        self.assertEqual((cloned, total), (1, 2))
        self.assertEqual(cov.status_icon(cloned, total), "◐")

    def test_tokenised_clone_matches_the_clean_api_url(self):
        # a repo cloned before the credential scrub must still count as present
        self.make_repo("initech/feed-ms",
                       "http://oauth2:SECRET-NOT-A-REAL-TOKEN@git.example.net/initech/feed-ms.git")
        index = cov.scan(self.dest)
        self.assertIn(("git.example.net", "initech/feed-ms"), index)


if __name__ == "__main__":
    unittest.main()
