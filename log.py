# log.py — Show recent commits across all local projects (tree view)

import os
import re
import paths
from git import get_commits_since
from printer import (
    print_section, print_info, print_warn, print_separator,
    BOLD, CYAN, YELLOW, GREEN, DIM, RESET
)

# Tree drawing chars
T  = "├──"
L  = "└──"
V  = "│  "
SP = "   "


def parse_since(raw: str) -> str:
    raw = raw.strip().lower()
    shortcuts = {"d": "day", "w": "week", "m": "month", "y": "year"}
    match = re.match(r"^(\d+)([dwmy])$", raw)
    if match:
        num  = int(match.group(1))
        unit = shortcuts[match.group(2)]
        if num > 1:
            unit += "s"
        return f"{num} {unit} ago"
    return raw


def _find_git_repos(base_path: str) -> list[str]:
    repos = []
    for root, dirs, _ in os.walk(base_path):
        if ".git" in dirs:
            repos.append(root)
            dirs.remove(".git")
    return sorted(repos)


def _branch_color(branch: str) -> str:
    b = branch.lower()
    if b in ("main", "master"):
        return f"\033[32m{branch}\033[0m"      # green
    if b in ("develop", "development", "dev"):
        return f"\033[36m{branch}\033[0m"      # cyan
    if b.startswith("feature"):
        return f"\033[33m{branch}\033[0m"      # yellow
    if b.startswith("fix") or b.startswith("hotfix"):
        return f"\033[31m{branch}\033[0m"      # red
    if b in ("stage", "staging"):
        return f"\033[35m{branch}\033[0m"      # magenta
    return f"\033[37m{branch}\033[0m"          # white


def _print_tree(group_path: str, repos: list[str], dest: str,
                since_parsed: str, author: str | None):
    """Build and print the full commit tree."""

    # Group repos by their relative path structure
    # repo_path → list of commits
    repo_commits = {}
    for repo in repos:
        commits = get_commits_since(repo, since_parsed, author)
        if commits:
            rel = os.path.relpath(repo, dest)
            repo_commits[rel] = commits

    if not repo_commits:
        return 0

    # Build folder tree structure
    # node: {name, children: {}, repos: [rel_path]}
    tree = {}
    for rel in repo_commits:
        parts = rel.split(os.sep)
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(f"__{part}", {})
        node[f"__REPO__{parts[-1]}"] = rel

    total = sum(len(v) for v in repo_commits.values())

    # Print root
    group_name = group_path.split("/")[-1]
    print(f"\n{BOLD}{CYAN}🌳 {group_name}{RESET}  "
          f"{DIM}({len(repo_commits)} repo, {total} commit){RESET}\n")

    def print_node(node: dict, prefix: str = "", depth: int = 0):
        items = list(node.items())
        for i, (key, val) in enumerate(items):
            is_last = (i == len(items) - 1)
            connector = L if is_last else T
            child_prefix = prefix + (SP if is_last else V)

            if key.startswith("__REPO__"):
                # It's a repo leaf
                repo_name = key[8:]
                rel = val
                commits = repo_commits[rel]
                count = len(commits)

                print(f"{prefix}{connector} {BOLD}{GREEN}📦 {repo_name}{RESET}  "
                      f"{DIM}({count} commit{'s' if count > 1 else ''}){RESET}")

                # Print commits
                for j, c in enumerate(commits):
                    c_last = (j == len(commits) - 1)
                    c_conn = L if c_last else T
                    c_prefix = child_prefix + (SP if c_last else V)

                    branch_str = _branch_color(c['branch'])

                    # Commit line
                    print(f"{child_prefix}{c_conn} {BOLD}{DIM}{c['hash']}{RESET}  "
                          f"[{branch_str}]")

                    # Message
                    msg = c['message']
                    if len(msg) > 72:
                        msg = msg[:69] + "..."
                    print(f"{c_prefix}{YELLOW}✎  {msg}{RESET}")

                    # Author + date
                    print(f"{c_prefix}{DIM}👤 {c['author']}   🕒 {c['date']}{RESET}")

                    if not c_last:
                        print(f"{c_prefix}")

            elif key.startswith("__"):
                # It's a folder/subgroup
                folder_name = key[2:]
                print(f"{prefix}{connector} {BOLD}{CYAN}📁 {folder_name}{RESET}")
                print_node(val, child_prefix, depth + 1)

    print_node(tree)
    return total


def cmd_log(cfg: dict, group_path: str, dest: str = ".",
            since: str = "1d", author: str = None):
    group_name = group_path.split("/")[-1]
    local_path = paths.resolve(group_path, dest)
    since_parsed = parse_since(since)

    print_section(f"Log: {group_path}")
    print_info(f"Since : {since_parsed}")
    if author:
        print_info(f"Author: {author}")
    print_separator()

    if not os.path.exists(local_path):
        print_warn(f"Folder '{local_path}' does not exist. Clone it first.")
        return

    repos = _find_git_repos(local_path)
    if not repos:
        print_warn("No git repos found. Run Clone first!")
        return

    total = _print_tree(group_path, repos, dest, since_parsed, author)

    print()
    print_separator()
    if total == 0:
        print_warn(f"No commits found since '{since_parsed}'.")
    else:
        repo_count = sum(
            1 for r in repos
            if get_commits_since(r, since_parsed, author)
        )
        print(f"{BOLD}📊 Total: {GREEN}{total} commit{RESET}"
              f"{BOLD} across {repo_count} repos{RESET}\n")
