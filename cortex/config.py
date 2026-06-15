"""Configuration. Everything is env-overridable; sane local-first defaults.

CORTEX is single-user and local, so config is just environment variables plus a
home directory under ~/.cortex. No config file format to maintain in v1.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _home() -> Path:
    return Path(os.environ.get("CORTEX_HOME", Path.home() / ".cortex")).expanduser()


@dataclass(frozen=True)
class Config:
    home: Path
    db_path: Path

    # Provider selection. Defaults are the offline "fake" providers so the whole
    # capture -> store -> retrieve loop runs with zero API keys (good for dev,
    # tests, and plumbing the hook before paying for tokens).
    embedding_provider: str  # fake | openai | voyage
    processing_provider: str  # fake | anthropic

    # Model ids (only used when the matching real provider is selected).
    embedding_model: str
    processing_model: str

    anthropic_api_key: str | None
    openai_api_key: str | None

    # HTTP server (dashboard backend) — one port; SPA served from the same app.
    host: str
    port: int

    @staticmethod
    def load() -> "Config":
        home = _home()
        return Config(
            home=home,
            db_path=Path(os.environ.get("CORTEX_DB_PATH", home / "cortex.db")).expanduser(),
            embedding_provider=os.environ.get("CORTEX_EMBEDDING_PROVIDER", "fake"),
            processing_provider=os.environ.get("CORTEX_PROCESSING_PROVIDER", "fake"),
            # text-embedding-3-small (1536 dims) when using OpenAI; voyage-3 for Voyage.
            embedding_model=os.environ.get("CORTEX_EMBEDDING_MODEL", "text-embedding-3-small"),
            # Current Haiku. Anthropic has no embeddings API, so Haiku is enrichment only.
            processing_model=os.environ.get("CORTEX_PROCESSING_MODEL", "claude-haiku-4-5"),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
            openai_api_key=os.environ.get("OPENAI_API_KEY"),
            host=os.environ.get("CORTEX_HOST", "127.0.0.1"),
            port=int(os.environ.get("CORTEX_PORT", "7432")),
        )

    def ensure_home(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
