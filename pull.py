# pull.py — Smart sync of every project (in parallel)
#
# Network fetches run in parallel (they are the slow part), but the decision
# making and the merge happen one repo at a time — so two processes never
# touch the same repo at once.

import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

import error_catalog
import git as _git
import paths
from git import (
    sync_repo,
    S_UP_TO_DATE, S_PULLED, S_AHEAD, S_DIVERGED,
    S_DIRTY, S_DETACHED, S_NO_REMOTE, S_GONE, S_AUTH, S_ERROR,
)
try:
    from git import S_LFS
except ImportError:                                     # pragma: no cover
    S_LFS = "lfs_incomplete"      # contract value; git.py owns the constant
# git.py owns these too; the fallback only keeps the import safe while a
# sibling change lands. Values are the WAVE 2 contract strings.
S_EMPTY  = getattr(_git, "S_EMPTY", "empty_remote")
S_PURGED = getattr(_git, "S_PURGED", "purged")
from printer import (
    print_section, print_info, print_warn, print_separator,
    GREEN, RED, YELLOW, CYAN, BOLD, DIM, RESET
)

# Number of concurrent fetches. Any higher and the server starts rate-limiting.
DEFAULT_WORKERS = 8
MAX_WORKERS     = 16

_LABELS = {
    S_UP_TO_DATE: ("✓ sync",      DIM),
    S_PULLED:     ("↓ pulled",    GREEN),
    S_AHEAD:      ("↑ ahead",     CYAN),
    S_DIVERGED:   ("⇅ diverged",  YELLOW),
    S_DIRTY:      ("✎ dirty",     YELLOW),
    S_DETACHED:   ("⚬ detached",  DIM),
    S_NO_REMOTE:  ("∅ no remote", DIM),
    S_GONE:       ("⊘ gone",      YELLOW),
    S_AUTH:       ("🔒 auth",     RED),
    S_ERROR:      ("✖ error",     RED),
    S_LFS:        ("⚠ lfs",       YELLOW),
    S_EMPTY:      ("○ empty",     DIM),
    S_PURGED:     ("⊘ purged",    DIM),
}

# Expected states, not problems: never in "Needs attention", never logged.
# S_EMPTY  = the project exists on the server but nobody has pushed yet.
# S_PURGED = GitLab finished a delayed deletion; the clone may be the last
#            copy. Listed once in a dim "Info" block after the problems.
_EMPTY_TEXT = "empty repository on the server"
_INFO_STATES = (S_PURGED,)

# Used only until error_catalog grows its "purged" entry (contract text).
_PURGED_INFO = error_catalog.ErrorInfo(
    code="purged", type="NotFound", title="Purged from the server",
    cause=("The project was in GitLab delayed deletion and is now gone; "
           "this clone may be the last copy."),
    fix="Archive this folder (mv it aside) if you still need it; nothing to sync.",
)

# Repos listed in the end-of-run "needs attention" block. Detached and
# no-remote repos are included: they were skipped, so nothing was synced.
_ATTENTION = (S_AHEAD, S_DIVERGED, S_DIRTY, S_DETACHED, S_NO_REMOTE,
              S_GONE, S_AUTH, S_ERROR, S_LFS)

# Not failures: the repo is usable, something is merely incomplete/pending.
# S_LFS = history and work tree are fine, only LFS file contents are missing.
_WARNINGS = (S_LFS, S_AHEAD)

# The repo's own state, decided by git.py's sync table. Their git.py detail
# is already short and specific ("origin: ↑2 ↓1"), so it IS the cause.
_LOCAL_STATES = (S_AHEAD, S_DIVERGED, S_DIRTY, S_DETACHED, S_NO_REMOTE, S_GONE)

# Status -> catalog code where the status alone determines the problem.
_STATUS_CODE = {
    S_DIRTY:     "dirty_tree",
    S_DIVERGED:  "diverged",
    S_DETACHED:  "detached",
    S_NO_REMOTE: "no_upstream",
    S_GONE:      "not_found",
}

# "ahead" is not an error, so the catalog has no entry for it.
_AHEAD_INFO = error_catalog.ErrorInfo(
    code="ahead", type="Local", title="Unpushed commits",
    cause="Local commits are not on any remote yet.",
    fix="Push them when ready — sync never pushes.",
)

# A repo with no remote at all (local-only or broken clone). "no_upstream"
# is kept for the other S_NO_REMOTE case: no remote has the current branch.
_NO_REMOTE_INFO = error_catalog.ErrorInfo(
    code="no_remote", type="Local", title="No remote configured",
    cause="The repo has no remote, so there is nothing to sync from.",
    fix="Add one with `git remote add origin <url>`, or ignore it if the repo is local-only.",
)

# Summary order: real failures, then repos skipped by the sync table, then
# warnings (the repo is usable).
_RANK = {S_ERROR: 0, S_AUTH: 0, S_GONE: 0}

_CAUSE_MAX = 110       # one short line per repo (catalog cause is <= 110)
_GROUP_MAX = 10        # repo lines shown per group in the summary


def _find_git_repos(base_path: str) -> list[str]:
    """Every git work tree at or below `base_path`, including nested ones.

    The previous implementation did ``dirs[:] = []`` as soon as it saw a
    ``.git``, so it stopped at the first repository on each branch of the
    tree. On a real tree that hid dozens of repositories from both ``pull``
    and the ``remotes`` credential audit — and a
    submodule or vendored checkout cloned with a token in its URL was
    therefore invisible to the audit. So we keep descending.

    Three properties are preserved deliberately:

    * **Never descend into a ``.git`` directory itself.** That is where the
      bulk of the files live (``.git/objects`` alone is most of the tree), it
      can never contain a work tree, and pruning it is what keeps a full walk
      cheap — measured ~0.6 s over hundreds of repos / hundreds of thousands of
      files, versus minutes if it were walked.
    * **Never follow symlinks.** ``os.walk`` does not follow them by default
      and we do not turn that on; on top of that every directory is keyed by
      ``(st_dev, st_ino)`` so a bind mount or any other cycle is visited once
      and then pruned, instead of looping forever.
    * **A ``.git`` *file* counts too** — that is how git records a submodule
      work tree or a ``git worktree`` checkout, and those have remotes (and
      therefore possible credentials) just like a normal clone.
    """
    repos: list[str] = []
    seen: set[tuple[int, int]] = set()

    def _key(path: str):
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError:
            return None
        return (st.st_dev, st.st_ino)

    root_key = _key(base_path)
    if root_key is not None:
        seen.add(root_key)

    for root, dirs, files in os.walk(base_path, topdown=True,
                                     followlinks=False, onerror=None):
        is_repo = False

        # A .git directory marks a repo; a .git file marks a submodule or a
        # linked worktree. Either way this directory is a work tree.
        if ".git" in dirs:
            is_repo = True
            dirs.remove(".git")       # never walk into .git — huge and useless
        elif ".git" in files:
            is_repo = True

        if is_repo:
            repos.append(root)
            # NOTE: no `dirs[:] = []` here — nested repositories are real and
            # must be discovered. See the docstring.

        # Prune symlinked directories and anything we have already visited,
        # so a cycle can never make this walk run forever.
        kept = []
        for d in dirs:
            p = os.path.join(root, d)
            if os.path.islink(p):
                continue
            k = _key(p)
            if k is None or k in seen:
                continue
            seen.add(k)
            kept.append(d)
        dirs[:] = kept

    return sorted(repos)


def _resolve_local_path(group_path: str, dest: str,
                        folder: str | None = None) -> str:
    """Local path of a group — shared logic lives in paths.py.

    `folder` is the active instance's `folder` setting (cfg["folder"]) and is
    AUTHORITATIVE. Dropping it made paths.is_instance_folder() fall back to
    paths.configured_folders(), i.e. to whatever ~/.gitlab-cli/instances.json
    happened to say — a global pool shared by every instance. Two ways that
    lies, both measured on a real setup:

      * the key is missing (config drift): the folder candidate is never
        generated, so Sync reported "folder does not exist" for most of the
        groups whose clones sit under <dest>/acme/<group>/;
      * the key belongs to ANOTHER instance: its value gates the shaping
        rules for this one.

    None keeps the old fall-back, so a caller without a cfg is unaffected.
    """
    return paths.resolve(group_path, dest, folder)


_ANSI_RE = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


def _visible_len(text: str) -> int:
    """Terminal columns `text` occupies: ANSI escapes are zero-width, East
    Asian wide / fullwidth characters (and most emoji) take two columns."""
    n = 0
    for ch in _ANSI_RE.sub("", text or ""):
        if unicodedata.combining(ch):
            continue
        n += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return n


def _pad(text: str, width: int) -> str:
    """Left-justify on VISIBLE width. str.ljust/format count the escape
    bytes of the dim "/" separators, so nested paths came out short."""
    return text + " " * max(0, width - _visible_len(text))


def _purged_info() -> "error_catalog.ErrorInfo":
    return error_catalog.by_code("purged") or _PURGED_INFO


class SyncAttention(list):
    """The `attention` list returned by sync_many() — a plain list of the
    repos that need attention (unchanged contract). `.info` carries the
    expected-state results (purged) for the Info block; they are NOT in the
    list, so len(attention) stays the needs-attention count."""

    def __init__(self, *args):
        super().__init__(*args)
        self.info: list[dict] = []


def _clip(text: str, limit: int = _CAUSE_MAX) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _info(r: dict) -> "error_catalog.ErrorInfo":
    """Catalog entry for one problem result (never None)."""
    st, detail = r["status"], r.get("detail") or ""
    if st == S_AHEAD:
        return _AHEAD_INFO
    if st == S_NO_REMOTE and "has branch" not in detail:
        return _NO_REMOTE_INFO
    code = _STATUS_CODE.get(st)
    if code is None:
        found = error_catalog.explain(detail)
        if st == S_AUTH and found.type not in ("Auth", "Access"):
            code = "auth"            # git.py already classified it as auth
        elif st == S_LFS and not found.code.startswith("lfs_"):
            code = "lfs_missing_object"
        else:
            return found
    return error_catalog.by_code(code) or error_catalog.explain(detail)


def _cause(r: dict, info=None) -> str:
    """One short, specific cause for a problem result."""
    info   = info or _info(r)
    detail = (r.get("detail") or "").strip()
    if r["status"] in _LOCAL_STATES and detail:
        # git.py appends its own hint after " — "; the fix is shown per group.
        cause = detail.split(" — ")[0]
    elif info.code == "unknown":
        cause = error_catalog.summarize(detail, _CAUSE_MAX) or info.cause
    else:
        cause = info.cause
    return _clip(cause)


def _short(r: dict) -> str:
    """Text after the status label on the live per-repo line."""
    st = r["status"]
    if st == S_EMPTY:
        return _EMPTY_TEXT
    if st == S_PURGED:
        return _clip(_purged_info().cause)
    if st in _ATTENTION:
        info = _info(r)
        if st in _LOCAL_STATES:
            return _cause(r, info)       # the label already names the state
        return f"{info.title} — {_cause(r, info)}"
    return _clip(r.get("detail") or "")


def _line(r: dict, base: str) -> str:
    """Render a single output line for one result."""
    st           = r["status"]
    label, color = _LABELS.get(st, _LABELS[S_ERROR])
    rel          = os.path.relpath(r["path"], base)
    name         = rel.replace(os.sep, f"{DIM}/{RESET}")
    branch       = f"{DIM}[{r['branch']}]{RESET} " if r.get("branch") else ""
    text         = _short(r)
    extra        = f" {DIM}{text}{RESET}" if text else ""
    if r.get("shallow"):
        extra += f" {DIM}· shallow (history incomplete){RESET}"
    out = f"  {_pad(name, 48)} {branch}{color}{label}{RESET}{extra}"
    if r.get("other"):
        out += f"\n      {DIM}branches behind: {', '.join(r['other'])}{RESET}"
    return out


def problem_line(r: dict, base: str) -> str:
    """'<relative repo path>  <status or catalog title> — <cause>' (plain)."""
    info = _info(r)
    rel  = os.path.relpath(r["path"], base)
    head = r["status"] if r["status"] in _LOCAL_STATES else info.title
    return f"{rel}  {head} — {_cause(r, info)}"


def group_problems(attention: list[dict]) -> list[tuple]:
    """[(ErrorInfo, [results])] — failures, then skipped, then warnings;
    within a tier by count, then title."""
    groups: dict[str, list] = {}
    infos:  dict[str, object] = {}
    for r in attention:
        info = _info(r)
        groups.setdefault(info.code, []).append(r)
        infos.setdefault(info.code, info)
    def _key(code):
        rs = groups[code]
        tier = min(2 if r["status"] in _WARNINGS else _RANK.get(r["status"], 1)
                   for r in rs)
        return (tier, -len(rs), infos[code].title)
    return [(infos[c], sorted(groups[c], key=lambda r: r["path"]))
            for c in sorted(groups, key=_key)]


def _print_groups(heading: str, color: str, groups: list, base: str):
    n = sum(len(rs) for _, rs in groups)
    print(f"\n{BOLD}{color}{heading} — {n} repo(s):{RESET}")
    for info, rs in groups:
        print(f"  {BOLD}{info.title}{RESET} {DIM}·{RESET} {len(rs)}")
        for r in rs[:_GROUP_MAX]:
            print(f"    {problem_line(r, base)}")
        if len(rs) > _GROUP_MAX:
            print(f"    {DIM}… +{len(rs) - _GROUP_MAX} more "
                  f"(listed above){RESET}")
        print(f"    {CYAN}fix:{RESET} {info.fix}")


def _print_info(results: list[dict], base: str):
    """One short dim block per expected state: title · count, cause once,
    the repo paths, ONE fix line. Not a failure, not an attention item."""
    if not results:
        return
    print(f"\n{BOLD}{DIM}Info — {len(results)} repo(s):{RESET}")
    info = _purged_info()
    rs = sorted(results, key=lambda r: r["path"])
    print(f"  {DIM}{BOLD}{info.title}{RESET} {DIM}· {len(rs)}{RESET}")
    print(f"    {DIM}{_clip(info.cause)}{RESET}")
    for r in rs[:_GROUP_MAX]:
        print(f"    {DIM}{os.path.relpath(r['path'], base)}{RESET}")
    if len(rs) > _GROUP_MAX:
        print(f"    {DIM}… +{len(rs) - _GROUP_MAX} more (listed above){RESET}")
    print(f"    {DIM}fix: {info.fix}{RESET}")


def _print_summary(stats: dict, attention: list[dict], base: str,
                   info: list[dict] | None = None):
    print_separator()
    print(f"{BOLD}Summary:{RESET}")
    order = [S_PULLED, S_UP_TO_DATE, S_EMPTY, S_AHEAD, S_LFS, S_DIVERGED,
             S_DIRTY, S_DETACHED, S_NO_REMOTE, S_GONE, S_PURGED, S_AUTH,
             S_ERROR]
    total = sum(stats.values())
    for st in order + sorted(set(stats) - set(order)):
        n = stats.get(st, 0)
        if not n:
            continue
        label, color = _LABELS.get(st, (st, DIM))
        print(f"  {color}{_pad(label, 12)}: {n}{RESET}")
    print(f"  {DIM}{'─'*20}{RESET}\n  {BOLD}{'total':<12}: {total}{RESET}")

    if info is None:
        info = getattr(attention, "info", [])
    # Defensive: an expected state handed in via `attention` is still not
    # a problem — route it to Info instead of "Needs attention".
    info = list(info) + [r for r in attention if r["status"] in _INFO_STATES]
    attention = [r for r in attention
                 if r["status"] not in _INFO_STATES and r["status"] != S_EMPTY]
    problems = [r for r in attention if r["status"] not in _WARNINGS]
    warnings = [r for r in attention if r["status"] in _WARNINGS]
    if problems:
        _print_groups("Needs attention", YELLOW, group_problems(problems), base)
    if warnings:
        _print_groups("Warnings", DIM, group_problems(warnings), base)
    _print_info(info, base)
    print()


def sync_many(repos: list[str], base: str, workers: int = DEFAULT_WORKERS,
              show_all: bool = True) -> tuple[dict, list]:
    """
    Sync several repos in parallel.
    Returns (stats, attention).
    """
    workers = max(1, min(workers, MAX_WORKERS, len(repos)))
    stats, attention, done = {}, SyncAttention(), 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(sync_repo, p): p for p in repos}

        for fut in as_completed(futures):
            path = futures[fut]
            done += 1
            try:
                r = fut.result()
            except Exception as e:                      # pragma: no cover
                r = {"path": path, "status": S_ERROR,
                     "detail": error_catalog.summarize(str(e)),
                     "branch": None, "other": []}

            st = r["status"]
            stats[st] = stats.get(st, 0) + 1
            if st in _ATTENTION:
                attention.append(r)
            elif st in _INFO_STATES:
                attention.info.append(r)

            # Repos already in sync are only counted — keeps the output clean
            if show_all or st != S_UP_TO_DATE:
                print(f"\r\033[K{_line(r, base)}")

            print(f"\r{DIM}  … {done}/{len(repos)}{RESET}", end="", flush=True)

    print("\r\033[K", end="")
    return stats, attention


def cmd_pull(cfg: dict, group_path: str, dest: str = ".",
             workers: int = DEFAULT_WORKERS, quiet: bool = False):
    local_path = _resolve_local_path(group_path, dest, cfg.get("folder"))

    print_section(f"Sync: {group_path}")
    print_info(f"Folder: {os.path.abspath(local_path)}")
    print_separator()

    if not os.path.exists(local_path):
        print_warn(f"Folder '{local_path}' does not exist. Clone it first.")
        return

    repos = _find_git_repos(local_path)
    if not repos:
        print_warn("No git repos found.")
        return

    w = max(1, min(workers, MAX_WORKERS, len(repos)))
    print_info(f"{len(repos)} repos · {w} in parallel\n")

    stats, attention = sync_many(repos, local_path, workers=w,
                                 show_all=not quiet)
    _print_summary(stats, attention, local_path)
