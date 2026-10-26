# log.py — Show recent commits across all local projects (tree view)

import os
import re
import shutil
import subprocess
import tempfile
import datetime

import error_catalog
import paths
from git import get_commits_since
from printer import (
    print_section, print_info, print_warn, print_error, print_separator,
    print_dim, BOLD, CYAN, YELLOW, GREEN, DIM, RESET
)

# Tree drawing chars
T  = "├──"
L  = "└──"
V  = "│  "
SP = "   "

GIT_BIN = shutil.which("git") or "git"

# A date probe is a local, repository-only git invocation. 20s is already
# absurdly generous; it exists only so a wedged git cannot hang the screen.
_PROBE_TIMEOUT = 20

# Per-repo lines (git failures, empty repositories) shown before "… and N
# more". The screen is a commit tree; a wall of diagnostics would bury it.
_SHOW = 10


# ───────────────────────────────────────────────────────────────────────────
# WHY THIS FILE VALIDATES BEFORE IT REPORTS
#
# `git log --since=<value>` does NOT fail on an unparseable date. git's
# approxidate parser has no error channel: anything it cannot understand
# silently becomes "now", the command exits 0, and it prints nothing.
# Measured on git 2.43.0 in this repository:
#
#     git rev-parse --since='b'            -> --max-age=<now>      rc=0
#     git rev-parse --since='not a date'   -> --max-age=<now>      rc=0
#     git rev-parse --since='--all'        -> --max-age=<now>      rc=0
#     git rev-parse --since='2 weeks ago'  -> --max-age=<now-14d>  rc=0
#
# So `git log --all --since=b` printed 0 lines on a repository with 19 commits
# and exited 0, and this screen turned that into "No commits found since 'b'".
# That is the project's recurring defect in its purest form: the result was
# computed by one mechanism (git's silent fallback to "now") and reported by
# another (a claim about the user's history).
#
# The fix has to separate three outcomes that all used to print one sentence:
#
#   1. "I did not understand what you typed"       -> refuse, query nothing
#   2. "your filter matched nothing"               -> only after verifying
#                                                     the repos DO have history
#   3. "these repositories have no commits at all" -> say that instead
#
# HOW A VALUE IS JUDGED — and why it is not a strict allowlist
# ------------------------------------------------------------
# A hand-written allowlist of date syntax would trade a false negative for a
# false rejection: git accepts far more than anyone can enumerate ("tea time",
# "last monday", "1 fortnight ago" all resolve correctly). So acceptance has
# two independent doors and a value only has to pass ONE:
#
#   Door A — it matches a form we positively recognise (relative offsets, ISO
#            dates, epoch stamps, calendar keywords). Cheap, offline, exact.
#   Door B — git itself MOVED THE CLOCK for it. We resolve the value and the
#            literal string "now" in a single `git rev-parse` process, so both
#            are stamped at the same instant, and compare. Garbage resolves to
#            exactly "now"; anything git genuinely understood as a past moment
#            does not.
#
# A value is refused only when it fails BOTH doors. "now"/"today"/a date that
# happens to be today resolve to now but pass Door A, so they are accepted.
#
# And whatever is accepted, the ABSOLUTE date git settled on is echoed back,
# because the user cannot otherwise see what was applied. Silence is what
# produced the original bug.
# ───────────────────────────────────────────────────────────────────────────


class _InputError(ValueError):
    """A filter value refused before anything was queried.

    `cause` and `fix` are one short sentence each so the screen prints ONE
    line ("Title — cause → fix"). str(exc) stays the joined sentence, because
    members.py reuses these validators and prints str(exc) whole.
    """
    title = "Invalid input"

    def __init__(self, cause: str, fix: str = ""):
        self.cause = cause
        self.fix = fix
        super().__init__(f"{cause} {fix}".strip())

    def line(self) -> str:
        text = f"{self.title} — {self.cause}"
        return f"{text} → {self.fix}" if self.fix else text


class SinceError(_InputError):
    """The Since value cannot be trusted as a date."""
    title = "Bad 'Since' date"


class AuthorError(_InputError):
    """The Author value is not a pattern git will accept."""
    title = "Bad author pattern"


_SINCE_FIX = "Use 1d, 2w, '2 weeks ago', yesterday or 2026-01-01."
_AUTHOR_FIX = "Escape regex characters such as [ ( \\ or leave them out."


def _q(value: str, limit: int = 40) -> str:
    """repr() of a user value, clipped so one bad paste cannot fill the line."""
    value = str(value)
    if len(value) > limit:
        value = value[:limit - 1] + "…"
    return repr(value)


def parse_since(raw: str) -> str:
    raw = raw.strip().lower()
    shortcuts = {"d": "day", "w": "week", "m": "month", "y": "year"}
    match = re.match(r"^(\d+)([dwmy])$", raw)
    if match:
        num  = int(match.group(1))
        unit = shortcuts[match.group(2)]
        if num > 1:
            unit += "s"
        return f"{num} {unit} ago"
    return raw


# ── Door A: forms we positively recognise ──────────────────────────────────

_UNIT = (r"(?:sec(?:ond)?|min(?:ute)?|hour|day|week|month|year|fortnight|"
         r"decade)s?")

# "2 weeks ago", "10 days", "3.days", "3.days.ago", "1 year 2 months ago"
_RELATIVE = re.compile(
    rf"^\d+[\s.]*{_UNIT}(?:[\s.]+\d+[\s.]*{_UNIT})*(?:[\s.]+ago)?$"
)

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday", "mon", "tue", "tues", "wed", "thu",
             "thur", "thurs", "fri", "sat", "sun")

_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december",
           "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
           "oct", "nov", "dec")

_KEYWORDS = {
    "now", "today", "yesterday", "midnight", "noon", "tea time", "teatime",
    "tea.time", "never",
}

_LAST_NEXT = re.compile(
    r"^(?:last|next|this)\s+"
    r"(?:second|minute|hour|day|week|month|year|fortnight|decade|"
    + "|".join(_WEEKDAYS) + "|" + "|".join(_MONTHS) + r")$"
)

_EPOCH = re.compile(r"^@?\d{9,12}$")

# Calendar formats a human actually types. Each is CALENDAR-validated below —
# "2026-13-45" matches the shape but is not a date, and git silently turns it
# into "10 days ago", which is exactly the class of surprise being removed.
_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
    "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
    "%m/%d/%Y",
    "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S %z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%b %d %Y", "%d %b %Y", "%b %d, %Y", "%B %d %Y", "%d %B %Y",
    "%b %Y", "%B %Y", "%Y",
)


def _is_calendar_date(value: str) -> bool:
    """True only if `value` is a REAL date in a format a human types."""
    v = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            datetime.datetime.strptime(v, fmt)
            return True
        except ValueError:
            continue
    # ISO 8601 with a numeric offset such as 2026-01-01T10:00:00+03:30
    try:
        datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _syntactically_known(value: str) -> bool:
    """Door A. Permissive on purpose: a False here is not a rejection."""
    norm = " ".join(value.strip().lower().split())
    if not norm:
        return False
    if norm in _KEYWORDS:
        return True
    if norm in _WEEKDAYS:
        return True
    if _LAST_NEXT.match(norm):
        return True
    if _RELATIVE.match(norm):
        return True
    if _EPOCH.match(norm):
        return True
    if _is_calendar_date(value):
        return True
    return False


# ── Door B: ask git, and read the clock it moved ───────────────────────────

_probe_dir = None


def _probe_gitdir(repos: list = None):
    """A git directory `git rev-parse` can run in.

    Prefers a repository we are about to query anyway; otherwise creates one
    throwaway bare repo per process (measured at ~5ms) under the system temp
    dir. Never inside the user's tree, and nothing of the user's is removed.
    """
    for repo in (repos or []):
        dotgit = os.path.join(repo, ".git")
        if os.path.exists(dotgit):
            return dotgit
    global _probe_dir
    if _probe_dir and os.path.isdir(_probe_dir):
        return _probe_dir
    try:
        tmp = tempfile.mkdtemp(prefix="gitlabcli_dateprobe_")
        bare = os.path.join(tmp, "probe.git")
        r = subprocess.run([GIT_BIN, "init", "-q", "--bare", bare],
                           capture_output=True, text=True,
                           timeout=_PROBE_TIMEOUT)
        if r.returncode != 0 or not os.path.isdir(bare):
            return None
        _probe_dir = bare
        return bare
    except Exception:
        return None


def _probe_env():
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_ASKPASS", "echo")
    return env


def _resolve_since(value: str, gitdir: str):
    """(epoch_of_value, epoch_of_now) as git resolves them, or (None, None).

    BOTH are resolved by ONE `git rev-parse` process so they share a single
    instant — that is what makes "did the clock move?" a reliable question
    rather than a race against the second boundary.

    The value travels as `--since=<value>`, ONE argv element, through
    subprocess WITHOUT a shell. It therefore cannot become a separate git
    option (`--since=--all` is the string "--all", not the option --all) and
    no shell metacharacter in it can reach a shell, because there is none.
    """
    try:
        r = subprocess.run(
            [GIT_BIN, "--git-dir=" + gitdir, "rev-parse",
             "--since=" + value, "--since=now"],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
            env=_probe_env(),
        )
    except Exception:
        return None, None
    if r.returncode != 0:
        return None, None
    stamps = [ln.split("=", 1)[1] for ln in r.stdout.splitlines()
              if ln.startswith("--max-age=")]
    if len(stamps) != 2:
        return None, None
    try:
        return int(stamps[0]), int(stamps[1])
    except ValueError:
        return None, None


def _human(epoch: int) -> str:
    try:
        return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "epoch %s" % epoch


def validate_since(raw: str, repos: list = None) -> tuple:
    """(parsed_value, resolved_epoch_or_None, human_note).

    Raises SinceError with a user-facing message when the value cannot be
    distinguished from garbage. Never returns a value it has not either
    recognised or watched git resolve.
    """
    if raw is None:
        raise SinceError("No 'Since' value was given.", _SINCE_FIX)

    parsed = parse_since(raw)

    if not parsed.strip():
        raise SinceError(
            "'Since' is empty; git would read it as 'now' and match nothing.",
            _SINCE_FIX)

    if any(ch in parsed for ch in "\0\n\r"):
        raise SinceError(
            "'Since' contains a control character; dates never do.",
            _SINCE_FIX)

    if parsed.lstrip().startswith("-"):
        raise SinceError(
            "%s looks like a command-line option, not a date." % _q(raw),
            _SINCE_FIX)

    known = _syntactically_known(parsed)

    gitdir = _probe_gitdir(repos)
    if gitdir is None:
        if known:
            return parsed, None, "not verified (no git available to resolve it)"
        raise SinceError(
            "git is unavailable and %s is not a date form this tool "
            "recognises." % _q(raw), _SINCE_FIX)

    epoch, now = _resolve_since(parsed, gitdir)

    if epoch is None:
        if known:
            return parsed, None, "not verified (git could not resolve it)"
        raise SinceError(
            "git could not resolve %s to a date." % _q(raw), _SINCE_FIX)

    # Door B: garbage lands EXACTLY on the instant git stamped as "now".
    # git reports no error for it — it silently filters on "now", which is
    # how this screen once answered "No commits found since 'b'" on a
    # repository full of commits.
    if not known and abs(epoch - now) <= 1:
        raise SinceError(
            "git does not understand %s as a date (it would silently use "
            "'now')." % _q(raw), _SINCE_FIX)

    note = _human(epoch)
    if not known:
        note += "  (interpreted loosely by git — check it is what you meant)"
    return parsed, epoch, note


def validate_author(author, repos: list = None):
    """Raise AuthorError if git will reject this --author pattern.

    Same defect shape as --since, different mechanism: git treats --author as
    a REGEX and exits 128 on a malformed one ("[" -> 'Invalid regular
    expression', "a\\" -> 'Trailing backslash'). git.get_commits_since()
    turns any non-zero exit into an empty list, so a typo'd bracket also came
    back as "No commits found". Checked once here with --max-count=0, which
    compiles the pattern and reads no history.
    """
    if author is None:
        return
    if any(ch in author for ch in "\0\n\r"):
        raise AuthorError("Author contains a control character.",
                          "Retype the author name or email.")
    gitdir = _probe_gitdir(repos)
    if gitdir is None:
        return
    try:
        r = subprocess.run(
            [GIT_BIN, "--git-dir=" + gitdir, "log", "--all",
             "--author=" + author, "--max-count=0"],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
            env=_probe_env(),
        )
    except Exception:
        return
    if r.returncode != 0:
        # The informative line is usually the LAST one (`fatal: ...`);
        # error_catalog.summarize picks it instead of dumping all of stderr.
        detail = (error_catalog.summarize(r.stderr or "", limit=80)
                  or "git rejected the pattern")
        raise AuthorError(
            "%s is not a valid regular expression: %s" % (_q(author), detail),
            _AUTHOR_FIX)


def _find_git_repos(base_path: str) -> list[str]:
    """Every git work tree under `base_path`.

    Delegates to coverage.find_repos rather than walking again. This module
    used to carry its own third implementation which had no symlink-cycle
    guard and recognised `.git` only as a DIRECTORY, so linked worktrees and
    submodules (a `.git` FILE holding a `gitdir:` pointer) were invisible to
    the log screen while being visible to clone and coverage. Three walkers
    that must agree will not stay in agreement; there is now one.
    """
    try:
        import coverage as _cov
        return _cov.find_repos(base_path)
    except Exception:
        # Never report an empty tree we failed to walk -- the caller prints
        # "No git repos found. Run Clone first!" on an empty list, which
        # would be a lie. Degrade to the local walk instead of to silence.
        repos = []
        for root, dirs, _ in os.walk(base_path):
            if ".git" in dirs:
                repos.append(root)
                dirs.remove(".git")
        return sorted(repos)


def _branch_color(branch: str) -> str:
    b = branch.lower()
    if b in ("main", "master"):
        return f"\033[32m{branch}\033[0m"      # green
    if b in ("develop", "development", "dev"):
        return f"\033[36m{branch}\033[0m"      # cyan
    if b.startswith("feature"):
        return f"\033[33m{branch}\033[0m"      # yellow
    if b.startswith("fix") or b.startswith("hotfix"):
        return f"\033[31m{branch}\033[0m"      # red
    if b in ("stage", "staging"):
        return f"\033[35m{branch}\033[0m"      # magenta
    return f"\033[37m{branch}\033[0m"          # white


def _failure_line(stderr: str) -> str:
    """ONE short line for a git failure: catalog title, cause, and fix.

    Never the raw stderr: an unrecognised failure shows the single most
    informative stderr line (error_catalog.summarize) as its cause, and no
    generic "ask your admin" fix, because that is not actionable.
    """
    info = error_catalog.explain(stderr)
    if info.code == "unknown":
        cause = error_catalog.summarize(stderr, limit=110) or info.cause
        return f"{info.title} — {cause}"
    line = f"{info.title} — {info.cause}"
    return f"{line} → {info.fix}" if info.fix else line


def _history_state(repo: str) -> tuple:
    """("history" | "empty" | "error", detail) for one repo, filter removed.

    git.get_commits_since() returns [] for an empty repository AND for a
    failing git, so a repo that matched nothing is asked once more here.
    `rev-list --all --max-count=1` reads at most one commit, and on an unborn
    HEAD it exits 0 with no output — an empty repository is a state, not an
    error, and is reported as one.
    """
    try:
        r = subprocess.run(
            [GIT_BIN, "-C", repo, "rev-list", "--all", "--max-count=1"],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
            env=_probe_env(),
        )
    except subprocess.TimeoutExpired:
        return "error", _failure_line(f"git timed out after {_PROBE_TIMEOUT}s")
    except OSError as exc:
        return "error", _failure_line(str(exc))
    if r.returncode != 0:
        return "error", _failure_line(r.stderr or f"git exited {r.returncode}")
    return ("history" if r.stdout.strip() else "empty"), ""


def _print_tree(group_path: str, repos: list[str], dest: str,
                since_parsed: str, author: str | None):
    """Build and print the full commit tree; return (total, matched_repos)."""

    # Group repos by their relative path structure
    # repo_path → list of commits
    repo_commits = {}
    for repo in repos:
        commits = get_commits_since(repo, since_parsed, author)
        if commits:
            rel = os.path.relpath(repo, dest)
            repo_commits[rel] = commits

    if not repo_commits:
        return 0, set()

    # Build folder tree structure
    # node: {name, children: {}, repos: [rel_path]}
    tree = {}
    for rel in repo_commits:
        parts = rel.split(os.sep)
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(f"__{part}", {})
        node[f"__REPO__{parts[-1]}"] = rel

    total = sum(len(v) for v in repo_commits.values())

    # Print root
    group_name = group_path.split("/")[-1]
    print(f"\n{BOLD}{CYAN}🌳 {group_name}{RESET}  "
          f"{DIM}({len(repo_commits)} repo, {total} commit){RESET}\n")

    def print_node(node: dict, prefix: str = "", depth: int = 0):
        items = list(node.items())
        for i, (key, val) in enumerate(items):
            is_last = (i == len(items) - 1)
            connector = L if is_last else T
            child_prefix = prefix + (SP if is_last else V)

            if key.startswith("__REPO__"):
                # It's a repo leaf
                repo_name = key[8:]
                rel = val
                commits = repo_commits[rel]
                count = len(commits)

                print(f"{prefix}{connector} {BOLD}{GREEN}📦 {repo_name}{RESET}  "
                      f"{DIM}({count} commit{'s' if count > 1 else ''}){RESET}")

                # Print commits
                for j, c in enumerate(commits):
                    c_last = (j == len(commits) - 1)
                    c_conn = L if c_last else T
                    c_prefix = child_prefix + (SP if c_last else V)

                    branch_str = _branch_color(c['branch'])

                    # Commit line
                    print(f"{child_prefix}{c_conn} {BOLD}{DIM}{c['hash']}{RESET}  "
                          f"[{branch_str}]")

                    # Message
                    msg = c['message']
                    if len(msg) > 72:
                        msg = msg[:69] + "..."
                    print(f"{c_prefix}{YELLOW}✎  {msg}{RESET}")

                    # Author + date
                    print(f"{c_prefix}{DIM}👤 {c['author']}   🕒 {c['date']}{RESET}")

                    if not c_last:
                        print(f"{c_prefix}")

            elif key.startswith("__"):
                # It's a folder/subgroup
                folder_name = key[2:]
                print(f"{prefix}{connector} {BOLD}{CYAN}📁 {folder_name}{RESET}")
                print_node(val, child_prefix, depth + 1)

    print_node(tree)
    matched = {repo for repo in repos
               if os.path.relpath(repo, dest) in repo_commits}
    return total, matched


def cmd_log(cfg: dict, group_path: str, dest: str = ".",
            since: str = "1d", author: str = None):
    group_name = group_path.split("/")[-1]
    # cfg["folder"] is authoritative for the shaping rules in paths.candidates;
    # without it paths.is_instance_folder() falls back to the global pool in
    # ~/.gitlab-cli/instances.json, which is either stale (folder key dropped →
    # "Folder does not exist" for a group that is fully cloned) or belongs to a
    # different instance. .get() so a caller without the key is unchanged.
    local_path = paths.resolve(group_path, dest, cfg.get("folder"))

    print_section(f"Log: {group_path}")

    # Validate BEFORE anything is queried and before anything is claimed.
    # A refusal here is an answer about the INPUT; it must never be able to
    # look like an answer about the user's commits.
    try:
        since_parsed, _since_epoch, since_note = validate_since(since)
        validate_author(author)
    except (SinceError, AuthorError) as exc:
        print_error(exc.line())
        print_dim("   Nothing was searched — this is not a statement about "
                  "your commits.")
        return

    print_info(f"Since : {since_parsed}")
    # Echo the ABSOLUTE moment git settled on. Without this the user cannot
    # see what was actually applied, which is how the original bug hid.
    print_info(f"        → commits after {since_note}")
    if author:
        print_info(f"Author: {author}")
    print_separator()

    if not os.path.exists(local_path):
        print_warn(f"Folder '{local_path}' does not exist. Clone it first.")
        return

    repos = _find_git_repos(local_path)
    if not repos:
        print_warn("No git repos found. Run Clone first!")
        return

    total, matched = _print_tree(group_path, repos, dest, since_parsed,
                                 author)

    # Verified, not assumed: a repo that matched nothing is either filtered
    # out (has history), empty (unborn HEAD — not an error), or unreadable
    # (git failed — say why, in one line).
    states = {r: _history_state(r) for r in repos if r not in matched}
    empty = [r for r, (s, _) in states.items() if s == "empty"]
    failed = [(r, d) for r, (s, d) in states.items() if s == "error"]
    with_history = len(matched) + sum(
        1 for s, _ in states.values() if s == "history")

    print()
    print_separator()
    for repo, line in failed[:_SHOW]:
        print_warn(f"{os.path.relpath(repo, dest)}: {line}")
    if len(failed) > _SHOW:
        print_warn(f"… and {len(failed) - _SHOW} more repos git could not read.")
    for repo in empty[:_SHOW]:
        print_dim(f"   {os.path.relpath(repo, dest)}  — empty repository")
    if len(empty) > _SHOW:
        print_dim(f"   … and {len(empty) - _SHOW} more empty repositories")

    if total == 0:
        if with_history == 0 and not failed:
            print_dim(
                f"{len(repos)} repos were searched and none of them has any "
                "commit at all — so this is not about your 'Since' filter.")
        elif with_history == 0:
            print_warn(
                f"{len(failed)} of {len(repos)} repos could not be read and "
                "the rest are empty — so this is not about your 'Since' "
                "filter.")
        else:
            who = f" by author '{author}'" if author else ""
            print_warn(
                f"No commits{who} after {since_note} "
                f"(from '{since_parsed}').")
            print_info(
                f"Verified: {with_history} of {len(repos)} repos DO have "
                "history, so the filter is what excluded them — widen it "
                "(e.g. 1y) to see more.")
    else:
        print(f"{BOLD}📊 Total: {GREEN}{total} commit{RESET}"
              f"{BOLD} across {len(matched)} repos{RESET}\n")
