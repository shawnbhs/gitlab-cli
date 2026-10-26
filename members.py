# members.py — GitLab Members management
#
# FAILURE POLICY (W6-3). This module used to carry the founding defect of this
# project: a private `_get_paginated` that returned the rows it had collected
# so far whenever a page failed (401/403/non-200/exception). A group with 40
# members whose page 2 500'd rendered as a tidy list of 20, and a token without
# scope rendered as "No users found." — both indistinguishable from the truth.
#
# Every listing path now goes through `api.get_paginated`, which is the
# contract the rest of the codebase already uses:
#
#   strict=True  (DEFAULT) -> a failed or truncated fetch raises api.ApiError.
#   strict=False           -> returns an api.PagedResult (a real list) whose
#                             `.complete` flag says whether it finished.
#
# Choice per screen:
#   * MUTATING screens (Group Permissions) fetch STRICT and abort on ApiError.
#     A missing group in that tree reads as "no access", and the operator would
#     then grant or revoke the wrong thing.
#   * DISPLAY-ONLY screens (Members list, Activity, Access Map) fetch
#     strict=False and print an explicit, unmissable ⚠ INCOMPLETE banner plus a
#     "fetched N, true total UNKNOWN" count. Raising there would turn a flaky
#     VPN into a dead menu; a silent short count is what we are forbidding, not
#     a labelled one.
#
# In no path may a number be shown that silently under-reports.

import requests
from collections import defaultdict

import re

import api
import error_catalog
from api import ApiError, PagedResult
from errors import log_error
from printer import (
    print_section, print_info, print_warn, print_error,
    print_separator, menu_key, print_unknown_choice,
    BOLD, CYAN, GREEN, YELLOW, DIM, RESET, RED, WHITE
)

# The Since validator lives in log.py and is REUSED, not reimplemented.
# This project's recurring defect is one rule existing in two places and
# drifting apart (it currently has three directory walkers that disagree).
# log.validate_since / log.validate_author are side-effect-free and already
# carry the two-door design and its regression suite; a second copy here
# would be the next thing to drift.
from log import (
    validate_since as _log_validate_since,
    validate_author as _log_validate_author,
    SinceError, AuthorError,
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


# ─── Failure lines ─────────────────────────────────────────────────────────────
#
# Every failure these screens report is ONE line from error_catalog:
# "Title — cause → fix". Never a bare status, never "unknown error".
#
# The catalog cannot know WHY GitLab refused a members endpoint with 403; this
# module can, from the endpoint. Those three cases get the specific fix,
# because "access denied" alone sends a non-admin chasing the wrong problem.

_FIX_403_MEMBERSHIPS = ("Only an administrator token can read another user's "
                        "memberships; ask a GitLab admin.")
_FIX_403_MEMBERS = ("Needs an admin token, or Maintainer (or Owner) role on "
                    "that group.")
_FIX_403_USERS = "This endpoint is admin-only; use an administrator's token."
# The instance-wide user list and per-user account writes are admin-only.
# Per-user READS such as /events are not, so they keep the catalog's fix.
_ADMIN_USER_ENDPOINT = re.compile(
    r"^users(/\d+(/(block|unblock|ban|unban|deactivate|activate|trust|untrust))?)?$")


def _endpoint(path_or_url: str) -> str:
    """'https://h/api/v4/groups/5/members/all?x=1' -> 'groups/5/members/all'."""
    return (path_or_url or "").split("/api/v4/", 1)[-1].split("?", 1)[0].strip("/")


def _fix_for_403(path_or_url: str) -> str | None:
    ep = _endpoint(path_or_url)
    if ep.endswith("/memberships"):
        return _FIX_403_MEMBERSHIPS
    if "/members" in ep:
        return _FIX_403_MEMBERS
    if _ADMIN_USER_ENDPOINT.match(ep):
        return _FIX_403_USERS
    return None


def _catalog_text(status: int, message: str) -> str:
    """What the catalog (and the error log) is given: 'HTTP 403: …' or the
    bare transport text when no HTTP status was received."""
    return f"HTTP {status}: {message}" if status else message


def _failure_line(status: int, message: str, path: str = "") -> str:
    """One line, 'Title — cause → fix', for a failure on endpoint `path`."""
    info = error_catalog.explain(_catalog_text(status, message))
    fix = (_fix_for_403(path) if status == 403 else None) or info.fix
    if info.code == "unknown":
        # The catalog did not recognise it. Say what GitLab actually said
        # rather than "unknown error".
        title = f"GitLab API error (HTTP {status})" if status else "Request failed"
        cause = error_catalog.summarize(message, 110) or "no detail was returned"
        return f"{title} — {cause} → {fix}"
    return f"{info.title} — {info.cause} → {fix}"


def _api_error_line(err) -> str:
    """Catalog line for an api.ApiError (or anything shaped like one)."""
    status = getattr(err, "status", 0) or 0
    message = getattr(err, "message", None) or str(err)
    return _failure_line(status, message, getattr(err, "url", ""))


def _response_detail(res) -> str:
    """The informative part of an error response body (never head-cut)."""
    reason = getattr(res, "reason", "") or ""
    body = error_catalog.summarize(getattr(res, "text", "") or "")
    return " ".join(p for p in (reason, body) if p) or "no response body"


def _report_write_failure(context: str, path: str, res=None, exc=None) -> None:
    """Log and print one catalog line for a failed WRITE (grant/revoke/edit)."""
    if exc is not None:
        status, message = 0, f"{type(exc).__name__}: {exc}"
    else:
        status, message = res.status_code, _response_detail(res)
    log_error(context, _catalog_text(status, message))
    print_error(f"Not changed. {_failure_line(status, message, path)}")


def _get_paginated(cfg: dict, path: str, params: dict = None,
                   quiet: bool = False, strict: bool = True) -> PagedResult:
    """
    Fetch every page of a paginated endpoint, through api.get_paginated.

    strict=True  (DEFAULT) — any failure raises api.ApiError. A short list is
                 never handed back as if it were the whole set.
    strict=False — returns a PagedResult; the caller MUST inspect `.complete`
                 and surface the partiality on screen.

    A successful fetch that finds nothing returns an EMPTY, COMPLETE result —
    which is the only way a caller can tell "this group has no members" apart
    from "the members request failed".
    """
    url = f"{cfg['url']}/api/v4/{path}"
    try:
        out = api.get_paginated(url, cfg["headers"], params,
                                quiet=quiet, strict=strict)
    except ApiError as e:
        log_error(f"api:{path}", f"{e.status} — {e.message}")
        raise
    if not out.complete and out.error is not None:
        log_error(f"api:{path}", f"{out.error.status} — {out.error.message}")
    return out


def _is_partial(result) -> bool:
    """True when `result` is a PagedResult that did NOT run to completion."""
    return getattr(result, "complete", True) is False


def _partial_banner(result, what: str) -> bool:
    """
    Print a loud, unmissable marker when a listing is incomplete, so the count
    printed next to it can never be read as the truth. Returns True if partial.
    """
    if not _is_partial(result):
        return False
    err = getattr(result, "error", None)
    failed = getattr(result, "failed_sources", None) or []
    print(f"\n  {RED}{BOLD}⚠ INCOMPLETE — THIS IS NOT THE FULL LIST.{RESET}")
    print(f"  {RED}{what}: {len(result)} row(s) were fetched before the fetch "
          f"failed. The real total is UNKNOWN.{RESET}")
    if err is not None:
        print(f"  {RED}{_api_error_line(err)}{RESET}")
    else:
        print(f"  {RED}No error detail was recorded for this failure.{RESET}")
    if failed:
        shown = ", ".join(str(f) for f in failed[:5])
        more  = f" (+{len(failed) - 5} more)" if len(failed) > 5 else ""
        print(f"  {DIM}sources that failed: {shown}{more}{RESET}")
    print(f"  {DIM}Re-run after the fix before trusting any number below.{RESET}\n")
    return True


def _count_label(result, n: int) -> str:
    """'42 total' when complete, '42 fetched — INCOMPLETE …' when not."""
    if _is_partial(result):
        return f"{n} fetched — ⚠ INCOMPLETE, true total UNKNOWN"
    return f"{n} total"


def _can_list_all_users(cfg: dict) -> bool:
    """Only an admin can see the full /users list.

    A probe that FAILED is not proof of "no admin access": it is reported as a
    failure, and the screen falls back to the group-derived view.
    """
    try:
        import caps
        info = caps.get(cfg)
    except Exception as exc:   # caps must never take the Members screen down
        print_warn("Admin check failed: "
                   + _failure_line(0, f"{type(exc).__name__}: {exc}"))
        return False
    if not info.get("ok"):
        detail = info.get("error") or "capability probe did not complete"
        m = re.match(r"^(?:HTTP )?(\d{3})\b", detail)
        if not m and detail.startswith("Network error"):
            # caps.probe's wording for any transport failure; name it as one
            # so the catalog gives the VPN/connection fix, not "unrecognised".
            detail = f"ConnectionError: {detail}"
        print_warn("Admin check failed: "
                   + _failure_line(int(m.group(1)) if m else 0, detail, "user"))
        return False
    return caps.can(info, "all_users")


def _fetch_one(cfg: dict, path: str) -> tuple:
    """(data, None) on success, (None, catalog_line) on any failure."""
    try:
        res = requests.get(
            f"{cfg['url']}/api/v4/{path}",
            headers=cfg["headers"], timeout=15
        )
    except requests.RequestException as exc:
        return None, _failure_line(0, f"{type(exc).__name__}: {exc}", path)
    if res.status_code != 200:
        return None, _failure_line(res.status_code, _response_detail(res), path)
    try:
        return res.json(), None
    except ValueError:
        return None, _failure_line(200, "unparseable JSON body", path)


def _get_all_users(cfg: dict) -> PagedResult:
    """
    Admin   → every user on the instance.
    Regular → only the members of groups the user belongs to.

    Returns a PagedResult (a real list). DISPLAY-ONLY, so it never raises: the
    caller renders `.complete` with _partial_banner() instead. An incomplete
    admin listing is NOT silently swapped for the smaller group-derived list —
    the fallback result is flagged partial so the screen says so.
    """
    if _can_list_all_users(cfg):
        active  = _get_paginated(cfg, "users", {"active": True},
                                 quiet=True, strict=False)
        blocked = _get_paginated(cfg, "users", {"blocked": True},
                                 quiet=True, strict=False)
        broke = [r for r in (active, blocked) if _is_partial(r)]
        if not broke:
            users = sorted(list(active) + list(blocked),
                           key=lambda u: u.get("name", "").lower())
            # A complete admin listing that is genuinely empty stays complete.
            return PagedResult(users, complete=True, error=None,
                               pages=active.pages + blocked.pages)

        # The admin listing could not be completed. Try the group-derived view,
        # but never present it as the whole instance.
        fallback = _get_users_from_my_groups(cfg)
        merged = {}
        for u in list(active) + list(blocked) + list(fallback):
            uid = u.get("id")
            if uid is not None:
                merged.setdefault(uid, u)
        users = sorted(merged.values(), key=lambda u: u.get("name", "").lower())
        out = PagedResult(users, complete=False, error=broke[0].error,
                          pages=active.pages + blocked.pages)
        out.failed_sources = (["/users?active"] if _is_partial(active) else []) \
                           + (["/users?blocked"] if _is_partial(blocked) else []) \
                           + list(getattr(fallback, "failed_sources", []) or [])
        return out

    return _get_users_from_my_groups(cfg)


def _get_users_from_my_groups(cfg: dict) -> PagedResult:
    """
    Fallback for non-admin users: collect the members of every group the
    user belongs to and merge them into a single list.

    Every fetch here is a listing that used to swallow its own failure. Now:
    a failed group listing, or ANY failed per-group member listing, marks the
    merged result incomplete and names the group that failed. A user missing
    from this screen must be attributable to an error, never to silence.
    """
    print_info("Admin listing not available — collecting members from your "
               "own groups…")

    groups = _get_paginated(cfg, "groups", {"min_access_level": 10},
                            quiet=True, strict=False)
    failed = []
    if _is_partial(groups):
        failed.append("/groups")

    seen = {}
    first_err = groups.error if _is_partial(groups) else None
    for g in groups:
        path = g.get("full_path", g.get("name", ""))
        members = _get_paginated(cfg, f"groups/{g['id']}/members/all",
                                 quiet=True, strict=False)
        if _is_partial(members):
            failed.append(f"groups/{path}/members")
            first_err = first_err or members.error
        for m in members:
            uid = m.get("id")
            if uid is None:
                continue
            if uid not in seen:
                m = dict(m)
                m["_groups"] = []
                seen[uid] = m
            seen[uid]["_groups"].append({
                "path":  path,
                "level": m.get("access_level", 0),
            })

    users = sorted(seen.values(), key=lambda u: u.get("name", "").lower())
    out = PagedResult(users, complete=not failed, error=first_err,
                      pages=getattr(groups, "pages", 0))
    out.failed_sources = failed
    return out


def _get_project_info(cfg: dict, project_id: int, cache: dict) -> dict:
    if project_id not in cache:
        data, failure = _fetch_one(cfg, f"projects/{project_id}")
        if data:
            cache[project_id] = {
                "name": data.get("name", ""),
                "path": data.get("path_with_namespace", ""),
            }
        else:
            # Display decoration only, so it is shown inline, not raised.
            title = (failure or "").split(" — ", 1)[0]
            cache[project_id] = {"name": f"project#{project_id} ({title})",
                                 "path": ""}
    return cache[project_id]


def _get_user_events(cfg: dict, user_id: int, since_date: str) -> PagedResult:
    """Activity events. DISPLAY-ONLY → strict=False; caller shows partiality."""
    return _get_paginated(cfg, f"users/{user_id}/events", {"after": since_date},
                          quiet=True, strict=False)


def _get_user_memberships(cfg: dict, user_id: int,
                          strict: bool = False) -> PagedResult:
    """
    All groups a user belongs to — memberships API (fast, 1-2 calls).

    strict=False (default) for the read-only Access Map, which labels a partial
    answer. cmd_group_permissions passes strict=True: there, a truncated
    membership list means an existing member renders as "no access" and the
    operator grants or revokes the wrong thing.
    """
    return _get_paginated(cfg, f"users/{user_id}/memberships",
                          quiet=True, strict=strict)


# ─── Since / Author validation ────────────────────────────────────────────────
#
# WHY THIS EXISTS (same defect as log.py, one mechanism worse).
#
# `_parse_since` used to translate ONLY the `\d+[dwmy]` shortcut into a real
# date and hand EVERYTHING ELSE to GitLab verbatim as `?after=<value>`. The
# events endpoint wants an ISO date there. Measured pre-fix:
#
#     _parse_since('b')            -> 'b'            -> ?after=b
#     _parse_since('')             -> ''             -> ?after=
#     _parse_since('2 weeks ago')  -> '2 weeks ago'  -> ?after=2 weeks ago
#     _parse_since('not a date')   -> 'not a date'   -> ?after=not a date
#     _parse_since('--all')        -> '--all'        -> ?after=--all
#
# GitLab answers an unparseable `after` with an empty page set, which this
# screen printed as "⚠ No activity found since b." — a confident claim about
# a person's work, computed by a mechanism that never looked at their work.
# Note this is BROADER than log.py's bug: there, `2 weeks ago` was a correct
# filter; here it was silently garbage too, because `?after=` is not git's
# approxidate parser.
#
# The fix reuses log.validate_since (two doors: positive recognition, and
# "did git actually move the clock?") and then converts the epoch git settled
# on into the ISO date the API needs — so the value the user typed and the
# value the API received are provably the same moment, and that moment is
# printed.

def _parse_since(since: str) -> str:
    """Legacy shortcut expansion. UNVALIDATED — do not call it directly.

    Kept because it is the local, offline expansion of `Nd/Nw/Nm/Ny` and is
    still the fallback used when git is unavailable to resolve a date. Every
    caller now goes through `validated_since()`, which will not return a value
    it has not either recognised or watched git resolve.
    """
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


def _iso_day_for_after(epoch: int) -> str:
    """Epoch → the `?after=` day GitLab needs, with the 1-day buffer kept.

    GitLab's `after` is exclusive on the DAY, so the original code subtracted
    a day to make the requested range inclusive. That behaviour is preserved
    exactly; only the way the day is arrived at has changed.
    """
    from datetime import datetime, timedelta
    dt = datetime.fromtimestamp(epoch) - timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def validated_since(raw: str) -> tuple:
    """(after_date, spoken_value, note) — or raise SinceError.

    `after_date`    the ISO day actually sent as `?after=`.
    `spoken_value`  what the user's input was understood to mean.
    `note`          the absolute moment git resolved, for echoing back.

    Delegates the judgement entirely to `log.validate_since`. The only thing
    added here is the epoch→ISO-day conversion the events API requires, which
    log.py does not need and must not grow a second opinion about.
    """
    if raw is None:
        raise SinceError("No 'Since' value was given.")

    parsed, epoch, note = _log_validate_since(raw)

    if epoch is not None:
        return _iso_day_for_after(epoch), parsed, note

    # git was unavailable but Door A recognised the value. The API needs an
    # absolute day regardless, so fall back to the offline shortcut — and only
    # when that yields a real date. Never send the raw words to `?after=`.
    offline = _parse_since(raw)
    import re as _re
    if _re.match(r"^\d{4}-\d{2}-\d{2}$", offline):
        return offline, parsed, f"{offline} (computed locally; {note})"
    raise SinceError(
        "'%s' could not be resolved to a calendar date, and GitLab's "
        "activity filter needs one. %s. Nothing was queried — an unresolved "
        "filter would come back empty and read as 'no activity'." % (raw, note))


def validated_author(author):
    """Reused from log.py verbatim; raises AuthorError. See log.validate_author.

    `--author` is a REGULAR EXPRESSION to git: `[` makes git exit 128 and the
    wrapper maps a non-zero exit to an empty list, which reads to the user as
    'no results'. members.py has no `--author` git call site today (see the
    report's value map), so this is the guard for any caller that passes one
    through and for the CLI surface, not dead weight in an existing path.
    """
    _log_validate_author(author)


def _print_since_refusal(exc):
    """One place that says 'this is about your INPUT, not about your data'."""
    print_error(str(exc))
    print_separator()
    print_warn("Nothing was searched. This is NOT a statement about this "
               "person's activity.")


# ─── Display ───────────────────────────────────────────────────────────────────

def _print_activity(cfg: dict, events: list, user_name: str, since_parsed: str = "",
                    partial: bool = False):
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
    suffix = " — ⚠ INCOMPLETE" if partial else ""
    print(f"\n{BOLD}{CYAN}👤 {user_name}{RESET}  {DIM}({total} event{suffix}){RESET}\n")

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
    if partial:
        print(f"{BOLD}{RED}📊 {total} event(s) shown — ⚠ INCOMPLETE, "
              f"the true total is UNKNOWN{RESET}\n")
    else:
        print(f"{BOLD}📊 Total: {GREEN}{total} event{RESET}\n")


# ─── Commands ──────────────────────────────────────────────────────────────────

def cmd_members_list(cfg: dict):
    print_section("GitLab Members")
    print_info("Fetching all users...")

    users = _get_all_users(cfg)
    partial = _partial_banner(users, "Members")
    if not users:
        if partial:
            print_error("No members could be listed because the fetch FAILED — "
                        "this is an ERROR, not an empty instance (see above).")
        else:
            print_warn("No members — the listing completed and returned nobody.")
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
    if partial:
        print(f"  {RED}{BOLD}⚠ every number below is a FLOOR, not a total{RESET}")
        print(f"  {DIM}Total (fetched):{RESET}  {BOLD}{RED}≥{total_users}{RESET}")
    else:
        print(f"  {DIM}Total         :{RESET}  {BOLD}{total_users}{RESET}")
    print(f"  {GREEN}Admin         :{RESET}  {GREEN}{len(admins)}{RESET}")
    print(f"  {CYAN}Active        :{RESET}  {CYAN}{len(active)}{RESET}")
    print(f"  {DIM}Bot/Token     :{RESET}  {DIM}{len(bots)}{RESET}")
    print(f"  {RED}Blocked       :{RESET}  {RED}{len(blocked)}{RESET}")
    if check != total_users:
        print(f"  {YELLOW}⚠ Check: {check} (diff: {total_users - check}){RESET}")
    print()
    print(f"\n{BOLD}{WHITE}  Members ({_count_label(users, total_users)}):{RESET}\n")

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
    if partial:
        print(f"  {RED}⚠ list is INCOMPLETE — members are missing from it.{RESET}")
    print(f"  {YELLOW}q{RESET}  exit\n")

    while True:
        try:
            raw = input(f"{BOLD}  Which member? (number or name): {RESET}")
        except (KeyboardInterrupt, EOFError):
            return None
        choice = raw.strip()

        if menu_key(raw) == "q":
            return None

        if not choice:
            # Empty is not a default here — no default was printed.
            print(f"  {RED}Nothing entered. Type a number, a name, or q.{RESET}")
            continue

        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(users):
                return users[idx]
            print(f"  {RED}Enter 1 to {len(users)}.{RESET}")
            continue

        needle  = menu_key(choice)
        matches = [u for u in users if
                   needle in menu_key(u.get("name", "")) or
                   needle in menu_key(u.get("username", ""))]
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


def _has_any_events(cfg: dict, user_id: int) -> PagedResult:
    """The same events query with the DATE FILTER REMOVED.

    Asked ONLY when the filtered query came back empty, so the common path
    pays nothing. It is what turns "no activity since X" from a guess into a
    statement: either this person has events and the filter excluded them, or
    they have none at all and the filter is not what the user should change.
    """
    return _get_paginated(cfg, f"users/{user_id}/events",
                          quiet=True, strict=False)


def cmd_member_activity(cfg: dict, user: dict, since: str = "1w",
                        author: str = None):
    name    = user.get("name", "")
    user_id = user.get("id")

    print_section(f"Activity: {name}")

    # Validate BEFORE anything is queried and before anything is claimed.
    # A refusal here is an answer about the INPUT; it must never be able to
    # look like an answer about this person's activity.
    try:
        since_date, spoken, note = validated_since(since)
    except SinceError as exc:
        _print_since_refusal(exc)
        return
    try:
        validated_author(author)
    except AuthorError as exc:
        _print_since_refusal(exc)
        return

    # Echo the interpretation that was ACTUALLY applied. Without this the user
    # cannot see the difference between what they typed and what was queried,
    # which is exactly how the original defect stayed invisible.
    print_info(f"Since : {spoken}")
    print_info(f"        → git read this as {note}")
    print_info(f"        → querying GitLab with after={since_date}")
    if author:
        print_info(f"Author: {author}")
    print_info("Fetching events...")
    print_separator()

    events = _get_user_events(cfg, user_id, since_date)
    partial = _partial_banner(events, "Activity events")
    if not events:
        if partial:
            print_error(f"Activity could not be fetched since {since_date} — "
                        f"this is an ERROR, not an absence of activity.")
            return
        # Verified, not assumed: distinguish "the filter excluded everything"
        # from "there is nothing to exclude".
        unfiltered = _has_any_events(cfg, user_id)
        if _is_partial(unfiltered):
            print_warn(f"No activity after {since_date} (from '{spoken}').")
            print_error("Could NOT verify whether this person has any activity "
                        "at all; treat the line above as unconfirmed.")
            print_error(_api_error_line(unfiltered.error) if unfiltered.error
                        else "The unfiltered check did not complete.")
        elif len(unfiltered) == 0:
            print_warn(f"{name} has NO recorded activity at all — so this is "
                       f"not about your 'Since' filter.")
        else:
            print_warn(f"No activity after {since_date} (from '{spoken}').")
            print_info(f"Verified: {name} does have {len(unfiltered)} recorded "
                       f"event(s), so the filter is what excluded them — widen "
                       f"it (e.g. 1y) to see more.")
        return

    _print_activity(cfg, events, name, since_parsed=since_date, partial=partial)


def cmd_member_access(cfg: dict, user: dict):
    name    = user.get("name", "")
    user_id = user.get("id")

    print_section(f"Access Map: {name}")
    print_info("Fetching memberships...")
    print_separator()

    memberships_raw = _get_user_memberships(cfg, user_id)
    partial = _partial_banner(memberships_raw, "Memberships")
    memberships = list(memberships_raw)

    # The memberships endpoint is admin-only. When it FAILED (partial) — or it
    # completed with nothing — fall back to what we already collected while
    # listing the members. The fallback is a strictly smaller view, so it never
    # clears the partial flag: it is labelled, not laundered.
    if (partial or not memberships) and user.get("_groups"):
        print_info("From shared groups (limited access)")
        for g in user["_groups"]:
            memberships.append({
                "source_type":      "Namespace",
                "source_full_name": g["path"],
                "access_level":     g["level"],
            })

    if not memberships:
        if partial:
            print_error(f"The membership list for {name} could not be fetched "
                        f"— this is an ERROR, not proof of no memberships.")
        else:
            print_warn(f"{name} is not a member of any group.")
        return

    # Filter only Group type
    group_memberships = [
        m for m in memberships
        if m.get("source_type") == "Namespace"
    ]

    if not group_memberships:
        if partial:
            print_error(f"No group memberships were retrieved for {name}, and "
                        f"the fetch was INCOMPLETE — do not read this as "
                        f"'not in any group'.")
        else:
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
    label = (f"{total} membership — ⚠ INCOMPLETE, true total UNKNOWN"
             if partial else f"{total} membership")
    print(f"\n{BOLD}{CYAN}👤 {name}{RESET}  {DIM}({label}){RESET}\n")

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
    if partial:
        print(f"  {RED}⚠ counts above are a FLOOR — the membership fetch did "
              f"not complete.{RESET}")
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
            action = menu_key(input(f"{BOLD}  Choice: {RESET}"))
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
                since = input(f"  Since [1w]: ").strip() or "1w"
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
                    sub = menu_key(input(f"{BOLD}  Choice (1/2/3): {RESET}"))
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
                    updated, failure = _fetch_one(cfg, f"users/{user.get('id')}")
                    if updated:
                        user.update(updated)
                        state = user.get("state", "active")
                    else:
                        print_warn(f"State shown may be stale — refresh failed: "
                                   f"{failure}")
                else:
                    # Never silently fall through to "continue". An answer the
                    # program does not understand is refused out loud.
                    print_unknown_choice(sub, ["1", "2", "3", "b"])

        else:
            # Unrecognised key: say so instead of quietly redrawing the menu.
            print_unknown_choice(action, ["1", "2", "3", "4", "b", "q"])
            continue

        try:
            nxt = input(f"\n{DIM}  Enter = continue   q = exit: {RESET}")
            if menu_key(nxt) == "q":
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

    # Get all groups with full tree.
    # STRICT on purpose: this screen GRANTS and REVOKES access. A truncated
    # group list silently hides groups, and a truncated membership list makes a
    # real member render "· no access" — the operator then grants access that
    # already exists at a different level, or revokes on the wrong row. Abort
    # instead of drawing a tree that is wrong in an invisible way.
    try:
        all_groups = _get_paginated(cfg, "groups", {"all_available": True},
                                    quiet=True, strict=True)
        memberships_raw = _get_user_memberships(cfg, user_id, strict=True)
    except ApiError as e:
        print_error("Group permissions cannot be shown: the listing did not "
                    "complete.")
        print_error(_api_error_line(e))
        print_warn("Refusing to draw an access tree that may be missing rows — "
                   "granting or revoking from it would target the wrong group.")
        return

    all_groups = sorted(all_groups, key=lambda g: g["full_path"])

    # Get current memberships
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
    if not items:
        print_warn("No groups are visible to this token (the listing completed "
                   "successfully and returned nothing).")
        return
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
            raw = input(f"{BOLD}  Which group? (number): {RESET}")
        except (KeyboardInterrupt, EOFError):
            return
        choice = raw.strip()

        if menu_key(raw) == "q":
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
            action = menu_key(input(f"{BOLD}  Choice: {RESET}"))
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
            print_unknown_choice(action, ["1", "2", "b"])
    else:
        print(f"  No access currently.\n")
        print(f"  {CYAN}1.{RESET}  ➕  Grant access")
        print(f"\n  {YELLOW}b{RESET}  back\n")
        try:
            action = menu_key(input(f"{BOLD}  Choice: {RESET}"))
        except (KeyboardInterrupt, EOFError):
            return
        if action == "1":
            role_level = _pick_role()
            if role_level:
                _set_group_member(cfg, gid, user_id, role_level, name, gpath)
        elif action != "b":
            print_unknown_choice(action, ["1", "b"])


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
        choice = menu_key(input(f"{BOLD}  Choice: {RESET}"))
    except (KeyboardInterrupt, EOFError):
        return None
    if choice == "b":
        return None
    if choice not in roles:
        # Previously `roles.get(choice)` returned None for an unknown key,
        # which the caller could not tell apart from "the user backed out" —
        # an unrecognised input silently treated as a default.
        print_unknown_choice(choice, ["1", "2", "3", "4", "5", "b"])
        return None
    return roles[choice]


def _set_group_member(cfg: dict, gid: int, user_id: int,
                      access_level: int, name: str, gpath: str):
    path = f"groups/{gid}/members"
    url = f"{cfg['url']}/api/v4/{path}"
    context = f"members:set:{gpath}:user_{user_id}"
    try:
        # Try add first
        res = requests.post(url, headers=cfg["headers"],
                            json={"user_id": user_id, "access_level": access_level},
                            timeout=15)
        if res.status_code == 409:
            # Already member → update
            res = requests.put(f"{url}/{user_id}", headers=cfg["headers"],
                               json={"access_level": access_level}, timeout=15)
    except requests.RequestException as e:
        _report_write_failure(context, path, exc=e)
        return
    if res.status_code in (200, 201):
        role = _role_str(access_level)
        print(f"\n  {GREEN}✔ {name} → {gpath}  {role}{RESET}\n")
    else:
        _report_write_failure(context, path, res=res)


def _revoke_group_member(cfg: dict, gid: int, user_id: int, name: str, gpath: str):
    try:
        confirm = input(f"\n  {RED}Are you sure? Remove {name} from {gpath}? (y/n): {RESET}")
    except (KeyboardInterrupt, EOFError):
        return
    if menu_key(confirm) != "y":
        print(f"  {DIM}Cancelled.{RESET}")
        return
    path = f"groups/{gid}/members/{user_id}"
    context = f"members:revoke:{gpath}:user_{user_id}"
    try:
        res = requests.delete(f"{cfg['url']}/api/v4/{path}",
                              headers=cfg["headers"], timeout=15)
    except requests.RequestException as e:
        _report_write_failure(context, path, exc=e)
        return
    if res.status_code == 204:
        print(f"\n  {GREEN}✔ Access removed.{RESET}\n")
    else:
        _report_write_failure(context, path, res=res)


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
        choice = menu_key(input(f"{BOLD}  Which field? (number): {RESET}"))
    except (KeyboardInterrupt, EOFError):
        return

    if choice == "b":
        return
    if choice not in fields:
        # Was: `if choice == "b" or choice not in fields: return` — an
        # unrecognised field number returned to the caller with no word said,
        # indistinguishable from a deliberate "back".
        print_unknown_choice(choice, list(fields.keys()) + ["b"])
        return

    field_key, label, current = fields[choice]

    # Special cases
    if field_key == "admin":
        print(f"\n  {CYAN}1.{RESET}  Regular")
        print(f"  {CYAN}2.{RESET}  Administrator")
        print(f"  {CYAN}3.{RESET}  External\n")
        try:
            sel = menu_key(input(f"{BOLD}  Choice: {RESET}"))
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
            print_unknown_choice(sel, ["1", "2", "3"])
            return

    elif field_key in ("can_create_group", "private_profile", "external"):
        print(f"\n  {CYAN}1.{RESET}  True")
        print(f"  {CYAN}2.{RESET}  False\n")
        try:
            sel = menu_key(input(f"{BOLD}  Choice: {RESET}"))
        except (KeyboardInterrupt, EOFError):
            return
        if sel not in ("1", "2"):
            # Was `payload = {field_key: sel == "1"}` — ANY unrecognised key
            # (including "3", "yes", or a typo) silently became False and was
            # written to the user's account. Same defect class, live blast
            # radius: it changes a permission the operator never chose.
            print_unknown_choice(sel, ["1", "2"])
            return
        payload = {field_key: sel == "1"}

    else:
        try:
            new_val = input(f"  {label} [{current}]: ").strip()
        except (KeyboardInterrupt, EOFError):
            return
        if not new_val:
            # A default that WAS printed ("[current]"), honoured only for the
            # empty string, and it changes nothing. That is the one legitimate
            # shape of a default.
            print(f"  {DIM}No change.{RESET}")
            return
        # Free text, deliberately NOT case-folded — this is a value, not a
        # menu key. It travels as a JSON body field through requests, never
        # through a shell and never as a command-line argument.
        if any(ch in new_val for ch in "\0\n\r"):
            print_error(f"{label} contains a control character — refusing to "
                        f"send it rather than guessing what you meant.")
            return
        payload = {field_key: new_val}

    # Send update
    path = f"users/{user_id}"
    context = f"edit_account:user_{user_id}"
    try:
        res = requests.put(f"{cfg['url']}/api/v4/{path}", headers=cfg["headers"],
                           json=payload, timeout=15)
    except requests.RequestException as e:
        _report_write_failure(context, path, exc=e)
        return
    if res.status_code == 200:
        print(f"\n  {GREEN}✔ Updated!{RESET}\n")
    else:
        _report_write_failure(context, path, res=res)


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
        action = menu_key(input(f"{BOLD}  Choice: {RESET}"))
    except (KeyboardInterrupt, EOFError):
        return

    if action == "b":
        return

    url_base = f"{cfg['url']}/api/v4/users/{user_id}"

    # The map is built PER STATE, matching exactly what was printed above.
    # It used to always contain 3=ban and 4=trust, even for a blocked user
    # whose menu offered only 1, 2, 5, 6 — so typing an un-offered key on that
    # screen silently performed a ban. An answer the screen did not offer is
    # not a shortcut, it is an unrecognised input.
    if state == "active":
        actions_map = {
            "1": ("block",      "POST",   f"{url_base}/block",      f"{name} has been blocked"),
            "2": ("deactivate", "POST",   f"{url_base}/deactivate", f"{name} has been deactivated"),
            "3": ("ban",        "POST",   f"{url_base}/ban",        f"{name} has been banned"),
            "4": ("trust",      "POST",   f"{url_base}/trust",      f"{name} is now trusted"),
        }
    else:
        actions_map = {
            "1": ("unblock",  "POST", f"{url_base}/unblock",  f"{name} has been unblocked"),
            "2": ("activate", "POST", f"{url_base}/activate", f"{name} has been activated"),
        }

    if action in ("5", "6"):
        # Delete — needs double confirmation
        try:
            c1 = input(f"\n  {RED}⚠ Are you sure you want to delete {name}? (yes/n): {RESET}")
            # Case-insensitive like every other prompt: "YES" is the same
            # word as "yes". The guard is that the operator typed the WORD,
            # not that they held shift.
            if menu_key(c1) != "yes":
                print(f"  {DIM}Cancelled.{RESET}")
                return
            c2 = input(f"  {RED}Type the username again to confirm (@{username}): {RESET}")
            # GitLab usernames are case-insensitive identifiers, so the second
            # gate compares them that way. It still proves the operator knows
            # WHICH account they are deleting, which is what the gate is for.
            if menu_key(c2) != menu_key(username):
                print(f"  {RED}Wrong username. Cancelled.{RESET}")
                return
        except (KeyboardInterrupt, EOFError):
            return

        params = {"hard_delete": action == "6"}
        context = f"user_actions:delete:user_{user_id}"
        try:
            res = requests.delete(url_base, headers=cfg["headers"],
                                  params=params, timeout=30)
        except requests.RequestException as e:
            _report_write_failure(context, url_base, exc=e)
            return
        if res.status_code == 204:
            msg = "and all their contributions" if action == "6" else ""
            print(f"\n  {GREEN}✔ {name} {msg} deleted.{RESET}\n")
        else:
            _report_write_failure(context, url_base, res=res)
        return

    if action not in actions_map:
        print_unknown_choice(action, sorted(actions_map.keys()) + ["5", "6", "b"])
        return

    _, method, url, success_msg = actions_map[action]

    # Confirm
    try:
        confirm = input(f"\n  Are you sure? ({action} → {name})  (y/n): ")
    except (KeyboardInterrupt, EOFError):
        return
    if menu_key(confirm) != "y":
        print(f"  {DIM}Cancelled.{RESET}")
        return

    context = f"user_actions:{actions_map[action][0]}:user_{user_id}"
    try:
        res = requests.request(method, url, headers=cfg["headers"], timeout=15)
    except requests.RequestException as e:
        _report_write_failure(context, url, exc=e)
        return
    if res.status_code in (200, 201, 204):
        print(f"\n  {GREEN}✔ {success_msg}{RESET}\n")
    else:
        _report_write_failure(context, url, res=res)
