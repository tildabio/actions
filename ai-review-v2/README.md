# PR Reviewer v2

LLM-agnostic AI PR reviewer with **codegraph**, **persistent learnings**, and
**incremental review state**. Designed to deliver CodeRabbit-class reviews
without per-seat pricing.

Talks to any OpenAI-compatible `chat/completions` and `/v1/embeddings` endpoint.
Self-hosted via a single GitHub Action. State lives in a SQLite DB persisted
on an orphan branch in your repo — zero external infrastructure.

---

## What's new in v2

| Capability | v1 | v2 |
|---|---|---|
| Multi-pass pipeline (triage / summary / per-file / cross-cut) | ✅ | ✅ |
| Static analyzers as noise filter | ✅ | ✅ |
| Path-based review instructions | — | ✅ (`.review.yaml`) |
| Cross-file caller context (codegraph) | — | ✅ (tree-sitter + ripgrep) |
| Persistent learnings (memory) | — | ✅ (sqlite-vec) |
| Incremental review (don't re-check unchanged files) | — | ✅ |
| Idempotent posting (no duplicate reviews on retries) | — | ✅ |
| Failed-CI log enrichment | — | ✅ |
| Cost telemetry (per-call usage logged to SQLite) | — | ✅ |
| Retry with backoff (tenacity) | — | ✅ |
| Skeletal test coverage | — | ✅ (extend as you go) |

---

## Architecture

```
                    ┌─────────────────────────────────┐
   PR opened   →    │  action.yml (composite)         │
                    │  → python -m src review         │
                    └─────────────────────────────────┘
                                  │
                                  ▼
              ┌───────────────────────────────────────┐
              │  StateBranch.setup()                  │
              │  clones __reviewer_state__ → /tmp/    │
              │  opens state.db (sqlite-vec loaded)   │
              └───────────────────────────────────────┘
                                  │
                                  ▼
   ┌─── orchestrator.review_pr() ────────────────────────────────┐
   │  1. Idempotency check (skip if head_sha already reviewed)   │
   │  2. Incremental filter (only files w/ changed blob_sha)     │
   │  3. Triage           ← cheap model                          │
   │  4. Static analyzers ← ruff/eslint/semgrep/gitleaks/etc.    │
   │  5. Codegraph        ← tree-sitter symbols + rg references  │
   │  6. Learnings        ← embed file → vec MATCH in SQLite     │
   │  7. Summary          ← smart model, 1 call, w/ CI logs      │
   │  8. Per-file review  ← smart model, N parallel              │
   │                        prompt includes:                     │
   │                          - analyzer output                  │
   │                          - codegraph callers                │
   │                          - top-K learnings                  │
   │                          - matched path instructions        │
   │  9. Cross-cut        ← smart model, 1 call, verdict         │
   │ 10. Filter by confidence threshold                          │
   │ 11. Post single GitHub Review (inline + summary)            │
   │ 12. Save state (per-file blob_sha, review_id)               │
   └─────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
              ┌───────────────────────────────────────┐
              │  StateBranch.commit_and_push()        │
              │  pull-rebase loop on conflict (5x)    │
              └───────────────────────────────────────┘
```

Separate workflow (`learn.yml`) listens for `@reviewer` mentions on PR comments
and extracts durable learnings:

```
   PR comment    →   issue_comment / pull_request_review_comment event
   "@reviewer       ↓
    we use early    learn.process_comment_event()
    returns          ├── detect parent review comment
    here, not        ├── LLM: is this a durable team preference?
    nested try"      ├── if yes: embed + insert into learnings + learnings_vec
                     └── reply: "🧠 Learning #42 added"
```

---

## What the codegraph actually does

When file `src/auth/middleware.py` is changed, the codegraph pass:

1. **Parses** the file with tree-sitter, extracting every function, class, and
   method definition with its line range.
2. **Filters** to "touched symbols" — definitions whose body overlaps the diff
   hunks. If you only changed lines 45-50 inside `validate_token()`, only
   `validate_token` is considered touched.
3. **Greps** the rest of the repo (via ripgrep, with the right `--type` filter)
   for call sites of each touched symbol, capturing ±2 lines of context per hit.
4. **Injects** up to 5 caller snippets per symbol into the per-file review
   prompt, under an `EXTERNAL CALLERS OF MODIFIED SYMBOLS:` block.

This is the difference between "your function looks fine" and "your function's
new third parameter is required, but the caller at `api/users.py:88` still
passes two args."

Symbol indices are **cached** in SQLite keyed by `content_sha`, so unchanged
files are never re-parsed.

---

## What the learnings system actually does

1. A reviewer comment fires. A dev replies: `@reviewer we prefer Result<T, E>
   over throwing in this module, see RFC-42`.
2. The `learn.yml` workflow triggers. It fetches the parent review comment
   and sends both to the LLM with `LEARNING_EXTRACT_SYSTEM`.
3. The LLM returns:
   ```json
   {
     "is_learning": true,
     "description": "In src/result/**, prefer Result<T,E> over exception-based control flow because we want explicit error paths per RFC-42.",
     "scope": "repo",
     "file_pattern": "src/result/**"
   }
   ```
4. We embed the description, insert into both `learnings` and `learnings_vec`,
   reply on the PR with `🧠 Learning #42 added`.
5. On the **next** review pass, when reviewing files matching `src/result/**`,
   we embed the file path+content, do a vec MATCH against `learnings_vec`,
   filter results by `file_pattern`, and inject the top-6 into the prompt.

Forgetting: any user can reply `@reviewer forget #42` to deactivate a learning.

---

## Setup

### 1. Publish this as a versioned action

The simplest path: push this directory to a public or private GitHub repo
(e.g. `your-org/pr-review-action-v2`), tag `v2.0.0`, and reference it from
consumer workflows.

### 2. Add the workflows to a target repo

Copy `workflows/review.yml` and `workflows/learn.yml` to `.github/workflows/`
in any repo you want reviewed. Pick a provider and fill in the secrets.

### 3. Set up secrets

You need at minimum:
- `GITHUB_TOKEN` — auto-provided, but requires `contents: write` permission
  in the workflow (already set in the example).
- Your LLM provider's API key, e.g. `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`.
- If using Anthropic as the review model, **also** provide an OpenAI or Voyage
  key for embeddings (Anthropic has no `/v1/embeddings`).

### 4. (Optional) Add `.review.yaml` to the target repo

Copy `.review.yaml.example` to `.review.yaml`, prune what you don't need, and
add path-based instructions for your hot paths (`src/api/**`, `src/db/**`,
etc).

### 5. (Optional) Add `CLAUDE.md` or `AGENTS.md` at the repo root

The orchestrator auto-loads the first of:
`CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `.windsurfrules`,
`CODESTYLE.md`, `STYLEGUIDE.md`, `CONTRIBUTING.md`.

This goes into both the summary and per-file review prompts as
`REPO CONVENTIONS`.

### 6. First run

Open or push a PR. On the first run, the action:
- Creates the `__reviewer_state__` orphan branch (commit: "init: reviewer state")
- Runs the full pipeline (no incremental, no learnings yet)
- Pushes the populated `state.db` back to `__reviewer_state__`

Subsequent runs reuse the state and get faster + smarter.

---

## Provider matrix

| Provider | base_url | model example | embeddings? |
|---|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o`, `gpt-4o-mini` | yes |
| Anthropic | `https://api.anthropic.com/v1` | `claude-opus-4-7` | **no — point embed-* at OpenAI/Voyage** |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | `gemini-2.5-pro` | yes |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` | yes |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` | no |
| OpenRouter | `https://openrouter.ai/api/v1` | many | yes |
| Ollama | `http://localhost:11434/v1` | `qwen2.5-coder:32b`, `nomic-embed-text` | yes |

---

## Operating cost (rough)

Per PR (200 lines, 5 files), single review pass:

| Stage | Tokens in | Tokens out | gpt-4o cost | Sonnet cost | DeepSeek cost |
|---|---|---|---|---|---|
| Triage | 1.5K | 0.2K | $0.005 | $0.005 | $0.0003 |
| Summary | 8K | 1.5K | $0.04 | $0.03 | $0.002 |
| Per-file × 5 | 6K each | 1K each | $0.18 | $0.13 | $0.008 |
| Cross-cut | 4K | 0.8K | $0.02 | $0.016 | $0.001 |
| Embeddings × ~10 | 2K each | — | $0.0004 | (via OpenAI) | (via DeepSeek) |
| **Total** | | | **~$0.25** | **~$0.18** | **~$0.012** |

At 500 PRs/month: **$125/mo (gpt-4o) to $6/mo (DeepSeek)**.

Telemetry is logged to the `llm_calls` table in `state.db` — query it for real
numbers from your traffic:

```sql
SELECT
  stage,
  COUNT(*) calls,
  SUM(prompt_tokens) input_tokens,
  SUM(completion_tokens) output_tokens,
  AVG(duration_ms) avg_ms
FROM llm_calls
WHERE created_at > datetime('now', '-30 days')
GROUP BY stage;
```

---

## Operations

### Backing up state
The state branch IS your backup. To snapshot:
```bash
git clone --branch __reviewer_state__ --depth 1 https://github.com/your-org/repo state-snapshot
```

### Browsing learnings
```bash
git fetch origin __reviewer_state__:__reviewer_state__
git checkout __reviewer_state__
sqlite3 state.db "SELECT id, description, scope, file_pattern, usage_count FROM learnings WHERE active = 1 ORDER BY usage_count DESC LIMIT 20"
```

### Resetting state (start over)
```bash
git push origin --delete __reviewer_state__
```
Next PR run will reinitialize.

### Querying cost
See the SQL snippet above.

### Pausing the reviewer on a specific PR
Add label `skip-review` to the PR. Then edit `orchestrator.review_pr()` to
early-return if `pr['labels']` contains `skip-review` — left as an exercise.

---

## Files

```
action.yml              Composite GitHub Action (entry point)
schema.sql              SQLite schema (versioned)
pyproject.toml          Dependencies + project metadata
.review.yaml.example    Per-repo config template
src/
  __init__.py
  __main__.py           CLI dispatcher: 'review' | 'learn'
  config.py             Env + YAML config loader
  prompts.py            All prompts (the high-leverage file)
  llm.py                OpenAI-compatible client + JSON parsing + embeddings
  github_api.py         gh CLI wrappers, PR data, posting, state branch sync
  analyzers.py          ruff/eslint/semgrep/gitleaks/etc runners
  codegraph.py          Tree-sitter symbols + ripgrep callers
  knowledge.py          SQLite + sqlite-vec: learnings, state, codegraph cache
  orchestrator.py       The 5-pass pipeline
  learn.py              @-mention listener → learnings extraction
workflows/
  review.yml            Triggers on PR events
  learn.yml             Triggers on PR comments
tests/
  test_basics.py        Starter coverage; extend as you iterate
```

---

## v3 roadmap (deferred from v2)

In priority order if you want to keep going:

1. **PR compression for huge diffs.** When the diff exceeds the model's token
   budget, summarize less-important hunks (deletions, generated code, far-from-
   center) and keep critical hunks verbatim. Steal PR-Agent's strategy.
2. **AST-based path instructions.** Beyond globs, match by tree-sitter pattern
   (e.g. "any function decorated with `@route`").
3. **Multi-repo codegraph.** For monorepos with multiple service dirs, share
   a single state branch and let callers cross service boundaries.
4. **Web dashboard.** A tiny static-site generator that reads `state.db` and
   renders the learnings + cost dashboards as a GitHub Pages site.
5. **Slop detection.** Flag AI-generated PRs that look superficially correct
   but skip error handling or duplicate existing logic.
6. **Auto-approve trivia.** When triage says `skip` AND the change matches
   patterns like "version bump", "typo fix", or "dependency-only update",
   post an approval rather than a comment.

---

## Maintenance commitment

You're the maintainer. The prompts in `src/prompts.py` are where you'll spend
most of your tuning time as you see what the model gets wrong on your codebase.
Two practices that pay off:

1. **Weekly:** scan low-confidence findings that got posted anyway. Either
   raise the threshold or add a "DO NOT FLAG" example to the prompt.
2. **Monthly:** browse the `learnings` table. Delete stale ones (deprecated
   patterns, departed teammates' preferences). Active count > 100 with usage
   > 5 each is healthy.
