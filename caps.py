# caps.py — Capability detection
#
# Not every user is an admin. Before we build a menu or make an API call,
# we work out exactly what this token can do and show only that.
#
# Error rule (T13): a failed probe says WHY. The old _get() swallowed every
# exception into None and probe() printed "Network error — check your
# VPN/internet" for a DNS failure, a timeout, a refused port and an untrusted
# TLS certificate alike — four different fixes behind one sentence. probe()
# now keeps the transport error's own text and tags the result with an
# error_catalog code, so every screen can print one "Title — cause → fix" line.

import os
import json
import time
import requests

import credentials as _creds
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
    """The capability cache. Missing or corrupt → {} (it is only a cache: the
    next successful probe rewrites it). An unreadable file is reported once,
    because then every call re-probes and the rewrite will fail too."""
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeDecodeError):
        return {}                           # corrupt cache: regenerated on save
    except OSError as exc:
        _creds.warn_once(("capsread", CACHE_FILE), _cache_io_line(exc, "read"))
        return {}


def _cache_io_line(exc: OSError, verb: str) -> str:
    return _creds.catalog_line(
        f"{type(exc).__name__}: {exc}",
        code="permission_denied" if isinstance(exc, PermissionError) else None,
        title="Capability cache unusable",
        cause=f"{CACHE_FILE} could not be {verb} ({exc.strerror or exc}); "
              f"every screen re-checks the token",
        fix=f"Run `ls -l {CACHE_FILE}`; delete it or `chmod 600` it as its owner")


def _save_cache(data: dict):
    # The cache holds the user's name, the token's scopes and the admin
    # flag — only the owner should be able to read it, from the first byte.
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        _creds.write_private_file(
            CACHE_FILE, json.dumps(data, indent=2, ensure_ascii=False))
    except OSError as exc:
        _creds.warn_once(("capswrite", CACHE_FILE),
                         _cache_io_line(exc, "written"))


def clear_cache(instance_name: str = None):
    if instance_name is None:
        _save_cache({})
        return
    data = _load_cache()
    data.pop(instance_name, None)
    _save_cache(data)


# ─── Probe ─────────────────────────────────────────────────────────────────────

def _describe_exc(exc: Exception, token: str = "") -> str:
    """'<ExceptionType>: <message>' — the type name is what error_catalog keys
    on (ReadTimeout, SSLError, ProxyError, ConnectionError). The token never
    travels in a URL here, but scrub it anyway: this text reaches the screen."""
    text = f"{type(exc).__name__}: {exc}"
    if token and token in text:
        text = text.replace(token, _creds.mask(token))
    return text


def _request(url: str, headers: dict, params: dict = None, timeout: int = 12):
    """(response, '') on an HTTP answer, (None, reason) on a transport failure."""
    try:
        return requests.get(url, headers=headers, params=params,
                            timeout=timeout), ""
    except Exception as exc:                # noqa: BLE001 - reason is returned
        return None, _describe_exc(exc, (headers or {}).get("PRIVATE-TOKEN", ""))


def _get(url: str, headers: dict, params: dict = None, timeout: int = 12):
    """Response or None. Kept for callers that only need yes/no."""
    return _request(url, headers, params, timeout)[0]


def _fail(info: dict, error: str, code: str = None, cause: str = None,
          title: str = None) -> dict:
    """Record a failed probe: raw-but-specific `error` (what doctor and the
    error log classify), its catalog `error_code`, an optional specific
    `error_cause`, and the finished `error_line`."""
    if not code:
        try:
            import error_catalog as _ec
            code = _ec.explain(error).code
        except Exception:                   # noqa: BLE001
            code = "unknown"
    info.update(ok=False, error=error, error_code=code)
    if cause:
        info["error_cause"] = cause
    if title:
        info["error_title"] = title
    info["error_line"] = _creds.catalog_line(error, code=code, cause=cause,
                                             title=title)
    return info


def probe(url: str, token: str) -> dict:
    """
    Work out what this token can do with 3-4 API calls.
    Returns a dict of capabilities plus user information.

    On failure: ok=False, `error` (specific text), `error_code` (an
    error_catalog code), `error_line` ("Title — cause → fix") and, where the
    catalog's generic cause would mislead, `error_cause`.
    """
    url     = url.rstrip("/")
    headers = {"PRIVATE-TOKEN": token}
    caps    = {k: False for k in CAP_LABELS}
    info    = {
        "ok": False, "error": "", "user": "", "username": "",
        "user_id": None, "is_admin": False, "scopes": [],
        "caps": caps, "top_groups": 0, "checked_at": int(time.time()),
        "warnings": [],
    }

    if not (token or "").strip():
        # Probing with an empty token answers 401 and blames a token that was
        # never set. Say what is actually wrong, without a network call.
        return _fail(info, "No token is set for this GitLab", code="token_missing")

    # 1) Who am I?
    r, err = _request(f"{url}/api/v4/user", headers)
    if r is None:
        return _fail(info, err)
    sc = r.status_code
    if sc == 401:
        return _fail(info, "HTTP 401 Unauthorized — invalid, expired or "
                           "revoked token", code="auth")
    if sc == 403:
        return _fail(info, "HTTP 403 Forbidden from /api/v4/user",
                     code="forbidden",
                     cause="The token was refused: it lacks the api/read_user "
                           "scope, or the account is blocked.")
    if sc == 404:
        return _fail(info, f"HTTP 404 Not Found at {url}/api/v4/user",
                     code="not_found",
                     cause=f"{url} has no GitLab API at /api/v4 — it is not "
                           f"the GitLab root address.")
    if sc != 200:
        reason = getattr(r, "reason", "") or ""
        return _fail(info, f"HTTP {sc} {reason}".strip() + " from /api/v4/user")

    try:
        me = r.json()
        if not isinstance(me, dict):
            raise ValueError("not an object")
    except ValueError:
        return _fail(info, f"HTTP 200 from {url}/api/v4/user but the answer "
                           f"is not GitLab JSON",
                     title="Not a GitLab API",
                     cause=f"{url} answered with a non-API page (a login "
                           f"page or a proxy), not GitLab's /api/v4.")
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

    def _note(label: str, resp, why: str):
        """A follow-up check that could not be answered (transport / 5xx).
        A 403/404 here is a real answer ("not allowed"), not a failure."""
        if resp is None:
            info["warnings"].append(f"{label}: {_creds.catalog_line(why)}")
        elif resp.status_code >= 500:
            info["warnings"].append(
                f"{label}: {_creds.catalog_line(f'HTTP {resp.status_code}')}")

    # 2) Token scopes (GitLab >= 14.x) — not every instance exposes them
    r, err = _request(f"{url}/api/v4/personal_access_tokens/self", headers)
    if r is not None and r.status_code == 200:
        try:
            info["scopes"] = r.json().get("scopes", []) or []
        except (ValueError, TypeError, KeyError, AttributeError):
            # Older instance or a non-JSON response — scopes are optional.
            pass
    else:
        _note("token scopes", r, err)

    scopes = set(info["scopes"])
    if scopes:
        caps["clone"] = bool(scopes & {"api", "read_repository", "write_repository"})
        caps["write"] = bool(scopes & {"api", "write_repository"})
    else:
        caps["write"] = True         # unknown — assume it's allowed

    # 3) How many top-level groups can it see?
    r, err = _request(f"{url}/api/v4/groups", headers,
                      {"top_level_only": True, "per_page": 1})
    if r is not None and r.status_code == 200:
        info["top_groups"] = int(r.headers.get("X-Total", 0) or 0)
    else:
        _note("group count", r, err)

    # 4) Can it list all users? (admin-only on most instances)
    r, err = _request(f"{url}/api/v4/users", headers,
                      {"per_page": 1, "active": True})
    if r is not None and r.status_code == 200:
        total = int(r.headers.get("X-Total", 0) or 0)
        # A non-admin only ever sees themselves
        caps["all_users"] = info["is_admin"] or total > 1
    else:
        _note("user listing", r, err)

    # 5) User events (for the activity section)
    if info["user_id"]:
        r, err = _request(f"{url}/api/v4/users/{info['user_id']}/events",
                          headers, {"per_page": 1})
        caps["user_events"] = r is not None and r.status_code == 200
        if not caps["user_events"]:
            _note("user activity", r, err)

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
    # Only a clean answer is cached: a partial one (warnings) would freeze a
    # transient failure into "you cannot" for 12 hours.
    if info["ok"] and not info.get("warnings"):
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
        return info.get("error_line") or info.get("error", "unknown")
    who  = info.get("user") or info.get("username") or "?"
    role = "Admin" if info.get("is_admin") else "User"
    line = f"{who} ({role}) · {info.get('top_groups', 0)} groups"
    n = len(info.get("warnings") or [])
    if n:
        line += f" · {n} check{'s' if n > 1 else ''} failed"
    return line


def can(info: dict, cap: str) -> bool:
    return bool(info.get("caps", {}).get(cap))


# ─── Coverage accounting lives in coverage.py ────────────────────────────────
#
# W10. W8-6 wrote the instance-coverage accounting — POLICY_USER_NAMESPACE,
# project_namespace_kind(), coverage_counts(), coverage_line() and
# coverage_consistent() — into THIS file, because coverage.py belonged to
# another worker that wave, and its report named coverage.py as the real home.
# W9-4 made coverage.py the address for the names via a PEP-562 forward but
# could not delete anything here. Both halves happen together in W10: the
# bodies now live in coverage.py beside group_coverage(), and this module keeps
# no copy and no re-export.
#
# NO FORWARDING SHIM ON PURPOSE. A `from coverage import coverage_counts` here
# would keep `caps.coverage_counts` working and therefore keep two import paths
# alive for one function — the shape that lets a future edit "fix" one address
# and leave the other behind. That is the defect this project has now hit
# twice: three directory walkers reporting three different work-tree counts, and two
# coverage fractions printed two screens apart. One name, one address:
# `import coverage as cov; cov.coverage_counts(...)`.
#
# caps.py answers "what may this token do?". It never had a reason to know how
# many projects are cloned on disk.
