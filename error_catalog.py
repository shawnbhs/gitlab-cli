# error_catalog.py — the single catalog of errors gitlab-cli knows how to explain
#
# Pure: stdlib only, no I/O, no imports from other repo modules. errors.py,
# git.py and the screens call explain()/summarize()/one_line(); nothing here
# may touch disk or the network.
#
# Matching rules — each one is scar tissue:
#   * HTTP status codes are matched ONLY with word boundaries AND an HTTP
#     context ("HTTP 404", "returned error: 401", "403 Forbidden", "404 — Not
#     found"). The old catalog matched a bare "404" anywhere, so a hex LFS oid
#     or a path containing those digits was reported as "Resource not found".
#   * Order is specific-first and the first match wins: LFS before the generic
#     4xx entries, pending_deletion before not_found, proxy before forbidden
#     (a proxy can answer 403), auth before permission_denied (publickey).
#   * stderr is summarised from the END: git prints progress first and the
#     real cause (fatal:/LFS:) last, so head-truncation hides it.

import re
from dataclasses import dataclass, replace

TYPES = ("Network", "Auth", "Access", "NotFound", "Server", "Git", "LFS",
         "Disk", "Config", "Safety", "Local", "Unknown")


@dataclass(frozen=True)
class ErrorInfo:
    code: str      # stable snake_case id, e.g. "lfs_missing_object"
    type: str      # one of TYPES
    title: str     # <= 40 chars
    cause: str     # ONE plain sentence, <= 110 chars: what actually went wrong
    fix: str       # ONE imperative sentence, <= 110 chars: what to do


@dataclass(frozen=True)
class _Entry:
    info: ErrorInfo
    pattern: re.Pattern


def _http(codes: str, reason: str) -> str:
    """Regex for an HTTP status in a real HTTP context — never a bare number.

    `codes` is a regex alternation of status digits (e.g. "401" or "50[0234]"),
    `reason` the reason phrase(s) that may follow the number.
    """
    c = rf"(?:{codes})"
    return (
        rf"\bHTTP(?:/[\d.]+)?\s*[:/ ]?\s*{c}\b"                  # HTTP 404 / HTTP/1.1 404
        rf"|\b(?:returned error|status(?: code)?|error)\s*[:=]?\s*{c}\b"  # returned error: 401
        rf"|\b{c}\s*(?:[—:-]\s*)?(?:{reason})"                  # 404 Not Found / 404 — Not found
        rf"|\bfrom HTTP {c}\b"                                  # git-lfs "... from HTTP 422"
    )


def _e(code, type_, title, cause, fix, pattern) -> _Entry:
    return _Entry(ErrorInfo(code, type_, title, cause, fix),
                  re.compile(pattern, re.I))


CATALOG: tuple = (
    # ── Safety: the tool's own refusals (exact texts from git.py) ────────────
    _e("unsafe_url", "Safety", "Refused an unsafe clone URL",
       "The remote URL is not plain http/https, so the tool refused to hand it to git.",
       "Fix the project URL to start with https:// (check it under Instances), then retry.",
       r"\bunsafe url\b|only http/https are accepted|a URL cannot start with '-'"),
    _e("unsafe_remote", "Safety", "Refused an unsafe remote name",
       "A remote name in this repo looks like a git option or holds control characters.",
       "Run `git remote -v` in the repo and rename the odd remote with `git remote rename`.",
       r"\bunsafe remote name|a remote name cannot"),
    _e("unsafe_ref", "Safety", "Refused an unsafe branch/ref name",
       "A branch or ref name starts with '-' or holds control characters, so git was not run.",
       "Run `git branch -a` in the repo and rename the odd branch with `git branch -m`.",
       r"a ref cannot (?:start with|contain)|\bunsafe ref\b"),
    _e("unsafe_arg", "Safety", "Refused an unsafe git argument",
       "An argument passed to git looked like an injected option, so the tool refused to run it.",
       "Re-run with --debug and check which project path or URL begins with '-'.",
       r"refused an unsafe git argument"),

    # ── LFS: before every generic 4xx/404 entry ──────────────────────────────
    _e("lfs_not_installed", "LFS", "git-lfs is not installed",
       "This repo stores large files in Git LFS but the git-lfs program is not installed.",
       "Run `sudo apt install git-lfs && git lfs install`, then retry the clone.",
       r"git: 'lfs' is not a git command|git-lfs\S*:\s*(?:\d+:\s*)?(?:git-lfs:\s*)?"
       r"(?:command )?not found|'git-lfs' was not found|cannot run git-lfs"),
    _e("lfs_auth", "LFS", "LFS download was not authorised",
       "The LFS server rejected the credentials when the large files were requested.",
       "Give the token read_repository scope (Instances → New token), then run `git lfs pull`.",
       r"(?:LFS|batch response):\s*Authori[sz]ation error"
       r"|(?:LFS|batch response)\b.*?(?:" + _http("40[13]", "Unauthorized|Forbidden") + ")"),
    _e("lfs_missing_object", "LFS", "LFS files missing on the server",
       "The repo cloned, but its LFS file contents are missing on the server, so files are pointers.",
       "Ask the project owner to run `git lfs push --all origin`, then run `git lfs pull` here.",
       r"smudge filter lfs failed|Smudge error|LFS:\s*(?:Client error|Repository or object not found)"
       r"|Error downloading object|batch response:\s*Repository or object not found"
       r"|Object does not exist on the server"),

    # ── Network: before the HTTP-status block (a proxy can answer 403) ───────
    _e("proxy", "Network", "Proxy refused the connection",
       "An HTTP proxy between you and GitLab refused or dropped the connection.",
       "Run `env | grep -i proxy`; add the GitLab host to no_proxy or fix the proxy setting.",
       r"Proxy CONNECT aborted|from proxy after CONNECT|\bProxyError\b"
       r"|Unable to connect to proxy|proxy CONNECT|Could not resolve proxy"),
    _e("ssl", "Network", "TLS certificate not trusted",
       "The GitLab server's TLS certificate could not be verified against the trusted CAs.",
       "Copy the company CA into /usr/local/share/ca-certificates/ and run `sudo update-ca-certificates`.",
       r"SSL certificate problem|server certificate verification failed|CERTIFICATE_VERIFY_FAILED"
       r"|\bSSLError\b|unable to get local issuer certificate|SSL_ERROR_SYSCALL"
       r"|self[- ]signed certificate"),
    _e("dns", "Network", "GitLab host name not found",
       "The GitLab host name could not be resolved, so the server was never contacted.",
       "Connect the VPN, then check the name resolves with `getent hosts <gitlab-host>`.",
       r"Could not resolve host|\bNameResolutionError\b|Name or service not known"
       r"|Temporary failure in name resolution|getaddrinfo failed|nodename nor servname"),
    _e("timeout", "Network", "Operation timed out",
       "GitLab did not answer before the time limit, so the operation was abandoned.",
       "Check the VPN with `curl -I https://<gitlab-host>`, then retry the same action.",
       r"\btimed out after \d+\s*s\b|\bReadTimeout\b|\bConnectTimeout(?:Error)?\b"
       r"|Connection timed out|Operation timed out|\bTimeout:\s"),
    _e("connection_refused", "Network", "Cannot connect to GitLab",
       "The GitLab server could not be reached: the connection was refused or no route exists.",
       "Connect the VPN and check the server answers with `curl -I https://<gitlab-host>`.",
       r"Connection refused|Failed to connect to|No route to host|Network is unreachable"
       r"|Max retries exceeded|\bConnectionError\b|Connection failed — check your VPN"
       r"|request failed \(timeout or connection error\)"),
    _e("host_key", "Network", "SSH host key not trusted",
       "SSH refused the server because its host key is unknown or has changed.",
       "Run `ssh-keygen -R <gitlab-host>` and connect once with `ssh -T git@<gitlab-host>`.",
       r"Host key verification failed|REMOTE HOST IDENTIFICATION HAS CHANGED"),

    # ── Transfer failures: specific curl codes before the generic RPC line ───
    _e("transfer_cap", "Network", "Large transfer was cut off",
       "The server or a proxy closed the connection mid-pack, typical for a very large repo.",
       "Retry; the tool falls back to a shallow clone, then run `git fetch --deepen=500` later.",
       r"transfer closed with outstanding read data|curl 18\b|curl 92\b"
       r"|HTTP/2 stream \d+ was not closed cleanly"),
    _e("rpc_failed", "Network", "Git transfer failed (RPC)",
       "The connection to GitLab broke while git was transferring data.",
       "Retry the same action; if it repeats, run `git config --global http.version HTTP/1.1`.",
       r"RPC failed|curl 56\b|Recv failure|Connection reset by peer"),
    _e("early_eof", "Network", "Server hung up mid-transfer",
       "The server stopped sending before git received the full pack of objects.",
       "Retry; for a huge repo the tool's shallow fallback clones it with less data.",
       r"early EOF|unexpected disconnect|index-pack failed|invalid index-pack output"
       r"|the remote end hung up unexpectedly"),

    # ── Auth / access / not-found (HTTP context only) ────────────────────────
    _e("auth", "Auth", "Token rejected (401)",
       "GitLab rejected the credentials: the token is missing, expired, revoked or wrong.",
       "Create a new Personal Access Token and set it with Instances → New token.",
       r"HTTP Basic: Access denied|Authentication failed|could not read Username"
       r"|could not read Password|Permission denied \(publickey|Invalid or expired token"
       r"|invalid_token|" + _http("401", "Unauthorized")),
    _e("purged", "NotFound", "Purged from the server",
       "The project was in GitLab delayed deletion and is now gone; this clone may be the last copy.",
       "Archive this folder (mv it aside) if you still need it; nothing to sync.",
       r"\bpurged from (?:the )?server\b|\bproject (?:was|has been) purged\b"),
    _e("pending_deletion", "NotFound", "Scheduled for deletion on GitLab",
       "This project or group is in GitLab's delayed deletion and will be removed soon.",
       "Skip it, or ask a group Owner to restore it under Settings → General → Advanced.",
       r"-deletion_scheduled-\d+\b|\bmarked_for_deletion|pending deletion|scheduled for deletion"),
    _e("forbidden", "Access", "Access denied (403)",
       "Your account can see this project or group but lacks the access level for the action.",
       "Ask a group Owner to give you at least the Reporter role on this project or group.",
       r"You are not allowed to (?:download|push|read)|Access denied — this section"
       r"|insufficient_scope|" + _http("403", "Forbidden")),
    _e("not_found", "NotFound", "Not found (404)",
       "The project or group does not exist at that path, was renamed, or is hidden from you.",
       "Open the path in the GitLab web UI; if it moved, re-run the scan to pick up the new path.",
       r"Repository not found|repository '[^']*' not found"
       r"|project you were looking for could not be found|does not appear to be a git repository"
       r"|Not found \(or you don't have access\)|" + _http("404", "Not Found")),
    _e("conflict", "Server", "Conflict (409)",
       "GitLab refused the change because it conflicts with the current state on the server.",
       "Refresh with a re-scan and retry; the resource may already exist or be changing.",
       _http("409", "Conflict")),
    _e("unprocessable", "Server", "Request rejected (422)",
       "GitLab understood the request but rejected its contents as invalid.",
       "Re-run with --debug and check the field GitLab named in the 422 response.",
       _http("422", "Unprocessable")),
    _e("rate_limited", "Server", "Rate limited (429)",
       "GitLab is throttling this token because too many requests were sent in a short time.",
       "Wait a minute, then retry with fewer parallel jobs.",
       r"Rate limited|Rate limit exceeded|Too Many Requests|" + _http("429", "Too Many")),
    _e("server_error", "Server", "GitLab server error (5xx)",
       "The GitLab server failed while handling the request; nothing is wrong on your side.",
       "Retry in a few minutes; if it persists, ask the GitLab admin to check the server logs.",
       r"Internal Server Error|Bad Gateway|Service Unavailable|Gateway Time-?out"
       r"|GitLab is not responding|" + _http("50[0234]", "Internal|Bad Gateway|Service|Gateway")),

    # ── Config: after auth so "HTTP 401 ... GITLAB_TOKEN" stays an auth error ─
    _e("token_missing", "Config", "Token is not set",
       "No GitLab token is configured for this instance, so the API cannot be called.",
       "Add a token with Instances → New token (or set GITLAB_TOKEN in the .env file).",
       r"\bGITLAB_TOKEN\w*|No token is set"),
    _e("config_missing", "Config", "Master folder is not set",
       "The folder where repositories are cloned (MASTER_DIR) is not configured.",
       "Set MASTER_DIR=/path/to/folder in the .env file, then restart gitlab-cli.",
       r"\bMASTER_DIR\b"),
    _e("api_truncated", "Server", "Incomplete list from GitLab",
       "GitLab returned fewer records than its X-Total header promised, so the list is partial.",
       "Re-run the scan; if the shortfall repeats, run with --debug and compare the page counts.",
       r"\btruncated: the server reports|the result is truncated|\bX-Total\b"
       r"|max_pages ceiling|NOT the full set|\bis incomplete\b"),

    # ── Git working-tree states ──────────────────────────────────────────────
    _e("empty_repo", "Git", "Repository is empty",
       "The project exists on GitLab but has no commits yet, so there is nothing to check out.",
       "Nothing to sync until someone pushes the first commit; the clone fills in after that.",
       r"You appear to have cloned an empty repository"),
    _e("dirty_tree", "Git", "Local changes block the pull",
       "Uncommitted local changes would be overwritten by the incoming commits.",
       "Run `git status` in the repo, then commit or `git stash` the changes and pull again.",
       r"Your local changes to the following files would be overwritten"
       r"|Please commit your changes or stash them|untracked working tree files would be overwritten"
       r"|You have unstaged changes|Your index contains uncommitted changes"),
    _e("diverged", "Git", "Branch has diverged",
       "Local and remote both have new commits, so a fast-forward pull is not possible.",
       "Run `git log --oneline HEAD...@{u}` in the repo, then rebase or merge by hand.",
       r"Not possible to fast-forward|have diverged|non-fast-forward"
       r"|refusing to merge unrelated histories"),
    _e("detached", "Git", "Repo is on a detached HEAD",
       "The repo is not on a branch, so there is nothing to pull into.",
       "Run `git switch <branch>` in the repo (e.g. `git switch main`), then pull again.",
       r"You are not currently on a branch|\bdetached HEAD\b|\bHEAD detached\b"),
    _e("no_upstream", "Git", "Branch has no upstream",
       "The current branch is not linked to a remote branch, so git does not know what to pull.",
       "If the remote lacks the branch, run `git push -u origin <branch>`; else `--set-upstream-to`.",
       r"no tracking information|no upstream configured|has no upstream branch"),
    _e("remote_ref_missing", "Git", "Branch is gone on the remote",
       "The branch this repo tracks no longer exists on GitLab.",
       "Run `git fetch --prune` and `git switch` to a branch that still exists.",
       r"couldn't find remote ref|no such ref was fetched"),
    _e("not_a_repo", "Local", "Folder is not a git repo",
       "The folder exists but has no .git directory, so git cannot work in it.",
       "Move the folder aside, then clone the project again from the group screen.",
       r"not a git repository|not a git repo\b"),
    _e("dest_exists", "Local", "Target folder already exists",
       "The clone target folder already exists and is not empty, so git refused to clone into it.",
       "Move or delete the existing folder, then run the clone again.",
       r"already exists and is not an empty directory"),

    # ── Local machine ────────────────────────────────────────────────────────
    _e("lock_file", "Local", "Git lock file is in the way",
       "A .lock file left by a crashed or still-running git command blocks this one.",
       "Make sure no git is running, then delete the named .lock file (often .git/index.lock).",
       r"\.lock'?:?\s*File exists|index\.lock|Unable to create '[^']*\.lock'|cannot lock ref"),
    _e("disk_full", "Disk", "Disk is full",
       "The disk ran out of space while writing repository data.",
       "Free space (check with `df -h`), then retry the action.",
       r"No space left on device|\bENOSPC\b|Disk quota exceeded"),
    _e("filename_too_long", "Local", "File name too long",
       "A file in the repo has a path longer than this filesystem allows.",
       "Clone to a shorter base path, or on Windows run `git config --global core.longpaths true`.",
       r"File ?name too long"),
    _e("permission_denied", "Local", "Local permission denied",
       "Your user cannot write to the target folder or one of its files.",
       "Run `ls -ld <folder>` and fix ownership with `sudo chown -R $USER <folder>`.",
       r"Permission denied(?! \(publickey)|\bPermissionError\b|Operation not permitted"
       r"|Read-only file system"),
)

_UNKNOWN = ErrorInfo(
    code="unknown",
    type="Unknown",
    title="Unrecognised error",
    cause="No error text was captured.",
    fix="Re-run with --debug; the full text is in ~/.gitlab-cli/errors.log.",
)
_BY_CODE = {e.info.code: e.info for e in CATALOG}
_BY_CODE[_UNKNOWN.code] = _UNKNOWN

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
# git/LFS put the real cause on these lines; progress chatter comes before them.
_CAUSE_LINE = re.compile(r"^(?:fatal:|error:|Error |remote: ERROR|LFS:|Smudge error)", re.I)


def summarize(text: str, limit: int = 160) -> str:
    """The most informative single line of `text`, at most `limit` chars.

    Prefers the LAST fatal:/error:/LFS: line (git prints the cause last), else
    the last non-empty line. ANSI colour codes are stripped.
    """
    if not text:
        return ""
    lines = [" ".join(_ANSI.sub("", ln).split()) for ln in str(text).splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    causes = [ln for ln in lines if _CAUSE_LINE.match(ln)]
    best = causes[-1] if causes else lines[-1]
    return _trim(best, limit)


def _trim(s: str, limit: int) -> str:
    if limit <= 0:
        return ""
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def explain(message: str) -> ErrorInfo:
    """The catalog entry for `message`; never None (falls back to code 'unknown')."""
    text = _ANSI.sub("", str(message)) if message else ""
    if text:
        for entry in CATALOG:
            if entry.pattern.search(text):
                return entry.info
    cause = summarize(text, 110)
    return replace(_UNKNOWN, cause=cause) if cause else _UNKNOWN


def one_line(message: str, limit: int = 160) -> str:
    """'Title — cause → fix', trimmed to `limit` chars."""
    i = explain(message)
    return _trim(f"{i.title} — {i.cause} → {i.fix}", limit)


def by_code(code: str) -> ErrorInfo | None:
    """The catalog entry with this code, or None ('unknown' is the generic fallback)."""
    return _BY_CODE.get(code)
