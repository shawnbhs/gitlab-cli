#!/usr/bin/env python3
"""
verify_mirror.py — ONE command that answers, with evidence:
    "is my mirror complete, and is it clean?"

for EVERY configured GitLab instance at once.

INDEPENDENCE (non-negotiable)
-----------------------------
This script is a REFEREE for the gitlab-cli. It therefore imports NOTHING from
the CLI — no `coverage`, no `paths`, no `instances`, no `api`, no `config`. It
never inserts the CLI install directory on sys.path. Standard library only, and
it must run under the SYSTEM `python3`, which cannot resolve the CLI's
dependencies. A checker that reuses the subject's modules cannot detect the
subject's bugs: that is exactly how "Clone All: done" coexisted with 51 of 86
projects never fetched.

It is a SUPERSET of /tmp/gitlab-cli-audit/ground_truth.py (same normalisation
and pagination approach, one instance -> all instances, plus rename-redirect
probing, policy exclusions, credential hygiene, shallow/broken/duplicate
detection, and an explicit UNKNOWN state).

DESIGN RULES, each learned from a real failure on this project
-------------------------------------------------------------
1. Identity is the full normalised origin URL: (host, path_with_namespace),
   lowercased, ".git" suffix and trailing slash stripped. NEVER a basename —
   30 basenames repeat across namespaces as genuinely different projects.
2. Group project listings pass include_subgroups=true. Verified: group 35
   returns 0 projects without the flag and 22 with it.
3. Pagination follows the X-Next-Page response header, never a guessed page
   count and never "stop on a short page".
4. A renamed project reads as missing AND orphan simultaneously. Every orphan
   is probed with GET /api/v4/projects/:url_encoded_old_path, which follows
   GitLab's own rename redirect and returns 200 with the current path. A prior
   audit reported 212 missing when 16 were renames already on disk.
5. A partial or failed fetch is reported as UNKNOWN — never as zero and never
   as success. A verdict that can only mean "healthy" is worthless.
6. Liveness uses /api/v4/metadata (fallback /api/v4/user). /api/v4/version
   returns 401 on a scope-restricted-but-fully-authorised admin PAT and would
   declare a working instance dead.
7. The filesystem walk does NOT prune at the first .git — nested checkouts are
   real here and a security sweep that misses them is worse than none.
8. No token value is ever printed, in stdout, stderr, or the JSON artifact.

EXIT CODES  (W9-7: the code has to MEAN something)
--------------------------------------------------
    0  CLEAN        complete, tidy, nothing to do
    1  CORRECTNESS  the server has data this mirror does not: `missing`
                    projects, broken clones, shallow clones
    2  OPERATIONAL  the check itself could not run or could not conclude:
                    bad token, unreachable server, absent clone root, a
                    listing that came back empty without an error. NOT a
                    failed check — an undetermined one, and never reportable
                    as a clean mirror
    3  SECURITY     the mirror is complete but a credential is sitting in a
                    .git/config on disk
    4  HYGIENE      the mirror is complete and the tree is untidy: duplicate
                    clones, .git/config not 0600, clones of projects deleted
                    upstream

Precedence is by SEVERITY, not by number:
    OPERATIONAL(2) > CORRECTNESS(1) > SECURITY(3) > HYGIENE(4) > CLEAN(0)
"undetermined" outranks everything because a result you cannot trust is worse
than a finding you can act on.

WHY THIS SPLIT EXISTS
---------------------
`1` used to mean "missing or dirty", conflating a mirror that has lost data
with a mirror that is complete and merely untidy. For most of this project's
recent history the referee exited 1 while `missing` was 0 on BOTH instances —
at one point driven entirely by 413 `.git/config` mode flags on otherwise
perfect repositories. An exit code that is always 1 is an exit code nobody
reads, and this tool exists precisely because a status that cannot
distinguish states teaches its user to ignore it.

Backward compatibility: 0 still means everything is fine and every non-zero
code still means "look at this". 1 and 2 keep their old meanings. Consumers
that only understand 0/1/2 can pass --legacy-exit-codes to collapse 3 and 4
back into 1; the printed verdict still names the real category.

USAGE
-----
    python3 scripts/verify_mirror.py                 # all instances
    python3 scripts/verify_mirror.py --instance Hub
    python3 scripts/verify_mirror.py --json out.json --verbose
    python3 scripts/verify_mirror.py --legacy-exit-codes
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
CONFIG_DIR = os.path.expanduser("~/.gitlab-cli")
INSTANCES_FILE = os.path.join(CONFIG_DIR, "instances.json")
ENV_FILE = os.path.join(CONFIG_DIR, "env")

# scripts/verify_mirror.py -> the CLI install root is the parent directory.
INSTALL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HTTP_TIMEOUT = 60
GIT_TIMEOUT = 30
PER_PAGE = 100
MAX_PAGES = 500

VERDICT_CLEAN = "COMPLETE_AND_CLEAN"
VERDICT_DIRTY = "INCOMPLETE_OR_DIRTY"
VERDICT_INSECURE = "COMPLETE_BUT_CREDENTIAL_ON_DISK"
VERDICT_UNTIDY = "COMPLETE_BUT_UNTIDY"
VERDICT_UNKNOWN = "UNKNOWN"

# The verdict a reader sees, keyed by the severity that produced it.
VERDICT_BY_SEVERITY = {
    "unknown": VERDICT_UNKNOWN,
    "incomplete": VERDICT_DIRTY,
    "insecure": VERDICT_INSECURE,
    "hygiene": VERDICT_UNTIDY,
    "clean": VERDICT_CLEAN,
}

EXIT_OK = 0
EXIT_INCOMPLETE = 1     # correctness: the server has data the mirror does not
EXIT_ERROR = 2          # operational: could not determine — not a failed check
EXIT_INSECURE = 3       # security: a credential is on disk
EXIT_HYGIENE = 4        # hygiene: complete but untidy

# Historical name for 1. Kept so any existing consumer, test or script that
# refers to EXIT_DIRTY keeps working — and keeps meaning 1.
EXIT_DIRTY = EXIT_INCOMPLETE

# What a user sees when they look up the number they got.
EXIT_CATEGORY = {
    EXIT_OK: "CLEAN",
    EXIT_INCOMPLETE: "CORRECTNESS",
    EXIT_ERROR: "OPERATIONAL",
    EXIT_INSECURE: "SECURITY",
    EXIT_HYGIENE: "HYGIENE",
}

EXIT_BLURB = {
    EXIT_OK: "complete and tidy",
    EXIT_INCOMPLETE: "the server has data this mirror does not",
    EXIT_ERROR: "undetermined — the check could not conclude",
    EXIT_INSECURE: "complete, but a credential is on disk",
    EXIT_HYGIENE: "complete; the tree is untidy",
}

# Worst first. Precedence is by SEVERITY, never by numeric value: an
# undetermined result outranks a definite finding, because a number you
# cannot trust is worse than a problem you can act on. Do NOT "simplify"
# the fleet verdict to max()/min() of the codes — that would rank HYGIENE(4)
# above OPERATIONAL(2) and hand a clean-looking 2 to a broken mirror.
SEVERITY_ORDER = ("unknown", "incomplete", "insecure", "hygiene", "clean")

SEVERITY_EXIT = {
    "unknown": EXIT_ERROR,
    "incomplete": EXIT_INCOMPLETE,
    "insecure": EXIT_INSECURE,
    "hygiene": EXIT_HYGIENE,
    "clean": EXIT_OK,
}

EXIT_LEGEND = (
    "EXIT CODES: 0 complete+tidy | 1 data the server has is not on disk | "
    "2 undetermined, operational failure | 3 credential on disk | "
    "4 untidy only")

# Directories never descended into. `.git` internals only — vendor dirs are
# still walked unless --skip-vendor, because a leaked credential inside a
# vendored checkout is still a leaked credential.
ALWAYS_SKIP = {".git"}
VENDOR_SKIP = {"node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache"}

# ---------------------------------------------------------------------------
# Credential shapes that must never sit in a .git/config.
#
# W7-2 false-positive fix. The previous `gitlab_pat` pattern was
# `\bgl[a-z]{2,6}-[A-Za-z0-9_\-]{16,}` — an OPEN-ENDED prefix class. GitLab
# token prefixes are a closed, documented set; `gl` + "any 2-6 letters" is not.
# It therefore matched ordinary PROJECT NAMES: the clean, credential-free URL
#     url = https://hub.example.com/atlas/glint-management-system.git
# matched on the path segment `glint-management-system` and made
# `verify-mirror` exit 1 forever on a demonstrably clean tree. A check that
# cries wolf gets ignored, so it is fixed in two independent ways:
#
#   1. ENUMERATE the real prefixes (below). `glint-` is not one of them, and
#      cannot become one, because the set is defined by GitLab, not by us.
#   2. Require the match to be in a CREDENTIAL POSITION — the userinfo
#      component of a URL, an `http.extraheader` value, or a `credential.*`
#      value. A bare path segment can never be a credential, whatever it spells.
#
# Both are applied: (1) alone would still flag a *project* literally named
# `glpat-something`, and (2) alone would miss a token pasted into a key this
# code does not yet know about. Together a finding needs a real prefix OR a
# real position, and the clean URL above satisfies neither.
# ---------------------------------------------------------------------------

# The closed set of GitLab token prefixes, as documented by GitLab. Extend this
# list when GitLab adds a prefix — never widen it back into a character class.
GITLAB_TOKEN_PREFIXES = (
    "glpat-",     # personal access token
    "gldt-",      # deploy token
    "glrt-",      # runner authentication token
    "glsoat-",    # SCIM OAuth access token
    "glptt-",     # pipeline trigger token
    "gloas-",     # OAuth application secret
    "glimt-",     # incoming mail token
    "glagent-",   # cluster agent token
    "glcbt-",     # cluster agent bot token
    "glffct-",    # feature flags client token
)
_GL_PREFIX_ALT = "|".join(re.escape(p) for p in GITLAB_TOKEN_PREFIXES)

# A token-shaped LITERAL: real prefix + a plausible secret body. Used both for
# detection and for belt-and-braces redaction.
RE_GITLAB_TOKEN_LITERAL = re.compile(
    r"(?:" + _GL_PREFIX_ALT + r")[A-Za-z0-9_\-]{16,}")
RE_GITHUB_TOKEN_LITERAL = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")

# A credential POSITION: the userinfo component of a URL (`scheme://user:secret@`
# or `scheme://secret@`), an extraheader value, or a credential.* value. These
# are the only places in a git config where a secret can actually live.
RE_URL_USERINFO = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s@]+@")
RE_EXTRAHEADER_VALUE = re.compile(
    r"^\s*extraheader\s*=\s*\S+", re.IGNORECASE | re.MULTILINE)
RE_CREDENTIAL_VALUE = re.compile(
    r"^\s*(?:password|username|askpass)\s*=\s*\S+",
    re.IGNORECASE | re.MULTILINE)

CRED_PATTERNS = [
    ("userinfo_in_url", RE_URL_USERINFO),
    ("gitlab_pat", RE_GITLAB_TOKEN_LITERAL),
    ("oauth2_literal", re.compile(r"\boauth2\s*:(?=[^/\s]*@)", re.IGNORECASE)),
    ("github_pat", RE_GITHUB_TOKEN_LITERAL),
    ("extraheader", RE_EXTRAHEADER_VALUE),
    ("credential_value", RE_CREDENTIAL_VALUE),
]

# Patterns whose matches are secret VALUES and so must be scrubbed from output.
# Positional patterns are excluded: their match text is a key name, not a
# secret, and substituting it would corrupt reports without hiding anything.
REDACT_PATTERNS = [
    ("gitlab_pat", RE_GITLAB_TOKEN_LITERAL),
    ("github_pat", RE_GITHUB_TOKEN_LITERAL),
]


# ---------------------------------------------------------------------------
# secret handling — collected once, scrubbed from absolutely every output path
# ---------------------------------------------------------------------------
class Redactor:
    """Holds every secret we have read so no code path can leak one."""

    def __init__(self):
        self._secrets = []

    def add(self, secret):
        if secret and len(secret) >= 8 and secret not in self._secrets:
            self._secrets.append(secret)

    def __call__(self, obj):
        if isinstance(obj, str):
            out = obj
            for s in self._secrets:
                if s in out:
                    out = out.replace(s, "<redacted>")
            # belt and braces: any token-shaped literal goes too
            for _name, pat in REDACT_PATTERNS:
                out = pat.sub("<redacted>", out)
            return out
        if isinstance(obj, dict):
            return {self(k): self(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self(v) for v in obj]
        if isinstance(obj, tuple):
            return tuple(self(v) for v in obj)
        return obj


REDACT = Redactor()


def say(*parts):
    print(REDACT(" ".join(str(p) for p in parts)))


def warn(*parts):
    print(REDACT(" ".join(str(p) for p in parts)), file=sys.stderr)


# ---------------------------------------------------------------------------
# config + token, read by this script itself (never sourced, never argv)
# ---------------------------------------------------------------------------
def read_instances(path=INSTANCES_FILE):
    """-> (list_of_instance_dicts, active_name). Raises OperationalError."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise OperationalError(f"instances config not found: {path}") from None
    except (OSError, ValueError) as exc:
        raise OperationalError(f"cannot parse {path}: {type(exc).__name__}: {exc}") from exc
    insts = data.get("instances")
    if not isinstance(insts, list) or not insts:
        raise OperationalError(f"{path} contains no instances")
    return insts, data.get("active")


def read_env_tokens(path=ENV_FILE):
    """Parse `[export] VAR=value` lines from the env file.

    The file is read here, by this script, on purpose: sourcing it inside a
    nested `bash -lc` one-liner both mangles quoting and risks echoing it.
    """
    tokens = {}
    pat = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.lstrip().startswith("#"):
                    continue
                m = pat.match(line)
                if not m:
                    continue
                val = m.group(2)
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                if val:
                    tokens[m.group(1)] = val
                    REDACT.add(val)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise OperationalError(f"cannot read {path}: {exc}") from exc
    for k, v in os.environ.items():
        if k.startswith("GITLAB_TOKEN") and v and k not in tokens:
            tokens[k] = v
            REDACT.add(v)
    return tokens


def env_file_mode(path=ENV_FILE):
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


class OperationalError(Exception):
    """Anything that makes a determination impossible -> UNKNOWN, exit 2."""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def api_request(base_url, path, token, params=None):
    """-> (status, parsed_or_None, headers_dict). Never raises on HTTP status."""
    url = base_url.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={  # noqa: S310 - https base_url from config
        "PRIVATE-TOKEN": token,
        "Accept": "application/json",
        "User-Agent": "verify-mirror/1.0 (independent referee)",
    })
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310 - url is built from the configured https base_url
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw.strip() else None
            except ValueError:
                parsed = None
            return resp.getcode(), parsed, dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            body = ""
        return exc.code, {"_body": body}, dict(exc.headers.items() if exc.headers else {})
    except urllib.error.URLError as exc:
        raise OperationalError(f"cannot reach {base_url}: {exc.reason}") from exc
    except OSError as exc:
        raise OperationalError(f"cannot reach {base_url}: {exc}") from exc


def check_liveness(base_url, token):
    """/api/v4/metadata, falling back to /api/v4/user.

    NOT /api/v4/version: it returns 401 on the Hub instance even with a valid
    admin token, and a /version-based check declares a working instance dead.
    """
    probes = []
    for path in ("/api/v4/metadata", "/api/v4/user"):
        status, body, _ = api_request(base_url, path, token)
        probes.append({"endpoint": path, "status": status})
        if status == 200 and isinstance(body, dict):
            return True, probes, {
                "version": body.get("version"),
                "user_is_admin": body.get("is_admin"),
                "username": body.get("username"),
            }
    return False, probes, {}


def paginate(base_url, path, token, params, what):
    """Follow X-Next-Page. Any failure mid-stream raises -> UNKNOWN, never 0."""
    items, page, pages = [], "1", 0
    while page:
        p = dict(params)
        p.update({"per_page": str(PER_PAGE), "page": page})
        status, body, headers = api_request(base_url, path, token, p)
        if status != 200:
            snippet = ""
            if isinstance(body, dict):
                snippet = str(body.get("_body", ""))[:200]
            raise OperationalError(
                f"{what}: HTTP {status} on page {page} of {path} — "
                f"result is UNKNOWN, not zero. {snippet}")
        if not isinstance(body, list):
            raise OperationalError(f"{what}: unexpected payload (not a list)")
        items.extend(body)
        pages += 1
        nxt = ""
        for k, v in headers.items():
            if k.lower() == "x-next-page":
                nxt = (v or "").strip()
        page = nxt or None
        if pages > MAX_PAGES:
            raise OperationalError(f"{what}: pagination did not terminate")
    return items, pages


# ---------------------------------------------------------------------------
# identity normalisation — rule 1
# ---------------------------------------------------------------------------
def normalize(url):
    """('host', 'group/sub/project') from any git remote URL, or None.

    Strips embedded credentials, a trailing '.git', trailing slashes, and
    lowercases both halves. NEVER returns a basename.
    """
    if not url:
        return None
    u = url.strip()
    host = path = ""
    if "://" in u:
        parsed = urllib.parse.urlsplit(u)
        host = parsed.hostname or ""
        path = parsed.path or ""
    else:
        m = re.match(r"^(?:[\w.+-]+(?::[^@]*)?@)?([^/@:]+):(?!\d+/)(.+)$", u)
        if not m:
            return None
        host, path = m.group(1), m.group(2)
    if not host:
        return None
    path = path.strip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    path = path.strip("/")
    if not path:
        return None
    return (host.lower(), path.lower())


# ---------------------------------------------------------------------------
# local filesystem scan
# ---------------------------------------------------------------------------
def run_git(args, cwd=None):
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo",
                "LC_ALL": "C"})
    try:
        return subprocess.run(["git", *args], cwd=cwd, env=env,  # noqa: S607 - git resolved from PATH by design
                              capture_output=True, text=True,
                              timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None


def git_common_dir(repo_dir):
    """The real config-bearing gitdir.

    `git rev-parse --absolute-git-dir` inside a linked worktree returns
    .git/worktrees/<name>, which holds NO config file — a hygiene sweep keyed
    on that silently checks nothing. --git-common-dir is the correct one.
    """
    r = run_git(["-C", repo_dir, "rev-parse", "--git-common-dir"])
    if r and r.returncode == 0 and r.stdout.strip():
        d = r.stdout.strip()
        return d if os.path.isabs(d) else os.path.normpath(os.path.join(repo_dir, d))
    cand = os.path.join(repo_dir, ".git")
    return cand if os.path.isdir(cand) else None


def read_origin(repo_dir, gitdir):
    r = run_git(["-C", repo_dir, "config", "--get", "remote.origin.url"])
    if r and r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    cfg = os.path.join(gitdir or os.path.join(repo_dir, ".git"), "config")
    try:
        with open(cfg, "r", encoding="utf-8", errors="replace") as fh:
            in_origin = False
            for line in fh:
                s = line.strip()
                if s.startswith("["):
                    in_origin = s.replace(" ", "").lower().startswith('[remote"origin"]')
                    continue
                if in_origin and s.lower().startswith("url"):
                    return s.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def inspect_config(gitdir):
    """-> dict(config_path, mode, mode_ok, credential_findings[])."""
    out = {"config_path": None, "mode": None, "mode_ok": None,
           "credential_findings": []}
    if not gitdir:
        return out
    cfg = os.path.join(gitdir, "config")
    out["config_path"] = cfg
    try:
        st = os.stat(cfg)
    except OSError:
        return out
    mode = stat.S_IMODE(st.st_mode)
    out["mode"] = oct(mode)
    # 0600 is the requirement; anything readable/writable by group or other
    # (or executable) fails.
    out["mode_ok"] = (mode == 0o600)
    try:
        with open(cfg, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return out
    for name, pat in CRED_PATTERNS:
        for m in pat.finditer(text):
            # Report the KIND and the line number only — never the value.
            line_no = text.count("\n", 0, m.start()) + 1
            out["credential_findings"].append({"kind": name, "line": line_no})
    return out


def repo_health(repo_dir, gitdir):
    """-> dict(head_ok, head, unborn, clone_completed, shallow, valid)."""
    info = {"head_ok": False, "unborn": False, "clone_completed": None,
            "shallow": False, "valid": False, "detail": ""}
    if gitdir:
        info["shallow"] = os.path.isfile(os.path.join(gitdir, "shallow"))
        cfg = os.path.join(gitdir, "config")
        try:
            with open(cfg, "r", encoding="utf-8", errors="replace") as fh:
                # `git clone` writes the [branch "<name>"] stanza only at the
                # very end of a successful run: the completed-clone marker.
                info["clone_completed"] = bool(
                    re.search(r'^\s*\[branch\s+"', fh.read(), re.MULTILINE))
        except OSError:
            info["clone_completed"] = None
    r = run_git(["-C", repo_dir, "rev-parse", "--verify", "HEAD"])
    if r and r.returncode == 0 and r.stdout.strip():
        info["head_ok"] = True
        info["valid"] = True
        return info
    # No HEAD. An UNBORN head is NOT a broken clone: a clone of a server-side
    # empty project legitimately has zero objects and zero refs. Only the
    # remote can discriminate, so we mark it unborn and let the caller decide
    # using the API's empty_repo flag.
    r2 = run_git(["-C", repo_dir, "rev-parse", "--is-inside-work-tree"])
    if r2 and r2.returncode == 0 and r2.stdout.strip() == "true":
        info["unborn"] = True
        info["valid"] = True
        info["detail"] = "unborn HEAD (no commits)"
        return info
    info["detail"] = "not a usable git repository"
    return info


def scan_tree(dest, skip_vendor=False, probe_health=True):
    """Every git work tree under dest.

    Rule 7: we do NOT prune at the first .git. Nested checkouts are real here
    and a credential sweep that misses them is worse than none. Depth is
    recorded so nested trees can be separated afterwards.
    """
    repos = []
    if not os.path.isdir(dest):
        raise OperationalError(f"destination directory does not exist: {dest}")
    dest_abs = os.path.abspath(dest)
    dirs_walked = 0
    for root, dirs, _files in os.walk(dest_abs, followlinks=False):
        dirs_walked += 1
        skip = set(ALWAYS_SKIP) | (VENDOR_SKIP if skip_vendor else set())
        dirs[:] = [d for d in dirs if d not in skip]
        marker = os.path.join(root, ".git")
        if not (os.path.isdir(marker) or os.path.isfile(marker)):
            continue
        gitdir = git_common_dir(root)
        url = read_origin(root, gitdir)
        norm = normalize(url)
        rel = os.path.relpath(root, dest_abs)
        rec = {
            "path": root,
            "relpath": rel,
            "depth": 0 if rel == "." else rel.count(os.sep) + 1,
            "gitdir": gitdir,
            "origin_present": bool(url),
            "host": norm[0] if norm else None,
            "path_with_namespace": norm[1] if norm else None,
        }
        rec.update(inspect_config(gitdir))
        rec["health"] = repo_health(root, gitdir) if probe_health else {}
        repos.append(rec)
    return repos, dirs_walked


# ---------------------------------------------------------------------------
# remote enumeration
# ---------------------------------------------------------------------------
def enumerate_projects(base_url, token):
    """Every project visible to the token, via /api/v4/projects."""
    raw, pages = paginate(base_url, "/api/v4/projects", token,
                          {"simple": "false", "order_by": "id", "sort": "asc",
                           "membership": "false", "statistics": "false"},
                          "project enumeration")
    projects, seen = [], set()
    for p in raw:
        pid = p.get("id")
        if pid in seen:
            continue
        seen.add(pid)
        ns = p.get("namespace") or {}
        projects.append({
            "id": pid,
            "path_with_namespace": (p.get("path_with_namespace") or "").strip("/"),
            "namespace_kind": ns.get("kind"),
            "namespace_full_path": ns.get("full_path"),
            "default_branch": p.get("default_branch"),
            "empty_repo": bool(p.get("empty_repo")),
            "archived": bool(p.get("archived")),
        })
    return projects, pages


def enumerate_via_groups(base_url, token):
    """Cross-check enumeration through top-level groups.

    include_subgroups=true is MANDATORY: a group whose projects all live in
    subgroups returns an empty list without it (verified: group 35 -> 0 vs 22).
    """
    groups, _ = paginate(base_url, "/api/v4/groups", token,
                         {"top_level_only": "true", "all_available": "true",
                          "order_by": "id", "sort": "asc"},
                         "group enumeration")
    paths, per_group = set(), {}
    for g in groups:
        gid = g.get("id")
        projs, _ = paginate(base_url, f"/api/v4/groups/{gid}/projects", token,
                            {"include_subgroups": "true", "order_by": "id",
                             "sort": "asc"},
                            f"group {gid} project listing")
        names = {(p.get("path_with_namespace") or "").strip("/").lower()
                 for p in projs if p.get("path_with_namespace")}
        per_group[str(gid)] = {"full_path": g.get("full_path"),
                               "projects": len(names)}
        paths |= names
    return paths, per_group


def probe_rename(base_url, token, old_path):
    """GitLab follows its own rename redirect.

    GET /api/v4/projects/:url_encoded_old_path returns 200 with the CURRENT
    path for a renamed project. Probing this before reporting a missing or
    orphan count is mandatory: a prior audit reported 212 missing when 16 of
    them were renames already on disk.

    -> dict(status, verdict in {renamed, same_path, deleted, unknown},
            current_path)
    """
    enc = urllib.parse.quote(old_path, safe="")
    status, body, _ = api_request(base_url, f"/api/v4/projects/{enc}", token)
    if status == 200 and isinstance(body, dict):
        cur = (body.get("path_with_namespace") or "").strip("/")
        return {"status": status,
                "verdict": "renamed" if cur.lower() != old_path.lower() else "same_path",
                "current_path": cur, "project_id": body.get("id")}
    if status == 404:
        return {"status": status, "verdict": "deleted", "current_path": None}
    return {"status": status, "verdict": "unknown", "current_path": None}


# ---------------------------------------------------------------------------
# per-instance verification
# ---------------------------------------------------------------------------
def verify_instance(inst, tokens, all_local, args):
    name = inst.get("name") or "?"
    url = (inst.get("url") or "").rstrip("/")
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    token_var = inst.get("token_env") or ""
    res = {
        "name": name, "url": url, "host": host, "token_var": token_var,
        "status": VERDICT_UNKNOWN, "severity": "unknown",
        "unknown_reasons": [],
        "counts": {}, "missing": [], "excluded_by_policy": [],
        "orphans": [], "duplicates": [], "shallow": [], "broken": [],
        "credential_leaks": [], "bad_mode": [],
        "findings_by_category": {"correctness": {}, "security": {},
                                 "hygiene": {}},
    }
    token = tokens.get(token_var)
    if not token:
        res["unknown_reasons"].append(
            f"token variable {token_var} not present in {ENV_FILE} or environment")
        return res
    if not host:
        res["unknown_reasons"].append(f"instance url is unusable: {url!r}")
        return res

    try:
        alive, probes, meta = check_liveness(url, token)
        res["liveness_probes"] = probes
        if not alive:
            res["unknown_reasons"].append(
                "instance did not answer /api/v4/metadata or /api/v4/user "
                f"(statuses: {[p['status'] for p in probes]}) — "
                "completeness is UNKNOWN, not zero")
            return res
        res["server"] = meta
        projects, pages = enumerate_projects(url, token)
        res["api_pages_followed"] = pages
        # THE SILENT-EMPTY-LIST GUARD. A paginated fetch that broke without
        # raising returned [] on this codebase, so a bad token yielded zero
        # projects, no exception and no warning — indistinguishable from an
        # instance that genuinely has no projects. An authenticated instance
        # with zero visible projects is therefore UNDETERMINED, not "nothing
        # to mirror".
        #
        # Gated on the group cross-check being enabled: "the instance has no
        # projects" is a claim from ONE enumeration path, and the guard's job
        # is to refuse to believe it without the second path. With
        # --no-group-crosscheck the operator has switched the corroborating
        # source off, so the guard has nothing to corroborate against and
        # stays silent. --allow-empty-instance opts out for an instance that
        # really is empty.
        if (not projects and not args.no_group_crosscheck
                and not getattr(args, "allow_empty_instance", False)):
            res["unknown_reasons"].append(
                "the project listing returned ZERO projects on an instance "
                "that answered liveness — this is the silent-empty-list "
                "failure shape (a broken fetch and a bad token both look like "
                "this). Pass --allow-empty-instance if the instance really is "
                "empty.")
        group_paths = None
        if not args.no_group_crosscheck:
            group_paths, per_group = enumerate_via_groups(url, token)
            res["group_crosscheck"] = {
                "groups_listed": len(per_group),
                "projects_via_groups": len(group_paths),
                "include_subgroups": True,
            }
    except OperationalError as exc:
        res["unknown_reasons"].append(str(exc))
        return res

    remote = {}
    for p in projects:
        pwn = p["path_with_namespace"]
        if pwn:
            remote[(host, pwn.lower())] = p

    # local repos belonging to THIS host only (one tree, two hosts)
    local_index = {}
    mine = []
    for r in all_local:
        if r["host"] == host:
            mine.append(r)
            local_index.setdefault((r["host"], r["path_with_namespace"]), []).append(r)

    if group_paths is not None:
        flat_group = {k[1] for k, p in remote.items() if p["namespace_kind"] == "group"}
        only_flat = sorted(flat_group - group_paths)
        only_group = sorted(group_paths - flat_group)
        res["group_crosscheck"].update({
            "flat_group_namespace_projects": len(flat_group),
            "only_in_flat_listing": only_flat[:20],
            "only_in_group_listing": only_group[:20],
            "agrees": not only_flat and not only_group,
        })
        # The cross-check used to be computed and then ignored — it printed a
        # disagreement and still allowed COMPLETE_AND_CLEAN. Two enumeration
        # paths that disagree means one of them is truncating, so the
        # headline number is not trustworthy: that is OPERATIONAL, not a
        # finding about the mirror.
        if only_group:
            res["unknown_reasons"].append(
                f"group listing (include_subgroups=true) sees "
                f"{len(only_group)} project(s) the flat /projects listing does "
                f"not: {only_group[:5]} — one enumeration path is truncating, "
                f"so the remote total cannot be trusted")
        if only_flat:
            res["unknown_reasons"].append(
                f"flat /projects listing sees {len(only_flat)} group-namespace "
                f"project(s) the group listing does not: {only_flat[:5]} — the "
                f"two enumeration paths disagree")

    # ---- present / missing / policy-excluded -----------------------------
    present, missing, excluded = [], [], []
    for key, p in sorted(remote.items(), key=lambda kv: kv[0][1]):
        if key in local_index:
            present.append({**p, "local_paths": [r["path"] for r in local_index[key]]})
            continue
        if args.exclude_user_namespace and p["namespace_kind"] == "user":
            excluded.append({**p, "policy": "user_namespace_not_mirrored"})
            continue
        if p["archived"] and args.exclude_archived:
            excluded.append({**p, "policy": "archived"})
            continue
        missing.append(dict(p))

    # ---- orphans + MANDATORY rename probe --------------------------------
    orphans = []
    for key, recs in sorted(local_index.items()):
        if key in remote:
            continue
        old = key[1]
        try:
            probe = probe_rename(url, token, old)
        except OperationalError as exc:
            probe = {"status": None, "verdict": "unknown",
                     "current_path": None, "error": str(exc)}
        entry = {"host": key[0], "local_path_with_namespace": old,
                 "local_paths": [r["path"] for r in recs],
                 "rename_probe": probe}
        cur = (probe.get("current_path") or "").lower()
        if probe["verdict"] == "renamed" and cur:
            entry["classification"] = "renamed_upstream"
            entry["current_path_with_namespace"] = probe["current_path"]
            # The rename lesson: this project IS on disk. Remove it from the
            # missing list so the headline count is not inflated.
            newkey = (host, cur)
            before = len(missing)
            missing = [m for m in missing
                       if m["path_with_namespace"].lower() != cur]
            entry["removed_from_missing"] = (len(missing) != before)
            if newkey in remote:
                # Only add it if the NEW path is not already matched on disk.
                # Both copies commonly exist (fresh clone at the new path plus
                # the stale one), and appending unconditionally inflated
                # `present` above the remote total — the same class of
                # double-count the rename probe exists to prevent.
                already = any(pr.get("id") == remote[newkey]["id"]
                              for pr in present)
                entry["new_path_also_on_disk"] = already
                if not already:
                    present.append(
                        {**remote[newkey],
                         "local_paths": [r["path"] for r in recs],
                         "note": "present via stale origin (renamed upstream)"})
        elif probe["verdict"] == "deleted":
            entry["classification"] = "deleted_upstream"
        elif probe["verdict"] == "same_path":
            # 200 but same path => visible to the token yet absent from the
            # listing. Honest answer: we do not know why.
            entry["classification"] = "unknown_listing_mismatch"
            res["unknown_reasons"].append(
                f"orphan {old} resolves 200 at its own path but was not in the "
                "project listing")
        else:
            entry["classification"] = "unknown"
            res["unknown_reasons"].append(
                f"rename probe for orphan {old} returned "
                f"status {probe.get('status')} — classification UNKNOWN")
        orphans.append(entry)

    # ---- hygiene / integrity (this host's repos) -------------------------
    for r in mine:
        if r.get("credential_findings"):
            res["credential_leaks"].append({
                "path": r["path"],
                "findings": r["credential_findings"]})
        if r.get("mode_ok") is False:
            res["bad_mode"].append({"path": r["path"], "mode": r.get("mode")})
        h = r.get("health") or {}
        if h.get("shallow"):
            res["shallow"].append(r["path"])
        if h.get("valid") is False:
            res["broken"].append({"path": r["path"], "detail": h.get("detail")})
        elif h.get("unborn"):
            key = (r["host"], r["path_with_namespace"])
            p = remote.get(key)
            if p is not None and not p["empty_repo"]:
                res["broken"].append({
                    "path": r["path"],
                    "detail": "unborn HEAD but remote reports empty_repo=false"})

    for key, recs in sorted(local_index.items()):
        if len(recs) > 1:
            res["duplicates"].append({
                "identity": f"{key[0]}/{key[1]}",
                "paths": [r["path"] for r in recs]})

    no_origin = [r["path"] for r in all_local if not r["host"]]

    res["missing"] = missing
    res["excluded_by_policy"] = excluded
    res["orphans"] = orphans
    res["counts"] = {
        "remote_projects": len(projects),
        "remote_unique_paths": len(remote),
        "remote_group_namespace": sum(1 for p in projects if p["namespace_kind"] == "group"),
        "remote_user_namespace": sum(1 for p in projects if p["namespace_kind"] == "user"),
        "remote_archived": sum(1 for p in projects if p["archived"]),
        "remote_empty": sum(1 for p in projects if p["empty_repo"]),
        "local_worktrees_this_host": len(mine),
        "present": len(present),
        "missing": len(missing),
        "excluded_by_policy": len(excluded),
        "orphans": len(orphans),
        "orphans_renamed": sum(1 for o in orphans
                               if o["classification"] == "renamed_upstream"),
        "orphans_deleted": sum(1 for o in orphans
                               if o["classification"] == "deleted_upstream"),
        "orphans_unknown": sum(1 for o in orphans
                               if o["classification"].startswith("unknown")),
        "duplicate_identities": len(res["duplicates"]),
        "shallow": len(res["shallow"]),
        "broken": len(res["broken"]),
        "credential_leaks": len(res["credential_leaks"]),
        "config_mode_not_0600": len(res["bad_mode"]),
        "local_worktrees_without_origin": len(no_origin),
    }

    # ---- CLASSIFICATION (W9-7) -------------------------------------------
    # Every finding is sorted into exactly one category, and each category
    # maps to one exit code. The reasoning for each placement:
    #
    # CORRECTNESS (exit 1) — the server holds data this mirror does not:
    #   missing   : the definitive failure. The server has a project, the
    #               mirror does not. This is the only job the tool has.
    #   broken    : a directory that is not a usable git repository holds no
    #               mirrored data. Arguable — a user could call it debris and
    #               want it merely tidied — but it occupies the path where
    #               the project is supposed to be, so `present` counts it
    #               while nothing is actually retrievable from it. Silently
    #               present-but-empty is worse than visibly absent, so it is
    #               ranked with `missing`.
    #   shallow   : history the server has is not on disk. A shallow clone
    #               satisfies "the directory exists" and fails "the mirror
    #               has the repository". Same reasoning as broken.
    #
    # SECURITY (exit 3) — the mirror is complete, but:
    #   credential_leaks : a token sitting in a .git/config on disk. Nothing
    #               is missing and nothing is untidy; this is a different
    #               axis and must not be diluted by a hygiene count that is
    #               routinely in the hundreds.
    #
    # HYGIENE (exit 4) — complete, retrievable, untidy:
    #   bad_mode  : .git/config not 0600. The repository is perfect. 413 of
    #               these once pinned the referee to exit 1 forever.
    #   duplicates: the content IS on disk — twice. Wasteful, not incorrect.
    #   orphans_deleted : a clone of a project the server no longer has. The
    #               mirror is complete with respect to what exists; whether
    #               to retain a deleted project's last copy is a retention
    #               decision, not a completeness failure. Deliberately NOT
    #               correctness: calling it one would make the referee demand
    #               deletion of the only surviving copy of deleted work.
    #
    # OPERATIONAL (exit 2) is not computed here — it is `unknown_reasons`,
    # accumulated wherever a determination could not be made, and it wins
    # over every category above.
    correctness = {
        "missing": len(missing),
        "broken": len(res["broken"]),
        "shallow": len(res["shallow"]),
    }
    security = {"credential_leaks": len(res["credential_leaks"])}
    hygiene = {
        "config_mode_not_0600": len(res["bad_mode"]),
        "duplicate_identities": len(res["duplicates"]),
        "orphans_deleted_upstream": res["counts"]["orphans_deleted"],
    }
    res["findings_by_category"] = {
        "correctness": correctness,
        "security": security,
        "hygiene": hygiene,
    }

    # Accounting invariant. If present + missing + excluded does not equal the
    # remote total, some project has been counted twice or lost, and the
    # headline number cannot be trusted. Say so rather than print it.
    tally = len(present) + len(missing) + len(excluded)
    res["accounting"] = {
        "present_plus_missing_plus_excluded": tally,
        "remote_unique_paths": len(remote),
        "balances": tally == len(remote),
    }
    if tally != len(remote):
        res["unknown_reasons"].append(
            f"accounting does not balance: present {len(present)} + missing "
            f"{len(missing)} + excluded {len(excluded)} = {tally}, but the "
            f"remote has {len(remote)} unique paths")

    if res["unknown_reasons"]:
        res["severity"] = "unknown"
    elif any(correctness.values()):
        res["severity"] = "incomplete"
    elif any(security.values()):
        res["severity"] = "insecure"
    elif any(hygiene.values()):
        res["severity"] = "hygiene"
    else:
        res["severity"] = "clean"
    # `status` is a PRE-EXISTING field contract with three values, consumed by
    # the offline regression suite and by anything reading the JSON artifact.
    # W9-7 is about making the EXIT CODE legible, not about renaming a field,
    # so the three-value field keeps its meaning and the new granularity
    # lands in `severity` + `verdict_detail`. Widening `status` in place would
    # silently break every existing reader for no gain.
    res["status"] = {
        "unknown": VERDICT_UNKNOWN,
        "incomplete": VERDICT_DIRTY,
        "insecure": VERDICT_DIRTY,
        "hygiene": VERDICT_DIRTY,
        "clean": VERDICT_CLEAN,
    }[res["severity"]]
    res["verdict_detail"] = VERDICT_BY_SEVERITY[res["severity"]]
    res["exit_code"] = SEVERITY_EXIT[res["severity"]]
    res["exit_category"] = EXIT_CATEGORY[res["exit_code"]]
    return res


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def print_instance(res, verbose):
    say("")
    say("=" * 78)
    say(f"INSTANCE  {res['name']}  ({res['url'] or '?'})   ->  "
        f"{res.get('verdict_detail') or res['status']}")
    say("=" * 78)
    if res["status"] == VERDICT_UNKNOWN and not res["counts"]:
        for r in res["unknown_reasons"]:
            say(f"  OPERATIONAL/UNKNOWN: {r}")
        say("  No completeness number is reported. UNKNOWN is not zero and is "
            "not success.")
        return
    c = res["counts"]
    srv = res.get("server") or {}
    if srv.get("version"):
        say(f"  server version : {srv['version']}"
            + (f"   token user: {srv.get('username')}"
               f" (admin={srv.get('user_is_admin')})" if srv.get("username") else ""))
    gx = res.get("group_crosscheck")
    if gx:
        say(f"  group listing  : {gx['groups_listed']} top-level groups, "
            f"{gx['projects_via_groups']} projects with include_subgroups=true "
            f"(agrees with flat listing: {gx.get('agrees')})")
    say(f"  remote projects: {c['remote_projects']} "
        f"(group-ns {c['remote_group_namespace']}, user-ns {c['remote_user_namespace']}, "
        f"archived {c['remote_archived']}, empty {c['remote_empty']})")
    say(f"  on disk        : {c['present']} present / "
        f"{c['local_worktrees_this_host']} work trees carrying this host's origin")
    say(f"  MISSING        : {c['missing']}")
    say(f"  excluded (policy): {c['excluded_by_policy']}")
    say(f"  orphans        : {c['orphans']} "
        f"(renamed upstream {c['orphans_renamed']}, deleted {c['orphans_deleted']}, "
        f"unknown {c['orphans_unknown']})")
    say(f"  hygiene        : credential leaks {c['credential_leaks']}, "
        f".git/config not 0600 {c['config_mode_not_0600']}")
    say(f"  integrity      : shallow {c['shallow']}, broken {c['broken']}, "
        f"duplicate identities {c['duplicate_identities']}")
    acc = res.get("accounting")
    if acc:
        say(f"  accounting     : present+missing+excluded = "
            f"{acc['present_plus_missing_plus_excluded']} vs remote "
            f"{acc['remote_unique_paths']}  -> "
            f"{'balances' if acc['balances'] else 'DOES NOT BALANCE'}")

    if res["missing"]:
        say(f"\n  --- MISSING ({len(res['missing'])}) ---")
        for m in res["missing"][:None if verbose else 40]:
            say(f"    {m['path_with_namespace']}  (id={m['id']}, "
                f"ns={m['namespace_kind']}, archived={m['archived']}, "
                f"empty={m['empty_repo']})")
        if not verbose and len(res["missing"]) > 40:
            say(f"    ... {len(res['missing']) - 40} more (use --verbose or the JSON)")
    if res["excluded_by_policy"]:
        say(f"\n  --- excluded by policy ({len(res['excluded_by_policy'])}) ---")
        pol = {}
        for e in res["excluded_by_policy"]:
            pol.setdefault(e["policy"], []).append(e["path_with_namespace"])
        for k, v in pol.items():
            say(f"    {k}: {len(v)}")
            for p in (v if verbose else v[:10]):
                say(f"      {p}")
            if not verbose and len(v) > 10:
                say(f"      ... {len(v) - 10} more")
    if res["orphans"]:
        say(f"\n  --- orphans ({len(res['orphans'])}) ---")
        for o in res["orphans"][:None if verbose else 40]:
            extra = ""
            if o["classification"] == "renamed_upstream":
                extra = (f" -> RENAMED upstream to "
                         f"{o['current_path_with_namespace']}"
                         f"{' (removed from missing)' if o.get('removed_from_missing') else ''}")
            say(f"    {o['local_path_with_namespace']} "
                f"[{o['classification']}, probe HTTP {o['rename_probe'].get('status')}]{extra}")
            say(f"      at {o['local_paths'][0]}")
    for label, items in (("credential leaks", res["credential_leaks"]),
                         (".git/config not mode 0600", res["bad_mode"]),
                         ("broken repositories", res["broken"]),
                         ("duplicate identities", res["duplicates"])):
        if items:
            say(f"\n  --- {label} ({len(items)}) ---")
            for it in items[:None if verbose else 25]:
                say(f"    {json.dumps(REDACT(it), ensure_ascii=False)}")
            if not verbose and len(items) > 25:
                say(f"    ... {len(items) - 25} more")
    if res["shallow"]:
        say(f"\n  --- shallow clones ({len(res['shallow'])}) ---")
        for p in res["shallow"][:None if verbose else 25]:
            say(f"    {p}")
    if res["unknown_reasons"]:
        say(f"\n  --- OPERATIONAL / UNKNOWN ({len(res['unknown_reasons'])}) ---")
        for r in res["unknown_reasons"][:None if verbose else 25]:
            say(f"    {r}")

    # Name the category this instance lands in, so the exit code a caller
    # sees is explainable from the same output.
    fb = res.get("findings_by_category") or {}
    sev = res.get("severity", "unknown")
    if res["counts"]:
        def _nz(d):
            return ", ".join(f"{k} {v}" for k, v in (d or {}).items() if v) or "none"
        say("")
        say(f"  CORRECTNESS (exit {EXIT_INCOMPLETE}) : {_nz(fb.get('correctness'))}")
        say(f"  SECURITY    (exit {EXIT_INSECURE}) : {_nz(fb.get('security'))}")
        say(f"  HYGIENE     (exit {EXIT_HYGIENE}) : {_nz(fb.get('hygiene'))}")
        say(f"  OPERATIONAL (exit {EXIT_ERROR}) : "
            f"{len(res['unknown_reasons'])} reason(s)")
        say(f"  -> category for {res['name']}: "
            f"{EXIT_CATEGORY[SEVERITY_EXIT[sev]]} "
            f"({res.get('verdict_detail') or res['status']})")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(
        prog="verify_mirror.py",
        description="Independent, stdlib-only completeness + hygiene referee "
                    "for every configured GitLab instance.")
    ap.add_argument("--dest", default=None,
                    help="clone root (default: <install root>/<master_dir> "
                         "from instances.json)")
    ap.add_argument("--config", default=INSTANCES_FILE)
    ap.add_argument("--env-file", default=ENV_FILE)
    ap.add_argument("--instance", action="append", default=None,
                    help="limit to an instance by name (repeatable)")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="write the machine-readable artifact here "
                         "(default: $TMPDIR/gitlab-cli-verify/verify-mirror.json)")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--skip-vendor", action="store_true",
                    help="do not walk node_modules/venv (faster, but a nested "
                         "checkout in there is then NOT swept)")
    ap.add_argument("--no-group-crosscheck", action="store_true",
                    help="skip the include_subgroups=true group enumeration "
                         "cross-check")
    ap.add_argument("--include-user-namespace", dest="exclude_user_namespace",
                    action="store_false", default=True,
                    help="treat user-namespace projects as required "
                         "(default: excluded by policy)")
    ap.add_argument("--exclude-archived", action="store_true",
                    help="treat archived projects as excluded by policy "
                         "(default: required)")
    ap.add_argument("--legacy-exit-codes", action="store_true",
                    help="collapse exit 3 (credential on disk) and exit 4 "
                         "(untidy) back into exit 1, for a consumer that only "
                         "understands 0/1/2. The printed verdict still names "
                         "the real category.")
    ap.add_argument("--allow-empty-instance", action="store_true",
                    help="accept an instance whose project listing returns "
                         "zero projects. Off by default: a silently broken "
                         "fetch and a bad token both look exactly like an "
                         "empty instance, and that failure has happened here.")
    return ap


def operational_exit(detail, extra=None):
    """Print an OPERATIONAL verdict and return exit 2.

    Every pre-flight failure in main() funnels through here so that a bad
    config, an absent token file and a missing clone root all produce the
    identical, legible shape — and none of them can be mistaken for a clean
    mirror. Exit 2 is deliberately NOT affected by --legacy-exit-codes: an
    undetermined result must look the same to every consumer.
    """
    say("")
    say("-" * 78)
    say(f"VERDICT: {VERDICT_UNKNOWN}   (exit {EXIT_ERROR} = "
        f"{EXIT_CATEGORY[EXIT_ERROR]}: {EXIT_BLURB[EXIT_ERROR]})")
    say(f"  operational error: {detail}")
    if extra:
        say(f"  {extra}")
    say("  This is NOT a failed check — it is an UNDETERMINED one. "
        "Completeness was not measured, so it is not zero and not success.")
    say(f"  {EXIT_LEGEND}")
    return EXIT_ERROR


def main(argv=None):
    args = build_parser().parse_args(argv)
    started = datetime.datetime.now(datetime.timezone.utc)

    try:
        instances, active = read_instances(args.config)
        tokens = read_env_tokens(args.env_file)
    except OperationalError as exc:
        return operational_exit(exc)

    if args.instance:
        wanted = {n.lower() for n in args.instance}
        instances = [i for i in instances
                     if (i.get("name") or "").lower() in wanted]
        if not instances:
            return operational_exit(
                f"no configured instance matches {args.instance}")

    master_dirs = {i.get("master_dir") or "MasterGroups" for i in instances}
    if args.dest:
        dest = os.path.abspath(os.path.expanduser(args.dest))
    else:
        md = sorted(master_dirs)[0]
        # Resolve relative to the INSTALL ROOT, never the process cwd: a
        # launcher invoked by absolute path without cd-ing would otherwise
        # audit a different tree than the one the tool clones into.
        dest = md if os.path.isabs(md) else os.path.normpath(
            os.path.join(INSTALL_ROOT, md))
        if len(master_dirs) > 1:
            warn(f"[warn] instances disagree on master_dir {sorted(master_dirs)}; "
                 f"auditing {dest}")

    say("gitlab-cli mirror verification — independent referee "
        "(stdlib only, imports nothing from the CLI)")
    say(f"  run (UTC)   : {started.isoformat(timespec='seconds')}")
    say(f"  python      : {sys.executable} ({sys.version.split()[0]})")
    say(f"  config      : {args.config}   active instance: {active}")
    say(f"  clone root  : {dest}")
    em = env_file_mode(args.env_file)
    say(f"  token file  : {args.env_file} mode "
        f"{oct(em) if em is not None else 'ABSENT'}"
        f"{'' if em == 0o600 else '   <-- WARNING: expected 0600'}")

    try:
        local, dirs_walked = scan_tree(dest, skip_vendor=args.skip_vendor)
    except OperationalError as exc:
        return operational_exit(
            exc, "The clone root could not be scanned, so nothing on disk was "
                 "compared against the server.")

    with_origin = [r for r in local if r["host"]]
    nested = [r for r in local if r["depth"] > 1 and any(
        o is not r and r["path"].startswith(o["path"] + os.sep) for o in local)]
    say(f"  local scan  : {len(local)} git work trees "
        f"({len(with_origin)} with a parsable origin, {len(nested)} nested), "
        f"{dirs_walked} directories walked"
        + ("  [vendor dirs skipped]" if args.skip_vendor else ""))

    results = [verify_instance(i, tokens, local, args) for i in instances]
    for r in results:
        print_instance(r, args.verbose)

    # ---- fleet verdict ---------------------------------------------------
    # Worst severity across the fleet wins, ranked by SEVERITY_ORDER and not
    # by numeric code: an instance we could not determine outranks one with a
    # definite finding. The whole point of W9-7 is that the number a caller
    # gets names WHICH kind of problem it has, so the fleet may not collapse
    # four distinct states into one.
    severities = [r.get("severity", "unknown") for r in results]
    worst = min(severities, key=lambda s: SEVERITY_ORDER.index(s))
    verdict = VERDICT_BY_SEVERITY[worst]
    code = SEVERITY_EXIT[worst]
    true_code = code
    legacy_applied = False
    if args.legacy_exit_codes and code in (EXIT_INSECURE, EXIT_HYGIENE):
        # For a consumer that only knows 0/1/2. The VERDICT text below still
        # names the real category, so the collapse never hides what fired.
        code = EXIT_INCOMPLETE
        legacy_applied = True

    finished = datetime.datetime.now(datetime.timezone.utc)
    report = {
        "tool": "verify_mirror.py",
        "independent_of_gitlab_cli": True,
        "imports_from_cli": [],
        "python": sys.version.split()[0],
        "started_utc": started.isoformat(timespec="seconds"),
        "finished_utc": finished.isoformat(timespec="seconds"),
        "clone_root": dest,
        "config": args.config,
        "policy": {
            "exclude_user_namespace": args.exclude_user_namespace,
            "exclude_archived": args.exclude_archived,
            "walk_prunes_at_dot_git": False,
            "vendor_dirs_skipped": args.skip_vendor,
            "identity": "normalised (host, path_with_namespace), never basename",
        },
        "local_scan": {
            "work_trees": len(local),
            "with_origin": len(with_origin),
            "without_origin": len(local) - len(with_origin),
            "nested": len(nested),
            "directories_walked": dirs_walked,
        },
        "verdict": verdict,
        "severity": worst,
        "exit_code": code,
        "exit_category": EXIT_CATEGORY[code],
        "exit_category_true": EXIT_CATEGORY[true_code],
        "exit_code_true": true_code,
        "legacy_exit_codes": bool(args.legacy_exit_codes),
        "exit_code_table": {str(k): EXIT_CATEGORY[k] for k in
                            (EXIT_OK, EXIT_INCOMPLETE, EXIT_ERROR,
                             EXIT_INSECURE, EXIT_HYGIENE)},
        "instances": results,
    }
    out = args.json_out
    if not out:
        d = os.path.join(os.environ.get("TMPDIR", "/tmp"), "gitlab-cli-verify")  # noqa: S108 - default output dir
        try:
            os.makedirs(d, exist_ok=True)
            out = os.path.join(d, "verify-mirror.json")
        except OSError:
            out = None
    if out:
        try:
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(REDACT(report), fh, indent=2, ensure_ascii=False)
            os.chmod(out, 0o600)
        except OSError as exc:
            warn(f"[warn] could not write JSON artifact: {exc}")
            out = None

    say("")
    say("-" * 78)
    for r in results:
        c = r["counts"]
        cat = EXIT_CATEGORY[SEVERITY_EXIT[r.get("severity", "unknown")]]
        label = r.get("verdict_detail") or r["status"]
        if c:
            say(f"  {r['name']:<12} {label:<32} [{cat}] "
                f"remote={c['remote_projects']:>4} present={c['present']:>4} "
                f"missing={c['missing']:>4} excluded={c['excluded_by_policy']:>3} "
                f"orphans={c['orphans']:>3} leaks={c['credential_leaks']:>3}")
        else:
            say(f"  {r['name']:<12} {label:<32} [{cat}] "
                f"(no numbers — see OPERATIONAL reasons above)")
    say("-" * 78)
    # THE VERDICT LINE. It must state the category, not just a number: a user
    # who sees an exit code has to be able to tell what KIND of problem they
    # have without reading this source.
    say(f"VERDICT: {verdict}   (exit {code} = {EXIT_CATEGORY[true_code]}: "
        f"{EXIT_BLURB[true_code]})")
    if legacy_applied:
        say(f"  --legacy-exit-codes: real category is "
            f"{EXIT_CATEGORY[true_code]} (exit {true_code}), reported as "
            f"{code} for a 0/1/2-only consumer.")
    if worst == "unknown":
        say("  At least one instance could not be determined. UNKNOWN is not "
            "zero, is not success, and is not a failed check — it is an "
            "UNDETERMINED one.")
    elif worst == "incomplete":
        say("  The server holds data this mirror does not. This is the "
            "failure the tool exists to catch.")
    elif worst == "insecure":
        say("  Nothing is missing. A credential is sitting in a .git/config "
            "on disk and must be removed.")
    elif worst == "hygiene":
        say("  Nothing is missing and nothing is exposed. The tree is untidy "
            "only — this is deliberately NOT exit 1.")
    say(f"  {EXIT_LEGEND}")
    if out:
        say(f"JSON artifact: {out}  (mode 0600, contains no token)")
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        warn("interrupted — result is UNKNOWN")
        sys.exit(EXIT_ERROR)
    except OperationalError as exc:
        warn(f"VERDICT: {VERDICT_UNKNOWN} — operational error: {exc}")
        sys.exit(EXIT_ERROR)
    except Exception as exc:  # noqa: BLE001
        warn(f"VERDICT: {VERDICT_UNKNOWN} — {type(exc).__name__}: "
             f"{REDACT(str(exc))}")
        sys.exit(EXIT_ERROR)
