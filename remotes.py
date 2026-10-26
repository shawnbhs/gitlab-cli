# remotes.py — Overview of every repo's remotes + URL safety
#
# Two main jobs:
#   1. audit()  → which repo has how many remotes, whether they are healthy,
#                 and whether a token is hardcoded into the URL
#   2. scrub()  → strip the token out of remote URLs and hand it to the
#                 git credential store (safer + a shared repo won't leak it)

import json
import os
import re
import stat
import subprocess

import coverage
import error_catalog
import git as G
import paths
from errors import redact
from printer import (
    print_header,
    BOLD, CYAN, GREEN, YELLOW, DIM, RESET, RED, WHITE
)

# https://oauth2:glpat-xxx@host/path  →  user, token, host
_CRED_RE = re.compile(r"^(https?://)([^/@]+)@(.+)$")


def strip_creds(url: str) -> tuple[str, str]:
    """
    'https://oauth2:glpat-xxx@host/p.git' → ('https://host/p.git', 'oauth2:glpat-xxx')
    If there are no credentials: (url, '')
    """
    m = _CRED_RE.match(url or "")
    if not m:
        return url, ""
    scheme, cred, rest = m.groups()
    return f"{scheme}{rest}", cred


def has_creds(url: str) -> bool:
    return bool(strip_creds(url)[1])


def mask_url(url: str) -> str:
    """Display-safe URL — the token is masked out."""
    clean, cred = strip_creds(url)
    if not cred:
        return url
    user = cred.split(":", 1)[0]
    scheme, rest = clean.split("://", 1)
    return f"{scheme}://{user}:***@{rest}"


# ─── Rendering attacker-controlled text ───────────────────────────────────────
#
# W10. A remote NAME and a remote URL are both read out of a `.git/config`
# this tool did not write. `git remote -v` prints a `[remote "..."]` section
# header back verbatim, escape sequences included: a name of
# `"\x1b[2K\rorigin"` erases the line it was printed on and redraws itself as
# `origin`, so the Remotes screen can be made to describe a repository that is
# not the one on disk. Verified against git 2.43: `remote -v` emits the ESC
# byte unchanged.
#
# Every attacker-controlled string is therefore passed through safe_text()
# before it reaches a terminal, and the ORIGINAL is what is compared,
# validated and (never) written back.

_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def safe_text(s: str, limit: int = 120) -> str:
    """Terminal-safe rendering of a string we did not author.

    Control bytes — ESC, CR, NUL, backspace — become a visible `?`, so a
    name cannot repaint, truncate or colour the line it is printed on.
    """
    if not s:
        return ""
    out = _CTRL_RE.sub("?", str(s))
    if len(out) > limit:
        out = out[:limit] + "…"
    return out


# ─── Reading the remotes ───────────────────────────────────────────────────────
#
# W10. Remotes are parsed out of the config FILE, not out of `git remote -v`.
# Three independent reasons, each of them a real bug that was reproduced:
#
#  1. REDACTION CORRUPTS THE WRITE-BACK. git._run() scrubs its stdout, and the
#     scrubber matches a GitLab-token *shape* anywhere in the text — including
#     inside a path. A real remote of
#     `https://host/acme/glpat-shaped-project-name.git` comes back from
#     `git remote -v` as `https://host/acme/[REDACTED-TOKEN].git`, and scrub()
#     then wrote that literal string into .git/config with `remote set-url`.
#     The repository's remote is destroyed, permanently, by the security
#     feature. remotes.py already knew this about *paths* (see
#     _resolve_gitdir_fs) but the URL path still trusted the redacted text.
#
#  2. THE OUTPUT IS NOT A GRAMMAR. `remote -v` is `name<TAB>url<SPACE>(kind)`
#     split on whitespace. A URL containing a space silently truncates; a URL
#     containing a newline FABRICATES A WHOLE EXTRA ROW. Verified: a remote
#     whose url is `https://real/y.git\nfake\thttps://evil/z (fetch)` makes
#     `git remote -v` print a remote named `fake` pointing at evil, and the old
#     splitter believed it — the audit reported, and the scrub would have
#     rewritten, a remote that does not exist.
#
#  3. It costs a git process per repository across a 700-repository walk.
#
# The parser below implements git's own config grammar (quoting, escapes,
# line continuation, comments) because a naive `line.split("=")` is what
# produced bug 2 one layer up.

def _unescape_subsection(raw: str) -> str:
    """`[remote "a\\"b"]` -> `a"b`. Only `\\\\` and `\\"` are escapes here."""
    out, i = [], 0
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt in ('"', "\\"):
                out.append(nxt)
                i += 2
                continue
        out.append(c)
        i += 1
    return "".join(out)


_ESCAPES = {"n": "\n", "t": "\t", "b": "\b", "\\": "\\", '"': '"'}


def _parse_value(raw: str) -> str:
    """git's value grammar: quotes, escapes, comments, trailing whitespace."""
    out, i, quoted = [], 0, False
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw):
            out.append(_ESCAPES.get(raw[i + 1], raw[i + 1]))
            i += 2
            continue
        if c == '"':
            quoted = not quoted
            i += 1
            continue
        if c in ";#" and not quoted:
            break
        out.append(c)
        i += 1
    return "".join(out).strip() if not quoted else "".join(out)


_SECTION_RE = re.compile(r'^\[([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\]')


def _parse_git_config(text: str) -> dict:
    """{(section, subsection, key): value} — last value wins, as git does."""
    out: dict = {}
    section = sub = ""
    pending = ""
    # Split on "\n" only, never str.splitlines(): a BARE CR is a legal byte
    # inside a section name, and git keeps it (verified — `git config
    # --get-regexp` reports `remote.in<ESC>[2K<CR>ter.url`). splitlines() also
    # splits on \r, \x0b, \x0c and U+2028, so a name carrying a CR was torn in
    # half and the stanza silently vanished from the audit. Only the trailing
    # CR of a CRLF line ending is dropped, which is what git does.
    for line in text.split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        if pending:
            line = pending + line
            pending = ""
        s = line.strip()
        if not s or s[0] in "#;":
            continue
        m = _SECTION_RE.match(s)
        if m:
            section = m.group(1).lower()
            sub = _unescape_subsection(m.group(2)) if m.group(2) else ""
            s = s[m.end():].strip()
            if not s or s[0] in "#;":
                continue
        if "=" not in s:
            continue
        key, _, raw = s.partition("=")
        # A backslash at end of line continues the value onto the next line.
        if raw.rstrip().endswith("\\") and not raw.rstrip().endswith("\\\\"):
            pending = line[:line.rindex("\\")]
            continue
        out[(section, sub, key.strip().lower())] = _parse_value(raw)
    return out


def read_remote_config(path: str) -> dict:
    """{remote name: {'url':…, 'pushurl':…}} straight off the disk.

    Reads the config git itself would read (config_path() already resolves
    the linked-worktree / submodule / bare cases), plus the per-worktree
    `config.worktree` override when one exists.
    """
    out: dict = {}
    for cfg in (config_path(path), worktree_config_path(path)):
        if not cfg or not os.path.isfile(cfg):
            continue
        try:
            # newline="" disables universal-newline translation. Without it
            # Python turns a BARE CR into "\n" on the way in, which tears a
            # section header such as `[remote "a<CR>b"]` in half and makes the
            # whole stanza — a remote that really is in the file and really is
            # used by git — disappear from the audit.
            with open(cfg, encoding="utf-8", errors="replace",
                      newline="") as fh:
                text = fh.read(1024 * 1024)
        except OSError:
            continue
        for (section, sub, key), val in _parse_git_config(text).items():
            if section != "remote" or not sub or key not in ("url", "pushurl"):
                continue
            out.setdefault(sub, {"url": "", "pushurl": ""})[key] = val
    return out


# ─── What this tool is willing to hand to git ─────────────────────────────────
#
# W10. Both halves of a remote are attacker-controlled, and each has its own
# way of becoming code execution. They are judged separately so a repository
# with one bad remote and one good one keeps working — refusing the whole
# repository would just push the user back to doing it by hand.

UNSAFE_NAME = "name"          # git would parse it as an option
UNSAFE_URL = "url"            # a transport that executes, or an option
UNSAFE_CONTROL = "control"    # a control character in either half

# Only these can be rewritten. `ext::` is git's own documented RCE, `file://`
# reads the local disk, and an `ssh://` host can carry `-oProxyCommand=`. None
# of them is a URL this tool ever creates, so none of them is a URL it repairs.
_WRITABLE_SCHEMES = ("https://", "http://")


def remote_problem(name: str, url: str) -> str:
    """'' when this remote is safe to act on, else which rule refused it."""
    if _CTRL_RE.search(name or "") or _CTRL_RE.search(url or ""):
        return UNSAFE_CONTROL
    if not G.is_safe_remote_name(name or ""):
        return UNSAFE_NAME
    u = url or ""
    if u.startswith("-") or not u.lower().startswith(_WRITABLE_SCHEMES):
        return UNSAFE_URL
    # No http(s) clone URL contains whitespace. One that does is either a
    # typo or an attempt to have one argv element read as two downstream.
    if any(c.isspace() for c in u):
        return UNSAFE_URL
    host = u.split("://", 1)[1].split("/", 1)[0].rsplit("@", 1)[-1]
    if not host or "/" in host:
        return UNSAFE_URL
    return ""


# ─── One-line explanations (error catalog) ────────────────────────────────────
#
# Every problem this module puts on screen is ONE short line: the catalog's
# title and cause, plus its fix where there is something to do. Raw git
# stderr is never head-truncated (`err[:120]` cut off the `fatal:` line that
# carries the real cause, which git prints LAST); it is reduced with
# error_catalog.summarize(), redacted again, and passed through safe_text().

def problem_code(name: str, url: str, problem: str) -> str:
    """Catalog code for a remote that remote_problem() refused."""
    if problem == UNSAFE_URL:
        return "unsafe_url"
    if problem == UNSAFE_CONTROL and not _CTRL_RE.search(name or ""):
        return "unsafe_url"          # the control byte is in the URL half
    return "unsafe_remote"


def explain_line(text: str = "", code: str = "", limit: int = 280) -> str:
    """`Title — cause → fix` for a code or a piece of git stderr.

    For an error the catalog does not recognise, the cause is the most
    informative line of the stderr itself (redacted) and no fix is invented:
    "ask your admin" is not a fix. The default limit fits a full catalog
    line (title <= 40, cause and fix <= 110 each), so the fix — the part the
    user acts on — is never the part that gets cut.
    """
    info = error_catalog.by_code(code) if code else None
    if info is None:
        info = error_catalog.explain(text or "")
    if info.code == "unknown" and text:
        line = f"{info.title} — {error_catalog.summarize(redact(text), 110)}"
    else:
        line = f"{info.title} — {info.cause}"
        if info.fix and info.code != "unknown":
            line += f" → {info.fix}"
    return safe_text(redact(line), limit)


def remote_project_path(clean_url: str) -> str:
    """`https://host/grp/sub/proj.git` -> `grp/sub/proj` ('' if none)."""
    if "://" not in (clean_url or ""):
        return ""
    rest = clean_url.split("://", 1)[1]
    if "/" not in rest:
        return ""
    path = rest.split("/", 1)[1].split("?", 1)[0].strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path


def list_remotes(path: str, reveal: bool = False) -> list[dict]:
    """All remotes of a repo, with full details.

    Every row carries `unsafe`: '' for a remote this tool may act on, else the
    rule that refused it. A refused remote is still REPORTED — it is exactly
    the thing the user needs to see — but it is never passed to git and never
    rewritten.

    `url` is MASKED by default. Reading the config file directly is what makes
    the scrub correct (git's stdout is redaction-mangled), but it also means
    the live token is now in this process's hands rather than pre-blunted by
    git._run(). These rows are returned by audit(), printed, and written to
    errors.log, so the default has to be the safe one. `reveal=True` is the
    write path only: scrub() needs the real credential to file it in the
    store, and never puts these rows into its stats.
    """
    cfg = read_remote_config(path)
    if not cfg:
        return []

    remotes = []
    for name, e in cfg.items():
        fetch_url = e.get("url") or ""
        push_url = e.get("pushurl") or ""
        url = fetch_url or push_url
        clean, cred = strip_creds(url)
        problem = remote_problem(name, url)
        project = remote_project_path(clean) if not problem else ""
        remotes.append({
            "name":       name,
            "display":    safe_text(name, 60),
            "url":        url if reveal else mask_url(url),
            "clean_url":  clean,
            "has_creds":  bool(cred),
            "unsafe":     problem,
            "unsafe_code": problem_code(name, url, problem) if problem else "",
            # GitLab's delayed deletion renames the project to
            # `<name>-deletion_scheduled-<id>`. A remote pointing there is
            # neither broken nor renamed: it is reported as such and never
            # re-pointed (see audit() and scrub()).
            "project":    project,
            "pending_deletion": bool(project)
                                and coverage.is_pending_deletion(project),
            "host":       clean.split("://")[-1].split("/")[0] if "://" in clean else "",
            "push_differs": bool(push_url and fetch_url and push_url != fetch_url),
            "protocol":   ("ssh" if clean.startswith(("git@", "ssh://"))
                           else "https" if clean.lower().startswith("https://")
                           else "http" if clean.lower().startswith("http://")
                           else "local"),
        })
    return sorted(remotes, key=lambda r: (r["name"] != "origin", r["name"]))


# W10. `insteadOf` is a config rewrite rule, so a repository can point a
# perfectly ordinary-looking `https://…` remote at `ext::sh -c …` without the
# URL we validated ever containing the payload. Verified: with
# `[url "ext::sh -c touch% /tmp/X "] insteadOf = https://nice.example/`,
# `git ls-remote -- <name>` executes the command. `--` does not stop it, and
# validating the URL does not stop it, because the rewrite happens after both.
# GIT_ALLOW_PROTOCOL does stop it, for this process only, without touching the
# user's config — and http/https keep working under it.
_TRANSPORT_ENV = {"GIT_ALLOW_PROTOCOL": "https:http"}


_AUTH_CODES = ("auth", "forbidden", "token_missing", "lfs_auth")


def _probe(path: str, remote) -> tuple[bool, str, str]:
    """(responds?, short tag, one-line catalog explanation)."""
    if isinstance(remote, dict):
        name, problem = remote.get("name", ""), remote.get("unsafe", "")
        code = remote.get("unsafe_code") or ""
    else:
        name = remote
        problem = UNSAFE_NAME if not G.is_safe_remote_name(name) else ""
        code = ""
    if problem:
        return False, "refused", explain_line(code=code or "unsafe_remote")
    rc, _, err = G._run([G.GIT_BIN, "-C", path, "ls-remote", "--exit-code",
                         "-h", "--", name],
                        timeout=25, env_extra=_TRANSPORT_ENV)
    if rc == 0:
        return True, "", ""
    # Classified on the WHOLE stderr: git prints the real cause last.
    info = error_catalog.explain(err)
    if info.code in _AUTH_CODES or G.is_auth_error(
            error_catalog.summarize(err)):
        tag = "auth"
    elif rc == 124 or info.code == "timeout":
        tag = "timeout"
    else:
        tag = "unreachable"
    return False, tag, explain_line(err)


def _reachable(path: str, remote) -> tuple[bool, str]:
    """Check whether the remote responds, without doing a full fetch."""
    ok, tag, _ = _probe(path, remote)
    return ok, tag


# ─── Which instance does a clone belong to? ───────────────────────────────────
#
# W8-2. Two GitLab instances share ONE clone root (`MasterGroups`) and are
# told apart by a per-instance `folder`:
#
#     Hub     (hub.example.com)  folder = "acme"  -> root/acme
#     Globex  (git.example.net)     folder = ""      -> root
#
# They are asymmetric BY DESIGN: the instance with an empty folder has the
# root itself as its subtree, so it structurally CONTAINS the other one. The
# instance-wide Remotes screen resolves its dest through the usual door
# (`gitlab_cli._resolve_dest` -> `paths.anchor(cfg["master_dir"])`, where
# master_dir already carries the folder), which is correct for Hub and a
# superset for globex: with globex active the walk returned every work
# tree, most of them Hub's. Remotes is the screen that OFFERS TO REWRITE
# remote URLs, so answering `y` there rewrote hundreds of repositories of an instance
# the user had not selected.
#
# The per-group screen was fixed in an earlier wave by resolving through
# `paths` with the instance folder (see gitlab_cli.py's action "3" branch).
# The same signal is used here rather than a second convention: an instance's
# folder, read from the same instances.json `paths.configured_folders()`
# reads, is what marks a subtree as belonging to someone else.
#
# The rule is deliberately an EXCLUSION, not an inclusion:
#
#   * a non-empty folder of ANY OTHER instance marks `root/<folder>` as that
#     instance's subtree; nothing under it is ever audited or scrubbed here.
#   * an other instance whose folder is EMPTY is NOT turned into an exclusion
#     -- its subtree is the root, and excluding the root would empty the walk
#     for the instance that does have a folder. That is exactly the symmetry
#     assumption that breaks one of the two instances.
#   * anything else, including a path outside the managed root entirely
#     (`gitlab-cli remotes ./here`), stays in scope. Out-of-tree destinations
#     are the user's explicit choice and already carry their own warning.
#
# Never imports instances.py: that module builds a cfg, and building a cfg
# resolves TOKENS. instances.json is read directly, exactly as paths.py does.

_scope_cache: dict = {"key": None, "scope": None}


def _read_instances() -> dict:
    try:
        st = os.stat(paths.INSTANCES_FILE)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    if _scope_cache["key"] == key and _scope_cache["scope"] is not None:
        return _scope_cache["scope"]
    try:
        with open(paths.INSTANCES_FILE, encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    _scope_cache.update(key=key, scope=data)
    return data


def active_scope() -> dict | None:
    """Describe the active instance's subtree, or None when undeterminable.

    None means "no scoping information" and is treated everywhere below as
    "do not filter" -- the legacy behaviour. A machine with no instances
    configured, an unreadable config, or no active instance must stay exactly
    as harmless as it is today.
    """
    data = _read_instances()
    insts = data.get("instances") or []
    active = (data.get("active") or "").strip()
    if not isinstance(insts, list) or not active:
        return None

    me = None
    for inst in insts:
        if isinstance(inst, dict) and (inst.get("name") or "").strip() == active:
            me = inst
            break
    if me is None:
        return None

    folder = (me.get("folder") or "").strip()
    root = paths.anchor(me.get("master_dir") or ".")

    others = []
    for inst in insts:
        if not isinstance(inst, dict) or inst is me:
            continue
        f = (inst.get("folder") or "").strip()
        if not f:
            # Empty folder == the root itself. Never an exclusion; see above.
            continue
        f = os.path.basename(os.path.normpath(f))
        if not f or f == folder:
            continue
        others.append({
            "name": (inst.get("name") or "").strip() or f,
            "folder": f,
            "root": os.path.normpath(
                os.path.join(paths.anchor(inst.get("master_dir") or "."), f)),
        })

    return {
        "name": active,
        "folder": folder,
        # `base` is the shared clone root (master_dir WITHOUT the folder);
        # `root` is this instance's subtree inside it. They are equal for the
        # instance whose folder is empty -- that is the whole asymmetry.
        "base": os.path.normpath(root),
        "root": os.path.normpath(os.path.join(root, folder) if folder else root),
        "others": others,
    }


def _is_under(parent: str, child: str) -> bool:
    """Whether child is parent or lives under it (symlinks resolved)."""
    try:
        p = os.path.realpath(parent)
        c = os.path.realpath(child)
    except OSError:
        return False
    return c == p or c.startswith(p + os.sep)


# Bucket name for a clone that sits inside the shared managed root but in
# neither instance's subtree (a hand-made directory, an abandoned layout, a
# tree moved there by hand). It is counted and named on screen, never scrubbed.
UNASSIGNED = "(unassigned)"


def owner_of(path: str, scope: dict | None = None) -> str:
    """'' when the path is in scope, otherwise who it belongs to instead.

    Two independent rules, because neither alone is sufficient:

      * EXCLUSION -- the path is under another instance's folder subtree.
        This is what protects the folder-less instance (globex), whose own
        subtree is the shared root and therefore contains Hub's.
      * CONTAINMENT -- the path is inside the shared managed root but outside
        this instance's subtree. This protects the instance that DOES have a
        folder when a caller hands it a root-wide walk, and it also catches
        directories belonging to no configured instance at all.

    A path outside the managed root entirely is NOT foreign: that is an
    explicit `gitlab-cli remotes /some/where`, which already prints its own
    off-site warning and must keep working.
    """
    if scope is None:
        scope = active_scope()
    if not scope:
        return ""

    for other in scope.get("others", ()):
        if _is_under(other["root"], path):
            return other["name"]

    base = scope.get("base") or ""
    root = scope.get("root") or ""
    if base and root and base != root:
        if _is_under(base, path) and not _is_under(root, path):
            return UNASSIGNED
    return ""


def split_by_instance(repos: list[str],
                      scope: dict | None = None) -> tuple[list[str], dict]:
    """(in-scope repos, {other instance name: count}).

    Repositories owned by another configured instance are REPORTED, never
    silently folded into the audit and never handed to the scrub.
    """
    if scope is None:
        scope = active_scope()
    keep, foreign = [], {}
    for path in repos:
        who = owner_of(path, scope)
        if who:
            foreign[who] = foreign.get(who, 0) + 1
        else:
            keep.append(path)
    return keep, foreign


def scope_label(scope: dict | None) -> str:
    if not scope:
        return "unknown instance"
    return scope.get("name") or "unknown instance"


def scope_line(scope: dict | None, count: int, foreign: dict | None = None) -> str:
    """The one sentence that makes the number on screen unmistakable."""
    who = scope_label(scope)
    where = (scope or {}).get("root", "")
    out = (f"  {BOLD}Instance{RESET} : {GREEN}{who}{RESET}   "
           f"{BOLD}{count}{RESET} repositories in scope")
    if where:
        out += f"\n  {DIM}scope    : {where}{RESET}"
    for name, n in sorted((foreign or {}).items()):
        if name == UNASSIGNED:
            out += (f"\n  {DIM}excluded : {n} repositories under the shared "
                    f"root belong to no configured instance and were NOT "
                    f"audited{RESET}")
        else:
            out += (f"\n  {DIM}excluded : {n} repositories belong to instance "
                    f"'{name}' and were NOT audited{RESET}")
    return out


def confirm_scrub_message(rows: list, scope: dict | None = None) -> str:
    """Exact wording a caller must show BEFORE asking for confirmation.

    The count is the number of repositories whose remotes would actually be
    rewritten, and the instance is named, so a `y` can never mean "and also
    every clone of the other GitLab on this disk".
    """
    if scope is None:
        scope = getattr(rows, "scope", None) or active_scope()
    leaky = [r for r in rows if r.get("leaks")]
    foreign = getattr(rows, "foreign", None) or {}
    msg = (f"About to rewrite remote URLs in {len(leaky)} of "
           f"{len(rows)} repositories of instance "
           f"'{scope_label(scope)}'.")
    if foreign:
        total = sum(foreign.values())
        names = ", ".join(sorted(foreign))
        msg += (f" {total} repositories of {names} are excluded and will not "
                f"be touched.")
    return msg


class AuditRows(list):
    """A plain list of rows that also remembers which instance it covers.

    A list subclass rather than a new return type: every existing caller and
    test treats the result of audit() as a list, and must keep working.
    """
    scope: dict | None = None
    foreign: dict = {}
    dropped: int = 0


# ─── Audit ─────────────────────────────────────────────────────────────────────

def audit(repos: list[str], check_network: bool = False,
          scope: dict | None = None,
          enforce_scope: bool = True) -> list[dict]:
    """Inspect the remotes of every repo of the ACTIVE instance.

    `enforce_scope=True` (the default) drops repositories that belong to a
    different configured instance. The result is an AuditRows -- still a
    list -- carrying `.scope` and `.foreign` so print_audit can name the
    instance and the count it actually covered.
    """
    if scope is None:
        scope = active_scope()
    if enforce_scope:
        repos, foreign = split_by_instance(repos, scope)
    else:
        foreign = {}

    rows = AuditRows()
    rows.scope = scope
    rows.foreign = foreign
    rows.dropped = sum(foreign.values())
    for path in repos:
        remotes = list_remotes(path)
        # W10. A remote this tool refuses to touch is still a remote the user
        # needs to know about — it is the single most interesting row on the
        # screen. It is reported by its SANITISED name and excluded from
        # `leaks`, which is the list the scrub acts on.
        refused = [r for r in remotes if r["unsafe"]]
        leaks   = [r["name"] for r in remotes if r["has_creds"] and not r["unsafe"]]
        row = {
            "path": path,
            "name": os.path.basename(path),
            "remotes": remotes,
            "count": len(remotes),
            "leaks": leaks,
            "has_creds": bool(leaks),
            "refused": [{"name": r["display"], "why": r["unsafe"],
                         "code": r["unsafe_code"],
                         "line": explain_line(code=r["unsafe_code"])}
                        for r in refused],
            "hosts": sorted({r["host"] for r in remotes
                             if r["host"] and not r["unsafe"]}),
            # Scheduled for deletion on the server: reported on its own, never
            # probed, never counted as broken or renamed.
            "pending_deletion": [{"name": r["display"],
                                  "project": safe_text(r["project"], 80)}
                                 for r in remotes
                                 if r["pending_deletion"] and not r["unsafe"]],
            "unreachable": [],
        }
        if check_network:
            for r in remotes:
                if r["unsafe"] or r["pending_deletion"]:
                    continue
                ok, _, line = _probe(path, r)
                if not ok:
                    row["unreachable"].append(f"{r['display']}: {line}")
        rows.append(row)
    return rows


def print_audit(rows: list[dict], show_all: bool = False):
    """Print a human-readable report."""
    multi   = [r for r in rows if r["count"] > 1]
    leaky   = [r for r in rows if r["leaks"]]
    nothing = [r for r in rows if r["count"] == 0]
    broken  = [r for r in rows if r["unreachable"]]
    refused = [r for r in rows if r.get("refused")]
    pending = [r for r in rows if r.get("pending_deletion")]

    print_header("Remotes")

    # W8-2. The instance and the covered count come FIRST, so the number on
    # screen can never be read as "every repository on this machine".
    scope   = getattr(rows, "scope", None)
    foreign = getattr(rows, "foreign", None) or {}
    print(scope_line(scope, len(rows), foreign))
    print()

    print(f"  {BOLD}{len(rows)}{RESET} repos inspected\n")
    print(f"  {GREEN}●{RESET} 1 remote        : {len(rows) - len(multi) - len(nothing)}")
    print(f"  {CYAN}●{RESET} multi remote    : {len(multi)}")
    if nothing:
        print(f"  {YELLOW}●{RESET} no remote       : {len(nothing)}")
    if leaky:
        print(f"  {RED}●{RESET} token in URL    : {len(leaky)}  {DIM}← security risk{RESET}")
    if broken:
        print(f"  {RED}●{RESET} not responding  : {len(broken)}")
    if refused:
        n = sum(len(r["refused"]) for r in refused)
        print(f"  {RED}●{RESET} refused remote  : {n}  "
              f"{DIM}← not safe to hand to git{RESET}")
    if pending:
        n = sum(len(r["pending_deletion"]) for r in pending)
        print(f"  {YELLOW}●{RESET} deletion pending: {n}  "
              f"{DIM}← scheduled for deletion on the server{RESET}")

    # All hosts
    hosts = {}
    for r in rows:
        for h in r["hosts"]:
            hosts[h] = hosts.get(h, 0) + 1
    if hosts:
        print(f"\n  {BOLD}Hosts:{RESET}")
        for h, c in sorted(hosts.items(), key=lambda x: -x[1]):
            print(f"    {DIM}{c:>4}{RESET}  {h}")

    if multi:
        print(f"\n  {BOLD}{CYAN}Repos with multiple remotes:{RESET}")
        for r in multi:
            names = "  ".join(
                f"{RED if rm['unsafe'] else GREEN if rm['name'] == 'origin' else CYAN}"
                f"{rm['display']}{RESET}"
                f"{DIM}({rm['protocol']}){RESET}"
                for rm in r["remotes"]
            )
            print(f"    {WHITE}{safe_text(r['name'], 60)}{RESET}  {names}")
            if show_all:
                for rm in r["remotes"]:
                    print(f"        {DIM}{rm['display']:12} "
                          f"{safe_text(mask_url(rm['url']))}{RESET}")

    if leaky:
        print(f"\n  {BOLD}{RED}Token in remote URL (must be removed):{RESET}")
        for r in leaky[:20]:
            shown = ", ".join(safe_text(n, 40) for n in r["leaks"])
            print(f"    {safe_text(r['name'], 60)}  {DIM}→ {shown}{RESET}")
        if len(leaky) > 20:
            print(f"    {DIM}… and {len(leaky) - 20} more{RESET}")
        print(f"\n  {YELLOW}→ Run the 'Strip tokens from remotes' option{RESET}")
        # Stated before any caller can ask for confirmation.
        print(f"  {BOLD}{confirm_scrub_message(rows, scope)}{RESET}")

    if refused:
        print(f"\n  {BOLD}{RED}Refused — NOT audited and NOT rewritten:{RESET}")
        for r in refused[:15]:
            for bad in r["refused"][:4]:
                line = bad.get("line") or explain_line(
                    code=bad.get("code") or "unsafe_remote")
                print(f"    {safe_text(r['name'], 40)}  {DIM}→{RESET} "
                      f"{RED}{bad['name']}{RESET}{DIM}: {line}{RESET}")
        if len(refused) > 15:
            print(f"    {DIM}… and {len(refused) - 15} more repositories{RESET}")

    if pending:
        print(f"\n  {BOLD}{YELLOW}Scheduled for deletion on the server "
              f"(not broken, not renamed — left as is):{RESET}")
        for r in pending[:15]:
            for p in r["pending_deletion"][:4]:
                print(f"    {safe_text(r['name'], 40)}  {DIM}→ {p['name']}: "
                      f"{p['project']}{RESET}")
        if len(pending) > 15:
            print(f"    {DIM}… and {len(pending) - 15} more repositories{RESET}")
        print(f"  {DIM}{explain_line(code='pending_deletion')}{RESET}")

    if nothing:
        print(f"\n  {BOLD}{YELLOW}No remote:{RESET}")
        for r in nothing[:15]:
            print(f"    {DIM}{safe_text(r['name'], 60)}{RESET}")

    if broken:
        print(f"\n  {BOLD}{RED}Not responding:{RESET}")
        for r in broken[:15]:
            # One line per remote; each entry is already `name: catalog line`
            # and already sanitised, so it is not cut down to a stub here.
            for u in r["unreachable"][:4]:
                print(f"    {safe_text(r['name'], 60)}  "
                      f"{DIM}{safe_text(u, 300)}{RESET}")
        if len(broken) > 15:
            print(f"    {DIM}… and {len(broken) - 15} more repositories{RESET}")
    print()


# ─── Scrub: token from URL → credential store ──────────────────────────────────

def _credential_approve(clean_url: str, cred: str) -> bool:
    """Hand the token over to the git credential helper.

    The `git credential` protocol is newline-delimited: one `key=value`
    per line. If the host or password contains a newline, extra fields can
    be injected. Verified: `password=x\\npassword=Y\\nhost=evil.com` causes
    the token to be stored for evil.com. The URL comes from `.git/config`,
    so it cannot be trusted.
    """
    try:
        scheme, rest = clean_url.split("://", 1)
    except ValueError:
        return False

    host = rest.split("/")[0]
    if ":" in cred:
        username, password = cred.split(":", 1)
    else:
        username, password = "oauth2", cred

    # W10. The protocol is newline-delimited and LATER KEYS WIN, so a single
    # `\n` anywhere lets a hostile .git/config append `host=evil.example` and
    # have OUR token filed under the attacker's host — every later push to it
    # then sends the token. Rejecting only \n\r\0 is not enough: a bare space
    # or a control byte in `host` produces an entry git will never match, and
    # a `host` that is not a hostname is not a host at all.
    for field in (scheme, host, username, password):
        if not field or _CTRL_RE.search(field) or any(c.isspace() for c in field):
            return False
    if scheme not in ("http", "https"):
        return False
    # `host[:port]` and nothing else — no path, no userinfo, no wildcard.
    if not re.match(r"^[A-Za-z0-9._-]+(?::[0-9]{1,5})?$", host):
        return False

    payload = (f"protocol={scheme}\nhost={host}\n"
               f"username={username}\npassword={password}\n\n")
    try:
        r = subprocess.run([G.GIT_BIN, "credential", "approve"],
                           input=payload, text=True,
                           capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


# ─── Permissions: .git/config must not stay world-readable ─────────────────────

# git creates .git/config as 0644. That is harmless for a credential-free
# clone, but it is exactly what turns "a token on my disk" into "a token every
# local account can read". After a scrub we tighten it so that if a credential
# is ever written there again it is at least not world-readable.
CONFIG_MODE = 0o600


def _read_small(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(4096)
    except OSError:
        return ""


def _resolve_gitdir_fs(path: str) -> str:
    """This worktree's git dir, resolved WITHOUT running git.

    `.git` is a directory for a plain clone, and a file
    (`gitdir: <path>`) for a submodule or a linked worktree. A bare repo
    is its own git dir.

    Needed because git's stdout is redacted on the way back (`git._run`
    scrubs token-shaped text), and a *path* can contain token-shaped text:
    `…/atlas/glint-management-system` matches the `gl<2-6 letters>-<16+>`
    GitLab-token pattern and comes back as `…/atlas/[REDACTED-TOKEN]`.
    Trusting that string would chmod nothing (best case) or the wrong file.
    """
    dot = os.path.join(path, ".git")
    if os.path.isdir(dot):
        return dot
    if os.path.isfile(dot):
        text = _read_small(dot).strip()
        if text.startswith("gitdir:"):
            target = text.split(":", 1)[1].strip()
            if not os.path.isabs(target):
                target = os.path.join(path, target)
            return os.path.abspath(target)
        return ""
    # Bare repository: the directory itself is the git dir.
    if (os.path.isfile(os.path.join(path, "HEAD"))
            and os.path.isdir(os.path.join(path, "objects"))
            and os.path.isdir(os.path.join(path, "refs"))):
        return os.path.abspath(path)
    return ""


def _common_dir_fs(path: str) -> str:
    """The git dir whose `config` git actually reads.

    For a linked worktree the per-worktree git dir
    (`<main>/.git/worktrees/<name>`) holds NO `config`; it holds a
    `commondir` file pointing at the shared dir, which does.
    """
    gitdir = _resolve_gitdir_fs(path)
    if not gitdir:
        return ""
    common = _read_small(os.path.join(gitdir, "commondir")).strip()
    if common:
        if not os.path.isabs(common):
            common = os.path.join(gitdir, common)
        return os.path.abspath(common)
    return gitdir


def _ask_git_common_dir(path: str) -> str:
    """git's own answer, or '' when it is unusable.

    Rejected when it is empty, redacted, or does not exist on disk — a
    redacted answer is a plausible-looking wrong path, which is worse than
    no answer.
    """
    for args in (("rev-parse", "--path-format=absolute", "--git-common-dir"),
                 ("rev-parse", "--git-common-dir")):
        rc, out, _ = G._git(path, *args)
        cand = out.strip() if rc == 0 else ""
        if not cand or "[REDACTED" in cand:
            continue
        if not os.path.isabs(cand):
            cand = os.path.join(path, cand)     # relative to git's cwd = path
        cand = os.path.abspath(cand)
        if os.path.isdir(cand):
            return cand
    return ""


def config_path(path: str) -> str:
    """Absolute path of the config file git actually READS for this repo.

    `.git` is not always a directory:

      * linked worktree — `.git` is a file and the per-worktree git dir
        (`<main>/.git/worktrees/<name>`) contains no `config`; git reads the
        *common* dir's config (`<main>/.git/config`).
      * submodule — `.git` is a file pointing at `<super>/.git/modules/<n>`,
        which IS the common dir and does hold `config`.
      * bare repo — the git dir is the repo directory itself.

    Assuming `<repo>/.git/config` (or asking for the per-worktree git dir)
    makes the hardening silently no-op on exactly the repos that the
    discovery fix made visible to the scrub.
    """
    common = _common_dir_fs(path) or _ask_git_common_dir(path)
    if not common:
        return os.path.join(path, ".git", "config")
    return os.path.join(common, "config")


def worktree_config_path(path: str) -> str | None:
    """`config.worktree` of this worktree, if one exists.

    Present only with `extensions.worktreeConfig`. It can carry per-worktree
    overrides, so it is hardened too — best effort.
    """
    gitdir = _resolve_gitdir_fs(path)
    if not gitdir:
        return None
    cfg = os.path.join(gitdir, "config.worktree")
    return cfg if os.path.isfile(cfg) else None


def _chmod_600(cfg: str) -> bool:
    """chmod the config to 0600 — through a file descriptor, never a path.

    W10. `.git/config` is a file inside a repository this tool did not
    create, so it can be a SYMLINK. `os.chmod(path)` follows it: a clone
    whose `.git/config` points at `~/.ssh/id_rsa` or at another user's file
    made the hardening step chmod the target instead. Verified: chmod
    through a symlink changes the victim's mode, not the link's.

    Opening with O_NOFOLLOW|O_RDONLY refuses the symlink outright (ELOOP)
    and then fchmod()s the descriptor we actually opened, so the file that
    is checked is the file that is changed — no TOCTOU window either.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(cfg, flags)
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return False
        if (st.st_mode & 0o777) != CONFIG_MODE:
            os.fchmod(fd, CONFIG_MODE)
        return (os.fstat(fd).st_mode & 0o777) == CONFIG_MODE
    except OSError:
        return False
    finally:
        os.close(fd)


def harden_config(path: str) -> bool:
    """chmod the repo's config to 0600. True if it is 0600 afterwards."""
    cfg = config_path(path)
    ok = _chmod_600(cfg)

    extra = worktree_config_path(path)
    if extra and os.path.realpath(extra) != os.path.realpath(cfg):
        _chmod_600(extra)          # best effort; does not decide the result

    return ok


def scrub(repos: list[str], save_to_store: bool = True,
          dry_run: bool = False, scope: dict | None = None,
          enforce_scope: bool = True) -> dict:
    """
    Strip tokens out of remote URLs.
    Before removing it, the token is saved to the credential store so that
    push/pull keeps working.

    W8-2. The scope filter is re-applied HERE and not inherited from audit():
    callers pass the same raw repo list to both, so a scrub that trusted the
    audit to have filtered would rewrite the other instance's remotes the
    moment one caller forgot. This is the write path -- it filters itself.
    """
    if scope is None:
        scope = active_scope()
    if enforce_scope:
        repos, foreign = split_by_instance(repos, scope)
    else:
        foreign = {}

    stats = {"scanned": 0, "cleaned": 0, "saved": 0, "failed": 0,
             "hardened": 0, "harden_failed": 0, "refused": 0,
             "pending_deletion": 0, "details": [],
             "instance": scope_label(scope),
             "skipped_other_instance": sum(foreign.values()),
             "skipped_by_instance": foreign}
    saved_hosts = set()

    for path in repos:
        stats["scanned"] += 1
        touched = False
        # reveal=True: the real credential is needed to file it in the store.
        # Nothing from these rows reaches stats["details"] unmasked.
        for rm in list_remotes(path, reveal=True):
            name = os.path.basename(path)

            # W10. The gate for the WRITE path. `remote set-url <name> <url>`
            # puts two attacker-controlled strings straight into a git argv:
            # a name of `--output=<file>` is an option, and a URL of
            # `ext::sh -c …` is git's documented RCE. A remote that fails
            # remote_problem() is counted, named (sanitised) and left exactly
            # as it is — repairing a remote we cannot parse is not repair.
            if rm["unsafe"]:
                stats["refused"] += 1
                stats["details"].append(
                    f"{RED}{name}/{rm['display']}: "
                    f"{explain_line(code=rm['unsafe_code'])}{RESET}")
                continue

            # A remote whose project is scheduled for deletion on the server
            # is counted and named. Its PATH is never rewritten: the only
            # change scrub() ever makes is dropping userinfo from the URL, and
            # `clean` below keeps the path byte-for-byte, so the token still
            # leaves the file without the remote being re-pointed.
            if rm["pending_deletion"]:
                stats["pending_deletion"] += 1
                stats["details"].append(
                    f"{YELLOW}{name}/{rm['display']}: scheduled for deletion "
                    f"on the server — path left as is{RESET}")

            if not rm["has_creds"]:
                continue

            clean, cred = strip_creds(rm["url"])

            # The replacement URL is built from the config text, never from
            # git's stdout — `git._run` redacts token-SHAPED substrings, and a
            # real path can be token-shaped. Re-validating it here means a
            # corrupted or option-shaped replacement can never be written.
            if remote_problem(rm["name"], clean):
                stats["refused"] += 1
                stats["details"].append(
                    f"{RED}{name}/{rm['display']}: "
                    f"{explain_line(code='unsafe_url')}{RESET}")
                continue

            # Belt and braces for the pending-deletion promise above.
            if remote_project_path(clean) != rm["project"]:
                stats["refused"] += 1
                stats["details"].append(
                    f"{RED}{name}/{rm['display']}: "
                    f"{explain_line(code='unsafe_url')}{RESET}")
                continue

            if dry_run:
                stats["cleaned"] += 1
                stats["details"].append(f"{name}/{rm['display']} → {clean}")
                continue

            # 1) Secure the token first
            host_key = clean.split("://")[-1].split("/")[0]
            if save_to_store and host_key not in saved_hosts:
                if _credential_approve(clean, cred):
                    saved_hosts.add(host_key)
                    stats["saved"] += 1

            # 2) Then strip it from the URL
            rc, _, err = G._run([G.GIT_BIN, "-C", path, "remote", "set-url",
                                 "--", rm["name"], clean])
            if rc == 0:
                stats["cleaned"] += 1
                touched = True
                stats["details"].append(f"{name}/{rm['display']}")
            else:
                stats["failed"] += 1
                stats["details"].append(
                    f"{RED}{name}/{rm['display']}: {explain_line(err)}{RESET}")

        # Defence in depth: a config that HELD a credential gets tightened to
        # 0600 even though it no longer holds one, so a future re-injection is
        # not immediately world-readable.
        if touched and not dry_run:
            if harden_config(path):
                stats["hardened"] += 1
            else:
                stats["harden_failed"] += 1

    return stats
