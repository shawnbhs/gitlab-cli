# setup.py — First run + health check
#
# Two jobs:
#   1. ensure_setup()  → if no GitLab is configured yet, run the wizard
#   2. doctor()        → full check: config, token, network, git, credentials
#
# The real "add an instance" wizard lives in instances.add_instance();
# this module only handles onboarding and diagnostics.

import os
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
    {WHITE}·{RESET} Personal Access Token {DIM}(scope: api, read/write_repository){RESET}

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
    print(f"  {DIM}· Menu → Manage      token / folder / new GitLab{RESET}")
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


# ─── Doctor ────────────────────────────────────────────────────────────────────

def _check(label: str, ok: bool, detail: str = "", warn: bool = False):
    if ok:
        icon, color = "✔", GREEN
    elif warn:
        icon, color = "!", YELLOW
    else:
        icon, color = "✖", RED
    line = f"  {color}{icon}{RESET}  {label:<34}"
    if detail:
        line += f" {DIM}{detail}{RESET}"
    print(line)
    return ok


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
    """Sensitive files must be 0600."""
    problems = []
    for f in (creds.ENV_FILE, I.INSTANCES_FILE):
        if not os.path.exists(f):
            _check(os.path.basename(f), True, "does not exist (that's fine)")
            continue
        mode = os.stat(f).st_mode & 0o777
        ok = mode <= 0o600
        _check(os.path.basename(f), ok, f"mode {oct(mode)[2:]}", warn=not ok)
        if not ok:
            problems.append(f"{f} is too open → chmod 600 {f}")
    return problems


def _instance_check() -> list[str]:
    """For each GitLab: is there a token? does it connect? what access does it have?"""
    problems = []
    insts = I.list_instances()

    if not insts:
        _check("GitLab configured", False, "none")
        return ["no GitLab is configured → Manage → New GitLab"]

    for inst in insts:
        name = inst["name"]
        print(f"\n  {BOLD}{WHITE}{name}{RESET} {DIM}{inst['url']}{RESET}")

        token = creds.resolve_token(inst)
        if not token:
            _check("token", False, f"not found ({inst.get('token_env','?')})")
            problems.append(f"[{name}] no token → Manage → New token")
            continue
        _check("token", True, creds.mask(token))

        info = caps.get(I.build_cfg(inst), force=True)
        if not info.get("ok"):
            err = info.get("error", "")
            _check("connects", False, err[:60])
            if "401" in err:
                problems.append(f"[{name}] token has expired → Manage → Token")
            elif "403" in err:
                problems.append(f"[{name}] token is missing the 'api' scope")
            else:
                problems.append(f"[{name}] server is not responding — check your VPN")
            continue

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

        # Folder
        cfg  = I.build_cfg(inst)
        d    = cfg["master_dir"]
        ex   = os.path.isdir(d)
        n    = len(os.listdir(d)) if ex else 0
        _check("folder", True,
               f"{os.path.abspath(d)}" + (f" ({n} items)" if ex else " (not created yet)"))

    return problems


def doctor():
    """Full health check."""
    print_header("Doctor", "checking everything that could be broken")

    print(f"  {BOLD}System{RESET}")
    problems = _git_check()

    print(f"\n  {BOLD}Files{RESET}")
    problems += _perm_check()

    print(f"\n  {BOLD}GitLab instances{RESET}")
    problems += _instance_check()

    print()
    if not problems:
        print_ok("  Everything is healthy ✔\n")
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
