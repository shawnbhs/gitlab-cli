# gitlab-cli regression suite

Run the whole thing:

```bash
cd /path/to/gitlab-cli && ./venv/bin/python tests/run_tests.py
```

The last line is the result, and it is the only thing worth reading:

```
RUN=1952 FAIL=0 ERROR=0 SKIP=1
```

(Counts move as tests are added; the shape of the line is the contract.
`FAIL` and `ERROR` must be 0. `SKIP` is the live-instance tests, which
skip when no GitLab is reachable.)

Add the opt-in live tests (needs a reachable GitLab and a configured instance;
skips cleanly otherwise):

```bash
GITLAB_CLI_LIVE_TESTS=1 ./venv/bin/python tests/run_tests.py
```

## What each file protects against

Every entry below names a defect that was real, reached a user, and would
return without the test. The suite is organised around failures, not around
modules.

### The original clone failure

| file | pins |
|---|---|
| `test_coverage_status.py` | 1-of-22 → `◐`, 0-of-N → `❌`, N-of-N → `✅`, 0-of-0 (empty remote group) → `✅`. A bare directory or a repo with no origin never counts as a clone. This is the check that replaced "the folder exists, so the group is done". |
| `test_clone_all_selection.py` | The consequence: `ca` must INCLUDE `◐` groups, and `pick_action` must still offer Clone for one. Excluding `◐` is what froze dozens of projects. |
| `test_resume_validity.py` | A directory with an invalid `.git` (or a `.git` file) must not be treated as cloned; a healthy repo must still be skipped. |
| `test_path_anchoring.py`, `test_cli_dest_resolution.py` | A relative destination resolves against the install dir, never the cwd — asserted from a foreign working directory. Plus traversal safety. |
| `test_url_normalization.py` | `.git`, trailing slash, mixed case, `user:password@`, ssh/scp form and a non-standard port all collapse to one key — and two projects sharing a basename never do. |
| `test_repo_discovery.py`, `test_w6_1_coverage_walker.py` | The walk finds repositories nested inside another repository's work tree. A walker that prunes at the first `.git` was blind to dozens of nested repositories on the machine this was measured on. |
| `test_layout_folder_gate.py`, `test_w6_8_folder_idempotence.py`, `test_w4_action_folder_gate.py`, `test_action_seam.py` | The instance `folder` reaches every path decision, and applying it twice does not double it. A menu action and the same action from the command line must resolve to the same directory. |

### Reporting something that was never checked

| file | pins |
|---|---|
| `test_api_pagination.py` | A failed page is an error, never an empty result. An expired token must not read as "you have zero projects". |
| `test_w6_2_apierror_handler.py` | Every path out of the CLI ends at one boundary that prints a cause, not a traceback. |
| `test_w6_3_members_strict.py` | A truncated members fetch raises. On the permissions screen a missing member renders as "no access", and the operator then revokes the wrong thing. |
| `test_w4_archived_visibility.py` | `archived` is a tri-state the caller chooses and the default shows both. Hardcoding `archived=false` silently drops the history nobody else is keeping. |
| `test_w8_1_since_validation.py` | A date git cannot parse must not surface as "no commits". Includes `GitReallyIsSilentTests`, which pins the *premise* — git exits 0 and prints nothing for `--since=b` — so that if a future git starts erroring, these fail loudly instead of the fix rotting quietly. Also: an unusable `--author` regex is reported as one, an empty result carries a verified history count, and five shell-metacharacter payloads create no file. |
| `test_w8_6_coverage_consistency.py`, `test_w8_6_coverage_call_sites.py` | The two legitimate coverage denominators reconcile, the rendered sentence is never a bare fraction, and a *new* module cannot start printing its own `projects on disk` line without being added to an explicit census. |
| `test_w7_bulk_resilience.py` | One group's failure does not end Clone All or Sync All — and is not swallowed either. |
| `test_w6_9_large_repo_fallback.py` | The shallow fallback fires only on the server-side response-cap signature, and its result is reported as `shallow`, never as `ok`. |
| `test_w6_10_verify_script.py` | The independent referee: its rename-redirect probe, its three-way present/missing/excluded accounting, and that a partial fetch reports UNKNOWN rather than zero. |

### Credentials and permissions

| file | pins |
|---|---|
| `test_credential_hygiene.py` | A clone's `.git/config` holds no credential. Includes a negative control proving the assertion can fail. |
| `test_git_credential_mechanics.py` | The token reaches git through the environment and the credential helper, and the URL written into the repository is clean. Asserts the exact argv handed to git. |
| `test_redaction.py` | Nothing logged or printed can carry a token. |
| `test_w7_2_credential_pattern.py` | The detector does not fire on a project whose *name* merely looks like a token. A false positive here sends someone scrubbing a clean repository. |
| `test_w8_2_remotes_instance_scope.py` | The audit and the scrub cover the active instance only. Both instances are tested explicitly, including the asymmetric case where one has no subfolder and its subtree therefore *is* the shared root. The scrub is tested against a deliberately unfiltered list, because it is the write path and must filter itself. |
| `test_w8_4_config_permissions.py` | A clone's `.git/config` is created at 0600 rather than narrowed afterwards — with a control test showing git produces 0644 without the mask. Plus exhaustive, cycle-safe, `gitdir:`-pointer-aware repository discovery, and a sweep that returns counts only and never a file's contents. |
| `test_remotes_hardening.py` | Only `http`/`https` reach git, and a `--` separator stops a path being parsed as an option. |

### The error log

| file | pins |
|---|---|
| `test_error_log_keys.py`, `test_w4_error_log_parity.py` | Nested group paths flatten unambiguously, so two groups cannot overwrite each other's log, and the menu looks for the log where the writer put it. |
| `test_w6_4_error_log_concurrency.py` | Concurrent writers do not lose entries, and reading never rewrites the file. |
| `test_w4_error_resolve_order.py` | Resolving an entry marks the entry the user was looking at. |
| `test_w8_3_stale_target_cleanup.py` | Removal is never a side effect of counting. An entry is removable only when its target *and* its parent are gone; a real clone under `/tmp` that still exists is kept, which is the test that proves the rule is not a `/tmp` heuristic. A key is a nomination, not an authorisation: the predicate is re-evaluated inside the lock. |
| `test_w7_live_error_log_isolation.py` | The suite cannot write to the real `~/.gitlab-cli/errors.log`. |

### Menus and settings

| file | pins |
|---|---|
| `test_w8_5_user_namespace_optin.py` | Off by default, including that an admin-ish config does not flip it and that the suite's own environment is clean. Three scopes, alias and precedence resolution, garbage is not an opt-in, and two tests proving the discovery path cannot clone or write to the filesystem. |
| `test_w8_5_doctor_no_false_orphans.py` | A project excluded by policy but already on disk is not an orphan — and one that is genuinely gone still is, so the fix cannot be over-applied. |
| `test_w4_menu_input_validation.py`, `test_w8_7_deadcode_and_prompts.py` | Unrecognised input is rejected out loud and never falls through to a default that happens to exist. |
| `test_user_namespace_discovery.py` | The API-layer discovery itself: a user namespace is classified as one, and bot/service namespaces are dropped. |

### Wave 9

| file | pins |
|---|---|
| `test_w9_1_members_validation.py` | The member-activity screen refuses a date it cannot resolve instead of sending it to `?after=` and reporting the empty answer as no activity. It pins that members.py defines **no second date judge** — the validators are asserted to be the identical objects from `log.py` — because two date parsers is how the screens drifted apart in the first place. |
| `test_w9_2_seam_wiring.py` | The four seams `gitlab_cli.py` owns. Four earlier fixes were each correct, tested, and never called — the function was never the gap, the CALL was. Each test fails against the pre-W9-2 entry point and passes against the wired one, so a fix that exists but is unreachable cannot pass again. |
| `test_w9_5_rename_redirect.py` | A project renamed upstream is repaired in place, never duplicated: the redirect is read from the server's own answer, the remote is repointed and the directory moved. A clone holding work the server does not have is refused instead of moved, and an undetermined answer (401/403/429/5xx) is never read as a deletion. |
| `test_w9_9_api_honesty.py` | An API failure cannot be mistaken for an empty result on any path out of `api.py`. |
| `test_w9_3_instances_config.py` | Every persisted key survives a save/load round-trip and is a fixed point under a second round-trip; a record without an optional key does not spontaneously gain one; an unknown key is reported by name rather than dropped in silence; and the token is still never written to the instance file. |
| `test_w9_4_coverage_home.py` | The coverage vocabulary moved to its own module and the old address still resolves to the *same objects* — not to a second copy. Each symbol is defined exactly once repo-wide, the one-line sentence is identical through either import path, and an unknown namespace kind is never silently treated as a group. |
| `test_w9_4_orphans.py` | Orphan detection is scoped to the host, claims no classification it did not make, and makes no network call. A rename redirect is read as *renamed, with the new path* rather than as a deletion, so a renamed project is not counted as both missing and orphaned. Prefix scoping is opt-in and matches on a path segment, never a substring. |
| `test_w9_7_exit_semantics.py` | The referee's exit code means what its report says. Five distinct codes exist, 0 is not repurposed, every non-OK code is non-zero, and severity order is deliberately not numeric order — so a missing project, a broken repository and a shallow clone are each attributable from the exit status alone. |

## Rules the suite obeys

- Fixtures are **real** repositories made by `git init` in `tempfile.mkdtemp()`. git is never mocked.
  The bugs were about what git actually does, and a mock cannot reproduce those.
- Nothing is written outside that temp dir; teardown refuses to remove anything that is not its own mkdtemp.
- The real `MasterGroups` tree is never read for writes, never deleted, never pushed to.
- The live error log is redirected at import time by `_error_log_guard.py`, so isolation is a property
  of the test *process* rather than of each author's diligence. A test that touches the live log fails.
- No test contains, prints, or requires a real token. Live tests read credentials the same way the
  tool does and `skipTest` when absent.
- `TestFolderPoolUnaffected` in `test_w6_8_folder_idempotence.py` reads the live
  `~/.gitlab-cli/instances.json` on purpose. It no longer spells any instance name as a
  literal: a placeholder name is absent from every real config, so the assertion would pass
  for the wrong reason and silently stop guarding the regression. The names are read out of
  the config itself, and the case `skipTest`s when there is no config.
