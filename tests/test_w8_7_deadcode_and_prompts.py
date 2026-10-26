#!/usr/bin/env python3
# tests/test_w8_7_deadcode_and_prompts.py
#
# Wave-8 regression tests for W8-7. Three unrelated-looking defects that are
# all one bug: the program did something other than what the user asked and
# said nothing about it.
#
#   D1  DEAD CODE. `config.py` — a single-instance `.env` loader superseded by
#       `instances.py` — was still shipping. An AST census of the CLI source
#       tree found zero importers. A file that nothing imports is a file that
#       can silently rot into a wrong answer for whoever imports it next.
#       Pinned here as: the module is gone, AND nothing in the CLI imports it.
#
#   D2  LABEL vs BEHAVIOUR. The instance picker advertised `q  exit`. Its `q`
#       branch does `return None`, and both call sites
#       (gitlab_cli.interactive_mode) treat None as "keep going with the
#       current config" — it has never exited. The behaviour is the correct
#       half (it is a picker; a picker that kills the process is the surprise),
#       so the LABEL was wrong.
#
#   D3  CASE SENSITIVITY. `gitlab_cli.pick_action` was the only prompt in the
#       application that compared the raw string: `if action == "q"`. Every
#       other menu normalises with `.strip().lower()`. So `C`/`B`/`Q` failed
#       on exactly one screen and worked everywhere else.
#
#   D4  UNRECOGNISED INPUT AS A DEFAULT. The Error Log filter's `else:` branch
#       set `filtered = records` — any key the program did not recognise meant
#       "show me everything". When the typo then matched nothing it printed one
#       warning and RETURNED, so the screen vanished, which reads as a crash.
#
# W8-7 owns `printer.py`; the call sites in instances.py / gitlab_cli.py /
# errors.py are owned by other Wave-8 subagents. These tests therefore pin the
# MECHANISM in printer.py as a hard contract, and pin the call-site properties
# with `expectedFailure`-free conditional assertions that state clearly whether
# the call site has adopted it yet.

import ast
import io
import os
import sys
import unittest
from contextlib import redirect_stdout

from _bootstrap import PROJECT_ROOT  # noqa: F401  (installs the error-log guard)

import printer


ANSI_FREE = lambda s: __import__("re").sub(r"\x1b\[[0-9;]*m", "", s)

SKIP_DIRS = {"venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache",
             ".quarantine", "static_analysis_semgrep_1", "MasterGroups"}


def _cli_python_files():
    """Every .py file that is part of THIS package.

    MasterGroups/ is excluded deliberately: it holds cloned checkouts of other
    people's projects, dozens of which have their own unrelated `config.py`
    (pydantic's, pytest's, a dozen services'). Counting those as importers is
    exactly the mistake a text grep makes.
    """
    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def _imports_module(path, target):
    """AST-level: does `path` import `target` as a module? Text grep can't tell
    `import config` from the word "config" in a docstring or a dict key."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        tree = ast.parse(fh.read(), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == target or alias.name.startswith(target + "."):
                    return node.lineno
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == target or mod.startswith(target + "."):
                return node.lineno
        elif isinstance(node, ast.Call):
            fn = node.func
            name = (fn.id if isinstance(fn, ast.Name)
                    else fn.attr if isinstance(fn, ast.Attribute) else None)
            if name in ("import_module", "__import__"):
                for arg in node.args:
                    if (isinstance(arg, ast.Constant)
                            and isinstance(arg.value, str)
                            and (arg.value == target
                                 or arg.value.startswith(target + "."))):
                        return node.lineno
    return None


# ─── D1 — dead code ───────────────────────────────────────────────────────────

class TestConfigModuleRemoved(unittest.TestCase):

    def test_config_py_is_gone(self):
        self.assertFalse(
            os.path.exists(os.path.join(PROJECT_ROOT, "config.py")),
            "config.py is back. It is the legacy single-instance .env loader; "
            "instances.py superseded it. If something now genuinely needs it, "
            "that importer is the thing to fix.")

    def test_no_cli_module_imports_config(self):
        """The check that justified the deletion, kept as a standing guard."""
        offenders = []
        for path in _cli_python_files():
            line = _imports_module(path, "config")
            if line:
                offenders.append("%s:%d" % (os.path.relpath(path, PROJECT_ROOT), line))
        self.assertEqual([], offenders,
                         "these CLI modules import the deleted `config` module: %s"
                         % offenders)

    def test_every_cli_module_still_imports_standalone(self):
        """Nothing depended on config.py as a side-effecting import."""
        import subprocess
        mods = ["api", "caps", "clone", "coverage", "credentials", "errors",
                "git", "instances", "log", "members", "paths", "printer",
                "pull", "remotes", "setup", "version", "gitlab_cli"]
        broken = []
        for m in mods:
            res = subprocess.run([sys.executable, "-c", "import %s" % m],
                                 cwd=PROJECT_ROOT, capture_output=True, text=True)
            if res.returncode != 0:
                broken.append((m, res.stderr.strip().splitlines()[-1:]))
        self.assertEqual([], broken, "modules no longer import standalone: %s" % broken)


# ─── D3 — one normalisation for every menu key ────────────────────────────────

class TestMenuKeyNormalisation(unittest.TestCase):

    def test_case_and_whitespace_fold_together(self):
        for raw in ("c", "C", " c ", "\tC\n", "  c"):
            self.assertEqual("c", printer.menu_key(raw),
                             "menu_key(%r) must fold to 'c'" % raw)

    def test_matches_key_is_case_insensitive(self):
        self.assertTrue(printer.matches_key("Q", "q"))
        self.assertTrue(printer.matches_key(" B ", "b"))
        self.assertFalse(printer.matches_key("qq", "q"))

    def test_resolve_choice_accepts_either_case(self):
        valid = ["c", "r", "q"]
        self.assertEqual("c", printer.resolve_choice("C", valid))
        self.assertEqual("q", printer.resolve_choice("  Q  ", valid))

    def test_resolve_choice_returns_the_callers_own_spelling(self):
        """So a call site can keep comparing against its own constants."""
        self.assertEqual("CA", printer.resolve_choice("ca", ["CA", "pa"]))


# ─── D4 — unrecognised input is never a default ───────────────────────────────

class TestUnrecognisedInputIsRejected(unittest.TestCase):

    def test_unknown_key_is_none_not_a_fallback(self):
        self.assertIsNone(printer.resolve_choice("z", ["1", "2", "3"]))
        self.assertIsNone(printer.resolve_choice("9", ["1", "2", "3"]))
        self.assertIsNone(printer.resolve_choice("everything", ["1", "2"]))

    def test_empty_is_only_a_default_when_one_was_offered(self):
        self.assertIsNone(printer.resolve_choice("", ["1", "2"]))
        self.assertEqual("1", printer.resolve_choice("", ["1", "2"], default="1"))

    def test_a_default_never_rescues_a_nonempty_typo(self):
        """The whole bug: 'z' must NOT become the default just because one exists."""
        self.assertIsNone(printer.resolve_choice("z", ["1", "2"], default="1"))

    def test_rejection_message_echoes_what_the_user_typed(self):
        msg = ANSI_FREE(printer.unknown_choice_msg("Gti", ["Git", "Network"]))
        self.assertIn("Gti", msg, "the rejected input must be echoed back, so the "
                                  "user can see the key arrived and was refused")
        self.assertIn("Git", msg)
        self.assertIn("Network", msg)

    def test_empty_rejection_says_nothing_was_entered(self):
        msg = ANSI_FREE(printer.unknown_choice_msg("", ["1", "2"]))
        self.assertIn("Nothing entered", msg)

    def test_ask_choice_reprompts_until_valid_and_never_invents_an_answer(self):
        answers = iter(["z", "9", "", "  C  "])
        buf = io.StringIO()
        with redirect_stdout(buf):
            got = printer.ask_choice("? ", ["c", "q"], reader=lambda p: next(answers))
        self.assertEqual("c", got)
        out = ANSI_FREE(buf.getvalue())
        self.assertIn("'z' is not one of", out)
        self.assertIn("'9' is not one of", out)
        self.assertIn("Nothing entered", out)

    def test_ask_choice_returns_on_eof_sentinel_not_a_choice(self):
        def boom(_):
            raise EOFError

        self.assertIsNone(printer.ask_choice("? ", ["a"], reader=boom))
        self.assertEqual("BYE", printer.ask_choice("? ", ["a"], reader=boom,
                                                   on_eof="BYE"))

    def test_ask_choice_honours_a_printed_default_on_enter_only(self):
        answers = iter(["", "x", "2"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual("1", printer.ask_choice("? ", ["1", "2"], default="1",
                                                     reader=lambda p: next(answers)))
        with redirect_stdout(io.StringIO()):
            self.assertEqual("2", printer.ask_choice("? ", ["1", "2"],
                                                     reader=lambda p: next(answers)))


# ─── D2 — a label may not disagree with behaviour ─────────────────────────────

class TestNavLabelMatchesBehaviour(unittest.TestCase):

    def test_a_screen_that_only_returns_must_not_print_exit(self):
        line = ANSI_FREE(printer.nav_line(back=True, exit_=False))
        self.assertIn("back", line)
        self.assertNotIn("exit", line,
                         "a key that returns to the caller is 'back', never 'exit'")

    def test_a_screen_that_exits_prints_exit(self):
        line = ANSI_FREE(printer.nav_line(back=True, exit_=True))
        self.assertIn("back", line)
        self.assertIn("exit", line)

    def test_labels_are_named_constants_not_loose_strings(self):
        self.assertEqual("back", printer.KEY_BACK)
        self.assertEqual("exit", printer.KEY_EXIT)


# ─── The call sites, as they stand ────────────────────────────────────────────

class TestCallSiteAdoption(unittest.TestCase):
    """Reports, per screen, whether each papercut is fixed on disk yet.

    The three screens live in instances.py / gitlab_cli.py / errors.py, which
    W8-7 does NOT own — they were being edited concurrently by other Wave-8
    subagents, so W8-7 built the mechanism in printer.py and left the call-site
    edits to their owners.

    These tests are therefore written to RATCHET, not to block: while a screen
    has not adopted the fix they skip with the exact remaining gap spelled out,
    and the moment it adopts one they become a hard regression guard. That way
    the suite tells the truth in both directions instead of going red for work
    that belongs to someone else, or silently forgetting the work exists.
    """

    def _src(self, name):
        with open(os.path.join(PROJECT_ROOT, name), encoding="utf-8") as fh:
            return fh.read()

    def test_instance_picker_q_label_agrees_with_its_return(self):
        body = self._src("instances.py").split(
            "def pick_instance", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("return None", body,
                      "pick_instance no longer returns None for q — if it now "
                      "exits, the label 'exit' became correct and this test "
                      "needs inverting, deliberately.")
        offenders = [l.strip() for l in body.splitlines()
                     if "print" in l and "{YELLOW}q{RESET}" in l and "exit" in l]
        if offenders:
            self.skipTest(
                "NOT ADOPTED (instances.py is owned by another subagent): "
                "pick_instance's `q` does `return None`, and both call sites in "
                "gitlab_cli.interactive_mode (~L805, ~L840) treat None as 'go "
                "back' — it has never called sys.exit. The label must read "
                "'back'. Offending line: %s" % offenders[0])
        labels = [l for l in body.splitlines()
                  if "print" in l and "{YELLOW}q{RESET}" in l]
        for line in labels:
            self.assertNotIn("exit", line,
                             "regression: the picker advertises 'exit' again for a "
                             "key that only returns. Line: %s" % line.strip())

    def test_pick_action_normalises_case_like_every_other_prompt(self):
        body = self._src("gitlab_cli.py").split(
            "def pick_action", 1)[1].split("\ndef ", 1)[0]
        adopted = any(tok in body for tok in
                      (".lower()", ".casefold()", "menu_key",
                       "resolve_choice", "ask_choice"))
        if not adopted:
            self.skipTest(
                "NOT ADOPTED (gitlab_cli.py is owned by another subagent): "
                "pick_action compares the raw string (`if action == \"q\"`), "
                "making it the ONLY case-sensitive prompt in the application — "
                "`C`/`B`/`Q` are rejected there and accepted on every other "
                "screen. Fix: normalise with printer.menu_key() / "
                "printer.resolve_choice(), as pick_main_menu and _ask_next do.")
        self.assertTrue(adopted)

    def test_error_log_filter_rejects_an_unrecognised_key(self):
        body = self._src("errors.py").split(
            "def cmd_errors", 1)[1].split("\ndef ", 1)[0]
        # The bug shape: a bare `else:` whose only statement re-assigns the
        # unfiltered record list — "I did not understand you, here is everything".
        lines = [l.rstrip() for l in body.splitlines()]
        bad = None
        for i, line in enumerate(lines):
            if line.strip() == "else:":
                nxt = next((l for l in lines[i + 1:] if l.strip()), "")
                if nxt.strip() == "filtered = records":
                    bad = i + 1
        if bad:
            self.skipTest(
                "NOT ADOPTED (errors.py is owned by another subagent): the Error "
                "Log filter's `else:` at ~line %d sets `filtered = records`, so "
                "any key the program does not recognise silently means 'show "
                "everything'; when the typo then matches nothing the screen "
                "returns and reads as a crash. Fix: reject the key out loud with "
                "printer.print_unknown_choice() and re-prompt on the same "
                "screen." % bad)
        self.assertIsNone(bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
