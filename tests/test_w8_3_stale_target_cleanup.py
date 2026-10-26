# tests/test_w8_3_stale_target_cleanup.py
#
# W8-3: the explicit stale-target cleanup that makes the main-menu badge
# trustworthy again, without reintroducing the "reading rewrites the file"
# defect this module already carried once.
#
# Every test here points errors.ERROR_DIR / ERROR_FILE at its own mkdtemp log.
# The live ~/.gitlab-cli/errors.log is never opened, and _error_log_guard
# (imported via _bootstrap) asserts that independently.

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta

import _bootstrap            # noqa: F401 — installs the live-log guard
import _error_log_guard

import errors


def _digest(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class StaleTargetBase(unittest.TestCase):
    """One temp log per test; errors.py redirected at it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gitlabcli_w83_")
        self.log_dir = os.path.join(self.tmp, "cfg")
        os.makedirs(self.log_dir)
        self.log = os.path.join(self.log_dir, "errors.log")

        self._saved = (errors.ERROR_DIR, errors.ERROR_FILE)
        errors.ERROR_DIR = self.log_dir
        errors.ERROR_FILE = self.log

        # Independent proof that nothing in this test reaches the real file.
        self._live_before = _error_log_guard.real_log_digest()

    def tearDown(self):
        errors.ERROR_DIR, errors.ERROR_FILE = self._saved
        self.assertEqual(self._live_before, _error_log_guard.real_log_digest(),
                         "a W8-3 test modified the LIVE error log")
        assert self.tmp.startswith(tempfile.gettempdir())
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── helpers ────────────────────────────────────────────────────────────

    def entry(self, context, date=None, resolved=False,
              message="boom"):
        if date is None:   # recent, so the 30-day expiry never ages it out
            date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        return {"date": date, "context": context, "message": message,
                "type": "Unknown", "title": "Unknown error",
                "fix": "x", "resolved": resolved}

    def write_lines(self, *lines):
        with open(self.log, "w", encoding="utf-8") as fh:
            for ln in lines:
                fh.write((ln if isinstance(ln, str)
                          else json.dumps(ln, ensure_ascii=False)) + "\n")
        os.chmod(self.log, 0o600)

    def raw_lines(self):
        with open(self.log, encoding="utf-8") as fh:
            return [ln for ln in fh.read().splitlines() if ln.strip()]

    def live_dir(self, name="alive"):
        """A directory that exists, plus a target path inside it."""
        parent = os.path.join(self.tmp, name)
        os.makedirs(parent, exist_ok=True)
        return parent

    def dead_target(self, name="ghost"):
        """A path whose target AND parent are both absent."""
        return os.path.join(self.tmp, "gone_" + name, "MasterGroups", "x")


# ── the predicate ──────────────────────────────────────────────────────────

class TestStalePredicate(StaleTargetBase):

    def test_target_and_parent_missing_is_stale(self):
        self.write_lines(self.entry("clone:" + self.dead_target()))
        self.assertEqual(errors.stale_target_count(), 1)

    def test_existing_target_is_not_stale(self):
        live = self.live_dir()
        self.write_lines(self.entry("clone:" + live))
        self.assertEqual(errors.stale_target_count(), 0)

    def test_missing_target_but_live_parent_is_not_stale(self):
        """A repo folder deleted by hand inside a live tree keeps its failure."""
        parent = self.live_dir("tree")
        target = os.path.join(parent, "repo-that-was-removed")
        self.assertFalse(os.path.exists(target))
        self.write_lines(self.entry("clone:" + target))
        self.assertEqual(errors.stale_target_count(), 0,
                         "a failure inside a still-existing tree was called stale")

    def test_non_path_context_is_never_stale(self):
        """`api:groups/999/members/all` has no filesystem target at all."""
        for ctx in ("api:groups/999999999/members/all",
                    "api:projects", "startup", "config:MASTER_DIR"):
            with self.subTest(ctx=ctx):
                self.write_lines(self.entry(ctx))
                self.assertEqual(errors.stale_target_count(), 0)

    def test_root_and_topmost_targets_are_never_stale(self):
        for ctx in ("clone:/", "clone:/definitely-not-here"):
            with self.subTest(ctx=ctx):
                self.write_lines(self.entry(ctx))
                self.assertEqual(errors.stale_target_count(), 0)

    def test_dangling_symlink_counts_as_present(self):
        parent = self.live_dir("symtree")
        target = os.path.join(parent, "link")
        try:
            os.symlink(os.path.join(self.tmp, "nowhere"), target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.write_lines(self.entry("clone:" + target))
        self.assertEqual(errors.stale_target_count(), 0)

    def test_opaque_line_is_never_a_candidate(self):
        self.write_lines("this is not json at all",
                         '{"date": "nope", "context": "clone:/gone/x/y"}',
                         '["a", "list", "not", "a", "dict"]')
        self.assertEqual(errors.stale_target_count(), 0)
        self.assertEqual(len(errors._read_records()), 3)

    def test_tmp_path_alone_is_not_the_rule(self):
        """A REAL failure cloning into /tmp must survive — no /tmp heuristic."""
        real = tempfile.mkdtemp(prefix="gitlabcli_w83_realclone_")
        try:
            target = os.path.join(real, "MasterGroups", "grp", "proj")
            os.makedirs(target)
            self.write_lines(self.entry("clone:" + target))
            self.assertEqual(errors.stale_target_count(), 0)
            self.assertTrue(target.startswith(tempfile.gettempdir()))
        finally:
            shutil.rmtree(real, ignore_errors=True)


# ── counting never writes ──────────────────────────────────────────────────

class TestCountingIsPure(StaleTargetBase):

    def setUp(self):
        super().setUp()
        self.write_lines(
            self.entry("clone:" + self.dead_target("a")),
            self.entry("clone:" + self.dead_target("b")),
            self.entry("clone:" + self.live_dir("keep")),
            "raw unparseable line",
        )

    def test_badge_and_stale_count_do_not_touch_the_file(self):
        before = (_digest(self.log), os.stat(self.log).st_mode & 0o777,
                  len(self.raw_lines()))
        for _ in range(3):
            errors.unresolved_count()
            errors.stale_target_count()
            errors.stale_target_records()
            errors._read_errors()
            errors._read_records()
        after = (_digest(self.log), os.stat(self.log).st_mode & 0o777,
                 len(self.raw_lines()))
        self.assertEqual(before, after,
                         "a counting/reading path modified the log — this is the "
                         "exact defect (read rewrites file) that must not return")

    def test_mtime_is_not_even_bumped(self):
        st_before = os.stat(self.log)
        errors.stale_target_count()
        errors.unresolved_count()
        st_after = os.stat(self.log)
        self.assertEqual(st_before.st_mtime_ns, st_after.st_mtime_ns)
        self.assertEqual(st_before.st_size, st_after.st_size)


# ── the removal ────────────────────────────────────────────────────────────

class TestRemoveStaleTargets(StaleTargetBase):

    def setUp(self):
        super().setUp()
        self.live = self.live_dir("keep")
        self.keep_entry = self.entry("clone:" + self.live)
        self.keep_api = self.entry("api:groups/999999999/members/all")
        self.opaque = "not json — the most important line in the file"
        self.write_lines(
            self.entry("clone:" + self.dead_target("a")),
            self.opaque,
            self.entry("clone:" + self.dead_target("b")),
            self.keep_entry,
            self.keep_api,
        )

    def test_removes_only_the_stale_records(self):
        cands = errors.stale_target_records()
        self.assertEqual(len(cands), 2)
        dropped, backup = errors.remove_stale_targets([r.key for r in cands])
        self.assertEqual(dropped, 2)

        lines = self.raw_lines()
        self.assertEqual(len(lines), 3)
        self.assertIn(self.opaque, lines, "an unparseable line was deleted")
        self.assertIn(json.dumps(self.keep_entry, ensure_ascii=False), lines)
        self.assertIn(json.dumps(self.keep_api, ensure_ascii=False), lines)
        self.assertEqual(errors.stale_target_count(), 0)
        self.assertEqual(errors.unresolved_count(), 2)

    def test_backup_is_written_with_the_full_pre_cleanup_content(self):
        before_lines = self.raw_lines()
        cands = errors.stale_target_records()
        dropped, backup = errors.remove_stale_targets([r.key for r in cands])
        self.assertTrue(backup and os.path.isfile(backup), backup)
        with open(backup, encoding="utf-8") as fh:
            restored = [ln for ln in fh.read().splitlines() if ln.strip()]
        self.assertEqual(restored, before_lines)
        self.assertEqual(os.stat(backup).st_mode & 0o777, 0o600)

    def test_mode_stays_0600_after_cleanup(self):
        cands = errors.stale_target_records()
        errors.remove_stale_targets([r.key for r in cands])
        self.assertEqual(os.stat(self.log).st_mode & 0o777, 0o600)

    def test_empty_key_set_is_a_no_op(self):
        before = _digest(self.log)
        self.assertEqual(errors.remove_stale_targets([]), (0, None))
        self.assertEqual(_digest(self.log), before)

    def test_keys_outside_the_scope_set_are_untouched(self):
        cands = errors.stale_target_records()
        dropped, _ = errors.remove_stale_targets([cands[0].key])
        self.assertEqual(dropped, 1)
        self.assertEqual(errors.stale_target_count(), 1,
                         "a record the screen never listed was removed")

    def test_a_key_is_a_nomination_not_an_authorisation(self):
        """Predicate is re-checked under the lock: a revived dir is kept."""
        cands = errors.stale_target_records()
        revived = errors._context_target(cands[0].entry["context"])
        os.makedirs(revived)                     # came back after the preview
        dropped, _ = errors.remove_stale_targets([r.key for r in cands])
        self.assertEqual(dropped, 1)
        self.assertIn(cands[0].raw, self.raw_lines(),
                      "a record whose directory reappeared was still deleted")

    def test_opaque_key_cannot_be_used_to_delete_it(self):
        opaque_rec = [r for r in errors._read_records() if r.entry is None]
        self.assertEqual(len(opaque_rec), 1)
        dropped, _ = errors.remove_stale_targets([opaque_rec[0].key])
        self.assertEqual(dropped, 0)
        self.assertIn(self.opaque, self.raw_lines())

    def test_concurrent_append_survives_the_cleanup(self):
        cands = errors.stale_target_records()
        # An entry that arrives after the preview was drawn.
        errors.log_error("clone:" + self.live, "later failure")
        dropped, _ = errors.remove_stale_targets([r.key for r in cands])
        self.assertEqual(dropped, 2)
        self.assertTrue(any("later failure" in ln for ln in self.raw_lines()),
                        "an entry appended after the preview was lost")

    def test_repeated_cleanup_is_idempotent(self):
        errors.remove_stale_targets([r.key for r in errors.stale_target_records()])
        digest = _digest(self.log)
        self.assertEqual(errors.remove_stale_targets(
            [r.key for r in errors.stale_target_records()]), (0, None))
        self.assertEqual(_digest(self.log), digest)


# ── the prompted screen ────────────────────────────────────────────────────

class TestCleanupPrompt(StaleTargetBase):

    def setUp(self):
        super().setUp()
        self.opaque = "opaque survivor"
        self.live = self.live_dir("keep")
        self.write_lines(
            self.entry("clone:" + self.dead_target("a")),
            self.entry("clone:" + self.dead_target("b")),
            self.opaque,
            self.entry("clone:" + self.live),
        )

    def _run_cmd(self, answer):
        import builtins
        saved = builtins.input
        builtins.input = lambda *a, **k: answer
        try:
            return errors.cmd_cleanup_stale({})
        finally:
            builtins.input = saved

    def test_declining_changes_nothing(self):
        before = _digest(self.log)
        for answer in ("", "no", "n", "y", "YES please", "130"):
            with self.subTest(answer=answer):
                self.assertEqual(self._run_cmd(answer), 0)
                self.assertEqual(_digest(self.log), before,
                                 f"answering {answer!r} modified the log")

    def test_confirming_with_yes_removes_the_candidates(self):
        self.assertEqual(self._run_cmd("yes"), 2)
        lines = self.raw_lines()
        self.assertEqual(len(lines), 2)
        self.assertIn(self.opaque, lines)
        self.assertEqual(errors.stale_target_count(), 0)

    def test_yes_is_case_and_whitespace_tolerant(self):
        self.assertEqual(self._run_cmd("  YES \n"), 2)

    def test_interrupt_at_the_prompt_changes_nothing(self):
        import builtins
        before = _digest(self.log)
        saved = builtins.input

        def boom(*a, **k):
            raise KeyboardInterrupt

        builtins.input = boom
        try:
            self.assertEqual(errors.cmd_cleanup_stale({}), 0)
        finally:
            builtins.input = saved
        self.assertEqual(_digest(self.log), before)

    def test_nothing_to_clean_is_a_no_op(self):
        self.write_lines(self.entry("clone:" + self.live), self.opaque)
        before = _digest(self.log)
        self.assertEqual(self._run_cmd("yes"), 0)
        self.assertEqual(_digest(self.log), before)


# ── badge truthfulness end-to-end ──────────────────────────────────────────

class TestBadgeBecomesTrustworthy(StaleTargetBase):

    def test_residue_shaped_log_collapses_to_the_real_failures(self):
        """A log shaped like the user's: 130 dead temp rows + 6 keepers."""
        live = self.live_dir("MasterGroups")
        real = os.path.join(live, "grp", "proj")
        os.makedirs(real)

        lines = []
        for i in range(130):
            lines.append(self.entry(
                "clone:%s/gitlabcli_test_%04d/MasterGroups/x"
                % (os.path.join(self.tmp, "vanished"), i)))
        lines.append(self.entry("clone:" + real))              # real failure
        lines.append(self.entry("api:groups/999999999/members/all"))
        lines.append("an unparseable line from an older version")
        self.write_lines(*lines)

        self.assertEqual(errors.unresolved_count(), 132)
        self.assertEqual(errors.stale_target_count(), 130)

        dropped = self._confirm_yes()
        self.assertEqual(dropped, 130)

        self.assertEqual(errors.unresolved_count(), 2)
        self.assertEqual(errors.stale_target_count(), 0)
        self.assertEqual(len(errors._read_records()), 3)
        self.assertTrue(any(r.entry is None for r in errors._read_records()),
                        "the unparseable line did not survive")

    def _confirm_yes(self):
        import builtins
        saved = builtins.input
        builtins.input = lambda *a, **k: "yes"
        try:
            return errors.cmd_cleanup_stale({})
        finally:
            builtins.input = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
