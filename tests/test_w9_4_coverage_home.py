# tests/test_w9_4_coverage_home.py — one home for the coverage vocabulary.
#
# W8-6 built the single accounting (coverage_counts / coverage_line /
# coverage_consistent / project_namespace_kind / POLICY_USER_NAMESPACE) and had
# to leave it in caps.py for ownership reasons. W9-4 made coverage.py its
# ADDRESS through a PEP-562 forward but could not delete the bodies, because a
# physical move needs both edits in one commit. W10 did both: the bodies are in
# coverage.py beside group_coverage(), caps.py holds no copy and NO re-export,
# and the forwarding layer is gone.
#
# This file pins the property that matters more than the address: there is
# exactly ONE definition of each symbol on disk, reachable by exactly one name.
#
# The failure mode being guarded: this codebase already carries three
# directory-walking implementations that disagree (600 / 564 / 600 work
# trees), purely because the same logic was written three times, and the
# security sweep went through the one that saw fewest. A second copy of the
# coverage accounting — or a shim that keeps two live import paths to one copy
# — would be that bug again with a different noun.
#
# Offline. No network, no token.

import ast
import os
import unittest

import _bootstrap  # noqa: F401

import caps
import coverage as cov

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VOCAB = ("POLICY_USER_NAMESPACE", "project_namespace_kind",
         "coverage_counts", "coverage_line", "coverage_consistent")


def src(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def code_only(text: str) -> str:
    """`text` with whole-line and trailing comments removed.

    These tests grep SOURCE, which means a comment that merely NAMES a banned
    construct ("no module may call caps.coverage_counts") would fail the very
    test it documents. Stripping comments keeps the assertion about code while
    letting the code stay explained. Strings are left alone: a banned call
    hidden in a string is still worth flagging.
    """
    out = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if "#" in line and line.count('"') % 2 == 0 and line.count("'") % 2 == 0:
            line = line.split("#", 1)[0]
        out.append(line)
    return "\n".join(out)


def top_level_defs(path):
    """{name: 'def'|'assign'} for every module-level definition in a file."""
    tree = ast.parse(src(path))
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = "def"
        elif isinstance(node, ast.ClassDef):
            out[node.name] = "class"
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = "assign"
    return out


class VocabularyLivesInCoverage(unittest.TestCase):
    """coverage.py is the one address AND the one definition site."""

    def test_every_symbol_resolves_through_coverage(self):
        for name in VOCAB:
            self.assertTrue(hasattr(cov, name), name)

    def test_every_symbol_is_a_real_top_level_definition_here(self):
        # hasattr() alone would still pass against a forwarding __getattr__.
        defs = top_level_defs("coverage.py")
        for name in VOCAB:
            self.assertIn(name, defs, name)

    def test_the_symbols_are_listed_in_dir(self):
        d = dir(cov)
        for name in VOCAB:
            self.assertIn(name, d, name)

    def test_the_declared_tuple_matches_what_this_file_checks(self):
        # COVERAGE_VOCABULARY is what a future reader will trust; keep it and
        # this test's VOCAB from drifting apart.
        self.assertEqual(sorted(cov.COVERAGE_VOCABULARY), sorted(VOCAB))

    def test_unknown_attribute_still_raises_attributeerror(self):
        with self.assertRaises(AttributeError):
            cov.definitely_not_a_real_symbol  # noqa: B018

    def test_no_forwarding_layer_remains_in_coverage(self):
        # The PEP-562 scaffolding existed only to survive the split commit.
        # Leaving it behind would make a future second definition invisible:
        # __getattr__ only fires for names the module does NOT define.
        defs = top_level_defs("coverage.py")
        self.assertNotIn("__getattr__", defs)
        self.assertNotIn("__dir__", defs)
        self.assertNotIn("_VOCABULARY_HOME", defs)

    def test_vocabulary_home_is_declared_and_truthful(self):
        self.assertEqual(cov.vocabulary_home(), "coverage")
        defs = top_level_defs(cov.vocabulary_home() + ".py")
        for name in VOCAB:
            self.assertIn(name, defs, name)


class CapsKeepsNothing(unittest.TestCase):
    """Not just "no second copy" — no second NAME for the one copy."""

    def test_caps_does_not_define_any_of_the_vocabulary(self):
        defs = top_level_defs("caps.py")
        leftover = [n for n in VOCAB if n in defs]
        self.assertEqual(leftover, [], f"caps.py still defines {leftover}")

    def test_caps_does_not_re_export_the_vocabulary(self):
        # A shim would keep caps.coverage_counts working, and therefore keep
        # two import paths alive for one function — the shape that lets one
        # call site be fixed while the other silently drifts.
        for name in VOCAB:
            self.assertFalse(hasattr(caps, name),
                             f"caps still exposes {name}")

    def test_caps_does_not_import_coverage(self):
        tree = ast.parse(src("caps.py"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertNotIn("coverage", [a.name for a in node.names])
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "coverage")

    def test_caps_still_does_its_own_job(self):
        # The move must not have taken capability detection with it.
        for name in ("probe", "get", "role_label", "summary_line", "can",
                     "CAP_LABELS"):
            self.assertTrue(hasattr(caps, name), name)


class ExactlyOneDefinition(unittest.TestCase):
    """The whole point: never a second copy."""

    def test_no_symbol_is_defined_in_both_modules(self):
        cov_defs = top_level_defs("coverage.py")
        caps_defs = top_level_defs("caps.py")
        both = [n for n in VOCAB if n in cov_defs and n in caps_defs]
        self.assertEqual(both, [],
                         f"duplicated coverage vocabulary: {both}")

    def test_each_symbol_is_defined_exactly_once_repo_wide(self):
        # Scan every top-level module of the tool (not venv, not the
        # MasterGroups clone corpus, not tests).
        counts = {n: [] for n in VOCAB}
        for name in sorted(os.listdir(ROOT)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(ROOT, name)
            if not os.path.isfile(path):
                continue
            defs = top_level_defs(name)
            for sym in VOCAB:
                if sym in defs:
                    counts[sym].append(name)
        for sym, where in counts.items():
            self.assertEqual(where, ["coverage.py"],
                             f"{sym} defined in {where} — expected coverage.py only")

    def test_no_module_reaches_the_vocabulary_through_caps(self):
        # A stale `caps.coverage_counts(...)` would now be an AttributeError at
        # runtime, inside a menu draw. Catch it here instead of on screen.
        for name in sorted(os.listdir(ROOT)):
            if not name.endswith(".py") or not os.path.isfile(
                    os.path.join(ROOT, name)):
                continue
            body = code_only(src(name))
            for sym in VOCAB:
                self.assertNotIn("caps." + sym, body, f"{name}: caps.{sym}")

    def test_coverage_does_not_import_caps_at_module_level(self):
        # Would drag requests/credentials into this cheap module, and would be
        # the first half of an import cycle.
        tree = ast.parse(src("coverage.py"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                self.assertNotIn("caps", [a.name for a in node.names])
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "caps")


# Hub-shaped fixture: 450 group projects all on disk, 30 user-namespace ones
# excluded by policy, 480 on the instance. Module level, not class level:
# a comprehension in a class body cannot see the class's own names.
HOST = "hub.example"
PROJECTS = (
    [{"path_with_namespace": f"grp/p{i}", "namespace": {"kind": "group"}}
     for i in range(450)]
    + [{"path_with_namespace": f"usr{i}/p", "namespace": {"kind": "user"}}
       for i in range(30)])
PRESENT = {(HOST, f"grp/p{i}") for i in range(450)}


class NumbersAreUnchangedByTheMove(unittest.TestCase):
    """A refactor that moves a number is a bug.

    These are the representative Hub-shaped figures that motivated
    W8-6, pinned across the relocation. Before W10 the same assertions ran
    through caps.*; the values must be identical now they run through cov.*.
    """

    def test_hub_shaped_counts(self):
        a = cov.coverage_counts(PROJECTS, PRESENT, HOST)
        self.assertEqual(a["group_present"], 450)
        self.assertEqual(a["group_total"], 450)
        self.assertEqual(a["all_total"], 480)
        self.assertEqual(a["user_total"], 30)
        self.assertEqual(a["excluded_user"], 30)
        self.assertEqual(a["missing"], 0)
        self.assertTrue(cov.coverage_consistent(a))

    def test_the_one_line_is_unchanged(self):
        c = cov.coverage_counts(PROJECTS, PRESENT, HOST)
        line = cov.coverage_line(c)
        self.assertEqual(
            line,
            "450/450 group projects on disk (480 on the instance — "
            "30 " + cov.POLICY_USER_NAMESPACE + ")")
        self.assertNotIn("\n", line)

    def test_group_coverage_still_answers_the_group_question(self):
        # coverage.group_coverage's denominator is a GROUP's recursive project
        # list, so it structurally cannot contain a user-namespace project.
        # The vocabulary living here must not blur that.
        index = {("h", "g/a"): "/d/a"}
        projects = [{"path_with_namespace": "g/a"},
                    {"path_with_namespace": "g/b"}]
        cfg = {"url": "http://h"}
        self.assertEqual(cov.group_coverage(cfg, {}, index, projects), (1, 2))

    def test_unknown_namespace_kind_is_never_silently_a_group(self):
        c = cov.coverage_counts([{"path_with_namespace": "a/b"}], set(), "h")
        self.assertEqual(c["unknown_total"], 1)
        self.assertEqual(c["group_total"], 0)
        self.assertEqual(cov.project_namespace_kind({}), "unknown")


if __name__ == "__main__":
    unittest.main()
