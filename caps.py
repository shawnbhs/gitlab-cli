# caps.py — Capability detection
#
# Not every user is an admin. Before we build a menu or make an API call,
# we work out exactly what this token can do and show only that.

import os
import json
import stat
import time
import requests

from credentials import CONFIG_DIR

CACHE_FILE = os.path.join(CONFIG_DIR, "caps.json")
CACHE_TTL  = 12 * 3600          # 12 hours

ROLE_NAMES = {
    10: "Guest", 15: "Planner", 20: "Reporter",
    30: "Developer", 40: "Maintainer", 50: "Owner",
}

# The things this token is able to do
CAP_LABELS = {
    "read":         "View groups/projects",
    "clone":        "Clone / pull",
    "write":        "Push",
    "members":      "View group members",
    "all_users":    "View all users on the instance (admin)",
    "admin":        "Instance admin",
    "user_events":  "View user activity",
}


# ─── Cache ─────────────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(data: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        # The cache holds the user's name, the token's scopes and the admin
        # flag — only the owner should be able to read it.
        os.chmod(CACHE_FILE, stat.S_IRUSR | stat.S_IWUSR)   # 0600
    except OSError:
        pass


def clear_cache(instance_name: str = None):
    if instance_name is None:
        _save_cache({})
        return
    data = _load_cache()
    data.pop(instance_name, None)
    _save_cache(data)


# ─── Probe ─────────────────────────────────────────────────────────────────────

def _get(url: str, headers: dict, params: dict = None, timeout: int = 12):
    try:
        return requests.get(url, headers=headers, params=params, timeout=timeout)
    except Exception:
        return None


def probe(url: str, token: str) -> dict:
    """
    Work out what this token can do with 3-4 API calls.
    Returns a dict of capabilities plus user information.
    """
    url     = url.rstrip("/")
    headers = {"PRIVATE-TOKEN": token}
    caps    = {k: False for k in CAP_LABELS}
    info    = {
        "ok": False, "error": "", "user": "", "username": "",
        "user_id": None, "is_admin": False, "scopes": [],
        "caps": caps, "top_groups": 0, "checked_at": int(time.time()),
    }

    # 1) Who am I?
    r = _get(f"{url}/api/v4/user", headers)
    if r is None:
        info["error"] = "Network error — check your VPN/internet"
        return info
    if r.status_code == 401:
        info["error"] = "401 — invalid or expired token"
        return info
    if r.status_code != 200:
        info["error"] = f"HTTP {r.status_code}"
        return info

    me = r.json()
    info.update(
        ok=True,
        user=me.get("name", ""),
        username=me.get("username", ""),
        user_id=me.get("id"),
        is_admin=bool(me.get("is_admin")),
    )
    caps["read"]  = True
    caps["clone"] = True
    caps["admin"] = info["is_admin"]

    # 2) Token scopes (GitLab >= 14.x) — not every instance exposes them
    r = _get(f"{url}/api/v4/personal_access_tokens/self", headers)
    if r is not None and r.status_code == 200:
        try:
            info["scopes"] = r.json().get("scopes", []) or []
        except (ValueError, TypeError, KeyError):
            # Older instance or a non-JSON response — scopes are optional.
            pass

    scopes = set(info["scopes"])
    if scopes:
        caps["clone"] = bool(scopes & {"api", "read_repository", "write_repository"})
        caps["write"] = bool(scopes & {"api", "write_repository"})
    else:
        caps["write"] = True         # unknown — assume it's allowed

    # 3) How many top-level groups can it see?
    r = _get(f"{url}/api/v4/groups", headers,
             {"top_level_only": True, "per_page": 1})
    if r is not None and r.status_code == 200:
        info["top_groups"] = int(r.headers.get("X-Total", 0) or 0)

    # 4) Can it list all users? (admin-only on most instances)
    r = _get(f"{url}/api/v4/users", headers, {"per_page": 1, "active": True})
    if r is not None and r.status_code == 200:
        total = int(r.headers.get("X-Total", 0) or 0)
        # A non-admin only ever sees themselves
        caps["all_users"] = info["is_admin"] or total > 1

    # 5) User events (for the activity section)
    if info["user_id"]:
        r = _get(f"{url}/api/v4/users/{info['user_id']}/events",
                 headers, {"per_page": 1})
        caps["user_events"] = r is not None and r.status_code == 200

    caps["members"] = True   # members of your own groups are always visible
    return info


def get(cfg: dict, force: bool = False) -> dict:
    """Capabilities, with caching. cfg must carry url/token/name."""
    name  = cfg.get("name") or cfg.get("url", "")
    cache = _load_cache()
    hit   = cache.get(name)

    if (not force and hit
            and time.time() - hit.get("checked_at", 0) < CACHE_TTL
            and hit.get("token_fp") == _fp(cfg.get("token", ""))):
        return hit

    info = probe(cfg["url"], cfg.get("token", ""))
    info["token_fp"] = _fp(cfg.get("token", ""))
    if info["ok"]:
        cache[name] = info
        _save_cache(cache)
    return info


def _fp(token: str) -> str:
    """Token fingerprint — used to invalidate the cache when the token changes."""
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()[:12] if token else ""


# ─── Display ───────────────────────────────────────────────────────────────────

def role_label(x) -> str:
    """
    Accepts either a numeric access level (10/20/30/40/50)
    or a capability dict (the output of get()/probe()).
    """
    if isinstance(x, dict):
        if not x.get("ok"):
            return x.get("error", "unknown")
        if x.get("is_admin"):
            return "Admin"
        c = x.get("caps", {})
        if c.get("all_users"):
            return "Owner/Maintainer"
        if c.get("write"):
            return "Developer"
        if c.get("clone"):
            return "Reporter"
        return "Guest"
    return ROLE_NAMES.get(x, f"Level {x}")


def invalidate(cfg: dict = None):
    """Clear the capability cache (after changing a token)."""
    name = (cfg or {}).get("name")
    clear_cache(name)


def summary_line(info: dict) -> str:
    """A one-line summary for the header."""
    if not info.get("ok"):
        return info.get("error", "unknown")
    who  = info.get("user") or info.get("username") or "?"
    role = "Admin" if info.get("is_admin") else "User"
    return f"{who} ({role}) · {info.get('top_groups', 0)} groups"


def can(info: dict, cap: str) -> bool:
    return bool(info.get("caps", {}).get(cap))
