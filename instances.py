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
from printer import (
    print_warn, print_header,
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

def _load() -> dict:
    if not os.path.exists(INSTANCES_FILE):
        return {"instances": [], "active": None}
    try:
        with open(INSTANCES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"instances": [], "active": None}
    data.setdefault("instances", [])
    data.setdefault("active", None)
    return data


def _save(data: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = INSTANCES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, INSTANCES_FILE)
    try:
        os.chmod(INSTANCES_FILE, stat.S_IRUSR | stat.S_IWUSR)   # 0600
    except OSError:
        pass


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
            creds.set_token(var, tok)
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

def build_cfg(inst: dict, allow_generic: bool = False) -> dict:
    """
    Raw instance → a config the rest of the modules can use.

    If 'folder' is empty, repos go straight into master_dir
    (legacy behaviour — existing configs keep working).
    If it is set, each GitLab gets its own subfolder, which avoids
    collisions when two GitLabs have groups with the same name.
    """
    token       = creds.resolve_token(inst, allow_generic=allow_generic)
    base_dir    = inst.get("master_dir", DEFAULT_MASTER)
    folder      = (inst.get("folder") or "").strip()
    inst_folder = os.path.join(base_dir, folder) if folder else base_dir

    return {
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


def _pick_access_level(current: int = 0) -> int:
    print(f"\n  {BOLD}Which groups should I show?{RESET}")
    print(f"  {DIM}If you only work on a few projects, filter them"
          f" so the list stays short.{RESET}\n")
    for i, (lvl, label) in enumerate(ACCESS_CHOICES, 1):
        mark = f" {GREEN}←{RESET}" if lvl == current else ""
        print(f"  {CYAN}{i}.{RESET}  {label}{mark}")
    print()
    try:
        c = input(f"  Choice [1]: ").strip() or "1"
    except (KeyboardInterrupt, EOFError):
        return current
    if c.isdigit() and 1 <= int(c) <= len(ACCESS_CHOICES):
        return ACCESS_CHOICES[int(c) - 1][0]
    return current


# ─── Add ───────────────────────────────────────────────────────────────────────

def add_instance(data: dict = None, quiet_header: bool = False) -> str | None:
    """Add a new GitLab. Returns the instance name."""
    if data is None:
        data = _load()

    if not quiet_header:
        print(f"\n{BOLD}  ➕ New GitLab{RESET}\n")

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
        print(f"\r  {RED}✖ Test failed: {info['error']}{RESET}      ")
        try:
            if input(f"  Save it anyway? (y/n): ").strip().lower() != "y":
                return None
        except (KeyboardInterrupt, EOFError):
            return None

    # 6) Access filter — only if there is anything worth filtering
    min_access = 0
    if info.get("ok") and info.get("top_groups", 0) > 3:
        min_access = _pick_access_level(0)

    # 7) Save
    var = creds.env_var_name(name)
    creds.set_token(var, token)

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
    _save(data)

    print(f"\n  {GREEN}✔ '{name}' added{RESET}")
    print(f"  {DIM}Token → env var {var} (file: {creds.ENV_FILE}, chmod 600){RESET}")
    print(f"  {DIM}Repos → {os.path.join(base, folder)}/{RESET}\n")
    return name


def _print_caps(info: dict):
    """Show what this token is able to do."""
    c = info.get("caps", {})
    yes = [caps.CAP_LABELS[k] for k in caps.CAP_LABELS if c.get(k)]
    no  = [caps.CAP_LABELS[k] for k in caps.CAP_LABELS if not c.get(k)]
    print(f"  {DIM}You can:{RESET} {GREEN}{' · '.join(yes)}{RESET}")
    if no:
        print(f"  {DIM}You cannot: {' · '.join(no)}{RESET}")


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
        print(f"  {YELLOW}q{RESET}   exit\n")

        try:
            choice = input(f"{BOLD}  Choice: {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            return None

        if choice.lower() == "q":
            return None

        if choice == "+":
            name = add_instance(data)
            data = _load()
            if name and len(data["instances"]) == 1:
                data["active"] = name
                _save(data)
            continue

        if choice.lower() == "e" and instances:
            edit_instance(data)
            data = _load()
            continue

        if choice.lower() == "d" and instances:
            # Local import: setup.py imports this module at the top, so a
            # module-level import here would be circular.
            import setup as setup_mod
            setup_mod.doctor()
            continue

        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(instances):
                sel = instances[idx]
                if not creds.resolve_token(sel):
                    print(f"\n  {RED}No token is set for '{sel['name']}'.{RESET}")
                    if _relogin(data, idx) is False:
                        continue
                    data = _load()
                data["active"] = sel["name"]
                _save(data)
                print(f"\n  {GREEN}✔ Active: {sel['name']}{RESET}")
                return get_active_instance()
            print(f"  {RED}Pick a number from 1 to {len(instances)}.{RESET}")
            continue

        # Search by name
        m = [i for i in instances if choice.lower() in i["name"].lower()]
        if len(m) == 1:
            data["active"] = m[0]["name"]
            _save(data)
            print(f"\n  {GREEN}✔ Active: {m[0]['name']}{RESET}")
            return get_active_instance()
        if not m:
            print(f"  {RED}'{choice}' not found.{RESET}")


def _relogin(data: dict, idx: int) -> bool:
    inst = data["instances"][idx]
    try:
        if input(f"  Enter a token now? (y/n): ").strip().lower() != "y":
            return False
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
        print(f"\r  {RED}✖ {info['error']}{RESET}          ")
        try:
            if input(f"  Save it anyway? (y/n): ").strip().lower() != "y":
                return False
        except (KeyboardInterrupt, EOFError):
            return False
    else:
        print(f"\r  {GREEN}✔ {caps.summary_line(info)}{RESET}          ")

    var = inst.get("token_env") or creds.env_var_name(inst["name"])
    creds.set_token(var, token)
    inst["token_env"] = var
    inst.pop("token", None)
    _save(data)
    caps.clear_cache(inst["name"])
    return True


# ─── Edit ──────────────────────────────────────────────────────────────────────

def edit_instance(data: dict):
    instances = data.get("instances", [])
    print(f"\n{BOLD}  Edit / delete:{RESET}\n")
    for i, inst in enumerate(instances, 1):
        print(f"  {CYAN}{i}.{RESET}  {inst['name']}  {DIM}{inst['url']}{RESET}")
    print(f"\n  {YELLOW}b{RESET}  back\n")

    try:
        choice = input(f"  Which one? ").strip()
    except (KeyboardInterrupt, EOFError):
        return
    if not choice.isdigit():
        return
    idx = int(choice) - 1
    if not (0 <= idx < len(instances)):
        return

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
        print(f"  {DIM}Access  : {caps.role_label(lvl) + '+' if lvl else 'all'}{RESET}\n")

        print(f"  {CYAN}1.{RESET}  New token")
        print(f"  {CYAN}2.{RESET}  Rename")
        print(f"  {CYAN}3.{RESET}  Change URL")
        print(f"  {CYAN}4.{RESET}  Change folder")
        print(f"  {CYAN}5.{RESET}  Change access filter")
        print(f"  {CYAN}6.{RESET}  Test connection")
        print(f"  {RED}7.{RESET}  Delete this GitLab")
        print(f"\n  {YELLOW}b{RESET}  back\n")

        try:
            action = input(f"  Choice: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return

        if action in ("b", ""):
            return

        elif action == "1":
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
                    creds.set_token(new_var, tok)
                    if old_var and old_var != new_var:
                        creds.unset_token(old_var)
                if data.get("active") == inst["name"]:
                    data["active"] = new
                caps.clear_cache(inst["name"])
                inst["name"]      = new
                inst["token_env"] = new_var
                _save(data)
                print(f"  {GREEN}✔ Updated{RESET}")

        elif action == "3":
            try:
                new = _normalize_url(input(f"  New URL [{inst['url']}]: ").strip())
            except (KeyboardInterrupt, EOFError):
                continue
            if new:
                inst["url"] = new
                caps.clear_cache(inst["name"])
                _save(data)
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
            _save(data)
            print(f"  {GREEN}✔ {build_cfg(inst)['master_dir']}/{RESET}")

        elif action == "5":
            inst["min_access_level"] = _pick_access_level(lvl)
            _save(data)
            print(f"  {GREEN}✔ Updated{RESET}")

        elif action == "6":
            print(f"  {DIM}Testing...{RESET}", end="", flush=True)
            info = caps.probe(inst["url"], tok)
            if info["ok"]:
                print(f"\r  {GREEN}✔ {caps.summary_line(info)}{RESET}       ")
                _print_caps(info)
                caps.clear_cache(inst["name"])
            else:
                print(f"\r  {RED}✖ {info['error']}{RESET}       ")

        elif action == "7":
            try:
                c = input(f"  {RED}Really delete '{inst['name']}'? (y/n): {RESET}")
            except (KeyboardInterrupt, EOFError):
                continue
            if c.strip().lower() == "y":
                var = inst.get("token_env")
                try:
                    if input(f"  Also remove the token from env? (y/n): ").strip().lower() == "y" and var:
                        creds.unset_token(var)
                except (KeyboardInterrupt, EOFError):
                    pass
                caps.clear_cache(inst["name"])
                data["instances"].pop(idx)
                if data.get("active") == inst["name"]:
                    data["active"] = (data["instances"][0]["name"]
                                      if data["instances"] else None)
                _save(data)
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
                state = f"{RED}{info.get('error', '?')}{RESET}"
        name = inst["name"]
        url  = inst["url"]
        print(f"  {mark} {CYAN}{i}.{RESET} {BOLD}{name}{RESET}")
        print(f"       {DIM}url      :{RESET} {url}")
        print(f"       {DIM}folder   :{RESET} {cfg['master_dir']}")
        print(f"       {DIM}access   :{RESET} {state}")
    print()
