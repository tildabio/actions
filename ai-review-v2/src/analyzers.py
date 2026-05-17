"""Static analyzers. Run language-appropriate tools and attach per-file output.

Philosophy: analyzers are advisory and tolerant. If a tool isn't installed
or fails, we silently skip it and tell the LLM "(not analyzed)". The review
prompt explicitly instructs the model not to re-flag anything the analyzers
caught — this is the noise-reduction half of the pipeline.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from .github_api import ChangedFile

log = logging.getLogger(__name__)


def run_all(files: list[ChangedFile]) -> None:
    by_lang: dict[str, list[ChangedFile]] = {}
    for f in files:
        if f.status == "removed" or not f.new_content:
            continue
        if f.language:
            by_lang.setdefault(f.language, []).append(f)

    if by_lang.get("python"):
        _python(by_lang["python"])
    js_like = by_lang.get("javascript", []) + by_lang.get("typescript", []) + by_lang.get("tsx", [])
    if js_like:
        _javascript(js_like)
    if by_lang.get("go"):
        _go(by_lang["go"])
    if by_lang.get("rust"):
        _rust(by_lang["rust"])

    # Semgrep + secrets scan across everything
    _semgrep(files)
    _gitleaks(files)


# ============================================================
# RUNNERS
# ============================================================

def _run(cmd: list[str], timeout: int = 120) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + ("\n" + r.stderr if r.stderr else "")).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.debug("analyzer %s skipped: %s", cmd[0], e)
        return ""


def _python(files: list[ChangedFile]) -> None:
    paths = [f.path for f in files]
    out_by_tool: dict[str, str] = {
        "ruff":   _run(["ruff", "check", "--output-format=concise", *paths]),
        "bandit": _run(["bandit", "-q", "-f", "txt", *paths], timeout=90),
    }
    if Path("pyproject.toml").exists() or Path("mypy.ini").exists():
        out_by_tool["mypy"] = _run(
            ["mypy", "--no-error-summary", "--no-color-output", *paths], timeout=90
        )
    _distribute(files, out_by_tool)


def _javascript(files: list[ChangedFile]) -> None:
    if not Path("package.json").exists():
        return
    paths = [f.path for f in files]
    eslint = Path("node_modules/.bin/eslint")
    if eslint.exists():
        out = _run([str(eslint), "--format", "compact", *paths], timeout=120)
        _distribute(files, {"eslint": out})


def _go(files: list[ChangedFile]) -> None:
    pkgs = sorted({"./" + str(Path(f.path).parent) for f in files if "/" in f.path})
    pkgs = [p for p in pkgs if p != "./."]
    if not pkgs:
        return
    out = _run(["go", "vet", *pkgs], timeout=120)
    _distribute(files, {"go vet": out})


def _rust(files: list[ChangedFile]) -> None:
    if not Path("Cargo.toml").exists():
        return
    out = _run(["cargo", "clippy", "--message-format=short", "--no-deps"], timeout=180)
    _distribute(files, {"clippy": out})


def _semgrep(files: list[ChangedFile]) -> None:
    paths = [f.path for f in files if f.status != "removed"]
    if not paths:
        return
    out = _run(
        ["semgrep", "--config=auto", "--quiet", "--json", "--timeout=60", *paths],
        timeout=180,
    )
    if not out:
        return
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return

    by_path: dict[str, list[str]] = {}
    for r in data.get("results", []):
        check = r["check_id"].rsplit(".", 1)[-1]
        msg = r["extra"]["message"][:200]
        by_path.setdefault(r["path"], []).append(f"  L{r['start']['line']}: [{check}] {msg}")

    for f in files:
        if f.path in by_path:
            f.analyzer_output += "\nsemgrep:\n" + "\n".join(by_path[f.path][:20])


def _gitleaks(files: list[ChangedFile]) -> None:
    out = _run(
        ["gitleaks", "detect", "--no-banner", "--redact", "--report-format=json"],
        timeout=60,
    )
    if out and out.startswith("[") and "findings" not in out.lower():
        try:
            findings = json.loads(out)
        except json.JSONDecodeError:
            return
        if not findings:
            return
        # gitleaks reports per file path
        by_path: dict[str, list[str]] = {}
        for fnd in findings:
            path = fnd.get("File")
            line = fnd.get("StartLine")
            rule = fnd.get("RuleID")
            if path:
                by_path.setdefault(path, []).append(f"  L{line}: [{rule}] potential secret")
        for f in files:
            if f.path in by_path:
                f.analyzer_output += "\ngitleaks:\n" + "\n".join(by_path[f.path])


# ============================================================
# DISTRIBUTION
# ============================================================

def _distribute(files: list[ChangedFile], outputs: dict[str, str]) -> None:
    for tool, raw in outputs.items():
        if not raw.strip():
            continue
        by_path: dict[str, list[str]] = {}
        for line in raw.splitlines():
            for f in files:
                if line.startswith(f.path) or f":{f.path}" in line:
                    by_path.setdefault(f.path, []).append("  " + line)
                    break
        for f in files:
            if f.path in by_path:
                f.analyzer_output += f"\n{tool}:\n" + "\n".join(by_path[f.path][:30])
