# FDE Agentic Ops - Autonomous Production Incident Troubleshooter (Stage 1)

## Overview
A chat assistant built with **FastAPI**, **Pydantic**, and **Streamlit**. The LLM is provider-neutral: Gemini, OpenAI (or any OpenAI-compatible API), and Anthropic are supported, with ordered failover.
Ask any question about your connected workspace (projects, boards, sprints, work items, counts, details). The model chooses the tools and writes the native query itself, so nothing is hardcoded to specific questions. Jira is supported today.

## How the agent works
- `backend/agent.py` runs a provider-agnostic tool-calling loop. The model picks from generic tools: `list_projects`, `list_boards`, `list_sprints`, `search_work_items`, `count_work_items`, `get_work_item`, `list_issue_types`, `get_required_fields`, `search_users`, `create_work_item`, and `ask_user`.
- Each tool is backed by an `IssueTracker` (`backend/trackers/base.py`). The agent only talks to this interface.
- `backend/trackers/jira_tracker.py` implements it for Jira. The query language is JQL, and the guidance is in `query_guide`. Required fields for creating an issue come from Jira's `createmeta` API, so no field list is hardcoded.
- `backend/trackers/registry.py` lists the providers in `TRACKER_PROVIDERS`. The first provider whose session is connected is used.
- `POST /agent/chat/stream` takes `{session_id, message, history}` and streams SSE events. The final event is `done` with `{answer, provider, execution_trace}`.
- `POST /agent/chat/resume` takes `{session_id, pending_id, answer}` and continues a run paused on an `ask` event. `answer` is `null` when the user skips the question.

### Interactive questions (`ask_user`)
- When the request is ambiguous (for example, "how many tasks?" with several projects) or a create flow needs a value, the model calls `ask_user` with one question, an `input_type` (`choice`, `text`, `number`, `date`, or `boolean`), and options for choices.
- The run pauses and emits an `ask` event. The frontend shows one question at a time as a native widget and posts the answer to `/agent/chat/resume`. The run then continues from the same point, updating the same assistant message rather than adding a new chat exchange. Previous selections remain visible as a compact summary. The chat input stays disabled until the flow completes or is cancelled.
- Creating a work item asks only for the fields Jira marks as required. Optional fields are skipped. The agent confirms the summary before it creates the item.
- Pending questions are kept in memory for 30 minutes, so they are lost on backend restart. An expired question returns a message asking the user to retry.

### LLM layer (`backend/llm/`)
- `base.py` defines neutral message and tool types. The agent only uses these.
- `gemini.py`, `openai_compat.py`, and `anthropic.py` each translate those types to one provider's API.
- `chain.py` tries the `LLM_CHAIN` entries in order. A transient failure (timeout, 429, 5xx) moves to the next entry. A provider that fails twice in a row is skipped for 60 seconds (circuit breaker). A rejected request (4xx other than 408 and 429) moves on without opening the breaker.
- Each LLM call appears in the execution trace with its model, latency, and token counts.

To add a provider, implement `LLMProvider.generate` in a new module under `backend/llm/`, then register it in `_build_provider` in `backend/llm/chain.py`.

### Adding a new tracker (e.g., Azure Boards)
1. Implement `IssueTracker` in `backend/trackers/azure_tracker.py`. Set `name`, `query_language` (WIQL), and `query_guide`, and implement the async methods.
2. Add a factory for it to `TRACKER_PROVIDERS` in `backend/trackers/registry.py`.

The agent, tool declarations, LLM layer, and chat endpoint need no changes.

## Quickstart Guide
1. Copy `.env.example` to `.env`. Set at least one LLM key (`GEMINI_API_KEY`, `OPENAI_API_KEY`, or `ANTHROPIC_API_KEY`) and, optionally, `LLM_CHAIN` (for example `gemini:gemini-flash-lite-latest,openai:gpt-4o-mini`). Add the Atlassian OAuth credentials as well.
2. Create a virtual environment and install dependencies:
   ```bash
   python -m venv .venv
   # Windows:
   .venv\Scripts\activate
   # macOS/Linux:
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Run the FastAPI Backend (Terminal 1):
   ```bash
   uvicorn backend.main:app --reload --port 8000
   ```
4. Run the Streamlit Dashboard (Terminal 2):
   ```bash
   streamlit run frontend/app.py --server.port 8501
   ```
