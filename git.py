# git.py — Git operation helpers

import subprocess
import os
import re
import shutil
from printer import print_ok, print_warn, print_error
from errors import log_error, redact

# Resolve git's absolute path once.
# Without this, `git` is looked up on PATH — so a planted `git` anywhere on
# the user's PATH (inside a cloned repo's directory, for example) would run
# instead. An absolute path closes that off.
GIT_BIN = shutil.which("git") or "git"

# No git command may exceed this (VPN drop, or an auth prompt).
GIT_TIMEOUT = 120

# Status codes for sync_repo()
S_UP_TO_DATE = "up_to_date"
S_PULLED     = "pulled"
S_AHEAD      = "ahead"
S_DIVERGED   = "diverged"
S_DIRTY      = "dirty"
S_DETACHED   = "detached"
S_NO_REMOTE  = "no_remote"
S_GONE       = "gone"
S_AUTH       = "auth"
S_ERROR      = "error"

_AUTH_PAT = re.compile(
    r"(401|403|Authentication failed|Access denied|HTTP Basic|"
    r"could not read Username|Permission denied \(publickey\))",
    re.I,
)

# The repository was deleted or renamed on the server.
_GONE_PAT = re.compile(
    r"(repository .* not found|project you were looking for could not be found|"
    r"Repository not found|does not appear to be a git repository)",
    re.I,
)


def _run(args: list[str], cwd: str = None, timeout: int = GIT_TIMEOUT):
    """Run git; always returns (rc, stdout, stderr) and never hangs.

    stdout/stderr are redacted: a remote URL can carry a token, and this
    text is both printed and written to ~/.gitlab-cli/errors.log.
    """
    env = dict(os.environ)
    # Suppress the interactive username/password prompt, which would hang us.
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_ASKPASS", "echo")
    try:
        r = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True,
            timeout=timeout, env=env,
        )
        return r.returncode, redact(r.stdout.strip()), redact(r.stderr.strip())
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except Exception as e:                       # pragma: no cover
        return 1, "", redact(str(e))


def _git(path: str, *args: str, timeout: int = GIT_TIMEOUT):
    return _run([GIT_BIN, "-C", path, *args], timeout=timeout)


def is_auth_error(text: str) -> bool:
    return bool(text and _AUTH_PAT.search(text))


def is_gone(text: str) -> bool:
    """The repository is no longer on the server — deleted, renamed, or access revoked."""
    return bool(text and _GONE_PAT.search(text))


def classify(text: str) -> str:
    """Map a git error to the matching status."""
    if is_auth_error(text):
        return S_AUTH
    if is_gone(text):
        return S_GONE
    return S_ERROR


def inject_token(http_url: str, token: str) -> str:
    """Put the token into the URL, for http:// as well as https:// (internal hosts use http)."""
    if not token or "@" in http_url.split("//", 1)[-1].split("/", 1)[0]:
        return http_url
    for scheme in ("https://", "http://"):
        if http_url.startswith(scheme):
            return f"{scheme}oauth2:{token}@{http_url[len(scheme):]}"
    return http_url


class UnsafeURL(ValueError):
    """The repository URL cannot be trusted."""


_ALLOWED_SCHEMES = ("https://", "http://")


def check_url(url: str) -> str:
    """
    Validate a repository URL before handing it to git.

    This blocks two attacks:

    1. Argument injection — git consumes a URL beginning with `-` as an
       option rather than an address. `--upload-pack=<cmd>` means command
       execution. Verified: it yields a real RCE.
    2. Dangerous transports — `ext::sh -c ...` is git's own documented RCE.
       Only http/https are allowed (an internal host without TLS still has
       to work, so http is not dropped).

    The URL comes from the GitLab API, which means anyone who can create a
    project on that instance controls this string.
    """
    u = (url or "").strip()
    if not u:
        raise UnsafeURL("empty URL")
    if u.startswith("-"):
        raise UnsafeURL(f"a URL cannot start with '-': {u[:60]}")
    if not u.startswith(_ALLOWED_SCHEMES):
        raise UnsafeURL(f"only http/https are accepted, not: {u[:60]}")
    return u


# ─── Clone ─────────────────────────────────────────────────────────────────────

def clone_repo(token: str, http_url: str, target_path: str) -> bool:
    try:
        safe_url = check_url(http_url)
    except UnsafeURL as e:
        print_error(f"Clone refused: {e}")
        log_error(f"clone:{target_path}", f"unsafe url: {e}")
        return False

    auth_url = inject_token(safe_url, token)
    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)

    # `--` is required: without it, a URL starting with `-` becomes an option.
    rc, _, err = _run([GIT_BIN, "clone", "--quiet", "--", auth_url, target_path])

    if rc == 0:
        print_ok("Cloned ✔")
        return True

    err = err[:200]
    if is_auth_error(err):
        print_error("Clone failed: token expired or access denied (401/403)")
    else:
        print_error(f"Clone failed: {err[:120]}")
    log_error(f"clone:{target_path}", err)
    return False


# ─── Sync (fetch, then work out which remote is ahead) ─────────────────────────

def _remotes(path: str) -> list[str]:
    rc, out, _ = _git(path, "remote")
    return [line.strip() for line in out.splitlines() if line.strip()] if rc == 0 else []


def _current_branch(path: str) -> str | None:
    rc, out, _ = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    return out if rc == 0 and out else None      # None = detached HEAD


def _is_dirty(path: str) -> bool:
    rc, out, _ = _git(path, "status", "--porcelain", "--untracked-files=no")
    return rc == 0 and bool(out)


def _ref_exists(path: str, ref: str) -> bool:
    rc, _, _ = _git(path, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
    return rc == 0


def _counts(path: str, local_ref: str, remote_ref: str) -> tuple[int, int]:
    """(ahead, behind) — ahead = local is further, behind = remote is further."""
    rc, out, _ = _git(path, "rev-list", "--left-right", "--count",
                      f"{local_ref}...{remote_ref}")
    if rc != 0 or not out:
        return 0, 0
    parts = out.split()
    if len(parts) != 2:
        return 0, 0
    return int(parts[0]), int(parts[1])


def _ref_time(path: str, ref: str) -> int:
    rc, out, _ = _git(path, "log", "-1", "--format=%ct", ref)
    return int(out) if rc == 0 and out.isdigit() else 0


def _other_branches_behind(path: str, remote: str, skip_branch: str) -> list[str]:
    """Other branches the remote is ahead of, excluding the current one."""
    rc, out, _ = _git(path, "for-each-ref", "--format=%(refname:short)",
                      "refs/heads")
    if rc != 0:
        return []
    behind = []
    for br in out.splitlines():
        br = br.strip()
        if not br or br == skip_branch:
            continue
        rref = f"{remote}/{br}"
        if not _ref_exists(path, rref):
            continue
        _, b = _counts(path, br, rref)
        if b > 0:
            behind.append(f"{br} ↓{b}")
    return behind


def sync_repo(path: str) -> dict:
    """
    fetch --all, then work out which remote is ahead for EACH remote and
    fast-forward only when that is safe.
    """
    res = {
        "path": path, "status": S_ERROR, "detail": "",
        "branch": None, "remote": None, "ahead": 0, "behind": 0,
        "other": [],
    }

    if not os.path.exists(os.path.join(path, ".git")):
        res["status"], res["detail"] = S_ERROR, "Not a git repo"
        return res

    remotes = _remotes(path)
    if not remotes:
        res["status"], res["detail"] = S_NO_REMOTE, "no remote configured"
        return res

    # 1) Always fetch first — there is no way to tell who is ahead without it.
    rc, _, err = _git(path, "fetch", "--all", "--prune", "--tags", "--quiet")
    if rc != 0:
        detail = " ".join(err.split())[:200]
        res["status"] = classify(detail)
        if res["status"] == S_GONE:
            res["detail"] = "not on the server (deleted, renamed, or access revoked)"
        else:
            res["detail"] = detail
        log_error(f"fetch:{path}", detail)
        return res

    branch = _current_branch(path)
    if branch is None:
        res["status"] = S_DETACHED
        res["detail"] = "detached HEAD — skipped"
        return res
    res["branch"] = branch

    # 2) Compute ahead/behind for every remote
    cands = []
    for rm in remotes:
        rref = f"{rm}/{branch}"
        if not _ref_exists(path, rref):
            continue
        a, b = _counts(path, branch, rref)
        cands.append({"remote": rm, "ref": rref, "ahead": a,
                      "behind": b, "time": _ref_time(path, rref)})

    if not cands:
        res["status"] = S_NO_REMOTE
        res["detail"] = f"no remote has branch '{branch}'"
        return res

    # 3) The furthest-ahead remote wins, but remotes that can fast-forward
    #    take priority — otherwise one diverged mirror blocks an otherwise
    #    healthy pull.
    ff_ok = [c for c in cands if c["ahead"] == 0]
    pool  = ff_ok or cands
    best  = max(pool, key=lambda c: (c["behind"], c["time"]))
    res.update(remote=best["remote"], ahead=best["ahead"], behind=best["behind"])
    res["other"] = _other_branches_behind(path, best["remote"], branch)

    dirty = _is_dirty(path)
    res["dirty"] = dirty

    # 4) No upstream? Set one, so a bare `git pull` works next time.
    if not _ref_exists(path, "@{u}"):
        _git(path, "branch", f"--set-upstream-to={best['ref']}", branch)

    # 5) Decide
    if best["behind"] == 0 and best["ahead"] == 0:
        # In sync, but there are uncommitted files.
        if dirty:
            res["status"] = S_DIRTY
            res["detail"] = "has uncommitted changes"
            return res
        res["status"] = S_UP_TO_DATE
        return res

    if best["ahead"] > 0 and best["behind"] > 0:
        res["status"] = S_DIVERGED
        res["detail"] = (f"{best['remote']}: ↑{best['ahead']} ↓{best['behind']} "
                         f"— merge or rebase by hand")
        return res

    if best["ahead"] > 0:
        res["status"] = S_AHEAD
        res["detail"] = f"{best['ahead']} unpushed commit(s)"
        if dirty:
            res["detail"] += " + uncommitted changes"
        return res

    # behind > 0 → the remote is ahead
    if dirty:
        res["status"] = S_DIRTY
        res["detail"] = (f"{best['behind']} commit(s) behind but the working "
                         f"tree is dirty — not pulled")
        return res

    rc, _, err = _git(path, "merge", "--ff-only", best["ref"])
    if rc == 0:
        res["status"] = S_PULLED
        res["detail"] = f"{best['behind']} commit(s) from {best['remote']}"
        return res

    detail = " ".join(err.split())[:200]
    res["status"] = classify(detail)
    res["detail"] = detail
    log_error(f"pull:{path}", detail)
    return res


def pull_repo(path: str) -> tuple[bool, str]:
    """Compatibility wrapper for older call sites."""
    r = sync_repo(path)
    ok = r["status"] in (S_UP_TO_DATE, S_PULLED, S_AHEAD)
    msg = r["detail"] or r["status"]
    if ok:
        print_ok(msg)
    else:
        print_warn(msg)
    return ok, msg


# ─── Log ───────────────────────────────────────────────────────────────────────

def get_commits_since(repo_path: str, since: str, author: str = None) -> list[dict]:
    if not os.path.exists(os.path.join(repo_path, ".git")):
        return []

    branch = _current_branch(repo_path) or "detached"

    fmt = "%H|%an|%ad|%s"
    cmd = [
        GIT_BIN, "-C", repo_path, "log",
        f"--since={since}",
        f"--format={fmt}",
        "--date=format:%Y-%m-%d %H:%M",
        "--all"
    ]
    if author:
        cmd += [f"--author={author}"]

    rc, out, _ = _run(cmd)
    if rc != 0 or not out:
        return []

    commits = []
    for line in out.splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append({
                "hash":    parts[0][:8],
                "author":  parts[1],
                "date":    parts[2],
                "message": parts[3],
                "branch":  branch,
            })
    return commits
