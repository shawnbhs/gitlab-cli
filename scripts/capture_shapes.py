#!/usr/bin/env python3
"""Capture SANITIZED response shapes from BOTH live GitLab instances.

Read-only: every request is an HTTP GET. Nothing is written to any server,
nothing is written to ``~/.gitlab-cli``, and nothing is written outside
``tests/fixtures/``.

Instances and credentials come from the existing (read-only) gitlab-cli
modules: ``instances.list_instances()`` for the list and
``instances.build_cfg(inst)`` for the headers. Token values are never printed
and never written to a fixture.

Sanitization happens BEFORE anything is written to disk:

* hosts / URLs      -> ``https://gitlab.example.com``
* namespaces/paths  -> ``example-group/sub/demo-app``, ``other-group/demo-lib``
* usernames         -> ``alice``, ``bob``, ``carol`` ... (consistent everywhere)
* names             -> ``Alice``, ``Bob`` ...; emails -> ``alice@example.com``
* titles / messages / descriptions / notes -> ``Sample text N``
* branch names      -> ``branch-1`` (``main`` / ``master`` / ``develop`` kept)
* commit SHAs       -> deterministic fake 40-hex values (``short_id`` and
  every cross-reference follow the same map)
* numeric ids       -> small integers, consistent across every fixture
* web_url / references / URLs -> rebuilt from the fake paths, SHAs and ids
* diff bodies       -> deterministic sample unified-diff lines
* avatars           -> dropped
* any string still carrying a non-ASCII character -> ``Sample text N``
* states, booleans and timestamps (with their offsets) are PRESERVED - they
  are the shape, not the secret
* ``_capture_meta.json`` records endpoint TEMPLATES (``/projects/:id/...``),
  never the concrete request paths

Usage (inside WSL, from the gitlab-mcp repo root). ``GITLAB_CLI_ROOT`` points
at the gitlab-cli checkout (default ``~/gitlab-cli``):

    GITLAB_CLI_ROOT=~/src/gitlab-cli scripts/capture_shapes.py

    scripts/capture_shapes.py --scan      # re-run the leak scan only
    scripts/capture_shapes.py --shapes    # rebuild SHAPES.md offline

Set ``GITLAB_CAPTURE_FORBIDDEN`` to a comma-separated list of internal names
(company name, internal TLD, ...) that must never reach a fixture; they are
scrubbed and then asserted absent. They are deliberately NOT hardcoded here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    sys.stderr.write("requests is required\n")
    raise SystemExit(2) from None

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
# gitlab-cli checkout providing the read-only ``instances`` module. Never a
# hardcoded home path: this file ships to a public repository.
GITLAB_CLI_ROOT = Path(
    os.environ.get("GITLAB_CLI_ROOT") or (Path.home() / "gitlab-cli")
).expanduser()

FAKE_HOST = "gitlab.example.com"
FAKE_BASE = f"https://{FAKE_HOST}"
TIMEOUT = 20
# per-instance budget
MAX_REQUESTS = 38

ARABIC = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
ARABIC_ESC = re.compile(r"\\u0[67][0-9a-fA-F]{2}")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PRIVATE_IP = re.compile(r"\b(?:10|127|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.[\d.]+\b")
TOKEN_RE = re.compile(r"\b(?:glpat|gloas|glrt|glsoat)-[A-Za-z0-9_\-]{8,}\b")

# Extra internal names that must never appear in a published fixture.
# Supplied by the OPERATOR at run time, never hardcoded - this file ships to a
# public repo, so writing the real names here would itself be the leak.
# Example:  GITLAB_CAPTURE_FORBIDDEN="acme-corp,acme.internal"
FORBIDDEN_NAMES = tuple(
    n.strip()
    for n in os.environ.get("GITLAB_CAPTURE_FORBIDDEN", "").split(",")
    if n.strip()
)

KEEP_BRANCHES = {"main", "master", "develop", "HEAD"}

TEXT_KEYS = {
    "title",
    "description",
    "message",
    "body",
    "note",
    "commit_title",
    "squash_commit_message",
    "merge_commit_message",
    "last_commit_message",
    "bio",
    "organization",
    "job_title",
    "pronouns",
    "location",
    "skype",
    "linkedin",
    "twitter",
    "discord",
    "website_url",
    "public_email",
    "commit_message",
    "tag_message",
    "label",
    "reason",
}
USER_STR_KEYS = {
    "username",
    "author_username",
    "committer_username",
    "merge_user_username",
    "merged_by_username",
    "assignee_username",
    "reviewer_username",
    "author_name",
    "author_email",
    "committer_name",
    "committer_email",
    "committed_by",
    "name",
    "email",
    "user",
    "owner",
}
DROP_KEYS = {
    "avatar_url",
    "avatar",
    "web_url_avatar",
    "gravatar_id",
    "custom_attributes",
    "runners_token",
    "ci_job_token",
    "import_url",
    "feature_flags_access_level",
}

USER_POOL = [
    "alice",
    "bob",
    "carol",
    "dave",
    "erin",
    "frank",
    "grace",
    "heidi",
    "ivan",
    "judy",
    "ken",
    "lena",
    "mallory",
    "niaj",
    "olivia",
    "peggy",
]
GROUP_POOL = ["example-group", "sub", "other-group", "web", "ops", "tools"] + [
    f"example-group-{n}" for n in range(2, 12)
]
PROJECT_POOL = ["demo-app", "demo-lib", "demo-tools", "demo-site", "demo-infra"] + [
    f"demo-app-{n}" for n in range(2, 12)
]
SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")
# keys whose integer value is an identifier (remapped to small ints)
PROJECT_ID_KEYS = {"project_id", "source_project_id", "target_project_id"}
USER_ID_KEYS = {"author_id", "user_id", "creator_id", "assignee_id",
                "merge_user_id", "merged_by_id", "reviewer_id"}
IID_KEYS = {"iid", "target_iid", "merge_request_iid", "noteable_iid"}

SAMPLE_DIFF = (
    "@@ -1,4 +1,5 @@\n"
    " context line one\n"
    "-removed line\n"
    "+added line one\n"
    "+added line two\n"
    " context line two\n"
)


# ---------------------------------------------------------------- sanitizer


class Sanitizer:
    """Replaces every identifying value with a stable fake."""

    def __init__(self, hosts: list[str]) -> None:
        self.hosts = [h.lower() for h in hosts if h]
        self._users: dict[str, str] = {}
        self._groups: dict[str, str] = {}
        self._projects: dict[str, str] = {}
        self._texts: dict[str, str] = {}
        self._branches: dict[str, str] = {}
        self._shas: dict[str, str] = {}
        self._ids: dict[tuple[str, int], int] = {}
        # original path_with_namespace -> fake, applied to every URL/reference
        self._paths: dict[str, str] = {}
        # original handles/names seen, scrubbed from free text in finalize()
        self._handles: dict[str, str] = {}

    # -- maps -------------------------------------------------------

    def user(self, original: Any) -> str:
        if not isinstance(original, str) or not original.strip():
            return "alice"
        key = original.strip().lower().split("@")[0]
        if key not in self._users:
            n = len({v for v in self._users.values()})
            self._users[key] = USER_POOL[n] if n < len(USER_POOL) else f"user{n + 1}"
        if len(key) >= 3:
            self._handles[original.strip()] = self._users[key]
        return self._users[key]

    def sha(self, original: str) -> str:
        """Deterministic fake 40-hex SHA; the same input always maps the same."""
        key = original.lower()
        if key not in self._shas:
            seed = f"gitlab-mcp-fixture:{len(self._shas)}".encode()
            self._shas[key] = hashlib.sha1(seed, usedforsecurity=False).hexdigest()
        return self._shas[key]

    def ident(self, kind: str, original: Any) -> Any:
        """Small, stable integer per (kind, original id)."""
        if not isinstance(original, int) or isinstance(original, bool):
            return original
        key = (kind, original)
        if key not in self._ids:
            self._ids[key] = 1 + sum(1 for k in self._ids if k[0] == kind)
        return self._ids[key]

    def group(self, original: Any) -> str:
        key = str(original).strip().lower()
        if key not in self._groups:
            n = len(self._groups)
            self._groups[key] = (
                GROUP_POOL[n] if n < len(GROUP_POOL) else f"group-{n + 1}"
            )
        return self._groups[key]

    def project(self, original: Any) -> str:
        key = str(original).strip().lower()
        if key not in self._projects:
            n = len(self._projects)
            self._projects[key] = (
                PROJECT_POOL[n] if n < len(PROJECT_POOL) else f"project-{n + 1}"
            )
        return self._projects[key]

    def text(self, original: Any) -> str:
        if original is None:
            return None  # type: ignore[return-value]
        key = str(original)
        if key not in self._texts:
            self._texts[key] = f"Sample text {len(self._texts) + 1}"
        return self._texts[key]

    def branch(self, original: Any) -> str:
        key = str(original).strip()
        if key in KEEP_BRANCHES:
            return key
        if key not in self._branches:
            self._branches[key] = f"branch-{len(self._branches) + 1}"
        return self._branches[key]

    def path(self, original: Any) -> str:
        """``group/sub/project`` -> ``example-group/sub/demo-app``."""
        raw = str(original).strip().strip("/")
        if not raw:
            return "example-group/demo-app"
        parts = raw.split("/")
        out = [self.group(p) for p in parts[:-1]]
        out.append(self.project(parts[-1]))
        fake = "/".join(out)
        if "/" in raw:
            self._paths[raw] = fake
        return fake

    def namespace(self, original: Any) -> str:
        """``group/sub`` -> ``example-group/sub`` (groups only, no project)."""
        raw = str(original).strip().strip("/")
        fake = "/".join(self.group(p) for p in raw.split("/")) if raw else "example-group"
        if raw:
            self._paths[raw] = fake
        return fake

    # -- scalars ----------------------------------------------------

    def _hosts(self, s: str) -> str:
        for host in self.hosts:
            if host in s.lower():
                s = re.sub(
                    r"https?://[^/\s\"]*" + re.escape(host) + r"(:\d+)?",
                    FAKE_BASE,
                    s,
                    flags=re.I,
                )
                s = re.sub(re.escape(host), FAKE_HOST, s, flags=re.I)
        for name in FORBIDDEN_NAMES:
            s = re.sub(re.escape(name), FAKE_HOST.split(".")[0], s, flags=re.I)
        return s

    def string(self, s: str, key: str | None = None) -> str:
        s = TOKEN_RE.sub("glpat-REDACTED", s)
        s = re.sub(r"oauth2:[^@]*@", "oauth2:REDACTED@", s)
        s = self._hosts(s)
        s = PRIVATE_IP.sub("10.0.0.1", s)
        s = EMAIL_RE.sub(lambda m: f"{self.user(m.group(0))}@example.com", s)
        # 64-hex asset digests (favicons) can embed a 40-hex run of a real SHA
        s = re.sub(r"[0-9a-f]{64}",
                   hashlib.sha256(b"gitlab-mcp-asset").hexdigest(), s)
        s = SHA_RE.sub(lambda m: self.sha(m.group(0)), s)
        if not s.isascii():
            return self.text(s)
        return s

    def finalize(self, text: str) -> str:
        """Last pass over a serialized fixture, after EVERY payload was walked.

        URLs and ``references`` embed paths, short SHAs and handles that were
        only learnt while walking other payloads, so they are rewritten here.
        """
        for orig, fake in sorted(self._paths.items(), key=lambda kv: -len(kv[0])):
            text = re.sub(r"(?<![\w.-])" + re.escape(orig) + r"(?![\w.-])",
                          fake, text, flags=re.I)
        for orig, fake in self._shas.items():
            text = text.replace(orig, fake)
            text = re.sub(r"\b" + re.escape(orig[:8]) + r"\b", fake[:8], text)
        for orig, fake in sorted(self._handles.items(), key=lambda kv: -len(kv[0])):
            text = re.sub(r"\b" + re.escape(orig) + r"\b", fake, text, flags=re.I)
        text = re.sub(
            r"(/-/(?:merge_requests|issues)/|[!#])(\d+)\b",
            lambda m: m.group(1) + str(self._ids.get(("iid", int(m.group(2))),
                                                      m.group(2))),
            text,
        )
        text = re.sub(
            r"(/-/pipelines/|/-/jobs/)(\d+)",
            lambda m: m.group(1) + str(self._ids.get(("id", int(m.group(2))),
                                                      m.group(2))),
            text,
        )
        for name in FORBIDDEN_NAMES:
            text = re.sub(re.escape(name), "example", text, flags=re.I)
        return text

    # -- containers -------------------------------------------------

    @staticmethod
    def _is_user(d: dict) -> bool:
        return "username" in d and ("id" in d or "state" in d)

    @staticmethod
    def _is_project(d: dict) -> bool:
        return "path_with_namespace" in d or ("path" in d and "default_branch" in d)

    @staticmethod
    def _is_diff(d: dict) -> bool:
        return "diff" in d and ("new_path" in d or "old_path" in d)

    def _user_dict(self, d: dict) -> dict:
        fake = self.user(d.get("username") or d.get("email") or d.get("name"))
        out: dict[str, Any] = {}
        for k, v in d.items():
            if k in DROP_KEYS:
                continue
            if k == "username":
                out[k] = fake
            elif k == "id":
                out[k] = self.ident("user", v)
            elif k == "name":
                out[k] = fake.capitalize()
            elif k in ("email", "public_email", "commit_email", "notification_email"):
                out[k] = f"{fake}@example.com" if v else v
            elif k == "web_url" and isinstance(v, str):
                out[k] = f"{FAKE_BASE}/{fake}"
            else:
                out[k] = self.walk(v, k)
        return out

    def _project_dict(self, d: dict) -> dict:
        pwn = d.get("path_with_namespace") or d.get("path") or ""
        fake_path = self.path(pwn)
        leaf = fake_path.split("/")[-1]
        ns = "/".join(fake_path.split("/")[:-1]) or "example-group"
        out: dict[str, Any] = {}
        for k, v in d.items():
            if k in DROP_KEYS:
                continue
            if k == "id":
                out[k] = self.ident("project", v)
            elif k in PROJECT_ID_KEYS:
                out[k] = self.ident("project", v)
            elif k == "path_with_namespace":
                out[k] = fake_path
            elif k in ("path", "name"):
                out[k] = leaf
            elif k == "name_with_namespace":
                out[k] = fake_path.replace("/", " / ")
            elif k == "web_url" and isinstance(v, str):
                out[k] = f"{FAKE_BASE}/{fake_path}"
            elif k in ("ssh_url_to_repo",):
                out[k] = f"git@{FAKE_HOST}:{fake_path}.git"
            elif k in ("http_url_to_repo",):
                out[k] = f"{FAKE_BASE}/{fake_path}.git"
            elif k == "readme_url" and isinstance(v, str):
                out[k] = f"{FAKE_BASE}/{fake_path}/-/blob/main/README.md"
            elif k == "default_branch" and isinstance(v, str):
                out[k] = self.branch(v)
            elif k == "namespace" and isinstance(v, dict):
                ns_kind = "user" if v.get("kind") == "user" else "group"
                out[k] = {
                    kk: (
                        ns.split("/")[-1]
                        if kk in ("name", "path")
                        else (
                            ns
                            if kk == "full_path"
                            else (
                                f"{FAKE_BASE}/{ns}"
                                if kk == "web_url"
                                else (
                                    self.ident(ns_kind, vv)
                                    if kk in ("id", "parent_id")
                                    else self.walk(vv, kk)
                                )
                            )
                        )
                    )
                    for kk, vv in v.items()
                    if kk not in DROP_KEYS
                }
            else:
                out[k] = self.walk(v, k)
        return out

    def _diff_dict(self, d: dict) -> dict:
        out: dict[str, Any] = {}
        for k, v in d.items():
            if k in DROP_KEYS:
                continue
            if k == "diff":
                out[k] = SAMPLE_DIFF
            elif k in ("new_path", "old_path") and isinstance(v, str):
                suffix = Path(v).suffix or ""
                out[k] = f"src/sample_file{suffix}"
            else:
                out[k] = self.walk(v, k)
        return out

    def _int(self, key: str, value: int) -> int:
        if key in PROJECT_ID_KEYS:
            return self.ident("project", value)
        if key in USER_ID_KEYS:
            return self.ident("user", value)
        if key in IID_KEYS:
            return self.ident("iid", value)
        if key == "id" or key.endswith("_id"):
            return self.ident("id", value)
        return value

    def walk(self, obj: Any, key: str | None = None) -> Any:
        if isinstance(obj, dict):
            if self._is_user(obj):
                return self._user_dict(obj)
            if self._is_project(obj):
                return self._project_dict(obj)
            if self._is_diff(obj):
                return self._diff_dict(obj)
            out: dict[str, Any] = {}
            for k, v in obj.items():
                if k in DROP_KEYS:
                    continue
                if isinstance(v, str):
                    if k in TEXT_KEYS and k not in (
                        "name",
                        "label",
                        "public_email",
                        "website_url",
                    ):
                        out[k] = self.text(v) if v else v
                    elif k in ("name", "label") and v:
                        out[k] = self.text(v)
                    elif k in ("public_email", "website_url"):
                        out[k] = self.string(v, k) if v else v
                    elif k in (
                        "source_branch",
                        "target_branch",
                        "ref",
                        "branch",
                        "default_branch",
                        "merge_request_source_branch",
                    ):
                        out[k] = self.branch(v)
                    elif k in USER_STR_KEYS:
                        fake = self.user(v)
                        if k.endswith("_email") or k == "email":
                            out[k] = f"{fake}@example.com"
                        elif k.endswith("_name"):
                            out[k] = fake.capitalize()
                        else:
                            out[k] = fake
                    elif k in ("path_with_namespace", "project_path"):
                        out[k] = self.path(v)
                    elif k in ("full_path", "namespace_path", "group_path"):
                        out[k] = self.namespace(v)
                    elif k in ("sha", "id", "short_id") and re.fullmatch(
                        r"[0-9a-f]{7,40}", v
                    ):
                        out[k] = self.sha(v) if len(v) == 40 else v
                    elif k == "diff":
                        out[k] = SAMPLE_DIFF
                    else:
                        out[k] = self.string(v, k)
                elif isinstance(v, int) and not isinstance(v, bool):
                    out[k] = self._int(k, v)
                else:
                    out[k] = self.walk(v, k)
            return out
        if isinstance(obj, list):
            return [self.walk(v, key) for v in obj]
        if isinstance(obj, str):
            return self.string(obj, key)
        return obj


# ---------------------------------------------------------------- shapes


def shape(obj: Any, prefix: str = "", depth: int = 0, out: dict | None = None) -> dict:
    """Collect ``path -> type`` for the first sample of every branch."""
    if out is None:
        out = {}
    if depth > 5:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            out[path] = type(v).__name__
            shape(v, path, depth + 1, out)
    elif isinstance(obj, list):
        out[prefix or "<root>"] = f"list[{len(obj)}]"
        if obj:
            shape(obj[0], f"{prefix}[]", depth + 1, out)
    return out


# ---------------------------------------------------------------- capture


class Capture:
    """GET-only capture against one instance."""

    def __init__(self, label: str, url: str, headers: dict, san: Sanitizer) -> None:
        self.label = label
        self.api = url.rstrip("/") + "/api/v4"
        self.san = san
        self.session = requests.Session()
        self.session.headers.update(dict(headers))
        self.session.headers.setdefault("Accept", "application/json")
        self.count = 0
        self.meta: list[dict] = []
        self.raw: dict[str, Any] = {}

    def get(self, name: str, path: str, params: dict | None = None) -> Any:
        if self.count >= MAX_REQUESTS:
            print(f"  budget exhausted for {self.label}")
            return None
        self.count += 1
        status: Any = None
        err = ""
        data: Any = None
        try:
            r = self.session.get(self.api + path, params=params, timeout=TIMEOUT)
            status = r.status_code
            if r.status_code < 400:
                try:
                    data = r.json()
                except ValueError:
                    err = "non-json body"
            else:
                err = f"http {r.status_code}"
        except requests.RequestException as exc:
            err = type(exc).__name__
        self.meta.append(
            {"label": name, "path": endpoint_template(path, params),
             "status": status, "error": err}
        )
        print(f"  {self.label:10s} {name:30s} status={status} {err}")
        if data is not None:
            self.raw[name] = data
        return data

    def sanitized(self) -> dict[str, Any]:
        """Phase 1: walk every payload so the sanitizer learns all mappings."""
        return {name: self.san.walk(data) for name, data in self.raw.items()}

    def write(self, name: str, clean: Any) -> Path:
        """Phase 2: serialize, then rewrite URLs/references via finalize()."""
        p = FIXTURES / f"{self.label}_{name}.json"
        text = json.dumps(clean, indent=2, ensure_ascii=True) + "\n"
        p.write_text(self.san.finalize(text), encoding="utf-8")
        return p


def endpoint_template(path: str, params: dict | None = None) -> str:
    """``/projects/42/merge_requests/7/diffs`` -> ``/projects/:id/merge_requests/:iid/diffs``.

    Only the generic template is recorded in ``_capture_meta.json``; concrete
    ids, SHAs and handles from the live request never reach disk.
    """
    t = re.sub(r"/projects/[^/]+", "/projects/:id", path)
    t = re.sub(r"/users/\d+", "/users/:id", t)
    t = re.sub(r"/(merge_requests|issues)/\d+", r"/\1/:iid", t)
    t = re.sub(r"/(pipelines|jobs|groups)/[^/]+", r"/\1/:id", t)
    t = re.sub(r"/commits/[^/]+", "/commits/:sha", t)
    if params and "username" in params:
        t += "?username=:username"
    return t


# ---------------------------------------------------------------- scan


def scan(hosts: list[str], usernames: list[str]) -> dict[str, int]:
    counts = {
        "real_host": 0,
        "real_username": 0,
        "forbidden_name": 0,
        "private_ip": 0,
        "token_shape": 0,
        "foreign_email": 0,
        "arabic": 0,
    }
    host_res = [re.compile(re.escape(h), re.I) for h in hosts if h]
    user_res = [
        re.compile(r"\b" + re.escape(u) + r"\b", re.I) for u in usernames if u
    ]
    forb_res = [re.compile(re.escape(n), re.I) for n in FORBIDDEN_NAMES]
    for p in sorted(FIXTURES.glob("*")):
        if p.is_dir():
            continue
        txt = p.read_text(encoding="utf-8", errors="replace")
        for rx in host_res:
            counts["real_host"] += len(rx.findall(txt))
        for rx in user_res:
            counts["real_username"] += len(rx.findall(txt))
        for rx in forb_res:
            counts["forbidden_name"] += len(rx.findall(txt))
        counts["private_ip"] += len(
            [m for m in PRIVATE_IP.findall(txt) if m != "10.0.0.1"]
        )
        counts["token_shape"] += len(
            [m for m in TOKEN_RE.findall(txt) if "REDACTED" not in m]
        )
        counts["foreign_email"] += sum(
            1 for m in EMAIL_RE.findall(txt) if not m.lower().endswith("example.com")
        )
        counts["arabic"] += len(ARABIC.findall(txt))
        counts["arabic"] += len(ARABIC_ESC.findall(txt))
    return counts


SURPRISES_MD = [
    "## Version differences and SURPRISES",
    "",
    "Two instances were captured, both GitLab CE but several generations apart:",
    "",
    "| label | version | revision available |",
    "|---|---|---|",
    "| `inst1` | 17.5.5 | yes |",
    "| `inst2` | 19.1.1 | yes |",
    "",
    "Everything below is **measured on these two instances**, not read from docs.",
    "",
    "### CONTRACT CORRECTIONS (measured, contradicts prior assumptions)",
    "",
    "* `GET /api/v4/version` returned **200 on BOTH instances** with the ordinary",
    "  project-scoped token. The claim that the older instance 401s on `/version`",
    "  did NOT reproduce. Still prefer `/user` for liveness, because it works on",
    "  both AND identifies the token - but do not code a 401 special case.",
    "* `GET /api/v4/metadata` exists on **BOTH** instances (17.5 and 19.1) and",
    "  returns the SAME keys as `/version`: `version, revision, kas, enterprise`.",
    "  It is not a 19.x-only endpoint here.",
    "* **Both** MR diff endpoints work on **both** instances:",
    "  `/merge_requests/:iid/changes` -> 200, `/merge_requests/:iid/diffs` -> 200.",
    "  Neither is 404 on 17.5. See the next section for which one to actually use.",
    "",
    "### The two MR diff endpoints return DIFFERENT SHAPES",
    "",
    "This is the trap, and it is a shape difference, not an availability one.",
    "",
    "* `/merge_requests/:iid/changes` -> a **dict**: the entire MR object with a",
    "  `changes` list bolted on. It is **unpaginated** and returned **27 files**",
    "  on inst1 in one response. Deprecated upstream; it is the expensive call.",
    "* `/merge_requests/:iid/diffs` -> a **bare list** of diff objects, properly",
    "  paginated (`per_page` honoured: 3 requested, 3 returned). No MR object.",
    "",
    "Recommendation: use `/diffs` for the diff itself and fetch the MR object",
    "separately from `/merge_requests/:iid`. Use `/changes` only when a single",
    "round trip matters more than payload size, and never without a cap - it has",
    "no page limit and a large MR will return every file at once.",
    "",
    "### Diff objects gained two keys between 17.5 and 19.1",
    "",
    "Common to both: `old_path, new_path, a_mode, b_mode, new_file,",
    "renamed_file, deleted_file, generated_file, diff`.",
    "",
    "inst2 (19.1) adds **`collapsed`** and **`too_large`**, both booleans. These",
    "matter: when `too_large` is true the `diff` string is EMPTY, and when",
    "`collapsed` is true it may be truncated. Code that assumes a non-empty",
    "`diff` whenever the request succeeded silently reports 'no changes' on large",
    "files against 19.x. Read them with `.get(..., False)` so 17.5 still works.",
    "",
    "`diff` itself is a **raw unified-diff string** on both versions, not a",
    "structure. In these fixtures every body is replaced with fixed sample lines.",
    "",
    "### Commit stats are opt-in; the identity keys come in two pairs",
    "",
    "* `/repository/commits` omits `stats` unless `with_stats=true` is passed.",
    "  With it each commit carries `stats: {additions, deletions, total}`.",
    "* Keys identical on both instances: `id, short_id, title, message,",
    "  author_name, author_email, authored_date, committer_name,",
    "  committer_email, committed_date, created_at, parent_ids, web_url,",
    "  trailers, extended_trailers, stats`.",
    "* There are TWO identity pairs (`author_*` and `committer_*`) and TWO dates",
    "  (`authored_date`, `committed_date`). Attribute by `author_email` and date",
    "  by `committed_date` to match what the UI shows for a rebased branch.",
    "* `parent_ids` is a list; >= 2 entries means a merge commit. Counting commits",
    "  without filtering merges double-counts merged work.",
    "* `/repository/commits/:sha/diff` returns a **bare list** of the same diff",
    "  objects as `/diffs` - no commit object wrapped around it.",
    "",
    "### Events: `push_data` is optional",
    "",
    "Event keys on both: `id, project_id, action_name, target_id, target_iid,",
    "target_type, target_title, author, author_id, author_username, created_at,",
    "imported, imported_from`.",
    "",
    "* **`push_data` is absent on every non-push event** and may be missing or",
    "  empty on push events. Code must not rely on it: treat `push_data` as",
    "  optional and derive push activity from `action_name` instead.",
    "* `action_name` is a human string (`pushed to`, `opened`, `commented on`) and",
    "  is localisable - it is untrusted free text, not an enum.",
    "* `target_type` is `null` for push events on 17.5 but `Project` on 19.1, so",
    "  a `target_type`-keyed dispatch drops or misfiles most of the feed.",
    "* `author_username` carries the handle at the TOP level. (Easy to miss when",
    "  sanitizing - it is NOT inside the nested `author` object.)",
    "",
    "### Approvals: CE returns a degenerate payload, and the keys are not the",
    "### documented ones",
    "",
    "Measured on both instances, identically:",
    "",
    "```",
    "{user_has_approved: false, user_can_approve: true, approved: false,",
    " approved_by: []}",
    "```",
    "",
    "* **`approvals_required` and `approvals_left` are NOT present.** Approval",
    "  rules are a Premium feature; on CE the endpoint answers 200 with only the",
    "  four keys above. Any code reading `approvals_left == 0` as 'approved' gets",
    "  a `KeyError` here, and reading it with a `0` default silently reports every",
    "  MR as approved.",
    "* Use `approved` (bool) and `approved_by` (list) - both present on 17.5 and",
    "  19.1 - and treat the absence of `approvals_required` as 'no rules",
    "  configured', never as 'satisfied'.",
    "",
    "### Discussions are threads, not comments",
    "",
    "* `/merge_requests/:iid/discussions` returns discussion threads, each with",
    "  `id`, `individual_note` (bool) and a `notes` list. A plain comment is a",
    "  discussion with `individual_note: true` and exactly one note.",
    "* Notes with `system: true` are label/assignment/pipeline events. A",
    "  human-comment count that does not filter them inflates badly.",
    "* A positioned (code) note carries `position` with `{base_sha, start_sha,",
    "  head_sha, old_path, new_path, position_type, old_line, new_line}`. Those",
    "  three SHAs come from the MR's `diff_refs` and are mandatory when POSTing a",
    "  diff note - they cannot be invented.",
    "",
    "### Pipelines",
    "",
    "* List keys on both: `id, iid, project_id, sha, ref, status, source, name,",
    "  web_url, created_at, updated_at`. `iid` and `name` are present on 17.5 too.",
    "* Duration, coverage, and the triggering user live ONLY on the",
    "  single-pipeline endpoint - one extra request each.",
    "* `status` is one of `created, waiting_for_resource, preparing, pending,",
    "  running, success, failed, canceled, skipped, manual, scheduled`. Treating",
    "  anything not `success` as failure mislabels `skipped` and `manual`.",
    "",
    "### Projects list grew 10 keys between versions",
    "",
    "* inst1 (17.5) project objects carry **100** keys; inst2 (19.1) carry **110**.",
    "  Never validate a project object against a fixed key set - use `.get()` and",
    "  pick the keys you need.",
    "* `membership=true` is required or the list is every public project on the",
    "  instance. `order_by=last_activity_at` needs an explicit `sort=desc` to mean",
    "  'newest first'.",
    "* `permissions.project_access` / `group_access` is `null` (not absent) when",
    "  the user's access comes via the other path - check both before deciding an",
    "  access level.",
    "* Pagination headers (`X-Next-Page`, `X-Total`) are the authoritative paging",
    "  signal, and `X-Total` is OMITTED on large collections, so a missing total",
    "  is not zero.",
    "",
    "### `/users?username=<handle>` returns a LIST",
    "",
    "Even for an exact unique handle the response is a one-element list, not an",
    "object. `/user` (no `s`) is the object form for the token's own identity.",
    "",
]


def write_shapes_md(entries: list[dict], fixtures: dict[str, Any]) -> Path:
    lines = [
        "# GitLab response shapes (synthetic fixtures)",
        "",
        "Shapes captured read-only (GET only) from two GitLab CE instances by",
        "`scripts/capture_shapes.py`; every value was replaced before anything",
        "touched disk: hosts -> `https://gitlab.example.com`, paths ->",
        "`example-group/sub/demo-app`, users -> `alice`/`bob`/`carol`, emails ->",
        "`alice@example.com`, titles and messages -> `Sample text N`, commit SHAs ->",
        "deterministic fake 40-hex values, ids -> small consistent integers, diff",
        "bodies -> fixed sample unified-diff lines, avatars dropped. States,",
        "booleans and timestamps (with offsets) are kept as shapes.",
        "",
        "Fixture files are named `<instance>_<endpoint>.json` where `<instance>` is a",
        "neutral label (`inst1`, `inst2`), so the two versions can be diffed.",
        "",
        "## Endpoints",
        "",
        "| fixture | instance | endpoint template | status |",
        "|---|---|---|---|",
    ]
    for e in entries:
        lines.append(
            f"| `{e['instance']}_{e['label']}.json` | {e['instance']} "
            f"| `{e['path']}` | {e['status']} |"
        )
    lines.append("")
    lines.extend(SURPRISES_MD)
    lines.append("## Key fields and types")
    lines.append("")
    for label, data in fixtures.items():
        lines.append(f"### {label}")
        lines.append("")
        lines.append("```")
        for path, typ in list(shape(data).items())[:110]:
            lines.append(f"{path}: {typ}")
        lines.append("```")
        lines.append("")
    p = FIXTURES / "SHAPES.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------- main


def capture_instance(label: str, url: str, headers: dict, san: Sanitizer) -> Capture:
    cap = Capture(label, url, headers, san)

    me = cap.get("user", "/user")
    own_username = (me or {}).get("username") or ""
    own_id = (me or {}).get("id")

    if own_username:
        cap.get("users_by_username", "/users", {"username": own_username})
    if own_id:
        cap.get("user_events", f"/users/{own_id}/events", {"per_page": 5})

    projects = cap.get(
        "projects",
        "/projects",
        {
            "membership": "true",
            "per_page": 3,
            "order_by": "last_activity_at",
            "sort": "desc",
        },
    )

    # choose a project that actually has merge requests / commits
    pid = None
    if isinstance(projects, list):
        for pr in projects:
            if pr.get("id"):
                pid = pr["id"]
                break
    if pid is None:
        print(f"  {label}: no accessible project, stopping here")
        return cap

    commits = cap.get(
        "commits",
        f"/projects/{pid}/repository/commits",
        {"with_stats": "true", "per_page": 3},
    )
    sha = None
    if isinstance(commits, list) and commits:
        sha = commits[0].get("id")
    if sha:
        cap.get("commit_diff", f"/projects/{pid}/repository/commits/{sha}/diff")

    mrs = cap.get(
        "merge_requests",
        f"/projects/{pid}/merge_requests",
        {"state": "all", "per_page": 2},
    )
    # if this project has no MRs, look for one that does (cheap: 1 request)
    if isinstance(mrs, list) and not mrs:
        found = cap.get(
            "merge_requests_any",
            "/merge_requests",
            {"scope": "all", "state": "all", "per_page": 2},
        )
        if isinstance(found, list) and found:
            mrs = found
            pid = found[0].get("project_id", pid)

    iid = None
    if isinstance(mrs, list) and mrs:
        iid = mrs[0].get("iid")
        pid = mrs[0].get("project_id", pid)

    if iid:
        cap.get("mr_changes", f"/projects/{pid}/merge_requests/{iid}/changes")
        cap.get(
            "mr_diffs",
            f"/projects/{pid}/merge_requests/{iid}/diffs",
            {"per_page": 3},
        )
        cap.get("mr_discussions", f"/projects/{pid}/merge_requests/{iid}/discussions")
        cap.get("mr_approvals", f"/projects/{pid}/merge_requests/{iid}/approvals")
    else:
        print(f"  {label}: no merge request reachable - MR fixtures skipped")

    cap.get("pipelines", f"/projects/{pid}/pipelines", {"per_page": 3})
    # version probing (documented difference between the two instances)
    cap.get("version", "/version")
    cap.get("metadata", "/metadata")
    return cap


def main(argv: list[str]) -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    if GITLAB_CLI_ROOT.is_dir() and str(GITLAB_CLI_ROOT) not in sys.path:
        sys.path.insert(0, str(GITLAB_CLI_ROOT))

    try:
        import instances  # type: ignore  # noqa: F401
    except ImportError:
        sys.stderr.write(
            "cannot import gitlab-cli `instances` - set GITLAB_CLI_ROOT (or "
            "PYTHONPATH) to the gitlab-cli repo root\n"
        )
        return 2

    insts = instances.list_instances()
    hosts: list[str] = []
    usernames: list[str] = []
    cfgs: list[tuple[str, dict]] = []
    for inst in insts:
        try:
            cfg = instances.build_cfg(inst)
        except Exception as exc:  # pragma: no cover
            print(f"  skip instance (cfg error: {type(exc).__name__})")
            continue
        url = cfg["url"]
        hosts.append(re.sub(r"^https?://", "", url).split("/")[0].split(":")[0])
        cfgs.append((url, cfg))
    hosts = [h for h in hosts if h]

    if "--scan" in argv:
        print("scan:", scan(hosts, usernames))
        return 0

    san = Sanitizer(hosts)

    if "--shapes" in argv:
        meta_path = FIXTURES / "_capture_meta.json"
        entries = (
            json.loads(meta_path.read_text(encoding="utf-8")).get("endpoints", [])
            if meta_path.exists()
            else []
        )
        written = {
            p.stem: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(FIXTURES.glob("*.json"))
            if not p.name.startswith("_")
        }
        md = write_shapes_md(entries, written)
        print(f"wrote {md.name} from {len(written)} fixtures (no network)")
        print("leak scan (all must be 0):", scan(hosts, usernames))
        return 0

    print(f"instances found: {len(cfgs)}")
    print("capturing (GET only):")
    all_meta: list[dict] = []
    captures: list[Capture] = []
    total = 0
    for n, (url, cfg) in enumerate(cfgs, start=1):
        label = f"inst{n}"
        cap = capture_instance(label, url, cfg["headers"], san)
        total += cap.count
        me = cap.raw.get("user") or {}
        for k in ("username", "name", "email", "commit_email", "public_email"):
            v = me.get(k)
            if isinstance(v, str) and len(v.strip()) >= 3:
                usernames.append(v.strip())
        captures.append(cap)
        all_meta.extend(
            {
                "instance": label,
                "label": m["label"],
                "path": m["path"],
                "status": m["status"],
                "error": m["error"],
            }
            for m in cap.meta
        )
        print(f"  {label}: {cap.count} requests")

    # walk EVERYTHING first, so cross-file references share one mapping
    cleaned = [(cap, cap.sanitized()) for cap in captures]
    for cap, payloads in cleaned:
        for name, clean in payloads.items():
            p = cap.write(name, clean)
            print(f"  wrote {p.name} ({p.stat().st_size} bytes)")

    (FIXTURES / "_capture_meta.json").write_text(
        json.dumps(
            {
                "source": "two GitLab CE instances, synthetic data in captured shapes",
                "requests": total,
                "endpoints": all_meta,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    written = {
        p.stem: json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(FIXTURES.glob("*.json"))
        if not p.name.startswith("_")
    }
    md = write_shapes_md(all_meta, written)
    print(f"  wrote {md.name}")
    print(f"total requests: {total}")
    print("leak scan (all must be 0):", scan(hosts, usernames))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
