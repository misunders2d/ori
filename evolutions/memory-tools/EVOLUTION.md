---
name: memory-tools
description: Advanced memory system using Pinecone (vector search) and Neo4j (graph relationships).
author: Bezos
created: 2026-04-08
verified: false
tags: [memory, knowledge-graph, pinecone, neo4j, vector-database]
files:
  - app/tools/pinecone_tools.py
  - app/tools/graph_tools.py
---

# memory-tools

A dual-layer memory architecture that combines natural language vector search (Pinecone) with structured entity relationship tracking (Neo4j).

## ⚠️ CRITICAL DEPENDENCY MISSING

This package was exported without its core engine: **`app/core/graph.py`**. The tools in this evolution will **NOT WORK** until that file is retrieved and integrated. They currently use `try/except ImportError` or `if not graph` checks to prevent crashes, but will return errors for most operations.

## Usage

### Prerequisites

1.  **Pinecone**:
    - API Key and Index Name (Serverless, `cosine` metric, `1536` dims for OpenAI).
    - `PINECONE_API_KEY`
    - `PINECONE_INDEX_NAME`
2.  **Neo4j**:
    - Instance URL, Username, and Password.
    - `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`
3.  **Dependencies**:
    - `uv add pinecone neo4j`

### Tools

#### Pinecone (Vector)
- **`search_knowledge`** — Semantic search across namespaces (personal, professional, people, technical).
- **`create_record`** — Stores new text memories with automated dual-write to the graph.
- **`create_person`** — Stores person metadata in the 'people' directory.

#### Neo4j (Graph)
- **`add_entity`** — Manually register an entity (person, project, concept).
- **`link_entities`** — Create a relationship edge between two entities.
- **`query_connections`** — Explore the graph around an entity.
- **`find_connection_path`** — Trace how two things are related.

### Dual-Write Architecture

The system is designed so that high-level actions (creating a record or person) automatically write to both databases:
1.  **Pinecone** stores the searchable text and metadata.
2.  **Neo4j** stores the entity nodes and relationship edges for structural queries.

## Files

- `app/tools/pinecone_tools.py`
- `app/tools/graph_tools.py`
