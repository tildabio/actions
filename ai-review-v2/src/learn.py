"""Listener for `@reviewer` mentions on PR comments.

Triggered by the `issue_comment` GitHub event. When a user replies to a review
comment with `@<bot> ...`, we ask the LLM whether the reply expresses a durable
team preference. If yes, we embed it, store it in SQLite, and reply with
"Learning added."
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess

from . import github_api, knowledge, llm, prompts
from .config import Config

log = logging.getLogger(__name__)

# Words used to dismiss / forget a learning by id, e.g. "@reviewer forget #42"
_FORGET_RE = re.compile(r"\b(forget|delete|remove)\s+#?(\d+)\b", re.IGNORECASE)

# Every comment v2 posts (reviews, inline findings, learn-mode replies) carries
# this HTML marker. Detecting it is the source-of-truth way to skip our own
# messages — username comparison is unreliable because the bot routinely posts
# under a real user's PAT (their token, not github-actions[bot]).
V2_MARKER = "<!-- pr-reviewer-v2 -->"


def process_comment_event(cfg: Config, conn, event: dict) -> None:
    """Entry point. `event` is the GitHub issue_comment webhook payload."""
    if event.get("action") not in ("created", "edited"):
        return

    comment = event.get("comment", {})
    body = comment.get("body", "") or ""
    user = comment.get("user", {}).get("login", "")

    # ① Skip anything we posted ourselves. The marker check covers the PAT case
    # the username check misses, and prevents the "bot replies to bot" loop
    # (our reply contains "@reviewer forget #N", which would otherwise re-fire
    # this workflow).
    if V2_MARKER in body:
        log.info("Skipping comment carrying v2 marker (self-bot output)")
        return
    if user.endswith("[bot]"):
        return

    # ② Mention check is case-insensitive so @Reviewer / @REVIEWER both work.
    if not _is_mention(body, cfg.bot_username):
        return

    pr_number = event.get("issue", {}).get("number") or cfg.pr_number
    if not pr_number:
        log.warning("No PR number found in event; skipping")
        return

    # Handle "forget" commands first — they don't need LLM
    m = _FORGET_RE.search(body)
    if m:
        learning_id = int(m.group(2))
        knowledge.deactivate_learning(conn, learning_id)
        log.info("Deactivated learning #%d (requested by %s)", learning_id, user)
        github_api.reply_to_comment(
            cfg.github_repo, pr_number, comment["id"],
            f"{V2_MARKER}\n🧠 Forgot learning #{learning_id}.",
        )
        return

    # Find the parent review comment (the line the user is replying to)
    parent = _find_parent_review_comment(cfg, comment, event)
    parent_body = parent.get("body", "") if parent else "(general PR comment)"
    file_path = parent.get("path") if parent else None

    pr_meta = github_api.get_pr_metadata(cfg.github_repo, pr_number)

    extract_client = llm.make_client(cfg.review)
    try:
        result, usage = llm.chat(
            extract_client, cfg.review.model,
            prompts.LEARNING_EXTRACT_SYSTEM,
            prompts.learning_extract_user(
                pr_title=pr_meta.get("title", ""),
                file_path=file_path,
                parent_review_comment=parent_body,
                user_reply=body,
                repo=cfg.github_repo,
            ),
        )
        knowledge.log_llm_call(conn, cfg.github_repo, pr_number, "learning_extract", usage, True)
    except Exception as e:
        log.exception("learning extraction failed")
        knowledge.log_llm_call(conn, cfg.github_repo, pr_number, "learning_extract",
                               {"model": cfg.review.model}, False, str(e))
        return

    if not result.get("is_learning"):
        log.info("Not a learning: %s", result.get("reasoning", ""))
        github_api.reply_to_comment(
            cfg.github_repo, pr_number, comment["id"],
            f"{V2_MARKER}\n👍 Noted. _(Not stored as a durable learning: "
            f"{result.get('reasoning', '')})_",
        )
        return

    description = result.get("description", "").strip()
    if not description:
        return

    # Embed and store
    try:
        embed_client = llm.make_client(cfg.embedding)
        vectors = llm.embed(embed_client, cfg.embedding.model, [description])
        if not vectors:
            return
        learning = knowledge.Learning(
            id=None,
            description=description,
            scope=result.get("scope") or "repo",
            file_pattern=result.get("file_pattern"),
            repo=cfg.github_repo,
            source_pr=pr_number,
            source_comment=comment["id"],
            created_by=user,
        )
        learning_id = knowledge.insert_learning(conn, learning, vectors[0])
    except Exception:
        log.exception("failed to store learning")
        return

    scope_str = learning.scope
    pattern_str = f" (scoped to `{learning.file_pattern}`)" if learning.file_pattern else ""
    github_api.reply_to_comment(
        cfg.github_repo, pr_number, comment["id"],
        f"{V2_MARKER}\n🧠 **Learning #{learning_id} added{pattern_str}**\n\n"
        f"> {description}\n\n"
        f"_Scope: `{scope_str}`. Reply `@reviewer forget #{learning_id}` to remove._",
    )


# ============================================================
# HELPERS
# ============================================================

def _is_mention(body: str, bot_username: str) -> bool:
    """Case-insensitive @-mention check.

    Always matches `@reviewer` (the generic alias we tell users to use in
    docs). Also matches the configured bot_username if set, with or without
    a `[bot]` suffix. All comparisons are lowercased — `@Reviewer`,
    `@REVIEWER`, and `@reviewer` all trigger the loop.
    """
    bl = body.lower()
    if "@reviewer" in bl:
        return True
    if bot_username:
        base = bot_username.replace("[bot]", "").lower()
        if base and f"@{base}" in bl:
            return True
        if f"@{bot_username.lower()}" in bl:
            return True
    return False


def _find_parent_review_comment(cfg: Config, comment: dict, event: dict) -> dict | None:
    """If this comment is a reply to a pull-request review comment, fetch the parent.

    GitHub's `issue_comment` event doesn't include the parent for review-thread
    replies — we have to look it up from the comment's in_reply_to or fall back
    to the most recent review comment on the same path/line.
    """
    in_reply_to = comment.get("in_reply_to_id")
    if not in_reply_to:
        return None

    pr_number = event.get("issue", {}).get("number") or cfg.pr_number
    try:
        out = subprocess.run(
            ["gh", "api", f"repos/{cfg.github_repo}/pulls/comments/{in_reply_to}"],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode == 0:
            return json.loads(out.stdout)
    except Exception:
        log.exception("failed to fetch parent comment %s", in_reply_to)
    return None


# ============================================================
# CLI ENTRY (called by learn workflow)
# ============================================================

def main() -> int:
    from . import config
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = config.load()

    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not event_path or not os.path.exists(event_path):
        log.error("GITHUB_EVENT_PATH not set or missing")
        return 1
    with open(event_path) as f:
        event = json.load(f)

    branch = github_api.StateBranch(cfg.github_repo, cfg.state_branch, cfg.state_dir, cfg.github_token)
    branch.setup()
    with knowledge.connect(branch.db_path) as conn:
        process_comment_event(cfg, conn, event)
    branch.commit_and_push("update: learning")
    return 0
