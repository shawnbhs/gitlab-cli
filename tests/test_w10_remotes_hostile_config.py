# tests/test_w10_remotes_hostile_config.py — W10: the Remotes screen against a
# repository that was built to attack it.
#
# remotes.py walks repositories this tool did not create and then OFFERS TO
# REWRITE THEM. Every string it works with — the remote name, the remote URL —
# is a `.git/config` section header or value written by whoever controls that
# repository. `git remote add` validates a name; nothing validates one that is
# already in the file, and `git remote -v` prints it back verbatim.
#
# Four separate defects are pinned here, each reproduced against real git
# (2.43) before it was fixed:
#
#   1. REDACTION CORRUPTED THE WRITE-BACK. git._run() scrubs its stdout for
#      token-shaped text, and a real repository PATH can be token-shaped:
#      `…/group-a/glint-management-system` matches the `gl<letters>-<16+>`
#      pattern. `git remote -v` therefore returned
#      `https://h/group-a/[REDACTED-TOKEN].git`, and scrub() wrote that literal
#      string back with `remote set-url`. The security feature destroyed the
#      remote. This is the one that loses data on a normal, friendly repo.
#   2. `git remote -v` IS NOT A GRAMMAR. A URL containing a newline fabricates
#      an entire extra row, so the audit reported — and the scrub would have
#      rewritten — a remote that does not exist.
#   3. ARGUMENT INJECTION ON THE WRITE PATH. `remote set-url <name> <url>`
#      with a name of `--output=<file>` is an option, not a name.
#   4. chmod THROUGH A SYMLINK. `.git/config` in a hostile repo can be a
#      symlink; `os.chmod()` follows it and changes the victim's mode.
#
# Every exploit assertion is on a SIDE EFFECT — a marker file, the bytes in a
# config, the mode of a victim file — never on a return code, because git
# exits 0 after doing the attacker's work.
#
# Offline: real git, real temp dirs, no network, no token, no live error log.

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

import _bootstrap  # noqa: F401  (sys.path + live-error-log isolation)
from _bootstrap import TempTreeTestCase, git

import git as gitmod
import remotes


TOKEN = "glpat-" + "A" * 20

# A real, ordinary GitLab path that happens to match the token-shape pattern
# the redactor uses. Not contrived: it is the example remotes.py itself cites.
TOKEN_SHAPED_PATH = "group-a/glint-management-system"  # noqa: S105 (that it LOOKS like a secret is the point)


def write_stanza(repo, text):
    """Append raw text to .git/config — how a hostile repository reaches us."""
    with open(os.path.join(repo, ".git", "config"), "a", encoding="utf-8") as fh:
        fh.write(text)


def remote_stanza(name, url, pushurl=None):
    out = '[remote "%s"]\n\turl = %s\n' % (name, url)
    if pushurl:
        out += "\tpushurl = %s\n" % pushurl
    out += "\tfetch = +refs/heads/*:refs/remotes/x/*\n"
    return out


def config_text(repo):
    with open(os.path.join(repo, ".git", "config"), encoding="utf-8",
              errors="replace") as fh:
        return fh.read()


def url_lines(repo):
    return [ln.split("=", 1)[1].strip()
            for ln in config_text(repo).splitlines()
            if ln.strip().startswith("url =")]


class RedactionMustNotReachTheConfig(TempTreeTestCase):
    """Defect 1 — the data-loss bug, and the reason the reader was rewritten.

    This is not an attack: it is what happens to an honest repository whose
    namespace happens to look like a token.
    """

    def setUp(self):
        super().setUp()
        self.repo = self.make_repo("grp/redact", origin=None)
        self.real = "https://oauth2:%s@git.example.net/%s.git" % (
            TOKEN, TOKEN_SHAPED_PATH)
        write_stanza(self.repo, remote_stanza("origin", self.real))
        # No network, no real credential store.
        self._orig = remotes._credential_approve
        remotes._credential_approve = lambda url, cred: True
        self.addCleanup(setattr, remotes, "_credential_approve", self._orig)

    def test_the_redactor_really_does_mangle_this_path(self):
        """Ground truth: without it the rest of this class proves nothing."""
        from errors import redact
        self.assertIn("[REDACTED", redact("https://h/%s.git" % TOKEN_SHAPED_PATH),
                      "the redactor no longer mangles this path; pick another")

    def test_list_remotes_returns_the_true_url_not_gits_redacted_stdout(self):
        rm = remotes.list_remotes(self.repo)
        self.assertEqual(len(rm), 1)
        self.assertNotIn("[REDACTED", rm[0]["url"])
        self.assertNotIn("[REDACTED", rm[0]["clean_url"])
        self.assertEqual(rm[0]["clean_url"],
                         "https://git.example.net/%s.git" % TOKEN_SHAPED_PATH)

    def test_scrub_leaves_a_working_remote_behind(self):
        remotes.scrub([self.repo], save_to_store=True, enforce_scope=False)
        after = url_lines(self.repo)
        self.assertEqual(after,
                         ["https://git.example.net/%s.git" % TOKEN_SHAPED_PATH],
                         "scrub wrote a corrupted URL into .git/config")
        self.assertNotIn("[REDACTED", config_text(self.repo))

    def test_the_token_is_gone_but_the_repository_still_points_somewhere_real(self):
        remotes.scrub([self.repo], save_to_store=True, enforce_scope=False)
        text = config_text(self.repo)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("oauth2:", text)
        # And git agrees the remote is still usable.
        out = git("remote", "get-url", "origin", cwd=self.repo).stdout.strip()
        self.assertTrue(out.startswith("https://git.example.net/"), out)

    def test_the_credential_handed_to_the_store_is_the_real_one(self):
        seen = []
        remotes._credential_approve = lambda url, cred: (
            seen.append((url, cred)), True)[1]
        remotes.scrub([self.repo], save_to_store=True, enforce_scope=False)
        self.assertEqual(len(seen), 1)
        url, cred = seen[0]
        self.assertNotIn("[REDACTED", cred,
                         "a redacted placeholder was filed as the password")
        self.assertEqual(cred, "oauth2:" + TOKEN)
        self.assertNotIn("[REDACTED", url)


class RemoteListIsNotParsedFromGitStdout(TempTreeTestCase):
    """Defect 2 — `git remote -v` can be made to print rows that do not exist."""

    def test_a_newline_in_a_url_does_not_fabricate_a_remote(self):
        repo = self.make_repo("grp/liar", origin=None)
        write_stanza(repo, '[remote "liar"]\n\turl = "https://real.test/y.git'
                           '\\nfake\\thttps://evil.test/z (fetch)"\n')
        # Ground truth: git itself really does print a row named `fake`.
        self.assertIn("fake", git("remote", "-v", cwd=repo).stdout)

        names = [r["name"] for r in remotes.list_remotes(repo)]
        self.assertEqual(names, ["liar"],
                         "a fabricated row survived into the remote list")

    def test_a_space_in_a_url_does_not_silently_truncate_it(self):
        repo = self.make_repo("grp/spacey", origin=None)
        write_stanza(repo, remote_stanza("spacey",
                                         "https://h.test/x.git extra"))
        rm = {r["name"]: r for r in remotes.list_remotes(repo)}["spacey"]
        self.assertEqual(rm["url"], "https://h.test/x.git extra")
        self.assertTrue(rm["unsafe"], "a URL containing a space was accepted")

    def test_a_pushurl_only_remote_is_still_seen(self):
        repo = self.make_repo("grp/pushonly", origin=None)
        write_stanza(repo, '[remote "po"]\n\tpushurl = https://h.test/p.git\n')
        rm = remotes.list_remotes(repo)
        self.assertEqual([r["name"] for r in rm], ["po"])
        self.assertEqual(rm[0]["url"], "https://h.test/p.git")

    def test_ordinary_repositories_are_read_exactly_as_before(self):
        repo = self.make_repo("grp/normal",
                              origin="https://git.example.net/grp/normal.git")
        git("remote", "add", "upstream",
            "https://git.example.net/up/normal.git", cwd=repo)
        rm = remotes.list_remotes(repo)
        self.assertEqual([r["name"] for r in rm], ["origin", "upstream"])
        self.assertEqual(rm[0]["clean_url"],
                         "https://git.example.net/grp/normal.git")
        self.assertEqual(rm[0]["protocol"], "https")
        self.assertEqual([r["unsafe"] for r in rm], ["", ""])

    def test_a_repository_with_no_git_dir_yields_nothing(self):
        empty = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(empty)
        self.assertEqual(remotes.list_remotes(empty), [])


class TheConfigParserMatchesGit(TempTreeTestCase):
    """The reader replaces a git process, so it has to agree with git."""

    def _roundtrip(self, name, url):
        repo = self.make_repo("grp/rt_%d" % abs(hash((name, url))), origin=None)
        r = git("remote", "add", name, url, cwd=repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = {x["name"]: x["url"] for x in remotes.list_remotes(repo)}
        self.assertEqual(got.get(name), url,
                         "parser disagrees with git for %r" % ((name, url),))

    def test_names_and_urls_git_itself_accepts(self):
        for name, url in (("origin", "https://h.test/a.git"),
                          ("team/mirror", "https://h.test/b.git"),
                          ("fork.old", "http://h.test:8080/c.git"),
                          ("gitlab-2", "https://h.test/d/e/f.git")):
            with self.subTest(name=name):
                self._roundtrip(name, url)

    def test_comments_and_blank_lines_are_ignored(self):
        repo = self.make_repo("grp/comments", origin=None)
        write_stanza(repo, "\n# a comment\n; another\n"
                           + remote_stanza("origin", "https://h.test/a.git")
                           + "\n")
        self.assertEqual([r["name"] for r in remotes.list_remotes(repo)],
                         ["origin"])

    def test_a_quoted_value_with_an_inline_comment_char_is_kept_whole(self):
        repo = self.make_repo("grp/quoted", origin=None)
        write_stanza(repo, '[remote "q"]\n\turl = "https://h.test/a#b.git"\n')
        got = {r["name"]: r["url"] for r in remotes.list_remotes(repo)}
        self.assertEqual(got["q"], "https://h.test/a#b.git")

    def test_the_last_value_wins_exactly_as_git_does(self):
        repo = self.make_repo("grp/dup", origin=None)
        write_stanza(repo, '[remote "d"]\n\turl = https://first.test/a.git\n'
                           '[remote "d"]\n\turl = https://second.test/a.git\n')
        expect = git("config", "--get", "remote.d.url", cwd=repo).stdout.strip()
        got = {r["name"]: r["url"] for r in remotes.list_remotes(repo)}
        self.assertEqual(got["d"], expect)

    def test_a_section_header_and_a_key_on_one_line(self):
        repo = self.make_repo("grp/oneline", origin=None)
        write_stanza(repo, '[remote "ol"] url = https://h.test/ol.git\n')
        expect = git("config", "--get", "remote.ol.url", cwd=repo).stdout.strip()
        got = {r["name"]: r["url"] for r in remotes.list_remotes(repo)}
        self.assertEqual(got.get("ol"), expect)


class HostileRemotesAreRefusedNotRewritten(TempTreeTestCase):
    """Defect 3 — the write path is where an option-shaped name becomes RCE."""

    def setUp(self):
        super().setUp()
        self.drop = os.path.join(self.tmp, "drop")
        os.makedirs(self.drop, exist_ok=True)
        self._orig = remotes._credential_approve
        remotes._credential_approve = lambda url, cred: True
        self.addCleanup(setattr, remotes, "_credential_approve", self._orig)

    def assertNothingDropped(self, why):
        self.assertEqual(sorted(os.listdir(self.drop)), [], why)

    def test_an_option_shaped_remote_name_is_flagged_unsafe(self):
        repo = self.make_repo("grp/optname", origin=None)
        write_stanza(repo, remote_stanza(
            "--output=" + os.path.join(self.drop, "X"),
            "https://oauth2:%s@h.test/x.git" % TOKEN))
        rm = remotes.list_remotes(repo)
        self.assertEqual(len(rm), 1)
        self.assertEqual(rm[0]["unsafe"], remotes.UNSAFE_NAME)

    def test_scrub_refuses_it_and_writes_nothing(self):
        repo = self.make_repo("grp/optname2", origin=None)
        write_stanza(repo, remote_stanza(
            "--output=" + os.path.join(self.drop, "X"),
            "https://oauth2:%s@h.test/x.git" % TOKEN))
        before = config_text(repo)
        stats = remotes.scrub([repo], save_to_store=True, enforce_scope=False)
        self.assertEqual(stats["cleaned"], 0)
        self.assertEqual(stats["refused"], 1)
        self.assertNothingDropped("git ran the payload during scrub()")
        self.assertEqual(config_text(repo), before,
                         "scrub modified a config it had refused")

    def test_an_ext_transport_url_is_never_handed_to_git(self):
        rce = os.path.join(self.drop, "RCE")
        repo = self.make_repo("grp/ext", origin=None)
        write_stanza(repo, remote_stanza("e", "ext::sh -c touch%% " + rce))
        rm = remotes.list_remotes(repo)
        self.assertEqual(rm[0]["unsafe"], remotes.UNSAFE_URL)
        remotes.scrub([repo], save_to_store=True, enforce_scope=False)
        remotes.audit([repo], check_network=True, enforce_scope=False)
        self.assertNothingDropped("an ext:: remote executed")

    def test_an_insteadof_rewrite_to_ext_cannot_execute(self):
        """The payload is not in the URL at all — it is in a rewrite rule.

        Verified against git 2.43: `ls-remote -- <name>` on a remote whose
        https URL is rewritten to `ext::sh -c …` runs the command. `--` does
        not stop it and validating the URL does not stop it, because the
        rewrite happens after both. GIT_ALLOW_PROTOCOL does.
        """
        marker = os.path.join(self.drop, "INSTEAD")
        repo = self.make_repo("grp/instead", origin=None)
        write_stanza(
            repo,
            '[url "ext::sh -c touch%% %s "]\n\tinsteadOf = https://nice.test/\n'
            % marker
            + '[protocol "ext"]\n\tallow = always\n'
            + remote_stanza("g", "https://nice.test/x.git"))

        # Ground truth: plain git, with no guard, really does execute it.
        env = dict(os.environ, HOME=self.tmp, GIT_TERMINAL_PROMPT="0")
        subprocess.run([gitmod.GIT_BIN, "-C", repo, "ls-remote", "-h", "--", "g"],
                       capture_output=True, timeout=30, env=env)
        self.assertTrue(os.path.exists(marker),
                        "this git does not honour the insteadOf->ext:: rewrite; "
                        "the assertion below would be vacuous")
        os.remove(marker)

        ok, why = remotes._reachable(repo, {"name": "g", "unsafe": ""})
        self.assertFalse(ok)
        self.assertNothingDropped("insteadOf->ext:: executed through _reachable")

    def test_reachability_refuses_an_unsafe_remote_without_calling_git(self):
        repo = self.make_repo("grp/reach", origin=None)
        ok, why = remotes._reachable(
            repo, {"name": "--output=" + os.path.join(self.drop, "R"),
                   "unsafe": remotes.UNSAFE_NAME})
        self.assertFalse(ok)
        self.assertEqual(why, "refused")
        self.assertNothingDropped("ls-remote ran with an option-shaped name")

    def test_a_good_remote_beside_a_hostile_one_is_still_repaired(self):
        """Refusing the bad one must not disable the good one."""
        repo = self.make_repo("grp/mixed", origin=None)
        write_stanza(repo, remote_stanza(
            "--output=" + os.path.join(self.drop, "X"), "https://h.test/bad.git"))
        write_stanza(repo, remote_stanza(
            "origin", "https://oauth2:%s@git.example.net/grp/mixed.git" % TOKEN))
        stats = remotes.scrub([repo], save_to_store=True, enforce_scope=False)
        self.assertEqual(stats["cleaned"], 1)
        self.assertEqual(stats["refused"], 1)
        self.assertNothingDropped("the payload ran")
        self.assertIn("https://git.example.net/grp/mixed.git", url_lines(repo))
        self.assertNotIn(TOKEN, config_text(repo))

    def test_the_audit_reports_a_refused_remote_instead_of_hiding_it(self):
        repo = self.make_repo("grp/report", origin=None)
        write_stanza(repo, remote_stanza("e", "ext::sh -c id"))
        rows = remotes.audit([repo], enforce_scope=False)
        self.assertEqual(len(rows[0]["refused"]), 1)
        self.assertEqual(rows[0]["refused"][0]["why"], remotes.UNSAFE_URL)
        # …and it is NOT in the list the scrub acts on.
        self.assertEqual(rows[0]["leaks"], [])

    def test_a_refused_remote_does_not_pollute_the_host_summary(self):
        repo = self.make_repo("grp/hosts", origin=None)
        write_stanza(repo, remote_stanza("e", "ext::sh -c id"))
        write_stanza(repo, remote_stanza("origin", "https://good.test/a.git"))
        rows = remotes.audit([repo], enforce_scope=False)
        self.assertEqual(rows[0]["hosts"], ["good.test"])


class TerminalEscapesCannotRepaintTheScreen(TempTreeTestCase):
    """A remote name is attacker text printed to a terminal."""

    def test_git_really_emits_the_escape(self):
        repo = self.make_repo("grp/esc_truth", origin=None)
        write_stanza(repo, remote_stanza("\x1b[2Korigin", "https://h.test/a.git"))
        self.assertIn("\x1b", git("remote", "-v", cwd=repo).stdout,
                      "git no longer echoes the ESC; the test below is vacuous")

    def test_the_display_name_carries_no_control_bytes(self):
        repo = self.make_repo("grp/esc", origin=None)
        write_stanza(repo, remote_stanza("\x1b[2K\rorigin", "https://h.test/a.git"))
        rm = remotes.list_remotes(repo)[0]
        self.assertNotIn("\x1b", rm["display"])
        self.assertNotIn("\r", rm["display"])
        # The ORIGINAL is preserved — it is what gets compared and validated.
        self.assertIn("\x1b", rm["name"])
        self.assertEqual(rm["unsafe"], remotes.UNSAFE_CONTROL)

    def test_safe_text_neutralises_the_sequences_that_matter(self):
        for bad in ("\x1b[2K", "\r", "\x00", "\x08", "\x1b]0;title\x07"):
            with self.subTest(bad=bad):
                out = remotes.safe_text("a" + bad + "b")
                self.assertNotIn("\x1b", out)
                self.assertNotIn("\r", out)
                self.assertNotIn("\x00", out)

    def test_safe_text_leaves_ordinary_text_alone(self):
        self.assertEqual(remotes.safe_text("origin"), "origin")
        self.assertEqual(remotes.safe_text("team/mirror-2.old"),
                         "team/mirror-2.old")

    def test_printing_an_audit_of_a_hostile_repo_emits_no_escape(self):
        import io
        import contextlib
        repo = self.make_repo("grp/esc_print", origin=None)
        write_stanza(repo, remote_stanza(
            "\x1b[2K\rorigin", "https://oauth2:%s@h.test/a.git" % TOKEN))
        write_stanza(repo, remote_stanza("real", "https://h.test/b.git"))
        rows = remotes.audit([repo], enforce_scope=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            remotes.print_audit(rows, show_all=True)
        text = buf.getvalue()
        # The colour codes print_audit writes itself are fine; a NAME-borne
        # erase-line is not. `[2K` is the payload's signature.
        self.assertNotIn("\x1b[2K", text)
        self.assertNotIn("\r", text)
        self.assertNotIn(TOKEN, text, "a token was printed to the screen")


class ConfigHardeningDoesNotFollowSymlinks(TempTreeTestCase):
    """Defect 4 — chmod through a symlink changes someone else's file."""

    def test_chmod_through_a_symlink_really_hits_the_target(self):
        """Ground truth for the fix below."""
        victim = os.path.join(self.tmp, "truth_victim")
        with open(victim, "w") as fh:
            fh.write("x")
        os.chmod(victim, 0o644)
        link = os.path.join(self.tmp, "truth_link")
        os.symlink(victim, link)
        os.chmod(link, 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(victim).st_mode), 0o600,
                         "os.chmod no longer follows symlinks on this platform")

    def test_harden_config_refuses_a_symlinked_config(self):
        repo = self.make_repo("grp/symlink", origin=None)
        victim = os.path.join(self.tmp, "victim_key")
        with open(victim, "w") as fh:
            fh.write("PRIVATE KEY")
        os.chmod(victim, 0o644)

        cfg = os.path.join(repo, ".git", "config")
        saved = config_text(repo)
        os.remove(cfg)
        os.symlink(victim, cfg)

        self.assertFalse(remotes.harden_config(repo),
                         "hardening claimed success on a symlinked config")
        self.assertEqual(stat.S_IMODE(os.stat(victim).st_mode), 0o644,
                         "chmod followed the symlink and changed the victim")
        # restore so teardown is clean
        os.remove(cfg)
        with open(cfg, "w") as fh:
            fh.write(saved)

    def test_harden_config_still_tightens_a_real_config(self):
        repo = self.make_repo("grp/realcfg", origin=None)
        cfg = os.path.join(repo, ".git", "config")
        os.chmod(cfg, 0o644)
        self.assertTrue(remotes.harden_config(repo))
        self.assertEqual(stat.S_IMODE(os.stat(cfg).st_mode), 0o600)

    def test_scrub_hardens_the_config_it_actually_rewrote(self):
        repo = self.make_repo("grp/hardened", origin=None)
        write_stanza(repo, remote_stanza(
            "origin", "https://oauth2:%s@git.example.net/a.git" % TOKEN))
        cfg = os.path.join(repo, ".git", "config")
        os.chmod(cfg, 0o644)
        self._orig = remotes._credential_approve
        remotes._credential_approve = lambda url, cred: True
        self.addCleanup(setattr, remotes, "_credential_approve", self._orig)
        stats = remotes.scrub([repo], save_to_store=True, enforce_scope=False)
        self.assertEqual(stats["hardened"], 1)
        self.assertEqual(stat.S_IMODE(os.stat(cfg).st_mode), 0o600)


class CredentialProtocolFieldsAreValidated(unittest.TestCase):
    """The git-credential stdin protocol: later keys win, so `\\n` is a hijack."""

    def setUp(self):
        self.calls = []
        self._real = subprocess.run

    def _capture(self, clean, cred):
        """Run _credential_approve with subprocess.run stubbed out."""
        import remotes as R

        seen = {}

        class Fake:
            returncode = 0

        def fake_run(argv, **kw):
            seen["argv"] = argv
            seen["input"] = kw.get("input", "")
            return Fake()

        real = R.subprocess.run
        R.subprocess.run = fake_run
        try:
            ok = R._credential_approve(clean, cred)
        finally:
            R.subprocess.run = real
        return ok, seen

    def test_a_newline_in_the_password_is_refused(self):
        ok, seen = self._capture("https://real.test/a.git",
                                 "oauth2:tok\npassword=HIJACK\nhost=evil.test")
        self.assertFalse(ok)
        self.assertEqual(seen, {}, "the hijack payload reached git's stdin")

    def test_a_newline_in_the_host_is_refused(self):
        ok, seen = self._capture("https://ho\nst.test/a.git", "oauth2:tok")
        self.assertFalse(ok)
        self.assertEqual(seen, {})

    def test_a_space_in_a_field_is_refused(self):
        ok, _ = self._capture("https://host .test/a.git", "oauth2:tok")
        self.assertFalse(ok)

    def test_a_non_host_authority_is_refused(self):
        for bad in ("https://*/a.git", "https:///a.git",
                    "https://ev il/a.git", "https://h;rm -rf/a.git"):
            with self.subTest(bad=bad):
                ok, _ = self._capture(bad, "oauth2:tok")
                self.assertFalse(ok)

    def test_a_non_http_scheme_is_refused(self):
        ok, _ = self._capture("ssh://host.test/a.git", "oauth2:tok")
        self.assertFalse(ok)

    def test_an_ordinary_credential_is_still_filed(self):
        ok, seen = self._capture("https://git.example.net:8443/a/b.git",
                                 "oauth2:" + TOKEN)
        self.assertTrue(ok)
        payload = seen["input"]
        self.assertIn("protocol=https\n", payload)
        self.assertIn("host=git.example.net:8443\n", payload)
        self.assertIn("username=oauth2\n", payload)
        self.assertIn("password=%s\n" % TOKEN, payload)
        self.assertTrue(payload.endswith("\n\n"))
        # exactly the four fields, no injected extras
        self.assertEqual(
            sorted(ln.split("=", 1)[0] for ln in payload.split("\n") if ln),
            ["host", "password", "protocol", "username"])


class TheDryRunTellsTheTruth(TempTreeTestCase):
    """A dry run that reports a rewrite the real run would refuse is a lie."""

    def setUp(self):
        super().setUp()
        self._orig = remotes._credential_approve
        remotes._credential_approve = lambda url, cred: True
        self.addCleanup(setattr, remotes, "_credential_approve", self._orig)

    def test_dry_run_and_real_run_agree_on_a_hostile_repo(self):
        repo = self.make_repo("grp/dry", origin=None)
        write_stanza(repo, remote_stanza("e", "ext::sh -c id"))
        write_stanza(repo, remote_stanza(
            "--output=/tmp/w10_never", "https://oauth2:%s@h.test/x.git" % TOKEN))
        write_stanza(repo, remote_stanza(
            "origin", "https://oauth2:%s@git.example.net/a.git" % TOKEN))

        dry = remotes.scrub([repo], save_to_store=False, dry_run=True,
                            enforce_scope=False)
        real = remotes.scrub([repo], save_to_store=True, enforce_scope=False)
        self.assertEqual(dry["cleaned"], real["cleaned"])
        self.assertEqual(dry["refused"], real["refused"])
        self.assertEqual(dry["cleaned"], 1)

    def test_dry_run_changes_nothing_on_disk(self):
        repo = self.make_repo("grp/dry2", origin=None)
        write_stanza(repo, remote_stanza(
            "origin", "https://oauth2:%s@git.example.net/a.git" % TOKEN))
        before = config_text(repo)
        remotes.scrub([repo], save_to_store=False, dry_run=True,
                      enforce_scope=False)
        self.assertEqual(config_text(repo), before)

    def test_no_secret_appears_in_the_details_a_caller_prints(self):
        repo = self.make_repo("grp/dry3", origin=None)
        write_stanza(repo, remote_stanza(
            "origin", "https://oauth2:%s@git.example.net/a.git" % TOKEN))
        for kw in ({"dry_run": True}, {}):
            stats = remotes.scrub([repo], save_to_store=True,
                                  enforce_scope=False, **kw)
            self.assertNotIn(TOKEN, " ".join(stats["details"]))


class ReadingTheConfigDirectlyDoesNotLeakTheToken(TempTreeTestCase):
    """The reader rewrite moved a live token into this process's hands.

    `git remote -v` used to hand back text git._run() had already redacted.
    Parsing the config file is what makes the scrub correct — but it means
    the real credential is now in the rows that audit() returns, prints, and
    writes to errors.log. The default therefore has to be masked, with the
    write path opting in explicitly.
    """

    def setUp(self):
        super().setUp()
        self.repo = self.make_repo("grp/leak", origin=None)
        write_stanza(self.repo, remote_stanza(
            "origin", "https://oauth2:%s@git.example.net/grp/leak.git" % TOKEN))

    def test_the_default_row_carries_no_token(self):
        rm = remotes.list_remotes(self.repo)[0]
        self.assertNotIn(TOKEN, rm["url"])
        self.assertNotIn(TOKEN, repr(rm))
        # …while still reporting, truthfully, that there IS one.
        self.assertTrue(rm["has_creds"])

    def test_the_audit_rows_a_caller_may_log_carry_no_token(self):
        rows = remotes.audit([self.repo], enforce_scope=False)
        self.assertNotIn(TOKEN, repr(rows))
        self.assertEqual(rows[0]["leaks"], ["origin"])

    def test_the_write_path_can_still_see_the_real_credential(self):
        rm = remotes.list_remotes(self.repo, reveal=True)[0]
        self.assertIn(TOKEN, rm["url"])

    def test_scrub_stats_never_carry_the_token(self):
        self._orig = remotes._credential_approve
        remotes._credential_approve = lambda url, cred: True
        self.addCleanup(setattr, remotes, "_credential_approve", self._orig)
        stats = remotes.scrub([self.repo], save_to_store=True,
                              enforce_scope=False)
        self.assertNotIn(TOKEN, repr(stats))
        self.assertEqual(stats["cleaned"], 1)


class TheScopeGateStillHolds(TempTreeTestCase):
    """W8-2's instance gate must survive the reader rewrite."""

    def test_scrub_still_filters_by_instance(self):
        repo = self.make_repo("grp/scoped", origin=None)
        write_stanza(repo, remote_stanza(
            "origin", "https://oauth2:%s@git.example.net/a.git" % TOKEN))
        scope = {"name": "me", "folder": "", "base": self.dest,
                 "root": self.dest,
                 "others": [{"name": "other", "folder": "grp",
                             "root": os.path.join(self.dest, "grp")}]}
        stats = remotes.scrub([repo], save_to_store=False, scope=scope)
        self.assertEqual(stats["cleaned"], 0)
        self.assertEqual(stats["skipped_other_instance"], 1)
        self.assertIn(TOKEN, config_text(repo), "a foreign repo was rewritten")


if __name__ == "__main__":
    unittest.main(verbosity=2)
