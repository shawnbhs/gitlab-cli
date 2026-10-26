# tests/_error_log_guard.py — hard isolation of the user's live error log.
#
# WHY THIS EXISTS
# ---------------
# `errors.py` resolves its log location ONCE, at import time:
#
#     ERROR_DIR  = os.path.expanduser("~/.gitlab-cli")
#     ERROR_FILE = os.path.join(ERROR_DIR, "errors.log")
#
# and `errors.log_error()` opens `ERROR_FILE` with O_CREAT|O_APPEND every time
# it is called. Any test that exercises a production code path which *logs* —
# `git.clone_repo()` refusing an unsafe URL, `git.fetch/pull` reporting a
# failure, the API error handler — therefore appends a real record to the
# developer's own ~/.gitlab-cli/errors.log. That is what was happening: the
# suite left junk entries such as
#
#     {"context": "clone:/tmp/gitlabcli_test_.../MasterGroups/x",
#      "message": "unsafe url: only http/https are accepted, not: ext::sh -c id"}
#
# in the user's live log on every run.
#
# Per-test redirection in setUp (what test_w4_error_log_parity.py and
# test_w6_4_error_log_concurrency.py do) is correct but only protects the
# module that remembers to do it. This module makes the isolation a property of
# the test *process* instead of a property of each author's diligence:
#
#   * import it and `errors.ERROR_DIR` / `errors.ERROR_FILE` are already
#     pointing into a per-process temp directory, before a single test runs;
#   * modules that redirect in setUp keep working unchanged — they save and
#     restore whatever is current, which is now the temp path, not the real one;
#   * an atexit check re-reads the real log and shouts if it changed, so a
#     future regression is reported rather than discovered in the user's file.
#
# It is imported by tests/_bootstrap.py (covering every module that uses the
# shared harness) and by tests/run_tests.py (covering the suite as a whole),
# and directly by the few modules that do not use the harness. Importing it
# twice is free — the work happens once, guarded by _INSTALLED.

import atexit
import hashlib
import os
import sys
import tempfile

# The CLI lives one level above tests/; make sure `import errors` resolves.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import errors as _errors_mod  # noqa: E402

# The file we are protecting. Resolved the same way errors.py resolves it, but
# captured here BEFORE we redirect the module, so it always names the real one.
REAL_ERROR_DIR = os.path.expanduser("~/.gitlab-cli")
REAL_ERROR_FILE = os.path.join(REAL_ERROR_DIR, "errors.log")

# Where the suite's writes actually go.
SANDBOX_DIR = None
SANDBOX_FILE = None

_INSTALLED = False


def _digest(path):
    """(size, mode, sha256) of `path`, or None when it does not exist."""
    if not os.path.exists(path):
        return None
    st = os.stat(path)
    with open(path, "rb") as fh:
        return (st.st_size, st.st_mode & 0o777,
                hashlib.sha256(fh.read()).hexdigest())


def install():
    """Redirect errors.py at the sandbox log. Idempotent."""
    global SANDBOX_DIR, SANDBOX_FILE, _INSTALLED
    if _INSTALLED:
        return SANDBOX_FILE

    SANDBOX_DIR = tempfile.mkdtemp(prefix="gitlabcli_errorlog_")
    SANDBOX_FILE = os.path.join(SANDBOX_DIR, "errors.log")

    _errors_mod.ERROR_DIR = SANDBOX_DIR
    _errors_mod.ERROR_FILE = SANDBOX_FILE

    before = _digest(REAL_ERROR_FILE)
    atexit.register(_verify_real_log_untouched, before)

    _INSTALLED = True
    return SANDBOX_FILE


def _verify_real_log_untouched(before):
    """Loud, non-fatal report if the live log changed during the run.

    Deliberately not an assertion: raising from atexit would mask the real test
    result. The suite's own guard test turns this into a hard failure.
    """
    after = _digest(REAL_ERROR_FILE)
    if before == after:
        return
    sys.stderr.write(
        "\n*** ERROR LOG GUARD: the live log at "
        f"{REAL_ERROR_FILE} CHANGED during this test run.\n"
        f"    before={before}\n    after ={after}\n"
        "    A test reached errors.log_error() against the real module-level\n"
        "    ERROR_FILE. Import tests/_error_log_guard.py from that module,\n"
        "    or redirect errors.ERROR_DIR/ERROR_FILE in its setUp.\n\n")


def real_log_digest():
    """Public helper so a test can assert the live log is untouched."""
    return _digest(REAL_ERROR_FILE)


install()
