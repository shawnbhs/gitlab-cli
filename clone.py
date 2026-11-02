# clone.py — Clone a GitLab group recursively (with resume + error log)

import contextlib
import os
import re
import json
import shutil
import signal
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote as _urlquote
import paths
import errors as _errors_mod
import coverage as cov
import git as gitlib
import api as gitlab_api
from api import get_group, get_subgroups, get_projects
from git import clone_repo

# error_catalog.py is new in this wave. The guard only keeps this module
# importable while it is absent; once it exists every failure line and every
# per-group summary is built from it.
try:
    import error_catalog as _ecat
except ImportError:                                     # pragma: no cover
    _ecat = None

from printer import (
    print_section, print_group, print_subgroup,
    print_project, print_info, print_error,
    print_summary, print_separator, print_warn, print_dim,
    GREEN, RED, YELLOW, DIM, RESET
)

# Global state — shared across all calls in one session
_session_stats = {"cloned": 0, "pulled": 0, "failed": 0, "invalid": 0,
                  "elsewhere": 0, "renamed": 0, "renamed_refused": 0,
                  "lfs": 0, "pending": 0}
_session_errors = []   # all errors across all groups in one bulk run
_current_dest   = "."
_current_group  = ""

# Marker written into an error entry for a directory that exists but is not a
# valid git repository. It is NOT a clone failure: nothing was attempted and
# nothing may be removed automatically — the user has to decide.
REASON_INVALID = "invalid_repo"

# Separator used to flatten a nested group path into one filename component.
# 'globex/x/core' -> 'globex__x__core'
_GROUP_SEP = "__"

# Marker for a project that is already cloned somewhere else under the same
# destination (a drifted layout, a renamed group, a nested checkout). Nothing
# is wrong with it and nothing is cloned again — it is reported, not an error.
REASON_ELSEWHERE = "already_present_elsewhere"

# A clone whose git data is complete but whose LFS file contents could not be
# downloaded (git.S_LFS). The repository IS present and valid: it is never
# deleted, never cloned again and counts as present. Its row stays in the
# per-group error log (reason=REASON_LFS) so the missing content stays visible.
S_LFS = getattr(gitlib, "S_LFS", "lfs_incomplete")
REASON_LFS = S_LFS

# GitLab "delayed deletion" renames a project — or a whole group — to
# '<name>-deletion_scheduled-<id>'. Such a project is neither missing nor
# renamed: it is going away. Mirrors coverage.PENDING_DELETION_RE.
_PENDING_RE = re.compile(r"-deletion_scheduled-\d+(?=/|$)")


def _bump(stat: str, n: int = 1):
    _session_stats[stat] = _session_stats.get(stat, 0) + n


def is_pending_deletion(project_or_path) -> bool:
    """True for a project / group / path that GitLab has scheduled for deletion.

    coverage.is_pending_deletion is authoritative; the local regex covers the
    same rule for a local filesystem path or a clone URL (a '.git' suffix and
    Windows separators are normalised away first).
    """
    fn = getattr(cov, "is_pending_deletion", None)
    if fn is not None:
        # A failing coverage hook falls through to the local check below.
        with contextlib.suppress(Exception):
            if fn(project_or_path):
                return True
    if isinstance(project_or_path, dict):
        p = project_or_path
        if p.get("marked_for_deletion_at") or p.get("marked_for_deletion_on"):
            return True
        text = p.get("path_with_namespace") or p.get("full_path") or ""
    else:
        text = project_or_path or ""
    text = str(text).replace("\\", "/").rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    return bool(_PENDING_RE.search(text))


# ─── Short, readable failure lines ────────────────────────────────────────────

def _err_info(detail: str = "", code: str = ""):
    """ErrorInfo for a failure: from the message when there is one, else code."""
    if _ecat is None:
        return None
    try:
        if detail:
            return _ecat.explain(detail)
        if code:
            return _ecat.by_code(code)
    except Exception:
        return None
    return None


def _err_code(detail: str = "", status: str = "") -> str:
    """Stable catalog code for a clone failure."""
    if status == getattr(gitlib, "S_SHALLOW", "shallow"):
        return "transfer_cap"          # the shallow fallback only follows a cap
    info = _err_info(detail)
    if info is not None and info.code != "unknown":
        return info.code
    if status == getattr(gitlib, "S_AUTH", "auth"):
        return "auth"
    if status == getattr(gitlib, "S_GONE", "gone"):
        return "not_found"
    return info.code if info is not None else "unknown"


def _lfs_code(detail: str = "") -> str:
    info = _err_info(detail)
    if info is not None and info.type == "LFS":
        return info.code
    return "lfs_missing_object"


def _summary_of(detail: str, limit: int = 120) -> str:
    if not detail:
        return ""
    if _ecat is not None:
        # A catalog failure degrades to the plain last-line summary below.
        with contextlib.suppress(Exception):
            return _ecat.summarize(detail, limit)
    lines = [ln.strip() for ln in str(detail).splitlines() if ln.strip()]
    return (lines[-1] if lines else "")[:limit]


def _err_line(project: str, detail: str = "", code: str = "") -> str:
    """'project — Title: summary' — one short line, never a wall of stderr."""
    info = _err_info(detail, code)
    title = info.title if info is not None else "Clone failed"
    summary = _summary_of(detail)
    return f"{project} — {title}: {summary}" if summary else f"{project} — {title}"


def _fix_for(code: str) -> str:
    info = _err_info("", code)
    return (info.fix if info is not None else "") or ""


def _lfs_missing(target: str):
    """True / False / None (unknown) — are LFS file contents still missing?

    `git lfs ls-files` marks a file whose content is present with '*' and a
    bare pointer with '-'. Anything that cannot be measured returns None, and
    every caller treats None as 'still missing' so the warning stays visible.
    """
    rc, out, _ = gitlib._run([gitlib.GIT_BIN, "-C", target, "lfs", "ls-files"])
    if rc != 0:
        return None
    marks = [ln.split()[1] for ln in (out or "").splitlines()
             if len(ln.split()) >= 3]
    if "-" in marks:
        return True
    if not marks:
        return None            # nothing listed proves nothing (failed checkout)
    # Every LFS file has content; a failed checkout also leaves deletions in
    # the work tree, so the tree must be clean of them too.
    rc, st, _ = gitlib._run([gitlib.GIT_BIN, "-C", target, "status",
                             "--porcelain"])
    if rc != 0:
        return None
    if any(ln[:2].strip() == "D" for ln in st.splitlines()):
        return True
    return False


# The boolean clone API, as imported. Tests replace the module-level name
# `clone_repo` with a stub; when they do, that stub is honoured. Otherwise the
# detailed API is used so an LFS-incomplete clone is told apart from a failure.
_GIT_CLONE_REPO = clone_repo


def _clone(token: str, url: str, target: str) -> dict:
    """{"status": ..., "detail": str} for one clone attempt."""
    if clone_repo is not _GIT_CLONE_REPO:
        ok = clone_repo(token, url, target)
        return {"status": "ok" if ok else gitlib.S_ERROR, "detail": ""}
    res = gitlib.clone_repo_detailed(token, url, target)
    if not isinstance(res, dict):
        return {"status": "ok" if res else gitlib.S_ERROR, "detail": ""}
    return res

# Cached presence index per destination (see presence_index). One Clone All run
# walks several groups over the same tree; rescanning hundreds of work trees per group
# would cost thousands of `git remote get-url` calls for an answer only this
# process changes. The TTL bounds staleness if a clone appears from elsewhere.
_INDEX_TTL     = 300.0
_INDEX_WORKERS = 16
_index_cache: dict = {}


# ─── Presence index — identity, not location ──────────────────────────────────
#
# The duplication defect: resume asked "is there a repo AT THIS PATH?". The
# identity of a clone is its origin URL, not its directory, so a project already
# on disk under a different layout (RC-3 drift `MasterGroups/globex/initech/x`,
# a renamed remote namespace, a nested checkout) was invisible to the check and
# Clone All cloned a second copy of it. These helpers answer the right
# question: "is this PROJECT anywhere under dest?".

def _origin_of(repo_dir: str) -> str:
    rc, out, _ = gitlib._run([gitlib.GIT_BIN, "-C", repo_dir,
                              "remote", "get-url", "origin"])
    return out.strip() if rc == 0 else ""


def build_presence_index(dest: str) -> dict:
    """
    {(host, 'group/sub/project'): [local_dir, ...]} for every clone under dest.

    Deliberately a LIST per key, unlike coverage.scan()'s single directory: the
    corpus really does contain the same project in two places, and a caller
    that has to report or reconcile them needs to see both.
    """
    index: dict = {}
    if not dest or not os.path.isdir(dest):
        return index
    repos = cov.find_repos(dest)          # same walker coverage counting uses
    if not repos:
        return index
    workers = max(1, min(_INDEX_WORKERS, len(repos)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for repo, url in zip(repos, pool.map(_origin_of, repos),
                             strict=False):
            key = cov.normalize(url)
            if key:
                index.setdefault(key, []).append(repo)
    return index


def presence_index(dest: str, refresh: bool = False) -> dict:
    """Cached build_presence_index for one destination."""
    key = os.path.abspath(dest or ".")
    hit = _index_cache.get(key)
    if not refresh and hit and (time.monotonic() - hit[0]) < _INDEX_TTL:
        return hit[1]
    index = build_presence_index(key)
    _index_cache[key] = (time.monotonic(), index)
    return index


def reset_presence_cache():
    """Drop every cached index (used by tests and by an explicit rescan)."""
    _index_cache.clear()
    # The redirect answers are keyed on the same tree's identities, so a
    # caller asking for a fresh scan must not be served last scan's answers.
    _redirect_cache.clear()


def project_key(proj: dict, host: str = ""):
    """
    Identity of a remote project, in the same shape coverage.normalize returns.

    Derived from http_url_to_repo so a clone and its project compare equal;
    falls back to host + path_with_namespace when the payload has no URL.
    """
    key = cov.normalize((proj or {}).get("http_url_to_repo") or "")
    if key:
        return key
    pwn = ((proj or {}).get("path_with_namespace") or "").strip("/").lower()
    return (host, pwn) if (host and pwn) else None


def _existing_copies(index: dict, key, exclude: str = "") -> list:
    """Directories in the index for `key` that still exist, minus `exclude`."""
    if index is None or not key:
        return []
    ex = os.path.abspath(exclude) if exclude else ""
    out = []
    for d in index.get(key, []):
        if ex and os.path.abspath(d) == ex:
            continue
        if os.path.isdir(d):
            out.append(d)
    return out


def _remember(index: dict, key, path: str):
    if index is None or not key:
        return
    dirs = index.setdefault(key, [])
    if not any(os.path.abspath(d) == os.path.abspath(path) for d in dirs):
        dirs.append(path)


# ─── Repository validity ───────────────────────────────────────────────────────

def repo_state(target: str) -> str:
    """
    Classify a clone target.

    'absent'  — nothing there (or an empty dir): safe to clone into.
    'ok'      — a real git repository (`git -C <dir> rev-parse --git-dir` succeeds).
    'invalid' — something is there that looks like a repo but is not one:
                an interrupted clone's half-built `.git/`, a `.git` FILE whose
                `gitdir:` pointer dangles, or a non-empty directory with no
                `.git` at all. Never skipped, never clobbered.

    Existence of `.git` is deliberately NOT the test: `git clone` creates
    `.git/` first and fills it during transfer, so a Ctrl-C or a dropped VPN
    leaves a `.git` that exists and is worthless.
    """
    if not os.path.exists(target):
        return "absent"
    if not os.path.isdir(target):
        return "invalid"          # a plain file sitting where the repo goes

    rc, _, _ = gitlib._run([gitlib.GIT_BIN, "-C", target, "rev-parse", "--git-dir"])
    if rc == 0:
        return "ok"

    # Not a repository. An empty directory is still a fine clone target.
    try:
        if not os.listdir(target):
            return "absent"
    except OSError:
        return "invalid"
    return "invalid"


def is_valid_repo(target: str) -> bool:
    return repo_state(target) == "ok"


# ─── Rename redirect — repair, never duplicate ─────────────────────────────────
#
# THE DEFECT THIS CLOSES (measured: hundreds of MB of duplicate checkouts).
#
# When a project is renamed or moved on the server, the clone on disk keeps its
# OLD origin URL. Identity here is the origin URL, so that one event produces
# two wrong answers at once:
#
#   * the project reads as MISSING — no clone carries its new path; and
#   * the old clone reads as an ORPHAN — its path matches nothing on the server.
#
# The clone path then writes a second, complete copy at the new path and the
# status display reports everything as fine. The repair is to rename the clone,
# not to re-fetch it.
#
# GitLab makes this cheap and, crucially, AUTHORITATIVE: it follows its own
# rename redirect, so `GET /projects/<old-url-encoded-path>` returns HTTP 200
# with the project's CURRENT `path_with_namespace`. One request distinguishes
# renamed from deleted without guessing:
#
#   200 + a different current path  -> RENAMED  (the server said so)
#   200 + the same path             -> still there; not our case
#   404                             -> DELETED upstream
#   401 / 403 / timeout / anything  -> UNDETERMINED. Never treated as deleted:
#                                      an auth failure is not evidence that a
#                                      project is gone.
#
# Everything below is deliberately asymmetric. A wrong guess in the "deleted"
# direction abandons real data; a wrong guess in the "renamed" direction moves
# a directory that should not have moved. So a move happens only when every
# safety condition is PROVEN, and anything undetermined results in no move and
# no clone — reported, left alone.

# Refs the live-server containment check fetches into. Namespaced so it can
# never collide with a real remote, and removed again in a finally block.
_CHECK_NS = "refs/remotes/__gitlab_cli_rename_check__"

# One redirect lookup per (host, old path) per session. A Clone All run walks
# the same tree six times; without this the same candidate is asked about six
# times over the network.
_REDIRECT_TTL = 300.0
_redirect_cache: dict = {}

# Why a candidate could not be repaired. These are reported, never logged as
# clone errors: nothing failed, and cmd_retry_failed must not re-read them.
REASON_RENAMED = "renamed_upstream"
REASON_RENAME_UNSAFE = "rename_repair_refused"


def reset_redirect_cache():
    """Drop every cached redirect answer (tests, and an explicit rescan)."""
    _redirect_cache.clear()


def redirect_lookup(cfg: dict, old_path: str) -> dict:
    """
    Ask the server what the project at `old_path` is called NOW.

    Returns {"state": ..., "new_path": str, "detail": str} where state is:

      "renamed"      — 200, and the current path differs from `old_path`.
      "same"         — 200, and the path is unchanged.
      "gone"         — 404: the server does not know this path at all, and it
                       does not know it as a redirect either, so the project
                       was deleted (or was never visible to this token).
      "undetermined" — anything else: auth failure, rate limit, 5xx, timeout,
                       unparseable body. NOT deleted. NOT renamed.

    The distinction between "gone" and "undetermined" is the whole point: a
    401 has already been mistaken for "unpublished" once on this project.
    """
    out = {"state": "undetermined", "new_path": "", "detail": ""}
    path = (old_path or "").strip().strip("/")
    if not path:
        out["detail"] = "empty path"
        return out

    host = cov.host_of(cfg.get("url", "") or "")
    ck = (host, path.lower())
    hit = _redirect_cache.get(ck)
    if hit and (time.monotonic() - hit[0]) < _REDIRECT_TTL:
        return dict(hit[1])

    encoded = _urlquote(path, safe="")
    url = f"{cfg['url']}/api/v4/projects/{encoded}"
    try:
        res = gitlab_api.request(url, cfg.get("headers") or {}, quiet=True)
    except Exception as e:                      # transport wrapper blew up
        out["detail"] = f"request failed: {type(e).__name__}"
        return out

    if res is None:
        out["detail"] = "no response (timeout or connection error)"
        return out                              # undetermined, never "gone"

    if res.status_code == 404:
        out["state"] = "gone"
        out["detail"] = "HTTP 404 — not on the server under this path"
    elif res.status_code == 200:
        try:
            data = res.json() or {}
        except Exception:
            out["detail"] = "HTTP 200 with an unparseable body"
            data = None
        if isinstance(data, dict):
            current = (data.get("path_with_namespace") or "").strip("/")
            if not current:
                out["detail"] = "HTTP 200 without path_with_namespace"
            elif current.lower() == path.lower():
                out["state"] = "same"
                out["new_path"] = current
                out["detail"] = "still at this path"
            elif is_pending_deletion(current):
                # GitLab's delayed deletion renames to
                # '<name>-deletion_scheduled-<id>'. That is NOT a rename: the
                # project is going away and nothing may follow it there.
                out["state"] = "pending_deletion"
                out["new_path"] = current
                out["detail"] = (f"scheduled for deletion on the server "
                                 f"({current}) — not a rename")
            else:
                out["state"] = "renamed"
                out["new_path"] = current
                out["detail"] = f"redirects to {current}"
    else:
        out["detail"] = f"HTTP {res.status_code} — undetermined"

    _redirect_cache[ck] = (time.monotonic(), dict(out))
    return out


def _nested_inside_another_repo(repo_dir: str, root: str = None) -> bool:
    """True when some ANCESTOR directory is itself a git work tree.

    A checkout nested inside another work tree (a vendored copy, a submodule)
    must never be moved out from under its parent by this code.

    `root` bounds the walk (D1). The clone root itself routinely lives INSIDE
    another repository — MasterGroups/ sits inside the gitlab-cli checkout —
    so an unbounded walk reads EVERY clone as nested and refuses every move.
    With a root, only ancestors STRICTLY inside it count; the root and
    everything above it are the container, not a parent work tree. Without a
    root the old, unbounded walk is kept for existing callers.
    """
    cur = os.path.dirname(os.path.abspath(repo_dir))
    if root is None:
        while True:
            if os.path.exists(os.path.join(cur, ".git")):
                return True
            parent = os.path.dirname(cur)
            if parent == cur:
                return False
            cur = parent
    root_r = os.path.realpath(root)
    cur = os.path.realpath(cur)
    while cur != root_r and cur.startswith(root_r + os.sep):
        if os.path.exists(os.path.join(cur, ".git")):
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return False


def _stash_count(repo_dir: str) -> int:
    rc, out, _ = gitlib._run([gitlib.GIT_BIN, "-C", repo_dir,
                              "stash", "list"])
    if rc != 0:
        return -1                               # cannot tell -> caller refuses
    return len([ln for ln in out.splitlines() if ln.strip()])


def _worktree_status(repo_dir: str):
    """(rc, lines) of `git status --porcelain` INCLUDING untracked files."""
    rc, out, _ = gitlib._run([gitlib.GIT_BIN, "-C", repo_dir,
                              "status", "--porcelain"])
    return rc, [ln for ln in out.splitlines() if ln.strip()]


def unique_commits_vs_server(repo_dir: str, url: str, token: str = "") -> dict:
    """
    How many commits this clone holds that the SERVER does not.

    Measured against the live server, not against the local remote-tracking
    refs. A local `origin/*` can simply be old: this project has already been
    told a clone was 2 993 commits ahead when every one of those commits had
    been authored upstream and the ref was merely frozen at a graft point.

    Method, and why each step is there:

      1. Refuse outright on a shallow clone. `--not --remotes` over-reports
         on one by construction, so no honest number exists here.
      2. Fetch the server's branches AND tags into a private ref namespace.
         This is purely additive — it only adds objects and refs, never
         rewrites or drops anything — and it is what makes the comparison a
         statement about the server rather than about a stale local ref.
      3. Count `rev-list HEAD --branches --tags --not --remotes`, which now
         includes those freshly fetched refs.
      4. Remove the private refs again, in a finally block.

    Returns {"ok": bool, "count": int, "detail": str}. ok=False means the
    question could NOT be answered — the caller must then refuse to move,
    never assume zero.
    """
    res = {"ok": False, "count": -1, "detail": ""}
    if repo_state(repo_dir) != "ok":
        res["detail"] = "not a valid git repository"
        return res
    if gitlib.is_shallow(repo_dir):
        res["detail"] = ("shallow clone — '--not --remotes' over-reports on a "
                         "truncated history, so containment cannot be proven")
        return res
    if not url:
        res["detail"] = "no URL to verify against"
        return res

    refspec = f"+refs/heads/*:{_CHECK_NS}/heads/*"
    tagspec = f"+refs/tags/*:{_CHECK_NS}/tags/*"
    try:
        rc, _, err = gitlib._run(
            [gitlib.GIT_BIN, "-C", repo_dir, "fetch", "--quiet", "--no-tags",
             "--", url, refspec, tagspec],
            env_extra=gitlib._auth_env(url, token) if token else None)
        if rc != 0:
            res["detail"] = ("could not reach the server to verify: "
                             + " ".join((err or "").split())[:160])
            return res

        # An UNBORN HEAD is not a fault. A clone of a genuinely empty
        # server-side project has zero objects and zero refs legitimately —
        # most repositories once suspected broken were exactly this. Such
        # a clone holds nothing the server lacks, so the honest answer is 0,
        # not a rev-list failure.
        rc_head, _, _ = gitlib._run(
            [gitlib.GIT_BIN, "-C", repo_dir, "rev-parse", "--verify",
             "--quiet", "HEAD^{commit}"])
        if rc_head != 0:
            res["ok"] = True
            res["count"] = 0
            res["detail"] = ("unborn HEAD — an empty clone holds nothing the "
                             "server lacks")
            return res

        rc, out, _ = gitlib._run(
            [gitlib.GIT_BIN, "-C", repo_dir, "rev-list", "--count",
             "HEAD", "--branches", "--tags", "--not", "--remotes"])
        if rc != 0 or not out.strip().isdigit():
            res["detail"] = "rev-list could not compute a count"
            return res
        res["ok"] = True
        res["count"] = int(out.strip())
        res["detail"] = (f"{res['count']} commit(s) present here and not on "
                         f"the server")
        return res
    finally:
        rc, out, _ = gitlib._run(
            [gitlib.GIT_BIN, "-C", repo_dir, "for-each-ref",
             "--format=%(refname)", _CHECK_NS])
        for ref in (out or "").splitlines():
            ref = ref.strip()
            if ref.startswith(_CHECK_NS):
                gitlib._run([gitlib.GIT_BIN, "-C", repo_dir, "update-ref",
                             "-d", ref])


def _local_only_commits(repo_dir: str) -> int:
    """Commits on local branches/tags/HEAD that no remote-tracking ref has.

    LOCAL measurement only — informational for a same-device rename, which
    carries every one of them along. -1 when it cannot be computed.
    """
    args = [gitlib.GIT_BIN, "-C", repo_dir, "rev-list", "--count"]
    rc_head, _, _ = gitlib._run([gitlib.GIT_BIN, "-C", repo_dir, "rev-parse",
                                 "--verify", "--quiet", "HEAD^{commit}"])
    if rc_head == 0:
        args.append("HEAD")
    args += ["--branches", "--tags", "--not", "--remotes"]
    rc, out, _ = gitlib._run(args)
    if rc != 0 or not out.strip().isdigit():
        return -1
    return int(out.strip())


def _carried_state(repo_dir: str) -> list:
    """What a same-device rename carries along, as short phrases."""
    out = []
    rc, lines = _worktree_status(repo_dir)
    if rc != 0:
        out.append("work-tree state unknown (`git status` failed)")
    else:
        dirty = [ln for ln in lines if not ln.startswith("??")]
        untracked = [ln for ln in lines if ln.startswith("??")]
        if dirty:
            out.append(f"{len(dirty)} uncommitted change(s)")
        if untracked:
            out.append(f"{len(untracked)} untracked file(s)")
    stashes = _stash_count(repo_dir)
    if stashes > 0:
        out.append(f"{stashes} stash(es)")
    local = _local_only_commits(repo_dir)
    if local > 0:
        out.append(f"{local} local-only commit(s)")
    return out


def _nearest_existing(path: str) -> str:
    cur = os.path.abspath(path)
    while not os.path.exists(cur):
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return cur


def _same_device(src_dir: str, target: str) -> bool:
    """Whether os.rename(src_dir, target) stays on one filesystem."""
    try:
        parent = _nearest_existing(os.path.dirname(os.path.abspath(target)))
        return os.stat(src_dir).st_dev == os.stat(parent).st_dev
    except OSError:
        return False


def _strip_userinfo(url: str) -> str:
    """Drop any 'user:token@' from a URL. Tokens never live in remotes."""
    u = (url or "").strip()
    if "://" not in u:
        return u
    scheme, rest = u.split("://", 1)
    head, sep, tail = rest.partition("/")
    if "@" in head:
        head = head.rsplit("@", 1)[1]
    return f"{scheme}://{head}{sep}{tail}"


def _inside_root(root: str, path: str) -> bool:
    """`path` lies STRICTLY inside `root`, lexically and after symlinks."""
    r = os.path.normpath(os.path.abspath(root))
    p = os.path.normpath(os.path.abspath(path))
    if not p.startswith(r + os.sep):
        return False
    return paths._under(r, p) and os.path.realpath(p) != os.path.realpath(r)


def _target_problem(repo_dir: str, target: str, new_url: str,
                    root: str) -> str:
    """Why `repo_dir` may not be renamed to `target` under `root` ('' = none)."""
    raw = (target or "").replace("\\", "/")
    if not raw or ".." in raw.split("/"):
        return "the new location contains '..' — refusing to move"
    if is_pending_deletion(target) or is_pending_deletion(new_url or ""):
        return ("the new location is scheduled for deletion on the server — "
                "refusing to move")
    if not _inside_root(root, target):
        return "the new location escapes the destination root — refusing to move"
    if not _inside_root(root, repo_dir):
        return "the clone is outside the destination root — refusing to move"
    if os.path.lexists(target):
        return "the new location already exists — refusing to move"
    src_r = os.path.realpath(repo_dir)
    tgt_r = os.path.realpath(target)
    if tgt_r == src_r or tgt_r.startswith(src_r + os.sep):
        return "the new location is inside the clone itself — refusing to move"
    return ""


def move_safety(repo_dir: str, new_url: str, token: str = "",
                root: str = None, target: str = None) -> dict:
    """
    Whether this clone may be MOVED. Every reason it may not is collected.

    Without `root` (the old contract): a move is refused for uncommitted
    changes, for untracked files, for any stash, for a nested checkout, for a
    shallow history, for anything that could not be measured, and for any
    commit the server does not have.

    With `root` AND `target` (D3): when the move is a same-device os.rename —
    which preserves every byte: work-tree changes, untracked files, stashes,
    local commits, branches — those conditions are no longer refusals. They
    are collected into `carried` and reported. Still refused: an invalid repo,
    a checkout nested inside another repo strictly inside root, a target that
    exists / is pending deletion / escapes root. A cross-device move falls
    back to the strict rules above, because it would have to COPY.

    Returns {"safe", "reasons", "unique", "mode": "rename"|"strict",
    "carried": [str]}.
    """
    out = {"safe": False, "reasons": [], "unique": -1, "mode": "strict",
           "carried": []}
    if repo_state(repo_dir) != "ok":
        out["reasons"].append("not a valid git repository")
        return out
    if _nested_inside_another_repo(repo_dir, root):
        out["reasons"].append("nested inside another git work tree")

    if root is not None and target is not None:
        problem = _target_problem(repo_dir, target, new_url, root)
        if problem:
            out["reasons"].append(problem)
        if out["reasons"]:
            return out
        if _same_device(repo_dir, target):
            out["mode"] = "rename"
            out["carried"] = _carried_state(repo_dir)
            out["unique"] = _local_only_commits(repo_dir)
            out["safe"] = True
            return out
        # Different filesystems: a move would be a copy + delete. Only a
        # fully clean, server-verified clone may take that path.

    rc, lines = _worktree_status(repo_dir)
    if rc != 0:
        out["reasons"].append("`git status` failed — state unknown")
    else:
        dirty = [ln for ln in lines if not ln.startswith("??")]
        untracked = [ln for ln in lines if ln.startswith("??")]
        if dirty:
            out["reasons"].append(f"{len(dirty)} uncommitted change(s)")
        if untracked:
            out["reasons"].append(f"{len(untracked)} untracked file(s)")

    stashes = _stash_count(repo_dir)
    if stashes < 0:
        out["reasons"].append("`git stash list` failed — state unknown")
    elif stashes:
        out["reasons"].append(f"{stashes} stash entr(y/ies)")

    uniq = unique_commits_vs_server(repo_dir, new_url, token)
    out["unique"] = uniq["count"]
    if not uniq["ok"]:
        out["reasons"].append(f"unverifiable against the server: {uniq['detail']}")
    elif uniq["count"] > 0:
        out["reasons"].append(
            f"{uniq['count']} commit(s) exist here and NOT on the server")

    out["safe"] = not out["reasons"]
    return out


def _prune_empty_parents(old_dir: str, root: str):
    """rmdir now-empty parents of `old_dir`, upward, never `root` or above.

    os.rmdir only — it cannot remove a non-empty directory — and the walk
    stops at the first directory that is not empty. Errors are ignored.
    """
    if not root:
        return
    root_r = os.path.realpath(root)
    cur = os.path.dirname(os.path.abspath(old_dir))
    while True:
        try:
            cur_r = os.path.realpath(cur)
        except OSError:
            return
        if cur_r == root_r or not cur_r.startswith(root_r + os.sep):
            return
        try:
            os.rmdir(cur)
        except OSError:
            return
        parent = os.path.dirname(cur)
        if parent == cur:
            return
        cur = parent


def _repair_worktrees(new_dir: str) -> dict:
    """After the main work tree moved: `git worktree repair`, then list.

    {"had": bool, "ok": bool, "linked": int, "prunable": [path], "detail": str}
    A failure here never undoes the move; it is reported loudly instead.
    """
    res = {"had": False, "ok": True, "linked": 0, "prunable": [], "detail": ""}
    wt_dir = os.path.join(new_dir, ".git", "worktrees")
    try:
        if not (os.path.isdir(wt_dir) and os.listdir(wt_dir)):
            return res
    except OSError:
        return res
    res["had"] = True
    rc, _, err = gitlib._run([gitlib.GIT_BIN, "-C", new_dir, "worktree",
                              "repair"])
    rc2, out2, err2 = gitlib._run([gitlib.GIT_BIN, "-C", new_dir, "worktree",
                                   "list", "--porcelain"])
    entries, current = [], None
    for ln in (out2 or "").splitlines():
        if ln.startswith("worktree "):
            current = ln[len("worktree "):].strip()
            entries.append(current)
        elif ln.startswith("prunable") and current is not None:
            res["prunable"].append(current)
    res["linked"] = max(0, len(entries) - 1)
    if rc != 0 or rc2 != 0:
        res["ok"] = False
        why = " ".join(((err or "") + " " + (err2 or "")).split())[:160]
        res["detail"] = (f"WORKTREE REPAIR FAILED (rc={rc}/{rc2}): {why} — the "
                         f"move was kept; run `git -C {new_dir} worktree "
                         f"repair` by hand")
    else:
        res["detail"] = f"{res['linked']} linked worktree(s) repaired"
    if res["prunable"]:
        res["detail"] += (f"; {len(res['prunable'])} marked PRUNABLE: "
                          + ", ".join(res["prunable"][:5]))
    return res


def _relocate(repo_dir: str, target: str, new_url: str,
              root: str = None, mode: str = "strict") -> dict:
    """Move the clone to `target` and point origin at `new_url`. No deletes.

    root=None keeps the old behaviour (set-url, then shutil.move).

    With a root and mode="rename" (D3): an atomic same-device os.rename of
    the directory FIRST, then `git remote set-url origin` on the NEW path; if
    set-url fails the directory is renamed back. Linked worktrees are
    repaired afterwards, and now-empty parents of the old location are
    pruned with os.rmdir. mode="strict" (cross-device) copies via
    shutil.move and is only ever reached for a fully clean, verified clone.
    """
    res = {"ok": False, "detail": "", "worktrees": None}
    new_url = _strip_userinfo(new_url)
    if root is None:
        if is_pending_deletion(target) or is_pending_deletion(new_url):
            res["detail"] = ("the new location is scheduled for deletion on "
                             "the server — refusing to move")
            return res
        if os.path.exists(target):
            res["detail"] = "the new location already exists — refusing to move"
            return res
        rc, _, err = gitlib._run([gitlib.GIT_BIN, "-C", repo_dir, "remote",
                                  "set-url", "origin", new_url])
        if rc != 0:
            res["detail"] = ("could not update origin: "
                             + " ".join(err.split())[:120])
            return res
        try:
            os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
            shutil.move(repo_dir, target)
        except OSError as e:
            res["detail"] = f"move failed: {e}"
            return res
        res["ok"] = True
        res["detail"] = f"remote repointed and moved to {target}"
        return res

    problem = _target_problem(repo_dir, target, new_url, root)
    if problem:
        res["detail"] = problem
        return res

    parent = os.path.dirname(os.path.abspath(target))

    if mode != "rename":
        # Cross-device: move_safety proved the clone fully clean and fully on
        # the server before this branch can be reached.
        old_url = _origin_of(repo_dir)
        rc, _, err = gitlib._run([gitlib.GIT_BIN, "-C", repo_dir, "remote",
                                  "set-url", "origin", new_url])
        if rc != 0:
            res["detail"] = ("could not update origin: "
                             + " ".join(err.split())[:120])
            return res
        try:
            os.makedirs(parent, exist_ok=True)
            shutil.move(repo_dir, target)
        except OSError as e:
            if old_url and os.path.isdir(repo_dir):
                gitlib._run([gitlib.GIT_BIN, "-C", repo_dir, "remote",
                             "set-url", "origin", old_url])
            res["detail"] = f"move failed: {e}"
            return res
        _prune_empty_parents(repo_dir, root)
        res["ok"] = True
        res["detail"] = f"remote repointed and moved (copied) to {target}"
        return res

    if not _same_device(repo_dir, target):
        res["detail"] = ("source and target are on different filesystems — "
                         "refusing an atomic rename")
        return res
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as e:
        res["detail"] = f"could not create the new parent directory: {e}"
        return res
    try:
        os.rename(repo_dir, target)
    except OSError as e:
        _prune_empty_parents(target, root)      # undo parents we just made
        res["detail"] = f"rename failed: {e}"
        return res

    rc, _, err = gitlib._run([gitlib.GIT_BIN, "-C", target, "remote",
                              "set-url", "origin", new_url])
    if rc != 0:
        why = " ".join((err or "").split())[:120]
        try:
            os.rename(target, repo_dir)
        except OSError as e:
            res["detail"] = (f"could not update origin ({why}) AND could not "
                             f"rename back ({e}) — the clone is now at "
                             f"{target} with its OLD origin")
            return res
        _prune_empty_parents(target, root)
        res["detail"] = (f"could not update origin: {why} — directory renamed "
                         f"back, nothing changed")
        return res

    wt = _repair_worktrees(target)
    res["worktrees"] = wt
    _prune_empty_parents(repo_dir, root)
    res["ok"] = True
    res["detail"] = f"renamed to {target} and origin repointed"
    if wt["had"]:
        res["detail"] += f"; {wt['detail']}"
    return res


def _rename_candidates(index: dict, key, host: str, claimed: set) -> list:
    """
    On-disk clones that could plausibly be `key` under its OLD path.

    Bounded on purpose — an unbounded sweep would cost one API request per
    clone in the tree. A rename either keeps the project name and changes the
    namespace, or keeps the namespace and changes the name, so:

      * same basename, different namespace   (project MOVED between groups)
      * same namespace, different basename   (project RENAMED in place)

    A project whose name AND namespace both changed in one step is not
    matched here; _orphan_candidates (tier 3) covers it when a `known` set
    is supplied. Clones already claimed by a project in this run are never
    offered as candidates.
    """
    if not key or index is None:
        return []
    want_host, want_path = key
    if host and want_host != host:
        return []
    if is_pending_deletion(want_path):
        return []                       # never a rename TARGET
    want_ns, _, want_base = want_path.rpartition("/")

    out = []
    for k, dirs in index.items():
        if k == key or k in claimed:
            continue
        k_host, k_path = k
        if k_host != want_host:
            continue
        k_ns, _, k_base = k_path.rpartition("/")
        if not ((k_base == want_base and k_ns != want_ns)
                or (k_ns == want_ns and k_base != want_base)):
            continue
        for d in dirs:
            if os.path.isdir(d):
                out.append((k, d))
    return out


def _orphan_candidates(index: dict, key, host: str, claimed: set,
                       known, skip=()) -> list:
    """
    Tier 3 (D2): clones whose identity matches NO current project.

    A project whose name AND namespace both changed (old-tool ->
    group-a/new-tool) matches neither tier above. Its old clone is,
    however, an ORPHAN: an index key on this host whose path is not the
    current path of any project in `known`. Restricted to the wanted
    project's top-level namespace so one rename costs a handful of cached
    redirect lookups, not one per clone in the tree. Each candidate is still
    only accepted on the server's own redirect proof.
    """
    if not key or index is None or not known:
        return []
    want_host, want_path = key
    if host and want_host != host:
        return []
    if is_pending_deletion(want_path):
        return []
    top = want_path.split("/", 1)[0]
    out = []
    for k, dirs in sorted(index.items(), key=lambda kv: str(kv[0])):
        if k == key or k in claimed or k in skip or k in known:
            continue
        k_host, k_path = k
        if k_host != want_host or k_path.split("/", 1)[0] != top:
            continue
        if is_pending_deletion(k_path):
            continue
        for d in dirs:
            if os.path.isdir(d):
                out.append((k, d))
    return out


def _orphans_possible(index: dict, key, claimed: set, skip) -> bool:
    """Cheap pre-check so a `known` fetch is only paid for when tier 3 could
    actually find something."""
    if not key or index is None:
        return False
    want_host, want_path = key
    top = want_path.split("/", 1)[0]
    for k, dirs in index.items():
        if k == key or k in claimed or k in skip:
            continue
        if k[0] == want_host and k[1].split("/", 1)[0] == top and dirs:
            return True
    return False


def _safe_rel_path(path: str):
    """'a/b/c' -> ['a','b','c'] when the server path is safe, else None."""
    try:
        parts = paths.sanitize(path)
    except paths.UnsafePath:
        return None
    if not parts or "/".join(parts) != (path or "").strip().strip("/"):
        return None
    return parts


def try_rename_repair(cfg: dict, proj: dict, target: str, index: dict,
                      host: str, claimed: set = None, known=None,
                      root: str = None) -> dict:
    """
    Repair a project that is 'missing' only because it was renamed upstream.

    Returns {"action": ..., "from": str, "detail": str} where action is:

      "moved"    — an existing clone was PROVEN (by the server's own redirect)
                   to be this project under its old path, proven safe to move,
                   and was repointed and relocated. Nothing was cloned.
      "refused"  — a candidate WAS proven to be this project, but moving it is
                   not safe. Nothing is moved and NOTHING IS CLONED: cloning
                   here is exactly what produced hundreds of MB of duplicates.
      "pending"  — the project is scheduled for deletion on the server.
                   Nothing is moved and nothing is cloned.
      "none"     — no candidate; the caller clones normally.

    `known` (optional) — set of project_key()s of every CURRENT project of
    the top-level group, or a zero-argument callable returning that set (or
    None); it enables tier 3 (_orphan_candidates), used only when tiers 1-2
    produced no proven match. `root` (optional) — the destination root: it
    bounds the nested-repo check (D1), validates the target, and enables the
    same-device rename policy (D3). Without it the old strict rules apply.

    Note the asymmetry once more: "none" is the only outcome that permits a
    clone, and it is reached only when the server gave a definite answer that
    no on-disk clone is this project.
    """
    res = {"action": "none", "from": "", "detail": "", "reasons": [],
           "carried": [], "worktrees": None}
    key = project_key(proj, host)
    if not key:
        return res
    new_path = (proj.get("path_with_namespace") or "").strip("/")
    new_url = proj.get("http_url_to_repo") or ""
    claimed = claimed or set()

    # A GitLab delayed-deletion rename is NOT a real rename: never relocate or
    # re-point an existing clone into a path that is pending deletion.
    if (is_pending_deletion(proj) or is_pending_deletion(new_path)
            or is_pending_deletion(target)):
        res.update(action="pending",
                   detail="scheduled for deletion on the server — not cloned")
        return res

    tried = set()

    def _attempt(cand_key, cand_dir):
        """None when this candidate proves nothing; else the final result."""
        tried.add(cand_key)
        old_path = cand_key[1]
        look = redirect_lookup(cfg, old_path)
        if look["state"] != "renamed":
            # "gone"/"same"/"pending_deletion"/"undetermined" all mean: this
            # candidate is not proof of anything. Never move on a guess.
            return None
        if is_pending_deletion(look["new_path"] or ""):
            return None
        if (look["new_path"] or "").strip("/").lower() != new_path.lower():
            return None                         # redirects, but not to us

        if root is not None and (_safe_rel_path(new_path) is None
                                 or _safe_rel_path(look["new_path"]) is None):
            why = (f"the server-provided path {new_path!r} is unsafe "
                   f"('..', absolute or malformed) — refusing to move")
            res.update(action="refused", **{"from": cand_dir})
            res["reasons"] = [why]
            res["detail"] = why
            return res

        if root is None:
            safety = move_safety(cand_dir, new_url, cfg.get("token") or "")
        else:
            safety = move_safety(cand_dir, new_url, cfg.get("token") or "",
                                 root=root, target=target)
        if not safety["safe"]:
            res.update(action="refused", **{"from": cand_dir})
            res["reasons"] = safety["reasons"]
            res["detail"] = ("renamed upstream, but this clone holds work that "
                             "would be at risk: " + "; ".join(safety["reasons"]))
            return res

        if root is None:
            moved = _relocate(cand_dir, target, new_url)
        else:
            moved = _relocate(cand_dir, target, new_url, root=root,
                              mode=safety.get("mode") or "strict")
        if not moved["ok"]:
            res.update(action="refused", **{"from": cand_dir})
            res["reasons"] = [moved["detail"]]
            res["detail"] = moved["detail"]
            return res

        # The index must forget the old identity and learn the new one, or a
        # later group in the same run walks into a directory that has moved.
        try:
            index.get(cand_key, []).remove(cand_dir)
        except (ValueError, AttributeError):
            pass
        _remember(index, key, target)
        detail = moved["detail"]
        carried = safety.get("carried") or []
        if carried:
            detail += "; carried along: " + ", ".join(carried)
        res.update(action="moved", detail=detail, carried=carried,
                   worktrees=moved.get("worktrees"))
        res["from"] = cand_dir
        return res

    for cand_key, cand_dir in _rename_candidates(index, key, host, claimed):
        done = _attempt(cand_key, cand_dir)
        if done is not None:
            return done

    # Tier 3 — only when tiers 1-2 proved nothing and a known set exists.
    if known is None or not _orphans_possible(index, key, claimed, tried):
        return res
    if callable(known):
        try:
            known = known()
        except Exception:
            known = None
    if not known:
        return res
    for cand_key, cand_dir in _orphan_candidates(index, key, host, claimed,
                                                 known, skip=tried):
        if cand_key in tried:
            continue
        done = _attempt(cand_key, cand_dir)
        if done is not None:
            return done

    return res


# ─── Error log ─────────────────────────────────────────────────────────────────

def _group_slug(group_path: str) -> str:
    """
    Flatten a full group path into one filename-safe component.

    Keyed on the FULL path, not the last component: 'initech/core' and
    'globex/x/core' used to share '.core_errors.json', so a clean run of one
    group os.remove()d the other group's pending errors.

    A top-level group ('acme') flattens to itself, so existing top-level log
    files keep working unchanged.
    """
    try:
        parts = paths.sanitize(group_path)
    except paths.UnsafePath:
        parts = []
    if not parts:
        parts = [(group_path or "group").strip().replace("/", _GROUP_SEP) or "group"]
    return _GROUP_SEP.join(parts)


def _error_log_path(dest: str, group_path: str) -> str:
    return os.path.join(dest, f".{_group_slug(group_path)}_errors.json")


def _legacy_error_log_path(dest: str, group_path: str) -> str:
    """The old, collision-prone name: last path component only."""
    last = (group_path or "").strip().rstrip("/").split("/")[-1]
    if not last:
        return ""
    return os.path.join(dest, f".{last}_errors.json")


def _read_log(path: str):
    """Return the parsed log dict, or None if unreadable/absent."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _owns_legacy(dest: str, group_path: str):
    """
    (path, data) of an old-style log file that provably belongs to this group,
    else (None, None).

    A legacy file is only attributed to us when its recorded 'group' matches
    this group path, or when the old and new names are identical anyway (a
    top-level group). A legacy file we cannot attribute is left strictly
    alone — never read as ours, never deleted.
    """
    legacy = _legacy_error_log_path(dest, group_path)
    if not legacy or legacy == _error_log_path(dest, group_path):
        return None, None
    data = _read_log(legacy)
    if data is None:
        return None, None
    recorded = (data.get("group") or "").strip()
    if recorded and recorded == (group_path or "").strip():
        return legacy, data
    return None, None


def _save_errors(dest: str, group_path: str, errors: list):
    path = _error_log_path(dest, group_path)

    if not errors:
        # Clean up our own error log if everything succeeded. Only files we can
        # attribute to THIS group are removed.
        if os.path.exists(path):
            os.remove(path)
        legacy, _ = _owns_legacy(dest, group_path)
        if legacy:
            os.remove(legacy)
        return

    # errors.write_private_json, not open()+json.dump: this artifact holds raw
    # git stderr and the project's http_url_to_repo, and it is written INTO the
    # clone tree — the directory a user tars up, rsyncs, or hands to a
    # colleague. The plain write inherited the umask and produced a 0644 file,
    # and it redacted nothing at all. The helper redacts every nested string,
    # creates the fd 0600 so the file is never even momentarily world readable,
    # and replaces atomically so a reader never sees a half-written artifact.
    _errors_mod.write_private_json(path, {
        "date":   datetime.now().isoformat(),
        "group":  group_path,
        "errors": errors,
    })

    # Migration: an attributable old-style file is superseded by the new one.
    legacy, _ = _owns_legacy(dest, group_path)
    if legacy:
        try:
            os.remove(legacy)
        except OSError:
            pass

    print(f"\n{YELLOW}⚠  {len(errors)} errors saved to:{RESET} {DIM}{path}{RESET}")


def load_errors(dest: str, group_path: str) -> list:
    data = _read_log(_error_log_path(dest, group_path))
    if data is not None:
        return data.get("errors", []) or []
    # Fall back to an old-style file, but only one we can attribute.
    legacy, ldata = _owns_legacy(dest, group_path)
    if ldata is not None:
        return ldata.get("errors", []) or []
    return []


# ─── Recursive clone ───────────────────────────────────────────────────────────

def _process_group(cfg: dict, group: dict, local_path: str,
                   depth: int = 0, errors: list = None, index: dict = None,
                   elsewhere: list = None, renamed: list = None,
                   pending: list = None, lfs_known: dict = None,
                   known=None, root: str = None, lfs_retry: list = None):
    """
    Recursively clone — skip a project that is already cloned ANYWHERE under
    the destination, not merely at the path this layout would give it.

    `index` is the presence index (see presence_index). It is mutated as we go,
    so a project cloned for one group in a Clone All run is not cloned again
    for an overlapping group later in the same run.

    `elsewhere` collects the projects that were found under another layout.
    They are NOT errors and deliberately never reach the error log: the menu
    renders a group with a non-empty log as ⚠ and cmd_retry_failed would keep
    re-reading them forever.

    `pending` collects projects GitLab has scheduled for deletion. They are
    skipped silently (one summary line at the end) — never cloned.

    `lfs_known` maps abspath -> previous REASON_LFS row. A resumed run keeps
    such a row while the LFS contents are still missing, so the warning does
    not vanish just because the repository itself is valid. Each such repo
    gets one `git lfs pull` retry; the outcome is appended to `lfs_retry` as
    {"project", "repaired", "code"} for the end-of-run summary.

    `known` / `root` are handed to try_rename_repair unchanged (tier-3
    orphan search; destination root for the D1/D3 move policy).
    """
    if errors is None:
        errors = []
    if elsewhere is None:
        elsewhere = []
    if renamed is None:
        renamed = []
    if pending is None:
        pending = []
    if lfs_known is None:
        lfs_known = {}
    if lfs_retry is None:
        lfs_retry = []
    group_pending = is_pending_deletion(group.get("full_path") or "")

    # NOT created eagerly any more: an empty `dest/<group>/` directory left by
    # a run with nothing to do becomes an existing-folder match for
    # paths.target_for_clone, which then prefers it over the group's real,
    # drifted location — and the next run clones a second copy into it.
    made_dir = False

    host = cov.host_of(cfg.get("url", "") or "")
    projects = get_projects(cfg["url"], cfg["headers"], group["id"])
    # Identities this run has already matched to a directory. A clone claimed
    # by one project can never be offered as another project's rename
    # candidate — a bad match here would MOVE the wrong directory.
    claimed = set()
    for proj in projects:
        # GitLab delayed deletion: not missing, not renamed — going away.
        if group_pending or is_pending_deletion(proj):
            pending.append(proj.get("path_with_namespace") or proj.get("path"))
            _bump("pending")
            continue

        target = os.path.join(local_path, proj["path"])
        key    = project_key(proj, host)

        state = repo_state(target)

        # Resume: skip only a genuinely valid repository.
        if state == "ok":
            print_project(proj["path"], depth + 1)
            prior = lfs_known.get(os.path.abspath(target))
            lfs_detail = ""
            if prior is not None and _lfs_missing(target) is not False:
                # Flagged LFS-incomplete last run: retry the content fetch
                # once (never re-cloned), then measure again.
                lfs_detail = gitlib._complete_lfs(
                    target, proj["http_url_to_repo"], cfg.get("token") or "")
            name = proj.get("path_with_namespace") or proj["path"]
            if prior is not None and _lfs_missing(target) is not False:
                # Present and valid, but its LFS contents are still missing:
                # keep the row so the warning stays visible.
                print(f"{YELLOW}skip ⚠{RESET} "
                      f"{DIM}present; LFS files still missing{RESET}")
                _bump("lfs")
                row = dict(prior)
                row.update(project=proj["path"], path=target,
                           url=proj["http_url_to_repo"], reason=REASON_LFS)
                if lfs_detail:
                    row.update(detail=lfs_detail, code=_lfs_code(lfs_detail))
                row.setdefault("code", "lfs_missing_object")
                errors.append(row)
                lfs_retry.append({"project": name, "repaired": False,
                                  "code": row["code"]})
            elif prior is not None:
                print(f"{GREEN}LFS repaired ✓{RESET}")
                lfs_retry.append({"project": name, "repaired": True,
                                  "code": ""})
            else:
                print(f"{DIM}skip ✓{RESET}")
            _remember(index, key, target)
            if key:
                claimed.add(key)
            continue

        # Something is there but it is not a repository. Do not skip it
        # silently and do not clone over it — report it and move on.
        if state == "invalid":
            print_project(proj["path"], depth + 1)
            print(f"{RED}broken ✖{RESET} {DIM}not a valid git repo — left untouched{RESET}")
            _session_stats["invalid"] += 1
            errors.append({
                "project": proj["path"],
                "path":    target,
                "url":     proj["http_url_to_repo"],
                "reason":  REASON_INVALID,
                "detail":  "directory exists but `git rev-parse --git-dir` fails "
                           "(interrupted clone, .git file, or foreign directory)",
            })
            continue

        # Nothing at this path — but the project may already be on disk
        # under another layout. Cloning it again would duplicate the repo
        # (the RC-3 drift duplicated projects exactly this way).
        found = _existing_copies(index, key, exclude=target)
        if found:
            print_project(proj["path"], depth + 1)
            print(f"{YELLOW}present ✓{RESET} "
                  f"{DIM}already cloned at {found[0]}{RESET}")
            _session_stats["elsewhere"] += 1
            elsewhere.append({
                "project":  proj.get("path_with_namespace") or proj["path"],
                "expected": target,
                "found_at": found,
                "reason":   REASON_ELSEWHERE,
            })
            if key:
                claimed.add(key)
            continue

        # Still nothing carrying this project's CURRENT identity. Before
        # cloning, ask whether a clone on disk IS this project under its old
        # path — a rename upstream makes the same project read as missing here
        # AND as an orphan there, and re-cloning is what produced hundreds of
        # MB of duplicate checkouts.
        repair = try_rename_repair(cfg, proj, target, index, host, claimed,
                                   known=known, root=root)

        if repair["action"] == "pending":
            pending.append(proj.get("path_with_namespace") or proj["path"])
            _bump("pending")
            continue

        if repair["action"] == "moved":
            print_project(proj["path"], depth + 1)
            print(f"{GREEN}renamed ✓{RESET} "
                  f"{DIM}repaired from {repair['from']} — not re-cloned{RESET}")
            _session_stats["renamed"] += 1
            renamed.append({
                "project":  proj.get("path_with_namespace") or proj["path"],
                "moved_from": repair["from"],
                "moved_to":   target,
                "reason":     REASON_RENAMED,
                "detail":     repair["detail"],
            })
            if key:
                claimed.add(key)
            continue

        if repair["action"] == "refused":
            # PROVEN to be this project under its old path, but not safe to
            # move. Nothing is moved and — deliberately — nothing is cloned:
            # a second copy here is the exact defect being fixed.
            print_project(proj["path"], depth + 1)
            print(f"{YELLOW}renamed ⚠{RESET} "
                  f"{DIM}{repair['from']} is this project under its old path, "
                  f"but it was left alone: {repair['detail']}{RESET}")
            _session_stats["renamed_refused"] += 1
            renamed.append({
                "project":    proj.get("path_with_namespace") or proj["path"],
                "moved_from": repair["from"],
                "moved_to":   "",
                "reason":     REASON_RENAME_UNSAFE,
                "detail":     repair["detail"],
                "reasons":    repair.get("reasons") or [],
            })
            if key:
                claimed.add(key)
            continue

        if not made_dir:
            os.makedirs(local_path, exist_ok=True)
            made_dir = True

        print_project(proj["path"], depth + 1)
        res = _clone(cfg["token"], proj["http_url_to_repo"], target)
        status = res.get("status")
        detail = res.get("detail") or ""
        if status == "ok":
            _bump("cloned")
            _remember(index, key, target)
            if key:
                claimed.add(key)
        elif status == S_LFS:
            # The repository IS here and valid; only LFS contents are missing.
            # Present for every purpose — never deleted, never re-cloned —
            # but the row stays in the error log so it is not forgotten.
            print(f"{YELLOW}LFS ⚠{RESET} "
                  f"{DIM}cloned; LFS files missing — repository kept{RESET}")
            _bump("lfs")
            _remember(index, key, target)
            if key:
                claimed.add(key)
            errors.append({
                "project": proj["path"],
                "path":    target,
                "url":     proj["http_url_to_repo"],
                "reason":  REASON_LFS,
                "code":    _lfs_code(detail),
                "detail":  detail,
            })
        else:
            _bump("failed")
            errors.append({
                "project": proj["path"],
                "path":    target,
                "url":     proj["http_url_to_repo"],
                "code":    _err_code(detail, status),
                "detail":  detail,
            })

    subgroups = get_subgroups(cfg["url"], cfg["headers"], group["id"])
    for sg in subgroups:
        # A subgroup scheduled for deletion gets no header; its projects are
        # all counted as pending inside the recursive call.
        if not (group_pending or is_pending_deletion(sg.get("full_path") or "")):
            print_subgroup(sg["full_path"], depth + 1)
        _process_group(cfg, sg, os.path.join(local_path, sg["path"]),
                       depth + 1, errors, index, elsewhere, renamed,
                       pending, lfs_known, known=known, root=root,
                       lfs_retry=lfs_retry)

    return errors


def _print_renamed_notice(renamed: list):
    """Report the rename repairs and — more importantly — the refusals."""
    if not renamed:
        return
    moved = [r for r in renamed if r.get("reason") == REASON_RENAMED]
    held  = [r for r in renamed if r.get("reason") == REASON_RENAME_UNSAFE]
    if moved:
        print(f"{GREEN}✔ {len(moved)} renamed upstream — repaired in place"
              f"{RESET} {DIM}(remote repointed and directory moved; no second "
              f"copy was cloned){RESET}")
        for r in moved[:10]:
            print(f"   {DIM}{r['project']}: {r['moved_from']} → "
                  f"{r['moved_to']}{RESET}")
        if len(moved) > 10:
            print(f"   {DIM}… and {len(moved) - 10} more{RESET}")
    if held:
        print(f"{YELLOW}⚠ {len(held)} renamed upstream — NOT repaired and NOT "
              f"cloned{RESET} {DIM}(the existing clone holds work that a move "
              f"would put at risk){RESET}")
        for r in held[:10]:
            print(f"   {DIM}{r['project']}: {r['moved_from']} — "
                  f"{r['detail']}{RESET}")
        if len(held) > 10:
            print(f"   {DIM}… and {len(held) - 10} more{RESET}")
        print(f"{DIM}Nothing was moved, deleted or duplicated. Resolve the "
              f"listed work by hand, then re-run clone.{RESET}")


def _failure_rows(errors: list) -> list:
    """Real clone failures — not broken dirs, not LFS warnings."""
    return [e for e in errors
            if e.get("reason") not in (REASON_INVALID, REASON_LFS)]


def _print_failure_summary(errors: list, limit: int = 10):
    """Failures grouped by error code with counts, then one line each."""
    rows = _failure_rows(errors)
    if not rows:
        return
    codes = [e.get("code") or _err_code(e.get("detail") or "") for e in rows]
    counts = Counter(codes)
    parts = ", ".join(f"{c} ×{n}" for c, n in counts.most_common())
    print(f"{RED}✖ {len(rows)} failed{RESET} {DIM}— {parts}{RESET}")
    for e, code in list(zip(rows, codes, strict=True))[:limit]:
        print(f"   {DIM}{_err_line(e.get('project', '?'), e.get('detail') or '', code)}{RESET}")
    if len(rows) > limit:
        print(f"   {DIM}… and {len(rows) - limit} more{RESET}")
    for code, _ in counts.most_common(3):
        fix = _fix_for(code)
        if fix:
            print(f"   {DIM}{code}: {fix}{RESET}")


def _print_lfs_notice(errors: list, limit: int = 10):
    rows = [e for e in errors if e.get("reason") == REASON_LFS]
    if not rows:
        return
    print(f"{YELLOW}⚠ cloned, LFS files missing ({len(rows)}){RESET} "
          f"{DIM}(repository present and kept; only large-file contents are "
          f"absent){RESET}")
    for e in rows[:limit]:
        print(f"   {DIM}{_err_line(e.get('project', '?'), e.get('detail') or '', e.get('code') or 'lfs_missing_object')}{RESET}")
    if len(rows) > limit:
        print(f"   {DIM}… and {len(rows) - limit} more{RESET}")
    fix = _fix_for(rows[0].get("code") or "lfs_missing_object") or (
        "Ask the GitLab admin to restore the missing LFS objects, then run "
        "'git lfs pull' in the repository.")
    print(f"   {DIM}{fix}{RESET}")


def _print_lfs_retry_notice(lfs_retry: list):
    """Per-repo outcome of retrying LFS for last run's LFS-incomplete repos."""
    for r in lfs_retry or []:
        if r.get("repaired"):
            print(f"{GREEN}✔ LFS repaired: {r['project']}{RESET}")
            continue
        print(f"{YELLOW}⚠ LFS still missing: {r['project']}{RESET}")
        fix = _fix_for(r.get("code") or "lfs_missing_object")
        if fix:
            print(f"   {DIM}→ {fix}{RESET}")


def _print_pending_notice(pending: list):
    if pending:
        print(f"{DIM}{len(pending)} project(s) skipped — scheduled for deletion "
              f"on the server{RESET}")


def _print_elsewhere_notice(elsewhere: list):
    if not elsewhere:
        return
    print(f"{YELLOW}⟳ {len(elsewhere)} already present elsewhere{RESET} "
          f"{DIM}(cloned under a different layout — not cloned again){RESET}")
    for e in elsewhere[:10]:
        print(f"   {DIM}{e['project']} → {e['found_at'][0]}{RESET}")
    if len(elsewhere) > 10:
        print(f"   {DIM}… and {len(elsewhere) - 10} more{RESET}")
    print(f"{DIM}Nothing was moved or deleted. This is layout drift, not a "
          f"failure — run 'doctor' to see it.{RESET}")


def _print_invalid_notice(errors: list):
    broken = [e for e in errors if e.get("reason") == REASON_INVALID]
    if not broken:
        return
    print(f"{RED}✖ {len(broken)} broken / invalid{RESET} "
          f"{DIM}(directory present, not a git repository){RESET}")
    for e in broken[:10]:
        print(f"   {DIM}{e['path']}{RESET}")
    if len(broken) > 10:
        print(f"   {DIM}… and {len(broken) - 10} more{RESET}")
    print(f"{DIM}Nothing was deleted. Inspect or remove these by hand, "
          f"then re-run clone.{RESET}")


def cmd_clone(cfg: dict, group_path: str, dest: str = "."):
    global _current_dest, _current_group
    _current_dest  = dest
    _current_group = group_path

    errors = []
    elsewhere = []
    renamed = []
    pending = []
    lfs_retry = []

    # Setup Ctrl+C handler to save errors before exit
    original_handler = signal.getsignal(signal.SIGINT)

    def _on_interrupt(sig, frame):
        print(f"\n\n{YELLOW}⚠  Interrupted — saving errors...{RESET}")
        _save_errors(dest, group_path, errors)
        print_summary(_session_stats["cloned"], _session_stats["pulled"],
                      _session_stats["failed"])
        _print_failure_summary(errors)
        _print_lfs_notice(errors)
        _print_lfs_retry_notice(lfs_retry)
        _print_pending_notice(pending)
        _print_renamed_notice(renamed)
        _print_elsewhere_notice(elsewhere)
        _print_invalid_notice(errors)
        signal.signal(signal.SIGINT, original_handler)
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _on_interrupt)
    # Everything below must be inside try/finally: any raise (paths.UnsafePath
    # from target_for_clone, anything from _process_group) used to leave
    # _on_interrupt installed over dead locals, so a later unrelated Ctrl-C
    # could delete a valid error log via the empty-list branch of _save_errors.
    try:
        print_section(f"Clone: {group_path}")
        print_info(f"GitLab : {cfg['url']}")
        print_info(f"Dest   : {os.path.abspath(dest)}")

        # Reuse the folder if it already exists, otherwise build the full
        # nested path (dest/company/team).
        #
        # cfg["folder"] is the active instance's folder setting and is
        # authoritative for paths.candidates' shaping rules #1/#4. Dropping it
        # made paths.is_instance_folder() consult the GLOBAL pool in
        # ~/.gitlab-cli/instances.json instead, which is wrong in both
        # directions: a dropped key hides the <dest>/<folder>/<group> candidate
        # so an already-cloned tree is not reused and a SECOND copy is written
        # beside it (the RC-3 mechanism), and another
        # instance's folder value gates the shaping for this one. .get() keeps
        # any caller that passes a cfg without the key on the old behaviour.
        local_path = paths.target_for_clone(group_path, dest,
                                            cfg.get("folder"))

        # Resume mode check
        if os.path.exists(local_path):
            print_info(f"{YELLOW}Resume mode{RESET} — already cloned repos are skipped")

        # Check for previous errors
        prev_errors = load_errors(dest, group_path)
        lfs_known = {os.path.abspath(e["path"]): e for e in prev_errors
                     if e.get("reason") == REASON_LFS and e.get("path")}
        prev_real = len(prev_errors) - len(lfs_known)
        if prev_real:
            print_info(f"{RED}{prev_real} errors left over from the previous run{RESET}")
        if lfs_known:
            print_info(f"{YELLOW}{len(lfs_known)} cloned with LFS files missing "
                       f"in the previous run{RESET}")

        print_separator()

        group = get_group(cfg["url"], cfg["headers"], group_path)
        if not group:
            print_error(f"Group '{group_path}' not found.")
            return

        print_group(group["full_path"])
        # Identity index of everything already under dest, so a project that
        # exists under a drifted layout is not cloned a second time here.
        index  = presence_index(dest)
        # Every current project of the TOP-LEVEL group, fetched at most once
        # and only if a rename needs the tier-3 orphan search.
        known  = _lazy_known(cfg, group_path)
        errors = _process_group(cfg, group, local_path, errors=errors,
                                index=index, elsewhere=elsewhere,
                                renamed=renamed, pending=pending,
                                lfs_known=lfs_known, known=known,
                                root=os.path.abspath(dest or "."),
                                lfs_retry=lfs_retry)
        _save_errors(dest, group_path, errors)
        print_summary(_session_stats["cloned"], _session_stats["pulled"],
                      _session_stats["failed"])
        _print_failure_summary(errors)
        _print_lfs_notice(errors)
        _print_lfs_retry_notice(lfs_retry)
        _print_pending_notice(pending)
        _print_renamed_notice(renamed)
        _print_elsewhere_notice(elsewhere)
        _print_invalid_notice(errors)
        # Only reached when the group was listed and processed: an early
        # abort (group not found, a raise) never cleans anything up.
        try:
            prune_leftovers(cfg, group_path, dest)
        except Exception as e:              # never fail the clone
            print_warn(f"leftover cleanup skipped for '{group_path}': "
                       f"{type(e).__name__}")
    finally:
        signal.signal(signal.SIGINT, original_handler)


def _recursive_projects(cfg: dict, group: dict) -> list:
    """Every project of `group` including all subgroups (include_subgroups)."""
    return list(cov.remote_projects(cfg, group) or [])


def _known_keys(cfg: dict, group_path: str, quiet: bool = False,
                projects: list = None, group: dict = None):
    """project_key()s of every project of group_path's TOP-LEVEL group.

    Returns None (tier 3 off) when the listing cannot be fetched; never raises.
    `projects`/`group` let a caller that already listed the top-level group
    reuse that listing.
    """
    host = cov.host_of(cfg.get("url", "") or "")
    top = (group_path or "").strip().strip("/").split("/")[0]
    if not top:
        return None
    try:
        if (projects is not None and group is not None and
                (group.get("full_path") or "").strip("/").lower() == top.lower()):
            plist = projects
        else:
            tg = get_group(cfg["url"], cfg["headers"], top)
            if not tg:
                raise LookupError(f"top-level group '{top}' not found")
            plist = _recursive_projects(cfg, tg)
    except Exception as e:
        if not quiet:
            print_dim(f"rename search across '{top}' is off this run — its "
                      f"project list is unavailable ({type(e).__name__})")
        return None
    keys = set()
    for p in plist or []:
        k = project_key(p, host)
        if k:
            keys.add(k)
    return keys


def _lazy_known(cfg: dict, group_path: str, quiet: bool = False):
    """A memoised zero-argument callable around _known_keys (one fetch/run)."""
    memo = {}

    def get():
        if "v" not in memo:
            memo["v"] = _known_keys(cfg, group_path, quiet=quiet)
        return memo["v"]
    return get


def reconcile_renames(cfg: dict, group_path: str, dest: str,
                      quiet: bool = False) -> list[dict]:
    """
    Move every clone of `group_path` that the server PROVES was renamed.

    group_path is the FULL group path and dest the same destination cmd_clone
    would receive. Projects are listed recursively; each one that is missing
    at its expected path and not present anywhere else is handed to
    try_rename_repair with the top-level `known` set (tier 3) and the
    destination root (D1/D3 policy). Returns the rows in _process_group's
    `renamed` shape (REASON_RENAMED / REASON_RENAME_UNSAFE).

    It NEVER clones, never deletes a non-empty directory and never raises on
    an API failure (one warning line, then []).
    """
    rows = []
    try:
        root = os.path.abspath(dest or ".")
        local_path = paths.target_for_clone(group_path, dest, cfg.get("folder"))
        group = get_group(cfg["url"], cfg["headers"], group_path)
        if not group:
            print_warn(f"rename check skipped: group '{group_path}' not found")
            return []
        projects = _recursive_projects(cfg, group)
        known = _known_keys(cfg, group_path, quiet=quiet, projects=projects,
                            group=group)
        host = cov.host_of(cfg.get("url", "") or "")
        full = (group.get("full_path") or group_path or "").strip().strip("/")
        index = presence_index(dest, refresh=True)
    except Exception as e:
        print_warn(f"rename check skipped for '{group_path}': "
                   f"{_summary_of(str(e)) or type(e).__name__}")
        return []

    # (proj, key, target) for every live project, and pre-claim everything
    # that is already where it belongs so it is never a candidate.
    work, claimed = [], set()
    for proj in projects:
        if is_pending_deletion(proj):
            continue
        pwn = (proj.get("path_with_namespace") or "").strip("/")
        if not pwn.lower().startswith(full.lower() + "/"):
            continue
        parts = _safe_rel_path(pwn[len(full) + 1:])
        if not parts:
            continue
        target = os.path.join(local_path, *parts)
        if not _inside_root(root, target):
            continue
        key = project_key(proj, host)
        state = repo_state(target)
        if state == "ok" or _existing_copies(index, key, exclude=target):
            if key:
                claimed.add(key)
            continue
        if state != "absent":
            continue                    # broken target: reported by clone/pull
        work.append((proj, key, target))

    moved_any = False
    for proj, key, target in work:
        pwn = proj.get("path_with_namespace") or proj.get("path") or ""
        try:
            repair = try_rename_repair(cfg, proj, target, index, host, claimed,
                                       known=known, root=root)
        except Exception as e:                  # never raise into pull
            print_warn(f"rename check failed for {pwn}: {type(e).__name__}")
            continue
        if repair["action"] == "moved":
            moved_any = True
            _bump("renamed")
            if key:
                claimed.add(key)
            rows.append({"project": pwn, "moved_from": repair["from"],
                         "moved_to": target, "reason": REASON_RENAMED,
                         "detail": repair["detail"]})
            if not quiet:
                print_project(pwn, 1)
                print(f"{GREEN}renamed ✓{RESET} {DIM}repaired from "
                      f"{repair['from']} — not re-cloned{RESET}")
        elif repair["action"] == "refused":
            _bump("renamed_refused")
            if key:
                claimed.add(key)
            rows.append({"project": pwn, "moved_from": repair["from"],
                         "moved_to": "", "reason": REASON_RENAME_UNSAFE,
                         "detail": repair["detail"],
                         "reasons": repair.get("reasons") or []})
            if not quiet:
                print_project(pwn, 1)
                print(f"{YELLOW}renamed ⚠{RESET} {DIM}{repair['from']} is this "
                      f"project under its old path, but it was left alone: "
                      f"{repair['detail']}{RESET}")

    if moved_any:
        reset_presence_cache()
    if rows and not quiet:
        _print_renamed_notice(rows)
    return rows


# ─── Leftover cleanup — only what provably holds nothing ──────────────────────
#
# After a rename is followed, the OLD tree keeps its skeleton: empty folders of
# projects and subgroups that no longer exist on the server, and clones of
# projects the server deleted (or scheduled for deletion) that were never more
# than an empty `git clone`. Measured under MasterGroups/group-a: an empty
# clone of a pending-deletion project plus four empty folders the server
# answers 404 for.
#
# The asymmetry is the same as the rename repair's: removing something that
# held data is unrecoverable, leaving an empty folder costs nothing. So:
#
#   * nothing happens unless the server's listing of the group (projects AND
#     subgroups) is complete — an uncertain answer deletes nothing;
#   * a folder is only ever os.rmdir()'d (which cannot remove content), only
#     when it is empty and no live subgroup/project has that path;
#   * a clone is removed only when the server says its project is gone or
#     pending deletion AND it provably holds zero commits, refs, objects,
#     stash, reflog, index entries, linked worktrees and work-tree files — all
#     re-checked immediately before the delete;
#   * symlinks are never followed and never removed, and nothing at or above
#     the group's own folder is touched.

_PRUNE_DEAD_STATES = ("pending_deletion", "gone")
_PRUNE_MAX_GROUPS = 5000


def _with_ancestors(rel: str, into: set):
    parts = [p for p in (rel or "").strip("/").lower().split("/") if p]
    for i in range(1, len(parts) + 1):
        into.add("/".join(parts[:i]))


def _complete(listing) -> bool:
    return listing is not None and bool(getattr(listing, "complete", True))


def _live_server_paths(cfg: dict, group: dict):
    """
    (live_rel_paths, live_project_keys) for `group`, or (None, None).

    live_rel_paths are lower-cased paths RELATIVE to the group's full_path of
    every subgroup (all levels, from the subgroup listing so a project-less
    subgroup counts), every project, and every ancestor namespace of either.
    Archived and pending-deletion projects are included (get_projects lists
    archived ones by default, and no min_access filter is applied, so a
    project this token can see but not write to still counts as live). The
    walk uses the same get_projects/get_subgroups calls as the clone itself.
    None means the server answer is missing or incomplete, and the caller
    must then delete nothing.
    """
    full = (group.get("full_path") or "").strip().strip("/")
    if not full or group.get("id") is None:
        return None, None
    prefix = full.lower() + "/"
    host = cov.host_of(cfg.get("url", "") or "")
    rel, keys = set(), set()

    queue, seen = [group], {group.get("id")}
    while queue:
        g = queue.pop(0)
        projects = get_projects(cfg["url"], cfg["headers"], g["id"])
        if not _complete(projects):
            return None, None
        for p in projects:
            if not isinstance(p, dict):
                return None, None
            k = project_key(p, host)
            if k:
                keys.add(k)
            pwn = (p.get("path_with_namespace") or "").strip().strip("/")
            if pwn.lower().startswith(prefix):
                _with_ancestors(pwn[len(prefix):], rel)
        subs = get_subgroups(cfg["url"], cfg["headers"], g["id"])
        if not _complete(subs):
            return None, None
        for sg in subs:
            if not isinstance(sg, dict) or sg.get("id") is None:
                return None, None
            if sg["id"] in seen:
                continue
            seen.add(sg["id"])
            if len(seen) > _PRUNE_MAX_GROUPS:
                return None, None
            sfp = (sg.get("full_path") or "").strip().strip("/")
            if sfp.lower().startswith(prefix):
                _with_ancestors(sfp[len(prefix):], rel)
            queue.append(sg)
    return rel, keys


def _scan_leftovers(root: str):
    """(plain_dirs, repo_dirs) strictly under root.

    Never descends into a git work tree (a dir holding a `.git` dir or file)
    and never follows, lists or returns a symlink.
    """
    plain, repos = [], []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_symlink() or not e.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if os.path.lexists(os.path.join(e.path, ".git")):
                repos.append(e.path)
            else:
                plain.append(e.path)
                stack.append(e.path)
    return plain, repos


def _is_empty_dir(d: str) -> bool:
    try:
        return (not os.path.islink(d)) and os.path.isdir(d) and not os.listdir(d)
    except OSError:
        return False


def _raw_origin_path(url: str) -> str:
    """'group/sub/proj' from an origin URL, case preserved ('' if unparsable)."""
    u = _strip_userinfo(url or "").strip()
    if "://" in u:
        u = u.split("://", 1)[1]
    elif u.startswith("git@"):
        u = u[4:].replace(":", "/", 1)
    _, _, rest = u.partition("/")
    return rest.strip("/").removesuffix(".git").strip("/")


def _git_out(repo_dir: str, *args):
    """(ok, stdout) of one read-only git call inside repo_dir."""
    rc, out, _ = gitlib._run([gitlib.GIT_BIN, "-C", repo_dir, *args])
    return rc == 0, (out or "")


def _dead_clone_problem(repo_dir: str, root: str) -> str:
    """'' when repo_dir provably holds nothing at all, else why it is kept."""
    dotgit = os.path.join(repo_dir, ".git")
    if os.path.islink(repo_dir) or not os.path.isdir(repo_dir):
        return "not a plain directory"
    if not _inside_root(root, repo_dir):
        return "outside the group folder"
    try:
        names = os.listdir(repo_dir)
    except OSError:
        return "unreadable"
    if names != [".git"]:
        return "the work tree holds files"
    if os.path.islink(dotgit) or not os.path.isdir(dotgit):
        return ".git is not a plain directory"
    wt = os.path.join(dotgit, "worktrees")
    if os.path.lexists(wt):
        try:
            if os.path.islink(wt) or not os.path.isdir(wt) or os.listdir(wt):
                return "it has linked worktrees"
        except OSError:
            return "unreadable worktrees"
    logs = os.path.join(dotgit, "logs")
    if os.path.lexists(logs):
        if os.path.islink(logs):
            return "unusual reflog layout"
        for dp, _dirs, fn in os.walk(logs):
            for f in fn:
                fp = os.path.join(dp, f)
                try:
                    if os.path.islink(fp) or os.path.getsize(fp) > 0:
                        return "it has reflog entries"
                except OSError:
                    return "unreadable reflog"
    ok, out = _git_out(repo_dir, "rev-parse", "--absolute-git-dir")
    if not ok or os.path.realpath(out.strip()) != os.path.realpath(dotgit):
        return "git does not resolve this .git"
    ok, out = _git_out(repo_dir, "rev-list", "--all", "--count")
    if not ok or out.strip() != "0":
        return "it has commits"
    ok, out = _git_out(repo_dir, "stash", "list")
    if not ok or out.strip():
        return "it has a stash"
    ok, out = _git_out(repo_dir, "for-each-ref")
    if not ok or out.strip():
        return "it has refs"
    ok, out = _git_out(repo_dir, "ls-files", "--stage")
    if not ok or out.strip():
        return "it has staged files"
    ok, out = _git_out(repo_dir, "count-objects", "-v")
    if not ok:
        return "count-objects failed"
    counts = {}
    for line in out.splitlines():
        k, _, v = line.partition(":")
        counts[k.strip()] = v.strip()
    if any(counts.get(k) != "0" for k in ("count", "in-pack", "packs")):
        return "it has git objects"
    return ""


def _remove_dead_clone(repo_dir: str, root: str) -> str:
    """Delete one provably-empty clone; '' on success, else why not."""
    problem = _dead_clone_problem(repo_dir, root)      # re-check, right now
    if problem:
        return problem
    dotgit = os.path.join(repo_dir, ".git")
    real = os.path.realpath(dotgit)
    root_r = os.path.realpath(root)
    if (os.path.basename(dotgit) != ".git" or os.path.basename(real) != ".git"
            or not real.startswith(root_r + os.sep)
            or os.path.dirname(real) != os.path.realpath(repo_dir)
            or os.path.realpath(repo_dir) == root_r):
        return "refused: .git does not resolve inside the group folder"
    try:
        shutil.rmtree(dotgit)
        os.rmdir(repo_dir)
    except OSError as e:
        return f"delete failed: {type(e).__name__}"
    return ""


def prune_leftovers(cfg: dict, group_path: str, dest: str,
                    quiet: bool = False) -> list[dict]:
    """
    Remove ONLY provably-empty leftovers inside group_path's local folder.

    Rule A: an empty directory whose path is not a live subgroup/project
    (case-insensitive) is os.rmdir()'d, bottom-up, so parents it empties go
    too. Rule B: a clone whose origin is not a live project, which the server
    reports as 'pending_deletion' or 'gone', and which holds nothing at all
    (_dead_clone_problem) has its .git removed and its folder rmdir()'d.

    The group folder itself and dest are never removed and nothing above them
    is touched. An incomplete or failed server listing deletes nothing. Never
    raises. Returns [{'path': rel-to-dest, 'kind': 'empty_dir' |
    'empty_dead_clone', 'reason': str}].
    """
    rows = []
    try:
        base = os.path.abspath(dest or ".")
        root = os.path.abspath(paths.target_for_clone(group_path, dest,
                                                      cfg.get("folder")))
        if os.path.islink(root) or not os.path.isdir(root):
            return []
        if not paths._under(base, root):
            print_warn(f"leftover cleanup skipped for '{group_path}': its "
                       f"folder is outside the destination")
            return []
        if os.path.lexists(os.path.join(root, ".git")):
            return []                       # the group folder is itself a repo
        plain, repos = _scan_leftovers(root)
        # Local pre-check, no network: rule A can only ever start from a dir
        # that is empty NOW, rule B only from a clone that holds nothing. A
        # tree with neither is left alone without asking the server anything.
        dead = [r for r in repos if not _dead_clone_problem(r, root)]
        if not dead and not any(_is_empty_dir(d) for d in plain):
            return []
        group = get_group(cfg["url"], cfg["headers"], group_path)
        if not group:
            print_warn(f"leftover cleanup skipped: group '{group_path}' "
                       f"not found")
            return []
        live, live_keys = _live_server_paths(cfg, group)
        if live is None:
            print_warn(f"leftover cleanup skipped for '{group_path}': the "
                       f"server listing is incomplete — nothing was removed")
            return []
        host = cov.host_of(cfg.get("url", "") or "")
    except Exception as e:
        print_warn(f"leftover cleanup skipped for '{group_path}': "
                   f"{_summary_of(str(e)) or type(e).__name__}")
        return []

    def _rel(p):
        return os.path.relpath(p, base).replace(os.sep, "/")

    try:
        # Rule B — dead, provably empty clones (re-proven before deletion).
        for repo in dead:
            with contextlib.suppress(Exception):  # keep it, try the next one
                url = _origin_of(repo)
                key = cov.normalize(url)
                if not key or key[0] != host or key in live_keys:
                    continue
                look = redirect_lookup(cfg, _raw_origin_path(url))
                if look.get("state") not in _PRUNE_DEAD_STATES:
                    continue
                if _remove_dead_clone(repo, root):
                    continue
                rows.append({"path": _rel(repo), "kind": "empty_dead_clone",
                             "reason": (f"{look.get('state')}: "
                                        f"{look.get('detail') or ''}").strip()})

        # Rule A — empty folders the server does not know, deepest first.
        for d in sorted(plain, key=lambda p: p.count(os.sep), reverse=True):
            try:
                rel_root = os.path.relpath(d, root).replace(os.sep, "/").lower()
                if rel_root in live or rel_root.startswith(".."):
                    continue
                if os.path.islink(d) or not os.path.isdir(d):
                    continue
                if not _inside_root(root, d) or os.listdir(d):
                    continue
                os.rmdir(d)
                rows.append({"path": _rel(d), "kind": "empty_dir",
                             "reason": "empty, and not a group or project "
                                       "on the server"})
            except OSError:
                continue
    except Exception as e:
        print_warn(f"leftover cleanup stopped for '{group_path}': "
                   f"{type(e).__name__}")

    if rows:
        reset_presence_cache()
        if not quiet:
            print(f"{GREEN}🧹 Cleaned {len(rows)} leftover(s) — nothing with "
                  f"data was removed{RESET}")
            for r in rows:
                label = ("empty clone" if r["kind"] == "empty_dead_clone"
                         else "empty folder")
                print_dim(f"   {label}: {r['path']} — {r['reason']}")
    return rows


def cmd_retry_failed(cfg: dict, group_path: str, dest: str = "."):
    """Retry only the previously failed projects."""
    errors = load_errors(dest, group_path)

    if not errors:
        print_info(f"{GREEN}There are no errors to retry!{RESET}")
        return

    print_section(f"Retry Failed: {group_path}")
    print_info(f"{len(errors)} projects will be retried...")
    print_separator()

    still_failed = []
    fixed     = 0
    broken    = 0
    elsewhere = 0
    gone      = 0
    lfs       = 0
    pending   = 0
    renamed_skipped = 0
    index     = presence_index(dest)

    for e in errors:
        print_project(e["project"], 1)
        target = e["path"]
        key    = cov.normalize(e.get("url") or "")

        # Scheduled for deletion on the server: never cloned, row dropped.
        if (is_pending_deletion(key[1] if key else "")
                or is_pending_deletion(e.get("url") or "")
                or is_pending_deletion(target)):
            print(f"{DIM}skipped — scheduled for deletion on the server{RESET}")
            pending += 1
            continue

        state  = repo_state(target)

        # Present, but its LFS contents were missing: never re-cloned. The
        # row is dropped only once the contents are measurably complete.
        if e.get("reason") == REASON_LFS and state == "ok":
            _remember(index, key, target)
            if _lfs_missing(target) is False:
                print(f"{GREEN}LFS complete ✓{RESET}")
                fixed += 1
                continue
            print(f"{YELLOW}LFS ⚠{RESET} {DIM}present; LFS files still "
                  f"missing — not re-cloned{RESET}")
            lfs += 1
            still_failed.append(dict(e))
            continue

        # If somehow already cloned (manual fix), skip
        if state == "ok":
            print(f"{DIM}already fixed ✓{RESET}")
            _remember(index, key, target)
            continue

        if state == "invalid":
            # A present-but-broken directory is never cloned over automatically.
            print(f"{RED}broken ✖{RESET} {DIM}not a valid git repo — left untouched{RESET}")
            broken += 1
            entry = dict(e)
            entry["reason"] = REASON_INVALID
            still_failed.append(entry)
            continue

        # Already on disk under another layout — retrying would duplicate it.
        present = _existing_copies(index, key, exclude=target)
        if present:
            print(f"{YELLOW}present ✓{RESET} "
                  f"{DIM}already cloned at {present[0]}{RESET}")
            elsewhere += 1
            continue

        # The recorded URL may have been renamed on the server since the log
        # was written. Cloning it anyway lands a clone whose origin is the OLD
        # path at the OLD location — a duplicate of whatever the clone path has
        # since put at the new one. Ask the server before spending the bytes.
        look = redirect_lookup(cfg, key[1]) if key else {"state": "same"}
        if look["state"] == "pending_deletion":
            print(f"{DIM}skipped — scheduled for deletion on the server{RESET}")
            pending += 1
            continue
        if look["state"] == "renamed":
            print(f"{YELLOW}renamed ⚠{RESET} "
                  f"{DIM}now '{look['new_path']}' on the server — not cloned "
                  f"at the stale path; re-run clone for its group{RESET}")
            renamed_skipped += 1
            continue
        if look["state"] == "gone":
            print(f"{RED}gone ✖{RESET} "
                  f"{DIM}deleted upstream (HTTP 404) — nothing to clone{RESET}")
            gone += 1
            entry = dict(e)
            entry["reason"] = "deleted_upstream"
            entry["code"] = "not_found"
            still_failed.append(entry)
            continue

        res = _clone(cfg["token"], e["url"], target)
        status = res.get("status")
        detail = res.get("detail") or ""
        if status == "ok":
            fixed += 1
            _remember(index, key, target)
        elif status == S_LFS:
            print(f"{YELLOW}LFS ⚠{RESET} "
                  f"{DIM}cloned; LFS files missing — repository kept{RESET}")
            lfs += 1
            _remember(index, key, target)
            entry = dict(e)
            entry.update(reason=REASON_LFS, code=_lfs_code(detail),
                         detail=detail)
            still_failed.append(entry)
        else:
            failed = dict(e)
            failed.pop("reason", None)
            failed["code"] = _err_code(detail, status)
            failed["detail"] = detail
            still_failed.append(failed)

    _save_errors(dest, group_path, still_failed)

    print_separator()
    if fixed:
        print(f"{GREEN}✔ {fixed} projects fixed{RESET}")
    if renamed_skipped:
        print(f"{YELLOW}⟳ {renamed_skipped} renamed on the server — not "
              f"cloned at their stale paths (no duplicate was created){RESET}")
    if gone:
        print(f"{RED}✖ {gone} deleted upstream — the server returns 404 for "
              f"them; nothing was cloned and nothing was deleted here{RESET}")
    if elsewhere:
        print(f"{YELLOW}⟳ {elsewhere} already present elsewhere — not cloned "
              f"again{RESET}")
    if broken:
        print(f"{RED}✖ {broken} broken / invalid — directory present but not a "
              f"git repository; nothing was deleted{RESET}")
    _print_pending_notice([None] * pending)
    _print_lfs_notice(still_failed)
    failing = [r for r in still_failed if r.get("reason") != REASON_LFS]
    if failing:
        print(f"{RED}✖ {len(failing)} still failing{RESET}")
        _print_failure_summary(still_failed)
    elif still_failed:
        print(f"{GREEN}✔ No clone failures left{RESET} {DIM}— only LFS "
              f"warnings remain{RESET}")
    else:
        print(f"{GREEN}✔ All errors resolved!{RESET}")
