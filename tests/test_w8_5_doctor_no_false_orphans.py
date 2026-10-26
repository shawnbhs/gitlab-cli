#!/usr/bin/env python3
# tests/test_w8_5_doctor_no_false_orphans.py — W8-5, second half.
#
# Partitioning user-namespace projects out of Doctor's coverage arithmetic
# created a NEW way to lie, and it fired on the live instance the first time
# it ran: anna_user/investment-data is already cloned, was removed from
# `want` by the policy filter, and Doctor then reported it as
#   "1 clone(s) the instance does not list"
# The instance lists it perfectly well. An exclusion that mutates into a
# different, wrong-sounding finding is exactly the failure this project
# exists to prevent — so orphan detection is computed over the UNPARTITIONED
# list, and the already-on-disk count is disclosed in words.

import unittest

from _bootstrap import TempTreeTestCase  # noqa: F401  (path bootstrap)

import setup as S


def proj(pwn, kind="user"):
    ns = pwn.split("/")[0]
    return {"path_with_namespace": pwn, "path": pwn.split("/")[-1],
            "namespace": {"kind": kind, "full_path": ns, "path": ns}}


class OrphanSourceList(unittest.TestCase):
    """The orphan set must be built from every project the instance listed."""

    def test_orphan_detection_reads_the_unpartitioned_list(self):
        import inspect
        src = inspect.getsource(S._coverage_check)
        head, _, tail = src.partition(
            "# Clones that exist locally but that the instance no longer lists.")
        self.assertTrue(tail, "orphan block not found")
        # `excluded` must be folded back in for the orphan comparison.
        self.assertIn("excluded", tail.split("orphans =")[0])

    def test_want_is_still_built_from_reachable_projects_only(self):
        import inspect
        src = inspect.getsource(S._coverage_check)
        self.assertIn("projs = reachable", src)

    def test_a_cloned_but_excluded_project_is_not_an_orphan(self):
        # Reproduces the live finding in pure form.
        listed = [proj("group/a", kind="group"),
                  proj("anna_user/investment-data")]
        reachable, excluded, _ = S.partition_by_policy(listed, None,
                                                       scope="off")
        everything = {(p.get("path_with_namespace") or "").lower()
                      for p in list(reachable) + list(excluded)}
        have = {("git.example.net", "anna_user/investment-data")}
        orphans = [k for k in have if k[1] not in everything]
        self.assertEqual(orphans, [])

    def test_a_genuinely_gone_clone_is_still_an_orphan(self):
        listed = [proj("group/a", kind="group")]
        reachable, excluded, _ = S.partition_by_policy(listed, None,
                                                       scope="off")
        everything = {(p.get("path_with_namespace") or "").lower()
                      for p in list(reachable) + list(excluded)}
        have = {("git.example.net", "group/deleted-long-ago")}
        orphans = [k for k in have if k[1] not in everything]
        self.assertEqual(len(orphans), 1)


class OnDiskDisclosure(unittest.TestCase):
    """Excluded-but-already-cloned is its own fact and is said out loud."""

    def _lines(self, **kw):
        out = []
        real = S._note
        S._note = lambda t: out.append(t)
        try:
            S._user_ns_report("live", {"url": "http://git.example.net",
                                       "headers": {}},
                              [proj("someone/x")], "off", "", [], [], **kw)
        finally:
            S._note = real
        return "\n".join(out)

    def test_already_cloned_count_is_stated(self):
        text = self._lines(excluded_on_disk=1)
        self.assertIn("ALREADY on disk", text)
        self.assertIn("NOT orphans", text)

    def test_nothing_on_disk_adds_no_line(self):
        self.assertNotIn("ALREADY on disk", self._lines(excluded_on_disk=0))

    def test_the_parameter_defaults_to_zero(self):
        import inspect
        sig = inspect.signature(S._user_ns_report)
        self.assertEqual(sig.parameters["excluded_on_disk"].default, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
