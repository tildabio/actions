"""Skeletal tests. Run with: pytest tests/

These aren't a comprehensive suite — they're a starting point. The functions
most worth covering as you iterate:
  - parse_diff_new_lines (correctness against weird diff formats)
  - parse_json (provider quirks)
  - codegraph.extract_symbols (per language)
  - knowledge.search_learnings (vector recall)
"""

import pytest

from src.github_api import parse_diff_new_lines
from src.llm import parse_json


# ============================================================
# parse_diff_new_lines
# ============================================================

def test_parse_diff_basic():
    diff = """@@ -1,3 +1,4 @@
 def foo():
+    new_line()
     return 1
 # end
"""
    assert parse_diff_new_lines(diff) == {2}


def test_parse_diff_multiple_hunks():
    diff = """@@ -1,2 +1,3 @@
 a
+b
 c
@@ -10,2 +11,3 @@
 x
+y
 z
"""
    lines = parse_diff_new_lines(diff)
    assert 2 in lines
    assert 12 in lines


def test_parse_diff_no_count():
    # Single-line hunk format: @@ -1 +1 @@
    diff = """@@ -1 +1 @@
-old
+new
"""
    assert parse_diff_new_lines(diff) == {1}


# ============================================================
# parse_json
# ============================================================

def test_parse_json_clean():
    assert parse_json('{"a": 1}') == {"a": 1}


def test_parse_json_code_fence():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_with_preamble():
    s = 'Here is the result:\n{"x": [1, 2]}\nDone.'
    assert parse_json(s) == {"x": [1, 2]}


def test_parse_json_garbage():
    assert parse_json("no json here") == {}


def test_parse_json_nested():
    s = '{"outer": {"inner": "value"}, "list": [1, 2, 3]}'
    assert parse_json(s) == {"outer": {"inner": "value"}, "list": [1, 2, 3]}


# ============================================================
# Codegraph (requires tree-sitter — skip if missing)
# ============================================================

def test_extract_python_symbols():
    pytest.importorskip("tree_sitter_languages")
    from src.codegraph import extract_symbols

    code = """
def alpha():
    pass

class Beta:
    def method(self):
        pass
"""
    syms = extract_symbols(code, "python")
    names = {s.name for s in syms}
    assert "alpha" in names
    assert "Beta" in names
    assert "method" in names


def test_extract_typescript_symbols():
    pytest.importorskip("tree_sitter_languages")
    from src.codegraph import extract_symbols

    code = """
export function fetchUser(id: string) { return null; }
export class UserService {
  async load() {}
}
"""
    syms = extract_symbols(code, "typescript")
    names = {s.name for s in syms}
    assert "fetchUser" in names
    assert "UserService" in names
    assert "load" in names


# ============================================================
# Path instruction matching
# ============================================================

def test_path_matching():
    import pathspec
    spec = pathspec.PathSpec.from_lines("gitwildmatch", ["src/api/**"])
    assert spec.match_file("src/api/handlers.py")
    assert spec.match_file("src/api/v2/users.py")
    assert not spec.match_file("src/lib/util.py")
