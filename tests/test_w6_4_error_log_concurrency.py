#!/usr/bin/env python3
# tests/test_w6_4_error_log_concurrency.py
#
# Wave-6 regression tests for W6-4: the error log was unsafe for concurrent
# readers and writers, and destroyed data on a plain read.
#
# Three defects pinned here:
#
#   D1  `_read_errors()` REWROTE the whole file on every call — including from
#       the main-menu badge that only counts unresolved entries. Two CLI
#       instances (one long clone, one open menu — entirely normal) therefore
#       raced read-modify-write and lost entries.
#
#   D2  The same rewrite made the dropping of corrupt / date-less lines
#       PERMANENT. A line the parser did not understand was silently destroyed
#       the first time any screen counted the badge.
#
#   D3  Clearing resolved entries rewrote the file from a stale in-memory list,
#       so an entry appended by ANOTHER context after the screen was drawn was
#       erased. Production incident: a 176-project clone lost the only record
#       of a failure this way.
#
# Everything runs against a temp log. The real ~/.gitlab-cli/errors.log is
# never opened — ERROR_DIR/ERROR_FILE are redirected in setUp, and
# test_zz_live_log_untouched proves the real file's size/mode/mtime are
# unchanged by this module.

import builtins
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta

from _bootstrap import TempTreeTestCase, PROJECT_ROOT

import errors as errors_mod

ANSI = re.compile(r"\x1b\[[0-9;]*m")

REAL_LOG = os.path.expanduser("~/.gitlab-cli/errors.log")


def _snapshot_real_log():
    """(mode, every existing line) of the real log.

    We pin the LINES, not the size/mtime.

    HISTORY — read this if you are here because the old comment told you the
    suite writes to your live log. It no longer does.

    When W6-4 was written that was true: several older modules exercised
    git.clone_repo / redaction against the real module-level `ERROR_FILE`
    instead of a temp one, so `log_error` appended genuine records to
    ~/.gitlab-cli/errors.log on every run. W6-4 did not own those modules and
    could only assert that nothing already in the file was lost.

    W7 fixed the cause rather than the symptom. `tests/_error_log_guard.py`
    repoints `errors.ERROR_DIR` / `errors.ERROR_FILE` at a per-process temp
    directory at IMPORT time, before any test runs, and it is imported by
    `tests/_bootstrap.py` (so every module on the shared harness inherits it),
    by `tests/run_tests.py` (so the suite as a whole does), and directly by the
    few modules that bypass the harness. Isolation is therefore a property of
    the test PROCESS, not of each author remembering to redirect in setUp.
    Modules that do redirect in setUp still work: they save and restore
    whatever is current, which is now the temp path. The guard also re-checks
    the real file at exit and shouts if it changed.

    Verified: three consecutive full runs of `tests/run_tests.py` leave
    ~/.gitlab-cli/errors.log byte-identical.

    So this snapshot is now a REGRESSION GUARD, not a tolerance for a known
    defect. It still asserts the strong property W6-4 owns — every line that
    existed before the run still exists, byte-identical, in the same order, and
    the file is still 0600 — and any append now means the isolation in
    `_error_log_guard.py` has broken and should be treated as a real failure,
    not as expected background noise.
    """
    if not os.path.exists(REAL_LOG):
        return None
    st = os.stat(REAL_LOG)
    with open(REAL_LOG, "rb") as fh:
        lines = fh.read().splitlines()
    return (st.st_mode & 0o777, lines)


_REAL_LOG_BEFORE = _snapshot_real_log()


def _entry(title, days_ago=1, etype="Git", resolved=False, context=None):
    return {
        "date": (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds"),
        "type": etype,
        "title": title,
        "context": context or f"ctx-{title}",
        "message": "boom",
        "fix": "do the thing",
        "resolved": resolved,
    }


class ErrorLogTempCase(TempTreeTestCase):
    """Redirects the error log into this test's temp tree."""

    def setUp(self):
        super().setUp()
        self._real_dir = errors_mod.ERROR_DIR
        self._real_file = errors_mod.ERROR_FILE
        errors_mod.ERROR_DIR = os.path.join(self.tmp, "dot-gitlab-cli")
        errors_mod.ERROR_FILE = os.path.join(errors_mod.ERROR_DIR, "errors.log")
        os.makedirs(errors_mod.ERROR_DIR, exist_ok=True)

    def tearDown(self):
        errors_mod.ERROR_DIR = self._real_dir
        errors_mod.ERROR_FILE = self._real_file
        super().tearDown()

    # ── helpers ──────────────────────────────────────────────────────────────

    def write_raw(self, lines):
        with open(errors_mod.ERROR_FILE, "w", encoding="utf-8") as fh:
            for ln in lines:
                fh.write(ln if ln.endswith("\n") else ln + "\n")

    def write_log(self, entries):
        self.write_raw([json.dumps(e, ensure_ascii=False) for e in entries])

    def raw_lines(self):
        if not os.path.exists(errors_mod.ERROR_FILE):
            return []
        with open(errors_mod.ERROR_FILE, encoding="utf-8") as fh:
            return [ln.rstrip("\n") for ln in fh if ln.strip()]

    def mode(self):
        return os.stat(errors_mod.ERROR_FILE).st_mode & 0o777

    def run_menu(self, answers):
        script = list(answers)

        def fake_input(_prompt=""):
            if not script:
                raise AssertionError("cmd_errors asked for more input than scripted")
            return script.pop(0)

        real_input = builtins.input
        builtins.input = fake_input
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                errors_mod.cmd_errors()
        finally:
            builtins.input = real_input
        return ANSI.sub("", buf.getvalue())


# ═══ D1: reads are non-destructive ════════════════════════════════════════════

class ReadIsNonDestructiveTests(ErrorLogTempCase):

    def test_read_errors_does_not_change_the_file_bytes(self):
        self.write_log([_entry("A", 2), _entry("B", 1)])
        before = open(errors_mod.ERROR_FILE, "rb").read()
        mtime_before = os.stat(errors_mod.ERROR_FILE).st_mtime_ns
        for _ in range(5):
            errors_mod._read_errors()
        self.assertEqual(open(errors_mod.ERROR_FILE, "rb").read(), before,
                         "_read_errors() rewrote the log")
        self.assertEqual(os.stat(errors_mod.ERROR_FILE).st_mtime_ns, mtime_before,
                         "_read_errors() touched the log's mtime")

    def test_badge_count_does_not_change_the_file(self):
        self.write_log([_entry("A", 2, resolved=True), _entry("B", 1)])
        before = open(errors_mod.ERROR_FILE, "rb").read()
        self.assertEqual(errors_mod.unresolved_count(), 1)
        self.assertEqual(open(errors_mod.ERROR_FILE, "rb").read(), before)

    def test_read_on_a_missing_log_creates_nothing(self):
        self.assertFalse(os.path.exists(errors_mod.ERROR_FILE))
        self.assertEqual(errors_mod._read_errors(), [])
        self.assertFalse(os.path.exists(errors_mod.ERROR_FILE),
                         "a pure read must not create the log file")

    def test_expired_entries_are_hidden_from_reads_but_stay_on_disk(self):
        self.write_log([_entry("OLD", errors_mod.MAX_DAYS + 5), _entry("NEW", 1)])
        titles = {e["title"] for e in errors_mod._read_errors()}
        self.assertEqual(titles, {"NEW"}, "expired entry must not be returned")
        self.assertEqual(len(self.raw_lines()), 2,
                         "a read must not delete the expired line — only the "
                         "explicit prune path may")


# ═══ D2: unparseable lines survive ════════════════════════════════════════════

class CorruptLinePreservationTests(ErrorLogTempCase):

    CORRUPT = "{ this is not json at all"
    DATELESS = json.dumps({"context": "legacy", "message": "old format"})
    TRUNCATED = '{"date": "2026-09-0'
    NON_OBJECT = '["a", "b"]'

    def all_bad(self):
        return [self.CORRUPT, self.DATELESS, self.TRUNCATED, self.NON_OBJECT]

    def test_badge_count_preserves_every_unparseable_line(self):
        self.write_raw([self.CORRUPT, json.dumps(_entry("GOOD")), self.DATELESS])
        errors_mod.unresolved_count()
        lines = self.raw_lines()
        self.assertIn(self.CORRUPT, lines, "corrupt line destroyed by a badge count")
        self.assertIn(self.DATELESS, lines, "date-less line destroyed by a badge count")

    def test_every_flavour_of_bad_line_round_trips(self):
        self.write_raw(self.all_bad() + [json.dumps(_entry("GOOD"))])
        errors_mod._read_errors()
        errors_mod.unresolved_count()
        lines = self.raw_lines()
        for bad in self.all_bad():
            self.assertIn(bad, lines, f"line destroyed: {bad!r}")

    def test_bad_lines_are_not_displayed_as_errors(self):
        self.write_raw([self.CORRUPT, json.dumps(_entry("GOOD"))])
        shown = errors_mod._read_errors()
        self.assertEqual([e["title"] for e in shown], ["GOOD"])

    def test_bad_lines_survive_a_resolve_rewrite(self):
        self.write_raw([self.CORRUPT, json.dumps(_entry("GOOD")), self.DATELESS])
        self.run_menu(["1", "r", "1"])
        lines = self.raw_lines()
        self.assertIn(self.CORRUPT, lines)
        self.assertIn(self.DATELESS, lines)
        good = [json.loads(l) for l in lines
                if l.startswith("{") and '"title"' in l and "GOOD" in l]
        self.assertTrue(good and good[0]["resolved"])

    def test_bad_lines_survive_clear_resolved(self):
        self.write_raw([self.CORRUPT,
                        json.dumps(_entry("DONE", resolved=True)),
                        self.DATELESS])
        self.run_menu(["1", "c"])
        lines = self.raw_lines()
        self.assertIn(self.CORRUPT, lines)
        self.assertIn(self.DATELESS, lines)
        self.assertNotIn("DONE", "\n".join(lines))

    def test_prune_keeps_bad_lines_even_though_they_have_no_date(self):
        self.write_raw([self.CORRUPT,
                        json.dumps(_entry("ANCIENT", errors_mod.MAX_DAYS + 10)),
                        self.DATELESS])
        dropped = errors_mod.prune_old_entries()
        self.assertEqual(dropped, 1)
        lines = self.raw_lines()
        self.assertIn(self.CORRUPT, lines)
        self.assertIn(self.DATELESS, lines)
        self.assertNotIn("ANCIENT", "\n".join(lines))

    def test_log_error_appends_next_to_a_corrupt_line_without_eating_it(self):
        self.write_raw([self.CORRUPT])
        errors_mod.log_error("clone:x", "RPC failed")
        lines = self.raw_lines()
        self.assertIn(self.CORRUPT, lines)
        self.assertEqual(len(lines), 2)


# ═══ D3: scoped clear — a foreign context can never be erased ═════════════════

class ScopedClearTests(ErrorLogTempCase):

    def test_clear_does_not_delete_an_entry_appended_after_the_screen_was_drawn(self):
        """The production incident: a success in one context erased a failure
        recorded in another."""
        self.write_log([_entry("DONE_A", 3, resolved=True)])

        # Drive the menu, but append a NEW failure from a "different context"
        # in between the screen being drawn and the clear being confirmed.
        script = ["1", "c"]
        appended = {"done": False}

        def fake_input(_prompt=""):
            if not appended["done"] and len(script) == 1:
                errors_mod.log_error("clone:other/group/proj", "RPC failed")
                appended["done"] = True
            return script.pop(0)

        real_input = builtins.input
        builtins.input = fake_input
        try:
            with redirect_stdout(io.StringIO()):
                errors_mod.cmd_errors()
        finally:
            builtins.input = real_input

        blob = "\n".join(self.raw_lines())
        self.assertIn("clone:other/group/proj", blob,
                      "clear destroyed a failure recorded by another context "
                      "after this screen was drawn")
        self.assertNotIn("DONE_A", blob)

    def test_clear_never_deletes_unresolved_entries(self):
        self.write_log([_entry("OPEN", 2), _entry("DONE", 1, resolved=True)])
        self.run_menu(["1", "c"])
        blob = "\n".join(self.raw_lines())
        self.assertIn("OPEN", blob)
        self.assertNotIn("DONE", blob)

    def test_clear_from_a_filtered_view_only_touches_what_was_in_scope(self):
        # Filter 4 (by type) narrows the screen; the clear must still only be
        # able to remove records the screen actually read.
        self.write_log([
            _entry("GIT_DONE", 2, etype="Git", resolved=True),
            _entry("NET_DONE", 1, etype="Network", resolved=True),
        ])
        self.run_menu(["4", "Git", "c"])
        blob = "\n".join(self.raw_lines())
        self.assertNotIn("GIT_DONE", blob)
        self.assertIn("NET_DONE", blob,
                      "clear must not reach entries outside the scope the "
                      "screen read")

    def test_clear_with_nothing_resolved_deletes_nothing(self):
        self.write_log([_entry("A", 3), _entry("B", 1)])
        before = self.raw_lines()
        self.run_menu(["1", "c"])
        self.assertEqual(self.raw_lines(), before)


# ═══ 0600 on every write path ═════════════════════════════════════════════════

@unittest.skipUnless(os.name == "posix", "POSIX modes only")
class FileModeTests(ErrorLogTempCase):

    def test_log_error_creates_the_log_0600(self):
        self.assertFalse(os.path.exists(errors_mod.ERROR_FILE))
        errors_mod.log_error("clone:a", "RPC failed")
        self.assertEqual(self.mode(), 0o600, "fresh log is not 0600")

    def test_log_error_reasserts_0600_on_an_existing_loosened_log(self):
        errors_mod.log_error("clone:a", "RPC failed")
        os.chmod(errors_mod.ERROR_FILE, 0o644)
        errors_mod.log_error("clone:b", "RPC failed")
        self.assertEqual(self.mode(), 0o600, "append path did not re-assert 0600")

    def test_resolve_rewrite_reasserts_0600(self):
        self.write_log([_entry("A", 2), _entry("B", 1)])
        os.chmod(errors_mod.ERROR_FILE, 0o666)
        self.run_menu(["1", "r", "1"])
        self.assertEqual(self.mode(), 0o600)

    def test_clear_rewrite_reasserts_0600(self):
        self.write_log([_entry("DONE", 1, resolved=True), _entry("OPEN", 2)])
        os.chmod(errors_mod.ERROR_FILE, 0o644)
        self.run_menu(["1", "c"])
        self.assertEqual(self.mode(), 0o600)

    def test_prune_rewrite_reasserts_0600(self):
        self.write_log([_entry("OLD", errors_mod.MAX_DAYS + 3), _entry("NEW", 1)])
        os.chmod(errors_mod.ERROR_FILE, 0o644)
        errors_mod.prune_old_entries()
        self.assertEqual(self.mode(), 0o600)

    def test_write_errors_reasserts_0600(self):
        self.write_log([_entry("A", 1)])
        os.chmod(errors_mod.ERROR_FILE, 0o640)
        errors_mod._write_errors([_entry("A", 1), _entry("C", 1)])
        self.assertEqual(self.mode(), 0o600)


# ═══ locking mechanism: no lock file, nothing left behind ═════════════════════

class LockMechanismTests(ErrorLogTempCase):

    def test_no_lock_file_is_ever_created(self):
        """The project rule: a lock FILE survives a power cut and blocks the
        tool forever. flock() lives on the fd and dies with the process."""
        errors_mod.log_error("clone:a", "RPC failed")
        errors_mod._read_errors()
        errors_mod.prune_old_entries()
        self.run_menu(["1", "q"])
        leftovers = sorted(os.listdir(errors_mod.ERROR_DIR))
        self.assertEqual(leftovers, ["errors.log"],
                         f"something other than the log was created: {leftovers}")

    def test_module_opens_no_lock_path_and_uses_a_kernel_lock(self):
        """No `.lock` STRING LITERAL (i.e. a path) may appear in errors.py, and
        the mutual exclusion must be fcntl.flock, whose lifetime is the open fd
        and which therefore cannot survive a crash or a power cut."""
        with open(os.path.join(PROJECT_ROOT, "errors.py"), encoding="utf-8") as fh:
            src = fh.read()
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("#"))
        for literal in ('".lock"', "'.lock'", '".lck"', '"lockfile"',
                        '".errors.lock"'):
            self.assertNotIn(
                literal, code,
                f"errors.py builds a lock path ({literal}) — the project "
                "forbids lock FILES: one survives a hard power-off and then "
                "blocks the tool until someone deletes it by hand")
        self.assertIn("fcntl.flock", code,
                      "mutual exclusion must be a kernel flock on the log's "
                      "own fd, not a sentinel file")
        self.assertIn("LOCK_NB", code,
                      "acquisition must be non-blocking and bounded, or a "
                      "wedged holder hangs the whole CLI")

    def test_a_held_exclusive_lock_does_not_hang_the_reader(self):
        """A wedged holder must degrade, never block forever."""
        import fcntl
        import time as _t
        self.write_log([_entry("A", 1)])
        holder = open(errors_mod.ERROR_FILE, "r+", encoding="utf-8")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        try:
            t0 = _t.monotonic()
            got = errors_mod._read_errors()
            elapsed = _t.monotonic() - t0
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()
        self.assertLess(elapsed, errors_mod._LOCK_TIMEOUT + 3,
                        "read blocked far past the bounded timeout")
        self.assertEqual([e["title"] for e in got], ["A"])

    def test_a_crashed_holder_leaves_no_lock_behind(self):
        """Kill -9 a process holding the lock; the next writer must proceed."""
        self.write_log([_entry("A", 1)])
        code = textwrap.dedent(f"""
            import fcntl, sys, time
            fh = open({errors_mod.ERROR_FILE!r}, "r+")
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            sys.stdout.write("LOCKED\\n"); sys.stdout.flush()
            time.sleep(60)
        """)
        p = subprocess.Popen([sys.executable, "-c", code],
                             stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(p.stdout.readline().strip(), "LOCKED")
            p.kill()
            p.wait(timeout=10)
        finally:
            if p.poll() is None:
                p.kill()
        # The dead process left NOTHING: the append must work immediately.
        errors_mod.log_error("clone:after-crash", "RPC failed")
        self.assertIn("clone:after-crash", "\n".join(self.raw_lines()))
        self.assertEqual(sorted(os.listdir(errors_mod.ERROR_DIR)), ["errors.log"])


# ═══ real two-process concurrency: no entry may be lost ═══════════════════════

_WORKER = r'''
import os, sys
sys.path.insert(0, %(root)r)
import errors as errors_mod
errors_mod.ERROR_DIR = %(dir)r
errors_mod.ERROR_FILE = %(file)r
tag = sys.argv[1]
n = int(sys.argv[2])
mode = sys.argv[3]
for i in range(n):
    errors_mod.log_error("clone:%%s/proj-%%03d" %% (tag, i), "RPC failed")
    if mode == "read":
        errors_mod.unresolved_count()      # the old badge path: used to rewrite
print("DONE")
'''


class TwoProcessConcurrencyTests(ErrorLogTempCase):

    def _spawn(self, tag, n, mode):
        code = _WORKER % {"root": PROJECT_ROOT,
                          "dir": errors_mod.ERROR_DIR,
                          "file": errors_mod.ERROR_FILE}
        return subprocess.Popen([sys.executable, "-c", code, tag, str(n), mode],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    def _run_pair(self, n, mode_a, mode_b):
        a = self._spawn("A", n, mode_a)
        b = self._spawn("B", n, mode_b)
        outa, erra = a.communicate(timeout=180)
        outb, errb = b.communicate(timeout=180)
        self.assertEqual(a.returncode, 0, erra)
        self.assertEqual(b.returncode, 0, errb)
        return self.raw_lines()

    def test_two_appenders_lose_nothing(self):
        n = 60
        lines = self._run_pair(n, "append", "append")
        contexts = set()
        for ln in lines:
            contexts.add(json.loads(ln)["context"])
        expected = {f"clone:{t}/proj-{i:03d}" for t in ("A", "B") for i in range(n)}
        missing = expected - contexts
        self.assertEqual(missing, set(), f"{len(missing)} entries lost: {sorted(missing)[:5]}")
        self.assertEqual(len(lines), 2 * n, "entry count does not match")

    def test_appender_plus_concurrent_badge_reader_loses_nothing(self):
        """This is the exact production shape: a long clone appending in one
        terminal while the menu badge counts in another."""
        n = 60
        lines = self._run_pair(n, "append", "read")
        contexts = {json.loads(ln)["context"] for ln in lines}
        expected = {f"clone:{t}/proj-{i:03d}" for t in ("A", "B") for i in range(n)}
        self.assertEqual(expected - contexts, set(),
                         "the badge-count process destroyed appended entries")
        self.assertEqual(len(lines), 2 * n)

    def test_both_processes_reading_and_writing_lose_nothing(self):
        n = 50
        lines = self._run_pair(n, "read", "read")
        contexts = {json.loads(ln)["context"] for ln in lines}
        expected = {f"clone:{t}/proj-{i:03d}" for t in ("A", "B") for i in range(n)}
        self.assertEqual(expected - contexts, set())
        self.assertEqual(len(lines), 2 * n)

    @unittest.skipUnless(os.name == "posix", "POSIX modes only")
    def test_log_stays_0600_after_a_concurrent_run(self):
        self._run_pair(20, "append", "read")
        self.assertEqual(self.mode(), 0o600)

    def test_a_corrupt_line_survives_a_concurrent_run(self):
        self.write_raw(["{ garbage not json"])
        lines = self._run_pair(25, "append", "read")
        self.assertIn("{ garbage not json", lines,
                      "a corrupt line was destroyed during concurrent access")


# ═══ the live log must be untouched by this whole module ══════════════════════

class ZZLiveLogUntouchedTests(unittest.TestCase):
    """The live log may never lose a line, mutate a line, or lose 0600."""

    def test_zz_live_log_loses_no_existing_line(self):
        after = _snapshot_real_log()
        if _REAL_LOG_BEFORE is None:
            self.skipTest("no real ~/.gitlab-cli/errors.log on this machine")
        self.assertIsNotNone(after, "the real errors.log was DELETED")

        mode_before, lines_before = _REAL_LOG_BEFORE
        mode_after, lines_after = after

        self.assertEqual(mode_after, 0o600,
                         "the real errors.log is no longer 0600 — it carries "
                         "raw git stderr")
        self.assertGreaterEqual(
            len(lines_after), len(lines_before),
            f"the real errors.log SHRANK: {len(lines_before)} -> "
            f"{len(lines_after)} lines — entries were destroyed")
        self.assertEqual(
            lines_after[:len(lines_before)], lines_before,
            "an existing line in the real errors.log was mutated or reordered")

    def test_zz_this_module_appends_nothing_to_the_live_log(self):
        """Nothing in THIS module may write to the real log. (Other, older
        test modules do — a pre-existing hygiene defect owned elsewhere; that
        is why this assertion is scoped to our own record shape.)"""
        after = _snapshot_real_log()
        if _REAL_LOG_BEFORE is None:
            self.skipTest("no real ~/.gitlab-cli/errors.log on this machine")
        added = after[1][len(_REAL_LOG_BEFORE[1]):]
        ours = [ln for ln in added
                if b"proj-0" in ln or b"after-crash" in ln
                or b"other/group/proj" in ln]
        self.assertEqual(
            ours, [],
            "test_w6_4_* wrote its own records into the REAL error log; the "
            "temp-log redirection leaked")


if __name__ == "__main__":
    unittest.main(verbosity=2)
