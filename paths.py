# paths.py — Managed GitLab paths and legacy group lookup
#
# A group can be nested (`company/team`) and, depending on how it was
# cloned, it may live in more than one place. Every module
# (clone / pull / log) uses this same logic so we never wrongly report
# "folder does not exist".

import json
import os
import re
from ipaddress import IPv4Address
from pathlib import Path
from stat import S_ISDIR
from urllib.parse import urlsplit

import idna

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
    """Invalid or unsafe managed or legacy path input."""


_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_NAMESPACE_SEGMENT = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*")
_WINDOWS_DEVICES = {"con", "prn", "aux", "nul"} | {
    f"{name}{digit}" for name in ("com", "lpt") for digit in "123456789"
}
_HTTP_PORTS = {"http": 80, "https": 443}


def _clean_url(url: str) -> bool:
    # urlsplit silently removes some control characters. Reject before parsing.
    return (isinstance(url, str) and bool(url) and not any(
        c.isspace() or ord(c) < 32 or ord(c) == 127 or c in "\\?#" for c in url
    ))


def _hostname(host: str) -> str:
    # Match requests' nontransitional UTS 46 IDNA; stdlib IDNA 2003 can
    # collapse distinct names (for example, faß and fass).
    host = idna.encode(host, uts46=True).decode("ascii").removesuffix(".")
    labels = host.split(".")
    if not all(_DNS_LABEL.fullmatch(label) for label in labels):
        raise ValueError
    if labels[0] in _WINDOWS_DEVICES:
        raise ValueError
    # Reject legacy short, octal and hexadecimal IP spellings, which clients
    # can route to a different canonical host than the spelling suggests.
    if re.fullmatch(r"[0-9]+|0x[0-9a-f]*", labels[-1]):
        return str(IPv4Address(host))
    return host


def _url_host_port(authority: str) -> tuple[str, int | None]:
    # IPv6 literals are intentionally unsupported as managed folder names.
    host, colon, raw_port = authority.partition(":")
    if not colon:
        return _hostname(host), None
    if not re.fullmatch(r"[0-9]{1,5}", raw_port) or not 1 <= int(raw_port) <= 65535:
        raise ValueError
    return _hostname(host), int(raw_port)


def gitlab_authority(url: str) -> tuple[str, str, int]:
    """Canonical (scheme, host, effective port) for an instance base URL."""
    if _clean_url(url):
        try:
            parsed = urlsplit(url)
            if parsed.scheme in _HTTP_PORTS and parsed.path in ("", "/"):
                host, port = _url_host_port(parsed.netloc)
                return parsed.scheme, host, port or _HTTP_PORTS[parsed.scheme]
        except ValueError:
            pass
    raise UnsafePath("Invalid GitLab instance URL: use an HTTP(S) host with an optional port and no credentials or path.")


def gitlab_host(url: str) -> str:
    """Hostname used as the managed directory for a GitLab instance."""
    return gitlab_authority(url)[1]


def _directory_chain(path: Path) -> None:
    for part in (*reversed(path.parents), path):
        try:
            mode = part.lstat().st_mode
        except FileNotFoundError:
            # Missing directories are valid proposed targets.
            return
        except OSError:
            raise UnsafePath("Cannot inspect managed path.") from None
        if not S_ISDIR(mode):
            raise UnsafePath("Managed paths must be directories without symlinks.")


def managed_root(base: str, url: str) -> str:
    """Return the absolute and link-free host child of the configured base."""
    return managed_path(anchor(os.fspath(base)), gitlab_host(url))


def _namespace_parts(namespace: str) -> list[str]:
    if not isinstance(namespace, str):
        raise UnsafePath("Invalid GitLab namespace path.")
    parts = namespace.split("/")
    if any(not _NAMESPACE_SEGMENT.fullmatch(part) or part.endswith(".")
           or part.split(".", 1)[0].lower() in _WINDOWS_DEVICES for part in parts):
        raise UnsafePath("Invalid GitLab namespace path.")
    return parts


def managed_path(root: str, namespace: str) -> str:
    """Append the exact namespace under the host root; never use legacy paths."""
    path = Path(anchor(os.fspath(root))).joinpath(*_namespace_parts(namespace))
    _directory_chain(path)
    return str(path)


def managed_namespace(root: str, repo: str) -> str:
    """Invert a contained project path without resolving symbolic links."""
    root = Path(anchor(os.fspath(root)))
    path = Path(repo)
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        raise UnsafePath("Project directory is outside the managed root.") from None
    if len(parts) < 2:
        raise UnsafePath("Managed project needs a namespace and repository.")
    namespace = "/".join(parts)
    managed_path(root, namespace)
    return namespace


_SCP_USER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
_USERINFO = re.compile(r"(?:[A-Za-z0-9._~!$&'()*+,;=:-]|%[0-9A-Fa-f]{2})+")


def remote_identity(url: str) -> tuple[str, str] | None:
    """Return host and exact project namespace from an effective Git URL."""
    if not _clean_url(url):
        return None
    try:
        if "://" in url:
            parsed = urlsplit(url)
            # Git transport scheme names are case-sensitive, unlike urlsplit.
            if (url.partition(":")[0] not in (*_HTTP_PORTS, "ssh")
                    or not parsed.path.startswith("/")):
                return None
            userinfo, at, authority = parsed.netloc.rpartition("@")
            if at:
                allowed = _SCP_USER if parsed.scheme == "ssh" else _USERINFO
                if not allowed.fullmatch(userinfo):
                    return None
            host, _ = _url_host_port(authority)
            path = parsed.path[1:]
        else:
            authority, colon, path = url.partition(":")
            if not colon or re.fullmatch(r"[A-Za-z]", authority):
                return None
            user, at, host = authority.rpartition("@")
            if at and not _SCP_USER.fullmatch(user):
                return None
            host = _hostname(host)
        # GitLab's transport suffix is optional. Do not strip separators,
        # fold case, decode ambiguous escapes, or strip multiple suffixes.
        namespace = path.removesuffix(".git")
        if len(_namespace_parts(namespace)) >= 2:
            return host, namespace
    except ValueError:
        pass
    return None


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
# In the legacy layout the destination's last component could be the
# instance's `folder` setting. A GitLab group sharing that folder's name —
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
