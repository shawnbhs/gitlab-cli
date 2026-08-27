# errors.py — Centralized error logging and viewer

import os
import re
import json
import stat
from datetime import datetime, timedelta
from printer import (
    print_section, print_warn, print_separator,
    BOLD, CYAN, GREEN, YELLOW, DIM, RESET, RED
)

ERROR_DIR  = os.path.expanduser("~/.gitlab-cli")
ERROR_FILE = os.path.join(ERROR_DIR, "errors.log")
MAX_DAYS   = 30


# ─── Redaction ─────────────────────────────────────────────────────────────────
#
# An error message may be raw git stderr. Git usually strips the token from the
# URL itself, but not on every version and not on every code path. Since this
# message lands on disk, we scrub anything that looks like a token before
# writing it out — defense in depth.

_SECRET_PATTERNS = [
    # GitLab token (glpat-, gldt-, glrt-, glsoat-, glcbt-, ...)
    re.compile(r"\bgl[a-z]{2,6}-[A-Za-z0-9_\-]{16,}"),
    # URL userinfo:  https://user:secret@host
    re.compile(r"(?<=://)([^/\s:@]+):([^/\s@]+)(?=@)"),
    # GitHub / OpenAI tokens, just to be safe
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9\-_]{16,})"),
]


def redact(text: str) -> str:
    """Strip anything that looks like a token out of the given text."""
    if not text:
        return text
    out = _SECRET_PATTERNS[0].sub("[REDACTED-TOKEN]", text)
    out = _SECRET_PATTERNS[1].sub(r"\1:[REDACTED]", out)
    out = _SECRET_PATTERNS[2].sub("[REDACTED-TOKEN]", out)
    return out


# Legacy alias kept for internal use
_redact = redact


# ─── Known errors + solutions ──────────────────────────────────────────────────

KNOWN_ERRORS = [
    {
        "pattern":  "NameResolutionError",
        "type":     "Network",
        "title":    "DNS resolution failed",
        "fix":      "Check your VPN/network. Try pinging the GitLab host.",
    },
    {
        "pattern":  "Max retries exceeded",
        "type":     "Network",
        "title":    "Connection timeout",
        "fix":      "Internet or VPN is down. Try again.",
    },
    {
        "pattern":  "ConnectionError",
        "type":     "Network",
        "title":    "Connection refused",
        "fix":      "Server is unreachable. Check with your admin.",
    },
    {
        "pattern":  "401",
        "type":     "Token",
        "title":    "Token expired or invalid",
        "fix":      "Create a new token: GitLab → Settings → Access Tokens",
    },
    {
        "pattern":  "403",
        "type":     "Token",
        "title":    "Insufficient access",
        "fix":      "Check the token scopes — it needs read_api and api.",
    },
    {
        "pattern":  "404",
        "type":     "API",
        "title":    "Resource not found",
        "fix":      "Check the group/project name. It may have been deleted.",
    },
    {
        "pattern":  "500",
        "type":     "Server",
        "title":    "GitLab server error",
        "fix":      "The problem is server-side. Check with your admin or retry later.",
    },
    {
        "pattern":  "RPC failed",
        "type":     "Git",
        "title":    "Git clone/pull failed",
        "fix":      "Slow network or a very large repo. Retry.",
    },
    {
        "pattern":  "curl 18",
        "type":     "Git",
        "title":    "Transfer was interrupted",
        "fix":      "Retry. If it keeps happening: git config --global http.postBuffer 524288000",
    },
    {
        "pattern":  "not a git repo",
        "type":     "Git",
        "title":    "Folder is not a git repo",
        "fix":      "Clone it first, then run pull/log.",
    },
    {
        "pattern":  "GITLAB_TOKEN",
        "type":     "Config",
        "title":    "Token is not set",
        "fix":      "Check your .env file: GITLAB_TOKEN=glpat-...",
    },
    {
        "pattern":  "MASTER_DIR",
        "type":     "Config",
        "title":    "Master folder is not set",
        "fix":      "Set MASTER_DIR=/path/to/folder in your .env file.",
    },
]


def _match_error(message: str) -> dict | None:
    msg_lower = message.lower()
    for e in KNOWN_ERRORS:
        if e["pattern"].lower() in msg_lower:
            return e
    return None


# ─── Log / Read ────────────────────────────────────────────────────────────────

def log_error(context: str, message: str, resolved: bool = False):
    """Save an error to the log file."""
    os.makedirs(ERROR_DIR, exist_ok=True)

    known = _match_error(message)
    entry = {
        "date":     datetime.now().isoformat()[:16],
        "context":  context,
        "message":  _redact(message)[:300],
        "type":     known["type"]  if known else "Unknown",
        "title":    known["title"] if known else "Unknown error",
        "fix":      known["fix"]   if known else "Read the log and follow up with your admin.",
        "resolved": resolved,
    }

    # The log can contain raw git stderr — keep it readable by the owner only
    new = not os.path.exists(ERROR_FILE)
    with open(ERROR_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if new:
        try:
            os.chmod(ERROR_FILE, stat.S_IRUSR | stat.S_IWUSR)   # 0600
        except OSError:
            pass


def _read_errors() -> list:
    """Read all errors, auto-cleanup older than 30 days."""
    if not os.path.exists(ERROR_FILE):
        return []

    cutoff = datetime.now() - timedelta(days=MAX_DAYS)
    valid  = []

    with open(ERROR_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                entry_date = datetime.fromisoformat(entry["date"])
                if entry_date >= cutoff:
                    valid.append(entry)
            except (ValueError, TypeError, KeyError):
                # Corrupt line or missing date — skip legacy log entries.
                continue

    # Rewrite file with only valid entries
    with open(ERROR_FILE, "w", encoding="utf-8") as f:
        for entry in valid:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return valid


# ─── Display ───────────────────────────────────────────────────────────────────

TYPE_COLORS = {
    "Network": "\033[33m",   # yellow
    "Token":   "\033[31m",   # red
    "Config":  "\033[35m",   # magenta
    "Git":     "\033[36m",   # cyan
    "API":     "\033[34m",   # blue
    "Server":  "\033[31m",   # red
    "Unknown": "\033[37m",   # white
}


def cmd_errors(cfg: dict = None):
    """Show error log with filters and resolve option."""
    print_section("Error Log")

    errors = _read_errors()

    if not errors:
        print(f"\n  {GREEN}✔ No errors logged!{RESET}\n")
        return

    # Stats
    total      = len(errors)
    unresolved = [e for e in errors if not e.get("resolved")]
    resolved   = [e for e in errors if e.get("resolved")]

    print(f"\n  {BOLD}Total     :{RESET}  {total}")
    print(f"  {RED}Unresolved:{RESET}  {RED}{len(unresolved)}{RESET}")
    print(f"  {GREEN}Resolved  :{RESET}  {GREEN}{len(resolved)}{RESET}")
    print(f"  {DIM}Entries older than 30 days are auto-deleted{RESET}\n")

    # Filter menu
    print(f"  {CYAN}1.{RESET}  All errors")
    print(f"  {CYAN}2.{RESET}  Unresolved only")
    print(f"  {CYAN}3.{RESET}  Resolved only")
    print(f"  {CYAN}4.{RESET}  Filter by type  (Network/Token/Git/...)")
    print(f"\n  {YELLOW}q{RESET}  exit\n")

    try:
        choice = input(f"{BOLD}  Filter: {RESET}").strip()
    except (KeyboardInterrupt, EOFError):
        return

    if choice == "q":
        return
    elif choice == "1":
        filtered = errors
    elif choice == "2":
        filtered = unresolved
    elif choice == "3":
        filtered = resolved
    elif choice == "4":
        types = sorted(set(e["type"] for e in errors))
        print(f"\n  Types: {', '.join(types)}")
        try:
            t = input(f"  Type: ").strip()
        except (KeyboardInterrupt, EOFError):
            return
        filtered = [e for e in errors if e["type"].lower() == t.lower()]
    else:
        filtered = errors

    if not filtered:
        print_warn("No errors match this filter.")
        return

    # Display errors
    print_separator()
    print(f"\n{BOLD}  {len(filtered)} error:{RESET}\n")

    for i, e in enumerate(sorted(filtered, key=lambda x: x["date"], reverse=True), 1):
        color     = TYPE_COLORS.get(e["type"], "\033[37m")
        resolved  = e.get("resolved", False)
        res_mark  = f"{GREEN}[OK]{RESET}" if resolved else f"{RED}[!]{RESET}"
        type_str  = f"{color}{e['type']:8}{RESET}"

        print(f"  {CYAN}{i:>3}.{RESET}  {res_mark}  {type_str}  {DIM}{e['date']}{RESET}")
        print(f"         {BOLD}{e['title']}{RESET}")
        print(f"         {DIM}Context: {e['context']}{RESET}")
        print(f"         {YELLOW}→ {e['fix']}{RESET}")
        print()

    # Resolve option
    print_separator()
    print(f"\n  {CYAN}r{RESET}  Mark as resolved  (enter numbers)")
    print(f"  {CYAN}c{RESET}  Clear all resolved")
    print(f"  {YELLOW}q{RESET}  exit\n")

    try:
        action = input(f"{BOLD}  Choice: {RESET}").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return

    if action == "q":
        return

    elif action == "c":
        # Clear resolved
        all_errors = _read_errors()
        remaining  = [e for e in all_errors if not e.get("resolved")]
        with open(ERROR_FILE, "w", encoding="utf-8") as f:
            for e in remaining:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        print(f"\n  {GREEN}✔ Cleared {len(all_errors) - len(remaining)} resolved errors.{RESET}\n")

    elif action == "r":
        try:
            nums = input(f"  Numbers (comma separated, e.g. 1,3,5): ").strip()
        except (KeyboardInterrupt, EOFError):
            return

        indices = []
        for n in nums.split(","):
            n = n.strip()
            if n.isdigit():
                idx = int(n) - 1
                if 0 <= idx < len(filtered):
                    indices.append(idx)

        if not indices:
            print_warn("Invalid number entered.")
            return

        # Mark as resolved in file
        targets = [filtered[i]["date"] + filtered[i]["context"] for i in indices]
        all_errors = _read_errors()
        count = 0
        for e in all_errors:
            key = e["date"] + e["context"]
            if key in targets and not e.get("resolved"):
                e["resolved"] = True
                count += 1

        with open(ERROR_FILE, "w", encoding="utf-8") as f:
            for e in all_errors:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

        print(f"\n  {GREEN}✔ {count} error(s) marked resolved.{RESET}\n")
