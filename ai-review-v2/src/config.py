"""Configuration loading and validation.

Three layers, merged in order of precedence (later wins):
  1. Sensible built-in defaults
  2. .review.yaml at the repo root (if present)
  3. Environment variables (set by action.yml from action inputs)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


@dataclass
class LLMEndpoint:
    base_url: str
    model: str
    api_key: str

    def __post_init__(self):
        if not self.api_key:
            raise ValueError(f"Missing API key for model {self.model}")


@dataclass
class PathInstruction:
    pattern: str               # gitignore-style glob, e.g. "src/api/**"
    instructions: str          # natural-language guidance for matching files


@dataclass
class Config:
    # LLM endpoints
    review:    LLMEndpoint
    triage:    LLMEndpoint
    embedding: LLMEndpoint
    embedding_dimensions: int = 1536

    # GitHub
    github_token: str = ""
    github_repo:  str = ""
    pr_number:    int = 0
    base_sha:     str = ""
    head_sha:     str = ""

    # State branch (where we keep the SQLite DB)
    state_branch: str = "__reviewer_state__"
    state_dir:    str = "/tmp/reviewer-state"

    # Behavior
    max_files: int = 50
    max_file_bytes: int = 100_000
    confidence_threshold: int = 3
    enable_analyzers: bool = True
    enable_codegraph: bool = True
    enable_learnings: bool = True
    enable_incremental: bool = True
    parallel_file_reviews: int = 6

    # Path-based review instructions
    path_instructions: list[PathInstruction] = field(default_factory=list)

    # Files to skip entirely
    skip_patterns: list[str] = field(default_factory=lambda: [
        "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
        "Cargo.lock", "go.sum", "Pipfile.lock", "composer.lock", "Gemfile.lock",
        "node_modules/**", "vendor/**", "dist/**", "build/**", ".next/**",
        "**/__generated__/**", "**/*.min.js", "**/*.min.css",
    ])

    # Bot identity (used by the learnings listener to detect @-mentions)
    bot_username: str = "github-actions[bot]"


def load() -> Config:
    """Build a Config from environment + .review.yaml."""
    yaml_data = _load_yaml(".review.yaml")

    def env_or_yaml(env_key: str, yaml_path: list[str], default: Any = None) -> Any:
        if v := os.environ.get(env_key):
            return v
        d = yaml_data
        for k in yaml_path:
            if not isinstance(d, dict) or k not in d:
                return default
            d = d[k]
        return d if d is not None else default

    # ---- LLM endpoints ----
    review_url = env_or_yaml("REVIEW_BASE_URL", ["review", "base_url"], "https://api.openai.com/v1")
    review_key = os.environ["REVIEW_API_KEY"]  # required, no fallback
    review_model = env_or_yaml("REVIEW_MODEL", ["review", "model"], "gpt-4o")

    triage = LLMEndpoint(
        base_url=env_or_yaml("TRIAGE_BASE_URL", ["triage", "base_url"], review_url),
        api_key=os.environ.get("TRIAGE_API_KEY") or review_key,
        model=env_or_yaml("TRIAGE_MODEL", ["triage", "model"], "gpt-4o-mini"),
    )
    review = LLMEndpoint(base_url=review_url, api_key=review_key, model=review_model)
    embedding = LLMEndpoint(
        base_url=env_or_yaml("EMBED_BASE_URL", ["embedding", "base_url"], review_url),
        api_key=os.environ.get("EMBED_API_KEY") or review_key,
        model=env_or_yaml("EMBED_MODEL", ["embedding", "model"], "text-embedding-3-small"),
    )

    cfg = Config(
        review=review,
        triage=triage,
        embedding=embedding,
        embedding_dimensions=int(env_or_yaml("EMBED_DIMENSIONS", ["embedding", "dimensions"], 1536)),
        github_token=os.environ.get("GITHUB_TOKEN", ""),
        github_repo=os.environ.get("GITHUB_REPOSITORY", ""),
        pr_number=int(os.environ.get("PR_NUMBER", "0") or 0),
        base_sha=os.environ.get("BASE_SHA", ""),
        head_sha=os.environ.get("HEAD_SHA", ""),
        state_branch=env_or_yaml("STATE_BRANCH", ["state", "branch"], "__reviewer_state__"),
        state_dir=env_or_yaml("STATE_DIR", ["state", "dir"], "/tmp/reviewer-state"),
        max_files=int(env_or_yaml("MAX_FILES", ["limits", "max_files"], 50)),
        max_file_bytes=int(env_or_yaml("MAX_FILE_BYTES", ["limits", "max_file_bytes"], 100_000)),
        confidence_threshold=int(env_or_yaml("CONFIDENCE_THRESHOLD", ["limits", "confidence"], 3)),
        enable_analyzers=_to_bool(env_or_yaml("ENABLE_ANALYZERS", ["features", "analyzers"], True)),
        enable_codegraph=_to_bool(env_or_yaml("ENABLE_CODEGRAPH", ["features", "codegraph"], True)),
        enable_learnings=_to_bool(env_or_yaml("ENABLE_LEARNINGS", ["features", "learnings"], True)),
        enable_incremental=_to_bool(env_or_yaml("ENABLE_INCREMENTAL", ["features", "incremental"], True)),
        parallel_file_reviews=int(env_or_yaml("PARALLEL_FILE_REVIEWS", ["limits", "parallel"], 6)),
        bot_username=env_or_yaml("BOT_USERNAME", ["bot_username"], "github-actions[bot]"),
    )

    # Path instructions only come from yaml (would be awkward as env)
    pi = yaml_data.get("path_instructions") or []
    cfg.path_instructions = [
        PathInstruction(pattern=p["pattern"], instructions=p["instructions"])
        for p in pi if "pattern" in p and "instructions" in p
    ]

    # Custom skip patterns extend defaults
    custom_skips = yaml_data.get("skip_patterns") or []
    if custom_skips:
        cfg.skip_patterns = cfg.skip_patterns + list(custom_skips)

    return cfg


def _load_yaml(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with p.open() as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            log.warning("%s is not a mapping at root; ignoring", path)
            return {}
        return data
    except Exception as e:
        log.warning("Failed to parse %s: %s", path, e)
        return {}


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("1", "true", "yes", "on")
    return bool(v)
