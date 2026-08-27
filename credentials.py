# credentials.py — Token handling: env vars, secure env file, git credential store
#
# Golden rule: a token is never printed to the screen and never stored in
# instances.json (which may end up committed).
#
# Order in which a token is looked up for an instance:
#   1. dedicated env var     → GITLAB_TOKEN_<NAME>
#   2. token_env field        → whatever env var name the user picked
#   3. GITLAB_TOKEN            → generic env var (only for the active instance)
#   4. token field             → legacy (inline in instances.json) — gets migrated

import os
import re
import stat
import getpass

CONFIG_DIR = os.path.expanduser("~/.gitlab-cli")
ENV_FILE   = os.path.join(CONFIG_DIR, "env")

ENV_PREFIX = "GITLAB_TOKEN_"


# ─── Env var name ──────────────────────────────────────────────────────────────

def env_var_name(instance_name: str) -> str:
    """'Work Hub' → 'GITLAB_TOKEN_WORK_HUB'"""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", instance_name).strip("_").upper()
    return f"{ENV_PREFIX}{slug or 'DEFAULT'}"


# ─── Reading / writing the env file ───────────────────────────────────────────

def _ensure_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, stat.S_IRWXU)          # 0700
    except OSError:
        pass


def read_env_file() -> dict:
    """Parse ~/.gitlab-cli/env → {VAR: value}"""
    if not os.path.exists(ENV_FILE):
        return {}
    out = {}
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:]
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def write_env_file(values: dict):
    """Write with chmod 600 — the file is always rewritten in full."""
    _ensure_dir()
    lines = [
        "# gitlab-cli — tokens. Do NOT commit this file.",
        "# Load: source ~/.gitlab-cli/env",
        "",
    ]
    for k in sorted(values):
        lines.append(f"export {k}='{values[k]}'")
    tmp = ENV_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, ENV_FILE)
    try:
        os.chmod(ENV_FILE, stat.S_IRUSR | stat.S_IWUSR)   # 0600
    except OSError:
        pass


def set_token(var_name: str, token: str):
    """Store the token in the env file AND export it into the current process."""
    values = read_env_file()
    values[var_name] = token
    write_env_file(values)
    os.environ[var_name] = token


def unset_token(var_name: str):
    values = read_env_file()
    if values.pop(var_name, None) is not None:
        write_env_file(values)
    os.environ.pop(var_name, None)


# ─── Resolve ───────────────────────────────────────────────────────────────────

def resolve_token(inst: dict, allow_generic: bool = False) -> str:
    """Find an instance's token. Returns '' if there isn't one."""
    file_vals = read_env_file()

    def lookup(var: str) -> str:
        return (os.environ.get(var) or file_vals.get(var) or "").strip()

    # 1) dedicated env var
    tok = lookup(env_var_name(inst.get("name", "")))
    if tok:
        return tok

    # 2) custom env var
    custom = (inst.get("token_env") or "").strip()
    if custom:
        tok = lookup(custom)
        if tok:
            return tok

    # 3) generic env var
    if allow_generic:
        tok = lookup("GITLAB_TOKEN")
        if tok:
            return tok

    # 4) legacy inline
    return (inst.get("token") or "").strip()


def mask(token: str) -> str:
    """
    glpat-…wxyz — just the type prefix and the last 4 characters.
    Enough to tell which token it is, too little to be useful to anyone
    shoulder-surfing.
    """
    if not token:
        return "(not set)"
    if len(token) <= 8:
        return "*" * len(token)
    # Keep the known prefix (glpat-, gldt-, ...)
    prefix = ""
    if "-" in token[:8]:
        prefix = token[:token.index("-") + 1]
    return f"{prefix}…{token[-4:]}"


# ─── Secret input ──────────────────────────────────────────────────────────────

def prompt_token(label: str = "Token") -> str:
    """
    Read a token without echoing it to the terminal.
    If the terminal isn't a TTY (pipe/CI) we fall back to plain input with a warning.
    """
    try:
        return getpass.getpass(f"  {label} (hidden — your typing is not shown): ").strip()
    except (KeyboardInterrupt, EOFError):
        raise
    except Exception:
        # Environment without a TTY
        print("  ⚠  This terminal does not support hidden input.")
        return input(f"  {label}: ").strip()


# ─── Aliases ───────────────────────────────────────────────────────────────────

def save_token(inst_or_name, token: str) -> str:
    """
    Store the token in the secure env file (chmod 600).
    Accepts either an instance dict or a plain name.
    Returns the env var name.
    """
    name = inst_or_name.get("name") if isinstance(inst_or_name, dict) else inst_or_name
    var  = env_var_name(name)
    set_token(var, token)
    return var
