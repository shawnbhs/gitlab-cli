# remotes.py — Overview of every repo's remotes + URL safety
#
# Two main jobs:
#   1. audit()  → which repo has how many remotes, whether they are healthy,
#                 and whether a token is hardcoded into the URL
#   2. scrub()  → strip the token out of remote URLs and hand it to the
#                 git credential store (safer + a shared repo won't leak it)

import os
import re
import subprocess

import git as G
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


# ─── Reading the remotes ───────────────────────────────────────────────────────

def list_remotes(path: str) -> list[dict]:
    """All remotes of a repo, with full details."""
    rc, out, _ = G._git(path, "remote", "-v")
    if rc != 0:
        return []

    seen = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        name, url, kind = parts[0], parts[1], parts[2].strip("()")
        e = seen.setdefault(name, {"name": name, "fetch": "", "push": ""})
        if kind in ("fetch", "push"):
            e[kind] = url

    remotes = []
    for name, e in seen.items():
        url = e["fetch"] or e["push"]
        clean, cred = strip_creds(url)
        remotes.append({
            "name":       name,
            "url":        url,
            "clean_url":  clean,
            "has_creds":  bool(cred),
            "host":       clean.split("://")[-1].split("/")[0] if "://" in clean else "",
            "push_differs": bool(e["push"] and e["fetch"] and e["push"] != e["fetch"]),
            "protocol":   ("ssh" if clean.startswith(("git@", "ssh://"))
                           else "https" if clean.startswith("https://")
                           else "http" if clean.startswith("http://")
                           else "local"),
        })
    return sorted(remotes, key=lambda r: (r["name"] != "origin", r["name"]))


def _reachable(path: str, remote: str) -> tuple[bool, str]:
    """Check whether the remote responds, without doing a full fetch."""
    rc, _, err = G._git(path, "ls-remote", "--exit-code", "-h", remote, timeout=25)
    if rc == 0:
        return True, ""
    e = err[:120]
    if G.is_auth_error(e):
        return False, "auth"
    if "timeout" in e.lower() or rc == 124:
        return False, "timeout"
    return False, "unreachable"


# ─── Audit ─────────────────────────────────────────────────────────────────────

def audit(repos: list[str], check_network: bool = False) -> list[dict]:
    """Inspect the remotes of every repo."""
    rows = []
    for path in repos:
        remotes = list_remotes(path)
        leaks   = [r["name"] for r in remotes if r["has_creds"]]
        row = {
            "path": path,
            "name": os.path.basename(path),
            "remotes": remotes,
            "count": len(remotes),
            "leaks": leaks,
            "has_creds": bool(leaks),
            "hosts": sorted({r["host"] for r in remotes if r["host"]}),
            "unreachable": [],
        }
        if check_network:
            for r in remotes:
                ok, why = _reachable(path, r["name"])
                if not ok:
                    row["unreachable"].append(f"{r['name']} ({why})")
        rows.append(row)
    return rows


def print_audit(rows: list[dict], show_all: bool = False):
    """Print a human-readable report."""
    multi   = [r for r in rows if r["count"] > 1]
    leaky   = [r for r in rows if r["leaks"]]
    nothing = [r for r in rows if r["count"] == 0]
    broken  = [r for r in rows if r["unreachable"]]

    print_header("Remotes")

    print(f"  {BOLD}{len(rows)}{RESET} repos inspected\n")
    print(f"  {GREEN}●{RESET} 1 remote        : {len(rows) - len(multi) - len(nothing)}")
    print(f"  {CYAN}●{RESET} multi remote    : {len(multi)}")
    if nothing:
        print(f"  {YELLOW}●{RESET} no remote       : {len(nothing)}")
    if leaky:
        print(f"  {RED}●{RESET} token in URL    : {len(leaky)}  {DIM}← security risk{RESET}")
    if broken:
        print(f"  {RED}●{RESET} not responding  : {len(broken)}")

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
                f"{GREEN if rm['name'] == 'origin' else CYAN}{rm['name']}{RESET}"
                f"{DIM}({rm['protocol']}){RESET}"
                for rm in r["remotes"]
            )
            print(f"    {WHITE}{r['name']}{RESET}  {names}")
            if show_all:
                for rm in r["remotes"]:
                    print(f"        {DIM}{rm['name']:12} {mask_url(rm['url'])}{RESET}")

    if leaky:
        print(f"\n  {BOLD}{RED}Token in remote URL (must be removed):{RESET}")
        for r in leaky[:20]:
            print(f"    {r['name']}  {DIM}→ {', '.join(r['leaks'])}{RESET}")
        if len(leaky) > 20:
            print(f"    {DIM}… and {len(leaky) - 20} more{RESET}")
        print(f"\n  {YELLOW}→ Run the 'Strip tokens from remotes' option{RESET}")

    if nothing:
        print(f"\n  {BOLD}{YELLOW}No remote:{RESET}")
        for r in nothing[:15]:
            print(f"    {DIM}{r['name']}{RESET}")

    if broken:
        print(f"\n  {BOLD}{RED}Not responding:{RESET}")
        for r in broken[:15]:
            print(f"    {r['name']}  {DIM}{', '.join(r['unreachable'])}{RESET}")
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

    # No field may contain a newline or NUL.
    for field in (scheme, host, username, password):
        if any(c in field for c in ("\n", "\r", "\x00")):
            return False
    if scheme not in ("http", "https") or not host:
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


def scrub(repos: list[str], save_to_store: bool = True,
          dry_run: bool = False) -> dict:
    """
    Strip tokens out of remote URLs.
    Before removing it, the token is saved to the credential store so that
    push/pull keeps working.
    """
    stats = {"scanned": 0, "cleaned": 0, "saved": 0, "failed": 0, "details": []}
    saved_hosts = set()

    for path in repos:
        stats["scanned"] += 1
        for rm in list_remotes(path):
            if not rm["has_creds"]:
                continue

            clean, cred = strip_creds(rm["url"])
            name = os.path.basename(path)

            if dry_run:
                stats["cleaned"] += 1
                stats["details"].append(f"{name}/{rm['name']} → {clean}")
                continue

            # 1) Secure the token first
            host_key = clean.split("://")[-1].split("/")[0]
            if save_to_store and host_key not in saved_hosts:
                if _credential_approve(clean, cred):
                    saved_hosts.add(host_key)
                    stats["saved"] += 1

            # 2) Then strip it from the URL
            rc, _, err = G._git(path, "remote", "set-url", rm["name"], clean)
            if rc == 0:
                stats["cleaned"] += 1
                stats["details"].append(f"{name}/{rm['name']}")
            else:
                stats["failed"] += 1
                stats["details"].append(f"{RED}{name}/{rm['name']}: {err[:60]}{RESET}")

    return stats
