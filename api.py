# api.py — GitLab API helpers
#
# Designed for every access level: admin, owner, developer, guest.
# Wherever an endpoint is admin-only, we keep a membership-based fallback.

import requests
from printer import print_error

TIMEOUT = 20


class ApiError(Exception):
    """An API error carrying its HTTP status code."""
    def __init__(self, status: int, message: str, url: str = ""):
        self.status  = status
        self.message = message
        self.url     = url
        super().__init__(f"HTTP {status}: {message}")


def _explain(status: int, url: str) -> str:
    return {
        401: "Invalid or expired token",
        403: "Access denied — this section needs a higher access level",
        404: "Not found (or you don't have access)",
        429: "Rate limited — wait a few seconds",
    }.get(status, f"HTTP {status}")


def request(url: str, headers: dict, params: dict = None,
            quiet: bool = False) -> requests.Response | None:
    """Single GET request. Returns None on error (and prints unless quiet)."""
    try:
        res = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
    except requests.Timeout:
        if not quiet:
            print_error(f"Timeout: {url}")
        return None
    except requests.ConnectionError:
        if not quiet:
            print_error("Connection failed — check your VPN/internet")
        return None

    if res.status_code >= 400 and not quiet:
        print_error(f"{_explain(res.status_code, url)}  {url.split('/api/v4/')[-1][:60]}")
    return res


def get_paginated(url: str, headers: dict, params: dict = None,
                  quiet: bool = False, max_pages: int = 200) -> list:
    """Fetch every page. On error, returns whatever was collected so far."""
    p = {**(params or {}), "per_page": 100, "page": 1}
    results, pages = [], 0

    while pages < max_pages:
        res = request(url, headers, p, quiet=quiet)
        if res is None or res.status_code != 200:
            break

        try:
            data = res.json()
        except ValueError:
            break
        if not data:
            break

        results.extend(data)
        pages += 1

        # If GitLab sent a next-page header, use it
        nxt = res.headers.get("X-Next-Page", "").strip()
        if nxt:
            p["page"] = int(nxt)
        elif len(data) < 100:
            break
        else:
            p["page"] += 1

    return results


# ─── Groups ────────────────────────────────────────────────────────────────────

def get_group(gitlab_url: str, headers: dict, group_path: str) -> dict | None:
    encoded = requests.utils.quote(str(group_path), safe="")
    res = request(f"{gitlab_url}/api/v4/groups/{encoded}", headers, quiet=True)
    if res is None:
        print_error("Connection failed")
        return None
    if res.status_code == 200:
        return res.json()
    print_error(f"Group '{group_path}': {_explain(res.status_code, '')}")
    return None


def get_top_level_groups(gitlab_url: str, headers: dict,
                         min_access_level: int = None) -> list:
    """
    Top-level groups.
    - Admin: sees everything.
    - Regular user: only groups they belong to (GitLab filters this itself).
    - min_access_level: only groups where your access level is >= this.
    """
    params = {"top_level_only": True}
    if min_access_level:
        params["min_access_level"] = min_access_level

    groups = get_paginated(f"{gitlab_url}/api/v4/groups", headers, params)
    return sorted(groups, key=lambda g: g.get("full_path", ""))


def get_all_groups(gitlab_url: str, headers: dict,
                   min_access_level: int = None) -> list:
    """Every group, subgroups included — used for search."""
    params = {}
    if min_access_level:
        params["min_access_level"] = min_access_level
    groups = get_paginated(f"{gitlab_url}/api/v4/groups", headers, params)
    return sorted(groups, key=lambda g: g.get("full_path", ""))


def get_subgroups(gitlab_url: str, headers: dict, group_id: int) -> list:
    return get_paginated(f"{gitlab_url}/api/v4/groups/{group_id}/subgroups",
                         headers, quiet=True)


# ─── Projects ──────────────────────────────────────────────────────────────────

def get_projects(gitlab_url: str, headers: dict, group_id: int,
                 min_access_level: int = None) -> list:
    """Projects directly owned by a group."""
    params = {"with_shared": False, "archived": False}
    if min_access_level:
        params["min_access_level"] = min_access_level
    return get_paginated(f"{gitlab_url}/api/v4/groups/{group_id}/projects",
                         headers, params, quiet=True)


def get_group_projects_recursive(gitlab_url: str, headers: dict, group_id: int,
                                 min_access_level: int = None) -> list:
    """
    Every project in a group plus all of its subgroups in a SINGLE API call.
    Much faster than walking the subgroup tree.
    """
    params = {"include_subgroups": True, "with_shared": False, "archived": False}
    if min_access_level:
        params["min_access_level"] = min_access_level
    return get_paginated(f"{gitlab_url}/api/v4/groups/{group_id}/projects",
                         headers, params, quiet=True)


def get_my_projects(gitlab_url: str, headers: dict,
                    min_access_level: int = None) -> list:
    """
    Every project the user can access, regardless of group.
    For users who belong to no top-level group but still have access
    to individual projects.
    """
    params = {"membership": True, "archived": False, "simple": True}
    if min_access_level:
        params["min_access_level"] = min_access_level
    projects = get_paginated(f"{gitlab_url}/api/v4/projects", headers, params)
    return sorted(projects, key=lambda p: p.get("path_with_namespace", ""))


def get_project(gitlab_url: str, headers: dict, project_ref) -> dict | None:
    encoded = requests.utils.quote(str(project_ref), safe="")
    res = request(f"{gitlab_url}/api/v4/projects/{encoded}", headers, quiet=True)
    if res is not None and res.status_code == 200:
        return res.json()
    return None


# ─── The user's own access ─────────────────────────────────────────────────────

def my_access_level(gitlab_url: str, headers: dict,
                    kind: str, ref) -> int | None:
    """
    The token's own access level on a group or project.
    kind: 'groups' or 'projects'. Returns 10..50, or None.
    """
    encoded = requests.utils.quote(str(ref), safe="")
    res = request(f"{gitlab_url}/api/v4/{kind}/{encoded}", headers,
                  {"with_custom_attributes": False}, quiet=True)
    if res is None or res.status_code != 200:
        return None
    data = res.json()
    perms = data.get("permissions") or {}
    levels = []
    for key in ("project_access", "group_access"):
        v = perms.get(key)
        if isinstance(v, dict) and v.get("access_level"):
            levels.append(v["access_level"])
    return max(levels) if levels else None
