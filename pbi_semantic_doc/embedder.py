"""
Embedding providers for pbi-semantic-doc RAG output.

Network-based providers (Voyage, Ollama) use stdlib urllib only —
zero hard dependencies. FastEmbed requires: pip install fastembed

Usage:
    provider = get_provider("voyage", api_key="va-...", model="voyage-3")
    embeddings = provider.embed_batch(["text1", "text2"])

    provider = get_provider("ollama", model="bge-m3")
    embedding = provider.embed("some text")

    provider = get_provider("fastembed", model="BAAI/bge-m3")
    embedding = provider.embed("some text")
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Optional


class EmbeddingProvider(ABC):
    """Abstract base for all embedding providers."""

    @abstractmethod
    def embed(self, text: str) -> list[float]: ...

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    @property
    @abstractmethod
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> Optional[int]:
        return None


# ── Voyage AI ─────────────────────────────────────────────────────────────────

class VoyageEmbedder(EmbeddingProvider):
    """
    Voyage AI embeddings (acquired by Anthropic).
    Recommended pairing with Claude for RAG pipelines.
    Uses stdlib urllib — no SDK required.
    """

    _DIMS: dict[str, int] = {
        "voyage-3":              1024,
        "voyage-3-lite":          512,
        "voyage-code-3":         1024,
        "voyage-multilingual-2": 1024,
        "voyage-finance-2":      1024,
        "voyage-law-2":          1024,
        "voyage-2":              1024,
        "voyage-large-2":        1536,
    }
    _URL = "https://api.voyageai.com/v1/embeddings"

    def __init__(self, api_key: str, model: str = "voyage-3"):
        self._api_key = api_key
        self._model = model

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimension(self) -> Optional[int]:
        return self._DIMS.get(self._model)

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload = json.dumps({"input": texts, "model": self._model}).encode()
        req = urllib.request.Request(
            self._URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise RuntimeError(
                f"Voyage API error {e.code}: {body}"
            ) from e
        return [
            item["embedding"]
            for item in sorted(data["data"], key=lambda x: x["index"])
        ]


# ── Ollama ────────────────────────────────────────────────────────────────────

class OllamaEmbedder(EmbeddingProvider):
    """
    Ollama local embedding server.
    Run: ollama pull bge-m3
    Uses stdlib urllib — no SDK required.
    """

    def __init__(
        self,
        model: str = "bge-m3",
        base_url: str = "http://localhost:11434",
    ):
        self._model = model
        self._base_url = base_url.rstrip("/")

    @property
    def model_name(self) -> str:
        return self._model

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": self._model, "input": texts}).encode()
        req = urllib.request.Request(
            f"{self._base_url}/api/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Cannot reach Ollama at {self._base_url}. "
                "Is the Ollama server running? (ollama serve)"
            ) from e
        return data["embeddings"]


# ── FastEmbed (Qdrant) ────────────────────────────────────────────────────────

class FastEmbedEmbedder(EmbeddingProvider):
    """
    FastEmbed (Qdrant) in-process embeddings — no server required.
    Install: pip install pbi-semantic-doc[fastembed]
             or: pip install fastembed
    """

    def __init__(self, model: str = "BAAI/bge-m3"):
        try:
            from fastembed import TextEmbedding  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "FastEmbed not installed.\n"
                "Run: pip install pbi-semantic-doc[fastembed]\n"
                "  or: pip install fastembed"
            ) from exc
        self._model_name = model
        self._embedder = TextEmbedding(model_name=model)

    @property
    def model_name(self) -> str:
        return self._model_name

    def embed(self, text: str) -> list[float]:
        return list(next(iter(self._embedder.embed([text]))))

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [list(v) for v in self._embedder.embed(texts)]


# ── Factory ───────────────────────────────────────────────────────────────────

def get_provider(
    name: str,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> EmbeddingProvider:
    """
    Return an EmbeddingProvider by name.

    Args:
        name:     "voyage" | "ollama" | "fastembed"
        model:    model identifier (provider-specific default if omitted)
        api_key:  required for voyage
        base_url: Ollama server URL (default http://localhost:11434)
    """
    name = name.lower().strip()

    if name == "voyage":
        if not api_key:
            raise ValueError(
                "--api-key is required for the Voyage provider.\n"
                "Get your key at: https://www.voyageai.com/"
            )
        return VoyageEmbedder(api_key=api_key, model=model or "voyage-3")

    if name == "ollama":
        return OllamaEmbedder(
            model=model or "bge-m3",
            base_url=base_url or "http://localhost:11434",
        )

    if name in ("fastembed", "fast"):
        return FastEmbedEmbedder(model=model or "BAAI/bge-m3")

    raise ValueError(
        f"Unknown embedding provider: {name!r}\n"
        "Available: voyage, ollama, fastembed"
    )
