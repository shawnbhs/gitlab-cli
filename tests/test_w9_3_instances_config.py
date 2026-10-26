#!/usr/bin/env python3
# tests/test_w9_3_instances_config.py — W9-3.
#
# instances.py is where several of the worst defects in this project's history
# started, and all three of them are the same shape: THE TOOL DID SOMETHING
# OTHER THAN WHAT THE USER ASKED AND SAID NOTHING. What is pinned here:
#
#   1. A SETTING THAT DID NOT SURVIVE BEING SAVED.
#      `user_namespaces` controls whether other people's personal repositories
#      are discoverable. `build_cfg()` published a fixed nine-key dict that did
#      not include it, and every screen in instances.py hands a built cfg back
#      to `_save()`. So the key worked when read straight off the raw record
#      (and the GITLAB_CLI_USER_NAMESPACES env form worked too, which made it
#      look correct), and then one unrelated save erased it with no message.
#      `min_access_level` — the group filter itself — was lost the same way.
#
#   2. A SILENT DEFAULT ON AN INVISIBLE SETTING.
#      `_pick_access_level` returned the unchanged current value for any answer
#      it did not recognise and printed nothing, so a typo produced a tool
#      quietly listing a subset of the user's estate. Worse: an EMPTY answer
#      was coerced to "1" = level 0, so pressing Enter on the edit screen threw
#      away an existing filter while advertising itself as the default.
#
#   3. A LABEL THAT DISAGREED WITH BEHAVIOUR.
#      The picker advertised `q  exit`; `q` does `return None`, and both call
#      sites in gitlab_cli.interactive_mode treat None as "go back".
#
# Everything here runs against a SANDBOX config file in a temp dir. The live
# ~/.gitlab-cli/instances.json is never read and never written: the two live
# instances are asymmetric by design (one owns the clone root with an empty
# `folder`), and rewriting either one moves repositories on disk.

import io
import json
import os
import sys
import tempfile
import unittest
import contextlib
import inspect
import shutil

from _bootstrap import PROJECT_ROOT  # noqa: F401  (path bootstrap)
import _error_log_guard  # noqa: F401

import instances as I
import printer
import setup as S


def read_text(path):
    """Read a file without leaking the handle (the suite runs with warnings on)."""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def read_bytes(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return fh.read()


# ─── sandbox ──────────────────────────────────────────────────────────────────

class SandboxConfig(unittest.TestCase):
    """Redirect instances.py's config file into a throwaway temp dir."""

    #: what a real, healthy record looks like — INCLUDING the new key.
    RECORD = {
        "name": "SandboxLab",
        "url": "http://git.example.net",
        "token_env": "GITLAB_TOKEN_SANDBOXLAB",
        "master_dir": "MasterGroups",
        "folder": "sandboxlab",
        "min_access_level": 30,
        "user_namespaces": "all",
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="w9_3_cfg_")
        self._dir, self._file = I.CONFIG_DIR, I.INSTANCES_FILE
        I.CONFIG_DIR = self.tmp
        I.INSTANCES_FILE = os.path.join(self.tmp, "instances.json")
        self.live = os.path.join(os.path.expanduser("~"), ".gitlab-cli",
                                 "instances.json")

    def tearDown(self):
        I.CONFIG_DIR, I.INSTANCES_FILE = self._dir, self._file
        assert self.tmp.startswith(tempfile.gettempdir())
        shutil.rmtree(self.tmp, ignore_errors=True)

    def save(self, *records, active=None):
        data = {"instances": [dict(r) for r in records],
                "active": active or records[0]["name"]}
        I._save(data)
        return data

    def on_disk(self, idx=0):
        return json.loads(read_text(I.INSTANCES_FILE))["instances"][idx]


class SandboxIsReallySandboxed(SandboxConfig):
    def test_the_module_is_not_pointed_at_the_live_file(self):
        self.assertNotEqual(os.path.realpath(I.INSTANCES_FILE),
                            os.path.realpath(self.live))
        self.assertTrue(I.INSTANCES_FILE.startswith(tempfile.gettempdir()))

    def test_a_save_writes_only_inside_the_sandbox(self):
        before = read_bytes(self.live)
        self.save(self.RECORD)
        after = read_bytes(self.live)
        self.assertEqual(before, after,
                         "a test wrote to the LIVE instances.json — that file "
                         "maps clones on disk")


# ─── item 1: the setting must survive a save ──────────────────────────────────

class UserNamespacesSurvivesASave(SandboxConfig):
    """The regression this test file exists for."""

    def test_key_is_declared_persistent(self):
        self.assertIn("user_namespaces", I.PERSISTED_KEYS)
        # and it is the SAME spelling setup.py resolves against
        self.assertIn(S.USER_NS_KEY, I.PERSISTED_KEYS)

    def test_build_cfg_copies_the_key_onto_the_cfg(self):
        cfg = I.build_cfg(dict(self.RECORD), allow_generic=True)
        self.assertEqual(cfg.get("user_namespaces"), "all")

    def test_the_effective_scope_follows_the_instance_key(self):
        # This is the user-visible consequence: before the fix the cfg had no
        # key, so setup resolved to "off" while the config file said "all".
        cfg = I.build_cfg(dict(self.RECORD), allow_generic=True)
        self.assertEqual(S.user_ns_scope(cfg, env={}), "all")
        self.assertTrue(S.user_ns_enabled(cfg, env={}))

    def test_plain_save_keeps_it(self):
        self.save(self.RECORD)
        self.assertEqual(self.on_disk().get("user_namespaces"), "all")

    def test_built_cfg_saved_back_keeps_it(self):
        """The exact path that erased it: build_cfg -> _save."""
        cfg = I.build_cfg(dict(self.RECORD), allow_generic=True)
        self.save(cfg)
        self.assertEqual(self.on_disk().get("user_namespaces"), "all",
                         "user_namespaces was erased by a save of a built cfg "
                         "— the original W9-3 defect")

    def test_round_trip_is_a_fixed_point_for_every_persisted_key(self):
        rec = dict(self.RECORD)
        for _ in range(3):
            cfg = I.build_cfg(rec, allow_generic=True)
            self.save(cfg)
            rec = self.on_disk()
        for key in I.PERSISTED_KEYS:
            self.assertIn(key, rec, f"{key} did not survive the round trip")
        self.assertEqual(rec["user_namespaces"], "all")
        self.assertEqual(rec["min_access_level"], 30)
        # and the folder is still applied exactly once (W6-8's invariant)
        self.assertEqual(rec["folder"], "sandboxlab")
        self.assertEqual(rec["master_dir"], "MasterGroups")

    def test_min_access_level_also_survives(self):
        """The filter itself was lost the same way; it governs group listing."""
        cfg = I.build_cfg(dict(self.RECORD), allow_generic=True)
        self.save(cfg)
        self.assertEqual(self.on_disk().get("min_access_level"), 30)
        rebuilt = I.build_cfg(self.on_disk(), allow_generic=True)
        self.assertEqual(rebuilt["min_access"], 30)

    def test_a_record_without_the_key_does_not_gain_one(self):
        rec = {k: v for k, v in self.RECORD.items() if k != "user_namespaces"}
        cfg = I.build_cfg(dict(rec), allow_generic=True)
        self.assertNotIn("user_namespaces", cfg)
        self.assertEqual(S.user_ns_scope(cfg, env={}), "off",
                         "absent must still mean off — this is an opt-in")
        self.save(cfg)
        self.assertNotIn("user_namespaces", self.on_disk())

    def test_the_token_is_still_never_persisted(self):
        cfg = I.build_cfg(dict(self.RECORD), allow_generic=True)
        cfg["token"] = "glpat-not-a-real-token"
        self.save(cfg)
        raw = read_text(I.INSTANCES_FILE)
        self.assertNotIn("glpat-not-a-real-token", raw)
        self.assertNotIn("token", self.on_disk())
        self.assertNotIn("headers", self.on_disk())


class DiscardedKeysAreNamedNotSwallowed(SandboxConfig):
    """The audit half of item 1: what else does the normaliser drop?"""

    #: the full set of keys a save/build cycle discards, as of W9-3.
    EXPECTED_DISCARDS = ("archived", "default_branch", "insecure_tls", "notes")

    def test_unknown_keys_are_reported_by_name(self):
        rec = dict(self.RECORD)
        rec.update({k: "x" for k in self.EXPECTED_DISCARDS})
        self.assertEqual(sorted(I.unknown_instance_keys(rec)),
                         sorted(self.EXPECTED_DISCARDS))

    def test_a_healthy_record_reports_nothing(self):
        self.assertEqual(I.unknown_instance_keys(dict(self.RECORD)), [])

    def test_cfg_only_keys_are_not_reported_as_unknown(self):
        cfg = I.build_cfg(dict(self.RECORD), allow_generic=True)
        self.assertEqual(I.unknown_instance_keys(cfg), [],
                         "a built cfg's own keys are recognised, not 'unknown'")

    def test_every_persisted_key_round_trips_and_nothing_else_claims_to(self):
        """A key is either in PERSISTED_KEYS and survives, or it is reported."""
        rec = dict(self.RECORD)
        rec.update({k: "x" for k in self.EXPECTED_DISCARDS})
        cfg = I.build_cfg(dict(rec), allow_generic=True)
        self.save(cfg)
        after = self.on_disk()
        for key in I.PERSISTED_KEYS:
            self.assertIn(key, after, f"{key} claims persistence and lost it")
        lost = [k for k in rec if k not in after]
        self.assertEqual(sorted(lost), sorted(self.EXPECTED_DISCARDS),
                         "a key started or stopped being dropped — if it is a "
                         "new setting, add it to instances.PERSISTED_KEYS "
                         "AND to this test, deliberately")
        # and the drop is announceable rather than silent
        for k in lost:
            self.assertIn(k, I.unknown_instance_keys(rec))

    def test_the_cfg_only_key_list_is_unchanged(self):
        self.assertEqual(tuple(I._CFG_ONLY_KEYS),
                         ("base_dir", "headers", "min_access"))


# ─── item 2: no silent default on the access filter ───────────────────────────

def drive(func, answers, *args, **kw):
    """Run a prompt loop with scripted answers; return (result, screen)."""
    buf = io.StringIO()
    seq = list(answers)

    def reader(prompt=""):
        buf.write(prompt)
        if not seq:
            raise EOFError
        a = seq.pop(0)
        buf.write(a + "\n")
        return a

    import builtins
    orig = builtins.input
    builtins.input = reader
    try:
        with contextlib.redirect_stdout(buf):
            if "reader" in inspect.signature(func).parameters:
                out = func(*args, reader=reader, **kw)
            else:
                out = func(*args, **kw)
    finally:
        builtins.input = orig
    return out, _plain(buf.getvalue())


def _plain(text):
    """Strip ANSI so assertions are about words, not colours."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "\033":
            j = text.find("m", i)
            i = len(text) if j < 0 else j + 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


class AccessLevelPromptIsHonest(unittest.TestCase):

    def test_valid_answer_is_taken(self):
        got, screen = drive(I._pick_access_level, ["3"], 0)
        self.assertEqual(got, 30)
        self.assertIn("Developer", screen)

    def test_empty_answer_keeps_the_current_value(self):
        """Regression: the old prompt coerced '' to '1', i.e. level 0.

        On the edit screen that meant pressing Enter silently removed an
        existing filter while the prompt called itself the default.
        """
        got, screen = drive(I._pick_access_level, [""], 30)
        self.assertEqual(got, 30, "Enter must KEEP the current filter, not "
                                  "reset it to 'everything'")
        self.assertIn("Kept", screen)

    def test_empty_default_is_printed_in_the_prompt(self):
        _, screen = drive(I._pick_access_level, [""], 30)
        self.assertIn("keep the current setting", screen)
        self.assertIn("Developer and above", screen)

    def test_unrecognised_word_is_rejected_out_loud_and_reasked(self):
        got, screen = drive(I._pick_access_level, ["developer", "3"], 30)
        self.assertIn("'developer' is not one of", screen)
        self.assertEqual(screen.count("Choice"), 2, "it must ask AGAIN")
        self.assertEqual(got, 30)

    def test_out_of_range_number_is_rejected_not_silently_kept(self):
        got, screen = drive(I._pick_access_level, ["9", "1"], 30)
        self.assertIn("'9' is not one of", screen)
        self.assertEqual(got, 0)

    def test_repeated_garbage_never_returns_a_value_the_user_did_not_pick(self):
        got, screen = drive(I._pick_access_level, ["x", "yy", "0", ""], 40)
        self.assertEqual(screen.count("is not one of"), 3)
        self.assertEqual(got, 40)

    def test_eof_keeps_the_current_value_and_says_so(self):
        got, screen = drive(I._pick_access_level, [], 50)
        self.assertEqual(got, 50)
        self.assertIn("Cancelled", screen)

    def test_never_returns_an_unlisted_level(self):
        valid = {lvl for lvl, _ in I.ACCESS_CHOICES}
        for answers in (["1"], ["2"], ["3"], ["4"], ["5"], ["zz", "5"], [""]):
            got, _ = drive(I._pick_access_level, answers, 20)
            self.assertIn(got, valid)


class AccessLevelIsVisible(unittest.TestCase):
    """A filter nobody can see is a filter that is silently wrong."""

    def test_zero_is_named_not_left_blank(self):
        self.assertIn("Everything I can see", I.access_label(0))
        self.assertIn("no filter", I.access_summary(0))

    def test_a_real_filter_says_what_it_hides(self):
        text = I.access_summary(30)
        self.assertIn("Developer", text)
        self.assertIn("NOT listed", text)

    def test_every_choice_has_a_label(self):
        for lvl, _ in I.ACCESS_CHOICES:
            self.assertTrue(I.access_label(lvl).strip())
            self.assertTrue(I.access_summary(lvl).strip())

    def test_unreadable_value_says_so_instead_of_pretending(self):
        self.assertIn("unreadable", I.access_label("banana"))

    def test_none_and_empty_are_the_no_filter_case(self):
        for v in (None, "", 0, "0"):
            self.assertIn("no filter", I.access_summary(v))

    def test_the_prompt_shows_the_level_currently_in_force(self):
        _, screen = drive(I._pick_access_level, [""], 40)
        self.assertIn("Currently:", screen)
        self.assertIn("Maintainer", screen)

    def test_the_prompt_echoes_the_level_it_is_returning(self):
        _, screen = drive(I._pick_access_level, ["4"], 0)
        self.assertIn("Access filter:", screen)
        self.assertIn("Maintainer", screen)

    def test_screens_that_can_change_it_state_it(self):
        src = read_text(os.path.join(PROJECT_ROOT, "instances.py"))
        for func in ("add_instance", "edit_instance", "print_instances"):
            body = src.split(f"def {func}", 1)[1].split("\ndef ", 1)[0]
            self.assertTrue(
                "access_summary" in body or "access_label" in body,
                f"{func} can show or change the access filter but never names "
                f"it — that is how a filter becomes invisible")


# ─── item 3: labels, case, and unrecognised input in the picker ───────────────

class PickerLabelAgreesWithBehaviour(SandboxConfig):

    def body(self, name="pick_instance"):
        src = read_text(os.path.join(PROJECT_ROOT, "instances.py"))
        return src.split(f"def {name}", 1)[1].split("\ndef ", 1)[0]

    def test_q_still_returns_none(self):
        self.save(self.RECORD)
        got, _ = drive(I.pick_instance, ["q"])
        self.assertIsNone(got)

    def test_q_is_not_advertised_as_exit(self):
        for line in self.body().splitlines():
            if "print" in line and "{YELLOW}q{RESET}" in line:
                self.assertNotIn(
                    "exit", line,
                    "the picker advertises 'exit' for a key that only returns "
                    "None to interactive_mode. Line: %s" % line.strip())

    def test_the_label_on_screen_reads_back(self):
        self.save(self.RECORD)
        _, screen = drive(I.pick_instance, ["q"])
        nav = [l.strip() for l in screen.splitlines()
               if l.strip().startswith("q ")]
        self.assertTrue(nav, "no nav line printed for q")
        self.assertEqual(nav[-1], f"q   {printer.KEY_BACK}")
        self.assertNotIn(printer.KEY_EXIT, nav[-1])

    def test_the_label_is_the_shared_constant_not_a_loose_string(self):
        self.assertIn("KEY_BACK", self.body())


class PickerIsCaseInsensitive(SandboxConfig):

    def test_uppercase_q_returns_like_lowercase_q(self):
        self.save(self.RECORD)
        for answer in ("q", "Q", "  Q  "):
            got, _ = drive(I.pick_instance, [answer])
            self.assertIsNone(got, f"{answer!r} was not understood as q")

    def test_name_search_folds_case(self):
        self.save(self.RECORD)
        for answer in ("sandbox", "SANDBOX", " SandBox "):
            got, screen = drive(I.pick_instance, [answer])
            self.assertIn("Active: SandboxLab", screen,
                          f"{answer!r} did not match the instance by name")

    def test_normalisation_goes_through_the_shared_helper(self):
        src = read_text(os.path.join(PROJECT_ROOT, "instances.py"))
        self.assertIn("menu_key", src)
        body = src.split("def pick_instance", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn(".lower()", body,
                         "hand-rolled lowercasing is back in pick_instance; "
                         "use printer.menu_key so every prompt folds the same "
                         "way in one place")


class PickerRejectsWhatItDoesNotUnderstand(SandboxConfig):

    def test_garbage_is_named_and_the_menu_is_reshown(self):
        self.save(self.RECORD)
        got, screen = drive(I.pick_instance, ["zzz", "q"])
        self.assertIsNone(got)
        self.assertIn("'zzz' is not one of", screen)
        self.assertEqual(screen.count("GitLabs:"), 2,
                         "the picker must re-ask, not fall through")

    def test_garbage_never_selects_an_instance(self):
        self.save(self.RECORD)
        _, screen = drive(I.pick_instance, ["zzz", "q"])
        self.assertNotIn("Active:", screen)

    def test_out_of_range_number_is_refused(self):
        self.save(self.RECORD)
        got, screen = drive(I.pick_instance, ["7", "q"])
        self.assertIsNone(got)
        self.assertIn("Pick a number from 1 to 1", screen)

    def test_an_ambiguous_name_is_not_resolved_by_guessing(self):
        a = dict(self.RECORD, name="SandboxOne", folder="one",
                 token_env="GITLAB_TOKEN_ONE")
        b = dict(self.RECORD, name="SandboxTwo", folder="two",
                 token_env="GITLAB_TOKEN_TWO")
        self.save(a, b, active="SandboxOne")
        got, screen = drive(I.pick_instance, ["sandbox", "q"])
        self.assertIsNone(got)
        self.assertIn("matches 2", screen)
        self.assertIn("SandboxOne", screen)
        self.assertIn("SandboxTwo", screen)
        self.assertNotIn("Active: SandboxOne", screen.split("matches 2")[-1])


class EveryPromptInThisFileFollowsTheConvention(unittest.TestCase):
    """The three defect classes, swept across the whole module."""

    def src(self):
        return read_text(os.path.join(PROJECT_ROOT, "instances.py"))

    def test_no_prompt_lowercases_by_hand(self):
        offenders = [l.strip() for l in self.src().splitlines()
                     if "input(" in l and ".lower()" in l]
        self.assertEqual(offenders, [],
                         "these prompts normalise by hand instead of using "
                         "printer.menu_key/ask_choice: %s" % offenders)

    def test_yes_no_prompts_go_through_ask_choice(self):
        """A y/n where anything-not-y means no is the same silent default."""
        src = self.src()
        raw_yn = [l.strip() for l in src.splitlines()
                  if "input(" in l and "(y/n)" in l]
        self.assertEqual(raw_yn, [],
                         "raw y/n input() calls left: %s" % raw_yn)
        self.assertGreaterEqual(src.count("ask_choice("), 4)

    def test_no_screen_prints_exit_for_a_key_that_returns(self):
        src = self.src()
        for line in src.splitlines():
            if "print" in line and "exit" in line and "{YELLOW}" in line:
                self.fail("a screen in instances.py advertises 'exit': %s"
                          % line.strip())

    def test_menu_loops_reject_unknown_keys(self):
        """Each interactive loop must call print_unknown_choice somewhere."""
        src = self.src()
        for func in ("pick_instance", "_pick_access_level", "edit_instance"):
            body = src.split(f"def {func}", 1)[1].split("\ndef ", 1)[0]
            self.assertIn("print_unknown_choice", body,
                          f"{func} has no path that tells the user their "
                          f"answer was not understood")

    def test_the_helpers_come_from_printer_not_a_local_copy(self):
        self.assertIs(I.menu_key, printer.menu_key)
        self.assertIs(I.ask_choice, printer.ask_choice)
        self.assertIs(I.print_unknown_choice, printer.print_unknown_choice)


# ─── the live config is never touched ─────────────────────────────────────────

class LiveConfigUntouched(unittest.TestCase):
    LIVE = os.path.join(os.path.expanduser("~"), ".gitlab-cli", "instances.json")

    def test_module_default_points_at_the_live_file(self):
        """Sanity: the sandbox really is an override, not the normal state."""
        import importlib
        mod = importlib.reload(I)
        self.assertEqual(os.path.realpath(mod.INSTANCES_FILE),
                         os.path.realpath(self.LIVE))

    def test_the_two_live_instances_are_allowed_to_be_asymmetric(self):
        """One instance owns the clone root with an empty `folder`, by design.

        Pinned so a future 'tidy-up' that normalises both instances to the same
        shape fails here instead of re-arming the path-stripping defect that
        produced a half-nested/double-nested split across the whole tree.
        """
        if not os.path.exists(self.LIVE):
            self.skipTest("no live config on this machine")
        insts = json.loads(read_text(self.LIVE)).get("instances", [])
        if len(insts) < 2:
            self.skipTest("fewer than two live instances")
        folders = [(i.get("folder") or "") for i in insts]
        self.assertNotEqual(len(set(folders)), 1,
                            "the live instances were normalised to the same "
                            "folder shape — they share one clone root and are "
                            "asymmetric ON PURPOSE")
        # and build_cfg must still give them DIFFERENT destinations
        dests = {I.build_cfg(dict(i), allow_generic=True)["master_dir"]
                 for i in insts}
        self.assertEqual(len(dests), len(insts),
                         "two live instances resolve to the same clone "
                         "destination — that is the folder-collision defect")


if __name__ == "__main__":
    unittest.main(verbosity=2)
