# api.py — GitLab API helpers
#
# Designed for every access level: admin, owner, developer, guest.
# Wherever an endpoint is admin-only, we keep a membership-based fallback.
#
# ─────────────────────────────────────────────────────────────────────────────
# FAILURE POLICY — the founding invariant of this project
# ─────────────────────────────────────────────────────────────────────────────
# A failure must never be indistinguishable from a legitimate result.
#
# Concretely, in this module:
#
#   1. NO FUNCTION RETURNS A SHORT COLLECTION AS IF IT WERE THE WHOLE SET.
#      get_paginated() raises ApiError by default; the opt-out (strict=False)
#      hands back a PagedResult whose `.complete` is False and whose `.error`
#      says why. An EMPTY, COMPLETE result is the only way to say "there is
#      nothing there".
#
#   2. COMPLETENESS SURVIVES POST-PROCESSING. sorted(), filtering and slicing
#      used to turn a PagedResult back into a plain list, which threw `.complete`
#      away — a flag that structurally cannot reach the caller is silence with
#      extra steps. Every wrapper now rebuilds a PagedResult (PagedResult.like).
#
#   3. NO SINGULAR GETTER CONFLATES "does not exist" WITH "I could not find
#      out". get_group / get_project return None ONLY for a real 404. A 401, a
#      403, a 500 or a dead socket raises ApiError. my_access_level() returns
#      None only when the payload genuinely carries no access level.
#
#   4. `quiet` SUPPRESSES OUTPUT, NEVER KNOWLEDGE. Every failure — quiet or
#      not — is appended to a process-wide ledger (see record_failure /
#      failures / failure_count). quiet=True silences the console line; it can
#      never silence the fact that something went wrong.
#
#   5. A COMPLETE FETCH IS CROSS-CHECKED AGAINST THE SERVER'S OWN COUNT.
#      GitLab sends X-Total on page 1. If we walked every page and still hold
#      fewer records than the server said exist, the fetch was truncated by
#      something we cannot see (a proxy, a rate limiter, a rewritten header)
#      and it is reported as incomplete rather than returned as the truth.
#
# This is the exact defect that let most of an instance's projects be skipped
# silently:
# get_paginated used to `break` out of its loop on a None response, a non-200
# status or a JSON parse error and return what it had. A bad token therefore
# produced zero projects, no exception and no warning — and the callers passed
# quiet=True, so not even an error line printed.

import re
import threading
import time

import requests
from printer import print_error

TIMEOUT = 20

# transient statuses worth one more try before we give up and shout
_RETRY_STATUSES = (429, 500, 502, 503, 504)
_RETRIES = 2
_RETRY_BACKOFF = 1.5  # seconds, multiplied by attempt number


class ApiError(Exception):
    """
    An API error carrying its HTTP status code.

    Attributes read elsewhere (gitlab_cli._api_error_advice / _report_*):
        .status   int, 0 for a transport failure (no HTTP answer at all)
        .message  the specific message this error was raised with
        .url      the full request URL (no credential: tokens travel in headers)
        .title    the error-catalog title for this status (short, <= 40 chars)
        .code     the error-catalog code for this status ("auth", "not_found" ..)

    str(exc) is ONE human line:
        HTTP 401 · Token rejected: Invalid or expired token (groups/12/projects)
    — the status, the catalog title, the message, and a SHORT endpoint (path
    after /api/v4/, no query string). The full URL stays on `.url` for callers
    that print host/endpoint themselves; it never reaches str() because a query
    string can carry a private_token.
    """
    def __init__(self, status: int, message: str, url: str = ""):
        self.status  = status
        self.message = message
        self.url     = url
        # status 0 is used both for "no HTTP answer" (transport, already
        # explained in `message`) and for internal completeness failures
        # (truncation, max_pages, X-Next-Page) — no catalog title fits both,
        # so none is guessed.
        info = _status_info(status, url) if status else None
        self.code    = info.code if info else ""
        self.title   = info.title if info else ""
        super().__init__(_one_line_error(status, message, url, self.title))


def _one_line_error(status: int, message: str, url: str, title: str) -> str:
    head = f"HTTP {status}" if status else "API error"
    # catalog titles carry their own "(401)" / "(5xx)" — drop it, the status
    # is already the first thing on the line
    title = re.sub(r"\s*\([0-9x]{3}\)\s*$", "", title or "")
    if title and title.lower() not in str(message).lower():
        head = f"{head} \u00b7 {title}"
    msg = " ".join(str(message).split())          # one line, always
    endpoint = _short(url) if url else ""
    return f"{head}: {msg}" + (f" ({endpoint})" if endpoint else "")


# ─── The failure ledger: `quiet` may hide the line, never the fact ────────────
#
# The original bug had two halves. The fetch swallowed its error AND the caller
# passed quiet=True, so the only witness to a 401 was a shorter number on the
# screen. Printing is a presentation choice; knowing is not. Every failure this
# module sees is recorded here whether or not it was printed, so a caller, a
# test, or a post-run audit can ask "did anything fail?" and get a real answer.

_FAILURES: list = []
_FAILURES_LOCK = threading.Lock()
_MAX_FAILURES = 500          # bounded: a long run must not grow without limit


def record_failure(status: int, message: str, url: str = "",
                   quiet: bool = False) -> ApiError:
    """
    Record one API failure and return it as an ApiError.

    ALWAYS records. `quiet` only decides whether a line is printed. This is the
    single choke point every failure path in this module goes through, so there
    is no code path that can know about a failure without saying so somewhere.
    """
    err = ApiError(status, message, url)
    with _FAILURES_LOCK:
        _FAILURES.append(err)
        if len(_FAILURES) > _MAX_FAILURES:
            del _FAILURES[:-_MAX_FAILURES]
    if not quiet:
        print_error(f"{message}  ({_short(url)})" if url else message)
    return err


def failures() -> list:
    """Every ApiError recorded since the last reset_failures(), oldest first."""
    with _FAILURES_LOCK:
        return list(_FAILURES)


def failure_count() -> int:
    with _FAILURES_LOCK:
        return len(_FAILURES)


def last_failure():
    with _FAILURES_LOCK:
        return _FAILURES[-1] if _FAILURES else None


def reset_failures() -> None:
    """Clear the ledger — call at the start of an operation you want to audit."""
    with _FAILURES_LOCK:
        _FAILURES.clear()


def _short(url: str) -> str:
    """The endpoint after /api/v4/, without query string or fragment, <= 60."""
    path = str(url).split("?", 1)[0].split("#", 1)[0]
    return path.split("/api/v4/")[-1][:60]


def _host(url: str) -> str:
    try:
        from urllib.parse import urlsplit
        return urlsplit(str(url)).hostname or ""
    except ValueError:
        return ""


class PagedResult(list):
    """
    The result of a paginated fetch.

    It IS a list (so every existing caller keeps working: len(), iteration,
    sorted(), indexing) but it also carries whether the fetch actually ran to
    completion:

        .complete  True  -> every page was fetched; an empty list really means
                            "there is nothing there".
                   False -> the fetch stopped early because of an error; the
                            contents are a PARTIAL result and must not be
                            treated as authoritative.
        .error     the ApiError describing why it is incomplete (or None)
        .pages     how many pages were successfully fetched
        .total     the server's own X-Total for the query, when it sent one
        .note      a human-readable disclosure about what this listing does and
                   does not contain (archived filter, a refused scope, ...)

    `.complete=False` is only reachable via strict=False. The default path
    raises instead, because a flag nobody reads is not much better than
    silence.
    """
    def __init__(self, items=(), complete: bool = True,
                 error: "ApiError | None" = None, pages: int = 0,
                 total: "int | None" = None, note: str = ""):
        super().__init__(items)
        self.complete = complete
        self.error    = error
        self.pages    = pages
        self.total    = total
        self.note     = note

    @property
    def partial(self) -> bool:
        return not self.complete

    @classmethod
    def like(cls, items, source, note: str = "") -> "PagedResult":
        """
        Rebuild a PagedResult around `items`, inheriting `source`'s honesty
        metadata.

        This is what keeps sorted()/filtered wrappers from silently downgrading
        a PagedResult to a plain list: a plain list has no `.complete`, so a
        caller that checks it would see the attribute missing (i.e. assume
        complete) on exactly the path that failed.
        """
        return cls(items,
                   complete=getattr(source, "complete", True),
                   error=getattr(source, "error", None),
                   pages=getattr(source, "pages", 0),
                   total=getattr(source, "total", None),
                   note=note or getattr(source, "note", ""))

    def require_complete(self, what: str = "listing"):
        """Raise the underlying ApiError unless the fetch ran to completion."""
        if not self.complete:
            raise self.error or ApiError(0, f"{what} is incomplete")
        return self

    def __repr__(self) -> str:
        state = "complete" if self.complete else f"PARTIAL ({self.error})"
        extra = f" total={self.total}" if self.total is not None else ""
        return f"<PagedResult n={len(self)} pages={self.pages}{extra} {state}>"


# ─── Per-status explanation, sourced from error_catalog ───────────────────────
#
# error_catalog owns the wording (title / cause / fix). This module only maps
# an HTTP status onto a catalog code; it does not keep a second table of
# advice. Each line below is the short lead the console prints, followed by
# the catalog's fix, so every API error reads the same everywhere:
#     Invalid or expired token — Re-enter it: gitlab-cli → Instances → e → ...
#
# The lead texts are kept here (not in the catalog) only because other
# modules and tests key on them ("Invalid or expired token" for 401). If
# error_catalog cannot be imported, the lead alone is printed — degraded, but
# never a crash in the error path.

_PENDING_DELETION_MARK = "-deletion_scheduled-"

# status -> (catalog code, short lead). 5xx and transport handled in code.
_STATUS_CODES = {
    401: ("auth",         "Invalid or expired token"),
    403: ("forbidden",    "Token lacks the scope/role for this (needs "
                          "read_api; admin for members)"),
    404: ("not_found",    "Not found, or the token has no access"),
    409: ("",             "Request rejected by the server (conflict)"),
    422: ("",             "Request rejected by the server (unprocessable)"),
    429: ("rate_limited", "Rate limited by the server"),
}

# Fallback fix lines, used ONLY when error_catalog is unavailable or lacks the
# code. Kept minimal on purpose; the catalog is the source of truth.
_FALLBACK_FIX = {
    "auth": "re-enter the token: gitlab-cli → Instances → e",
}


def _catalog():
    try:
        import error_catalog
        return error_catalog
    except Exception:                    # noqa: BLE001 - error path must not crash
        return None


def _code_for(status: int, url: str = "") -> str:
    if status == 404 and _PENDING_DELETION_MARK in str(url):
        return "pending_deletion"
    if status in _STATUS_CODES:
        return _STATUS_CODES[status][0]
    if 500 <= (status or 0) < 600:
        return "server_error"
    if not status:
        return "connection_refused"
    return ""


def _status_info(status: int, url: str = "", code: str = ""):
    """The catalog ErrorInfo for an HTTP status (None if unavailable)."""
    code = code or _code_for(status, url)
    cat = _catalog()
    if not code or cat is None:
        return None
    try:
        return cat.by_code(code)
    except Exception:                    # noqa: BLE001
        return None


def _explain(status: int, url: str, code: str = "") -> str:
    """
    One short line for an HTTP failure: what happened — what to do.

    `code` overrides the status->catalog mapping for transport failures
    ("timeout", "dns", "connection_refused"). Never multi-line, never carries
    the token or the query string; a catalog "<gitlab-host>" placeholder is
    filled with the real host.
    """
    host = _host(url)
    if status == 404 and _PENDING_DELETION_MARK in str(url):
        lead = "Scheduled for deletion on the server (GitLab delayed deletion)"
    elif status in _STATUS_CODES:
        lead = _STATUS_CODES[status][1]
    elif 500 <= (status or 0) < 600:
        lead = f"Server-side error (HTTP {status})"
    elif not status:
        what = {"timeout": f"Timed out after {TIMEOUT}s",
                "dns": "Host name did not resolve"}.get(code, "No connection")
        lead = what + (f" ({host})" if host else "") + " — VPN/DNS/timeout"
    else:
        lead = f"HTTP {status}"

    if status == 401:
        # the in-app path is the fix the user can act on right now
        fix = "re-enter it: gitlab-cli → Instances → e → re-enter token"
    else:
        info = _status_info(status, url, code)
        fix = (getattr(info, "fix", "") or "") if info else ""
        if not fix:
            fix = _FALLBACK_FIX.get(code or _code_for(status, url), "")
        if host:
            scheme = str(url).split("://", 1)[0] if "://" in str(url) else "https"
            fix = (fix.replace("https://<gitlab-host>", f"{scheme}://{host}")
                      .replace("<gitlab-host>", host))
    line = f"{lead} — {fix.rstrip('.')}" if fix else lead
    return " ".join(line.split())


def request(url: str, headers: dict, params: dict = None,
            quiet: bool = False, record: bool = True) -> requests.Response | None:
    """
    Single GET request. Returns None on a TRANSPORT error (timeout / connection
    refused); an HTTP error status comes back as a real response so the caller
    can tell 404 apart from 500.

    record=True (DEFAULT): every transport failure and every >=400 status is
    recorded in the failure ledger regardless of `quiet`, so a quiet caller
    still cannot lose the fact that something went wrong.

    record=False: for EXPECTED failures only (an orphan probe whose 404 is the
    answer, not an error). Nothing is appended to the ledger and nothing is
    printed for a failure; the return value is exactly the same — the caller
    still receives the response / None and is then responsible for reporting
    anything that was not the expected outcome.
    """
    def _fail(status: int, message: str) -> None:
        if record:
            record_failure(status, message, url, quiet=quiet)

    try:
        res = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
    except requests.Timeout:
        _fail(0, _explain(0, url, "timeout"))
        return None
    except requests.ConnectionError as exc:
        text = str(exc).lower()
        dns = any(s in text for s in ("name or service not known",
                                      "failed to resolve", "nodename nor",
                                      "temporary failure in name resolution",
                                      "getaddrinfo"))
        _fail(0, _explain(0, url, "dns" if dns else "connection_refused"))
        return None

    if res.status_code >= 400:
        _fail(res.status_code, _explain(res.status_code, url))
    return res


def _total_header(res) -> "int | None":
    """The server's own record count for this query, when it sends one."""
    try:
        raw = (res.headers.get("X-Total") or "").strip()
    except Exception:
        return None
    return int(raw) if raw.isdigit() else None


def get_paginated(url: str, headers: dict, params: dict = None,
                  quiet: bool = False, max_pages: int = 200,
                  strict: bool = True) -> PagedResult:
    """
    Fetch every page.

    strict=True  (DEFAULT) — any failure (transport error, non-200, unparseable
                 body, hitting the max_pages ceiling, or ending up with fewer
                 records than the server's X-Total says exist) raises ApiError.
                 A short list is never returned as if it were the whole set.
    strict=False — returns a PagedResult; the caller MUST check `.complete`
                 before trusting it. Used only where a partial answer is
                 genuinely acceptable and is handled explicitly.

    `quiet` suppresses the console line only. The failure is recorded either
    way, and under strict=True it still raises.

    A successful fetch that finds nothing returns an EMPTY, complete
    PagedResult — which is how a caller tells "no projects here" apart from
    "the request failed".
    """
    p = {**(params or {})}
    p.setdefault("per_page", 100)
    p["page"] = p.get("page", 1)
    per_page = int(p["per_page"])
    started_at_first_page = int(p["page"]) == 1

    results, pages = [], 0
    err: ApiError | None = None
    server_total: int | None = None

    while pages < max_pages:
        res = None
        for attempt in range(_RETRIES + 1):
            res = request(url, headers, p, quiet=quiet)
            transient = (res is None) or (res.status_code in _RETRY_STATUSES)
            if not transient or attempt == _RETRIES:
                break
            time.sleep(_RETRY_BACKOFF * (attempt + 1))

        if res is None:
            err = ApiError(0, "request failed (timeout or connection error) "
                              f"on page {p['page']}", url)
            break
        if res.status_code != 200:
            err = ApiError(res.status_code,
                           f"{_explain(res.status_code, url)} on page {p['page']}",
                           url)
            break

        if pages == 0:
            server_total = _total_header(res)

        try:
            data = res.json()
        except ValueError:
            err = ApiError(res.status_code,
                           f"unparseable JSON body on page {p['page']}", url)
            break
        if not isinstance(data, list):
            err = ApiError(res.status_code,
                           f"expected a JSON list on page {p['page']}, "
                           f"got {type(data).__name__}", url)
            break

        if not data:
            break                      # clean end of data

        results.extend(data)
        pages += 1

        # If GitLab sent a usable next-page header, use it.
        nxt = res.headers.get("X-Next-Page", "").strip()
        if nxt.isdigit():
            p["page"] = int(nxt)
        elif nxt:
            # Non-numeric cursor (keyset pagination / a proxy rewriting it):
            # we cannot follow it, so refuse to claim the set is complete.
            err = ApiError(res.status_code,
                           f"unsupported non-numeric X-Next-Page value "
                           f"after page {p['page']}", url)
            break
        elif len(data) < per_page:
            break                      # short page, header empty -> finished
        else:
            p["page"] += 1
    else:
        err = ApiError(0, f"stopped at the max_pages ceiling ({max_pages}); "
                          "the result is truncated", url)

    # ── Cross-check against the server's own count ────────────────────────────
    # We believe we walked every page. GitLab told us on page 1 how many records
    # the query matches. If we hold fewer, something truncated the walk in a way
    # the loop above could not see — a proxy that dropped a page, a rewritten
    # X-Next-Page, a rate limiter returning an empty page. That is the founding
    # defect's exact shape, so it is an incomplete fetch, not a result.
    #
    # Only a SHORTFALL is an error. Holding more than X-Total is normal on a
    # live instance (a project created between page 1 and page 3) and is not a
    # loss of data. The check is skipped when the caller started mid-set, since
    # then a shortfall is expected by construction.
    if err is None and server_total is not None and started_at_first_page \
            and len(results) < server_total:
        err = ApiError(0,
                       f"truncated: the server reports {server_total} record(s) "
                       f"for this query but only {len(results)} were received",
                       url)

    if err is not None:
        record_failure(err.status,
                       f"Incomplete fetch: {err.message} "
                       f"— got {len(results)} item(s) so far, NOT the full set",
                       url, quiet=quiet)
        if strict:
            raise err
        return PagedResult(results, complete=False, error=err, pages=pages,
                           total=server_total)

    return PagedResult(results, complete=True, error=None, pages=pages,
                       total=server_total)


# ─── Groups ────────────────────────────────────────────────────────────────────

def get_group(gitlab_url: str, headers: dict, group_path: str) -> dict | None:
    """
    One group, or None when it genuinely does not exist (HTTP 404).

    Anything else — a dead socket, an invalid token, a 403, a 500 — raises
    ApiError. Returning None for those made "your token expired" render as
    "Group 'x' not found.", which is the same lie the pagination bug told.
    """
    encoded = requests.utils.quote(str(group_path), safe="")
    url = f"{gitlab_url}/api/v4/groups/{encoded}"
    res = request(url, headers, quiet=True)
    if res is None:
        raise record_failure(
            0, f"Group '{group_path}': connection failed — cannot tell whether "
               "it exists", url, quiet=True)
    if res.status_code == 200:
        try:
            return res.json()
        except ValueError as exc:
            raise record_failure(
                res.status_code,
                f"Group '{group_path}': unparseable JSON body", url, quiet=True) from exc
    if res.status_code == 404:
        print_error(f"Group '{group_path}': {_explain(404, '')}")
        return None
    raise record_failure(
        res.status_code,
        f"Group '{group_path}': {_explain(res.status_code, '')} — cannot tell "
        "whether it exists", url, quiet=True)


def get_top_level_groups(gitlab_url: str, headers: dict,
                         min_access_level: int = None) -> PagedResult:
    """
    Top-level groups.
    - Admin: sees everything.
    - Regular user: only groups they belong to (GitLab filters this itself).
    - min_access_level: only groups where your access level is >= this.

    Raises ApiError if the listing could not be completed — an empty list from
    here means "you can see no top-level groups", never "the request failed".
    Returns a PagedResult so `.complete` survives the sort.
    """
    params = {"top_level_only": "true"}
    if min_access_level:
        params["min_access_level"] = min_access_level

    groups = get_paginated(f"{gitlab_url}/api/v4/groups", headers, params)
    return PagedResult.like(
        sorted(groups, key=lambda g: g.get("full_path", "")), groups)


def get_all_groups(gitlab_url: str, headers: dict,
                   min_access_level: int = None) -> PagedResult:
    """Every group, subgroups included — used for search. Raises ApiError."""
    params = {}
    if min_access_level:
        params["min_access_level"] = min_access_level
    groups = get_paginated(f"{gitlab_url}/api/v4/groups", headers, params)
    return PagedResult.like(
        sorted(groups, key=lambda g: g.get("full_path", "")), groups)


def get_subgroups(gitlab_url: str, headers: dict, group_id: int) -> PagedResult:
    """
    Direct subgroups of a group.

    NOT quiet and NOT tolerant: a failed subgroup listing used to read as
    "this group has no subgroups", which silently dropped the group's entire
    subtree from a clone. It now prints and raises ApiError.
    """
    return get_paginated(f"{gitlab_url}/api/v4/groups/{group_id}/subgroups",
                         headers)


# ─── Projects ──────────────────────────────────────────────────────────────────

# Query params are sent as the lowercase strings GitLab documents ("true" /
# "false"). Python bools are encoded by requests as "True"/"False"; this
# GitLab happens to accept them, but that is server leniency, not a contract.

# ARCHIVED VISIBILITY.
# Every project listing used to hardcode archived="false", which does not mean
# "prefer active projects" — it means the archived ones do not exist as far as
# this tool is concerned. They were absent from the project list, therefore
# absent from the clone set AND from coverage's denominator, so a group missing
# five archived repositories still printed ✅, and six archived repositories
# already on disk were counted in neither half. A mirror that silently refuses
# to mirror archived projects drops exactly the history nobody else is keeping.
#
# Measured on two real instances: one holds archived projects, the other
# none. The old default was therefore harmless on ONE of them — as a property of
# that instance's data, never of the code. That is precisely the kind of
# accident that hides a defect until the day the data changes.
#
# `archived` is now a tri-state the caller chooses, and the DEFAULT is "show
# everything", because the default behaviour of a mirroring tool must be to see
# what is there:
#
#     archived=None / "all"    -> param omitted. GitLab returns archived AND
#                                 active projects. THE DEFAULT.
#     archived=False/"active"  -> archived="false". Active projects only
#                                 (the old, lossy behaviour — now opt-in).
#     archived=True / "only"   -> archived="true". ONLY archived projects,
#                                 for auditing what has been decommissioned.
#
# The value is always encoded as a lowercase string; a Python bool is never put
# on the wire. Whichever branch is taken, the returned PagedResult carries a
# `.note` naming the filter, so a caller that prints a count can disclose what
# that count excludes instead of implying it is everything.

_ARCHIVED_ALIASES = {
    # -> None (omit the parameter: everything)
    "all": None, "any": None, "both": None, "include": None, "": None,
    # -> "false" (active only)
    "active": "false", "false": "false", "no": "false", "exclude": "false",
    "off": "false",
    # -> "true" (archived only)
    "true": "true", "only": "true", "archived": "true", "yes": "true",
    "on": "true",
}


def archived_param(archived=None) -> dict:
    """
    The `archived` query parameter for one listing call, as a dict to merge.

    Returns {} when everything should be listed (the parameter is omitted),
    {"archived": "false"} for active-only, {"archived": "true"} for
    archived-only. Never returns a Python bool.

    Raises ValueError on an unknown value rather than silently degrading to a
    filter nobody asked for — a wrong guess here is another silent under-count.
    """
    if archived is None:
        return {}
    if isinstance(archived, bool):
        return {"archived": "true" if archived else "false"}
    key = str(archived).strip().lower()
    if key not in _ARCHIVED_ALIASES:
        raise ValueError(
            f"archived={archived!r} is not one of "
            f"{sorted(k for k in _ARCHIVED_ALIASES if k)} (or None/True/False)")
    value = _ARCHIVED_ALIASES[key]
    return {} if value is None else {"archived": value}


def archived_disclosure(archived=None) -> str:
    """
    A one-line, human-readable statement of what an `archived` choice excludes.

    An unfiltered listing says so too: "includes archived projects" is the
    difference between a count a reader can trust and a count that merely looks
    complete. Never returns "" for a filtered listing.
    """
    param = archived_param(archived)
    value = param.get("archived")
    if value is None:
        return "includes archived projects"
    if value == "false":
        return "ACTIVE PROJECTS ONLY — archived projects are EXCLUDED from this count"
    return "ARCHIVED PROJECTS ONLY — active projects are EXCLUDED from this count"


def get_projects(gitlab_url: str, headers: dict, group_id: int,
                 min_access_level: int = None, archived=None) -> PagedResult:
    """
    Projects directly owned by a group.

    archived: see archived_param(). Default None = archived projects are
    INCLUDED, because a project you cannot see is a project you cannot clone.
    The result's `.note` states the filter in words.

    Raises ApiError if the listing could not be completed, so an empty result
    unambiguously means "this group owns no projects".
    """
    params = {"with_shared": "false", **archived_param(archived)}
    if min_access_level:
        params["min_access_level"] = min_access_level
    out = get_paginated(f"{gitlab_url}/api/v4/groups/{group_id}/projects",
                        headers, params)
    return PagedResult.like(out, out, note=archived_disclosure(archived))


# The recursive-listing trap, measured on real groups whose projects all live
# in subgroups (for example):
#
#   GET /groups/<id>/projects                     -> 0 projects
#   GET /groups/<id>/projects?include_subgroups=true -> 20 projects
#
# A group whose projects all live in SUBGROUPS owns none directly, so the flat
# listing is an EMPTY LIST — a perfectly well-formed, HTTP 200, complete,
# truthful answer to the wrong question. No error handling can catch this,
# because nothing failed. The only defence is that the flag is never dropped,
# which tests/test_w9_9_api_honesty.py asserts on the wire params.
_INCLUDE_SUBGROUPS = "include_subgroups"


def get_group_projects_recursive(gitlab_url: str, headers: dict, group_id: int,
                                 min_access_level: int = None,
                                 archived=None) -> PagedResult:
    """
    Every project in a group plus all of its subgroups in a SINGLE API call.
    Much faster than walking the subgroup tree.

    This is coverage.remote_projects()'s source, i.e. the DENOMINATOR of the
    ✅/◐/❌ icon. Filtering archived projects out here is what let a group with
    five missing archived repositories report itself complete, so the default
    (archived=None) lists them.

    `include_subgroups=true` is NOT optional: without it this endpoint returns
    an empty list for any group that keeps its projects in subgroups, and an
    empty denominator renders as "complete". The flag is asserted, not assumed.

    Raises ApiError if the listing could not be completed.
    """
    params = {_INCLUDE_SUBGROUPS: "true", "with_shared": "false",
              **archived_param(archived)}
    if min_access_level:
        params["min_access_level"] = min_access_level
    # Belt and braces: a future edit that drops or falsifies the flag turns a
    # populated group into a silent zero. Fail here instead of on the screen.
    if params.get(_INCLUDE_SUBGROUPS) != "true":
        raise ApiError(0, "internal: include_subgroups must be 'true' for a "
                          "recursive project listing")
    out = get_paginated(f"{gitlab_url}/api/v4/groups/{group_id}/projects",
                        headers, params)
    return PagedResult.like(
        out, out,
        note=f"{archived_disclosure(archived)}; includes subgroup projects")


# NOTE (unused — do not build on this).
# get_my_projects() has no caller anywhere in this repo; its only occurrence is
# this definition. It is also the WRONG basis for "projects outside any group I
# can clone": membership=true returns only projects the token is a *member* of
# (24 here) and it EXCLUDES the one project living in a user namespace, which
# is precisely the case it looks like it solves. The correct basis for that is
# GET /projects?simple=true filtered on namespace.kind == "user", and it must be
# opt-in (an admin token would otherwise sweep up every user's private repos).
# That feature is owned elsewhere; this function is kept only so nothing that
# imports it breaks.
def get_my_projects(gitlab_url: str, headers: dict,
                    min_access_level: int = None, archived=None) -> PagedResult:
    """
    UNUSED. Every project the user is a member of, regardless of group.
    See the NOTE above before using this for anything.
    """
    params = {"membership": "true", "simple": "true",
              **archived_param(archived)}
    if min_access_level:
        params["min_access_level"] = min_access_level
    projects = get_paginated(f"{gitlab_url}/api/v4/projects", headers, params)
    return PagedResult.like(
        sorted(projects, key=lambda p: p.get("path_with_namespace", "")),
        projects,
        note=f"{archived_disclosure(archived)}; membership=true — only projects "
             "the token owner is a MEMBER of")


# ─── Projects outside every group (USER namespaces) ────────────────────────────
#
# A project whose namespace is a USER has no group above it, so NO amount of
# group traversal can ever reach it: GET /groups/<user-ns-id> is a 404, and
# the whole clone path is built on /groups/**. On the instance this was found
# on, one project sat in a user namespace — "Clone All" was structurally incapable of
# meaning all.
#
# Why not get_my_projects(): membership=true only returns projects the TOKEN
# OWNER is a member of. An admin is not a member of somebody else's personal
# project, so exactly the case this solves is the case it misses (measured:
# the user-namespace project was absent from the list).
#
# The correct basis is the unfiltered project list plus a namespace.kind
# filter. `simple=true` already carries the namespace object, so one paginated
# call answers it with no N+1 lookups.
#
# DANGER, and the reason every caller must gate this behind explicit opt-in:
# with an ADMIN token this sees EVERY user's personal namespace on the
# instance, i.e. every employee's private scratch repos. Callers pass
# scope="own" (only the token owner's namespace) unless the user has
# deliberately asked for "all".

# Namespaces that belong to GitLab itself, never to a person. Cloning these is
# noise at best; they are excluded from both scopes.
_SYSTEM_NS_RE = re.compile(
    r"^(?:group_\d+_bot_|project_\d+_bot_|.*_bot_[0-9a-f]{6,})", re.I)
_SYSTEM_NS_EXACT = frozenset({"ghost", "support-bot", "alert-bot",
                              "gitlab-admin-bot", "gitlabduo", "automation-bot",
                              "visual-review-bot", "migration-bot"})


def is_system_namespace(full_path: str) -> bool:
    """Whether a user-namespace path is a GitLab service principal."""
    name = (full_path or "").strip().lower()
    if not name:
        return True
    return name in _SYSTEM_NS_EXACT or bool(_SYSTEM_NS_RE.match(name))


def _user_ns_projects(projects: list, note: str = "") -> PagedResult:
    """
    Keep only real user-namespace projects, sorted by path_with_namespace.

    Returns a PagedResult that inherits `projects`' completeness. Filtering a
    partial fetch produces a partial answer; returning a plain list here would
    have dropped that fact on the floor.
    """
    out = []
    for p in projects:
        ns = p.get("namespace") or {}
        if ns.get("kind") != "user":
            continue
        if is_system_namespace(ns.get("full_path") or ns.get("path") or ""):
            continue
        out.append(p)
    return PagedResult.like(
        sorted(out, key=lambda p: p.get("path_with_namespace", "")),
        projects, note=note)


def get_user_projects(gitlab_url: str, headers: dict, user_ref,
                      min_access_level: int = None,
                      archived=None) -> PagedResult:
    """
    Projects in ONE user's personal namespace (`GET /users/<id|username>/projects`).

    `user_ref` is the user id or username — NOT the namespace id: they differ
    (namespace 115 vs user 49 on the instance this was written against), and
    the namespace id 404s here.

    archived: see archived_param(); default None includes archived projects.

    Raises ApiError if the listing could not be completed.
    """
    params = {"simple": "true", **archived_param(archived)}
    if min_access_level:
        params["min_access_level"] = min_access_level
    encoded = requests.utils.quote(str(user_ref), safe="")
    projects = get_paginated(f"{gitlab_url}/api/v4/users/{encoded}/projects",
                             headers, params)
    # /users/<id>/projects can also return group projects the user contributed
    # to on some versions, so the namespace filter is applied here too.
    return _user_ns_projects(
        projects,
        note=f"{archived_disclosure(archived)}; user namespace of {user_ref!r} only")


def get_user_namespace_projects(gitlab_url: str, headers: dict,
                                min_access_level: int = None,
                                scope: str = "own",
                                owner_ref=None, archived=None) -> PagedResult:
    """
    Every project that lives in a USER namespace, i.e. outside every group.

    scope="own"  — only `owner_ref`'s personal namespace (the safe default).
                   Requires owner_ref (id or username); without it, NOTHING is
                   returned rather than guessing and over-collecting. This
                   refusal is load-bearing and must not be loosened: guessing
                   an owner with an admin token sweeps up every employee's
                   private repositories.
    scope="all"  — every user namespace the token can see. With an admin token
                   that is the entire instance. Opt-in only.
    scope off/none/false/"" — the feature is disabled; nothing is collected.

    The two "nothing" paths above are COMPLETE results (nothing was requested,
    so nothing failed) but they carry a `.note` saying they were skipped, so a
    caller can never read "no personal projects exist" out of "I did not look".

    Raises ApiError if a listing that WAS attempted could not be completed — an
    empty list from an attempted listing means "there are none", never "the
    request failed".
    """
    s = (scope or "own").strip().lower()
    if s in ("", "off", "none", "false"):
        return PagedResult(
            [], complete=True, pages=0,
            note="NOT COLLECTED: user-namespace discovery is disabled "
                 "(scope=off). This is not a statement that none exist.")
    if s == "own":
        if not owner_ref:
            return PagedResult(
                [], complete=True, pages=0,
                note="NOT COLLECTED: scope='own' needs owner_ref and none was "
                     "given; refusing to guess an owner. This is not a "
                     "statement that none exist.")
        return get_user_projects(gitlab_url, headers, owner_ref,
                                 min_access_level, archived=archived)

    params = {"simple": "true", **archived_param(archived)}
    if min_access_level:
        params["min_access_level"] = min_access_level
    projects = get_paginated(f"{gitlab_url}/api/v4/projects", headers, params)
    return _user_ns_projects(
        projects,
        note=f"{archived_disclosure(archived)}; scope=all — EVERY visible user "
             "namespace on the instance")


def is_archived(project: dict) -> bool:
    """
    Whether a project dict from any listing is archived.

    Now that archived projects are listed by default, callers that print or
    clone them should be able to say so. `simple=true` payloads carry
    `archived`, so this needs no extra request; a payload without the key is
    treated as not archived rather than guessing.
    """
    return bool((project or {}).get("archived", False))


def get_project(gitlab_url: str, headers: dict, project_ref) -> dict | None:
    """
    One project, or None when it genuinely does not exist (HTTP 404).

    Anything else raises ApiError. The caller in gitlab_cli._orphans() uses
    this as the confirming lookup before labelling a clone an orphan; a None
    that meant "your token is invalid" would mass-label every healthy clone.
    """
    encoded = requests.utils.quote(str(project_ref), safe="")
    url = f"{gitlab_url}/api/v4/projects/{encoded}"
    res = request(url, headers, quiet=True)
    if res is None:
        raise record_failure(
            0, f"Project '{project_ref}': connection failed — cannot tell "
               "whether it exists", url, quiet=True)
    if res.status_code == 200:
        try:
            return res.json()
        except ValueError as exc:
            raise record_failure(
                res.status_code,
                f"Project '{project_ref}': unparseable JSON body", url,
                quiet=True) from exc
    if res.status_code == 404:
        return None
    raise record_failure(
        res.status_code,
        f"Project '{project_ref}': {_explain(res.status_code, '')} — cannot "
        "tell whether it exists", url, quiet=True)


# ─── The user's own access ─────────────────────────────────────────────────────

def my_access_level(gitlab_url: str, headers: dict,
                    kind: str, ref) -> int | None:
    """
    The token's own access level on a group or project.
    kind: 'groups' or 'projects'. Returns 10..50.

    None means ONE thing: the payload arrived and carries no access level for
    you. A failed lookup raises ApiError. Returning None for both used to make
    "the server is down" and "you are not a member" the same answer, which is
    the founding defect applied to a permission check — the more dangerous
    direction, since the caller then under-reports your rights.
    """
    if kind not in ("groups", "projects"):
        raise ValueError(f"kind must be 'groups' or 'projects', got {kind!r}")
    encoded = requests.utils.quote(str(ref), safe="")
    url = f"{gitlab_url}/api/v4/{kind}/{encoded}"
    res = request(url, headers, {"with_custom_attributes": "false"}, quiet=True)
    if res is None:
        raise record_failure(
            0, f"Access level for {kind}/{ref}: connection failed — cannot "
               "tell what your access is", url, quiet=True)
    if res.status_code != 200:
        raise record_failure(
            res.status_code,
            f"Access level for {kind}/{ref}: {_explain(res.status_code, '')} — "
            "cannot tell what your access is", url, quiet=True)
    try:
        data = res.json()
    except ValueError as exc:
        raise record_failure(
            res.status_code,
            f"Access level for {kind}/{ref}: unparseable JSON body", url,
            quiet=True) from exc
    perms = (data or {}).get("permissions") or {}
    levels = []
    for key in ("project_access", "group_access"):
        v = perms.get(key)
        if isinstance(v, dict) and v.get("access_level"):
            levels.append(v["access_level"])
    return max(levels) if levels else None
