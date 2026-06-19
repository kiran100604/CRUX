"""Configuration. Everything is env-overridable; sane local-first defaults.

CRUX is single-user and local, so config is just environment variables plus a
home directory under ~/.crux. No config file format to maintain in v1.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _home() -> Path:
    return Path(os.environ.get("CRUX_HOME", Path.home() / ".crux")).expanduser()


def _default_user() -> str:
    import getpass
    try:
        return getpass.getuser()
    except Exception:
        return "user"


def _load_env_file(home: Path) -> dict:
    """Read ~/.crux/config.env (KEY=VALUE lines) so keys persist without the user
    editing their shell rc. Real environment variables still take precedence."""
    out: dict[str, str] = {}
    f = home / "config.env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def save_env_file(home: Path, values: dict) -> Path:
    """Persist config values (merging with any existing file)."""
    home.mkdir(parents=True, exist_ok=True)
    current = _load_env_file(home)
    current.update({k: v for k, v in values.items() if v})
    f = home / "config.env"
    f.write_text("\n".join(f"{k}={v}" for k, v in current.items()) + "\n", encoding="utf-8")
    return f


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
    # OpenAI-compatible base URL (NVIDIA NIM, Groq, Together, Ollama, …). When set
    # with provider=openai, chat + embeddings go through this endpoint.
    api_base: str | None

    # HTTP server (dashboard backend) — one port; SPA served from the same app.
    host: str
    port: int

    # Capture hotkey chosen in setup (modifiers + key), used by the tray app.
    hotkey_mods: tuple[str, ...]
    hotkey_key: str

    # Team mode: when set, the hook/CLI talk to a shared CRUX server over HTTP
    # instead of the local DB, so a whole team shares one intent graph.
    server: str | None
    user: str

    @staticmethod
    def load() -> "Config":
        home = _home()
        # file values are defaults; real env vars override them
        env = {**_load_env_file(home), **os.environ}
        return Config(
            home=home,
            db_path=Path(env.get("CRUX_DB_PATH", home / "crux.db")).expanduser(),
            embedding_provider=env.get("CRUX_EMBEDDING_PROVIDER", "fake"),
            processing_provider=env.get("CRUX_PROCESSING_PROVIDER", "fake"),
            # text-embedding-3-small (1536 dims) when using OpenAI; voyage-3 for Voyage.
            embedding_model=env.get("CRUX_EMBEDDING_MODEL", "text-embedding-3-small"),
            # Current Haiku. Anthropic has no embeddings API, so Haiku is enrichment only.
            processing_model=env.get("CRUX_PROCESSING_MODEL", "claude-haiku-4-5"),
            anthropic_api_key=env.get("ANTHROPIC_API_KEY"),
            openai_api_key=env.get("OPENAI_API_KEY"),
            api_base=(env.get("CRUX_OPENAI_BASE_URL") or env.get("CRUX_API_BASE") or None),
            host=env.get("CRUX_HOST", "127.0.0.1"),
            port=int(env.get("CRUX_PORT", "7432")),
            hotkey_mods=tuple(
                m for m in env.get(
                    "CRUX_HOTKEY_MODS",
                    "cmd,shift" if sys.platform == "darwin" else "ctrl,shift"
                ).split(",") if m
            ),
            hotkey_key=env.get("CRUX_HOTKEY_KEY", "space"),
            server=(env.get("CRUX_SERVER") or "").rstrip("/") or None,
            user=env.get("CRUX_USER") or _default_user(),
        )

    def ensure_home(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def marker_path(self) -> Path:
        """Flag file written once first-run setup completes (via UI or wizard)."""
        return self.home / ".configured"

    def is_configured(self) -> bool:
        return self.marker_path.exists()

    def mark_configured(self) -> None:
        self.ensure_home()
        self.marker_path.write_text("ok", encoding="utf-8")
