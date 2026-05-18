"""GitHub interactions: PR metadata, file fetching, review posting, state-branch sync.

State branch strategy
---------------------
The SQLite knowledge DB lives on a dedicated orphan branch named `__reviewer_state__`
in the same repo. On startup we shallow-clone just that branch into $STATE_DIR.
After the run, if the DB changed, we commit it back with `[skip ci]` and push.

This gives us:
- Zero external infra (no S3, no Postgres)
- True persistence (not subject to Actions cache eviction)
- Version-controlled, auditable, trivially backed up
- Conflict-tolerant (pull-rebase loop on push failure)
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)


# ============================================================
# DATA MODEL
# ============================================================

@dataclass
class ChangedFile:
    path: str
    status: str         # added | modified | removed | renamed
    additions: int
    deletions: int
    blob_sha_new: str   # head-side blob sha (for incremental review)
    diff: str = ""
    diff_line_numbers: set[int] = field(default_factory=set)
    language: str = ""
    old_content: str | None = None
    new_content: str | None = None
    analyzer_output: str = ""


# ============================================================
# SHELL HELPERS
# ============================================================

def gh(args: list[str], **kw) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, **kw)
    if r.returncode != 0:
        log.warning("gh %s failed: %s", " ".join(args[:3]), r.stderr.strip()[:300])
        return ""
    return r.stdout


def git(args: list[str], cwd: str | None = None, check: bool = True, **kw) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, **kw)
    if check and r.returncode != 0:
        log.warning("git %s failed: %s", " ".join(args[:3]), r.stderr.strip()[:300])
    return r


# ============================================================
# PR METADATA
# ============================================================

def get_pr_metadata(repo: str, pr_number: int) -> dict:
    out = gh([
        "pr", "view", str(pr_number), "--repo", repo,
        "--json", "title,body,labels,author,baseRefName,headRefName,additions,deletions,commits",
    ])
    return json.loads(out) if out else {}


# ============================================================
# CHANGED FILES
# ============================================================

def get_changed_files(repo: str, pr_number: int) -> list[ChangedFile]:
    out = gh([
        "api", "--paginate",
        f"repos/{repo}/pulls/{pr_number}/files",
    ])
    if not out:
        return []
    data = _parse_paginated_json(out)

    files: list[ChangedFile] = []
    for f in data:
        cf = ChangedFile(
            path=f["filename"],
            status=f["status"],
            additions=f.get("additions", 0),
            deletions=f.get("deletions", 0),
            blob_sha_new=f.get("sha", ""),
            diff=f.get("patch", "") or "",
        )
        cf.diff_line_numbers = parse_diff_new_lines(cf.diff)
        cf.language = detect_language(cf.path)
        files.append(cf)
    return files


def _parse_paginated_json(out: str) -> list:
    """gh --paginate may return one big array or concatenated arrays."""
    try:
        v = json.loads(out)
        return v if isinstance(v, list) else [v]
    except json.JSONDecodeError:
        # paginated arrays often come back as `][` separated
        try:
            return json.loads("[" + out.replace("][", ",")
                              .replace("]\n[", ",") + "]")
        except json.JSONDecodeError:
            log.error("failed to parse paginated gh output")
            return []


_LANG_BY_EXT = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".rb": "ruby", ".php": "php", ".cs": "csharp",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".swift": "swift", ".scala": "scala", ".sh": "bash", ".bash": "bash",
    ".sql": "sql", ".md": "markdown", ".yml": "yaml", ".yaml": "yaml",
    ".json": "json", ".toml": "toml",
}


def detect_language(path: str) -> str:
    return _LANG_BY_EXT.get(Path(path).suffix.lower(), "")


def parse_diff_new_lines(diff: str) -> set[int]:
    """Lines in the new file that appear in diff hunks.
    GitHub rejects inline comments outside this set."""
    lines: set[int] = set()
    cur = 0
    for line in diff.splitlines():
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                cur = int(m.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            lines.add(cur); cur += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        elif line.startswith("\\"):
            continue
        else:
            cur += 1
    return lines


# ============================================================
# FILE CONTENT
# ============================================================

def fetch_file_contents(files: list[ChangedFile], base_sha: str, head_sha: str) -> None:
    for f in files:
        if f.status not in ("added",):
            f.old_content = git_show(base_sha, f.path)
        if f.status not in ("removed",):
            f.new_content = git_show(head_sha, f.path)


def git_show(ref: str, path: str) -> str | None:
    r = git(["show", f"{ref}:{path}"], check=False)
    return r.stdout if r.returncode == 0 else None


# ============================================================
# POSTING REVIEW
# ============================================================

def post_review(
    repo: str,
    pr_number: int,
    head_sha: str,
    body: str,
    inline_comments: list[dict],
    event: str,
) -> int | None:
    """POST /repos/{owner}/{repo}/pulls/{pr}/reviews. Returns the review id.

    GitHub's review-with-comments API is fussy: a single bad line/path in any
    comment fails the whole submission with HTTP 422 and a single inscrutable
    error string. We try the all-in-one submission first; if it 422s, we retry
    submitting the summary review without inline comments, then attach the
    inline findings as standalone review comments one at a time (skipping any
    individual comment that GitHub still rejects).

    GitHub also forbids APPROVE / REQUEST_CHANGES on a PR opened by the same
    user as the reviewer (which happens when the bot uses a PAT whose owner
    also raised the PR). When we detect that specific error we downgrade the
    event to COMMENT and retry.
    """
    rid, downgraded_event = _try_submit_review(
        repo, pr_number, head_sha, body, inline_comments, event,
    )
    if rid is not None:
        return rid
    return _fallback_split_submit(
        repo, pr_number, head_sha, body, inline_comments, downgraded_event,
    )


_SELF_REVIEW_ERRORS = (
    "Can not request changes on your own pull request",
    "Can not approve your own pull request",
)


def _try_submit_review(
    repo: str, pr_number: int, head_sha: str,
    body: str, inline_comments: list[dict], event: str,
) -> tuple[int | None, str]:
    """Submit the review. Returns (review_id_or_None, event_for_retry).

    If GitHub rejects APPROVE/REQUEST_CHANGES because the reviewer is the
    PR author, retry once with event=COMMENT and surface that downgrade
    to the caller so the fallback path also uses COMMENT.
    """
    payload = {
        "commit_id": head_sha,
        "body": body,
        "event": event,
        "comments": inline_comments,
    }
    r = subprocess.run(
        ["gh", "api", "--method", "POST",
         f"repos/{repo}/pulls/{pr_number}/reviews",
         "--input", "-"],
        input=json.dumps(payload),
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        try:
            return json.loads(r.stdout).get("id"), event
        except Exception:
            return None, event

    n = len(inline_comments)
    stdout, stderr = r.stdout.strip()[:1500], r.stderr.strip()[:1500]
    log.error(
        "review submit failed (n_inline=%d, event=%s): stderr=%s | stdout=%s",
        n, event, stderr, stdout,
    )

    # Self-review on a PR opened by the same user as the token: GitHub
    # forbids APPROVE / REQUEST_CHANGES. Downgrade to COMMENT and retry.
    if event != "COMMENT" and any(m in stdout for m in _SELF_REVIEW_ERRORS):
        log.info("Downgrading event %s → COMMENT (self-PR) and retrying", event)
        payload["event"] = "COMMENT"
        r2 = subprocess.run(
            ["gh", "api", "--method", "POST",
             f"repos/{repo}/pulls/{pr_number}/reviews",
             "--input", "-"],
            input=json.dumps(payload),
            capture_output=True, text=True,
        )
        if r2.returncode == 0:
            try:
                return json.loads(r2.stdout).get("id"), "COMMENT"
            except Exception:
                return None, "COMMENT"
        log.error(
            "retry as COMMENT also failed: stderr=%s | stdout=%s",
            r2.stderr.strip()[:1500], r2.stdout.strip()[:1500],
        )
        return None, "COMMENT"

    return None, event


def _fallback_split_submit(
    repo: str, pr_number: int, head_sha: str,
    body: str, inline_comments: list[dict], event: str,
) -> int | None:
    """Post the summary review with no comments, then attach each inline as a
    separate review comment. Slower but resilient to a single bad line."""
    log.info("Falling back to split submission: summary review + per-comment")
    review_id, _ = _try_submit_review(repo, pr_number, head_sha, body, [], event)
    if review_id is None:
        # Even the summary failed; degrade to a plain PR comment.
        log.error("Summary review also failed; degrading to plain PR comment")
        gh(["pr", "comment", str(pr_number), "--repo", repo, "--body", body])
        return None

    posted = 0
    for c in inline_comments:
        payload = {
            "body": c["body"],
            "commit_id": head_sha,
            "path": c["path"],
            "line": c["line"],
            "side": c.get("side", "RIGHT"),
        }
        r = subprocess.run(
            ["gh", "api", "--method", "POST",
             f"repos/{repo}/pulls/{pr_number}/comments",
             "--input", "-"],
            input=json.dumps(payload),
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            posted += 1
        else:
            log.warning(
                "inline comment failed for %s:%s — %s",
                c.get("path"), c.get("line"), r.stderr.strip()[:300],
            )
    log.info("Attached %d/%d inline review comments", posted, len(inline_comments))
    return review_id


def post_pr_comment(repo: str, pr_number: int, body: str) -> None:
    gh(["pr", "comment", str(pr_number), "--repo", repo, "--body", body])


def reply_to_comment(repo: str, pr_number: int, parent_comment_id: int, body: str) -> None:
    """Reply to an issue comment (PR comments are issue comments)."""
    payload = {"body": body, "in_reply_to": parent_comment_id}
    subprocess.run(
        ["gh", "api", "--method", "POST",
         f"repos/{repo}/issues/{pr_number}/comments",
         "--input", "-"],
        input=json.dumps(payload), capture_output=True, text=True,
    )


# ============================================================
# STATE BRANCH SYNC
# ============================================================

class StateBranch:
    """Manages the SQLite DB on the __reviewer_state__ orphan branch."""

    INIT_README = (
        "# Reviewer State\n\n"
        "This branch holds the SQLite knowledge DB for the AI PR reviewer.\n"
        "Do not edit by hand. Do not delete unless you want to lose learnings.\n"
    )

    def __init__(self, repo: str, branch: str, state_dir: str, token: str):
        self.repo = repo
        self.branch = branch
        self.dir = Path(state_dir)
        self.token = token
        self.db_path = self.dir / "state.db"

    def remote_url(self) -> str:
        return f"https://x-access-token:{self.token}@github.com/{self.repo}.git"

    def setup(self) -> None:
        """Clone or initialize the state branch into self.dir."""
        if self.dir.exists():
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True, exist_ok=True)

        r = git(["clone", "--depth", "1", "--branch", self.branch, self.remote_url(), str(self.dir)],
                check=False)
        if r.returncode == 0:
            log.info("Cloned state branch %s into %s", self.branch, self.dir)
            self._configure_git()
            return

        # Branch doesn't exist yet — initialize it as an orphan
        log.info("State branch %s does not exist; initializing", self.branch)
        git(["clone", "--depth", "1", self.remote_url(), str(self.dir)])
        self._configure_git()
        git(["checkout", "--orphan", self.branch], cwd=str(self.dir))
        git(["rm", "-rf", "."], cwd=str(self.dir), check=False)
        (self.dir / "README.md").write_text(self.INIT_README)
        git(["add", "README.md"], cwd=str(self.dir))
        git(["commit", "-m", "init: reviewer state branch"], cwd=str(self.dir))
        git(["push", "origin", self.branch], cwd=str(self.dir))

    def _configure_git(self) -> None:
        git(["config", "user.email", "reviewer-bot@users.noreply.github.com"], cwd=str(self.dir))
        git(["config", "user.name", "PR Reviewer Bot"], cwd=str(self.dir))

    def commit_and_push(self, message: str = "update: reviewer state") -> bool:
        """Commit any changes (DB updates) and push. Retries on conflict.

        Returns True if state was actually pushed; False if no changes."""
        if not self.dir.exists():
            return False
        git(["add", "-A"], cwd=str(self.dir))
        status = git(["status", "--porcelain"], cwd=str(self.dir), check=False).stdout
        if not status.strip():
            log.debug("No state changes to commit")
            return False

        full_msg = f"{message} [skip ci]"
        git(["commit", "-m", full_msg], cwd=str(self.dir))

        for attempt in range(5):
            r = git(["push", "origin", self.branch], cwd=str(self.dir), check=False)
            if r.returncode == 0:
                log.info("State pushed to %s (attempt %d)", self.branch, attempt + 1)
                return True
            log.info("Push conflicted; pulling and retrying (attempt %d)", attempt + 1)
            git(["pull", "--rebase", "origin", self.branch], cwd=str(self.dir), check=False)
        log.error("Failed to push state after 5 attempts; state will be lost this run")
        return False


# ============================================================
# CI SIGNAL (used by orchestrator to enrich summary prompt)
# ============================================================

def get_failed_workflow_logs(repo: str, head_sha: str, max_lines: int = 200) -> str:
    """Return tail of logs for any failed workflow runs on this commit. Best-effort."""
    out = gh([
        "api", "--paginate",
        f"repos/{repo}/actions/runs?head_sha={head_sha}&per_page=10",
    ])
    if not out:
        return ""
    try:
        data = json.loads(out)
        runs = data.get("workflow_runs", []) if isinstance(data, dict) else []
    except Exception:
        return ""
    failed = [r for r in runs if r.get("conclusion") == "failure"][:3]
    if not failed:
        return ""
    parts: list[str] = []
    for run in failed:
        run_id = run["id"]
        name = run.get("name", "?")
        # gh run view returns text logs
        log_out = gh(["run", "view", str(run_id), "--repo", repo, "--log-failed"])
        if log_out:
            tail = "\n".join(log_out.splitlines()[-max_lines:])
            parts.append(f"### FAILED WORKFLOW: {name}\n{tail}")
    return "\n\n".join(parts)
