# coverage.py — how much of a group is actually on disk
#
# The menu used to call a group "cloned" when its *folder* existed. That is
# wrong in both directions: a folder with one repo in it looked complete, and a
# group cloned under a drifted layout looked missing. Both made the interactive
# "Clone All" skip work it should have done.
#
# The only trustworthy identity of a local clone is its origin URL, so that is
# what we compare against the project list the API returns.

import inspect
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

GIT_BIN = shutil.which("git") or "git"
_SCAN_WORKERS = 16


def normalize(url: str):
    """
    http(s)://[creds@]host/a/b.git  ->  ("host", "a/b")
    git@host:a/b.git               ->  ("host", "a/b")

    Returns None when the URL cannot be parsed. Credentials are stripped, so a
    tokenised remote and a clean one compare equal.
    """
    u = (url or "").strip()
    if not u:
        return None
    if "://" in u:
        u = u.split("://", 1)[1]
    elif u.startswith("git@"):
        u = u[4:].replace(":", "/", 1)
    head, _, rest = u.partition("/")
    if "@" in head:
        head = head.split("@", 1)[1]
    if not rest:
        return None
    host = head.split(":")[0].lower()
    path = rest.strip("/").removesuffix(".git").strip("/").lower()
    if not path:
        return None
    return host, path


def _origin(path: str) -> str:
    try:
        r = subprocess.run([GIT_BIN, "-C", path, "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=20)
    except Exception:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def find_repos(root: str, nested: bool = True) -> list[str]:
    """Every git work tree under `root`.

    W6-1. This walker is what the interactive menu's status icons are
    ultimately computed from (via scan()), and it used to prune: it did
    ``dirnames[:] = []`` on the first ``.git`` it met and it only recognised
    ``.git`` as a DIRECTORY. Two consequences, both measured on a real
    MasterGroups/ tree (example counts):

      * ``coverage.find_repos``  -> 95
      * ``pull._find_git_repos`` -> 100
      * independent stdlib walk  -> 100

    The gap is a whole second ``MasterGroups`` checked out inside the
    ``globex/meta-repo`` work tree. Because coverage could not see them,
    the menu was STRUCTURALLY BLIND to those clones: a project genuinely on
    disk inside a nested tree could never read as present, and no icon, no
    "Clone All" selection and no action gate could ever be told otherwise.
    That is the same "status computed one way, action taken another" defect
    this tool keeps producing.

    WAS THE PRUNE DELIBERATE? Partly. The original rationale (still recorded
    in setup.py::_all_work_trees) is that "a checkout nested inside another
    clone is not the canonical clone of a project and must not count as one",
    and the prune is also what keeps the walk cheap. Both concerns survive:

      * Cost is not the reason to prune. Never descending into ``.git``
        itself is what makes a full walk cheap — ``.git/objects`` is most of
        the tree. Measured over a real tree: 0.01 s pruned vs ~0.7 s
        exhaustive. Sub-second, once, for a correct answer.
      * Identity is not affected. Coverage keys clones by their normalised
        ORIGIN URL, not by their directory, so a nested copy of an
        already-indexed project collapses onto the same key. Measured over
        a real tree, de-pruning added exactly ONE new key
        (``git.example.net / globex/agent-deletion_scheduled-1``, a clone that
        exists only inside the nested tree) and removed NONE. No group can
        therefore go from complete to incomplete because of this change.

    So the default is now exhaustive, matching ``pull._find_git_repos``. The
    old semantics remain available and explicit for the one caller that
    genuinely wants "canonical clones only" — duplicate and layout reporting
    in doctor, where counting a nested checkout invents a duplicate that no
    move can resolve. That caller should pass ``nested=False``.

    Three properties are preserved deliberately, matching pull.py:

      * **Never descend into a ``.git`` directory.** It cannot contain a work
        tree and it is most of the files on disk.
      * **Never follow symlinks**, and key every visited directory by
        ``(st_dev, st_ino)`` so a bind mount or a symlink cycle is visited
        once and pruned instead of looping forever. An unguarded walk on this
        machine can loop.
      * **A ``.git`` FILE counts too.** That is how git records a submodule
        work tree or a ``git worktree`` checkout (``gitdir: ...``); such a
        directory has an origin and is a real clone.

    Returns an absolute-free, sorted list so two runs compare equal.
    """
    repos: list[str] = []
    if not os.path.isdir(root):
        return repos

    seen: set[tuple[int, int]] = set()

    def _key(path: str):
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError:
            return None
        return (st.st_dev, st.st_ino)

    root_key = _key(root)
    if root_key is not None:
        seen.add(root_key)

    for dirpath, dirnames, filenames in os.walk(root, topdown=True,
                                                followlinks=False):
        is_repo = False
        if ".git" in dirnames:
            is_repo = True
            dirnames.remove(".git")      # huge, and never holds a work tree
        elif ".git" in filenames:
            # `gitdir:` pointer — a submodule or a linked worktree.
            is_repo = True

        if is_repo:
            repos.append(dirpath)
            if not nested:
                # Explicit opt-in to the old pruning semantics.
                dirnames[:] = []
                continue

        # Cycle guard: drop symlinked directories and anything already seen.
        kept = []
        for d in dirnames:
            p = os.path.join(dirpath, d)
            if os.path.islink(p):
                continue
            k = _key(p)
            if k is None or k in seen:
                continue
            seen.add(k)
            kept.append(d)
        dirnames[:] = kept

    return sorted(repos)


def find_repos_pruned(root: str) -> list[str]:
    """Canonical clones only — stops at the first work tree on each branch.

    The pre-W6-1 behaviour of find_repos(), kept as a named entry point for
    callers whose question really is "which directories are the managed
    clones?" (duplicate detection, layout reporting) rather than "what git
    work trees exist?". Everything that decides PRESENCE must use
    find_repos().
    """
    return find_repos(root, nested=False)


def scan(root: str) -> dict:
    """{(host, path_with_namespace): local_dir} for every clone under root."""
    repos = find_repos(root)
    if not repos:
        return {}
    out = {}
    workers = max(1, min(_SCAN_WORKERS, len(repos)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for path, url in zip(repos, pool.map(_origin, repos), strict=True):
            key = normalize(url)
            if key:
                out[key] = path
    return out


def host_of(gitlab_url: str) -> str:
    return gitlab_url.split("://", 1)[-1].split("/")[0].split(":")[0].lower()


# ─── GitLab delayed deletion ──────────────────────────────────────────────────
#
# D5. With delayed deletion on, GitLab does not remove a project (or a whole
# group) at once: it RENAMES it to `<name>-deletion_scheduled-<id>` and purges
# it days later. The flat `simple=true` listing usually omits the
# `marked_for_deletion_*` fields, so the path suffix is the main signal.
#
# Such a project is not something to mirror. Counting it made one group read
# 16/28 with all 12 "missing" ones pending deletion, offered them to Clone All,
# and presented the clone at the OLD path as "renamed upstream — re-point the
# remote", i.e. advice to point a working clone at a project about to vanish.
# Pending projects are therefore EXCLUDED from every total and missing list in
# this module and reported as their own integer, `pending_deletion`.

PENDING_DELETION_RE = re.compile(r"-deletion_scheduled-\d+(?=/|$)")


def is_pending_deletion(project_or_path) -> bool:
    """True when a project dict or a path is scheduled for deletion on GitLab.

    dict — truthy `marked_for_deletion_at` / `marked_for_deletion_on`, or the
           `-deletion_scheduled-<id>` suffix on any segment of
           `path_with_namespace` (a whole group can carry it).
    str  — the suffix test alone.
    """
    if isinstance(project_or_path, dict):
        p = project_or_path
        if p.get("marked_for_deletion_at") or p.get("marked_for_deletion_on"):
            return True
        path = p.get("path_with_namespace") or ""
    else:
        path = project_or_path or ""
    return bool(PENDING_DELETION_RE.search(str(path).strip("/")))


def split_pending(projects) -> tuple[list, int]:
    """(projects that count, how many were pending deletion)."""
    live, pending = [], 0
    for p in projects or ():
        if is_pending_deletion(p or {}):
            pending += 1
        else:
            live.append(p)
    return live, pending


def remote_projects(cfg: dict, group: dict) -> list[dict]:
    """Every project of a group including all of its subgroups."""
    from api import get_group_projects_recursive
    return get_group_projects_recursive(
        cfg["url"], cfg["headers"], group["id"],
        cfg.get("min_access") or None,
    )


def group_coverage(cfg: dict, group: dict, index: dict,
                   projects: list = None) -> tuple[int, int]:
    """
    (cloned, total) for one group.

    index is a scan() of the whole destination, so a group cloned under a
    drifted layout still counts as present — the point is whether the code is
    on disk, not whether it sits in the prettiest folder.
    """
    c = group_coverage_counts(cfg, group, index, projects)
    return c["cloned"], c["total"]


def group_coverage_counts(cfg: dict, group: dict, index: dict,
                          projects: list = None) -> dict:
    """group_coverage() as a dict, with the pending-deletion exclusion named.

        {"cloned", "total", "missing", "pending_deletion"}

    Projects scheduled for deletion on the server are in neither `total` nor
    `missing`; `pending_deletion` says how many were left out so a screen can
    say so instead of silently shrinking the denominator.
    """
    projects = remote_projects(cfg, group) if projects is None else projects
    live, pending = split_pending(projects)
    host = host_of(cfg["url"])
    want = {(host, p["path_with_namespace"].lower()) for p in live}
    cloned = len(want & set(index))
    return {"cloned": cloned, "total": len(want),
            "missing": len(want) - cloned, "pending_deletion": pending}


def status_icon(cloned: int, total: int) -> str:
    """✅ complete · ◐ partial · ❌ nothing (a group with no projects is ✅)."""
    if total == 0:
        return "✅"
    if cloned >= total:
        return "✅"
    if cloned == 0:
        return "❌"
    return "◐"


def missing_projects(cfg: dict, group: dict, index: dict,
                     projects: list = None) -> list[dict]:
    """The projects of a group that have no clone anywhere under dest.

    A project pending deletion on the server is never missing: cloning it
    would fetch something GitLab is about to purge (D5).
    """
    projects = remote_projects(cfg, group) if projects is None else projects
    live, _pending = split_pending(projects)
    host = host_of(cfg["url"])
    return [p for p in live
            if (host, p["path_with_namespace"].lower()) not in index]


# ─── Coverage accounting — ONE fraction, ONE place ────────────────────────────
#
# W8-6 found the defect: one session printed two different coverage figures
# for the same instance.
#
#   Doctor (setup.py::_coverage_check)     -> "40/45 projects on disk"
#   Group list (gitlab_cli.py::pick_group) -> "40/40 projects on disk"
#
# Neither number is wrong. They answer different questions:
#
#   * Doctor's denominator is the FLAT project list, GET /projects?simple=true,
#     which includes projects that live in a USER namespace and therefore sit
#     under no group at all. In this example that is 45, of which 5 are
#     user-namespace projects the mirror deliberately does not clone.
#   * The group list's denominator is the union of every top-level GROUP's
#     recursive project list. A user-namespace project has no group above it,
#     so it can never enter that set: 40.
#
# The defect is not arithmetic, it is presentation. Two BARE fractions that
# disagree teach the user to trust neither, which is exactly the
# status-versus-status disagreement every earlier wave of this project found.
#
# The rule enforced here: a coverage fraction is NEVER printed bare. It is
# printed as a labelled noun plus, when the two denominators differ, an
# explicit clause naming the exclusion. Every screen derives that string from
# `coverage_line()` below, so the wording can only be computed once.
#
# W10 — THE MOVE IS FINISHED. The bodies live HERE, beside `group_coverage()`,
# which is the home W8-6 named and W9-4 prepared. W8-6 had to write them into
# `caps.py` (coverage.py belonged to another worker that wave); W9-4 could only
# make this module their ADDRESS, through a PEP-562 `__getattr__`, because
# deleting them from `caps.py` requires the same commit that adds them here.
# This is that commit: the forwarding layer is gone, `caps.py` defines and
# re-exports none of these symbols, and each one has exactly ONE definition on
# disk. A capability-detection module was never the place to decide how many
# projects are cloned, and a second copy of the accounting would be the bug
# this codebase already paid for once — three directory walkers reporting
# three different work-tree counts — with a different noun.
#
# Nothing in this section imports caps/api/requests, so coverage.py stays the
# cheap, network-free module anything may import.

# A user-namespace project is not mirrored. This is a POLICY, not a bug, and
# this is the wording scripts/verify_mirror.py already uses
# ("user_namespace_not_mirrored").
POLICY_USER_NAMESPACE = "user-namespace projects excluded by policy"

# The five names that make up the accounting. Kept as a declared tuple so a
# test can enumerate them instead of hard-coding a list that silently rots.
COVERAGE_VOCABULARY = (
    "POLICY_USER_NAMESPACE",
    "project_namespace_kind",
    "coverage_counts",
    "coverage_line",
    "coverage_consistent",
)


def vocabulary_home() -> str:
    """The module that physically defines the accounting: this one.

    Kept after the move (rather than deleted with the rest of the forwarding
    scaffolding) so the property stays machine-checkable: a test asserts that
    every name in COVERAGE_VOCABULARY is a top-level definition of the module
    this returns, and is defined nowhere else in the tool.
    """
    return __name__


def project_namespace_kind(project: dict) -> str:
    """'group' | 'user' | 'unknown' for one project dict from the API.

    GitLab's `simple=true` project payload carries `namespace.kind`. Some
    older instances omit it; those projects are 'unknown' and are counted in
    the ALL denominator but never silently folded into either bucket.
    """
    ns = (project or {}).get("namespace") or {}
    kind = (ns.get("kind") or "").strip().lower()
    return kind if kind in ("group", "user") else "unknown"


def coverage_counts(projects, present_keys, host: str) -> dict:
    """The single accounting of an instance's coverage.

    projects      — the FLAT project list (every namespace), as doctor fetches.
    present_keys  — iterable of (host, path_with_namespace) keys for clones
                    found on disk, i.e. the keys of scan().
    host          — the instance host, lower case.

    Returns both fractions plus the exclusion, so no caller has to build a
    denominator of its own:

        group_present / group_total   the menu's fraction
        all_present   / all_total     doctor's fraction
        excluded_user                 the difference, named

    `pending_deletion` counts projects GitLab has scheduled for deletion;
    they are excluded from every other figure here (see is_pending_deletion).

    `unknown_total` is surfaced rather than hidden: if an instance stops
    reporting namespace.kind, the two fractions stop being explainable and a
    caller printing a bare number would be back to the original defect.
    """
    host = (host or "").lower()
    have = {(str(h).lower(), str(p).lower()) for h, p in (present_keys or ())}

    c = {"group_total": 0, "group_present": 0,
         "user_total": 0, "user_present": 0,
         "unknown_total": 0, "unknown_present": 0,
         "pending_deletion": 0}

    for p in projects or ():
        pwn = ((p or {}).get("path_with_namespace") or "").strip("/").lower()
        if not pwn:
            continue
        if is_pending_deletion(p):
            # D5: neither a total nor a miss — counted, named, left out.
            c["pending_deletion"] += 1
            continue
        kind = project_namespace_kind(p)
        c[kind + "_total"] += 1
        if (host, pwn) in have:
            c[kind + "_present"] += 1

    c["all_total"]     = c["group_total"] + c["user_total"] + c["unknown_total"]
    c["all_present"]   = c["group_present"] + c["user_present"] + c["unknown_present"]
    c["excluded_user"] = c["user_total"] - c["user_present"]
    c["missing"]       = c["group_total"] - c["group_present"]
    return c


def coverage_line(counts: dict, short: bool = False) -> str:
    """The ONE sentence every screen prints. Never a bare fraction.

    Canonical form — the mirrored fraction first, the difference between the
    two denominators spelled out:

        40/40 group projects on disk
        (45 on the instance — 5 user-namespace projects excluded by policy)

    `short=True` drops the trailing "on disk" for tight lines but keeps both
    the noun and the exclusion clause, which are the parts that make the two
    numbers reconcilable without reading the source.
    """
    c = counts or {}
    gp = int(c.get("group_present", 0))
    gt = int(c.get("group_total", 0))
    at = int(c.get("all_total", gt))
    excl = int(c.get("excluded_user", 0))
    unknown = int(c.get("unknown_total", 0))
    pending = int(c.get("pending_deletion", 0) or 0)
    pending_note = (" · " + str(pending) + " pending deletion on server, "
                    "not counted") if pending > 0 else ""

    head = str(gp) + "/" + str(gt) + " group projects" + ("" if short else " on disk")

    tail = []
    if at != gt:
        tail.append(str(at) + " on the instance")
        if excl:
            tail.append(str(excl) + " " + POLICY_USER_NAMESPACE)
        elif c.get("user_total"):
            tail.append(str(c["user_total"]) + " in user namespaces, all on disk")
    if unknown:
        tail.append(str(unknown) + " of unknown namespace kind")

    if not tail:
        return head + pending_note
    return head + " (" + " — ".join(tail) + ")" + pending_note


def coverage_consistent(counts: dict) -> bool:
    """Whether the two denominators still reconcile.

    group_total + user_total + unknown_total must equal all_total. False here
    means a caller is about to print two figures that cannot be explained to
    the user, and it must report UNKNOWN instead of a number.

    `pending_deletion` sits OUTSIDE all_total (it is excluded, not a bucket),
    so it never enters the sum; it only has to be a non-negative integer when
    present. Counts built before the key existed stay consistent.
    """
    c = counts or {}
    try:
        if int(c.get("pending_deletion", 0) or 0) < 0:
            return False
        return (int(c["group_total"]) + int(c["user_total"])
                + int(c["unknown_total"]) == int(c["all_total"]))
    except (KeyError, TypeError, ValueError):
        return False


# ─── Orphaned clones: on disk, matching nothing the server reports ────────────
#
# W9-4. An ORPHAN is a work tree under the destination whose normalised origin
# URL matches no project the instance currently lists. The menu has never shown
# them, and when a referee counted 29 the menu could only reach 6 — because the
# menu asked the question once per top-level GROUP, so a clone whose whole
# top-level group had been dissolved server-side was never even visited.
# `instance_orphans()` below asks the question once, against the instance.
#
# WHY A SINGLE COUNT IS NOT ENOUGH. The 29 orphans characterised in W7 split
# 17 renamed / 12 deleted, and those need OPPOSITE handling: a renamed orphan
# is a live project whose clone is at a stale path (fix the remote, or retire
# the directory because the new path is already cloned); a deleted orphan is
# content the server no longer has, so it may be the only copy left and must be
# archived before anything touches it. "29 orphans" tells a user neither.
#
# WHY CLASSIFYING COSTS A ROUND-TRIP. GitLab follows its own rename redirect:
# GET /api/v4/projects/<url-encoded OLD path> returns 200 with the CURRENT
# path_with_namespace. Nothing on disk can distinguish renamed from deleted —
# both look identical locally. So:
#
#     detection      = LOCAL   (disk + the project list already fetched)
#     classification = SERVER  (one GET per orphan)
#
# Every orphan record carries `source` saying which of the two produced its
# `classification`, so a caller can never mistake "not asked yet" for an
# answer. An orphan that has not been probed, or whose probe did not answer
# cleanly (403, 500, timeout, a 200 at its own path), stays ORPHAN_UNKNOWN. It
# is NEVER defaulted to deleted — a token that briefly lost access would
# otherwise present a healthy clone as server-side deleted, and that is the
# sentence a user acts on with `rm -rf`.

ORPHAN_RENAMED = "renamed_upstream"
ORPHAN_DELETED = "deleted_upstream"
ORPHAN_UNKNOWN = "unknown"
# D5. The server redirects the old path to a `-deletion_scheduled-<id>` path:
# technically a rename, but nothing should be re-pointed at it.
ORPHAN_PENDING = "pending_deletion"
ORPHAN_CLASSIFICATIONS = (ORPHAN_PENDING, ORPHAN_RENAMED, ORPHAN_DELETED,
                          ORPHAN_UNKNOWN)

# Which side of the wire produced a record's classification.
SOURCE_LOCAL = "local"      # disk only — classification is necessarily UNKNOWN
SOURCE_SERVER = "server"    # a GET to the instance answered (or failed to)

ORPHAN_LABELS = {
    ORPHAN_PENDING: "scheduled for deletion on the server",
    ORPHAN_RENAMED: "renamed upstream — the project is alive at a new path",
    ORPHAN_DELETED: "deleted upstream — the server no longer has this project",
    ORPHAN_UNKNOWN: "unknown — the server did not give a usable answer",
}

ORPHAN_ADVICE = {
    ORPHAN_PENDING: ("the project is being deleted on GitLab — keep or back up "
                     "this clone if you still need it; nothing to re-point"),
    ORPHAN_RENAMED: ("the clone points at the old path; re-point the remote or "
                     "retire the directory if the new path is already cloned"),
    ORPHAN_DELETED: ("this may be the only remaining copy — archive it before "
                     "anything is moved or removed"),
    ORPHAN_UNKNOWN: ("do not act on this: an access or network failure looks "
                     "exactly like a deletion from here"),
}

# A 404 at a `-deletion_scheduled-` path: delayed deletion ran to completion.
ADVICE_PURGED = ("already purged from the server — this may be the last copy; "
                 "back it up before removing")


def _request_accepts_record(fn) -> bool:
    """Whether api.request() takes `record=` (T06 contract) on this checkout."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "record" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def local_orphans(index: dict, projects, host: str,
                  prefix: str = None) -> list[dict]:
    """Every clone under `index` for `host` that no listed project claims.

    PURELY LOCAL. No network call is made and no classification is guessed:
    every record comes back ORPHAN_UNKNOWN with source SOURCE_LOCAL. That is
    the honest local answer — on disk the two real causes are identical.

    index    — a scan() of the destination: {(host, path): local_dir}.
    projects — the project list to compare against. Pass the instance's FLAT
               list for a complete answer; a single group's recursive list
               gives a group-scoped one that cannot see a dissolved group.
    prefix   — optional namespace prefix filter, e.g. a group's full_path.
    """
    host = (host or "").lower()
    live = set()
    for p in projects or ():
        pwn = ((p or {}).get("path_with_namespace") or "").strip("/").lower()
        # D5: a project pending deletion is excluded from coverage, so a clone
        # of it must surface here or it would be counted nowhere at all.
        if pwn and not is_pending_deletion(p or {}):
            live.add(pwn)

    pre = (prefix or "").strip("/").lower()
    out = []
    for (h, path), local_dir in (index or {}).items():
        if h != host or path in live:
            continue
        if pre and not (path == pre or path.startswith(pre + "/")):
            continue
        out.append({
            "host": host,
            "path": path,
            "dir": local_dir,
            "classification": ORPHAN_UNKNOWN,
            "source": SOURCE_LOCAL,
            "probe_status": None,
            "current_path": None,
            "reason": "not classified — no server round-trip has been made",
        })
    return sorted(out, key=lambda o: o["path"])


def probe_path(cfg: dict, path: str) -> dict:
    """Ask the instance what it currently knows about a stale project path.

    SERVER ROUND-TRIP. Returns {status, verdict, current_path} with verdict in
    {'renamed', 'same_path', 'deleted', 'unknown'}.

    The HTTP status is kept rather than collapsed to found/not-found on
    purpose: 404 is a real deletion, but 401/403/429/5xx and a transport
    failure are NOT, and a helper that returns None for all of them cannot
    tell a user the difference between "gone" and "you lost access".
    """
    from api import request
    import requests as _rq

    enc = _rq.utils.quote(str(path or "").strip("/"), safe="")
    url = cfg["url"].rstrip("/") + "/api/v4/projects/" + enc
    # D6. A 404 is the EXPECTED answer for a deleted orphan, not an API
    # failure: keep it out of the failure ledger so it is not printed twice.
    kwargs = {"quiet": True}
    if _request_accepts_record(request):
        kwargs["record"] = False
    try:
        res = request(url, cfg.get("headers") or {}, **kwargs)
    except Exception:
        return {"status": None, "verdict": "unknown", "current_path": None}
    if res is None:
        return {"status": None, "verdict": "unknown", "current_path": None}
    if res.status_code == 200:
        try:
            body = res.json()
        except Exception:
            return {"status": 200, "verdict": "unknown", "current_path": None}
        cur = ((body or {}).get("path_with_namespace") or "").strip("/")
        same = cur.lower() == str(path or "").strip("/").lower()
        return {"status": 200,
                "verdict": "same_path" if same or not cur else "renamed",
                "current_path": cur or None}
    if res.status_code == 404:
        return {"status": 404, "verdict": "deleted", "current_path": None}
    return {"status": res.status_code, "verdict": "unknown", "current_path": None}


def classify_orphan(cfg: dict, orphan: dict) -> dict:
    """One orphan, classified by asking the server. Returns a NEW dict.

    source becomes SOURCE_SERVER whether or not the answer was usable: the
    round-trip happened, and an inconclusive answer is reported as
    ORPHAN_UNKNOWN with the status that produced it.
    """
    o = dict(orphan or {})
    probe = probe_path(cfg, o.get("path", ""))
    o["source"] = SOURCE_SERVER
    o["probe_status"] = probe["status"]
    o["current_path"] = probe["current_path"]

    own_pending = is_pending_deletion(o.get("path", ""))
    if probe["verdict"] == "renamed" and is_pending_deletion(
            probe["current_path"] or ""):
        o["classification"] = ORPHAN_PENDING
        o["reason"] = (f"the server redirects this path to "
                       f"{probe['current_path']}, which is scheduled for deletion")
    elif probe["verdict"] == "renamed":
        o["classification"] = ORPHAN_RENAMED
        o["reason"] = f"the server redirects this path to {probe['current_path']}"
    elif probe["verdict"] == "deleted" and own_pending:
        o["classification"] = ORPHAN_DELETED
        o["advice"] = ADVICE_PURGED
        o["reason"] = ("the server returns 404 for this deletion-scheduled "
                       "path — the delayed deletion has completed")
    elif probe["verdict"] == "deleted":
        o["classification"] = ORPHAN_DELETED
        o["reason"] = "the server returns 404 for this path"
    elif probe["verdict"] == "same_path" and own_pending:
        o["classification"] = ORPHAN_PENDING
        o["reason"] = ("the server still serves this deletion-scheduled path; "
                       "it will be purged")
    elif probe["verdict"] == "same_path":
        o["classification"] = ORPHAN_UNKNOWN
        o["reason"] = ("the server returns 200 at this very path, yet it was "
                       "absent from the project listing we compared against")
    else:
        o["classification"] = ORPHAN_UNKNOWN
        o["reason"] = ("the server did not answer usably (status "
                       f"{probe['status']}) — this is NOT evidence of deletion")
    return o


def classify_orphans(cfg: dict, orphans: list, probe: bool = True,
                     workers: int = 8) -> list[dict]:
    """Classify a list of orphans. `probe=False` leaves every record LOCAL.

    The opt-out exists so a caller that must not touch the network (an offline
    screen, a test) still gets the orphan list — as ORPHAN_UNKNOWN records
    flagged SOURCE_LOCAL, never as an invented answer.
    """
    items = list(orphans or ())
    if not items:
        return []
    if not probe:
        return [dict(o) for o in items]
    n = max(1, min(int(workers or 1), len(items)))
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(lambda o: classify_orphan(cfg, o), items))


def instance_orphans(cfg: dict, index: dict, projects,
                     probe: bool = True) -> list[dict]:
    """The instance-wide orphan list, detected locally then classified.

    This is the call the menu wants. Asking once per instance is what closes
    the 29-versus-6 gap: a group-scoped sweep cannot see a clone whose entire
    top-level group was dissolved server-side, because there is no group left
    to iterate over.
    """
    found = local_orphans(index, projects, host_of(cfg["url"]))
    return classify_orphans(cfg, found, probe=probe)


def orphan_summary(orphans: list) -> dict:
    """Counts per classification, plus how the answers were obtained.

    `unclassified` counts records that were never probed, and is reported
    separately from `unknown` (probed, no usable answer). Collapsing the two
    would hide the difference between "we did not ask" and "the server would
    not say" — the pair that a single undifferentiated count destroys.
    """
    items = list(orphans or ())
    out = {"total": len(items), "pending_deletion": 0, "renamed": 0,
           "deleted": 0, "unknown": 0, "unclassified": 0, "probed": 0, "local_only": 0,
           "needs_server": False}
    for o in items:
        cls = (o or {}).get("classification") or ORPHAN_UNKNOWN
        src = (o or {}).get("source") or SOURCE_LOCAL
        if src == SOURCE_SERVER:
            out["probed"] += 1
        else:
            out["local_only"] += 1
        if cls == ORPHAN_PENDING:
            out["pending_deletion"] += 1
        elif cls == ORPHAN_RENAMED:
            out["renamed"] += 1
        elif cls == ORPHAN_DELETED:
            out["deleted"] += 1
        elif src == SOURCE_SERVER:
            out["unknown"] += 1
        else:
            out["unclassified"] += 1
    out["needs_server"] = out["local_only"] > 0
    out["clean"] = out["total"] == 0
    return out


def orphan_headline(summary: dict) -> str:
    """One line. Never a bare orphan count.

    Zero is stated explicitly rather than suppressed: the clone trees are clean
    today, so "0 orphaned clones" is the display that makes the first non-zero
    number visible instead of a block that silently starts appearing.
    """
    s = summary or {}
    total = int(s.get("total", 0))
    if total == 0:
        return "0 orphaned clones — every work tree matches a live project"
    parts = []
    if s.get("pending_deletion"):
        parts.append(f"{s['pending_deletion']} pending deletion")
    if s.get("renamed"):
        parts.append(f"{s['renamed']} renamed upstream")
    if s.get("deleted"):
        parts.append(f"{s['deleted']} deleted upstream")
    if s.get("unknown"):
        parts.append(f"{s['unknown']} unknown")
    if s.get("unclassified"):
        parts.append(f"{s['unclassified']} not classified (server not asked)")
    noun = "orphaned clone" + ("" if total == 1 else "s")
    return f"{total} {noun} — " + ", ".join(parts)


def _class_advice(cls: str, group: list) -> str:
    """The class advice, or a record-level override every record agrees on."""
    own = {o.get("advice") for o in group}
    if len(own) == 1 and None not in own:
        return own.pop()
    return ORPHAN_ADVICE[cls]


def orphan_display_lines(orphans: list, summary: dict = None,
                         limit: int = 40, detail: bool = False) -> list[str]:
    """Plain, colour-free lines for the interface to print.

    The rendering lives here so the menu, doctor and any future screen cannot
    grow three descriptions of the same classifications. No ANSI: the caller
    owns colour, and these lines must survive into a log file unchanged.

    detail=False (D7) — the headline plus ONE line per class present:
        "  <label>  (N)  — <advice>". No paths; at most 1 + classes lines.
    detail=True — the full listing: every path, its directory, and `limit`
        truncation (0 = no limit) with an "... and N more" line.
    """
    items = list(orphans or ())
    s = summary if summary is not None else orphan_summary(items)
    lines = [orphan_headline(s)]
    if not items:
        return lines

    if not detail:
        for cls in ORPHAN_CLASSIFICATIONS:
            group = [o for o in items
                     if (o.get("classification") or ORPHAN_UNKNOWN) == cls]
            if group:
                lines.append(f"  {ORPHAN_LABELS[cls]}  ({len(group)})  — "
                             f"{_class_advice(cls, group)}")
        return lines

    shown = items if not limit else items[:limit]
    for cls in ORPHAN_CLASSIFICATIONS:
        group = [o for o in shown
                 if (o.get("classification") or ORPHAN_UNKNOWN) == cls]
        if not group:
            continue
        lines.append(f"  {ORPHAN_LABELS[cls]}  ({len(group)})")
        lines.append(f"    what to do: {_class_advice(cls, group)}")
        for o in group:
            tail = ""
            if cls in (ORPHAN_RENAMED, ORPHAN_PENDING) and o.get("current_path"):
                tail = f"  ->  {o['current_path']}"
            elif o.get("probe_status") is not None:
                tail = f"  [HTTP {o['probe_status']}]"
            elif o.get("source") == SOURCE_LOCAL:
                tail = "  [not probed]"
            lines.append(f"      {o.get('path')}{tail}")
            if o.get("dir"):
                lines.append(f"        {o['dir']}")
    if limit and len(items) > limit:
        lines.append(f"  ... and {len(items) - limit} more")
    if s.get("needs_server"):
        lines.append("  some records were never probed; classification needs "
                     "one GET per orphan against the instance")
    return lines
