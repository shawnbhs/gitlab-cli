# printer.py — Terminal output helpers

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RED    = "\033[31m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
BLUE   = "\033[34m"
CYAN   = "\033[36m"
WHITE  = "\033[37m"


def print_banner():
    print(f"""
{BOLD}{CYAN}╔══════════════════════════════════════╗
║         GitLab CLI  v1.0             ║
║   Clone · Sync · Log your projects   ║
╚══════════════════════════════════════╝{RESET}
""")


def print_section(title: str):
    print(f"\n{BOLD}{BLUE}┌─ {title}{RESET}")


def print_header(title: str, subtitle: str = ""):
    """Section header with a separator line."""
    print(f"\n{BOLD}{WHITE}  {title}{RESET}")
    if subtitle:
        print(f"  {DIM}{subtitle}{RESET}")
    print(f"{DIM}  {'─' * 52}{RESET}\n")


def print_group(name: str):
    print(f"\n{BOLD}{CYAN}📁  {name}{RESET}")


def print_subgroup(name: str, depth: int = 1):
    indent = "   " * depth
    print(f"{indent}{YELLOW}📂  {name}{RESET}")


def print_project(name: str, depth: int = 1):
    indent = "   " * depth
    print(f"{indent}{GREEN}📦  {name}{RESET}", end="  ")


def print_ok(msg: str = "✔"):
    print(f"{GREEN}{msg}{RESET}")


def print_info(msg: str):
    print(f"{CYAN}ℹ  {msg}{RESET}")


def print_warn(msg: str):
    print(f"{YELLOW}⚠  {msg}{RESET}")


def print_error(msg: str):
    print(f"{RED}✖  {msg}{RESET}")


def print_dim(msg: str):
    print(f"{DIM}{msg}{RESET}")


def print_separator():
    print(f"{DIM}{'─' * 50}{RESET}")


def print_summary(cloned: int, pulled: int, failed: int):
    print_separator()
    print(f"{BOLD}Summary:{RESET}")
    print(f"  {GREEN}✔  Cloned  : {cloned}{RESET}")
    print(f"  {CYAN}✔  Pulled  : {pulled}{RESET}")
    if failed:
        print(f"  {RED}✖  Failed  : {failed}{RESET}")
    print()
