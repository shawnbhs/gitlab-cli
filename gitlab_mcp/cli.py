"""``gitlab-report`` — the manual bash CLI over the gitlab-mcp layer.

Everything the MCP server can do is reachable by hand from a shell, with the
same two-step shape for anything that mutates:

1. the ``preview_<action>`` is run and printed — no change has happened yet,
2. the human answers ``Proceed? [y/N]``,
3. only then does ``do_<action>`` run.

Design notes that are easy to get wrong later:

* **Reads** call ``gitlab_mcp.reporting.<fn>(ctx, ...)``; the context is
  ``gitlab_mcp.context.GitLabContext(instance)``. Both modules are imported
  lazily (``_reporting()``, ``_context()``) so this CLI stays importable — and
  unit-testable — while its siblings are written, and so tests can patch the
  real attributes rather than a copy captured at import time.
* **``--yes`` skips the question, never the disclosure.** The preview is
  printed either way. The one case ``--yes`` cannot skip is merging into a
  protected/production branch: that always asks, and it asks for the target
  branch name to be typed out, because "y" is a reflex and a branch name is a
  decision.
* A preview with ``ok=false`` is never offered for confirmation at all. Its
  reasons are printed and the process exits 1.
* Table output measures *display* width (``unicodedata.east_asian_width``), so
  a CJK title does not shear the column it sits in. Markdown output escapes
  ``|`` so a commit message cannot break out of its cell.

Exit codes: ``0`` success (including a declined confirmation — a decision, not
a failure), ``1`` error or refused preview, ``130`` Ctrl-C.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import unicodedata
from importlib import import_module
from typing import Any, Callable, Iterable, Sequence

__all__ = ["main", "build_parser", "render", "render_table", "render_markdown"]

PROG = "gitlab-report"

# ─── Terminal styling ─────────────────────────────────────────────────────────
# The same escape codes gitlab-cli's printer.py uses, so the two tools look
# like one tool. Colour is dropped when stdout is not a terminal or NO_COLOR
# is set, because a report that gets piped into a file should not carry escape
# sequences into it.

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"


def color_enabled(stream: Any = None) -> bool:
    """Whether to emit ANSI escapes on ``stream``."""
    if os.environ.get("NO_COLOR"):
        return False
    stream = stream or sys.stdout
    try:
        return bool(stream.isatty())
    except Exception:                                        # noqa: BLE001
        return False


def paint(text: str, code: str, stream: Any = None) -> str:
    """``text`` wrapped in ``code`` when the stream can show colour."""
    if not code or not color_enabled(stream):
        return text
    return f"{code}{text}{RESET}"


# ─── Secret redaction ─────────────────────────────────────────────────────────

_REDACT_RULES = (
    (re.compile(r"glpat-[A-Za-z0-9_\-]{6,}"), "glpat-***"),
    (re.compile(r"(?i)(private-token\s*[:=]\s*)\S+"), r"\1***"),
    (re.compile(r"(?i)(bearer\s+)\S+"), r"\1***"),
    (re.compile(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@"), r"\1***:***@"),
)


#: Every context this process built. Each knows its own literal token, and
#: :func:`redact` runs them all, so a token that matches no pattern (a CI job
#: or OAuth token) is still removed from what the CLI prints.
_CONTEXTS: list[Any] = []


def redact(text: str, *, error: bool = True) -> str:
    """Strip every secret out of ``text`` before it is printed.

    ``gitlab_mcp.server.scrub_text`` is the single authority (literal tokens
    of every configured instance, each live context's own token, and
    credential-shaped fragments) and is used whenever it can be imported.
    The local rules below exist only so an error raised *while* importing
    that module can still be printed safely; even then each live context's
    ``redact`` runs first.

    Args:
        text: Text to print.
        error: Apply the error-only rules too (default). Rendered report
            output passes ``False`` so diffs are not mangled.
    """
    out = str(text)
    with contextlib.suppress(Exception):
        return str(_module("server").scrub_text(out, list(_CONTEXTS),
                                                error=error))
    for ctx in list(_CONTEXTS):
        with contextlib.suppress(Exception):
            out = str(ctx.redact(out))
    with contextlib.suppress(Exception):
        out = str(_module("context").redact(out))
    for pattern, replacement in _REDACT_RULES:
        out = pattern.sub(replacement, out)
    return out


def scrub_data(value: Any) -> Any:
    """A display copy of ``value`` with every string passed through redact.

    Used for previews and results before they are printed; the original
    object (whose ``sha`` the merge relies on) is never modified.
    """
    with contextlib.suppress(Exception):
        return _module("server").scrub(value, list(_CONTEXTS))
    if isinstance(value, str):
        return redact(value, error=False)
    if isinstance(value, dict):
        return {k: scrub_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_data(v) for v in value]
    return value


# ─── Terminal safety ──────────────────────────────────────────────────────────
# MR titles, commit messages, user names, diffs and error text are written by
# whoever controls the GitLab data. Printed raw, an escape sequence inside them
# can recolour or erase lines, rewrite a preview, set the window title or
# write the clipboard (OSC 52). Every human-readable path strips them.

#: C0 controls except tab and newline, DEL, the C1 block (incl. 8-bit CSI
#: U+009B) and the bidi embedding/override/isolate characters.
_UNSAFE_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def terminal_safe(text: Any) -> str:
    """``text`` without terminal control characters; newline and tab kept."""
    return _UNSAFE_RE.sub("", "" if text is None else str(text))


def terminal_safe_line(text: Any) -> str:
    """Like :func:`terminal_safe`, with newline and tab folded to spaces."""
    return terminal_safe(text).replace("\n", " ").replace("\t", " ")


def _json_dumps(value: Any) -> str:
    """JSON with every control, C1 and bidi character escaped as ``\\uXXXX``.

    ``json.dumps`` already escapes C0; with ``ensure_ascii=False`` it leaves
    C1 and bidi characters raw, so they are escaped here. The data is
    unchanged for any JSON parser.
    """
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return _UNSAFE_RE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


# ─── Lazy module access ───────────────────────────────────────────────────────

def _module(name: str) -> Any:
    """Import ``gitlab_mcp.<name>`` at call time.

    Call time, not import time, for two reasons: this CLI must import even
    while ``reporting``/``context`` are still being written, and a test that
    injects a stub into ``sys.modules`` must be seen.
    """
    return import_module(f"gitlab_mcp.{name}")


def _reporting() -> Any:
    return _module("reporting")


def _actions() -> Any:
    return _module("actions")


def _context() -> Any:
    return _module("context")


def make_context(instance: str | None) -> Any:
    """``GitLabContext`` for ``instance`` (``None`` = the active instance).

    The context is remembered so :func:`redact` can remove its literal token
    from anything printed later.
    """
    ctx = _context().GitLabContext(instance)
    _CONTEXTS.append(ctx)
    return ctx


# ─── Width-aware rendering ────────────────────────────────────────────────────

def display_width(text: Any) -> int:
    """Printed width of ``text`` in terminal cells.

    East-Asian wide and fullwidth characters occupy two cells; combining marks
    occupy none. ``len()`` is wrong for both.
    """
    total = 0
    for char in str(text):
        if unicodedata.combining(char):
            continue
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return total


def _cell(value: Any) -> str:
    """One table cell: a single line, never ``None``."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(_cell(v) for v in value)
    if isinstance(value, dict):
        return terminal_safe_line(json.dumps(value, ensure_ascii=False, default=str))
    return terminal_safe_line(value)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def render_table(columns: Sequence[str], rows: Iterable[dict],
                 title: str | None = None) -> str:
    """A plain-text table whose columns line up for non-ASCII values too."""
    columns = list(columns)
    body = [[_cell(row.get(col)) for col in columns] for row in rows]
    widths = [display_width(col) for col in columns]
    for line in body:
        for i, text in enumerate(line):
            widths[i] = max(widths[i], display_width(text))

    out: list[str] = []
    if title:
        out.append(terminal_safe_line(title))
    out.append("  ".join(_pad(col, widths[i]) for i, col in enumerate(columns)))
    out.append("  ".join("─" * w for w in widths))
    out.extend("  ".join(_pad(t, widths[i]) for i, t in enumerate(line))
               for line in body)
    if not body:
        out.append("(no rows)")
    return "\n".join(out)


def _md_cell(value: Any) -> str:
    """A markdown table cell: pipes escaped, newlines flattened."""
    return _cell(value).replace("\\", "\\\\").replace("|", "\\|")


def render_markdown(columns: Sequence[str], rows: Iterable[dict],
                    title: str | None = None) -> str:
    """A GitHub-flavoured markdown table."""
    columns = list(columns)
    out: list[str] = []
    if title:
        out.append(f"## {terminal_safe_line(title)}\n")
    out.append("| " + " | ".join(_md_cell(c) for c in columns) + " |")
    out.append("| " + " | ".join("---" for _ in columns) + " |")
    for row in rows:
        out.append("| " + " | ".join(
            _md_cell(row.get(col)) for col in columns) + " |")
    return "\n".join(out)


def _tabular(result: Any) -> tuple[list[str], list[dict]] | None:
    """``(columns, rows)`` when ``result`` is naturally a table, else None."""
    if isinstance(result, list):
        if all(isinstance(item, dict) for item in result):
            columns: list[str] = []
            for row in result:
                for key in row:
                    if key not in columns:
                        columns.append(key)
            return columns, list(result)
        return None
    if isinstance(result, dict):
        rows = result.get("rows")
        if isinstance(rows, list) and all(isinstance(r, dict) for r in rows):
            columns = list(result.get("columns") or [])
            if not columns:
                for row in rows:
                    for key in row:
                        if key not in columns:
                            columns.append(key)
            return columns, rows
    return None


def _scalar_rows(data: dict) -> list[dict]:
    return [{"field": k, "value": _cell(v)} for k, v in data.items()
            if not isinstance(v, (list, dict))]


def _nested_tables(data: dict) -> list[tuple[str, list[str], list[dict]]]:
    """Sub-tables for every list-of-dicts value in ``data``."""
    out = []
    for key, value in data.items():
        if key == "rows":
            continue
        table = _tabular(value)
        if table and table[1]:
            out.append((key, table[0], table[1]))
    return out


def render(result: Any, fmt: str = "table", title: str | None = None) -> str:
    """Render a reporting/action result in ``fmt`` (``table``/``md``/``json``)."""
    if fmt == "json":
        return _json_dumps(result)

    renderer = render_markdown if fmt == "md" else render_table
    table = _tabular(result)
    if table:
        columns, rows = table
        heading = title or (result.get("title")
                            if isinstance(result, dict) else None)
        chunks = [renderer(columns, rows, heading)]
        if isinstance(result, dict):
            summary = result.get("summary")
            if isinstance(summary, dict) and summary:
                chunks.append("")
                chunks.append(renderer(["field", "value"],
                                       _scalar_rows(summary), "summary"))
        return "\n".join(chunks)

    if isinstance(result, dict):
        chunks = [renderer(["field", "value"], _scalar_rows(result), title)]
        for key, columns, rows in _nested_tables(result):
            chunks.append("")
            chunks.append(renderer(columns, rows, key))
        return "\n".join(chunks)

    if isinstance(result, list):
        return "\n".join(_cell(item) for item in result) or "(empty)"
    return _cell(result)


# ─── Output ───────────────────────────────────────────────────────────────────

def write_output(text: str, out_path: str | None,
                 stream: Any = None) -> None:
    """Print ``text``, or write it to ``out_path``.

    A path that names an existing directory is refused rather than guessed at,
    and the parent directory must already exist — silently creating a tree
    because of a typo is how reports end up where nobody looks for them.
    """
    stream = stream or sys.stdout
    text = terminal_safe(redact(text, error=False))
    if not out_path:
        print(text, file=stream)
        return
    target = os.path.abspath(os.path.expanduser(out_path))
    if os.path.isdir(target):
        raise ValueError(f"--out '{out_path}' is a directory")
    parent = os.path.dirname(target) or "."
    if not os.path.isdir(parent):
        raise ValueError(f"--out directory does not exist: {parent}")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(text if text.endswith("\n") else text + "\n")
    print(f"wrote {target}", file=stream)


def _resolve_format(args: argparse.Namespace) -> str:
    """``--json`` is shorthand for ``--format json``; ``--format`` wins ties."""
    if getattr(args, "format", None):
        return args.format
    if getattr(args, "json", False):
        return "json"
    return "table"


# ─── Confirmation ─────────────────────────────────────────────────────────────

def _ask(prompt: str) -> str:
    """Read one answer from the human. Patched in tests."""
    return input(prompt)


def _print_preview(preview: dict, stream: Any = None) -> None:
    """Print a preview: what would happen, loudly if it is dangerous."""
    stream = stream or sys.stdout
    action = terminal_safe_line(preview.get("action", "action"))
    print(paint(f"── preview: {action} ──", BOLD, stream), file=stream)
    print(terminal_safe(preview.get("summary", "")), file=stream)

    if preview.get("danger"):
        target = (preview.get("target_branch") or preview.get("branch")
                  or preview.get("project") or action)
        target = terminal_safe_line(target)
        # The sentence is uppercase; the target is not, because a branch name
        # is case-sensitive and shouting it wrong makes it unverifiable.
        line = (f"!! DANGER: '{target}' IS A PROTECTED/PRODUCTION TARGET — "
                f"THIS CHANGE AFFECTS EVERYONE !!")
        print(paint(line, BOLD + RED, stream), file=stream)

    for warning in preview.get("warnings") or []:
        print(paint(f"warning: {terminal_safe_line(warning)}", YELLOW, stream),
              file=stream)

    detail = {k: v for k, v in preview.items()
              if k not in ("action", "ok", "danger", "summary", "reasons",
                           "warnings")
              and not isinstance(v, (list, dict))}
    for key, value in detail.items():
        print(f"  {terminal_safe_line(key)}: {_cell(value)}", file=stream)


def _print_reasons(preview: dict, stream: Any = None) -> None:
    stream = stream or sys.stderr
    print(paint("refused — this will not run:", BOLD + RED, stream),
          file=stream)
    for reason in preview.get("reasons") or ["the preview reported ok=false"]:
        print(f"  - {terminal_safe_line(reason)}", file=stream)


def confirm_write(command: str, preview: dict, assume_yes: bool,
                  stream: Any = None) -> bool:
    """Ask the human whether to proceed. ``False`` means do nothing.

    ``--yes`` (``assume_yes``) skips the question for every action except a
    merge into a protected branch, which always asks and requires the target
    branch name to be typed exactly. That exception is the whole point of the
    flag's existence being safe: the cheap confirmations can be scripted, the
    irreversible one cannot.
    """
    stream = stream or sys.stdout
    if command == "mr-merge" and preview.get("danger"):
        target = str(preview.get("target_branch") or "").strip()
        if not target:
            print("error: the preview reports no target branch to confirm",
                  file=sys.stderr)
            return False
        shown = terminal_safe_line(target)
        print(paint(f"This merges into '{shown}'. --yes does not apply here.",
                    BOLD, stream), file=stream)
        answer = _ask(f"Type the target branch name ('{shown}') to confirm: ")
        if (answer or "").strip() != target:
            print("branch name did not match — nothing was changed",
                  file=stream)
            return False
        return True

    if assume_yes:
        return True
    answer = _ask("Proceed? [y/N] ")
    return (answer or "").strip().lower() in ("y", "yes")


# ─── Read commands ────────────────────────────────────────────────────────────
# Each handler takes the parsed namespace and returns the reporting result.
# They are deliberately one line each: the mapping from flags to the reporting
# signature is the only thing they are allowed to know.

def _since(args: argparse.Namespace, default: str) -> str:
    return getattr(args, "since", None) or default


def _read_instances(args: argparse.Namespace) -> Any:
    return _reporting().instances()


def _read_groups(args: argparse.Namespace) -> Any:
    return _reporting().groups(make_context(args.instance),
                               search=args.search, limit=args.limit)


def _read_projects(args: argparse.Namespace) -> Any:
    return _reporting().projects(make_context(args.instance), group=args.group,
                                 search=args.search, limit=args.limit)


def _read_overview(args: argparse.Namespace) -> Any:
    return _reporting().project_overview(make_context(args.instance),
                                         args.project, since=_since(args, "30d"))


def _read_commits(args: argparse.Namespace) -> Any:
    return _reporting().commits(
        make_context(args.instance), args.project, since=_since(args, "7d"),
        until=args.until, author=args.author, branch=args.branch,
        with_stats=not args.no_stats, limit=args.limit)


def _read_commit(args: argparse.Namespace) -> Any:
    return _reporting().commit_detail(make_context(args.instance), args.project,
                                      args.sha, max_diff_chars=args.max_diff_chars)


def _read_compare(args: argparse.Namespace) -> Any:
    return _reporting().compare(make_context(args.instance), args.project,
                                args.base, args.head,
                                max_diff_chars=args.max_diff_chars)


def _read_contributors(args: argparse.Namespace) -> Any:
    return _reporting().contributors(make_context(args.instance), args.project,
                                     since=_since(args, "30d"),
                                     branch=args.branch)


def _split_csv(value: str | None) -> list[str]:
    """``"alice, bob"`` -> ``["alice", "bob"]``, order kept, blanks dropped."""
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _read_person(args: argparse.Namespace) -> Any:
    people = list(args.people or []) + _split_csv(args.users)
    deduped: list[str] = []
    for person in people:
        if person not in deduped:
            deduped.append(person)
    if not deduped:
        raise ValueError("name at least one person: "
                         f"'{PROG} person alice' or '--users alice,bob'")
    return _reporting().person_report(
        make_context(args.instance), deduped, since=_since(args, "7d"),
        until=args.until, projects=_split_csv(args.projects) or None,
        max_projects=args.max_projects)


def _read_progress(args: argparse.Namespace) -> Any:
    return _reporting().project_progress(make_context(args.instance),
                                         args.project, since=_since(args, "30d"))


def _read_mrs(args: argparse.Namespace) -> Any:
    return _reporting().merge_requests(
        make_context(args.instance), project=args.project, group=args.group,
        state=args.state, author=args.author, since=args.since,
        target_branch=args.target_branch, limit=args.limit)


def _read_mr(args: argparse.Namespace) -> Any:
    return _reporting().merge_request(
        make_context(args.instance), args.project, args.iid,
        with_diff=not args.no_diff, max_diff_chars=args.max_diff_chars)


def _read_pipelines(args: argparse.Namespace) -> Any:
    return _reporting().pipelines(make_context(args.instance), args.project,
                                  ref=args.ref, limit=args.limit)


def _read_branches(args: argparse.Namespace) -> Any:
    return _reporting().branches(make_context(args.instance), args.project,
                                 search=args.search)


def _read_users(args: argparse.Namespace) -> Any:
    return _reporting().users(make_context(args.instance), args.search,
                              limit=args.limit)


def _read_file(args: argparse.Namespace) -> Any:
    return _reporting().file_content(make_context(args.instance), args.project,
                                     args.path, ref=args.ref,
                                     max_chars=args.max_chars)


def _read_local_status(args: argparse.Namespace) -> Any:
    return _reporting().local_status(make_context(args.instance),
                                     group=args.group)


READ_HANDLERS: dict[str, Callable[[argparse.Namespace], Any]] = {
    "instances": _read_instances,
    "groups": _read_groups,
    "projects": _read_projects,
    "overview": _read_overview,
    "commits": _read_commits,
    "commit": _read_commit,
    "compare": _read_compare,
    "contributors": _read_contributors,
    "person": _read_person,
    "progress": _read_progress,
    "mrs": _read_mrs,
    "mr": _read_mr,
    "pipelines": _read_pipelines,
    "branches": _read_branches,
    "users": _read_users,
    "file": _read_file,
    "local-status": _read_local_status,
}


# ─── Write commands ───────────────────────────────────────────────────────────

def _body_text(args: argparse.Namespace) -> str:
    """The note body, from the positional argument or ``--body-file``."""
    if getattr(args, "body_file", None):
        with open(os.path.expanduser(args.body_file), encoding="utf-8") as fh:
            return fh.read()
    if args.body:
        return args.body
    raise ValueError("provide the comment text, or --body-file FILE")


def _description_text(args: argparse.Namespace) -> str:
    if getattr(args, "description_file", None):
        with open(os.path.expanduser(args.description_file),
                  encoding="utf-8") as fh:
            return fh.read()
    return args.description or ""


def _squash_flag(args: argparse.Namespace) -> bool | None:
    """Tri-state: ``--squash``, ``--no-squash``, or leave it to the project."""
    if args.squash:
        return True
    if args.no_squash:
        return False
    return None


def _quick_actions(args: argparse.Namespace) -> dict:
    """``{"allow_quick_actions": True}`` only when the flag was given.

    Omitted otherwise, so the action's own default (neutralise) applies and
    a body's ``/merge`` line is posted as text, never executed.
    """
    return ({"allow_quick_actions": True}
            if getattr(args, "allow_quick_actions", False) else {})


#: command -> function mapping the preview to keyword arguments only the CLI
#: (never the user's flags) supplies to ``do_<action>``: the state the human
#: just saw, so a change made after they looked is rejected.
_PINNED_FROM_PREVIEW: dict[str, Callable[[dict], dict]] = {
    "mr-merge": lambda p: {"sha": p.get("sha")},
    "mr-approve": lambda p: {"sha": p.get("sha")},
    "mr-create": lambda p: ({"description_sha256": p["description_sha256"]}
                            if p.get("description_sha256") else {}),
}


# command -> (action base name, kwargs builder)
WRITE_SPECS: dict[str, tuple[str, Callable[[argparse.Namespace], dict]]] = {
    "sync": ("sync_local",
             lambda a: {"group": a.group, "project": a.project}),
    "clone-missing": ("clone_missing", lambda a: {"group": a.group}),
    "mr-comment": ("mr_comment",
                   lambda a: {"project": a.project, "iid": a.iid,
                              "body": _body_text(a),
                              **_quick_actions(a)}),
    "mr-review-comment": ("mr_review_comment",
                          lambda a: {"project": a.project, "iid": a.iid,
                                     "body": _body_text(a), "path": a.path,
                                     "new_line": a.new_line,
                                     "old_line": a.old_line,
                                     **_quick_actions(a)}),
    "mr-approve": ("mr_approve",
                   lambda a: {"project": a.project, "iid": a.iid}),
    "mr-unapprove": ("mr_unapprove",
                     lambda a: {"project": a.project, "iid": a.iid}),
    "mr-create": ("mr_create",
                  lambda a: {"project": a.project,
                             "source_branch": a.source_branch,
                             "target_branch": a.target_branch,
                             "title": a.title,
                             "description": _description_text(a),
                             "draft": a.draft,
                             **_quick_actions(a)}),
    "mr-merge": ("mr_merge",
                 lambda a: {"project": a.project, "iid": a.iid,
                            "squash": _squash_flag(a),
                            "remove_source_branch": a.remove_source_branch}),
    "issue-comment": ("issue_comment",
                      lambda a: {"project": a.project, "iid": a.iid,
                                 "body": _body_text(a),
                                 **_quick_actions(a)}),
    "create-branch": ("create_branch",
                      lambda a: {"project": a.project, "branch": a.branch,
                                 "ref": a.ref}),
}


def run_write(command: str, args: argparse.Namespace) -> int:
    """preview → print → ask → do. The only path to a mutation."""
    name, build_kwargs = WRITE_SPECS[command]
    kwargs = build_kwargs(args)
    actions = _actions()
    ctx = make_context(args.instance)

    preview = getattr(actions, f"preview_{name}")(ctx, **kwargs)
    shown = scrub_data(preview)
    fmt = _resolve_format(args)
    if fmt == "json":
        print(render(shown, "json"))
    else:
        _print_preview(shown)

    if not preview.get("ok", True):
        _print_reasons(shown)
        return 1

    if not confirm_write(command, preview, bool(args.yes)):
        print("cancelled — nothing was changed")
        return 0

    pin = _PINNED_FROM_PREVIEW.get(command)
    if pin is not None:
        # Taken from the preview, so a source branch that moved (or a
        # description that changed) since the human read it is rejected
        # instead of acted on blind.
        kwargs.update(pin(preview))

    result = scrub_data(getattr(actions, f"do_{name}")(ctx, **kwargs))
    write_output(render(result, fmt, title=result.get("action")
                        if isinstance(result, dict) else None), args.out)
    return 0


def run_read(command: str, args: argparse.Namespace) -> int:
    """Run a read command and render its result."""
    result = READ_HANDLERS[command](args)
    write_output(render(result, _resolve_format(args), title=command), args.out)
    return 0


# ─── Parser ───────────────────────────────────────────────────────────────────

_EPILOG = f"""examples:
  {PROG} instances
  {PROG} projects --group group-a
  {PROG} commits group-a/project-a --since 7d --format md --out commits.md
  {PROG} person alice --since 2w
  {PROG} mr group-a/project-a 7
  {PROG} mr-comment group-a/project-a 7 "looks good to me"
  {PROG} mr-merge group-a/project-a 7      # into main: type the branch name

every command that changes something prints a preview first and asks
Proceed? [y/N]. --yes skips the question, except a merge into a
protected branch, which always asks for the branch name.
"""


def _common_parser(suppress: bool) -> argparse.ArgumentParser:
    """Flags accepted both before and after the subcommand name.

    ``suppress`` is the subtle half. A flag declared on a parent parser is
    re-declared on every subparser, and a subparser that carries its own
    default *overwrites* whatever the main parser already stored — so
    ``gitlab-report --json groups`` would silently lose ``--json``. The
    subparser copies therefore default to ``argparse.SUPPRESS``: they set the
    attribute only when the flag is actually given, leaving the pre-command
    value in place otherwise.
    """
    nothing = argparse.SUPPRESS if suppress else None
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--instance", default=nothing,
                        help="GitLab instance name (default: the active one)")
    common.add_argument("--since", default=nothing,
                        help="start of the window: YYYY-MM-DD, 7d, 2w, 1m, today")
    common.add_argument("--until", default=nothing, help="end of the window")
    common.add_argument("--json", action="store_true",
                        default=argparse.SUPPRESS if suppress else False,
                        help="shorthand for --format json")
    common.add_argument("--format", choices=("table", "md", "json"),
                        default=nothing,
                        help="output format (default: table)")
    common.add_argument("--out", default=nothing, metavar="FILE",
                        help="write the output to FILE instead of stdout")
    common.add_argument("--yes", action="store_true",
                        default=argparse.SUPPRESS if suppress else False,
                        help="skip Proceed? for writes (never for a "
                             "protected-branch merge)")
    return common


def build_parser() -> argparse.ArgumentParser:
    """The whole command surface. One subparser per MCP tool."""
    common = _common_parser(suppress=True)
    parser = argparse.ArgumentParser(
        prog=PROG, parents=[_common_parser(suppress=False)], epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Report on and act against GitLab from the shell — the "
                    "same operations the gitlab MCP server exposes.")
    subs = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return subs.add_parser(name, parents=[common], help=help_text,
                               description=help_text)

    # ── reads ────────────────────────────────────────────────────────────────
    add("instances", "list the configured GitLab instances")

    p = add("groups", "list groups")
    p.add_argument("--search", default=None, help="filter by name")
    p.add_argument("--limit", type=int, default=100)

    p = add("projects", "list projects (subgroups included)")
    p.add_argument("--group", default=None, help="group path")
    p.add_argument("--search", default=None)
    p.add_argument("--limit", type=int, default=200)

    p = add("overview", "one project at a glance")
    p.add_argument("project", help="group/project path or numeric id")

    p = add("commits", "commits in a window, with line counts")
    p.add_argument("project")
    p.add_argument("--author", default=None, help="filter by author")
    p.add_argument("--branch", default=None, help="branch or ref")
    p.add_argument("--no-stats", action="store_true",
                   help="skip per-commit line counts (much faster)")
    p.add_argument("--limit", type=int, default=200)

    p = add("commit", "one commit with its diff")
    p.add_argument("project")
    p.add_argument("sha")
    p.add_argument("--max-diff-chars", type=int, default=60000)

    p = add("compare", "diff between two refs")
    p.add_argument("project")
    p.add_argument("base")
    p.add_argument("head")
    p.add_argument("--max-diff-chars", type=int, default=60000)

    p = add("contributors", "per-author commit and line counts")
    p.add_argument("project")
    p.add_argument("--branch", default=None)

    p = add("person", "what one or more people did, across projects")
    p.add_argument("people", nargs="*", metavar="USERNAME",
                   help="usernames (or use --users alice,bob)")
    p.add_argument("--users", default=None, metavar="A,B",
                   help="comma-separated usernames")
    p.add_argument("--projects", default=None, metavar="A,B",
                   help="limit to these project paths")
    p.add_argument("--max-projects", type=int, default=50)

    p = add("progress", "project progress: commits per week, MRs, pipelines")
    p.add_argument("project")

    p = add("mrs", "list merge requests")
    p.add_argument("--project", default=None)
    p.add_argument("--group", default=None)
    p.add_argument("--state", default="opened",
                   choices=("opened", "closed", "merged", "locked", "all"))
    p.add_argument("--author", default=None)
    p.add_argument("--target-branch", default=None,
                   help="only merge requests aimed at this branch")
    p.add_argument("--limit", type=int, default=100)

    p = add("mr", "one merge request, with changes, for review")
    p.add_argument("project")
    p.add_argument("iid", type=int)
    p.add_argument("--no-diff", action="store_true",
                   help="omit the changed files and diffs")
    p.add_argument("--max-diff-chars", type=int, default=80000)

    p = add("pipelines", "recent pipelines")
    p.add_argument("project")
    p.add_argument("--ref", default=None)
    p.add_argument("--limit", type=int, default=20)

    p = add("branches", "list branches")
    p.add_argument("project")
    p.add_argument("--search", default=None)

    p = add("users", "search users")
    p.add_argument("search", metavar="QUERY")
    p.add_argument("--limit", type=int, default=20)

    p = add("file", "print a file from a repository")
    p.add_argument("project")
    p.add_argument("path", help="path inside the repository")
    p.add_argument("--ref", default=None, help="branch, tag or sha")
    p.add_argument("--max-chars", type=int, default=100000)

    p = add("local-status", "state of the local clones under the managed dir")
    p.add_argument("--group", default=None)

    # ── writes ───────────────────────────────────────────────────────────────
    p = add("sync", "[write] fetch + fast-forward the local clones")
    p.add_argument("--group", default=None)
    p.add_argument("--project", default=None)

    p = add("clone-missing", "[write] clone a group's projects locally")
    p.add_argument("group")

    p = add("mr-comment", "[write] comment on a merge request")
    p.add_argument("project")
    p.add_argument("iid", type=int)
    p.add_argument("body", nargs="?", default=None)
    p.add_argument("--body-file", default=None, metavar="FILE")
    p.add_argument("--allow-quick-actions", action="store_true",
                   help="let GitLab EXECUTE /quick-action lines in the text "
                        "(default: they are neutralised and posted as text)")

    p = add("mr-review-comment",
            "[write] start a review discussion on a diff line")
    p.add_argument("project")
    p.add_argument("iid", type=int)
    p.add_argument("body", nargs="?", default=None)
    p.add_argument("--body-file", default=None, metavar="FILE")
    p.add_argument("--allow-quick-actions", action="store_true",
                   help="let GitLab EXECUTE /quick-action lines in the text "
                        "(default: they are neutralised and posted as text)")
    p.add_argument("--path", required=True, help="file path in the diff")
    p.add_argument("--new-line", type=int, default=None,
                   help="line number in the new file")
    p.add_argument("--old-line", type=int, default=None,
                   help="line number in the old file")

    p = add("mr-approve", "[write] approve a merge request")
    p.add_argument("project")
    p.add_argument("iid", type=int)

    p = add("mr-unapprove", "[write] withdraw your approval")
    p.add_argument("project")
    p.add_argument("iid", type=int)

    p = add("mr-create", "[write] open a merge request")
    p.add_argument("project")
    p.add_argument("source_branch", metavar="SOURCE")
    p.add_argument("target_branch", metavar="TARGET")
    p.add_argument("title")
    p.add_argument("--description", default=None)
    p.add_argument("--description-file", default=None, metavar="FILE")
    p.add_argument("--draft", action="store_true")
    p.add_argument("--allow-quick-actions", action="store_true",
                   help="let GitLab EXECUTE /quick-action lines in the text "
                        "(default: they are neutralised and posted as text)")

    p = add("mr-merge", "[write] merge a merge request")
    p.add_argument("project")
    p.add_argument("iid", type=int)
    p.add_argument("--squash", action="store_true")
    p.add_argument("--no-squash", action="store_true")
    p.add_argument("--remove-source-branch", action="store_true")

    p = add("issue-comment", "[write] comment on an issue")
    p.add_argument("project")
    p.add_argument("iid", type=int)
    p.add_argument("body", nargs="?", default=None)
    p.add_argument("--body-file", default=None, metavar="FILE")
    p.add_argument("--allow-quick-actions", action="store_true",
                   help="let GitLab EXECUTE /quick-action lines in the text "
                        "(default: they are neutralised and posted as text)")

    p = add("create-branch", "[write] create a branch from a ref")
    p.add_argument("project")
    p.add_argument("branch")
    p.add_argument("ref")

    return parser


# ─── Entry point ──────────────────────────────────────────────────────────────

def main(argv: Sequence[str] | None = None) -> int:
    """Run one command. Returns the process exit code; never raises."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None
                             else sys.argv[1:])
    if not args.command:
        parser.print_help()
        return 0

    try:
        if args.command in WRITE_SPECS:
            return run_write(args.command, args)
        return run_read(args.command, args)
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        return 130
    except Exception as exc:                 # noqa: BLE001 - this IS the boundary
        message = terminal_safe(redact(str(exc) or exc.__class__.__name__))
        print(paint(f"error: {message}", RED, sys.stderr), file=sys.stderr)
        if os.environ.get("GITLAB_MCP_DEBUG"):
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":                   # python -m gitlab_mcp.cli
    sys.exit(main())
