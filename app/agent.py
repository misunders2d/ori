import os
from google.adk.apps import App
from google.adk.apps.app import EventsCompactionConfig
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer

from app.app_utils.models import get_model
from app.sub_agents.coordinator_agent import root_agent

# The internal application name used for session isolation
app_name = os.environ.get("APP_NAME", "ori")

# Custom summarizer prompt — the default collapses code/SQL/task definitions
# into prose, which has caused the agent to reconstruct missing details from
# unrelated memory recalls (e.g. pulling a stale MSRP task into an FBA-task
# scheduling call). The verbatim-preservation clauses force the summarizer to
# keep literal blocks intact so downstream turns don't have to guess.
_SUMMARIZER_PROMPT_TEMPLATE = (
    "Summarize the following conversation between a user and an AI agent. "
    "Capture key information, decisions, and unresolved tasks.\n\n"
    "STRICT PRESERVATION RULES — you MUST copy these verbatim into the summary, "
    "never paraphrasing or omitting them:\n"
    "1. Code blocks — any text fenced with triple backticks (```), in any language.\n"
    "2. SQL queries, even unfenced.\n"
    "3. URLs, IDs, channel handles (sl_*, tg_*, mem_*, ASIN codes), file paths.\n"
    "4. Tool-call invocations with their full arguments.\n"
    "5. The most recently approved task_prompt for any schedule_*_task / "
    "edit_scheduled_task discussion — copy the user's exact phrasing AND the "
    "agent's full task description that the user approved.\n"
    "6. Any text the user explicitly told the agent to remember verbatim.\n\n"
    "For everything else, be concise.\n\n"
    "Conversation:\n{conversation_history}"
)

app = App(
    root_agent=root_agent,
    name=app_name,
    events_compaction_config=EventsCompactionConfig(
        # Every 10 events (exchanges), the history is summarized.
        # This provides a good balance between nuance and memory efficiency.
        compaction_interval=10,
        # Keep the last 3 events raw to preserve immediate conversational context.
        overlap_size=3,
        summarizer=LlmEventSummarizer(
            llm=get_model("summarizer"),
            prompt_template=_SUMMARIZER_PROMPT_TEMPLATE,
        ),
    ),
)
