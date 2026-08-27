# paths.py — Resolving the local directory of a group
#
# A group can be nested (`company/team`) and, depending on how it was
# cloned, it may live in more than one place. Every module
# (clone / pull / log) uses this same logic so we never wrongly report
# "folder does not exist".

import os


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


def candidates(group_path: str, dest: str) -> list[str]:
    """Every possible directory for a group, in order of preference."""
    parts         = sanitize(group_path)
    group_name    = parts[-1] if parts else ""
    dest_norm     = os.path.normpath(dest or ".")
    dest_basename = os.path.basename(dest_norm)

    out = []

    # 1) dest is already the group itself:  dest=.../team  group=team
    if group_name and dest_basename == group_name:
        out.append(dest_norm)

    # 2) the full path:  dest/company/team
    if parts:
        out.append(os.path.join(dest_norm, *parts))

    # 3) just the last component:  dest/team
    if group_name:
        out.append(os.path.join(dest_norm, group_name))

    # 4) dest matches the top level:  dest=.../company  group=company/team
    if len(parts) > 1 and dest_basename == parts[0]:
        out.append(os.path.join(dest_norm, *parts[1:]))

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


def resolve(group_path: str, dest: str) -> str:
    """
    Return the first existing directory.
    If none exist, return the 'correct' suggested path
    (for an error message, or for creating the folder).
    """
    cands = candidates(group_path, dest)
    for c in cands:
        if os.path.isdir(c):
            return c
    return cands[0] if cands else dest


def target_for_clone(group_path: str, dest: str) -> str:
    """
    At clone time: reuse an existing folder if there is one,
    otherwise build the full nested path.
    """
    cands = candidates(group_path, dest)
    for c in cands:
        if os.path.isdir(c):
            return c
    # Prefer the full path (index 0 may be a bare dest)
    parts = sanitize(group_path)
    if parts:
        dest_norm = os.path.normpath(dest or ".")
        if os.path.basename(dest_norm) == parts[-1]:
            return dest_norm
        return os.path.join(dest_norm, *parts)
    return cands[0] if cands else dest
