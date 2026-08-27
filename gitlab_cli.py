#!/usr/bin/env python3
# gitlab_cli.py — Main entry point
#
# The menu is built from the user's actual access level: anything your
# token is not allowed to do is either hidden or greyed out with a reason.
#
# Required Notice: Copyright (c) 2026 Shayan Bakhshaee
# PolyForm Noncommercial License 1.0.0 — see LICENSE.md.
# Free for personal/educational use; commercial use is not permitted.

import os
import sys

import caps
import setup as setup_mod
from api import get_top_level_groups
from clone import cmd_clone, cmd_retry_failed
from pull import cmd_pull
from log import cmd_log
from members import cmd_members_list, cmd_member_actions
from errors import cmd_errors
from instances import pick_instance
from printer import (
    print_banner, print_error, print_info, print_warn, print_separator,
    BOLD, CYAN, GREEN, YELLOW, DIM, RESET, WHITE, RED
)


def group_status(group: dict, dest: str) -> str:
    local_path = os.path.join(dest, group["path"])
    return "✅" if os.path.exists(local_path) else "❌"


def _cap_info(cfg: dict) -> dict:
    """Capabilities of the active instance (cached)."""
    try:
        return caps.get(cfg)
    except Exception:
        return {"ok": False, "caps": {}, "is_admin": False, "error": ""}


def pick_group(cfg: dict, dest: str) -> dict:
    print_info("Fetching groups...")
    groups = get_top_level_groups(cfg["url"], cfg["headers"])

    if not groups:
        print_error("No groups found.")
        print_info("The token probably has no access — try 'doctor'.")
        return {"_bulk": "none"}

    statuses = {g["id"]: group_status(g, dest) for g in groups}

    print_separator()
    print(f"\n{BOLD}{WHITE}  Groups:{RESET}\n")

    for i, g in enumerate(groups, 1):
        status = statuses[g["id"]]
        vis    = g.get("visibility", "")
        lock   = "🔒" if vis == "private" else "🌐"
        desc   = g.get("description") or ""
        dstr   = f"  {DIM}{desc[:45]}{RESET}" if desc else ""
        err_path = os.path.join(dest, f".{g['path']}_errors.json")
        err_mark = f" {RED}⚠{RESET}" if os.path.exists(err_path) else ""
        print(f"  {CYAN}{i:>2}.{RESET}  {status}  {lock}  "
              f"{BOLD}{g['full_path']}{RESET}{err_mark}{dstr}")

    print(f"\n{DIM}  {'─'*45}{RESET}")
    print(f"  {GREEN}ca{RESET}  Clone All  — clone every ❌ group")
    print(f"  {CYAN}pa{RESET}  Pull All   — sync every ✅ group")
    print(f"  {YELLOW}b{RESET}   back   {YELLOW}q{RESET}  exit\n")
    print(f"  {DIM}✅ = cloned   ❌ = not cloned   ⚠ = has errors{RESET}\n")

    while True:
        try:
            choice = input(f"{BOLD}  Which group? (number / name / ca / pa): {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{DIM}Goodbye!{RESET}")
            sys.exit(0)

        low = choice.lower()

        if low == "q":
            print(f"{DIM}Goodbye!{RESET}")
            sys.exit(0)

        if low == "b":
            return {"_bulk": "none"}

        if low == "ca":
            not_cloned = [g for g in groups if statuses[g["id"]] == "❌"]
            if not not_cloned:
                print(f"  {GREEN}✔ Every group is already cloned!{RESET}\n")
                continue
            return {"_bulk": "clone_all", "_groups": not_cloned}

        if low == "pa":
            cloned = [g for g in groups if statuses[g["id"]] == "✅"]
            if not cloned:
                print(f"  {YELLOW}⚠  No group has been cloned yet!{RESET}\n")
                continue
            return {"_bulk": "pull_all", "_groups": cloned}

        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(groups):
                selected = groups[idx]
                selected["_status"] = statuses[selected["id"]]
                return selected
            print(f"  {RED}Pick a number from 1 to {len(groups)}.{RESET}")
            continue

        matches = [g for g in groups if low in g["full_path"].lower()]
        if len(matches) == 1:
            matches[0]["_status"] = statuses[matches[0]["id"]]
            return matches[0]
        elif len(matches) > 1:
            print(f"  {YELLOW}Several matches found:{RESET}")
            for m in matches:
                print(f"    {statuses[m['id']]}  {m['full_path']}")
        else:
            print(f"  {RED}'{choice}' not found.{RESET}")


def pick_action(status: str, has_errors: bool = False, info: dict = None) -> str:
    info = info or {}
    can_clone = caps.can(info, "clone") if info else True

    print(f"\n{BOLD}{WHITE}  What would you like to do?{RESET}\n")

    if status == "✅":
        print(f"  {CYAN}1.{RESET}  🔄  Sync    — update every project")
        print(f"  {CYAN}2.{RESET}  📋  Log     — show the latest commits")
        print(f"  {CYAN}3.{RESET}  🔗  Remotes — remotes of each repo")
        valid = ["1", "2", "3"]
        if has_errors:
            print(f"  {CYAN}4.{RESET}  🔁  Retry   — {RED}retry the failed projects{RESET}")
            valid.append("4")
    else:
        if can_clone:
            print(f"  {CYAN}1.{RESET}  📥  Clone   — clone every subgroup and project")
            valid = ["1"]
        else:
            print(f"  {DIM}1.  Clone   — token has no clone scope{RESET}")
            valid = []

    print(f"\n  {YELLOW}b{RESET}  back   {YELLOW}q{RESET}  exit\n")

    while True:
        try:
            action = input(f"{BOLD}  Choice: {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{DIM}Goodbye!{RESET}")
            sys.exit(0)

        if action == "q":
            sys.exit(0)
        if action == "b":
            return "back"
        if action in valid:
            return action
        print(f"  {RED}Invalid choice.{RESET}")


def pick_main_menu(cfg: dict, info: dict) -> str:
    """Main menu — only what the token is allowed to do."""
    from errors import _read_errors
    unresolved = len([e for e in _read_errors() if not e.get("resolved")])
    err_mark   = f"  {RED}⚠ {unresolved} unresolved{RESET}" if unresolved else ""

    role = caps.role_label(info) if info.get("ok") else f"{RED}offline{RESET}"
    user = info.get("user") or info.get("username") or "?"

    print(f"\n{DIM}  {user} — {role}{RESET}")
    print(f"\n{BOLD}{WHITE}  Which section do you want?{RESET}\n")
    print(f"  {CYAN}1.{RESET}  📁  Master Groups  — clone, sync, log projects")

    valid = ["1", "3", "4", "5", "b", "q"]

    if caps.can(info, "members"):
        print(f"  {CYAN}2.{RESET}  👥  Members        — members, activity, access")
        valid.append("2")
    else:
        print(f"  {DIM}2.  👥  Members        — token has no access{RESET}")

    print(f"  {CYAN}3.{RESET}  🔗  Remotes        — audit the remotes of every repo")
    print(f"  {CYAN}4.{RESET}  🩺  Doctor         — health check of config and token")
    print(f"  {CYAN}5.{RESET}  ⚠️   Error Log      — errors and how to fix them{err_mark}")
    print(f"  {YELLOW}b.{RESET}  🔄  Instances      — switch / add a GitLab")
    print(f"\n  {YELLOW}q{RESET}  exit\n")

    while True:
        try:
            choice = input(f"{BOLD}  Choice: {RESET}").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{DIM}Goodbye!{RESET}")
            sys.exit(0)
        if choice == "q":
            sys.exit(0)
        if choice in valid:
            return choice
        if choice == "2":
            print_warn("  Your token cannot list users.")
            continue
        print(f"  {RED}Invalid choice.{RESET}")


def _show_instance_banner(cfg: dict, info: dict):
    inst_name = cfg.get("name", cfg.get("url", ""))
    dest      = cfg["master_dir"]
    print_info(f"Instance : {GREEN}{inst_name}{RESET}  {DIM}({cfg['url']}){RESET}")
    print_info(f"Folder   : {os.path.abspath(dest)}")
    if info.get("ok"):
        print_info(f"Access   : {caps.role_label(info)}"
                   f"  {DIM}({info.get('top_groups', 0)} top-level groups){RESET}")
    elif info.get("error"):
        print_warn(f"Token    : {info['error']}")


def _run_remotes(cfg: dict):
    """Audit the remotes of every repo of this instance."""
    import remotes
    from pull import _find_git_repos

    dest  = cfg["master_dir"]
    repos = _find_git_repos(dest)
    if not repos:
        print_warn(f"There is no repo in {dest}. Clone something first.")
        return

    print_info(f"{len(repos)} repos found — checking...")
    rows = remotes.audit(repos, check_network=False)
    remotes.print_audit(rows)

    leaky = [r for r in rows if r["has_creds"]]
    if not leaky:
        return

    print_warn(f"{len(leaky)} repos have the token stored in the remote URL.")
    print(f"  {DIM}The token is stored in plain text in .git/config —"
          f" it leaks if you share the repo.{RESET}")
    try:
        ans = input(f"{BOLD}  Clean it up and move it to the credential store? (y/N): {RESET}").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return
    if ans != "y":
        return

    stats = remotes.scrub(repos, save_to_store=True)
    print_info(f"cleaned: {stats['cleaned']}   "
               f"creds saved: {stats['saved']}   failed: {stats['failed']}")


def interactive_mode(cfg: dict):
    new_cfg = pick_instance()
    if new_cfg:
        cfg = new_cfg

    dest = cfg["master_dir"]
    os.makedirs(dest, exist_ok=True)
    info = _cap_info(cfg)
    _show_instance_banner(cfg, info)

    while True:
        section = pick_main_menu(cfg, info)

        if section == "2":
            while True:
                user = cmd_members_list(cfg)
                if not user:
                    break
                cmd_member_actions(cfg, user)
            continue

        if section == "3":
            _run_remotes(cfg)
            _ask_next()
            continue

        if section == "4":
            setup_mod.doctor()
            _ask_next()
            continue

        if section == "5":
            cmd_errors(cfg)
            continue

        if section == "b":
            new_cfg = pick_instance()
            if new_cfg:
                cfg  = new_cfg
                dest = cfg["master_dir"]
                os.makedirs(dest, exist_ok=True)
                caps.invalidate(cfg)
                info = _cap_info(cfg)
                _show_instance_banner(cfg, info)
            continue

        # ─── Master Groups ────────────────────────────────
        group = pick_group(cfg, dest)

        if group.get("_bulk") == "none":
            continue

        if group.get("_bulk") == "clone_all":
            groups = group["_groups"]
            print(f"\n{BOLD}📥 Clone All — {len(groups)} groups{RESET}\n")
            print_separator()
            for i, g in enumerate(groups, 1):
                print(f"\n{CYAN}[{i}/{len(groups)}]{RESET} {BOLD}{g['full_path']}{RESET}")
                try:
                    cmd_clone(cfg, g["full_path"], dest)
                except KeyboardInterrupt:
                    print(f"\n{YELLOW}⚠ Clone All interrupted.{RESET}\n")
                    break
            else:
                print(f"\n{GREEN}✔ Clone All finished!{RESET}\n")
            _ask_next()
            print()
            continue

        if group.get("_bulk") == "pull_all":
            groups = group["_groups"]
            print(f"\n{BOLD}🔄 Sync All — {len(groups)} groups{RESET}\n")
            print_separator()
            for i, g in enumerate(groups, 1):
                print(f"\n{CYAN}[{i}/{len(groups)}]{RESET} {BOLD}{g['full_path']}{RESET}")
                try:
                    cmd_pull(cfg, g["full_path"], dest)
                except KeyboardInterrupt:
                    print(f"\n{YELLOW}⚠ Sync All interrupted.{RESET}\n")
                    break
            else:
                print(f"\n{GREEN}✔ Sync All finished!{RESET}\n")
            _ask_next()
            print()
            continue

        # ─── Single group ──────────────────────────────────
        status     = group.get("_status", "❌")
        gpath      = group["full_path"]
        group_name = gpath.split("/")[-1]

        err_path   = os.path.join(dest, f".{group_name}_errors.json")
        err_count  = 0
        has_errors = False
        if os.path.exists(err_path):
            try:
                import json
                with open(err_path) as fh:
                    err_count = len(json.load(fh).get("errors", []))
                has_errors = err_count > 0
            except Exception:
                has_errors = False

        status_label = (f"{GREEN}cloned{RESET}" if status == "✅"
                        else f"{YELLOW}not cloned{RESET}")
        err_label    = f"  {RED}⚠ {err_count} errors{RESET}" if has_errors else ""
        print(f"\n  {status}  {BOLD}{gpath}{RESET}  {DIM}({status_label}){RESET}{err_label}\n")
        print_separator()

        action = pick_action(status, has_errors, info)

        if action == "back":
            print()
            continue

        print()
        print_separator()

        if status == "❌":
            cmd_clone(cfg, gpath, dest)
        else:
            if action == "1":
                cmd_pull(cfg, gpath, dest)
            elif action == "2":
                since, author = _ask_log_params()
                print()
                cmd_log(cfg, gpath, dest, since, author)
            elif action == "3":
                import remotes
                from pull import _find_git_repos
                gdir  = os.path.join(dest, gpath.split("/")[-1])
                repos = _find_git_repos(gdir)
                if repos:
                    remotes.print_audit(remotes.audit(repos, check_network=False))
                else:
                    print_warn(f"There is no repo in {gdir}.")
            elif action == "4":
                cmd_retry_failed(cfg, gpath, dest)

        _ask_next()
        print()


def _ask_log_params() -> tuple:
    print(f"""
{DIM}  ┌─ Help ─────────────────────────────────┐
  │  1d   = last 1 day                     │
  │  7d   = last 7 days                    │
  │  2w   = last 2 weeks                   │
  │  1m   = last 1 month                   │
  │  1y   = last 1 year                    │
  │  2026-05-01 = from a specific date     │
  │  Enter = last 1 day (default)          │
  └────────────────────────────────────────┘{RESET}""")
    try:
        since   = input("  Since  : ").strip() or "1d"
        _author = input("  Author (Enter = everyone): ").strip()
        author  = None if _author.lower() in ("", "all") else _author
    except (KeyboardInterrupt, EOFError):
        sys.exit(0)
    return since, author


def _ask_next():
    print(f"\n{DIM}  {'─'*45}{RESET}")
    print(f"  {YELLOW}b{RESET}  back to menu   {YELLOW}q{RESET}  exit\n")
    try:
        nxt = input(f"{BOLD}  Choice: {RESET}").strip().lower()
    except (KeyboardInterrupt, EOFError):
        sys.exit(0)
    if nxt == "q":
        print(f"{DIM}Goodbye!{RESET}")
        sys.exit(0)


def print_usage():
    print(f"""
{BOLD}Usage:{RESET}
  gitlab-cli                          interactive mode
  gitlab-cli setup                    wizard for adding a GitLab
  gitlab-cli doctor                   health check of config, token, git
  gitlab-cli clone   <group>          clone a single group
  gitlab-cli sync    <group>          smart sync of every project
  gitlab-cli log     <group> [since]  latest commits
  gitlab-cli remotes [--fix]          audit remotes (+ strip tokens)
  gitlab-cli instances                list the configured GitLabs

{BOLD}Examples:{RESET}
  gitlab-cli clone my-group
  gitlab-cli sync  my-group
  gitlab-cli log   my-group 3d
  gitlab-cli remotes --fix
""")


def _cmd_remotes_cli(cfg: dict, argv: list):
    import remotes
    from pull import _find_git_repos

    dest  = cfg["master_dir"]
    repos = _find_git_repos(dest)
    if not repos:
        print_warn(f"There is no repo in {dest}.")
        return
    rows = remotes.audit(repos, check_network=False)
    remotes.print_audit(rows)
    if "--fix" in argv:
        stats = remotes.scrub(repos, save_to_store=True)
        print_info(f"cleaned: {stats['cleaned']}   saved: {stats['saved']}   "
                   f"failed: {stats['failed']}")


def main():
    print_banner()
    args = sys.argv[1:]

    # Commands that do not need an active config
    if args and args[0] in ("--help", "-h", "help"):
        print_usage()
        return
    if args and args[0] == "setup":
        setup_mod.run_setup()
        return
    if args and args[0] == "doctor":
        setup_mod.doctor()
        return
    if args and args[0] == "instances":
        from instances import print_instances
        print_instances()
        return

    # First run? wizard
    cfg = setup_mod.ensure_setup()
    if not cfg:
        sys.exit(0)

    dest = cfg["master_dir"]

    try:
        if not args:
            interactive_mode(cfg)

        elif args[0] == "clone":
            if len(args) < 2:
                print_error("Provide a group name:  gitlab-cli clone <group>")
                sys.exit(1)
            cmd_clone(cfg, args[1], args[2] if len(args) > 2 else dest)

        elif args[0] in ("pull", "sync"):
            if len(args) < 2:
                print_error("Provide a group name:  gitlab-cli sync <group>")
                sys.exit(1)
            cmd_pull(cfg, args[1], args[2] if len(args) > 2 else dest)

        elif args[0] == "log":
            if len(args) < 2:
                print_error("Provide a group name:  gitlab-cli log <group>")
                sys.exit(1)
            since  = args[2] if len(args) > 2 else "1d"
            author = args[3] if len(args) > 3 else None
            cmd_log(cfg, args[1], args[4] if len(args) > 4 else dest, since, author)

        elif args[0] == "remotes":
            _cmd_remotes_cli(cfg, args[1:])

        else:
            print_error(f"Unknown command: '{args[0]}'")
            print_usage()

    except KeyboardInterrupt:
        print(f"\n{DIM}Goodbye!{RESET}")
        sys.exit(0)


if __name__ == "__main__":
    main()
