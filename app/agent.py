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
        # Less aggressive than the previous 10/3. Gemini Flash has a 1M-token
        # context — compacting every 10 events is wasteful and was the root
        # cause of the FBA→MSRP scheduling contamination (overlap=3 left the
        # agent with too few raw turns to ground on, forcing memory-recall
        # reconstruction). 60/10 means most conversations never hit compaction;
        # those that do still have 10 raw turns of anchor.
        compaction_interval=60,
        overlap_size=10,
        summarizer=LlmEventSummarizer(
            llm=get_model("summarizer"),
            prompt_template=_SUMMARIZER_PROMPT_TEMPLATE,
        ),
    ),
)
