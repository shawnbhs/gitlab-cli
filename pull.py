# pull.py — Smart sync of every project (in parallel)
#
# Network fetches run in parallel (they are the slow part), but the decision
# making and the merge happen one repo at a time — so two processes never
# touch the same repo at once.

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import paths
from git import (
    sync_repo,
    S_UP_TO_DATE, S_PULLED, S_AHEAD, S_DIVERGED,
    S_DIRTY, S_DETACHED, S_NO_REMOTE, S_GONE, S_AUTH, S_ERROR,
)
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
}

_ATTENTION = (S_AHEAD, S_DIVERGED, S_DIRTY, S_GONE, S_AUTH, S_ERROR)


def _find_git_repos(base_path: str) -> list[str]:
    repos = []
    for root, dirs, _ in os.walk(base_path):
        if ".git" in dirs:
            repos.append(root)
            dirs[:] = []          # don't descend into submodules / nested repos
    return sorted(repos)


def _resolve_local_path(group_path: str, dest: str) -> str:
    """Local path of a group — shared logic lives in paths.py."""
    return paths.resolve(group_path, dest)


def _line(r: dict, base: str) -> str:
    """Render a single output line for one result."""
    st           = r["status"]
    label, color = _LABELS.get(st, _LABELS[S_ERROR])
    rel          = os.path.relpath(r["path"], base)
    name         = rel.replace(os.sep, f"{DIM}/{RESET}")
    branch       = f"{DIM}[{r['branch']}]{RESET} " if r.get("branch") else ""
    extra        = f" {DIM}{r['detail']}{RESET}" if r.get("detail") else ""
    out = f"  {name:<48} {branch}{color}{label}{RESET}{extra}"
    if r.get("other"):
        out += f"\n      {DIM}branches behind: {', '.join(r['other'])}{RESET}"
    return out


def _print_summary(stats: dict, attention: list[dict], base: str):
    print_separator()
    print(f"{BOLD}Summary:{RESET}")
    order = [S_PULLED, S_UP_TO_DATE, S_AHEAD, S_DIVERGED,
             S_DIRTY, S_DETACHED, S_NO_REMOTE, S_GONE, S_AUTH, S_ERROR]
    total = sum(stats.values())
    for st in order:
        n = stats.get(st, 0)
        if not n:
            continue
        label, color = _LABELS[st]
        print(f"  {color}{label:<12}: {n}{RESET}")
    print(f"  {DIM}{'─'*20}{RESET}\n  {BOLD}{'total':<12}: {total}{RESET}")

    if attention:
        print(f"\n{BOLD}{YELLOW}Needs attention:{RESET}")
        for r in sorted(attention, key=lambda x: x["status"]):
            label, color = _LABELS[r["status"]]
            rel = os.path.relpath(r["path"], base)
            print(f"  {color}{label:<12}{RESET} {BOLD}{rel}{RESET} "
                  f"{DIM}{r['detail']}{RESET}")
    print()


def sync_many(repos: list[str], base: str, workers: int = DEFAULT_WORKERS,
              show_all: bool = True) -> tuple[dict, list]:
    """
    Sync several repos in parallel.
    Returns (stats, attention).
    """
    workers = max(1, min(workers, MAX_WORKERS, len(repos)))
    stats, attention, done = {}, [], 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(sync_repo, p): p for p in repos}

        for fut in as_completed(futures):
            path = futures[fut]
            done += 1
            try:
                r = fut.result()
            except Exception as e:                      # pragma: no cover
                r = {"path": path, "status": S_ERROR, "detail": str(e)[:120],
                     "branch": None, "other": []}

            st = r["status"]
            stats[st] = stats.get(st, 0) + 1
            if st in _ATTENTION:
                attention.append(r)

            # Repos already in sync are only counted — keeps the output clean
            if show_all or st != S_UP_TO_DATE:
                print(f"\r\033[K{_line(r, base)}")

            print(f"\r{DIM}  … {done}/{len(repos)}{RESET}", end="", flush=True)

    print("\r\033[K", end="")
    return stats, attention


def cmd_pull(cfg: dict, group_path: str, dest: str = ".",
             workers: int = DEFAULT_WORKERS, quiet: bool = False):
    local_path = _resolve_local_path(group_path, dest)

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
