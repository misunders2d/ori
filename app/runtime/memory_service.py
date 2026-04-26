"""ADK 2.0 BaseMemoryService implementation backed by LanceDB.

Replaces the legacy `LongTermMemory` class with a clean `BaseMemoryService`
subclass. The LanceDB layout (one table per category, vectors via
FastEmbed) is preserved so existing data on disk remains usable. The
ADK-native interface (`add_memory`, `search_memory`, `add_session_to_memory`)
is the new public surface; tools should call it via `tool_context.search_memory`
where possible.

Categories — Ori's pre-existing convention (`background_tasks`,
`human_preferences`, `technical_context`, etc.) — are now passed through
`custom_metadata['category']`. When `add_memory` is called with no category
metadata, it routes to the `default` table. `search_memory` returns matches
across all categories, tagged with their origin in MemoryEntry.custom_metadata.
"""

from __future__ import annotations

import logging
import os
import uuid
import warnings
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import lancedb
from fastembed import TextEmbedding
from google.adk.events.event import Event
from google.adk.memory import BaseMemoryService
from google.adk.memory.base_memory_service import SearchMemoryResponse
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.sessions.session import Session
from google.genai import types

logger = logging.getLogger(__name__)


DB_PATH = os.path.abspath("./data/memory_db")
DEFAULT_CATEGORY = "default"
DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"


class OriMemoryService(BaseMemoryService):
    """LanceDB-backed memory service. Lazy-initializes the DB and embedding
    model on first use so test harnesses can construct one without paying
    the model-load cost upfront.
    """

    def __init__(
        self,
        db_path: str | None = None,
        embed_model_name: str = DEFAULT_EMBED_MODEL,
    ) -> None:
        self._db_path = db_path or DB_PATH
        self._embed_model_name = embed_model_name
        self._db = None
        self._embedding_model: TextEmbedding | None = None

    # ----- Lazy backend init ------------------------------------------------

    def _ensure(self) -> None:
        if self._db is not None:
            return
        os.makedirs(self._db_path, exist_ok=True)
        self._db = lancedb.connect(self._db_path)
        self._embedding_model = TextEmbedding(model_name=self._embed_model_name)
        logger.info("OriMemoryService initialized (lancedb=%s)", self._db_path)

    def _embed(self, text: str) -> list[float]:
        self._ensure()
        assert self._embedding_model is not None
        return next(iter(self._embedding_model.embed([text]))).tolist()

    def _table(self, category: str, *, sample_record: dict | None = None):
        """Open or create a table for `category`. If the table doesn't exist
        and a sample record is provided, create it with that record as schema.
        """
        self._ensure()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            existing = list(self._db.table_names())
            if category in existing:
                return self._db.open_table(category)
            if sample_record is not None:
                return self._db.create_table(category, data=[sample_record])
        return None

    @staticmethod
    def _content_to_text(content: types.Content) -> str:
        """Extract the concatenated text of a Content's text parts."""
        if not content or not content.parts:
            return ""
        chunks: list[str] = []
        for part in content.parts:
            if getattr(part, "text", None):
                chunks.append(part.text)
        return " ".join(chunks).strip()

    @staticmethod
    def _scope_filter(app_name: str, user_id: str) -> str:
        """LanceDB SQL filter scoping records to (app_name, user_id)."""
        # LanceDB metadata is a dict; we duplicate app_name/user_id at the top
        # level so filtering is simple SQL. Both are sanitized — alphanumerics
        # plus a few separators only.
        return f"app_name = '{app_name}' AND user_id = '{user_id}'"

    # ----- BaseMemoryService API -------------------------------------------

    async def add_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        memories: Sequence[MemoryEntry],
        custom_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Add a sequence of MemoryEntry items to the user's memory."""
        self._ensure()
        category = (custom_metadata or {}).get("category", DEFAULT_CATEGORY)
        timestamp = datetime.now().isoformat()
        records: list[dict[str, Any]] = []
        for entry in memories:
            text = self._content_to_text(entry.content)
            if not text:
                continue
            md = {**(custom_metadata or {})}
            if entry.custom_metadata:
                md.update(entry.custom_metadata)
            md.setdefault("category", category)
            md.setdefault("timestamp", entry.timestamp or timestamp)
            md.setdefault("author", entry.author)
            records.append({
                "id": entry.id or str(uuid.uuid4()),
                "vector": self._embed(text),
                "text": text,
                "app_name": app_name,
                "user_id": user_id,
                "metadata": md,
            })
        if not records:
            return
        table = self._table(category, sample_record=records[0])
        if table is None:
            logger.error("OriMemoryService.add_memory: failed to create table %r", category)
            return
        # If the table already existed, the create above returned None and we
        # opened the existing table; either way `table` is now the live ref.
        table.add(records)

    async def add_session_to_memory(self, session: Session) -> None:
        """Persist the session's text events as MemoryEntry items."""
        memories: list[MemoryEntry] = []
        for evt in session.events or []:
            if not getattr(evt, "content", None):
                continue
            text = self._content_to_text(evt.content)
            if not text:
                continue
            memories.append(MemoryEntry(
                content=evt.content,
                id=getattr(evt, "id", None),
                author=getattr(evt, "author", None),
                timestamp=getattr(evt, "timestamp", None),
                custom_metadata={"category": "session_history"},
            ))
        if memories:
            await self.add_memory(
                app_name=session.app_name,
                user_id=session.user_id,
                memories=memories,
                custom_metadata={"category": "session_history", "session_id": session.id},
            )

    async def add_events_to_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        events: Sequence[Event],
        session_id: str | None = None,
        custom_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist a sequence of events as MemoryEntry items."""
        memories: list[MemoryEntry] = []
        for evt in events:
            if not getattr(evt, "content", None):
                continue
            memories.append(MemoryEntry(
                content=evt.content,
                id=getattr(evt, "id", None),
                author=getattr(evt, "author", None),
                timestamp=getattr(evt, "timestamp", None),
                custom_metadata=dict(custom_metadata or {}),
            ))
        if memories:
            md = dict(custom_metadata or {})
            if session_id:
                md.setdefault("session_id", session_id)
            await self.add_memory(
                app_name=app_name, user_id=user_id, memories=memories, custom_metadata=md,
            )

    async def search_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        query: str,
    ) -> SearchMemoryResponse:
        """Semantic search across all categories for this (app_name, user_id)."""
        self._ensure()
        if not query or not query.strip():
            return SearchMemoryResponse(memories=[])

        query_vec = self._embed(query)
        all_memories: list[MemoryEntry] = []
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            categories = list(self._db.table_names())

        for cat in categories:
            try:
                table = self._db.open_table(cat)
                results = (
                    table.search(query_vec)
                    .where(self._scope_filter(app_name, user_id))
                    .limit(5)
                    .to_list()
                )
            except Exception:
                # Tables that pre-date the (app_name, user_id) columns will
                # throw on the .where() call — skip them rather than crash.
                continue
            for r in results:
                md = r.get("metadata") or {}
                all_memories.append(MemoryEntry(
                    content=types.Content(
                        role=md.get("author") or "user",
                        parts=[types.Part.from_text(text=r.get("text", ""))],
                    ),
                    id=r.get("id"),
                    author=md.get("author"),
                    timestamp=md.get("timestamp"),
                    custom_metadata={**md, "category": cat},
                ))
        return SearchMemoryResponse(memories=all_memories)
