# GitLab CLI

**v1.2.0** · [Changelog](CHANGELOG.md) · [License](LICENSE.md)

A command-line tool for working with several GitLab instances at once:
clone whole group trees, sync hundreds of repositories intelligently,
review team activity, and audit your remotes.

It is built for every access level, from Guest to Admin. Anything your
token cannot do is either hidden or greyed out with the reason shown.
It never crashes on a permission it does not have.

---

## Install

Requires Python 3.10 or newer, and git.

```bash
git clone <this-repo> gitlab-cli
cd gitlab-cli
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python gitlab_cli.py
```

The first run opens a setup wizard that asks for everything it needs.

Optional, for convenience:

```bash
./install.sh          # puts a `gitlab-cli` command on your PATH
```

`install.sh` writes a shell alias that calls this checkout by absolute
path: `gitlab-cli` = `<install-dir>/venv/bin/python <install-dir>/gitlab_cli.py`.
That matters — see *Where your repositories go* below.

---

## MCP server (for AI agents)

`gitlab-cli mcp` runs a [Model Context Protocol](https://modelcontextprotocol.io)
server over stdio, so an AI agent can read your GitLab instances and, with your
confirmation, act on them. Reads run at once. Every write returns a preview
and a single-use confirmation token first, and changes nothing until it is
called again with that token. The full tool list, the confirmation flow and
the safety model are in [docs/MCP.md](docs/MCP.md).

**Register it by absolute path.** `install.sh` creates a shell *alias*, and an
MCP client does not start a shell, so it cannot see `gitlab-cli`. Point the
client at the venv's Python and `gitlab_cli.py` instead:

```bash
# Claude Code
claude mcp add gitlab -- \
  /path/to/gitlab-cli/venv/bin/python /path/to/gitlab-cli/gitlab_cli.py mcp

# look-only agent: writes off, and a human must paste any token
claude mcp add gitlab-ro \
  --env GITLAB_MCP_READ_ONLY=1 \
  --env GITLAB_MCP_CONFIRM_MODE=human \
  -- /path/to/gitlab-cli/venv/bin/python /path/to/gitlab-cli/gitlab_cli.py mcp
```

Any other MCP client takes the same two parts: `command` is
`/path/to/gitlab-cli/venv/bin/python` and `args` is
`["/path/to/gitlab-cli/gitlab_cli.py", "mcp"]`.

- `GITLAB_MCP_READ_ONLY=1` turns every write off: write tools are rejected
  before a preview is built, and the HTTP layer refuses every `POST`, `PUT`
  and `DELETE`. Use it whenever the agent only needs to look.
- The server reads tokens from `~/.gitlab-cli/env`, like the CLI. **Do not put
  a token in the MCP client's config**: those files are plain text.

The same reports and previewed writes are available by hand:
`gitlab-cli report <command>` (see [docs/CLI.md](docs/CLI.md); run
`gitlab-cli report --help` for the list).

---

## First run

The wizard asks for:

1. **Name** — anything you like (`Work`, `Personal`, `Internal`, ...)
2. **URL** — for example `https://gitlab.com` or `https://gitlab.example.com`
3. **Token** — a Personal Access Token (input is hidden as you type)
4. **Directory** — where repositories get cloned

It then tests the token and tells you exactly what access you have.

### Getting a token

GitLab → **Settings → Access Tokens** → create a new token.

Scopes: `read_api` + `read_repository` is the minimum; add `api` for
write actions.

| Scope              | Needed for |
|--------------------|-----------|
| `read_api`         | listing groups, projects and members (minimum) |
| `read_repository`  | clone and fetch (minimum) |
| `api`              | write actions: comments, approvals, merges, branches (MCP server and `gitlab-cli report`) |
| `write_repository` | push, if you need it |

> **Important:** use a Personal Access Token, **not** a project or group
> bot token. A bot token returns 401 and cannot see your groups.

Tokens are stored in `~/.gitlab-cli/env` with mode 600 — **not** in
`instances.json`, so they can never end up in a commit.

To manage them yourself, set an environment variable instead:

```bash
export GITLAB_TOKEN_WORK=glpat-xxxx       # for the instance named "Work"
export GITLAB_TOKEN=glpat-xxxx            # fallback for all instances
```

An environment variable always wins over the file.

### The token is never written into a repository

A clone needs the token, but it is not left behind in the clone. Before
cloning, the token is handed to git's credential helper
(`~/.git-credentials`, mode 600); the remote URL written into
`.git/config` is the clean, credential-free URL, and `.git/config` is
chmod'ed to 600 as a second line of defence. `git pull` inside the clone
keeps working because the credential helper supplies the token.

If your clones predate this behaviour they may still carry
`http://oauth2:<token>@host/...` in `.git/config`. `gitlab-cli remotes`
finds them and `gitlab-cli remotes --fix` cleans them up.

---

## Several GitLab instances

Add as many as you want:

```bash
gitlab-cli setup        # add another instance
gitlab-cli instances    # list them with their access levels
```

Press `b` in the menu to switch between them. Each instance keeps:

- its own token, stored separately and securely
- its own destination directory
- its own capability set, cached for 12 hours

### Directory layout

With more than one instance, give each its own subdirectory so
same-named groups do not collide:

```
MasterGroups/
├── work/              ← internal GitLab
│   ├── platform/
│   └── infra/
└── personal/          ← another GitLab
    ├── projects/
    └── docs/
```

The wizard asks for this (`folder`). Leave it empty and groups are
cloned directly into the root directory.

Two groups in different namespaces may share a project *name*
(`atlas/feed-ms` and `beacon/feed-ms` are different projects). Identity
is always the full `namespace/project` path, never the last component,
so they never collide and are never mistaken for one another.

### Where your repositories go

The destination in your config is resolved **against the install
directory**, never against the shell's current directory. `gitlab-cli`
run from `~`, from `/tmp` or from anywhere else always writes to the
same managed folder. `doctor` prints the absolute path it resolved to.

If you pass a directory explicitly on the command line:

- a **relative** path (`./here`) is resolved against your current
  directory, like any other Unix tool;
- an **absolute** path is used as given;
- either way, if it lands outside the managed folder you get a warning
  saying so — because the interactive menu only ever reads the managed
  folder, so anything written elsewhere will not be listed there.

---

## Commands

```bash
gitlab-cli                          # interactive mode
gitlab-cli setup                    # add a GitLab instance
gitlab-cli doctor [--quick]         # health check
gitlab-cli instances                # list instances
gitlab-cli clone   <group> [dir]    # clone an entire group tree
gitlab-cli sync    <group> [dir]    # smart sync
gitlab-cli log     <group> [since] [author] [dir]
gitlab-cli remotes [dir] [--fix]    # audit remotes (active instance only)
gitlab-cli harden-perms [--dry-run] [root ...]
gitlab-cli --help                   # this list
```

`[dir]` is optional everywhere. Left out, it is the managed folder from
your instance config.

Add `--debug` (or set `GITLAB_CLI_DEBUG=1`) to any command to print the
full Python traceback after the plain-language error. You never need it
day to day; it is what you attach to a bug report.

Retrying failed projects and browsing the error log are available from
the interactive menu (a group with leftover errors is marked `⚠` and
offers a **Retry** action; the main menu has an **Error Log** section).

### Clearing out entries you can no longer act on

The Error Log screen offers `s` — *remove entries whose directory no
longer exists*. The rule is deliberately narrow. An entry is removable
only when **both** its target directory and the directory above it are
gone: a failure you can still go and look at is never removed, and a
user who deleted one repository by hand still has the group folder above
it, so their failure is kept. Entries with no filesystem target at all
(an API failure, for instance) can never be removed by this action, and
neither can a line the reader could not parse.

Counting the candidates is a pure read — the count on the screen and the
badge on the main menu never modify the log. Removal requires the typed
word `yes`, writes a dated backup of the whole log first, and re-checks
every record inside the lock, so a directory that came back between the
preview and the confirmation is kept.

Separately, entries older than 30 days are dropped when the Error Log
screen is opened.

### Verifying the mirror independently

```bash
./scripts/verify-mirror                        # every instance, full report
./scripts/verify-mirror --verbose
./scripts/verify-mirror --instance work --json /tmp/work.json
```

This is a **referee**, not part of the CLI. It imports nothing from this
project and runs under the system `python3`, so it cannot resolve the
CLI's own modules or dependencies — a checker that reuses the subject's
code cannot detect the subject's bugs, which is exactly how "Clone All:
done" once coexisted with a large share of a group's projects never
fetched.

It reports, per instance, what the server has versus what is on disk,
probes GitLab's rename redirect so a renamed project is not counted as
both missing and orphan, and flags shallow, broken and duplicated
clones. A partial fetch is reported as **UNKNOWN**, never as zero.

Its exit code is the machine-readable half of the report, and each code
names one category so a caller can tell a *correctness* failure from an
*operational* one without parsing the text:

| code | category | meaning |
|---|---|---|
| `0` | CLEAN | complete and tidy |
| `1` | CORRECTNESS | the server has data this mirror does not |
| `2` | OPERATIONAL | undetermined — the check could not conclude |
| `3` | SECURITY | complete, but a credential is on disk |
| `4` | HYGIENE | complete; the tree is untidy |

Severity order is deliberately not numeric order: `2` means the referee
could not answer, which is not "less bad" than `3`. Nothing that went
wrong exits `0`, and an unfinished check never borrows the code for a
successful one.

Read the code in the shell that actually runs the script — a
`wsl -- bash -lc '...'` wrapper returns 0 regardless of what it ran.

---

## Cloning a group

`clone` walks the group and every subgroup below it and clones each
project into `<dest>/<namespace>/<...>/<project>`.

It is **resumable**. A project that is already a healthy clone is
skipped, so re-running after an interruption or a network failure picks
up where it stopped. "Healthy" means git itself can resolve the
repository — a directory left half-written by an interrupted clone is
reported as **broken/invalid**, not silently counted as done. Nothing is
ever deleted automatically; you are told the path and you decide.

Failures are written to a per-group error log next to the destination
(`.<group>_errors.json`, with nested groups flattened, e.g. a group
`platform/core` under `acme` becomes
`.acme__platform__core_errors.json`). The group shows `⚠` in the
menu while that log has entries, and **Retry** re-runs only the failed
projects. Ctrl-C saves the log before exiting.

### Very large repositories

A multi-gigabyte repository can die mid-transfer on every attempt while
every other repo on the same host clones fine. That is a server- or
proxy-side response cap, not a fault in your network — it survives
HTTP/1.1, a larger `http.postBuffer` and disabled compression, so
retrying the same full fetch only burns hours.

When, and only when, the failure matches that signature, the clone falls
back to a **shallow** clone, which asks the server for a far smaller
response and produces a real, usable working tree instead of a project
that is never fetched. It is reported as `shallow`, never as `ok`, and
you are told in plain words — because a shallow clone is not a mirror.

### Group status in the menu

A group's status is based on how many of its projects are really on
disk, not on whether a folder with its name exists:

| Icon | Meaning |
|---|---|
| `✅` | every project of the group is cloned (a group with no projects counts as complete) |
| `◐` | partially cloned — some projects are missing |
| `❌` | nothing cloned |
| `⚠` | the group has a leftover error log |
| `⚑` | orphan — a clone on disk whose project no longer exists on the server |

Each group also shows `cloned/total`, and the list ends with an overall
line:

```
40/40 group projects on disk (45 on the instance — 5 user-namespace
projects excluded by policy)
```

**Why there are two numbers.** The group list's denominator is built by
walking groups, so a project in a personal namespace can never enter it.
`doctor`'s denominator came from the flat project list, which does
contain them. Both fractions were correct and they disagreed, two
screens apart, with nothing on either screen saying why — so the tool
answered one question two ways. They are now rendered by one function
into one labelled sentence: the mirrored fraction first, because that is
the number `ca` can actually change, then the instance total and what
the difference consists of. When the two denominators coincide the
parenthetical disappears. If they stop reconciling, the tool reports
UNKNOWN rather than inventing a third number.

"On disk" is decided by the **origin URL** of each clone, not by its
location, so a repository you moved somewhere else under the managed
folder still counts, and an empty directory or a repo with no origin
never does.

`ca` (**Clone All**) fetches every missing project of every group that
is `❌` **or** `◐`. Partial groups are included — that is the point.
A partially cloned group also still offers **Clone** in its own action
menu, because `sync` can never fetch a project that was never cloned.

### Archived projects

Archived projects are **included** by default. Hiding them is not a
cosmetic preference: an archived repository is precisely the history
nobody else is keeping, and a mirror that silently omits it prints `✅`
while the thing you most wanted to preserve is missing. They are counted
in coverage like any other project.

### Orphans, and projects that were renamed

A clone whose remote project returns 404 is listed after the group list
with its local path. This is **reporting only** — nothing is moved and
nothing is deleted, because a 404 can also mean a token that briefly
lost access.

A *rename* is no longer part of that category. Before asking whether a
project is gone, the tool asks the server about the old path and reads
the answer it gives back:

| the server says | conclusion |
|---|---|
| 200 at a different path | **renamed** — this is the server following its own redirect, not a guess |
| 200 at the same path | still there |
| 404 | gone |
| 401 / 403 / 429 / 5xx / timeout | **undetermined** — never read as a deletion |

A renamed project is *repaired in place*: the remote is repointed and
the directory is moved to where the new path belongs. Nothing is
re-cloned, so a rename no longer produces a second full copy on disk
alongside an orphan directory that audits then report forever.

The repair is refused — loudly, and without moving anything — when the
existing clone holds work the server does not have: commits that are not
upstream, stash entries, or uncommitted changes. Those are listed by
path with the reason, for you to resolve by hand. A shallow clone is
refused outright, because containment cannot be measured honestly on
one.

### Projects in personal namespaces

Group traversal cannot see a project that lives in a *user* namespace
(`someuser/their-project`): `GET /groups/<user-namespace>` is a 404.
Such projects are therefore not reachable by "Clone All", which walks
groups.

They are no longer silently absent. `doctor` counts them, names whose
namespaces they are, and prints the setting that would include them:

```
!  user namespaces          scope=off — 5 project(s) excluded by policy
   5 project(s) live in USER namespaces and are EXCLUDED by policy —
   they are not missing and not broken, and the group-based menu cannot
   reach them
```

The setting has **three** values and defaults to `off`:

| value | what is in scope |
|---|---|
| `off` *(default)* | nothing. No extra API call is made at all. |
| `own` | only the namespace of the account the token authenticates as |
| `all` | every user namespace the token can see |

```bash
export GITLAB_CLI_USER_NAMESPACES=own
```

or, per instance, a `"user_namespaces"` key in
`~/.gitlab-cli/instances.json`. The instance key wins over the
environment variable. A value that is not one of the three is **not** an
opt-in: it degrades to `off` and `doctor` says so, because a setting that
looks like it worked but did not is the same failure as a silent
exclusion.

> **Why the default is off, and why you should think before changing it.**
> `all` means *every user namespace this token can see*. If the token you
> configured is an admin token — and on a self-hosted instance it often
> is — that is every employee's personal namespace, including their
> private scratch repositories. Turning this on with an admin token is a
> decision about other people's data, not a convenience setting. `own` is
> the safe first step; it only ever reaches the namespace of the account
> the token belongs to, and it returns nothing at all if that account
> owns no personal project.

Discovery is read-only: enabling the setting makes the tool *list and
disclose* these projects. It does not by itself clone them.

### Tightening permissions on clones that already exist

```bash
gitlab-cli harden-perms --dry-run          # report only, changes nothing
gitlab-cli harden-perms                    # apply
gitlab-cli harden-perms /some/other/tree   # including a tree this tool did not create
```

New clones are created with a private `.git/config` (see *Credentials*
below), but that does nothing for repositories cloned before the fix, or
by git directly. This walks every repository under each root and tightens
`.git/config`, the reflog, `FETCH_HEAD` and `.git` itself to 0600/0700.

The walk is **exhaustive**: it does not stop at the first `.git` it
finds, so a repository nested inside another repository's work tree is
swept too — a pruning walk misses every repository nested inside another
one.

`--dry-run` is genuinely read-only. It counts what differs from the
target mode and performs no `chmod` at all, so neither the mode nor the
mtime of any file changes; that property is asserted by a test that
compares full `stat` results before and after, not by the flag's name.
Either way the output is counts and mode histograms — never the contents
of any file it opened.

Extra roots may be named on the command line, because a tree the tool did
not create is exactly the tree a config-scoped cleanup silently misses.
This is a maintenance pass you run, not a hidden step of clone or pull:
making every sync walk tens of thousands of directories to re-check modes
that are already correct is a cost with no matching benefit.

---

## Recent commits

```bash
gitlab-cli log <group> [since] [author] [dir]
```

`since` is handed to `git log --since=`, and git's date parser has **no
error channel**: anything it cannot read it silently treats as *now*,
exits 0 and prints nothing. `gitlab-cli log my-group b` therefore used
to print `No commits found since 'b'` about a group full of commits — a
result produced by one mechanism and reported as if verified by another.

Both filters are now checked before anything is queried:

- **`since`** is accepted if it is a recognised date form, or if git
  resolves it to a moment that is not exactly now. A value that fails
  both is refused, nothing is searched, and the refusal says so.
- **`author`** is a *regular expression* as far as git is concerned, so
  a stray `[` makes git exit 128 — which the old code mapped to an empty
  result. An unusable pattern is now reported as an unusable pattern.

Every accepted value echoes the absolute moment git settled on, so a
value git reinterpreted is visible rather than silent:

```
ℹ  Since : 2 weeks ago
ℹ          → commits after 2026-09-09 08:50
```

### Three different nothings

An empty screen has three causes and the tool now distinguishes them:

| what happened | what you see |
|---|---|
| the input was not understood | `git does not understand 'b' as a date` + `Nothing was searched. This is NOT a statement about your commits.` |
| the filter genuinely matched nothing | `No commits after …` + `Verified: 12 of 12 repos DO have history, so the filter is what excluded them` |
| the repositories have no commits at all | `12 repos were searched and none of them has any commit at all — so this is not about your 'Since' filter` |

The middle row is the point: that second sentence is produced by asking
git again *with the filter removed*, not by assuming.

A value is passed to git as a single argv element and never through a
shell, so `--all` is the literal string `--all` and never an option, and
shell metacharacters have no shell to reach.

### The same three nothings on the Members screen

A member's activity screen had the identical defect against a different
backend: its `Since` value went to GitLab's `?after=` parameter, and an
unresolvable filter came back empty and read as "no activity". It now
shares the log screen's validation, converts the resolved moment to the
calendar day the events API requires, and echoes all three — what you
typed, what git read it as, and what was actually sent:

```
ℹ  Since : 2 weeks ago
ℹ          → git read this as 2026-09-09 08:50
ℹ          → querying GitLab with after=2026-09-08
```

An empty result is checked the same way, by asking again without the
filter. If that second check itself does not complete, the tool says the
first line is unconfirmed rather than treating an unfinished check as a
negative answer.

---

## Smart sync

Rather than running a plain `git pull`, for each repository it:

1. runs `fetch --all --prune --tags`
2. measures how far ahead or behind **every** remote is
3. picks the furthest remote that can fast-forward
4. runs `merge --ff-only`, so it never creates an unwanted merge commit

The current branch is whatever the repository is actually on — nothing
assumes `main` or `master`.

Each repository ends in one of these states:

| Status | Meaning |
|---|---|
| `✓ sync` | up to date |
| `↓ pulled` | updated |
| `↑ ahead` | you have unpushed commits |
| `⇅ diverged` | ahead and behind — resolve by hand |
| `✎ dirty` | uncommitted changes present |
| `⊘ gone` | repository no longer on the server (deleted or renamed) |
| `🔒 auth` | token lacks access |

At the end it lists only the repositories that need attention.

**Fetches run in parallel**, merges run one at a time so they stay safe.

---

## Remote audit

```bash
gitlab-cli remotes
```

For every repository **of the active instance** it reports:

- how many remotes it has and which hosts they point to
- which ones store a plain-text token in the URL (**a security risk**)
- repositories with no remote at all

### It audits one instance, not one directory

Several instances can share a clone root, and an instance whose `folder`
is empty has the shared root *as* its subtree — so a directory walk from
its destination structurally contains the other instance's entire tree.
The audit and the scrub therefore decide by **ownership**, not by where
the walk happened to reach: repositories belonging to another configured
instance, and repositories under the shared root belonging to no
instance, are excluded from both and reported as a count that names the
owner.

```
  Instance : personal   12 repositories in scope
  scope    : /path/to/gitlab-cli/MasterGroups
  excluded : 40 repositories belong to instance 'work' and were NOT audited
```

The same sentence is printed before any confirmation prompt, so what
`--fix` is about to rewrite is stated before you answer it. The scrub
re-applies the filter itself rather than trusting the audit to have done
it — both are handed the same list of paths, and the scrub is the write
path.

A destination outside the managed root is never treated as foreign, so
`gitlab-cli remotes /somewhere/else` still audits what you pointed it at.

The walk finds **nested** repositories too — submodules, vendored
checkouts and `git worktree` trees inside another repository — so a
credential hidden one level down is not missed. It never descends into a
`.git` directory and never follows symlinks.

`--fix` strips tokens out of those URLs and hands them to
`git credential store`. Pulling still works, but the token is no longer
sitting in `.git/config`, and the file is tightened to mode 600.

> Why this matters: a token embedded in a remote URL is stored in
> `.git/config` in plain text, and any script running `git remote -v`
> prints it.

---

## Doctor

```bash
gitlab-cli doctor            # everything
gitlab-cli doctor --quick    # skip the network and the tree walk
```

Checks, in order:

- **System** — git installed, version, credential helper, `user.name`/`user.email`
- **Files** — config dir and `env`/`instances.json` permissions (700/600)
- **Per instance** —
  - token present and valid, who it authenticates as, access level
  - the **absolute** managed folder it resolved to
  - **coverage**: how many projects the instance has vs how many are on
    disk, per namespace, with `✅ / ◐ / ❌` and `n/N`
  - **user namespaces**: how many projects the group-based menu cannot
    reach, whose namespaces they are, and the setting that includes them
  - **credential hygiene**: clones whose `.git/config` holds a token
    (counts and paths only — no credential value is ever printed), and
    how many `.git/config` files are group- or world-readable
  - **layout**: projects cloned into more than one directory, and clones
    sitting where the current configuration would not put them
  - **config vs disk**: a missing or mismatched `folder` key, i.e. the
    config pointing somewhere other than where your existing tree is

Every problem it finds comes with the fix. Nothing is moved, deleted or
rewritten — `doctor` only reports.

With `--quick`, the network and tree-walk checks report **UNKNOWN** and
are counted as problems. They are never assumed to be OK.

---

## Access levels

The tool works out what your token can do, then adapts the menu:

| Capability | Meaning |
|---|---|
| `read` | list groups and projects |
| `clone` | clone and fetch |
| `write` | push |
| `members` | view members of groups you belong to |
| `all_users` | view every user on the instance (admin) |
| `admin` | manage users |

Capabilities are probed once per token and cached for 12 hours.

Without admin rights:

- the Members section is built from your own groups instead of `/users`
- admin-only options are greyed out with the reason
- no 403 ever surfaces as a crash

A failed API page is an **error**, not an empty result: a bad or expired
token makes the tool say so, instead of quietly reporting that you have
zero projects.

---

## Layout

```
gitlab_cli.py    entry point, adaptive menu, destination resolution
version.py       the single source of the version string
setup.py         first-run wizard and doctor
instances.py     multi-instance management
paths.py         install-dir anchoring and safe path shaping
coverage.py      what is really on disk, matched by origin URL
credentials.py   token loading: env var or 600-mode env file
caps.py          access-level detection, cached 12h
api.py           GitLab API, strict pagination, non-admin fallbacks
clone.py         recursive group cloning, resume, per-group error log
pull.py          parallel sync, full nested-repo discovery
git.py           per-repository sync logic and credential hygiene
remotes.py       remote audit and scrub
members.py       users, activity, access map
log.py           recent commits
errors.py        error log, stale-entry cleanup, suggested fixes (tokens redacted)
error_catalog.py one catalog of failure codes: title, cause, fix
printer.py       colour and formatting
scripts/         verify-mirror — the independent completeness referee
gitlab_mcp/      MCP server, and the engine behind `gitlab-cli report`
docs/            MCP.md (MCP server), CLI.md (`gitlab-cli report`)
tests/           regression suite — see tests/README.md
```

### Tests

```bash
./venv/bin/python tests/run_tests.py
```

About 1,950 checks across 68 test files (run it for the exact figure — it
moves as tests are added). The suite pins the behaviour described
above: destination anchoring, strict pagination, coverage-based group
status, `ca` including partial groups, resume validity, URL
normalisation, credential hygiene and redaction, error-log concurrency
and stale-entry cleanup, archived-project visibility, date and author
validation, remote-audit instance scoping, the user-namespace opt-in
defaulting to off, the interactive action seam, and the MCP server's
confirmation and read-only gates. It uses real
repositories in a temp directory; git is never mocked and your real
clone tree is never written to.

Opt-in live tests, which need a reachable GitLab and a configured
instance and skip cleanly without one:

```bash
GITLAB_CLI_LIVE_TESTS=1 ./venv/bin/python tests/run_tests.py
```

---

## Your private files

These are in `.gitignore` and **never get committed**:

- `~/.gitlab-cli/env` — tokens (mode 600)
- `~/.gitlab-cli/instances.json` — instance configuration
- `MasterGroups/*` — your cloned repositories
- `.*_errors.json`, `errors.log` — per-group and global error logs

The `MasterGroups/` directory itself is tracked (via `.gitkeep`), but
nothing inside it is.

---

## Troubleshooting

**Getting a `401`**
The token has expired, or it is a bot token. Run `gitlab-cli doctor`,
then enter a new token from the instances menu.

**No groups are listed**
The token is missing the `read_api` scope, or you genuinely have no
group access. `doctor` tells you which one it is.

**"Clone All" says done but projects are missing**
Run `gitlab-cli doctor` and read the **coverage** line: it prints
`n/N projects on disk` per namespace. If a namespace is `◐` or `❌`,
`ca` in the menu will pick it up. If `doctor` also reports a
`config matches the disk` problem, your `folder` setting and your
existing tree disagree — new clones are landing next to the old ones
rather than in them.

**A project keeps being skipped**
Its directory exists but is not a valid repository (an interrupted
clone). `clone` reports it as broken/invalid with its path; remove or
rename that directory yourself and re-run.

**An internal GitLab does not respond**
Usually the VPN. Ping the host and check `doctor`.

**Sync hangs**
Every git command has a 120-second timeout and runs with
`GIT_TERMINAL_PROMPT=0`, so an indefinite hang should not happen. If it
does, run `doctor`.

---

## License

[PolyForm Noncommercial 1.0.0](LICENSE.md)

**Free for:**
- personal use, hobby projects, learning, evaluation
- modifying and forking
- universities, schools, NGOs, government bodies, nonprofits

**Not permitted:**
- commercial use, such as day-to-day work inside a company
- selling it, or bundling it into a product you sell
- offering it as a paid service

If you want to use it commercially, get in touch and we can talk.
