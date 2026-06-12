"""Vector memory for agents using ChromaDB.

Allows agents to retrieve similar past code/review patterns
across runs, giving the system cross-run learning ability.
Falls back to an in-memory dict if ChromaDB is not installed.
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

_CHROMADB_AVAILABLE = False
try:
    import chromadb  # type: ignore
    _CHROMADB_AVAILABLE = True
except ImportError:
    pass


class AgentMemory:
    """Vector store for agent knowledge persistence."""

    def __init__(self, collection_name: str = "agent_memory"):
        self.collection_name = collection_name
        self._fallback: dict[str, dict] = {}

        if _CHROMADB_AVAILABLE:
            self._client = chromadb.PersistentClient(path="output/memory")
            self._collection = self._client.get_or_create_collection(
                name=collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        else:
            self._client = None
            self._collection = None

    def store(self, content: str, metadata: dict) -> str:
        """Store a code snippet or review pattern."""
        doc_id = hashlib.md5(content.encode()).hexdigest()[:12]

        if self._collection is not None:
            self._collection.upsert(
                ids=[doc_id],
                documents=[content],
                metadatas=[metadata],
            )
        else:
            self._fallback[doc_id] = {"content": content, "metadata": metadata}

        return doc_id

    def query(self, query_text: str, n_results: int = 3) -> list[dict]:
        """Retrieve similar past documents."""
        if self._collection is not None:
            try:
                results = self._collection.query(
                    query_texts=[query_text],
                    n_results=min(n_results, self._collection.count()),
                )
                docs = results.get("documents", [[]])[0]
                metas = results.get("metadatas", [[]])[0]
                return [{"content": d, "metadata": m} for d, m in zip(docs, metas)]
            except Exception:
                return []
        # Fallback: return last N items
        items = list(self._fallback.values())[-n_results:]
        return items

    @property
    def count(self) -> int:
        if self._collection is not None:
            return self._collection.count()
        return len(self._fallback)


# Global memory instances per agent type
coder_memory = AgentMemory("coder_memory")
reviewer_memory = AgentMemory("reviewer_memory")
