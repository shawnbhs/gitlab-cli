# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.2.0] - 2026-10-26

The MCP server moved into this repository, and every error screen learned to
say what went wrong, why, and what to do about it.

### Added

- **`gitlab-cli mcp`** starts the MCP server over stdio, so an AI agent can use
  the same code the command line uses. Reads run at once. Every write (a
  comment, an approval, a merge, a branch, a local sync or clone) is a two-call
  flow: the first call returns a preview and a single-use confirmation token
  bound to the exact arguments, and changes nothing. `GITLAB_MCP_CONFIRM_MODE=human`
  keeps the token out of the agent's context. See [docs/MCP.md](docs/MCP.md).
- **`GITLAB_MCP_READ_ONLY`** turns writes off. It fails closed: any value other
  than unset, empty, `0`, `false`, `no` or `off` enables read-only mode. The
  server rejects every write tool before a preview is built, and the shared
  HTTP layer refuses every `POST`, `PUT` and `DELETE`.
- **`gitlab-cli report <command>`** gives the same reports and previewed writes
  by hand from the shell, with a `Proceed? [y/N]` prompt in place of the token.
  A merge into a protected branch always asks for the branch name, even with
  `--yes`. See [docs/CLI.md](docs/CLI.md).
- **`error_catalog.py`**: one catalog of failure codes, each with a title, a
  cause and a fix. Patterns are anchored to their HTTP context rather than
  matched as bare substrings, and every screen now prints a one-line
  "title - cause -> fix". It replaces the old substring table in `errors.py`.

### Changed

- **API errors carry a code and a title.** `ApiError` exposes `.code` and
  `.title`, and its message is one line with the query string stripped.
  Transport failures now say whether DNS, a timeout or a refused connection was
  the cause. Expected 404 probes no longer reach the error log.
- **The capability probe reports the specific failure** and never caches a
  partial result. Private files are written at mode 600 from the first byte.
- **The Error Log screen** tags each entry with its instance, code and cause,
  keeps the tail of long messages, folds repeats into `xN` rows, splits entries
  per instance, re-explains entries written by older versions, and validates
  every menu key.

### Fixed

- **A renamed project produced a second clone.** Clone and pull now follow an
  upstream rename by moving the existing clone to its new path (a same-device
  rename, with worktrees repaired). The move is refused when the clone holds
  unpushed commits, stash entries or uncommitted changes.
- **Projects pending deletion** are skipped and reported instead of failing.
- **LFS and empty or purged repositories** failed with an unexplained git error.
  Both now get a plain explanation.
- **Leftover directories** from projects that no longer exist are pruned only
  after a complete server listing, and only when they are provably empty.
- **A test that depended on the calendar** failed once its fixed date aged out.
  It now uses a relative date.

---

## [1.1.0] — 2026-09-23

Three waves of work after 1.0.0, all of the same kind: the tool still had
places where it answered a question it had not actually asked. A date it did
not understand read as "you have no commits". An instance-wide screen offered
to rewrite a second instance's repositories. Two screens printed two different
coverage fractions and neither explained itself.

### Security

- **The remote audit and scrub now belong to one instance, not one directory.**
  Several instances share a clone root, and an instance configured without its
  own subfolder has that shared root *as* its subtree — so walking from its
  destination reached the other instance's entire tree. Answering `y` at the
  scrub prompt, or running `gitlab-cli remotes --fix`, would have rewritten the
  remote URLs of hundreds of repositories belonging to a GitLab that screen was
  not showing. Ownership is now decided from the instance configuration:
  foreign repositories, and repositories under the shared root belonging to no
  instance, are excluded from both the audit and the scrub and reported as a
  count that names the owner. The scrub re-applies the filter itself rather
  than trusting the audit to have done it, because both are handed the same
  list and the scrub is the write path.
- **New clones are created with a private `.git/config`.** Git writes the
  remote URL into `.git/config` at the *start* of a clone that may then run for
  minutes, so a clone that was interrupted left a group-readable file behind
  permanently. The clone child now runs under a tighter file-creation mask, so
  the file is born at mode 600 instead of being narrowed afterwards. The
  reflog, `FETCH_HEAD` and `packed-refs` carry copies of the same URL and are
  tightened too; object storage and hooks are deliberately left alone.
- **Permission drift in repositories that already exist** can now be swept:
  `gitlab-cli harden-perms [--dry-run] [root ...]`. The creation-time fix above
  does nothing for clones made before it, or by git directly, and the sweep had
  been written and tested in the previous wave without ever being given a
  command — so the fix existed and no user could reach it. The walk is
  exhaustive rather than pruning, because a repository nested inside another
  repository's work tree is exactly the one a naive walk misses. `--dry-run` is
  genuinely read-only: it changes no mode and no mtime, which is proved by
  comparing full `stat` results, not asserted by the flag's name. Extra roots
  may be named, because a tree this tool did not create is the one a
  config-scoped cleanup silently skips. It is deliberately not a step of clone
  or pull, which would make every sync pay for a walk of tens of thousands of
  directories to re-check modes that are already correct.

### Fixed

- **A date the tool could not read was reported as "no commits".** `git log
  --since=` has no error channel: git silently interprets anything it cannot
  parse as *now*, exits 0 and prints nothing. `gitlab-cli log my-group b` — a
  typo — therefore announced that a group full of commits had none. `since` is
  now checked before anything is queried, by recognising known date forms and,
  for anything else, by asking git what moment it resolved to and rejecting a
  value that lands exactly on now. Every accepted value echoes the absolute
  moment git settled on, so a value git quietly reinterpreted is visible.
- **A malformed author filter was reported as "no commits" too, by a different
  mechanism.** `--author` is a regular expression; a stray `[` makes git exit
  128, and every non-zero exit was being mapped to an empty commit list. An
  unusable pattern now says it is an unusable pattern.
- **The member-activity screen had the same defect against a different
  backend.** Its `Since` value went to GitLab's `?after=` parameter, so an
  unresolvable filter came back empty and read as "this person has no
  activity". It now shares the log screen's validation, converts the resolved
  moment to the calendar day the events API needs, and echoes what you typed,
  what git read it as, and what was actually sent.
- **An empty result and an unanswerable question no longer look alike.** When a
  log filter matches nothing, the tool asks git again with the filter removed
  and reports which of the three situations it is in: your filter excluded
  everything (with the count of repositories that *do* have history, verified,
  not assumed), these repositories have no commits at all, or the input was
  never understood and nothing was searched.
- **`doctor` told you to clone projects the menu cannot reach.** Projects in
  personal namespaces have no group above them, so the group-rooted clone path
  can never fetch one. They were nevertheless folded into "N projects are NOT
  on disk → Clone", which is advice for an action that does not exist. They are
  now partitioned out of the coverage arithmetic and disclosed separately, with
  their count, whose namespaces they are, and the setting that would include
  them. Classification costs no extra API call — it reads the list already
  fetched. A project excluded by that policy but already on disk from an
  earlier clone is no longer reported as an orphan either.
- **Two screens printed two different coverage fractions.** The group list said
  `40/40`, `doctor` said `40/45`, and neither was wrong: one denominator is
  built by walking groups and structurally cannot contain a personal-namespace
  project, the other is the flat project list and does. Nothing on either
  screen said so, so the tool answered one question two ways. Both figures are
  now rendered by one function into one labelled sentence — the mirrored
  fraction first, then the instance total and what the difference consists of.
  If the two stop reconciling, the tool reports UNKNOWN instead of inventing a
  third number.
- **The main menu's error badge counted entries nobody could act on.** Most
  entries in a long-lived log were residue whose target directories no longer
  existed; the badge reported every one of them as outstanding work.
- **The instance picker's `q` said "exit" and meant "back"**, and the group
  action prompt was the only case-sensitive menu in the application, so `Q` and
  `B` did not do what `q` and `b` did.

### Added

- **A user-namespace setting with three values, defaulting to off.**
  `GITLAB_CLI_USER_NAMESPACES` (or a per-instance `user_namespaces` key) takes
  `off`, `own` or `all`. `off` is the default and issues no additional API call
  at all. The default is off because `all` means *every user namespace this
  token can see* — and if the configured token is an admin token, that is every
  employee's personal namespace. A value that is none of the three is not an
  opt-in: it degrades to off and says so, because a setting that looks like it
  worked but did not is the same failure as a silent exclusion.
- **A prompted cleanup for error-log entries that can no longer be acted on.**
  An entry is removable only when both its target directory and the directory
  above it are gone — so a failure you can still go and inspect is kept, and so
  is one whose group folder survives. Entries with no filesystem target, and
  lines the reader could not parse, are structurally ineligible. Counting the
  candidates is a pure read; removal needs the typed word `yes`, writes a dated
  backup of the whole log first, and re-checks every record inside the lock, so
  a directory that reappeared between the preview and the confirmation is kept.
- **Bulk loops survive one group's failure.** Clone All and Sync All caught only
  `KeyboardInterrupt`, so a single 403, a rename, or three seconds of VPN
  flicker ended the whole run at the first failing group — on a tool whose purpose is bulk
  work. A failing group is now printed where it fails, logged, and counted in a
  closing summary; the remaining groups are still attempted. The failure is not
  swallowed, which would trade a loud abort for a silent gap.
- **Strict membership listing.** A truncated members fetch now raises instead of
  returning a short list, because on the permissions screen a missing member
  renders as "no access" and the operator revokes the wrong thing.

### Changed

- **The regression suite grew from 504 checks to over 1,000**, across 48 files. New
  coverage: date and author validation, remote-audit instance scoping, the
  user-namespace opt-in and its default, the coverage accounting reconciling,
  stale-entry cleanup, `.git` permissions at creation, exhaustive repository
  discovery, and the large-repository fallback.
- **The test suite can no longer write to your real error log.** Isolation is
  now a property of the test process rather than of each author's diligence,
  and a guard fails the run if the live log is touched.
- **`config.py` was removed.** It read a single instance out of a `.env` file
  and predated the multi-instance model; an AST census of every module confirmed
  nothing imported it.

---

## [1.0.0] — 2026-09-23

First release. The tool existed before this point, but it reported success
it had not earned: "Clone All: done" coexisted with most of a group's
projects never fetched. Everything below is the work of making the output true.

### Fixed

- **A relative destination resolved against the shell's current directory.**
  `gitlab-cli clone <group> ./here` wrote repositories, and the per-group
  error log, wherever the user happened to be standing — somewhere the
  interactive menu, which only ever reads the configured folder, never
  looks. A destination now enters the program through one function, is
  anchored to the install directory, and a path landing outside the managed
  folder produces an explicit warning instead of silence.
- **HTTP errors during pagination were swallowed.** A 401 on page 3 of a
  project listing ended the loop and returned the pages that had already
  succeeded. An expired token therefore read as "you have zero projects".
  A failed page is now an error, never an empty result.
- **Group status was inferred from a folder existing.** A directory named
  after the group was enough to print `✅`, whether it held every repository
  or none. Status is now computed from how many of the group's projects are
  genuinely on disk, and each group shows `cloned/total`.
- **An interrupted clone was counted as done.** A half-written directory
  looked like a clone to the resume check and was skipped forever. Validity
  is now decided by git itself; a directory git cannot resolve is reported
  as broken with its path, and nothing is ever deleted automatically.
- **Per-group error logs collided.** Two groups whose names flattened to the
  same string overwrote each other's log. Nested group paths are now
  flattened unambiguously (`.acme__platform__core_errors.json`).
- **The access token was written into `.git/config`.** Clones carried
  `http://oauth2:<token>@host/...` in plain text, printed by any script
  running `git remote -v`. The token is now passed to a single git process
  through its environment and handed to git's credential helper; the URL
  written into the repository is credential-free and `.git/config` is
  tightened to mode 600. `gitlab-cli remotes --fix` cleans up clones made
  before this change.
- **The remote audit pruned nested repositories out of its own walk.**
  Submodules, vendored checkouts and worktrees inside another repository
  were skipped, so a credential one level down was never found. The walk now
  descends into them, while still never entering a `.git` directory and
  never following symlinks.
- **Archived projects were invisible.** Every listing hardcoded
  `archived=false`, which does not mean "prefer active projects" — it means
  the archived ones do not exist. Groups printed `✅` while archived
  repositories were never mirrored, which is precisely the history nobody
  else is keeping. `archived` is now a tri-state the caller chooses, and the
  default shows both.
- **The interactive action seam.** The menu's per-group actions did not
  receive the instance's `folder`, so an action taken from the menu and the
  same action taken from the command line resolved to different directories.
  The folder is now passed at the source.
- **Remaining silent-failure seams** in the coverage walker, the error log,
  the members listing, the git layer and the CLI boundary: a truncated
  members fetch now raises rather than returning a short list; concurrent
  writers to an error log no longer lose entries; and an uncaught exception
  reaches one boundary that prints a plain-language cause instead of a
  traceback.

### Added

- **Coverage scanning by origin URL.** What is on disk is determined by each
  clone's origin URL, not by its location, so a repository moved elsewhere
  under the managed folder still counts and an empty directory never does.
  Identity is the full `namespace/project` path, because the same basename can
  repeat across namespaces as genuinely different projects.
- **`doctor`** — a health check that reports, without changing anything:
  git and credential-helper state, config-directory and token-file
  permissions, token validity and access level, the absolute folder it
  resolved to, per-namespace coverage with `n/N`, clones whose `.git/config`
  still holds a token (paths and counts only, never a value), layout drift,
  and a `folder` setting that disagrees with the existing tree. `--quick`
  skips the network and the tree walk and reports those checks as UNKNOWN —
  never as OK.
- **`scripts/verify-mirror`** — an independent referee that answers "is my
  mirror complete, and is it clean?" for every configured instance. It
  imports nothing from the CLI and runs under the system `python3`, because
  a checker that reuses the subject's modules cannot detect the subject's
  bugs. It probes GitLab's rename redirect, so a renamed project is not
  double-counted as both missing and orphan, and reports UNKNOWN rather than
  zero on a partial fetch.
- **Orphan reporting** — a clone whose remote project returns 404 is listed
  with its local path. Reporting only: a 404 can also mean a token that
  briefly lost access.
- **A large-repository fallback.** A multi-gigabyte repository that dies
  mid-pack on every attempt (a server-side response cap, not a client
  defect) falls back to a shallow clone, producing a real working tree
  instead of a project that is never fetched. It is reported as `shallow`,
  never as `ok`.
- **User-namespace project discovery** in the API layer, deliberately not
  wired into any command: with an admin token it would enumerate every
  user's private namespace, so it has to be an explicit opt-in.
- **A regression suite of 504 checks** across 28 test files, covering destination
  anchoring, pagination strictness, coverage-based status, resume validity,
  URL normalisation, credential hygiene, redaction, error-log concurrency
  and the interactive action seam. It uses real repositories in a temporary
  directory; git is never mocked and the user's clone tree is never written
  to.
- **`version.py`** as the single source of the version string.

### Changed

- **`ca` (Clone All) now includes partially cloned groups.** It previously
  fetched only groups with nothing on disk, which is how a group missing
  most of its projects was never revisited. A partial group also still offers
  **Clone** in its own menu, because `sync` can never fetch a project that
  was never cloned.
- **Sync picks a remote instead of assuming one.** For each repository it
  fetches every remote, measures how far ahead or behind each is, chooses
  the furthest that can fast-forward, and merges `--ff-only`, so it never
  creates an unwanted merge commit. The branch is whatever the repository is
  actually on; nothing assumes `main` or `master`.
- **Dependency floors raised** (`requests>=2.33.0`, `python-dotenv>=1.2.2`).
  The previous floors installed cleanly and pulled in four known CVEs;
  audited against a real install, not only against the requirements file.

### Security

- Tokens are stored in `~/.gitlab-cli/env` at mode 600, never in
  `instances.json`, and an environment variable always wins over the file.
- Every logged or printed value passes a redactor, so a token cannot reach
  an error log or the terminal.
- Clone URLs are validated before git sees them: only `http`/`https` are
  accepted, and a `--` separator stops git parsing a path as an option.
  This is what blocks `--upload-pack=` and `ext::sh -c` remote-code
  execution — `shell=False` alone does not.
- CI scans the **full** history for secrets (`fetch-depth: 0`), alongside
  semgrep, bandit, pip-audit and ruff, with one scanner invocation per
  ruleset so a registry failure in one cannot mask findings from another.

[1.2.0]: https://github.com/OWNER/gitlab-cli/releases/tag/v1.2.0
[1.1.0]: https://github.com/OWNER/gitlab-cli/releases/tag/v1.1.0
[1.0.0]: https://github.com/OWNER/gitlab-cli/releases/tag/v1.0.0
