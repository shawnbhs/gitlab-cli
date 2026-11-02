#!/usr/bin/env python3
# gitlab_cli.py — Main entry point
#
# The menu is built from the user's actual access level: anything your
# token is not allowed to do is either hidden or greyed out with a reason.
#
# Required Notice: Copyright (c) 2026 shawnbhs
# PolyForm Noncommercial License 1.0.0 — see LICENSE.md.
# Free for personal/educational use; commercial use is not permitted.

import os
import sys
import traceback
from collections import Counter
from urllib.parse import unquote, urlsplit

import caps
import coverage as cov
import error_catalog
import errors
import paths
import printer
import setup as setup_mod
import api
from api import ApiError, get_top_level_groups
from clone import cmd_clone, cmd_retry_failed
from pull import cmd_pull
from log import cmd_log
from members import cmd_members_list, cmd_member_actions
from errors import cmd_errors
from instances import pick_instance
from printer import (
    print_banner, print_error, print_info, print_warn, print_separator,
    BOLD, CYAN, GREEN, YELLOW, DIM, RESET, WHITE, RED
)


# ─── The destination boundary ──────────────────────────────────────────────────
#
# RC-4, second half. Wave 0 anchored `master_dir` inside instances.build_cfg,
# but that is a *caller*, not the boundary. Every command that accepts a
# destination used to hand raw argv (or the literal default ".") straight to
# clone/pull/log, so `gitlab-cli clone globex ./here` resolved against the
# shell's cwd — repos and the per-group error log landed somewhere the
# interactive menu, which only ever reads cfg["master_dir"], never looks at.
#
# _resolve_dest() is now the single door. Nothing downstream can receive a
# relative path, because the only place a destination enters the program is
# here. Three rules, in order of how often they fire:
#
#   1. No destination given  ->  cfg["master_dir"] (already anchored to the
#      install dir). The DEFAULT never touches the cwd. This is the actual
#      original bug: `dest="."` as a default value.
#
#   2. An explicit RELATIVE path -> resolved against the CWD, like every other
#      Unix tool. A user typing `./here` means the directory they are standing
#      in; silently anchoring it to the install dir would be a different
#      surprise, not a fix. It is made ABSOLUTE right here, so no downstream
#      code and no later os.chdir can re-resolve it differently.
#
#   3. Any destination that is not inside cfg["master_dir"] -> loud warning
#      naming the absolute path and stating that the menu will not list it.
#      The defect was never "relative"; it was "silent". Explicit is allowed,
#      silent is not.
#
# Rejecting an explicit relative path outright was considered and dropped: it
# would also break the legitimate `gitlab-cli clone g /mnt/backup/mirror`,
# which has exactly the same off-menu property and must stay possible.

def _resolve_dest(cfg: dict, raw=None, what: str = "destination") -> str:
    """Absolute destination. The one and only entry point for a dest value."""
    default = paths.anchor(cfg.get("master_dir") or ".")

    if raw is None or not str(raw).strip():
        return default

    d = str(raw).strip()
    if os.path.isabs(d):
        resolved = paths.anchor(d)                       # normalise; idempotent
    else:
        # Explicit relative: the user's cwd, resolved once, here.
        resolved = os.path.normpath(os.path.join(os.getcwd(), d))

    _warn_if_offsite(resolved, default, what)
    return resolved


def _is_inside(parent: str, child: str) -> bool:
    try:
        p = os.path.realpath(parent)
        c = os.path.realpath(child)
    except OSError:
        return False
    return c == p or c.startswith(p + os.sep)


def _warn_if_offsite(resolved: str, default: str, what: str):
    """Say it out loud when files will land outside the managed tree."""
    if _is_inside(default, resolved):
        return
    print_warn(f"{what} is outside the managed folder.")
    print(f"  {DIM}writing to : {RESET}{BOLD}{resolved}{RESET}")
    print(f"  {DIM}managed    : {default}{RESET}")
    print(f"  {YELLOW}The interactive menu only reads the managed folder, so "
          f"anything written here will not be listed there.{RESET}\n")


def _n(count: int, noun: str) -> str:
    """'1 group' / '2 groups' — a count of one never takes the plural."""
    return f"{count} {noun}{'' if count == 1 else 's'}"


def group_status(group: dict, dest: str) -> str:
    """Kept for compatibility — folder existence only. Prefer group_coverage."""
    local_path = os.path.join(dest, group["path"])
    return "✅" if os.path.exists(local_path) else "❌"


def scan_dest(dest: str) -> dict:
    """Index every clone under dest by its origin URL (one walk for the menu)."""
    return cov.scan(dest)


# ─── The action seam ──────────────────────────────────────────────────────────
#
# RC-2 was fixed in the LISTING code and nowhere else. The icon a group shows
# comes from coverage.scan(), which indexes clones by their ORIGIN URL and is
# therefore layout-agnostic: it finds the clone wherever on disk it sits. The
# ACTIONS behind that icon (Sync / Log / Remotes / Clone / Retry) each rebuilt
# the directory from scratch, with a weaker rule:
#
#   * Sync and Log called paths.resolve(gpath, dest) without the instance
#     folder, so on an instance whose clones live under a `folder` shape the
#     config no longer produces, most groups that render as ✅ answered
#     "Folder does not exist. Clone it first."
#   * Remotes did not use the resolver at all — os.path.join(dest, last
#     component of the group path). With two instances sharing one master_dir
#     that walked straight into the OTHER instance's tree and offered to
#     rewrite its remotes: 91 foreign repos audited under a screen labelled
#     with this instance's group.
#   * Clone / Retry likewise missed the existing tree, so "Clone All" would
#     have written a second copy of every repo already on disk.
#
# The cure is to stop deriving the directory a second way. Everything below
# starts from the SAME coverage index the icon was computed from, and only
# falls back to paths.resolve() (now WITH cfg["folder"]) when the index knows
# nothing about the group — i.e. when nothing is cloned yet and there is no
# disk fact to disagree with.
#
# cmd_pull / cmd_log / cmd_clone keep their (cfg, group_path, dest) signature:
# those files are not ours to change here. Instead the destination handed to
# them is chosen so that THEIR OWN resolver lands on the directory the index
# proved — and that is verified by calling their resolver before the call is
# made, under both folder=None (what they do today) and folder=cfg["folder"]
# (what they will do if they are fixed later). A pair that cannot be verified
# is never used; the old behaviour is the fallback.

def _group_dirs_from_index(cfg: dict, gpath: str, dest: str,
                           index: dict) -> list[str]:
    """Directories that really hold clones of this group, busiest first."""
    if not index:
        return []
    host   = cov.host_of(cfg["url"])
    prefix = (gpath or "").strip().strip("/").lower()
    if not prefix:
        return []
    dest_n = os.path.normpath(dest)
    counts: dict = {}
    for key, local_dir in index.items():
        try:
            h, path = key
        except (TypeError, ValueError):
            continue
        if h != host:
            continue
        if not (path == prefix or path.startswith(prefix + "/")):
            continue
        # Strip exactly the project's own components back off the local dir,
        # which leaves the directory that stands for the GROUP itself.
        extra = len(path.split("/")) - len(prefix.split("/"))
        d = os.path.normpath(local_dir)
        for _ in range(max(0, extra)):
            nd = os.path.dirname(d)
            if not nd or nd == d:
                break
            d = nd
        # Never climb out of (or all the way back up to) the managed folder:
        # a flattened layout would otherwise hand an action the whole tree,
        # including another instance's clones.
        if not d or os.path.normpath(d) == dest_n or not _is_inside(dest_n, d):
            continue
        counts[d] = counts.get(d, 0) + 1
    return [d for d, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _holds_group(cfg: dict, gpath: str, d: str, sample: int = 8) -> bool:
    """Whether directory d really holds clones of THIS group on THIS instance.

    scan() maps one origin URL to one directory, so when the same project is
    cloned twice only one copy is in the index — that must not decide which of
    two equally valid trees an action uses. This asks the directory itself,
    reading at most `sample` origins so a 150-repo tree is still cheap.

    It is also what rejects the cross-instance collision: the colliding
    directory exists and is full of repos, but none of them belong here.
    """
    host   = cov.host_of(cfg["url"])
    prefix = (gpath or "").strip().strip("/").lower()
    if not prefix:
        return False
    repos = cov.find_repos(d)[:sample]
    for r in repos:
        key = cov.normalize(cov._origin(r))
        if not key:
            continue
        h, path = key
        if h == host and (path == prefix or path.startswith(prefix + "/")):
            return True
    return False


def _group_dir(cfg: dict, gpath: str, dest: str, index: dict = None) -> str:
    """The one directory an action must operate on for this group.

    Precedence, and why:

    1. The FIRST path the normal resolver offers that both exists AND is
       proven by the index to hold at least one clone of this group. The
       resolver stays authoritative whenever it is right — two identical
       trees on disk (the RC-3 duplication) must not start being picked
       differently just because a scan happened to record one of them.
    2. Otherwise the directory that actually holds the most clones of this
       group. This is the case the resolver gets wrong: a drifted layout
       (clones under a `folder` shape the config no longer produces), and the
       cross-instance collision where the resolver's answer exists but holds
       another instance's repos — it holds ZERO of ours, so rule 1 rejects it.
    3. Otherwise the resolver's suggestion, for the error message and for a
       group with nothing on disk yet.
    """
    owned = _group_dirs_from_index(cfg, gpath, dest, index or {})
    ownset = set(owned)
    try:
        cands = paths.candidates(gpath, dest, cfg.get("folder"))
    except paths.UnsafePath:
        cands = []
    for c in cands:
        c = os.path.normpath(c)
        if not os.path.isdir(c):
            continue
        if c in ownset or _holds_group(cfg, gpath, c):
            return c
    for d in owned:
        if os.path.isdir(d):
            return d

    # Nothing cheap worked. The menu always passes the index it drew the icon
    # from, so reaching here from the menu means the group really has nothing
    # on disk. The argv commands have no index — scan now rather than never:
    # the cost is paid only in the case that used to be wrong.
    if index is None:
        try:
            owned = _group_dirs_from_index(cfg, gpath, dest, cov.scan(dest))
        except Exception:
            owned = []
        for d in owned:
            if os.path.isdir(d):
                return d

    try:
        return paths.resolve(gpath, dest, cfg.get("folder"))
    except paths.UnsafePath:
        return os.path.normpath(dest)


def _resolves_to(fn, gpath: str, dst: str, target: str, folder) -> bool:
    """Whether the callee's own resolver lands on target, folder or not."""
    for f in (None, folder):
        try:
            if os.path.normpath(fn(gpath, dst, f)) != target:
                return False
        except (paths.UnsafePath, OSError, ValueError):
            return False
    return True


def _action_args(cfg: dict, gpath: str, gdir: str, dest: str) -> tuple:
    """(group_path, dest) to hand cmd_pull / cmd_log so they land on gdir."""
    target = os.path.normpath(gdir)
    base   = os.path.basename(target)
    parent = os.path.dirname(target)
    folder = cfg.get("folder")
    for gp, dst in ((gpath, target), (base, target),
                    (gpath, parent), (base, parent)):
        if not gp or not dst:
            continue
        if _is_inside(dest, dst) and _resolves_to(paths.resolve, gp, dst,
                                                  target, folder):
            return gp, dst
    return gpath, dest


def _clone_dest(cfg: dict, gpath: str, dest: str, index: dict = None) -> str:
    """dest for cmd_clone / cmd_retry_failed: reuse the tree, never duplicate."""
    gdir = _group_dir(cfg, gpath, dest, index)
    if not os.path.isdir(gdir):
        return dest                      # nothing on disk: normal shaping
    target = os.path.normpath(gdir)
    folder = cfg.get("folder")
    for dst in (dest, os.path.dirname(target), target):
        if not dst or not _is_inside(dest, dst):
            continue
        if _resolves_to(paths.target_for_clone, gpath, dst, target, folder):
            return dst
    return dest


def _reconcile_renames(cfg: dict, gpath: str, dest: str, index: dict = None):
    """Apply upstream renames for one group BEFORE a Sync acts on it.

    cmd_pull only ever looks at the group's CURRENT path, so a project (or the
    group itself) renamed on the server left its clone under the old path and
    Sync either skipped it or answered "Folder does not exist". This asks
    clone.reconcile_renames() to move every proven-renamed clone first.

    Returns the index the caller must use afterwards: a fresh coverage scan
    when something moved (the old one maps origins to the OLD directories),
    otherwise the index it was given. index=None (the argv commands) stays
    None so _group_dir does its own lazy scan of the tree as it is now.

    Never raises: a failed rename check costs one warning, never the Sync.
    Clone does not come through here — cmd_clone repairs renames itself
    inside _process_group, and a second pass would double the API cost.
    """
    import clone as _clone                 # attribute lookup at call time
    fn = getattr(_clone, "reconcile_renames", None)
    if fn is None:
        return index
    try:
        rows = fn(cfg, gpath, _clone_dest(cfg, gpath, dest, index))
    except Exception as exc:               # noqa: BLE001 - Sync must still run
        print_warn(f"Rename check skipped for {gpath}: {exc}")
        return index
    reason = getattr(_clone, "REASON_RENAMED", "renamed_upstream")
    moved = any(isinstance(r, dict) and r.get("reason") == reason
                for r in (rows or []))
    if not moved or index is None:
        return index
    try:
        return scan_dest(dest)
    except Exception as exc:               # noqa: BLE001
        print_warn(f"Rescan after rename failed for {gpath}: {exc}")
        return index


def _prune_leftovers(cfg: dict, gpath: str, dest: str, index: dict = None):
    """Remove provably-empty leftovers of one group AFTER a Sync.

    Calls clone.prune_leftovers() with the same dest cmd_clone would get.
    Returns the index to use afterwards (a fresh scan when something was
    removed). Never raises: a failure costs one warning line.
    """
    import clone as _clone                 # attribute lookup at call time
    fn = getattr(_clone, "prune_leftovers", None)
    if fn is None:
        return index
    try:
        rows = fn(cfg, gpath, _clone_dest(cfg, gpath, dest, index))
    except Exception as exc:               # noqa: BLE001 - never fail Sync
        print_warn(f"Leftover cleanup skipped for {gpath}: {exc}")
        return index
    if not rows or index is None:
        return index
    try:
        return scan_dest(dest)
    except Exception as exc:               # noqa: BLE001
        print_warn(f"Rescan after cleanup failed for {gpath}: {exc}")
        return index


def _sync_group(cfg: dict, gpath: str, dest: str, index: dict = None):
    """Rename pre-step, then cmd_pull on the directory the index proves.

    The one Sync path for Sync All, the group menu's Sync and `gitlab-cli
    pull <group>`. The directory is derived AFTER the reconcile, so a group
    whose clones were just moved is synced at its new location instead of
    failing cmd_pull's "Clone it first" check. Returns the index in use.
    """
    index = _reconcile_renames(cfg, gpath, dest, index)
    gdir = _group_dir(cfg, gpath, dest, index)
    gp, dst = _action_args(cfg, gpath, gdir, dest)
    cmd_pull(cfg, gp, dst)
    # Leftovers of a followed rename (empty folders, empty clones of
    # deleted projects) are cleaned only after the pull has run.
    index = _prune_leftovers(cfg, gpath, dest, index)
    return index


def _err_log_path(cfg: dict, gpath: str, dest: str, index: dict = None) -> str:
    """Where THIS group's clone-error log is, keyed on the full group path.

    clone._group_slug keys on the whole path ('initech/core' and 'globex/x/core'
    used to share one file); the menu still spelled it with the last component
    only, so the ⚠ marker and the Retry option read a name clone never writes
    as soon as a subgroup is selectable. One source of truth now, and the
    master-dir location is still honoured for logs written before this change.
    """
    from clone import _error_log_path as _p
    cdest = _clone_dest(cfg, gpath, dest, index)
    try:
        path = _p(cdest, gpath)
        if os.path.exists(path):
            return path
        legacy = _p(dest, gpath)
        return legacy if os.path.exists(legacy) else path
    except (paths.UnsafePath, OSError, ValueError):
        return os.path.join(dest, ".__unknown_errors.json")


# A group whose listing FAILED is UNKNOWN, never ✅. status_icon() calls a
# zero denominator "complete", which is correct for a group that genuinely owns
# no projects and catastrophic for one the server would not list.
UNKNOWN_ICON = "❓"


def group_coverage(cfg: dict, group: dict, index: dict) -> dict:
    """
    How much of a group is really on disk.

    A folder that exists proves nothing: a group with 30 projects and 1 clone
    used to read as "cloned" and was then excluded from Clone All forever,
    which is the whole reason a clone-everything run could report success and
    leave dozens of projects unfetched.
    """
    try:
        projects = cov.remote_projects(cfg, group)
        error = ""
    except Exception as exc:               # noqa: BLE001 - the menu must draw
        # W10. `projects = []` was the founding defect in miniature: an empty
        # denominator renders as ✅ complete, so a group the server REFUSED to
        # list printed the same icon as a fully mirrored one. The failure is
        # carried out of here now and the icon says UNKNOWN.
        projects = []
        error = "%s: %s" % (exc.__class__.__name__, exc)

    # Projects GitLab has scheduled for deletion are outside `total` (D5);
    # their count travels with the dict so the screen can say so.
    counts = cov.group_coverage_counts(cfg, group, index, projects)
    cloned, total = counts["cloned"], counts["total"]

    # api.PagedResult carries `.complete` (did the walk finish?), `.total`
    # (the server's own X-Total) and `.note` (what this listing does and does
    # not contain — archived filter, subgroup inclusion). All three were
    # discarded here, so the fraction reached the screen with no statement of
    # what it counts and no way to tell a complete answer from a short one.
    complete = (not error) and bool(getattr(projects, "complete", True))
    if not complete and not error:
        err = getattr(projects, "error", None)
        error = str(err) if err else "the project listing did not complete"

    return {"icon": (cov.status_icon(cloned, total) if complete
                     else UNKNOWN_ICON),
            "cloned": cloned, "total": total,
            "note": getattr(projects, "note", ""),
            "server_total": getattr(projects, "total", None),
            "complete": complete,
            "error": error,
            "pending_deletion": int(counts.get("pending_deletion", 0) or 0),
            "orphans": []}


# ─── Orphans: on disk, matching nothing the server lists ─────────────────
#
# W10. An ORPHAN is a work tree whose origin matches no project the instance
# currently reports. `globex/agent` is a real one. Coverage is a one-way
# comparison (remote -> disk), so an orphan is invisible in both directions: it
# is not missing, and it is not counted.
#
# THIS SCREEN USED TO ASK THE QUESTION ONCE PER TOP-LEVEL GROUP, and that is
# why an audit found many orphans on one instance's tree while the menu could
# only reach a few: a clone whose entire top-level group was dissolved server-side has
# no group left to iterate, so it was never even visited. The question is now
# asked ONCE, against the instance's flat project list.
#
# It also used to print every orphan as "404 on the server" after a single
# get_project() call per candidate. That sentence was wrong for most of them:
# GitLab follows its own rename redirect, so a renamed project answers 200 at
# its old path. coverage.classify_orphan() makes the distinction, keeps the
# HTTP status that produced it, and reports UNKNOWN rather than guessing
# "deleted" when the server did not answer usably — because "deleted upstream"
# is the sentence a user acts on with rm -rf.
#
# Reporting only. No move, no delete, no prompt.

def _instance_projects(cfg: dict):
    """(projects, note) — the ONE flat project list this screen is built on.

    setup._remote_projects() verifies its own count against the server's
    X-Total and returns projects=None with a note whenever the answer is
    UNKNOWN. Both the coverage roll-up and the orphan sweep read THIS list, so
    the two blocks on screen can never be computed from different populations.
    """
    try:
        return setup_mod._remote_projects(cfg)
    except Exception as exc:               # noqa: BLE001 - the menu must draw
        return None, "%s: %s" % (exc.__class__.__name__, exc)


def _instance_orphans(cfg: dict, index: dict, projects, probe: bool = True):
    """Classified orphans for the whole instance, or [] when unknowable.

    `projects is None` means the instance listing itself is UNKNOWN. Detecting
    orphans against an unknown project list would label every healthy clone an
    orphan — the same class of confident-wrong report this wave exists to end —
    so the sweep is SKIPPED and the caller discloses that it was.
    """
    if projects is None:
        return []
    try:
        return cov.instance_orphans(cfg, index, projects, probe=probe)
    except Exception:                      # noqa: BLE001 - never take the menu down
        return []


def _print_orphans(orphans):
    """The orphan block, compact: headline + one line per class, no paths.

    One renderer, owned by coverage.py. Zero is printed, not suppressed: a
    block that silently starts appearing is a block nobody notices. D7: the
    full per-path listing (40+ lines of /home/... on a real tree) buried the
    prompt, so it moved behind the group list's `o` key.
    """
    items = list(orphans or ())
    lines = cov.orphan_display_lines(items, detail=False)
    colour = YELLOW if items else DIM
    print()
    for i, line in enumerate(lines):
        print(f"  {colour if i == 0 else DIM}{line}{RESET}")


def _print_listing_disclosures(covmap: dict, proj_note: str,
                               new_failures: list):
    """What the numbers above this line do and do not include.

    Three things the API layer already knew and the screen never said:

      1. the SCOPE of every group listing (PagedResult.note) — whether archived
         projects are in the denominator, whether subgroups are;
      2. any listing that did NOT complete, named, with the reason — those
         groups carry ❓ and their projects are absent from the roll-up;
      3. failures recorded by api.record_failure() while this screen was built.
         `quiet=True` suppresses the console line, never the ledger, and the
         whole original defect was a failure nobody was told about.
    """
    scopes = sorted({(c.get("note") or "").strip()
                     for c in covmap.values() if (c.get("note") or "").strip()})
    for note in scopes:
        print(f"  {DIM}listing scope: {note}{RESET}")

    if proj_note:
        print(f"  {YELLOW}instance project list UNKNOWN — {proj_note}{RESET}")
        print(f"  {DIM}the orphan sweep was SKIPPED: comparing clones against "
              f"an unknown project list would call healthy clones orphans"
              f"{RESET}")

    bad = [(gid, c) for gid, c in covmap.items() if not c.get("complete", True)]
    if bad:
        print(f"  {RED}❓ {len(bad)} group listing(s) did not complete — their "
              f"projects are NOT in the totals above:{RESET}")
        for _gid, c in bad:
            print(f"    {DIM}{c.get('error') or 'reason not recorded'}{RESET}")

    if new_failures:
        print(f"  {YELLOW}⚠ {_failure_summary(new_failures)}{RESET}"
              f"  {DIM}· o = details{RESET}")


def _failure_summary(failures: list) -> str:
    """ONE line for N API failures: the count and the most common cause.

    The title comes from error_catalog, so it is the same title the Error Log
    shows for the same message. The full URLs are behind the `o` key (D7).
    """
    infos = [error_catalog.explain(str(f)) for f in failures]
    title, _n = Counter(i.title for i in infos).most_common(1)[0]
    if all(i.code == "unknown" for i in infos):
        title = error_catalog.summarize(str(failures[0]), 80)
    return (f"{len(failures)} API failure(s) were recorded while building "
            f"this screen — {title}")


def _project_path_of(url: str) -> str:
    """'group/sub/proj' for a /api/v4/projects/<encoded path> URL, else ''."""
    marker = "/api/v4/projects/"
    if marker not in (url or ""):
        return ""
    tail = url.split(marker, 1)[1].split("?", 1)[0]
    return unquote(tail).strip("/").lower()


def _failures_to_disclose(failures: list, orphans) -> list:
    """The API failures the screen still has to mention (D6).

    An orphan probe that answered 404 IS the orphan block's evidence — the
    orphan record already carries that HTTP status. Listing it again as an
    "API failure" reported one fact twice, with a URL nobody can act on.
    """
    probed = {(o.get("path") or "").strip("/").lower() for o in orphans or ()}
    probed.discard("")
    return [f for f in failures
            if _project_path_of(getattr(f, "url", "") or "") not in probed]


def _print_group_menu(incomplete: int, has_details: bool):
    """The key lines under the group list (redrawn after the `o` screen)."""
    print(f"\n  {GREEN}ca{RESET}  Clone All  — fetch every missing project"
          f"  {DIM}({_n(incomplete, 'group')} incomplete){RESET}")
    print(f"  {CYAN}pa{RESET}  Pull All   — sync every group that has clones")
    if has_details:
        print(f"  {CYAN}o{RESET}   Details    — orphan paths and API failures "
              f"in full")
    print(f"  {YELLOW}b{RESET}   back   {YELLOW}q{RESET}  exit")
    print(f"  {DIM}✅ complete  ◐ partial  ❌ nothing  ❓ listing failed  "
          f"⚠ has errors{RESET}\n")


def _print_screen_details(orphans, failures):
    """The `o` key: everything the compact group screen summarised, in full."""
    print_separator()
    if orphans:
        print()
        for line in cov.orphan_display_lines(list(orphans), limit=0,
                                             detail=True):
            print(f"  {DIM}{line}{RESET}")
    if failures:
        print(f"\n  {YELLOW}{len(failures)} API failure(s) recorded while "
              f"building the group list:{RESET}")
        for err in failures:
            print(f"    {_redacted(str(err))}")
            print(f"      {DIM}{error_catalog.one_line(str(err))}{RESET}")
    print_separator()
    try:
        input(f"{DIM}  Press Enter to return to the group list…{RESET}")
    except (KeyboardInterrupt, EOFError):
        print()


def _cap_info(cfg: dict) -> dict:
    """Capabilities of the active instance (cached)."""
    try:
        return caps.get(cfg)
    except Exception:
        return {"ok": False, "caps": {}, "is_admin": False, "error": ""}


def _as_index(choice: str):
    """Return `choice` as an int, or None when it is not a whole number.

    `str.isdigit()` is False for "-1" and "+2", which is what made a signed
    number fall out of the numeric branch and into the group-name search.
    """
    text = choice.strip()
    body = text[1:] if text[:1] in ("+", "-") else text
    if body.isdigit():
        return int(text)
    return None


_UNSET = object()


def _instance_coverage_counts(cfg: dict, index: dict, projects=_UNSET):
    """coverage.coverage_counts() for the active instance, or None when UNKNOWN.

    W9-2 / W8-6-§5.2. The group list's roll-up is a sum over
    coverage.group_coverage(), whose denominator is built from the recursive
    GROUP project list — a project in a USER namespace has no group above it
    and can therefore never enter that set. Doctor's denominator is the flat
    /projects list, which does contain them. Both numbers were printed BARE,
    two screens apart, and disagreed.

    THE PRE-PARTITION LIST IS LOAD BEARING. `setup._remote_projects()` returns
    the flat list before any policy filter. Handing `coverage_counts()` the
    post-partition list would zero `excluded_user` and put an unexplained
    number back on screen — the exact defect this replaces.

    Returns None on any failure: an UNKNOWN must never be rendered as a
    number, and the menu must still draw if the API is unreachable.
    """
    if projects is _UNSET:
        projs, note = _instance_projects(cfg)
    else:
        projs, note = projects, ""
    if not projs or note:
        return None
    try:
        counts = cov.coverage_counts(projs, index, cov.host_of(cfg["url"]))
    except Exception:                       # noqa: BLE001
        return None
    return counts if cov.coverage_consistent(counts) else None


def _orphans_under(orphans, full_path: str) -> list:
    """The instance orphan list, narrowed to one group's namespace.

    Narrowing a complete list is not the same as asking per group: the record
    for a clone whose top-level group no longer exists still EXISTS here, it
    simply does not match this prefix. That is the difference between 29 and 6.
    """
    pre = (full_path or "").strip("/").lower()
    if not pre:
        return []
    return [o for o in (orphans or ())
            if (o.get("path") or "") == pre
            or (o.get("path") or "").startswith(pre + "/")]


def pick_group(cfg: dict, dest: str) -> dict:
    print_info("Fetching groups...")
    groups = get_top_level_groups(cfg["url"], cfg["headers"])

    if not groups:
        print_error("No groups found.")
        print_info("The token probably has no access — try 'doctor'.")
        return {"_bulk": "none"}

    print_info("Checking what is already on disk...")
    index   = scan_dest(dest)

    # W10. Snapshot the failure ledger instead of clearing it: api records
    # EVERY failure it sees whether or not `quiet` printed a line, and a
    # screen that consumed one silently is the defect this whole repair is
    # about. Anything appended between here and the disclosure block was
    # caused by this screen.
    try:
        _failures_before = len(api.failures())
    except Exception:                      # noqa: BLE001
        _failures_before = 0

    covmap  = {g["id"]: group_coverage(cfg, g, index) for g in groups}
    statuses = {gid: c["icon"] for gid, c in covmap.items()}

    # ONE flat listing, shared by the coverage roll-up and the orphan sweep.
    projects, proj_note = _instance_projects(cfg)
    orphans = _instance_orphans(cfg, index, projects)

    print_separator()
    print(f"\n{BOLD}{WHITE}  Groups:{RESET}\n")

    for i, g in enumerate(groups, 1):
        status = statuses[g["id"]]
        c      = covmap[g["id"]]
        vis    = g.get("visibility", "")
        lock   = "🔒" if vis == "private" else "🌐"
        desc   = g.get("description") or ""
        dstr   = f"  {DIM}{desc[:45]}{RESET}" if desc else ""
        err_path = _err_log_path(cfg, g["full_path"], dest, index)
        err_mark = f" {RED}⚠{RESET}" if os.path.exists(err_path) else ""
        # W10. "empty" is an ANSWER. A group whose listing failed has a zero
        # denominator for the same reason a genuinely empty group does, and
        # printing the same word for both is the confident-wrong report this
        # wave removes.
        if not c.get("complete", True):
            cnt = f"  {RED}? unknown{RESET}"
        elif c["total"]:
            cnt = f"  {DIM}{c['cloned']}/{c['total']}{RESET}"
        else:
            cnt = f"  {DIM}empty{RESET}"
        print(f"  {CYAN}{i:>2}.{RESET}  {status}  {lock}  "
              f"{BOLD}{g['full_path']}{RESET}{cnt}{err_mark}{dstr}")

    done    = sum(c["cloned"] for c in covmap.values())
    total   = sum(c["total"] for c in covmap.values())
    # ❓ (listing failed) is included: clone can only ADD, and excluding a
    # group we know nothing about is how "Clone All" quietly became "clone
    # some".
    pending = [g for g in groups
               if statuses[g["id"]] in ("❌", "◐", UNKNOWN_ICON)]

    print(f"\n{DIM}  {'─'*45}{RESET}")

    # W9-2 / W8-6-§5.2. `done`/`total` is a sum over group_coverage(), so it
    # is STRUCTURALLY the group-namespace fraction and can never contain a
    # user-namespace project. Printed bare it contradicted Doctor's larger
    # denominator. cov.coverage_line() renders both in one labelled
    # sentence; when the instance total is unobtainable the fraction is still
    # printed with the noun, never bare.
    counts = _instance_coverage_counts(cfg, index, projects)
    # The same numbers the coverage line just printed, handed to Clone All so
    # its user-namespace line cannot contradict this screen. None = UNKNOWN.
    user_ns = ({"total": int(counts.get("user_total", 0)),
                "present": int(counts.get("user_present", 0))}
               if counts else None)
    if counts and counts.get("group_total") == total:
        # The two group denominators agree (recursive-per-group sum == flat
        # namespace.kind=='group' count), so the roll-up can be rendered by
        # the shared sentence. When they DISAGREE the accounting no longer
        # reconciles and inventing a combined line would be a third
        # unexplained number, so the labelled fraction is printed alone.
        counts = dict(counts)
        counts["group_present"] = done
        counts["missing"] = max(0, total - done)
        # Pending deletion gets its own dim line below, not a clause on this
        # one — the coverage sentence must stay readable at a glance.
        counts.pop("pending_deletion", None)
        line = cov.coverage_line(counts)
    else:
        # Still never bare: the trailing qualifier is what stops this being
        # read as coverage of the whole instance, which is the number Doctor
        # prints against a larger denominator.
        line = f"{done}/{total} projects on disk (group namespaces)"
    print(f"  {BOLD}  {line}{RESET}"
          + (f"  {YELLOW}— {total - done} missing{RESET}" if total > done else ""))
    pending_del = sum(int(c.get("pending_deletion", 0) or 0)
                      for c in covmap.values())
    if pending_del:
        print(f"  {DIM}{pending_del} project(s) scheduled for deletion on the "
              f"server — not counted{RESET}")

    try:
        _new_failures = api.failures()[_failures_before:]
    except Exception:                      # noqa: BLE001
        _new_failures = []
    failures = _failures_to_disclose(_new_failures, orphans)
    _print_listing_disclosures(covmap, proj_note, failures)
    _print_orphans(orphans)

    # `o` exists only when there is something behind it.
    has_details = bool(orphans or failures)
    keys = "ca / pa / o" if has_details else "ca / pa"
    _print_group_menu(len(pending), has_details)

    while True:
        try:
            choice = input(f"{BOLD}  Which group? (number / name / {keys}): {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{DIM}Goodbye!{RESET}")
            sys.exit(0)

        low = choice.lower()

        # D7 (W3-8 #7). Enter is the most common accidental keystroke there
        # is. Without this guard a blank line falls through every branch into
        # the substring search below, and `"" in anything` is True -- so the
        # menu dumps every group under a heading claiming the user searched
        # for something. A blank line means "I typed nothing": re-prompt.
        if not choice:
            print(f"  {DIM}Type a number, a group name, "
                  f"or {keys} / b / q.{RESET}")
            continue

        if low == "o" and has_details:
            _print_screen_details(orphans, failures)
            _print_group_menu(len(pending), has_details)
            continue

        if low == "q":
            print(f"{DIM}Goodbye!{RESET}")
            sys.exit(0)

        if low == "b":
            return {"_bulk": "none"}

        if low == "ca":
            # Partial groups (◐) must be included. Excluding them is what made
            # "clone everything" a no-op on a group that was 1% cloned.
            not_cloned = [g for g in groups
                          if statuses[g["id"]] in ("❌", "◐", UNKNOWN_ICON)]
            if not not_cloned:
                print(f"  {GREEN}✔ Every project is already cloned!{RESET}\n")
                continue
            return {"_bulk": "clone_all", "_groups": not_cloned,
                    "_index": index, "_user_ns": user_ns}

        if low == "pa":
            cloned = [g for g in groups if statuses[g["id"]] in ("✅", "◐")]
            if not cloned:
                print(f"  {YELLOW}⚠  No group has been cloned yet!{RESET}\n")
                continue
            return {"_bulk": "pull_all", "_groups": cloned,
                    "_index": index}

        # D8 (W3-8 #8). `"-1".isdigit()` is False, so a signed number used to
        # skip the range check and be reported as a missing group *name*,
        # sending the user to look for a group called "-1". Anything that is
        # a whole number is now answered as a number. (A negative index never
        # reached groups[idx] even before this -- there is no wrap-around bug
        # behind it, only a misleading message.)
        numeric = _as_index(choice)
        if numeric is not None:
            idx = numeric - 1
            if 0 <= idx < len(groups):
                selected = groups[idx]
                selected["_status"] = statuses[selected["id"]]
                selected["_cov"]    = covmap[selected["id"]]
                selected["_index"]  = index
                selected["_orphans"] = _orphans_under(orphans,
                                                      selected["full_path"])
                return selected
            print(f"  {RED}Pick a number from 1 to {len(groups)}.{RESET}")
            continue

        matches = [g for g in groups if low in g["full_path"].lower()]
        if len(matches) == 1:
            matches[0]["_status"] = statuses[matches[0]["id"]]
            matches[0]["_cov"]    = covmap[matches[0]["id"]]
            matches[0]["_index"]  = index
            matches[0]["_orphans"] = _orphans_under(orphans,
                                                    matches[0]["full_path"])
            return matches[0]
        elif len(matches) > 1:
            print(f"  {YELLOW}Several matches found:{RESET}")
            for m in matches:
                print(f"    {statuses[m['id']]}  {m['full_path']}")
        else:
            print(f"  {RED}'{choice}' not found.{RESET}")


def pick_action(status: str, has_errors: bool = False, info: dict = None,
                cov_info: dict = None) -> str:
    info = info or {}
    can_clone = caps.can(info, "clone") if info else True
    cov_info  = cov_info or {}
    missing   = max(0, cov_info.get("total", 0) - cov_info.get("cloned", 0))

    print(f"\n{BOLD}{WHITE}  What would you like to do?{RESET}\n")

    if status == "❌":
        # Nothing on disk — clone is the only sensible action.
        if can_clone:
            print(f"  {CYAN}1.{RESET}  📥  Clone   — clone every subgroup and project")
            valid = ["1"]
        else:
            print(f"  {DIM}1.  Clone   — token has no clone scope{RESET}")
            valid = []
    else:
        print(f"  {CYAN}1.{RESET}  🔄  Sync    — update every project")
        print(f"  {CYAN}2.{RESET}  📋  Log     — show the latest commits")
        print(f"  {CYAN}3.{RESET}  🔗  Remotes — remotes of each repo")
        valid = ["1", "2", "3"]
        if has_errors:
            print(f"  {CYAN}4.{RESET}  🔁  Retry   — {RED}retry the failed projects{RESET}")
            valid.append("4")
        # Clone stays available for a group that is only partly on disk —
        # sync can never fetch a project that was never cloned.
        if can_clone:
            note = (f"  {YELLOW}({missing} missing){RESET}" if missing
                    else f"  {DIM}(nothing missing){RESET}")
            print(f"  {CYAN}5.{RESET}  📥  Clone   — fetch the missing projects{note}")
            valid.append("5")

    print(f"\n  {YELLOW}b{RESET}  back   {YELLOW}q{RESET}  exit\n")

    while True:
        try:
            action = input(f"{BOLD}  Choice: {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{DIM}Goodbye!{RESET}")
            sys.exit(0)

        # W9-2 / W8-7. pick_action was the ONLY case-sensitive prompt in the
        # application: it compared the raw string, so `Q` and `B` were
        # rejected here and accepted on every other screen. Normalise with
        # the shared helper, exactly as pick_main_menu and _ask_next do.
        key = printer.menu_key(action)

        if key == "q":
            sys.exit(0)
        if key == "b":
            return "back"
        if key in valid:
            return key
        print(f"  {RED}Invalid choice.{RESET}")


def pick_main_menu(cfg: dict, info: dict) -> str:
    """Main menu — only what the token is allowed to do."""
    # Scoped to the active instance ($GITLAB_CLI_INSTANCE, set by
    # _activate_instance): the badge used to count every instance's records,
    # stale test entries included (D4).
    unresolved = errors.unresolved_count()
    err_mark   = f"  {RED}⚠ {unresolved} unresolved{RESET}" if unresolved else ""

    role = caps.role_label(info) if info.get("ok") else f"{RED}offline{RESET}"
    user = info.get("user") or info.get("username") or "?"

    print(f"\n{DIM}  {user} — {role}{RESET}")
    print(f"\n{BOLD}{WHITE}  Which section do you want?{RESET}\n")
    print(f"  {CYAN}1.{RESET}  📁  Master Groups  — clone, sync, log projects")

    valid = ["1", "3", "4", "5", "b", "q"]

    if caps.can(info, "members"):
        print(f"  {CYAN}2.{RESET}  👥  Members        — members, activity, access")
        valid.append("2")
    else:
        print(f"  {DIM}2.  👥  Members        — token has no access{RESET}")

    print(f"  {CYAN}3.{RESET}  🔗  Remotes        — audit the remotes of every repo")
    print(f"  {CYAN}4.{RESET}  🩺  Doctor         — health check of config and token")
    print(f"  {CYAN}5.{RESET}  ⚠️   Error Log      — errors and how to fix them{err_mark}")
    print(f"  {YELLOW}b.{RESET}  🔄  Instances      — switch / add a GitLab")
    print(f"\n  {YELLOW}q{RESET}  exit\n")

    while True:
        try:
            choice = input(f"{BOLD}  Choice: {RESET}").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{DIM}Goodbye!{RESET}")
            sys.exit(0)
        if choice == "q":
            sys.exit(0)
        if choice in valid:
            return choice
        if choice == "2":
            print_warn("  Your token cannot list users.")
            continue
        print(f"  {RED}Invalid choice.{RESET}")


INSTANCE_ENV = "GITLAB_CLI_INSTANCE"


def _activate_instance(cfg: dict) -> None:
    """Make `cfg` the instance every error record is filed under.

    errors.log_error() stamps each record with $GITLAB_CLI_INSTANCE and
    errors.unresolved_count() filters on it, so the Error Log and its badge
    belong to the instance on screen instead of to every instance ever used.
    Child processes inherit it, which is the point of using the environment.
    """
    name = (cfg or {}).get("name") or (cfg or {}).get("url") or ""
    if name:
        os.environ[INSTANCE_ENV] = name


def _show_instance_banner(cfg: dict, info: dict):
    inst_name = cfg.get("name", cfg.get("url", ""))
    dest      = cfg["master_dir"]
    print_info(f"Instance : {GREEN}{inst_name}{RESET}  {DIM}({cfg['url']}){RESET}")
    print_info(f"Folder   : {os.path.abspath(dest)}")
    if info.get("ok"):
        print_info(f"Access   : {caps.role_label(info)}"
                   f"  {DIM}({info.get('top_groups', 0)} top-level groups){RESET}")
    elif info.get("error"):
        print_warn(f"Token    : {info['error']}")


def _remotes_scope_and_repos(cfg: dict, dest: str):
    """(scope, in_scope_repos, foreign) for a remotes screen over `dest`.

    W9-2 / W8-2-H1. The walk is instance-wide by construction: on a shared
    master_dir `_find_git_repos(dest)` returns BOTH instances' clones. The
    number a user is shown must therefore be the number this screen actually
    covers, not the number of directories the walker happened to enter.
    """
    import remotes
    from pull import _find_git_repos

    repos = _find_git_repos(dest)
    scope = remotes.active_scope()
    in_scope, foreign = remotes.split_by_instance(repos, scope)
    return scope, in_scope, foreign


def _run_remotes(cfg: dict):
    """Audit the remotes of every repo of this instance."""
    import remotes

    dest = _resolve_dest(cfg)
    scope, repos, foreign = _remotes_scope_and_repos(cfg, dest)

    # W9-2 / W8-2-H1. This line used to print len() of the UNFILTERED walk,
    # two lines above a banner stating the correctly filtered scope — the
    # screen contradicted itself and the LARGER of the two numbers was the
    # wrong one. It is now the in-scope count, and the instance is named, so
    # the count and the banner can never disagree again.
    if not repos:
        who = remotes.scope_label(scope)
        print_warn(f"There is no repo of instance '{who}' in {dest}."
                   f" Clone something first.")
        for name, n in sorted((foreign or {}).items()):
            print(f"  {DIM}({_n(n, 'repo')} under this root "
                  f"belong{'s' if n == 1 else ''} to {name} and "
                  f"{'is' if n == 1 else 'are'} not this instance's){RESET}")
        return

    print_info(f"{_n(len(repos), 'repo')} of '{remotes.scope_label(scope)}' "
               f"found — checking...")
    rows = remotes.audit(repos, check_network=False, scope=scope)
    remotes.print_audit(rows)

    leaky = [r for r in rows if r["has_creds"]]
    if not leaky:
        return

    print_warn(f"{_n(len(leaky), 'repo')} "
               f"{'has' if len(leaky) == 1 else 'have'} the token stored in "
               f"the remote URL.")
    print(f"  {DIM}The token is stored in plain text in .git/config —"
          f" it leaks if you share the repo.{RESET}")

    # W9-2 / W8-2-H2. The instance and the count are restated AT THE POINT OF
    # DECISION. print_audit says it once, higher up the screen; a `y` typed
    # after a page of scrolling must not be a `y` to an unnamed scope.
    print(f"  {BOLD}{remotes.confirm_scrub_message(rows, scope)}{RESET}")
    try:
        ans = input(f"{BOLD}  Clean it up and move it to the credential store? (y/N): {RESET}").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return
    if ans != "y":
        return

    stats = remotes.scrub(repos, save_to_store=True, scope=scope)
    print_info(f"instance: {stats.get('instance', '?')}   "
               f"cleaned: {stats['cleaned']}   "
               f"creds saved: {stats['saved']}   failed: {stats['failed']}")


# ─── Bulk loops (Clone All / Sync All) ────────────────────────────────
#
# W7. Both bulk loops used to catch ONLY KeyboardInterrupt. Every other
# exception -- and the most likely one by far is an ApiError: one group 403s,
# one group 404s after a rename, the VPN blinks for three seconds -- escaped
# the loop, escaped interactive_mode(), and was caught by main()'s fatal
# boundary. That boundary does its job (printed, logged, non-zero) but it is a
# PROCESS-level handler: it ends the run. So "clone all 12 groups" stopped at
# group 3 and the remaining 9 were never attempted, on a tool whose entire
# purpose is bulk work.
#
# The fix is NOT "swallow the error". That would trade a loud abort for a
# silent gap, which is the bug class this project exists to remove. One
# group's failure is now:
#
#     printed where it happened  +  written to the error log
#     +  named again in the end-of-run summary  +  success banner withheld
#
# ...and the loop moves on to the next group. KeyboardInterrupt still stops
# everything immediately -- Ctrl-C means "I want out", not "skip one". Every
# other BaseException (SystemExit, MemoryError) is deliberately NOT caught:
# those are not "this group failed", they are "this process is going down".


def _bulk_failure(kind: str, gpath: str, exc: BaseException) -> dict:
    """One group blew up inside a bulk loop: report it, log it, keep going.

    Returns a record for the end-of-run summary. Never raises: a failure in
    the failure handler must not take down the remaining groups either, so a
    logging error is itself printed rather than propagated.
    """
    if isinstance(exc, ApiError):
        headline, _todo = _api_error_advice(exc)
        host, endpoint  = _endpoint_of(getattr(exc, "url", "") or "")
        detail = _redacted(getattr(exc, "message", "") or str(exc))
        status = getattr(exc, "status", None)
    else:
        headline = (f"stopped on an unexpected internal error: "
                    f"{type(exc).__name__}.")
        host, endpoint = "", ""
        detail = _redacted(str(exc) or type(exc).__name__)
        status = None
    info = error_catalog.explain(_catalog_text(exc))

    print(f"\n{RED}✖  {kind} FAILED for {gpath}{RESET}")
    print(f"   {DIM}reason   :{RESET} {headline}")
    if host or endpoint:
        print(f"   {DIM}at       :{RESET} {host}{endpoint}")
    if detail:
        print(f"   {DIM}detail   :{RESET} {error_catalog.summarize(detail)}")
    print(f"   {YELLOW}Not done — continuing; re-run {kind} afterwards, "
          f"it resumes.{RESET}")

    logged = False
    try:
        from errors import log_error
        log_error(f"{kind}: {gpath}",
                  f"{headline} [{type(exc).__name__}] "
                  f"host={host or '?'} endpoint={endpoint or '?'} "
                  f"detail={detail}")
        logged = True
    except Exception as log_exc:        # noqa: BLE001 - reported, not hidden
        print(f"   {YELLOW}⚠  could not write the error log: "
              f"{type(log_exc).__name__}: {log_exc}{RESET}")

    return {"group": gpath, "exc": type(exc).__name__, "status": status,
            "headline": headline, "detail": detail, "logged": logged,
            "code": info.code, "title": info.title, "fix": info.fix}


def _api_error_lines(api_errors: list, shown: int = 5) -> list:
    """One dim line per distinct API error message, most frequent first."""
    counts = Counter(str(e) for e in api_errors)
    lines = [f"   {DIM}✖ {msg}" + (f"  (×{n})" if n > 1 else "") + RESET
             for msg, n in counts.most_common(shown)]
    if len(counts) > shown:
        lines.append(f"   {DIM}… and {len(counts) - shown} more{RESET}")
    return lines


def _print_bulk_summary(kind: str, total: int, done: int,
                        failed: list, interrupted: bool,
                        api_errors: list = ()):
    """Close a bulk run honestly: "finished" is earned, never the default.

    `api_errors` are the ledger entries recorded DURING the run. A group can
    "succeed" on a listing cut short by a dropped VPN, so any of them
    downgrades the green tick to a warning.
    """
    print_separator()
    api_errors = list(api_errors or ())
    skipped = total - done - len(failed)
    if failed:
        print(f"\n{YELLOW}⚠ {kind} finished with failures — "
              f"{done}/{_n(total, 'group')} done, {len(failed)} failed"
              + (f", {skipped} not attempted" if skipped > 0 else "")
              + f".{RESET}")
        for line in _bulk_failure_groups(failed):
            print(line)
        print(f"\n   {DIM}All of the above are in the error log — review "
              f"with 'gitlab-cli' → 5 (Error Log). Re-running {kind} "
              f"retries only what is still missing.{RESET}\n")
    elif interrupted:
        print(f"\n{YELLOW}⚠ {kind} interrupted — {done}/{_n(total, 'group')} "
              f"done, {skipped} not attempted.{RESET}\n")
    elif api_errors:
        print(f"\n{YELLOW}⚠ {kind} finished with {len(api_errors)} API "
              f"error(s) — {done}/{_n(total, 'group')}; results may be "
              f"incomplete; reconnect the VPN and re-run.{RESET}")
        for line in _api_error_lines(api_errors):
            print(line)
        print()
        return
    else:
        print(f"\n{GREEN}✔ {kind} finished — {done}/{_n(total, 'group')}."
              f"{RESET}\n")
        return
    if api_errors:
        print(f"   {YELLOW}⚠ {len(api_errors)} API error(s) during this run — "
              f"results may be incomplete; reconnect the VPN and re-run:"
              f"{RESET}")
        for line in _api_error_lines(api_errors):
            print(line)
        print()


def _bulk_failure_groups(failed: list, names_shown: int = 5) -> list:
    """Summary lines for a bulk run's failures, one block per error code.

    Twenty groups failing on the same expired token are ONE problem with
    twenty victims; listing them as twenty rows hid that, and hid any second
    cause between them. Most frequent cause first.
    """
    by_code: dict = {}
    for rec in failed:
        by_code.setdefault(rec.get("code") or "unknown", []).append(rec)

    lines = []
    for code, recs in sorted(by_code.items(), key=lambda kv: -len(kv[1])):
        first = recs[0]
        title = (first.get("title") if code != "unknown" else "") \
            or first["headline"]
        statuses = sorted({str(r["status"]) for r in recs if r.get("status")})
        http = f" (HTTP {', '.join(statuses)})" if statuses else ""
        names = ", ".join(r["group"] for r in recs[:names_shown])
        if len(recs) > names_shown:
            names += f", +{len(recs) - names_shown} more"
        lines.append(f"   {RED}✖{RESET} {len(recs)} × {title}{http} — {names}")
        if code != "unknown" and first.get("fix"):
            lines.append(f"     {DIM}→ {first['fix']}{RESET}")
    return lines


def _run_bulk(kind: str, groups: list, run_one) -> tuple:
    """Drive a bulk loop; one group's failure never ends the whole run.

    Returns (done, failed, interrupted).
    """
    done, failed, interrupted = 0, [], False
    total = len(groups)
    # Snapshot, never reset: earlier failures belong to earlier screens.
    # ponytail: the ledger is capped at 500, so a run that overflows it
    # under-reports the tail; add a run id to api.record_failure if that bites.
    try:
        ledger_before = api.failure_count()
    except Exception:                      # noqa: BLE001
        ledger_before = None

    for i, g in enumerate(groups, 1):
        gpath = g.get("full_path") or str(g.get("id", "?"))
        print(f"\n{CYAN}[{i}/{total}]{RESET} {BOLD}{gpath}{RESET}")
        try:
            run_one(g)
            done += 1
        except KeyboardInterrupt:
            interrupted = True
            print(f"\n{YELLOW}⚠ {kind} interrupted.{RESET}")
            break
        except Exception as exc:        # noqa: BLE001 - reported + logged
            failed.append(_bulk_failure(kind, gpath, exc))

    try:
        api_errors = ([] if ledger_before is None
                      else api.failures()[ledger_before:])
    except Exception:                      # noqa: BLE001
        api_errors = []
    _print_bulk_summary(kind, total, done, failed, interrupted, api_errors)
    return done, failed, interrupted


_GUARD_FAILED = object()


def _guarded(context: str, fn, *args, **kwargs):
    """Run one menu action; an ApiError ends the ACTION, not the session.

    W9-2 / point 5. Returns `_GUARD_FAILED` when the action was abandoned,
    so the caller can skip whatever depended on its result. KeyboardInterrupt
    and SystemExit pass straight through: Ctrl-C and `q` are decisions, not
    failures, and must keep their existing contract.
    """
    try:
        return fn(*args, **kwargs)
    except (KeyboardInterrupt, SystemExit):
        raise
    except ApiError as exc:
        _report_recoverable(exc, context)
        return _GUARD_FAILED


def interactive_mode(cfg: dict):
    new_cfg = pick_instance()
    if new_cfg:
        cfg = new_cfg
    _activate_instance(cfg)

    dest = _resolve_dest(cfg)
    os.makedirs(dest, exist_ok=True)
    info = _cap_info(cfg)
    _show_instance_banner(cfg, info)

    while True:
        section = pick_main_menu(cfg, info)

        if section == "2":
            while True:
                user = _guarded("interactive :: members", cmd_members_list, cfg)
                if user is _GUARD_FAILED or not user:
                    break
                if _guarded("interactive :: member actions",
                            cmd_member_actions, cfg, user) is _GUARD_FAILED:
                    break
            continue

        if section == "3":
            _guarded("interactive :: remotes", _run_remotes, cfg)
            _ask_next()
            continue

        if section == "4":
            _guarded("interactive :: doctor", setup_mod.doctor)
            _ask_next()
            continue

        if section == "5":
            cmd_errors(cfg)
            continue

        if section == "b":
            new_cfg = pick_instance()
            if new_cfg:
                cfg  = new_cfg
                _activate_instance(cfg)
                dest = _resolve_dest(cfg)
                os.makedirs(dest, exist_ok=True)
                caps.invalidate(cfg)
                info = _cap_info(cfg)
                _show_instance_banner(cfg, info)
            continue

        # ─── Master Groups ────────────────────────────────
        group = _guarded("interactive :: group list", pick_group, cfg, dest)
        if group is _GUARD_FAILED:
            _ask_next()
            continue

        if group.get("_bulk") == "none":
            continue

        if group.get("_bulk") == "clone_all":
            groups = group["_groups"]
            index  = group.get("_index") or {}
            print(f"\n{BOLD}📥 Clone All — {_n(len(groups), 'group')}{RESET}\n")
            print_separator()

            # W9-2 / W8-5-§10.1. "Clone All" is group-based. Whether that
            # means "everything" depends on a setting the user may never
            # have seen, so the policy is stated here, before the first
            # clone, in every scope — and scope=all is confirmed out loud.
            user_ns = _user_namespace_gate(cfg, group.get("_user_ns"))
            if user_ns:
                print_warn(f"{len(user_ns)} user-namespace project(s) are in "
                           f"scope and CONFIRMED, but the clone engine is "
                           f"group-based: cmd_clone() walks a group tree and "
                           f"a personal namespace is not a group. They are "
                           f"NOT cloned by this run.")
                print(f"  {DIM}This is a named gap, not a silent one — a "
                      f"per-project clone entry point belongs to clone.py"
                      f"{RESET}")
            print_separator()

            # Upstream renames are applied INSIDE cmd_clone (_process_group ->
            # try_rename_repair), so no separate reconcile_renames() pass here.
            def _clone_one(g, _cfg=cfg, _dest=dest, _index=index):
                cmd_clone(_cfg, g["full_path"],
                          _clone_dest(_cfg, g["full_path"], _dest, _index))

            _run_bulk("Clone All", groups, _clone_one)
            _ask_next()
            print()
            continue

        if group.get("_bulk") == "pull_all":
            groups = group["_groups"]
            index  = group.get("_index") or {}
            print(f"\n{BOLD}🔄 Sync All — {_n(len(groups), 'group')}{RESET}\n")
            print_separator()

            # The index is shared across groups: once one group's clones are
            # moved, every later group must resolve against the fresh scan.
            state = {"index": index}

            def _pull_one(g, _cfg=cfg, _dest=dest, _state=state):
                _state["index"] = _sync_group(_cfg, g["full_path"], _dest,
                                              _state["index"])

            _run_bulk("Sync All", groups, _pull_one)
            _ask_next()
            print()
            continue

        # ─── Single group ──────────────────────────────────
        status     = group.get("_status", "❌")
        gpath      = group["full_path"]
        index      = group.get("_index") or {}

        # One directory, derived once, from the same index the icon used.
        gdir       = _group_dir(cfg, gpath, dest, index)
        cdest      = _clone_dest(cfg, gpath, dest, index)
        err_path   = _err_log_path(cfg, gpath, dest, index)
        err_count  = 0
        has_errors = False
        if os.path.exists(err_path):
            try:
                import json
                with open(err_path) as fh:
                    err_count = len(json.load(fh).get("errors", []))
                has_errors = err_count > 0
            except Exception:
                has_errors = False

        gcov = group.get("_cov") or {}
        if status == UNKNOWN_ICON:
            # W10. The listing FAILED for this group. Saying "not cloned"
            # here would be a made-up answer with the same shape as the real
            # one; the fraction below it would be 0/0.
            status_label = f"{RED}coverage UNKNOWN — the project listing failed{RESET}"
        elif status == "✅":
            status_label = f"{GREEN}complete{RESET}"
        elif status == "◐":
            status_label = (f"{YELLOW}partial — "
                            f"{gcov.get('cloned', 0)}/{gcov.get('total', 0)}{RESET}")
        else:
            status_label = f"{YELLOW}not cloned{RESET}"
        err_label    = (f"  {RED}⚠ {_n(err_count, 'error')}{RESET}"
                        if has_errors else "")
        print(f"\n  {status}  {BOLD}{gpath}{RESET}  {DIM}({status_label}){RESET}{err_label}\n")

        # The listing's own disclosure of what it contains, and why it is
        # untrustworthy when it is. Both came from api.PagedResult and were
        # discarded before W10.
        if gcov.get("note"):
            print(f"  {DIM}listing scope: {gcov['note']}{RESET}")
        if gcov.get("error"):
            print(f"  {RED}listing failure: {gcov['error']}{RESET}")
        # W10. Was a hard-coded "404 on the server" for every orphan, which
        # was wrong for the many that are RENAMED and answer 200 at their
        # old path. Each record now states what the server actually said.
        group_orphans = group.get("_orphans") or []
        if group_orphans:
            for line in cov.orphan_display_lines(group_orphans, detail=False):
                print(f"  {DIM}{line}{RESET}")
            print(f"  {DIM}full paths: b, then o on the group list{RESET}")
        print_separator()

        action = pick_action(status, has_errors, info, gcov)

        if action == "back":
            print()
            continue

        print()
        print_separator()

        if status == "❌" or action == "5":
            # Renames are repaired inside cmd_clone itself.
            cmd_clone(cfg, gpath, cdest)
        else:
            if action == "1":
                index = _sync_group(cfg, gpath, dest, index)
            elif action == "2":
                since, author = _ask_log_params()
                print()
                gp, dst = _action_args(cfg, gpath, gdir, dest)
                cmd_log(cfg, gp, dst, since, author)
            elif action == "3":
                import remotes
                from pull import _find_git_repos
                # Was os.path.join(dest, last component): on a shared
                # master_dir that audited (and offered to rewrite) another
                # instance's repos.
                repos = _find_git_repos(gdir) if os.path.isdir(gdir) else []
                if repos:
                    remotes.print_audit(remotes.audit(repos, check_network=False))
                else:
                    print_warn(f"There is no repo in {gdir}.")
            elif action == "4":
                cmd_retry_failed(cfg, gpath, cdest)

        _ask_next()
        print()


def _user_namespace_gate(cfg: dict, user_counts: dict = None) -> list:
    """Disclose the user-namespace policy, and gate `scope=all` behind y/N.

    W9-2 / W8-5-§10.1. `setup.fetch_user_namespace_projects()` was built,
    tested and left WITHOUT A CALLER. This is that caller, and it is on the
    clone path because that is where the decision has consequences.

    Two rules, both load bearing:

      * The disclosure prints in EVERY scope, including off. A silent
        exclusion is the bug class this whole project exists to eliminate:
        the user must be told that group-only means group-only, and how to
        change it, even when nothing changes.
      * `scope=all` requires an explicit `y`. That scope can pull OTHER
        PEOPLE's private repositories onto this laptop — which is precisely
        why it stayed unwired for so long. Enter, EOF, Ctrl-C and anything
        that is not `y` all mean no, and mean it by excluding.

    `user_counts` ({"total", "present"} from the group list's coverage
    counts, or None when UNKNOWN) feeds the scope=off line, so Clone All
    states the same numbers the group list just printed, in one line.

    Returns the list of user-namespace projects the caller may clone —
    ALWAYS empty unless the scope is enabled and, for `all`, confirmed.
    Nothing here clones and nothing here writes.
    """
    try:
        projects, scope, lines = setup_mod.fetch_user_namespace_projects(cfg)
    except ApiError as exc:
        # An API failure here is a discovery failure, not "there are none".
        headline, _todo = _api_error_advice(exc)
        print_warn(f"user namespaces: discovery failed — {headline}")
        print(f"  {DIM}that is UNKNOWN, not empty; no user-namespace project "
              f"is included in this run{RESET}")
        _log_recoverable(exc, "clone all :: user-namespace discovery")
        return []
    except Exception as exc:                # noqa: BLE001 - never a silent []
        print_warn(f"user namespaces: discovery failed "
                   f"({type(exc).__name__}) — treated as UNKNOWN, not empty.")
        _log_recoverable(exc, "clone all :: user-namespace discovery")
        return []

    if scope == "off":
        print(f"\n  {DIM}{_user_ns_off_line(user_counts)}{RESET}")
        _scope, note = setup_mod.user_ns_setting(cfg)
        if note:                            # an unreadable setting, said aloud
            print(f"  {YELLOW}{note}{RESET}")
        return []

    print(f"\n{BOLD}{WHITE}  User namespaces{RESET}  {DIM}(scope={scope}){RESET}")
    for line in lines:
        print(f"  {DIM}{line}{RESET}")

    if not projects:
        return []

    if scope == "all":
        owners = setup_mod.user_ns_owners(projects)
        print_warn(f"scope=all will clone {len(projects)} project(s) from "
                   f"{len(owners)} personal namespace(s) — these are OTHER "
                   f"PEOPLE's private repositories, onto this machine.")
        try:
            ans = input(f"{BOLD}  Include them in this clone run? (y/N): "
                        f"{RESET}").strip().lower()
        except (KeyboardInterrupt, EOFError):
            ans = ""
        if ans != "y":
            print(f"  {DIM}not confirmed — {len(projects)} user-namespace "
                  f"project(s) EXCLUDED from this run{RESET}")
            return []
        print(f"  {GREEN}confirmed{RESET} — {len(projects)} user-namespace "
              f"project(s) included")
        for p in projects[:20]:
            print(f"    {DIM}{p.get('path_with_namespace', '?')}{RESET}")
        if len(projects) > 20:
            print(f"    {DIM}… and {len(projects) - 20} more{RESET}")

    return list(projects)


def _user_ns_off_line(counts) -> str:
    """The ONE line Clone All prints for scope=off, with the real count."""
    if not counts:
        what = "count unknown"
    else:
        total = int(counts.get("total", 0))
        missing = max(0, total - int(counts.get("present", 0)))
        if total == 0:
            what = "none on this instance"
        elif missing == 0:
            what = f"{_n(total, 'project')}, already on disk"
        else:
            what = f"{_n(total, 'project')}, {missing} not on disk"
    return (f"User namespaces: off ({what}) — EXCLUDED by policy "
            f"(scope=off); set {setup_mod.USER_NS_ENV}=own or all to include "
            f"them")


def _ask_log_params() -> tuple:
    print(f"""
{DIM}  ┌─ Help ─────────────────────────────────┐
  │  1d   = last 1 day                     │
  │  7d   = last 7 days                    │
  │  2w   = last 2 weeks                   │
  │  1m   = last 1 month                   │
  │  1y   = last 1 year                    │
  │  2026-05-01 = from a specific date     │
  │  Enter = last 1 day (default)          │
  └────────────────────────────────────────┘{RESET}""")
    try:
        since   = input("  Since  : ").strip() or "1d"
        _author = input("  Author (Enter = everyone): ").strip()
        author  = None if _author.lower() in ("", "all") else _author
    except (KeyboardInterrupt, EOFError):
        sys.exit(0)
    return since, author


def _ask_next():
    # D10 (W3-8 #10). This prompt appears after every long operation and
    # advertises exactly two answers, but only "q" used to be tested --
    # everything else, including a fat-fingered key, was silently accepted as
    # "back to menu". Ask again until one of the advertised answers is given.
    print(f"\n{DIM}  {'─'*45}{RESET}")
    print(f"  {YELLOW}b{RESET}  back to menu   {YELLOW}q{RESET}  exit\n")
    while True:
        try:
            nxt = input(f"{BOLD}  Choice: {RESET}").strip().lower()
        except (KeyboardInterrupt, EOFError):
            sys.exit(0)
        if nxt == "q":
            print(f"{DIM}Goodbye!{RESET}")
            sys.exit(0)
        if nxt == "b":
            return
        print(f"  {RED}Type b to go back, or q to exit.{RESET}")


def print_usage():
    print(f"""
{BOLD}Usage:{RESET}
  gitlab-cli                          interactive mode
  gitlab-cli setup                    wizard for adding a GitLab
  gitlab-cli doctor [--quick]         health check: config, token, git,
                                      coverage, credentials, layout
  gitlab-cli clone   <group> [dir]    clone a group tree (resumable)
  gitlab-cli sync    <group> [dir]    smart sync of every project
  gitlab-cli log     <group> [since] [author] [dir]  latest commits
  gitlab-cli remotes [dir] [--fix]    audit remotes (+ strip tokens)
  gitlab-cli harden-perms [--dry-run] [root ...]
                                      tighten .git/config + reflog to 0600
                                      across every clone; --dry-run reports
                                      what it would change and changes nothing
  gitlab-cli instances                list the configured GitLabs
  gitlab-cli mcp                      run the MCP server over stdio (for AI
                                      agents; reads free, writes need a
                                      human-approved confirmation token)
  gitlab-cli report <command> [...]   read-only reports + confirmed writes
                                      (same engine as the MCP server);
                                      `gitlab-cli report --help` lists them

{DIM}  --debug (or GITLAB_CLI_DEBUG=1) prints the full Python traceback after
  the plain-language error. Everyday users never need it; it is what you
  attach to a bug report.{RESET}

{DIM}  [dir] is optional. Left out, it is the managed folder from your
  instance config. A relative [dir] is resolved against your CURRENT
  directory, like any other Unix tool — and you get a warning when that
  lands outside the managed folder, because the menu will not list it.{RESET}

{BOLD}Examples:{RESET}
  gitlab-cli clone my-group
  gitlab-cli sync  my-group
  gitlab-cli log   my-group 3d
  gitlab-cli remotes --fix

{DIM}  Retrying the failed projects of a group, and browsing the error log,
  live in the interactive menu.{RESET}
""")


def _cmd_remotes_cli(cfg: dict, argv: list):
    import remotes

    # `remotes` accepts an optional folder too — it goes through the same door
    # as every other destination, so `gitlab-cli remotes ./here` audits the
    # folder the user is standing in rather than silently auditing something
    # else, and says so when that is outside the managed tree.
    positional = [a for a in argv if not a.startswith("-")]
    dest = _resolve_dest(cfg, positional[0] if positional else None,
                         "remotes folder")
    scope, repos, foreign = _remotes_scope_and_repos(cfg, dest)
    who = remotes.scope_label(scope)
    if not repos:
        print_warn(f"There is no repo of instance '{who}' in {dest}.")
        for name, n in sorted((foreign or {}).items()):
            print(f"  {DIM}({_n(n, 'repo')} under this root "
                  f"belong{'s' if n == 1 else ''} to {name}){RESET}")
        return
    rows = remotes.audit(repos, check_network=False, scope=scope)
    remotes.print_audit(rows)
    if "--fix" in argv:
        # W9-2 / W8-2-H3. `--fix` is non-interactive and has NO prompt at all:
        # it rewrites remote URLs on disk with nothing on screen saying what
        # it is about to touch. A flag may legitimately skip the PROMPT; it
        # may not skip the DISCLOSURE. The instance and the count are stated
        # before the first rewrite, so the output of a scripted run still
        # records which instance's repositories were modified.
        print_warn("--fix rewrites remote URLs on disk. No confirmation is "
                   "asked for, so here is exactly what it covers:")
        print(f"  {BOLD}{remotes.confirm_scrub_message(rows, scope)}{RESET}")
        stats = remotes.scrub(repos, save_to_store=True, scope=scope)
        print_info(f"instance: {stats.get('instance', who)}   "
                   f"cleaned: {stats['cleaned']}   saved: {stats['saved']}   "
                   f"failed: {stats['failed']}   "
                   f"skipped (other instance): "
                   f"{stats.get('skipped_other_instance', 0)}")


def _cmd_harden_perms(cfg: dict, argv: list):
    """`gitlab-cli harden-perms [--dry-run] [root ...]`

    W9-2 / W8-4-§4.2 + §11.1. `git.sweep_permissions()` was built and tested
    and had no command. It tightens `.git/config`, the reflog, FETCH_HEAD and
    `.git` itself to 0600/0700 across every repository under each root —
    exhaustively, including repositories nested inside another repository's
    work tree, which a pruning walk misses.

    --dry-run is GENUINELY READ-ONLY: git.sweep_permissions(dry_run=True)
    counts what differs from the target mode and performs no chmod at all,
    so neither the mode nor the mtime of any file changes. That property is
    what lets this be run for reporting, and it is proved by execution, not
    asserted.

    Extra roots may be named on the command line, because a tree the tool did
    not create — the stray older MasterGroups — is exactly the tree a
    config-scoped cleanup silently misses.
    """
    import git as git_mod

    dry = "--dry-run" in argv or "-n" in argv
    extra = [a for a in argv if not a.startswith("-")]
    roots = [_resolve_dest(cfg, r, "harden-perms root") for r in extra] \
        or [_resolve_dest(cfg)]

    print_info(("DRY RUN — reporting only, nothing will be changed"
                if dry else "Tightening .git permissions"))
    for r in roots:
        print(f"  {DIM}root: {r}{RESET}")

    res = git_mod.sweep_permissions(roots, dry_run=dry)

    verb = "would change" if dry else "changed"
    print_info(f"repos swept: {res['repos']}   "
               f"repos {verb}: {res['repos_changed']}   "
               f"files {verb}: {res['files_changed']}")
    if res.get("config_modes_before"):
        print(f"  {DIM}.git/config modes before: "
              f"{dict(sorted(res['config_modes_before'].items()))}{RESET}")
    if not dry and res.get("config_modes_after"):
        print(f"  {DIM}.git/config modes after : "
              f"{dict(sorted(res['config_modes_after'].items()))}{RESET}")
    for rr in res.get("roots", []):
        flag = "  (MISSING)" if rr.get("missing") else ""
        print(f"  {DIM}{rr['root']}{flag}  repos={rr['repos']} "
              f"repos_changed={rr['repos_changed']} "
              f"files_changed={rr['files_changed']}{RESET}")
    errs = res.get("errors") or []
    if errs:
        print_warn(f"{len(errs)} path(s) could not be read or changed")
        for e in errs[:10]:
            print(f"    {DIM}{e}{RESET}")
    if dry:
        print(f"  {DIM}nothing was modified — re-run without --dry-run to "
              f"apply{RESET}")
    elif res["files_changed"] == 0:
        print(f"  {GREEN}✔ already tight — nothing needed changing{RESET}")


# ─── The top-level error boundary ───────────────────────────────────
#
# W6-2. An earlier wave turned silent pagination truncation into a raised
# api.ApiError -- the right call: a bad token used to read as "this group has
# 0 projects", which is the root cause this whole effort exists to fix. But an
# AST sweep of all 17 modules found ZERO `except ApiError` handlers, and main()
# caught only KeyboardInterrupt. So the cure landed the user in a raw Python
# traceback: a 500 from the server, a dropped VPN mid-clone and an expired
# token all looked identical, and all looked like a bug in the tool.
#
# This is the *other* half of that fix, and it is deliberately NOT a rescue:
#
#   * The failure is still printed, on STDERR, naming host + endpoint.
#   * The failure is still written to the shared error log (~/.gitlab-cli/
#     errors.log) so `gitlab-cli` -> 5 shows it after the terminal scrolls.
#   * The process still exits NON-ZERO, with a code that distinguishes an API
#     failure (2) from an internal defect (3), so a cron/CI caller cannot
#     mistake either for success.
#
# In other words the handler changes the FACE of the failure, never its
# visibility. Turning any of the three guarantees off would recreate exactly
# the bug class this project was started to remove.

EXIT_OK        = 0     # success, and the KeyboardInterrupt contract
EXIT_API_ERROR = 2     # the server/token/network said no
EXIT_INTERNAL  = 3     # an unexpected exception -- a defect in the tool

_DEBUG_FLAGS = ("--debug", "--traceback")


def _debug_enabled(args=None) -> bool:
    """Developer escape hatch: full traceback on demand, never by default.

    Justification for having one at all: without it, hiding the traceback
    would make the tool *harder* to debug than before this change -- the
    friendly message is a strict information loss for whoever has to fix it.
    With it, the default stays human and the detail is one flag away, so
    nobody is ever tempted to comment the handler out to see a stack.
    """
    if os.environ.get("GITLAB_CLI_DEBUG", "").strip().lower() \
            in ("1", "true", "yes", "on"):
        return True
    return any(a in _DEBUG_FLAGS for a in (args if args is not None
                                           else sys.argv[1:]))


def _strip_debug_flag(args: list) -> list:
    """--debug is ours; it must never be mistaken for a group name or a dir."""
    return [a for a in args if a not in _DEBUG_FLAGS]


def _endpoint_of(url: str) -> tuple:
    """(host, endpoint) for a message, with any credential scrubbed out."""
    if not url:
        return ("", "")
    try:
        parts = urlsplit(url)
        host  = parts.hostname or ""
        path  = parts.path or ""
    except ValueError:
        return ("", "")
    if "/api/v4/" in path:
        path = "/api/v4/" + path.split("/api/v4/", 1)[1]
    return (host, path)


def _redacted(text: str) -> str:
    """Never let a token reach the terminal or the log, even via a URL."""
    try:
        from errors import redact
        return redact(str(text))
    except Exception:
        return str(text)


def _api_error_advice(exc: ApiError) -> tuple:
    """(headline, [what to do]) -- an HTTP 401 must NOT read like a dead host."""
    status = getattr(exc, "status", 0) or 0
    # ONE fix each (D7/T07): a three-bullet essay per failure was skipped,
    # not read. The HTTP status is the most precise fact there is, so these
    # override the catalog's generic fix for an ApiError.
    if status == 0:
        return ("Could not reach the GitLab server "
                "(timeout or connection refused).",
                ["Check the VPN (these instances are only reachable through "
                 "it) and that the host resolves, then re-run — it resumes."])
    if status == 401:
        return ("GitLab rejected the token (HTTP 401 Unauthorized).",
                ["This is NOT a network problem — create a new token "
                 "(read_api, read_repository) and store it with: "
                 "gitlab-cli setup"])
    if status == 403:
        return ("GitLab accepted the token but refused this action "
                "(HTTP 403 Forbidden).",
                ["The token lacks the role this needs — ask an owner for "
                 "access; gitlab-cli doctor shows what it can do."])
    if status == 404:
        return ("GitLab has no such group/project, or the token cannot see it "
                "(HTTP 404).",
                ["Use the FULL path (group/subgroup); a private project the "
                 "token cannot read also answers 404."])
    if status == 429:
        return ("GitLab is rate limiting this token (HTTP 429).",
                ["Wait a minute and re-run (it resumes); avoid parallel jobs "
                 "against the same instance."])
    if 500 <= status < 600:
        return (f"The GitLab server failed while answering (HTTP {status}).",
                ["The server's fault, not your token — retry in a few "
                 "minutes; clone and sync resume."])
    return (f"The GitLab API call failed (HTTP {status}).",
            ["Retry (it resumes); if it persists, run gitlab-cli doctor and "
             "send the endpoint to your GitLab admin."])


def _catalog_text(exc: BaseException) -> str:
    """The text error_catalog classifies for `exc` (ApiError str has HTTP nnn)."""
    if isinstance(exc, ApiError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _describe_failure(exc: BaseException) -> dict:
    """Title, cause, fix and location for one top-level failure.

    error_catalog supplies the cause — and the fix whenever no HTTP status
    maps one — so the terminal and the Error Log explain a failure in the
    same words. An ApiError keeps its HTTP-specific headline and fix.
    """
    info = error_catalog.explain(_catalog_text(exc))
    host, endpoint = _endpoint_of(getattr(exc, "url", "") or "")
    known = info.code != "unknown"
    if isinstance(exc, ApiError):
        title, todo = _api_error_advice(exc)
        fix = todo[0]
        detail = _redacted(getattr(exc, "message", "") or str(exc))
    else:
        name = type(exc).__name__
        title = (f"{info.title} ({name})" if known else
                 f"The command stopped on an unexpected internal error: "
                 f"{name}.")
        fix = (info.fix if known else
               "This is a defect in gitlab-cli — re-run with --debug and "
               "send the traceback to the maintainer; nothing on disk was "
               "removed.")
        detail = _redacted(str(exc) or name)
    return {"title": title, "cause": info.cause if known else "",
            "fix": fix, "host": host, "endpoint": endpoint, "detail": detail}


def _print_failure_block(d: dict, context: str, out) -> None:
    """The short block: title, cause, detail, where, command, one fix."""
    print(f"\n{RED}✖  {d['title']}{RESET}", file=out)
    if d["cause"]:
        print(f"   {DIM}cause    :{RESET} {d['cause']}", file=out)
    if d["detail"]:
        print(f"   {DIM}detail   :{RESET} "
              f"{error_catalog.summarize(d['detail'])}", file=out)
    if d["host"] or d["endpoint"]:
        print(f"   {DIM}at       :{RESET} {d['host']}{d['endpoint']}",
              file=out)
    print(f"   {DIM}command  :{RESET} {context}", file=out)
    print(f"   {BOLD}What to do:{RESET} {d['fix']}", file=out)


def _log_recoverable(exc: BaseException, context: str) -> bool:
    """Write a RECOVERED failure to the shared error log. Never raises.

    W9-2 / point 5. `_report_fatal()` logs on the way out of the process.
    A failure the menu absorbs never reaches it, so without this the error
    log would under-report exactly the failures the user kept working
    through — the log must record it whether or not the run continues.
    """
    try:
        from errors import log_error
        host, endpoint = _endpoint_of(getattr(exc, "url", "") or "")
        detail = _redacted(getattr(exc, "message", "") or str(exc))
        log_error(context, f"{type(exc).__name__} host={host or '?'} "
                           f"endpoint={endpoint or '?'} detail={detail}")
        return True
    except Exception as log_exc:            # noqa: BLE001 - reported, not hidden
        print(f"  {YELLOW}⚠  could not write the error log: "
              f"{type(log_exc).__name__}{RESET}", file=sys.stderr)
        return False


def _report_recoverable(exc: BaseException, context: str):
    """An ApiError the MENU survives: reported as an error, never a crash.

    W9-2 / point 5. main() already converts an escaped ApiError into a
    friendly, logged, non-zero exit (W6-2) — but for an interactive session
    that is still a process death: a 500 while drawing one screen ended the
    whole menu and threw away the user's place in it. Inside the menu loop
    the same failure is now printed with the same advice, logged the same
    way, and the loop continues. Visibility is unchanged; only the blast
    radius shrinks.
    """
    _print_failure_block(_describe_failure(exc), context, sys.stderr)
    logged = "logged (Error Log, menu 5); " if _log_recoverable(exc, context) \
        else ""
    print(f"   {DIM}{logged}the menu is still open — nothing on disk was "
          f"changed{RESET}", file=sys.stderr)
    if _debug_enabled():
        traceback.print_exception(type(exc), exc, exc.__traceback__,
                                  file=sys.stderr)
    print("", file=sys.stderr)


def _report_fatal(exc: BaseException, context: str, debug: bool) -> int:
    """Print a short actionable message, LOG it, and return a non-zero code.

    Three guarantees, all load bearing (see the boundary note above):
    printed, logged, non-zero. Every branch below satisfies all three.
    """
    code = EXIT_API_ERROR if isinstance(exc, ApiError) else EXIT_INTERNAL
    d = _describe_failure(exc)
    out = sys.stderr
    _print_failure_block(d, context, out)

    # Logged, always. The terminal scrolls; the log is how the failure stays
    # visible tomorrow. A logging failure is itself reported, never swallowed.
    # The record keeps the full (redacted) detail; only the screen is short.
    try:
        from errors import log_error
        log_error(context, f"{d['title']} [{type(exc).__name__}] "
                           f"host={d['host'] or '?'} "
                           f"endpoint={d['endpoint'] or '?'} "
                           f"detail={d['detail']}")
        print(f"   {DIM}logged — review it with 'gitlab-cli' → 5 "
              f"(Error Log){RESET}", file=out)
    except Exception as log_exc:            # noqa: BLE001 - reported, not hidden
        print(f"   {YELLOW}⚠  could not write the error log: "
              f"{type(log_exc).__name__}: {log_exc}{RESET}", file=out)

    if debug:
        print(f"\n   {DIM}--- traceback (--debug) ---{RESET}", file=out)
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=out)
    else:
        print(f"   {DIM}--debug (or GITLAB_CLI_DEBUG=1) shows the full "
              f"traceback{RESET}", file=out)
    print("", file=out)
    return code


def main() -> int:
    """Thin boundary. All real work is in _run(); nothing escapes past here."""
    argv    = sys.argv[1:]
    debug   = _debug_enabled(argv)
    args    = _strip_debug_flag(argv)
    context = "gitlab-cli " + (" ".join(args) if args else "(interactive)")

    try:
        _run(args)
    except KeyboardInterrupt:
        # Unchanged contract: Ctrl-C is a user decision, not a failure.
        # clone.py restores the SIGINT handler in its own try/finally, which
        # runs during this unwind -- catching here cannot regress that.
        print(f"\n{DIM}Goodbye!{RESET}")
        return EXIT_OK
    except ApiError as exc:
        return _report_fatal(exc, context, debug)
    except Exception as exc:                # noqa: BLE001 - this IS the boundary
        return _report_fatal(exc, context, debug)
    return EXIT_OK


def _run_sidecar(args: list):
    """`mcp` and `report` belong to the gitlab_mcp package.

    Both are dispatched BEFORE print_banner(): `mcp` speaks JSON-RPC on stdout,
    so a single banner byte corrupts the session, and `report --json` output
    must stay machine-readable. gitlab_mcp is imported lazily so the classic
    commands never pay for (or depend on) the `mcp` SDK being installed.
    Returns an exit code, or None when args are not a sidecar command.
    """
    if not args or args[0] not in ("mcp", "report"):
        return None
    if args[0] == "mcp":
        from gitlab_mcp import server as mcp_server
        mcp_server.main()
        return EXIT_OK
    from gitlab_mcp import cli as report_cli
    return report_cli.main(args[1:])


def _run(args: list):
    code = _run_sidecar(args)
    if code is not None:
        raise SystemExit(code)

    print_banner()

    # Commands that do not need an active config
    if args and args[0] in ("--help", "-h", "help"):
        print_usage()
        return
    if args and args[0] == "setup":
        setup_mod.run_setup()
        return
    if args and args[0] == "doctor":
        setup_mod.doctor()
        return
    if args and args[0] == "instances":
        from instances import print_instances
        print_instances()
        return

    # First run? wizard
    cfg = setup_mod.ensure_setup()
    if not cfg:
        sys.exit(0)
    _activate_instance(cfg)

    dest = _resolve_dest(cfg)

    try:
        if not args:
            interactive_mode(cfg)

        elif args[0] == "clone":
            if len(args) < 2:
                print_error("Provide a group name:  gitlab-cli clone <group>")
                sys.exit(1)
            _d = _resolve_dest(cfg, args[2] if len(args) > 2 else None,
                               "clone destination")
            # Renames are repaired inside cmd_clone itself.
            cmd_clone(cfg, args[1], _clone_dest(cfg, args[1], _d))

        elif args[0] in ("pull", "sync"):
            if len(args) < 2:
                print_error("Provide a group name:  gitlab-cli sync <group>")
                sys.exit(1)
            _d = _resolve_dest(cfg, args[2] if len(args) > 2 else None,
                               "sync destination")
            _sync_group(cfg, args[1], _d)

        elif args[0] == "log":
            if len(args) < 2:
                print_error("Provide a group name:  gitlab-cli log <group>")
                sys.exit(1)
            since  = args[2] if len(args) > 2 else "1d"
            author = args[3] if len(args) > 3 else None
            _d = _resolve_dest(cfg, args[4] if len(args) > 4 else None,
                               "log folder")
            _gp, _dst = _action_args(cfg, args[1],
                                     _group_dir(cfg, args[1], _d), _d)
            cmd_log(cfg, _gp, _dst, since, author)

        elif args[0] == "remotes":
            _cmd_remotes_cli(cfg, args[1:])

        elif args[0] == "harden-perms":
            _cmd_harden_perms(cfg, args[1:])

        else:
            print_error(f"Unknown command: '{args[0]}'")
            print_usage()

    except KeyboardInterrupt:
        print(f"\n{DIM}Goodbye!{RESET}")
        sys.exit(0)


if __name__ == "__main__":
    sys.exit(main())
