"""Agent definitions. The root is `coordinator.root_agent`; sub-agents
(developer, knowledge) are imported from there. The Google Search subagent
lives in `app/tools/google_search.py` since its public face is a tool, not
an agent reference."""

from app.agents.coordinator import root_agent
from app.agents.developer import developer_agent
from app.agents.knowledge import knowledge_agent

__all__ = ["root_agent", "developer_agent", "knowledge_agent"]
