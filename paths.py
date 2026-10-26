# paths.py — Resolving the local directory of a group
#
# A group can be nested (`company/team`) and, depending on how it was
# cloned, it may live in more than one place. Every module
# (clone / pull / log) uses this same logic so we never wrongly report
# "folder does not exist".

import json
import os

# The directory this CLI is installed in. A relative master_dir is resolved
# against it, never against the current working directory: the launcher is an
# alias that runs the script by absolute path without cd-ing, so a cwd-relative
# dest silently creates a fresh MasterGroups tree wherever the shell happened
# to be. Repos then land somewhere the menu never looks at, and the menu
# reports the group as "not cloned" while a full copy exists elsewhere.
INSTALL_ROOT = os.path.dirname(os.path.abspath(__file__))


def anchor(dest: str) -> str:
    """Absolute destination. Relative paths hang off the install dir."""
    d = (dest or ".").strip()
    if os.path.isabs(d):
        return os.path.normpath(d)
    return os.path.normpath(os.path.join(INSTALL_ROOT, d))


class UnsafePath(ValueError):
    """group_path tried to escape the destination directory (path traversal)."""


def sanitize(group_path: str) -> list[str]:
    """
    Turn group_path into safe path components.

    group_path comes from two sources: the user's argv, and full_path from the
    GitLab API. Neither is trustworthy, so any dangerous component ('..', an
    absolute path, a Windows separator, a hidden name) is dropped or rejected.
    """
    raw = (group_path or "").strip()
    # Normalise Windows separators to / as well, so '..\\..' isn't missed
    raw = raw.replace("\\", "/")

    # An absolute path isn't technically dangerous (the join below puts it back
    # under dest), but it is misleading — the user would think /etc/x is read.
    # Reject it explicitly.
    if raw.startswith("/"):
        raise UnsafePath(f"absolute paths are not allowed: {group_path!r}")

    parts = []
    for p in raw.split("/"):
        p = p.strip()
        if not p or p == ".":
            continue
        if p == "..":
            raise UnsafePath(f"invalid path: {group_path!r}")
        # 'C:' or anything else that looks like a Windows drive letter
        if len(p) == 2 and p[1] == ":":
            raise UnsafePath(f"invalid path: {group_path!r}")
        parts.append(p)
    return parts


def _under(dest_norm: str, path: str) -> bool:
    """Whether path really lives under dest (after resolving symlinks)."""
    try:
        d = os.path.realpath(dest_norm)
        p = os.path.realpath(path)
    except OSError:
        return False
    return p == d or p.startswith(d + os.sep)


# ─── Is dest's last component an instance folder? ─────────────────────────────
#
# Two of the shaping rules below (#1 and #4) exist for the case where the user
# points dest AT a group directory:  dest=.../company, group=company/team  ->
# don't create .../company/company/team. They were gated on a pure *string*
# comparison (basename(dest) == a component of the group path), which fires on
# an accidental NAME COLLISION as readily as on a real structural duplication.
#
# On a real configuration the destination's last component is usually the
# instance's `folder` setting (instances.build_cfg: master_dir = base_dir +
# folder). A GitLab top-level group that happens to share that folder's name —
# folder "globex" and group "globex" both existing — then made every repo
# in that namespace lose its leading namespace component, splitting one tree
# into two incompatible shapes (half-nested vs double-nested).
#
# The correct signal is therefore the instance configuration, not the string:
# if dest's last component IS the instance folder, it is a *folder*, never the
# group's own directory, and rules #1/#4 must not shape NEW clones with it.
# They are still emitted as trailing candidates so the hundreds of repos
# already on disk in the legacy shape keep resolving and are never re-cloned.

INSTANCES_FILE = os.path.expanduser("~/.gitlab-cli/instances.json")

_folder_cache: dict = {"key": None, "folders": frozenset()}


def configured_folders() -> frozenset:
    """
    Every non-empty `folder` value across the configured instances.

    Read straight from instances.json (never via instances.py — that module
    imports this one, and building a cfg would resolve tokens). A missing,
    unreadable or folder-less config yields an empty set, which is the current
    state of this machine and must stay harmless.
    """
    try:
        st  = os.stat(INSTANCES_FILE)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        _folder_cache.update(key=None, folders=frozenset())
        return _folder_cache["folders"]

    if _folder_cache["key"] == key:
        return _folder_cache["folders"]

    folders = set()
    try:
        with open(INSTANCES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for inst in (data or {}).get("instances", []) or []:
            name = (inst.get("folder") or "").strip()
            if name:
                folders.add(os.path.basename(os.path.normpath(name)))
    except (OSError, ValueError, AttributeError, TypeError):
        folders = set()

    _folder_cache.update(key=key, folders=frozenset(folders))
    return _folder_cache["folders"]


def is_instance_folder(dest: str, folder: str | None = None) -> bool:
    """
    Whether dest's last component came from an instance's `folder` setting.

    `folder` lets a caller pass cfg["folder"] explicitly (authoritative).
    With no argument the configured instances are consulted. No folder
    configured anywhere -> False, i.e. exactly the legacy behaviour.
    """
    base = os.path.basename(os.path.normpath(dest or "."))
    if not base:
        return False
    if folder is not None:
        return bool(folder.strip()) and base == folder.strip()
    return base in configured_folders()


def candidates(group_path: str, dest: str,
               folder: str | None = None) -> list[str]:
    """
    Every possible directory for a group, in order of preference.

    `folder` is optional and backward compatible: pass cfg["folder"] when you
    have it, otherwise the instance config is consulted. The first candidate
    is the shape a NEW clone should take; later ones exist so already-cloned
    trees in older shapes are still found.
    """
    parts         = sanitize(group_path)
    group_name    = parts[-1] if parts else ""
    dest_norm     = os.path.normpath(dest or ".")
    dest_basename = os.path.basename(dest_norm)
    dest_is_folder = is_instance_folder(dest_norm, folder)

    out    = []
    legacy = []

    # 1) dest is already the group itself:  dest=.../team  group=team
    #    Only when dest is NOT an instance folder — otherwise it is a name
    #    collision between a folder and a top-level group.
    if group_name and dest_basename == group_name:
        (legacy if dest_is_folder else out).append(dest_norm)

    # 2) the full path:  dest/company/team
    if parts:
        out.append(os.path.join(dest_norm, *parts))

    # 3) just the last component:  dest/team
    if group_name:
        out.append(os.path.join(dest_norm, group_name))

    # 4) dest matches the top level:  dest=.../company  group=company/team
    #    Same gate as #1: a folder named like the namespace must not strip it.
    if len(parts) > 1 and dest_basename == parts[0]:
        (legacy if dest_is_folder else out).append(
            os.path.join(dest_norm, *parts[1:]))

    # Legacy shapes stay reachable, but never ahead of the correct one.
    out.extend(legacy)

    if not out:
        out.append(dest_norm)

    # dedupe while keeping order + no candidate may escape dest
    seen, uniq = set(), []
    for c in out:
        if c in seen:
            continue
        seen.add(c)
        if c == dest_norm or _under(dest_norm, c):
            uniq.append(c)
    if not uniq:
        raise UnsafePath(f"invalid path: {group_path!r}")
    return uniq


def resolve(group_path: str, dest: str, folder: str | None = None) -> str:
    """
    Return the first existing directory.
    If none exist, return the 'correct' suggested path
    (for an error message, or for creating the folder).
    """
    cands = candidates(group_path, dest, folder)
    for c in cands:
        if os.path.isdir(c):
            return c
    return cands[0] if cands else dest


def target_for_clone(group_path: str, dest: str,
                     folder: str | None = None) -> str:
    """
    At clone time: reuse an existing folder if there is one,
    otherwise build the full nested path.
    """
    cands = candidates(group_path, dest, folder)
    for c in cands:
        if os.path.isdir(c):
            return c
    # Nothing on disk yet — build the correct shape for a NEW clone.
    parts = sanitize(group_path)
    if parts:
        dest_norm = os.path.normpath(dest or ".")
        # Same gate as candidates() rule #1: collapsing onto dest is only
        # right when dest really IS that group's directory, not when an
        # instance folder merely shares its name.
        if (os.path.basename(dest_norm) == parts[-1]
                and not is_instance_folder(dest_norm, folder)):
            return dest_norm
        return os.path.join(dest_norm, *parts)
    return cands[0] if cands else dest
