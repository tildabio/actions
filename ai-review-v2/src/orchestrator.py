"""Review orchestrator: the 5-pass pipeline.

  1. Triage      → depth + focus areas        (cheap model)
  2. Analyzers   → ruff/eslint/semgrep/etc    (no LLM)
  3. Codegraph   → cross-file callers         (tree-sitter + ripgrep)
  4. Summary     → TL;DR + walkthrough        (smart model, 1 call)
  5. Per-file    → deep review per file        (smart model, N parallel calls)
                   — pulls path instructions and relevant learnings into prompt
  6. Cross-cut   → PR-level issues + verdict   (smart model, 1 call)
  7. Post        → single GitHub Review

Side effects: writes telemetry to SQLite, updates review state for incremental
re-review on subsequent pushes.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import sqlite3
from pathlib import Path

import pathspec

from . import analyzers, codegraph, github_api, knowledge, llm, prompts
from .config import Config
from .github_api import ChangedFile

log = logging.getLogger(__name__)


SEVERITY_EMOJI = {"critical": "🔴", "major": "🟠", "minor": "🟡", "nit": "💭"}

# Visible + hidden v2 branding so reviewers (and grep) can never confuse v2
# output with v1's. Every comment/review v2 posts carries both.
V2_BADGE = "🧪 **PR Reviewer v2**"
V2_MARKER = "<!-- pr-reviewer-v2 -->"


def review_pr(cfg: Config, conn: sqlite3.Connection) -> None:
    log.info("Reviewing PR #%d in %s", cfg.pr_number, cfg.github_repo)

    # --- Idempotency check ---
    prior = knowledge.get_review_state(conn, cfg.github_repo, cfg.pr_number)
    if prior and prior.last_head_sha == cfg.head_sha:
        log.info("Already reviewed %s at this head SHA; skipping", cfg.head_sha)
        return

    # --- Context gathering ---
    pr = github_api.get_pr_metadata(cfg.github_repo, cfg.pr_number)
    all_files = github_api.get_changed_files(cfg.github_repo, cfg.pr_number)
    files = [f for f in all_files if not _should_skip(f.path, cfg.skip_patterns)][:cfg.max_files]
    log.info("%d files in scope (%d filtered)", len(files), len(all_files) - len(files))
    if not files:
        github_api.post_pr_comment(cfg.github_repo, cfg.pr_number, "_No reviewable files in this PR._")
        return

    github_api.fetch_file_contents(files, cfg.base_sha, cfg.head_sha)
    conventions = _load_repo_conventions()

    # --- Incremental: filter to files whose content actually changed since last review ---
    files_for_deep_review = files
    if cfg.enable_incremental and prior:
        files_for_deep_review = [
            f for f in files
            if prior.reviewed_files.get(f.path) != f.blob_sha_new
        ]
        if not files_for_deep_review:
            log.info("All files unchanged since last review; updating state only")
            _update_state(conn, cfg, files, prior.last_review_id, "skip", 0)
            return
        log.info("Incremental: %d/%d files changed since last review",
                 len(files_for_deep_review), len(files))

    # === Pass 1: Triage ===
    triage_client = llm.make_client(cfg.triage)
    log.info("[triage] model=%s", cfg.triage.model)
    try:
        triage, usage = llm.chat(
            triage_client, cfg.triage.model,
            prompts.TRIAGE_SYSTEM, prompts.triage_user(pr, files),
        )
        knowledge.log_llm_call(conn, cfg.github_repo, cfg.pr_number, "triage", usage, True)
    except Exception as e:
        log.exception("triage failed; defaulting to 'standard'")
        knowledge.log_llm_call(conn, cfg.github_repo, cfg.pr_number, "triage",
                               {"model": cfg.triage.model}, False, str(e))
        triage = {"depth": "standard", "focus_areas": [], "reasoning": "triage error"}

    depth = triage.get("depth", "standard")
    focus_areas = triage.get("focus_areas") or []
    log.info("triage → depth=%s focus=%s", depth, focus_areas)

    # Operator override: FORCE_DEPTH env var beats the triage result.
    # Useful for evaluation runs, high-risk PRs, or debugging.
    force_depth = (os.environ.get("FORCE_DEPTH") or "").strip().lower()
    if force_depth in {"light", "standard", "deep"}:
        if force_depth != depth:
            log.info("FORCE_DEPTH=%s overriding triage depth=%s", force_depth, depth)
        depth = force_depth

    if depth == "skip":
        github_api.post_pr_comment(
            cfg.github_repo, cfg.pr_number,
            f"{V2_MARKER}\n{V2_BADGE} — _skipped by triage_\n\n"
            f"> {triage.get('reasoning', '(no reason given)')}\n\n"
            f"<sub>Triage skips only when the diff itself is trivial "
            f"(lockfile bump / docs-only / generated). Push a substantive "
            f"commit to re-engage, or set `force-depth` to override.</sub>",
        )
        # Record head_sha for idempotency but DO NOT mark per-file blob_shas as
        # "reviewed" — they weren't. If we did, a later FORCE_DEPTH run (or a
        # config change that flips the same diff into a real review) would
        # filter every file out as "already reviewed since last time".
        _update_state(conn, cfg, [], prior.last_review_id if prior else None, "skip", 0)
        return

    # === Pass 2: Static analyzers ===
    if cfg.enable_analyzers and depth in ("standard", "deep"):
        log.info("[analyzers]")
        analyzers.run_all(files)

    # === Pass 3: Codegraph ===
    codegraph_blocks: dict[str, str] = {}
    if cfg.enable_codegraph and depth in ("standard", "deep"):
        log.info("[codegraph]")
        for f in files_for_deep_review:
            if not f.new_content or not f.language:
                continue
            try:
                ctx = codegraph.build_context_for_file(
                    conn,
                    repo=cfg.github_repo,
                    path=f.path,
                    content=f.new_content,
                    language=f.language,
                    diff_line_numbers=f.diff_line_numbers,
                    repo_root=".",
                )
                block = ctx.render()
                if block:
                    codegraph_blocks[f.path] = block
            except Exception:
                log.exception("codegraph failed for %s", f.path)
        log.info("codegraph: callers found for %d/%d files",
                 len(codegraph_blocks), len(files_for_deep_review))

    # === Learnings retrieval (per file, via embedding) ===
    learnings_by_file: dict[str, list] = {}
    if cfg.enable_learnings:
        embed_client = llm.make_client(cfg.embedding)
        # Query = file path + first 2KB of new content. Cheap to embed; good signal.
        try:
            queries = []
            for f in files_for_deep_review:
                q = f.path + "\n\n" + (f.new_content or "")[:2000]
                queries.append(q)
            if queries:
                vectors = llm.embed(embed_client, cfg.embedding.model, queries)
                for f, vec in zip(files_for_deep_review, vectors, strict=False):
                    learnings_by_file[f.path] = knowledge.search_learnings(
                        conn, vec, repo=cfg.github_repo, file_path=f.path, limit=6
                    )
                total = sum(len(v) for v in learnings_by_file.values())
                log.info("learnings retrieved: %d total across files", total)
        except Exception:
            log.exception("learnings retrieval failed; continuing without")

    # === Pass 4: Summary ===
    review_client = llm.make_client(cfg.review)
    ci_logs = github_api.get_failed_workflow_logs(cfg.github_repo, cfg.head_sha) if depth in ("standard", "deep") else ""

    log.info("[summary] model=%s", cfg.review.model)
    try:
        summary, usage = llm.chat(
            review_client, cfg.review.model,
            prompts.SUMMARY_SYSTEM,
            prompts.summary_user(pr, files, conventions, ci_logs),
        )
        knowledge.log_llm_call(conn, cfg.github_repo, cfg.pr_number, "summary", usage, True)
    except Exception as e:
        log.exception("summary failed")
        knowledge.log_llm_call(conn, cfg.github_repo, cfg.pr_number, "summary",
                               {"model": cfg.review.model}, False, str(e))
        summary = {"tldr": "(summary generation failed)", "walkthrough": "",
                   "sequence_diagram": None, "risk_areas": [],
                   "suggested_reviewers_focus": ""}

    # === Pass 5: Per-file review (parallel) ===
    reviewable = [
        f for f in files_for_deep_review
        if f.new_content
        and len(f.new_content) <= cfg.max_file_bytes
        and not _is_generated(f.new_content)
        and f.status != "removed"
    ]
    log.info("[per-file] %d files, parallel=%d", len(reviewable), cfg.parallel_file_reviews)

    def review_one(f: ChangedFile) -> tuple[str, dict]:
        try:
            path_instr = _match_path_instructions(f.path, cfg)
            user = prompts.file_review_user(
                f, pr, conventions, focus_areas,
                codegraph_block=codegraph_blocks.get(f.path, ""),
                learnings=learnings_by_file.get(f.path),
                path_instructions=path_instr,
            )
            result, usage = llm.chat(
                review_client, cfg.review.model,
                prompts.FILE_REVIEW_SYSTEM, user,
            )
            knowledge.log_llm_call(conn, cfg.github_repo, cfg.pr_number, "file_review", usage, True)
            return f.path, result
        except Exception as e:
            log.exception("file review failed: %s", f.path)
            knowledge.log_llm_call(conn, cfg.github_repo, cfg.pr_number, "file_review",
                                   {"model": cfg.review.model}, False, str(e))
            return f.path, {"file_summary": "", "findings": []}

    file_findings: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.parallel_file_reviews) as ex:
        for path, result in ex.map(review_one, reviewable):
            file_findings[path] = result
            n = len(result.get("findings", []))
            if n:
                log.info("  %s: %d findings", path, n)

    # === Pass 6: Cross-cutting ===
    log.info("[cross-cut]")
    try:
        cross, usage = llm.chat(
            review_client, cfg.review.model,
            prompts.CROSS_CUT_SYSTEM,
            prompts.cross_cut_user(pr, summary, file_findings, files),
        )
        knowledge.log_llm_call(conn, cfg.github_repo, cfg.pr_number, "cross_cut", usage, True)
    except Exception as e:
        log.exception("cross-cut failed")
        knowledge.log_llm_call(conn, cfg.github_repo, cfg.pr_number, "cross_cut",
                               {"model": cfg.review.model}, False, str(e))
        cross = {"cross_cutting_findings": [], "verdict": "comment",
                 "verdict_reasoning": "cross-cut error"}

    # === Filter by confidence ===
    for fr in file_findings.values():
        fr["findings"] = [
            x for x in fr.get("findings", [])
            if int(x.get("confidence", 0) or 0) >= cfg.confidence_threshold
        ]
    cross["cross_cutting_findings"] = [
        x for x in cross.get("cross_cutting_findings", [])
        if int(x.get("confidence", 0) or 0) >= cfg.confidence_threshold
    ]

    # === Pass 7: Post ===
    review_id = _post_review(cfg, pr, summary, file_findings, cross, files)
    total_findings = sum(len(fr.get("findings", [])) for fr in file_findings.values())
    _update_state(conn, cfg, files, review_id, depth, total_findings)
    log.info("done. %d findings posted. review_id=%s", total_findings, review_id)


# ============================================================
# HELPERS
# ============================================================

def _should_skip(path: str, patterns: list[str]) -> bool:
    spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)
    return spec.match_file(path)


def _is_generated(content: str) -> bool:
    head = content[:500].lower()
    return any(m in head for m in (
        "@generated", "do not edit", "auto-generated", "code generated by",
    ))


def _load_repo_conventions() -> str:
    for name in ("CLAUDE.md", "AGENTS.md", ".cursorrules", ".windsurfrules",
                 "CODESTYLE.md", "STYLEGUIDE.md", "CONTRIBUTING.md"):
        p = Path(name)
        if p.exists() and p.is_file():
            try:
                return p.read_text()[:8000]
            except Exception:
                continue
    return ""


def _match_path_instructions(path: str, cfg: Config) -> str:
    out = []
    for pi in cfg.path_instructions:
        spec = pathspec.PathSpec.from_lines("gitwildmatch", [pi.pattern])
        if spec.match_file(path):
            out.append(pi.instructions.strip())
    return "\n\n".join(out)


def _update_state(
    conn: sqlite3.Connection,
    cfg: Config,
    files: list[ChangedFile],
    review_id: int | None,
    depth: str,
    findings_count: int,
) -> None:
    blob_shas = {f.path: f.blob_sha_new for f in files if f.blob_sha_new}
    knowledge.save_review_state(
        conn, cfg.github_repo, cfg.pr_number,
        cfg.head_sha, review_id, blob_shas, depth, findings_count,
    )


# ============================================================
# POSTING
# ============================================================

def _post_review(
    cfg: Config,
    pr: dict,
    summary: dict,
    file_findings: dict,
    cross: dict,
    files: list[ChangedFile],
) -> int | None:
    diff_lines = {f.path: f.diff_line_numbers for f in files}

    inline: list[dict] = []
    dropped: list[tuple[str, dict]] = []

    for path, fr in file_findings.items():
        for x in fr.get("findings", []):
            line = x.get("line")
            if not isinstance(line, int):
                continue
            if line in diff_lines.get(path, set()):
                inline.append({"path": path, "line": line, "side": "RIGHT",
                               "body": _format_finding(x)})
            else:
                dropped.append((path, x))

    for x in cross.get("cross_cutting_findings", []):
        path, line = x.get("file"), x.get("line")
        if path and isinstance(line, int) and line in diff_lines.get(path, set()):
            inline.append({"path": path, "line": line, "side": "RIGHT",
                           "body": _format_finding(x, prefix="🔀 Cross-cutting: ")})

    summary_md = _build_summary_md(summary, file_findings, cross, dropped)
    event = {
        "approve": "APPROVE",
        "comment": "COMMENT",
        "request_changes": "REQUEST_CHANGES",
    }.get(cross.get("verdict", "comment"), "COMMENT")

    return github_api.post_review(
        cfg.github_repo, cfg.pr_number, cfg.head_sha,
        body=summary_md, inline_comments=inline, event=event,
    )


def _format_finding(f: dict, prefix: str = "") -> str:
    """Render an inline finding with clear sections and a one-click suggestion.

    Output structure:
        <hidden v2 marker>
        🧪 **PR Reviewer v2** · 🔴 **CRITICAL · security** · confidence 5/5
        ### {prefix}Title

        {body}                          ← markdown allowed: bold, code, bullets

        **Suggested fix**
        ```suggestion
        {code GitHub will offer as one-click "Commit suggestion"}
        ```
    """
    sev = (f.get("severity") or "").lower()
    cat = (f.get("category") or "").lower()
    conf = f.get("confidence", "?")
    sev_emoji = SEVERITY_EMOJI.get(sev, "")
    title = (f.get("title") or "").strip()
    body = (f.get("body") or "").strip()

    header = (
        f"{V2_MARKER}\n"
        f"{V2_BADGE} · {sev_emoji} **{sev.upper() or 'FINDING'}"
        f"{' · ' + cat if cat else ''}** · confidence {conf}/5"
    )
    parts: list[str] = [header]
    if title:
        parts.append(f"### {prefix}{title}")
    if body:
        parts.append(body)

    sug = f.get("suggestion")
    if isinstance(sug, str) and sug.strip():
        sug = sug.strip()
        # If the model already wrapped it in a ```suggestion fence, keep as-is.
        # Otherwise wrap so GitHub renders the one-click "Commit suggestion" UI.
        already_fenced = sug.startswith("```suggestion")
        suggestion_block = sug if already_fenced else f"```suggestion\n{sug}\n```"
        parts.append("**Suggested fix** _(click \"Commit suggestion\" to accept)_")
        parts.append(suggestion_block)

    return "\n\n".join(parts)


def _strip_code_fence(text: str) -> str:
    """Strip an outer ```lang ... ``` fence if the LLM included one.

    Models sometimes return `sequence_diagram` already wrapped in a
    ```mermaid fence. Our caller then wraps it AGAIN, producing
    nested fences that GitHub renders as plain text instead of a
    rendered mermaid diagram. Normalize to plain inner content.
    """
    s = text.strip()
    if not s.startswith("```"):
        return s
    # Drop the opening fence line (e.g. ```mermaid or ```)
    first_newline = s.find("\n")
    if first_newline == -1:
        return s  # malformed; leave it
    s = s[first_newline + 1 :]
    # Drop a trailing fence if present
    if s.rstrip().endswith("```"):
        s = s.rstrip()[: -len("```")]
    return s.rstrip()


def _build_summary_md(summary: dict, file_findings: dict, cross: dict,
                      dropped: list[tuple[str, dict]]) -> str:
    total = sum(len(fr.get("findings", [])) for fr in file_findings.values())
    cross_list = cross.get("cross_cutting_findings", [])

    out = [
        V2_MARKER,
        "## 🧪 PR Reviewer v2",
        "_LLM-agnostic reviewer with codegraph, learnings, and incremental "
        "state. Distinct from the existing v1 `ai-review` action._",
        "",
    ]
    if summary.get("tldr"):
        out += [f"**TL;DR.** {summary['tldr']}", ""]
    if summary.get("walkthrough"):
        out += ["### Walkthrough", summary["walkthrough"], ""]
    if summary.get("sequence_diagram"):
        out += ["### Flow", "```mermaid", _strip_code_fence(summary["sequence_diagram"]), "```", ""]
    if summary.get("suggested_reviewers_focus"):
        out += ["### Reviewer focus", summary["suggested_reviewers_focus"], ""]

    out.append(f"### Findings  ·  {total} inline  ·  {len(cross_list)} cross-cutting")
    verdict = cross.get("verdict", "comment")
    verdict_emoji = {"approve": "✅", "comment": "💬", "request_changes": "🛑"}.get(verdict, "")
    out += [f"{verdict_emoji} **Verdict: `{verdict}`** — {cross.get('verdict_reasoning', '')}", ""]

    if cross_list:
        out.append("**Cross-cutting findings:**")
        for f in cross_list:
            emoji = SEVERITY_EMOJI.get(f.get("severity", ""), "")
            loc = f"`{f['file']}`" if f.get("file") else "_PR-level_"
            out.append(f"- {emoji} {loc} — **{f.get('title', '')}** _(conf {f.get('confidence', '?')}/5)_")
            body = (f.get("body") or "").replace("\n", " ")[:400]
            if body:
                out.append(f"  > {body}")
        out.append("")

    if dropped:
        out.append("### Findings outside diff context")
        for path, x in dropped[:20]:
            emoji = SEVERITY_EMOJI.get(x.get("severity", ""), "")
            out.append(f"- {emoji} `{path}` L{x.get('line')} — **{x.get('title', '')}**")
        out.append("")

    out.append(
        "<sub>Reply with `@reviewer ...` on any inline comment to teach the bot a "
        "team preference. Findings below confidence 3 are filtered.</sub>"
    )
    return "\n".join(out)
