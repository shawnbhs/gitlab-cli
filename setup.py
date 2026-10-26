# setup.py — First run + health check
#
# Two jobs:
#   1. ensure_setup()  → if no GitLab is configured yet, run the wizard
#   2. doctor()        → full check: config, token, network, git, credentials,
#                        coverage, credential hygiene, layout, config drift
#
# The real "add an instance" wizard lives in instances.add_instance();
# this module only handles onboarding and diagnostics.
#
# Doctor design rule (learned the hard way):
#   A check that cannot be performed reports "?" / unknown — never "✔".
#   The bug that hid dozens of missing projects for weeks survived precisely because
#   the self-check reported health it had never verified.

import contextlib
import functools
import os
import re
import sys
import time
import shutil
import subprocess

import caps
import credentials as creds
import instances as I
from printer import (
    print_ok, print_warn, print_error, print_info, print_header,
    BOLD, CYAN, GREEN, YELLOW, DIM, RESET, WHITE, RED
)

CONFIG_DIR = creds.CONFIG_DIR


# ─── Onboarding ────────────────────────────────────────────────────────────────

def _welcome():
    print(f"""
{BOLD}{CYAN}  ╭────────────────────────────────────────────╮
  │            GitLab CLI  ·  setup            │
  ╰────────────────────────────────────────────╯{RESET}

  {DIM}No GitLab instance is configured yet.
  A few quick questions and everything will be ready.

  What you need:{RESET}
    {WHITE}·{RESET} GitLab address        {DIM}(for example https://gitlab.com){RESET}
    {WHITE}·{RESET} Personal Access Token {DIM}(scopes: read_api + read_repository; api for writes){RESET}

  {DIM}The token is never echoed to the screen. It is stored in a
  private file ({creds.ENV_FILE}, readable only by you).{RESET}
""")


def first_run() -> bool:
    """True = at least one instance was configured."""
    _welcome()

    data = I._load()
    name = I.add_instance(data)
    if not name:
        print_warn("\n  Cancelled — nothing can be done without a GitLab.\n")
        return False

    # More than one GitLab?
    print(f"\n  {DIM}If you have another GitLab too (work / personal / client),"
          f" add it now.{RESET}")
    while True:
        try:
            more = input(f"  {CYAN}?{RESET} Add another one? "
                         f"{DIM}[y/N]{RESET}: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not more.startswith("y"):
            break
        data = I._load()
        if not I.add_instance(data):
            break

    n = len(I.list_instances())
    print_header("You're ready 🚀", f"{n} GitLab configured")
    print(f"  {DIM}· Menu → Clone       pull your repos down{RESET}")
    print(f"  {DIM}· Menu → Sync        update all of them{RESET}")
    print(f"  {DIM}· Menu → Instances   token / folder / new GitLab{RESET}")
    print(f"  {DIM}· Menu → Doctor      whenever something stops working{RESET}\n")
    return True


def ensure_setup():
    """
    Called before anything else.
    Returns the active instance config, or None if setup has not been done
    or the user cancelled.
    """
    try:
        os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    except OSError as e:
        print_error(f"Could not create {CONFIG_DIR}: {e}")
        return None

    # Legacy tokens that were stored inside instances.json → secure env file
    moved = I.migrate_inline_tokens()
    if moved:
        print_info(f"{moved} token(s) moved from the config into the secure file "
                   f"({creds.ENV_FILE})")

    if not I.list_instances():
        if not first_run():
            return None

    cfg = I.get_active_instance()
    if not cfg:
        # Instances exist but none is active — take the first one
        insts = I.list_instances()
        if insts:
            I.set_active(insts[0]["name"])
            cfg = I.get_active_instance()

    if not cfg:
        print_error("There is no usable instance. "
                    "Run `gitlab-cli setup`.")
        return None

    return cfg


# ─── Doctor · output primitives ────────────────────────────────────────────────
#
# Four states, not two. "unknown" exists so a check that could not run can say
# so out loud instead of quietly looking like a pass.

_STATE_STYLE = {
    "ok":      ("✔", GREEN),
    "warn":    ("!", YELLOW),
    "fail":    ("✖", RED),
    "unknown": ("?", CYAN),
}


def _mark(label: str, state: str, detail: str = "") -> str:
    icon, color = _STATE_STYLE.get(state, _STATE_STYLE["unknown"])
    line = f"  {color}{icon}{RESET}  {label:<34}"
    if detail:
        line += f" {DIM}{detail}{RESET}"
    print(line)
    return state


def _check(label: str, ok: bool, detail: str = "", warn: bool = False):
    """Legacy two-state helper, kept so the existing checks read the same."""
    state = "ok" if ok else ("warn" if warn else "fail")
    _mark(label, state, detail)
    return ok


def _note(text: str):
    print(f"      {DIM}{text}{RESET}")


# ─── Doctor · failure text (one line: catalog title — cause → fix) ────────────
#
# Every failure doctor prints goes through error_catalog, so the screen and the
# error log name a failure the same way. The catalog is imported lazily and
# every helper degrades to a plain last-line summary: doctor is the command a
# user runs when something is already broken, so it must never crash on its
# own explanation layer.

def _explain(message: str):
    """(code, title, cause, fix) for a raw failure text. Never raises."""
    try:
        import error_catalog as _ec
        info = _ec.explain(message or "")
        return info.code, info.title, info.cause, info.fix
    except Exception:
        return ("unknown", "Unknown error", _summary(message, 110),
                "Run it again and read the full message above.")


def _summary(message: str, limit: int = 70) -> str:
    """The most informative single line of `message` (the END, not the head)."""
    try:
        import error_catalog as _ec
        return _ec.summarize(message or "", limit)
    except Exception:
        lines = [l.strip() for l in (message or "").splitlines() if l.strip()]
        return (lines[-1] if lines else "")[:limit]


def _fail_line(message: str, fix: str = None) -> str:
    """'Title — cause → fix'. `fix` overrides the catalog's with a menu path."""
    code, title, cause, cat_fix = _explain(message)
    return f"{title} — {cause} → {fix or cat_fix}"


def _with_fix(text: str) -> str:
    """`text`, plus the catalog fix when the catalog recognises it."""
    code, _title, _cause, fix = _explain(text)
    return text if code == "unknown" else f"{text} → {fix}"


def _lfs_available(git_bin: str) -> bool:
    with contextlib.suppress(Exception):  # git.py unusable: probe the binary directly below
        import git as _g
        fn = getattr(_g, "lfs_available", None)
        if fn is not None:
            return bool(fn())
    try:
        r = subprocess.run([git_bin, "lfs", "version"], capture_output=True,
                           text=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


# GitLab "delayed deletion" renames a project (or a whole group) to
# `<name>-deletion_scheduled-<id>`. Same pattern as coverage.PENDING_DELETION_RE;
# kept here only as the fallback when coverage.is_pending_deletion is absent.
_PENDING_RE = re.compile(r"-deletion_scheduled-\d+(?=/|$)")


def _is_pending_deletion(project) -> bool:
    with contextlib.suppress(Exception):  # coverage hook unusable: use the fields below
        import coverage as _cov
        fn = getattr(_cov, "is_pending_deletion", None)
        if fn is not None:
            return bool(fn(project))
    if isinstance(project, dict):
        if project.get("marked_for_deletion_at") or \
                project.get("marked_for_deletion_on"):
            return True
        project = project.get("path_with_namespace") or ""
    return bool(_PENDING_RE.search(str(project or "")))


# ─── Doctor · system + files ───────────────────────────────────────────────────

def _git_check() -> list[str]:
    """Git checks. Returns the list of problems found."""
    problems = []

    git = shutil.which("git")
    _check("git installed", bool(git), git or "not found")
    if not git:
        problems.append("git is not installed → sudo apt install git")
        return problems

    rc = subprocess.run([git, "--version"], capture_output=True, text=True)
    _check("version", rc.returncode == 0, rc.stdout.strip())

    # git-lfs — without it an LFS repo clones but its checkout dies in the
    # smudge filter, and the project reads as "missing" forever.
    lfs = _lfs_available(git)
    _check("git-lfs installed", lfs,
           "" if lfs else "not found — LFS repos cannot be checked out",
           warn=not lfs)
    if not lfs:
        problems.append(_fail_line("git: 'lfs' is not a git command",
                                   fix="sudo apt install git-lfs && "
                                       "git lfs install"))

    # credential helper — without it every pull asks for the token
    r = subprocess.run([git, "config", "--get", "credential.helper"],
                       capture_output=True, text=True)
    helper = r.stdout.strip()
    ok = bool(helper)
    _check("credential helper", ok,
           helper or "not set — the token is asked for every time", warn=not ok)
    if not ok:
        problems.append("no credential helper → "
                        "git config --global credential.helper store")

    # user.name / user.email — without them you cannot commit
    for key in ("user.name", "user.email"):
        r = subprocess.run([git, "config", "--get", key],
                           capture_output=True, text=True)
        v = r.stdout.strip()
        _check(key, bool(v), v or "not set", warn=not v)
        if not v:
            problems.append(f"{key} is missing → git config --global {key} \"...\"")

    return problems


def _perm_check() -> list[str]:
    """
    The tool's own config directory and every file in it.

    Not just the two files we happen to know about: anything the CLI drops in
    ~/.gitlab-cli (error logs, caps caches, stray backups) sits next to the
    token and is checked the same way.
    """
    problems = []

    # The directory itself
    if not os.path.isdir(CONFIG_DIR):
        _mark("config dir", "warn", f"{CONFIG_DIR} does not exist yet")
    else:
        try:
            dmode = os.stat(CONFIG_DIR).st_mode & 0o777
        except OSError as e:
            _mark("config dir", "unknown", f"cannot stat: {e.__class__.__name__}")
            problems.append(f"{CONFIG_DIR} could not be checked — permissions unknown")
            dmode = None
        if dmode is not None:
            ok = not (dmode & 0o077)
            _mark("config dir", "ok" if ok else "fail",
                  f"{CONFIG_DIR}  mode {oct(dmode)[2:]}")
            if not ok:
                problems.append(f"{CONFIG_DIR} is readable by others → "
                                f"chmod 700 {CONFIG_DIR}")

    # The two files we require by name (absence is fine and is stated as such)
    named = {creds.ENV_FILE, I.INSTANCES_FILE}
    for f in sorted(named):
        if not os.path.exists(f):
            _check(os.path.basename(f), True, "does not exist (that's fine)")
            continue
        try:
            mode = os.stat(f).st_mode & 0o777
        except OSError as e:
            _mark(os.path.basename(f), "unknown", f"cannot stat: {e.__class__.__name__}")
            problems.append(f"{f} could not be checked — permissions unknown")
            continue
        ok = mode <= 0o600
        _check(os.path.basename(f), ok, f"mode {oct(mode)[2:]}", warn=not ok)
        if not ok:
            problems.append(f"{f} is too open → chmod 600 {f}")

    # Everything else that lives in the config dir
    if os.path.isdir(CONFIG_DIR):
        try:
            others = sorted(
                os.path.join(CONFIG_DIR, n) for n in os.listdir(CONFIG_DIR)
            )
        except OSError as e:
            _mark("other config files", "unknown",
                  f"cannot list {CONFIG_DIR}: {e.__class__.__name__}")
            others = []
            problems.append(f"contents of {CONFIG_DIR} could not be listed")
        loose = []
        for p in others:
            if p in named or not os.path.isfile(p):
                continue
            try:
                m = os.stat(p).st_mode & 0o777
            except OSError:
                loose.append((os.path.basename(p), None))
                continue
            if m & 0o077:
                loose.append((os.path.basename(p), m))
        if not others:
            pass
        elif not loose:
            _mark("other config files", "ok",
                  f"{len([p for p in others if os.path.isfile(p)]) - len(named & set(others))}"
                  f" file(s), none group/world readable")
        else:
            shown = ", ".join(f"{n} ({oct(m)[2:] if m else '?'})" for n, m in loose[:4])
            _mark("other config files", "warn",
                  f"{len(loose)} too open: {shown}")
            problems.append(f"{len(loose)} file(s) in {CONFIG_DIR} are readable by "
                            f"others → chmod 600 {CONFIG_DIR}/*")

    return problems


# ─── Doctor · on-disk inventory (one pass, no secret is ever read out) ─────────

# scheme://something@host — userinfo present, i.e. a credential in the URL.
_USERINFO_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://[^/@\s]+@")


def _url_has_creds(url: str) -> bool:
    try:
        import remotes
        return bool(remotes.has_creds(url))
    except Exception:
        return bool(_USERINFO_RE.match((url or "").strip()))


def _git_config_path(repo: str):
    """
    Absolute path of the .git/config that governs `repo`, or (None, note).

    `.git` is usually a directory, but for a submodule or a `git worktree`
    checkout it is a *file* holding `gitdir: <path>`. Those work trees have a
    remote — and therefore a possible credential — exactly like a normal clone,
    so they must be followed rather than skipped.
    """
    g = os.path.join(repo, ".git")
    if os.path.isdir(g):
        return os.path.join(g, "config"), ""
    if not os.path.isfile(g):
        return None, "no .git entry"
    try:
        with open(g, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError as e:
        return None, f"{g}: {e.__class__.__name__}"
    line = next((ln.strip() for ln in head.splitlines()
                 if ln.strip().lower().startswith("gitdir:")), "")
    if not line:
        return None, "'.git' is a file but holds no gitdir: pointer"
    gitdir = line.split(":", 1)[1].strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.normpath(os.path.join(repo, gitdir))
    # A linked worktree keeps its own dir but shares the repository config,
    # whose location is recorded in `commondir`.
    common = os.path.join(gitdir, "commondir")
    if os.path.isfile(common):
        try:
            with open(common, "r", encoding="utf-8", errors="replace") as f:
                rel = f.read().strip()
            if rel:
                gitdir = rel if os.path.isabs(rel) else \
                    os.path.normpath(os.path.join(gitdir, rel))
        except OSError:
            pass
    return os.path.join(gitdir, "config"), ""


def _read_repo_config(repo: str):
    """
    (origin_url, mode, note) for one work tree, read straight from
    .git/config — no subprocess, and the value is never returned to a caller
    that prints it.

    note is non-empty when the answer is UNKNOWN rather than negative.
    """
    cfgp, note = _git_config_path(repo)
    if note:
        return None, None, note
    try:
        mode = os.stat(cfgp).st_mode & 0o777
        with open(cfgp, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        return None, None, f"{cfgp}: {e.__class__.__name__}"

    url, section = "", ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = line.strip("[]").strip().lower()
            continue
        if section.startswith('remote "origin"') and line.lower().startswith("url"):
            _, sep, v = line.partition("=")
            if sep:
                url = v.strip()
    return url, mode, ""


def _all_work_trees(dest: str) -> list[str]:
    """
    Every work tree under `dest`, including nested ones and `.git`-file
    checkouts (submodules, linked worktrees).

    Deliberately NOT coverage.find_repos: that walker stops at the first
    repository on each branch (`dirnames[:] = []`) and ignores `.git` files,
    which is the right definition for *coverage* — a checkout nested inside
    another clone is not the canonical clone of a project and must not count as
    one — but the wrong definition for a *security sweep*. A vendored or
    submodule checkout can carry a token in its remote URL just like any other
    clone, and `remotes --fix` (which uses this exhaustive walk) would find and
    scrub it while doctor claimed everything was clean. On a real tree the gap
    was dozens of repositories.
    """
    try:
        from pull import _find_git_repos
        return _find_git_repos(dest)
    except Exception:
        # Fall back rather than report a clean tree we never walked.
        import coverage as _cov
        return _cov.find_repos(dest)


def _inventory(dest: str) -> dict:
    """
    One walk of the destination producing everything the deep checks need:
    duplicate detection, credential hygiene, permissions, layout.

    ASYMMETRY, ON PURPOSE: this walk is the exhaustive one (`_all_work_trees`),
    while coverage counting uses `coverage.scan`/`find_repos`. Security and
    permissions must see every work tree that could hold a credential; coverage
    must count only canonical clones. The two numbers can therefore differ and
    both are correct — `repos` below is the security population.
    """
    inv = {
        "ok": False, "reason": "",
        "repos": 0, "by_key": {},          # (host, path_with_namespace) -> [dirs]
        "cred": [], "world": 0, "checked": 0,
        "unknown": [],                      # repos we could not inspect
        "no_origin": 0,
        "nested": 0,                        # seen here, invisible to coverage
    }
    try:
        import coverage as _cov
    except Exception as e:
        inv["reason"] = f"coverage module unavailable ({e.__class__.__name__})"
        return inv
    if not os.path.isdir(dest):
        inv["reason"] = "destination folder does not exist"
        return inv
    try:
        repos = _all_work_trees(dest)
        # find_repos_pruned, NOT find_repos: W6-1 made find_repos
        # exhaustive by default. `canonical` must stay the set of managed
        # clones, or `nested` below collapses to 0 and every nested
        # checkout is reported as a duplicate no move can resolve.
        canonical = set(_cov.find_repos_pruned(dest))
    except OSError as e:
        inv["reason"] = f"cannot walk {dest}: {e.__class__.__name__}"
        return inv

    inv["ok"] = True
    inv["repos"] = len(repos)
    inv["nested"] = len([r for r in repos if r not in canonical])
    for r in repos:
        url, mode, note = _read_repo_config(r)
        if note:
            inv["unknown"].append((r, note))
            continue
        inv["checked"] += 1
        if mode is not None and (mode & 0o044):
            inv["world"] += 1
        if not url:
            inv["no_origin"] += 1
            continue
        if _url_has_creds(url):
            inv["cred"].append(r)
        key = _cov.normalize(url)
        # by_key drives DUPLICATE and LAYOUT reporting, which are statements
        # about canonical clones. A checkout nested inside another clone shares
        # its project identity but is not a second copy of the managed tree, so
        # counting it here would invent duplicates that no move can resolve.
        if key and r in canonical:
            inv["by_key"].setdefault(key, []).append(r)
    return inv


# ─── User-namespace projects · reachable, but only on an explicit opt-in ──────
#
# api.get_user_namespace_projects() has existed since W3 and was called from
# nowhere on purpose. The configured token is an ADMIN token: scope="all" sees
# every user namespace on the instance — i.e. every employee's personal
# repositories. An on-by-default version would pull other people's private
# work onto one laptop, and the account this tool
# authenticates as owns ZERO of those projects, so switching it on by default
# would be all cost and no benefit. That restraint is preserved: the default
# below is OFF, and it is off *because* the token is an admin token, not in
# spite of it.
#
# What was wrong was the other half. Doctor counted those projects as "missing"
# and advised "→ Clone", while the menu is built entirely on /groups/** and is
# structurally incapable of reaching a project with no group above it. The tool
# was recommending an action it does not offer, and a silent exclusion is
# indistinguishable from a bug. So the exclusion is now stated out loud, with
# its count, whose namespaces they are, and the exact setting that lifts it.
#
#   off  (default)  excluded. The count and the owners are DISCLOSED anyway,
#                   and they are reported as excluded-by-policy, never as
#                   missing-go-clone-them.
#   own             only the authenticated user's own personal namespace.
#                   For this token that is zero projects — the honest answer,
#                   and the least surprising one.
#   all             every user namespace the token can see. The owners are
#                   NAMED and counted before anything is fetched or cloned.
#
# Resolution order: the instance record's "user_namespaces" key, then the
# environment variable, then off. An unrecognised value is never treated as an
# opt-in; it degrades to off and says so.

USER_NS_ENV     = "GITLAB_CLI_USER_NAMESPACES"
USER_NS_KEY     = "user_namespaces"
USER_NS_DEFAULT = "off"
USER_NS_SCOPES  = ("off", "own", "all")

_USER_NS_ALIASES = {
    "off": "off", "0": "off", "no": "off", "none": "off", "false": "off",
    "disabled": "off", "exclude": "off",
    "own": "own", "mine": "own", "me": "own", "self": "own",
    "all": "all", "everyone": "all", "every": "all",
}


def user_ns_setting(cfg: dict = None, env: dict = None) -> tuple:
    """
    (scope, note) — the effective user-namespace scope and why.

    `note` is empty when the value was understood. It is non-empty when the
    configured value could not be used, so the caller can say that out loud
    instead of silently falling back to off: a rejected setting that looks
    like a working one is the same failure class as a silent exclusion.
    """
    env = os.environ if env is None else env

    raw, src = "", ""
    if isinstance(cfg, dict) and str(cfg.get(USER_NS_KEY) or "").strip():
        raw, src = str(cfg[USER_NS_KEY]).strip(), f"instance key '{USER_NS_KEY}'"
    elif str((env or {}).get(USER_NS_ENV) or "").strip():
        raw, src = str(env[USER_NS_ENV]).strip(), USER_NS_ENV

    if not raw:
        return USER_NS_DEFAULT, ""

    scope = _USER_NS_ALIASES.get(raw.lower())
    if scope is None:
        return USER_NS_DEFAULT, (
            f"{src} is {raw!r}, which is not one of "
            f"{'/'.join(USER_NS_SCOPES)} — treated as off (an unreadable "
            f"setting is never an opt-in)")
    return scope, ""


def user_ns_scope(cfg: dict = None, env: dict = None) -> str:
    """The effective scope only. Defaults to off, admin token or not."""
    return user_ns_setting(cfg, env)[0]


def user_ns_enabled(cfg: dict = None, env: dict = None) -> bool:
    return user_ns_scope(cfg, env) != "off"


def user_ns_how_to_enable() -> str:
    return (f"set {USER_NS_ENV}=own for your own namespace only, or "
            f"{USER_NS_ENV}=all for every user namespace this token can see "
            f"(with an admin token that is the whole instance); the same value "
            f'can be stored per instance as "{USER_NS_KEY}" in '
            f"{I.INSTANCES_FILE}")


def _ns_path(project: dict) -> str:
    ns = (project or {}).get("namespace") or {}
    return str(ns.get("full_path") or ns.get("path") or "").strip()


def _is_user_ns(project: dict) -> bool:
    """
    A project that lives in a USER namespace and is not a GitLab service
    principal. Pure classification of an ALREADY-FETCHED payload — it issues
    no request, which is what lets the default path disclose a count without
    spending a single extra API call.
    """
    ns = (project or {}).get("namespace") or {}
    if ns.get("kind") != "user":
        return False
    path = _ns_path(project)
    try:
        import api
        return not api.is_system_namespace(path)
    except Exception:
        return bool(path)


def split_user_namespace(projects) -> tuple:
    """(group_projects, user_namespace_projects). No network."""
    group, user = [], []
    for p in projects or []:
        (user if _is_user_ns(p) else group).append(p)
    return group, user


def user_ns_owners(projects) -> list:
    """Sorted distinct personal-namespace paths in `projects`."""
    return sorted({_ns_path(p) for p in projects or [] if _ns_path(p)})


def _own_username(cfg: dict) -> str:
    """The authenticated user's namespace path, or '' if it cannot be told."""
    try:
        info = caps.get(cfg) or {}
    except Exception:
        return ""
    return str(info.get("username") or "").strip()


def partition_by_policy(projects, cfg: dict = None, scope: str = None,
                        owner: str = None) -> tuple:
    """
    (in_scope, excluded, scope) for an already-fetched project list.

    off → every user-namespace project is excluded.
    own → only the token owner's own namespace is in scope. If the owner
          cannot be determined, nothing is admitted: guessing would
          over-collect, and over-collecting here means other people's
          private repositories.
    all → every user-namespace project is in scope.
    """
    if scope is None:
        scope = user_ns_scope(cfg)
    group, user = split_user_namespace(projects)
    if scope == "all":
        return group + user, [], scope
    if scope == "own":
        me = (owner if owner is not None else (_own_username(cfg) if cfg else ""))
        me = (me or "").strip().lower()
        if not me:
            return group, user, scope
        mine   = [p for p in user if _ns_path(p).lower() == me]
        others = [p for p in user if _ns_path(p).lower() != me]
        return group + mine, others, scope
    return group, user, scope


def user_ns_disclosure(excluded, scope: str, note: str = "",
                       in_scope=None, max_names: int = 8) -> list:
    """
    The lines that MUST be shown about user-namespace projects — in every
    scope, including off. This is the whole point of the feature: when the
    tool declines to look at something it says so, says how many, says whose,
    and says what to set to change its mind.

    Returns a list of plain strings so it can be printed, logged or asserted.
    """
    lines = []
    if note:
        lines.append(note)

    excluded = list(excluded or [])
    in_scope = [p for p in (in_scope or []) if _is_user_ns(p)]

    if scope == "off":
        if not excluded:
            lines.append("no user-namespace project on this instance — "
                         "nothing is being excluded")
            return lines
        owners = user_ns_owners(excluded)
        shown  = ", ".join(owners[:max_names])
        lines.append(
            f"{len(excluded)} project(s) live in USER namespaces and are "
            f"EXCLUDED by policy — they are not missing and not broken, and "
            f"the group-based menu cannot reach them")
        lines.append(
            f"they belong to {len(owners)} personal namespace(s): {shown}"
            + (f" … and {len(owners) - max_names} more"
               if len(owners) > max_names else ""))
        lines.append(f"to include them: {user_ns_how_to_enable()}")
        return lines

    owners = user_ns_owners(in_scope)
    lines.append(
        f"user namespaces are ENABLED (scope={scope}) — {len(in_scope)} "
        f"project(s) in {len(owners)} personal namespace(s) are in scope; "
        f"these are PEOPLE's private repositories, not group projects")
    if owners:
        lines.append("namespaces in scope: "
                     + ", ".join(owners[:max_names])
                     + (f" … and {len(owners) - max_names} more"
                        if len(owners) > max_names else ""))
    else:
        lines.append(f"no project is in scope for scope={scope}"
                     + (" — the authenticated account owns none"
                        if scope == "own" else ""))
    if excluded:
        lines.append(f"{len(excluded)} further user-namespace project(s) "
                     f"remain excluded by scope={scope}")
    return lines


def fetch_user_namespace_projects(cfg: dict, scope: str = None,
                                  owner_ref=None, api_mod=None,
                                  archived=None) -> tuple:
    """
    (projects, scope, disclosure_lines) — the CLONE-side entry point, and the
    first call site api.get_user_namespace_projects() has ever had.

    Contract, in order of importance:
      * scope=off (the default) issues NO API call at all and returns an empty
        list plus the lines explaining that the exclusion is a policy, not a
        failure;
      * any other scope performs READ-ONLY discovery and returns the
        disclosure BEFORE the caller pulls anything, so the names and the
        count are on screen before a single byte of anybody's private
        repository is written to disk.

    Callers must print `disclosure_lines` and, for scope=all, confirm with the
    user before cloning. Nothing here clones.
    """
    scope_eff, note = (user_ns_setting(cfg) if scope is None else (scope, ""))

    if scope_eff == "off":
        lines = [note] if note else []
        lines.append(
            "user-namespace projects are EXCLUDED by policy (scope=off); no "
            "request about them was made, so this is not a count of them — "
            "Doctor classifies the project list it already has and reports "
            "the number")
        lines.append(f"to include them: {user_ns_how_to_enable()}")
        return [], "off", lines

    if api_mod is None:
        try:
            import api as api_mod
        except Exception as e:
            return [], scope_eff, [
                f"user namespaces could not be discovered — api module "
                f"unavailable ({e.__class__.__name__}); that is UNKNOWN, "
                f"not empty"]

    if scope_eff == "own" and not owner_ref:
        owner_ref = _own_username(cfg)
        if not owner_ref:
            return [], "own", [
                "scope=own, but the authenticated account could not be "
                "identified — nothing is returned rather than guessing an "
                "owner and collecting somebody else's namespace"]

    projects = api_mod.get_user_namespace_projects(
        cfg["url"], cfg["headers"],
        min_access_level=((cfg or {}).get("min_access") or None),
        scope=scope_eff, owner_ref=owner_ref, archived=archived)

    lines = ([note] if note else []) + user_ns_disclosure(
        [], scope_eff, in_scope=projects)
    lines.append("nothing has been cloned yet — this was discovery only")
    return list(projects), scope_eff, lines


# ─── Doctor · the four checks that the original bug slipped past ───────────────

def _remote_projects(cfg: dict):
    """
    (projects, note). projects is None whenever the answer is UNKNOWN.

    Two failure modes are handled, and neither may end as a number:

    * A silent short read. `get_paginated(strict=False)` stops early on an
      error and returns a PARTIAL list, which is the same failure class as the
      bug this check exists for — so the count is verified against the X-Total
      header instead of trusted.
    * A raised `ApiError`. `get_paginated` is strict by default: a 500 or a
      dropped VPN on page 2 raises, and an exception escaping here aborts the
      whole doctor run with a traceback — the one command a user runs when the
      server is already misbehaving. It is caught and converted to UNKNOWN.
    """
    try:
        import api
    except Exception as e:
        return None, f"api module unavailable ({e.__class__.__name__})"

    base = f"{cfg['url']}/api/v4/projects"
    try:
        probe = api.request(base, cfg["headers"],
                            {"simple": True, "per_page": 1, "page": 1}, quiet=True)
    except Exception as e:
        return None, f"probe request failed ({e.__class__.__name__})"
    if probe is None:
        return None, "no answer from the API (network/VPN?)"
    if probe.status_code != 200:
        return None, f"HTTP {probe.status_code} from /projects"

    total = probe.headers.get("X-Total", "").strip()
    try:
        projs = api.get_paginated(base, cfg["headers"], {"simple": True},
                                  quiet=True, strict=False)
    except getattr(api, "ApiError", Exception) as e:
        return None, (f"the project list could not be fetched "
                      f"({_summary(str(e), 100)})")
    except Exception as e:
        return None, f"the project list could not be fetched ({e.__class__.__name__})"

    # strict=False hands back a PagedResult that knows whether it finished.
    if getattr(projs, "complete", True) is False:
        return None, (f"pagination stopped early after {len(projs)} project(s)"
                      + (f" — {projs.error}" if getattr(projs, "error", None) else ""))
    if total.isdigit():
        if len(projs) != int(total):
            return None, (f"pagination incomplete — {len(projs)} of {total} "
                          f"projects received")
    elif not projs:
        return None, "empty project list and no X-Total header to verify it"
    return projs, ""


def _user_ns_report(name: str, cfg: dict, excluded: list, scope: str,
                    scope_note: str, in_scope: list, problems: list,
                    excluded_on_disk: int = 0):
    """
    Say out loud what the coverage numbers above do NOT include.

    Doctor used to fold 32 user-namespace projects into "missing → Clone".
    They are not missing: they are other people's personal repositories that
    the group-rooted menu is structurally incapable of cloning. Either state
    is fine; saying nothing is not, because a silent exclusion and a bug look
    identical from outside.
    """
    n = len(excluded or [])
    in_user = [p for p in (in_scope or []) if _is_user_ns(p)]

    if scope == "off":
        state = "ok" if not n else "warn"
        _mark("user namespaces", state,
              f"scope=off — {n} project(s) excluded by policy"
              if n else "scope=off — none on this instance")
    else:
        _mark("user namespaces", "warn",
              f"scope={scope} — {len(in_user)} project(s) IN SCOPE"
              + (f", {n} still excluded" if n else ""))

    for line in user_ns_disclosure(excluded, scope, note=scope_note,
                                   in_scope=in_scope):
        _note(line)
    if excluded_on_disk:
        _note(f"{excluded_on_disk} of them are ALREADY on disk from an "
              f"earlier clone — excluded from the coverage arithmetic above, "
              f"but still listed by the instance and NOT orphans")

    if scope == "off" and n:
        owners = user_ns_owners(excluded)
        # One line: the full how-to was already printed under the check.
        problems.append(
            f"[{name}] {n} project(s) in {len(owners)} USER namespace(s) are "
            f"excluded by policy — the group-based menu cannot reach them, "
            f"they are not missing → to include them set "
            f"{USER_NS_ENV}=own (or =all)")
    elif scope != "off" and in_user:
        problems.append(
            f"[{name}] user namespaces are ENABLED (scope={scope}): "
            f"{len(in_user)} personal project(s) belonging to "
            f"{len(user_ns_owners(in_user))} account(s) are in scope. Review "
            f"whose data that is before cloning.")
    if scope_note:
        problems.append(f"[{name}] {scope_note}")


def _coverage_check(name: str, cfg: dict, index, host: str, problems: list):
    """
    How many projects the instance has vs how many are on disk, per namespace.

    This is the check that would have made the original failure impossible to
    miss: the tool said "done" while most projects had never been cloned.
    """
    if index is None:
        _mark("coverage", "unknown", "the destination could not be scanned")
        problems.append(f"[{name}] coverage UNKNOWN — destination not scannable")
        return

    projs, note = _remote_projects(cfg)
    if projs is None:
        _mark("coverage", "unknown", f"cannot ask the instance — {note}")
        problems.append(f"[{name}] coverage UNKNOWN — {_with_fix(note)}")
        return

    # GitLab delayed deletion: the project still lists, renamed to
    # `<name>-deletion_scheduled-<id>`. It is going away, so it is neither
    # "missing → Clone" nor part of the denominator — same rule as the group
    # screen (coverage_counts' 'pending_deletion').
    pending = [p for p in projs if _is_pending_deletion(p)]
    projs = [p for p in projs if not _is_pending_deletion(p)]

    # A project in a USER namespace has no group above it, so the entire
    # group-rooted clone path — and therefore the Clone menu — cannot reach
    # it. Counting those as "missing … → Clone" told the user to perform an
    # action the tool does not offer. They are partitioned out of the
    # coverage arithmetic and reported separately, with their count and
    # whose namespaces they are, so the exclusion is disclosed rather than
    # silent. No extra API call: this classifies the list already fetched.
    scope, scope_note = user_ns_setting(cfg)
    reachable, excluded, scope = partition_by_policy(projs, cfg, scope=scope)
    projs = reachable

    want = {}
    for p in projs:
        pwn = (p.get("path_with_namespace") or "").lower()
        if not pwn:
            continue
        want.setdefault(pwn.split("/")[0], set()).add(pwn)

    have_keys = set(index)
    total_want = sum(len(v) for v in want.values())
    total_have = 0
    rows = []
    for ns in sorted(want):
        w = want[ns]
        h = len({p for p in w if (host, p) in have_keys})
        total_have += h
        rows.append((ns, h, len(w)))

    short = [r for r in rows if r[1] < r[2]]
    state = "ok" if not short else ("fail" if total_have == 0 else "warn")
    _mark("coverage", state,
          f"{total_have}/{total_want} projects on disk"
          + ("" if not short else f" — {total_want - total_have} missing"))

    for ns, h, w in rows:
        icon = "✅" if h >= w else ("❌" if h == 0 else "◐")
        colour = GREEN if h >= w else (RED if h == 0 else YELLOW)
        print(f"      {colour}{icon}{RESET} {DIM}{ns:<38}{RESET} {h}/{w}")

    # Paths that belong to a pending-deletion project: its current
    # `…-deletion_scheduled-<id>` path and the ORIGINAL path a clone made
    # before the rename still sits at. A clone whose own path carries the
    # suffix belongs here too, even when /projects no longer lists it.
    pending_paths = set()
    for p in pending:
        pwn = (p.get("path_with_namespace") or "").lower()
        pending_paths.update((pwn, _PENDING_RE.sub("", pwn)))
    pending_paths.discard("")
    pending_on_disk = {k for k in have_keys
                       if k[1] in pending_paths or _is_pending_deletion(k[1])}
    if pending or pending_on_disk:
        _note(f"{len(pending)} project(s) are scheduled for deletion on the "
              f"server — not counted above, not offered for clone"
              + (f" ({len(pending_on_disk)} clone(s) of them on disk, kept)"
                 if pending_on_disk else ""))

    if short:
        problems.append(f"[{name}] {total_want - total_have} of {total_want} "
                        f"projects are NOT on disk "
                        f"(short namespaces: "
                        f"{', '.join(r[0] for r in short[:4])}"
                        f"{'…' if len(short) > 4 else ''}) → Clone")

    # The user-namespace population — disclosed in EVERY scope, never silent.
    on_disk = len([p for p in excluded
                   if (host, (p.get("path_with_namespace") or "").lower())
                   in have_keys])
    _user_ns_report(name, cfg, excluded, scope, scope_note, projs, problems,
                    excluded_on_disk=on_disk)

    # Clones that exist locally but that the instance no longer lists.
    #
    # Computed over the UNPARTITIONED list on purpose. `want` holds only the
    # projects the group-based menu can reach; a user-namespace project that
    # is excluded by policy but already sits on disk is still listed by the
    # instance, and calling it an orphan would be a false claim of the exact
    # kind this project forbids — an exclusion must never mutate into a
    # different, wrong-sounding finding.
    everything = {(p.get("path_with_namespace") or "").lower()
                  for p in list(projs) + list(excluded)}
    # A pending-deletion project's clones (at its original or its renamed
    # path) were disclosed above; they are not orphans either.
    everything.discard("")
    orphans = [k for k in have_keys
               if k[1] not in everything and k not in pending_on_disk]
    if orphans:
        _mark("no orphan clones", "warn",
              f"{len(orphans)} clone(s) the instance does not list, e.g. "
              f"{sorted(orphans)[0][1]}")


def _credential_check(name: str, inv: dict, problems: list):
    """Counts and paths only — a credential value is never read out."""
    if not inv["ok"]:
        _mark("credentials in remotes", "unknown", inv["reason"])
        _mark(".git/config permissions", "unknown", inv["reason"])
        problems.append(f"[{name}] credential hygiene UNKNOWN — {inv['reason']}")
        return

    n = len(inv["cred"])
    _mark("credentials in remotes", "ok" if n == 0 else "fail",
          f"{n} of {inv['checked']} clones embed a credential in origin"
          + ("" if n == 0 else f", e.g. {inv['cred'][0]}"))
    if inv.get("nested"):
        _note(f"includes {inv['nested']} work tree(s) nested inside another "
              f"clone or attached via a .git file — coverage does not count "
              f"these, the security sweep must")
    if n:
        problems.append(f"[{name}] {n} clone(s) store a credential in "
                        f".git/config → `gitlab-cli remotes --fix`, and rotate "
                        f"the token: a scrub does not un-disclose it")

    w = inv["world"]
    _mark(".git/config permissions", "ok" if w == 0 else ("fail" if n else "warn"),
          f"{w} of {inv['checked']} are group/world readable")
    if w and n:
        problems.append(f"[{name}] {w} .git/config file(s) are world-readable "
                        f"AND {n} contain a credential → chmod 600 them")

    if inv["unknown"]:
        _mark("every clone inspected", "unknown",
              f"{len(inv['unknown'])} could not be read, e.g. {inv['unknown'][0][0]}")
        problems.append(f"[{name}] {len(inv['unknown'])} clone(s) could not be "
                        f"inspected — their credential state is UNKNOWN")
    if inv["no_origin"]:
        _mark("every clone has an origin", "warn",
              f"{inv['no_origin']} clone(s) have no origin URL")


def _layout_check(name: str, cfg: dict, inv: dict, host: str, problems: list):
    """Duplicates and off-layout clones. Reports only — never moves anything."""
    if not inv["ok"]:
        _mark("one directory per project", "unknown", inv["reason"])
        _mark("layout matches the config", "unknown", inv["reason"])
        return

    dest = os.path.normpath(cfg["master_dir"])
    mine = {k: v for k, v in inv["by_key"].items() if k[0] == host}

    dupes = {k: v for k, v in mine.items() if len(v) > 1}
    _mark("one directory per project", "ok" if not dupes else "warn",
          f"{len(dupes)} project(s) exist in more than one directory"
          if dupes else f"{len(mine)} clone(s), no duplicates")
    for (_host, pwn), dirs in sorted(dupes.items())[:5]:
        print(f"      {YELLOW}·{RESET} {DIM}{pwn}{RESET}")
        for d in dirs:
            print(f"          {DIM}{d}{RESET}")
    if len(dupes) > 5:
        _note(f"… and {len(dupes) - 5} more")
    if dupes:
        problems.append(f"[{name}] {len(dupes)} project(s) are cloned twice under "
                        f"{dest} — review before any move (report only, nothing "
                        f"was touched)")

    off = []
    for (_host, pwn), dirs in mine.items():
        expected = os.path.join(dest, *pwn.split("/"))
        for d in dirs:
            if os.path.normpath(d).lower() != expected.lower():
                off.append((pwn, d))
    _mark("layout matches the config", "ok" if not off else "warn",
          f"{len(off)} clone(s) sit where the current config would not put them"
          if off else f"all {len(mine)} clone(s) at {dest}/<namespace>/<project>")
    for pwn, d in sorted(off)[:5]:
        print(f"      {YELLOW}·{RESET} {DIM}{d}{RESET}")
        print(f"          {DIM}expected {os.path.join(dest, *pwn.split('/'))}{RESET}")
    if len(off) > 5:
        _note(f"… and {len(off) - 5} more")
    if off:
        problems.append(f"[{name}] {len(off)} clone(s) live under a path shape the "
                        f"current configuration no longer produces — a fresh "
                        f"clone would create a second copy next to them")


def _config_completeness_check(name: str, cfg: dict, inv: dict, host: str,
                               problems: list):
    """
    Configuration that is in effect on disk but missing from the config file.

    The instance lost its `folder` key and nothing noticed: clones kept being
    made one level higher than the whole existing tree.
    """
    if not inv["ok"]:
        _mark("config matches the disk", "unknown", inv["reason"])
        problems.append(f"[{name}] config/disk consistency UNKNOWN — {inv['reason']}")
        return

    base = os.path.normpath(cfg["base_dir"])
    folder = (cfg.get("folder") or "").strip()

    implied = {}
    for (h, pwn), dirs in inv["by_key"].items():
        if h != host:
            continue
        for d in dirs:
            rel = os.path.relpath(os.path.normpath(d), base).replace(os.sep, "/")
            if rel.startswith(".."):
                continue
            if rel.lower().endswith(pwn):
                prefix = rel[:len(rel) - len(pwn)].strip("/")
                implied[prefix] = implied.get(prefix, 0) + 1

    if not implied:
        _mark("config matches the disk", "unknown",
              "no clone of this instance on disk to compare against")
        return

    shapes = sorted(implied.items(), key=lambda kv: -kv[1])
    desc = ", ".join(f"'{k or '(none)'}'×{v}" for k, v in shapes[:4])

    ghost = [(k, v) for k, v in shapes if k and k != folder]
    if not folder and ghost:
        _mark("config matches the disk", "fail",
              f"config has no 'folder' key, but {ghost[0][1]} clone(s) sit under "
              f"'{ghost[0][0]}/' — {desc}")
        problems.append(
            f"[{name}] INCONSISTENT: instances.json has no 'folder' for this "
            f"instance, yet {ghost[0][1]} clone(s) on disk were made with "
            f"folder='{ghost[0][0]}'. New clones land in {cfg['master_dir']} "
            f"while the existing tree is in "
            f"{os.path.join(base, ghost[0][0])} → set folder, or accept the split "
            f"deliberately")
    elif folder and folder not in implied:
        _mark("config matches the disk", "warn",
              f"config says folder='{folder}' but no clone uses it — {desc}")
        problems.append(f"[{name}] folder='{folder}' in the config matches no "
                        f"clone on disk ({desc})")
    elif len(shapes) > 1:
        _mark("config matches the disk", "warn",
              f"{len(shapes)} different layouts on disk — {desc}")
        problems.append(f"[{name}] clones exist under {len(shapes)} different "
                        f"layouts ({desc}) — only '{folder or '(none)'}' is "
                        f"configured")
    else:
        _mark("config matches the disk", "ok",
              f"folder='{folder or '(none)'}' agrees with all "
              f"{shapes[0][1]} clone(s)")


# ─── Doctor · per-instance ─────────────────────────────────────────────────────

def _top_error_codes(err_mod, instance, limit: int = 3) -> list:
    """[(code, count)] of unresolved log entries for `instance`, most first.

    Same view as errors.unresolved_count(): the non-expired entries. Legacy
    entries written before the catalog carry no `code`; they are explained
    from their message now so they still group under a real code.
    """
    from collections import Counter
    owner, hints = getattr(err_mod, "_owner", None), None
    if owner is not None and hasattr(err_mod, "_instance_hints"):
        hints = err_mod._instance_hints()
    tally = Counter()
    for e in err_mod._read_errors():
        if e.get("resolved"):
            continue
        if instance is not None:
            # unresolved_count(instance) also counts entries that belong to
            # NO instance (the Error Log lists them under every instance);
            # the top codes must be drawn from that same population.
            who = owner(e, hints) if hints is not None else (e.get("instance") or "")
            if who not in (instance, ""):
                continue
        tally[e.get("code") or _explain(e.get("message", ""))[0]] += 1
    return tally.most_common(limit)


def _error_log_check(name: str, problems: list):
    """Unresolved error-log entries for THIS instance, with the top codes."""
    try:
        import errors as _err
    except Exception as e:
        _mark("error log", "unknown",
              f"errors module unavailable ({e.__class__.__name__})")
        return
    scoped = True
    try:
        n = _err.unresolved_count(instance=name)
    except TypeError:                           # pre-instance errors.py
        scoped = False
        n = _err.unresolved_count()
    except Exception as e:
        _mark("error log", "unknown",
              f"could not read it: {e.__class__.__name__}")
        return
    if not n:
        _mark("error log", "ok", "no unresolved errors"
              + (" for this instance" if scoped else ""))
        return
    try:
        top = _top_error_codes(_err, name if scoped else None)
    except Exception:
        top = []
    shown = ", ".join(f"{code}×{c}" for code, c in top)
    where = "" if scoped else " (all instances)"
    _mark("error log", "warn",
          f"{n} unresolved{where}" + (f" — {shown}" if shown else ""))
    problems.append(f"[{name}] {n} unresolved error(s) in the log{where}"
                    + (f" ({shown})" if shown else "")
                    + " → gitlab-cli → 5 (Error Log)")


def _instance_check(deep: bool = True) -> list[str]:
    """For each GitLab: is there a token? does it connect? what access does it have?"""
    problems = []
    insts = I.list_instances()

    if not insts:
        _check("GitLab configured", False, "none")
        return ["No GitLab configured — there is no instance to check → "
                "Instances → add a GitLab (or run `gitlab-cli setup`)"]

    for inst in insts:
        name = inst["name"]
        print(f"\n  {BOLD}{WHITE}{name}{RESET} {DIM}{inst['url']}{RESET}")

        token = creds.resolve_token(inst)
        if not token:
            _check("token", False, f"not found ({inst.get('token_env','?')})")
            problems.append(f"[{name}] Token missing — no token found in "
                            f"{inst.get('token_env', '?')} → Instances → New token")
            continue
        _check("token", True, creds.mask(token))

        info = caps.get(I.build_cfg(inst), force=True)
        online = bool(info.get("ok"))
        if not online:
            err = info.get("error", "") or "no answer from the server"
            _check("connects", False, _summary(err, 60))
            code = _explain(err)[0]
            menu_fix = {
                "auth":      "Instances → New token: paste a new personal access token",
                "forbidden": "give the token the 'api' scope (Instances → New token)",
                # caps reports a dropped connection as plain prose the catalog
                # does not classify; the useful advice is still the network.
                "unknown":   "check the VPN/network and the URL "
                             "(Instances → Change URL), then re-run doctor",
            }.get(code)
            problems.append(f"[{name}] {_fail_line(err, fix=menu_fix)}")
        else:
            _check("connects", True, caps.summary_line(info))

            c = info.get("caps", {})
            gr = info.get("top_groups", 0)
            _check("sees groups", gr > 0,
                   f"{gr} groups", warn=(gr == 0))
            if gr == 0:
                problems.append(f"[{name}] sees no groups — "
                                f"the token may be a bot token (it must be personal)")

            _check("can clone", bool(c.get("clone")), warn=True)
            _check("sees members", bool(c.get("members")),
                   "" if c.get("members") else "no access (that's fine)",
                   warn=True)

        # Folder — always absolute, so it cannot depend on the cwd
        cfg  = I.build_cfg(inst)
        d    = cfg["master_dir"]
        ex   = os.path.isdir(d)
        n    = len(os.listdir(d)) if ex else 0
        _check("folder", True,
               f"{d}" + (f" ({n} items)" if ex else " (not created yet)"))

        _error_log_check(name, problems)

        if not deep:
            _mark("coverage / hygiene / layout", "unknown",
                  "skipped (--quick) — nothing about the clone tree was verified")
            problems.append(f"[{name}] deep checks were skipped (--quick): "
                            f"coverage, credential hygiene, layout and config "
                            f"consistency are UNKNOWN")
            continue

        # ── deep checks ────────────────────────────────────────────────────
        t0 = time.time()
        try:
            import coverage as _cov
            host = _cov.host_of(cfg["url"])
        except Exception as e:
            _mark("deep checks", "unknown",
                  f"coverage module unavailable ({e.__class__.__name__})")
            problems.append(f"[{name}] deep checks could not run — "
                            f"coverage module unavailable")
            continue

        inv = _inventory(d)

        # The authoritative index the menu itself uses (origin via git), so
        # doctor and the menu can never disagree about what is cloned.
        index = None
        try:
            index = {k for k in _cov.scan(d) if k[0] == host}
        except Exception as e:
            _mark("scan destination", "unknown",
                  f"{d}: {e.__class__.__name__}")

        # Each deep check is isolated: doctor is the command a user runs when
        # something is already broken, so one failing check must degrade to
        # UNKNOWN rather than abort the run (and every later instance) with a
        # traceback.
        for label, fn in (
            ("credential hygiene", functools.partial(_credential_check, name, inv, problems)),
            ("layout",             functools.partial(_layout_check,
                                                    name, cfg, inv, host, problems)),
            ("config vs disk",     functools.partial(_config_completeness_check,
                                                    name, cfg, inv, host, problems)),
        ):
            try:
                fn()
            except Exception as e:
                _mark(label, "unknown",
                      f"the check itself failed: {e.__class__.__name__}: {e}"[:90])
                problems.append(f"[{name}] the '{label}' check crashed "
                                f"({e.__class__.__name__}) — that result is UNKNOWN")

        if online:
            try:
                _coverage_check(name, cfg, index, host, problems)
            except Exception as e:
                _mark("coverage", "unknown",
                      f"the check itself failed: {e.__class__.__name__}: {e}"[:90])
                problems.append(f"[{name}] coverage UNKNOWN — the check crashed "
                                f"({e.__class__.__name__})")
        else:
            _mark("coverage", "unknown",
                  "the instance is unreachable — the project list is unknown")
            problems.append(f"[{name}] coverage UNKNOWN — instance unreachable")

        # Clones of this GitLab that live OUTSIDE the configured folder.
        # A cwd-relative dest used to scatter them; each stray tree is a full
        # copy the menu never sees, so the group reads as "not cloned".
        try:
            inside = index if index is not None else set()
            stray  = {}
            for cand in (os.path.expanduser("~"), os.getcwd()):
                base = os.path.join(cand, os.path.basename(cfg["base_dir"]))
                if os.path.isdir(base) and os.path.realpath(base) != os.path.realpath(d):
                    for k, v in _cov.scan(base).items():
                        if k[0] == host and k not in inside:
                            stray[k] = v
            _check("no stray clones", not stray,
                   "" if not stray
                   else f"{len(stray)} repos outside the folder, e.g. "
                        f"{next(iter(stray.values()))}",
                   warn=bool(stray))
            if stray:
                problems.append(f"[{name}] {len(stray)} clones live outside "
                                f"{d} — move or re-clone them")
        except Exception as e:
            _mark("no stray clones", "unknown",
                  f"could not look outside {d}: {e.__class__.__name__}")

        _note(f"deep checks took {time.time() - t0:.1f}s over "
              f"{inv['repos']} work tree(s)")

    return problems


def doctor(argv: list = None):
    """
    Full health check.

    Flags:
      --quick   skip everything that needs the network or a tree walk.
                The skipped checks report UNKNOWN; they are never assumed OK.
    """
    argv = sys.argv[1:] if argv is None else argv
    deep = not any(a in ("--quick", "-q") for a in argv)

    print_header("Doctor", "checking everything that could be broken")

    print(f"  {BOLD}System{RESET}")
    problems = _git_check()

    print(f"\n  {BOLD}Files{RESET}")
    problems += _perm_check()

    print(f"\n  {BOLD}GitLab instances{RESET}")
    problems += _instance_check(deep=deep)

    print()
    if not problems:
        print_ok("  Everything that could be checked is healthy ✔\n")
        return

    print(f"  {BOLD}{YELLOW}{len(problems)} things need fixing:{RESET}\n")
    for p in problems:
        print(f"    {YELLOW}·{RESET} {p}")
    print()


def run_setup():
    """Wizard for adding a new GitLab (the `setup` command)."""
    import instances as I
    _welcome()
    return I.add_instance(quiet_header=True)
