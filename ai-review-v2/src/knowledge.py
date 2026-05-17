"""Knowledge layer: SQLite-backed learnings, review state, and codegraph cache.

Single SQLite file at $STATE_DIR/state.db, with sqlite-vec loaded for vector
search over learning embeddings. The file is persisted by syncing the parent
directory to the `__reviewer_state__` orphan branch (see github_api.py).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import struct
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pathspec
import sqlite_vec

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


# ============================================================
# CONNECTION
# ============================================================

@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open the state DB with sqlite-vec loaded. Auto-applies schema if missing."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # check_same_thread=False: per-file review pass uses a ThreadPoolExecutor
    # and each worker calls knowledge.log_llm_call on this same connection.
    # SQLite itself serializes writes; we just need to opt out of Python's
    # client-side thread check.
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _ensure_schema(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    schema_sql = Path(__file__).parent.parent / "schema.sql"
    if not schema_sql.exists():
        raise FileNotFoundError(f"schema.sql not found at {schema_sql}")
    conn.executescript(schema_sql.read_text())


# ============================================================
# VECTOR HELPERS
# ============================================================

def _vec_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


# ============================================================
# LEARNINGS
# ============================================================

@dataclass
class Learning:
    id: int | None
    description: str
    scope: str            # 'repo' | 'org' | 'global'
    file_pattern: str | None
    repo: str
    source_pr: int | None
    source_comment: int | None
    created_by: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Learning":
        return cls(
            id=row["id"],
            description=row["description"],
            scope=row["scope"],
            file_pattern=row["file_pattern"],
            repo=row["repo"],
            source_pr=row["source_pr"],
            source_comment=row["source_comment"],
            created_by=row["created_by"],
        )


def insert_learning(
    conn: sqlite3.Connection, learning: Learning, embedding: list[float]
) -> int:
    cur = conn.execute(
        """INSERT INTO learnings
           (description, scope, file_pattern, repo, source_pr, source_comment, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            learning.description,
            learning.scope,
            learning.file_pattern,
            learning.repo,
            learning.source_pr,
            learning.source_comment,
            learning.created_by,
        ),
    )
    learning_id = cur.lastrowid
    conn.execute(
        "INSERT INTO learnings_vec(learning_id, embedding) VALUES (?, ?)",
        (learning_id, _vec_to_blob(embedding)),
    )
    log.info("Inserted learning #%d (scope=%s, repo=%s)", learning_id, learning.scope, learning.repo)
    return learning_id


def search_learnings(
    conn: sqlite3.Connection,
    query_embedding: list[float],
    repo: str,
    *,
    file_path: str | None = None,
    limit: int = 10,
) -> list[Learning]:
    """Return up to `limit` learnings relevant to this repo and (optionally) file.

    Vector similarity search filtered by scope/repo + post-filtered by file_pattern.
    Marks retrieved learnings as used (updates last_used_at, increments usage_count).
    """
    blob = _vec_to_blob(query_embedding)
    rows = conn.execute(
        """
        SELECT l.*, vec.distance
        FROM learnings_vec vec
        JOIN learnings l ON l.id = vec.learning_id
        WHERE vec.embedding MATCH ?
          AND l.active = 1
          AND (l.scope = 'global' OR (l.scope IN ('org', 'repo') AND l.repo = ?))
        ORDER BY vec.distance ASC
        LIMIT ?
        """,
        (blob, repo, limit * 3),  # over-fetch, post-filter by file_pattern
    ).fetchall()

    out: list[Learning] = []
    ids_used: list[int] = []
    for r in rows:
        learning = Learning.from_row(r)
        if learning.file_pattern and file_path:
            spec = pathspec.PathSpec.from_lines("gitwildmatch", [learning.file_pattern])
            if not spec.match_file(file_path):
                continue
        out.append(learning)
        ids_used.append(learning.id)
        if len(out) >= limit:
            break

    if ids_used:
        placeholders = ",".join("?" * len(ids_used))
        conn.execute(
            f"UPDATE learnings SET last_used_at = datetime('now'), "
            f"usage_count = usage_count + 1 WHERE id IN ({placeholders})",
            ids_used,
        )
    return out


def deactivate_learning(conn: sqlite3.Connection, learning_id: int) -> None:
    conn.execute("UPDATE learnings SET active = 0 WHERE id = ?", (learning_id,))


# ============================================================
# REVIEW STATE
# ============================================================

@dataclass
class ReviewState:
    last_head_sha: str
    last_review_id: int | None
    reviewed_files: dict[str, str]  # path -> blob_sha at last review


def get_review_state(conn: sqlite3.Connection, repo: str, pr_number: int) -> ReviewState | None:
    row = conn.execute(
        "SELECT * FROM reviewed_prs WHERE repo = ? AND pr_number = ?",
        (repo, pr_number),
    ).fetchone()
    if not row:
        return None

    files = {
        r["path"]: r["blob_sha"]
        for r in conn.execute(
            "SELECT path, blob_sha FROM reviewed_files WHERE repo = ? AND pr_number = ?",
            (repo, pr_number),
        )
    }
    return ReviewState(
        last_head_sha=row["last_head_sha"],
        last_review_id=row["last_review_id"],
        reviewed_files=files,
    )


def save_review_state(
    conn: sqlite3.Connection,
    repo: str,
    pr_number: int,
    head_sha: str,
    review_id: int | None,
    file_blob_shas: dict[str, str],
    triage_depth: str,
    findings_count: int,
) -> None:
    conn.execute(
        """INSERT INTO reviewed_prs
           (repo, pr_number, last_head_sha, last_review_id, triage_depth, findings_count)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(repo, pr_number) DO UPDATE SET
             last_head_sha = excluded.last_head_sha,
             last_review_id = excluded.last_review_id,
             reviewed_at = datetime('now'),
             triage_depth = excluded.triage_depth,
             findings_count = excluded.findings_count""",
        (repo, pr_number, head_sha, review_id, triage_depth, findings_count),
    )
    # Replace per-file shas wholesale (small, simpler than diffing)
    conn.execute(
        "DELETE FROM reviewed_files WHERE repo = ? AND pr_number = ?",
        (repo, pr_number),
    )
    conn.executemany(
        "INSERT INTO reviewed_files (repo, pr_number, path, blob_sha) VALUES (?, ?, ?, ?)",
        [(repo, pr_number, p, sha) for p, sha in file_blob_shas.items()],
    )


# ============================================================
# CODEGRAPH CACHE
# ============================================================

def get_cached_symbols(
    conn: sqlite3.Connection, repo: str, path: str, content_sha: str
) -> list[dict] | None:
    row = conn.execute(
        "SELECT symbols FROM file_symbols WHERE repo = ? AND path = ? AND content_sha = ?",
        (repo, path, content_sha),
    ).fetchone()
    return json.loads(row["symbols"]) if row else None


def cache_symbols(
    conn: sqlite3.Connection,
    repo: str,
    path: str,
    content_sha: str,
    language: str,
    symbols: list[dict],
) -> None:
    conn.execute(
        """INSERT INTO file_symbols (repo, path, content_sha, language, symbols)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(repo, path) DO UPDATE SET
             content_sha = excluded.content_sha,
             language = excluded.language,
             symbols = excluded.symbols,
             cached_at = datetime('now')""",
        (repo, path, content_sha, language, json.dumps(symbols)),
    )


def content_sha(content: str | bytes) -> str:
    if isinstance(content, str):
        content = content.encode("utf-8", errors="replace")
    return hashlib.sha1(content).hexdigest()


# ============================================================
# COST TELEMETRY
# ============================================================

def log_llm_call(
    conn: sqlite3.Connection,
    repo: str,
    pr_number: int | None,
    stage: str,
    usage: dict,
    succeeded: bool,
    error: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO llm_calls
           (repo, pr_number, stage, model, prompt_tokens, completion_tokens,
            duration_ms, succeeded, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            repo,
            pr_number,
            stage,
            usage.get("model", "?"),
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            usage.get("duration_ms"),
            1 if succeeded else 0,
            error,
        ),
    )
