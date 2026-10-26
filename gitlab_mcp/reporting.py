"""Read-only GitLab reporting: who did what, where, and how much.

Every function in this module is a *query*. Nothing here issues a ``POST`` or
``PUT``, nothing writes to disk, and nothing runs a git command that can
change a repository (``local_status`` reads working trees and never fetches).
All of them return JSON-serialisable dicts or lists so the MCP server can hand
them straight to a model.

The module answers two shapes of question:

* **project-centric** — "what happened on project X, who worked how much,
  how many commits, how much code changed" (:func:`project_overview`,
  :func:`commits`, :func:`contributors`, :func:`project_progress`,
  :func:`commit_detail`, :func:`compare`).
* **person-centric** — "what did A and B do, on which projects, what did they
  build" (:func:`person_report`).

Two conventions apply throughout:

* **Dates.** Every ``since``/``until`` argument accepts ``"7d"``, ``"2w"``,
  ``"1m"``, ``"today"``, a ``"YYYY-MM-DD"`` date, or a full ISO-8601 stamp;
  :func:`resolve_since` turns all of them into a UTC ISO-8601 string.
* **Truncation is explicit.** Anything that can grow without bound (commit
  lists, diffs, file bodies) is cut at a documented cap and the result carries
  ``"truncated": true`` plus ``"total"`` where the total is knowable. A caller
  must never have to guess whether it saw everything.

Attribution is best-effort by construction. GitLab records a commit's author
as a free-text name and e-mail taken from the committer's git config, which is
not the same identity as a GitLab account. :func:`person_report` therefore
reports its matching rule and its blind spots in the ``method`` and
``limitations`` fields of its own result rather than presenting a number as if
it were exact.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence
from urllib.parse import quote

import git as gl_git
import pull as gl_pull

__all__ = [
    "MAX_DIFF_CHARS",
    "GIT_TIMEOUT_SECONDS",
    "resolve_since",
    "instances",
    "groups",
    "projects",
    "project_overview",
    "commits",
    "commit_detail",
    "compare",
    "contributors",
    "person_report",
    "project_progress",
    "merge_requests",
    "merge_request",
    "pipelines",
    "branches",
    "users",
    "file_content",
    "local_status",
]

# GitLab's maximum page size. Asking for fewer just multiplies round trips.
PER_PAGE = 100

# Default diff budget for one result. A model reading a review needs the
# hunks, not a vendored lockfile, so the budget is spent file by file and the
# remainder is reported as omitted rather than silently dropped.
MAX_DIFF_CHARS = 60_000

# A local git query that has not answered in ten seconds is hung (a network
# remote, a filesystem stall, or a credential prompt we refused to answer).
GIT_TIMEOUT_SECONDS = 10

# Resolved once at import so a repository that ships its own `git` executable
# cannot be picked up from PATH while we are walking it.
GIT_BIN = shutil.which("git") or "git"

# Suffix -> days, for the compact relative forms. A "month" here is 30 days
# and a "year" 365: these are reporting windows, not calendar arithmetic, and
# a caller who needs a real calendar boundary passes an explicit date.
_RELATIVE_UNITS = {"d": 1, "w": 7, "m": 30, "y": 365}


# ─── Dates ────────────────────────────────────────────────────────────────────

def _utc_now() -> datetime:
    """Current time, UTC, timezone-aware. Patched in tests."""
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    """ISO-8601 in UTC with a ``Z`` suffix, the form GitLab's API accepts."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_since(value: str | None, now: datetime | None = None) -> str | None:
    """Turn a human time window into an ISO-8601 UTC stamp for GitLab.

    Accepted forms:

    * ``None`` or ``""`` -> ``None`` (meaning "no lower bound").
    * ``"7d"``, ``"2w"``, ``"1m"``, ``"3y"`` -> that long ago, to the second.
    * ``"today"`` -> midnight UTC this morning.
    * ``"yesterday"`` -> midnight UTC yesterday morning.
    * ``"2026-10-04"`` -> midnight UTC on that date.
    * anything containing ``"T"`` -> passed through as an ISO stamp, after a
      parse check, so a caller can hand us an exact boundary.

    ``now`` is injectable so the relative forms are testable without patching
    the clock globally.

    Raises:
        ValueError: the string is not one of the accepted forms. Guessing
            would silently report the wrong window, which is worse than an
            error the caller can see.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    reference = now or _utc_now()
    lowered = text.lower()

    if lowered == "today":
        return _iso(reference.replace(hour=0, minute=0, second=0, microsecond=0))
    if lowered == "yesterday":
        midnight = reference.replace(hour=0, minute=0, second=0, microsecond=0)
        return _iso(midnight - timedelta(days=1))

    unit = lowered[-1]
    if unit in _RELATIVE_UNITS and lowered[:-1].isdigit():
        days = int(lowered[:-1]) * _RELATIVE_UNITS[unit]
        return _iso(reference - timedelta(days=days))

    if "T" in text or "t" in lowered[4:]:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"not an ISO-8601 timestamp: {text!r}") from exc
        return _iso(parsed)

    try:
        parsed_date = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"unrecognised time window {text!r}; "
            "use 7d, 2w, 1m, today, YYYY-MM-DD or an ISO-8601 timestamp"
        ) from exc
    return _iso(datetime(parsed_date.year, parsed_date.month, parsed_date.day,
                         tzinfo=timezone.utc))


def _as_event_date(iso_stamp: str | None) -> str | None:
    """The ``YYYY-MM-DD`` form that ``/events?after=`` wants.

    The events endpoint filters by date, not by instant, and its bounds are
    *exclusive*. :func:`person_report` records that in its limitations.
    """
    if not iso_stamp:
        return None
    return iso_stamp[:10]


def _parse_stamp(value: str | None) -> datetime | None:
    """Best-effort parse of a GitLab timestamp; ``None`` when unusable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# ─── Request plumbing ─────────────────────────────────────────────────────────

def _enc(value: Any) -> str:
    """Percent-encode a path segment, including its slashes.

    Project refs (``group-a/project-a``) and file paths both travel inside a
    single URL segment, so ``/`` has to be escaped too.
    """
    return quote(str(value), safe="")


def _page(ctx: Any, path: str, params: dict | None = None,
          limit: int | None = None) -> tuple[list, bool, int | None]:
    """Paginate, normalising the two shapes ``ctx.paginate`` may return.

    The context returns either ``(items, truncated, total)`` or a bare list
    depending on whether the endpoint reported a total. Callers here always
    want the triple.
    """
    result = ctx.paginate(path, params, limit)
    if isinstance(result, tuple) and len(result) == 3:
        items, truncated, total = result
        return list(items or []), bool(truncated), total
    items = list(result or [])
    hit_cap = limit is not None and len(items) >= limit
    return items, hit_cap, len(items)


def _optional_get(ctx: Any, path: str, params: dict | None = None,
                  notes: list[str] | None = None,
                  label: str | None = None) -> Any:
    """GET an endpoint that legitimately may not exist on this instance.

    Approvals are Premium-only (CE answers 404), ``languages`` can be absent,
    and the diffs endpoint was renamed between versions. Those are expected
    outcomes, not failures, so the reason is appended to ``notes`` and the
    caller carries on with ``None`` — nothing is swallowed silently.
    """
    try:
        return ctx.get(path, params) if params is not None else ctx.get(path)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, reason recorded
        if notes is not None:
            notes.append(f"{label or path} unavailable: {exc}")
        return None


def _project_path(ctx: Any, project: str | int) -> str:
    """``/projects/<numeric id>`` for a path or an id."""
    return f"/projects/{ctx.project_id(project)}"


# ─── Diff accounting ──────────────────────────────────────────────────────────

def _count_diff_lines(diff_text: str | None) -> tuple[int, int]:
    """Added and removed line counts for one unified-diff body.

    The ``+++``/``---`` file headers are not content lines and are excluded;
    ``\\ No newline at end of file`` markers start with a backslash and are
    excluded by construction.
    """
    additions = deletions = 0
    for line in (diff_text or "").splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return additions, deletions


def _diff_entries(payload: Any) -> tuple[list[dict], bool]:
    """Normalise every diff-carrying response shape to ``(entries, overflow)``.

    Measured on GitLab 17.5 and 19.1 (``tests/fixtures/SHAPES.md``):

    * ``/merge_requests/:iid/diffs`` and ``/repository/commits/:sha/diff``
      answer a **bare list** of diff objects;
    * ``/merge_requests/:iid/changes`` answers a **dict** — the whole MR with a
      ``changes`` list (and possibly ``overflow``) attached.

    Anything else (``None``, a scalar) is treated as "no entries" rather than
    crashing the report.
    """
    if isinstance(payload, list):
        return [e for e in payload if isinstance(e, dict)], False
    if isinstance(payload, dict):
        changes = payload.get("changes")
        if changes is None:
            changes = payload.get("diffs")
        entries = [e for e in (changes or []) if isinstance(e, dict)]
        return entries, bool(payload.get("overflow"))
    return [], False


def _pack_diffs(entries: Iterable[dict], max_diff_chars: int) -> tuple[list[dict], bool]:
    """Describe each changed file, spending a shared diff-text budget in order.

    Line counts are computed for *every* file even when its diff text is
    omitted, so "how much code changed" stays accurate after truncation. The
    second element of the result is ``True`` when at least one diff body was
    shortened or dropped — by our budget *or* by GitLab itself.

    GitLab 19.x adds ``too_large`` and ``collapsed`` to each diff object.
    ``too_large`` means GitLab sent an **empty** ``diff`` string: that file is
    reported with ``too_large: true`` and ``additions``/``deletions`` of
    ``None`` (unknown), never as an unchanged file with zero lines.
    ``collapsed`` means the body may be cut short, so its counts are flagged
    as a lower bound with ``line_counts_exact: false``. Both keys are read
    with a ``False`` default, so 17.x payloads (which lack them) still work.
    """
    files: list[dict] = []
    remaining = max(0, int(max_diff_chars))
    truncated = False

    for entry in entries or []:
        diff_text = entry.get("diff") or ""
        too_large = bool(entry.get("too_large", False))
        collapsed = bool(entry.get("collapsed", False))
        server_omitted = (too_large or collapsed) and not diff_text
        if server_omitted:
            additions: int | None = None
            deletions: int | None = None
        else:
            additions, deletions = _count_diff_lines(diff_text)
        record = {
            "path": entry.get("new_path") or entry.get("old_path"),
            "old_path": entry.get("old_path"),
            "new_file": bool(entry.get("new_file")),
            "deleted_file": bool(entry.get("deleted_file")),
            "renamed_file": bool(entry.get("renamed_file")),
            "additions": additions,
            "deletions": deletions,
        }
        if too_large or collapsed:
            record["too_large"] = too_large
            record["collapsed"] = collapsed
            record["line_counts_exact"] = False
        if server_omitted:
            record["diff"] = ""
            record["diff_truncated"] = True
            record["diff_omitted_reason"] = (
                "too_large: GitLab did not send this diff"
                if too_large else
                "collapsed: GitLab did not send this diff")
            truncated = True
        elif not diff_text:
            record["diff"] = ""
            record["diff_truncated"] = False
        elif remaining >= len(diff_text):
            record["diff"] = diff_text
            record["diff_truncated"] = False
            remaining -= len(diff_text)
        else:
            record["diff"] = diff_text[:remaining]
            record["diff_truncated"] = True
            record["diff_omitted_chars"] = len(diff_text) - remaining
            remaining = 0
            truncated = True
        files.append(record)

    return files, truncated


def _diff_totals(files: Sequence[dict]) -> dict:
    """Roll per-file line counts into one additions/deletions/files summary.

    When any file's counts are unknown (GitLab sent it as ``too_large``) or
    only a lower bound (``collapsed``), the summary says so with
    ``line_counts_exact: false`` and ``files_without_line_counts``; the sums
    then cover only the files whose bodies were received.
    """
    additions = sum(int(f.get("additions") or 0) for f in files)
    deletions = sum(int(f.get("deletions") or 0) for f in files)
    totals: dict[str, Any] = {
        "files_changed": len(files),
        "additions": additions,
        "deletions": deletions,
        "total": additions + deletions,
    }
    unknown = sum(1 for f in files if f.get("additions") is None
                  and "additions" in f)
    inexact = sum(1 for f in files if f.get("line_counts_exact") is False)
    if unknown or inexact:
        totals["line_counts_exact"] = False
        totals["files_without_line_counts"] = unknown
        totals["files_too_large"] = sum(1 for f in files if f.get("too_large"))
    return totals


def _note_server_omitted(files: Sequence[dict], notes: list[str]) -> None:
    """Explain, once per report, files GitLab itself declined to send."""
    too_large = [f["path"] for f in files if f.get("too_large")]
    collapsed = [f["path"] for f in files
                 if f.get("collapsed") and not f.get("too_large")]
    if too_large:
        notes.append(
            f"{len(too_large)} file(s) were too_large: GitLab sent no diff body, "
            "so their line counts are unknown (null), not zero: "
            + ", ".join(str(p) for p in too_large[:10]))
    if collapsed:
        notes.append(
            f"{len(collapsed)} file(s) were collapsed by GitLab; their diff and "
            "line counts may be incomplete: "
            + ", ".join(str(p) for p in collapsed[:10]))


# ─── Commit rows ──────────────────────────────────────────────────────────────

def _commit_row(raw: dict) -> dict:
    """One commit, flattened, with its stats hoisted to the top level."""
    stats = raw.get("stats") or {}
    parents = raw.get("parent_ids") or []
    return {
        "sha": raw.get("id"),
        "is_merge": isinstance(parents, list) and len(parents) >= 2,
        "short_id": raw.get("short_id"),
        "title": raw.get("title"),
        "author_name": raw.get("author_name"),
        "author_email": raw.get("author_email"),
        "committed_date": raw.get("committed_date") or raw.get("created_at"),
        "created_at": raw.get("created_at"),
        "web_url": raw.get("web_url"),
        "additions": int(stats.get("additions") or 0),
        "deletions": int(stats.get("deletions") or 0),
        "total": int(stats.get("total") or 0),
    }


def _fetch_commit_rows(ctx: Any, pid_path: str, since: str | None,
                       until: str | None, branch: str | None,
                       author: str | None, with_stats: bool,
                       limit: int) -> tuple[list[dict], bool, int | None]:
    """The raw commit query every commit-shaped report is built on."""
    params: dict[str, Any] = {"per_page": PER_PAGE}
    if with_stats:
        params["with_stats"] = "true"
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    if branch:
        params["ref_name"] = branch
    if author:
        params["author"] = author
    raw, truncated, total = _page(ctx, f"{pid_path}/repository/commits", params, limit)
    return [_commit_row(c) for c in raw], truncated, total


def _authored_work(rows: Iterable[dict]) -> list[dict]:
    """Rows that represent authored work: merge commits removed.

    A merge commit's stats repeat the lines of the branch it merged, so
    counting it would double-count that work (``parent_ids`` >= 2, measured on
    GitLab 19.1 in ``tests/fixtures``).
    """
    return [r for r in rows if not r.get("is_merge")]


def _stamp_key(stamp: str | None) -> tuple[int, Any]:
    """Sort key for a GitLab timestamp that respects its UTC offset.

    GitLab returns commit dates in the committer's own offset
    (``+03:30``, ``+01:00``, ``+00:00`` in one list), so comparing the raw
    strings orders them wrongly. Unparseable stamps sort by their text, after
    every parseable one.
    """
    parsed = _parse_stamp(stamp)
    if parsed is None:
        return 1, str(stamp)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return 0, parsed.astimezone(timezone.utc)


def _aggregate_authors(rows: Iterable[dict]) -> list[dict]:
    """Per-author commit count, lines touched and first/last activity.

    Grouped by e-mail (the stable key in git) and labelled with the most
    recent name seen for that e-mail. Sorted by commit count, descending.
    Merge commits are counted separately in ``merge_commits`` and contribute
    neither to ``commits`` nor to the line totals (see :func:`_authored_work`).
    """
    buckets: dict[str, dict] = {}
    for row in rows:
        key = (row.get("author_email") or row.get("author_name") or "unknown").lower()
        bucket = buckets.setdefault(key, {
            "name": row.get("author_name"),
            "email": row.get("author_email"),
            "commits": 0,
            "merge_commits": 0,
            "additions": 0,
            "deletions": 0,
            "first_date": None,
            "last_date": None,
        })
        if row.get("is_merge"):
            bucket["merge_commits"] += 1
        else:
            bucket["commits"] += 1
            bucket["additions"] += row.get("additions", 0)
            bucket["deletions"] += row.get("deletions", 0)
        stamp = row.get("committed_date")
        if stamp:
            key_now = _stamp_key(stamp)
            if (bucket["first_date"] is None
                    or key_now < _stamp_key(bucket["first_date"])):
                bucket["first_date"] = stamp
            if (bucket["last_date"] is None
                    or key_now > _stamp_key(bucket["last_date"])):
                bucket["last_date"] = stamp
                bucket["name"] = row.get("author_name") or bucket["name"]
    ordered = sorted(buckets.values(),
                     key=lambda b: (-b["commits"], (b["email"] or "")))
    return ordered


# ─── Catalogue ────────────────────────────────────────────────────────────────

def instances() -> list[dict]:
    """Configured gitlab-cli instances: name, URL, and which one is active.

    Never includes a token. Imported lazily so this module is usable (and
    testable) without the context module present.
    """
    from .context import list_instance_names

    return list(list_instance_names())


def groups(ctx: Any, search: str | None = None, limit: int = 100) -> list[dict]:
    """Groups visible to the token, newest-ancestor-first as GitLab orders them."""
    params: dict[str, Any] = {"per_page": PER_PAGE, "all_available": "true"}
    if search:
        params["search"] = search
    rows, _, _ = _page(ctx, "/groups", params, limit)
    return [{
        "id": g.get("id"),
        "name": g.get("name"),
        "full_path": g.get("full_path"),
        "description": g.get("description"),
        "visibility": g.get("visibility"),
        "web_url": g.get("web_url"),
        "parent_id": g.get("parent_id"),
    } for g in rows]


def projects(ctx: Any, group: str | None = None, search: str | None = None,
             membership: bool | None = None, limit: int = 200) -> list[dict]:
    """Projects, either inside one group (including subgroups) or instance-wide.

    ``membership=True`` restricts the instance-wide listing to projects the
    token's user belongs to, which is usually what "my projects" means.
    """
    params: dict[str, Any] = {"per_page": PER_PAGE, "order_by": "last_activity_at"}
    if search:
        params["search"] = search
    if group:
        params["include_subgroups"] = "true"
        path = f"/groups/{_enc(group)}/projects"
    else:
        path = "/projects"
        if membership:
            params["membership"] = "true"
    rows, _, _ = _page(ctx, path, params, limit)
    return [{
        "id": p.get("id"),
        "name": p.get("name"),
        "path_with_namespace": p.get("path_with_namespace"),
        "description": p.get("description"),
        "default_branch": p.get("default_branch"),
        "visibility": p.get("visibility"),
        "last_activity_at": p.get("last_activity_at"),
        "archived": bool(p.get("archived")),
        "web_url": p.get("web_url"),
    } for p in rows]


def users(ctx: Any, search: str, limit: int = 20) -> list[dict]:
    """Find GitLab accounts by username, name or e-mail fragment."""
    rows, _, _ = _page(ctx, "/users", {"search": search, "per_page": PER_PAGE}, limit)
    return [{
        "id": u.get("id"),
        "username": u.get("username"),
        "name": u.get("name"),
        "state": u.get("state"),
        "public_email": u.get("public_email"),
        "web_url": u.get("web_url"),
    } for u in rows]


def branches(ctx: Any, project: str, search: str | None = None,
             limit: int = 200) -> list[dict]:
    """Branches of a project with their tip commit and protection flag."""
    params: dict[str, Any] = {"per_page": PER_PAGE}
    if search:
        params["search"] = search
    rows, _, _ = _page(ctx, f"{_project_path(ctx, project)}/repository/branches",
                       params, limit)
    return [{
        "name": b.get("name"),
        "default": bool(b.get("default")),
        "protected": bool(b.get("protected")),
        "merged": bool(b.get("merged")),
        "commit_sha": (b.get("commit") or {}).get("id"),
        "commit_title": (b.get("commit") or {}).get("title"),
        "committed_date": (b.get("commit") or {}).get("committed_date"),
    } for b in rows]


def pipelines(ctx: Any, project: str, ref: str | None = None,
              limit: int = 20) -> list[dict]:
    """Recent pipelines, newest first, optionally for one ref."""
    params: dict[str, Any] = {"per_page": min(PER_PAGE, max(1, limit))}
    if ref:
        params["ref"] = ref
    rows, _, _ = _page(ctx, f"{_project_path(ctx, project)}/pipelines", params, limit)
    return [{
        "id": p.get("id"),
        "iid": p.get("iid"),
        "status": p.get("status"),
        "ref": p.get("ref"),
        "sha": p.get("sha"),
        "source": p.get("source"),
        "created_at": p.get("created_at"),
        "updated_at": p.get("updated_at"),
        "web_url": p.get("web_url"),
    } for p in rows]


def file_content(ctx: Any, project: str, path: str, ref: str | None = None,
                 max_chars: int = 100_000) -> dict:
    """One file's text at a ref, capped at ``max_chars``.

    GitLab returns the body base64-encoded. A file that does not decode as
    UTF-8 is reported as binary with its size rather than mangled into
    replacement characters.
    """
    params = {"ref": ref} if ref else None
    blob = ctx.get(
        f"{_project_path(ctx, project)}/repository/files/{_enc(path)}", params
    ) or {}
    raw = base64.b64decode(blob.get("content") or "")
    result = {
        "path": blob.get("file_path") or path,
        "ref": blob.get("ref") or ref,
        "size": blob.get("size"),
        "blob_sha": blob.get("blob_id"),
        "last_commit_sha": blob.get("last_commit_id"),
        "binary": False,
        "truncated": False,
        "content": "",
    }
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        result["binary"] = True
        result["bytes"] = len(raw)
        return result
    if len(text) > max_chars:
        result["content"] = text[:max_chars]
        result["truncated"] = True
        result["total"] = len(text)
    else:
        result["content"] = text
        result["total"] = len(text)
    return result


# ─── Commits ──────────────────────────────────────────────────────────────────

def commits(ctx: Any, project: str, since: str = "7d", until: str | None = None,
            author: str | None = None, branch: str | None = None,
            with_stats: bool = True, limit: int = 200) -> dict:
    """Commits in a window, with how much code each one changed.

    ``with_stats`` costs GitLab extra work per commit but is what makes
    "how much code was added" answerable; leave it on unless only titles are
    needed.
    """
    pid_path = _project_path(ctx, project)
    since_iso = resolve_since(since)
    until_iso = resolve_since(until)
    rows, truncated, total = _fetch_commit_rows(
        ctx, pid_path, since_iso, until_iso, branch, author, with_stats, limit
    )
    summary = {
        "commits": len(rows),
        "merge_commits": sum(1 for r in rows if r.get("is_merge")),
        "additions": sum(r["additions"] for r in _authored_work(rows)),
        "deletions": sum(r["deletions"] for r in _authored_work(rows)),
    }
    summary["total"] = summary["additions"] + summary["deletions"]
    return {
        "project": str(project),
        "since": since_iso,
        "until": until_iso,
        "branch": branch,
        "author_filter": author,
        "with_stats": bool(with_stats),
        "summary": summary,
        "rows": rows,
        "truncated": truncated,
        "total": total,
    }


def commit_detail(ctx: Any, project: str, sha: str,
                  max_diff_chars: int = MAX_DIFF_CHARS) -> dict:
    """One commit with its message, stats and per-file diff.

    The diff bodies share a ``max_diff_chars`` budget spent in GitLab's file
    order; per-file line counts are exact regardless of truncation.
    """
    pid_path = _project_path(ctx, project)
    notes: list[str] = []
    raw = ctx.get(f"{pid_path}/repository/commits/{_enc(sha)}") or {}
    payload = _optional_get(ctx, f"{pid_path}/repository/commits/{_enc(sha)}/diff",
                            notes=notes, label="commit diff")
    diffs, _overflow = _diff_entries(payload)
    files, truncated = _pack_diffs(diffs, max_diff_chars)
    _note_server_omitted(files, notes)
    return {
        "project": str(project),
        "sha": raw.get("id") or sha,
        "short_id": raw.get("short_id"),
        "title": raw.get("title"),
        "message": raw.get("message"),
        "author_name": raw.get("author_name"),
        "author_email": raw.get("author_email"),
        "committed_date": raw.get("committed_date"),
        "parent_ids": raw.get("parent_ids") or [],
        "web_url": raw.get("web_url"),
        "stats": raw.get("stats") or {},
        "diff_summary": _diff_totals(files),
        "files": files,
        "truncated": truncated,
        "total": len(diffs),
        "notes": notes,
    }


def compare(ctx: Any, project: str, base: str, head: str,
            max_diff_chars: int = MAX_DIFF_CHARS) -> dict:
    """What changed between two refs — "what did this branch add".

    ``base``/``head`` are passed as query parameters, never as path segments,
    so a ref containing ``/`` needs no special handling.
    """
    pid_path = _project_path(ctx, project)
    payload = ctx.get(f"{pid_path}/repository/compare",
                      {"from": base, "to": head}) or {}
    entries, _overflow = _diff_entries(payload.get("diffs") or [])
    files, truncated = _pack_diffs(entries, max_diff_chars)
    commit_rows = [_commit_row(c) for c in (payload.get("commits") or [])]
    return {
        "project": str(project),
        "base": base,
        "head": head,
        "same_ref": bool(payload.get("compare_same_ref")),
        "timed_out": bool(payload.get("compare_timeout")),
        "commits": commit_rows,
        "commit_count": len(commit_rows),
        "diff_summary": _diff_totals(files),
        "files": files,
        "truncated": truncated,
        "total": len(entries),
    }


def contributors(ctx: Any, project: str, since: str = "30d",
                 until: str | None = None, branch: str | None = None,
                 limit: int = 1000) -> dict:
    """Who committed to a project in a window, and how much.

    Computed from the commit list with stats rather than from GitLab's
    ``/contributors`` endpoint, because that endpoint ignores date windows and
    so cannot answer "in the last month".
    """
    pid_path = _project_path(ctx, project)
    since_iso = resolve_since(since)
    until_iso = resolve_since(until)
    rows, truncated, total = _fetch_commit_rows(
        ctx, pid_path, since_iso, until_iso, branch, None, True, limit
    )
    authors = _aggregate_authors(rows)
    return {
        "project": str(project),
        "since": since_iso,
        "until": until_iso,
        "branch": branch,
        "authors": authors,
        "author_count": len(authors),
        "commits_considered": len(rows),
        "merge_commits_excluded": sum(1 for r in rows if r.get("is_merge")),
        "additions": sum(a["additions"] for a in authors),
        "deletions": sum(a["deletions"] for a in authors),
        "truncated": truncated,
        "total": total,
    }


# ─── Project-level reports ────────────────────────────────────────────────────

def project_overview(ctx: Any, project: str, since: str = "30d",
                     top: int = 10) -> dict:
    """One-screen answer to "what is the state of project X".

    Default branch, last activity, the commit and line volume in the window,
    the top contributors, open merge requests and the language mix.
    """
    pid = ctx.project_id(project)
    pid_path = f"/projects/{pid}"
    notes: list[str] = []
    since_iso = resolve_since(since)

    proj = ctx.get(pid_path) or {}
    rows, truncated, _ = _fetch_commit_rows(
        ctx, pid_path, since_iso, None, None, None, True, 1000
    )
    authors = _aggregate_authors(rows)
    open_mrs, mrs_truncated, mrs_total = _page(
        ctx, f"{pid_path}/merge_requests",
        {"state": "opened", "per_page": PER_PAGE}, 200,
    )
    languages = _optional_get(ctx, f"{pid_path}/languages",
                              notes=notes, label="languages") or {}

    return {
        "project": proj.get("path_with_namespace") or str(project),
        "id": proj.get("id") or pid,
        "name": proj.get("name"),
        "description": proj.get("description"),
        "default_branch": proj.get("default_branch"),
        "visibility": proj.get("visibility"),
        "archived": bool(proj.get("archived")),
        "last_activity_at": proj.get("last_activity_at"),
        "web_url": proj.get("web_url"),
        "since": since_iso,
        "activity": {
            "commits": len(_authored_work(rows)),
            "merge_commits": sum(1 for r in rows if r.get("is_merge")),
            "additions": sum(r["additions"] for r in _authored_work(rows)),
            "deletions": sum(r["deletions"] for r in _authored_work(rows)),
            "commits_truncated": truncated,
        },
        "top_contributors": authors[:max(0, top)],
        "contributor_count": len(authors),
        "open_merge_requests": len(open_mrs),
        "open_merge_requests_truncated": mrs_truncated,
        "open_merge_requests_total": mrs_total,
        "languages": languages,
        "notes": notes,
    }


def _iso_week(stamp: str | None) -> str | None:
    """``2026-W41`` for a GitLab timestamp, or ``None`` when unparseable."""
    moment = _parse_stamp(stamp)
    if moment is None:
        return None
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def project_progress(ctx: Any, project: str, since: str = "30d",
                     until: str | None = None,
                     pipeline_sample: int = 50) -> dict:
    """Is the project moving, and in whose hands.

    Commits bucketed per ISO week and per author, merge requests merged in the
    window, merge requests still open, the success rate over the last
    ``pipeline_sample`` pipelines, and the five most recent tags.
    """
    pid_path = _project_path(ctx, project)
    since_iso = resolve_since(since)
    until_iso = resolve_since(until)
    notes: list[str] = []

    rows, truncated, _ = _fetch_commit_rows(
        ctx, pid_path, since_iso, until_iso, None, None, True, 2000
    )

    per_week: dict[str, dict] = {}
    for row in _authored_work(rows):
        week = _iso_week(row.get("committed_date"))
        if week is None:
            notes.append(f"commit {row.get('short_id')} has no usable date")
            continue
        bucket = per_week.setdefault(week, {"week": week, "commits": 0,
                                            "additions": 0, "deletions": 0})
        bucket["commits"] += 1
        bucket["additions"] += row["additions"]
        bucket["deletions"] += row["deletions"]

    merged_params: dict[str, Any] = {"state": "merged", "per_page": PER_PAGE}
    if since_iso:
        merged_params["updated_after"] = since_iso
    if until_iso:
        merged_params["updated_before"] = until_iso
    merged, _, _ = _page(ctx, f"{pid_path}/merge_requests", merged_params, 500)
    open_mrs, _, open_total = _page(
        ctx, f"{pid_path}/merge_requests",
        {"state": "opened", "per_page": PER_PAGE}, 500,
    )

    sample = max(1, int(pipeline_sample))
    recent_pipelines, _, _ = _page(
        ctx, f"{pid_path}/pipelines", {"per_page": min(PER_PAGE, sample)}, sample
    )
    finished = [p for p in recent_pipelines
                if p.get("status") in {"success", "failed", "canceled"}]
    successes = sum(1 for p in finished if p.get("status") == "success")
    success_rate = round(successes / len(finished), 4) if finished else None

    tags, _, _ = _page(ctx, f"{pid_path}/repository/tags",
                       {"order_by": "updated", "sort": "desc", "per_page": 5}, 5)

    return {
        "project": str(project),
        "since": since_iso,
        "until": until_iso,
        "commits_per_week": sorted(per_week.values(), key=lambda b: b["week"]),
        "commits_per_author": _aggregate_authors(rows),
        "commits": len(_authored_work(rows)),
        "merge_commits": sum(1 for r in rows if r.get("is_merge")),
        "commits_truncated": truncated,
        "merge_requests_merged": [{
            "iid": m.get("iid"),
            "title": m.get("title"),
            "author": (m.get("author") or {}).get("username"),
            "merged_at": m.get("merged_at"),
            "target_branch": m.get("target_branch"),
            "web_url": m.get("web_url"),
        } for m in merged],
        "merged_count": len(merged),
        "open_merge_requests": len(open_mrs),
        "open_merge_requests_total": open_total,
        "pipelines_sampled": len(recent_pipelines),
        "pipelines_finished": len(finished),
        "pipeline_success_rate": success_rate,
        "latest_tags": [{
            "name": t.get("name"),
            "message": t.get("message"),
            "target": t.get("target"),
            "committed_date": (t.get("commit") or {}).get("committed_date"),
        } for t in tags[:5]],
        "notes": notes,
    }


# ─── Merge requests ───────────────────────────────────────────────────────────

def _mr_row(raw: dict) -> dict:
    """A merge request flattened to the fields a reviewer actually scans."""
    return {
        "iid": raw.get("iid"),
        "project_id": raw.get("project_id"),
        "title": raw.get("title"),
        "state": raw.get("state"),
        "draft": bool(raw.get("draft") or raw.get("work_in_progress")),
        "author": (raw.get("author") or {}).get("username"),
        "assignees": [a.get("username") for a in (raw.get("assignees") or [])],
        "source_branch": raw.get("source_branch"),
        "target_branch": raw.get("target_branch"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "merged_at": raw.get("merged_at"),
        "closed_at": raw.get("closed_at"),
        "has_conflicts": bool(raw.get("has_conflicts")),
        "web_url": raw.get("web_url"),
    }


def merge_requests(ctx: Any, project: str | None = None, group: str | None = None,
                   state: str = "opened", author: str | None = None,
                   since: str | None = None, target_branch: str | None = None,
                   limit: int = 100) -> list[dict]:
    """Merge requests for one project, one group, or the whole instance.

    ``since`` filters on creation time. ``author`` is a username and is
    resolved to a user id, because GitLab's MR endpoint filters by
    ``author_id`` and silently ignores an unknown parameter name.
    """
    params: dict[str, Any] = {"state": state, "per_page": PER_PAGE,
                              "order_by": "updated_at", "sort": "desc"}
    since_iso = resolve_since(since)
    if since_iso:
        params["created_after"] = since_iso
    if target_branch:
        params["target_branch"] = target_branch
    if author:
        params["author_id"] = ctx.user_id(author)

    if project:
        path = f"{_project_path(ctx, project)}/merge_requests"
    elif group:
        path = f"/groups/{_enc(group)}/merge_requests"
    else:
        path = "/merge_requests"
        params["scope"] = "all"

    rows, _, _ = _page(ctx, path, params, limit)
    return [_mr_row(m) for m in rows]


def merge_request(ctx: Any, project: str, iid: int, with_diff: bool = True,
                  max_diff_chars: int = 80_000) -> dict:
    """One merge request in enough detail to review it.

    Collects the MR itself, its changed files, its discussion threads, its
    approval state and its head pipeline. Measured behaviour (GitLab CE 17.5
    and 19.1, ``tests/fixtures/SHAPES.md``): ``/diffs`` is a bare paginated
    list and ``/changes`` a dict, both accepted (``/changes`` is the fallback);
    ``/approvals`` answers 200 on CE without ``approvals_required`` or
    ``approvals_left``, which are then reported as ``None``, never 0; a 19.x
    file sent ``too_large`` has unknown (``None``) line counts. Each of these
    is recorded in ``notes`` instead of failing or misreporting.
    """
    base = f"{_project_path(ctx, project)}/merge_requests/{int(iid)}"
    notes: list[str] = []
    raw = ctx.get(base) or {}
    result = _mr_row(raw)
    result.update({
        "project": str(project),
        "description": raw.get("description"),
        "detailed_merge_status": raw.get("detailed_merge_status"),
        "merge_status": raw.get("merge_status"),
        "reviewers": [r.get("username") for r in (raw.get("reviewers") or [])],
        "labels": raw.get("labels") or [],
        "sha": raw.get("sha"),
        "diff_refs": raw.get("diff_refs") or {},
        "user_notes_count": raw.get("user_notes_count"),
    })

    if with_diff:
        entries, truncated, total = _mr_diff_entries(ctx, base, notes)
        files, diff_truncated = _pack_diffs(entries, max_diff_chars)
        _note_server_omitted(files, notes)
        result["files"] = files
        result["diff_summary"] = _diff_totals(files)
        result["truncated"] = bool(truncated or diff_truncated)
        result["total"] = total
    else:
        result["files"] = []
        result["diff_summary"] = _diff_totals([])
        result["truncated"] = False
        result["total"] = 0

    discussions = _optional_get(ctx, f"{base}/discussions",
                                notes=notes, label="discussions") or []
    result["discussions"] = _summarise_discussions(discussions)
    result["discussion_count"] = len(result["discussions"])

    approvals = _optional_get(ctx, f"{base}/approvals",
                              notes=notes, label="approvals")
    if not isinstance(approvals, dict):
        result["approvals"] = None
        notes.append("approvals are a GitLab Premium feature; "
                     "Community Edition does not expose them")
    else:
        result["approvals"] = _approval_summary(approvals, notes)

    head = raw.get("head_pipeline") or raw.get("pipeline") or {}
    result["head_pipeline"] = {
        "id": head.get("id"),
        "status": head.get("status"),
        "ref": head.get("ref"),
        "sha": head.get("sha"),
        "web_url": head.get("web_url"),
    } if head else None

    result["notes"] = notes
    return result


def _mr_diff_entries(ctx: Any, base: str,
                     notes: list[str]) -> tuple[list[dict], bool, int | None]:
    """Changed files for an MR, from ``/diffs`` or else ``/changes``.

    Both endpoints answer 200 on GitLab 17.5 and 19.1, but with different
    shapes: ``/diffs`` is a bare, paginated list; ``/changes`` is the whole MR
    as a dict with an unpaginated ``changes`` list. :func:`_diff_entries`
    accepts either, so a proxy or version that answers ``/diffs`` with the
    dict form is still read correctly.
    """
    diffs = _optional_get(ctx, f"{base}/diffs", {"per_page": PER_PAGE},
                          notes=notes, label="mr diffs")
    if isinstance(diffs, (list, dict)):
        entries, overflow = _diff_entries(diffs)
    else:
        changes = _optional_get(ctx, f"{base}/changes", notes=notes,
                                label="mr changes")
        entries, overflow = _diff_entries(changes)
    if overflow:
        notes.append("GitLab reported a diff overflow; not every file is listed")
    return entries, overflow, len(entries)


def _approval_summary(approvals: dict, notes: list[str]) -> dict:
    """Approval state without inventing rules the instance does not have.

    Measured on GitLab CE 17.5 and 19.1: ``/approvals`` answers 200 with only
    ``user_has_approved, user_can_approve, approved, approved_by``.
    ``approvals_required``/``approvals_left`` are Premium keys. Their absence
    means "no approval rules on this edition", never "satisfied": they are
    reported as ``None`` and explained in ``notes``. Defaulting them to 0
    would make every merge request look fully approved.
    """
    approved_by = []
    for entry in approvals.get("approved_by") or []:
        if not isinstance(entry, dict):
            continue
        user = entry.get("user") if isinstance(entry.get("user"), dict) else entry
        approved_by.append(user.get("username"))
    required = approvals.get("approvals_required")
    left = approvals.get("approvals_left")
    rules_known = "approvals_required" in approvals or "approvals_left" in approvals
    if not rules_known:
        notes.append(
            "approval rules are not reported by this GitLab edition "
            "(Community Edition): approvals_required and approvals_left are "
            "null (unknown), not 0; use 'approved' and 'approved_by'")
    approved = approvals.get("approved")
    return {
        "approved": bool(approved) if approved is not None else None,
        "approvals_required": required,
        "approvals_left": left,
        "approval_rules_available": rules_known,
        "approved_by": approved_by,
        "user_has_approved": approvals.get("user_has_approved"),
        "user_can_approve": approvals.get("user_can_approve"),
    }


def _summarise_discussions(discussions: Iterable[dict]) -> list[dict]:
    """Flatten discussion threads to author, body, file and resolution state."""
    summary = []
    for thread in discussions or []:
        entries = thread.get("notes") or []
        summary.append({
            "id": thread.get("id"),
            "resolved": all(n.get("resolved") for n in entries if n.get("resolvable"))
            if any(n.get("resolvable") for n in entries) else None,
            "notes": [{
                "author": (n.get("author") or {}).get("username"),
                "created_at": n.get("created_at"),
                "system": bool(n.get("system")),
                "body": n.get("body"),
                "path": (n.get("position") or {}).get("new_path"),
                "new_line": (n.get("position") or {}).get("new_line"),
            } for n in entries],
        })
    return summary


# ─── Person-centric report ────────────────────────────────────────────────────

# How a GitLab event's action_name maps onto the buckets people ask about.
_EVENT_PUSH = {"pushed to", "pushed new", "deleted branch", "pushed"}
_EVENT_COMMENT = {"commented on"}

_PERSON_METHOD = (
    "For each username: the account is resolved with /users?username=, then "
    "GitLab events (/users/:id/events, filtered with after=/before=) establish "
    "which projects the person touched. Explicitly requested projects are added "
    "to that set. For every project in the set the commit list is fetched with "
    "stats and a commit is attributed to the person when its author_email "
    "matches one of the account's visible e-mail addresses, or its author_name "
    "equals the account's name or username case-insensitively. Merge requests "
    "are counted through the API by author_id; comments and pushes are counted "
    "from the event stream, pushes by action_name (push_data is optional and "
    "often absent). Merge commits (two or more parents) are excluded from "
    "commit and line counts because they repeat the merged branch's lines."
)

_PERSON_LIMITATIONS = (
    "Commits authored under an e-mail address the API does not expose (a "
    "private commit e-mail, or a laptop with a personal git config) are "
    "missed, so commit counts are a lower bound.",
    "The events endpoint filters by date, not by instant, and its after=/"
    "before= bounds are exclusive, so the event window can be up to a day "
    "wider or narrower than the requested one.",
    "GitLab prunes contribution events after its configured retention period, "
    "so a window older than that retention will look empty even when work "
    "happened.",
    "Only the default branch's history is read for commit attribution; work "
    "that lives on an unmerged branch is not counted.",
    "At most max_projects projects are inspected; when the cap is hit the "
    "result sets truncated=true and the remaining projects are not counted.",
    "Events only record projects the token can see; private projects the "
    "person worked on but the token cannot read are invisible.",
)


def _resolve_person(ctx: Any, handle: str, notes: list[str]) -> dict | None:
    """Find one GitLab account by exact username, then by search."""
    matches = _optional_get(ctx, "/users", {"username": handle},
                            notes=notes, label=f"user lookup {handle}")
    if not matches:
        matches = _optional_get(ctx, "/users", {"search": handle},
                                notes=notes, label=f"user search {handle}")
    if not matches:
        return None
    return matches[0] if isinstance(matches, list) else matches


def _person_emails(user: dict) -> set[str]:
    """Every e-mail address for this account the API was willing to show us."""
    candidates = (user.get("public_email"), user.get("email"),
                  user.get("commit_email"))
    return {str(e).strip().lower() for e in candidates if e}


def _person_names(user: dict) -> set[str]:
    """Name and username, lowercased, for the name-equality fallback."""
    return {str(v).strip().lower()
            for v in (user.get("name"), user.get("username")) if v}


def _attributes_to_person(row: dict, emails: set[str], names: set[str]) -> bool:
    """Whether one commit row belongs to this person.

    E-mail is the reliable signal. Name equality is a deliberate fallback for
    the common case of a commit e-mail the API will not disclose; it is exact
    equality, never a substring, so "Ali" does not absorb "Alice".
    """
    email = (row.get("author_email") or "").strip().lower()
    if email and email in emails:
        return True
    name = (row.get("author_name") or "").strip().lower()
    return bool(name) and name in names


def _event_author(event: dict) -> tuple[Any, str | None]:
    """``(author_id, username)`` of an event, from wherever GitLab put them.

    The handle lives in the top-level ``author_username`` key on both 17.5
    and 19.1; the nested ``author`` object is a fallback, not the source.
    """
    nested = event.get("author") if isinstance(event.get("author"), dict) else {}
    author_id = event.get("author_id", nested.get("id"))
    username = event.get("author_username") or nested.get("username")
    return author_id, (str(username).lower() if username else None)


def _events_by(events: Iterable[dict], user: dict) -> list[dict]:
    """Keep only events whose author is ``user``.

    ``/users/:id/events`` should already be scoped, but an event that
    positively names another author (by id or by ``author_username``) is not
    this person's work and is dropped. Events that name no author are kept.
    """
    want_id = user.get("id")
    want_name = str(user.get("username") or "").lower() or None
    kept = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        author_id, username = _event_author(event)
        if author_id is not None and want_id is not None and author_id != want_id:
            continue
        if username and want_name and username != want_name:
            continue
        kept.append(event)
    return kept


def _is_push(action: str) -> bool:
    """Whether an ``action_name`` describes a push.

    Push activity is derived from ``action_name`` alone. ``push_data`` is
    optional (it was absent from most captured events on both measured
    instances) and ``target_type`` is ``null`` on 17.5 but ``"Project"`` on
    19.1 for the same push, so neither can be the signal.
    """
    return any(action.startswith(p) for p in _EVENT_PUSH)


def _bucket_events(events: Iterable[dict]) -> tuple[dict[int, dict], dict]:
    """Group raw events by project id and total them.

    Returns ``(per_project, totals)``; ``per_project`` is keyed by the numeric
    project id the event carries. ``pushed_commits`` sums
    ``push_data.commit_count`` only where GitLab sent it, and
    ``pushes_without_push_data`` says how many pushes lacked it, so the sum is
    never mistaken for a complete count.
    """
    per_project: dict[int, dict] = {}
    totals = {"events": 0, "pushes": 0, "comments": 0, "mrs_opened": 0,
              "mrs_merged": 0, "issues_opened": 0, "pushed_commits": 0,
              "pushes_without_push_data": 0}

    for event in events or []:
        project_id = event.get("project_id")
        action = str(event.get("action_name") or "").strip().lower()
        target = str(event.get("target_type") or "")
        bucket = per_project.setdefault(project_id, {
            "events": 0, "pushes": 0, "comments": 0,
            "mrs_opened": 0, "mrs_merged": 0, "issues_opened": 0,
        })
        bucket["events"] += 1
        totals["events"] += 1

        if _is_push(action):
            bucket["pushes"] += 1
            totals["pushes"] += 1
            push_data = event.get("push_data")
            count = (push_data or {}).get("commit_count") \
                if isinstance(push_data, dict) else None
            if isinstance(count, int):
                totals["pushed_commits"] += count
            else:
                totals["pushes_without_push_data"] += 1
        elif action in _EVENT_COMMENT:
            bucket["comments"] += 1
            totals["comments"] += 1
        elif action in {"accepted", "merged"} and target == "MergeRequest":
            bucket["mrs_merged"] += 1
            totals["mrs_merged"] += 1
        elif action == "opened" and target == "MergeRequest":
            bucket["mrs_opened"] += 1
            totals["mrs_opened"] += 1
        elif action == "opened" and target == "Issue":
            bucket["issues_opened"] += 1
            totals["issues_opened"] += 1

    return per_project, totals


def _project_label(ctx: Any, project_id: Any, cache: dict,
                   notes: list[str]) -> dict:
    """``{"id", "path"}`` for a project id, resolved once and cached."""
    if project_id in cache:
        return cache[project_id]
    proj = _optional_get(ctx, f"/projects/{_enc(project_id)}", notes=notes,
                         label=f"project {project_id}") or {}
    label = {"id": proj.get("id") or project_id,
             "path": proj.get("path_with_namespace") or str(project_id),
             "default_branch": proj.get("default_branch")}
    cache[project_id] = label
    return label


def _person_project_work(ctx: Any, label: dict, user: dict, emails: set[str],
                         names: set[str], since_iso: str | None,
                         until_iso: str | None, event_counts: dict,
                         notes: list[str]) -> dict:
    """One person's footprint on one project."""
    pid_path = f"/projects/{label['id']}"
    rows: list[dict] = []
    truncated = False
    try:
        rows, truncated, _ = _fetch_commit_rows(
            ctx, pid_path, since_iso, until_iso, None, None, True, 1000
        )
    except Exception as exc:  # noqa: BLE001 - one unreadable project must not kill the report
        notes.append(f"commits for {label['path']} unavailable: {exc}")

    mine = [r for r in _authored_work(rows)
            if _attributes_to_person(r, emails, names)]

    mr_params: dict[str, Any] = {"state": "all", "per_page": PER_PAGE,
                                 "author_id": user.get("id")}
    if since_iso:
        mr_params["created_after"] = since_iso
    if until_iso:
        mr_params["created_before"] = until_iso
    authored = _optional_get(ctx, f"{pid_path}/merge_requests", mr_params,
                             notes=notes,
                             label=f"merge requests for {label['path']}") or []
    if isinstance(authored, dict):
        authored = []

    return {
        "project": label["path"],
        "project_id": label["id"],
        "commits": len(mine),
        "additions": sum(r["additions"] for r in mine),
        "deletions": sum(r["deletions"] for r in mine),
        "sample_commit_titles": [r["title"] for r in mine[:5]],
        "commits_scanned": len(rows),
        "commits_truncated": truncated,
        "merge_requests_opened": len(authored),
        "merge_requests_merged": sum(1 for m in authored
                                     if m.get("state") == "merged"),
        "merge_requests": [_mr_row(m) for m in authored[:10]],
        "comments": event_counts.get("comments", 0),
        "pushes": event_counts.get("pushes", 0),
        "events": event_counts.get("events", 0),
    }


def person_report(ctx: Any, people: list[str], since: str = "7d",
                  until: str | None = None, projects: list[str] | None = None,
                  max_projects: int = 50) -> dict:
    """What did these people do, on which projects, and how much code.

    One entry per requested handle. Each entry lists the projects the person
    touched in the window with commit counts, lines added and removed, a few
    commit titles as evidence, merge requests opened and merged, comments and
    pushes — plus a ``totals`` roll-up.

    The result always carries ``method`` and ``limitations``: the numbers are
    a defensible lower bound, not an audit, and a reader needs to know which.
    """
    since_iso = resolve_since(since)
    until_iso = resolve_since(until)
    cap = max(1, int(max_projects))
    label_cache: dict[Any, dict] = {}

    explicit: list[dict] = []
    notes: list[str] = []
    for ref in projects or []:
        try:
            explicit.append(_project_label(ctx, ctx.project_id(ref),
                                           label_cache, notes))
        except Exception as exc:  # noqa: BLE001 - a bad ref is a note, not a crash
            notes.append(f"project {ref} could not be resolved: {exc}")

    reports: list[dict] = []
    for handle in people or []:
        reports.append(_one_person_report(
            ctx, handle, since_iso, until_iso, explicit, cap,
            label_cache, notes,
        ))

    totals = {
        "people": len(reports),
        "commits": sum(p["totals"]["commits"] for p in reports),
        "additions": sum(p["totals"]["additions"] for p in reports),
        "deletions": sum(p["totals"]["deletions"] for p in reports),
        "merge_requests_opened": sum(p["totals"]["merge_requests_opened"]
                                     for p in reports),
        "merge_requests_merged": sum(p["totals"]["merge_requests_merged"]
                                     for p in reports),
        "comments": sum(p["totals"]["comments"] for p in reports),
        "pushes": sum(p["totals"]["pushes"] for p in reports),
    }

    return {
        "since": since_iso,
        "until": until_iso,
        "max_projects": cap,
        "requested": list(people or []),
        "people": reports,
        "totals": totals,
        "truncated": any(p.get("truncated") for p in reports),
        "method": _PERSON_METHOD,
        "limitations": list(_PERSON_LIMITATIONS),
        "notes": notes,
    }


def _one_person_report(ctx: Any, handle: str, since_iso: str | None,
                       until_iso: str | None, explicit: list[dict], cap: int,
                       label_cache: dict, notes: list[str]) -> dict:
    """The per-handle body of :func:`person_report`."""
    user = _resolve_person(ctx, handle, notes)
    if not user:
        return {
            "requested": handle,
            "found": False,
            "projects": [],
            "totals": _empty_person_totals(),
            "truncated": False,
            "note": f"no GitLab account matches {handle!r}",
        }

    emails = _person_emails(user)
    names = _person_names(user)

    event_params: dict[str, Any] = {"per_page": PER_PAGE}
    after = _as_event_date(since_iso)
    before = _as_event_date(until_iso)
    if after:
        event_params["after"] = after
    if before:
        event_params["before"] = before
    events, events_truncated, _ = _page(
        ctx, f"/users/{user['id']}/events", event_params, 2000
    )
    per_project_events, event_totals = _bucket_events(_events_by(events, user))

    # Explicit projects first: a caller who named a project wants it reported
    # even when no event mentions it (events expire; commits do not).
    ordered: list[dict] = list(explicit)
    seen = {label["id"] for label in ordered}
    for project_id, _counts in sorted(
        per_project_events.items(),
        key=lambda kv: -kv[1]["events"],
    ):
        if project_id is None:
            continue
        label = _project_label(ctx, project_id, label_cache, notes)
        if label["id"] in seen:
            continue
        seen.add(label["id"])
        ordered.append(label)

    truncated = events_truncated or len(ordered) > cap
    per_project = [
        _person_project_work(
            ctx, label, user, emails, names, since_iso, until_iso,
            per_project_events.get(label["id"], {}), notes,
        )
        for label in ordered[:cap]
    ]

    totals = {
        "projects": len(per_project),
        "commits": sum(p["commits"] for p in per_project),
        "additions": sum(p["additions"] for p in per_project),
        "deletions": sum(p["deletions"] for p in per_project),
        "merge_requests_opened": sum(p["merge_requests_opened"]
                                     for p in per_project),
        "merge_requests_merged": sum(p["merge_requests_merged"]
                                     for p in per_project),
        "comments": event_totals["comments"],
        "pushes": event_totals["pushes"],
        "events": event_totals["events"],
    }

    return {
        "requested": handle,
        "found": True,
        "username": user.get("username"),
        "name": user.get("name"),
        "user_id": user.get("id"),
        "state": user.get("state"),
        "matched_emails": sorted(emails),
        "matched_names": sorted(names),
        "event_summary": event_totals,
        "projects": per_project,
        "projects_considered": len(ordered),
        "totals": totals,
        "truncated": truncated,
    }


def _empty_person_totals() -> dict:
    """Zeroed totals, so a not-found person still sums cleanly."""
    return {"projects": 0, "commits": 0, "additions": 0, "deletions": 0,
            "merge_requests_opened": 0, "merge_requests_merged": 0,
            "comments": 0, "pushes": 0, "events": 0}


# ─── Local clones ─────────────────────────────────────────────────────────────

#: ``-c`` overrides put in front of EVERY git command a read tool runs. A
#: repository found on disk is untrusted input: its own ``.git/config`` can
#: name a program for git to execute (``core.fsmonitor`` runs on every
#: ``git status``), so each such key is pinned to an inert value on the
#: command line, which outranks repository config. Verified on git 2.43.
SAFE_GIT_CONFIG: tuple[str, ...] = (
    "-c", "core.fsmonitor=false",
    "-c", "core.untrackedCache=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.pager=cat",
    "-c", "core.sshCommand=",
    "-c", "core.askPass=",
    "-c", "core.gitProxy=",
    "-c", "credential.helper=",
    "-c", "diff.external=",
    "-c", "protocol.file.allow=never",
    "-c", "protocol.ext.allow=never",
)

#: Environment applied on top of whatever the context supplies, for every
#: read-tool git process: no system config, no prompt, no optional locks.
SAFE_GIT_ENV: dict[str, str] = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "GIT_PAGER": "cat",
    "GCM_INTERACTIVE": "never",
}

#: Inherited variables that would re-point git or name a program; removed.
_UNSAFE_GIT_ENV = ("GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
                   "GIT_CONFIG_GLOBAL", "GIT_DIR", "GIT_WORK_TREE",
                   "GIT_EXEC_PATH", "GIT_EXTERNAL_DIFF", "GIT_SSH",
                   "GIT_SSH_COMMAND", "GIT_PROXY_COMMAND")

# Per-driver keys a repository can set to a command: ``filter.<x>.clean`` runs
# during ``git status`` whenever a tracked file's stat data is stale.
_DRIVER_KEYS = r"^(filter|diff|merge)\..+\.(clean|smudge|process|command|textconv|driver)$"


def _safe_env(env: dict | None) -> dict:
    """``env`` with :data:`SAFE_GIT_ENV` forced over it (a fresh dict)."""
    out = dict(env or {})
    out.update(SAFE_GIT_ENV)
    for key in _UNSAFE_GIT_ENV:
        out.pop(key, None)
    return out


def _driver_overrides(repo: str, env: dict) -> list[str] | None:
    """``-c`` args blanking every command-valued driver the repo configures.

    Returns ``None`` when a configured name cannot be overridden safely (a
    ``=`` or control character in it would make ``-c name=`` mean something
    else); the caller must then not run content-reading commands there.
    """
    try:
        done = subprocess.run(  # noqa: S603 - argv list, no shell, fixed binary
            [GIT_BIN, "--no-optional-locks", *SAFE_GIT_CONFIG, "config",
             "--name-only", "--get-regexp", _DRIVER_KEYS],
            cwd=repo, env=_safe_env(env), capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SECONDS, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if done.returncode not in (0, 1):          # 1 == no matching key
        return None
    out: list[str] = []
    for name in (done.stdout or "").splitlines():
        name = name.strip()
        if not name:
            continue
        if "=" in name or any(ord(ch) < 0x20 for ch in name):
            return None
        out += ["-c", f"{name}="]
        if name.startswith("filter."):
            driver = name.rsplit(".", 1)[0]
            out += ["-c", f"{driver}.required=false"]
    return out


def _git(repo: str, args: Sequence[str], env: dict,
         extra_config: Sequence[str] = ()) -> tuple[int, str]:
    """Run one read-only git command inside ``repo``.

    The properties that matter here and are not negotiable:

    * **argv list, never a shell.** Nothing is interpolated into a command
      string, so a repository or remote name cannot inject an argument.
    * **``cwd``, not ``-C <path>``.** The path never becomes a git argument
      at all, which removes the whole class of "a directory named
      ``--upload-pack=...``" problems.
    * **Repository config cannot run a program.** :data:`SAFE_GIT_CONFIG`
      and ``--no-optional-locks`` precede every command, ``extra_config``
      carries per-repo driver overrides, and :data:`SAFE_GIT_ENV` is forced
      into the environment regardless of what the caller passed.
    * **``GIT_TERMINAL_PROMPT=0`` and a timeout.** A repo whose remote wants
      credentials must fail fast instead of blocking the report forever.
    """
    try:
        done = subprocess.run(  # noqa: S603 - argv list, no shell, fixed binary
            [GIT_BIN, "--no-optional-locks", *SAFE_GIT_CONFIG,
             *extra_config, *args],
            cwd=repo,
            env=_safe_env(env),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout or "").strip()


def _git_env(ctx: Any) -> dict:
    """Environment for local git calls, preferring the context's own."""
    getter = getattr(ctx, "git_env", None)
    if callable(getter):
        env = dict(getter())
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        return env
    import os

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["GCM_INTERACTIVE"] = "never"
    return env


def _ahead_behind(repo: str, env: dict) -> tuple[int | None, int | None]:
    """Commits ahead of and behind the tracked upstream, without fetching.

    ``git rev-list --left-right --count @{u}...HEAD`` prints
    ``<upstream-only> <head-only>`` — i.e. behind then ahead. A branch with
    no upstream exits non-zero, which is reported as ``(None, None)`` rather
    than as zero, because "no upstream" and "in sync" are different facts.
    """
    code, out = _git(repo, ["rev-list", "--left-right", "--count",
                            "@{u}...HEAD"], env)
    if code != 0:
        return None, None
    parts = out.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None, None
    return int(parts[1]), int(parts[0])


def _contained_dir(root: str, relative: str | None) -> str:
    """``root/relative`` as a real path, guaranteed to stay inside ``root``.

    ``relative`` is caller-supplied (an MCP client or prompt-injected agent),
    so it is rejected outright when it is absolute, contains a ``..`` or
    ``.`` segment, a backslash, a NUL or any other control character. The
    joined path is then resolved with :func:`os.path.realpath` — following
    every symlink — and must still lie inside ``realpath(root)``.

    Raises:
        ValueError: the path is unsafe or escapes ``root``.
    """
    import os

    real_root = os.path.realpath(os.path.abspath(root))
    if relative is None:
        return real_root
    text = str(relative)
    if any(ord(ch) < 0x20 or ch == "\x7f" for ch in text):
        raise ValueError("group must not contain NUL or control characters")
    if "\\" in text:
        raise ValueError("group must use '/' separators, not '\\'")
    stripped = text.strip()
    if stripped.startswith("/") or os.path.isabs(stripped) or \
            (len(stripped) > 1 and stripped[1] == ":"):
        raise ValueError(
            f"group must be relative to the managed directory: {relative!r}")
    parts = [p for p in stripped.rstrip("/").split("/")]
    if any(p in ("", ".", "..") for p in parts) and stripped:
        raise ValueError(
            f"group must not contain empty, '.' or '..' segments: {relative!r}")
    target = os.path.realpath(os.path.join(real_root, *parts)) if stripped \
        else real_root
    if os.path.commonpath([real_root, target]) != real_root:
        raise ValueError(
            f"group resolves outside the managed directory: {relative!r}")
    return target


def local_status(ctx: Any, group: str | None = None) -> dict:
    """State of the local clones under the instance's managed directory.

    For each repository: current branch, whether the working tree is dirty
    and by how many entries, how far ahead/behind its upstream it is, and its
    ``origin`` URL with any embedded credential stripped.

    **No network access.** Nothing fetches, so ahead/behind reflects the last
    time the clone was updated — that is the point, since this report is used
    to decide whether a sync is needed.

    ``group`` must name a directory *inside* the managed directory; anything
    absolute, containing ``..``, or resolving (through symlinks) elsewhere is
    a ``ValueError``. Every git process runs with repository-supplied program
    hooks disabled (see :func:`_git`).

    Repository discovery reuses ``pull._find_git_repos`` rather than
    reimplementing a tree walk; it prints, so it is called through
    ``ctx.run_captured``.
    """
    import os

    base = _contained_dir(ctx.master_dir, group if group else None)

    if not os.path.isdir(base):
        return {"base": base, "group": group, "repos": [], "count": 0,
                "dirty": 0, "output": "",
                "note": "managed directory does not exist locally"}

    found, output = ctx.run_captured(gl_pull._find_git_repos, base)
    env = _git_env(ctx)

    repos: list[dict] = []
    for repo in sorted(found or []):
        real_repo = os.path.realpath(repo)
        if os.path.commonpath([base, real_repo]) != base:
            continue                     # a symlinked escape; never touch it
        code, branch = _git(repo, ["rev-parse", "--abbrev-ref", "HEAD"], env)
        branch_name = branch if code == 0 else None

        drivers = _driver_overrides(repo, env)
        if drivers is None:
            code, porcelain = 1, ""      # unsafe driver config: skip status
        else:
            code, porcelain = _git(repo, ["status", "--porcelain",
                                          "--ignore-submodules=all"],
                                   env, drivers)
        dirty_entries = len([ln for ln in porcelain.splitlines() if ln.strip()]) \
            if code == 0 else None

        ahead, behind = _ahead_behind(repo, env)

        code, origin = _git(repo, ["remote", "get-url", "origin"], env)
        origin_url = gl_git.strip_userinfo(origin) if code == 0 and origin else None

        repos.append({
            "path": repo,
            "relative_path": os.path.relpath(repo, base),
            "branch": branch_name,
            "detached": branch_name == "HEAD",
            "dirty": bool(dirty_entries),
            "dirty_entries": dirty_entries,
            "ahead": ahead,
            "behind": behind,
            "has_upstream": ahead is not None,
            "origin": origin_url,
        })

    return {
        "base": base,
        "group": group,
        "instance": getattr(ctx, "name", None),
        "repos": repos,
        "count": len(repos),
        "dirty": sum(1 for r in repos if r["dirty"]),
        "ahead": sum(r["ahead"] or 0 for r in repos),
        "behind": sum(r["behind"] or 0 for r in repos),
        "output": output,
        "fetched": False,
    }
