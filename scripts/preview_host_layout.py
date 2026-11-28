#!/usr/bin/env python3
"""Read-only preview from separately trusted, offline inventory evidence.

Version 1 JSON: {version: 1, base: ABSOLUTE_BASE, projects: SECTION, sources: SECTION}.
Each section has {complete: true, count: N, rows: [...]}; count is the independently
known total, not just the number of rows collected. Include all project and local
checkout rows in the declared base scope, including unresolved local checkouts.
Projects have {host, id, path_with_namespace}. Sources have {path, host, id, proof};
path is relative to base. A proof has {kind: "gitlab_project_lookup", host, id,
origin_namespace, lookup_namespace, checkout, git_dir, git_config}. A trusted
collector must bind the actual effective GitLab source to a positive project-ID
lookup on the same instance, with its canonical namespace from that lookup. For
rename/transfer, it must establish that the old exact origin still maps to that
same ID; guessing from names or accepting an unverified redirect is not proof.
Only attest a standalone checkout whose effective source has no external config
or URL rewrite dependencies. Capture checkout, .git and .git/config lstat stamps
in that same observation: [st_dev, st_ino, st_ctime_ns, st_mtime_ns, st_size]. Pass
SHA-256 obtained separately from that trusted collector; a hash computed from
untrusted input proves nothing. Run on a quiescent tree. This is not a fresh
server check, a work-state audit, or authorization to move anything.
Exit: 0 preview with no blockers, 1 row blockers, 2 unusable inventory or arguments.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from collections import Counter
from pathlib import Path

# A standalone invocation must not leave __pycache__ in the install tree.
if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paths import gitlab_host, remote_identity  # noqa: E402


STATUSES = ("eligible", "already_canonical", "blocked")
PROOF_KEYS = {"kind", "host", "id", "origin_namespace", "lookup_namespace",
              "checkout", "git_dir", "git_config"}
MAX_MANIFEST_BYTES = 32 * 1024 * 1024


class Blocker(ValueError):
    """A fixed diagnostic that may be reported without echoing input."""


class PreviewParser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, "Invalid preview arguments; use --help.\n")


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Blocker("duplicate_manifest_key")
        result[key] = value
    return result


def lstat_or_none(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise Blocker("path_stat_failed") from None


def directory_prefix(path):
    """Stop before a missing entry; never descend into a symlink or file."""
    for part in (*reversed(path.parents), path):
        entry = lstat_or_none(part)
        if entry is None:
            break
        if not stat.S_ISDIR(entry.st_mode):
            raise Blocker("path_not_directory_or_symlink")
        yield part, entry


def load_manifest(path, digest):
    if not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        raise Blocker("invalid_inventory_digest")
    if path.anchor != "/" or ".." in path.parts:
        raise Blocker("invalid_inventory_path")
    try:
        tuple(directory_prefix(path.parent))
        entry = lstat_or_none(path)
        if entry is None or not stat.S_ISREG(entry.st_mode):
            raise Blocker("inventory_not_regular_file")
        # No leaf symlink following or blocking if a FIFO appears after lstat.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if stamp(os.fstat(stream.fileno())) != stamp(entry):
                raise Blocker("inventory_changed")
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
    except OSError:
        raise Blocker("inventory_unreadable") from None
    if len(raw) > MAX_MANIFEST_BYTES:
        raise Blocker("inventory_too_large")
    if hashlib.sha256(raw).hexdigest() != digest.lower():
        raise Blocker("inventory_digest_mismatch")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicate_keys)
    except Blocker:
        raise
    except (ValueError, RecursionError):
        raise Blocker("invalid_inventory_json") from None


def section_rows(section, name):
    if (not isinstance(section, dict) or set(section) != {"complete", "count", "rows"}
            or section["complete"] is not True or type(section["count"]) is not int
            or not isinstance(section["rows"], list) or section["count"] != len(section["rows"])):
        raise Blocker(f"incomplete_{name}_inventory")
    return section["rows"]


def project_key(row):
    if not isinstance(row, dict):
        return None
    host, project_id = row.get("host"), row.get("id")
    if not isinstance(host, str) or type(project_id) is not int or not 0 < project_id < 2**63:
        return None
    try:
        if gitlab_host(f"https://{host}") == host:
            return host, project_id
    except ValueError:
        return None
    return None


def valid_namespace(host, namespace):
    # Reuse GitLab's exact full-path grammar without reading paths or URLs on disk.
    return (isinstance(namespace, str)
            and remote_identity(f"https://{host}/{namespace}.git") == (host, namespace))


def source_path(base, value):
    if not isinstance(value, str) or any(c in "\\:" or not c.isprintable() for c in value):
        return None
    if any(p in {"", ".", ".."} or p.casefold() == ".git" or p.endswith((".", " "))
           for p in value.split("/")):
        return None
    return base / value


def stamp(entry):
    return [entry.st_dev, entry.st_ino, entry.st_ctime_ns, entry.st_mtime_ns, entry.st_size]


def same_stamp(expected, entry):
    return (isinstance(expected, list) and len(expected) == 5
            and all(type(value) is int for value in expected) and expected == stamp(entry))


def reject_git_ancestors(chain):
    for path, _ in chain:
        if lstat_or_none(path / ".git") is not None:
            raise Blocker("nested_checkout")


def attested_origin(proof, key):
    if (isinstance(proof, dict) and set(proof) == PROOF_KEYS
            and proof["kind"] == "gitlab_project_lookup" and project_key(proof) == key
            and valid_namespace(key[0], proof["origin_namespace"])):
        return proof["origin_namespace"]
    return None


def inspect_source(path, key, namespace, proof):
    chain = tuple(directory_prefix(path))
    if chain[-1][0] != path:
        raise Blocker("source_not_found")
    reject_git_ancestors(chain[:-1])
    checkout = chain[-1][1]
    git_dir = path / ".git"
    git_entry = lstat_or_none(git_dir)
    if git_entry is None:
        raise Blocker("git_metadata_missing")
    if not stat.S_ISDIR(git_entry.st_mode) or any(
        lstat_or_none(git_dir / name) is not None
        for name in ("commondir", "gitdir", "worktrees", "modules", "config.worktree")
    ):
        raise Blocker("linked_or_indirect_git_metadata")
    config = lstat_or_none(git_dir / "config")
    head = lstat_or_none(git_dir / "HEAD")
    if config is None or head is None or not all(stat.S_ISREG(item.st_mode) for item in (config, head)):
        raise Blocker("git_metadata_missing_or_unsafe")
    if len({checkout.st_dev, git_entry.st_dev, config.st_dev, head.st_dev, chain[-2][1].st_dev}) != 1:
        raise Blocker("cross_device")
    origin = attested_origin(proof, key)
    if origin is None or proof["lookup_namespace"] != namespace:
        raise Blocker("source_provenance_unverified")
    if not all(same_stamp(proof[field], entry) for field, entry in (
        ("checkout", checkout), ("git_dir", git_entry), ("git_config", config),
    )):
        raise Blocker("stale_source_provenance")
    return checkout, origin


def inspect_destination(path, device):
    chain = tuple(directory_prefix(path.parent))
    reject_git_ancestors(chain)
    if chain[-1][0] == path.parent and lstat_or_none(path) is not None:
        raise Blocker("destination_exists")
    if device != chain[-1][1].st_dev:
        raise Blocker("cross_device")
    ancestor = chain[-1][0]
    try:
        name_limit = os.pathconf(ancestor, "PC_NAME_MAX")
        path_limit = os.pathconf(ancestor, "PC_PATH_MAX")
    except (OSError, ValueError):
        raise Blocker("destination_path_limit_unknown") from None
    if (0 < path_limit <= len(os.fsencode(path)) or
            any(0 < name_limit < len(os.fsencode(part)) for part in path.relative_to(ancestor).parts)):
        raise Blocker("destination_path_too_long")


def folded_path(path):
    # ponytail: block case-only collisions; consult filesystem-specific name
    # comparison rules before supporting a filesystem with broader aliases.
    return tuple(part.casefold() for part in path.parts)


def proper_prefixes(parts):
    return {parts[:length] for length in range(1, len(parts))}


def build_rows(manifest, base):
    if (not isinstance(manifest, dict) or set(manifest) != {"version", "base", "projects", "sources"}
            or type(manifest["version"]) is not int or manifest["version"] != 1
            or manifest["base"] != str(base)):
        raise Blocker("invalid_inventory_schema_or_base")
    projects = section_rows(manifest["projects"], "project")
    sources = section_rows(manifest["sources"], "source")
    if not projects:
        raise Blocker("empty_project_inventory")

    project_counts = Counter()
    destination_counts = Counter()
    destinations = {}
    origin_ids = {}
    rows = []
    for index, project in enumerate(projects, 1):
        key = project_key(project)
        if key is not None:
            project_counts[key] += 1
        if key is None or set(project) != {"host", "id", "path_with_namespace"}:
            row = {"project_row": index, "status": "blocked", "blockers": ["invalid_project_identity"]}
            if key is not None:
                row.update(host=key[0], id=key[1])
            rows.append(row)
            continue
        namespace = project["path_with_namespace"]
        if not valid_namespace(key[0], namespace):
            rows.append({"host": key[0], "id": key[1], "project_row": index,
                         "status": "blocked", "blockers": ["invalid_project_namespace"]})
            continue
        destination = base / key[0] / namespace
        origin_ids.setdefault((key[0], namespace), set()).add(key[1])
        destinations[key] = destination
        destination_counts[folded_path(destination)] += 1

    source_keys = Counter(key for row in sources if (key := project_key(row)) is not None)
    for source in sources:
        key = project_key(source)
        origin = attested_origin(source.get("proof"), key) if key is not None else None
        if origin is not None:
            origin_ids.setdefault((key[0], origin), set()).add(key[1])
    source_paths = [source_path(base, row.get("path")) if isinstance(row, dict) else None for row in sources]
    source_counts = Counter(folded_path(path) for path in source_paths if path is not None)
    managed_hosts = {host for host, _ in project_counts}
    # Missing source rows aren't proof that a project was never cloned.
    for host, project_id in sorted(project_counts.keys() - source_keys.keys()):
        rows.append({"host": host, "id": project_id, "status": "blocked", "blockers": ["source_unproven"]})

    layouts = source_counts.keys() | destination_counts.keys()
    ancestors = set().union(*(proper_prefixes(path) for path in layouts))
    for index, (source, path) in enumerate(zip(sources, source_paths, strict=True), 1):
        key = project_key(source)
        destination = destinations.get(key)
        row = {"source_row": index, "status": "blocked", "blockers": []}
        reasons = row["blockers"]
        if key is not None:
            row.update(host=key[0], id=key[1])
        if path is not None:
            row["source"] = path.relative_to(base).as_posix()
        if destination is not None:
            row["destination"] = destination.relative_to(base).as_posix()
        if key is None or not {"path", "host", "id"} <= source.keys() <= {"path", "host", "id", "proof"}:
            reasons.append("invalid_source_identity")
        elif destination is None:
            reasons.append("unknown_project_identity")
        if source_keys[key] > 1 or project_counts[key] > 1:
            reasons.append("duplicate_project_identity")
        if path is None:
            reasons.append("invalid_source_path")
        elif source_counts[folded_path(path)] > 1:
            reasons.append("duplicate_source_path")
        if path is not None and key is not None:
            source_root = path.relative_to(base).parts[0].casefold()
            if source_root in managed_hosts and source_root != key[0]:
                reasons.append("source_under_other_host")
        if destination is not None and destination_counts[folded_path(destination)] > 1:
            reasons.append("duplicate_destination")
        for candidate in (path, destination):
            if candidate is not None:
                parts = folded_path(candidate)
                if parts in ancestors or proper_prefixes(parts) & layouts:
                    reasons.append("nested_layout")
                    break
        if destination is not None and path is not None and destination != path and folded_path(destination) in source_counts:
            reasons.append("destination_conflicts_with_source")
        if not reasons:
            try:
                namespace = destination.relative_to(base / key[0]).as_posix()
                checkout, origin_namespace = inspect_source(path, key, namespace, source.get("proof"))
                row["origin_namespace"] = origin_namespace
                if any(len(origin_ids[(key[0], name)]) > 1 for name in (namespace, origin_namespace)):
                    raise Blocker("conflicting_origin_identity")
                if path == destination:
                    if origin_namespace != namespace:
                        raise Blocker("canonical_path_origin_mismatch")
                    row["status"] = "already_canonical"
                else:
                    inspect_destination(destination, checkout.st_dev)
                    row.update(status="eligible", move_mode="same_device_rename")
            except Blocker as error:
                reasons.append(str(error))
        rows.append(row)
    return sorted(rows, key=lambda row: (row.get("host", ""), row.get("id", 0), row.get("source", "")))


def main():
    parser = PreviewParser(description=__doc__.splitlines()[0],
                           epilog=__doc__.split("\n", 2)[2].strip(),
                           formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    parser.add_argument("--base", type=Path, required=True, help="absolute existing MasterGroups directory")
    parser.add_argument("--inventory", type=Path, required=True, help="absolute path to offline JSON inventory")
    parser.add_argument("--inventory-sha256", required=True, help="trusted collector's SHA-256 for inventory bytes")
    args = parser.parse_args()
    rows = []
    blockers = []
    try:
        base = args.base
        if os.name != "posix" or base.anchor != "/" or base == base.parent or ".." in base.parts:
            raise Blocker("invalid_base_path_or_unsupported_platform")
        if tuple(directory_prefix(base))[-1][0] != base:
            raise Blocker("base_not_found")
        rows = build_rows(load_manifest(args.inventory, args.inventory_sha256), base)
    except Blocker as error:
        blockers.append(str(error))
    totals = Counter(row["status"] for row in rows)
    print(json.dumps({"preview_only": True, "local_work_inspected": False, "blockers": blockers,
                      "counts": {status: totals[status] for status in STATUSES}, "rows": rows}, indent=2))
    if blockers:
        return 2
    return int(bool(totals["blocked"]))


if __name__ == "__main__":
    sys.exit(main())
