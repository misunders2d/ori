import os

from google.adk.apps import App
from google.adk.apps.app import EventsCompactionConfig
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer

from app.app_utils.models import get_model
from app.sub_agents.coordinator_agent import root_agent

# The internal application name used for session isolation
app_name = os.environ.get("APP_NAME", "ori")

# Prefer token-triggered compaction over event-count compaction. ADK 1.28 still
# requires compaction_interval, so keep it effectively unreachable.
_COMPACTION_INTERVAL = 10_000_000
_COMPACTION_OVERLAP_SIZE = 20
_COMPACTION_TOKEN_THRESHOLD = 800_000
_COMPACTION_EVENT_RETENTION_SIZE = 40

# Custom summarizer prompt — the default collapses code/SQL/task definitions
# into prose, which has caused the agent to reconstruct missing details from
# unrelated memory recalls (e.g. pulling a stale MSRP task into an FBA-task
# scheduling call). The verbatim-preservation clauses force the summarizer to
# keep literal blocks intact so downstream turns don't have to guess.
_SUMMARIZER_PROMPT_TEMPLATE = (
    "Summarize the following conversation between a user and an AI agent. "
    "Capture key information, decisions, and unresolved tasks.\n\n"
    "Begin your output with this exact marker on its own line:\n"
    "[CONVERSATION SUMMARY — older turns were compacted; if the user references "
    "specific text that is not preserved verbatim below, ASK them to repeat it "
    "rather than guess]\n\n"
    "STRICT PRESERVATION RULES — you MUST copy these verbatim into the summary, "
    "never paraphrasing or omitting them:\n"
    "1. Code blocks — any text fenced with triple backticks (```), in any language.\n"
    "2. SQL queries, even unfenced.\n"
    "3. URLs, IDs, channel handles (sl_*, tg_*, mem_*, ASIN codes), file paths.\n"
    "4. Tool-call invocations with their full arguments.\n"
    "5. The most recently approved task_prompt and 'steps' list for any "
    "schedule_*_task / edit_scheduled_task discussion — copy the user's exact "
    "phrasing AND the agent's full task/step descriptions that the user approved.\n"
    "6. Any text the user explicitly told the agent to remember verbatim.\n"
    "7. For scheduled tasks currently in execution: preserve the 'task_prompt' "
    "and 'steps' from the session start verbatim.\n\n"
    "For everything else, be concise.\n\n"
    "Conversation:\n{conversation_history}"
)

app = App(
    root_agent=root_agent,
    name=app_name,
    events_compaction_config=EventsCompactionConfig(
        compaction_interval=_COMPACTION_INTERVAL,
        overlap_size=_COMPACTION_OVERLAP_SIZE,
        token_threshold=_COMPACTION_TOKEN_THRESHOLD,
        event_retention_size=_COMPACTION_EVENT_RETENTION_SIZE,
        summarizer=LlmEventSummarizer(
            llm=get_model("summarizer"),
            prompt_template=_SUMMARIZER_PROMPT_TEMPLATE,
        ),
    ),
)
