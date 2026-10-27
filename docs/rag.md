# Optional RAG Integration

Swarm Controller has an optional `rag_query(question, top_k)` agent tool for
Godot documentation lookup. It is disabled by default and requires an
operator-supplied local documentation index.

## Configuration

Add a `rag` section to `config.json`:

```json
{
  "rag": {
    "enabled": true,
    "index_path": "/path/to/your/godot-doc-index",
    "backend": "chromadb",
    "top_k": 5
  }
}
```

The `index_path` must point to a compatible local index package containing
`config.yaml` and the configured ChromaDB persistence directory. Keep that index
outside this repository unless its licensing and size are appropriate for public
distribution.

If RAG is disabled or misconfigured, `rag_query` returns a clear error object
instead of raising a stack trace inside the agent.

## Relationship To MCP

RAG is for documentation retrieval and examples. MCP integrations are for
project/runtime tools. Agents may use both, but RAG should not be required for
normal project creation or validation.
