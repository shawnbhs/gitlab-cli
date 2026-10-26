# git.py — Git operation helpers

import base64
import contextlib
import subprocess
import os
import re
import shutil
import stat
from printer import print_ok, print_warn, print_error
from errors import log_error, redact

# Resolve git's absolute path once.
# Without this, `git` is looked up on PATH — so a planted `git` anywhere on
# the user's PATH (inside a cloned repo's directory, for example) would run
# instead. An absolute path closes that off.
GIT_BIN = shutil.which("git") or "git"

# No git command may exceed this (VPN drop, or an auth prompt).
GIT_TIMEOUT = 120

# ─── Permissions ──────────────────────────────────────────────────────────────
#
# Git creates `.git/config` with 0666 & ~umask, which on a default 0022 umask
# is 0644. That file is where git persists a remote URL, and a remote URL is
# where git persists a credential when one is ever supplied. An earlier wave
# of this project found a live admin token sitting in 120 such files.
#
# The token is gone and the trees are verified clean, so 0644 is not an active
# leak. It is the *condition* that made the leak durable, and it is fixed here
# at the point of creation rather than by a later sweep, because a clone of a
# multi-gigabyte repository can run for minutes and git writes the remote URL
# into .git/config at the very start of it. A post-clone chmod leaves that
# window open; a umask on the git process itself does not.
#
# CLONE_UMASK is applied to the `git clone` child process only. Set it to None
# to disable (the post-clone chmod still runs).
CLONE_UMASK = 0o077

# Files inside a .git directory that can carry a remote URL — and therefore a
# credential, if one was ever supplied — or otherwise describe the repository's
# private state. These are tightened to 0600.
#
#   config / config.worktree  remote.origin.url lives here; this is the file
#                             that leaked.
#   logs/**                   the reflog. `logs/HEAD` begins with a literal
#                             "clone: from <url>" line, so it carries the same
#                             URL the config does.
#   FETCH_HEAD                records "branch 'x' of <url>" after every fetch.
#   ORIG_HEAD, shallow        private repository state, no URL.
#   packed-refs               ref names only; tightened for consistency.
#
# Deliberately NOT touched: objects/ (content-addressed repository data, huge,
# no credential surface) and hooks/ (changing an executable bit on a hook is a
# behaviour change, not a permissions fix — see the report).
_SENSITIVE_GIT_FILES = (
    "config",
    "config.worktree",
    "packed-refs",
    "FETCH_HEAD",
    "ORIG_HEAD",
    "shallow",
)

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
# An incomplete-but-usable clone: the working tree is real, the history is
# truncated. NEVER counted as a complete clone — see clone_repo_detailed().
S_SHALLOW    = "shallow"
# A valid, complete clone whose Git LFS file CONTENTS are missing: the working
# tree holds LFS pointer stubs instead of the real files. The repository is
# kept — it is not debris — but it is not reported as "ok" either.
S_LFS        = "lfs_incomplete"
# The server-side project exists but has no branches at all (nobody has
# pushed yet) and the local clone has no commits of its own to lose. An
# expected state, not an error: nothing to sync until someone pushes.
S_EMPTY      = "empty_remote"
# Fetch says the repository is gone AND the clone's path or remote URL path
# carries GitLab's delayed-deletion suffix `-deletion_scheduled-<id>`: the
# server has purged it. Expected, not an error; this clone may be the last copy.
S_PURGED     = "purged"

# Checkout must never die on an LFS object the server cannot hand out (D1:
# `smudge filter lfs failed` turned a complete clone into "Clone failed" and
# the project stayed missing forever). With this set, git-lfs leaves pointer
# files during clone/merge; LFS content is fetched afterwards by `lfs pull`,
# whose failure is reported as S_LFS instead of failing the whole operation.
_LFS_SKIP_SMUDGE = {"GIT_LFS_SKIP_SMUDGE": "1"}

LFS_NOT_INSTALLED = "git-lfs is not installed — LFS files are pointer stubs"

# HTTP status codes are matched ONLY in HTTP context and on word boundaries:
# a bare "401"/"403" also occurs inside hex LFS oids, commit hashes and paths
# (`group/project-401`), and must not turn an unrelated failure into "auth".
_HTTP_CTX = (r"(?:\bHTTP(?:/[\d.]+)?\s*|\breturned error:\s*|"
             r"\bstatus(?: code)?:?\s*|\berror:\s*|\bcode\s*)")

_AUTH_PAT = re.compile(
    r"(" + _HTTP_CTX + r"(?:401|403)\b|Authentication failed|"
    r"Access denied|HTTP Basic|"
    r"could not read Username|Permission denied \(publickey\))",
    re.I,
)

# A Git LFS failure — smudge during checkout, `lfs pull`, or git-lfs missing.
_LFS_PAT = re.compile(
    r"(smudge filter lfs failed|Smudge error|\bLFS:|gitlab-lfs/objects|"
    r"/info/lfs\b|Error downloading object|batch response|"
    r"'lfs' is not a git command|git-lfs[^\n]*(?:command )?not found)",
    re.I,
)

# The repository was deleted or renamed on the server.
_GONE_PAT = re.compile(
    r"(repository .* not found|project you were looking for could not be found|"
    r"Repository not found|does not appear to be a git repository)",
    re.I,
)


def _umask_wrap(args: list[str], umask: int) -> list[str]:
    """Re-express `args` so the child runs under `umask`.

    Why a /bin/sh wrapper rather than os.umask() in this process: umask is
    per-process state, and mutating it here would change the mode of every
    file any other part of the program writes for as long as the git call
    runs. It is also not thread-safe, and `preexec_fn` carries its own
    documented fork-safety caveat. The shell sets the mask in the child and
    then `exec`s git, so nothing else is affected and no extra process
    survives.

    The git argv is passed positionally, so no argument is ever
    interpolated into the script text.
    """
    return ["/bin/sh", "-c", 'umask "$1"; shift; exec "$@"',
            "sh", format(umask, "04o"), *args]


# The umask in force for git children started inside a clone_umask() block.
# It is module state rather than a _run() keyword on purpose: several test
# modules replace _run with a stub of the original signature, and an extra
# keyword at the call site would break every one of them. Wrapping below the
# call site also keeps the argv those tests inspect completely unchanged.
_ACTIVE_UMASK = None


@contextlib.contextmanager
def clone_umask(mask: int = None):
    """Run git children under a tighter file-creation mask for this block."""
    global _ACTIVE_UMASK
    prev = _ACTIVE_UMASK
    _ACTIVE_UMASK = CLONE_UMASK if mask is None else mask
    try:
        yield
    finally:
        _ACTIVE_UMASK = prev


def _run(args: list[str], cwd: str = None, timeout: int = GIT_TIMEOUT,
         env_extra: dict = None, umask: int = None):
    """Run git; always returns (rc, stdout, stderr) and never hangs.

    stdout/stderr are redacted: a remote URL can carry a token, and this
    text is both printed and written to ~/.gitlab-cli/errors.log.

    `env_extra` adds environment variables for this one git process only.
    It is how a credential reaches git without ever touching argv or a
    file — see _auth_env().

    `umask` (or an enclosing clone_umask() block) runs git under a tighter
    file-creation mask, so the files git creates are born private instead
    of being chmod'ed private afterwards. Ignored on platforms without
    /bin/sh (Windows); the post-clone chmod still covers those.
    """
    bad = unsafe_git_arg(args)
    if bad is not None:
        # Refused rather than raised: every caller reads (rc, out, err), and a
        # refusal must look like a failed git call, not an unhandled crash in
        # the middle of a 700-repository walk.
        detail = f"refused an unsafe git argument: {redact(bad)[:80]}"
        log_error("git:unsafe-argv", detail)
        return 1, "", detail

    mask = umask if umask is not None else _ACTIVE_UMASK
    if mask is not None and os.name == "posix" and os.path.exists("/bin/sh"):
        args = _umask_wrap(args, mask)
    env = dict(os.environ)
    # Suppress the interactive username/password prompt, which would hang us.
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_ASKPASS", "echo")
    if env_extra:
        env.update(env_extra)
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


def _summary(text: str) -> str:
    """The most informative single line of git's stderr (one line, bounded).

    Delegates to error_catalog.summarize(). Imported lazily so git.py stays
    importable even if error_catalog is absent; the fallback is the LAST
    non-empty line — git's cause is at the end, never at the head.
    """
    if not text:
        return ""
    try:
        import error_catalog
        return error_catalog.summarize(text)
    except Exception:
        lines = [" ".join(ln.split()) for ln in str(text).splitlines()]
        lines = [ln for ln in lines if ln]
        if not lines:
            return ""
        last = lines[-1]
        return last if len(last) <= 160 else last[:159] + "\u2026"


def is_auth_error(text: str) -> bool:
    return bool(text and _AUTH_PAT.search(text))


def is_gone(text: str) -> bool:
    """The repository is no longer on the server — deleted, renamed, or access revoked."""
    return bool(text and _GONE_PAT.search(text))


def is_lfs_error(text: str) -> bool:
    """A Git LFS failure (missing object, smudge, LFS auth, git-lfs absent)."""
    return bool(text and _LFS_PAT.search(text))


def classify(text: str) -> str:
    """Map a git error to the matching status.

    LFS is checked first: an LFS object failure (even an LFS 401/403) means
    the repository itself is fine and only file contents are missing.
    """
    if is_lfs_error(text):
        return S_LFS
    if is_auth_error(text):
        return S_AUTH
    if is_gone(text):
        return S_GONE
    return S_ERROR


def inject_token(http_url: str, token: str) -> str:
    """Put the token into the URL, for http:// as well as https:// (internal hosts use http).

    DANGEROUS — do not hand the result to any git command that stores a URL
    (`clone`, `remote add`, `remote set-url`, `submodule add`). Git writes
    such a URL verbatim into .git/config, which is how a live admin token
    ended up world-readable in every cloned repository. The clone path no longer
    uses this function; it is kept only for callers that build a URL for a
    one-shot, non-persisting operation. Prefer _auth_env().
    """
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
    # The message reaches print_error() and the error log, and the URL comes
    # from the API — it may itself carry `user:secret@`. Redact before quoting.
    shown = redact(u)[:60]
    if u.startswith("-"):
        raise UnsafeURL(f"a URL cannot start with '-': {shown}")
    if not u.startswith(_ALLOWED_SCHEMES):
        raise UnsafeURL(f"only http/https are accepted, not: {shown}")
    return u


# ─── The same class, one step further in: names and refs ──────────────────────
#
# W10. check_url() closed argument injection for the URL git is given at CLONE
# time. It is not the only string of ours that reaches a git argv, and the URL
# is not the only one an attacker controls.
#
# A remote NAME is attacker-controlled too. Git validates a remote name when it
# is created (`git remote add`) but never when it is READ: `git remote` prints
# whatever the `[remote "..."]` section headers in .git/config say, including
# a name that is a git option. This tool walks repositories it did not create —
# foreign clones, clones from another instance, anything a colleague handed over
# or a CI job dropped on disk — so "someone else wrote that .git/config" is the
# normal case, not the exotic one.
#
# From a name, the sync path builds a ref (`f"{remote}/{branch}"`) and hands it
# to rev-parse, rev-list, log, merge and `branch --set-upstream-to=`. Verified
# on git 2.43: a remote named `--output=/tmp/x` makes
#
#     git -C <repo> log -1 --format=%ct --output=/tmp/x/master
#
# exit 0 and create /tmp/x — an arbitrary file write from a config file we do
# not own. `--upload-pack=<cmd>` in the same position is the command-execution
# form of the identical bug. The exit code is useless as a signal: git happily
# returns 0 after doing the attacker's work.
#
# So the rule is applied to every argument class git parses, not only URLs.

class UnsafeRemote(ValueError):
    """A remote name that git would parse as an option (or worse)."""


class UnsafeRef(ValueError):
    """A ref/branch name that git would parse as an option."""


# Git's own remote-name grammar in practice: a word of letters, digits and
# `.`, `_`, `-`, `/`, never starting with `-` or `.`. Deliberately an
# ALLOW-list — a block-list of option names is a list of the options someone
# has thought of so far.
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]*$")

# Never legal in any argument we build, in any position.
_ARG_FORBIDDEN = ("\n", "\r", "\x00")


def is_safe_remote_name(name: str) -> bool:
    """True when `name` can be handed to git as a remote without risk."""
    try:
        check_remote_name(name)
        return True
    except UnsafeRemote:
        return False


def check_remote_name(name: str) -> str:
    """Validate a remote name read from a repository we did not write."""
    n = name or ""
    if not n:
        raise UnsafeRemote("empty remote name")
    if any(c in n for c in _ARG_FORBIDDEN):
        raise UnsafeRemote("a remote name cannot contain a control character")
    if n.startswith("-"):
        raise UnsafeRemote(
            f"a remote name cannot start with '-': {redact(n)[:60]}")
    if not _REMOTE_NAME_RE.match(n):
        raise UnsafeRemote(
            f"refusing an implausible remote name: {redact(n)[:60]}")
    return n


def check_ref(ref: str) -> str:
    """Validate a ref/branch/revision before it becomes a git argument.

    Looser than check_remote_name on purpose — this tool legitimately builds
    `origin/main`, `main...origin/main`, `main^{commit}` and `@{u}` — but the
    option rule is the same, and it is the option rule that is the bug.
    """
    r = ref or ""
    if not r:
        raise UnsafeRef("empty ref")
    if any(c in r for c in _ARG_FORBIDDEN):
        raise UnsafeRef("a ref cannot contain a control character")
    if r.lstrip().startswith("-"):
        raise UnsafeRef(f"a ref cannot start with '-': {redact(r)[:60]}")
    return r


# Options that make git run a command or write a file of the caller's
# choosing. None of them is ever built by this tool, so any appearance of one
# in an argv means a value flowed in from a repository, an API response or a
# config file. This is the backstop under the per-call-site validation above:
# it also covers the call sites in remotes.py, clone.py and coverage.py, and
# it covers the option nobody has thought of yet only insofar as it is listed —
# which is why it is a BACKSTOP and not the fix.
_DANGEROUS_GIT_OPTS = (
    "--upload-pack", "--receive-pack", "--exec", "--output",
    "--config", "--config-env", "--templates", "--separate-git-dir",
    "--upload-archive", "--remote-tab",
)
# Short forms that take an attached value: `-u<cmd>`, `-o<file>`, `-c k=v`.
_DANGEROUS_GIT_SHORT = ("-u", "-o", "-c")


def _is_git_argv(args: list) -> bool:
    """True when args[0] is the git binary.

    _run() is also used for the /bin/sh umask wrapper and (in tests) for
    non-git programs, whose options are not git's options — `sh -c` is how
    the wrapper works, not an injection.
    """
    if not args or not isinstance(args[0], str):
        return False
    prog = os.path.basename(args[0])
    return prog == "git" or prog == "git.exe" or args[0] == GIT_BIN


def unsafe_git_arg(args: list) -> str | None:
    """The first argument of `args` git would treat as a dangerous option.

    Returns None when the argv is clean, and for an argv that does not start
    with git at all. Positional values are exempt after a literal `--`, which
    is exactly what `--` is for.
    """
    if not _is_git_argv(args):
        return None
    for i, a in enumerate(args):
        if i == 0 or not isinstance(a, str):
            continue
        if a == "--":
            break
        if not a.startswith("-"):
            continue
        head = a.split("=", 1)[0]
        if head in _DANGEROUS_GIT_OPTS:
            return a
        if a in _DANGEROUS_GIT_SHORT or (
                len(a) > 2 and a[:2] in _DANGEROUS_GIT_SHORT and a[1] != "-"):
            return a
    return None


# ─── Credentials that git does not persist ────────────────────────────

# No credential field may contain these: the git-credential protocol is
# newline-delimited and an HTTP header cannot contain CR/LF either.
_CRED_FORBIDDEN = ("\n", "\r", "\x00")


def strip_userinfo(url: str) -> str:
    """Remove any `user:pass@` from a URL's authority.

    The URL arrives from the GitLab API, so anyone who can create a project
    on the instance controls it. Without this, a project whose clone URL
    already embeds a credential would have that credential written into
    .git/config by us.
    """
    try:
        scheme, rest = url.split("://", 1)
    except ValueError:
        return url
    authority, _, path = rest.partition("/")
    if "@" in authority:
        authority = authority.rsplit("@", 1)[-1]
    return f"{scheme}://{authority}" + (f"/{path}" if path else "")


def base_url(url: str) -> str:
    """`scheme://host[:port]/` — the key git matches per-URL http config on."""
    try:
        scheme, rest = strip_userinfo(url).split("://", 1)
    except ValueError:
        return ""
    host = rest.split("/", 1)[0]
    return f"{scheme}://{host}/" if host else ""


def _auth_env(url: str, token: str) -> dict:
    """Environment-only git config that carries the token for ONE git process.

    GIT_CONFIG_COUNT / GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n (git >= 2.31)
    injects config into the git process being spawned. Three properties
    matter here:

      * it is **not** copied into the new repository's .git/config — unlike
        `git clone -c <key>=<value>`, which git deliberately persists;
      * it lives in the environment, not in argv. /proc/<pid>/cmdline is
        world-readable, /proc/<pid>/environ is owner-only — so this is
        strictly less exposed than the old tokenised URL argument;
      * the header is scoped to one exact base URL, so a redirect to a
        different host does not carry the credential along.

    The value is an HTTP Basic header, which is what GitLab expects for
    `oauth2:<token>` over http(s).
    """
    base = base_url(url)
    if not token or not base:
        return {}
    if any(c in token for c in _CRED_FORBIDDEN):
        return {}
    basic = base64.b64encode(f"oauth2:{token}".encode()).decode()
    try:
        start = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        start = 0
    return {
        f"GIT_CONFIG_KEY_{start}":   f"http.{base}.extraHeader",
        f"GIT_CONFIG_VALUE_{start}": f"Authorization: Basic {basic}",
        "GIT_CONFIG_COUNT":          str(start + 1),
    }


def save_credential(url: str, token: str, username: str = "oauth2") -> bool:
    """Hand the token to git's credential helper, so later fetch/pull works.

    Without this, a clone whose remote URL has no credential in it could not
    authenticate afterwards. With it, the token lives in ONE place the user
    already controls (`~/.git-credentials`, mode 0600) instead of in every
    clone's world-readable .git/config.

    Nothing from this function is printed or logged.
    """
    if not token:
        return False
    clean = strip_userinfo(url)
    try:
        scheme, rest = clean.split("://", 1)
    except ValueError:
        return False
    host = rest.split("/", 1)[0]
    if scheme not in ("http", "https") or not host:
        return False
    # git-credential is a newline-delimited key=value protocol: a newline in
    # any field lets an attacker inject extra fields (e.g. another host).
    for field in (scheme, host, username, token):
        if any(c in field for c in _CRED_FORBIDDEN):
            return False

    payload = (f"protocol={scheme}\nhost={host}\n"
               f"username={username}\npassword={token}\n\n")
    try:
        r = subprocess.run([GIT_BIN, "credential", "approve"],
                           input=payload, text=True,
                           capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def credential_known(url: str) -> bool:
    """True if a helper can already answer for this host.

    `git credential fill` prints the password on stdout, so its output is
    inspected in-process for the presence of the field and never returned,
    printed or logged.
    """
    clean = strip_userinfo(url)
    try:
        scheme, rest = clean.split("://", 1)
    except ValueError:
        return False
    host = rest.split("/", 1)[0]
    if scheme not in ("http", "https") or not host:
        return False
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "echo"
    try:
        r = subprocess.run([GIT_BIN, "credential", "fill"],
                           input=f"protocol={scheme}\nhost={host}\n\n",
                           text=True, capture_output=True, timeout=15, env=env)
    except Exception:
        return False
    if r.returncode != 0:
        return False
    for line in (r.stdout or "").splitlines():
        if line.startswith("password=") and len(line) > len("password="):
            return True
    return False


def resolve_git_dir(repo_path: str) -> str | None:
    """Return the real .git directory for `repo_path`, or None.

    `.git` is NOT always a directory. In a linked worktree and in a
    submodule it is a FILE containing `gitdir: <path>` — and that path is
    frequently relative. A permissions routine that only does
    `os.path.join(repo, ".git", "config")` silently does nothing for every
    such repository while reporting that it succeeded, which is the exact
    failure mode this project exists to stamp out.
    """
    dot_git = os.path.join(repo_path, ".git")
    if os.path.isdir(dot_git):
        return dot_git
    if os.path.isfile(dot_git):
        try:
            with open(dot_git, "r", errors="replace") as fh:
                head = fh.read(4096)
        except OSError:
            return None
        for line in head.splitlines():
            line = line.strip()
            if line.startswith("gitdir:"):
                target = line.split(":", 1)[1].strip()
                if not target:
                    return None
                if not os.path.isabs(target):
                    target = os.path.normpath(
                        os.path.join(os.path.dirname(os.path.abspath(dot_git)),
                                     target))
                return target if os.path.isdir(target) else None
    return None


def _chmod_if_needed(path: str, mode: int) -> bool:
    """chmod only when the mode actually differs. True = changed."""
    try:
        cur = stat.S_IMODE(os.lstat(path).st_mode)
    except OSError:
        return False
    if cur == mode:
        return False
    try:
        os.chmod(path, mode)
    except OSError:
        return False
    return True


def harden_git_dir(git_dir: str) -> dict:
    """Tighten one .git directory. Returns a per-file summary.

    Scope and justification, file by file, is in _SENSITIVE_GIT_FILES.
    Everything is 0600 for files and 0700 for the directories that hold
    them — a 0600 file inside a 0755 directory still leaks its name, and
    for a reflog the *name* is a branch name.

    Never touches objects/ or hooks/. See the report for why.
    """
    out = {"git_dir": git_dir, "changed": [], "checked": 0, "errors": []}
    if not git_dir or not os.path.isdir(git_dir):
        out["errors"].append("not a .git directory")
        return out
    # Refuse an arbitrary directory. Being handed the wrong path and
    # cheerfully chmod'ing it 0700 would be a real hazard, and returning
    # "no errors" for it would be a lie. A git dir — including a linked
    # worktree's or a submodule's — always has at least one of these.
    if not any(os.path.exists(os.path.join(git_dir, n))
               for n in ("HEAD", "config", "objects", "commondir", "gitdir")):
        out["errors"].append("does not look like a git directory")
        return out

    # The .git directory itself.
    out["checked"] += 1
    if _chmod_if_needed(git_dir, 0o700):
        out["changed"].append(git_dir)

    for name in _SENSITIVE_GIT_FILES:
        p = os.path.join(git_dir, name)
        if os.path.isfile(p) and not os.path.islink(p):
            out["checked"] += 1
            if _chmod_if_needed(p, 0o600):
                out["changed"].append(p)

    # The reflog: logs/HEAD's first line is literally "clone: from <url>".
    logs = os.path.join(git_dir, "logs")
    if os.path.isdir(logs):
        for dirpath, _dirnames, filenames in os.walk(logs, followlinks=False):
            out["checked"] += 1
            if _chmod_if_needed(dirpath, 0o700):
                out["changed"].append(dirpath)
            for f in filenames:
                p = os.path.join(dirpath, f)
                if os.path.islink(p):
                    continue
                out["checked"] += 1
                if _chmod_if_needed(p, 0o600):
                    out["changed"].append(p)
    return out


def _harden_repo_config(target_path: str) -> bool:
    """Tighten a freshly created repository's private files.

    Defence in depth, NOT the primary control: the clone itself now runs
    under CLONE_UMASK so .git/config is created 0600 rather than created
    0644 and narrowed a moment later. This still runs because the umask
    wrapper is POSIX-only and because a later `remote set-url` (see
    _finish_clone) rewrites config through a fresh temp file.
    """
    git_dir = resolve_git_dir(target_path)
    if not git_dir:
        return False
    return not harden_git_dir(git_dir)["errors"]


# ─── Permissions sweep for repositories that already exist ────────────────────

def iter_repos(root: str, follow_symlinks: bool = False):
    """Yield (work_tree, git_dir) for EVERY repository under `root`.

    Three properties, each of which a naive walk gets wrong:

    1. **Exhaustive.** The common idiom prunes — on finding `.git` it stops
       descending, assuming no repository lives inside another's work tree.
       On a real tree that assumption was false and cost dozens of
       repositories. A permissions sweep that misses repositories is worse
       than no sweep, because it reports a success it did not achieve.
    2. **`.git` may be a FILE.** resolve_git_dir() handles the
       `gitdir: <path>` pointer used by linked worktrees and submodules.
    3. **Cycle-safe.** Directories are tracked by (st_dev, st_ino), so a
       symlink loop — or a bind mount that re-enters the tree — terminates
       instead of recursing forever. Symlinked directories are not followed
       at all by default; following them can walk straight out of `root`.

    It does descend into a `.git` *work tree's* siblings but never into the
    .git directory itself: object directories are enormous and contain no
    nested repositories.
    """
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return
    seen: set[tuple[int, int]] = set()
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            st = os.stat(d)
        except OSError:
            continue
        key = (st.st_dev, st.st_ino)
        if key in seen:
            continue
        seen.add(key)
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            try:
                is_link = e.is_symlink()
                if e.name == ".git":
                    g = resolve_git_dir(d)
                    if g:
                        yield d, g
                    continue                 # never descend into .git
                if not e.is_dir(follow_symlinks=follow_symlinks):
                    continue
                if is_link and not follow_symlinks:
                    continue
                stack.append(e.path)
            except OSError:
                continue


def sweep_permissions(roots, dry_run: bool = False) -> dict:
    """Tighten .git permissions across every repository under `roots`.

    WHERE THIS BELONGS. It lives here as a library callable rather than as
    a hidden step of clone/pull for three reasons: it is a *maintenance*
    action over pre-existing state, not part of any single repository
    operation; it walks tens of thousands of directories and must not be
    paid on every pull; and it must be runnable, and auditable, against a
    tree the CLI did not create — such as the stray older tree at
    ~/MasterGroups. New clones do not need it at all, because the clone
    path is now correct by construction.

    WIRING (not done here — gitlab_cli.py and setup.py are owned by other
    tasks): expose as `gitlab-cli harden-perms [--dry-run]`, dispatched in
    gitlab_cli.py's top-level command table beside the existing `doctor`
    branch, and report the `too_open_before` count as a `doctor` check so
    drift is noticed without the user having to remember to sweep.

    Returns counts only — never a file's contents.
    """
    if isinstance(roots, (str, bytes, os.PathLike)):
        roots = [roots]
    res = {
        "roots": [], "repos": 0, "files_changed": 0, "repos_changed": 0,
        "checked": 0, "errors": [], "dry_run": bool(dry_run),
        "config_modes_before": {}, "config_modes_after": {},
    }
    for root in roots:
        rroot = {"root": str(root), "repos": 0, "files_changed": 0,
                 "repos_changed": 0, "missing": not os.path.isdir(str(root))}
        for _work_tree, git_dir in iter_repos(str(root)):
            rroot["repos"] += 1
            res["repos"] += 1
            cfg = os.path.join(git_dir, "config")
            try:
                m = oct(stat.S_IMODE(os.lstat(cfg).st_mode))
                res["config_modes_before"][m] = \
                    res["config_modes_before"].get(m, 0) + 1
            except OSError:
                pass
            if dry_run:
                # Count what WOULD change, without changing it.
                would = 0
                for name in ("",) + _SENSITIVE_GIT_FILES:
                    p = os.path.join(git_dir, name) if name else git_dir
                    want = 0o700 if not name else 0o600
                    try:
                        if stat.S_IMODE(os.lstat(p).st_mode) != want:
                            would += 1
                    except OSError:
                        continue
                if would:
                    rroot["repos_changed"] += 1
                rroot["files_changed"] += would
            else:
                r = harden_git_dir(git_dir)
                res["checked"] += r["checked"]
                res["errors"] += r["errors"]
                if r["changed"]:
                    rroot["repos_changed"] += 1
                rroot["files_changed"] += len(r["changed"])
            try:
                m = oct(stat.S_IMODE(os.lstat(cfg).st_mode))
                res["config_modes_after"][m] = \
                    res["config_modes_after"].get(m, 0) + 1
            except OSError:
                pass
        res["files_changed"] += rroot["files_changed"]
        res["repos_changed"] += rroot["repos_changed"]
        res["roots"].append(rroot)
    return res


def remote_has_userinfo(target_path: str) -> bool:
    """Belt-and-braces check that we did not persist a credential."""
    rc, out, _ = _git(target_path, "config", "--get", "remote.origin.url")
    if rc != 0 or not out:
        return False
    try:
        rest = out.split("://", 1)[1]
    except IndexError:
        return False
    return "@" in rest.split("/", 1)[0]


# A very large repository can be unfetchable in one shot while every other
# repository on the same host is fine: the transfer dies partway through the
# pack with a consistent symptom. This is a *server/proxy response cap or
# idle timeout*, not a client defect — it survives http.version=HTTP/1.1, a
# 500 MB http.postBuffer and core.compression=0. The remedy is not to retry
# the same full fetch: it is to ask for less per request.
_CAP_PAT = re.compile(
    r"(transfer closed with outstanding read data|"
    r"RPC failed|\bcurl (?:18|56|92)\b|unexpected disconnect|"
    r"early EOF|index-pack failed|fetch-pack: invalid index-pack output|"
    r"the remote end hung up unexpectedly)",
    re.I,
)


def is_transfer_cap(text: str) -> bool:
    """True for the truncated-pack failure class of a too-large fetch.

    Deliberately narrow: auth and 'repository gone' failures must NOT be
    routed into the shallow fallback, or a 401 would quietly produce a
    depth-1 repository and look like partial success.
    """
    if not text:
        return False
    if is_auth_error(text) or is_gone(text) or is_lfs_error(text):
        return False
    return bool(_CAP_PAT.search(text))


def is_shallow(path: str) -> bool:
    """True when this repo's history is truncated.

    `.git/shallow` is the authoritative marker; the rev-parse is the
    fallback for a worktree/`--git-dir` layout where the file is elsewhere.
    """
    if os.path.isfile(os.path.join(path, ".git", "shallow")):
        return True
    rc, out, _ = _git(path, "rev-parse", "--is-shallow-repository")
    return rc == 0 and out.strip().lower() == "true"


def shallow_depth(path: str) -> int:
    """How many commits the truncated history actually holds (0 = unknown)."""
    rc, out, _ = _git(path, "rev-list", "--count", "HEAD")
    return int(out) if rc == 0 and out.strip().isdigit() else 0


def deepen_repo(path: str, token: str = "", step: int = 1,
                max_steps: int = 200, timeout: int = 3600) -> dict:
    """Complete a shallow repository incrementally, `step` commits at a time.

    Why this works when a full fetch does not: each `git fetch --deepen=<n>`
    is a separate, much smaller HTTP response, so no single response reaches
    the cap that kills the one-shot fetch. It is strictly additive — it only
    adds objects and never rewrites or drops history, so it is safe to run
    on a repository holding real data.

    Returns a dict; `complete` is True ONLY when `.git/shallow` is gone.
    A run that stops early returns complete=False with the reason, so a
    caller can never mistake partial progress for a finished repository.
    """
    res = {"path": path, "complete": False, "steps": 0,
           "commits": shallow_depth(path), "detail": ""}

    if not os.path.exists(os.path.join(path, ".git")):
        res["detail"] = "not a git repo"
        return res
    if not is_shallow(path):
        res["complete"] = True
        res["detail"] = "already complete"
        return res

    rc, url, _ = _git(path, "config", "--get", "remote.origin.url")
    env_extra = _auth_env(url, token) if (rc == 0 and url and token) else None

    for _ in range(max_steps):
        before = shallow_depth(path)
        rc, _, err = _run([GIT_BIN, "-C", path, "fetch",
                           f"--deepen={step}", "--quiet", "origin"],
                          timeout=timeout, env_extra=env_extra)
        res["steps"] += 1
        if rc != 0:
            res["detail"] = _summary(err) or f"deepen failed rc={rc}"
            log_error(f"deepen:{path}", err or res["detail"])
            res["commits"] = shallow_depth(path)
            return res
        if not is_shallow(path):
            res["complete"] = True
            res["commits"] = shallow_depth(path)
            res["detail"] = f"history completed in {res['steps']} step(s)"
            return res
        after = shallow_depth(path)
        res["commits"] = after
        if after == before:
            # Deepening stopped adding commits while .git/shallow persists:
            # do NOT call that complete — it is a stall, and reporting it as
            # success is exactly the founding bug of this project.
            res["detail"] = (f"deepening stalled at {after} commit(s) — "
                             f"still shallow")
            return res

    res["detail"] = f"still shallow after {max_steps} step(s)"
    return res


# ─── Clone ───────────────────────────────────────────────────────

def clone_repo(token: str, http_url: str, target_path: str) -> bool:
    """Clone with a CLEAN remote URL. The token never enters .git/config.

    Returns True ONLY for a COMPLETE clone. A repository that could only be
    obtained shallowly returns False — it is usable, but it is not a
    complete clone, and the caller's success counter must not include it.
    Callers that want to know the difference call clone_repo_detailed().
    """
    return clone_repo_detailed(token, http_url, target_path)["status"] == "ok"


def clone_repo_detailed(token: str, http_url: str, target_path: str,
                        allow_shallow: bool = True,
                        shallow_depth_hint: int = 10) -> dict:
    """Clone, with a documented fallback for a repository too large to fetch.

    How authentication works, before and after:

      * during the clone — an `http.<base>.extraHeader` carrying HTTP Basic
        `oauth2:<token>`, supplied through the environment of that single
        git process (see _auth_env). Git does not copy it into the repo.
      * afterwards — the token is handed to git's credential helper first,
        so `git fetch` / `git pull` in the new clone authenticates from the
        helper (`~/.git-credentials`, 0600) instead of from the URL.

    A failed or interrupted clone leaves nothing behind to scrub: whatever
    partial .git/ exists carries the clean URL, because the clean URL is the
    only URL git was ever given.

    LARGE-REPOSITORY FALLBACK
    -------------------------
    A multi-gigabyte repository can die mid-pack (`curl 18 transfer closed
    with outstanding read data`) on every attempt while every other repo on
    the same host clones fine. That is a server- or proxy-side response cap
    or idle timeout, NOT a client defect: it survives HTTP/1.1, a 500 MB
    http.postBuffer and core.compression=0. Retrying the same full fetch
    only burns hours and leaves debris.

    So when — and only when — the failure matches is_transfer_cap(), this
    falls back to `clone --depth=<n>`, which asks the server for a far
    smaller response. The result is a REAL, usable working tree, which is
    what stops the project being "never cloned".

    It is NOT a success. status is "shallow", never "ok"; clone_repo()
    returns False for it; the caller is told in plain words, and the error
    log keeps the row. The full history is then completed out of band with
    deepen_repo(), or by asking the server owner for the fix in the report.

    GIT LFS
    -------
    The clone runs with GIT_LFS_SKIP_SMUDGE=1, so checkout can never die on
    an LFS object. LFS content is then fetched with `git lfs pull`; if that
    fails, or git-lfs is not installed, the clone is KEPT and the status is
    S_LFS ("lfs_incomplete") with a readable one-line detail.

    Returns {"status": "ok"|"shallow"|"lfs_incomplete"|"auth"|"gone"|"error",
             "path", "shallow": bool, "commits": int, "detail": str,
             "lfs": str}   # "lfs" = LFS problem summary, "" when none
    """
    res = {"status": S_ERROR, "path": target_path, "shallow": False,
           "commits": 0, "detail": "", "lfs": ""}
    try:
        safe_url = check_url(http_url)
    except UnsafeURL as e:
        print_error(f"Clone refused: {e}")
        log_error(f"clone:{target_path}", f"unsafe url: {e}")
        res["detail"] = str(e)
        return res

    # The API-supplied URL itself may embed a credential — never persist it.
    safe_url = strip_userinfo(safe_url)
    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)

    # 1) Secure the token BEFORE the clone, so the resulting repo can pull.
    #    Done first on purpose: if it fails we still clone, but we warn.
    stored = save_credential(safe_url, token) if token else False

    # 2) Clone the clean URL. `--` is required: without it, a URL starting
    #    with `-` becomes an option. clone_umask() makes git create
    #    .git/config 0600 from the first byte, closing the window between
    #    "git wrote the remote URL" and "we chmod'ed it".
    with clone_umask():
        rc, _, err = _run([GIT_BIN, "clone", "--quiet", "--", safe_url, target_path],
                          env_extra={**_auth_env(safe_url, token),
                                     **_LFS_SKIP_SMUDGE})

    if rc == 0:
        _finish_clone(safe_url, token, target_path, stored)
        lfs_detail = _complete_lfs(target_path, safe_url, token)
        # A server-side-empty project legitimately clones to 0 refs and is
        # NOT shallow; only .git/shallow makes it incomplete.
        if is_shallow(target_path):
            detail = "clone is shallow — history incomplete"
            if lfs_detail:
                detail += f"; LFS incomplete: {lfs_detail}"
            res.update(status=S_SHALLOW, shallow=True,
                       commits=shallow_depth(target_path),
                       detail=detail, lfs=lfs_detail)
            print_warn("Cloned SHALLOW ✖ — history incomplete, not a full clone")
            log_error(f"clone-shallow:{target_path}", res["detail"])
            return res
        if lfs_detail:
            res.update(status=S_LFS, commits=shallow_depth(target_path),
                       detail=lfs_detail, lfs=lfs_detail)
            print_warn(f"Cloned, but LFS files are incomplete: {lfs_detail}")
            return res
        res.update(status="ok", commits=shallow_depth(target_path))
        print_ok("Cloned ✔")
        return res

    # The cause is at the END of git's stderr (`fatal:` / `LFS:`), so the log
    # gets the whole (already redacted) text and the user gets its most
    # informative line — never the first N characters.
    summary = _summary(err) or f"git clone failed (rc={rc})"
    if is_auth_error(err) and not is_lfs_error(err):
        print_error("Clone failed: token expired or access denied (401/403)")
        log_error(f"clone:{target_path}", err or summary)
        res.update(status=S_AUTH, detail=summary)
        return res

    if allow_shallow and is_transfer_cap(err):
        shallow_res = _clone_shallow_fallback(
            safe_url, token, target_path, shallow_depth_hint, stored)
        if shallow_res is not None:
            log_error(f"clone-shallow:{target_path}",
                      f"full clone hit a transfer cap; shallow fallback used: "
                      f"{err}")
            return shallow_res

    status = classify(err)
    if status == S_LFS and _has_completed_clone(target_path):
        # The repository arrived; only checkout of LFS content failed. Keep
        # it, and give it the same credential/0600/clean-URL treatment as a
        # successful clone.
        _finish_clone(safe_url, token, target_path, stored)
        print_warn(f"Cloned, but LFS files are incomplete: {summary}")
        log_error(f"lfs:{target_path}", err or summary)
        res.update(status=S_LFS, detail=summary, lfs=summary,
                   commits=shallow_depth(target_path))
        return res

    print_error(f"Clone failed: {summary}")
    log_error(f"clone:{target_path}", err or summary)
    res.update(status=S_ERROR if status == S_LFS else status, detail=summary)
    return res


def _finish_clone(safe_url: str, token: str, target_path: str, stored: bool):
    """Post-clone hardening shared by the normal and the shallow path."""
    _harden_repo_config(target_path)
    if remote_has_userinfo(target_path):
        # Should be unreachable; if it ever happens, fix it immediately.
        _git(target_path, "remote", "set-url", "origin", safe_url)
        print_warn("Stripped an unexpected credential from the new remote.")
        _harden_repo_config(target_path)
    if token and not stored and not credential_known(safe_url):
        print_warn("Token not saved to a credential helper — 'git pull' "
                   "inside this clone will need credentials. Fix with: "
                   "git config --global credential.helper store")


# ─── Git LFS ──────────────────────────────────────────────────────────

# (the _run used to probe, result). Keyed on _run so a test that swaps the
# subprocess seam never leaves a stale answer behind for real git.
_LFS_PROBE = [None, None]


def lfs_available() -> bool:
    """True when `git lfs` works. Probed once per process (cached)."""
    if _LFS_PROBE[0] is not _run or _LFS_PROBE[1] is None:
        rc, out, err = _run([GIT_BIN, "lfs", "version"], timeout=15)
        _LFS_PROBE[0] = _run
        _LFS_PROBE[1] = rc == 0 and "git-lfs" in f"{out} {err}".lower()
    return bool(_LFS_PROBE[1])


_LFS_ATTR_RE = re.compile(r"\bfilter\s*=\s*lfs\b")
_ATTR_READ_MAX = 256 * 1024


def _attrs_mention_lfs(target: str) -> bool:
    """True when the root or any tracked .gitattributes routes to the LFS filter.

    Works without git-lfs installed, which is exactly when it matters: the
    tree then silently holds pointer stubs. Only regular files are read
    (never a symlink or FIFO a hostile repository might commit).
    """
    paths = [".gitattributes"]
    rc, out, _ = _run([GIT_BIN, "-C", target, "ls-files", "-z", "--",
                       "*/.gitattributes"], timeout=GIT_TIMEOUT)
    if rc == 0 and out:
        paths += [p for p in out.split("\0") if p]
    root = os.path.realpath(target)
    for rel in paths:
        p = os.path.realpath(os.path.join(target, rel))
        if not (p == root or p.startswith(root + os.sep)):
            continue
        if os.path.islink(os.path.join(target, rel)) or not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                if _LFS_ATTR_RE.search(fh.read(_ATTR_READ_MAX)):
                    return True
        except OSError:
            continue
    return False


def _lfs_used(target: str) -> bool:
    """Does this work tree track any file with Git LFS?"""
    if _attrs_mention_lfs(target):
        return True
    if lfs_available():
        rc, out, _ = _run([GIT_BIN, "-C", target, "lfs", "ls-files"],
                          timeout=GIT_TIMEOUT)
        return rc == 0 and bool(out.strip())
    return False


def _complete_lfs(target: str, url: str = "", token: str = "") -> str:
    """Fetch LFS content for a checkout made with smudging skipped.

    Returns "" when the repository uses no LFS or its content is complete,
    otherwise a ONE-line summary of what is missing (the caller sets S_LFS).
    Never raises, never deletes anything. The full redacted stderr goes to
    the error log; nothing here prints.
    """
    try:
        if not _lfs_used(target):
            return ""
    except Exception as e:                       # pragma: no cover
        log_error(f"lfs:{target}", f"could not inspect LFS usage: {redact(str(e))}")
        return ""
    if not lfs_available():
        log_error(f"lfs:{target}",
                  f"{LFS_NOT_INSTALLED} (git: 'lfs' is not a git command)")
        return LFS_NOT_INSTALLED
    env_extra = _auth_env(url, token) if (url and token) else None
    rc, _, err = _run([GIT_BIN, "-C", target, "lfs", "pull"],
                      timeout=GIT_TIMEOUT, env_extra=env_extra)
    # `lfs pull` writes under .git/lfs and may touch the index; re-tighten.
    _harden_repo_config(target)
    if rc == 0:
        return ""
    summary = _summary(err) or f"git lfs pull failed (rc={rc})"
    log_error(f"lfs:{target}", err or summary)
    return summary


def _clone_shallow_fallback(safe_url: str, token: str, target_path: str,
                            depth: int, stored: bool):
    """`clone --depth=<n>` after a transfer cap. Returns a result or None.

    None means the fallback ALSO failed — the caller then reports the
    original error. There is no path through here that returns "ok": a
    shallow clone is reported as shallow, always.
    """
    # Only clean up our own failed attempt; never touch a pre-existing tree.
    if os.path.exists(target_path):
        if os.path.exists(os.path.join(target_path, ".git", "shallow")):
            # A previous fallback already produced a usable shallow repo.
            return {"status": S_SHALLOW, "path": target_path, "shallow": True,
                    "commits": shallow_depth(target_path),
                    "detail": "existing shallow clone kept — history incomplete",
                    "lfs": ""}
        if _has_completed_clone(target_path):
            return None                       # real data: never clobber it
        try:
            shutil.rmtree(target_path)
        except OSError:
            return None

    print_warn(f"Full clone hit a server/proxy transfer cap — retrying "
               f"shallow (--depth={depth}).")
    with clone_umask():
        rc, _, err = _run([GIT_BIN, "clone", "--quiet", f"--depth={depth}",
                           "--", safe_url, target_path],
                          env_extra={**_auth_env(safe_url, token),
                                     **_LFS_SKIP_SMUDGE})
    if rc != 0:
        return None

    _finish_clone(safe_url, token, target_path, stored)
    lfs_detail = _complete_lfs(target_path, safe_url, token)
    n = shallow_depth(target_path)
    print_warn(f"Cloned SHALLOW ✖ — {n} of an unknown number of commits. "
               f"NOT a complete clone; complete it with deepen_repo() or "
               f"ask the server owner for a bundle.")
    detail = f"shallow clone at depth {depth} — history incomplete"
    if lfs_detail:
        detail += f"; LFS incomplete: {lfs_detail}"
    return {"status": S_SHALLOW, "path": target_path, "shallow": True,
            "commits": n, "detail": detail, "lfs": lfs_detail}


def _has_completed_clone(target_path: str) -> bool:
    """True when this directory holds a finished clone, not clone debris.

    Git writes the `[branch "..."]` stanza only after a clone succeeds, so
    its presence separates a real repository from an aborted transfer.
    """
    rc, out, _ = _git(target_path, "config", "--get-regexp", r"^branch\.")
    return rc == 0 and bool(out.strip())



# ─── Sync (fetch, then work out which remote is ahead) ─────────────────────────

def _remotes(path: str) -> list[str]:
    """Remote names of `path`, minus any that git would parse as an option.

    `git remote` prints the `[remote "..."]` section headers of a config file
    this tool did not write. A name such as `--upload-pack=<cmd>` is refused
    here, once, instead of at each of the five places a ref built from it
    later reaches git.
    """
    rc, out, _ = _git(path, "remote")
    if rc != 0:
        return []
    names, refused = [], []
    for line in out.splitlines():
        name = line.strip()
        if not name:
            continue
        if is_safe_remote_name(name):
            names.append(name)
        else:
            refused.append(name)
    if refused:
        detail = "unsafe remote name(s) ignored: " + ", ".join(
            redact(r)[:40] for r in refused)
        print_warn(f"{path}: {detail}")
        log_error(f"remote-name:{path}", detail)
    return names


def _current_branch(path: str) -> str | None:
    rc, out, _ = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    return out if rc == 0 and out else None      # None = detached HEAD


def _is_dirty(path: str) -> bool:
    rc, out, _ = _git(path, "status", "--porcelain", "--untracked-files=no")
    return rc == 0 and bool(out)


def _ref_exists(path: str, ref: str) -> bool:
    try:
        ref = check_ref(ref)
    except UnsafeRef:
        return False
    rc, _, _ = _git(path, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
    return rc == 0


def _counts(path: str, local_ref: str, remote_ref: str) -> tuple[int, int]:
    """(ahead, behind) — ahead = local is further, behind = remote is further."""
    try:
        local_ref = check_ref(local_ref)
        remote_ref = check_ref(remote_ref)
    except UnsafeRef:
        return 0, 0
    rc, out, _ = _git(path, "rev-list", "--left-right", "--count",
                      f"{local_ref}...{remote_ref}")
    if rc != 0 or not out:
        return 0, 0
    parts = out.split()
    if len(parts) != 2:
        return 0, 0
    return int(parts[0]), int(parts[1])


def _ref_time(path: str, ref: str) -> int:
    try:
        ref = check_ref(ref)
    except UnsafeRef:
        return 0
    rc, out, _ = _git(path, "log", "-1", "--format=%ct", ref)
    return int(out) if rc == 0 and out.isdigit() else 0


def _other_branches_behind(path: str, remote: str, skip_branch: str) -> list[str]:
    """Other branches the remote is ahead of, excluding the current one."""
    rc, out, _ = _git(path, "for-each-ref", "--format=%(refname:short)",
                      "refs/heads")
    if rc != 0:
        return []
    try:
        remote = check_remote_name(remote)
    except UnsafeRemote:
        return []
    behind = []
    for br in out.splitlines():
        br = br.strip()
        if not br or br == skip_branch:
            continue
        try:
            check_ref(br)
        except UnsafeRef:
            continue
        rref = f"{remote}/{br}"
        if not _ref_exists(path, rref):
            continue
        _, b = _counts(path, br, rref)
        if b > 0:
            behind.append(f"{br} ↓{b}")
    return behind


def _remote_tracking_refs(path: str, remotes: list[str]) -> list[str]:
    """Remote-tracking branch refs (refs/remotes/<rm>/*, minus <rm>/HEAD).

    Read from the local ref store right after a successful
    `fetch --all --prune`, so an empty list means no remote has any branch —
    derived without another network round-trip.
    """
    rc, out, _ = _git(path, "for-each-ref", "--format=%(refname)",
                      "refs/remotes")
    if rc != 0:
        return []
    prefixes = tuple(f"refs/remotes/{rm}/" for rm in remotes)
    return [r for r in (ln.strip() for ln in out.splitlines())
            if r.startswith(prefixes) and not r.endswith("/HEAD")]


def _local_commit_count(path: str) -> int:
    """Commits reachable from any ref (0 for an unborn HEAD); -1 if unknown."""
    rc, out, _ = _git(path, "rev-list", "--all", "--count")
    if rc != 0:
        return -1
    return int(out) if out.isdigit() else -1


def _is_pending_deletion_path(text: str) -> bool:
    """`-deletion_scheduled-<digits>` as a path-segment suffix in `text`.

    Same semantics as coverage.PENDING_DELETION_RE; imported lazily so git.py
    never depends on coverage at import time.
    """
    if not text:
        return False
    try:
        from coverage import is_pending_deletion
        return bool(is_pending_deletion(str(text)))
    except Exception:
        return bool(re.search(r"-deletion_scheduled-\d+(?=/|$)",
                              str(text).strip("/")))


def _url_path(url: str) -> str:
    """Path component of a remote URL (or the URL/path itself), no `.git`."""
    u = strip_userinfo(url or "").strip()
    m = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/]*(/.*)?$", u)
    if m:
        u = m.group(1) or ""
    elif re.match(r"^[^/:]+@?[^/:]*:", u) and "://" not in u:
        u = u.split(":", 1)[1]                     # scp-like host:path
    u = u.rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    return u


def _is_purged_clone(path: str, remotes: list[str]) -> bool:
    """True when the clone's path or any remote URL path is pending deletion."""
    if _is_pending_deletion_path(os.path.normpath(path)):
        return True
    for rm in remotes:
        rc, out, _ = _git(path, "config", "--get", f"remote.{rm}.url")
        if rc == 0 and _is_pending_deletion_path(_url_path(out)):
            return True
    return False


def sync_repo(path: str) -> dict:
    """
    fetch --all, then work out which remote is ahead for EACH remote and
    fast-forward only when that is safe.
    """
    res = {
        "path": path, "status": S_ERROR, "detail": "",
        "branch": None, "remote": None, "ahead": 0, "behind": 0,
        "other": [], "shallow": False,
    }

    if not os.path.exists(os.path.join(path, ".git")):
        res["status"], res["detail"] = S_ERROR, "Not a git repo"
        return res

    # Surfaced on every sync: a shallow repo is a working repo with an
    # incomplete history, and that fact must never be silently dropped.
    res["shallow"] = is_shallow(path)

    remotes = _remotes(path)
    if not remotes:
        res["status"], res["detail"] = S_NO_REMOTE, "no remote configured"
        return res

    # 1) Always fetch first — there is no way to tell who is ahead without it.
    rc, _, err = _git(path, "fetch", "--all", "--prune", "--tags", "--quiet")
    if rc != 0:
        detail = _summary(err) or f"git fetch failed (rc={rc})"
        res["status"] = classify(err)
        if res["status"] == S_LFS:
            # A fetch never smudges; an LFS-looking fetch error is a plain error.
            res["status"] = S_ERROR
        if res["status"] == S_GONE and _is_purged_clone(path, remotes):
            # GitLab delayed deletion ran out: expected, not an error — never
            # logged, or every sync re-adds it to the Error Log.
            res["status"] = S_PURGED
            res["detail"] = "purged from the server (was scheduled for deletion)"
            return res
        if res["status"] == S_GONE:
            res["detail"] = "not on the server (deleted, renamed, or access revoked)"
        else:
            res["detail"] = detail
        log_error(f"fetch:{path}", err or detail)
        return res

    branch = _current_branch(path)
    if branch is None:
        res["status"] = S_DETACHED
        res["detail"] = "detached HEAD — skipped"
        return res
    # HEAD is as attacker-controlled as any other file in a repository we did
    # not create: a symbolic ref can point at `refs/heads/--upload-pack=...`.
    try:
        branch = check_ref(branch)
    except UnsafeRef as e:
        res["status"], res["detail"] = S_ERROR, str(e)
        log_error(f"sync:{path}", str(e))
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
        if not _remote_tracking_refs(path, remotes):
            # The fetch succeeded and no remote has ANY branch: the server
            # repository is empty.
            n = _local_commit_count(path)
            if n == 0:
                res["status"] = S_EMPTY
                res["detail"] = "empty repository on the server"
            elif n > 0:
                res["detail"] = (f"remote has no branches; {n} local commit(s) "
                                 f"exist and are not pushed — "
                                 f"git push -u origin {branch}")
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

    # Smudge is skipped so an unreachable LFS object cannot abort the
    # fast-forward half-way; LFS content is fetched best-effort afterwards and
    # a failure there is a warning on an otherwise successful pull.
    rc, _, err = _run([GIT_BIN, "-C", path, "merge", "--ff-only", best["ref"]],
                      env_extra=dict(_LFS_SKIP_SMUDGE))
    if rc == 0:
        res["status"] = S_PULLED
        res["detail"] = f"{best['behind']} commit(s) from {best['remote']}"
        lfs_detail = _complete_lfs(path)
        res["lfs"] = lfs_detail
        if lfs_detail:
            res["detail"] += f" — LFS files incomplete: {lfs_detail}"
        return res

    detail = _summary(err) or f"git merge --ff-only failed (rc={rc})"
    res["status"] = classify(err)
    if res["status"] == S_LFS:
        res["status"] = S_ERROR
    res["detail"] = detail
    log_error(f"pull:{path}", err or detail)
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

    # `since` and `author` are interpolated into `--since=`/`--author=`, so a
    # value with a newline in it cannot inject a second argument — but one
    # that IS an option would, and log.py's validator is not on every path
    # into this function.
    for value in (since, author):
        if value is not None and (
                any(c in str(value) for c in _ARG_FORBIDDEN)
                or str(value).lstrip().startswith("-")):
            log_error(f"log:{repo_path}",
                      f"refused an option-like log filter: {redact(str(value))[:60]}")
            return []

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
