# errors.py — Centralized error logging and viewer

import os
import re
import json
import stat
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from error_catalog import explain, summarize, by_code
from printer import (
    print_section, print_warn, print_separator,
    print_unknown_choice, resolve_choice,
    BOLD, CYAN, GREEN, YELLOW, DIM, RESET, RED
)

ERROR_DIR  = os.path.expanduser("~/.gitlab-cli")
ERROR_FILE = os.path.join(ERROR_DIR, "errors.log")
MAX_DAYS   = 30


# ─── Redaction ─────────────────────────────────────────────────────────────────
#
# An error message may be raw git stderr. Git usually strips the token from the
# URL itself, but not on every version and not on every code path. Since this
# message lands on disk, we scrub anything that looks like a token before
# writing it out — defense in depth.

# (pattern, replacement) pairs, applied in order. Keep them as a list of pairs
# rather than positional indexes: the old code applied patterns 0,1,2 by hand,
# so a fourth pattern added to the list would have been silently dead.
_SECRET_RULES = [
    # 1. GitLab token (glpat-, gldt-, glrt-, glsoat-, glcbt-, ...)
    (re.compile(r"\bgl[a-z]{2,6}-[A-Za-z0-9_\-]{16,}"), "[REDACTED-TOKEN]"),
    # 2. GitHub / OpenAI tokens, just to be safe
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9\-_]{16,})"),
     "[REDACTED-TOKEN]"),
    # 3. URL userinfo with a password:  https://user:secret@host
    #    The username is kept (it is diagnostic: 'oauth2' tells you which code
    #    path injected it); only the secret half is destroyed.
    (re.compile(r"(?<=://)([^/\s:@]+):([^/\s@]+)(?=@)"), r"\1:[REDACTED]"),
    # 4. URL userinfo WITHOUT a colon:  https://<token>@host
    #    This is how a PAT is embedded when it is used as the whole userinfo,
    #    and rule 3 does not see it. Must run after rule 3 — by then a
    #    user:pass pair contains a ':' and cannot match here.
    (re.compile(r"(?<=://)[^/\s:@]+(?=@)"), "[REDACTED-USERINFO]"),
    # 5. Token carried in a query string:  ?private_token=… / &access_token=…
    (re.compile(r"(?i)([?&](?:private[_-]?token|access[_-]?token|token|"
                r"job[_-]?token)=)[^&\s\"']+"), r"\1[REDACTED]"),
    # 6. A token echoed as a header value (git/curl -v output).
    #    The auth SCHEME is kept — 'Bearer' vs 'Basic' says which code path
    #    built the request, and that is diagnostic — but the credential after
    #    it is destroyed. The old rule consumed a single \S+, so
    #    'Authorization: Bearer <token>' redacted the word 'Bearer' and wrote
    #    the token itself to disk in the clear. ':' or '=' because -H and
    #    --header are spelled both ways.
    (re.compile(r"(?i)\b(PRIVATE-TOKEN|Authorization|Proxy-Authorization|"
                r"X-Gitlab-Token|X-Api-Key|Api-Key|Cookie|Set-Cookie)"
                r"(\s*[:=]\s*)"
                r"(Bearer|Basic|Token|OAuth|Digest|JWT)?[ \t]*\S+"),
     lambda m: "%s%s%s[REDACTED]" % (
         m.group(1), m.group(2),
         (m.group(3) + " ") if m.group(3) else "")),
    # 7. A bare auth scheme carrying its credential, outside a header line:
    #    'Bearer <token>' as it appears inside a git or API error body.
    #    Length-bounded so ordinary prose ('token expired') is left alone.
    (re.compile(r"(?i)\b(Bearer|OAuth|Basic)[ \t]+(?!\[REDACTED)"
                r"[A-Za-z0-9._\-+/=]{12,}"), r"\1 [REDACTED]"),
    # 8. Scheme-less userinfo:  x-access-token:SECRET@gitlab.example.com
    #    Rules 3 and 4 both require '://' and therefore never see a credential
    #    quoted out of a .netrc line, a credential-helper echo, or a remote
    #    printed without its scheme. The right-hand side must look like a
    #    hostname so ordinary 'word:word@word' prose cannot match.
    (re.compile(r"(?<![\w:/])([A-Za-z0-9._\-]{1,64}):(?!\[REDACTED)"
                r"[^\s/@\"']{4,}@(?=[A-Za-z0-9\-]+\.[A-Za-z0-9.\-]+)"),
     r"\1:[REDACTED]@"),
    # 9. Any secret-shaped key/value pair:  password=…, api_key: …,
    #    GITLAB_TOKEN=…, "private_token": "…". This is the env-var and
    #    config-dump shape, which carries an underscore before the keyword and
    #    so never matched a \b-anchored rule.
    (re.compile(r"(?i)([A-Za-z0-9_\-]*(?:password|passwd|secret|api[_\-]?key|"
                r"token|credential)s?)(\s*[:=]\s*\"?)(?!\[REDACTED)"
                r"[^\s\"',;)}\]]{8,}"), r"\1\2[REDACTED]"),
    # 10. Credential flags on a quoted command line: -u user:secret,
    #     --password=secret, as they appear when the CLI echoes what it ran.
    (re.compile(r"(?i)(--password|--pass)(=|[ \t]+)(?!\[REDACTED)\S+"),
     r"\1\2[REDACTED]"),
    (re.compile(r"(?i)(\B-u[ \t]+|--user(?:name)?[=\s])([^\s:]{1,64}):\S+"),
     r"\1\2:[REDACTED]"),
    # 11. Git's own denial line, which quotes the credential it rejected:
    #     'remote: HTTP Basic: Access denied for <token>'. Deliberately last —
    #     everything structured has already been handled, so this only ever
    #     sees leftovers.
    (re.compile(r"(?i)(access denied\s+(?:for|to)\s+)(?!\[REDACTED)"
                r"[A-Za-z0-9._\-+/=]{16,}"), r"\1[REDACTED]"),
]

# Legacy name kept for anything that imported it.
_SECRET_PATTERNS = [p for p, _ in _SECRET_RULES]


def redact(text: str) -> str:
    """
    Strip anything that looks like a credential out of the given text.

    Covers a bare token, a token embedded in a URL as `user:secret@host` *and*
    as `secret@host`, a token in a query string, and a token echoed as a
    header. Never widen the input without adding a rule here: this text lands
    on disk in errors.log.
    """
    if not text:
        return text
    out = text
    for pat, repl in _SECRET_RULES:
        out = pat.sub(repl, out)
    return out


# Legacy alias kept for internal use
_redact = redact


# ─── Error catalog wiring ──────────────────────────────────────────────────────
#
# The explanations live in error_catalog.py. The table that used to sit here
# matched bare substrings ("401", "404", "500" anywhere in the text), so a hex
# LFS oid or a path could be "explained" as an auth failure, while common real
# failures (DNS, timeouts, LFS smudge, unsafe url) fell through to "Unknown
# error". explain() is now the single source; nothing here re-implements it.

#: Stored message cap. Git prints the cause LAST (`fatal:` / `LFS:`), so the
#: TAIL is kept — head-truncation is exactly what hid the cause before.
MESSAGE_CAP = 2000


def _keep_tail(text: str, cap: int = MESSAGE_CAP) -> str:
    if len(text) <= cap:
        return text
    return "…" + text[-(cap - 1):]


def _match_error(message: str) -> dict | None:
    """
    Legacy shape of a catalog hit (pattern/type/title/fix), or None.

    Kept so an old caller of the removed table still works; new code calls
    error_catalog.explain() directly. `pattern` now carries the catalog code.
    """
    info = explain(message or "")
    if info.code == "unknown":
        return None
    return {"pattern": info.code, "code": info.code, "type": info.type,
            "title": info.title, "cause": info.cause, "fix": info.fix}


def current_instance() -> str:
    """The active instance name gitlab_cli publishes, or '' when none."""
    return os.environ.get("GITLAB_CLI_INSTANCE", "") or ""


# ─── Concurrency: locking, reading, writing ────────────────────────────────────
#
# WHY A KERNEL LOCK AND NOT A LOCK FILE
# -------------------------------------
# This project has a standing rule: no lock FILES. A `.lock` sentinel survives a
# hard power-off or `kill -9`, and the next start then blocks forever until a
# human deletes it by hand.
#
# `fcntl.flock()` has the opposite lifetime. The lock is a property of the OPEN
# FILE DESCRIPTION held by the kernel, not a name on disk:
#
#   * it is released automatically when the fd is closed — including the
#     implicit close the kernel performs on process exit, on a crash, on
#     SIGKILL, and on reboot after a power cut;
#   * it creates no extra path, so there is nothing that can be left behind;
#   * we take it on errors.log ITSELF, so there is no second object to clean up.
#
# Deadlock is prevented structurally:
#
#   * there is exactly ONE lock in this module and it is never held while
#     taking another lock, so no lock-ordering cycle can exist;
#   * every acquisition goes through `_locked()`, which is a context manager
#     with a try/finally — an exception inside the body still unlocks;
#   * acquisition is LOCK_NB in a bounded retry loop, so a wedged holder can
#     never hang the CLI: after `_LOCK_TIMEOUT` seconds we give up and take the
#     documented degraded path (append anyway / skip the rewrite) instead of
#     blocking forever.
#
# The degraded append path is still safe: the log is opened O_APPEND and one
# entry is one short `write()`, which the kernel positions atomically at EOF.
# The lock protects the read-modify-write paths (resolve / clear / prune),
# which genuinely need mutual exclusion.

try:
    import fcntl                      # POSIX only; absent on Windows
except ImportError:                   # pragma: no cover - not a target platform
    fcntl = None

_LOCK_TIMEOUT = 5.0      # seconds; never block longer than this
_LOCK_POLL    = 0.02

_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR       # 0600 — the log quotes git stderr


@contextmanager
def _locked(fh, exclusive: bool, timeout: float = _LOCK_TIMEOUT):
    """
    Take an flock on `fh` for the duration of the block.

    Yields True when the lock was actually acquired, False on timeout — the
    caller decides what a timeout means. Never raises, never blocks
    indefinitely, and always unlocks on the way out.
    """
    if fcntl is None:                 # pragma: no cover - non-POSIX fallback
        yield True
        return

    flag = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
    deadline = time.monotonic() + max(0.0, timeout)
    got = False
    while True:
        try:
            fcntl.flock(fh.fileno(), flag)
            got = True
            break
        except OSError:
            if time.monotonic() >= deadline:
                break
            time.sleep(_LOCK_POLL)
    try:
        yield got
    finally:
        if got:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def _chmod_private(fh) -> None:
    """Re-assert 0600 on the open fd. Every write path calls this."""
    try:
        os.fchmod(fh.fileno(), _FILE_MODE)
    except (OSError, AttributeError):
        try:
            os.chmod(ERROR_FILE, _FILE_MODE)
        except OSError:
            pass


class _Rec:
    """
    One physical line of the log.

    `entry` is the parsed dict when the line is a displayable error record,
    and None when it is anything else — a corrupt line, a truncated line, a
    JSON object from an older version we do not understand, a line with an
    unparseable date. Those lines are OPAQUE: they are carried through every
    rewrite untouched and are never displayed, never pruned and never deleted.
    Dropping them, as the old `_read_errors()` did on every badge count, was a
    silent, permanent data loss.

    `seq` disambiguates byte-identical lines (the same failure twice inside one
    minute serialises identically), so `(raw, seq)` is a stable identity for a
    record across a re-read under the lock.
    """

    __slots__ = ("raw", "entry", "seq")

    def __init__(self, raw: str, entry, seq: int = 0):
        self.raw = raw
        self.entry = entry
        self.seq = seq

    @property
    def key(self):
        return (self.raw, self.seq)


def _parse_entry(line: str):
    """Parsed dict for a displayable record, else None (keep the line opaque)."""
    try:
        entry = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(entry, dict):
        return None
    try:
        datetime.fromisoformat(entry["date"])
    except (ValueError, TypeError, KeyError):
        return None          # date-less / malformed: preserve, do not display
    return entry


def _records_from_text(text: str) -> list:
    records = []
    seen = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        seq = seen.get(line, 0)
        seen[line] = seq + 1
        records.append(_Rec(line, _parse_entry(line), seq))
    return records


def _serialize(records: list) -> str:
    return "".join(r.raw + "\n" for r in records)


def _read_records() -> list:
    """
    Every line of the log, parsed where possible. READ ONLY — this function
    never writes, never truncates and never reorders the file.
    """
    if not os.path.exists(ERROR_FILE):
        return []
    try:
        fh = open(ERROR_FILE, "r", encoding="utf-8", errors="replace")
    except OSError:
        return []
    with fh:
        with _locked(fh, exclusive=False):
            # A shared lock keeps us off a rewrite in progress. On timeout we
            # read anyway: reading cannot corrupt anything, and a half-seen
            # line simply parses as opaque.
            try:
                text = fh.read()
            except OSError:
                return []
    return _records_from_text(text)


def _transform(fn):
    """
    Read-modify-write the whole log under an EXCLUSIVE lock.

    `fn(records)` must return `(records_or_None, result)`. Returning None for
    the records means "no change" and nothing is written at all.

    The records are re-read from disk INSIDE the lock, so an entry appended by
    another process after the caller's screen was drawn is still present in the
    list that gets written back — that is the fix for the lost-update race.

    On lock timeout the rewrite is SKIPPED (returns (False, None)); a skipped
    rewrite loses nothing, a forced one could.
    """
    os.makedirs(ERROR_DIR, exist_ok=True)
    try:
        fd = os.open(ERROR_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False, None
    with os.fdopen(fd, "r+", encoding="utf-8", errors="replace") as fh:
        with _locked(fh, exclusive=True) as got:
            if not got:
                return False, None
            try:
                fh.seek(0)
                text = fh.read()
            except OSError:
                return False, None
            records = _records_from_text(text)
            new_records, result = fn(records)
            if new_records is None:
                _chmod_private(fh)
                return True, result
            payload = _serialize(new_records)
            fh.seek(0)
            fh.write(payload)
            fh.truncate()
            fh.flush()
            _chmod_private(fh)
            return True, result


# ─── Log / Read ────────────────────────────────────────────────────────────────

def log_error(context: str, message: str, resolved: bool = False):
    """Append one error to the log. Never rewrites the file."""
    os.makedirs(ERROR_DIR, exist_ok=True)

    text = _redact(message if isinstance(message, str) else str(message)) or ""
    # Explained from the REDACTED text: that is the string the screen
    # re-explains later, so the stored and the rendered answer agree.
    info = explain(text)
    entry = {
        "date":     datetime.now().isoformat()[:16],
        # The context is redacted too. It is NOT a safe constant: callers build
        # it from a clone target path (`clone:{target_path}`) and from an API
        # path (`api:{path}`), and an API path is a URL fragment that can carry
        # `?private_token=…`. Only `message` used to be scrubbed, so a token
        # riding in the context was written to disk in the clear.
        "context":  _redact(context if isinstance(context, str)
                            else str(context))[:300],
        # Redact FIRST, then cap: a cap that cut a token in half would leave
        # a fragment the redaction rules no longer recognise.
        "message":  _keep_tail(text),
        "code":     info.code,
        "type":     info.type,
        "title":    info.title,
        "cause":    info.cause,
        "fix":      info.fix,
        "instance": current_instance(),
        "resolved": resolved,
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"

    # O_APPEND + 0600 at creation time: the file is never momentarily world
    # readable, and the write is positioned at EOF atomically by the kernel, so
    # two processes appending at once cannot overwrite each other.
    fd = os.open(ERROR_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        with _locked(fh, exclusive=True):
            # A lock timeout here is harmless — see the O_APPEND note above —
            # so we append regardless rather than drop the error on the floor.
            fh.write(line)
            fh.flush()
        _chmod_private(fh)      # mode is re-asserted even on a pre-existing file

def redact_obj(obj, _depth: int = 0):
    """
    `redact()` applied to every string inside a JSON-shaped structure.

    A per-group error artifact is a nested dict of lists of dicts whose values
    are raw git stderr and raw remote URLs. Redacting only the top-level
    "message" — which is all a per-field call can do — leaves the URL, the
    detail and any nested payload untouched. Dict KEYS are scrubbed as well:
    a key is a string that reached us from outside and can carry a secret.

    Non-string scalars are returned unchanged. Recursion is depth-bounded so a
    pathological structure cannot blow the stack on the way to disk.
    """
    if _depth > 20:
        return "[REDACTED-DEPTH]"
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {redact(k) if isinstance(k, str) else k: redact_obj(v, _depth + 1)
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        seq = [redact_obj(v, _depth + 1) for v in obj]
        return tuple(seq) if isinstance(obj, tuple) else seq
    return obj


def write_private_json(path: str, data, indent: int = 2) -> str:
    """
    Write `data` as JSON to `path`, redacted, mode 0600, atomically.

    Three properties the plain `open(path, "w"); json.dump(...)` this replaces
    did not have:

      * REDACTED — every string in the structure goes through `redact_obj()`,
        so a token embedded in a remote URL or in captured git stderr cannot
        reach the file;
      * 0600 — the fd is created with the mode, so the file is never even
        momentarily group/world readable. The old writer inherited the
        process umask and produced a 0644 artifact sitting in the clone tree,
        which is exactly the tree a user tars up and hands to a colleague;
      * ATOMIC — written to a temp file in the SAME directory and then
        os.replace()d, so a crash or a concurrent reader never sees a
        half-written artifact, and the mode is right before the name exists.

    Returns the path. Raises OSError on failure — the caller decides whether a
    failed artifact is fatal; silently swallowing it here would be a lie about
    what is on disk.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    payload = json.dumps(redact_obj(data), indent=indent, ensure_ascii=False)

    tmp = os.path.join(directory, ".%s.tmp-%d" % (os.path.basename(path),
                                                  os.getpid()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        # os.replace is atomic within a filesystem and preserves our 0600.
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # Re-assert the mode: `path` may have pre-existed as 0644 on a filesystem
    # where replace kept the destination's inode semantics, and a re-run must
    # repair a artifact written by an older version rather than leave it open.
    try:
        os.chmod(path, _FILE_MODE)
    except OSError:
        pass
    return path


def _write_errors(entries: list) -> None:
    """
    Compatibility choke point: replace the displayable entries of the log with
    `entries`, preserving every opaque line.

    Prefer the targeted transforms (`_mark_resolved`, `_clear_resolved`,
    `prune_old_entries`) — they are race-free because they re-read under the
    lock. This one still exists for callers that hold a full list.
    """
    def fn(records):
        kept = [r for r in records if r.entry is None]      # opaque survives
        kept += [_Rec(json.dumps(e, ensure_ascii=False), e) for e in entries]
        return kept, None

    _transform(fn)


def _read_errors() -> list:
    """
    Displayable, non-expired entries. NON-DESTRUCTIVE.

    The old implementation rewrote the whole file on every call, including from
    the main-menu badge that only counts unresolved entries. That made two
    concurrent CLI instances lose entries (read-modify-write race) and made the
    dropping of corrupt / date-less lines permanent. Reading is now a pure
    read; expiry is applied to the RETURNED VIEW only, and the file is only
    ever shrunk by the explicit `prune_old_entries()` path.
    """
    cutoff = datetime.now() - timedelta(days=MAX_DAYS)
    out = []
    for r in _read_records():
        if r.entry is None:
            continue
        try:
            if datetime.fromisoformat(r.entry["date"]) >= cutoff:
                out.append(r.entry)
        except (ValueError, TypeError, KeyError):
            continue
    return out


# ─── Instance attribution ──────────────────────────────────────────────────────
#
# The log is shared by every configured instance. Records written since the
# instance tag carry `instance`; older ones do not, and without attribution
# two instances show each other's failures. An untagged record is
# attributed by EVIDENCE — exactly one instance host named in its text, or a
# path under exactly one instance's clone root (longest root wins, so a
# folder-per-instance root beats the shared base). A record with no evidence
# keeps owner '' and is shown in its own bucket: an unattributable failure may
# still be the user's, so it is never silently dropped.

def _instance_hints() -> list:
    """
    [(name, host, clone_root)] for every configured instance.

    Read only. Any failure yields [] — the Error Log must still render, with
    everything unattributed, when instances.json is missing or broken; that is
    why the except below is deliberately broad.
    """
    try:
        import instances as _inst        # lazy: instances imports half the app
        data = _inst._load()
    except Exception:
        return []
    hints = []
    for inst in data.get("instances") or []:
        if not isinstance(inst, dict) or not inst.get("name"):
            continue
        host = (urlsplit(str(inst.get("url") or "")).hostname or "").lower()
        try:
            base = _inst._paths.anchor(_inst._base_of(inst))
            folder = (inst.get("folder") or "").strip()
            root = os.path.join(base, folder) if folder else base
        except Exception:
            root = ""
        hints.append((str(inst["name"]), host, root or ""))
    return hints


def _owner(entry: dict, hints) -> str:
    """The instance a record belongs to: its tag, else inferred, else ''."""
    tagged = entry.get("instance")
    if isinstance(tagged, str) and tagged:
        return tagged
    text = ("%s\n%s" % (entry.get("context") or "",
                        entry.get("message") or "")).lower()
    by_host = {name for name, host, _root in hints
               if host and re.search(r"(?<![\w.\-])%s(?![\w\-]|\.\w)"
                                     % re.escape(host), text)}
    if len(by_host) == 1:
        return by_host.pop()
    path = _context_target(entry.get("context")) or ""
    best, best_len = "", 0
    for name, _host, root in hints:
        root = root.rstrip("/")
        if root and path.startswith(root + "/") and len(root) > best_len:
            best, best_len = name, len(root)
    return best


def unresolved_count(instance: str | None = None) -> int:
    """
    Badge helper: count only. Touches nothing on disk.

    `instance=None` means the active instance (GITLAB_CLI_INSTANCE) when one
    is set, else every instance. A scoped count also includes older entries
    that cannot be attributed to ANY instance: the Error Log screen lists them
    under every instance, and the badge must not hide what the screen shows.
    """
    if instance is None:
        instance = current_instance()
    open_entries = [e for e in _read_errors() if not e.get("resolved")]
    if not instance:
        return len(open_entries)
    hints = _instance_hints()
    return sum(1 for e in open_entries if _owner(e, hints) in (instance, ""))

def prune_old_entries() -> int:
    """
    Delete entries older than MAX_DAYS. The ONLY path in this module that
    shrinks the log without the user asking for it, and it is called only from
    the Error Log screen — never from a badge count.

    Opaque lines are never pruned: we cannot prove how old an unparseable line
    is, so it is kept.
    """
    cutoff = datetime.now() - timedelta(days=MAX_DAYS)

    def fn(records):
        kept = []
        dropped = 0
        for r in records:
            if r.entry is None:
                kept.append(r)
                continue
            try:
                fresh = datetime.fromisoformat(r.entry["date"]) >= cutoff
            except (ValueError, TypeError, KeyError):
                kept.append(r)
                continue
            if fresh:
                kept.append(r)
            else:
                dropped += 1
        if not dropped:
            return None, 0
        return kept, dropped

    ok, dropped = _transform(fn)
    return dropped or 0


def _mark_resolved(keys) -> int:
    """Flag the records identified by `keys` resolved, under the lock."""
    wanted = list(keys)
    if not wanted:
        return 0

    def fn(records):
        index = {r.key: r for r in records}
        count = 0
        for k in wanted:
            r = index.get(k)
            if r is None or r.entry is None or r.entry.get("resolved"):
                continue
            r.entry["resolved"] = True
            r.raw = json.dumps(r.entry, ensure_ascii=False)
            count += 1
        if not count:
            return None, 0
        return records, count

    ok, count = _transform(fn)
    return count or 0


def _clear_resolved(scope_keys) -> int:
    """
    Delete resolved entries — but ONLY those inside `scope_keys`, the set of
    records this screen actually read and showed the user.

    This is the errors.py half of the incident rule: a success (or a clear) in
    one context must never erase a failure recorded in another. A record that
    arrived from a different process, a different group, or after this screen
    was drawn is outside the scope set and is untouchable here. Unresolved
    entries and opaque lines are never deletable by this path at all.
    """
    scope = set(scope_keys)
    if not scope:
        return 0

    def fn(records):
        kept, dropped = [], 0
        for r in records:
            if (r.key in scope and r.entry is not None
                    and r.entry.get("resolved")):
                dropped += 1
                continue
            kept.append(r)
        if not dropped:
            return None, 0
        return kept, dropped

    ok, dropped = _transform(fn)
    return dropped or 0


# ─── Stale-target cleanup (an EXPLICIT maintenance action) ─────────────────────
#
# WHAT THIS IS FOR
# ----------------
# Before the test suite was isolated (previous wave) every regression run
# appended real records to the user's live log, naming throwaway directories
# such as `clone:/tmp/gitlabcli_test_ab12cd34/MasterGroups/x`. Isolation now
# holds, so no new residue is produced, but the residue already on disk kept
# the main-menu badge permanently alarming about failures that never happened
# to the user, on directories that no longer exist. An alarm that is always on
# is an alarm nobody reads — so the badge has to become truthful again, which
# means the dead rows have to be removable.
#
# THE RULE: "STALE TARGET"
# ------------------------
# A record is a cleanup CANDIDATE if and only if all of the following hold:
#
#   1. the line PARSED into a record. An opaque line is never a candidate —
#      not "probably fine to delete", never a candidate. A line the parser
#      does not understand may be the single most important line in the file.
#   2. its `context` names a filesystem target, i.e. it has the shape
#      `<op>:<absolute path>` (`clone:/srv/x`, `pull:/srv/x`). Contexts that
#      are not filesystem targets — `api:groups/999/members/all` — are
#      structurally ineligible and can never be touched by this path.
#   3. that target path does NOT exist, and
#   4. its PARENT directory does not exist either.
#
# Why this rule and not "the path is under /tmp": a /tmp test is a property of
# the CONTENT of the string, and it becomes wrong the first day somebody
# legitimately clones into /tmp — their real failure would be deleted as
# "residue". Non-existence is a property of the WORLD, checked at cleanup time:
# a record whose target is gone can no longer be acted on, retried, inspected
# or fixed, because there is nothing there to act on.
#
# Condition 4 is what makes the rule conservative rather than clever. A live
# clone tree whose one repository directory was removed by hand still has its
# parent (the group folder / MasterGroups) in place, so its failure is KEPT.
# Only a target whose whole containing directory has evaporated — which is
# exactly what `tempfile.mkdtemp()` + `shutil.rmtree()` leaves behind — is
# considered stale.
#
# FALSE POSITIVES (a real failure removed)
#   * The user really cloned into a directory tree and then deleted the whole
#     tree — parent included — and still wanted the failure record. The record
#     is then removed. This is accepted because (a) the cleanup only ever runs
#     when the user asks for it and confirms a shown count, and (b) the full
#     pre-cleanup log is written to a dated backup first, so the record is
#     recoverable.
#   * An unmounted network/removable target looks non-existent. Mitigated the
#     same way: nothing runs unprompted, and the preview shows the paths.
#
# FALSE NEGATIVES (residue that survives)
#   * Residue whose temp directory happens to have been recreated, or whose
#     context is not a path at all (the four `api:groups/999999999/...` rows
#     produced by the suite are of this kind). These simply stay. That is the
#     correct direction to be wrong in: an entry kept is a cosmetic annoyance,
#     an entry deleted is data loss.
#
# WHAT THIS IS NOT
# ----------------
# It is NOT part of counting. `unresolved_count()` and `_read_errors()` remain
# pure reads. The defect this file already carried once was that READING
# rewrote the file — the badge count silently dropped records on every main
# menu draw. Re-implementing that as "the badge tidies up stale rows while it
# counts" would be the same defect in a new costume. Removal happens only in
# `remove_stale_targets()`, which is reached only from the prompted
# `cmd_cleanup_stale()` screen, and the predicate is re-verified under the
# exclusive lock before any record is dropped.

_TARGET_CONTEXT_RE = re.compile(
    r"^(?P<op>[A-Za-z][A-Za-z0-9_.\-]*):(?P<path>/[^\s]*)$")


def _context_target(context):
    """
    The absolute filesystem path a context refers to, or None.

    None means "this record is not about a filesystem target" and therefore
    "this record can never be a stale-target candidate".
    """
    if not isinstance(context, str):
        return None
    m = _TARGET_CONTEXT_RE.match(context.strip())
    if not m:
        return None
    path = m.group("path")
    if path.rstrip("/") == "":           # "/" is not a clone target
        return None
    return path


def _is_stale_target(rec) -> bool:
    """True only for a PARSED record whose target and target's parent are gone."""
    if rec.entry is None:
        return False                     # opaque: never deletable, full stop
    target = _context_target(rec.entry.get("context"))
    if target is None:
        return False
    # lexists, not exists: a dangling symlink still means "something is here".
    if os.path.lexists(target):
        return False
    parent = os.path.dirname(target.rstrip("/"))
    if parent in ("", "/"):
        return False                     # never call a top-level target stale
    if os.path.lexists(parent):
        return False                     # tree is alive; keep the failure
    return True


def stale_target_records() -> list:
    """
    The candidate records. READ ONLY — touches nothing on disk.

    Safe to call from anywhere, including a display path, precisely because it
    cannot modify the log.
    """
    return [r for r in _read_records() if _is_stale_target(r)]


def stale_target_count() -> int:
    """How many records are cleanup candidates right now. Read only."""
    return len(stale_target_records())


def _backup_path(when=None) -> str:
    ts = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return os.path.join(ERROR_DIR, "errors.log.bak-%s" % ts)


def _write_backup(text: str):
    """Dated 0600 copy of the log's exact pre-cleanup bytes. Path, or None."""
    path = _backup_path()
    try:
        os.makedirs(ERROR_DIR, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
        return path
    except OSError:
        return None


def remove_stale_targets(keys):
    """
    Delete the stale-target records named by `keys`. Returns (dropped, backup).

    `keys` are the `(raw, seq)` identities the screen actually showed the user,
    exactly like `_clear_resolved()`: an entry appended by another process
    after the preview was drawn was never shown and cannot be destroyed here.

    A key is a NOMINATION, not an authorisation: `_is_stale_target()` is
    re-evaluated for every record inside the exclusive lock, so a directory
    that came back between preview and confirmation is kept, and an opaque
    line can never be dropped even if its key is passed in.

    The whole pre-cleanup file is written to a dated backup before the rewrite.
    """
    scope = set(keys)
    if not scope:
        return 0, None
    out = {}

    def fn(records):
        kept, dropped = [], 0
        for r in records:
            if r.key in scope and _is_stale_target(r):
                dropped += 1
                continue
            kept.append(r)
        if not dropped:
            return None, 0
        out["backup"] = _write_backup(_serialize(records))
        return kept, dropped

    ok, dropped = _transform(fn)
    return (dropped or 0), out.get("backup")


def cmd_cleanup_stale(cfg: dict = None, records=None) -> int:
    """
    The prompted maintenance screen. Previews the candidates, requires a typed
    confirmation, then removes them. Answering anything but `yes` changes
    nothing on disk.
    """
    print_section("Error Log — remove entries for directories that no longer exist")

    candidates = stale_target_records() if records is None else \
        [r for r in records if _is_stale_target(r)]

    if not candidates:
        print(f"\n  {GREEN}✔ Nothing to clean up.{RESET}\n")
        return 0

    total = len(_read_records())
    print(f"\n  {BOLD}Log entries      :{RESET}  {total}")
    print(f"  {BOLD}Removable        :{RESET}  {len(candidates)}")
    print(f"  {DIM}An entry is removable only when its target directory AND the{RESET}")
    print(f"  {DIM}directory above it are both gone. Unparseable lines and{RESET}")
    print(f"  {DIM}entries whose folder still exists are never removed.{RESET}\n")

    shown = candidates[:10]
    for r in shown:
        target = _context_target(r.entry.get("context"))
        print(f"    {DIM}{r.entry.get('date', '?')}{RESET}  {target}")
    if len(candidates) > len(shown):
        print(f"    {DIM}… and {len(candidates) - len(shown)} more{RESET}")

    print(f"\n  {YELLOW}A dated backup of the whole log is written first.{RESET}")
    try:
        answer = input(f"{BOLD}  Remove {len(candidates)} entries? type 'yes': {RESET}")
    except (KeyboardInterrupt, EOFError):
        print(f"\n  {DIM}Cancelled — nothing was changed.{RESET}\n")
        return 0

    if answer.strip().lower() != "yes":
        print(f"\n  {DIM}Cancelled — nothing was changed.{RESET}\n")
        return 0

    dropped, backup = remove_stale_targets([r.key for r in candidates])
    if backup:
        print(f"\n  {DIM}Backup: {backup}{RESET}")
    print(f"  {GREEN}✔ Removed {dropped} stale entries.{RESET}\n")
    return dropped


# ─── Display ───────────────────────────────────────────────────────────────────

TYPE_COLORS = {
    "Network":  "\033[33m",   # yellow
    "Auth":     "\033[31m",   # red
    "Access":   "\033[31m",
    "NotFound": "\033[34m",   # blue
    "Server":   "\033[31m",
    "Git":      "\033[36m",   # cyan
    "LFS":      "\033[35m",   # magenta
    "Disk":     "\033[31m",
    "Config":   "\033[35m",
    "Safety":   "\033[33m",
    "Local":    "\033[37m",
    "Unknown":  "\033[37m",   # white
    # Types written by the pre-catalog table; old rows still carry them.
    "Token":    "\033[31m",
    "API":      "\033[34m",
}

# The pre-catalog fallback. "Ask your admin" is not advice, so a row carrying
# it is re-explained at render time instead of being shown verbatim.
_LEGACY_TITLE = "Unknown error"
_LEGACY_FIX = "Read the log and follow up with your admin."

_MASTER = "MasterGroups"           # instances.DEFAULT_MASTER; not imported so
                                   # errors.py keeps no import-time dependency
# Display-only prefix match for shortening logged paths; nothing is created.
_TEMP_ROOTS = ("/tmp/", "/var/tmp/")  # noqa: S108  # nosec B108


def _clip(text, width: int = 96) -> str:
    """One display line: whitespace folded, cut with an ellipsis."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= width else flat[:width - 1] + "…"


def _explain_entry(entry: dict) -> dict:
    """
    code/type/title/cause/fix for a record, re-derived from its message NOW.

    Rows written before the catalog say "Unknown error" for failures the
    catalog explains today (DNS, timeout, LFS smudge), so the stored fields
    are a fallback, not the truth. When the catalog still cannot explain the
    text, the real message becomes the cause — the user must always see what
    actually went wrong.
    """
    message = entry.get("message") or ""
    info = explain(message) if message else None
    if (info is None or info.code == "unknown") and entry.get("code"):
        info = by_code(entry["code"]) or info
    if info is not None and info.code != "unknown":
        return {"code": info.code, "type": info.type, "title": info.title,
                "cause": info.cause, "fix": info.fix}
    title = entry.get("title") or ""
    fix = entry.get("fix") or ""
    return {
        "code":  "unknown",
        "type":  entry.get("type") or (info.type if info else "Unknown"),
        "title": (title if title and title != _LEGACY_TITLE
                  else (info.title if info else _LEGACY_TITLE)),
        "cause": summarize(message, 110) if message else (entry.get("cause") or ""),
        "fix":   (fix if fix and fix != _LEGACY_FIX
                  else (info.fix if info else "")),
    }


def _short_path(path: str, hints) -> str:
    """A target path as the user thinks of it: `group/sub/project`."""
    for _name, _host, root in sorted(hints, key=lambda h: -len(h[2])):
        root = root.rstrip("/")
        if root and path.startswith(root + "/"):
            return path[len(root) + 1:]
    segs = [s for s in path.split("/") if s]
    if path.startswith(_TEMP_ROOTS):
        # Drop `tmp/<random mkdtemp name>`: the random part differs on every
        # retry and would stop identical failures folding into one ×N row.
        return "(temp) " + "/".join(segs[2:][-3:] or segs[-1:])
    if _MASTER in segs:
        segs = segs[segs.index(_MASTER) + 1:]
    if len(segs) > 4:
        segs = ["…"] + segs[-3:]
    return "/".join(segs) or path


def _short_context(context, hints) -> str:
    """`clone:/home/…/MasterGroups/group-a/x` → `clone group-a/x`."""
    ctx = context if isinstance(context, str) else str(context or "")
    op, sep, rest = ctx.partition(":")
    if not sep or not re.fullmatch(r"[A-Za-z][\w.\-]*", op):
        return _clip(ctx, 70)
    rest = rest.strip()
    if rest.startswith("/"):
        rest = _short_path(rest, hints)
    return _clip("%s %s" % (op, rest), 70)


def _fresh_records() -> list:
    """Parsed, non-expired records with their keys. Read only."""
    cutoff = datetime.now() - timedelta(days=MAX_DAYS)
    out = []
    for r in _read_records():
        if r.entry is None:
            continue                     # opaque line: preserved, not shown
        try:
            if datetime.fromisoformat(r.entry["date"]) >= cutoff:
                out.append(r)
        except (ValueError, TypeError, KeyError):
            continue
    return out


def _build_rows(records, hints) -> list:
    """
    Collapse repeats into one row per (where, message, resolved, owner).

    `where` is the SHORT context, so the same failure retried from two
    throw-away temp clones of one project folds into one ×N row. Every
    member record's key travels with the row: resolving a row resolves all
    of its duplicates, and a clear can only reach records the screen showed.
    """
    groups = {}
    for r in records:
        e = r.entry
        owner = _owner(e, hints)
        where = _short_context(e.get("context"), hints)
        key = (where, e.get("message") or "", bool(e.get("resolved")), owner)
        groups.setdefault(key, []).append(r)
    rows = []
    for (where, _msg, resolved, owner), recs in groups.items():
        recs.sort(key=lambda r: r.entry["date"], reverse=True)
        rows.append({"records": recs, "entry": recs[0].entry, "where": where,
                     "owner": owner, "resolved": resolved,
                     "info": _explain_entry(recs[0].entry)})
    rows.sort(key=lambda row: row["entry"]["date"], reverse=True)
    return rows


def _instance_sections(rows, current: str):
    """([(heading, rows)], hidden_count) for the active instance's view."""
    mine, inferred, unknown, hidden = [], [], [], 0
    for row in rows:
        if row["owner"] == current:
            tagged = any(r.entry.get("instance") for r in row["records"])
            (mine if tagged else inferred).append(row)
        elif not row["owner"]:
            unknown.append(row)
        else:
            hidden += len(row["records"])
    sections = [
        ("Instance %s" % current, mine),
        ("Older, untagged — attributed to %s by host/path" % current, inferred),
        ("Older, untagged — instance unknown", unknown),
    ]
    return [(h, rs) for h, rs in sections if rs], hidden


def _render_row(number: int, row: dict, show_owner: bool) -> None:
    """At most four short lines: headline, why, fix, dim where."""
    e, info = row["entry"], row["info"]
    color = TYPE_COLORS.get(info["type"], "\033[37m")
    n = len(row["records"])
    count = f"  {YELLOW}×{n}{RESET}" if n > 1 else ""
    mark = f"  {GREEN}✔ resolved{RESET}" if row["resolved"] else ""
    when = str(e.get("date", "?"))[:16].replace("T", " ")
    print(f"  {CYAN}{number:>2}.{RESET} {color}[{info['type']}]{RESET} "
          f"{BOLD}{_clip(info['title'], 48)}{RESET}{count}  {DIM}{when}{RESET}{mark}")
    if info["cause"]:
        print(f"      why: {_clip(info['cause'], 100)}")
    if info["fix"]:
        print(f"      {YELLOW}fix: {_clip(info['fix'], 100)}{RESET}")
    where = row["where"]
    if show_owner:
        where = "%s · %s" % (row["owner"] or "untagged", where)
    # A catalog cause is generic; the real line from git/the API is what
    # proves it. An unknown row already shows that line as its cause.
    if info["code"] != "unknown" and e.get("message"):
        where += '  "%s"' % summarize(e["message"], 70)
    print(f"      {DIM}{_clip(where, 100)}{RESET}")


def _ask(prompt: str, valid, default: str = None):
    """Re-prompt until the answer is one of `valid`. None on Ctrl-C / EOF."""
    while True:
        try:
            raw = input(prompt)
        except (KeyboardInterrupt, EOFError):
            return None
        key = resolve_choice(raw, valid, default=default)
        if key is not None:
            return key
        print_unknown_choice(raw, valid)


def _filter_by_type(records) -> list | None:
    """Records whose CURRENT explanation has the typed type. None = cancelled."""
    typed = [(r, _explain_entry(r.entry)["type"]) for r in records]
    types = sorted({t for _r, t in typed})
    print(f"\n  Types: {', '.join(types)}")
    try:
        wanted = input("  Type: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return None
    return [r for r, t in typed if t.lower() == wanted]


def cmd_errors(cfg: dict = None):
    """Error log: compact rows with the real cause, filters, resolve, clear."""
    print_section("Error Log")

    # The 30-day cleanup happens HERE — on the explicit Error Log screen, once,
    # deliberately — and never from the main-menu badge.
    prune_old_entries()

    records = _fresh_records()
    if not records:
        print(f"\n  {GREEN}✔ No errors logged!{RESET}\n")
        return

    current = current_instance()
    hints = _instance_hints()
    unresolved = [r for r in records if not r.entry.get("resolved")]
    resolved = [r for r in records if r.entry.get("resolved")]

    print(f"\n  {BOLD}Total     :{RESET}  {len(records)}")
    print(f"  {RED}Unresolved:{RESET}  {RED}{len(unresolved)}{RESET}")
    print(f"  {GREEN}Resolved  :{RESET}  {GREEN}{len(resolved)}{RESET}")
    if current:
        print(f"  {BOLD}Instance  :{RESET}  {current}"
              f"  {DIM}(totals are for every instance){RESET}")
    print(f"  {DIM}Entries older than {MAX_DAYS} days are auto-deleted{RESET}")

    # Read-only: counting candidates never modifies the log. The removal is a
    # separate, confirmed action below (option 's').
    stale = stale_target_count()
    if stale:
        print(f"  {YELLOW}Stale     :{RESET}  {stale}"
              f"  {DIM}(target directory no longer exists — 's' to review){RESET}")
    print()

    print(f"  {CYAN}1.{RESET}  All errors")
    print(f"  {CYAN}2.{RESET}  Unresolved only")
    print(f"  {CYAN}3.{RESET}  Resolved only")
    print(f"  {CYAN}4.{RESET}  Filter by type  (Network/Auth/Git/LFS/...)")
    valid = ["1", "2", "3", "4", "s", "q"]
    if current:
        print(f"  {CYAN}5.{RESET}  All errors, every instance")
        valid.insert(4, "5")
    print(f"  {CYAN}s.{RESET}  Remove entries whose directory no longer exists"
          f"  {DIM}({stale} candidate(s)){RESET}")
    print(f"\n  {YELLOW}q{RESET}  back\n")

    # An unrecognised key is rejected out loud and re-asked; it used to fall
    # into an `else` that silently meant "show everything".
    choice = _ask(f"{BOLD}  Filter [1]: {RESET}", valid, default="1")
    if choice in (None, "q"):
        return
    if choice == "s":
        # Deliberate, prompted, one-purpose action. Reached only because the
        # user typed 's' here; never a side effect of drawing this screen and
        # never a side effect of the main-menu badge count.
        cmd_cleanup_stale(cfg)
        return
    if choice == "2":
        filtered = unresolved
    elif choice == "3":
        filtered = resolved
    elif choice == "4":
        filtered = _filter_by_type(records)
        if filtered is None:
            return
    elif choice in ("1", "5"):
        filtered = records

    # `display` is the ONE canonical ordering: what the user sees AND what the
    # typed numbers index. (An earlier defect printed a sorted list but
    # resolved by position in the unsorted one.)
    rows = _build_rows(filtered, hints)
    if current and choice != "5":
        sections, hidden = _instance_sections(rows, current)
    else:
        sections, hidden = [(None, rows)], 0
    display = [row for _h, rs in sections for row in rs]

    if not display:
        extra = (f" {hidden} belong to other instances — 5 shows them."
                 if hidden else "")
        print_warn("No errors match this filter." + extra)
        return

    # Scope of the destructive actions below: exactly the records THIS screen
    # listed, duplicates included. Anything the user never saw — another
    # instance's failure, an entry appended by a concurrent clone, a record
    # excluded by the active filter — is outside the scope and untouchable.
    scope_keys = [r.key for row in display for r in row["records"]]

    print_separator()
    folded = ""
    if len(scope_keys) != len(display):
        folded = f"  {DIM}({len(scope_keys)} log entries; repeats shown as ×N){RESET}"
    print(f"\n{BOLD}  {len(display)} error(s):{RESET}{folded}\n")

    number = 0
    for heading, section in sections:
        if heading:
            print(f"  {BOLD}{heading}{RESET}")
        for row in section:
            number += 1
            _render_row(number, row, show_owner=not current or choice == "5")
            print()
    if hidden:
        noun = "entry" if hidden == 1 else "entries"
        print(f"  {DIM}{hidden} {noun} from other instances not shown"
              f" — filter 5 lists every instance.{RESET}\n")

    print_separator()
    print(f"\n  {CYAN}r{RESET}  Mark as resolved  (enter numbers; a ×N row resolves all N)")
    print(f"  {CYAN}c{RESET}  Clear resolved (only the entries listed above)")
    print(f"  {YELLOW}q{RESET}  back\n")

    action = _ask(f"{BOLD}  Choice: {RESET}", ["r", "c", "q"])
    if action in (None, "q"):
        return

    if action == "c":
        # Scoped clear: only the resolved records THIS screen read.
        dropped = _clear_resolved(scope_keys)
        print(f"\n  {GREEN}✔ Cleared {dropped} resolved errors.{RESET}\n")
        return

    try:
        nums = input("  Numbers (comma separated, e.g. 1,3,5): ").strip()
    except (KeyboardInterrupt, EOFError):
        return

    indices, rejected = [], []
    for n in nums.split(","):
        n = n.strip()
        if not n:
            continue
        if n.isdigit() and 1 <= int(n) <= len(display):
            idx = int(n) - 1
            if idx not in indices:          # "1,1" must not count twice
                indices.append(idx)
        else:
            rejected.append(n)

    if rejected:
        print_warn("Ignored (not a listed number): " + ", ".join(rejected))
    if not indices:
        print_warn("Invalid number entered.")
        return

    # Identify by each record's (raw line, occurrence) key, never by list
    # position in a re-read list and never by `date + context` (minute
    # resolution: two failures in one minute would share it). The rewrite
    # re-reads under the lock, so a concurrent append survives.
    keys = [r.key for i in indices for r in display[i]["records"]]
    count = _mark_resolved(keys)
    print(f"\n  {GREEN}✔ {count} error(s) marked resolved.{RESET}\n")
