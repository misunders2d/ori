"""Background entity extraction — extracts entities and relationships from
conversation turns and writes them to the Neo4j knowledge graph.

Runs async (fire-and-forget) after each agent response, so it adds zero
latency to the conversation.
"""

import asyncio
import json
import logging

from google import genai

from app.app_utils.models import get_model_name
from app.core import graph as neo4j_graph

logger = logging.getLogger(__name__)

_EXTRACTION_PROMPT = """\
Analyze this conversation exchange and extract entities and relationships.

USER MESSAGE:
{user_message}

AGENT RESPONSE:
{agent_response}

Extract ONLY concrete, named entities and their relationships. Skip vague references.

Return a JSON object with this exact structure (no markdown, no extra text):
{{
  "entities": [
    {{"name": "Entity Name", "type": "person|company|project|product|concept|event", "properties": {{}}}}
  ],
  "relationships": [
    {{"from": "Entity A Name", "to": "Entity B Name", "type": "RELATIONSHIP_TYPE", "properties": {{}}}}
  ]
}}

Rules:
- Only extract entities that are clearly named (not "the supplier" or "that project")
- Relationship types should be UPPER_SNAKE_CASE (e.g. WORKS_WITH, MANAGES, SUPPLIES, INVOLVED_IN)
- If no entities or relationships are found, return {{"entities": [], "relationships": []}}
- Keep it minimal — only extract what's clearly stated, don't infer
"""


async def extract_entities_background(
    user_message: str, agent_response: str, session_id: str
) -> None:
    """Fire-and-forget entity extraction from a conversation turn.

    Call this with asyncio.create_task() — it logs errors but never raises.
    """
    if not neo4j_graph.is_configured():
        return

    # Skip very short exchanges — nothing useful to extract
    if len(user_message) < 20 and len(agent_response) < 50:
        return

    try:
        client = genai.Client()
        prompt = _EXTRACTION_PROMPT.format(
            user_message=user_message[:2000],
            agent_response=agent_response[:2000],
        )

        response = await client.aio.models.generate_content(
            model=get_model_name("session_summarizer"),
            contents=prompt,
        )

        text = (response.text or "").strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        data = json.loads(text)

        entities = data.get("entities", [])
        relationships = data.get("relationships", [])

        if not entities and not relationships:
            return

        # Build a name→id map as we create entities
        name_to_id = {}

        for ent in entities:
            name = ent.get("name", "").strip()
            etype = ent.get("type", "concept")
            props = ent.get("properties", {})
            if not name:
                continue

            # Check if entity already exists by name
            existing = await neo4j_graph.search_entities(name, limit=1)
            if existing.get("entities"):
                name_to_id[name] = existing["entities"][0]["entity_id"]
                # Update existing entity with any new properties
                await neo4j_graph.upsert_entity(
                    entity_id=existing["entities"][0]["entity_id"],
                    name=name,
                    entity_type=etype,
                    properties={**props, "source": "auto_extraction"},
                    author="agent:auto",
                )
            else:
                import uuid
                from datetime import datetime
                entity_id = f"ent_{datetime.now().strftime('%Y%m%d')}_{uuid.uuid4().hex[:8]}"
                await neo4j_graph.upsert_entity(
                    entity_id=entity_id,
                    name=name,
                    entity_type=etype,
                    properties={**props, "source": "auto_extraction"},
                    author="agent:auto",
                )
                name_to_id[name] = entity_id

        for rel in relationships:
            from_name = rel.get("from", "").strip()
            to_name = rel.get("to", "").strip()
            rel_type = rel.get("type", "RELATED_TO")
            props = rel.get("properties", {})

            from_id = name_to_id.get(from_name)
            to_id = name_to_id.get(to_name)

            if not from_id or not to_id:
                continue

            await neo4j_graph.add_relationship(
                from_entity_id=from_id,
                to_entity_id=to_id,
                relation_type=rel_type,
                properties={**props, "source": "auto_extraction"},
                author="agent:auto",
            )

        if entities or relationships:
            logger.info(
                "Auto-extracted %d entities, %d relationships from session %s",
                len(entities), len(relationships), session_id,
            )

    except json.JSONDecodeError:
        logger.debug("Entity extraction returned non-JSON for session %s", session_id)
    except Exception as e:
        logger.warning("Background entity extraction failed for session %s: %s", session_id, e)
