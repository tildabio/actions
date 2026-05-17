"""Codegraph: tree-sitter symbol extraction + ripgrep-based reference finding.

Two responsibilities:
  1. For each changed file, extract function/class/method definitions via tree-sitter.
     Symbol index is cached in SQLite keyed by content_sha.
  2. For each *touched* symbol (i.e. a definition that overlaps the diff hunks),
     find call sites in OTHER files via ripgrep + tree-sitter validation.

The output is a per-changed-file "callers context block" that gets injected into
the review prompt. This is the single biggest quality unlock over diff-only review.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter_languages import get_language, get_parser

from . import knowledge

log = logging.getLogger(__name__)


# Languages we support. Adding a new one: pick a tree-sitter grammar name and
# write a query that captures function/class definitions.
SUPPORTED_LANGUAGES: dict[str, str] = {
    "python":     "python",
    "javascript": "javascript",
    "typescript": "typescript",
    "tsx":        "tsx",
    "go":         "go",
    "rust":       "rust",
    "java":       "java",
    "ruby":       "ruby",
}

# Tree-sitter queries: capture the name of definitions per language.
# We deliberately keep these minimal — name + range is what we need.
_QUERIES: dict[str, str] = {
    "python": """
        (function_definition name: (identifier) @name)
        (class_definition name: (identifier) @name)
    """,
    "javascript": """
        (function_declaration name: (identifier) @name)
        (method_definition name: (property_identifier) @name)
        (class_declaration name: (identifier) @name)
        (variable_declarator name: (identifier) @name value: (arrow_function))
        (variable_declarator name: (identifier) @name value: (function))
    """,
    "typescript": """
        (function_declaration name: (identifier) @name)
        (method_definition name: (property_identifier) @name)
        (class_declaration name: (type_identifier) @name)
        (interface_declaration name: (type_identifier) @name)
        (type_alias_declaration name: (type_identifier) @name)
    """,
    "tsx": """
        (function_declaration name: (identifier) @name)
        (method_definition name: (property_identifier) @name)
        (class_declaration name: (type_identifier) @name)
    """,
    "go": """
        (function_declaration name: (identifier) @name)
        (method_declaration name: (field_identifier) @name)
        (type_declaration (type_spec name: (type_identifier) @name))
    """,
    "rust": """
        (function_item name: (identifier) @name)
        (struct_item name: (type_identifier) @name)
        (impl_item type: (type_identifier) @name)
        (trait_item name: (type_identifier) @name)
    """,
    "java": """
        (method_declaration name: (identifier) @name)
        (class_declaration name: (identifier) @name)
        (interface_declaration name: (identifier) @name)
    """,
    "ruby": """
        (method name: (identifier) @name)
        (class name: (constant) @name)
        (module name: (constant) @name)
    """,
}


@dataclass
class Symbol:
    name: str
    kind: str          # 'function' | 'class' | 'method' (we infer from query node type)
    start_line: int    # 1-based
    end_line: int
    signature: str     # the first line of the definition, trimmed


@dataclass
class CallerSnippet:
    file: str
    line: int
    context: str       # ±3 lines around the call


@dataclass
class CodegraphContext:
    """What gets injected into the per-file review prompt for one file."""
    touched_symbols: list[Symbol] = field(default_factory=list)
    callers_by_symbol: dict[str, list[CallerSnippet]] = field(default_factory=dict)

    def render(self) -> str:
        if not self.touched_symbols:
            return ""
        parts = ["EXTERNAL CALLERS OF MODIFIED SYMBOLS:"]
        for sym in self.touched_symbols:
            callers = self.callers_by_symbol.get(sym.name, [])
            if not callers:
                continue
            parts.append(f"\n  {sym.kind} {sym.name} (L{sym.start_line}-{sym.end_line})")
            for c in callers[:5]:
                snippet = "\n      ".join(c.context.splitlines())
                parts.append(f"    called at {c.file}:{c.line}")
                parts.append(f"      {snippet}")
        return "\n".join(parts) if len(parts) > 1 else ""


# ============================================================
# SYMBOL EXTRACTION
# ============================================================

def extract_symbols(content: str, language: str) -> list[Symbol]:
    if language not in SUPPORTED_LANGUAGES:
        return []
    ts_lang = SUPPORTED_LANGUAGES[language]
    if ts_lang not in _QUERIES:
        return []
    try:
        parser = get_parser(ts_lang)
        lang = get_language(ts_lang)
        tree = parser.parse(content.encode("utf-8", errors="replace"))
    except Exception as e:
        log.debug("tree-sitter parse failed for language=%s: %s", language, e)
        return []

    try:
        query = lang.query(_QUERIES[ts_lang])
        captures = query.captures(tree.root_node)
    except Exception as e:
        log.debug("tree-sitter query failed for language=%s: %s", language, e)
        return []

    symbols: list[Symbol] = []
    lines = content.splitlines()
    for node, _capture_name in captures:
        # The captured node is the @name identifier; its parent is the definition.
        parent = node.parent
        if parent is None:
            continue
        start_line = parent.start_point[0] + 1
        end_line = parent.end_point[0] + 1
        try:
            name = node.text.decode("utf-8", errors="replace")
        except Exception:
            continue
        # First line, trimmed, as a usable signature
        signature = lines[start_line - 1].strip() if 0 <= start_line - 1 < len(lines) else ""
        kind = _classify(parent.type)
        symbols.append(Symbol(name=name, kind=kind, start_line=start_line, end_line=end_line, signature=signature))
    return symbols


def _classify(node_type: str) -> str:
    if "class" in node_type:
        return "class"
    if "interface" in node_type or "trait" in node_type:
        return "interface"
    if "method" in node_type:
        return "method"
    if "function" in node_type or "func" in node_type:
        return "function"
    if "type" in node_type or "struct" in node_type:
        return "type"
    return "symbol"


def get_or_extract_symbols(
    conn: sqlite3.Connection,
    repo: str,
    path: str,
    content: str,
    language: str,
) -> list[Symbol]:
    """Cached symbol extraction. Re-parses only when content_sha changes."""
    sha = knowledge.content_sha(content)
    cached = knowledge.get_cached_symbols(conn, repo, path, sha)
    if cached is not None:
        return [Symbol(**s) for s in cached]
    symbols = extract_symbols(content, language)
    knowledge.cache_symbols(
        conn, repo, path, sha, language, [s.__dict__ for s in symbols]
    )
    return symbols


# ============================================================
# REFERENCE FINDING (ripgrep)
# ============================================================

def find_callers(
    symbol_name: str,
    own_file: str,
    *,
    repo_root: str = ".",
    languages: set[str] | None = None,
    max_results: int = 8,
) -> list[CallerSnippet]:
    """ripgrep for `symbol_name(` across the repo, excluding the defining file.

    We deliberately keep this textual + cheap. Tree-sitter validation per-hit
    would be more precise but ~10x slower; the LLM tolerates the noise fine.
    """
    if not shutil.which("rg"):
        log.debug("ripgrep not installed; skipping caller search")
        return []

    # Word-boundary match before the symbol; helps avoid substring hits.
    # We allow `.symbol(`, `symbol(`, `<symbol(` etc.
    pattern = rf"\b{symbol_name}\s*\("

    cmd = [
        "rg", "--no-heading", "--line-number", "--context", "2",
        "--max-count", str(max_results), "--max-filesize", "500K",
        "-e", pattern, repo_root,
    ]
    if languages:
        for lang in languages:
            cmd += ["--type", _rg_type(lang)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        log.debug("rg timeout looking for %s", symbol_name)
        return []

    if proc.returncode not in (0, 1):  # 1 = no matches
        log.debug("rg failed for %s: %s", symbol_name, proc.stderr[:200])
        return []

    return _parse_rg_output(proc.stdout, own_file=own_file, max_results=max_results)


def _rg_type(lang: str) -> str:
    return {
        "python": "py", "javascript": "js", "typescript": "ts", "tsx": "ts",
        "go": "go", "rust": "rust", "java": "java", "ruby": "ruby",
    }.get(lang, lang)


def _parse_rg_output(stdout: str, *, own_file: str, max_results: int) -> list[CallerSnippet]:
    """Parse `rg --context 2` output into CallerSnippets, one per matched line.

    rg context output looks like:
        path/file.py-5-def caller():
        path/file.py-6-    setup()
        path/file.py:7:    do_thing(arg)
        path/file.py-8-    teardown()
        --
    Lines with `:` are matches; lines with `-` are context.
    """
    own_norm = str(Path(own_file)).lstrip("./")
    blocks: list[list[str]] = []
    cur: list[str] = []
    for line in stdout.splitlines():
        if line == "--":
            if cur:
                blocks.append(cur)
                cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append(cur)

    results: list[CallerSnippet] = []
    for block in blocks:
        match_line = None
        match_lineno = 0
        match_file = ""
        ctx_lines: list[str] = []
        for raw in block:
            # rg uses ':' for the match line, '-' for context. The first separator
            # after the path tells us which it is.
            # Format: path<sep>lineno<sep>text
            parts = raw.split(":", 2)
            if len(parts) == 3 and parts[1].isdigit():
                match_file = parts[0]
                match_lineno = int(parts[1])
                match_line = parts[2]
                ctx_lines.append(parts[2])
            else:
                parts = raw.split("-", 2)
                if len(parts) == 3 and parts[1].isdigit():
                    if not match_file:
                        match_file = parts[0]
                    ctx_lines.append(parts[2])

        if not match_line or not match_file:
            continue
        norm = str(Path(match_file)).lstrip("./")
        if norm == own_norm:
            continue
        results.append(CallerSnippet(
            file=match_file,
            line=match_lineno,
            context="\n".join(ctx_lines),
        ))
        if len(results) >= max_results:
            break
    return results


# ============================================================
# TOP-LEVEL: build context for a single changed file
# ============================================================

def build_context_for_file(
    conn: sqlite3.Connection,
    *,
    repo: str,
    path: str,
    content: str,
    language: str,
    diff_line_numbers: set[int],
    repo_root: str = ".",
) -> CodegraphContext:
    """For a changed file, compute the codegraph context block.

    Steps:
      1. Extract (or load cached) symbols from the new file content.
      2. Identify which symbols overlap the changed line range (touched_symbols).
      3. For each touched symbol, ripgrep the repo for callers.
    """
    ctx = CodegraphContext()
    if not language or language not in SUPPORTED_LANGUAGES:
        return ctx

    symbols = get_or_extract_symbols(conn, repo, path, content, language)
    if not symbols:
        return ctx

    # Symbols whose body overlaps diff lines are "touched"
    touched: list[Symbol] = []
    for sym in symbols:
        if any(sym.start_line <= ln <= sym.end_line for ln in diff_line_numbers):
            touched.append(sym)
    # Cap at 6 to keep the prompt bounded
    ctx.touched_symbols = touched[:6]

    for sym in ctx.touched_symbols:
        callers = find_callers(
            sym.name,
            own_file=path,
            repo_root=repo_root,
            languages={language},
            max_results=5,
        )
        if callers:
            ctx.callers_by_symbol[sym.name] = callers

    return ctx
