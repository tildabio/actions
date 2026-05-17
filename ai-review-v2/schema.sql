-- pr-reviewer state schema. Lives on the __reviewer_state__ orphan branch.
--
-- All tables are explicitly versioned. The `schema_version` table tracks the
-- current migration level so future versions can ALTER safely.
--
-- The vec0 virtual table requires the sqlite-vec extension loaded at connect time
-- (see knowledge.py::connect).

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS schema_version (
    version  INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO schema_version (version) VALUES (1);

-- ============================================================
-- LEARNINGS
-- Natural-language preferences extracted from @-mention replies on PR comments.
-- Loaded into review prompts via vector similarity at review time.
-- ============================================================

CREATE TABLE IF NOT EXISTS learnings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    description     TEXT    NOT NULL,             -- the learning text itself
    scope           TEXT    NOT NULL DEFAULT 'repo',  -- 'repo' | 'org' | 'global'
    file_pattern    TEXT,                         -- optional gitignore-style glob
    repo            TEXT    NOT NULL,             -- owner/name
    source_pr       INTEGER,                      -- PR number that produced this
    source_comment  INTEGER,                      -- GitHub comment id
    created_by      TEXT    NOT NULL,             -- GitHub login
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_used_at    TEXT,
    usage_count     INTEGER NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1    -- soft-delete via UPDATE
);

CREATE INDEX IF NOT EXISTS idx_learnings_repo ON learnings(repo, active);
CREATE INDEX IF NOT EXISTS idx_learnings_scope ON learnings(scope, active);

-- Vector index (sqlite-vec). Dimension matches the configured embedding model.
-- 1536 is text-embedding-3-small; override in knowledge.py if you switch models.
CREATE VIRTUAL TABLE IF NOT EXISTS learnings_vec USING vec0(
    learning_id INTEGER PRIMARY KEY,
    embedding   FLOAT[1536]
);

-- ============================================================
-- REVIEW STATE
-- Per-PR record of what we've already reviewed, so re-pushes don't re-flag everything.
-- ============================================================

CREATE TABLE IF NOT EXISTS reviewed_prs (
    repo            TEXT    NOT NULL,
    pr_number       INTEGER NOT NULL,
    last_head_sha   TEXT    NOT NULL,
    last_review_id  INTEGER,                       -- GitHub review id (idempotency)
    reviewed_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    triage_depth    TEXT,                          -- skip|light|standard|deep
    findings_count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, pr_number)
);

-- Per-file SHA hash at last review. On re-push, we diff against this to
-- find which files actually changed since we last looked.
CREATE TABLE IF NOT EXISTS reviewed_files (
    repo        TEXT    NOT NULL,
    pr_number   INTEGER NOT NULL,
    path        TEXT    NOT NULL,
    blob_sha    TEXT    NOT NULL,                  -- git blob sha at last review
    reviewed_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (repo, pr_number, path),
    FOREIGN KEY (repo, pr_number) REFERENCES reviewed_prs(repo, pr_number) ON DELETE CASCADE
);

-- ============================================================
-- CODEGRAPH CACHE
-- File-level symbol index. Re-parsed only when the file's content_sha changes.
-- ============================================================

CREATE TABLE IF NOT EXISTS file_symbols (
    repo        TEXT NOT NULL,
    path        TEXT NOT NULL,
    content_sha TEXT NOT NULL,
    language    TEXT NOT NULL,
    -- JSON array of {name, kind, start_line, end_line, signature}
    symbols     TEXT NOT NULL,
    cached_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (repo, path)
);

CREATE INDEX IF NOT EXISTS idx_file_symbols_lang ON file_symbols(repo, language);

-- ============================================================
-- COST / OBSERVABILITY (lightweight)
-- ============================================================

CREATE TABLE IF NOT EXISTS llm_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repo            TEXT NOT NULL,
    pr_number       INTEGER,
    stage           TEXT NOT NULL,            -- triage|summary|file_review|cross_cut|learning_extract
    model           TEXT NOT NULL,
    prompt_tokens   INTEGER,
    completion_tokens INTEGER,
    duration_ms     INTEGER,
    succeeded       INTEGER NOT NULL,
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_pr ON llm_calls(repo, pr_number, created_at);
