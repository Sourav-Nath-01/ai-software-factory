"""Vector memory for agents using ChromaDB.

Allows agents to retrieve similar past code/review patterns
across runs, giving the system cross-run learning ability.
Falls back to an in-memory dict if ChromaDB is not installed, disabled,
or fails at runtime — memory is optional and must never break a pipeline run.
"""
from __future__ import annotations

import hashlib
import logging
import threading

from src.core.config import settings

logger = logging.getLogger(__name__)

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
        # Connect lazily so importing this module never touches disk
        self._collection = None
        self._initialised = False
        self._init_lock = threading.Lock()

    def _get_collection(self):
        """Open the ChromaDB collection on first use, or return None if unavailable.

        Whether memory is used at all is the Pipeline's decision (settings.enable_memory).
        """
        with self._init_lock:
            if not self._initialised:
                self._initialised = True
                if _CHROMADB_AVAILABLE:
                    try:
                        # Under output_dir, so each eval run gets its own isolated memory
                        client = chromadb.PersistentClient(path=str(settings.output_path / "memory"))
                        self._collection = client.get_or_create_collection(
                            name=self.collection_name,
                            metadata={"hnsw:space": "cosine"},
                        )
                    except Exception as e:
                        logger.warning("ChromaDB unavailable for %s (%s); using in-memory fallback",
                                       self.collection_name, e)
        return self._collection

    def store(self, content: str, metadata: dict) -> str:
        """Store a code snippet or review pattern. Never raises."""
        # A content id, not a security hash (bandit B324 flagged the bare md5)
        doc_id = hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()[:12]

        collection = self._get_collection()
        if collection is not None:
            try:
                collection.upsert(
                    ids=[doc_id],
                    documents=[content],
                    metadatas=[metadata],
                )
                return doc_id
            except Exception as e:
                logger.warning("Memory write to %s failed (%s); using in-memory fallback",
                               self.collection_name, e)

        self._fallback[doc_id] = {"content": content, "metadata": metadata}
        return doc_id

    def query(self, query_text: str, n_results: int = 3) -> list[dict]:
        """Retrieve similar past documents."""
        collection = self._get_collection()
        if collection is not None:
            try:
                results = collection.query(
                    query_texts=[query_text],
                    n_results=min(n_results, collection.count()),
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
        collection = self._get_collection()
        if collection is not None:
            try:
                return collection.count()
            except Exception:
                pass
        return len(self._fallback)


# Issues found in past reviews, retrieved by project similarity to steer new reviews
reviewer_memory = AgentMemory("reviewer_memory")
