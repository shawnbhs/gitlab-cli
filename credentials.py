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
#
# Error rule (T13): a config file that EXISTS but cannot be read is never
# treated as "empty". The old read_env_file() swallowed OSError and returned
# {}, and set_token() then rewrote the env file from that {} — silently
# deleting every other instance's token. Reads that feed a WRITE now raise
# CredentialsFileError; display-only reads warn once (stderr) and go on.

import contextlib
import os
import re
import stat
import sys
import getpass

CONFIG_DIR = os.path.expanduser("~/.gitlab-cli")
ENV_FILE   = os.path.join(CONFIG_DIR, "env")

ENV_PREFIX = "GITLAB_TOKEN_"

# The exact place in the interactive UI where a token is replaced. Every
# "fix" line that tells the user to set a token names this path.
# A UI path, not a secret (S105 matches on the name).
TOKEN_MENU = "Menu → b Instances → e Edit / delete → {name} → 1 New token"  # noqa: S105


class CredentialsFileError(RuntimeError):
    """The env file exists but cannot be read — it must not be overwritten.

    str(exc) is a finished one-line explanation ("Title — cause → fix").
    """


# ─── One-line explanations through error_catalog ──────────────────────────────

_WARNED: set = set()


def catalog_line(message: str = "", *, code: str = None, cause: str = None,
                 fix: str = None, title: str = None) -> str:
    """'Title — cause → fix' for a failure, via error_catalog.

    `code` picks a catalog entry directly; otherwise the entry is found by
    explain(message). `cause`/`fix` override the catalog's generic wording with
    something specific (a file path and line, an exact menu path). `title` is
    used only when the catalog has no entry for this failure — the raw message
    summary then becomes the cause, so an unknown failure is never reduced to
    "something went wrong".

    Lives in this module because it is the lowest of instances/caps/credentials
    (no project imports), so all three can share one formatter. Never raises:
    a broken catalog degrades to the raw message, it does not hide it.
    """
    msg = message or ""
    try:
        import error_catalog as _ec
        info = (_ec.by_code(code) if code else None) or _ec.explain(msg)
        t, c, f = info.title, info.cause, info.fix
        known = info.code != "unknown"
        summ = _ec.summarize(msg, 110)
    except Exception:                       # noqa: BLE001 - degrade, never hide
        lines = [ln.strip() for ln in msg.splitlines() if ln.strip()]
        summ = (lines[-1] if lines else "")[:110]
        t, c, f, known = "Error", "", "", False
    if not known:
        t = title or t
        c = summ or c
    return f"{t} — {cause or c} → {fix or f}"


def warn_once(key, line: str) -> None:
    """Print a warning line to stderr, once per process per `key`.

    stderr, not stdout: `gitlab-cli mcp` speaks JSON-RPC on stdout, and these
    readers run inside it too.
    """
    if key in _WARNED:
        return
    _WARNED.add(key)
    # A closed or broken stderr must not turn a warning into a crash.
    with contextlib.suppress(Exception):
        print(f"  ⚠  {line}", file=sys.stderr, flush=True)


def _perm_title(default: str, code: str) -> str:
    """A catalog title when the catalog has a dedicated code, else `default`."""
    # The catalog is optional here; any failure keeps the caller's default.
    with contextlib.suppress(Exception):
        import error_catalog as _ec
        info = _ec.by_code(code)
        if info is not None:
            return info.title
    return default


def _too_open_line(path: str, mode: int) -> str:
    title = _perm_title("Token file readable by others", "token_file_open")
    return (f"{title} — {path} is mode {mode:04o}, so other users or any copy "
            f"of it can read the tokens → run `chmod 600 {path}`, then rotate "
            f"any token that may have been copied")


def check_private_mode(path: str) -> int | None:
    """Warn (once) if `path` is readable/writable by group or others.

    Returns the mode, or None if the file is absent. The file is NOT chmod'ed
    here: tightening it silently would hide that the token was already exposed,
    and only rotation un-discloses a token.
    """
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        return None
    except OSError:
        return None                         # reported by the reader itself
    if mode & 0o077:
        warn_once(("mode", path, mode), _too_open_line(path, mode))
    return mode


# ─── Env var name ──────────────────────────────────────────────────────────────

def env_var_name(instance_name: str) -> str:
    """'Work Hub' → 'GITLAB_TOKEN_WORK_HUB'"""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", instance_name).strip("_").upper()
    return f"{ENV_PREFIX}{slug or 'DEFAULT'}"


# ─── Private files ────────────────────────────────────────────────────────────

def write_private_file(path: str, text: str) -> None:
    """Write `text` to `path` as 0600 FROM THE FIRST BYTE, atomically.

    The old writers did open(tmp, "w") (umask → usually 0644), wrote the
    secret, renamed, and only then chmod'ed — a window in which the file was
    world-readable, and a swallowed chmod error left it that way for good.
    Raises OSError on failure; the temp file is removed.
    """
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if hasattr(os, "fchmod"):
                # O_CREAT's mode is ignored when tmp already existed.
                os.fchmod(f.fileno(), stat.S_IRUSR | stat.S_IWUSR)
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)             # 0600
    except OSError:
        pass                                # verified just below instead
    check_private_mode(path)                # e.g. a filesystem ignoring chmod


# ─── Reading / writing the env file ───────────────────────────────────────────

def _ensure_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, stat.S_IRWXU)          # 0700
    except OSError as exc:
        warn_once(("dirmode", CONFIG_DIR), catalog_line(
            f"PermissionError: {exc}", code="permission_denied",
            cause=f"{CONFIG_DIR} could not be set to 0700 ({exc.strerror})",
            fix=f"Run `chmod 700 {CONFIG_DIR}` and make sure your user owns it"))


def _env_read_error(exc: Exception) -> CredentialsFileError:
    reason = getattr(exc, "strerror", None) or str(exc)
    return CredentialsFileError(catalog_line(
        f"{type(exc).__name__}: {exc}",
        code="permission_denied" if isinstance(exc, PermissionError) else None,
        title="Token file cannot be read",
        cause=f"{ENV_FILE} exists but cannot be read ({reason}); tokens unknown",
        fix=f"Run `ls -l {ENV_FILE}`, then `chmod 600 {ENV_FILE}` as its owner"))


def _read_env_strict() -> dict:
    """Parse ~/.gitlab-cli/env → {VAR: value}. Raises CredentialsFileError
    when the file exists but cannot be read (never returns a fake {})."""
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
    except (OSError, UnicodeDecodeError) as exc:
        raise _env_read_error(exc) from exc
    check_private_mode(ENV_FILE)
    return out


def read_env_file() -> dict:
    """Parse ~/.gitlab-cli/env → {VAR: value}.

    For READ-ONLY callers: an unreadable file is reported once on stderr and
    treated as holding no tokens. Anything that writes the file back must use
    the strict reader (set_token/unset_token do).
    """
    try:
        return _read_env_strict()
    except CredentialsFileError as exc:
        warn_once(("envread", ENV_FILE), str(exc))
        return {}


def env_file_problem() -> str:
    """'' if the env file is absent or readable, else the one-line reason."""
    try:
        _read_env_strict()
    except CredentialsFileError as exc:
        return str(exc)
    return ""


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
    write_private_file(ENV_FILE, "\n".join(lines) + "\n")


def set_token(var_name: str, token: str):
    """Store the token in the env file AND export it into the current process.

    Raises CredentialsFileError (and writes nothing) when the existing env
    file cannot be read — rewriting it would delete every other token.
    """
    values = _read_env_strict()
    values[var_name] = token
    write_env_file(values)
    os.environ[var_name] = token


def unset_token(var_name: str):
    values = _read_env_strict()
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
