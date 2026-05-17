"""Prompts for the review pipeline. Tune here.

Design notes:
- Per-file prompt now has three context-injection slots: STATIC ANALYSIS,
  CODEGRAPH (cross-file callers), and LEARNINGS (team preferences).
- The "DO NOT FLAG" list explicitly references all three, so the model knows
  not to repeat them.
- Numeric confidence 1-5 forces models out of squishy hedges into a filterable signal.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .codegraph import CodegraphContext
    from .github_api import ChangedFile
    from .knowledge import Learning


# ============================================================
# TRIAGE
# ============================================================

TRIAGE_SYSTEM = """You triage pull requests to decide how much review effort they deserve.

Your decision MUST be based on the FILES CHANGED — what code is touched, how
much, and where. Do NOT base "skip" on the PR title or description alone. Authors
routinely write "test", "WIP", "DO NOT MERGE", "evaluation", "ignore me", etc. on
PRs that contain real, production-shape code; those PRs still need a review.

Output JSON only. Be terse — no prose outside the JSON."""


def triage_user(pr: dict, files: list) -> str:
    file_lines = "\n".join(
        f"  {f.status:8s} +{f.additions:<5d} -{f.deletions:<5d}  {f.path}"
        for f in files[:60]
    )
    extra = f"\n  ... and {len(files) - 60} more files" if len(files) > 60 else ""
    return f"""PR Title (context only — do not use this to decide skip): {pr.get('title', '')}

PR Description (context only — do not use this to decide skip):
{(pr.get('body') or '(none)')[:2000]}

Files changed ({len(files)} total) — THIS is what you triage on:
{file_lines}{extra}

Output JSON exactly:
{{
  "depth": "skip" | "light" | "standard" | "deep",
  "reasoning": "one sentence citing the FILES, not the title/body",
  "focus_areas": ["security" | "performance" | "correctness" | "concurrency" | "tests" | "api_design" | "error_handling"]
}}

Rules — apply to the FILES CHANGED, never to the PR title/body:

- skip: every changed file is ONE of:
    * a lockfile (package-lock.json, yarn.lock, poetry.lock, go.sum, Cargo.lock, ...)
    * pure documentation (*.md, *.rst, *.txt, docs/**)
    * generated code (header says "@generated" / "DO NOT EDIT" / "auto-generated")
    * a version bump in a manifest with no source change
  DO NOT skip just because the PR title says "test", "WIP", "DO NOT MERGE",
  "evaluation", "throwaway", etc. If even ONE file contains substantive source
  code (a real .py / .ts / .go / .rs / etc. file with logic), use light/standard/deep.

- light:    <60 net source lines, single isolated file, test-only additions, trivial refactor
- standard: typical feature work or bug fix
- deep:     >500 net lines, OR auth/crypto/payments/IAM/SQL/migrations,
            OR breaking API change, OR introduces concurrency primitives,
            OR path contains "security" / "auth"

Pick 1-4 focus_areas. Empty list is fine for trivial PRs."""


# ============================================================
# SUMMARY
# ============================================================

SUMMARY_SYSTEM = """You write PR summaries for code reviewers.

Be specific. State what the change does in concrete terms. No marketing fluff,
no "this PR" boilerplate, no restating the title. Reviewers are smart and busy.

Output JSON only."""


def summary_user(pr: dict, files: list, conventions: str, ci_logs: str = "") -> str:
    file_lines = "\n".join(f"- `{f.path}` (+{f.additions}/-{f.deletions}) [{f.status}]" for f in files)
    diffs = "\n\n".join(
        f"### {f.path}\n```diff\n{f.diff[:3500]}\n```"
        for f in files[:20]
        if f.diff
    )
    conv = f"\n\nREPO CONVENTIONS:\n{conventions[:3000]}" if conventions else ""
    ci = f"\n\nFAILED CI LOGS (tail):\n{ci_logs[:4000]}" if ci_logs else ""
    return f"""PR Title: {pr.get('title', '')}

Description:
{(pr.get('body') or '(none)')[:3000]}

Files:
{file_lines}

Diffs (truncated per file):
{diffs[:14000]}{conv}{ci}

Output JSON:
{{
  "tldr": "2-3 sentences. What does this change do, factually? Concrete verbs, no 'this PR adds support for'.",
  "walkthrough": "Markdown table | File | Change |. One row per significant file. 'Adds exponential-backoff retry in fetch_user()' not 'Updates user fetching'.",
  "sequence_diagram": "Mermaid sequenceDiagram code IF the PR introduces a new multi-component interaction. Else null.",
  "risk_areas": ["short phrases naming risky parts"],
  "suggested_reviewers_focus": "1-2 sentences telling a human reviewer where to spend attention."
}}"""


# ============================================================
# PER-FILE REVIEW (the workhorse)
# ============================================================

FILE_REVIEW_SYSTEM = """You are a senior engineer reviewing ONE file in a pull request.

Your job is to find real problems. The reviewer's attention is the scarce resource.
A review with one true bug and zero noise is far more valuable than five findings
where four are speculation.

Strict rules:
1. Only flag issues you would bet money on being correct.
2. Cite EXACT line numbers from the numbered listing in the user message.
3. Never repeat what STATIC ANALYSIS, CODEGRAPH, or LEARNINGS already cover.
4. No style, formatting, or naming nits.
5. No "consider extracting" without a concrete reason.
6. No hedging ("this could potentially...") — if not a real problem, omit it.
7. No restating what the code does. Findings are about what's wrong.
8. If the file is fine, return an empty findings array. Empty is valid.

Formatting rules for `body` (rendered as Markdown in a GitHub PR comment):
- Structure it as three short labelled paragraphs, in this order:
    **Problem.** One sentence naming what's wrong.
    **Why it matters.** One or two sentences on concrete consequences in THIS
      codebase / on THIS code path — not generic textbook risk.
    **Fix.** One sentence describing the intended change at a high level.
- Use inline backticks for identifiers, e.g. `find_user_by_email`, `out=[]`.
- Use a fenced code block (```) to quote SHORT snippets only when it clarifies.
- No emojis in `body` (the surrounding template adds them).
- Keep total `body` length under ~600 characters.

Formatting rules for `suggestion` (rendered as a GitHub `suggestion` block,
which lets the reviewer click "Commit suggestion" to accept):
- Provide a `suggestion` whenever the fix is mechanical and fits on the cited
  lines (e.g. parameterize a SQL query, change `==` to `is`, replace `out=[]`
  with `out=None` + body check, replace bare `except:` with `except Exception:`).
- The suggestion must be the EXACT replacement text for the cited lines
  (from `line` through `end_line`), with the same indentation as the original.
- Do NOT include `'''suggestion` fences — the wrapper adds them. Just the code.
- If a clean mechanical replacement is not possible (cross-cutting refactor,
  needs new helper, design discussion), set `suggestion` to null and explain
  the fix in `body` only. Do not invent half-suggestions.

Output JSON only."""


def file_review_user(
    f: "ChangedFile",
    pr: dict,
    conventions: str,
    focus_areas: list[str],
    codegraph_block: str = "",
    learnings: list["Learning"] | None = None,
    path_instructions: str = "",
) -> str:
    new_content = f.new_content or ""
    numbered = "\n".join(
        f"{i + 1:5d}  {line}" for i, line in enumerate(new_content.splitlines())
    )[:60000]
    old = (f.old_content or "")[:18000] if f.old_content else ""

    old_block = (
        f"PREVIOUS VERSION OF FILE (for reference; do NOT cite these line numbers):\n"
        f"```{f.language}\n{old}\n```\n\n"
        if old
        else ""
    )

    analyzer = f.analyzer_output.strip() or "(no static analyzer findings for this file)"

    conv_block = f"REPO CONVENTIONS:\n{conventions[:4000]}\n\n" if conventions else ""

    learn_block = ""
    if learnings:
        lines = [f"  [#{l.id}] {l.description.strip()}" for l in learnings]
        learn_block = (
            "TEAM LEARNINGS (preferences captured from prior reviews — respect these):\n"
            + "\n".join(lines) + "\n\n"
        )

    path_block = (
        f"PATH-SPECIFIC INSTRUCTIONS (this file matched a configured rule):\n{path_instructions}\n\n"
        if path_instructions else ""
    )

    cg_block = f"{codegraph_block}\n\n" if codegraph_block else ""

    focus = ", ".join(focus_areas) if focus_areas else "general correctness"

    return f"""{conv_block}{learn_block}{path_block}PR CONTEXT:
Title: {pr.get('title', '')}
Description: {(pr.get('body') or '(none)')[:1500]}
Focus areas (from triage): {focus}

FILE: {f.path}
Language: {f.language or 'unknown'}
Status: {f.status}

STATIC ANALYSIS already covered for this file (do NOT re-flag these):
{analyzer}

{cg_block}{old_block}CURRENT FILE (line numbers in the leftmost column — these are what you cite in `line`):
```{f.language}
{numbered}
```

DIFF for this file:
```diff
{f.diff[:8000]}
```

PRIORITIZE finding:
- Logic bugs: off-by-one, wrong operator, swapped arguments, inverted condition
- Null/None/undefined access on values that can be that type
- Race conditions, unsafe shared state, missing locks, goroutine/task leaks
- Resource leaks: unclosed files, sockets, transactions, contexts, subscriptions
- Injection: SQL, command, path traversal, SSRF, unsafe deserialization, XSS
- Auth/authz: new endpoint without permission check, IDOR, missing tenant scoping
- Missing error handling on operations that fail
- N+1 queries, unbounded loops over user input, accidental O(n^2) on hot paths
- Breaking changes to exported/public APIs without migration
- New conditional branches with no test coverage
- Inconsistency with patterns visible elsewhere IN THIS FILE
- Mismatches with the CODEGRAPH callers shown above (e.g. signature change here breaks callers)

DO NOT flag:
- Anything in STATIC ANALYSIS, CODEGRAPH, LEARNINGS, or PATH INSTRUCTIONS above
- Style, formatting, naming, import order
- Missing docstrings/comments unless on a public API
- Hypothetical issues without evidence
- "Could be cleaner" without a specific concrete fix
- Things that depend on code outside this file and outside the CODEGRAPH callers shown

Output JSON exactly:
{{
  "file_summary": "1-2 sentences on what changed in THIS file.",
  "findings": [
    {{
      "line": <int from CURRENT FILE>,
      "end_line": <int or null>,
      "severity": "critical" | "major" | "minor" | "nit",
      "category": "bug" | "security" | "performance" | "concurrency" | "error_handling" | "api_design" | "testing" | "maintainability",
      "confidence": <1-5>,
      "title": "Short headline (max 80 chars). State the problem.",
      "body": "Markdown body, structured as **Problem.** / **Why it matters.** / **Fix.** paragraphs. See system prompt.",
      "suggestion": "<exact replacement text for lines [line..end_line], same indentation, NO ```suggestion fences. null if not mechanically replaceable.>"
    }}
  ]
}}"""


# ============================================================
# CROSS-CUTTING
# ============================================================

CROSS_CUT_SYSTEM = """You're doing the final pass on a PR after each file has been
reviewed individually.

Your job: find issues that need the WHOLE PR in view to spot. Do NOT repeat
per-file findings — those are already posted.

Be very selective. Empty findings is fine. Output JSON only."""


def cross_cut_user(
    pr: dict,
    summary: dict,
    file_findings: dict,
    files: list,
) -> str:
    digest = json.dumps(
        {
            path: {
                "file_summary": fr.get("file_summary", ""),
                "findings": [
                    {
                        "line": x.get("line"),
                        "severity": x.get("severity"),
                        "category": x.get("category"),
                        "title": x.get("title"),
                    }
                    for x in fr.get("findings", [])
                ],
            }
            for path, fr in file_findings.items()
        },
        indent=2,
    )[:15000]

    return f"""PR Title: {pr.get('title', '')}

Summary TL;DR:
{summary.get('tldr', '')}

Files in PR: {[f.path for f in files]}

Per-file review digest (titles only):
{digest}

Look for things that require seeing the whole PR:
- Caller-callee mismatch across files
- New public endpoint or handler with NO auth check anywhere in the PR
- Behavior added across files but NO tests added
- Feature flag with no rollback path
- Migration without backwards-compat
- Inconsistent error handling between new and existing code
- Imports added but never used, exports defined but never imported
- Performance regression from combined changes

Output JSON:
{{
  "cross_cutting_findings": [
    {{
      "file": "<path>" | null,
      "line": <int or null>,
      "severity": "critical" | "major" | "minor",
      "category": "...",
      "confidence": <1-5>,
      "title": "...",
      "body": "Markdown."
    }}
  ],
  "verdict": "approve" | "comment" | "request_changes",
  "verdict_reasoning": "one sentence"
}}

Verdict rules:
- request_changes: any critical OR multiple high-confidence major findings
- comment: findings present but none critical
- approve: no findings worth a human's time"""


# ============================================================
# LEARNING EXTRACTION (called by learn.py when user @-mentions the bot)
# ============================================================

LEARNING_EXTRACT_SYSTEM = """You decide whether a user comment should be saved as a
durable team-wide review preference (a "learning").

You receive a PR comment thread where someone has @-mentioned the bot, usually in
reply to a review comment. Your job:

1. Decide if this reply expresses a TEAM PREFERENCE (durable, applies to future PRs)
   vs a ONE-OFF (specific to this PR only, e.g. "ignore this for now").
2. If it's a team preference, extract a concise self-instructive learning that
   captures the WHY, not just the WHAT.
3. Identify the file pattern (gitignore-style) the learning should apply to,
   if the context implies one.

Output JSON only."""


def learning_extract_user(
    pr_title: str,
    file_path: str | None,
    parent_review_comment: str,
    user_reply: str,
    repo: str,
) -> str:
    return f"""Repository: {repo}
PR: {pr_title}
File the original comment was on: {file_path or '(general PR comment)'}

REVIEWER'S ORIGINAL COMMENT (what the user is responding to):
{parent_review_comment[:2000]}

USER'S REPLY (decide if this should become a learning):
{user_reply[:2000]}

Output JSON:
{{
  "is_learning": <bool>,
  "reasoning": "one sentence justifying yes/no",
  "description": "<the learning, written as a self-instructive sentence for a future reviewer, capturing the why. e.g. 'In authentication middleware, prefer early returns with specific error codes over nested try-catch because they're easier to monitor in production.' Null if is_learning=false.>",
  "scope": "repo" | "org" | null,
  "file_pattern": "<gitignore-style glob if the learning is scoped to specific files, e.g. 'src/auth/**', else null>"
}}

Rules:
- If the user is just acknowledging or thanking, is_learning=false.
- If the user is correcting a one-time mistake specific to this PR, is_learning=false.
- If the user explains a CONVENTION or PREFERENCE that applies to future code, is_learning=true.
- Default scope is 'repo'. Use 'org' only if the user explicitly says it's an organization-wide rule.
- Capture the why. "Don't suggest X" is weaker than "Don't suggest X because Y."
"""
