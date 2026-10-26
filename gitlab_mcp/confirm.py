"""Single-use confirmation tokens for the mutating MCP tools.

The rule this module exists to enforce: **an agent may never decide on its own
to change something.** Every write tool is called twice.

1. The first call carries no token. The tool builds a preview, asks the store
   for a token that is bound to *this tool* and *these exact arguments*, and
   returns both. Nothing is executed.
2. The human reads the preview and approves it. The agent calls again with the
   same arguments plus the token. The store verifies the binding, burns the
   token, and only then does the tool execute.

A token is therefore worthless for anything except the one operation the human
actually saw:

* it is bound to the tool name and to a SHA-256 fingerprint of the canonical
  JSON of the arguments (with ``confirm_token`` removed), so changing any
  argument after the preview invalidates it;
* it expires after :data:`DEFAULT_TTL_SECONDS` (15 minutes);
* it is single use — a successful check deletes it;
* comparisons go through :func:`hmac.compare_digest`, and a rejection never
  says *why*, so the caller cannot probe the store.

The clock is injectable so the expiry behaviour is testable without sleeping.

Confirmation modes (``GITLAB_MCP_CONFIRM_MODE``):

* ``agent`` (default, also when unset or empty): the token is returned to the
  caller, as above. This is a drift guard — it proves the arguments did not
  change — but not proof that a human approved: an agent can pass its own
  token straight back.
* ``human``: the token is **not** returned. It is written to a fresh ``0600``
  file in a ``0700`` directory under the user's cache dir
  (``$XDG_CACHE_HOME/gitlab-mcp/confirm`` or ``~/.cache/gitlab-mcp/confirm``;
  ``%LOCALAPPDATA%\\gitlab-mcp\\confirm`` on Windows) and only the file's path
  is returned, with instructions to ask the user to paste the token. Binding,
  single use and expiry are unchanged; the file is deleted when the token is
  used, expires or is evicted. Any other value fails closed to ``human``.
  This keeps the token out of the agent's context; it does not stop an agent
  that can read arbitrary files as the same OS user.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import sys
import time
from collections import OrderedDict
from typing import Any, Callable

__all__ = [
    "CONFIRM_MODE_ENV",
    "DEFAULT_TTL_SECONDS",
    "MAX_PENDING",
    "TOKEN_BYTES",
    "ConfirmStore",
    "canonical_args",
    "confirm_mode",
    "default_token_dir",
    "fingerprint",
]

# 15 minutes, as specified by the MCP contract. Long enough for a human to read
# a merge preview, short enough that a forgotten token is not a standing
# permission to merge.
DEFAULT_TTL_SECONDS = 900

# Entropy of the token itself: 16 random bytes, URL-safe. Guessing is not a
# realistic attack at this size; the args binding is what carries the security.
TOKEN_BYTES = 16

# A pending-confirmation store is a human-sized queue, not a cache. The cap
# stops a looping agent from growing it without bound; the oldest entries are
# evicted first.
MAX_PENDING = 256

# The confirmation token must never be part of what the token is bound to,
# otherwise the fingerprint could never match on the second call.
_TOKEN_ARG = "confirm_token"  # noqa: S105 - an argument name, not a secret

#: Environment variable selecting who must hand the token back.
CONFIRM_MODE_ENV = "GITLAB_MCP_CONFIRM_MODE"
MODE_AGENT = "agent"
MODE_HUMAN = "human"


def confirm_mode(value: str | None = None) -> str:
    """``agent`` or ``human`` for ``value`` (default: the environment).

    Unset or empty means ``agent`` (backward compatible). Anything that is not
    recognisably ``agent`` fails closed to ``human``.
    """
    raw = os.environ.get(CONFIRM_MODE_ENV) if value is None else value
    text = (raw or "").strip().lower()
    if text in ("", MODE_AGENT):
        return MODE_AGENT
    return MODE_HUMAN


def default_token_dir() -> str:
    """Where human-mode token files go: a per-user cache directory."""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or ""
        if not os.path.isabs(base):
            base = os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "gitlab-mcp", "confirm")


def _private_dir(path: str) -> str:
    """Create ``path`` as a 0700 directory and refuse a symlink or a foreign one."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PermissionError(f"refusing token directory {path}: not a "
                              f"real directory")
    if hasattr(os, "getuid"):
        if info.st_uid != os.getuid():
            raise PermissionError(f"refusing token directory {path}: owned "
                                  f"by another user")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.chmod(path, 0o700)
    return path


def _write_token_file(directory: str, token: str) -> str:
    """Write ``token`` to a new 0600 file with a random name; return its path."""
    _private_dir(directory)
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    path = os.path.join(directory, f"confirm-{secrets.token_hex(8)}.token")
    fd = os.open(path, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        os.write(fd, (token + "\n").encode("ascii"))
    finally:
        os.close(fd)
    return path


def _unlink_quietly(path: str | None) -> None:
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _human_message(path: str) -> str:
    return ("Nothing has been changed yet. This server is in human-confirmation "
            "mode: the confirmation token was NOT given to you. Show the "
            "preview to the user verbatim, including every warning, and ask "
            "them to review it and, if they approve, to open the file "
            f"{path} and paste the token it contains back to you. Then call "
            "this same tool again with identical arguments plus "
            "confirm_token set to the pasted value. Do not read that file "
            "yourself and do not re-run the tool to obtain another token.")


def canonical_args(args: dict[str, Any]) -> str:
    """Canonical JSON for ``args``, ignoring ``confirm_token``.

    Key order and whitespace are normalised so that two calls with the same
    arguments in a different order produce the same text. ``default=str``
    keeps the function total: an argument that is not JSON-native still
    contributes its string form rather than raising.
    """
    payload = {k: v for k, v in (args or {}).items() if k != _TOKEN_ARG}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def fingerprint(tool: str, args: dict[str, Any]) -> str:
    """SHA-256 hex digest binding a tool name to a set of arguments."""
    material = f"{tool}\x00{canonical_args(args)}".encode()
    return hashlib.sha256(material).hexdigest()


class _Pending:
    """One outstanding confirmation. Internal; never returned to a caller."""

    __slots__ = ("tool", "digest", "expires_at", "extra", "token_file")

    def __init__(self, tool: str, digest: str, expires_at: float,
                 extra: Any, token_file: str | None = None) -> None:
        self.tool = tool
        self.digest = digest
        self.expires_at = expires_at
        self.extra = extra
        self.token_file = token_file


class ConfirmStore:
    """In-memory store of single-use, argument-bound confirmation tokens.

    The store lives for the lifetime of the server process only: restarting
    the server invalidates every outstanding token, which is the safe
    direction to fail.

    Args:
        ttl_seconds: Lifetime of a token in seconds.
        max_pending: Hard cap on outstanding tokens; the oldest is evicted
            when a new token would exceed it.
        clock: Monotonic-ish source of seconds, injected by the tests.
        mode: ``"agent"`` or ``"human"``; ``None`` reads
            ``GITLAB_MCP_CONFIRM_MODE`` at every :meth:`issue`.
        token_dir: Directory for human-mode token files; ``None`` means
            :func:`default_token_dir` at issue time.
    """

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 max_pending: int = MAX_PENDING,
                 clock: Callable[[], float] | None = None,
                 mode: str | None = None,
                 token_dir: str | None = None) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_pending <= 0:
            raise ValueError("max_pending must be positive")
        self.ttl_seconds = int(ttl_seconds)
        self.max_pending = int(max_pending)
        self._clock = clock or time.monotonic
        self._mode = None if mode is None else confirm_mode(mode)
        self._token_dir = token_dir
        self._pending: "OrderedDict[str, _Pending]" = OrderedDict()

    # ── public API ────────────────────────────────────────────────────────────

    def issue(self, tool: str, args: dict[str, Any],
              extra: Any = None) -> dict:
        """Mint a token bound to ``tool`` and ``args``.

        ``extra`` is server-side state that the second call must not be able to
        influence — ``mr_merge`` stores the head sha it previewed here, so the
        merge is pinned to the commit the human actually saw even though the
        agent never gets to pass the sha itself.

        Returns:
            Agent mode: ``{"confirm_token": str, "expires_in": int,
            "confirm_mode": "agent"}``.
            Human mode: ``{"confirm_token": None, "expires_in": int,
            "confirm_mode": "human", "token_file": str, "message": str}`` —
            the token itself only exists in the 0600 file.
        """
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError("tool must be a non-empty string")

        now = self._clock()
        self._drop_expired(now)
        mode = self.mode
        token = secrets.token_urlsafe(TOKEN_BYTES)
        digest = fingerprint(tool, args)
        token_file = None
        if mode == MODE_HUMAN:
            token_file = _write_token_file(
                self._token_dir or default_token_dir(), token)
        self._pending[token] = _Pending(
            tool=tool,
            digest=digest,
            expires_at=now + self.ttl_seconds,
            extra=extra,
            token_file=token_file,
        )
        while len(self._pending) > self.max_pending:
            _, evicted = self._pending.popitem(last=False)
            _unlink_quietly(evicted.token_file)
        if mode == MODE_HUMAN:
            return {"confirm_token": None, "expires_in": self.ttl_seconds,
                    "confirm_mode": MODE_HUMAN, "token_file": token_file,
                    "message": _human_message(token_file)}
        return {"confirm_token": token, "expires_in": self.ttl_seconds,
                "confirm_mode": MODE_AGENT}

    @property
    def mode(self) -> str:
        """The confirmation mode in force for the next :meth:`issue`."""
        return self._mode if self._mode is not None else confirm_mode()

    def check(self, tool: str, args: dict[str, Any],
              token: str | None) -> tuple[bool, Any]:
        """Verify and burn ``token``.

        Returns:
            ``(True, extra)`` when the token exists, has not expired, and was
            issued for exactly this tool and these arguments — and the token is
            consumed, so a second check of the same token fails.
            ``(False, None)`` in every other case, with no indication of which
            check failed.
        """
        if not isinstance(token, str) or not token:
            return False, None

        now = self._clock()
        self._drop_expired(now)
        entry = self._take(token)
        if entry is None:
            return False, None
        if entry.expires_at <= now:
            return False, None
        if not hmac.compare_digest(entry.tool, tool):
            return False, None
        if not hmac.compare_digest(entry.digest, fingerprint(tool, args)):
            return False, None
        return True, entry.extra

    def pending_count(self) -> int:
        """How many tokens are outstanding (expired ones dropped first)."""
        self._drop_expired(self._clock())
        return len(self._pending)

    def clear(self) -> None:
        """Forget every outstanding token (and delete its token file)."""
        for entry in self._pending.values():
            _unlink_quietly(entry.token_file)
        self._pending.clear()

    # ── internals ─────────────────────────────────────────────────────────────

    def _take(self, token: str) -> _Pending | None:
        """Pop the entry for ``token``, comparing in constant time.

        A plain dict lookup would leak through timing; every stored token is
        compared with :func:`hmac.compare_digest` instead. The entry is removed
        whether or not the remaining checks pass, so a token is burned by the
        attempt and cannot be retried with different arguments.
        """
        found: str | None = None
        for candidate in self._pending:
            if hmac.compare_digest(candidate, token):
                found = candidate
        if found is None:
            return None
        entry = self._pending.pop(found)
        _unlink_quietly(entry.token_file)
        return entry

    def _drop_expired(self, now: float) -> None:
        expired = [t for t, e in self._pending.items() if e.expires_at <= now]
        for token in expired:
            entry = self._pending.pop(token, None)
            if entry is not None:
                _unlink_quietly(entry.token_file)
