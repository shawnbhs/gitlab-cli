# `gitlab-cli report`: the manual CLI

This CLI gives you, by hand from bash, everything the
[MCP server](MCP.md) can do. It uses the same modules, the same previews and
the same refusals. The MCP server and this CLI are two front doors onto
`gitlab_mcp.reporting` and `gitlab_mcp.actions`.

Run it as `gitlab-cli report <command>`:

```bash
gitlab-cli report <command> [options]
gitlab-cli report --help          # the full command list
```

`gitlab-cli` is the alias `install.sh` creates. Without it, or from a script,
write the same thing out by absolute path:

```bash
/path/to/gitlab-cli/venv/bin/python /path/to/gitlab-cli/gitlab_cli.py report <command> [options]
```

`python -m gitlab_mcp.cli <command>`, run from the checkout, is the same
program. Its usage line calls it `gitlab-report`.

The command and flag lists below come from walking the parser that
`gitlab_mcp.cli.build_parser()` returns. That walk was done on 2026-10-11 at
05:50:59 and again at 06:02:26 (UTC-06:00).

## The rule that matters

**Nothing that changes anything happens without a preview first.**

Every write command runs `actions.preview_<action>`, which issues only `GET`
requests. It prints what it found and then asks:

```
Proceed? [y/N]
```

The default answer is **No**. An empty answer, `n`, or anything other than
`y` or `yes` cancels the command. A cancelled command exits 0, because
declining is a decision, not a failure.

* `--yes` skips the **question**, never the **disclosure**. The preview is
  always printed.
* **`--yes` does not apply to `mr-merge` into a protected branch.** That
  case always asks, and you must type the target branch name exactly:

  ```
  !! DANGER: 'main' IS A PROTECTED/PRODUCTION TARGET — THIS CHANGE AFFECTS EVERYONE !!
  This merges into 'main'. --yes does not apply here.
  Type the target branch name ('main') to confirm:
  ```

  Protected means `main`, `master`, `production` or `prod`, or any name
  starting with `release`, compared case-insensitively.
* A preview that returns `ok: false` is **never offered for confirmation**.
  Its reasons are printed to stderr and the command exits 1. `--yes` does
  not override this.
* Some values are pinned from the preview. They are taken from what you
  read and passed to the action unchanged:
  * `mr-merge` and `mr-approve` pass the previewed head `sha`. If someone
    pushes to the source branch between the preview and your answer, GitLab
    rejects the merge or the approval instead of acting on a commit you
    never read.
  * `mr-create` passes the `description_sha256` of the description shown in
    the preview.

The CLI has no confirmation token. Your `y` at the prompt is the
confirmation. `GITLAB_MCP_CONFIRM_MODE` affects only the MCP server.
`GITLAB_MCP_READ_ONLY` is enforced in the shared HTTP layer, which serves
both front ends. With it set, any `POST`, `PUT` or `DELETE` the CLI attempts
fails with a read-only error.

## Common flags

These flags are accepted both before and after the command name.

| Flag | Meaning |
|---|---|
| `--instance NAME` | Which configured GitLab instance to use. Default: the active one. |
| `--since VALUE` | Start of the window: `YYYY-MM-DD`, `7d`, `2w`, `1m` or `today`. |
| `--until VALUE` | End of the window. |
| `--format table\|md\|json` | Output format. Default: `table`. |
| `--json` | Shorthand for `--format json`. |
| `--out FILE` | Write the output to `FILE` instead of stdout. |
| `--yes` | Skip `Proceed?` for writes. A protected-branch merge still asks. |

`table` output is width-aware: CJK and fullwidth characters count as two
cells, so columns line up. `md` output escapes `|`, so a commit message
cannot break out of its table cell.

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Success, or the confirmation was declined. |
| `1` | An error, or a refused preview. |
| `2` | Bad usage (an argparse error). |
| `130` | Interrupted with Ctrl-C. |

Set `GITLAB_MCP_DEBUG` to any non-empty value to print a traceback after the
error line.

## Read commands

| Command | What it reports |
|---|---|
| `instances` | The configured GitLab instances. Tokens are never shown. |
| `groups [--search Q] [--limit 100]` | Groups. |
| `projects [--group G] [--search Q] [--limit 200]` | Projects, including those in subgroups. |
| `overview PROJECT [--since 30d]` | Default branch, activity, commits, open MRs, contributors and languages. |
| `commits PROJECT [--since 7d] [--until D] [--author A] [--branch B] [--no-stats] [--limit 200]` | Commits, with additions and deletions. `--no-stats` is much faster. |
| `commit PROJECT SHA [--max-diff-chars 60000]` | One commit: its message, stats and per-file diff. |
| `compare PROJECT BASE HEAD [--max-diff-chars 60000]` | The diff between two refs. |
| `contributors PROJECT [--since 30d] [--branch B]` | Per author: commits, lines, and first and last dates. |
| `person [USERNAME ...] [--users a,b] [--projects x,y] [--since 7d] [--max-projects 50]` | What people did across projects. |
| `progress PROJECT [--since 30d]` | Commits per week and per author, MRs, pipeline success rate and tags. |
| `mrs [--project P] [--group G] [--state opened\|closed\|merged\|locked\|all] [--author A] [--target-branch B] [--limit 100]` | Merge requests. |
| `mr PROJECT IID [--no-diff] [--max-diff-chars 80000]` | One MR, with discussions, approvals, pipeline and changes. |
| `pipelines PROJECT [--ref R] [--limit 20]` | Recent pipelines. |
| `branches PROJECT [--search Q]` | Branches. |
| `users QUERY [--limit 20]` | A user search. |
| `file PROJECT PATH [--ref R] [--max-chars 100000]` | One file from the repository. |
| `local-status [--group G]` | Local clones: path, branch, dirty state and ahead/behind. Nothing is fetched. `G` must lie inside the managed directory, and the git hardening described in [MCP.md](MCP.md#local-clones-confinement-and-git-hardening) applies. |

`PROJECT` is a `group/subgroup/project` path or a numeric project id.

`person` accepts names either way. Both lists are merged and de-duplicated,
keeping their order:

```bash
gitlab-cli report person alice bob
gitlab-cli report person --users alice,bob
```

## Write commands

Each write command previews, asks, and then acts.

| Command | What it changes |
|---|---|
| `sync [--group G] [--project P]` | Fetches the local clones and pulls them **fast-forward only**. A repo that is dirty, ahead, diverged or detached is reported and left alone. |
| `clone-missing GROUP` | Clones the group's projects into the managed directory. Anything already cloned is skipped. |
| `mr-comment PROJECT IID [BODY] [--body-file FILE] [--allow-quick-actions]` | Posts a plain comment on a merge request. |
| `mr-review-comment PROJECT IID [BODY] --path FILE [--new-line N] [--old-line N] [--body-file FILE] [--allow-quick-actions]` | Posts a positioned diff note, which starts a review discussion. |
| `mr-approve PROJECT IID` | Records your approval, pinned to the previewed sha. |
| `mr-unapprove PROJECT IID` | Withdraws your approval. |
| `mr-create PROJECT SOURCE_BRANCH TARGET_BRANCH TITLE [--description T] [--description-file FILE] [--draft] [--allow-quick-actions]` | Opens a merge request. The preview shows the description that will be sent. |
| `mr-merge PROJECT IID [--squash\|--no-squash] [--remove-source-branch]` | Merges the MR, pinned to the previewed sha. |
| `issue-comment PROJECT IID [BODY] [--body-file FILE] [--allow-quick-actions]` | Posts a comment on an issue. |
| `create-branch PROJECT BRANCH REF` | Creates a branch from an existing ref. |

`--squash` and `--no-squash` form a tri-state. If you pass neither, the
project's own default applies.

**Quick actions.** Some lines in a body or description start with a GitLab
slash command, such as `/merge`, `/approve` or `/assign_reviewer`. By
default those lines are neutralised: they are wrapped in backticks and
posted as text, and the preview shows a `DANGER` warning that lists them.
Pass `--allow-quick-actions` only if you really want GitLab to **execute**
them. The preview then warns that it will.

A comment body can come from a file instead of the shell. This avoids
quoting a long review into one argument:

```bash
gitlab-cli report mr-comment example-group/project-a 7 --body-file review.md
```

## Worked example

This example reviews a merge request, comments on one line, approves it and
merges it. It runs against the configured instance, here
`gitlab.example.com`.

```bash
# 1. Read the MR. --format md makes the output easy to paste into a ticket.
gitlab-cli report mr example-group/project-a 7 --format md --out mr-7.md

# 2. Leave a note on one line of the diff.
gitlab-cli report mr-review-comment example-group/project-a 7 \
    "This drops the error; it needs to propagate." \
    --path src/app.py --new-line 42
# ── preview: mr_review_comment ──
# Start a review discussion on example-group/project-a!7 at src/app.py:new line 42 (44 characters).
#   project: example-group/project-a
#   iid: 7
#   endpoint: POST /projects/12/merge_requests/7/discussions
# Proceed? [y/N] y

# 3. Approve it. The approval is pinned to the sha shown in the preview.
gitlab-cli report mr-approve example-group/project-a 7 --yes

# 4. Merge it. The target is 'main', so --yes does NOT skip the question.
gitlab-cli report mr-merge example-group/project-a 7 --remove-source-branch --yes
# ── preview: mr_merge ──
# Merge example-group/project-a!7 — 'Add retries' ('feature' → 'main') at 9f1c2d3a4b5c.
# !! DANGER: 'main' IS A PROTECTED/PRODUCTION TARGET — THIS CHANGE AFFECTS EVERYONE !!
#   sha: 9f1c2d3a4b5cde...
#   merge_status: mergeable
# This merges into 'main'. --yes does not apply here.
# Type the target branch name ('main') to confirm: main
```

A refusal looks like the following. The command exits 1 without asking
anything:

```bash
gitlab-cli report mr-merge example-group/project-a 8 --yes
# ── preview: mr_merge ──
# refused — this will not run:
#   - the merge request has conflicts with 'main'
#   - blocking discussions are not resolved
echo $?   # 1
```

A weekly report, written to a file in machine-readable form:

```bash
gitlab-cli report person --users alice,bob --since 1w --json --out week.json
gitlab-cli report progress example-group/project-a --since 30d --format md --out progress.md
```

## Notes

* **Output is terminal-safe.** MR titles, commit messages, user names, diffs
  and error messages come from GitLab and may contain escape sequences. Before
  anything is printed, the CLI removes C0 control characters (newline and tab
  are kept in text bodies; table cells fold them to spaces), DEL, the C1 block
  (including the 8-bit CSI `U+009B`) and the bidi embedding, override and
  isolate characters. `--json` output is unchanged as data: those characters
  are escaped as `\uXXXX`, so `json.loads` returns the original strings.

* **Tokens are never printed.** Previews, results and error messages all
  pass through the same scrubber the MCP server uses, `server.scrub_text`.
  That scrubber removes the instance's literal token as well as any
  `glpat-…`-style token, `PRIVATE-TOKEN` or bearer value, and URL userinfo.
* **`--out` is strict.** It refuses a path that is a directory, and it
  refuses to create missing parent directories, so a typo cannot quietly
  file the report somewhere nobody looks.
* **`sync` and `clone-missing` do not reimplement git.** They reuse
  gitlab-cli's own `pull` and `clone` code. Their stdout is captured and
  returned as `output`. Both act only inside the instance's `master_dir`.
