"""Read-only, completeness-checked comparison of two GitLab group inventories.

Only GET collection is performed. Unknown access provenance is explicitly
indeterminate; matching usernames are candidates, not verified identities.
"""
from __future__ import annotations

from datetime import date
from urllib.parse import quote

import api
from gitlab_mcp.context import GitLabError


class MigrationAuditError(Exception):
    """Inventory could not be collected or validated without ambiguity."""


_ROLES = {10: "guest", 15: "planner", 20: "reporter", 30: "developer",
          40: "maintainer", 50: "owner"}
_STATUSES = ("identical", "different", "missing", "extra")
# Distinct invited (shared) groups expanded per side. Exceeding it aborts the
# audit; silently skipping groups would fake completeness.
# ponytail: fixed cap; make it a CLI option if real inventories need more.
_MAX_SHARED_GROUPS = 50
_LIMITATIONS = [
    "Matching usernames across instances are candidate identities only, not verified identities.",
    "Direct membership presence does not prove the effective role came from that grant when inherited or shared grants are higher.",
    "Repository parity does not prove LFS, wiki, issues/MRs, CI variables, registry, hooks, or runners.",
    "Access parity does not prove absence of other access: public/internal visibility, instance admins, hidden groups, custom roles, or token scope.",
    "Missing and extra rows describe observed inventory, not independently proven admin completeness.",
    "This report is observed inventory and never proof that the source is safe to delete.",
]


def _id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MigrationAuditError("Invalid or missing positive resource ID")
    return value


def _text(obj: dict, key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise MigrationAuditError("Missing or invalid required text metadata")
    return value


def _object(value: object) -> dict:
    if not isinstance(value, dict):
        raise MigrationAuditError("Expected an object response")
    return value


def _field(obj: dict, key: str, kind: type) -> object:
    if key not in obj or type(obj[key]) is not kind:
        raise MigrationAuditError("Missing or invalid required metadata")
    return obj[key]


def _nullable_text(obj: dict, key: str) -> str | None:
    if key not in obj or (obj[key] is not None and not isinstance(obj[key], str)):
        raise MigrationAuditError("Missing or invalid required metadata")
    return obj[key]


def _expiry(obj: dict) -> str | None:
    value = _nullable_text(obj, "expires_at")
    if value is None:
        return None
    try:
        canonical = date.fromisoformat(value).isoformat() == value
    except ValueError:
        canonical = False
    if not canonical:
        raise MigrationAuditError("Expiry is not a canonical YYYY-MM-DD date")
    return value


def _get(ctx: object, path: str) -> dict:
    try:
        return _object(ctx.get(path))
    except GitLabError as exc:
        # ``path`` is built here from ids and quoted paths; never echo exc text.
        raise MigrationAuditError(f"GET {path} failed (HTTP {exc.status})") from None


def _pages(ctx: object, path: str, params: dict | None = None,
           optional_shared: bool = False) -> list[dict] | None:
    # Never expose cfg_for_git() or exception text: either may contain secrets.
    try:
        cfg = ctx.cfg_for_git()
        headers = cfg["headers"]
        url = ctx.url.rstrip("/") + "/api/v4" + path
        # quiet: the legacy lister prints failures to stdout, corrupting JSON.
        result = api.get_paginated(url, headers, params, strict=True, max_pages=200,
                                   quiet=True)
    except (api.ApiError, GitLabError) as exc:
        if optional_shared and getattr(exc, "status", None) in (403, 404):
            return None
        raise MigrationAuditError(
            f"GET {path} failed (HTTP {getattr(exc, 'status', None)})") from None
    except (KeyError, TypeError, ValueError):
        raise MigrationAuditError("Invalid GitLab list configuration") from None
    if not isinstance(result, api.PagedResult) or not result.complete:
        raise MigrationAuditError(f"GET {path}: incomplete paginated result")
    if not all(isinstance(row, dict) for row in result):
        raise MigrationAuditError("Malformed paginated GET response")
    return result


def _optional_get(ctx: object, path: str) -> dict | None:
    # Shared (invited) groups may be outside the token's view: 403/404 is
    # missing evidence, not an error, and must surface as indeterminate.
    try:
        return _object(ctx.get(path))
    except GitLabError as exc:
        if exc.status in (403, 404):
            return None
        raise MigrationAuditError(f"GET {path} failed (HTTP {exc.status})") from None


def _shared_members(ctx: object, shared_id: int,
                    cache: dict[int, dict | None]) -> dict | None:
    if shared_id in cache:
        return cache[shared_id]
    if len(cache) >= _MAX_SHARED_GROUPS:
        raise MigrationAuditError(
            f"Shared group expansion limit ({_MAX_SHARED_GROUPS}) exceeded")
    detail = _optional_get(ctx, f"/groups/{shared_id}")
    members = None
    if detail is not None:
        if _id(detail.get("id")) != shared_id:
            raise MigrationAuditError("Inconsistent shared group detail")
        _text(detail, "full_path")
        if "parent_id" not in detail or (detail["parent_id"] is not None and
                                          _id(detail["parent_id"]) == shared_id):
            raise MigrationAuditError("Invalid shared group ancestry metadata")
        _share_rows(detail)
        # members/all adds the invited group's inherited members, which a
        # share also grants; unreadable listing stays unknown, never empty.
        rows = _pages(ctx, f"/groups/{shared_id}/members/all", optional_shared=True)
        members = None if rows is None else _member_rows(rows)
    cache[shared_id] = members
    return members


def _share_rows(detail: dict) -> list[tuple[int, int, str | None]]:
    raw = detail.get("shared_with_groups")
    if not isinstance(raw, list):
        raise MigrationAuditError("Missing group share metadata")
    shares = []
    for entry in raw:
        item = _object(entry)
        shares.append((_id(item.get("group_id")),
                       _id(item.get("group_access_level")), _expiry(item)))
    return shares


def _member_rows(rows: list[dict]) -> dict[str, dict]:
    members: dict[str, dict] = {}
    ids: set[int] = set()
    for row in rows:
        username = _text(row, "username")
        identity = _id(row.get("id"))
        if username in members or identity in ids:
            raise MigrationAuditError("Duplicate member identity in collection")
        ids.add(identity)
        _id(row.get("access_level"))
        _text(row, "state")
        _expiry(row)
        members[username] = row
    return members


def _group_inventory(ctx: object, root_path: str) -> tuple[dict, dict[str, dict], dict[int, dict]]:
    root = _get(ctx, "/groups/" + quote(root_path, safe=""))
    root_id = _id(root.get("id"))
    full_path = _text(root, "full_path")
    if full_path != root_path:
        raise MigrationAuditError("Group path does not match requested root")
    descendants = _pages(ctx, f"/groups/{root_id}/descendant_groups")
    # Full descendant enumeration, not a capped report helper. Ancestors of a
    # nested root are fetched separately because they may supply inherited grants.
    groups_by_id = {root_id: root}
    groups_by_path = {full_path: root}
    for item in descendants:
        group_id = _id(item.get("id"))
        path = _text(item, "full_path")
        if not path.startswith(full_path + "/") or group_id in groups_by_id or path in groups_by_path:
            raise MigrationAuditError("Invalid descendant group inventory")
        detail = _get(ctx, f"/groups/{group_id}")
        if _id(detail.get("id")) != group_id or _text(detail, "full_path") != path:
            raise MigrationAuditError("Inconsistent group detail")
        groups_by_id[group_id] = detail
        groups_by_path[path] = detail
    # Fetch ancestor chain outside the selected root as well. A missing parent
    # ID on an absolute root indicates the instance's top-level group.
    current = root
    seen = set(groups_by_id)
    while True:
        if "parent_id" not in current:
            raise MigrationAuditError("Missing parent group metadata")
        if current["parent_id"] is None:
            break
        parent_id = _id(current["parent_id"])
        if parent_id in seen:
            raise MigrationAuditError("Cyclic group ancestry")
        seen.add(parent_id)
        parent = _get(ctx, f"/groups/{parent_id}")
        if _id(parent.get("id")) != parent_id:
            raise MigrationAuditError("Inconsistent parent group detail")
        groups_by_id[parent_id] = parent
        current = parent
    for group_id, group in list(groups_by_id.items()):
        group["_direct_members"] = _member_rows(_pages(ctx, f"/groups/{group_id}/members"))
        group["_shares"] = _share_rows(group)
    return root, groups_by_path, groups_by_id


def _project_inventory(ctx: object, root: dict) -> dict[str, dict]:
    root_id = _id(root["id"])
    prefix = _text(root, "full_path") + "/"
    listed = _pages(ctx, f"/groups/{root_id}/projects",
                    {"include_subgroups": True, "with_shared": False})
    projects = {}
    seen_ids = set()
    for item in listed:
        project_id = _id(item.get("id"))
        path = _text(item, "path_with_namespace")
        if not path.startswith(prefix) or not path[len(prefix):] or project_id in seen_ids:
            raise MigrationAuditError("Project escaped group root or duplicate project ID")
        relative = path[len(prefix):]
        if relative in projects:
            raise MigrationAuditError("Duplicate relative project path")
        seen_ids.add(project_id)
        detail = _get(ctx, f"/projects/{project_id}")
        if _id(detail.get("id")) != project_id or _text(detail, "path_with_namespace") != path:
            raise MigrationAuditError("Inconsistent project detail")
        for key in ("archived", "empty_repo"):
            _field(detail, key, bool)
        _text(detail, "visibility")
        if "default_branch" not in detail or not (detail["default_branch"] is None or
                                                     isinstance(detail["default_branch"], str)):
            raise MigrationAuditError("Missing default branch metadata")
        _share_rows(detail)
        branches = _pages(ctx, f"/projects/{project_id}/repository/branches")
        tags = _pages(ctx, f"/projects/{project_id}/repository/tags")
        branch_map, tag_map = {}, {}
        for value in branches:
            name = _text(value, "name")
            if name in branch_map:
                raise MigrationAuditError("Duplicate branch name")
            branch_map[name] = _text(_object(value.get("commit")), "id")
        for value in tags:
            name = _text(value, "name")
            if name in tag_map:
                raise MigrationAuditError("Duplicate tag name")
            tag_map[name] = {"commit_id": _text(_object(value.get("commit")), "id"),
                             "target": _text(value, "target"),
                             "message": _nullable_text(value, "message")}
        detail["_branches"] = branch_map
        detail["_tags"] = tag_map
        detail["_direct_members"] = _member_rows(_pages(ctx, f"/projects/{project_id}/members"))
        detail["_effective_members"] = _member_rows(_pages(ctx, f"/projects/{project_id}/members/all"))
        projects[relative] = detail
    return projects


def _effective_expiry(member_expiry: str | None, share_expiry: str | None) -> str | None:
    # ISO dates emitted by GitLab sort chronologically; None = no deadline.
    return min((date for date in (member_expiry, share_expiry) if date), default=None)


def _origin(ctx: object, project: dict, groups: dict[str, dict], by_id: dict[int, dict],
            shared_cache: dict[int, dict | None], member: dict) -> tuple[str, bool]:
    path = project["path_with_namespace"]
    namespace = path.rsplit("/", 1)[0]
    group = groups.get(namespace)
    if group is None:
        raise MigrationAuditError("Project namespace absent from group inventory")
    wanted_id = member["id"]
    wanted_role = member["access_level"]
    wanted_expiry = member["expires_at"]
    wanted_state = member["state"]

    def same(grant: dict | None, role: int, expiry: str | None) -> bool:
        # Grant explains the row only if identity, role, expiry and state agree.
        return bool(grant) and grant["id"] == wanted_id and role == wanted_role \
            and expiry == wanted_expiry and grant["state"] == wanted_state

    candidates = set()
    uncertainty = False
    direct = project["_direct_members"].get(member["username"])
    if direct and same(direct, direct["access_level"], direct["expires_at"]):
        candidates.add("direct")
    # Include all ancestor groups, including those outside a nested root.
    current = group
    visited = set()
    chain = []
    while current is not None:
        current_id = _id(current.get("id"))
        if current_id in visited:
            raise MigrationAuditError("Cyclic project group ancestry")
        visited.add(current_id)
        chain.append(current)
        if "parent_id" not in current:
            raise MigrationAuditError("Missing parent group metadata")
        parent = current["parent_id"]
        current = by_id.get(_id(parent)) if parent is not None else None
        if parent is not None and current is None:
            raise MigrationAuditError("Missing ancestor group metadata")
    for ancestor in chain:
        found = ancestor["_direct_members"].get(member["username"])
        if found and same(found, found["access_level"], found["expires_at"]):
            candidates.add("inherited")
    shares = list(_share_rows(project))
    for ancestor in chain:
        shares.extend(ancestor["_shares"])
    for shared_id, cap, expiry in shares:
        # A share explains the grant only when the invited group's member,
        # capped by group_access_level and the share expiry, equals the
        # effective row; username presence alone proves nothing.
        members = _shared_members(ctx, shared_id, shared_cache)
        if members is None:
            uncertainty = True
            continue
        found = members.get(member["username"])
        if found and same(found, min(found["access_level"], cap),
                          _effective_expiry(found["expires_at"], expiry)):
            candidates.add("shared")
    if uncertainty or not candidates:
        return "indeterminate", True
    # Several grants that each exactly explain the row are known provenance,
    # reported together; only *no* explanation (or unreadable shares) is unknown.
    return "+".join(sorted(candidates)), False


def _side(ctx: object, root_path: str) -> tuple[dict, dict[str, dict], bool]:
    root, groups, by_id = _group_inventory(ctx, root_path)
    projects = _project_inventory(ctx, root)
    uncertain = False
    shared_cache: dict[int, dict | None] = {}
    for project in projects.values():
        origins = {}
        for username, member in project["_effective_members"].items():
            origin, unknown = _origin(ctx, project, groups, by_id, shared_cache, member)
            origins[username] = origin
            uncertain |= unknown
        project["_origins"] = origins
    return root, projects, uncertain


def _repo_row(relative: str, source: dict | None, target: dict | None) -> dict:
    fields = ("default_branch", "archived", "visibility", "empty_repo", "_branches", "_tags")
    differences = ([field.removeprefix("_") for field in fields if source[field] != target[field]]
                   if source and target else [])
    return {"source_path": relative if source else None,
            "target_path": relative if target else None,
            "status": "missing" if source and not target else "extra" if target and not source
            else "different" if differences else "identical",
            "differences": differences}


def _access_rows(source: dict[str, dict], target: dict[str, dict]) -> list[dict]:
    rows = []
    for path in sorted(source.keys() | target.keys()):
        src = source.get(path)
        dst = target.get(path)
        users = ((set(src["_effective_members"]) if src else set()) |
                 (set(dst["_effective_members"]) if dst else set()))
        for username in sorted(users):
            a = src["_effective_members"].get(username) if src else None
            b = dst["_effective_members"].get(username) if dst else None
            origin_a = src["_origins"].get(username) if src else None
            origin_b = dst["_origins"].get(username) if dst else None
            roles = (a["access_level"] if a else None, b["access_level"] if b else None)
            same = (a and b and roles[0] == roles[1] and a["state"] == b["state"]
                    and a["expires_at"] == b["expires_at"] and origin_a == origin_b)
            rows.append({"project": path, "username": username,
                         "source_role": _ROLES.get(roles[0], f"level-{roles[0]}") if a else None,
                         "target_role": _ROLES.get(roles[1], f"level-{roles[1]}") if b else None,
                         "source_state": a["state"] if a else None,
                         "target_state": b["state"] if b else None,
                         "source_expires_at": a["expires_at"] if a else None,
                         "target_expires_at": b["expires_at"] if b else None,
                         "source_origin": origin_a, "target_origin": origin_b,
                         "status": "missing" if a and not b else "extra" if b and not a
                         else "identical" if same else "different"})
    return rows


def audit(source_ctx: object, target_ctx: object, source_group: str,
          target_group: str | None = None) -> dict:
    """Collect and compare all owned descendant projects and observed grants.

    Raises MigrationAuditError on any failed mandatory GET or missing metadata.
    Indeterminate origin is returned only when collection succeeded but the
    available evidence cannot uniquely explain an effective member grant.
    """
    if not isinstance(source_group, str) or not source_group:
        raise MigrationAuditError("Source group path is required")
    target_group = source_group if target_group is None else target_group
    if not isinstance(target_group, str) or not target_group:
        raise MigrationAuditError("Target group path is required")
    _, source, unknown_source = _side(source_ctx, source_group)
    _, target, unknown_target = _side(target_ctx, target_group)
    repos = [_repo_row(path, source.get(path), target.get(path))
             for path in sorted(source.keys() | target.keys())]
    access = _access_rows(source, target)
    complete = not (unknown_source or unknown_target)
    counts = {table: {status: sum(row["status"] == status for row in rows)
                      for status in _STATUSES}
              for table, rows in (("repositories", repos), ("access", access))}
    summary = {**counts, "source_group": source_group, "target_group": target_group,
               "source_instance": source_ctx.name, "target_instance": target_ctx.name,
               "complete": complete}
    limitations = list(_LIMITATIONS)
    if not complete:
        limitations.append("Some effective access origins are indeterminate due to missing or conflicting evidence.")
    return {"repositories": repos, "access": access, "summary": summary,
            "limitations": limitations, "complete": complete}
