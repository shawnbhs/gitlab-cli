# members.py — GitLab Members management

import requests
from collections import defaultdict
from errors import log_error
from printer import (
    print_section, print_info, print_warn, print_error,
    print_separator, BOLD, CYAN, GREEN, YELLOW, DIM, RESET, RED, WHITE
)

ROLES = {
    10: ("Guest",      "\033[37m"),
    20: ("Reporter",   "\033[34m"),
    30: ("Developer",  "\033[32m"),
    40: ("Maintainer", "\033[33m"),
    50: ("Owner",      "\033[35m"),
}


def _role_str(access_level: int) -> str:
    name, color = ROLES.get(access_level, ("Unknown", "\033[37m"))
    return f"{color}{name}{RESET}"


def _get_paginated(cfg: dict, path: str, params: dict = None,
                   quiet: bool = False) -> list:
    """
    Fetch every page of a paginated endpoint.
    On 403 (admin-only endpoint) it returns whatever it has instead of
    raising noise — a regular user simply doesn't have that access.
    """
    url = f"{cfg['url']}/api/v4/{path}"
    p   = {**(params or {}), "per_page": 100, "page": 1}
    results = []
    while True:
        try:
            res = requests.get(url, headers=cfg["headers"], params=p, timeout=15)
            if res.status_code in (401, 403):
                if not quiet:
                    log_error(f"api:{path}", f"{res.status_code} — access denied")
                return results
            if res.status_code != 200:
                return results
            data = res.json()
            if not data:
                break
            results.extend(data)
            p["page"] += 1
            if len(data) < 100:
                break
        except Exception as e:
            err = str(e)
            log_error(f"api:{path}", err)
            if not quiet:
                if "NameResolutionError" in err or "Max retries" in err:
                    print_error("Network error — check your VPN or internet connection!")
                else:
                    print_error(f"Error: {err[:100]}")
            break
    return results


def _can_list_all_users(cfg: dict) -> bool:
    """Only an admin can see the full /users list."""
    try:
        import caps
        return caps.can(caps.get(cfg), "all_users")
    except Exception:
        return False


def _get_one(cfg: dict, path: str) -> dict | None:
    try:
        res = requests.get(
            f"{cfg['url']}/api/v4/{path}",
            headers=cfg["headers"], timeout=15
        )
        if res.status_code == 200:
            return res.json()
    except (requests.RequestException, ValueError):
        # Network is unreachable or the response isn't JSON — caller gets None.
        pass
    return None


def _get_all_users(cfg: dict) -> list:
    """
    Admin   → every user on the instance.
    Regular → only the members of groups the user belongs to.
    """
    if _can_list_all_users(cfg):
        active  = _get_paginated(cfg, "users", {"active": True})
        blocked = _get_paginated(cfg, "users", {"blocked": True})
        users   = active + blocked
        if users:
            return sorted(users, key=lambda u: u.get("name", "").lower())

    return _get_users_from_my_groups(cfg)


def _get_users_from_my_groups(cfg: dict) -> list:
    """
    Fallback for non-admin users: collect the members of every group the
    user belongs to and merge them into a single list.
    """
    print_info("No admin access — collecting from your own groups…")

    groups = _get_paginated(cfg, "groups", {"min_access_level": 10}, quiet=True)
    if not groups:
        return []

    seen = {}
    for g in groups:
        members = _get_paginated(
            cfg, f"groups/{g['id']}/members/all", quiet=True
        )
        for m in members:
            uid = m.get("id")
            if uid is None:
                continue
            if uid not in seen:
                m = dict(m)
                m["_groups"] = []
                seen[uid] = m
            seen[uid]["_groups"].append({
                "path":  g.get("full_path", g.get("name", "")),
                "level": m.get("access_level", 0),
            })

    users = list(seen.values())
    return sorted(users, key=lambda u: u.get("name", "").lower())


def _get_project_info(cfg: dict, project_id: int, cache: dict) -> dict:
    if project_id not in cache:
        data = _get_one(cfg, f"projects/{project_id}")
        if data:
            cache[project_id] = {
                "name": data.get("name", ""),
                "path": data.get("path_with_namespace", ""),
            }
        else:
            cache[project_id] = {"name": f"project#{project_id}", "path": ""}
    return cache[project_id]


def _get_user_events(cfg: dict, user_id: int, since_date: str) -> list:
    return _get_paginated(cfg, f"users/{user_id}/events", {"after": since_date})


def _get_user_memberships(cfg: dict, user_id: int) -> list:
    """Get all groups a user belongs to — uses memberships API (fast, 1-2 calls)."""
    return _get_paginated(cfg, f"users/{user_id}/memberships")


def _parse_since(since: str) -> str:
    import re
    from datetime import datetime, timedelta
    raw = since.strip().lower()
    match = re.match(r"^(\d+)([dwmy])$", raw)
    if match:
        num, unit = int(match.group(1)), match.group(2)
        delta = {
            "d": timedelta(days=num),
            "w": timedelta(weeks=num),
            "m": timedelta(days=num * 30),
            "y": timedelta(days=num * 365),
        }[unit]
        # Add 1 day buffer so GitLab "after" param includes the full range
        dt = datetime.now() - delta - timedelta(days=1)
        return dt.strftime("%Y-%m-%d")
    return raw


# ─── Display ───────────────────────────────────────────────────────────────────

def _print_activity(cfg: dict, events: list, user_name: str, since_parsed: str = ""):
    proj_cache = {}

    # Filter out 0-commit pushes (merge noise)
    def is_noise(e):
        pd = e.get("push_data", {})
        return pd and pd.get("commit_count", 1) == 0

    events = [e for e in events if not is_noise(e)]

    # Group: date → project_id → events
    by_date = defaultdict(lambda: defaultdict(list))
    for e in events:
        date = e.get("created_at", "")[:10]
        pid  = e.get("project_id")
        by_date[date][pid].append(e)

    total = len(events)
    print(f"\n{BOLD}{CYAN}👤 {user_name}{RESET}  {DIM}({total} event){RESET}\n")

    icons = {
        "pushed":    "📤", "commented": "💬", "merged":   "🔀",
        "created":   "✨", "accepted":  "✅", "approved": "👍",
        "opened":    "📂", "closed":    "🔒", "reopened": "🔄",
        "deleted":   "🗑️",
    }

    for date in sorted(by_date.keys(), reverse=True):
        if date not in by_date:
            print(f"{DIM}📅 {date}  😴 No activity{RESET}")
            continue

        proj_map  = by_date[date]
        day_total = sum(len(v) for v in proj_map.values())

        print(f"{BOLD}{YELLOW}📅 {date}{RESET}  {DIM}({day_total} event){RESET}")
        print(f"{DIM}{'─'*55}{RESET}")

        for pid, evs in sorted(proj_map.items(), key=lambda x: str(x[0])):
            proj  = _get_project_info(cfg, pid, proj_cache)
            ppath = proj["path"] or proj["name"]
            parts = ppath.split("/")

            if len(parts) >= 2:
                grp  = "/".join(parts[:-1])
                repo = parts[-1]
                print(f"\n  {DIM}📁 {grp} /{RESET} {BOLD}{GREEN}{repo}{RESET}  {DIM}({len(evs)} event  🗓 {date}){RESET}")
            else:
                print(f"\n  {BOLD}{GREEN}📦 {ppath}{RESET}  {DIM}({len(evs)} event  🗓 {date}){RESET}")

            for e in evs:
                action      = e.get("action_name", "")
                target_type = e.get("target_type", "") or ""
                target_title= e.get("target_title", "") or ""
                time_str    = e.get("created_at", "")[11:16]
                push_data   = e.get("push_data", {})
                icon        = icons.get(action, "•")

                if push_data:
                    branch  = push_data.get("ref", "")
                    n_comm  = push_data.get("commit_count", 0)
                    ctitle  = push_data.get("commit_title", "") or ""
                    branch_col = f"{CYAN}{branch}{RESET}"
                    msg = ctitle[:65] + "..." if len(ctitle) > 65 else ctitle
                    print(f"    {icon} {DIM}{time_str}{RESET}  {branch_col}  {DIM}({n_comm} commit){RESET}")
                    if msg:
                        print(f"       {YELLOW}✎ {msg}{RESET}")
                else:
                    tt  = f"{DIM}{target_type}{RESET} " if target_type else ""
                    ttl = target_title[:65] + "..." if len(target_title) > 65 else target_title
                    print(f"    {icon} {DIM}{time_str}{RESET}  {action} {tt}{BOLD}{ttl}{RESET}")

        print()

    print_separator()
    print(f"{BOLD}📊 Total: {GREEN}{total} event{RESET}\n")


# ─── Commands ──────────────────────────────────────────────────────────────────

def cmd_members_list(cfg: dict):
    print_section("GitLab Members")
    print_info("Fetching all users...")

    users = _get_all_users(cfg)
    if not users:
        print_error("No users found.")
        return None

    # Summary — simple & accurate
    total_users = len(users)
    admins      = [u for u in users if u.get("is_admin") and u.get("state") != "blocked"]
    blocked     = [u for u in users if u.get("state") == "blocked"]
    # Bot: username contains project_ or group_ (GitLab bot pattern)
    bots        = [u for u in users
                   if u.get("state") != "blocked"
                   and not u.get("is_admin")
                   and (
                       "_bot" in u.get("username","") or
                       u.get("username","").startswith("project_") or
                       u.get("username","").startswith("group_")
                   )]
    active      = [u for u in users
                   if u.get("state") != "blocked"
                   and not u.get("is_admin")
                   and u not in bots]

    # Verify math
    check = len(admins) + len(blocked) + len(bots) + len(active)

    print_separator()
    print(f"\n{BOLD}{WHITE}  Summary:{RESET}")
    print(f"  {DIM}Total         :{RESET}  {BOLD}{total_users}{RESET}")
    print(f"  {GREEN}Admin         :{RESET}  {GREEN}{len(admins)}{RESET}")
    print(f"  {CYAN}Active        :{RESET}  {CYAN}{len(active)}{RESET}")
    print(f"  {DIM}Bot/Token     :{RESET}  {DIM}{len(bots)}{RESET}")
    print(f"  {RED}Blocked       :{RESET}  {RED}{len(blocked)}{RESET}")
    if check != total_users:
        print(f"  {YELLOW}⚠ Check: {check} (diff: {total_users - check}){RESET}")
    print()
    print(f"\n{BOLD}{WHITE}  Members ({total_users} total):{RESET}\n")

    for i, u in enumerate(users, 1):
        name       = u.get("name", "")
        username   = u.get("username", "")
        state      = u.get("state", "")
        is_admin   = u.get("is_admin", False)
        is_blocked = state == "blocked"

        if is_blocked:
            name_str = f"{RED}{name}{RESET}"
            tag      = f" {RED}[Blocked]{RESET}"
        elif is_admin:
            name_str = f"{GREEN}{name}{RESET}"
            tag      = f" {GREEN}[Admin]{RESET}"
        else:
            name_str = f"{BOLD}{name}{RESET}"
            tag      = ""

        print(f"  {CYAN}{i:>3}.{RESET}  {name_str}  {DIM}@{username}{RESET}{tag}")

    print(f"\n{DIM}  {'─'*45}{RESET}")
    print(f"  {YELLOW}q{RESET}  exit\n")

    while True:
        try:
            choice = input(f"{BOLD}  Which member? (number or name): {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            return None

        if choice.lower() == "q":
            return None

        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(users):
                return users[idx]
            print(f"  {RED}Enter 1 to {len(users)}.{RESET}")
            continue

        matches = [u for u in users if
                   choice.lower() in u.get("name", "").lower() or
                   choice.lower() in u.get("username", "").lower()]
        if len(matches) == 1:
            return matches[0]
        elif len(matches) > 1:
            print(f"  {YELLOW}Multiple matches:{RESET}")
            for m in matches:
                print(f"    • {m['name']}  @{m['username']}")
        else:
            print(f"  {RED}'{choice}' not found.{RESET}")


def cmd_member_profile(cfg: dict, user: dict):
    name     = user.get("name", "")
    username = user.get("username", "")
    email    = user.get("email", "") or user.get("public_email", "") or "—"
    state    = user.get("state", "")
    is_admin = user.get("is_admin", False)
    created  = user.get("created_at", "")[:10]
    last_act = user.get("last_activity_on", "") or "—"
    bio      = user.get("bio", "") or "—"
    web      = user.get("web_url", "")

    print(f"\n{BOLD}{CYAN}👤  {name}{RESET}  {DIM}@{username}{RESET}\n")
    print(f"  {DIM}Email    :{RESET}  {email}")
    print(f"  {DIM}State    :{RESET}  {GREEN if state=='active' else RED}{state}{RESET}")
    print(f"  {DIM}Admin    :{RESET}  {'Yes' if is_admin else 'No'}")
    print(f"  {DIM}Joined   :{RESET}  {created}")
    print(f"  {DIM}Last act :{RESET}  {last_act}")
    print(f"  {DIM}Bio      :{RESET}  {bio}")
    print(f"  {DIM}Profile  :{RESET}  {web}\n")


def cmd_member_activity(cfg: dict, user: dict, since: str = "1w"):
    name    = user.get("name", "")
    user_id = user.get("id")

    since_date = _parse_since(since)
    print_section(f"Activity: {name}")
    print_info(f"Since: {since_date}")
    print_info("Fetching events...")
    print_separator()

    events = _get_user_events(cfg, user_id, since_date)
    if not events:
        print_warn(f"No activity found since {since_date}.")
        return

    _print_activity(cfg, events, name, since_parsed=since_date)


def cmd_member_access(cfg: dict, user: dict):
    name    = user.get("name", "")
    user_id = user.get("id")

    print_section(f"Access Map: {name}")
    print_info("Fetching memberships...")
    print_separator()

    memberships = _get_user_memberships(cfg, user_id)

    # The memberships endpoint is admin-only. If it fails, fall back to what
    # we already collected while listing the members.
    if not memberships and user.get("_groups"):
        print_info("From shared groups (limited access)")
        for g in user["_groups"]:
            memberships.append({
                "source_type":      "Namespace",
                "source_full_name": g["path"],
                "access_level":     g["level"],
            })

    if not memberships:
        print_warn(f"{name} is not a member of any group, or access was denied.")
        return

    # Filter only Group type
    group_memberships = [
        m for m in memberships
        if m.get("source_type") == "Namespace"
    ]

    if not group_memberships:
        print_warn(f"{name} was not found in any group.")
        return

    # Group by top-level
    by_top = defaultdict(list)
    for m in group_memberships:
        full_name = m.get("source_full_name", "") or m.get("source_name", "")
        # Convert "Group / Subgroup / Project" format to path
        path = "/".join(p.strip() for p in full_name.split("/")) if full_name else "unknown"
        top  = path.split("/")[0]
        by_top[top].append({
            "path":         path,
            "access_level": m.get("access_level", 0),
        })

    total = len(group_memberships)
    print(f"\n{BOLD}{CYAN}👤 {name}{RESET}  {DIM}({total} membership){RESET}\n")

    for top in sorted(by_top.keys()):
        items = sorted(by_top[top], key=lambda x: x["path"])
        print(f"{BOLD}{CYAN}📁 {top}{RESET}")
        for j, m in enumerate(items):
            is_last   = (j == len(items) - 1)
            connector = "└──" if is_last else "├──"
            depth     = m["path"].count("/")
            indent    = "   " * depth
            label     = m["path"].split("/")[-1] if depth > 0 else top
            role      = _role_str(m["access_level"])
            print(f"  {indent}{connector} {DIM}{label}{RESET}  {role}")
        print()

    print_separator()
    from collections import Counter
    role_counts = Counter(
        ROLES.get(m.get("access_level", 0), ("Unknown", ""))[0]
        for m in group_memberships
    )
    print(f"{BOLD}Role Summary:{RESET}")
    for role, count in role_counts.most_common():
        print(f"  {DIM}{role:15}{RESET} {count} group")
    print()


def cmd_member_actions(cfg: dict, user: dict):
    import sys
    name     = user.get("name", "")
    username = user.get("username", "")
    state    = user.get("state", "active")

    while True:
        state_icon = f"{RED}🔴 Blocked{RESET}" if state == "blocked" else f"{GREEN}🟢 Active{RESET}"
        print(f"\n{BOLD}{CYAN}👤 {name}{RESET}  {DIM}@{username}{RESET}  {state_icon}\n")
        print(f"  {CYAN}1.{RESET}  👤  Profile          — general info")
        print(f"  {CYAN}2.{RESET}  📋  Activity Log      — everything they have done")
        print(f"  {CYAN}3.{RESET}  🗺️   Access Map        — access level in each group")
        print(f"  {CYAN}4.{RESET}  🔐  Manage          — permissions, edit, block/delete")
        print(f"\n  {YELLOW}b{RESET}  back   {YELLOW}q{RESET}  exit\n")

        try:
            action = input(f"{BOLD}  Choice: {RESET}").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return

        if action == "q":
            sys.exit(0)
        if action == "b":
            return

        if action == "1":
            cmd_member_profile(cfg, user)

        elif action == "2":
            print(f"""
{DIM}  ┌─ Help ─────────────────────────────────┐
  │  1d = 1 day    7d = 7 days             │
  │  2w = 2 weeks  1m = 1 month  1y = 1 yr │
  │  Enter = 1 week (default)              │
  └────────────────────────────────────────┘{RESET}""")
            try:
                since = input(f"  Since: ").strip() or "1w"
            except (KeyboardInterrupt, EOFError):
                return
            cmd_member_activity(cfg, user, since)

        elif action == "3":
            cmd_member_access(cfg, user)

        elif action == "4":
            import caps as _caps
            info = _caps.get(cfg)
            can_manage = _caps.can(info, "admin")
            while True:
                print(f"\n{BOLD}  🔐 Manage — {name}{RESET}\n")
                print(f"  {CYAN}1.{RESET}  Group Permissions  — add/revoke access")
                if can_manage:
                    print(f"  {CYAN}2.{RESET}  Edit Account       — edit profile fields")
                    print(f"  {CYAN}3.{RESET}  User Actions       — block/ban/delete")
                else:
                    print(f"  {DIM}2.  Edit Account       — admin required{RESET}")
                    print(f"  {DIM}3.  User Actions       — admin required{RESET}")
                print(f"\n  {YELLOW}b{RESET}  back\n")
                try:
                    sub = input(f"{BOLD}  Choice (1/2/3): {RESET}").strip().lower()
                except (KeyboardInterrupt, EOFError):
                    break
                if sub == "b":
                    break
                elif sub == "1":
                    cmd_group_permissions(cfg, user)
                elif sub in ("2", "3") and not can_manage:
                    print_warn("  This action requires admin access.")
                elif sub == "2":
                    cmd_edit_account(cfg, user)
                elif sub == "3":
                    cmd_user_actions(cfg, user)
                    updated = _get_one(cfg, f"users/{user.get('id')}")
                    if updated:
                        user.update(updated)
                        state = user.get("state", "active")

        try:
            nxt = input(f"\n{DIM}  Enter = continue   q = exit: {RESET}").strip()
            if nxt.lower() == "q":
                sys.exit(0)
        except (KeyboardInterrupt, EOFError):
            return


# ─── Manage Section ────────────────────────────────────────────────────────────

def cmd_group_permissions(cfg: dict, user: dict):
    """Add or revoke group/project access for a user."""
    name    = user.get("name", "")
    user_id = user.get("id")

    print_section(f"Group Permissions: {name}")
    print_info("Fetching all groups...")
    print_separator()

    # Get all groups with full tree
    all_groups = _get_paginated(cfg, "groups", {"all_available": True})
    all_groups = sorted(all_groups, key=lambda g: g["full_path"])

    # Get current memberships
    memberships_raw   = _get_user_memberships(cfg, user_id)
    current_access    = {
        m.get("source_id"): m.get("access_level", 0)
        for m in memberships_raw
        if m.get("source_type") == "Namespace"
    }

    # Build indexed list
    items = []
    for g in all_groups:
        has_access = g["id"] in current_access
        role_level = current_access.get(g["id"], 0)
        depth      = g["full_path"].count("/")
        items.append({
            "idx":        len(items) + 1,
            "id":         g["id"],
            "full_path":  g["full_path"],
            "name":       g["name"],
            "depth":      depth,
            "has_access": has_access,
            "role_level": role_level,
        })

    # Print tree
    print(f"\n{BOLD}{WHITE}  Groups ({len(items)} total):{RESET}\n")
    for item in items:
        indent    = "   " * item["depth"]
        connector = "└──" if item["depth"] > 0 else "📁"
        has_mark  = f"{GREEN}✔{RESET}" if item["has_access"] else f"{DIM}·{RESET}"
        role_str  = f"  {_role_str(item['role_level'])}" if item["has_access"] else ""
        print(f"  {CYAN}{item['idx']:>3}.{RESET}  {has_mark}  {indent}{connector} {BOLD}{item['full_path']}{RESET}{role_str}")

    print(f"\n{DIM}  {'─'*45}{RESET}")
    print(f"  {GREEN}✔ = has access{RESET}   {DIM}· = no access{RESET}")
    print(f"  {YELLOW}q{RESET}  exit\n")

    while True:
        try:
            choice = input(f"{BOLD}  Which group? (number): {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            return

        if choice.lower() == "q":
            return

        if not choice.isdigit():
            print(f"  {RED}Enter a number.{RESET}")
            continue

        idx = int(choice) - 1
        if not (0 <= idx < len(items)):
            print(f"  {RED}Enter 1 to {len(items)}.{RESET}")
            continue

        selected = items[idx]
        _manage_group_access(cfg, user, selected)
        return


def _manage_group_access(cfg: dict, user: dict, group: dict):
    """Add or revoke access for a user on a specific group."""
    name    = user.get("name", "")
    user_id = user.get("id")
    gid     = group["id"]
    gpath   = group["full_path"]
    has_acc = group["has_access"]

    print(f"\n  {BOLD}{gpath}{RESET}")
    if has_acc:
        current_role = _role_str(group["role_level"])
        print(f"  Current access: {current_role}\n")
        print(f"  {CYAN}1.{RESET}  🔄  Change role")
        print(f"  {CYAN}2.{RESET}  {RED}🗑  Remove access (Revoke){RESET}")
        print(f"\n  {YELLOW}b{RESET}  back\n")
        try:
            action = input(f"{BOLD}  Choice: {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            return
        if action == "b":
            return
        if action == "1":
            role_level = _pick_role()
            if role_level:
                _set_group_member(cfg, gid, user_id, role_level, name, gpath)
        elif action == "2":
            _revoke_group_member(cfg, gid, user_id, name, gpath)
    else:
        print(f"  No access currently.\n")
        print(f"  {CYAN}1.{RESET}  ➕  Grant access")
        print(f"\n  {YELLOW}b{RESET}  back\n")
        try:
            action = input(f"{BOLD}  Choice: {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            return
        if action == "1":
            role_level = _pick_role()
            if role_level:
                _set_group_member(cfg, gid, user_id, role_level, name, gpath)


def _pick_role() -> int | None:
    print(f"\n{BOLD}  Pick a role:{RESET}\n")
    print(f"  {CYAN}1.{RESET}  Guest       (10)")
    print(f"  {CYAN}2.{RESET}  Reporter    (20)")
    print(f"  {CYAN}3.{RESET}  Developer   (30)")
    print(f"  {CYAN}4.{RESET}  Maintainer  (40)")
    print(f"  {CYAN}5.{RESET}  Owner       (50)")
    print(f"\n  {YELLOW}b{RESET}  back\n")
    roles = {"1": 10, "2": 20, "3": 30, "4": 40, "5": 50}
    try:
        choice = input(f"{BOLD}  Choice: {RESET}").strip()
    except (KeyboardInterrupt, EOFError):
        return None
    if choice == "b":
        return None
    return roles.get(choice)


def _set_group_member(cfg: dict, gid: int, user_id: int,
                      access_level: int, name: str, gpath: str):
    url = f"{cfg['url']}/api/v4/groups/{gid}/members"
    try:
        # Try add first
        res = requests.post(url, headers=cfg["headers"],
                            json={"user_id": user_id, "access_level": access_level},
                            timeout=15)
        if res.status_code == 409:
            # Already member → update
            res = requests.put(f"{url}/{user_id}", headers=cfg["headers"],
                               json={"access_level": access_level}, timeout=15)
        if res.status_code in (200, 201):
            role = _role_str(access_level)
            print(f"\n  {GREEN}✔ {name} → {gpath}  {role}{RESET}\n")
        else:
            print_error(f"Failed: HTTP {res.status_code} — {res.text[:100]}")
    except Exception as e:
        print_error(f"Connection error: {e}")


def _revoke_group_member(cfg: dict, gid: int, user_id: int, name: str, gpath: str):
    try:
        confirm = input(f"\n  {RED}Are you sure? Remove {name} from {gpath}? (y/n): {RESET}").strip()
    except (KeyboardInterrupt, EOFError):
        return
    if confirm.lower() != "y":
        print(f"  {DIM}Cancelled.{RESET}")
        return
    url = f"{cfg['url']}/api/v4/groups/{gid}/members/{user_id}"
    try:
        res = requests.delete(url, headers=cfg["headers"], timeout=15)
        if res.status_code == 204:
            print(f"\n  {GREEN}✔ Access removed.{RESET}\n")
        else:
            print_error(f"Failed: HTTP {res.status_code}")
    except Exception as e:
        print_error(f"Connection error: {e}")


def cmd_edit_account(cfg: dict, user: dict):
    """Edit user account fields."""
    name     = user.get("name", "")
    user_id  = user.get("id")
    username = user.get("username", "")

    print_section(f"Edit Account: {name}")

    fields = {
        "1":  ("name",              "Name",              user.get("name", "")),
        "2":  ("username",          "Username",          user.get("username", "")),
        "3":  ("email",             "Email",             user.get("email", "")),
        "4":  ("password",          "Password",          ""),
        "5":  ("projects_limit",    "Projects Limit",    str(user.get("projects_limit", 100000))),
        "6":  ("can_create_group",  "Can Create Group",  str(user.get("can_create_group", True))),
        "7":  ("private_profile",   "Private Profile",   str(user.get("private_profile", False))),
        "8":  ("admin",             "Access Level",      "Admin" if user.get("is_admin") else "Regular"),
        "9":  ("external",          "External",          str(user.get("external", False))),
        "10": ("skype",             "Skype",             user.get("skype", "") or ""),
        "11": ("linkedin",          "LinkedIn",          user.get("linkedin", "") or ""),
        "12": ("twitter",           "X (Twitter)",       user.get("twitter", "") or ""),
        "13": ("website_url",       "Website URL",       user.get("website_url", "") or ""),
        "14": ("note",              "Admin Note",        user.get("note", "") or ""),
    }

    print(f"\n{BOLD}{CYAN}👤 {name}{RESET}  {DIM}@{username}{RESET}\n")
    for key, (_field, label, val) in fields.items():
        display = f"{DIM}(empty){RESET}" if not val else val
        print(f"  {CYAN}{key:>2}.{RESET}  {DIM}{label:20}{RESET}  {display}")

    print(f"\n  {YELLOW}b{RESET}  back\n")

    try:
        choice = input(f"{BOLD}  Which field? (number): {RESET}").strip()
    except (KeyboardInterrupt, EOFError):
        return

    if choice == "b" or choice not in fields:
        return

    field_key, label, current = fields[choice]

    # Special cases
    if field_key == "admin":
        print(f"\n  {CYAN}1.{RESET}  Regular")
        print(f"  {CYAN}2.{RESET}  Administrator")
        print(f"  {CYAN}3.{RESET}  External\n")
        try:
            sel = input(f"{BOLD}  Choice: {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            return
        payload = {}
        if sel == "1":
            payload = {"admin": False, "external": False}
        elif sel == "2":
            payload = {"admin": True}
        elif sel == "3":
            payload = {"external": True}
        else:
            return

    elif field_key in ("can_create_group", "private_profile", "external"):
        print(f"\n  {CYAN}1.{RESET}  True")
        print(f"  {CYAN}2.{RESET}  False\n")
        try:
            sel = input(f"{BOLD}  Choice: {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            return
        payload = {field_key: sel == "1"}

    else:
        try:
            new_val = input(f"  {label} [{current}]: ").strip()
        except (KeyboardInterrupt, EOFError):
            return
        if not new_val:
            print(f"  {DIM}No change.{RESET}")
            return
        payload = {field_key: new_val}

    # Send update
    url = f"{cfg['url']}/api/v4/users/{user_id}"
    try:
        res = requests.put(url, headers=cfg["headers"], json=payload, timeout=15)
        if res.status_code == 200:
            print(f"\n  {GREEN}✔ Updated!{RESET}\n")
        else:
            print_error(f"Failed: HTTP {res.status_code} — {res.text[:150]}")
    except Exception as e:
        print_error(f"Connection error: {e}")


def cmd_user_actions(cfg: dict, user: dict):
    """Block/Unblock/Ban/Delete user."""
    name     = user.get("name", "")
    user_id  = user.get("id")
    username = user.get("username", "")
    state    = user.get("state", "active")
    is_admin = user.get("is_admin", False)

    print_section(f"User Actions: {name}")
    print(f"\n{BOLD}{CYAN}👤 {name}{RESET}  {DIM}@{username}{RESET}  "
          f"{'🔴 Blocked' if state == 'blocked' else '🟢 Active'}\n")

    if state == "active":
        print(f"  {CYAN}1.{RESET}  🔒  Block          — cannot log in")
        print(f"  {CYAN}2.{RESET}  😴  Deactivate      — account becomes inactive")
        print(f"  {CYAN}3.{RESET}  🚫  Ban             — block + IP ban")
        print(f"  {CYAN}4.{RESET}  ✅  Trust           — becomes a trusted user")
    else:
        print(f"  {CYAN}1.{RESET}  🔓  Unblock         — restores access")
        print(f"  {CYAN}2.{RESET}  ▶️   Activate        — reactivates the account")

    print(f"  {CYAN}5.{RESET}  {RED}🗑   Delete user{RESET}             — account is deleted")
    print(f"  {CYAN}6.{RESET}  {RED}💣  Delete + contributions{RESET}   — account + all commits/MRs")
    print(f"\n  {YELLOW}b{RESET}  back\n")

    try:
        action = input(f"{BOLD}  Choice: {RESET}").strip()
    except (KeyboardInterrupt, EOFError):
        return

    if action == "b":
        return

    url_base = f"{cfg['url']}/api/v4/users/{user_id}"

    actions_map = {
        "1": ("block",      "POST",   f"{url_base}/block",      f"{name} has been blocked"),
        "2": ("deactivate", "POST",   f"{url_base}/deactivate", f"{name} has been deactivated"),
        "3": ("ban",        "POST",   f"{url_base}/ban",        f"{name} has been banned"),
        "4": ("trust",      "POST",   f"{url_base}/trust",      f"{name} is now trusted"),
    }

    if state != "active":
        actions_map["1"] = ("unblock",  "POST", f"{url_base}/unblock",  f"{name} has been unblocked")
        actions_map["2"] = ("activate", "POST", f"{url_base}/activate", f"{name} has been activated")

    if action in ("5", "6"):
        # Delete — needs double confirmation
        try:
            c1 = input(f"\n  {RED}⚠ Are you sure you want to delete {name}? (yes/n): {RESET}").strip()
            if c1 != "yes":
                print(f"  {DIM}Cancelled.{RESET}")
                return
            c2 = input(f"  {RED}Type the username again to confirm (@{username}): {RESET}").strip()
            if c2 != username:
                print(f"  {RED}Wrong username. Cancelled.{RESET}")
                return
        except (KeyboardInterrupt, EOFError):
            return

        params = {"hard_delete": action == "6"}
        try:
            res = requests.delete(url_base, headers=cfg["headers"],
                                  params=params, timeout=30)
            if res.status_code == 204:
                msg = "and all their contributions" if action == "6" else ""
                print(f"\n  {GREEN}✔ {name} {msg} deleted.{RESET}\n")
            else:
                print_error(f"Failed: HTTP {res.status_code} — {res.text[:150]}")
        except Exception as e:
            print_error(f"Connection error: {e}")
        return

    if action not in actions_map:
        print(f"  {RED}Invalid option.{RESET}")
        return

    _, method, url, success_msg = actions_map[action]

    # Confirm
    try:
        confirm = input(f"\n  Are you sure? ({action} → {name})  (y/n): ").strip()
    except (KeyboardInterrupt, EOFError):
        return
    if confirm.lower() != "y":
        print(f"  {DIM}Cancelled.{RESET}")
        return

    try:
        res = requests.request(method, url, headers=cfg["headers"], timeout=15)
        if res.status_code in (200, 201, 204):
            print(f"\n  {GREEN}✔ {success_msg}{RESET}\n")
        else:
            print_error(f"Failed: HTTP {res.status_code} — {res.text[:150]}")
    except Exception as e:
        err = str(e)
        log_error(f"edit_account:user_{user_id}", err)
        if "NameResolutionError" in err or "Max retries" in err:
            print_error("Network error — check your VPN or internet connection!")
        else:
            print_error(f"Error: {err[:100]}")
