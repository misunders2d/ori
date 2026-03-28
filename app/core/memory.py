import os
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

import lancedb
from fastembed import TextEmbedding

logger = logging.getLogger(__name__)

DB_PATH = os.path.abspath("./data/memory_db")

class LongTermMemory:
    """Core memory system using LanceDB and local FastEmbed embeddings."""

    def __init__(self):
        self._db = None
        self._embedding_model = None
        self._initialized = False

    def _init_db(self):
        if self._initialized:
            return
            
        try:
            os.makedirs(DB_PATH, exist_ok=True)
            self._db = lancedb.connect(DB_PATH)
            
            # Use a lightweight, high-performance local model
            # This happens on CPU and stays completely private.
            self._embedding_model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
            
            self._initialized = True
            logger.info("Long-term memory initialized with LanceDB and FastEmbed.")
        except Exception as e:
            logger.error("Failed to initialize long-term memory: %s", e)

    def _get_table(self, table_name: str):
        self._init_db()
        if table_name in self._db.table_names():
            return self._db.open_table(table_name)
        return None

    def _create_table_if_not_exists(self, table_name: str, data: List[Dict[str, Any]]):
        self._init_db()
        if table_name not in self._db.table_names():
            return self._db.create_table(table_name, data=data)
        return self._db.open_table(table_name)

    async def remember(self, category: str, text: str, metadata: Dict[str, Any] = None):
        """Stores a piece of information in the specified memory category."""
        self._init_db()
        metadata = metadata or {}
        metadata["timestamp"] = datetime.now().isoformat()
        
        # Generate embedding locally
        embeddings = list(self._embedding_model.embed([text]))
        vector = embeddings[0].tolist()
        
        record = {
            "vector": vector,
            "text": text,
            "metadata": metadata
        }
        
        table = self._create_table_if_not_exists(category, [record])
        if table:
            table.add([record])

    async def search(self, category: str, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Performs a semantic search in the specified memory category."""
        self._init_db()
        table = self._get_table(category)
        if not table:
            return []
            
        # Generate query embedding locally
        query_embeddings = list(self._embedding_model.embed([query]))
        query_vector = query_embeddings[0].tolist()
        
        results = table.search(query_vector).limit(limit).to_list()
        return results

    async def forget(self, category: str, filter_expr: str):
        """Deletes records from memory based on a SQL-like filter expression."""
        table = self._get_table(category)
        if table:
            table.delete(filter_expr)

# Global instance
memory = LongTermMemory()
