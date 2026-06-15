"""Embedding providers.

Pluggable so we never hardcode a vector dimension (Anthropic has no embeddings
API; OpenAI is 1536; Voyage differs). The default `fake` provider is fully
offline and deterministic so the whole loop — and the test suite — runs without
any API key or network.
"""

from __future__ import annotations

import hashlib
import math
import struct
from typing import Protocol


class EmbeddingProvider(Protocol):
    model: str

    def embed(self, text: str) -> list[float]: ...


def pack(vec: list[float]) -> bytes:
    """Vector -> float32 BLOB for SQLite storage."""
    return struct.pack(f"{len(vec)}f", *vec)


def unpack(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class FakeEmbedding:
    """Deterministic hashing embedding. Not semantically great, but stable and
    offline — good enough to wire and test the full pipeline. Captures coarse
    lexical overlap by hashing tokens into a fixed-width bag-of-words vector."""

    model = "fake-256"
    dim = 256

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _tokens(text):
            h = int.from_bytes(hashlib.md5(tok.encode()).digest()[:4], "little")
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm:
            vec = [v / norm for v in vec]
        return vec


class OpenAIEmbedding:
    def __init__(self, model: str, api_key: str | None):
        from openai import OpenAI  # imported lazily; optional dependency

        self.model = model
        self._client = OpenAI(api_key=api_key)

    def embed(self, text: str) -> list[float]:
        resp = self._client.embeddings.create(model=self.model, input=text)
        return resp.data[0].embedding


def _tokens(text: str) -> list[str]:
    return [t for t in "".join(c.lower() if c.isalnum() else " " for c in text).split() if len(t) > 1]


def get_embedding_provider(cfg) -> EmbeddingProvider:
    if cfg.embedding_provider == "openai":
        try:
            return OpenAIEmbedding(cfg.embedding_model, cfg.openai_api_key)
        except Exception as e:  # missing package / bad key — never break the tool
            import sys
            print(f"[crux] OpenAI embeddings unavailable ({e}); using offline embeddings. "
                  f"Install with: pip install 'crux[openai]'", file=sys.stderr)
    # voyage could be added here the same way.
    return FakeEmbedding()
