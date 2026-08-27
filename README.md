# GitLab CLI

A command-line tool for working with several GitLab instances at once:
clone whole group trees, sync hundreds of repositories intelligently,
review team activity, and audit your remotes.

It is built for every access level, from Guest to Admin. Anything your
token cannot do is either hidden or greyed out with the reason shown.
It never crashes on a permission it does not have.

---

## Install

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

---

## First run

The wizard asks for:

1. **Name** — anything you like (`Work`, `Personal`, `Internal`, ...)
2. **URL** — for example `https://gitlab.com` or `http://gitlab.internal`
3. **Token** — a Personal Access Token (input is hidden as you type)
4. **Directory** — where repositories get cloned

It then tests the token and tells you exactly what access you have.

### Getting a token

GitLab → **Settings → Access Tokens** → create a new token.

Scopes:

| Scope              | Needed for |
|--------------------|-----------|
| `read_api`         | listing groups and projects (minimum) |
| `api`              | everything, including members and management |
| `read_repository`  | clone and fetch |
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

The wizard asks for this. Leave it empty and groups are cloned directly
into the root directory.

---

## Commands

```bash
gitlab-cli                     # interactive mode
gitlab-cli setup               # add a GitLab instance
gitlab-cli doctor              # health check
gitlab-cli instances           # list instances
gitlab-cli clone   <group>     # clone an entire group tree
gitlab-cli sync    <group>     # smart sync
gitlab-cli log     <group> 3d  # commits from the last 3 days
gitlab-cli remotes [--fix]     # audit remotes
```

---

## Smart sync

Rather than running a plain `git pull`, for each repository it:

1. runs `fetch --all --prune --tags`
2. measures how far ahead or behind **every** remote is
3. picks the furthest remote that can fast-forward
4. runs `merge --ff-only`, so it never creates an unwanted merge commit

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
478 repositories take around 55 seconds instead of about 5 minutes.

---

## Remote audit

```bash
gitlab-cli remotes
```

For every repository it reports:

- how many remotes it has and which hosts they point to
- which ones store a plain-text token in the URL (**a security risk**)
- repositories with no remote at all

`--fix` strips tokens out of those URLs and hands them to
`git credential store`. Pulling still works, but the token is no longer
sitting in `.git/config`.

> Why this matters: a token embedded in a remote URL is stored in
> `.git/config` in plain text, and any script running `git remote -v`
> prints it.

---

## Doctor

```bash
gitlab-cli doctor
```

Checks configuration validity, file permissions (600), each instance's
token, network reachability, whether git is installed, and the
credential helper.

Every problem it finds comes with the fix.

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

Without admin rights:

- the Members section is built from your own groups instead of `/users`
- admin-only options are greyed out with the reason
- no 403 ever surfaces as a crash

---

## Layout

```
gitlab_cli.py    entry point and adaptive menu
setup.py         first-run wizard and doctor
instances.py     multi-instance management
credentials.py   token loading: env var or 600-mode env file
caps.py          access-level detection, cached 12h
api.py           GitLab API with non-admin fallbacks
clone.py         recursive group cloning
pull.py          parallel sync
git.py           per-repository sync logic
remotes.py       remote audit and scrub
members.py       users, activity, access map
log.py           recent commits
errors.py        error log with suggested fixes
printer.py       colour and formatting
```

---

## Your private files

These are in `.gitignore` and **never get committed**:

- `~/.gitlab-cli/env` — tokens (mode 600)
- `~/.gitlab-cli/instances.json` — instance configuration
- `MasterGroups/*` — your cloned repositories

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
