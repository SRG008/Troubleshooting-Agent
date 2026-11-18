# FDE Agentic Ops - Autonomous Production Incident Troubleshooter (Stage 1)

## Overview
A chat assistant built with **FastAPI**, **Pydantic**, and **Streamlit**. The LLM is provider-neutral: Gemini, OpenAI (or any OpenAI-compatible API), and Anthropic are supported, with ordered failover.
Ask any question about your connected workspace (projects, boards, sprints, work items, counts, details). The model chooses the tools and writes the native query itself, so nothing is hardcoded to specific questions. Jira is connected through the **Atlassian Rovo MCP server**.

## How the agent works
- `backend/agent.py` runs a provider-agnostic tool-calling loop. When Jira is connected, the model gets the tools the Rovo MCP server offers the signed-in user (for example `searchJiraIssuesUsingJql`, `getJiraIssue`, `createJiraIssue`, `editJiraIssue`, `transitionJiraIssue`, `addOrEditJiraIssueComment`, plus `discover` / `executeRead` / `executeWrite` for the rest of the catalog). The app's own `ask_user` tool is always added.
- `POST /agent/chat/stream` takes `{session_id, message, history}` and streams SSE events. The final event is `done` with `{answer, provider, execution_trace}`.
- `POST /agent/chat/resume` takes `{session_id, pending_id, answer}` and continues a run paused on an `ask` event. `answer` is `null` when the user skips the question.

### Jira through the Atlassian Rovo MCP server
- `backend/atlassian_auth.py` signs users in. **OAuth 2.1** (recommended): on first use the backend registers itself with Atlassian through dynamic client registration, so no Atlassian app, client ID, or secret is configured. Each sign-in uses PKCE and a random single-use `state`, and tokens are refreshed automatically. **API token**: a personal token created with scopes for the Rovo MCP app (`read:jira:agent-interface`, `search:jira:agent-interface`, `read:me`, `read:account`, plus `write:jira:agent-interface` for changes), sent as Basic auth together with the Jira site URL; your Atlassian organization admin must enable API-token authentication for the Rovo MCP server.
- Tokens stay on the server, are saved encrypted in `data/atlassian/`, and are never returned by `/auth/jira/status`.
- `backend/atlassian_mcp.py` calls the server with the official `mcp` Python SDK, lists its tools (cached for 10 minutes per session), and converts them into agent tools. The sidebar shows the user's sites; the selected site's `cloudId` is given to the model.
- Requested scopes are set by `ATLASSIAN_MCP_SCOPES`. Every action uses the signed-in user's own Jira permissions. Some Rovo tools use Rovo credits.

### Approving changes
- Every tool that can change data (`create*`, `edit*`, `transition*`, `add*`, `executeWrite`, `executeDestructive`, and any unknown tool that is not clearly read-only) is held by the backend. The run pauses with an approval question that shows the exact tool and arguments, and the call runs only after the user picks **Approve**. **Reject** tells the model the change was not made.
- This is enforced in code, not by the prompt, so text inside a ticket or a file cannot trigger a Jira change without approval.

### Interactive questions (`ask_user`)
- When the request is ambiguous (for example, "how many tasks?" with several projects) or a create flow needs a value, the model calls `ask_user` with one question, an `input_type` (`choice`, `text`, `number`, `date`, or `boolean`), and options for choices.
- The run pauses and emits an `ask` event. The frontend shows one question at a time as a native widget and posts the answer to `/agent/chat/resume`. The run then continues from the same point, updating the same assistant message rather than adding a new chat exchange. Previous selections remain visible as a compact summary. The chat input stays disabled until the flow completes or is cancelled.
- Creating a work item asks only for the fields the work item type requires. Optional fields are skipped. The backend approval shows the final values before the item is created.
- Pending questions are kept in memory for 30 minutes, so they are lost on backend restart. An expired question returns a message asking the user to retry.

### LLM layer (`backend/llm/`)
- `base.py` defines neutral message and tool types. The agent only uses these.
- `gemini.py`, `openai_compat.py`, and `anthropic.py` each translate those types to one provider's API. `gemini.py` also reduces MCP tool schemas to the JSON Schema subset Gemini accepts.
- `chain.py` tries the `LLM_CHAIN` entries in order. A transient failure (timeout, 429, 5xx) moves to the next entry. A provider that fails twice in a row is skipped for 60 seconds (circuit breaker). A rejected request (4xx other than 408 and 429) moves on without opening the breaker.
- Each LLM call appears in the execution trace with its model, latency, and token counts.

To add a provider, implement `LLMProvider.generate` in a new module under `backend/llm/`, then register it in `_build_provider` in `backend/llm/chain.py`.

### GitHub repositories
- Connect GitHub from the sidebar. Sign-in uses a GitHub OAuth app and is limited to `GITHUB_ORGANIZATION` members. Tokens stay on the server and are tied to a per-browser session cookie (30 days).
- Select the repositories the agent may use in the sidebar. The agent refuses any other repository.
- For each selected repository, set its local folder in the sidebar: type the path or click Browse to pick it, then Save. The folder can be an existing clone of the repository, or a parent folder the repository is cloned into. The agent never picks a location itself; if no folder is set, it asks for one in chat.
- Tools in `backend/github_tools.py`: list repositories and branches, read and search files, write files, show changes, discard a file, and commit and open a pull request.
- Commits are authored as the connected GitHub user. The agent asks for the base branch every time and commits only after the user confirms.

## Quickstart Guide
1. Copy `.env.example` to `.env`. Set at least one LLM key (`GEMINI_API_KEY`, `OPENAI_API_KEY`, or `ANTHROPIC_API_KEY`) and, optionally, `LLM_CHAIN` (for example `gemini:gemini-flash-lite-latest,openai:gpt-4o-mini`). Jira needs no credentials in `.env`: users sign in with Atlassian from the sidebar. Keep `JIRA_REDIRECT_URI` pointing at the backend's `/auth/jira/callback`.
   On a corporate network (Zscaler, Netskope, ...), set `HTTPS_PROXY` to the agent's local proxy (for Zscaler usually `http://127.0.0.1:9000`). Python cannot read the Windows PAC file, so without it every outbound call (Atlassian, GitHub, LLMs) fails with "connection forcibly closed". `USE_SYSTEM_CERTIFICATES=true` (the default) verifies HTTPS with the Windows certificate store, which holds the proxy's root certificate.
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
