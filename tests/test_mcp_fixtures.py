"""Reporting against REAL GitLab response shapes — offline.

Every payload here is loaded from ``tests/fixtures/inst1_*.json`` (GitLab CE
17.5) or ``tests/fixtures/inst2_*.json`` (GitLab 19.1): the key sets, types
and quirks of live responses, with every value replaced by synthetic data
(see ``tests/fixtures/SHAPES.md``). Hand-written payloads only appear where a
fixture is *derived* from a captured one to exercise a documented shape the
capture happened not to contain (a 19.x ``too_large`` diff, an event without
``push_data``).

The double is a fake context — a working in-memory ``GitLabContext`` keyed by
request path — not a mock, so the assertions are about the report.
"""

from __future__ import annotations

import copy
import json
import os
import re
import unittest

from gitlab_mcp import reporting
from tests import _home_guard

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def setUpModule():
    _home_guard.activate()


def tearDownModule():
    _home_guard.deactivate()


def load(name: str):
    """One fixture file, parsed."""
    with open(os.path.join(FIXTURES, f"{name}.json"), encoding="utf-8") as fh:
        return json.load(fh)


# The ids the captures were taken against (see SHAPES.md endpoint table).
INSTANCES = {
    "inst1": {"project_id": 1, "mr_iid": 2, "user_id": 1,
              "sha": "acb4060f6b28b9b55d30b9f46c8ff05b50857e5c"},
    "inst2": {"project_id": 11, "mr_iid": 5, "user_id": 1,
              "sha": "9f945b78fff5495f9fb4e952448e193b01e4aaca"},
}


class NotFound(Exception):
    """What the fake raises for a path it has no payload for (a 404)."""


class FixtureCtx:
    """An in-memory ``GitLabContext`` serving captured payloads by path.

    ``get`` raises :class:`NotFound` for unknown paths; ``paginate`` returns
    an empty page for them (a list endpoint with no rows). Writes fail.
    """

    def __init__(self, responses: dict, projects: list[dict]):
        self.name = "fixture"
        self.url = "https://gitlab.example.com"
        self.master_dir = "/srv/none"
        self._responses = responses
        self._by_path = {p["path_with_namespace"]: p["id"] for p in projects}
        self.gets: list[str] = []

    def _lookup(self, path, params):
        state = (params or {}).get("state")
        if state and (path, state) in self._responses:
            return self._responses[(path, state)]
        if path not in self._responses:
            raise NotFound(f"404 {path}")
        return self._responses[path]

    def get(self, path, params=None):
        self.gets.append(path)
        value = self._lookup(path, params)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def paginate(self, path, params=None, limit=None):
        try:
            items = self._lookup(path, params)
        except NotFound:
            items = []
        if isinstance(items, Exception):
            raise items
        items = copy.deepcopy(list(items))
        total = len(items)
        page = items[:limit] if limit is not None else items
        return page, limit is not None and total > limit, total

    def post(self, path, json=None):  # pragma: no cover - must never run
        raise AssertionError(f"reporting must not POST ({path})")

    def put(self, path, json=None):  # pragma: no cover - must never run
        raise AssertionError(f"reporting must not PUT ({path})")

    def project_id(self, ref):
        if str(ref).isdigit():
            return int(ref)
        return self._by_path[ref]

    def user_id(self, ref):
        raise AssertionError("not needed by these reports")


def instance_ctx(inst: str, **overrides) -> FixtureCtx:
    """A context wired with every captured endpoint of one instance."""
    ids = INSTANCES[inst]
    pid, iid, uid, sha = (ids["project_id"], ids["mr_iid"], ids["user_id"],
                          ids["sha"])
    proj = f"/projects/{pid}"
    mr_base = f"{proj}/merge_requests/{iid}"
    projects = load(f"{inst}_projects")
    mrs = load(f"{inst}_merge_requests")
    mr_obj = next(m for m in mrs if m["iid"] == iid)
    commits = load(f"{inst}_commits")
    responses = {
        proj: next(p for p in projects if p["id"] == pid),
        f"{proj}/repository/commits": commits,
        f"{proj}/repository/commits/{sha}":
            next(c for c in commits if c["id"] == sha),
        f"{proj}/repository/commits/{sha}/diff": load(f"{inst}_commit_diff"),
        f"{proj}/merge_requests": mrs,
        (f"{proj}/merge_requests", "merged"):
            [m for m in mrs if m["state"] == "merged"],
        (f"{proj}/merge_requests", "opened"):
            [m for m in mrs if m["state"] == "opened"],
        mr_base: mr_obj,
        f"{mr_base}/diffs": load(f"{inst}_mr_diffs"),
        f"{mr_base}/changes": load(f"{inst}_mr_changes"),
        f"{mr_base}/discussions": load(f"{inst}_mr_discussions"),
        f"{mr_base}/approvals": load(f"{inst}_mr_approvals"),
        f"{proj}/pipelines": load(f"{inst}_pipelines"),
        "/users": load(f"{inst}_users_by_username"),
        f"/users/{uid}/events": load(f"{inst}_user_events"),
        "/projects": projects,
    }
    for other in projects:
        responses.setdefault(f"/projects/{other['id']}", other)
    responses.update(overrides)
    return FixtureCtx(responses, projects)


def project_ref(inst: str) -> str:
    """The sanitized path of the captured project."""
    pid = INSTANCES[inst]["project_id"]
    return next(p["path_with_namespace"] for p in load(f"{inst}_projects")
                if p["id"] == pid)


def too_large_variant(entries: list[dict]) -> list[dict]:
    """Captured diff objects with the first one turned into a 19.x too_large.

    GitLab 19.1 sends ``too_large: true`` with an EMPTY ``diff`` string
    (SHAPES.md "Diff objects gained two keys").
    """
    entries = copy.deepcopy(entries)
    entries[0]["too_large"] = True
    entries[0]["collapsed"] = False
    entries[0]["diff"] = ""
    return entries


# ─── merge_request ───────────────────────────────────────────────────────────

class MergeRequestApprovalsOnCommunityEdition(unittest.TestCase):
    """CE /approvals answers 200 with no approvals_required/approvals_left."""

    def check(self, inst):
        ids = INSTANCES[inst]
        result = reporting.merge_request(instance_ctx(inst), project_ref(inst),
                                         ids["mr_iid"])
        approvals = result["approvals"]
        self.assertIsNone(approvals["approvals_required"])
        self.assertIsNone(approvals["approvals_left"])
        self.assertFalse(approvals["approval_rules_available"])
        self.assertIs(approvals["approved"], False)
        self.assertEqual(approvals["approved_by"], [])
        self.assertTrue(any("approvals_required and approvals_left are null"
                            in n for n in result["notes"]))

    def test_inst1_ce_17_approvals_are_unknown_not_zero(self):
        self.check("inst1")

    def test_inst2_ce_19_approvals_are_unknown_not_zero(self):
        self.check("inst2")

    def test_premium_shape_still_reports_its_rule_counts(self):
        premium = dict(load("inst1_mr_approvals"), approvals_required=2,
                       approvals_left=1,
                       approved_by=[{"user": {"username": "bob"}}])
        base = "/projects/1/merge_requests/2/approvals"
        result = reporting.merge_request(instance_ctx("inst1", **{base: premium}),
                                         project_ref("inst1"), 2)
        self.assertEqual(result["approvals"]["approvals_required"], 2)
        self.assertEqual(result["approvals"]["approvals_left"], 1)
        self.assertEqual(result["approvals"]["approved_by"], ["bob"])
        self.assertTrue(result["approvals"]["approval_rules_available"])


class MergeRequestDiffShapes(unittest.TestCase):
    """/diffs is a bare list; /changes is the MR dict with a changes list."""

    def test_inst1_diffs_list_counts_every_file(self):
        result = reporting.merge_request(instance_ctx("inst1"),
                                         project_ref("inst1"), 2)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["diff_summary"]["files_changed"], 3)
        self.assertEqual(result["diff_summary"]["additions"], 6)
        self.assertEqual(result["diff_summary"]["deletions"], 3)
        self.assertNotIn("line_counts_exact", result["diff_summary"])

    def test_inst1_falls_back_to_changes_dict_with_27_files(self):
        ctx = instance_ctx("inst1", **{
            "/projects/1/merge_requests/2/diffs": NotFound("404 diffs")})
        result = reporting.merge_request(ctx, project_ref("inst1"), 2)
        self.assertEqual(result["total"], 27)
        self.assertEqual(len(result["files"]), 27)
        self.assertEqual(result["files"][0]["path"], "src/sample_file.py")
        self.assertFalse(result["truncated"])

    def test_inst2_changes_dict_returned_from_the_diffs_path_is_read(self):
        ctx = instance_ctx("inst2", **{
            "/projects/11/merge_requests/5/diffs": load("inst2_mr_changes")})
        result = reporting.merge_request(ctx, project_ref("inst2"), 5)
        self.assertEqual(result["total"], 6)

    def test_inst2_too_large_file_has_unknown_line_counts(self):
        diffs = too_large_variant(load("inst2_mr_diffs"))
        ctx = instance_ctx("inst2", **{
            "/projects/11/merge_requests/5/diffs": diffs})
        result = reporting.merge_request(ctx, project_ref("inst2"), 5)
        big = result["files"][0]
        self.assertTrue(big["too_large"])
        self.assertIsNone(big["additions"])
        self.assertIsNone(big["deletions"])
        self.assertTrue(big["diff_truncated"])
        self.assertIn("too_large", big["diff_omitted_reason"])
        self.assertFalse(result["diff_summary"]["line_counts_exact"])
        self.assertEqual(result["diff_summary"]["files_without_line_counts"], 1)
        self.assertEqual(result["diff_summary"]["files_too_large"], 1)
        self.assertTrue(result["truncated"])
        self.assertTrue(any("too_large" in n for n in result["notes"]))

    def test_inst2_normal_files_keep_exact_counts_beside_a_too_large_one(self):
        diffs = too_large_variant(load("inst2_mr_diffs"))
        ctx = instance_ctx("inst2", **{
            "/projects/11/merge_requests/5/diffs": diffs})
        files = reporting.merge_request(ctx, project_ref("inst2"), 5)["files"]
        self.assertEqual(files[1]["additions"], 2)
        self.assertEqual(files[1]["deletions"], 1)
        self.assertNotIn("too_large", files[1])

    def test_inst2_discussions_and_mr_fields_survive_the_newer_shape(self):
        result = reporting.merge_request(instance_ctx("inst2"),
                                         project_ref("inst2"), 5)
        self.assertEqual(result["iid"], 5)
        self.assertEqual(result["state"], "merged")
        self.assertEqual(result["author"], "lena")
        self.assertEqual(result["discussion_count"], 2)


# ─── commit_detail ───────────────────────────────────────────────────────────

class CommitDetailFromFixtures(unittest.TestCase):

    def test_inst1_bare_list_diff(self):
        sha = INSTANCES["inst1"]["sha"]
        result = reporting.commit_detail(instance_ctx("inst1"),
                                         project_ref("inst1"), sha)
        self.assertEqual(result["sha"], sha)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["diff_summary"]["additions"], 6)
        self.assertEqual(result["stats"]["additions"], 137)
        self.assertEqual(result["notes"], [])

    def test_inst2_merge_commit_keeps_both_parents(self):
        sha = INSTANCES["inst2"]["sha"]
        result = reporting.commit_detail(instance_ctx("inst2"),
                                         project_ref("inst2"), sha)
        self.assertEqual(len(result["parent_ids"]), 2)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["files"][0]["path"], "src/sample_file.yml")

    def test_inst2_too_large_commit_file_is_not_reported_as_no_change(self):
        sha = INSTANCES["inst2"]["sha"]
        path = f"/projects/11/repository/commits/{sha}/diff"
        ctx = instance_ctx("inst2", **{
            path: too_large_variant(load("inst2_commit_diff"))})
        result = reporting.commit_detail(ctx, project_ref("inst2"), sha)
        self.assertIsNone(result["files"][0]["additions"])
        self.assertTrue(result["files"][0]["too_large"])
        self.assertFalse(result["diff_summary"]["line_counts_exact"])
        self.assertTrue(result["truncated"])
        self.assertTrue(any("not zero" in n for n in result["notes"]))


# ─── contributors ────────────────────────────────────────────────────────────

class ContributorsFromFixtures(unittest.TestCase):

    def test_inst1_single_author_totals(self):
        result = reporting.contributors(instance_ctx("inst1"),
                                        project_ref("inst1"))
        self.assertEqual(result["author_count"], 1)
        carol = result["authors"][0]
        self.assertEqual(carol["email"], "carol@example.com")
        self.assertEqual(carol["commits"], 3)
        self.assertEqual(carol["additions"], 1416)
        self.assertEqual(carol["deletions"], 78)
        self.assertEqual(carol["first_date"], "2026-10-10T12:22:46.000+05:30")
        self.assertEqual(carol["last_date"], "2026-10-11T13:54:13.000+05:30")

    def test_inst2_merge_commit_is_not_double_counted(self):
        result = reporting.contributors(instance_ctx("inst2"),
                                        project_ref("inst2"))
        ivan = next(a for a in result["authors"]
                    if a["email"] == "ivan@example.com")
        self.assertEqual(ivan["commits"], 1)
        self.assertEqual(ivan["merge_commits"], 1)
        self.assertEqual(ivan["additions"], 1252)
        self.assertEqual(result["merge_commits_excluded"], 1)
        self.assertEqual(result["additions"], 1253)

    def test_inst2_mixed_offsets_and_a_malformed_date_do_not_crash(self):
        result = reporting.contributors(instance_ctx("inst2"),
                                        project_ref("inst2"))
        ken = next(a for a in result["authors"]
                   if a["email"] == "ken@example.com")
        self.assertEqual(ken["last_date"], "2026-09-02T08:56:28.000+00:00")


# ─── project_progress ────────────────────────────────────────────────────────

class ProjectProgressFromFixtures(unittest.TestCase):

    def test_inst1_weeks_mrs_and_pipelines(self):
        result = reporting.project_progress(instance_ctx("inst1"),
                                            project_ref("inst1"))
        self.assertEqual(result["commits"], 3)
        self.assertEqual(result["merged_count"], 1)
        self.assertEqual(result["merge_requests_merged"][0]["author"], "dave")
        self.assertEqual(result["open_merge_requests"], 1)
        self.assertEqual(result["pipeline_success_rate"], 1.0)
        self.assertEqual(result["pipelines_sampled"], 3)
        self.assertEqual(sum(w["commits"] for w in result["commits_per_week"]), 3)

    def test_inst2_merge_commit_excluded_from_weekly_volume(self):
        result = reporting.project_progress(instance_ctx("inst2"),
                                            project_ref("inst2"))
        self.assertEqual(result["commits"], 2)
        self.assertEqual(result["merge_commits"], 1)
        self.assertEqual(sum(w["additions"] for w in result["commits_per_week"]),
                         1253)
        self.assertEqual(result["merged_count"], 2)
        self.assertEqual(result["open_merge_requests"], 0)


# ─── person_report ───────────────────────────────────────────────────────────

class PersonReportFromFixtures(unittest.TestCase):

    def person(self, inst, **overrides):
        result = reporting.person_report(instance_ctx(inst, **overrides),
                                         ["alice"])
        return result, result["people"][0]

    def test_inst1_pushes_counted_by_action_name(self):
        _, alice = self.person("inst1")
        self.assertTrue(alice["found"])
        self.assertEqual(alice["user_id"], 1)
        self.assertEqual(alice["event_summary"]["pushes"], 2)
        self.assertEqual(alice["event_summary"]["mrs_merged"], 2)
        self.assertEqual(alice["event_summary"]["mrs_opened"], 1)

    def test_inst2_push_with_project_target_type_is_still_a_push(self):
        _, alice = self.person("inst2")
        self.assertEqual(alice["event_summary"]["pushes"], 2)
        self.assertEqual(alice["event_summary"]["events"], 5)
        self.assertEqual({p["project_id"] for p in alice["projects"]}, {11, 14})

    def test_pushes_counted_when_push_data_is_absent(self):
        events = load("inst2_user_events")
        for event in events:
            event.pop("push_data", None)
        _, alice = self.person("inst2", **{"/users/1/events": events})
        self.assertEqual(alice["event_summary"]["pushes"], 2)
        self.assertEqual(alice["event_summary"]["pushes_without_push_data"], 2)
        self.assertEqual(alice["event_summary"]["pushed_commits"], 0)

    def test_push_data_commit_counts_are_summed_when_present(self):
        _, alice = self.person("inst1")
        self.assertEqual(alice["event_summary"]["pushed_commits"], 7)
        self.assertEqual(alice["event_summary"]["pushes_without_push_data"], 0)

    def test_event_by_another_author_username_is_excluded(self):
        events = load("inst1_user_events")
        events[1]["author_username"] = "bob"
        events[1]["author_id"] = None
        _, alice = self.person("inst1", **{"/users/1/events": events})
        self.assertEqual(alice["event_summary"]["events"], 4)
        self.assertEqual(alice["event_summary"]["pushes"], 1)

    def test_unreadable_event_project_is_a_note_not_a_crash(self):
        result, alice = self.person("inst1")
        self.assertEqual(alice["projects"][0]["project"], "4")
        self.assertTrue(any("project 4" in n for n in result["notes"]))

    def test_method_documents_push_rule(self):
        result, _ = self.person("inst1")
        self.assertIn("action_name", result["method"])


# ─── version-varying shapes ──────────────────────────────────────────────────

class ShapeDriftAcrossVersions(unittest.TestCase):

    def test_project_listing_needs_no_fixed_key_set(self):
        for inst in ("inst1", "inst2"):
            rows = reporting.projects(instance_ctx(inst))
            self.assertEqual(len(rows), 3, inst)
            self.assertTrue(all(r["path_with_namespace"] for r in rows), inst)

    def test_version_endpoint_answers_on_both(self):
        self.assertEqual(load("inst1_version")["version"], "17.5.5")
        self.assertEqual(load("inst2_version")["version"], "19.1.1")


# ─── fixture hygiene (the fixtures ship in a public repository) ──────────────

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://([^/\s\"'`<>)]*)", re.I)
SSH_REMOTE_RE = re.compile(r"\bgit@([A-Za-z0-9.-]+):")
ARABIC_SCRIPT_RE = re.compile(
    "[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
ESCAPED_ARABIC_RE = re.compile(r"\\u0[678][0-9a-fA-F]{2}")


def _fixture_texts() -> dict[str, str]:
    """Every file under ``tests/fixtures`` (JSON and SHAPES.md), as text."""
    texts = {}
    for name in sorted(os.listdir(FIXTURES)):
        path = os.path.join(FIXTURES, name)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                texts[name] = fh.read()
    return texts


def _host_is_example(host: str) -> bool:
    host = host.rsplit("@", 1)[-1].split(":", 1)[0].lower()
    return host == "example.com" or host.endswith(".example.com")


def _emails(text: str) -> list[str]:
    """Email addresses in ``text``; ``git@host:path`` remotes are not emails."""
    return EMAIL_RE.findall(SSH_REMOTE_RE.sub(" ", text))


def _hosts(text: str) -> list[str]:
    """Hosts of every ``scheme://`` URL and ``git@host:`` remote in ``text``."""
    return URL_RE.findall(text) + SSH_REMOTE_RE.findall(text)


def _forbidden_words() -> list[str]:
    """Operator-supplied words that must never appear (never hardcoded)."""
    raw = os.environ.get("GITLAB_FIXTURE_FORBIDDEN", "")
    return [w.strip().lower() for w in raw.split(",") if w.strip()]


class FixtureHygiene(unittest.TestCase):
    """Fixtures carry shapes only: no real people, hosts or internal names."""

    def setUp(self):
        self.texts = _fixture_texts()
        self.assertGreater(len(self.texts), 28)

    def test_every_email_is_at_example_com(self):
        bad = [(name, m) for name, text in self.texts.items()
               for m in _emails(text)
               if m.lower().rsplit("@", 1)[-1] != "example.com"]
        self.assertEqual(bad, [])

    def test_every_url_host_is_under_example_com(self):
        bad = [(name, host) for name, text in self.texts.items()
               for host in _hosts(text) if not _host_is_example(host)]
        self.assertEqual(bad, [])

    def test_no_arabic_script_characters(self):
        bad = [name for name, text in self.texts.items()
               if ARABIC_SCRIPT_RE.search(text)
               or ESCAPED_ARABIC_RE.search(text)]
        self.assertEqual(bad, [])

    def test_no_operator_forbidden_word(self):
        words = _forbidden_words()
        bad = [(name, index) for name, text in self.texts.items()
               for index, word in enumerate(words) if word in text.lower()]
        # Report only the word's position in the env list, never the word.
        self.assertEqual(bad, [], "GITLAB_FIXTURE_FORBIDDEN entries found")


class FixtureHygieneCheckersCatchLeaks(unittest.TestCase):
    """The hygiene checks above would actually fail on a leaked value."""

    SAMPLE = ('x@corp.test "https://git.corp.test/a" git@corp.test:a.git '
              '"wss://gitlab.example.com/-/k"')

    def test_foreign_email_is_found_and_ssh_remote_is_not_an_email(self):
        self.assertEqual(_emails(self.SAMPLE), ["x@corp.test"])

    def test_foreign_url_and_ssh_hosts_are_rejected(self):
        hosts = _hosts(self.SAMPLE)
        self.assertEqual([h for h in hosts if not _host_is_example(h)],
                         ["git.corp.test", "corp.test"])

    def test_example_subdomain_is_accepted(self):
        self.assertTrue(_host_is_example("gitlab.example.com"))
        self.assertFalse(_host_is_example("example.com.corp.test"))

    def test_arabic_script_raw_and_escaped_are_found(self):
        self.assertTrue(ARABIC_SCRIPT_RE.search("\u0633"))
        self.assertTrue(ESCAPED_ARABIC_RE.search("\\u0633"))


if __name__ == "__main__":
    unittest.main()
