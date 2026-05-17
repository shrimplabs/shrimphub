"""
RAG (Retrieval-Augmented Generation) adapter for Swarm Agents.

Provides a swappable interface for different vector database backends.
Supports: ChromaDB, FAISS, Pinecone, etc.
"""

import os
import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Dict, Any, Optional


class RAGBackend(ABC):
    """Abstract base class for RAG backends."""
    
    @abstractmethod
    def query(self, question: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        Query the vector store for relevant documents.
        
        Args:
            question: The question to search for
            top_k: Number of results to return
            
        Returns:
            List of relevant documents with metadata
        """
        pass
    
    @abstractmethod
    def ingest(self, texts: List[str], metadata: List[Dict]) -> int:
        """
        Ingest documents into the vector store.
        
        Args:
            texts: List of text chunks
            metadata: List of metadata dicts for each chunk
            
        Returns:
            Number of chunks ingested
        """
        pass
    
    @abstractmethod
    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about the vector store."""
        pass


class ChromaDBBackend(RAGBackend):
    """ChromaDB backend using an operator-supplied documentation index."""
    
    def __init__(self, persist_directory: str = None):
        self.persist_directory = persist_directory or os.path.expanduser(
            "~/.cache/swarm-controller/rag/chromadb"
        )
        self._client = None
        self._collection = None
    
    def _get_client(self):
        """Lazy initialization of ChromaDB client."""
        if self._client is None:
            try:
                import chromadb
                from chromadb.config import Settings
                self._client = chromadb.PersistentClient(
                    path=self.persist_directory,
                    settings=Settings(anonymized_telemetry=False)
                )
            except ImportError:
                raise ImportError("ChromaDB not installed. Run: pip install chromadb")
        return self._client
    
    def _get_collection(self, collection_name: str = "godot_docs"):
        """Get or create collection."""
        if self._collection is None:
            client = self._get_client()
            try:
                self._collection = client.get_collection(name=collection_name)
            except:
                self._collection = client.create_collection(name=collection_name)
        return self._collection
    
    def query(self, question: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """Query ChromaDB for relevant documents."""
        try:
            collection = self._get_collection()
            
            # Get embeddings model for query
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer('BAAI/bge-small-en-v1.5')
            query_embedding = model.encode([question]).tolist()
            
            results = collection.query(
                query_embeddings=query_embedding,
                n_results=top_k
            )
            
            documents = []
            # ChromaDB returns: {'documents': [[doc1, doc2, ...]], 'metadatas': [[meta1, meta2, ...]]}
            docs = results.get('documents', [[]])[0] if results else []
            metas = results.get('metadatas', [[]])[0] if results else []
            
            for i, doc in enumerate(docs):
                meta = metas[i] if i < len(metas) else {}
                documents.append({
                    'text': doc,
                    'title': meta.get('title', 'Unknown'),
                    'source': meta.get('source', 'unknown'),
                    'url': meta.get('url', ''),
                })
            
            return documents
            
        except Exception as e:
            return [{'error': str(e)}]
    
    def ingest(self, texts: List[str], metadata: List[Dict]) -> int:
        """Ingest documents into ChromaDB."""
        try:
            collection = self._get_collection()
            
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer('BAAI/bge-small-en-v1.5')
            embeddings = model.encode(texts).tolist()
            
            ids = [f"doc_{i}" for i in range(len(texts))]
            
            collection.add(
                documents=texts,
                embeddings=embeddings,
                metadatas=metadata,
                ids=ids
            )
            
            return len(texts)
            
        except Exception as e:
            print(f"Error ingesting: {e}")
            return 0
    
    def get_stats(self) -> Dict[str, Any]:
        """Get ChromaDB stats."""
        try:
            collection = self._get_collection()
            return {
                'backend': 'chromadb',
                'persist_directory': self.persist_directory,
                'count': collection.count(),
                'collection_name': collection.name
            }
        except Exception as e:
            return {'backend': 'chromadb', 'error': str(e)}


class FAISSBackend(RAGBackend):
    """FAISS backend (in-memory, fast)."""
    
    def __init__(self, index_path: str = None):
        self.index_path = index_path
        self._index = None
        self._texts = []
    
    def query(self, question: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """Query FAISS index."""
        if self._index is None:
            return [{'error': 'Index not loaded'}]
        
        try:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer('BAAI/bge-small-en-v1.5')
            query_embedding = model.encode([question]).tolist()
            
            scores, indices = self._index.search(
                [query_embedding[0]], 
                min(top_k, len(self._texts))
            )
            
            results = []
            for score, idx in zip(scores[0], indices[0]):
                if idx < len(self._texts):
                    results.append({
                        'text': self._texts[idx],
                        'distance': float(score)
                    })
            
            return results
            
        except Exception as e:
            return [{'error': str(e)}]
    
    def ingest(self, texts: List[str], metadata: List[Dict]) -> int:
        """Build FAISS index from texts."""
        try:
            import faiss
            from sentence_transformers import SentenceTransformer
            
            model = SentenceTransformer('BAAI/bge-small-en-v1.5')
            embeddings = model.encode(texts).tolist()
            
            dim = len(embeddings[0])
            self._index = faiss.IndexFlatL2(dim)
            self._index.add(embeddings)
            self._texts = texts
            
            return len(texts)
            
        except Exception as e:
            return 0
    
    def get_stats(self) -> Dict[str, Any]:
        """Get FAISS stats."""
        return {
            'backend': 'faiss',
            'index_loaded': self._index is not None,
            'num_texts': len(self._texts)
        }


def create_rag_backend(backend_type: str = "chromadb", **config) -> RAGBackend:
    """
    Factory function to create RAG backend.
    
    Args:
        backend_type: One of 'chromadb', 'faiss', 'pinecone'
        **config: Backend-specific configuration
        
    Returns:
        RAGBackend instance
    """
    if backend_type == "chromadb":
        return ChromaDBBackend(
            persist_directory=config.get("persist_directory")
        )
    elif backend_type == "faiss":
        return FAISSBackend(
            index_path=config.get("index_path")
        )
    elif backend_type == "pinecone":
        # Would need pinecone installed
        raise NotImplementedError("Pinecone backend not yet implemented")
    else:
        raise ValueError(f"Unknown backend: {backend_type}")


class RAGClient:
    """RAG client for swarm agents."""
    
    def __init__(self, config: Dict = None):
        self.config = config or {}
        self._backend = None
    
    def _get_backend(self) -> RAGBackend:
        """Lazy initialization of backend."""
        if self._backend is None:
            backend_type = self.config.get("backend", "chromadb")
            self._backend = create_rag_backend(
                backend_type,
                **self.config.get(backend_type, {})
            )
        return self._backend
    
    def query(self, question: str, top_k: int = None) -> Dict[str, Any]:
        """
        Query the RAG system.
        
        Args:
            question: Question to ask
            top_k: Number of results (default from config)
            
        Returns:
            Dict with 'answer', 'sources', 'context'
        """
        top_k = top_k or self.config.get("top_k", 5)
        
        docs = self._get_backend().query(question, top_k)
        
        # Build context from documents
        context = "\n\n".join([
            f"--- Source: {d.get('title', 'Unknown')} ---\n{d.get('text', '')}"
            for d in docs if 'error' not in d
        ])
        
        sources = [
            {
                'title': d.get('title', 'Unknown'),
                'url': d.get('url', ''),
                'text': d.get('text', '')[:200] + "..."
            }
            for d in docs if 'error' not in d
        ]
        
        return {
            'question': question,
            'context': context,
            'sources': sources,
            'num_results': len(docs)
        }
    
    def get_stats(self) -> Dict[str, Any]:
        """Get RAG system statistics."""
        return self._get_backend().get_stats()


# Convenience function for agents
def rag_query(question: str, top_k: int = 5, config_path: str = None) -> Dict[str, Any]:
    """
    Simple query function for agents.
    
    Usage in agent:
        result = rag_query("How do I create a signal in GDScript?")
        print(result['context'])
    """
    # Load config from swarm config
    if config_path is None:
        config_path = os.path.join(
            os.path.dirname(__file__), 
            "..", "..", "config.json"
        )
    
    try:
        with open(config_path) as f:
            config = json.load(f)
        rag_config = config.get("rag", {})
    except:
        rag_config = {"backend": "chromadb", "top_k": top_k}
    
    client = RAGClient(rag_config)
    return client.query(question, top_k)


if __name__ == "__main__":
    # Test
    client = RAGClient({"backend": "chromadb"})
    result = client.query("How do I create a signal in GDScript?")
    print(f"Results: {result['num_results']}")
    for src in result['sources'][:2]:
        print(f"- {src['title']}")
