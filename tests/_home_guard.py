"""Isolation from the developer's real home directory, for the gitlab-mcp tests.

Not a test module (the leading underscore keeps ``unittest discover`` away from
it). A test module opts in with::

    from tests import _home_guard

    def setUpModule():
        _home_guard.activate()

    def tearDownModule():
        _home_guard.deactivate()

While active:

* ``HOME`` points at a fresh temporary directory, ``GITLAB_TOKEN*`` variables
  are removed from the environment, and git is told to ignore system config.
* Every gitlab-cli path constant that was computed from ``~`` at import time
  (``credentials.CONFIG_DIR``/``ENV_FILE``, ``instances.INSTANCES_FILE``,
  ``paths.INSTANCES_FILE``, ``caps.CACHE_FILE``, ``errors.ERROR_FILE``, ...) is
  repointed into that directory. Patching ``HOME`` alone is not enough: those
  constants were frozen when the module was first imported.
* A fake ``instances.json`` with one instance, ``example``, is written there.
* A process audit hook refuses (``PermissionError``) and records any attempt
  to open a dot-path under the *real* home (``~/.gitlab-cli``, ``~/.gitconfig``,
  ``~/.git-credentials``, ``~/.netrc``, ``~/.config/...``), to start a
  subprocess whose ``HOME`` is the real home, or to open a non-loopback socket.
* The credential stores in :data:`SECRET_PATHS` (``~/.git-credentials``,
  ``~/.ssh``, ...) are refused explicitly and also cannot be *listed*
  (``os.listdir``/``os.scandir``), so neither a key nor a key's file name can
  leak into a test log.

gitlab-cli swallows ``OSError`` in several readers (``instances._load``,
``credentials.read_env_file``), so a refused open does not always surface as a
failing test. :func:`deactivate` therefore raises :class:`AssertionError` when
anything was recorded, which unittest reports as a ``tearDownModule`` error.

The real home is taken from the password database, not from ``HOME``, so the
guard cannot be fooled by a test that has already replaced ``HOME``. Paths
under the real home that are not dot-paths (the gitlab-cli checkout and its
virtualenv) stay readable, since the interpreter imports code from there.
"""

from __future__ import annotations

import importlib
import json
import os
import pwd
import shutil
import sys
import tempfile
from unittest import mock

REAL_HOME = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)

#: (module, attribute, path relative to the fake home) for every gitlab-cli
#: constant that holds a location derived from ``~`` at import time.
_PATH_CONSTANTS = (
    ("credentials", "CONFIG_DIR", ".gitlab-cli"),
    ("credentials", "ENV_FILE", ".gitlab-cli/env"),
    ("instances", "CONFIG_DIR", ".gitlab-cli"),
    ("instances", "INSTANCES_FILE", ".gitlab-cli/instances.json"),
    ("paths", "INSTANCES_FILE", ".gitlab-cli/instances.json"),
    ("caps", "CONFIG_DIR", ".gitlab-cli"),
    ("caps", "CACHE_FILE", ".gitlab-cli/caps.json"),
    ("errors", "ERROR_DIR", ".gitlab-cli"),
    ("errors", "ERROR_FILE", ".gitlab-cli/errors.log"),
    ("setup", "CONFIG_DIR", ".gitlab-cli"),
)

#: Modules imported eagerly so their constants are patched before any test
#: (or lazy import inside the code under test) can read them. ``setup`` is
#: only patched when something else has already imported it.
_EAGER_MODULES = ("credentials", "instances", "paths", "caps", "errors")

#: The one instance the fake config contains. No token anywhere.
FAKE_INSTANCE = {
    "name": "example",
    "url": "https://gitlab.example.com",
    "master_dir": "MasterGroups",
    "folder": "example",
    "token_env": "GITLAB_TOKEN_EXAMPLE",
}

_LOOPBACK = ("127.0.0.1", "::1", "localhost")

#: Credential stores under the real home that must never be read or listed,
#: whatever the generic dot-path rule says (relative to the real home).
SECRET_PATHS = (".git-credentials", ".ssh", ".netrc", ".gitlab-cli")


class _State:
    """Mutable guard state shared by the audit hook and activate/deactivate."""

    depth = 0
    hook_installed = False
    fake_home: str | None = None
    patchers: list = []
    violations: list[str] = []


def _first_component_under_real_home(path) -> str | None:
    """First path component below the real home, or ``None`` if outside it.

    The path is resolved first, so a symlink elsewhere that points into the
    real home is judged by its target.
    """
    if isinstance(path, int):
        return None                      # already-open file descriptor
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not isinstance(path, str) or not path:
        return None
    full = os.path.realpath(os.path.abspath(path))
    if not full.startswith(REAL_HOME + os.sep):
        return None
    return os.path.relpath(full, REAL_HOME).split(os.sep, 1)[0]


def _under_real_home_dotpath(path) -> bool:
    """True for ``<real home>/.<anything>`` (after resolving the path)."""
    first = _first_component_under_real_home(path)
    return first is not None and first.startswith(".")


def is_secret_path(path) -> bool:
    """True for a :data:`SECRET_PATHS` entry of the real home, or inside one."""
    return _first_component_under_real_home(path) in SECRET_PATHS


def _socket_target_is_remote(address) -> bool:
    if not isinstance(address, tuple) or not address:
        return False                     # AF_UNIX path / socketpair
    return str(address[0]) not in _LOOPBACK


def _refuse(message: str) -> None:
    _State.violations.append(message)
    raise PermissionError(f"[home guard] {message}")


def _audit(event: str, args: tuple) -> None:
    """Process-wide audit hook; inert unless a guarded module is running."""
    if _State.depth <= 0:
        return
    if event == "open":
        if is_secret_path(args[0]):
            _refuse(f"open of a credential store: {args[0]!r}")
        if _under_real_home_dotpath(args[0]):
            _refuse(f"open of a path under the real home: {args[0]!r}")
    elif event in ("os.listdir", "os.scandir"):
        if args and is_secret_path(args[0]):
            _refuse(f"listing of a credential store: {args[0]!r}")
    elif event == "socket.connect":
        if _socket_target_is_remote(args[1]):
            _refuse(f"network connection to {args[1]!r}")
    elif event == "socket.getaddrinfo":
        if str(args[0]) not in _LOOPBACK:
            _refuse(f"DNS lookup of {args[0]!r}")
    elif event == "subprocess.Popen":
        env = args[3] if len(args) > 3 else None
        home = (env if env is not None else os.environ).get("HOME", "")
        if home and os.path.realpath(home) == REAL_HOME:
            _refuse(f"subprocess {args[1]!r} started with the real HOME")


def _write_fake_config(home: str) -> None:
    config_dir = os.path.join(home, ".gitlab-cli")
    os.makedirs(config_dir, mode=0o700)
    with open(os.path.join(config_dir, "instances.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"instances": [dict(FAKE_INSTANCE)], "active": "example"},
                  fh, indent=2)


def _start(patcher) -> None:
    patcher.start()
    _State.patchers.append(patcher)


def _environment(home: str) -> dict:
    return {
        "HOME": home,
        "XDG_CONFIG_HOME": os.path.join(home, ".config"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def activate() -> str:
    """Isolate the running test module from the real home; return fake HOME."""
    _State.depth += 1
    if _State.depth > 1:
        return _State.fake_home or ""
    if not _State.hook_installed:
        sys.addaudithook(_audit)
        _State.hook_installed = True

    home = tempfile.mkdtemp(prefix="gitlab-mcp-home-")
    _State.fake_home = home
    _State.violations = []
    _State.patchers = []
    _write_fake_config(home)

    tokens = [k for k in os.environ if k.startswith("GITLAB_TOKEN")]
    _start(mock.patch.dict(os.environ, _environment(home)))
    for key in tokens:
        os.environ.pop(key, None)        # restored by patch.dict on stop

    for name in _EAGER_MODULES:
        try:
            importlib.import_module(name)
        except ImportError:              # gitlab-cli not on PYTHONPATH
            continue
    for module_name, attr, rel in _PATH_CONSTANTS:
        module = sys.modules.get(module_name)
        if module is None or not hasattr(module, attr):
            continue
        _start(mock.patch.object(module, attr, os.path.join(home, rel)))

    paths_mod = sys.modules.get("paths")
    cache = getattr(paths_mod, "_folder_cache", None)
    if isinstance(cache, dict):
        _start(mock.patch.dict(cache, {"key": None, "folders": frozenset()}))
    return home


def deactivate() -> None:
    """Undo :func:`activate`; raise if anything reached for the real home."""
    _State.depth -= 1
    if _State.depth > 0:
        return
    for patcher in reversed(_State.patchers):
        patcher.stop()
    _State.patchers = []
    if _State.fake_home:
        shutil.rmtree(_State.fake_home, ignore_errors=True)
    _State.fake_home = None
    violations, _State.violations = _State.violations, []
    if violations:
        raise AssertionError(
            "tests reached outside their sandbox:\n  " + "\n  ".join(violations))


def refresh() -> None:
    """Re-apply the patches, e.g. after a test ``importlib.reload``-ed a module.

    A reload re-executes ``CONFIG_DIR = os.path.expanduser(...)`` and so puts
    the real path back; this points it at the fake home again.
    """
    if _State.depth <= 0:
        return
    depth, _State.depth = _State.depth, 1
    pending, _State.violations = list(_State.violations), []
    deactivate()
    activate()
    _State.depth = depth
    _State.violations = pending


def fake_home() -> str | None:
    """The temporary HOME of the active guard, or ``None``."""
    return _State.fake_home


def violations() -> list[str]:
    """A copy of what the guard has refused so far in this module."""
    return list(_State.violations)


def clear_violations() -> None:
    """Forget recorded violations (only for the guard's own self-tests)."""
    _State.violations = []


__all__ = ["REAL_HOME", "FAKE_INSTANCE", "SECRET_PATHS", "activate",
           "deactivate", "refresh", "fake_home", "violations",
           "clear_violations", "is_secret_path"]
