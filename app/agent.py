from google.adk.apps import App
from google.adk.apps.app import EventsCompactionConfig
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
from google.adk.models import Gemini

from app.sub_agents.coordinator_agent import root_agent

app = App(
    root_agent=root_agent,
    name="ori",
    events_compaction_config=EventsCompactionConfig(
        compaction_interval=5,
        overlap_size=2,
        summarizer=LlmEventSummarizer(llm=Gemini(model="gemini-2.5-flash-lite")),
    ),
)
