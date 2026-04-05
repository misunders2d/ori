import os
from google.adk.apps import App
from google.adk.apps.app import EventsCompactionConfig
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer

from app.app_utils.models import get_model
from app.sub_agents.coordinator_agent import root_agent

# The internal application name used for session isolation
app_name = os.environ.get("APP_NAME", "ori")

app = App(
    root_agent=root_agent,
    name=app_name,
    events_compaction_config=EventsCompactionConfig(
        # Every 10 events (exchanges), the history is summarized.
        # This provides a good balance between nuance and memory efficiency.
        compaction_interval=10,
        # Keep the last 3 events raw to preserve immediate conversational context.
        overlap_size=3,
        summarizer=LlmEventSummarizer(llm=get_model("summarizer")),
    ),
)
