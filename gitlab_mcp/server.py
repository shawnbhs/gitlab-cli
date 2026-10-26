"""FastMCP server exposing gitlab-cli over the Model Context Protocol.

Shape of the surface:

* **Read tools** (``gitlab_instances`` … ``gitlab_local_status``) call
  :mod:`gitlab_mcp.reporting` and execute immediately. Reading is free.
* **Write tools** (``gitlab_sync_local`` … ``gitlab_create_branch``) call
  :mod:`gitlab_mcp.actions` and are **two-call**. The first call returns a
  preview plus a single-use ``confirm_token`` and changes nothing; the agent
  must show that preview to the human and get an explicit approval before
  calling again with the token. A token is never minted and consumed in the
  same call.

Transport is stdio, which means **stdout belongs to the protocol**. Nothing in
this module prints. The gitlab-cli functions that do print are called through
``GitLabContext.run_captured``, which redirects and redacts their output.

Every tool returns a JSON-serialisable dict. Failures become
``{"status": "error", "error": <redacted>, "type": <exception class>}`` rather
than propagating a traceback, and long lists are capped with an explicit
``truncated``/``total`` pair.

``GITLAB_MCP_READ_ONLY=1`` in the environment makes every write tool refuse
without even building a preview.

The three collaborating modules are imported as modules, not as names, so a
test can patch ``gitlab_mcp.server.context.GitLabContext`` or
``gitlab_mcp.server.actions.do_mr_merge`` and have the tools see the patch.
"""

from __future__ import annotations

import contextlib
import inspect
import os
import re
from typing import Any, Callable, Iterable

from mcp.server.fastmcp import FastMCP

from gitlab_mcp import actions, confirm, context, reporting

__all__ = ["mcp", "main", "confirm_store", "reset_state", "scrub",
           "scrub_text", "READ_TOOL_LABEL", "WRITE_TOOL_LABEL"]

SERVER_NAME = "gitlab"

# Hard cap on how many rows a single tool result may carry. An agent that
# needs more should narrow the query; dumping 20k commits into a context
# window is never the right answer.
MAX_ITEMS = 500

# Environment flag that disables every write tool.
READ_ONLY_ENV = "GITLAB_MCP_READ_ONLY"

_CONFIRM_MESSAGE = (
    "Nothing has been changed yet. Show the preview above to the user "
    "verbatim, including every warning, and wait for their explicit approval. "
    "Only after they approve, call this same tool again with identical "
    "arguments plus confirm_token. Do not approve on the user's behalf and do "
    "not re-run this tool to obtain a fresh token."
)

# A user-facing message, not a secret (S105 matches on the name).
_REJECTED_TOKEN_REASON = (
    "the confirmation token is unknown, expired, already used, or was issued "  # noqa: S105
    "for different arguments; call the tool again without a token to get a "
    "fresh preview and have the user approve it"
)

mcp = FastMCP(SERVER_NAME)

#: Process-wide token store. Restarting the server invalidates every token.
confirm_store = confirm.ConfirmStore()

# One GitLabContext per instance name, built on first use. Building one reads
# the instance config and the token file, so it is not free.
_contexts: dict[str | None, Any] = {}


# ─── Plumbing ─────────────────────────────────────────────────────────────────

def reset_state() -> None:
    """Drop cached contexts and outstanding tokens (used by the tests)."""
    _contexts.clear()
    confirm_store.clear()


def _read_only() -> bool:
    """Whether writes are disabled. Read per call, never cached."""
    return context.read_only()


# ─── Secret scrubbing ─────────────────────────────────────────────────────────
#
# One scrubber for everything this package hands back to an agent or prints
# for a human: tool results, previews, error strings, exception text, and the
# CLI's error line (cli.py imports scrub_text from here).
#
# Two layers, because pattern matching alone is not enough:
#
# 1. **Literal secrets.** The token of every *configured* instance (resolved
#    the same way gitlab-cli resolves it: dedicated env var, custom env var,
#    the ~/.gitlab-cli/env file, legacy inline value) plus every
#    ``GITLAB_TOKEN*`` variable, plus each live context's own ``redact``.
#    This is what catches a CI job token or an OAuth token that looks like
#    nothing in particular — e.g. ``abcd1234secret``.
# 2. **Credential-shaped fragments**: ``gl*-`` tokens, URL userinfo
#    (``oauth2:…@``, ``token@``), ``PRIVATE-TOKEN: …``, ``JOB-TOKEN``,
#    ``Authorization: Bearer …``, bare ``Bearer …`` and token query params.
#
# Error text additionally goes through ``context.redact`` (which also masks
# generic ``password=…``/``secret=…`` shapes). Successful data does not,
# because that rule would mangle legitimate diffs and file contents.

SCRUBBED = "[REDACTED]"

#: Shortest literal secret that is replaced verbatim. Shorter strings would
#: corrupt ordinary text; no real GitLab credential is this short.
_MIN_SECRET_LEN = 6

_TOKEN_ENV_PREFIX = "GITLAB_TOKEN"  # noqa: S105 - an env-var prefix, not a secret

def _header_value(match: "re.Match[str]") -> str:
    """Keep the header name and auth scheme; destroy the credential."""
    scheme = (match.group(3) + " ") if match.group(3) else ""
    return f"{match.group(1)}{match.group(2)}{scheme}{SCRUBBED}"


#: Applied to every string handed back, data and errors alike.
_FRAGMENT_RULES: tuple[tuple[re.Pattern[str], Any], ...] = (
    # GitLab tokens of every flavour: glpat-, gldt-, glrt-, glcbt-, ...
    (re.compile(r"\bgl[a-z]{2,8}-[A-Za-z0-9_\-]{8,}"), "[REDACTED-TOKEN]"),
    # URL userinfo, both shapes: scheme://user:secret@ and scheme://tok@
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.\-]*://)(?!\[REDACTED)"
                r"[^/\s@\"'<>]+@"), r"\1[REDACTED-USERINFO]@"),
    # oauth2:<token>@ without a scheme (git remote output, logs).
    (re.compile(r"(?i)\b(oauth2|gitlab-ci-token|x-access-token):"
                r"(?!\[REDACTED)[^\s@/\"']+@"), r"\1:" + SCRUBBED + "@"),
    # Header values, including the repr of a headers dict.
    (re.compile(r"(?i)\b(PRIVATE-TOKEN|JOB-TOKEN|X-Gitlab-Token|"
                r"DEPLOY-TOKEN)([\"']?\s*[:=]\s*[\"']?)(?!\[REDACTED)"
                r"[^\s\"',;})\]]+"), r"\1\2" + SCRUBBED),
    (re.compile(r"(?i)\b(Authorization|Proxy-Authorization)"
                r"([\"']?\s*[:=]\s*[\"']?)"
                r"(?:(Bearer|Basic|Token|OAuth)\s+)?(?!\[REDACTED)"
                r"[^\s\"',;})\]]+"), _header_value),
    # Token in a query string.
    (re.compile(r"(?i)([?&](?:private[_-]?token|access[_-]?token|"
                r"job[_-]?token|token)=)(?!\[REDACTED)[^&\s\"'#]+"),
     r"\1" + SCRUBBED),
)

#: Applied to error text only: too aggressive for diffs and file contents.
_ERROR_ONLY_RULES: tuple[tuple[re.Pattern[str], Any], ...] = (
    # A bare bearer credential, wherever it appears.
    (re.compile(r"(?i)\b(Bearer)\s+(?!\[REDACTED)[A-Za-z0-9._~+/=\-]{8,}"),
     r"\1 " + SCRUBBED),
)


def _configured_secrets() -> set[str]:
    """Every token value this process could know about. Never returned.

    Read on each call (it is a small file read) so a token rotated with
    gitlab-cli while the server runs is still scrubbed. Any failure yields
    what was collected so far: scrubbing must never raise.
    """
    found: set[str] = set()
    for key, value in os.environ.items():
        if key.upper().startswith(_TOKEN_ENV_PREFIX) and value:
            found.add(value.strip())
    with contextlib.suppress(Exception):  # scrubbing must never raise
        from importlib import import_module
        creds = import_module("credentials")
        gl_instances = import_module("instances")
        for value in (creds.read_env_file() or {}).values():
            if value:
                found.add(str(value).strip())
        for inst in gl_instances.list_instances() or []:
            try:
                token = creds.resolve_token(inst, allow_generic=True)
            except Exception:                                # noqa: BLE001
                token = ""
            if token:
                found.add(str(token).strip())
    return {s for s in found if len(s) >= _MIN_SECRET_LEN}


def scrub_text(text: Any, ctxs: Iterable[Any] = (), *, error: bool = False,
               secrets: Iterable[str] | None = None) -> str:
    """Remove every known and every credential-shaped secret from ``text``.

    Args:
        text: Anything; it is converted with ``str()``.
        ctxs: Contexts whose own ``redact`` method should also run (each
            knows its literal token).
        error: Also apply the error-only rules and ``context.redact``
            (generic ``secret=…`` shapes). Use for error and exception text,
            not for result data.
        secrets: Literal secrets to remove; ``None`` means "look them up"
            (:func:`_configured_secrets`). :func:`scrub` passes them in so a
            large result costs one lookup, not one per string.

    Returns:
        The scrubbed string. Never raises.
    """
    out = "" if text is None else str(text)
    if not out:
        return out
    known = _configured_secrets() if secrets is None else secrets
    for secret in sorted(known, key=len, reverse=True):
        out = out.replace(secret, SCRUBBED)
    for ctx in ctxs or ():
        method = getattr(ctx, "redact", None)
        if callable(method):
            with contextlib.suppress(Exception):
                out = str(method(out))
    if error:
        # Before the local rules, so the context module sees raw text rather
        # than another redactor's placeholder.
        module_redact = getattr(context, "redact", None)
        if callable(module_redact):
            with contextlib.suppress(Exception):
                out = str(module_redact(out))
    rules = _FRAGMENT_RULES + (_ERROR_ONLY_RULES if error else ())
    for pattern, replacement in rules:
        out = pattern.sub(replacement, out)
    return out


def _live_contexts() -> list[Any]:
    """Every context this server has built (each knows its own token)."""
    return [c for c in list(_contexts.values()) if c is not None]


def scrub(value: Any, ctxs: Iterable[Any] | None = None) -> Any:
    """Recursively scrub a JSON-shaped tool result.

    Strings (including dict keys) are scrubbed, lists/tuples/dicts are walked,
    numbers/bools/None pass through, and any other object is rendered with
    ``str()`` and scrubbed rather than handed over with its repr intact.
    ``ctxs`` defaults to every context this server has built.
    """
    contexts_ = _live_contexts() if ctxs is None else list(ctxs)
    secrets = _configured_secrets()

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            return scrub_text(item, contexts_, secrets=secrets)
        if isinstance(item, dict):
            return {(walk(k) if isinstance(k, str) else k): walk(v)
                    for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [walk(v) for v in item]
        if isinstance(item, (bool, int, float)) or item is None:
            return item
        return scrub_text(item, contexts_, secrets=secrets)

    return walk(value)


def _redact(text: str) -> str:
    """Scrub error text: every layer, including ``context.redact``."""
    return scrub_text(text, _live_contexts(), error=True)


def _ctx(instance: str | None) -> Any:
    """The cached :class:`GitLabContext` for ``instance`` (None = active)."""
    key = instance or None
    if key not in _contexts:
        _contexts[key] = context.GitLabContext(key)
    return _contexts[key]


def _error(exc: BaseException) -> dict:
    """Uniform, scrubbed error result. Never carries a traceback or a token.

    ``str(exc)`` is used, falling back to ``repr(exc)`` and then the class
    name; whichever is used is scrubbed with every literal token known to
    the process, so a non-``glpat`` token in exception text is removed too.
    """
    text = str(exc) or repr(exc) or exc.__class__.__name__
    return {
        "status": "error",
        "error": _redact(text) or exc.__class__.__name__,
        "type": exc.__class__.__name__,
    }


def _capped(value: Any) -> Any:
    """Cap a list result and wrap it in a dict with ``total``/``truncated``."""
    if isinstance(value, list):
        return {
            "items": value[:MAX_ITEMS],
            "total": len(value),
            "truncated": len(value) > MAX_ITEMS,
        }
    return value


def _read(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a reporting call, capping lists and converting failures to errors."""
    try:
        return scrub(_capped(fn(*args, **kwargs)))
    except Exception as exc:                                 # noqa: BLE001
        return _error(exc)


def _report(fn: Callable[..., Any], instance: str | None, *args: Any,
            **kwargs: Any) -> Any:
    """Same as :func:`_read`, but builds the context inside the error guard.

    Resolving an instance can fail (unknown name, missing token), and that
    failure must come back as an error result rather than as an exception
    escaping the tool.
    """
    try:
        return scrub(_capped(fn(_ctx(instance), *args, **kwargs)))
    except Exception as exc:                                 # noqa: BLE001
        return _error(exc)


def _as_list(value: Any) -> list[str]:
    """Accept either a list of names or a comma-separated string."""
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


# ─── The write path ───────────────────────────────────────────────────────────

def _merge_sha(preview: dict) -> dict:
    """Server-side state pinned at preview time for ``mr_merge``/``mr_approve``.

    The head sha goes into the token's ``extra`` rather than into the tool
    arguments, so the agent cannot swap it between the preview and the
    confirmed call: GitLab rejects the merge or approval if the source branch
    moved after the human looked at it.
    """
    return {"sha": preview.get("sha")}


def _description_hash(preview: dict) -> dict:
    """Pin ``mr_create`` to the exact description text the human previewed."""
    digest = preview.get("description_sha256")
    return {"description_sha256": digest} if digest else {}


def _write(name: str, args: dict[str, Any], instance: str | None,
           confirm_token: str | None,
           pin: Callable[[dict], dict] | None = None) -> dict:
    """:func:`_write_unscrubbed`, with every byte of the result scrubbed.

    Previews, ``done`` results and rejection reasons all pass through
    :func:`scrub`, so a token echoed by GitLab or by gitlab-cli inside a
    successful result is removed exactly like one inside an exception.
    """
    return scrub(_write_unscrubbed(name, args, instance, confirm_token, pin))


def _write_unscrubbed(name: str, args: dict[str, Any], instance: str | None,
                      confirm_token: str | None,
                      pin: Callable[[dict], dict] | None = None) -> dict:
    """Run one mutating tool through the two-call confirmation flow.

    Args:
        name: Action name; ``actions.preview_<name>`` and ``actions.do_<name>``
            must exist.
        args: The tool arguments, exactly as they will be passed to the action.
        instance: GitLab instance name, part of what the token is bound to.
        confirm_token: ``None`` on the first call, the issued token on the
            second.
        pin: Optional function mapping the preview to extra keyword arguments
            that only the server may supply to the doer.

    Returns:
        ``needs_confirmation`` (first call), ``done`` (confirmed call),
        ``rejected`` (read-only, unusable preview, bad token) or ``error``.
    """
    if _read_only():
        return {
            "status": "rejected",
            "reason": (f"{READ_ONLY_ENV} is set: this server is in read-only "
                       f"mode and will not perform '{name}'"),
        }

    bound = dict(args)
    bound["instance"] = instance

    try:
        ctx = _ctx(instance)
        if confirm_token:
            ok, extra = confirm_store.check(name, bound, confirm_token)
            if not ok:
                return {"status": "rejected",
                        "reason": _REJECTED_TOKEN_REASON}
            doer = getattr(actions, f"do_{name}")
            call = dict(args)
            call.update(extra or {})
            return {"status": "done", "result": doer(ctx, **call)}

        preview = getattr(actions, f"preview_{name}")(ctx, **args)
    except Exception as exc:                                 # noqa: BLE001
        return _error(exc)

    if not isinstance(preview, dict):
        return {"status": "error",
                "error": f"preview_{name} did not return a preview dict",
                "type": "TypeError"}
    if not preview.get("ok", True):
        return {
            "status": "rejected",
            "reason": "; ".join(preview.get("reasons") or
                                ["the preview refused this operation"]),
            "preview": preview,
        }

    try:
        issued = confirm_store.issue(name, bound,
                                     pin(preview) if pin else None)
    except Exception as exc:                                 # noqa: BLE001
        # Human mode writes a token file; a refused or unwritable directory
        # must fail closed as an error, never fall back to agent mode.
        return _error(exc)
    result = {
        "status": "needs_confirmation",
        "preview": preview,
        "expires_in": issued["expires_in"],
        "danger": bool(preview.get("danger")),
        "confirm_mode": issued.get("confirm_mode", "agent"),
    }
    if issued.get("confirm_token"):
        # Agent mode: the token comes back to the caller.
        result["confirm_token"] = issued["confirm_token"]
        result["message"] = _CONFIRM_MESSAGE
    else:
        # Human mode (GITLAB_MCP_CONFIRM_MODE=human): the token is only in a
        # private file the user must open; the agent gets the path and the
        # instructions, never the token.
        result["token_file"] = issued.get("token_file")
        result["message"] = issued.get("message") or _CONFIRM_MESSAGE
    return result


# ─── Tool registration ────────────────────────────────────────────────────────
#
# Every tool description starts with a fixed, machine-checkable label so an
# agent (and the human reading the client's tool list) can tell at a glance
# whether calling it can change anything.

READ_TOOL_LABEL = ("[READ-ONLY tool] Changes nothing on GitLab or on disk; "
                   "no confirm_token is needed.")
WRITE_TOOL_LABEL = (
    "[WRITE tool] Changes GitLab or the local clones. REQUIRES "
    "confirm_token: the first call (without confirm_token) only returns a "
    "preview and a single-use confirm_token; show the preview to the user, "
    "get their explicit approval, then call again with identical arguments "
    "plus that confirm_token. With GITLAB_MCP_CONFIRM_MODE=human the token "
    "is not returned to you: the result names a private file and the user "
    "must paste the token from it. Never confirm on the user's behalf.")


def _described(label: str, fn: Callable[..., Any]) -> str:
    """``label`` followed by the function's own (dedented) docstring."""
    doc = inspect.cleandoc(fn.__doc__ or "")
    return f"{label}\n\n{doc}" if doc else label


def _read_tool(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a read tool whose description carries :data:`READ_TOOL_LABEL`."""
    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        return mcp.tool(name=name,
                        description=_described(READ_TOOL_LABEL, fn))(fn)
    return register


def _write_tool(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a write tool whose description carries :data:`WRITE_TOOL_LABEL`."""
    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        return mcp.tool(name=name,
                        description=_described(WRITE_TOOL_LABEL, fn))(fn)
    return register


# ─── Read tools ───────────────────────────────────────────────────────────────

@_read_tool("gitlab_instances")
def _tool_instances() -> dict:
    """List the configured GitLab instances (name, URL, which one is active).

    Never returns tokens. Use the returned name as the `instance` argument of
    any other tool to work against a non-active instance.
    """
    lister = getattr(context, "list_instances_public", None)
    if not callable(lister):
        lister = getattr(context, "list_instance_names", reporting.instances)
    return _read(lister)


@_read_tool("gitlab_groups")
def _tool_groups(search: str | None = None, limit: int = 100,
                 instance: str | None = None) -> dict:
    """List GitLab groups, optionally filtered by a search string."""
    return _report(reporting.groups, instance, search=search, limit=limit)


@_read_tool("gitlab_projects")
def _tool_projects(group: str | None = None, search: str | None = None,
                   limit: int = 200, instance: str | None = None) -> dict:
    """List projects, optionally within a group (subgroups included)."""
    return _report(reporting.projects, instance, group=group,
                   search=search, limit=limit)


@_read_tool("gitlab_project_overview")
def _tool_project_overview(project: str, since: str = "30d",
                           instance: str | None = None) -> dict:
    """Overview of one project: default branch, last activity, commit count
    since `since`, open merge requests, top contributors and languages.

    `project` is a path like "group-a/project-a" or a numeric project id.
    `since` is "YYYY-MM-DD" or a relative span like "7d", "2w", "1m", "today".
    """
    return _report(reporting.project_overview, instance, project,
                   since=since)


@_read_tool("gitlab_commits")
def _tool_commits(project: str, since: str = "7d", until: str | None = None,
                  author: str | None = None, branch: str | None = None,
                  with_stats: bool = True, limit: int = 200,
                  instance: str | None = None) -> dict:
    """List commits of a project with per-commit line statistics.

    Rows carry sha, title, author name and email, date, additions, deletions
    and total. Set `with_stats` false for a much cheaper call that omits the
    line counts.
    """
    return _report(reporting.commits, instance, project, since=since,
                   until=until, author=author, branch=branch,
                   with_stats=with_stats, limit=limit)


@_read_tool("gitlab_commit_detail")
def _tool_commit_detail(project: str, sha: str, max_diff_chars: int = 60000,
                        instance: str | None = None) -> dict:
    """Full detail of one commit: message, stats and per-file diffs.

    Diffs are truncated at `max_diff_chars` characters in total.
    """
    return _report(reporting.commit_detail, instance, project, sha,
                   max_diff_chars=max_diff_chars)


@_read_tool("gitlab_compare")
def _tool_compare(project: str, base: str, head: str,
                  max_diff_chars: int = 60000,
                  instance: str | None = None) -> dict:
    """Compare two refs (branches, tags or shas) and return the diff.

    `base` and `head` are refs; the result lists the commits and changed files
    between them, with diffs truncated at `max_diff_chars`.
    """
    return _report(reporting.compare, instance, project, base, head,
                   max_diff_chars=max_diff_chars)


@_read_tool("gitlab_contributors")
def _tool_contributors(project: str, since: str = "30d",
                       branch: str | None = None,
                       instance: str | None = None) -> dict:
    """Per-author contribution summary for a project.

    For each author: commit count, lines added and removed, and first and last
    commit date in the window, computed from the commits themselves.
    """
    return _report(reporting.contributors, instance, project, since=since,
                   branch=branch)


@_read_tool("gitlab_person_report")
def _tool_person_report(people: list[str] | str, since: str = "7d",
                        until: str | None = None,
                        projects: list[str] | str | None = None,
                        max_projects: int = 50,
                        instance: str | None = None) -> dict:
    """What specific people did on GitLab in a time window.

    `people` is a list of usernames or a comma-separated string. Per person the
    report covers events (pushes, merge requests opened and merged, comments,
    issues), per-project commit counts and lines changed, the projects they
    touched, and the merge requests they authored with state. The result states
    its own method and limitations — read them before quoting numbers.
    """
    return _report(reporting.person_report, instance, _as_list(people),
                   since=since, until=until,
                   projects=_as_list(projects) or None,
                   max_projects=max_projects)


@_read_tool("gitlab_project_progress")
def _tool_project_progress(project: str, since: str = "30d",
                           instance: str | None = None) -> dict:
    """Progress of a project over time: commits per week and per author,
    merge requests merged and still open, pipeline success rate, latest tags.
    """
    return _report(reporting.project_progress, instance, project,
                   since=since)


@_read_tool("gitlab_merge_requests")
def _tool_merge_requests(project: str | None = None, group: str | None = None,
                         state: str = "opened", author: str | None = None,
                         since: str | None = None, limit: int = 100,
                         instance: str | None = None) -> dict:
    """List merge requests for a project or a whole group.

    `state` is "opened", "closed", "merged" or "all".
    """
    return _report(reporting.merge_requests, instance, project=project,
                   group=group, state=state, author=author, since=since,
                   limit=limit)


@_read_tool("gitlab_merge_request")
def _tool_merge_request(project: str, iid: int, with_diff: bool = True,
                        max_diff_chars: int = 80000,
                        instance: str | None = None) -> dict:
    """One merge request in full, for code review.

    Includes description, discussions, approvals, pipeline status and the
    changed files with their diffs (truncated at `max_diff_chars`). `iid` is
    the per-project merge request number shown as "!123".
    """
    return _report(reporting.merge_request, instance, project, iid,
                   with_diff=with_diff, max_diff_chars=max_diff_chars)


@_read_tool("gitlab_pipelines")
def _tool_pipelines(project: str, ref: str | None = None, limit: int = 20,
                    instance: str | None = None) -> dict:
    """List recent CI pipelines of a project, newest first."""
    return _report(reporting.pipelines, instance, project, ref=ref,
                   limit=limit)


@_read_tool("gitlab_branches")
def _tool_branches(project: str, search: str | None = None,
                   instance: str | None = None) -> dict:
    """List the branches of a project, optionally filtered by `search`."""
    return _report(reporting.branches, instance, project, search=search)


@_read_tool("gitlab_users")
def _tool_users(search: str, limit: int = 20,
                instance: str | None = None) -> dict:
    """Search GitLab users by username, name or email fragment."""
    return _report(reporting.users, instance, search, limit=limit)


@_read_tool("gitlab_file_content")
def _tool_file_content(project: str, path: str, ref: str | None = None,
                       max_chars: int = 100000,
                       instance: str | None = None) -> dict:
    """Read one file from a project at a ref (default: the default branch).

    `path` is repository-relative. Content is truncated at `max_chars`.
    """
    return _report(reporting.file_content, instance, project, path,
                   ref=ref, max_chars=max_chars)


@_read_tool("gitlab_local_status")
def _tool_local_status(group: str | None = None,
                       instance: str | None = None) -> dict:
    """State of the local clones under this instance's managed directory.

    Per clone: path, current branch, whether it is dirty, and how far
    ahead/behind its upstream it is. Nothing is fetched and nothing is changed.
    """
    return _report(reporting.local_status, instance, group=group)


# ─── Write tools (two-call confirmation) ──────────────────────────────────────

@_write_tool("gitlab_sync_local")
def _tool_sync_local(group: str | None = None, project: str | None = None,
                     instance: str | None = None,
                     confirm_token: str | None = None) -> dict:
    """Fetch and fast-forward the local clones. REQUIRES USER CONFIRMATION.

    Call once without `confirm_token` to get a preview of exactly which
    repositories would be pulled; show it to the user, get their explicit
    approval, then call again with identical arguments plus the token. A repo
    that is dirty, ahead, diverged or detached is reported and left untouched —
    nothing is ever merged, rebased or reset.
    """
    return _write("sync_local", {"group": group, "project": project},
                  instance, confirm_token)


@_write_tool("gitlab_clone_missing")
def _tool_clone_missing(group: str, instance: str | None = None,
                        confirm_token: str | None = None) -> dict:
    """Clone a group's missing projects into the managed directory.
    REQUIRES USER CONFIRMATION.

    Call once without `confirm_token` for a preview listing what would be
    cloned and where, get the user's explicit approval, then call again with
    the token. Already-cloned projects are skipped, never clobbered.
    """
    return _write("clone_missing", {"group": group}, instance, confirm_token)


@_write_tool("gitlab_mr_comment")
def _tool_mr_comment(project: str, iid: int, body: str,
                     allow_quick_actions: bool = False,
                     instance: str | None = None,
                     confirm_token: str | None = None) -> dict:
    """Post a plain comment on a merge request. REQUIRES USER CONFIRMATION.

    Call once without `confirm_token`, show the user the preview with the exact
    comment text, get their explicit approval, then call again with identical
    arguments plus the token. Editing the body invalidates the token.

    Lines starting with a GitLab quick action (``/merge``, ``/approve``,
    ``/close``, ...) are neutralised and posted as text unless
    `allow_quick_actions` is true; set it only when the user explicitly asks
    for the quick actions to run, because GitLab executes them with no
    further preview.
    """
    return _write("mr_comment",
                  {"project": project, "iid": iid, "body": body,
                   "allow_quick_actions": allow_quick_actions},
                  instance, confirm_token)


@_write_tool("gitlab_mr_review_comment")
def _tool_mr_review_comment(project: str, iid: int, body: str, path: str,
                            new_line: int | None = None,
                            old_line: int | None = None,
                            allow_quick_actions: bool = False,
                            instance: str | None = None,
                            confirm_token: str | None = None) -> dict:
    """Start a review discussion on a specific line of a merge request diff.
    REQUIRES USER CONFIRMATION.

    Pass `new_line` for a line in the new file or `old_line` for a line in the
    old file; at least one is required. Call once without `confirm_token` for a
    preview, get the user's explicit approval, then call again with the token.

    Lines starting with a GitLab quick action (``/merge``, ``/approve``,
    ``/close``, ...) are neutralised and posted as text unless
    `allow_quick_actions` is true; set it only when the user explicitly asks
    for the quick actions to run, because GitLab executes them with no
    further preview.
    """
    return _write("mr_review_comment",
                  {"project": project, "iid": iid, "body": body, "path": path,
                   "new_line": new_line, "old_line": old_line,
                   "allow_quick_actions": allow_quick_actions},
                  instance, confirm_token)


@_write_tool("gitlab_mr_approve")
def _tool_mr_approve(project: str, iid: int, instance: str | None = None,
                     confirm_token: str | None = None) -> dict:
    """Record your approval of a merge request. REQUIRES USER CONFIRMATION.

    Approving is a human judgement, never an agent's. Call once without
    `confirm_token` for a preview showing the merge request, its target branch
    and the current approval state, get the user's explicit approval, then call
    again with the token. The approval is pinned server-side to the head sha
    shown in the preview, so a push made after the user looked is not
    approved.
    """
    return _write("mr_approve", {"project": project, "iid": iid}, instance,
                  confirm_token, pin=_merge_sha)


@_write_tool("gitlab_mr_unapprove")
def _tool_mr_unapprove(project: str, iid: int, instance: str | None = None,
                       confirm_token: str | None = None) -> dict:
    """Withdraw your approval from a merge request.
    REQUIRES USER CONFIRMATION.

    Call once without `confirm_token` for a preview, get the user's explicit
    approval, then call again with identical arguments plus the token.
    """
    return _write("mr_unapprove", {"project": project, "iid": iid}, instance,
                  confirm_token)


@_write_tool("gitlab_mr_create")
def _tool_mr_create(project: str, source_branch: str, target_branch: str,
                    title: str, description: str = "", draft: bool = False,
                    allow_quick_actions: bool = False,
                    instance: str | None = None,
                    confirm_token: str | None = None) -> dict:
    """Open a merge request. REQUIRES USER CONFIRMATION.

    Call once without `confirm_token` for a preview showing source and target
    branch, the final title, and any existing open merge request between the
    same branches; get the user's explicit approval, then call again with the
    token. A protected target branch (main, master, production, prod,
    release*) is flagged `danger`. The preview carries the exact description
    that will be sent, and the confirmed call is pinned to its hash.

    Lines starting with a GitLab quick action (``/merge``, ``/approve``,
    ``/close``, ...) are neutralised and posted as text unless
    `allow_quick_actions` is true; set it only when the user explicitly asks
    for the quick actions to run, because GitLab executes them with no
    further preview.
    """
    return _write("mr_create",
                  {"project": project, "source_branch": source_branch,
                   "target_branch": target_branch, "title": title,
                   "description": description, "draft": draft,
                   "allow_quick_actions": allow_quick_actions},
                  instance, confirm_token, pin=_description_hash)


@_write_tool("gitlab_mr_merge")
def _tool_mr_merge(project: str, iid: int, squash: bool | None = None,
                   remove_source_branch: bool = False,
                   instance: str | None = None,
                   confirm_token: str | None = None) -> dict:
    """Merge a merge request. REQUIRES USER CONFIRMATION. The most destructive
    tool on this server.

    Call once without `confirm_token`. The preview shows the target branch,
    merge status, conflicts, draft state, head pipeline and approvals, and
    refuses outright when GitLab would not merge cleanly. Show the whole
    preview to the user, get their explicit approval, then call again with
    identical arguments plus the token.

    The commit to merge is not an argument: the merge is pinned server-side to
    the head sha recorded in the preview, so a source branch that moved after
    the user looked at it is rejected by GitLab instead of being merged
    silently.
    """
    return _write("mr_merge",
                  {"project": project, "iid": iid, "squash": squash,
                   "remove_source_branch": remove_source_branch},
                  instance, confirm_token, pin=_merge_sha)


@_write_tool("gitlab_issue_comment")
def _tool_issue_comment(project: str, iid: int, body: str,
                        allow_quick_actions: bool = False,
                        instance: str | None = None,
                        confirm_token: str | None = None) -> dict:
    """Post a comment on an issue. REQUIRES USER CONFIRMATION.

    Call once without `confirm_token`, show the user the preview with the exact
    comment text, get their explicit approval, then call again with identical
    arguments plus the token.

    Lines starting with a GitLab quick action (``/merge``, ``/approve``,
    ``/close``, ...) are neutralised and posted as text unless
    `allow_quick_actions` is true; set it only when the user explicitly asks
    for the quick actions to run, because GitLab executes them with no
    further preview.
    """
    return _write("issue_comment",
                  {"project": project, "iid": iid, "body": body,
                   "allow_quick_actions": allow_quick_actions},
                  instance, confirm_token)


@_write_tool("gitlab_create_branch")
def _tool_create_branch(project: str, branch: str, ref: str,
                        instance: str | None = None,
                        confirm_token: str | None = None) -> dict:
    """Create a branch from an existing ref. REQUIRES USER CONFIRMATION.

    Call once without `confirm_token` for a preview, get the user's explicit
    approval, then call again with identical arguments plus the token.
    """
    return _write("create_branch",
                  {"project": project, "branch": branch, "ref": ref},
                  instance, confirm_token)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    """Serve MCP over stdio. Prints nothing: stdout is the protocol."""
    mcp.run(transport="stdio")
