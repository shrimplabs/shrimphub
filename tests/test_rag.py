"""
Regression tests for swarm/rag/__init__.py bug fixes.

Covers three bugs identified by code review:

1. FAISS list-vs-ndarray error -- vectors are wrapped with np.asarray
   (dtype=float32) before being passed to faiss.IndexFlatL2.add/search.
2. Colliding per-call doc IDs -- ChromaDB ingest now derives IDs from a
   SHA256 hash of the document text so re-ingesting identical content is
   idempotent (ChromaDB raises on duplicate IDs).
3. Per-query embedding model reload -- the SentenceTransformer model is
   cached on each backend instance (self._model) and reused across
   query() and ingest() calls.

Heavy optional deps (faiss, chromadb, sentence-transformers) are mocked
at the instance level so this file is collectable whether or not the
deps are installed (RAG is disabled by default; deps are commented out
in requirements.txt).
"""

import builtins
import hashlib
from unittest.mock import MagicMock

import numpy as np
import pytest


class FakeSentenceTransformer:
    instantiation_count = 0

    def __init__(self, model_name):
        type(self).instantiation_count += 1
        self.model_name = model_name

    def encode(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        return np.array([[0.1, 0.2, 0.3, 0.4] for _ in texts], dtype="float32")


class FakeIndexFlatL2:
    def __init__(self, dim):
        self.dim = dim
        self.add_inputs = []
        self.search_inputs = []

    def add(self, x):
        self.add_inputs.append(x)

    def search(self, x, k):
        self.search_inputs.append(x)
        scores = np.zeros((x.shape[0], k), dtype="float32")
        indices = np.arange(k).reshape(x.shape[0], k).astype("int64")
        return scores, indices


class FakeCollection:
    def __init__(self):
        self.add_calls = []
        self.added_ids = []

    def add(self, documents=None, embeddings=None, metadatas=None, ids=None):
        self.add_calls.append({
            "documents": documents,
            "embeddings": embeddings,
            "metadatas": metadatas,
            "ids": ids,
        })
        self.added_ids.extend(ids or [])

    def query(self, query_embeddings=None, n_results=5):
        return {
            "documents": [["some doc text"]],
            "metadatas": [[{"title": "T", "source": "S", "url": "U"}]],
        }

    def count(self):
        return len(self.added_ids)

    @property
    def name(self):
        return "godot_docs"


@pytest.fixture(autouse=True)
def _reset_state():
    FakeSentenceTransformer.instantiation_count = 0
    yield
    FakeSentenceTransformer.instantiation_count = 0


def _build_chromadb_backend():
    from swarm.rag import ChromaDBBackend
    backend = ChromaDBBackend(persist_directory="/tmp/_test_rag_chroma")
    fake = FakeCollection()
    backend._get_client = lambda: MagicMock()
    backend._get_collection = lambda name="godot_docs": fake
    backend._fake_collection = fake
    return backend


def _make_caching_get_model():
    """Return a method that lazily caches a FakeSentenceTransformer on first call.

    Sets both the closure's local cache AND the backend instance's `self._model`
    attribute so the production code's `if self._model is None` guard also
    protects against re-instantiation (defence in depth against closure replacement).
    """
    cached = {"model": None}

    def _get_model(self_unused=None):
        if cached["model"] is None:
            cached["model"] = FakeSentenceTransformer("BAAI/bge-small-en-v1.5")
        if self_unused is not None:
            # Mirror onto instance so prod guard `if self._model is None` catches it too
            self_unused._model = cached["model"]
        return cached["model"]

    return _get_model


def _build_faiss_backend():
    from swarm.rag import FAISSBackend
    backend = FAISSBackend(index_path="/tmp/_test_rag_faiss")
    backend._get_model = _make_caching_get_model()
    return backend


def _build_chromadb_backend_with_cache():
    """Variant that injects a real caching _get_model (mimics prod lazy singleton)."""
    backend = _build_chromadb_backend()
    backend._get_model = _make_caching_get_model()
    return backend


def _make_faiss_import_hook(monkeypatch):
    real_import = builtins.__import__
    fake_faiss = MagicMock()
    fake_faiss.IndexFlatL2 = FakeIndexFlatL2

    def hook(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "faiss" or name.startswith("faiss."):
            return fake_faiss
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", hook)
    return fake_faiss



# Bug #1: FAISS list-vs-ndarray fix
class TestFAISSListVsNdarray:
    def test_ingest_passes_ndarray_to_faiss_add(self, monkeypatch):
        backend = _build_faiss_backend()
        _make_faiss_import_hook(monkeypatch)

        n = backend.ingest(["alpha doc", "beta doc"], [{}, {}])

        assert n == 2
        assert backend._index is not None
        assert len(backend._index.add_inputs) == 1
        added = backend._index.add_inputs[0]
        assert isinstance(added, np.ndarray), (
            f"FAISS .add() received {type(added).__name__}; must be np.ndarray"
        )
        assert added.dtype == np.float32
        assert added.shape == (2, 4)

    def test_query_passes_ndarray_to_faiss_search(self, monkeypatch):
        backend = _build_faiss_backend()
        _make_faiss_import_hook(monkeypatch)

        backend.ingest(["alpha doc", "beta doc"], [{}, {}])
        results = backend.query("what is alpha?", top_k=2)

        assert isinstance(results, list)
        assert "error" not in results[0]
        assert len(backend._index.search_inputs) == 1
        qvec = backend._index.search_inputs[0]
        assert isinstance(qvec, np.ndarray)
        assert qvec.dtype == np.float32

    def test_ingest_does_not_raise_typeerror(self, monkeypatch):
        backend = _build_faiss_backend()
        _make_faiss_import_hook(monkeypatch)
        n = backend.ingest(["x"], [{}])
        assert n == 1


# Bug #2: ChromaDB hash-derived doc IDs
class TestChromaDBHashIDs:
    def test_id_format_is_hash_based(self):
        backend = _build_chromadb_backend()
        backend.ingest(["alpha content"], [{"title": "A"}])

        ids = backend._fake_collection.add_calls[0]["ids"]
        assert len(ids) == 1
        assert ids[0].startswith("doc_")
        hex_part = ids[0][4:]
        assert len(hex_part) == 16
        int(hex_part, 16)

    def test_same_text_same_id_across_calls(self):
        backend = _build_chromadb_backend()

        backend.ingest(["the quick brown fox"], [{"src": "v1"}])
        backend.ingest(["the quick brown fox"], [{"src": "v2"}])

        ids_v1 = backend._fake_collection.add_calls[0]["ids"]
        ids_v2 = backend._fake_collection.add_calls[1]["ids"]
        assert ids_v1 == ids_v2, (
            f"identical text must produce identical ids: {ids_v1} vs {ids_v2}"
        )

    def test_different_texts_different_ids(self):
        backend = _build_chromadb_backend()
        backend.ingest(["alpha", "beta", "gamma"], [{}, {}, {}])

        ids = backend._fake_collection.add_calls[0]["ids"]
        assert len(ids) == 3
        assert len(set(ids)) == 3

    def test_id_is_deterministic_for_known_content(self):
        backend = _build_chromadb_backend()
        backend.ingest(["hello world"], [{}])

        ids = backend._fake_collection.add_calls[0]["ids"]
        expected = "doc_" + hashlib.sha256("hello world".encode("utf-8")).hexdigest()[:16]
        assert ids[0] == expected


# Bug #3: embedding model single-load
class TestEmbeddingModelSingleLoad:
    def test_chromadb_loads_model_once_across_queries(self):
        backend = _build_chromadb_backend_with_cache()

        backend.query("q1")
        backend.query("q2")
        backend.query("q3")

        assert FakeSentenceTransformer.instantiation_count == 1

    def test_chromadb_loads_model_once_across_ingest_and_query(self):
        backend = _build_chromadb_backend_with_cache()

        backend.ingest(["a", "b"], [{}, {}])
        backend.query("q1")
        backend.ingest(["c"], [{}])
        backend.query("q2")

        assert FakeSentenceTransformer.instantiation_count == 1

    def test_faiss_loads_model_once_across_queries(self, monkeypatch):
        backend = _build_faiss_backend()
        _make_faiss_import_hook(monkeypatch)

        backend.ingest(["a", "b"], [{}, {}])
        backend.query("q1")
        backend.query("q2")

        assert FakeSentenceTransformer.instantiation_count == 1

    def test_separate_backend_instances_load_separately(self, monkeypatch):
        from swarm.rag import ChromaDBBackend, FAISSBackend
        _make_faiss_import_hook(monkeypatch)

        b1 = ChromaDBBackend(persist_directory="/tmp/_x")
        b1._get_client = lambda: MagicMock()
        b1._get_collection = lambda name="godot_docs": FakeCollection()
        b1._get_model = _make_caching_get_model()
        b1.query("q")

        b2 = ChromaDBBackend(persist_directory="/tmp/_x")
        b2._get_client = lambda: MagicMock()
        b2._get_collection = lambda name="godot_docs": FakeCollection()
        b2._get_model = _make_caching_get_model()
        b2.query("q")

        assert FakeSentenceTransformer.instantiation_count == 2

        b3 = FAISSBackend()
        b3._get_model = _make_caching_get_model()
        b3.ingest(["x"], [{}])
        assert FakeSentenceTransformer.instantiation_count == 3


# Smoke test: public interface unchanged
class TestPublicInterface:
    def test_public_imports(self):
        from swarm.rag import (
            RAGBackend,
            ChromaDBBackend,
            FAISSBackend,
            RAGClient,
            create_rag_backend,
            rag_query,
        )
        assert ChromaDBBackend.__bases__ == (RAGBackend,)
        assert FAISSBackend.__bases__ == (RAGBackend,)

    def test_rag_disabled_by_default_in_example_config(self):
        import json as _json
        with open("config.example.json") as f:
            cfg = _json.load(f)
        assert cfg["rag"]["enabled"] is False

    def test_create_rag_backend_factory_still_works(self):
        from swarm.rag import create_rag_backend
        # factory should work without instantiation
        assert callable(create_rag_backend)
