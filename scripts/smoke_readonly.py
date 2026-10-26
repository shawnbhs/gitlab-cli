#!/usr/bin/env python3
"""LIVE read-only smoke test for gitlab-mcp.

Calls every READ function in ``gitlab_mcp.reporting`` exactly once against the
real GitLab instance(s) and prints **PASS/FAIL plus a count per function**.
It never prints project data, user data, hostnames, tokens, diff bodies, or any
response body.

WRITE SAFETY
------------
* ``gitlab_mcp.actions`` is NEVER imported. Not one ``do_*`` or ``preview_*``
  function is referenced; the module name appears only in this docstring.
* ``GITLAB_MCP_READ_ONLY=1`` is forced into the environment before anything is
  imported, so ``GitLabContext.post``/``put`` raise instead of mutating.
* No git subprocess is run, nothing is cloned or pulled, and nothing is written
  to ``~/.gitlab-cli``.

This script is for AFTER integration - it imports ``gitlab_mcp``, which needs
the gitlab-cli repo root AND the gitlab-mcp root on ``sys.path``.

Usage (inside WSL, from the gitlab-mcp repo root):

    GITLAB_CLI_ROOT=~/gitlab-cli scripts/smoke_readonly.py   # default ~/gitlab-cli
    scripts/smoke_readonly.py --list              # list checks, call nothing
    scripts/smoke_readonly.py --instance NAME     # one instance only

Exit code: 0 when every check passed, 1 otherwise.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
# gitlab-cli checkout: from the environment or the user's home, never a
# hardcoded path (this file ships to a public repository).
GITLAB_CLI_ROOT = Path(
    os.environ.get("GITLAB_CLI_ROOT") or (Path.home() / "gitlab-cli")
).expanduser()
if GITLAB_CLI_ROOT.is_dir() and str(GITLAB_CLI_ROOT) not in sys.path:
    sys.path.append(str(GITLAB_CLI_ROOT))

# Refuse writes before the package is imported.
os.environ["GITLAB_MCP_READ_ONLY"] = "1"
os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")

# Mutating action names, listed so the exclusion is auditable. This script must
# never import gitlab_mcp.actions nor reference any of these.
WRITE_ACTIONS = frozenset(
    {
        "sync_local",
        "clone_missing",
        "mr_comment",
        "mr_review_comment",
        "mr_approve",
        "mr_unapprove",
        "mr_create",
        "mr_merge",
        "issue_comment",
        "create_branch",
    }
)


class Runner:
    def __init__(self, list_only: bool = False) -> None:
        self.list_only = list_only
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    @staticmethod
    def _count(value: object) -> str:
        """A SAFE description of a result: shape only, never content."""
        if value is None:
            return "none"
        if isinstance(value, bool):
            return f"bool={value}"
        if isinstance(value, (int, float)):
            return f"n={value}"
        if isinstance(value, str):
            return f"len={len(value)}"
        if isinstance(value, dict):
            rows = value.get("rows")
            if isinstance(rows, list):
                return f"keys={len(value)} rows={len(rows)}"
            return f"keys={len(value)}"
        if isinstance(value, (list, tuple, set)):
            return f"items={len(value)}"
        return type(value).__name__

    def check(self, name: str, fn, *args, **kwargs) -> object:
        short = name.split(".")[-1]
        if short in WRITE_ACTIONS:  # pragma: no cover - defensive
            raise AssertionError(f"{name} mutates; refusing to call it")
        if self.list_only:
            print(f"  LIST {name}")
            self.skipped += 1
            return None
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            self.failed += 1
            print(f"  FAIL {name:44s} {type(exc).__name__}")
            if os.environ.get("GITLAB_SMOKE_DEBUG"):
                traceback.print_exc()
            return None
        self.passed += 1
        print(f"  PASS {name:44s} {self._count(result)}")
        return result

    def skip(self, name: str, why: str) -> None:
        self.skipped += 1
        print(f"  SKIP {name:44s} {why}")

    def summary(self) -> int:
        print(
            f"\nresult: {self.passed} passed, {self.failed} failed, "
            f"{self.skipped} skipped"
        )
        return 0 if self.failed == 0 else 1


def smoke_instance(r: Runner, name: str | None) -> None:
    from gitlab_mcp import context as ctx_mod
    from gitlab_mcp import reporting

    label = name or "<active>"
    print(f"\ninstance {label} (GET only, read_only forced on):")

    if r.list_only:
        ctx = None
    else:
        ctx = r.check("context.GitLabContext", ctx_mod.GitLabContext, name)
        if ctx is None:
            return

    if ctx:
        r.check("context.get(/user)", ctx.get, "/user")
        r.check("context.git_env", ctx.git_env)

    # discovery first - later checks need a project reference
    projects = r.check("reporting.projects", reporting.projects, ctx, None, None, 3)
    project_ref = None
    if isinstance(projects, list) and projects:
        first = projects[0]
        project_ref = (
            first.get("path_with_namespace") or first.get("id") or first.get("path")
        )

    r.check("reporting.groups", reporting.groups, ctx, None, 5)

    if ctx and project_ref is not None:
        r.check("context.project_id", ctx.project_id, project_ref)
        r.check(
            "reporting.project_overview",
            reporting.project_overview,
            ctx,
            project_ref,
            "30d",
        )
        commits = r.check(
            "reporting.commits",
            reporting.commits,
            ctx,
            project_ref,
            "30d",
            None,
            None,
            None,
            True,
            5,
        )
        r.check(
            "reporting.contributors",
            reporting.contributors,
            ctx,
            project_ref,
            "30d",
            None,
        )
        r.check(
            "reporting.project_progress",
            reporting.project_progress,
            ctx,
            project_ref,
            "30d",
        )
        r.check("reporting.branches", reporting.branches, ctx, project_ref, None)
        r.check("reporting.pipelines", reporting.pipelines, ctx, project_ref, None, 5)
        r.check(
            "reporting.file_content",
            reporting.file_content,
            ctx,
            project_ref,
            "README.md",
            None,
            4000,
        )

        rows = (commits or {}).get("rows") if isinstance(commits, dict) else None
        sha = (rows or [{}])[0].get("sha") if rows else None
        if sha:
            r.check(
                "reporting.commit_detail",
                reporting.commit_detail,
                ctx,
                project_ref,
                sha,
                20000,
            )
        else:
            r.skip("reporting.commit_detail", "no commit reachable")

        default_branch = None
        if isinstance(projects, list) and projects:
            default_branch = projects[0].get("default_branch")
        if default_branch and sha:
            r.check(
                "reporting.compare",
                reporting.compare,
                ctx,
                project_ref,
                sha,
                default_branch,
                20000,
            )
        else:
            r.skip("reporting.compare", "no base/head pair reachable")

        mrs = r.check(
            "reporting.merge_requests",
            reporting.merge_requests,
            ctx,
            project_ref,
            None,
            "all",
            None,
            None,
            5,
        )
        iid = None
        if isinstance(mrs, list) and mrs:
            iid = mrs[0].get("iid")
        if iid:
            r.check(
                "reporting.merge_request",
                reporting.merge_request,
                ctx,
                project_ref,
                iid,
                True,
                20000,
            )
        else:
            r.skip("reporting.merge_request", "no merge request reachable")
    elif not r.list_only:
        r.skip("reporting.project_overview", "no accessible project")

    if ctx:
        r.check("reporting.users", reporting.users, ctx, "a", 5)
        r.check("reporting.local_status", reporting.local_status, ctx, None)


def main(argv: list[str]) -> int:
    list_only = "--list" in argv
    only = None
    if "--instance" in argv:
        only = argv[argv.index("--instance") + 1]

    r = Runner(list_only)

    from gitlab_mcp import context as ctx_mod
    from gitlab_mcp import reporting

    print("local (no network):")
    names = r.check("context.list_instance_names", ctx_mod.list_instance_names)
    r.check("context.redact", ctx_mod.redact, "glpat-0000000000000000000x")
    r.check("reporting.instances", reporting.instances)
    r.check("reporting.resolve_since", reporting.resolve_since, "7d")

    targets: list[str | None]
    if only:
        targets = [only]
    elif isinstance(names, list) and names:
        targets = [n.get("name") for n in names]
    else:
        targets = [None]

    for name in targets:
        smoke_instance(r, name)

    return r.summary()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
