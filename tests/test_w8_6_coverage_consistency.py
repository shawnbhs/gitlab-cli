# tests/test_w8_6_coverage_consistency.py — one coverage number, not two.
#
# W8-6. The same session printed two different coverage figures for the same
# instance and explained neither:
#
#   Doctor (setup.py::_coverage_check)     -> "450/480 projects on disk"
#   Group list (gitlab_cli.py::pick_group) -> "450/450 projects on disk"
#
# The numbers are both defensible; the PRESENTATION was not. These tests pin
# the single accounting (W8-6 wrote it in cov.py; W10 moved it to its
# real home, coverage.py, beside group_coverage() — one definition, no
# forwarding shim):
#
#   * the two denominators are computed once, from one project list
#   * they always reconcile (group + user + unknown == all)
#   * the rendered line is never a bare fraction: it names the noun
#     ("group projects") and, when the denominators differ, the exclusion
#     ("30 user-namespace projects excluded by policy")
#
# Offline: no network, no token, no GitLab. Pure data in, string out.

import re
import unittest

import _bootstrap  # noqa: F401  (path bootstrap + error-log guard)

import coverage as cov


HOST = "git.example.net"


def proj(pwn, kind="group"):
    ns = {"kind": kind, "full_path": pwn.rsplit("/", 1)[0]}
    return {"path_with_namespace": pwn, "namespace": ns}


def corpus(n_group_present, n_group_missing, n_user):
    """A flat project list shaped like the live Hub instance."""
    projects, present = [], set()
    for i in range(n_group_present):
        p = "grp/team/p%03d" % i
        projects.append(proj(p))
        present.add((HOST, p))
    for i in range(n_group_missing):
        projects.append(proj("grp/team/absent%03d" % i))
    for i in range(n_user):
        projects.append(proj("someuser/personal%03d" % i, kind="user"))
    return projects, present


class NamespaceKind(unittest.TestCase):

    def test_group_user_and_unknown(self):
        self.assertEqual(cov.project_namespace_kind(proj("a/b")), "group")
        self.assertEqual(
            cov.project_namespace_kind(proj("u/b", kind="user")), "user")

    def test_missing_namespace_is_unknown_not_silently_group(self):
        # An instance that stops reporting namespace.kind must NOT have its
        # projects quietly folded into either bucket — that is how a
        # denominator drifts without anyone noticing.
        self.assertEqual(
            cov.project_namespace_kind({"path_with_namespace": "a/b"}),
            "unknown")
        self.assertEqual(cov.project_namespace_kind({}), "unknown")
        self.assertEqual(cov.project_namespace_kind(None), "unknown")


class Counts(unittest.TestCase):

    def test_both_denominators_from_one_list(self):
        projects, present = corpus(450, 0, 30)
        c = cov.coverage_counts(projects, present, HOST)
        self.assertEqual((c["group_present"], c["group_total"]), (450, 450))
        self.assertEqual(c["all_total"], 480)
        self.assertEqual(c["excluded_user"], 30)
        self.assertEqual(c["missing"], 0)

    def test_accounting_always_reconciles(self):
        projects, present = corpus(450, 0, 30)
        c = cov.coverage_counts(projects, present, HOST)
        self.assertTrue(cov.coverage_consistent(c))
        self.assertEqual(c["group_total"] + c["user_total"]
                         + c["unknown_total"], c["all_total"])

    def test_missing_group_projects_are_counted(self):
        projects, present = corpus(40, 5, 3)
        c = cov.coverage_counts(projects, present, HOST)
        self.assertEqual((c["group_present"], c["group_total"]), (40, 45))
        self.assertEqual(c["missing"], 5)

    def test_host_mismatch_is_not_counted_present(self):
        # One tree, two hosts. A clone of the OTHER instance must never be
        # credited to this one.
        projects, _ = corpus(5, 0, 0)
        other = {("hub.example.com", p["path_with_namespace"])
                 for p in projects}
        c = cov.coverage_counts(projects, other, HOST)
        self.assertEqual(c["group_present"], 0)

    def test_case_and_slash_insensitive_matching(self):
        projects = [proj("Grp/Team/Proj")]
        c = cov.coverage_counts(projects, {(HOST.upper(), "grp/team/proj")},
                                 HOST)
        self.assertEqual(c["group_present"], 1)

    def test_a_present_user_namespace_project_is_not_an_exclusion(self):
        projects, present = corpus(10, 0, 4)
        present = set(present)
        present.add((HOST, "someuser/personal000"))
        c = cov.coverage_counts(projects, present, HOST)
        self.assertEqual(c["user_present"], 1)
        self.assertEqual(c["excluded_user"], 3)

    def test_empty_inputs_do_not_raise(self):
        c = cov.coverage_counts([], set(), HOST)
        self.assertEqual(c["all_total"], 0)
        self.assertTrue(cov.coverage_consistent(c))

    def test_project_without_path_is_skipped_not_miscounted(self):
        c = cov.coverage_counts([{"namespace": {"kind": "group"}},
                                  proj("a/b")], {(HOST, "a/b")}, HOST)
        self.assertEqual(c["all_total"], 1)
        self.assertEqual(c["group_present"], 1)


class Line(unittest.TestCase):

    def test_never_a_bare_fraction(self):
        projects, present = corpus(450, 0, 30)
        line = cov.coverage_line(cov.coverage_counts(projects, present, HOST))
        # A bare "450/480" or "450/450" with no noun is the defect.
        self.assertIn("group projects", line)
        self.assertNotRegex(line.strip(), r"^\d+/\d+\s*$")

    def test_both_numbers_appear_and_are_explained(self):
        projects, present = corpus(450, 0, 30)
        line = cov.coverage_line(cov.coverage_counts(projects, present, HOST))
        self.assertIn("450/450 group projects", line)
        self.assertIn("480 on the instance", line)
        self.assertIn("30 " + cov.POLICY_USER_NAMESPACE, line)

    def test_reader_can_reconcile_without_the_source(self):
        # The two fractions a user could ever see must both be derivable
        # from this one string: group total, instance total, and the gap.
        projects, present = corpus(450, 0, 30)
        line = cov.coverage_line(cov.coverage_counts(projects, present, HOST))
        nums = [int(n) for n in re.findall(r"\d+", line)]
        self.assertIn(450, nums)
        self.assertIn(480, nums)
        self.assertIn(30, nums)

    def test_no_user_namespace_means_no_confusing_clause(self):
        projects, present = corpus(25, 0, 0)   # the git.example.net shape
        line = cov.coverage_line(cov.coverage_counts(projects, present, HOST))
        self.assertIn("25/25 group projects", line)
        self.assertNotIn("on the instance", line)
        self.assertNotIn(cov.POLICY_USER_NAMESPACE, line)

    def test_short_form_keeps_noun_and_exclusion(self):
        projects, present = corpus(450, 0, 30)
        c = cov.coverage_line(cov.coverage_counts(projects, present, HOST),
                               short=True)
        self.assertIn("group projects", c)
        self.assertIn(cov.POLICY_USER_NAMESPACE, c)
        self.assertNotIn("on disk", c)

    def test_unknown_namespace_kind_is_surfaced_not_hidden(self):
        projects = [proj("a/b"), {"path_with_namespace": "weird/x"}]
        line = cov.coverage_line(
            cov.coverage_counts(projects, {(HOST, "a/b")}, HOST))
        self.assertIn("unknown namespace kind", line)

    def test_missing_group_projects_still_render_one_line(self):
        projects, present = corpus(40, 5, 3)
        line = cov.coverage_line(cov.coverage_counts(projects, present, HOST))
        self.assertIn("40/45 group projects", line)
        self.assertIn("48 on the instance", line)


class Consistency(unittest.TestCase):

    def test_broken_counts_report_inconsistent(self):
        self.assertFalse(cov.coverage_consistent(
            {"group_total": 450, "user_total": 30, "unknown_total": 0,
             "all_total": 600}))

    def test_malformed_counts_never_raise(self):
        for bad in ({}, None, {"group_total": "x"}, {"all_total": 1}):
            self.assertFalse(cov.coverage_consistent(bad))


if __name__ == "__main__":
    unittest.main()
