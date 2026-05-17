"""Entry point.

Two modes:
  python -m src review    → run the PR review pipeline
  python -m src learn     → process an issue_comment event for learnings
"""

from __future__ import annotations

import logging
import sys

from . import config, github_api, knowledge, learn, orchestrator


def cli() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("pr-reviewer")

    args = sys.argv[1:]
    mode = args[0] if args else "review"

    cfg = config.load()
    log.info("mode=%s repo=%s pr=%s", mode, cfg.github_repo, cfg.pr_number)

    branch = github_api.StateBranch(
        cfg.github_repo, cfg.state_branch, cfg.state_dir, cfg.github_token,
    )
    branch.setup()

    try:
        with knowledge.connect(branch.db_path) as conn:
            if mode == "review":
                orchestrator.review_pr(cfg, conn)
            elif mode == "learn":
                import json
                import os
                event_path = os.environ.get("GITHUB_EVENT_PATH")
                if not event_path or not os.path.exists(event_path):
                    log.error("learn mode requires GITHUB_EVENT_PATH")
                    return 1
                with open(event_path) as f:
                    event = json.load(f)
                learn.process_comment_event(cfg, conn, event)
            else:
                log.error("Unknown mode: %s (use 'review' or 'learn')", mode)
                return 2
    finally:
        branch.commit_and_push(f"update: state after {mode} on PR #{cfg.pr_number}")

    return 0


if __name__ == "__main__":
    sys.exit(cli())
