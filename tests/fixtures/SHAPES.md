# GitLab response shapes (synthetic fixtures)

The **shapes** in this directory - key sets, value types, nesting, list vs
dict, which keys are absent on which version - were measured read-only (GET
only) against two GitLab CE instances by `scripts/capture_shapes.py`. Every
**value** has since been replaced with synthetic data, so nothing here
identifies a real instance, group, project, person or commit:

* hosts -> `https://gitlab.example.com` (ssh: `git@gitlab.example.com:`)
* groups / projects -> `example-group/sub/demo-app`, `example-group/sub/demo-lib`,
  `other-group/demo-tools`, `example-group/web/demo-site`,
  `example-group/ops/demo-infra`
* users -> `alice`, `bob`, `carol`, ... ; emails -> `<name>@example.com`;
  avatars dropped
* titles, messages, notes, MR titles in events -> `Sample text N` /
  `Sample merge request title N`; branches -> `branch-N` (`main`/`develop` kept)
* commit SHAs -> deterministic fake 40-hex values; the same fake SHA is used
  everywhere one commit is referenced (`id`, `short_id`, `parent_ids`,
  `web_url`, MR `sha`/`diff_refs`, pipeline `sha`, event `push_data`)
* ids -> small integers, consistent across files (project `1` / `11` is the
  captured project on inst1 / inst2; the token user is id `1` on both;
  MR `!2` / `!5`)
* diff bodies -> fixed sample unified-diff lines
* states, booleans, counts, dates and timezone offsets are kept as shapes

Fixture files are named `<instance>_<endpoint>.json` where `<instance>` is a
neutral label (`inst1`, `inst2`), so the two versions can be diffed.
`tests/test_mcp_fixtures.py` enforces the hygiene rules (emails only at
`example.com`, URL hosts only under `example.com`, no Arabic-script text, and
no word from the operator's `GITLAB_FIXTURE_FORBIDDEN` list).

## Endpoints

| fixture | endpoint template | status (inst1 / inst2) |
|---|---|---|
| `{inst1,inst2}_user.json` | `/user` | 200 / 200 |
| `{inst1,inst2}_users_by_username.json` | `/users?username=:username` | 200 / 200 |
| `{inst1,inst2}_user_events.json` | `/users/:id/events` | 200 / 200 |
| `{inst1,inst2}_projects.json` | `/projects` | 200 / 200 |
| `{inst1,inst2}_commits.json` | `/projects/:id/repository/commits` | 200 / 200 |
| `{inst1,inst2}_commit_diff.json` | `/projects/:id/repository/commits/:sha/diff` | 200 / 200 |
| `{inst1,inst2}_merge_requests.json` | `/projects/:id/merge_requests` | 200 / 200 |
| `{inst1,inst2}_mr_changes.json` | `/projects/:id/merge_requests/:iid/changes` | 200 / 200 |
| `{inst1,inst2}_mr_diffs.json` | `/projects/:id/merge_requests/:iid/diffs` | 200 / 200 |
| `{inst1,inst2}_mr_discussions.json` | `/projects/:id/merge_requests/:iid/discussions` | 200 / 200 |
| `{inst1,inst2}_mr_approvals.json` | `/projects/:id/merge_requests/:iid/approvals` | 200 / 200 |
| `{inst1,inst2}_pipelines.json` | `/projects/:id/pipelines` | 200 / 200 |
| `{inst1,inst2}_version.json` | `/version` | 200 / 200 |
| `{inst1,inst2}_metadata.json` | `/metadata` | 200 / 200 |

## Version differences and SURPRISES

Two instances were captured, both GitLab CE but several generations apart:

| label | version | revision available |
|---|---|---|
| `inst1` | 17.5.5 | yes |
| `inst2` | 19.1.1 | yes |

Everything below is **measured on these two instances**, not read from docs.

### CONTRACT CORRECTIONS (measured, contradicts prior assumptions)

* `GET /api/v4/version` returned **200 on BOTH instances** with the ordinary
  project-scoped token. The claim that the older instance 401s on `/version`
  did NOT reproduce. Still prefer `/user` for liveness, because it works on
  both AND identifies the token - but do not code a 401 special case.
* `GET /api/v4/metadata` exists on **BOTH** instances (17.5 and 19.1) and
  returns the SAME keys as `/version`: `version, revision, kas, enterprise`.
  It is not a 19.x-only endpoint here.
* **Both** MR diff endpoints work on **both** instances:
  `/merge_requests/:iid/changes` -> 200, `/merge_requests/:iid/diffs` -> 200.
  Neither is 404 on 17.5. See the next section for which one to actually use.

### The two MR diff endpoints return DIFFERENT SHAPES

This is the trap, and it is a shape difference, not an availability one.

* `/merge_requests/:iid/changes` -> a **dict**: the entire MR object with a
  `changes` list bolted on. It is **unpaginated** and returned **27 files**
  on inst1 in one response. Deprecated upstream; it is the expensive call.
* `/merge_requests/:iid/diffs` -> a **bare list** of diff objects, properly
  paginated (`per_page` honoured: 3 requested, 3 returned). No MR object.

Recommendation: use `/diffs` for the diff itself and fetch the MR object
separately from `/merge_requests/:iid`. Use `/changes` only when a single
round trip matters more than payload size, and never without a cap - it has
no page limit and a large MR will return every file at once.

### Diff objects gained two keys between 17.5 and 19.1

Common to both: `old_path, new_path, a_mode, b_mode, new_file,
renamed_file, deleted_file, generated_file, diff`.

inst2 (19.1) adds **`collapsed`** and **`too_large`**, both booleans. These
matter: when `too_large` is true the `diff` string is EMPTY, and when
`collapsed` is true it may be truncated. Code that assumes a non-empty
`diff` whenever the request succeeded silently reports 'no changes' on large
files against 19.x. Read them with `.get(..., False)` so 17.5 still works.

`diff` itself is a **raw unified-diff string** on both versions, not a
structure. In these fixtures every body is replaced with fixed sample lines.

### Commit stats are opt-in; the identity keys come in two pairs

* `/repository/commits` omits `stats` unless `with_stats=true` is passed.
  With it each commit carries `stats: {additions, deletions, total}`.
* Keys identical on both instances: `id, short_id, title, message,
  author_name, author_email, authored_date, committer_name,
  committer_email, committed_date, created_at, parent_ids, web_url,
  trailers, extended_trailers, stats`.
* There are TWO identity pairs (`author_*` and `committer_*`) and TWO dates
  (`authored_date`, `committed_date`). Attribute by `author_email` and date
  by `committed_date` to match what the UI shows for a rebased branch.
* `parent_ids` is a list; >= 2 entries means a merge commit. Counting commits
  without filtering merges double-counts merged work.
* `/repository/commits/:sha/diff` returns a **bare list** of the same diff
  objects as `/diffs` - no commit object wrapped around it.

### Commit dates carry mixed timezone offsets

* `authored_date` / `committed_date` keep the committer's local offset, so
  one project's history mixes `+05:30`, `+01:00` and `+00:00`. Compare dates
  as aware datetimes, never as strings.
* `inst2_commits.json` deliberately keeps one malformed timestamp
  (`...T13:48:10.0.0.1+01:00`). Date parsing must degrade to "unknown" for
  that row, not crash the whole report.

### Events: `push_data` is optional

Event keys on both: `id, project_id, action_name, target_id, target_iid,
target_type, target_title, author, author_id, author_username, created_at,
imported, imported_from`.

* **`push_data` is absent on every non-push event** and was missing or empty
  on push events in the first capture runs. The fixtures keep it on push
  events (with a `commit_count`), and the test suite strips it to exercise the
  empty case. Code must not rely on it: treat `push_data` as optional and
  derive push activity from `action_name` instead.
* `action_name` is a human string (`pushed to`, `opened`, `commented on`) and
  is localisable - it is untrusted free text, not an enum.
* `target_type` is `null` for push events on 17.5 but `Project` on 19.1
  (with `target_id`/`target_iid` set to the project id), so a
  `target_type`-keyed dispatch drops or misfiles most of the feed.
* `author_username` carries the handle at the TOP level of the event. (This
  key is easy to miss when sanitizing - it is NOT inside the nested `author`
  object.)

### Approvals: CE returns a degenerate payload, and the keys are not the
### documented ones

Measured on both instances, identically:

```
{user_has_approved: false, user_can_approve: true, approved: false,
 approved_by: []}
```

* **`approvals_required` and `approvals_left` are NOT present.** Approval
  rules are a Premium feature; on CE the endpoint answers 200 with only the
  four keys above. Any code reading `approvals_left == 0` as 'approved' gets
  a `KeyError` here, and reading it with a `0` default silently reports every
  MR as approved.
* Use `approved` (bool) and `approved_by` (list) - both present on 17.5 and
  19.1 - and treat the absence of `approvals_required` as 'no rules
  configured', never as 'satisfied'.

### Discussions are threads, not comments

* `/merge_requests/:iid/discussions` returns discussion threads, each with
  `id`, `individual_note` (bool) and a `notes` list. A plain comment is a
  discussion with `individual_note: true` and exactly one note.
* Notes with `system: true` are label/assignment/pipeline events. A
  human-comment count that does not filter them inflates badly.
* A positioned (code) note carries `position` with `{base_sha, start_sha,
  head_sha, old_path, new_path, position_type, old_line, new_line}`. Those
  three SHAs come from the MR's `diff_refs` and are mandatory when POSTing a
  diff note - they cannot be invented.

### Pipelines

* List keys on both: `id, iid, project_id, sha, ref, status, source, name,
  web_url, created_at, updated_at`. `iid` and `name` are present on 17.5 too.
* Duration, coverage, and the triggering user live ONLY on the
  single-pipeline endpoint - one extra request each.
* `status` is one of `created, waiting_for_resource, preparing, pending,
  running, success, failed, canceled, skipped, manual, scheduled`. Treating
  anything not `success` as failure mislabels `skipped` and `manual`.

### Projects list grew 10 keys between versions

* inst1 (17.5) project objects carry **100** keys; inst2 (19.1) carry **110**.
  Never validate a project object against a fixed key set - use `.get()` and
  pick the keys you need.
* `membership=true` is required or the list is every public project on the
  instance. `order_by=last_activity_at` needs an explicit `sort=desc` to mean
  'newest first'.
* `permissions.project_access` / `group_access` is `null` (not absent) when
  the user's access comes via the other path - check both before deciding an
  access level.
* Pagination headers (`X-Next-Page`, `X-Total`) are the authoritative paging
  signal, and `X-Total` is OMITTED on large collections, so a missing total
  is not zero.

### `/users?username=<handle>` returns a LIST

Even for an exact unique handle the response is a one-element list, not an
object. `/user` (no `s`) is the object form for the token's own identity.

## Key fields and types

### inst1_commit_diff

```
<root>: list[3]
[].diff: str
[].new_path: str
[].old_path: str
[].a_mode: str
[].b_mode: str
[].new_file: bool
[].renamed_file: bool
[].deleted_file: bool
[].generated_file: NoneType
```

### inst1_commits

```
<root>: list[3]
[].id: str
[].short_id: str
[].created_at: str
[].parent_ids: list[1]
[].title: str
[].message: str
[].author_name: str
[].author_email: str
[].authored_date: str
[].committer_name: str
[].committer_email: str
[].committed_date: str
[].trailers: dict
[].extended_trailers: dict
[].web_url: str
[].stats: dict
[].stats.additions: int
[].stats.deletions: int
[].stats.total: int
```

### inst1_merge_requests

```
<root>: list[2]
[].id: int
[].iid: int
[].project_id: int
[].title: str
[].description: str
[].state: str
[].created_at: str
[].updated_at: str
[].merged_by: dict
[].merged_by.id: int
[].merged_by.username: str
[].merged_by.name: str
[].merged_by.state: str
[].merged_by.locked: bool
[].merged_by.web_url: str
[].merge_user: dict
[].merge_user.id: int
[].merge_user.username: str
[].merge_user.name: str
[].merge_user.state: str
[].merge_user.locked: bool
[].merge_user.web_url: str
[].merged_at: str
[].closed_by: NoneType
[].closed_at: NoneType
[].target_branch: str
[].source_branch: str
[].user_notes_count: int
[].upvotes: int
[].downvotes: int
[].author: dict
[].author.id: int
[].author.username: str
[].author.name: str
[].author.state: str
[].author.locked: bool
[].author.web_url: str
[].assignees: list[1]
[].assignees[].id: int
[].assignees[].username: str
[].assignees[].name: str
[].assignees[].state: str
[].assignees[].locked: bool
[].assignees[].web_url: str
[].assignee: dict
[].assignee.id: int
[].assignee.username: str
[].assignee.name: str
[].assignee.state: str
[].assignee.locked: bool
[].assignee.web_url: str
[].reviewers: list[0]
[].source_project_id: int
[].target_project_id: int
[].labels: list[0]
[].draft: bool
[].imported: bool
[].imported_from: str
[].work_in_progress: bool
[].milestone: NoneType
[].merge_when_pipeline_succeeds: bool
[].merge_status: str
[].detailed_merge_status: str
[].merge_after: NoneType
[].sha: str
[].merge_commit_sha: str
[].squash_commit_sha: NoneType
[].discussion_locked: NoneType
[].should_remove_source_branch: NoneType
[].force_remove_source_branch: NoneType
[].prepared_at: str
[].reference: str
[].references: dict
[].references.short: str
[].references.relative: str
[].references.full: str
[].web_url: str
[].time_stats: dict
[].time_stats.time_estimate: int
[].time_stats.total_time_spent: int
[].time_stats.human_time_estimate: NoneType
[].time_stats.human_total_time_spent: NoneType
[].squash: bool
[].squash_on_merge: bool
[].task_completion_status: dict
[].task_completion_status.count: int
[].task_completion_status.completed_count: int
[].has_conflicts: bool
[].blocking_discussions_resolved: bool
```

### inst1_metadata

```
version: str
revision: str
kas: dict
kas.enabled: bool
kas.externalUrl: str
kas.version: str
enterprise: bool
```

### inst1_mr_approvals

```
user_has_approved: bool
user_can_approve: bool
approved: bool
approved_by: list[0]
```

### inst1_mr_changes

```
id: int
iid: int
project_id: int
title: str
description: str
state: str
created_at: str
updated_at: str
merged_by: dict
merged_by.id: int
merged_by.username: str
merged_by.name: str
merged_by.state: str
merged_by.locked: bool
merged_by.web_url: str
merge_user: dict
merge_user.id: int
merge_user.username: str
merge_user.name: str
merge_user.state: str
merge_user.locked: bool
merge_user.web_url: str
merged_at: str
closed_by: NoneType
closed_at: NoneType
target_branch: str
source_branch: str
user_notes_count: int
upvotes: int
downvotes: int
author: dict
author.id: int
author.username: str
author.name: str
author.state: str
author.locked: bool
author.web_url: str
assignees: list[1]
assignees[].id: int
assignees[].username: str
assignees[].name: str
assignees[].state: str
assignees[].locked: bool
assignees[].web_url: str
assignee: dict
assignee.id: int
assignee.username: str
assignee.name: str
assignee.state: str
assignee.locked: bool
assignee.web_url: str
reviewers: list[0]
source_project_id: int
target_project_id: int
labels: list[0]
draft: bool
imported: bool
imported_from: str
work_in_progress: bool
milestone: NoneType
merge_when_pipeline_succeeds: bool
merge_status: str
detailed_merge_status: str
merge_after: NoneType
sha: str
merge_commit_sha: str
squash_commit_sha: NoneType
discussion_locked: NoneType
should_remove_source_branch: NoneType
force_remove_source_branch: NoneType
prepared_at: str
reference: str
references: dict
references.short: str
references.relative: str
references.full: str
web_url: str
time_stats: dict
time_stats.time_estimate: int
time_stats.total_time_spent: int
time_stats.human_time_estimate: NoneType
time_stats.human_total_time_spent: NoneType
squash: bool
squash_on_merge: bool
task_completion_status: dict
task_completion_status.count: int
task_completion_status.completed_count: int
has_conflicts: bool
blocking_discussions_resolved: bool
subscribed: bool
changes_count: str
latest_build_started_at: NoneType
latest_build_finished_at: NoneType
first_deployed_to_production_at: str
pipeline: NoneType
head_pipeline: dict
head_pipeline.id: int
head_pipeline.iid: int
head_pipeline.project_id: int
head_pipeline.sha: str
head_pipeline.ref: str
head_pipeline.status: str
head_pipeline.source: str
head_pipeline.created_at: str
head_pipeline.updated_at: str
head_pipeline.web_url: str
head_pipeline.before_sha: str
head_pipeline.tag: bool
head_pipeline.yaml_errors: NoneType
head_pipeline.user: dict
```

### inst1_mr_diffs

```
<root>: list[3]
[].diff: str
[].new_path: str
[].old_path: str
[].a_mode: str
[].b_mode: str
[].new_file: bool
[].renamed_file: bool
[].deleted_file: bool
[].generated_file: bool
```

### inst1_mr_discussions

```
<root>: list[2]
[].id: str
[].individual_note: bool
[].notes: list[1]
[].notes[].id: int
[].notes[].type: NoneType
[].notes[].body: str
[].notes[].attachment: NoneType
[].notes[].author: dict
[].notes[].author.id: int
[].notes[].author.username: str
[].notes[].author.name: str
[].notes[].author.state: str
[].notes[].author.locked: bool
[].notes[].author.web_url: str
[].notes[].created_at: str
[].notes[].updated_at: str
[].notes[].system: bool
[].notes[].noteable_id: int
[].notes[].noteable_type: str
[].notes[].project_id: int
[].notes[].resolvable: bool
[].notes[].confidential: bool
[].notes[].internal: bool
[].notes[].imported: bool
[].notes[].imported_from: str
[].notes[].noteable_iid: int
[].notes[].commands_changes: dict
```

### inst1_pipelines

```
<root>: list[3]
[].id: int
[].iid: int
[].project_id: int
[].sha: str
[].ref: str
[].status: str
[].source: str
[].created_at: str
[].updated_at: str
[].web_url: str
[].name: NoneType
```

### inst1_projects

```
<root>: list[3]
[].id: int
[].description: NoneType
[].name: str
[].name_with_namespace: str
[].path: str
[].path_with_namespace: str
[].created_at: str
[].default_branch: str
[].tag_list: list[0]
[].topics: list[0]
[].ssh_url_to_repo: str
[].http_url_to_repo: str
[].web_url: str
[].readme_url: NoneType
[].forks_count: int
[].star_count: int
[].last_activity_at: str
[].namespace: dict
[].namespace.id: int
[].namespace.name: str
[].namespace.path: str
[].namespace.kind: str
[].namespace.full_path: str
[].namespace.parent_id: int
[].namespace.web_url: str
[].repository_storage: str
[]._links: dict
[]._links.self: str
[]._links.issues: str
[]._links.merge_requests: str
[]._links.repo_branches: str
[]._links.labels: str
[]._links.events: str
[]._links.members: str
[]._links.cluster_agents: str
[].packages_enabled: bool
[].empty_repo: bool
[].archived: bool
[].visibility: str
[].resolve_outdated_diff_discussions: bool
[].container_expiration_policy: dict
[].container_expiration_policy.cadence: str
[].container_expiration_policy.enabled: bool
[].container_expiration_policy.keep_n: int
[].container_expiration_policy.older_than: str
[].container_expiration_policy.name_regex: str
[].container_expiration_policy.name_regex_keep: NoneType
[].container_expiration_policy.next_run_at: str
[].repository_object_format: str
[].issues_enabled: bool
[].merge_requests_enabled: bool
[].wiki_enabled: bool
[].jobs_enabled: bool
[].snippets_enabled: bool
[].container_registry_enabled: bool
[].service_desk_enabled: bool
[].service_desk_address: NoneType
[].can_create_merge_request_in: bool
[].issues_access_level: str
[].repository_access_level: str
[].merge_requests_access_level: str
[].forking_access_level: str
[].wiki_access_level: str
[].builds_access_level: str
[].snippets_access_level: str
[].pages_access_level: str
[].analytics_access_level: str
[].container_registry_access_level: str
[].security_and_compliance_access_level: str
[].releases_access_level: str
[].environments_access_level: str
[].infrastructure_access_level: str
[].monitor_access_level: str
[].model_experiments_access_level: str
[].model_registry_access_level: str
[].emails_disabled: bool
[].emails_enabled: bool
[].shared_runners_enabled: bool
[].lfs_enabled: bool
[].creator_id: int
[].import_type: NoneType
[].import_status: str
[].open_issues_count: int
[].description_html: str
[].updated_at: str
[].ci_default_git_depth: int
[].ci_forward_deployment_enabled: bool
[].ci_forward_deployment_rollback_allowed: bool
[].ci_job_token_scope_enabled: bool
[].ci_separated_caches: bool
[].ci_allow_fork_pipelines_to_run_in_parent_project: bool
[].ci_id_token_sub_claim_components: list[3]
[].build_git_strategy: str
[].keep_latest_artifact: bool
[].restrict_user_defined_variables: bool
[].ci_pipeline_variables_minimum_override_role: str
[].runner_token_expiration_interval: NoneType
[].group_runners_enabled: bool
[].auto_cancel_pending_pipelines: str
[].build_timeout: int
[].auto_devops_enabled: bool
[].auto_devops_deploy_strategy: str
[].ci_push_repository_for_job_token_allowed: bool
[].ci_config_path: NoneType
[].public_jobs: bool
[].shared_with_groups: list[0]
[].only_allow_merge_if_pipeline_succeeds: bool
[].allow_merge_on_skipped_pipeline: NoneType
[].request_access_enabled: bool
```

### inst1_user

```
id: int
username: str
name: str
state: str
locked: bool
web_url: str
created_at: str
bio: str
location: str
public_email: str
skype: str
linkedin: str
twitter: str
discord: str
website_url: str
organization: str
job_title: str
pronouns: str
bot: bool
work_information: NoneType
local_time: NoneType
last_sign_in_at: str
confirmed_at: str
last_activity_on: str
email: str
theme_id: int
color_scheme_id: int
projects_limit: int
current_sign_in_at: str
identities: list[0]
can_create_group: bool
can_create_project: bool
two_factor_enabled: bool
external: bool
private_profile: bool
commit_email: str
is_admin: bool
note: str
namespace_id: int
created_by: dict
created_by.id: int
created_by.username: str
created_by.name: str
created_by.state: str
created_by.locked: bool
created_by.web_url: str
email_reset_offered_at: NoneType
```

### inst1_user_events

```
<root>: list[5]
[].id: int
[].project_id: int
[].action_name: str
[].target_id: int
[].target_iid: int
[].target_type: str
[].author_id: int
[].target_title: str
[].created_at: str
[].author: dict
[].author.id: int
[].author.username: str
[].author.name: str
[].author.state: str
[].author.locked: bool
[].author.web_url: str
[].imported: bool
[].imported_from: str
[].author_username: str
```

### inst1_users_by_username

```
<root>: list[1]
[].id: int
[].username: str
[].name: str
[].state: str
[].locked: bool
[].web_url: str
[].created_at: str
[].bio: str
[].location: str
[].public_email: str
[].skype: str
[].linkedin: str
[].twitter: str
[].discord: str
[].website_url: str
[].organization: str
[].job_title: str
[].pronouns: str
[].bot: bool
[].work_information: NoneType
[].local_time: NoneType
[].last_sign_in_at: str
[].confirmed_at: str
[].last_activity_on: str
[].email: str
[].theme_id: int
[].color_scheme_id: int
[].projects_limit: int
[].current_sign_in_at: str
[].identities: list[0]
[].can_create_group: bool
[].can_create_project: bool
[].two_factor_enabled: bool
[].external: bool
[].private_profile: bool
[].commit_email: str
[].is_admin: bool
[].note: str
[].namespace_id: int
[].created_by: dict
[].created_by.id: int
[].created_by.username: str
[].created_by.name: str
[].created_by.state: str
[].created_by.locked: bool
[].created_by.web_url: str
[].email_reset_offered_at: NoneType
```

### inst1_version

```
version: str
revision: str
kas: dict
kas.enabled: bool
kas.externalUrl: str
kas.version: str
enterprise: bool
```

### inst2_commit_diff

```
<root>: list[1]
[].diff: str
[].collapsed: bool
[].too_large: bool
[].new_path: str
[].old_path: str
[].a_mode: str
[].b_mode: str
[].new_file: bool
[].renamed_file: bool
[].deleted_file: bool
[].generated_file: NoneType
```

### inst2_commits

```
<root>: list[3]
[].id: str
[].short_id: str
[].created_at: str
[].parent_ids: list[2]
[].title: str
[].message: str
[].author_name: str
[].author_email: str
[].authored_date: str
[].committer_name: str
[].committer_email: str
[].committed_date: str
[].trailers: dict
[].extended_trailers: dict
[].web_url: str
[].stats: dict
[].stats.additions: int
[].stats.deletions: int
[].stats.total: int
```

### inst2_merge_requests

```
<root>: list[2]
[].id: int
[].iid: int
[].project_id: int
[].title: str
[].description: str
[].state: str
[].created_at: str
[].updated_at: str
[].merged_by: dict
[].merged_by.id: int
[].merged_by.username: str
[].merged_by.public_email: str
[].merged_by.name: str
[].merged_by.state: str
[].merged_by.locked: bool
[].merged_by.web_url: str
[].merge_user: dict
[].merge_user.id: int
[].merge_user.username: str
[].merge_user.public_email: str
[].merge_user.name: str
[].merge_user.state: str
[].merge_user.locked: bool
[].merge_user.web_url: str
[].merged_at: str
[].closed_by: NoneType
[].closed_at: NoneType
[].target_branch: str
[].source_branch: str
[].user_notes_count: int
[].upvotes: int
[].downvotes: int
[].author: dict
[].author.id: int
[].author.username: str
[].author.public_email: str
[].author.name: str
[].author.state: str
[].author.locked: bool
[].author.web_url: str
[].assignees: list[1]
[].assignees[].id: int
[].assignees[].username: str
[].assignees[].public_email: str
[].assignees[].name: str
[].assignees[].state: str
[].assignees[].locked: bool
[].assignees[].web_url: str
[].assignee: dict
[].assignee.id: int
[].assignee.username: str
[].assignee.public_email: str
[].assignee.name: str
[].assignee.state: str
[].assignee.locked: bool
[].assignee.web_url: str
[].reviewers: list[0]
[].source_project_id: int
[].target_project_id: int
[].labels: list[0]
[].draft: bool
[].imported: bool
[].imported_from: str
[].work_in_progress: bool
[].milestone: NoneType
[].merge_when_pipeline_succeeds: bool
[].merge_status: str
[].detailed_merge_status: str
[].merge_after: NoneType
[].sha: str
[].merge_commit_sha: str
[].squash_commit_sha: NoneType
[].discussion_locked: NoneType
[].should_remove_source_branch: bool
[].force_remove_source_branch: bool
[].prepared_at: str
[].reference: str
[].references: dict
[].references.short: str
[].references.relative: str
[].references.full: str
[].web_url: str
[].time_stats: dict
[].time_stats.time_estimate: int
[].time_stats.total_time_spent: int
[].time_stats.human_time_estimate: NoneType
[].time_stats.human_total_time_spent: NoneType
[].squash: bool
[].squash_on_merge: bool
[].task_completion_status: dict
[].task_completion_status.count: int
[].task_completion_status.completed_count: int
[].has_conflicts: bool
[].blocking_discussions_resolved: bool
```

### inst2_metadata

```
version: str
revision: str
kas: dict
kas.enabled: bool
kas.externalUrl: str
kas.externalK8sProxyUrl: str
kas.version: str
enterprise: bool
```

### inst2_mr_approvals

```
user_has_approved: bool
user_can_approve: bool
approved: bool
approved_by: list[0]
```

### inst2_mr_changes

```
id: int
iid: int
project_id: int
title: str
description: str
state: str
created_at: str
updated_at: str
merged_by: dict
merged_by.id: int
merged_by.username: str
merged_by.public_email: str
merged_by.name: str
merged_by.state: str
merged_by.locked: bool
merged_by.web_url: str
merge_user: dict
merge_user.id: int
merge_user.username: str
merge_user.public_email: str
merge_user.name: str
merge_user.state: str
merge_user.locked: bool
merge_user.web_url: str
merged_at: str
closed_by: NoneType
closed_at: NoneType
target_branch: str
source_branch: str
user_notes_count: int
upvotes: int
downvotes: int
author: dict
author.id: int
author.username: str
author.public_email: str
author.name: str
author.state: str
author.locked: bool
author.web_url: str
assignees: list[1]
assignees[].id: int
assignees[].username: str
assignees[].public_email: str
assignees[].name: str
assignees[].state: str
assignees[].locked: bool
assignees[].web_url: str
assignee: dict
assignee.id: int
assignee.username: str
assignee.public_email: str
assignee.name: str
assignee.state: str
assignee.locked: bool
assignee.web_url: str
reviewers: list[0]
source_project_id: int
target_project_id: int
labels: list[0]
draft: bool
imported: bool
imported_from: str
work_in_progress: bool
milestone: NoneType
merge_when_pipeline_succeeds: bool
merge_status: str
detailed_merge_status: str
merge_after: NoneType
sha: str
merge_commit_sha: str
squash_commit_sha: NoneType
discussion_locked: NoneType
should_remove_source_branch: bool
force_remove_source_branch: bool
prepared_at: str
reference: str
references: dict
references.short: str
references.relative: str
references.full: str
web_url: str
time_stats: dict
time_stats.time_estimate: int
time_stats.total_time_spent: int
time_stats.human_time_estimate: NoneType
time_stats.human_total_time_spent: NoneType
squash: bool
squash_on_merge: bool
task_completion_status: dict
task_completion_status.count: int
task_completion_status.completed_count: int
has_conflicts: bool
blocking_discussions_resolved: bool
subscribed: bool
changes_count: str
latest_build_started_at: NoneType
latest_build_finished_at: NoneType
first_deployed_to_production_at: NoneType
pipeline: NoneType
head_pipeline: NoneType
diff_refs: dict
diff_refs.base_sha: str
diff_refs.head_sha: str
diff_refs.start_sha: str
merge_error: NoneType
user: dict
user.can_merge: bool
changes: list[6]
changes[].diff: str
```

### inst2_mr_diffs

```
<root>: list[3]
[].diff: str
[].collapsed: bool
[].too_large: bool
[].new_path: str
[].old_path: str
[].a_mode: str
[].b_mode: str
[].new_file: bool
[].renamed_file: bool
[].deleted_file: bool
[].generated_file: bool
```

### inst2_mr_discussions

```
<root>: list[2]
[].id: str
[].individual_note: bool
[].resolvable: bool
[].notes: list[1]
[].notes[].id: int
[].notes[].type: NoneType
[].notes[].body: str
[].notes[].author: dict
[].notes[].author.id: int
[].notes[].author.username: str
[].notes[].author.public_email: str
[].notes[].author.name: str
[].notes[].author.state: str
[].notes[].author.locked: bool
[].notes[].author.web_url: str
[].notes[].created_at: str
[].notes[].updated_at: str
[].notes[].system: bool
[].notes[].noteable_id: int
[].notes[].noteable_type: str
[].notes[].project_id: int
[].notes[].resolvable: bool
[].notes[].confidential: bool
[].notes[].internal: bool
[].notes[].imported: bool
[].notes[].imported_from: str
[].notes[].noteable_iid: int
[].notes[].commands_changes: dict
```

### inst2_pipelines

```
<root>: list[3]
[].id: int
[].iid: int
[].project_id: int
[].sha: str
[].ref: str
[].status: str
[].source: str
[].created_at: str
[].updated_at: str
[].web_url: str
[].name: NoneType
```

### inst2_projects

```
<root>: list[3]
[].id: int
[].description: NoneType
[].name: str
[].name_with_namespace: str
[].path: str
[].path_with_namespace: str
[].created_at: str
[].default_branch: str
[].tag_list: list[0]
[].topics: list[0]
[].ssh_url_to_repo: str
[].http_url_to_repo: str
[].web_url: str
[].readme_url: str
[].forks_count: int
[].star_count: int
[].last_activity_at: str
[].visibility: str
[].namespace: dict
[].namespace.id: int
[].namespace.name: str
[].namespace.path: str
[].namespace.kind: str
[].namespace.full_path: str
[].namespace.parent_id: int
[].namespace.web_url: str
[].repository_storage: str
[]._links: dict
[]._links.self: str
[]._links.issues: str
[]._links.merge_requests: str
[]._links.repo_branches: str
[]._links.labels: str
[]._links.events: str
[]._links.members: str
[]._links.cluster_agents: str
[].marked_for_deletion_at: NoneType
[].marked_for_deletion_on: NoneType
[].packages_enabled: bool
[].empty_repo: bool
[].archived: bool
[].resolve_outdated_diff_discussions: bool
[].container_expiration_policy: dict
[].container_expiration_policy.cadence: str
[].container_expiration_policy.enabled: bool
[].container_expiration_policy.keep_n: int
[].container_expiration_policy.older_than: str
[].container_expiration_policy.name_regex: str
[].container_expiration_policy.name_regex_keep: NoneType
[].container_expiration_policy.next_run_at: str
[].repository_object_format: str
[].issues_enabled: bool
[].merge_requests_enabled: bool
[].wiki_enabled: bool
[].jobs_enabled: bool
[].snippets_enabled: bool
[].container_registry_enabled: bool
[].service_desk_enabled: bool
[].service_desk_address: NoneType
[].can_create_merge_request_in: bool
[].issues_access_level: str
[].repository_access_level: str
[].merge_requests_access_level: str
[].forking_access_level: str
[].wiki_access_level: str
[].builds_access_level: str
[].snippets_access_level: str
[].pages_access_level: str
[].analytics_access_level: str
[].container_registry_access_level: str
[].security_and_compliance_access_level: str
[].releases_access_level: str
[].environments_access_level: str
[].infrastructure_access_level: str
[].monitor_access_level: str
[].model_experiments_access_level: str
[].model_registry_access_level: str
[].package_registry_access_level: str
[].emails_disabled: bool
[].emails_enabled: bool
[].show_diff_preview_in_email: bool
[].shared_runners_enabled: bool
[].lfs_enabled: bool
[].creator_id: int
[].import_type: NoneType
[].import_status: str
[].open_issues_count: int
[].description_html: str
[].updated_at: str
[].ci_default_git_depth: int
[].ci_delete_pipelines_in_seconds: NoneType
[].ci_forward_deployment_enabled: bool
[].ci_forward_deployment_rollback_allowed: bool
[].ci_job_token_scope_enabled: bool
[].ci_separated_caches: bool
[].ci_allow_fork_pipelines_to_run_in_parent_project: bool
[].ci_id_token_sub_claim_components: list[3]
[].build_git_strategy: str
[].keep_latest_artifact: bool
[].restrict_user_defined_variables: bool
[].ci_pipeline_variables_minimum_override_role: str
[].runner_token_expiration_interval: NoneType
[].group_runners_enabled: bool
[].resource_group_default_process_mode: str
[].auto_cancel_pending_pipelines: str
[].build_timeout: int
[].auto_devops_enabled: bool
[].auto_devops_deploy_strategy: str
[].ci_push_repository_for_job_token_allowed: bool
```

### inst2_user

```
id: int
username: str
public_email: str
name: str
state: str
locked: bool
web_url: str
created_at: str
bio: str
location: str
linkedin: str
twitter: str
discord: str
website_url: str
github: str
job_title: str
pronouns: str
organization: str
bot: bool
work_information: NoneType
local_time: NoneType
last_sign_in_at: str
confirmed_at: str
last_activity_on: str
email: str
theme_id: int
color_scheme_id: int
projects_limit: int
current_sign_in_at: str
identities: list[0]
can_create_group: bool
can_create_project: bool
two_factor_enabled: bool
external: bool
private_profile: bool
commit_email: str
preferred_language: str
is_admin: bool
note: str
namespace_id: int
created_by: dict
created_by.id: int
created_by.username: str
created_by.public_email: NoneType
created_by.name: str
created_by.state: str
created_by.locked: bool
created_by.web_url: str
```

### inst2_user_events

```
<root>: list[5]
[].id: int
[].project_id: int
[].action_name: str
[].target_id: int
[].target_iid: int
[].target_type: str
[].author_id: int
[].target_title: str
[].created_at: str
[].author: dict
[].author.id: int
[].author.username: str
[].author.public_email: str
[].author.name: str
[].author.state: str
[].author.locked: bool
[].author.web_url: str
[].imported: bool
[].imported_from: str
[].author_username: str
```

### inst2_users_by_username

```
<root>: list[1]
[].id: int
[].username: str
[].public_email: str
[].name: str
[].state: str
[].locked: bool
[].web_url: str
[].created_at: str
[].bio: str
[].location: str
[].linkedin: str
[].twitter: str
[].discord: str
[].website_url: str
[].github: str
[].job_title: str
[].pronouns: str
[].organization: str
[].bot: bool
[].work_information: NoneType
[].local_time: NoneType
[].last_sign_in_at: str
[].confirmed_at: str
[].last_activity_on: str
[].email: str
[].theme_id: int
[].color_scheme_id: int
[].projects_limit: int
[].current_sign_in_at: str
[].identities: list[0]
[].can_create_group: bool
[].can_create_project: bool
[].two_factor_enabled: bool
[].external: bool
[].private_profile: bool
[].commit_email: str
[].preferred_language: str
[].is_admin: bool
[].note: str
[].namespace_id: int
[].created_by: dict
[].created_by.id: int
[].created_by.username: str
[].created_by.public_email: NoneType
[].created_by.name: str
[].created_by.state: str
[].created_by.locked: bool
[].created_by.web_url: str
```

### inst2_version

```
version: str
revision: str
kas: dict
kas.enabled: bool
kas.externalUrl: str
kas.externalK8sProxyUrl: str
kas.version: str
enterprise: bool
```

