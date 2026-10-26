# tests/test_w9_4_orphans.py — orphaned clones, classified, visible.
#
# W9-4. An orphan is a work tree on disk whose origin matches no project the
# server reports. Two facts this file pins:
#
#   1. DETECTION is local, CLASSIFICATION needs the server. Nothing on disk
#      can tell a renamed project from a deleted one — GitLab follows its own
#      rename redirect, so only a GET can. Every record says which side
#      produced its answer.
#   2. A classification that could not be determined says UNKNOWN. It is never
#      defaulted to deleted: a 403 from a token that briefly lost access looks
#      identical to a deletion from here, and "deleted upstream" is the
#      sentence a user acts on with rm -rf.
#
# A previous audit found 29 orphans on the Hub tree (17 renamed, 12 deleted,
# 866 MB) while the menu could only see 6, because the menu asked once per
# top-level GROUP and a dissolved group has nothing left to iterate.
#
# Offline: the server is a stub. No network, no token.

import unittest

import _bootstrap  # noqa: F401

import coverage as cov

HOST = "git.example.net"
CFG = {"url": "http://" + HOST, "headers": {}}


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class StubServer:
    """Stands in for api.request. Records every path it was asked about."""

    def __init__(self, table):
        self.table = table          # encoded-path-tail -> FakeResponse | None
        self.asked = []

    def __call__(self, url, headers, params=None, quiet=False):
        tail = url.rsplit("/api/v4/projects/", 1)[-1]
        import urllib.parse
        path = urllib.parse.unquote(tail)
        self.asked.append(path)
        return self.table.get(path, FakeResponse(404))


class ServerStubbed(unittest.TestCase):
    def install(self, table):
        import api
        stub = StubServer(table)
        self._orig = api.request
        api.request = stub
        self.addCleanup(lambda: setattr(api, "request", self._orig))
        return stub


class DetectionIsLocal(unittest.TestCase):

    INDEX = {
        (HOST, "live/a"): "/d/live/a",
        (HOST, "gone/b"): "/d/gone/b",
        (HOST, "old/c"): "/d/old/c",
        ("other.host", "live/a"): "/d/other/a",   # different instance
    }
    PROJECTS = [{"path_with_namespace": "live/a"},
                {"path_with_namespace": "live/d"}]

    def test_finds_every_unmatched_clone_for_this_host_only(self):
        found = cov.local_orphans(self.INDEX, self.PROJECTS, HOST)
        self.assertEqual([o["path"] for o in found], ["gone/b", "old/c"])

    def test_makes_no_network_call_and_claims_no_classification(self):
        found = cov.local_orphans(self.INDEX, self.PROJECTS, HOST)
        for o in found:
            self.assertEqual(o["classification"], cov.ORPHAN_UNKNOWN)
            self.assertEqual(o["source"], cov.SOURCE_LOCAL)
            self.assertIsNone(o["probe_status"])
            self.assertIn("no server round-trip", o["reason"])

    def test_a_present_project_is_never_an_orphan(self):
        found = cov.local_orphans(self.INDEX, self.PROJECTS, HOST)
        self.assertNotIn("live/a", [o["path"] for o in found])

    def test_carries_the_local_directory_so_the_user_can_look(self):
        found = cov.local_orphans(self.INDEX, self.PROJECTS, HOST)
        self.assertEqual(found[0]["dir"], "/d/gone/b")

    def test_prefix_scope_is_opt_in_not_the_default(self):
        # The 29-vs-6 gap: a group-scoped sweep cannot see a clone whose whole
        # top-level group was dissolved server-side. Unscoped is the default.
        everything = cov.local_orphans(self.INDEX, self.PROJECTS, HOST)
        scoped = cov.local_orphans(self.INDEX, self.PROJECTS, HOST,
                                   prefix="gone")
        self.assertEqual(len(everything), 2)
        self.assertEqual([o["path"] for o in scoped], ["gone/b"])

    def test_prefix_matches_on_a_segment_boundary_not_a_substring(self):
        index = {(HOST, "gonely/x"): "/d/x", (HOST, "gone/y"): "/d/y"}
        scoped = cov.local_orphans(index, [], HOST, prefix="gone")
        self.assertEqual([o["path"] for o in scoped], ["gone/y"])

    def test_empty_inputs_are_not_an_error(self):
        self.assertEqual(cov.local_orphans({}, [], HOST), [])
        self.assertEqual(cov.local_orphans(None, None, HOST), [])


class ClassificationNeedsTheServer(ServerStubbed):

    def orphan(self, path):
        return {"host": HOST, "path": path, "dir": "/d/" + path,
                "classification": cov.ORPHAN_UNKNOWN,
                "source": cov.SOURCE_LOCAL, "probe_status": None,
                "current_path": None, "reason": "x"}

    def test_rename_redirect_is_read_as_renamed_with_the_new_path(self):
        self.install({"old/c": FakeResponse(
            200, {"path_with_namespace": "new/ns/c", "id": 7})})
        o = cov.classify_orphan(CFG, self.orphan("old/c"))
        self.assertEqual(o["classification"], cov.ORPHAN_RENAMED)
        self.assertEqual(o["current_path"], "new/ns/c")
        self.assertEqual(o["source"], cov.SOURCE_SERVER)
        self.assertEqual(o["probe_status"], 200)

    def test_a_404_is_read_as_deleted(self):
        self.install({})
        o = cov.classify_orphan(CFG, self.orphan("gone/b"))
        self.assertEqual(o["classification"], cov.ORPHAN_DELETED)
        self.assertEqual(o["probe_status"], 404)
        self.assertEqual(o["source"], cov.SOURCE_SERVER)

    def test_403_is_unknown_and_explicitly_not_deletion(self):
        self.install({"maybe/d": FakeResponse(403)})
        o = cov.classify_orphan(CFG, self.orphan("maybe/d"))
        self.assertEqual(o["classification"], cov.ORPHAN_UNKNOWN)
        self.assertEqual(o["probe_status"], 403)
        self.assertIn("NOT evidence of deletion", o["reason"])

    def test_transport_failure_is_unknown_not_deleted(self):
        self.install({"maybe/e": None})
        o = cov.classify_orphan(CFG, self.orphan("maybe/e"))
        self.assertEqual(o["classification"], cov.ORPHAN_UNKNOWN)
        self.assertIsNone(o["probe_status"])
        self.assertNotEqual(o["classification"], cov.ORPHAN_DELETED)

    def test_500_is_unknown(self):
        self.install({"maybe/f": FakeResponse(500)})
        o = cov.classify_orphan(CFG, self.orphan("maybe/f"))
        self.assertEqual(o["classification"], cov.ORPHAN_UNKNOWN)
        self.assertEqual(o["probe_status"], 500)

    def test_200_at_the_same_path_is_unknown_and_says_why(self):
        self.install({"odd/g": FakeResponse(
            200, {"path_with_namespace": "odd/g"})})
        o = cov.classify_orphan(CFG, self.orphan("odd/g"))
        self.assertEqual(o["classification"], cov.ORPHAN_UNKNOWN)
        self.assertIn("absent from the project listing", o["reason"])

    def test_classification_is_case_insensitive_about_the_same_path(self):
        self.install({"Odd/G": FakeResponse(
            200, {"path_with_namespace": "odd/g"})})
        o = cov.classify_orphan(CFG, self.orphan("Odd/G"))
        self.assertEqual(o["classification"], cov.ORPHAN_UNKNOWN)

    def test_the_original_record_is_not_mutated(self):
        self.install({})
        src = self.orphan("gone/b")
        cov.classify_orphan(CFG, src)
        self.assertEqual(src["classification"], cov.ORPHAN_UNKNOWN)
        self.assertEqual(src["source"], cov.SOURCE_LOCAL)

    def test_probe_opt_out_leaves_records_local_and_asks_nothing(self):
        stub = self.install({})
        out = cov.classify_orphans(CFG, [self.orphan("gone/b")], probe=False)
        self.assertEqual(stub.asked, [])
        self.assertEqual(out[0]["source"], cov.SOURCE_LOCAL)
        self.assertEqual(out[0]["classification"], cov.ORPHAN_UNKNOWN)

    def test_instance_wide_sweep_probes_every_orphan_once(self):
        stub = self.install({"old/c": FakeResponse(
            200, {"path_with_namespace": "new/c"})})
        index = {(HOST, "live/a"): "/d/a", (HOST, "old/c"): "/d/c",
                 (HOST, "gone/b"): "/d/b"}
        out = cov.instance_orphans(
            CFG, index, [{"path_with_namespace": "live/a"}])
        self.assertEqual(sorted(stub.asked), ["gone/b", "old/c"])
        by = {o["path"]: o["classification"] for o in out}
        self.assertEqual(by, {"old/c": cov.ORPHAN_RENAMED,
                              "gone/b": cov.ORPHAN_DELETED})


class SummaryAndDisplay(unittest.TestCase):

    def rec(self, path, cls, source=cov.SOURCE_SERVER, status=200, cur=None):
        return {"host": HOST, "path": path, "dir": "/d/" + path,
                "classification": cls, "source": source,
                "probe_status": status, "current_path": cur, "reason": ""}

    def w7_shape(self):
        """The real Hub case: 17 renamed + 12 deleted."""
        return ([self.rec(f"r{i}/p", cov.ORPHAN_RENAMED, cur=f"new/r{i}/p")
                 for i in range(17)]
                + [self.rec(f"d{i}/p", cov.ORPHAN_DELETED, status=404)
                   for i in range(12)])

    def test_the_two_causes_are_counted_separately(self):
        s = cov.orphan_summary(self.w7_shape())
        self.assertEqual(s["total"], 29)
        self.assertEqual(s["renamed"], 17)
        self.assertEqual(s["deleted"], 12)
        self.assertEqual(s["unknown"], 0)
        self.assertEqual(s["probed"], 29)

    def test_not_asked_is_distinct_from_no_usable_answer(self):
        items = [self.rec("a/1", cov.ORPHAN_UNKNOWN, cov.SOURCE_LOCAL, None),
                 self.rec("b/2", cov.ORPHAN_UNKNOWN, cov.SOURCE_SERVER, 403)]
        s = cov.orphan_summary(items)
        self.assertEqual(s["unclassified"], 1)   # never probed
        self.assertEqual(s["unknown"], 1)        # probed, no usable answer
        self.assertTrue(s["needs_server"])

    def test_a_clean_tree_is_reported_as_zero_not_hidden(self):
        s = cov.orphan_summary([])
        self.assertEqual(s["total"], 0)
        self.assertTrue(s["clean"])
        line = cov.orphan_headline(s)
        self.assertIn("0 orphaned clones", line)
        self.assertEqual(cov.orphan_display_lines([]), [line])

    def test_headline_never_shows_a_bare_undifferentiated_count(self):
        line = cov.orphan_headline(cov.orphan_summary(self.w7_shape()))
        self.assertIn("29", line)
        self.assertIn("17 renamed upstream", line)
        self.assertIn("12 deleted upstream", line)

    def test_display_separates_the_two_and_gives_different_advice(self):
        lines = cov.orphan_display_lines(self.w7_shape())
        text = "\n".join(lines)
        self.assertIn(cov.ORPHAN_LABELS[cov.ORPHAN_RENAMED], text)
        self.assertIn(cov.ORPHAN_LABELS[cov.ORPHAN_DELETED], text)
        self.assertNotEqual(cov.ORPHAN_ADVICE[cov.ORPHAN_RENAMED],
                            cov.ORPHAN_ADVICE[cov.ORPHAN_DELETED])
        self.assertIn(cov.ORPHAN_ADVICE[cov.ORPHAN_DELETED], text)

    def test_a_renamed_record_shows_where_the_project_went(self):
        lines = cov.orphan_display_lines(
            [self.rec("old/c", cov.ORPHAN_RENAMED, cur="new/ns/c")], detail=True)
        self.assertTrue(any("new/ns/c" in ln for ln in lines))

    def test_display_lines_carry_no_ansi_and_no_newlines(self):
        for ln in cov.orphan_display_lines(self.w7_shape()):
            self.assertNotIn("\x1b[", ln)
            self.assertNotIn("\n", ln)

    def test_every_classification_has_a_label_and_advice(self):
        for cls in cov.ORPHAN_CLASSIFICATIONS:
            self.assertTrue(cov.ORPHAN_LABELS[cls])
            self.assertTrue(cov.ORPHAN_ADVICE[cls])

    def test_limit_truncates_but_says_so(self):
        lines = cov.orphan_display_lines(self.w7_shape(), limit=3, detail=True)
        self.assertTrue(any("and 26 more" in ln for ln in lines))


if __name__ == "__main__":
    unittest.main()
