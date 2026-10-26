# instances.py — Multi-GitLab account manager
#
# A user can have several GitLabs (work, personal, client, ...).
# Each instance has:
#   - a friendly name       (Work, Personal, gitlab.com, ...)
#   - a URL
#   - a token → stored in ~/.gitlab-cli/env with chmod 600, NOT in this file
#   - a folder of its own to clone into
#   - min_access_level → only groups where you have at least this level

import os
import json
import stat

import caps
import credentials as creds
import paths as _paths
from printer import (
    print_warn, print_header,
    menu_key, matches_key, ask_choice, print_unknown_choice,
    KEY_BACK as printer_KEY_BACK,
    BOLD, CYAN, GREEN, YELLOW, DIM, RESET, RED, WHITE
)

CONFIG_DIR     = creds.CONFIG_DIR
INSTANCES_FILE = os.path.join(CONFIG_DIR, "instances.json")

DEFAULT_MASTER = "MasterGroups"

ACCESS_CHOICES = [
    (0,  "Everything I can see"),
    (20, "Reporter and above"),
    (30, "Developer and above"),
    (40, "Maintainer and above"),
    (50, "Owner only"),
]


# ─── Load / Save ───────────────────────────────────────────────────────────────

class InstancesFileError(RuntimeError):
    """instances.json exists but cannot be used — it must not be overwritten.

    str(exc) is a finished one-line explanation ("Title — cause → fix").
    """


def _empty() -> dict:
    return {"instances": [], "active": None}


def _read_instances() -> tuple:
    """(data, problem). `problem` is '' when the file is absent or usable.

    The old _load() turned ANY failure — a stray comma, a permission error —
    into "no GitLabs configured". The setup wizard then ran, and its _save()
    overwrote the user's whole (repairable) file with one new instance. Now the
    failure is named with the file path and, for JSON, the line and column.
    """
    path = INSTANCES_FILE
    if not os.path.exists(path):
        return _empty(), ""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        return _empty(), creds.catalog_line(
            f"JSONDecodeError: {exc}", code="config_invalid",
            title="instances.json is not valid JSON",
            cause=f"{path} line {exc.lineno} col {exc.colno}: {exc.msg}",
            fix=f"Fix line {exc.lineno} in a text editor, or move the file "
                f"aside and run `gitlab-cli setup`")
    except UnicodeDecodeError as exc:
        return _empty(), creds.catalog_line(
            f"UnicodeDecodeError: {exc}", code="config_invalid",
            title="instances.json is not valid JSON",
            cause=f"{path} is not UTF-8 text (bad byte at offset {exc.start})",
            fix="Re-save it as UTF-8 in a text editor, or move it aside and "
                "run `gitlab-cli setup`")
    except OSError as exc:
        return _empty(), creds.catalog_line(
            f"{type(exc).__name__}: {exc}",
            code="permission_denied" if isinstance(exc, PermissionError) else None,
            title="instances.json cannot be read",
            cause=f"{path} cannot be read ({exc.strerror or exc})",
            fix=f"Run `ls -l {path}`, then `chmod 600 {path}` as its owner")

    shape = ""
    if not isinstance(data, dict):
        shape = f"the top level is a JSON {type(data).__name__}, not an object"
    elif not isinstance(data.get("instances", []), list):
        shape = "\"instances\" is not a list"
    else:
        for i, inst in enumerate(data.get("instances", []), 1):
            if not isinstance(inst, dict):
                shape = f"instance #{i} is not an object"
            else:
                miss = [k for k in ("name", "url")
                        if not isinstance(inst.get(k), str) or not inst.get(k)]
                if miss:
                    shape = f"instance #{i} has no {' / '.join(repr(k) for k in miss)}"
            if shape:
                break
    if shape:
        return _empty(), creds.catalog_line(
            f"invalid instances.json: {shape}", code="config_invalid",
            title="instances.json is not valid",
            cause=f"{path}: {shape}",
            fix="Add the missing field in a text editor, or delete that entry")

    data.setdefault("instances", [])
    data.setdefault("active", None)
    return data, ""


def instances_file_problem() -> str:
    """'' when instances.json is absent or usable, else the one-line reason."""
    return _read_instances()[1]


def _load() -> dict:
    """The instances file. Unusable → reported once (stderr) and read as empty;
    _save() then REFUSES to write, so the broken file is never overwritten."""
    data, problem = _read_instances()
    if problem:
        creds.warn_once(("instances", problem), problem)
    return data


#: Every key a user may legitimately set on an instance record.
#
# This list exists because of the defect it closes. `_normalize_instance` used
# to keep whatever it was handed EXCEPT the cfg-only keys, and `build_cfg`
# published a fixed nine-key dict. The combination meant that any key build_cfg
# did not know about survived a plain save (it was still in the raw record) but
# evaporated the moment a BUILT cfg was written back — which every screen in
# this file does, because they all call `_save(data)` after `build_cfg`. A
# setting could therefore work for weeks and then be erased by an unrelated
# save, with nothing on screen. `user_namespaces` was the key that hit it.
#
# The rule now: a key listed here is carried through build_cfg AND through a
# save, both ways. A key NOT listed here is reported by
# `unknown_instance_keys()` instead of vanishing, so the next person to add one
# finds out at once rather than after it has been silently dropped.
PERSISTED_KEYS = (
    "name",
    "url",
    "token_env",
    "master_dir",
    "folder",
    "min_access_level",
    "user_namespaces",
)

#: Keys the tool itself creates on a raw record but that carry no user intent.
_INTERNAL_KEYS = ("token",)


def unknown_instance_keys(inst: dict) -> list:
    """Keys on `inst` that neither PERSISTED_KEYS nor the cfg layer recognises.

    A normaliser that drops unrecognised keys without saying so will do it
    again to the next key someone adds. This is the naming half of the fix:
    anything here is about to be discarded, and callers can say so out loud.
    """
    if not isinstance(inst, dict):
        return []
    known = set(PERSISTED_KEYS)
    known.update(_CFG_ONLY_KEYS)
    known.update(_INTERNAL_KEYS)
    return sorted(k for k in inst if k not in known)


def _normalize_instance(inst: dict) -> dict:
    """
    Make an instance record safe to persist.

    Three jobs now. Two are about keeping `master_dir` an UNFOLDED base:
      * if the record is really a built cfg (it carries `base_dir`), restore
        master_dir from base_dir and drop every cfg-only key — including the
        resolved token, which must never be written to instances.json;
      * otherwise strip any trailing copies of `folder` that already crept in.

    The third is PRESERVATION: every key in PERSISTED_KEYS is left alone, and
    anything outside it is reported by `unknown_instance_keys()` rather than
    disappearing without a word.

    Mutates and returns `inst`, so the caller's in-memory copy is fixed too.
    """
    if not isinstance(inst, dict):
        return inst

    base = (inst.get("base_dir") or "").strip()
    if base:
        # base_dir is always absolute (it came out of paths.anchor). The record
        # it is replacing usually held a RELATIVE value, and persisting an
        # absolute one would silently pin the tree to today's install dir, so
        # give the relative form back whenever base_dir really is install-local.
        inst["master_dir"] = _relative_to_install(base)
        # A built cfg carries a live token; never persist it.
        inst.pop("token", None)
    else:
        md = _strip_folder_suffix(inst.get("master_dir", "") or "",
                                  inst.get("folder") or "")
        if md:
            inst["master_dir"] = md

    for k in _CFG_ONLY_KEYS:
        inst.pop(k, None)
    return inst


def _save(data: dict):
    """Write instances.json (0600 from the first byte, atomic).

    Raises InstancesFileError — and writes nothing — when the file on disk
    exists but cannot be read: `data` was then loaded as empty, and writing it
    would erase every configured GitLab.
    """
    problem = instances_file_problem()
    if problem:
        raise InstancesFileError(problem)
    for inst in (data or {}).get("instances", []) or []:
        _normalize_instance(inst)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    creds.write_private_file(INSTANCES_FILE,
                             json.dumps(data, indent=2, ensure_ascii=False))


def _try_save(data: dict) -> bool:
    """_save() for interactive screens: a failure is printed as one line and
    reported as False, never swallowed and never a traceback."""
    try:
        _save(data)
        return True
    except InstancesFileError as exc:
        print(f"  {RED}✖ {exc}{RESET}")
    except OSError as exc:
        print(f"  {RED}✖ " + creds.catalog_line(
            f"{type(exc).__name__}: {exc}",
            cause=f"{INSTANCES_FILE} could not be written ({exc.strerror or exc})",
            fix=f"Run `ls -ld {CONFIG_DIR}` and make sure your user owns it")
              + f"{RESET}")
    print(f"  {YELLOW}Nothing was saved.{RESET}")
    return False


def _token_store_failed(exc: Exception) -> None:
    print(f"  {RED}✖ {exc}{RESET}")
    print(f"  {YELLOW}The token was NOT stored.{RESET}")


def list_instances() -> list:
    return _load().get("instances", [])


def has_instances() -> bool:
    return bool(list_instances())


# ─── Migration: inline token → env file ────────────────────────────────────────

def migrate_inline_tokens() -> int:
    """
    Move tokens that used to sit in plain text inside instances.json
    into the env file. Returns how many tokens were moved.
    """
    data    = _load()
    moved   = 0
    changed = False

    for inst in data.get("instances", []):
        tok = (inst.pop("token", "") or "").strip()
        if tok:
            var = creds.env_var_name(inst["name"])
            try:
                creds.set_token(var, tok)
            except creds.CredentialsFileError as exc:
                # Keep the token where it is; rewriting the env file from an
                # unreadable copy would delete the other instances' tokens.
                inst["token"] = tok
                creds.warn_once(("migrate", str(exc)), str(exc))
                continue
            inst["token_env"] = var
            moved  += 1
            changed = True
        elif "token_env" not in inst:
            inst["token_env"] = creds.env_var_name(inst["name"])
            changed = True

    if changed:
        _save(data)
    return moved


# ─── Ready-to-use config ───────────────────────────────────────────────────────

# Keys that only ever exist on a BUILT cfg, never on a raw instance record.
# Their presence is the reliable signal that a cfg was handed back to code that
# expects an instance dict.
_CFG_ONLY_KEYS = ("base_dir", "headers", "min_access")


def _strip_folder_suffix(master_dir: str, folder: str) -> str:
    """
    Remove any trailing repetitions of `folder` from `master_dir`.

    build_cfg publishes master_dir = base_dir + folder. If that value is ever
    written back into the `master_dir` field of an instance record, the next
    build appends `folder` a second time and every path for that instance sits
    one level too deep (MasterGroups/acme/acme). Stripping here makes
    the accumulation unrepresentable rather than merely unlikely.

    A master_dir that is *exactly* the folder name is left alone: there is no
    parent left to fall back to, and the user may have meant it.
    """
    folder = (folder or "").strip()
    md     = (master_dir or "").strip()
    if not folder or not md:
        return md
    while True:
        norm   = os.path.normpath(md)
        parent = os.path.dirname(norm)
        if os.path.basename(norm) != folder or not parent:
            return md
        md = parent


def _relative_to_install(path: str) -> str:
    """
    An install-local absolute path expressed relative to the install dir again.

    Anything outside the install root (a genuinely absolute master_dir the user
    chose) is returned untouched.
    """
    p = os.path.normpath(path or "")
    root = os.path.normpath(_paths.INSTALL_ROOT)
    if p == root:
        return "."
    if p.startswith(root + os.sep):
        return os.path.relpath(p, root)
    return p


def _base_of(inst: dict) -> str:
    """
    The UNFOLDED base directory of an instance record.

    Accepts a raw instance dict and, idempotently, a dict that is already a
    built cfg: a cfg carries `base_dir` (authoritative, folder not applied)
    alongside a `master_dir` that already has the folder in it. Preferring
    base_dir makes build_cfg(build_cfg(x)) == build_cfg(x) for any depth, so
    the folder is applied exactly once no matter how the dict travelled.
    """
    base = (inst.get("base_dir") or "").strip()
    if base:
        return base
    return _strip_folder_suffix(inst.get("master_dir", DEFAULT_MASTER)
                                or DEFAULT_MASTER,
                                inst.get("folder") or "")


def build_cfg(inst: dict, allow_generic: bool = False) -> dict:
    """
    Raw instance → a config the rest of the modules can use.

    If 'folder' is empty, repos go straight into master_dir
    (legacy behaviour — existing configs keep working).
    If it is set, each GitLab gets its own subfolder, which avoids
    collisions when two GitLabs have groups with the same name.
    """
    token       = creds.resolve_token(inst, allow_generic=allow_generic)
    base_dir    = _paths.anchor(_base_of(inst))
    folder      = (inst.get("folder") or "").strip()
    inst_folder = os.path.join(base_dir, folder) if folder else base_dir

    cfg = {
        "token":       token,
        "token_env":   inst.get("token_env") or creds.env_var_name(inst["name"]),
        "url":         inst["url"].rstrip("/"),
        "master_dir":  inst_folder,
        "base_dir":    base_dir,
        "folder":      folder,
        "name":        inst["name"],
        "min_access":  int(inst.get("min_access_level") or 0),
        "headers":     {"PRIVATE-TOKEN": token},
    }

    # Carry the persisted per-instance settings onto the cfg UNCHANGED, under
    # their own names. setup.user_ns_setting() reads cfg["user_namespaces"]
    # directly, and `min_access_level` has to travel too: a cfg is routinely
    # handed straight back to _save(), and a key that is not on the cfg is a
    # key that the next save erases. This loop is what makes
    # build_cfg -> _save -> _load -> build_cfg a fixed point for every setting
    # rather than only for the five build_cfg happens to name above.
    for key in PERSISTED_KEYS:
        if key in cfg:
            continue
        if key in inst:
            cfg[key] = inst[key]

    return cfg


def get_active_instance() -> dict | None:
    data   = _load()
    active = data.get("active")
    if not active:
        return None
    for inst in data.get("instances", []):
        if inst["name"] == active:
            return build_cfg(inst, allow_generic=True)
    return None


def set_active(name: str):
    data = _load()
    data["active"] = name
    _save(data)


# ─── Input helpers ─────────────────────────────────────────────────────────────

def _ask(label: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        val = input(f"  {label}{hint}: ").strip()
    except (KeyboardInterrupt, EOFError):
        raise
    return val or default


def _normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        # Internal host (no dot, or .internal) → http, everything else → https
        url = ("http://" if "." not in url.split("/")[0].split(":")[0]
               or url.endswith(".internal") else "https://") + url
    return url


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-"
                   for c in name.lower()).strip("-") or "gitlab"


def access_label(level) -> str:
    """The human name of a min_access_level, for any screen that shows one.

    `0` is not "no filter set, who knows" — it is the explicit answer
    "everything I can see", and it is printed as such. A filter that is
    invisible is a filter that is silently wrong.
    """
    try:
        lvl = int(level or 0)
    except (TypeError, ValueError):
        return f"unreadable ({level!r}) — treated as 0"
    for choice, label in ACCESS_CHOICES:
        if choice == lvl:
            return label if lvl == 0 else f"{label} ({caps.role_label(lvl)}+)"
    return f"{caps.role_label(lvl)}+ (level {lvl})"


def access_summary(level) -> str:
    """One line naming the access filter AND what it does to the group list."""
    try:
        lvl = int(level or 0)
    except (TypeError, ValueError):
        lvl = 0
    if lvl <= 0:
        return "Access filter: every group this token can see (no filter)"
    return (f"Access filter: {access_label(lvl)} — groups where you have less "
            f"than {caps.role_label(lvl)} are NOT listed")


def _pick_access_level(current: int = 0, reader=input) -> int:
    """Ask which groups to show. An answer we do not understand is refused.

    This setting decides which groups the tool ever lists. The old version
    returned `current` unchanged for any unrecognised answer and printed
    nothing, so a typo produced a tool quietly showing a subset of the user's
    estate — the founding defect of this project in miniature. Now an
    unrecognised answer is named out loud and the question is asked again, and
    the level that is actually in force is echoed back before returning.

    `reader` is injectable so the loop is testable without a terminal.
    """
    print(f"\n  {BOLD}Which groups should I show?{RESET}")
    print(f"  {DIM}If you only work on a few projects, filter them"
          f" so the list stays short.{RESET}")
    print(f"  {DIM}Currently: {access_summary(current)}{RESET}\n")
    for i, (lvl, label) in enumerate(ACCESS_CHOICES, 1):
        mark = f" {GREEN}←  currently active{RESET}" if lvl == current else ""
        print(f"  {CYAN}{i}.{RESET}  {label}{mark}")
    print(f"\n  {DIM}Enter to keep the current setting"
          f" ({access_label(current)}).{RESET}\n")

    valid = [str(i) for i in range(1, len(ACCESS_CHOICES) + 1)]
    while True:
        try:
            raw = reader(f"  Choice [keep current]: ")
        except (KeyboardInterrupt, EOFError):
            print(f"\n  {YELLOW}Cancelled — kept "
                  f"{access_label(current)}.{RESET}")
            return current
        key = menu_key(raw)
        if not key:
            # The ONLY legitimate default, and it is printed in the prompt.
            print(f"  {DIM}Kept: {access_summary(current)}{RESET}")
            return current
        if key in valid:
            picked = ACCESS_CHOICES[int(key) - 1][0]
            print(f"  {GREEN}✔ {access_summary(picked)}{RESET}")
            return picked
        print_unknown_choice(raw, valid + ["(empty = keep current)"])


# ─── Add ───────────────────────────────────────────────────────────────────────

def add_instance(data: dict = None, quiet_header: bool = False) -> str | None:
    """Add a new GitLab. Returns the instance name."""
    if data is None:
        data = _load()

    if not quiet_header:
        print(f"\n{BOLD}  ➕ New GitLab{RESET}\n")

    # Refuse BEFORE asking for anything: saving would overwrite a broken
    # instances.json (losing every GitLab in it) or a broken token file.
    problem = instances_file_problem() or creds.env_file_problem()
    if problem:
        print(f"  {RED}✖ {problem}{RESET}")
        print(f"  {YELLOW}Nothing was added — fix that file first.{RESET}")
        return None

    try:
        # 1) URL
        url = _normalize_url(_ask("GitLab URL (example: https://gitlab.com)"))
        if not url:
            print_warn("The URL was empty.")
            return None

        # 2) Name
        guess = url.split("//")[-1].split("/")[0].split(".")[0].capitalize()
        name  = _ask("Friendly name", guess)
        if any(i["name"].lower() == name.lower() for i in data["instances"]):
            print_warn(f"'{name}' already exists.")
            return None

        # 3) Token
        print(f"\n  {DIM}Create a token here: {url}/-/user_settings/personal_access_tokens{RESET}")
        print(f"  {DIM}Required scopes: api, read_repository, write_repository{RESET}")
        print(f"  {DIM}⚠  It must be a Personal Access Token — NOT a project/group bot token{RESET}\n")
        token = creds.prompt_token()
        if not token:
            print_warn("The token was empty.")
            return None

        # 4) Folder
        print(f"\n  {DIM}Where should the repos be cloned?{RESET}")
        base = _ask("Base folder", DEFAULT_MASTER)
        print(f"  {DIM}A subfolder avoids collisions when two GitLabs have"
              f" groups with the same name. Leave empty for no subfolder.{RESET}")
        folder = _ask("Subfolder for this GitLab", _slug(name))

    except (KeyboardInterrupt, EOFError):
        print()
        return None

    # 5) Test + capability
    print(f"\n  {DIM}Testing the connection...{RESET}", end="", flush=True)
    info = caps.probe(url, token)

    if info["ok"]:
        print(f"\r  {GREEN}✔ Connected — {caps.summary_line(info)}{RESET}      ")
        _print_caps(info)
    else:
        print(f"\r  {RED}✖ Test failed: {_probe_failure_line(info)}{RESET}      ")
        if ask_choice(f"  Save it anyway? (y/n): ", ["y", "n"],
                      on_eof="n") != "y":
            return None

    # 6) Access filter — only if there is anything worth filtering
    min_access = 0
    if info.get("ok") and info.get("top_groups", 0) > 3:
        min_access = _pick_access_level(0)

    # 7) Save
    var = creds.env_var_name(name)
    try:
        creds.set_token(var, token)
    except creds.CredentialsFileError as exc:
        _token_store_failed(exc)
        print(f"  {YELLOW}'{name}' was not added.{RESET}")
        return None

    inst = {
        "name":             name,
        "url":              url,
        "token_env":        var,
        "master_dir":       base,
        "folder":           folder,
        "min_access_level": min_access,
    }
    data["instances"].append(inst)
    if not data.get("active"):
        data["active"] = name
    if not _try_save(data):
        data["instances"].remove(inst)
        print(f"  {YELLOW}'{name}' was not added (its token is stored as "
              f"{var} in {creds.ENV_FILE}).{RESET}")
        return None

    print(f"\n  {GREEN}✔ '{name}' added{RESET}")
    print(f"  {DIM}Token → env var {var} (file: {creds.ENV_FILE}, chmod 600){RESET}")
    print(f"  {DIM}Repos → {os.path.join(base, folder)}/{RESET}")
    # The access filter changes WHICH GROUPS THE TOOL EVER LISTS, so it is
    # stated at every point it can have been set, not only inside the picker.
    print(f"  {DIM}{access_summary(min_access)}{RESET}\n")
    return name


def _print_caps(info: dict):
    """Show what this token is able to do."""
    c = info.get("caps", {})
    yes = [caps.CAP_LABELS[k] for k in caps.CAP_LABELS if c.get(k)]
    no  = [caps.CAP_LABELS[k] for k in caps.CAP_LABELS if not c.get(k)]
    print(f"  {DIM}You can:{RESET} {GREEN}{' · '.join(yes)}{RESET}")
    if no:
        print(f"  {DIM}You cannot: {' · '.join(no)}{RESET}")
    warns = info.get("warnings") or []
    if warns:
        print(f"  {YELLOW}⚠  {len(warns)} check(s) could not be answered — "
              f"the 'cannot' list may be incomplete:{RESET}")
        for w in warns:
            print(f"     {DIM}{w}{RESET}")


def _probe_failure_line(info: dict, name: str = None) -> str:
    """'Title — cause → fix' for a failed caps.probe(), with the fix naming the
    exact menu path. `name` = an existing instance; None = the add-a-GitLab
    flow, where the record does not exist yet."""
    code = info.get("error_code") or ""
    if name:
        def go(action):
            return f"Menu → b Instances → e Edit / delete → {name} → {action}"
        tok_fix = ("Create a Personal Access Token (scope api), then "
                   + go("1 New token"))
        url_fix = "Use the address you open in a browser: " + go("3 Change URL")
        dns_fix = "Connect the VPN, or fix the host name: " + go("3 Change URL")
    else:
        tok_fix = ("Answer n, create a Personal Access Token (scope api), "
                   "then + Add a new GitLab again")
        url_fix = ("Answer n, then + Add a new GitLab again with the address "
                   "you open in a browser")
        dns_fix = "Connect the VPN, or answer n and add it again with the right host"
    fix = {"auth": tok_fix, "forbidden": tok_fix, "token_missing": tok_fix,
           "not_found": url_fix, "dns": dns_fix}.get(code)
    if not fix and code in ("", "unknown") and info.get("error_cause"):
        fix = url_fix                       # a non-API page answered
    if not fix and info.get("error_line"):
        return info["error_line"]
    return creds.catalog_line(info.get("error") or "", code=code or None,
                              cause=info.get("error_cause"), fix=fix,
                              title=info.get("error_title"))


# ─── Picker ────────────────────────────────────────────────────────────────────

def pick_instance(force_menu: bool = True) -> dict | None:
    """GitLab selection menu. Returns the active config."""
    data = _load()

    while True:
        instances = data.get("instances", [])
        active    = data.get("active")

        print(f"\n{BOLD}{WHITE}  GitLabs:{RESET}\n")

        if not instances:
            print(f"  {DIM}You haven't added any GitLab yet.{RESET}\n")
        else:
            for i, inst in enumerate(instances, 1):
                is_active = inst["name"] == active
                mark      = f"{GREEN}●{RESET}" if is_active else f"{DIM}○{RESET}"
                url_short = inst["url"].split("//")[-1]
                folder    = build_cfg(inst)["master_dir"]
                tok       = creds.resolve_token(inst)
                tok_mark  = (f"{DIM}token ✓{RESET}" if tok
                             else f"{RED}token ✖{RESET}")
                lvl       = int(inst.get("min_access_level") or 0)
                lvl_str   = (f"  {DIM}[{caps.role_label(lvl)}+]{RESET}"
                             if lvl else "")

                print(f"  {CYAN}{i:>2}.{RESET}  {mark}  {BOLD}{inst['name']}{RESET}"
                      f"  {DIM}{url_short}{RESET}  {tok_mark}{lvl_str}")
                print(f"          {DIM}└─ {folder}/{RESET}")

        print(f"\n{DIM}  {'─'*45}{RESET}")
        print(f"  {GREEN}+{RESET}   Add a new GitLab")
        if instances:
            print(f"  {YELLOW}e{RESET}   Edit / delete")
            print(f"  {CYAN}d{RESET}   Doctor — health check")
        # THE LABEL IS THE CONTRACT. `q` here does `return None`, and both call
        # sites in gitlab_cli.interactive_mode treat None as "keep the current
        # instance and carry on" — it has never called sys.exit in its life.
        # printer.KEY_BACK is the word for that, so that is what is printed.
        print(f"  {YELLOW}q{RESET}   {printer_KEY_BACK}\n")

        try:
            choice = input(f"{BOLD}  Choice: {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            return None

        key = menu_key(choice)

        if key == "q":
            return None

        if key == "+":
            name = add_instance(data)
            data = _load()
            if name and len(data["instances"]) == 1:
                data["active"] = name
                _try_save(data)
            continue

        if key == "e" and instances:
            edit_instance(data)
            data = _load()
            continue

        if key == "d" and instances:
            # Local import: setup.py imports this module at the top, so a
            # module-level import here would be circular.
            import setup as setup_mod
            setup_mod.doctor()
            continue

        if key.isdigit():
            idx = int(key) - 1
            if 0 <= idx < len(instances):
                sel = instances[idx]
                if not creds.resolve_token(sel):
                    print(f"\n  {RED}No token is set for '{sel['name']}'.{RESET}")
                    if _relogin(data, idx) is False:
                        continue
                    data = _load()
                data["active"] = sel["name"]
                if not _try_save(data):
                    continue
                print(f"\n  {GREEN}✔ Active: {sel['name']}{RESET}")
                return get_active_instance()
            print(f"  {RED}Pick a number from 1 to {len(instances)}.{RESET}")
            continue

        # Search by name — case-insensitively, like every other key here.
        m = [i for i in instances if key and key in menu_key(i["name"])]
        if len(m) == 1:
            data["active"] = m[0]["name"]
            if not _try_save(data):
                continue
            print(f"\n  {GREEN}✔ Active: {m[0]['name']}{RESET}")
            return get_active_instance()
        if len(m) > 1:
            # Ambiguous is NOT "pick the first one". Say which ones matched.
            names = ", ".join(i["name"] for i in m)
            print(f"  {RED}'{choice}' matches {len(m)} GitLabs: {names}. "
                  f"Type more of the name, or its number.{RESET}")
            continue
        # Unrecognised input is never a default: name it and ask again.
        print_unknown_choice(
            choice,
            [str(i) for i in range(1, len(instances) + 1)]
            + ["+"] + (["e", "d"] if instances else []) + ["q"])


def _relogin(data: dict, idx: int) -> bool:
    inst = data["instances"][idx]
    if ask_choice(f"  Enter a token now? (y/n): ", ["y", "n"],
                  on_eof="n") != "y":
        return False
    try:
        token = creds.prompt_token()
    except (KeyboardInterrupt, EOFError):
        return False
    if not token:
        return False
    return _set_and_test(data, idx, token)


def _set_and_test(data: dict, idx: int, token: str) -> bool:
    inst = data["instances"][idx]
    print(f"  {DIM}Testing...{RESET}", end="", flush=True)
    info = caps.probe(inst["url"], token)
    if not info["ok"]:
        print(f"\r  {RED}✖ {_probe_failure_line(info, inst['name'])}{RESET}          ")
        if ask_choice(f"  Save it anyway? (y/n): ", ["y", "n"],
                      on_eof="n") != "y":
            return False
    else:
        print(f"\r  {GREEN}✔ {caps.summary_line(info)}{RESET}          ")

    var = inst.get("token_env") or creds.env_var_name(inst["name"])
    try:
        creds.set_token(var, token)
    except creds.CredentialsFileError as exc:
        _token_store_failed(exc)
        return False
    inst["token_env"] = var
    inst.pop("token", None)
    ok = _try_save(data)
    caps.clear_cache(inst["name"])
    return ok


# ─── Edit ──────────────────────────────────────────────────────────────────────

def edit_instance(data: dict):
    instances = data.get("instances", [])
    print(f"\n{BOLD}  Edit / delete:{RESET}\n")
    for i, inst in enumerate(instances, 1):
        print(f"  {CYAN}{i}.{RESET}  {inst['name']}  {DIM}{inst['url']}{RESET}")
    print(f"\n  {YELLOW}b{RESET}  back\n")

    valid = [str(i) for i in range(1, len(instances) + 1)] + ["b"]
    while True:
        try:
            choice = input(f"  Which one? [b = back] ")
        except (KeyboardInterrupt, EOFError):
            return
        key = menu_key(choice)
        if key in ("b", ""):
            return
        if key in valid:
            break
        print_unknown_choice(choice, valid)
    idx = int(key) - 1

    while True:
        inst   = data["instances"][idx]
        tok    = creds.resolve_token(inst)
        base   = inst.get("master_dir", DEFAULT_MASTER)
        folder = (inst.get("folder") or "").strip()
        lvl    = int(inst.get("min_access_level") or 0)

        print(f"\n  {BOLD}{inst['name']}{RESET}")
        print(f"  {DIM}URL     : {inst['url']}{RESET}")
        print(f"  {DIM}Token   : {creds.mask(tok)}  (env: {inst.get('token_env','-')}){RESET}")
        print(f"  {DIM}Folder  : {build_cfg(inst)['master_dir']}/{RESET}")
        print(f"  {DIM}Access  : {access_label(lvl)}{RESET}")
        print(f"  {DIM}          {access_summary(lvl)}{RESET}\n")

        print(f"  {CYAN}1.{RESET}  New token")
        print(f"  {CYAN}2.{RESET}  Rename")
        print(f"  {CYAN}3.{RESET}  Change URL")
        print(f"  {CYAN}4.{RESET}  Change folder")
        print(f"  {CYAN}5.{RESET}  Change access filter")
        print(f"  {CYAN}6.{RESET}  Test connection")
        print(f"  {RED}7.{RESET}  Delete this GitLab")
        print(f"\n  {YELLOW}b{RESET}  back\n")

        try:
            raw_action = input(f"  Choice [b = back]: ")
        except (KeyboardInterrupt, EOFError):
            return
        action = menu_key(raw_action)

        if action in ("b", ""):
            return

        if action not in ("1", "2", "3", "4", "5", "6", "7"):
            print_unknown_choice(raw_action,
                                 ["1", "2", "3", "4", "5", "6", "7", "b"])
            continue

        if action == "1":
            try:
                token = creds.prompt_token("New token")
            except (KeyboardInterrupt, EOFError):
                continue
            if token:
                _set_and_test(data, idx, token)

        elif action == "2":
            try:
                new = input(f"  New name [{inst['name']}]: ").strip()
            except (KeyboardInterrupt, EOFError):
                continue
            if new and new != inst["name"]:
                old_var = inst.get("token_env")
                new_var = creds.env_var_name(new)
                if tok:
                    try:
                        creds.set_token(new_var, tok)
                        if old_var and old_var != new_var:
                            creds.unset_token(old_var)
                    except creds.CredentialsFileError as exc:
                        _token_store_failed(exc)
                        print(f"  {YELLOW}Not renamed.{RESET}")
                        continue
                if data.get("active") == inst["name"]:
                    data["active"] = new
                caps.clear_cache(inst["name"])
                inst["name"]      = new
                inst["token_env"] = new_var
                if _try_save(data):
                    print(f"  {GREEN}✔ Updated{RESET}")

        elif action == "3":
            try:
                new = _normalize_url(input(f"  New URL [{inst['url']}]: ").strip())
            except (KeyboardInterrupt, EOFError):
                continue
            if new:
                inst["url"] = new
                caps.clear_cache(inst["name"])
                if _try_save(data):
                    print(f"  {GREEN}✔ Updated{RESET}")

        elif action == "4":
            try:
                nb = input(f"  Base folder [{base}]: ").strip() or base
                nf = input(f"  Subfolder (empty = no subfolder) "
                           f"[{folder or '-'}]: ").strip()
            except (KeyboardInterrupt, EOFError):
                continue
            inst["master_dir"] = nb
            inst["folder"]     = "" if nf == "-" else (nf or folder)
            if _try_save(data):
                print(f"  {GREEN}✔ {build_cfg(inst)['master_dir']}/{RESET}")

        elif action == "5":
            inst["min_access_level"] = _pick_access_level(lvl)
            if _try_save(data):
                print(f"  {GREEN}✔ {access_summary(inst['min_access_level'])}{RESET}")

        elif action == "6":
            print(f"  {DIM}Testing...{RESET}", end="", flush=True)
            info = caps.probe(inst["url"], tok)
            if info["ok"]:
                print(f"\r  {GREEN}✔ {caps.summary_line(info)}{RESET}       ")
                _print_caps(info)
                caps.clear_cache(inst["name"])
            else:
                print(f"\r  {RED}✖ {_probe_failure_line(info, inst['name'])}"
                      f"{RESET}       ")

        elif action == "7":
            c = ask_choice(f"  {RED}Really delete '{inst['name']}'? (y/n): "
                           f"{RESET}", ["y", "n"], on_eof="n")
            if c == "y":
                var = inst.get("token_env")
                if ask_choice(f"  Also remove the token from env? (y/n): ",
                              ["y", "n"], on_eof="n") == "y" and var:
                    try:
                        creds.unset_token(var)
                    except creds.CredentialsFileError as exc:
                        print(f"  {RED}✖ {exc}{RESET}")
                        print(f"  {YELLOW}The token {var} was NOT removed."
                              f"{RESET}")
                caps.clear_cache(inst["name"])
                data["instances"].pop(idx)
                if data.get("active") == inst["name"]:
                    data["active"] = (data["instances"][0]["name"]
                                      if data["instances"] else None)
                if _try_save(data):
                    print(f"  {GREEN}✔ Deleted{RESET}")
                return


def print_instances():
    """List every configured GitLab (the `instances` command)."""
    import caps as _caps
    data   = _load()
    active = data.get("active")
    insts  = data.get("instances", [])

    if not insts:
        print_warn("No GitLab is configured. Run `gitlab-cli setup`.")
        return

    print_header("Configured GitLabs", f"{len(insts)} instances")
    for i, inst in enumerate(insts, 1):
        cfg  = build_cfg(inst)
        mark = f"{GREEN}●{RESET}" if inst["name"] == active else f"{DIM}○{RESET}"
        tok  = creds.resolve_token(inst)
        if not tok:
            state = f"{RED}no token{RESET}"
        else:
            info = _caps.get(cfg)
            if info.get("ok"):
                state = f"{GREEN}{_caps.role_label(info)}{RESET}"
            else:
                state = f"{RED}{_probe_failure_line(info, inst['name'])}{RESET}"
        name = inst["name"]
        url  = inst["url"]
        print(f"  {mark} {CYAN}{i}.{RESET} {BOLD}{name}{RESET}")
        print(f"       {DIM}url      :{RESET} {url}")
        print(f"       {DIM}folder   :{RESET} {cfg['master_dir']}")
        print(f"       {DIM}token    :{RESET} {state}")
        # The token's role and the configured FILTER are different facts. The
        # old screen printed only the first under the label "access", so a
        # min_access_level that was hiding groups never appeared anywhere.
        print(f"       {DIM}access   :{RESET} "
              f"{access_summary(inst.get('min_access_level'))}")
        extra = unknown_instance_keys(inst)
        if extra:
            print(f"       {DIM}unknown  :{RESET} {YELLOW}"
                  f"{', '.join(extra)} — not a setting this version knows; "
                  f"kept in the file, ignored by the tool{RESET}")
    print()
