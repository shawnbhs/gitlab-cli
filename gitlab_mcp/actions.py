"""Mutating GitLab actions, each as a side-effect-free preview plus a doer.

Every action in this module changes something — on the GitLab server or on
disk — so each one is exposed as a pair:

* ``preview_<name>(ctx, **args) -> dict`` validates the arguments, reads
  whatever it needs with ``GET`` only, and describes exactly what
  ``do_<name>`` would do. It never issues a ``POST`` or ``PUT``.
* ``do_<name>(ctx, **args) -> dict`` performs the change and returns a
  JSON-serialisable result.

The server layer runs the preview first, shows it to the human, and only
calls the doer once the confirmation token comes back.

Local clone operations (``sync_local``, ``clone_missing``) deliberately do
not reimplement git. They reuse the existing gitlab-cli implementation
(``pull.sync_many``, ``clone.cmd_clone``) through ``ctx.run_captured``,
because those functions print to stdout and MCP speaks its protocol on
stdout. The directory they act on is always resolved with
``paths.resolve`` / ``paths.target_for_clone`` against the instance's
``master_dir`` anchored to an absolute path, never against the current
working directory.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any
from urllib.parse import quote

import clone as gl_clone
import git as gl_git
import paths as gl_paths
import pull as gl_pull

__all__ = [
    "MAX_BODY_CHARS",
    "MAX_DESCRIPTION_DISPLAY_CHARS",
    "MAX_TITLE_CHARS",
    "find_quick_actions",
    "neutralize_quick_actions",
    "PROTECTED_BRANCHES",
    "is_protected_branch",
    "build_review_position",
    "preview_sync_local", "do_sync_local",
    "preview_clone_missing", "do_clone_missing",
    "preview_mr_comment", "do_mr_comment",
    "preview_mr_review_comment", "do_mr_review_comment",
    "preview_mr_approve", "do_mr_approve",
    "preview_mr_unapprove", "do_mr_unapprove",
    "preview_mr_create", "do_mr_create",
    "preview_mr_merge", "do_mr_merge",
    "preview_issue_comment", "do_issue_comment",
    "preview_create_branch", "do_create_branch",
]

# A note body longer than this is a copy-paste accident, not a review.
MAX_BODY_CHARS = 100_000
MAX_TITLE_CHARS = 1_000

# A merge request description is shown in full in the preview up to this many
# characters; past it the display is cut with an explicit marker, and the
# SHA-256 of the full text that will be sent is shown alongside.
MAX_DESCRIPTION_DISPLAY_CHARS = 4_000

# A GitLab quick action is a line that starts with ``/`` immediately followed
# by a command word and then whitespace or the end of the line (``/merge``,
# ``/assign @bob``). GitLab executes them when a note or description is
# saved, so a body that carries one does more than post text. Leading
# whitespace is tolerated here on purpose: over-matching only costs a pair of
# backticks, under-matching lets a merge through unseen.
_QUICK_ACTION_RE = re.compile(r"^[ \t]*/([A-Za-z_][A-Za-z0-9_]*)(?=\s|$)")

# Branch names that mean "people depend on this". Compared case-insensitively;
# ``release`` additionally matches any ``release*`` name (release/1.2, release-24).
PROTECTED_BRANCHES = ("main", "master", "production", "prod")
PROTECTED_PREFIXES = ("release",)

# How many paths a preview lists before it starts summarising. A group can
# hold hundreds of clones and the preview is meant to be read by a human.
MAX_LISTED_PATHS = 50

# detailed_merge_status (GitLab >= 15.6) and the older merge_status each have
# exactly one value that means "GitLab would accept a merge right now".
_MERGEABLE_DETAILED = frozenset({"mergeable"})
_MERGEABLE_LEGACY = frozenset({"can_be_merged"})


# ─── Validation ───────────────────────────────────────────────────────────────

def is_protected_branch(branch: str | None) -> bool:
    """Whether ``branch`` names a production-ish target.

    ``main``, ``master``, ``production``, ``prod`` and anything starting with
    ``release``, case-insensitively.
    """
    name = (branch or "").strip().lower()
    if not name:
        return False
    if name in PROTECTED_BRANCHES:
        return True
    return any(name.startswith(p) for p in PROTECTED_PREFIXES)


def _require_text(value: Any, field: str, max_chars: int) -> str:
    """Validate a human-authored string argument.

    Empty (or whitespace-only) is rejected because GitLab silently accepts an
    empty note body and creates a useless note.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    if len(value) > max_chars:
        raise ValueError(
            f"{field} is {len(value)} characters; the limit is {max_chars}")
    return value


def _require_branch(value: Any, field: str) -> str:
    """Validate a branch/ref argument with gitlab-cli's own ref check.

    ``git.check_ref`` is the authority here: it rejects control characters and
    anything git would read as an option (a leading ``-``), which is the
    argument-injection boundary for every later git invocation.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    name = value.strip()
    if not name:
        raise ValueError(f"{field} must not be empty")
    try:
        return gl_git.check_ref(name)
    except gl_git.UnsafeRef as exc:
        raise ValueError(f"{field}: {exc}") from None


def _require_iid(value: Any, field: str = "iid") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            value = int(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{field} must be an integer") from None
    if value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _require_path(value: Any, field: str = "path") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    text = value.strip()
    if any(c in text for c in ("\n", "\r", "\x00")):
        raise ValueError(f"{field} must not contain control characters")
    return text


# ─── Shared plumbing ──────────────────────────────────────────────────────────

def _context_cfg(ctx: Any) -> dict:
    """The instance cfg dict the gitlab-cli commands expect.

    ``GitLabContext`` exposes ``cfg_public()``, which strips ``token`` and
    ``headers``. ``clone.cmd_clone`` needs both, so a context that offers a
    fuller accessor is preferred when one exists. See CONTRACT ISSUES in the
    task report.
    """
    for name in ("cfg_for_git", "_cfg", "cfg_public"):
        getter = getattr(ctx, name, None)
        if callable(getter):
            cfg = getter()
            if isinstance(cfg, dict):
                return cfg
    return {}


def _managed_dir(ctx: Any) -> str:
    """The instance's clone root as an absolute path.

    ``paths.anchor`` is what makes this safe: a relative ``master_dir`` would
    otherwise be resolved against the current working directory, and a sync or
    clone would act on a tree nobody is looking at.
    """
    return gl_paths.anchor(getattr(ctx, "master_dir", "") or ".")


def _group_dir(ctx: Any, group: str | None) -> str:
    """Local directory for ``group`` under the managed dir (or the dir itself)."""
    dest = _managed_dir(ctx)
    if not group:
        return dest
    return gl_paths.resolve(group, dest, _context_cfg(ctx).get("folder"))


def _mr_path(ctx: Any, project: str | int, iid: int) -> str:
    return f"/projects/{ctx.project_id(project)}/merge_requests/{iid}"


def _listed(items: list[str]) -> dict:
    """A human-sized view of a possibly long path list."""
    return {
        "total": len(items),
        "sample": items[:MAX_LISTED_PATHS],
        "truncated": len(items) > MAX_LISTED_PATHS,
    }


def _preview(action: str, summary: str, *, ok: bool = True,
             danger: bool = False, reasons: list[str] | None = None,
             warnings: list[str] | None = None, **extra: Any) -> dict:
    """Assemble a preview dict with the keys every preview shares."""
    out: dict[str, Any] = {
        "action": action,
        "ok": ok,
        "danger": danger,
        "summary": summary,
        "reasons": list(reasons or []),
        "warnings": list(warnings or []),
    }
    out.update(extra)
    return out


def _danger_line(target: str) -> str:
    return (f"DANGER: '{target}' is a protected/production target — "
            f"this change affects everyone. Read the preview twice.")


def find_quick_actions(text: str | None) -> list[str]:
    """Quick-action commands (``/merge``, ``/approve`` ...) found in ``text``.

    Lines are split the way :meth:`str.splitlines` does, which is a superset
    of GitLab's own line breaks, so nothing GitLab would run is missed. The
    result keeps the order of first appearance, lower-cased, without
    duplicates.
    """
    found: list[str] = []
    for line in (text or "").splitlines():
        match = _QUICK_ACTION_RE.match(line)
        if match:
            cmd = "/" + match.group(1).lower()
            if cmd not in found:
                found.append(cmd)
    return found


def _inline_code(text: str) -> str:
    """Wrap ``text`` in a Markdown code span that survives inner backticks."""
    longest = max((len(r) for r in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if longest else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def neutralize_quick_actions(text: str) -> str:
    """Render every quick-action line of ``text`` as inline code.

    The line then starts with a backtick instead of ``/``, so GitLab shows it
    as text and does not execute it. Leading indentation and line endings are
    kept; every other line is returned unchanged.
    """
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")
        ending = line[len(body):]
        if _QUICK_ACTION_RE.match(body):
            stripped = body.lstrip(" \t")
            indent = body[:len(body) - len(stripped)]
            body = indent + _inline_code(stripped.rstrip())
        out.append(body + ending)
    return "".join(out)


def _quick_action_gate(text: str, field: str,
                       allow_quick_actions: bool) -> tuple[str, list[str],
                                                           list[str]]:
    """(text to send, commands found, warnings) for a human-authored body.

    Quick actions are neutralised unless ``allow_quick_actions`` is exactly
    ``True`` (a truthy string from a loose client does not count).
    """
    commands = find_quick_actions(text)
    if not commands:
        return text, [], []
    listed = ", ".join(commands)
    if allow_quick_actions is True:
        warning = (f"DANGER: the {field} contains GitLab quick actions "
                   f"({listed}) and allow_quick_actions=True: GitLab WILL "
                   f"EXECUTE them when this is posted (they can merge, "
                   f"approve, close, assign or relabel without any further "
                   f"preview).")
        return text, commands, [warning]
    warning = (f"DANGER: the {field} contains GitLab quick-action lines "
               f"({listed}). They have been neutralised (wrapped in "
               f"backticks) and will be posted as text, not executed. Pass "
               f"allow_quick_actions=True only if the user explicitly wants "
               f"them run.")
    return neutralize_quick_actions(text), commands, [warning]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _display_text(text: str, limit: int = MAX_DESCRIPTION_DISPLAY_CHARS
                  ) -> tuple[str, bool]:
    """(text for a human to read, whether it was cut for display)."""
    if len(text) <= limit:
        return text, False
    hidden = len(text) - limit
    return (text[:limit] + f"\n\n[... TRUNCATED FOR DISPLAY ONLY: {hidden} "
            f"more characters will be sent; compare description_sha256 ...]",
            True)


def _get_optional(ctx: Any, path: str, params: dict | None = None) -> Any:
    """GET that returns None instead of raising.

    Used for the extras a preview is nicer for having (approvals on a CE
    instance, a pipeline list) but must not fail over.
    """
    try:
        return ctx.get(path, params) if params is not None else ctx.get(path)
    except Exception:                                    # noqa: BLE001
        return None


# ─── sync_local ───────────────────────────────────────────────────────────────

def _repos_to_sync(ctx: Any, group: str | None,
                   project: str | None) -> tuple[str, list[str]]:
    """(base directory, git work trees under it that would be pulled)."""
    base = _group_dir(ctx, group)
    if not os.path.isdir(base):
        return base, []
    repos = gl_pull._find_git_repos(base)
    if project:
        wanted = os.path.basename(project.replace("\\", "/").rstrip("/"))
        repos = [r for r in repos if os.path.basename(r) == wanted]
    return base, repos


def preview_sync_local(ctx: Any, group: str | None = None,
                       project: str | None = None) -> dict:
    """Describe the fast-forward-only pull of the local clones."""
    base, repos = _repos_to_sync(ctx, group, project)
    scope = f"group '{group}'" if group else "the whole managed directory"
    if project:
        scope = f"project '{project}'"

    note = ("Fetch then fast-forward only: a repo that is dirty, ahead, "
            "diverged or detached is reported and left untouched. Nothing is "
            "merged, rebased, reset or discarded.")

    if not os.path.isdir(base):
        return _preview(
            "sync_local",
            f"Nothing to sync: '{base}' does not exist. Clone it first.",
            ok=False, reasons=[f"directory does not exist: {base}"],
            base=base, scope=scope, repo_count=0, repos=_listed([]),
            fast_forward_only=True, policy=note)
    if not repos:
        return _preview(
            "sync_local", f"No git repositories found under '{base}'.",
            ok=False, reasons=[f"no git repositories under {base}"],
            base=base, scope=scope, repo_count=0, repos=_listed([]),
            fast_forward_only=True, policy=note)

    return _preview(
        "sync_local",
        f"Fetch and fast-forward {len(repos)} local repositories for {scope} "
        f"under '{base}'.",
        base=base, scope=scope, repo_count=len(repos), repos=_listed(repos),
        fast_forward_only=True, policy=note,
        workers=min(len(repos), gl_pull.DEFAULT_WORKERS))


def do_sync_local(ctx: Any, group: str | None = None,
                  project: str | None = None) -> dict:
    """Fast-forward the local clones via ``pull.sync_many``.

    ``pull.sync_many`` prints its progress, so it runs inside
    ``ctx.run_captured``; the captured (redacted) text is returned as
    ``output``. Its return value carries a per-status count for every repo and
    a full per-repo record for each repo that needs attention, which is what
    ``outcomes`` reports.
    """
    base, repos = _repos_to_sync(ctx, group, project)
    if not repos:
        return {
            "action": "sync_local", "ok": False, "base": base,
            "repo_count": 0, "counts": {}, "outcomes": [], "output": "",
            "error": f"no git repositories to sync under {base}",
        }

    workers = min(len(repos), gl_pull.DEFAULT_WORKERS)
    result, output = ctx.run_captured(
        gl_pull.sync_many, repos, base, workers=workers, show_all=False)
    stats, attention = result if isinstance(result, tuple) else ({}, [])

    outcomes = [
        {
            "path": os.path.relpath(r.get("path", ""), base),
            "status": r.get("status"),
            "branch": r.get("branch"),
            "detail": r.get("detail") or "",
        }
        for r in (attention or [])
    ]
    return {
        "action": "sync_local",
        "ok": True,
        "base": base,
        "repo_count": len(repos),
        "fast_forward_only": True,
        "counts": dict(stats or {}),
        "outcomes": outcomes,
        "outcome_detail": ("per-repo detail is reported for repositories that "
                           "need attention; the rest are counted by status"),
        "needs_attention": len(outcomes),
        "output": output,
    }


# ─── clone_missing ────────────────────────────────────────────────────────────

def preview_clone_missing(ctx: Any, group: str) -> dict:
    """Describe the clone of a group's projects into the managed directory."""
    if not isinstance(group, str) or not group.strip():
        raise ValueError("group must be a non-empty string")
    group = group.strip()

    dest = _managed_dir(ctx)
    try:
        target = gl_paths.target_for_clone(
            group, dest, _context_cfg(ctx).get("folder"))
    except gl_paths.UnsafePath as exc:
        raise ValueError(f"group: {exc}") from None

    state = gl_clone.repo_state(target) if os.path.exists(target) else "absent"
    existing = gl_pull._find_git_repos(target) if os.path.isdir(target) else []

    remote = _get_optional(
        ctx, f"/groups/{quote(group, safe='')}/projects",
        {"include_subgroups": "true", "archived": "false"})
    remote_paths = sorted(
        p.get("path_with_namespace", "") for p in (remote or [])
        if isinstance(p, dict))
    have = {os.path.basename(p) for p in existing}
    missing = [p for p in remote_paths if os.path.basename(p) not in have]

    warnings: list[str] = []
    if state == "invalid":
        warnings.append(
            f"'{target}' exists but is not a usable git repository "
            f"(an interrupted clone?); it is reported, never clobbered")
    if remote is None:
        warnings.append("could not list the group's projects; the counts below "
                        "come from the local tree only")

    return _preview(
        "clone_missing",
        f"Clone the projects of group '{group}' into '{target}'. "
        f"{len(existing)} repositories are already there; "
        f"{len(missing) if remote is not None else 'an unknown number'} "
        f"would be cloned.",
        warnings=warnings, group=group, managed_dir=dest, target=target,
        target_state=state, existing_count=len(existing),
        existing=_listed([os.path.relpath(p, target) for p in existing]),
        remote_project_count=None if remote is None else len(remote_paths),
        would_clone=_listed(missing),
        policy=("Resume mode: a project already cloned anywhere under the "
                "managed directory is skipped, never cloned twice."))


def do_clone_missing(ctx: Any, group: str) -> dict:
    """Clone the group's missing projects via ``clone.cmd_clone``."""
    if not isinstance(group, str) or not group.strip():
        raise ValueError("group must be a non-empty string")
    group = group.strip()
    dest = _managed_dir(ctx)

    _, output = ctx.run_captured(
        gl_clone.cmd_clone, _context_cfg(ctx), group, dest)

    target = gl_paths.resolve(group, dest, _context_cfg(ctx).get("folder"))
    repos = gl_pull._find_git_repos(target) if os.path.isdir(target) else []
    return {
        "action": "clone_missing",
        "ok": True,
        "group": group,
        "managed_dir": dest,
        "target": target,
        "repo_count": len(repos),
        "repos": _listed([os.path.relpath(p, target) for p in repos]),
        "output": output,
    }


# ─── Merge request notes ──────────────────────────────────────────────────────

def _mr_summary(mr: dict) -> dict:
    return {
        "iid": mr.get("iid"),
        "title": mr.get("title"),
        "state": mr.get("state"),
        "author": (mr.get("author") or {}).get("username"),
        "source_branch": mr.get("source_branch"),
        "target_branch": mr.get("target_branch"),
        "web_url": mr.get("web_url"),
    }


def preview_mr_comment(ctx: Any, project: str | int, iid: int,
                       body: str, allow_quick_actions: bool = False) -> dict:
    """Describe the plain comment that would be posted on a merge request.

    ``body`` in the preview is the exact text that will be sent: any quick
    action is already neutralised unless ``allow_quick_actions`` is ``True``.
    """
    iid = _require_iid(iid)
    body = _require_text(body, "body", MAX_BODY_CHARS)
    body, commands, warnings = _quick_action_gate(
        body, "comment", allow_quick_actions)
    mr = ctx.get(_mr_path(ctx, project, iid))

    if mr.get("state") in ("merged", "closed"):
        warnings.append(
            f"this merge request is already {mr.get('state')}; the comment is "
            f"still posted but nobody may be watching it")

    return _preview(
        "mr_comment",
        f"Post a comment ({len(body)} characters) on "
        f"{project}!{iid} \u2014 {mr.get('title')!r}.",
        danger=bool(commands), warnings=warnings, project=str(project),
        iid=iid, merge_request=_mr_summary(mr), body=body,
        quick_actions=commands,
        quick_actions_allowed=bool(commands) and allow_quick_actions is True,
        endpoint=f"POST {_mr_path(ctx, project, iid)}/notes")


def do_mr_comment(ctx: Any, project: str | int, iid: int, body: str,
                  allow_quick_actions: bool = False) -> dict:
    """Post a plain comment on a merge request (quick actions neutralised)."""
    iid = _require_iid(iid)
    body = _require_text(body, "body", MAX_BODY_CHARS)
    body, _, _ = _quick_action_gate(body, "comment", allow_quick_actions)
    note = ctx.post(f"{_mr_path(ctx, project, iid)}/notes", {"body": body})
    return {
        "action": "mr_comment", "ok": True, "project": str(project),
        "iid": iid, "note_id": (note or {}).get("id"),
        "created_at": (note or {}).get("created_at"),
    }


def build_review_position(ctx: Any, project: str | int, iid: int, path: str,
                          new_line: int | None = None,
                          old_line: int | None = None) -> tuple[dict, dict]:
    """(position payload, the diff refs it came from) for a positioned note.

    GitLab rejects a diff note whose ``base_sha``/``start_sha``/``head_sha``
    do not match a real version of the merge request, so the refs are read
    from the merge request itself (``diff_refs``) and, when that is absent,
    from its newest version.
    """
    path = _require_path(path)
    if new_line is None and old_line is None:
        raise ValueError(
            "a review comment needs a line: pass new_line (a line in the new "
            "file) or old_line (a line in the old file)")
    if new_line is not None:
        new_line = _require_iid(new_line, "new_line")
    if old_line is not None:
        old_line = _require_iid(old_line, "old_line")

    base = _mr_path(ctx, project, iid)
    mr = ctx.get(base)
    refs = mr.get("diff_refs") or {}
    source = "merge_request.diff_refs"
    if not all(refs.get(k) for k in ("base_sha", "start_sha", "head_sha")):
        versions = ctx.get(f"{base}/versions") or []
        newest = versions[0] if versions else {}
        refs = {
            "base_sha": newest.get("base_commit_sha"),
            "start_sha": newest.get("start_commit_sha"),
            "head_sha": newest.get("head_commit_sha"),
        }
        source = "merge_request.versions[0]"
    missing = [k for k in ("base_sha", "start_sha", "head_sha")
               if not refs.get(k)]
    if missing:
        raise ValueError(
            "cannot place a review comment: the merge request has no diff "
            f"refs ({', '.join(missing)} missing) \u2014 it may have no "
            "changes yet")

    position = {
        "position_type": "text",
        "base_sha": refs["base_sha"],
        "start_sha": refs["start_sha"],
        "head_sha": refs["head_sha"],
        "new_path": path,
        "old_path": path,
    }
    if new_line is not None:
        position["new_line"] = new_line
    if old_line is not None:
        position["old_line"] = old_line
    return position, {"refs_source": source, "merge_request": mr}


def preview_mr_review_comment(ctx: Any, project: str | int, iid: int,
                              body: str, path: str,
                              new_line: int | None = None,
                              old_line: int | None = None,
                              allow_quick_actions: bool = False) -> dict:
    """Describe the positioned diff note that would be posted."""
    iid = _require_iid(iid)
    body = _require_text(body, "body", MAX_BODY_CHARS)
    body, commands, warnings = _quick_action_gate(
        body, "review comment", allow_quick_actions)
    position, meta = build_review_position(
        ctx, project, iid, path, new_line, old_line)
    mr = meta["merge_request"]

    where = (f"new line {position['new_line']}" if "new_line" in position
             else f"old line {position['old_line']}")
    return _preview(
        "mr_review_comment",
        f"Start a review discussion on {project}!{iid} at {path}:{where} "
        f"({len(body)} characters).",
        danger=bool(commands), warnings=warnings,
        project=str(project), iid=iid, merge_request=_mr_summary(mr),
        body=body, quick_actions=commands,
        quick_actions_allowed=bool(commands) and allow_quick_actions is True,
        position=position, refs_source=meta["refs_source"],
        endpoint=f"POST {_mr_path(ctx, project, iid)}/discussions")


def do_mr_review_comment(ctx: Any, project: str | int, iid: int, body: str,
                         path: str, new_line: int | None = None,
                         old_line: int | None = None,
                         allow_quick_actions: bool = False) -> dict:
    """Post a positioned diff note (a review discussion) on a merge request."""
    iid = _require_iid(iid)
    body = _require_text(body, "body", MAX_BODY_CHARS)
    body, _, _ = _quick_action_gate(body, "review comment",
                                    allow_quick_actions)
    position, _ = build_review_position(
        ctx, project, iid, path, new_line, old_line)
    disc = ctx.post(f"{_mr_path(ctx, project, iid)}/discussions",
                    {"body": body, "position": position})
    notes = (disc or {}).get("notes") or []
    return {
        "action": "mr_review_comment", "ok": True, "project": str(project),
        "iid": iid, "discussion_id": (disc or {}).get("id"),
        "note_id": notes[0].get("id") if notes else None,
        "position": position,
    }


def preview_issue_comment(ctx: Any, project: str | int, iid: int,
                          body: str, allow_quick_actions: bool = False) -> dict:
    """Describe the comment that would be posted on an issue."""
    iid = _require_iid(iid)
    body = _require_text(body, "body", MAX_BODY_CHARS)
    body, commands, warnings = _quick_action_gate(
        body, "comment", allow_quick_actions)
    pid = ctx.project_id(project)
    issue = ctx.get(f"/projects/{pid}/issues/{iid}")

    if issue.get("state") == "closed":
        warnings.append("this issue is closed; the comment is still posted")

    return _preview(
        "issue_comment",
        f"Post a comment ({len(body)} characters) on {project}#{iid} "
        f"\u2014 {issue.get('title')!r}.",
        danger=bool(commands), warnings=warnings, project=str(project),
        iid=iid, quick_actions=commands,
        quick_actions_allowed=bool(commands) and allow_quick_actions is True,
        issue={"iid": issue.get("iid"), "title": issue.get("title"),
               "state": issue.get("state"), "web_url": issue.get("web_url")},
        body=body, endpoint=f"POST /projects/{pid}/issues/{iid}/notes")


def do_issue_comment(ctx: Any, project: str | int, iid: int,
                     body: str, allow_quick_actions: bool = False) -> dict:
    """Post a comment on an issue (quick actions neutralised by default)."""
    iid = _require_iid(iid)
    body = _require_text(body, "body", MAX_BODY_CHARS)
    body, _, _ = _quick_action_gate(body, "comment", allow_quick_actions)
    pid = ctx.project_id(project)
    note = ctx.post(f"/projects/{pid}/issues/{iid}/notes", {"body": body})
    return {
        "action": "issue_comment", "ok": True, "project": str(project),
        "iid": iid, "note_id": (note or {}).get("id"),
        "created_at": (note or {}).get("created_at"),
    }


# ─── Approvals ────────────────────────────────────────────────────────────────

def _approvals(ctx: Any, project: str | int, iid: int) -> dict:
    """Approval state of a merge request, or a note when it is unavailable."""
    raw = _get_optional(ctx, f"{_mr_path(ctx, project, iid)}/approvals")
    if not isinstance(raw, dict):
        return {"available": False,
                "note": "approvals are not reported by this instance"}
    return {
        "available": True,
        "required": raw.get("approvals_required"),
        "left": raw.get("approvals_left"),
        "approved_by": [
            (a.get("user") or {}).get("username")
            for a in (raw.get("approved_by") or []) if isinstance(a, dict)
        ],
        "user_has_approved": raw.get("user_has_approved"),
    }


def _approval_preview(ctx: Any, action: str, verb: str, project: str | int,
                      iid: int) -> dict:
    iid = _require_iid(iid)
    mr = ctx.get(_mr_path(ctx, project, iid))
    target = mr.get("target_branch")
    danger = is_protected_branch(target)
    warnings = [_danger_line(target)] if danger else []

    reasons = []
    if mr.get("state") != "opened":
        reasons.append(f"the merge request is {mr.get('state')}, not opened")
    head_sha = mr.get("sha")
    pinned: dict[str, Any] = {}
    if action == "mr_approve":
        # The approval is pinned to the head the human sees here: GitLab
        # answers 409 if the source branch moved before the confirmed call.
        if not head_sha:
            reasons.append("the merge request reports no head sha, so the "
                           "approval cannot be pinned to a known commit")
        pinned["sha"] = head_sha

    at = f" at {str(head_sha)[:12]}" if action == "mr_approve" and head_sha \
        else ""
    return _preview(
        action,
        f"{verb} {project}!{iid} \u2014 {mr.get('title')!r} "
        f"({mr.get('source_branch')} \u2192 {target}){at}.",
        ok=not reasons, danger=danger, reasons=reasons, warnings=warnings,
        project=str(project), iid=iid, merge_request=_mr_summary(mr),
        approvals=_approvals(ctx, project, iid), **pinned,
        endpoint=f"POST {_mr_path(ctx, project, iid)}/"
                 f"{'approve' if action == 'mr_approve' else 'unapprove'}")


def preview_mr_approve(ctx: Any, project: str | int, iid: int) -> dict:
    """Describe the approval that would be recorded."""
    return _approval_preview(ctx, "mr_approve", "Approve", project, iid)


def do_mr_approve(ctx: Any, project: str | int, iid: int,
                  sha: str | None = None) -> dict:
    """Approve a merge request, pinned to ``sha``.

    ``sha`` is required and comes from ``preview_mr_approve`` (the server pins
    it from the preview, exactly like ``mr_merge``). GitLab rejects the
    approval with 409 when it is no longer the head of the merge request, so a
    push made after the human read the preview is never approved.
    """
    iid = _require_iid(iid)
    if not isinstance(sha, str) or not sha.strip():
        raise ValueError(
            "sha is required: pass the 'sha' returned by preview_mr_approve so "
            "an approval of a commit nobody previewed is rejected")
    res = ctx.post(f"{_mr_path(ctx, project, iid)}/approve",
                   {"sha": sha.strip()})
    return {
        "action": "mr_approve", "ok": True, "project": str(project),
        "iid": iid, "sha": sha.strip(),
        "approvals_left": (res or {}).get("approvals_left"),
        "approvals_required": (res or {}).get("approvals_required"),
    }


def preview_mr_unapprove(ctx: Any, project: str | int, iid: int) -> dict:
    """Describe the approval that would be withdrawn."""
    return _approval_preview(ctx, "mr_unapprove", "Revoke the approval of",
                             project, iid)


def do_mr_unapprove(ctx: Any, project: str | int, iid: int) -> dict:
    """Withdraw your approval from a merge request."""
    iid = _require_iid(iid)
    ctx.post(f"{_mr_path(ctx, project, iid)}/unapprove")
    return {"action": "mr_unapprove", "ok": True, "project": str(project),
            "iid": iid}


# ─── mr_create ────────────────────────────────────────────────────────────────

def _branch_exists(ctx: Any, pid: int, branch: str) -> bool | None:
    """True/False, or None when the lookup itself failed."""
    res = _get_optional(
        ctx, f"/projects/{pid}/repository/branches/{quote(branch, safe='')}")
    if res is None:
        return None
    return bool(isinstance(res, dict) and res.get("name"))


def _draft_title(title: str, draft: bool) -> str:
    if not draft:
        return title
    return title if title.lower().startswith("draft:") else f"Draft: {title}"


def _mr_description(description: Any, allow_quick_actions: bool
                    ) -> tuple[str, list[str], list[str]]:
    """(description to send, quick actions found, warnings)."""
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise ValueError("description must be a string")
    if len(description) > MAX_BODY_CHARS:
        raise ValueError(f"description is {len(description)} characters; "
                         f"the limit is {MAX_BODY_CHARS}")
    return _quick_action_gate(description, "description", allow_quick_actions)


def preview_mr_create(ctx: Any, project: str | int, source_branch: str,
                      target_branch: str, title: str, description: str = "",
                      draft: bool = False,
                      allow_quick_actions: bool = False) -> dict:
    """Describe the merge request that would be opened.

    ``description`` in the preview is the exact text that will be sent (quick
    actions neutralised unless ``allow_quick_actions`` is ``True``), cut for
    display only past :data:`MAX_DESCRIPTION_DISPLAY_CHARS` with a marker.
    ``description_sha256`` is the hash of the full text; the confirmation
    token is bound to the full ``description`` argument itself.
    """
    source_branch = _require_branch(source_branch, "source_branch")
    target_branch = _require_branch(target_branch, "target_branch")
    title = _require_text(title, "title", MAX_TITLE_CHARS)
    to_send, commands, qa_warnings = _mr_description(
        description, allow_quick_actions)
    if source_branch == target_branch:
        raise ValueError("source_branch and target_branch are the same")

    pid = ctx.project_id(project)
    danger = is_protected_branch(target_branch) or bool(commands)
    warnings = ([_danger_line(target_branch)]
                if is_protected_branch(target_branch) else [])
    warnings.extend(qa_warnings)
    reasons: list[str] = []

    src = _branch_exists(ctx, pid, source_branch)
    tgt = _branch_exists(ctx, pid, target_branch)
    if src is False:
        reasons.append(f"source branch '{source_branch}' does not exist")
    if tgt is False:
        reasons.append(f"target branch '{target_branch}' does not exist")
    if src is None or tgt is None:
        warnings.append("could not confirm both branches exist")

    existing = _get_optional(ctx, f"/projects/{pid}/merge_requests",
                             {"source_branch": source_branch,
                              "target_branch": target_branch,
                              "state": "opened"}) or []
    duplicates = [{"iid": m.get("iid"), "title": m.get("title"),
                   "web_url": m.get("web_url")}
                  for m in existing if isinstance(m, dict)]
    if duplicates:
        warnings.append(
            f"{len(duplicates)} open merge request(s) already go from "
            f"'{source_branch}' to '{target_branch}'")

    final_title = _draft_title(title, draft)
    shown, cut = _display_text(to_send)
    return _preview(
        "mr_create",
        f"Open a merge request on {project}: '{source_branch}' \u2192 "
        f"'{target_branch}' titled {final_title!r}.",
        ok=not reasons, danger=danger, reasons=reasons, warnings=warnings,
        project=str(project), source_branch=source_branch,
        target_branch=target_branch, title=final_title, draft=bool(draft),
        description=shown, description_truncated_for_display=cut,
        description_chars=len(to_send),
        description_sha256=_sha256(to_send),
        quick_actions=commands,
        quick_actions_allowed=bool(commands) and allow_quick_actions is True,
        existing_open_merge_requests=duplicates,
        endpoint=f"POST /projects/{pid}/merge_requests")


def do_mr_create(ctx: Any, project: str | int, source_branch: str,
                 target_branch: str, title: str, description: str = "",
                 draft: bool = False, allow_quick_actions: bool = False,
                 description_sha256: str | None = None) -> dict:
    """Open a merge request.

    ``description_sha256`` is optional server-side pinning (from the preview):
    when given, the description about to be sent must hash to it.
    """
    source_branch = _require_branch(source_branch, "source_branch")
    target_branch = _require_branch(target_branch, "target_branch")
    title = _require_text(title, "title", MAX_TITLE_CHARS)
    to_send, _, _ = _mr_description(description, allow_quick_actions)
    if source_branch == target_branch:
        raise ValueError("source_branch and target_branch are the same")
    if description_sha256 is not None and \
            _sha256(to_send) != str(description_sha256).strip().lower():
        raise ValueError("the description no longer matches the previewed "
                         "description_sha256; preview again")

    pid = ctx.project_id(project)
    mr = ctx.post(f"/projects/{pid}/merge_requests", {
        "source_branch": source_branch,
        "target_branch": target_branch,
        "title": _draft_title(title, draft),
        "description": to_send,
    })
    return {
        "action": "mr_create", "ok": True, "project": str(project),
        "iid": (mr or {}).get("iid"), "web_url": (mr or {}).get("web_url"),
        "title": (mr or {}).get("title"), "state": (mr or {}).get("state"),
        "source_branch": source_branch, "target_branch": target_branch,
        "description_sha256": _sha256(to_send),
    }


# ─── mr_merge ─────────────────────────────────────────────────────────────────

def _mergeability(mr: dict) -> tuple[str, bool, str]:
    """(status string, whether GitLab would merge now, which field said so)."""
    detailed = (mr.get("detailed_merge_status") or "").strip()
    if detailed:
        return detailed, detailed in _MERGEABLE_DETAILED, "detailed_merge_status"
    legacy = (mr.get("merge_status") or "").strip()
    return legacy, legacy in _MERGEABLE_LEGACY, "merge_status"


def _head_pipeline(ctx: Any, project: str | int, iid: int, mr: dict) -> dict:
    """Status of the pipeline the merge would be gated on."""
    pipe = mr.get("head_pipeline")
    if not isinstance(pipe, dict) or not pipe:
        listed = _get_optional(ctx, f"{_mr_path(ctx, project, iid)}/pipelines")
        pipe = (listed or [{}])[0] if isinstance(listed, list) and listed else {}
    if not pipe:
        return {"status": None, "note": "no pipeline found for this merge request"}
    return {
        "id": pipe.get("id"),
        "status": pipe.get("status"),
        "sha": pipe.get("sha"),
        "web_url": pipe.get("web_url"),
    }


def preview_mr_merge(ctx: Any, project: str | int, iid: int,
                     squash: bool | None = None,
                     remove_source_branch: bool = False,
                     sha: str | None = None) -> dict:
    """Describe the merge, and refuse it when GitLab would not merge cleanly.

    The returned ``sha`` is the merge request's current head. ``do_mr_merge``
    requires it, so a merge confirmed against this preview is rejected by
    GitLab if someone pushes to the source branch in the meantime.
    """
    iid = _require_iid(iid)
    mr = ctx.get(_mr_path(ctx, project, iid))

    target = mr.get("target_branch")
    head_sha = mr.get("sha")
    state = mr.get("state")
    is_draft = bool(mr.get("draft", mr.get("work_in_progress", False)))
    conflicts = bool(mr.get("has_conflicts"))
    discussions_resolved = mr.get("blocking_discussions_resolved")
    status, mergeable, status_field = _mergeability(mr)
    if status == "conflict":
        conflicts = True

    danger = is_protected_branch(target)
    warnings = [_danger_line(target)] if danger else []
    reasons: list[str] = []

    if state != "opened":
        reasons.append(f"the merge request is {state}, not opened")
    if conflicts:
        reasons.append(f"the merge request has conflicts with '{target}'")
    if is_draft:
        reasons.append("the merge request is a draft")
    if discussions_resolved is False:
        reasons.append("blocking discussions are not resolved")
    if not mergeable:
        reasons.append(f"GitLab does not report it as mergeable "
                       f"({status_field}={status or 'unknown'})")
    if not head_sha:
        reasons.append("the merge request reports no head sha, so the merge "
                       "cannot be pinned to a known commit")

    pipeline = _head_pipeline(ctx, project, iid, mr)
    if pipeline.get("status") not in (None, "success"):
        warnings.append(f"head pipeline is '{pipeline.get('status')}'")
    approvals = _approvals(ctx, project, iid)
    if approvals.get("available") and (approvals.get("left") or 0):
        warnings.append(f"{approvals['left']} approval(s) still required")
    if sha and head_sha and sha != head_sha:
        warnings.append(f"the sha you passed ({sha[:12]}) is not the current "
                        f"head ({str(head_sha)[:12]})")

    return _preview(
        "mr_merge",
        f"Merge {project}!{iid} \u2014 {mr.get('title')!r} "
        f"('{mr.get('source_branch')}' \u2192 '{target}') at "
        f"{str(head_sha)[:12] or 'unknown'}.",
        ok=not reasons, danger=danger, reasons=reasons, warnings=warnings,
        project=str(project), iid=iid, merge_request=_mr_summary(mr),
        target_branch=target, source_branch=mr.get("source_branch"),
        sha=head_sha, state=state, merge_status=status,
        merge_status_field=status_field, mergeable=mergeable,
        has_conflicts=conflicts, draft=is_draft,
        blocking_discussions_resolved=discussions_resolved,
        head_pipeline=pipeline, approvals=approvals,
        squash=squash, remove_source_branch=bool(remove_source_branch),
        endpoint=f"PUT {_mr_path(ctx, project, iid)}/merge")


def do_mr_merge(ctx: Any, project: str | int, iid: int,
                squash: bool | None = None,
                remove_source_branch: bool = False,
                sha: str | None = None) -> dict:
    """Merge a merge request, pinned to ``sha``.

    ``sha`` is required and comes from ``preview_mr_merge``. GitLab refuses the
    merge when it is no longer the head of the source branch, which is the only
    protection against merging a commit nobody previewed.
    """
    iid = _require_iid(iid)
    if not isinstance(sha, str) or not sha.strip():
        raise ValueError(
            "sha is required: pass the 'sha' returned by preview_mr_merge so a "
            "source branch that moved since the preview is rejected")
    payload: dict[str, Any] = {
        "sha": sha.strip(),
        "should_remove_source_branch": bool(remove_source_branch),
    }
    if squash is not None:
        payload["squash"] = bool(squash)

    merged = ctx.put(f"{_mr_path(ctx, project, iid)}/merge", payload)
    return {
        "action": "mr_merge", "ok": True, "project": str(project), "iid": iid,
        "sha": payload["sha"], "state": (merged or {}).get("state"),
        "merge_commit_sha": (merged or {}).get("merge_commit_sha"),
        "squash_commit_sha": (merged or {}).get("squash_commit_sha"),
        "target_branch": (merged or {}).get("target_branch"),
        "web_url": (merged or {}).get("web_url"),
    }


# ─── create_branch ────────────────────────────────────────────────────────────

def preview_create_branch(ctx: Any, project: str | int, branch: str,
                          ref: str) -> dict:
    """Describe the branch that would be created."""
    branch = _require_branch(branch, "branch")
    ref = _require_branch(ref, "ref")
    pid = ctx.project_id(project)

    danger = is_protected_branch(branch)
    warnings = [_danger_line(branch)] if danger else []
    reasons: list[str] = []

    if _branch_exists(ctx, pid, branch):
        reasons.append(f"branch '{branch}' already exists")
    if _branch_exists(ctx, pid, ref) is False:
        # ref may legitimately be a tag or a commit sha, so this is a warning.
        warnings.append(f"'{ref}' is not a branch; it must be an existing tag "
                        f"or commit sha")

    return _preview(
        "create_branch",
        f"Create branch '{branch}' on {project} from '{ref}'.",
        ok=not reasons, danger=danger, reasons=reasons, warnings=warnings,
        project=str(project), branch=branch, ref=ref,
        endpoint=f"POST /projects/{pid}/repository/branches")


def do_create_branch(ctx: Any, project: str | int, branch: str,
                     ref: str) -> dict:
    """Create a branch from an existing ref."""
    branch = _require_branch(branch, "branch")
    ref = _require_branch(ref, "ref")
    pid = ctx.project_id(project)
    res = ctx.post(f"/projects/{pid}/repository/branches",
                   {"branch": branch, "ref": ref})
    return {
        "action": "create_branch", "ok": True, "project": str(project),
        "branch": (res or {}).get("name", branch), "ref": ref,
        "commit": ((res or {}).get("commit") or {}).get("id"),
        "web_url": (res or {}).get("web_url"),
    }
