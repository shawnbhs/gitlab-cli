# clone.py — Clone a GitLab group recursively (with resume + error log)

import os
import json
import signal
from datetime import datetime
import paths
from api import get_group, get_subgroups, get_projects
from git import clone_repo
from printer import (
    print_section, print_group, print_subgroup,
    print_project, print_info, print_error,
    print_summary, print_separator,
    GREEN, RED, YELLOW, DIM, RESET
)

# Global state — shared across all calls in one session
_session_stats = {"cloned": 0, "pulled": 0, "failed": 0}
_session_errors = []   # all errors across all groups in one bulk run
_current_dest   = "."
_current_group  = ""


def _error_log_path(dest: str, group_name: str) -> str:
    return os.path.join(dest, f".{group_name}_errors.json")


def _save_errors(dest: str, group_name: str, errors: list):
    if not errors:
        # Clean up old error log if everything succeeded
        path = _error_log_path(dest, group_name)
        if os.path.exists(path):
            os.remove(path)
        return
    path = _error_log_path(dest, group_name)
    with open(path, "w") as f:
        json.dump({
            "date":   datetime.now().isoformat(),
            "group":  group_name,
            "errors": errors
        }, f, indent=2, ensure_ascii=False)
    print(f"\n{YELLOW}⚠  {len(errors)} errors saved to:{RESET} {DIM}{path}{RESET}")


def load_errors(dest: str, group_name: str) -> list:
    path = _error_log_path(dest, group_name)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f).get("errors", [])
        except Exception:
            return []
    return []


def _process_group(cfg: dict, group: dict, local_path: str,
                   depth: int = 0, errors: list = None):
    """Recursively clone — skip already cloned repos (resume mode)."""
    if errors is None:
        errors = []

    os.makedirs(local_path, exist_ok=True)

    projects = get_projects(cfg["url"], cfg["headers"], group["id"])
    for proj in projects:
        target = os.path.join(local_path, proj["path"])

        # Resume: skip if .git already exists
        if os.path.exists(os.path.join(target, ".git")):
            print_project(proj["path"], depth + 1)
            print(f"{DIM}skip ✓{RESET}")
            continue

        print_project(proj["path"], depth + 1)
        ok = clone_repo(cfg["token"], proj["http_url_to_repo"], target)
        if ok:
            _session_stats["cloned"] += 1
        else:
            _session_stats["failed"] += 1
            errors.append({
                "project": proj["path"],
                "path":    target,
                "url":     proj["http_url_to_repo"],
            })

    subgroups = get_subgroups(cfg["url"], cfg["headers"], group["id"])
    for sg in subgroups:
        print_subgroup(sg["full_path"], depth + 1)
        _process_group(cfg, sg, os.path.join(local_path, sg["path"]),
                       depth + 1, errors)

    return errors


def cmd_clone(cfg: dict, group_path: str, dest: str = "."):
    global _current_dest, _current_group
    _current_dest  = dest
    _current_group = group_path.split("/")[-1]

    errors = []

    # Setup Ctrl+C handler to save errors before exit
    original_handler = signal.getsignal(signal.SIGINT)
    def _on_interrupt(sig, frame):
        print(f"\n\n{YELLOW}⚠  Interrupted — saving errors...{RESET}")
        _save_errors(dest, _current_group, errors)
        print_summary(_session_stats["cloned"], _session_stats["pulled"],
                      _session_stats["failed"])
        signal.signal(signal.SIGINT, original_handler)
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, _on_interrupt)

    print_section(f"Clone: {group_path}")
    print_info(f"GitLab : {cfg['url']}")
    print_info(f"Dest   : {os.path.abspath(dest)}")

    group_name = group_path.split("/")[-1]

    # Reuse the folder if it already exists, otherwise build the full
    # nested path (dest/company/team).
    local_path = paths.target_for_clone(group_path, dest)

    # Resume mode check
    if os.path.exists(local_path):
        print_info(f"{YELLOW}Resume mode{RESET} — already cloned repos are skipped")

    # Check for previous errors
    prev_errors = load_errors(dest, group_name)
    if prev_errors:
        print_info(f"{RED}{len(prev_errors)} errors left over from the previous run{RESET}")

    print_separator()

    group = get_group(cfg["url"], cfg["headers"], group_path)
    if not group:
        print_error(f"Group '{group_path}' not found.")
        signal.signal(signal.SIGINT, original_handler)
        return

    print_group(group["full_path"])
    errors = _process_group(cfg, group, local_path, errors=errors)
    _save_errors(dest, group_name, errors)
    print_summary(_session_stats["cloned"], _session_stats["pulled"],
                  _session_stats["failed"])

    signal.signal(signal.SIGINT, original_handler)


def cmd_retry_failed(cfg: dict, group_path: str, dest: str = "."):
    """Retry only the previously failed projects."""
    group_name = group_path.split("/")[-1]
    errors     = load_errors(dest, group_name)

    if not errors:
        print_info(f"{GREEN}There are no errors to retry!{RESET}")
        return

    print_section(f"Retry Failed: {group_path}")
    print_info(f"{len(errors)} projects will be retried...")
    print_separator()

    still_failed = []
    retried = 0

    for e in errors:
        print_project(e["project"], 1)
        target = e["path"]

        # If somehow already cloned (manual fix), skip
        if os.path.exists(os.path.join(target, ".git")):
            print(f"{DIM}already fixed ✓{RESET}")
            continue

        ok = clone_repo(cfg["token"], e["url"], target)
        retried += 1
        if not ok:
            still_failed.append(e)

    _save_errors(dest, group_name, still_failed)

    print_separator()
    fixed = retried - len(still_failed)
    if fixed:
        print(f"{GREEN}✔ {fixed} projects fixed{RESET}")
    if still_failed:
        print(f"{RED}✖ {len(still_failed)} still failing{RESET}")
    else:
        print(f"{GREEN}✔ All errors resolved!{RESET}")
