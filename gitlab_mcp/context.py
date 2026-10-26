"""Instance context for the GitLab MCP server.

``GitLabContext`` is the single object every reporting function, every action
and every MCP tool talks to. It owns three things nothing else should have to
think about:

* **Which GitLab.** The instance is selected by name, or the active one from
  ``~/.gitlab-cli/instances.json`` when no name is given. The instance config
  is built by the existing ``instances.build_cfg`` so the MCP server and the
  CLI can never disagree about a URL, a clone directory or a token source.
* **The credential.** The token lives in one private attribute and inside one
  private :class:`requests.Session` whose ``PRIVATE-TOKEN`` header is set
  once. There is no public attribute, property or accessor that returns it,
  ``repr()``/``str()`` never contain it, and every string this module hands
  back passes through :func:`redact`.
* **stdout.** The MCP server speaks its protocol on stdout, while the
  gitlab-cli functions this package reuses print progress there. Any call into
  that code goes through :meth:`GitLabContext.run_captured`, which redirects
  both streams into a buffer and returns the captured text redacted.

Importing this module (and the gitlab-cli modules it imports) performs no
network request and asks no question on the terminal: ``instances`` and
``credentials`` only read files, and they are read lazily — the first config
read happens when a ``GitLabContext`` is constructed, not at import time.
"""

from __future__ import annotations

import io
import os
import re
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Callable, Iterable
from urllib.parse import quote

import requests

import instances as gl_instances
import paths as gl_paths

__all__ = [
    "TIMEOUT",
    "VERIFY_TLS",
    "READ_ONLY_ENV",
    "GitLabError",
    "GitLabTransportError",
    "GitLabAuthError",
    "GitLabPermissionError",
    "GitLabNotFoundError",
    "GitLabRateLimitError",
    "GitLabHTTPError",
    "GitLabReadOnlyError",
    "GitLabContext",
    "redact",
    "list_instances_public",
    "list_instance_names",
    "read_only",
]

#: Seconds before any single HTTP request is abandoned. Matches ``api.TIMEOUT``
#: in gitlab-cli so a slow instance behaves the same way in both front ends.
TIMEOUT = 20

#: TLS verification, matching gitlab-cli: it calls ``requests.get`` without a
#: ``verify`` argument, i.e. verification on with the certifi bundle. Kept as a
#: named constant so the value is visible rather than implied by an omission.
VERIFY_TLS = True

#: Any value other than a clear "off" value turns read-only ON (fail closed):
#: ``GITLAB_MCP_READ_ONLY=enabled`` must not silently leave writes enabled.
READ_ONLY_ENV = "GITLAB_MCP_READ_ONLY"

_API_PREFIX = "/api/v4"

#: Longest error body quoted back to a client. A GitLab HTML error page is
#: useless and unbounded; the first few hundred characters of a JSON error are
#: the whole message.
_MAX_ERROR_CHARS = 600

#: The only values of ``GITLAB_MCP_READ_ONLY`` that leave writes enabled.
#: Unset and empty count as off; everything else is read-only.
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})

#: Inherited variables removed from :meth:`GitLabContext.git_env`: each one
#: either re-points git at another repository/config or names a program.
_UNSAFE_GIT_ENV = ("GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
                   "GIT_CONFIG_GLOBAL", "GIT_DIR", "GIT_WORK_TREE",
                   "GIT_EXEC_PATH", "GIT_EXTERNAL_DIFF", "GIT_SSH",
                   "GIT_SSH_COMMAND", "GIT_PROXY_COMMAND")


# ─── Redaction ────────────────────────────────────────────────────────────────
#
# Captured gitlab-cli output, a git stderr line and a GitLab error body can all
# carry a credential. Every one of them reaches a client, so they are scrubbed
# here rather than at each call site. The rules are ordered: the `user:pass@`
# rule must run before the bare-userinfo rule, otherwise the latter eats the
# username half of a pair and the password survives.

_REDACTED = "[REDACTED]"

_SECRET_RULES: list[tuple[re.Pattern[str], Any]] = [
    # GitLab tokens of every flavour: glpat-, gldt-, glrt-, glsoat-, glcbt-…
    (re.compile(r"\bgl[a-z]{2,8}-[A-Za-z0-9_\-]{8,}"), "[REDACTED-TOKEN]"),
    # URL userinfo carrying a password: https://oauth2:secret@host,
    # https://alice:secret@host. The username is diagnostic and kept.
    (re.compile(r"(?<=://)([^/\s:@]{1,64}):(?!\[REDACTED)[^/\s@]+(?=@)"),
     r"\1:" + _REDACTED),
    # URL userinfo that IS the token: https://glpat-…@host once the rule above
    # has had its turn, or any other single-field userinfo.
    (re.compile(r"(?<=://)(?!\[REDACTED)[^/\s:@]+(?=@)"),
     "[REDACTED-USERINFO]"),
    # Token in a query string: ?private_token=… / &access_token=…
    (re.compile(r"(?i)([?&](?:private[_-]?token|access[_-]?token|token|"
                r"job[_-]?token)=)[^&\s\"']+"), r"\1" + _REDACTED),
    # Header value, as printed by curl -v or an echoed request. The auth scheme
    # is kept (Bearer vs Basic says which code path built the request); the
    # credential after it is destroyed.
    (re.compile(r"(?i)\b(PRIVATE-TOKEN|Authorization|Proxy-Authorization|"
                r"X-Gitlab-Token|X-Api-Key)(\s*[:=]\s*)"
                r"(Bearer|Basic|Token|OAuth)?[ \t]*(?!\[REDACTED)\S+"),
     lambda m: "%s%s%s%s" % (m.group(1), m.group(2),
                             (m.group(3) + " ") if m.group(3) else "",
                             _REDACTED)),
    # A secret-shaped key/value pair, which is the env-var and config-dump
    # shape: GITLAB_TOKEN=…, "private_token": "…", password=…
    (re.compile(r"(?i)([A-Za-z0-9_\-]*(?:password|secret|api[_\-]?key|token|"
                r"credential)s?)(\s*[:=]\s*\"?)(?!\[REDACTED)"
                r"[^\s\"',;)}\]]{8,}"), r"\1\2" + _REDACTED),
]


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Strip anything that looks like a credential out of ``text``.

    Masks GitLab tokens (``glpat-``…), URL userinfo in both the
    ``user:pass@host`` and ``token@host`` shapes, ``PRIVATE-TOKEN`` and
    ``Authorization`` header values, token query parameters, and any literal
    string passed in ``secrets`` — which is how a context removes its own
    token even when it does not match a known pattern.

    Args:
        text: Text that may contain a credential. ``None``/``""`` pass through.
        secrets: Literal values to remove verbatim before pattern matching.

    Returns:
        The redacted text.
    """
    if not text:
        return text
    out = str(text)
    # Literals first: a token that does not look like a token is still a token.
    for secret in secrets:
        if secret and len(secret) >= 4:
            out = out.replace(secret, _REDACTED)
    for pattern, replacement in _SECRET_RULES:
        out = pattern.sub(replacement, out)
    return out


def read_only() -> bool:
    """Whether writes are disabled by ``GITLAB_MCP_READ_ONLY``.

    Fails closed: only unset, empty, ``0``, ``false``, ``no`` or ``off``
    (case-insensitive, surrounding whitespace ignored) mean writes are
    allowed. A typo or an unexpected value such as ``enabled`` or ``2``
    means read-only.
    """
    return (os.environ.get(READ_ONLY_ENV) or "").strip().lower() \
        not in _FALSE_VALUES


# ─── Errors ───────────────────────────────────────────────────────────────────

class GitLabError(Exception):
    """A GitLab request failed. ``str()`` is always redacted.

    Attributes:
        status: HTTP status, or ``None`` for a transport/local failure.
        message: Redacted explanation.
        url: Redacted request path (never the full URL with a query token).
    """

    def __init__(self, message: str, status: int | None = None,
                 url: str = "", secrets: Iterable[str] = ()) -> None:
        self.status = status
        self.message = redact(message or "", secrets)
        self.url = redact(url or "", secrets)
        super().__init__(self.message)

    def __str__(self) -> str:
        where = f" ({self.url})" if self.url else ""
        status = f"HTTP {self.status}: " if self.status else ""
        return f"{status}{self.message}{where}"

    def __repr__(self) -> str:
        return (f"{type(self).__name__}(status={self.status!r}, "
                f"message={self.message!r})")


class GitLabTransportError(GitLabError):
    """Timeout, DNS failure, refused connection — no HTTP status exists."""


class GitLabAuthError(GitLabError):
    """401 — the token is missing, invalid or expired."""


class GitLabPermissionError(GitLabError):
    """403 — authenticated, but not allowed to do this."""


class GitLabNotFoundError(GitLabError):
    """404 — no such object, or no access to it (GitLab conflates the two)."""


class GitLabRateLimitError(GitLabError):
    """429 — too many requests."""


class GitLabHTTPError(GitLabError):
    """Any other non-2xx status."""


class GitLabReadOnlyError(GitLabError):
    """A write was attempted while ``GITLAB_MCP_READ_ONLY`` is set."""


_STATUS_ERRORS: dict[int, type[GitLabError]] = {
    401: GitLabAuthError,
    403: GitLabPermissionError,
    404: GitLabNotFoundError,
    429: GitLabRateLimitError,
}


# ─── Instance discovery ───────────────────────────────────────────────────────

def list_instances_public() -> list[dict]:
    """Every configured instance as ``{"name", "url", "active"}``.

    No token, no token environment variable, no headers: this result is safe to
    return straight to an MCP client.
    """
    active = (gl_instances._load().get("active") or "")
    out: list[dict] = []
    for inst in gl_instances.list_instances():
        name = inst.get("name") or ""
        out.append({
            "name": name,
            "url": (inst.get("url") or "").rstrip("/"),
            "active": name == active,
        })
    return out


#: Contract name for :func:`list_instances_public`.
list_instance_names = list_instances_public


def _find_instance(name: str) -> dict:
    """The raw instance record named ``name``.

    Raises:
        ValueError: when no instance has that name; the message lists the
            names that do exist, because that is the only information the
            caller can act on.
    """
    records = gl_instances.list_instances()
    wanted = name.strip()
    for inst in records:
        if (inst.get("name") or "") == wanted:
            return inst
    for inst in records:                      # a forgiving second pass
        if (inst.get("name") or "").lower() == wanted.lower():
            return inst
    known = ", ".join(repr(i.get("name") or "") for i in records) or "(none)"
    raise ValueError(f"unknown GitLab instance {name!r}; configured: {known}")


# ─── The context ──────────────────────────────────────────────────────────────

class GitLabContext:
    """One configured GitLab instance, with a token nobody else can read.

    Args:
        instance: Instance name, or ``None`` for the active instance.

    Raises:
        ValueError: unknown instance name, or no active instance configured.
    """

    def __init__(self, instance: str | None = None) -> None:
        if instance is None or not str(instance).strip():
            cfg = gl_instances.get_active_instance()
            if not cfg:
                known = ", ".join(i["name"] for i in list_instances_public())
                raise ValueError(
                    "no active GitLab instance; configured: "
                    f"{known or '(none)'} — select one with gitlab-cli, or "
                    "pass instance=<name>")
        else:
            cfg = gl_instances.build_cfg(_find_instance(str(instance)))

        self.__token: str = str(cfg.get("token") or "")
        # The cfg minus the credential. Kept separately so cfg_public() cannot
        # leak a token by forgetting a key, and so the full cfg is assembled
        # explicitly, only where it is genuinely needed.
        self.__cfg_public: dict = {k: v for k, v in cfg.items()
                                   if k not in ("token", "headers")}

        self.name: str = str(cfg.get("name") or "")
        self.url: str = str(cfg.get("url") or "").rstrip("/")
        # Absolute: paths.anchor resolves a relative master_dir against the
        # gitlab-cli install dir, never the cwd, so a sync or clone always acts
        # on the tree the CLI manages rather than wherever the server started.
        self.master_dir: str = gl_paths.anchor(cfg.get("master_dir") or ".")
        self.__cfg_public["master_dir"] = self.master_dir

        self.__session = requests.Session()
        self.__session.headers.update({
            "PRIVATE-TOKEN": self.__token,
            "Accept": "application/json",
        })
        self.__project_ids: dict[str, int] = {}
        self.__user_ids: dict[str, int] = {}

    # -- representation -----------------------------------------------------

    def __repr__(self) -> str:
        """Name and URL only — never the token or the session headers."""
        return f"<GitLabContext name={self.name!r} url={self.url!r}>"

    __str__ = __repr__

    # -- config -------------------------------------------------------------

    def cfg_public(self) -> dict:
        """The instance config without ``token`` or ``headers``.

        Safe to return to an MCP client or write to a log.
        """
        return dict(self.__cfg_public)

    def cfg_for_git(self) -> dict:
        """The FULL instance config, including ``token`` and ``headers``.

        ``clone.cmd_clone`` and the other gitlab-cli commands read
        ``cfg["headers"]`` and ``cfg["token"]`` directly, so they need the
        whole dict.

        This is for in-process gitlab-cli calls only. The result contains a
        live credential and must never be returned from an MCP tool, logged,
        put in an error message, or serialised anywhere.
        """
        cfg = dict(self.__cfg_public)
        cfg["token"] = self.__token
        cfg["headers"] = {"PRIVATE-TOKEN": self.__token}
        return cfg

    def git_env(self) -> dict:
        """Environment for a git subprocess: never prompt, never page.

        Carries no credential. Authentication for git is handled by
        gitlab-cli's own per-process config injection.

        Also ignores the system gitconfig and takes no optional locks, and
        drops inherited variables that would point git at another repository
        or name a program for it to run. The ``-c`` overrides that stop a
        repository's own ``.git/config`` from running a program are added by
        the read-tool git wrapper (``reporting._git``), since they are argv,
        not environment.
        """
        env = dict(os.environ)
        for key in _UNSAFE_GIT_ENV:
            env.pop(key, None)
        env.update({
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
            "GCM_INTERACTIVE": "never",
            "GIT_PAGER": "cat",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        })
        return env

    def redact(self, text: str) -> str:
        """:func:`redact`, additionally removing this instance's own token."""
        return redact(text, (self.__token,))

    # -- HTTP ---------------------------------------------------------------

    def _api_url(self, path: str) -> str:
        """``/projects/1`` → ``https://host/api/v4/projects/1``."""
        clean = str(path or "").strip()
        if clean.startswith(("http://", "https://")):
            raise ValueError(
                "path must be relative (e.g. '/projects/1'); an absolute URL "
                "could send the token to another host")
        if not clean.startswith("/"):
            clean = "/" + clean
        if clean.startswith(_API_PREFIX):
            clean = clean[len(_API_PREFIX):] or "/"
        return f"{self.url}{_API_PREFIX}{clean}"

    def _ensure_writable(self) -> None:
        if read_only():
            raise GitLabReadOnlyError(
                f"read-only: writes are disabled by {READ_ONLY_ENV}")

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json: Any = None) -> requests.Response:
        """One HTTP call. Transport failures become ``GitLabTransportError``."""
        url = self._api_url(path)
        try:
            return self.__session.request(
                method, url, params=params, json=json,
                timeout=TIMEOUT, verify=VERIFY_TLS)
        except requests.RequestException as exc:
            raise GitLabTransportError(
                f"{method} failed: {type(exc).__name__}: {exc}",
                status=None, url=path, secrets=(self.__token,)) from None

    def _parse(self, res: requests.Response, path: str) -> Any:
        """Parsed JSON body, or ``None`` for an empty/204 response.

        Raises:
            GitLabError: on any non-2xx status, with a redacted message.
        """
        if not 200 <= res.status_code < 300:
            raise self._error_for(res, path)
        if res.status_code == 204 or not (res.content or b"").strip():
            return None
        try:
            return res.json()
        except ValueError:
            raise GitLabHTTPError(
                "response body was not JSON", status=res.status_code,
                url=path, secrets=(self.__token,)) from None

    def _error_for(self, res: requests.Response,
                   path: str) -> GitLabError:
        """The right ``GitLabError`` subclass for a non-2xx response."""
        detail = ""
        try:
            body = res.json()
        except ValueError:
            detail = (res.text or "").strip()
        else:
            if isinstance(body, dict):
                for key in ("message", "error", "error_description"):
                    if body.get(key):
                        detail = str(body[key])
                        break
                else:
                    detail = str(body)
            elif body:
                detail = str(body)
        detail = detail[:_MAX_ERROR_CHARS] or f"HTTP {res.status_code}"
        cls = _STATUS_ERRORS.get(res.status_code, GitLabHTTPError)
        return cls(detail, status=res.status_code, url=path,
                   secrets=(self.__token,))

    def get(self, path: str, params: dict | None = None, **kw: Any) -> Any:
        """GET ``path`` under ``/api/v4`` and return the parsed JSON."""
        return self._parse(self._request("GET", path, params=params, **kw),
                           path)

    def post(self, path: str, json: Any = None, **kw: Any) -> Any:
        """POST ``json`` to ``path``. Refused when read-only."""
        self._ensure_writable()
        return self._parse(self._request("POST", path, json=json, **kw), path)

    def put(self, path: str, json: Any = None, **kw: Any) -> Any:
        """PUT ``json`` to ``path``. Refused when read-only."""
        self._ensure_writable()
        return self._parse(self._request("PUT", path, json=json, **kw), path)

    def delete(self, path: str, params: dict | None = None, **kw: Any) -> Any:
        """DELETE ``path``. Refused when read-only."""
        self._ensure_writable()
        return self._parse(
            self._request("DELETE", path, params=params, **kw), path)

    # -- pagination ---------------------------------------------------------

    def paginate(self, path: str, params: dict | None = None,
                 max_items: int | None = None,
                 limit: int | None = None) -> tuple[list, bool, int | None]:
        """Walk every page of a list endpoint.

        Follows GitLab's ``X-Next-Page`` header, which is the only reliable
        end-of-data signal: a short page is not proof the walk is over.

        Args:
            path: List endpoint, e.g. ``"/projects"``.
            params: Query parameters; ``per_page`` defaults to 100.
            max_items: Stop after this many items. ``None`` means no ceiling.
            limit: Contract alias for ``max_items``.

        Returns:
            ``(items, truncated, total)`` — ``truncated`` is True when the walk
            stopped at ``max_items`` with more data left, and ``total`` is the
            server's ``X-Total`` count when it sends one, else ``None``.

        Raises:
            GitLabError: on any failing page.
        """
        ceiling = max_items if max_items is not None else limit
        query: dict = dict(params or {})
        query.setdefault("per_page", 100)
        page = int(query.pop("page", 1) or 1)

        items: list = []
        total: int | None = None
        truncated = False

        while True:
            query["page"] = page
            res = self._request("GET", path, params=query)
            data = self._parse(res, path)
            if total is None:
                total = _int_header(res, "X-Total")
            if data is None:
                break
            if not isinstance(data, list):
                raise GitLabHTTPError(
                    f"expected a JSON list from {path}, got "
                    f"{type(data).__name__}", status=res.status_code,
                    url=path, secrets=(self.__token,))
            items.extend(data)

            if ceiling is not None and len(items) >= ceiling:
                truncated = (len(items) > ceiling
                             or _next_page(res) is not None)
                items = items[:ceiling]
                break

            nxt = _next_page(res)
            if nxt is None:
                break
            page = nxt

        return items, truncated, total

    # -- id lookups ---------------------------------------------------------

    def project_id(self, ref: str | int) -> int:
        """Numeric project id for an id or a ``group/sub/project`` path.

        The path is URL-encoded (``group%2Fproject``), which is how GitLab
        addresses a project by path. Results are cached per context.
        """
        key = str(ref).strip()
        if not key:
            raise ValueError("project reference must not be empty")
        if isinstance(ref, int) or key.isdigit():
            return int(key)
        if key in self.__project_ids:
            return self.__project_ids[key]
        data = self.get(f"/projects/{quote(key.strip('/'), safe='')}")
        pid = _id_of(data, f"project {key!r}")
        self.__project_ids[key] = pid
        return pid

    def user_id(self, ref: str | int) -> int:
        """Numeric user id for an id or a username. Cached per context."""
        key = str(ref).strip().lstrip("@")
        if not key:
            raise ValueError("user reference must not be empty")
        if isinstance(ref, int) or key.isdigit():
            return int(key)
        if key in self.__user_ids:
            return self.__user_ids[key]
        found = self.get("/users", {"username": key})
        if not isinstance(found, list) or not found:
            raise GitLabNotFoundError(f"no user named {key!r}", status=404,
                                      url="/users")
        uid = _id_of(found[0], f"user {key!r}")
        self.__user_ids[key] = uid
        return uid

    # -- calling into gitlab-cli -------------------------------------------

    def run_captured(self, fn: Callable[..., Any], *args: Any,
                     **kwargs: Any) -> tuple[Any, str]:
        """Call ``fn`` with stdout and stderr captured and redacted.

        The gitlab-cli commands print their progress. The MCP server speaks its
        protocol on stdout, so a single stray line corrupts the session — hence
        every call into that code goes through here.

        Returns:
            ``(result, captured_text)`` with the text already redacted.
        """
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            result = fn(*args, **kwargs)
        return result, self.redact(buf.getvalue())


# ─── Small helpers ────────────────────────────────────────────────────────────

def _int_header(res: requests.Response, name: str) -> int | None:
    """An integer response header, or ``None`` when absent/not a number."""
    try:
        raw = (res.headers.get(name) or "").strip()
    except Exception:                                        # noqa: BLE001
        return None
    return int(raw) if raw.isdigit() else None


def _next_page(res: requests.Response) -> int | None:
    """The next page number from ``X-Next-Page``, or ``None`` at the end."""
    return _int_header(res, "X-Next-Page")


def _id_of(data: Any, what: str) -> int:
    """``data["id"]`` as an int, or a clear error."""
    if isinstance(data, dict):
        value = data.get("id")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    raise GitLabError(f"{what} lookup returned no usable id")
