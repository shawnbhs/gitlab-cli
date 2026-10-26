#!/usr/bin/env python3
# tests/run_tests.py — the single entry point for the whole regression suite.
#
#   cd /home/user/gitlab-cli && ./venv/bin/python tests/run_tests.py
#
# stdlib unittest only, so it runs in any venv. Offline by default; the live
# tests are opt-in via GITLAB_CLI_LIVE_TESTS=1 and skip cleanly without it.
#
# Options (all optional — a bare invocation runs everything, which is the
# contract; the options exist for bisecting a red run, not for trimming CI):
#
#   -q / --quiet          one character per test instead of one line per test
#   -k SUBSTRING          run only tests whose id contains SUBSTRING (repeatable,
#                         AND-combined)
#   -f / --failfast       stop at the first failure
#   --list                print every selected test id and exit without running
#   <module> ...          run only these modules, e.g. test_redaction
#
# Exit status: 0 iff FAIL=0 and ERROR=0; 2 if the selection is empty or a module
# name is wrong, so a typo can never be mistaken for a green suite.
#
# Skips never fail the run, but every skip is echoed in the recap with its
# reason. A skip in this suite is one of exactly two things: a live test with no
# reachable GitLab, or a deliberate ratchet naming a fix that is not adopted
# yet. Printing them is what stops either kind from going quiet forever.

import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (HERE, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

# Redirect errors.ERROR_DIR/ERROR_FILE into a temp dir before ANY test module
# is imported, so no test can append to the user's live ~/.gitlab-cli/errors.log.
import _error_log_guard  # noqa: E402,F401


def _flatten(suite):
    """Every leaf TestCase, so -k and --list can work on real test ids."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _load(modules):
    loader = unittest.TestLoader()
    if modules:
        return loader.loadTestsFromNames(modules)
    return loader.discover(start_dir=HERE, pattern="test_*.py", top_level_dir=HERE)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    quiet = False
    failfast = False
    only_list = False
    patterns = []
    modules = []

    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-q", "--quiet"):
            quiet = True
        elif a in ("-f", "--failfast"):
            failfast = True
        elif a == "--list":
            only_list = True
        elif a in ("-k", "--filter"):
            i += 1
            if i >= len(argv):
                print("-k needs a value", file=sys.stderr)
                return 2
            patterns.append(argv[i])
        elif a.startswith("-k"):
            patterns.append(a[2:])
        elif a in ("-h", "--help"):
            print(__doc__ or "read the comment header of tests/run_tests.py")
            return 0
        elif a.startswith("-"):
            print(f"unknown option: {a}", file=sys.stderr)
            return 2
        else:
            modules.append(a[:-3] if a.endswith(".py") else os.path.basename(a))
        i += 1

    try:
        suite = _load(modules)
    except Exception as exc:                      # a bad module name, mostly
        print(f"could not load tests: {exc}", file=sys.stderr)
        return 2

    tests = list(_flatten(suite))
    # A module that fails to import arrives as a _FailedTest, which must run (and
    # fail) rather than be filtered away, so no -k filter is applied to those.
    if patterns:
        tests = [t for t in tests
                 if t.__class__.__name__ == "_FailedTest"
                 or all(p.lower() in t.id().lower() for p in patterns)]
    if not tests:
        print("no tests matched — refusing to report success for an empty run",
              file=sys.stderr)
        return 2

    if only_list:
        for tid in sorted(t.id() for t in tests):
            print(tid)
        print(str(len(tests)) + " test(s) selected")
        return 0

    selected = unittest.TestSuite(tests)
    started = time.time()
    result = unittest.TextTestRunner(verbosity=1 if quiet else 2,
                                     failfast=failfast).run(selected)
    elapsed = time.time() - started

    # Recap: a red run names its casualties without anyone scrolling a thousand
    # lines, and a skipped test states why it was skipped.
    for label, bucket in (("FAILURE", result.failures), ("ERROR", result.errors)):
        for case, _tb in bucket:
            print(f"{label}: {case.id()}")
    for case, reason in result.skipped:
        first = (reason or "").strip().splitlines()[0] if reason else ""
        print(f"SKIP: {case.id()} — {first[:200]}")

    live = os.environ.get("GITLAB_CLI_LIVE_TESTS") == "1"
    print(f"\nRUN={result.testsRun} "
          f"FAIL={len(result.failures)} ERROR={len(result.errors)} "
          f"SKIP={len(result.skipped)} "
          f"TIME={elapsed:.1f}s LIVE={'1' if live else '0'}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
