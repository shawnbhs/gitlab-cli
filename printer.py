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


# The banner is the only place a user ever sees the version, so it reads
# it from version.py instead of carrying its own copy. A hardcoded "v1.0"
# here is exactly the string that keeps printing 1.0 three releases later.
from version import __version__ as _VERSION

_BOX_W = 38          # inner width of the box; both text rows are centred in it


def _row(text: str) -> str:
    return "║" + text.center(_BOX_W) + "║"


def print_banner():
    top = "╔" + "═" * _BOX_W + "╗"
    bot = "╚" + "═" * _BOX_W + "╝"
    mid = _row(f"GitLab CLI  v{_VERSION}")
    sub = _row("Clone · Sync · Log your projects")
    print(f"""
{BOLD}{CYAN}{top}
{mid}
{sub}
{bot}{RESET}
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


# ─── Prompt conventions ───────────────────────────────────────────────────────
#
# Everything below exists because of one bug class, found three separate times
# by live dogfooding and once by the project's founding defect:
#
#   THE TOOL DID SOMETHING OTHER THAN WHAT THE USER ASKED, AND SAID NOTHING.
#
# It showed up as a clone that covered a fraction of the projects and reported success;
# it shows up in a menu as a filter key the program does not recognise being
# quietly treated as "show me everything". Same shape, different blast radius.
# The rules are therefore stated once, here, instead of being re-derived at
# forty-odd `input()` call sites:
#
#   1. ONE NORMALISATION.  Every menu key is `.strip().casefold()`-ed, so `C`
#      and `c` are the same key everywhere. A prompt that is case-sensitive in
#      an application where every other prompt is not is a trap, not a feature.
#
#   2. UNRECOGNISED INPUT IS NEVER A DEFAULT.  If the program does not
#      understand the key, it says so and asks again. Falling through to
#      "everything" / "the first option" / "continue" is the founding defect in
#      miniature. A default is only legitimate where it is PRINTED in the
#      prompt (e.g. "Since [1d]:"), and then only for the empty string.
#
#   3. THE LABEL IS THE CONTRACT.  A key advertised as "exit" must exit; a key
#      that returns to the previous screen is labelled "back". When the two
#      disagree, the one to change is whichever is not what the code actually
#      does for the caller.
#
# These helpers are the mechanism for those rules. They do not print menus —
# each screen still owns its own layout — they own the *answer handling*.

#: The single normalisation every menu key goes through.
def menu_key(raw: str) -> str:
    """Normalise a raw `input()` answer into a comparable menu key.

    Strip surrounding whitespace, then casefold. `casefold()` rather than
    `lower()` so non-ASCII answers fold the way the user expects; for the
    ASCII keys this app uses the two agree, but the intent is 'compare
    case-insensitively', and that is what casefold means.

        >>> menu_key("  C ") == menu_key("c") == "c"
        True
    """
    return (raw or "").strip().casefold()


def matches_key(raw: str, key: str) -> bool:
    """True if the user's answer is `key`, case- and whitespace-insensitively."""
    return menu_key(raw) == menu_key(key)


# Labels for the two universal navigation keys. Named so that a screen cannot
# accidentally print "exit" over a key that merely returns.
KEY_BACK = "back"
KEY_EXIT = "exit"


def nav_line(back: bool = True, exit_: bool = True,
             back_key: str = "b", exit_key: str = "q") -> str:
    """The standard navigation footer.

    A screen passes what its keys ACTUALLY do. A screen whose quit key returns
    to the caller passes ``exit_=False`` and gets "back" printed — which is why
    the instance picker, whose `q` returns None to `interactive_mode()` and
    never calls `sys.exit`, must not advertise "exit".
    """
    bits = []
    if back:
        bits.append(f"{YELLOW}{back_key}{RESET}  {KEY_BACK}")
    if exit_:
        bits.append(f"{YELLOW}{exit_key}{RESET}  {KEY_EXIT}")
    return "  " + "   ".join(bits)


def print_nav(back: bool = True, exit_: bool = True,
              back_key: str = "b", exit_key: str = "q"):
    print("\n" + nav_line(back, exit_, back_key, exit_key) + "\n")


def unknown_choice_msg(raw: str, valid) -> str:
    """The message shown when the program does not understand an answer.

    It NAMES the rejected input. "Invalid choice." leaves the user guessing
    whether the program read what they typed; echoing it back proves the key
    arrived and was refused, rather than arriving and being reinterpreted.
    """
    keys = ", ".join(str(v) for v in valid)
    shown = (raw or "").strip()
    if not shown:
        return f"{RED}Nothing entered. Pick one of: {keys}{RESET}"
    return f"{RED}'{shown}' is not one of: {keys}{RESET}"


def print_unknown_choice(raw: str, valid):
    """Reject an unrecognised answer out loud. Never silently default."""
    print("  " + unknown_choice_msg(raw, valid))


def resolve_choice(raw: str, valid, default: str = None):
    """Map a raw answer onto one of `valid`, or return None to re-prompt.

    `default` is honoured ONLY for an empty answer, and only when the caller
    printed it in the prompt. Anything non-empty that is not in `valid` returns
    None — the caller must re-ask, not guess.

        >>> resolve_choice("C", ["c", "r", "q"])
        'c'
        >>> resolve_choice("z", ["c", "r", "q"]) is None
        True
        >>> resolve_choice("", ["1", "2"], default="1")
        '1'
        >>> resolve_choice("", ["1", "2"]) is None
        True
    """
    key = menu_key(raw)
    if not key:
        return menu_key(default) if default is not None else None
    lookup = {menu_key(v): v for v in valid}
    return lookup.get(key)


def ask_choice(prompt: str, valid, default: str = None,
               on_eof=None, reader=input):
    """Ask until the answer is one of `valid`. The honest prompt loop.

    Returns the matching element of `valid` (in ITS original spelling, so the
    caller can keep comparing against its own constants), `default` for an
    empty answer when one was offered, or `on_eof` if the user sends Ctrl-C /
    Ctrl-D. It never returns something the user did not choose.

    `reader` is injectable so the behaviour can be tested without a terminal.
    """
    while True:
        try:
            raw = reader(prompt)
        except (KeyboardInterrupt, EOFError):
            return on_eof
        picked = resolve_choice(raw, valid, default)
        if picked is not None:
            return picked
        print_unknown_choice(raw, valid)
