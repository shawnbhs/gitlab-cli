# The GitLab MCP server (`gitlab-cli mcp`)

`gitlab_mcp` gives an AI agent access to the gitlab-cli toolbox through the
Model Context Protocol. The layer is thin. The agent speaks MCP, and the
server calls the same gitlab-cli code that the command line uses
([CLI.md](CLI.md)).

The whole surface follows two rules:

* **Reads run immediately.** A read tool never needs confirmation.
* **Every write goes through two calls.** Any tool that changes something,
  whether on GitLab or on local disk, has to be called twice. The first call
  returns a preview and a single-use `confirm_token`, and it changes nothing.
  The agent is supposed to show that preview to you and wait for your
  explicit approval. Only then may it call again with the token.

  How strong that guarantee is depends on the confirmation mode. See
  [Who can confirm](#who-can-confirm-gitlab_mcp_confirm_mode) before you rely
  on it.

The transport is stdio, so **stdout carries the protocol**. The server never
prints. Some gitlab-cli functions do print, and those run with their output
captured and token-redacted. That output comes back inside the tool result.

Contents:

* [Read tools](#read-tools)
* [Write tools](#write-tools)
* [The confirmation flow](#the-confirmation-flow)
* [Who can confirm (`GITLAB_MCP_CONFIRM_MODE`)](#who-can-confirm-gitlab_mcp_confirm_mode)
* [GitLab quick actions (`allow_quick_actions`)](#gitlab-quick-actions-allow_quick_actions)
* [Read-only mode (`GITLAB_MCP_READ_ONLY`)](#read-only-mode-gitlab_mcp_read_only)
* [Local clones: confinement and git hardening](#local-clones-confinement-and-git-hardening)
* [Registering the server](#registering-the-server)
* [Credentials](#credentials)
* [Provenance](#provenance)

The tool list and parameters below were taken from the code itself. The
server was imported in the gitlab-cli venv and FastMCP's `list_tools()` was
read, as described under [Provenance](#provenance).

---

## Read tools

Every read tool has the description prefix `[READ-ONLY tool]`. None of them
accepts a `confirm_token`.

Every read tool except `gitlab_instances` takes an optional `instance`. This
is the name of a configured GitLab instance, as listed by `gitlab_instances`.
If you leave it out, the active instance is used.

The **Parameters** column below lists required parameters in **bold**,
followed by optional ones with their defaults. `instance` is left out of the
column because every row would repeat it.

| Tool | Parameters | Returns |
|---|---|---|
| `gitlab_instances` | none | The configured instances, with name, URL and which one is active. Tokens are never included. |
| `gitlab_groups` | `search=null`, `limit=100` | Groups. |
| `gitlab_projects` | `group=null`, `search=null`, `limit=200` | Projects, including those in subgroups. |
| `gitlab_project_overview` | **`project`**, `since="30d"` | Default branch, last activity, commits since `since`, open MRs, top contributors and languages. |
| `gitlab_commits` | **`project`**, `since="7d"`, `until=null`, `author=null`, `branch=null`, `with_stats=true`, `limit=200` | Commits, with additions and deletions per commit. `with_stats=false` is much cheaper. |
| `gitlab_commit_detail` | **`project`**, **`sha`**, `max_diff_chars=60000` | One commit: its message, stats and per-file diffs. |
| `gitlab_compare` | **`project`**, **`base`**, **`head`**, `max_diff_chars=60000` | The diff between two refs. |
| `gitlab_contributors` | **`project`**, `since="30d"`, `branch=null` | Per author: commits, lines added and removed, and first and last commit dates. |
| `gitlab_person_report` | **`people`**, `since="7d"`, `until=null`, `projects=null`, `max_projects=50` | What the named people did: events, commits and lines per project, MRs authored. The report states its own method and limitations. |
| `gitlab_project_progress` | **`project`**, `since="30d"` | Commits per week and per author, MRs merged and open, pipeline success rate and the latest tags. |
| `gitlab_merge_requests` | `project=null`, `group=null`, `state="opened"`, `author=null`, `since=null`, `limit=100` | MRs for one project or for a whole group. |
| `gitlab_merge_request` | **`project`**, **`iid`**, `with_diff=true`, `max_diff_chars=80000` | One MR in full, for review: description, discussions, approvals, pipeline, and the changed files with diffs. |
| `gitlab_pipelines` | **`project`**, `ref=null`, `limit=20` | Recent CI pipelines, newest first. |
| `gitlab_branches` | **`project`**, `search=null` | Branches. |
| `gitlab_users` | **`search`**, `limit=20` | A user search by username, name or email fragment. |
| `gitlab_file_content` | **`project`**, **`path`**, `ref=null`, `max_chars=100000` | One file at a ref. The file path is URL-encoded, so it cannot leave the project's files API. |
| `gitlab_local_status` | `group=null` | The local clones under the managed directory: path, branch, dirty state, ahead/behind and `origin` (credentials stripped). Nothing is fetched or changed. See [Local clones](#local-clones-confinement-and-git-hardening). |

Notes on the parameters:

* `project` is a `group/subgroup/project` path, for example
  `example-group/project-a`, or a numeric project id.
* `since` and `until` accept `YYYY-MM-DD` or a relative span: `7d`, `2w`,
  `1m` or `today`.
* `people` and `projects` on `gitlab_person_report` accept either a list or
  a comma-separated string, for example `"alice,bob"`.

### Result shapes

A tool that returns a list caps it:

```json
{"items": [...], "total": 1234, "truncated": true}
```

`total` is the real count. `items` holds at most 500 rows. If you need more,
narrow the query rather than raising the cap.

A failure never raises through MCP. It comes back like this:

```json
{"status": "error", "error": "<redacted message>", "type": "GitLabError"}
```

Tokens, `PRIVATE-TOKEN` headers and `oauth2:…@` URL userinfo are redacted
from every message, including preview and `done` results.

---

## Write tools

Every write tool has the description prefix `[WRITE tool] … REQUIRES
confirm_token`. Every one also takes the optional parameters `instance` and
`confirm_token`, which the **Parameters** column does not repeat.

| Tool | Parameters | Changes |
|---|---|---|
| `gitlab_sync_local` | `group=null`, `project=null` | Fetches local clones and pulls them **fast-forward only**. A repo that is dirty, ahead, diverged or detached is reported and left alone. Nothing is merged, rebased or reset. |
| `gitlab_clone_missing` | **`group`** | Clones the group's missing projects into the managed directory. Existing clones are skipped and never overwritten. |
| `gitlab_mr_comment` | **`project`**, **`iid`**, **`body`**, `allow_quick_actions=false` | Posts a plain comment on a merge request. |
| `gitlab_mr_review_comment` | **`project`**, **`iid`**, **`body`**, **`path`**, `new_line=null`, `old_line=null`, `allow_quick_actions=false` | Starts a review discussion on one diff line. |
| `gitlab_mr_approve` | **`project`**, **`iid`** | Records an approval, [pinned to the previewed head sha](#what-the-server-pins-from-the-preview). |
| `gitlab_mr_unapprove` | **`project`**, **`iid`** | Withdraws an approval. |
| `gitlab_mr_create` | **`project`**, **`source_branch`**, **`target_branch`**, **`title`**, `description=""`, `draft=false`, `allow_quick_actions=false` | Opens a merge request. The preview shows the description that will be sent. |
| `gitlab_mr_merge` | **`project`**, **`iid`**, `squash=null`, `remove_source_branch=false` | **Merges a merge request.** This is the most destructive tool. When `squash` is `null`, the project's own default applies. |
| `gitlab_issue_comment` | **`project`**, **`iid`**, **`body`**, `allow_quick_actions=false` | Posts a comment on an issue. |
| `gitlab_create_branch` | **`project`**, **`branch`**, **`ref`** | Creates a branch from an existing ref. |

Branch and ref arguments are checked with gitlab-cli's `git.check_ref`.
Names such as `-orphan`, ` -c` or `--upload-pack=…` are rejected before any
request is made.

---

## The confirmation flow

### Call 1: no `confirm_token`

The server validates the arguments and reads what it needs, using only
`GET` requests. It then returns:

```json
{
  "status": "needs_confirmation",
  "confirm_mode": "agent",
  "preview": {
    "action": "mr_merge",
    "ok": true,
    "danger": true,
    "summary": "Merge example-group/project-a!42 — 'Add retry' ('feature/retry' → 'main') at 9f2a1c4d8e30.",
    "reasons": [],
    "warnings": ["DANGER: 'main' is a protected/production target — ..."],
    "sha": "9f2a1c4d8e30...",
    "merge_status": "mergeable"
  },
  "confirm_token": "KQ3n…",
  "expires_in": 900,
  "danger": true,
  "message": "Nothing has been changed yet. Show the preview above to the user verbatim …"
}
```

Nothing has changed at this point. `danger` is copied to the top level so
that a protected target cannot be missed. The protected targets are `main`,
`master`, `production` and `prod`, plus any name starting with `release`,
compared case-insensitively.

### Call 2: the same arguments plus `confirm_token`

The server checks the token and runs the action:

```json
{"status": "done", "result": {"action": "mr_merge", "ok": true, "state": "merged", "...": "..."}}
```

A token can only be spent on the second call. The token-issuing path and the
executing path are separate, so no single call can both mint a token and
spend it.

### Properties of the token

* **It is bound to the tool and to the exact arguments.** The token carries
  a SHA-256 fingerprint of two things: the canonical JSON of the arguments
  (with `confirm_token` removed), and the instance name. If one character of
  a comment body changes, or the call points at a different instance, the
  token stops working.
* **It is single use.** A successful check burns the token, and so does a
  failed one. A token therefore cannot be retried with different arguments.
* **It is short-lived.** A token lasts 900 seconds (15 minutes). The server
  keeps at most 256 tokens outstanding and evicts the oldest first.
* **It is bound to the process.** Restarting the server invalidates every
  outstanding token.
* **A rejection never says why.** This stops a caller from probing the
  token store.

### What the server pins from the preview

Some values are read during the preview and then supplied **by the server**
on the confirmed call. The agent never passes them, so it cannot change them
in between.

| Tool | Pinned value | Effect |
|---|---|---|
| `gitlab_mr_merge` | The MR head `sha` | GitLab rejects the merge if someone pushed after the preview. |
| `gitlab_mr_approve` | The MR head `sha` | GitLab rejects the approval (409) if someone pushed after the preview. |
| `gitlab_mr_create` | `description_sha256` of the text that will be sent | A description that changed after the preview is rejected. |

None of these tools takes a `sha` argument.

### Rejections

```json
{"status": "rejected", "reason": "..."}
```

The server rejects a call in these cases:

* **The preview refuses the operation** (`ok: false`). Examples: conflicts,
  a draft MR, a closed MR, a branch that already exists, or nothing to sync.
  The preview is attached, but **no token is issued**, so the operation
  cannot be confirmed until the problem is fixed.
* **The token is unknown, expired or already used, or it was issued for
  other arguments.** Call again without a token to get a fresh preview, then
  approve that preview.
* **Read-only mode is on** (see
  [Read-only mode](#read-only-mode-gitlab_mcp_read_only)).

---

## Who can confirm (`GITLAB_MCP_CONFIRM_MODE`)

| Value | Behaviour |
|---|---|
| unset, empty, or `agent` | **Agent mode** (the default). The token is returned to the caller in the first result. |
| `human` | **Human mode.** The token is **not** returned. |
| any other value | Fails closed to `human`. |

### Agent mode

In agent mode the token is a **drift guard**, not proof that a human
approved. It proves that the arguments did not change between the preview
and the action. It does not prove that a person saw the preview. An agent,
or a prompt injection steering one, can pass its own token straight back
and complete a merge in two tool calls.

The tool descriptions tell the agent not to do that. That instruction is not
enforcement. If you use agent mode, turn on your MCP client's per-tool
approval prompt for the `[WRITE tool]` tools as well.

### Human mode

In human mode the server writes the token to a new private file:

* The file has mode `0600` and sits in a `0700` directory.
* On Linux that directory is `$XDG_CACHE_HOME/gitlab-mcp/confirm`, or
  `~/.cache/gitlab-mcp/confirm` when `XDG_CACHE_HOME` is not set.
* On Windows it is `%LOCALAPPDATA%\gitlab-mcp\confirm`.

The first result contains `token_file` (the file's path) and no
`confirm_token`. The agent is told to show you the preview and to ask you
to paste the token from that file.

The token's binding, single use and expiry work exactly as in agent mode.
The file is deleted when the token is used, when it expires, or when it is
evicted. If the directory is a symlink, is owned by another user, or cannot
be written, the call returns an error. It never falls back to agent mode.

Human mode keeps the token out of the agent's context. It does **not** stop
an agent that can read arbitrary files as the same OS user. If that matters
to you, combine human mode with client-side tool approval, or with a
sandbox that hides the cache directory from the agent.

---

## GitLab quick actions (`allow_quick_actions`)

GitLab executes *quick actions* when a comment or a description is posted.
A quick action is a line that begins with a slash command, such as
`/merge`, `/approve` or `/assign_reviewer`. A body copied from an MR or a
commit message could therefore merge or approve without the preview ever
having said so.

Four tools take a body or a description that could carry quick actions:
`gitlab_mr_comment`, `gitlab_mr_review_comment`, `gitlab_issue_comment` and
`gitlab_mr_create`. For each of them:

* **By default (`allow_quick_actions=false`)**, every quick-action line is
  neutralised: it is wrapped in backticks and posted as text. The preview
  then carries a `DANGER` warning listing the commands, and it shows the
  exact text that will be sent.
* **With `allow_quick_actions=true`**, and only when the value is the
  boolean `true` rather than a truthy string, the lines are sent unchanged
  and GitLab **will execute them**. The preview carries a `DANGER` warning
  saying so. Set this only when the user explicitly asks for the quick
  actions to run.

`allow_quick_actions` is one of the arguments the token is bound to.
Switching it on after the preview therefore invalidates the token.

---

## Read-only mode (`GITLAB_MCP_READ_ONLY`)

Read-only mode **fails closed**. Writes stay enabled only when the variable
is unset, empty, `0`, `false`, `no` or `off`. The comparison ignores case and
surrounding whitespace. **Any other value turns read-only mode on**,
including `1`, `true`, `enabled`, `y`, `2` and typos.

Two gates enforce read-only mode, and both use the same rule:

* **The early gate in the server** (`server._read_only()`, which delegates to
  `context.read_only()`) makes every write tool return `status: "rejected"`
  straight away. When it fires, no preview is built and no token is issued,
  and a token issued earlier stops working. This covers the local-only write
  tools `gitlab_sync_local` and `gitlab_clone_missing` too.
* **The HTTP gate** (`context.read_only()`, checked by
  `GitLabContext._ensure_writable`) refuses every `POST`, `PUT` and
  `DELETE`. The CLI goes through it as well.

Read tools work as normal.

Use read-only mode whenever the agent only needs to look. Read-only mode does
not switch off `gitlab_local_status`, which runs local `git` read commands.
The hardening described in the next section is what makes that safe.

---

## Local clones: confinement and git hardening

Three tools work on the local disk: `gitlab_local_status`,
`gitlab_sync_local` and `gitlab_clone_missing`. They act only inside the
instance's **managed directory**. This is `master_dir` from the instance
config, anchored to an absolute path.

**Confinement to `master_dir`.**

* `gitlab_local_status` validates `group` before any work starts. A `group`
  is rejected with a `ValueError` in any of these cases:
  * it is absolute (`/etc`, `C:…`);
  * it contains an empty, `.` or `..` segment;
  * it contains a backslash, a NUL byte or another control character;
  * it resolves outside `realpath(master_dir)` once symlinks are followed.
* Each repository found is checked again in the same way. A repo reached
  through a symlink that points outside the managed directory is skipped.
* `gitlab_sync_local` resolves `group` with gitlab-cli's
  `paths.resolve`, and `gitlab_clone_missing` uses
  `paths.target_for_clone`. Both reject `../…` and absolute paths.
* None of these tools accepts an arbitrary filesystem path.

**Git hardening for the read tool.** A repository found on disk is treated
as untrusted input. Its own `.git/config` could name a program for git to
run, for example `core.fsmonitor`, which runs on every `git status`. Every
git process that `gitlab_local_status` starts therefore runs with the
following settings:

* **argv only:**
  * The command is an argv list, never a shell.
  * The repository is passed as `cwd`, never as `-C <path>`. The path never
    becomes a git argument.
* **Command-line overrides:** these outrank the repository's config.
  * The command starts with `--no-optional-locks`.
  * It then passes `-c` with these values:
    * `core.fsmonitor=false`
    * `core.untrackedCache=false`
    * `core.hooksPath=/dev/null`
    * `core.pager=cat`
    * `core.sshCommand=`
    * `core.askPass=`
    * `core.gitProxy=`
    * `credential.helper=`
    * `diff.external=`
    * `protocol.file.allow=never`
    * `protocol.ext.allow=never`
* **Driver neutralisation:**
  * Before `git status` runs, the repository's `filter.*`, `diff.*` and
    `merge.*` keys that name a command (`clean`, `smudge`, `process`,
    `command`, `textconv`, `driver`) are listed. Each of them is blanked
    with `-c name=`.
  * `git status` also runs with `--ignore-submodules=all`.
  * If a configured key name cannot be overridden safely, the status check
    is skipped for that repo and `dirty_entries` is reported as `null`.
* **Environment:**
  * These variables are forced: `GIT_CONFIG_NOSYSTEM=1`,
    `GIT_OPTIONAL_LOCKS=0`, `GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=`,
    `SSH_ASKPASS=`, `GIT_PAGER=cat` and `GCM_INTERACTIVE=never`.
  * Any inherited variable that would point git at another repository or
    config, or name a program for it to run, is removed. These are
    `GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT`, `GIT_CONFIG_GLOBAL`,
    `GIT_DIR`, `GIT_WORK_TREE`, `GIT_EXEC_PATH`, `GIT_EXTERNAL_DIFF`,
    `GIT_SSH`, `GIT_SSH_COMMAND` and `GIT_PROXY_COMMAND`.
* **Timeout:** each git call is limited to 10 seconds. A repo whose remote
  asks for credentials fails at once instead of hanging.

`gitlab_sync_local` and `gitlab_clone_missing` reuse gitlab-cli's own
`pull.sync_many` and `clone.cmd_clone`. They run under `GitLabContext.git_env`,
which also sets `GIT_CONFIG_NOSYSTEM=1` and `GIT_OPTIONAL_LOCKS=0` and drops
the variables listed above. The `-c` argv overrides are added only by the
read-tool wrapper. Those two tools run only after you confirm, and only
inside `master_dir`.

---

## Registering the server

The server is part of the gitlab-cli checkout and imports its modules
(`api`, `instances`, `pull`, `clone`, `git`, `paths`). Two conditions must
hold:

* The interpreter must be the gitlab-cli venv, with the `mcp` SDK installed
  from `requirements.txt` (a 1.x release, which provides FastMCP at
  `mcp.server.fastmcp`).
* The command must give an **absolute path**. `install.sh` creates a shell
  *alias* named `gitlab-cli`. An MCP client starts the server without a
  shell, so it never sees that alias, and `command: gitlab-cli` fails.

The examples below use `/path/to/gitlab-cli` as the checkout.

### `gitlab-cli mcp`

```bash
/path/to/gitlab-cli/venv/bin/python /path/to/gitlab-cli/gitlab_cli.py mcp
```

This is the subcommand `gitlab-cli mcp`, written out so that it works without
the alias. It starts the stdio server before anything is printed, so no
banner byte reaches the protocol stream. Running the script by path puts the
checkout on `sys.path`, so no `PYTHONPATH` is needed.

`python -m gitlab_mcp` starts the same server. It needs the checkout as the
working directory, or `PYTHONPATH=/path/to/gitlab-cli`.

### Claude Code

```bash
claude mcp add gitlab -- \
  /path/to/gitlab-cli/venv/bin/python /path/to/gitlab-cli/gitlab_cli.py mcp
```

The read-only variant, with human confirmation in case writes are ever
re-enabled:

```bash
claude mcp add gitlab-ro \
  --env GITLAB_MCP_READ_ONLY=1 \
  --env GITLAB_MCP_CONFIRM_MODE=human \
  -- /path/to/gitlab-cli/venv/bin/python /path/to/gitlab-cli/gitlab_cli.py mcp
```

To check the registration, run `claude mcp list`. Inside a session, `/mcp`
lists the tools.

### Other MCP clients

Any client that launches a stdio server takes a command and its arguments.
For example, a YAML config:

```yaml
mcp_servers:
  gitlab:
    command: /path/to/gitlab-cli/venv/bin/python
    args: ["/path/to/gitlab-cli/gitlab_cli.py", "mcp"]
    env:
      GITLAB_MCP_CONFIRM_MODE: human
```

or the common JSON shape:

```json
{
  "mcpServers": {
    "gitlab": {
      "command": "/path/to/gitlab-cli/venv/bin/python",
      "args": ["/path/to/gitlab-cli/gitlab_cli.py", "mcp"],
      "env": {"GITLAB_MCP_READ_ONLY": "1"}
    }
  }
}
```

For read-only use, set `GITLAB_MCP_READ_ONLY` to `"1"` under `env`.

---

## Credentials

The server reads credentials from the same files as the CLI:

* `~/.gitlab-cli/instances.json` holds the instances, for example one named
  `example` pointing at `https://gitlab.example.com`.
* `~/.gitlab-cli/env` holds the `GITLAB_TOKEN_<NAME>` values.

**Do not put a token in the MCP configuration.** `env` entries in an MCP
client's config, Claude Code's included, are stored in clear text. A token
placed there would also show up in process listings.

TLS verification is on for every request, and every request has a 20-second
timeout. Request paths must be relative to the instance's API. An absolute
URL is refused, so the token cannot be sent to another host.

---

## Provenance

This document was checked against the code by importing the code and reading
it. It was not written from memory.

* The MCP tool names, the split between read and write tools, and every
  parameter and default come from `gitlab_mcp.server.mcp.list_tools()`. That
  list was sampled on 2026-10-11 (UTC-06:00):
  * **05:50:59**: 27 tools, no `allow_quick_actions`;
  * **06:02:26**: 27 tools, `allow_quick_actions` present and
    `GITLAB_MCP_CONFIRM_MODE` present;
  * **the final re-check**, recorded in the line below.
* The environment variables come from a grep for `GITLAB_MCP_*` across
  `gitlab_mcp/*.py`.
* The behaviour descriptions come from reading `server.py`, `confirm.py`,
  `context.py`, `reporting.py` and `actions.py` at those times.

Final re-check: the list was sampled again at **2026-10-11 06:08:41** and
**06:10:33** (UTC-06:00). Both samples matched the tables above exactly:

* 27 tools, of which 10 are write tools;
* the same parameters and the same CLI tree;
* the environment variables `GITLAB_MCP_READ_ONLY`,
  `GITLAB_MCP_CONFIRM_MODE` and `GITLAB_MCP_DEBUG`.

Later change (2026-10-11): `server._read_only()` now delegates to
`context.read_only()`, so the early gate fails closed as described under
[Read-only mode](#read-only-mode-gitlab_mcp_read_only).

Re-checked for 1.2.0 (2026-10-26): `gitlab-cli mcp` exists in
`gitlab_cli.py`, the HTTP gate covers `POST`, `PUT` and `DELETE`
(`GitLabContext.post`, `.put` and `.delete` all call `_ensure_writable`), and
the registration commands above were exercised over stdio. Re-run the
introspection after any later change to `server.py`.
