import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

from backend.llm import (
    AssistantMessage,
    LLMResponse,
    LLMUnavailableError,
    Message,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    UserMessage,
    get_llm_chain,
)
from backend.models import AgentStepTrace, ChatResponse, ChatTurn
from backend.trackers.base import IssueTracker
from backend.trackers.registry import get_active_tracker

MAX_TOOL_ROUNDS = 6
MAX_HISTORY_TURNS = 10
MAX_TOOL_RESULT_CHARS = 15000
METADATA_TTL_SECONDS = 300.0

_metadata_cache: Dict[Tuple[str, str, str], Tuple[float, Any]] = {}

TOOL_SPECS: List[ToolSpec] = [
    ToolSpec(
        name="list_projects",
        description="List all projects/spaces the user can access in the connected workspace.",
        parameters={"type": "object", "properties": {}},
    ),
    ToolSpec(
        name="list_boards",
        description="List boards (Scrum/Kanban/team boards). Optionally limit to one project key.",
        parameters={
            "type": "object",
            "properties": {
                "project_key": {"type": "string", "description": "Project key to filter boards by."},
            },
        },
    ),
    ToolSpec(
        name="list_sprints",
        description="List sprints of a board. Use the board id from list_boards.",
        parameters={
            "type": "object",
            "properties": {
                "board_id": {"type": "string", "description": "Board id from list_boards."},
                "state": {"type": "string", "description": "Comma-separated states: active, future, closed."},
            },
            "required": ["board_id"],
        },
    ),
    ToolSpec(
        name="search_work_items",
        description=(
            "Search work items (tickets/issues/tasks) with a query written in the workspace's native query "
            "language. Returns key, summary, type, status, priority, assignee and project for each item. "
            "Use this for any listing or filtering question. Returns at most 100 items."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Query in the native query language."},
                "max_results": {"type": "integer", "description": "Maximum items to return (1-100)."},
            },
            "required": ["query"],
        },
    ),
    ToolSpec(
        name="count_work_items",
        description="Count work items matching a query written in the workspace's native query language.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Query in the native query language."},
            },
            "required": ["query"],
        },
    ),
    ToolSpec(
        name="get_work_item",
        description="Get full details of one work item (description, comments, labels) by its key or id.",
        parameters={
            "type": "object",
            "properties": {
                "item_id": {"type": "string", "description": "Work item key, e.g. KAN-1."},
            },
            "required": ["item_id"],
        },
    ),
]


def _build_system_prompt(tracker: Optional[IssueTracker]) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if tracker is None:
        return f"""You are an AI assistant for issue tracking. Current date and time: {today}.

No issue tracker is connected right now, so you cannot query any workspace. Answer general questions directly from general knowledge.
If the user asks about their workspace (projects, boards, sprints, work items, counts, or details), tell them to connect their workspace in the sidebar first. Never invent workspace data.
Answer in clear Markdown.
"""
    return f"""You are an AI assistant for issue tracking, connected to the {tracker.name} workspace "{tracker.workspace_name}".
Current date and time: {today}.

Answer the user's question by calling the available tools to query the live workspace. You can answer questions about anything in the account: projects, boards, sprints, work items, counts, assignees, statuses, and details.

Rules:
- If the question is not about the workspace (for example general knowledge), answer briefly from general knowledge without calling any tool.
- Never invent keys, names, counts or statuses. Base every statement about the workspace on tool results. If nothing matches, say so.
- Do not assume a project, board or sprint. If the question is not scoped, search the whole workspace.
- If the user refers to a project, board or sprint by name and you do not know its key or id, look it up first with list_projects, list_boards or list_sprints.
- For listing, filtering or counting, write queries in {tracker.query_language}. {tracker.query_guide}
- Use get_work_item only when the user asks about a specific item.
- If a tool returns an error, adjust the query or the approach and try again; explain the problem if you cannot resolve it.
- Answer in clear Markdown. Use bullet points or a table for lists (key, summary, status, priority). State totals, and mention when results were capped.
"""


def _to_messages(history: List[ChatTurn]) -> List[Message]:
    messages: List[Message] = []
    for turn in history[-MAX_HISTORY_TURNS:]:
        if not turn.content.strip():
            continue
        if turn.role == "assistant":
            messages.append(AssistantMessage(text=turn.content))
        else:
            messages.append(UserMessage(text=turn.content))
    return messages


async def _cached(key: Tuple[str, str, str], fetch: Callable[[], Awaitable[Any]]) -> Any:
    now = time.monotonic()
    hit = _metadata_cache.get(key)
    if hit and now - hit[0] < METADATA_TTL_SECONDS:
        return hit[1]
    value = await fetch()
    _metadata_cache[key] = (now, value)
    return value


def _build_handlers(tracker: IssueTracker, session_id: str) -> Dict[str, Callable[[Dict[str, Any]], Awaitable[Any]]]:
    def required(args: Dict[str, Any], key: str) -> str:
        value = args.get(key)
        if value in (None, ""):
            raise ValueError(f"Missing required argument '{key}'.")
        return str(value)

    def cache_key(tool: str, args: Dict[str, Any] | None = None) -> Tuple[str, str, str]:
        return (session_id, tool, json.dumps(args or {}, sort_keys=True))

    async def search(args: Dict[str, Any]) -> Any:
        max_results = int(args.get("max_results") or 50)
        return await tracker.search_work_items(query=required(args, "query"), max_results=max_results)

    async def count(args: Dict[str, Any]) -> Any:
        return {"count": await tracker.count_work_items(query=required(args, "query"))}

    async def list_boards(args: Dict[str, Any]) -> Any:
        project_key = args.get("project_key") or None
        return await _cached(
            cache_key("list_boards", {"project_key": project_key}),
            lambda: tracker.list_boards(project_key=project_key),
        )

    async def list_sprints(args: Dict[str, Any]) -> Any:
        return await tracker.list_sprints(board_id=required(args, "board_id"), state=args.get("state") or None)

    async def get_item(args: Dict[str, Any]) -> Any:
        return await tracker.get_work_item(item_id=required(args, "item_id"))

    async def projects(_: Dict[str, Any]) -> Any:
        return await _cached(cache_key("list_projects"), tracker.list_projects)

    return {
        "list_projects": projects,
        "list_boards": list_boards,
        "list_sprints": list_sprints,
        "search_work_items": search,
        "count_work_items": count,
        "get_work_item": get_item,
    }


def _tool_content(result: Any) -> str:
    text = json.dumps(result, default=str, ensure_ascii=False)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + " ...[truncated]"
    return text


async def _run_tool(
    handlers: Dict[str, Callable[[Dict[str, Any]], Awaitable[Any]]], call: ToolCall
) -> Tuple[ToolCall, Any, str]:
    handler = handlers.get(call.name)
    try:
        if handler is None:
            raise ValueError(f"Unknown tool '{call.name}'.")
        return call, await handler(call.args), "SUCCESS"
    except Exception as e:
        return call, {"error": str(e)}, "FAILED"


def _llm_details(resp: LLMResponse) -> str:
    tokens = ""
    if resp.input_tokens is not None or resp.output_tokens is not None:
        tokens = f", tokens in/out {resp.input_tokens or 0}/{resp.output_tokens or 0}"
    return f"{resp.elapsed_ms} ms{tokens}"


async def stream_answer(session_id: str, message: str, history: List[ChatTurn]) -> AsyncIterator[Dict[str, Any]]:
    """Run the agent and yield events: delta (answer text), reset (text was preamble before tool calls),
    status (tool calls in progress) and a final done event with the full answer and trace."""
    tracker = get_active_tracker(session_id)
    handlers = _build_handlers(tracker, session_id) if tracker else {}
    tools = TOOL_SPECS if tracker else []
    provider_name = tracker.name if tracker else "general"
    system_prompt = _build_system_prompt(tracker)
    messages = _to_messages(history)
    messages.append(UserMessage(text=message))
    chain = get_llm_chain()
    trace: List[AgentStepTrace] = []

    for _ in range(MAX_TOOL_ROUNDS):
        resp: Optional[LLMResponse] = None
        emitted_text = False
        try:
            async for item in chain.stream(system_prompt, messages, tools):
                if isinstance(item, LLMResponse):
                    resp = item
                else:
                    emitted_text = True
                    yield {"type": "delta", "text": item}
        except LLMUnavailableError as e:
            yield {
                "type": "done",
                "answer": f"⚠️ The AI model is busy or unreachable right now. Please try again in a moment. ({e})",
                "provider": provider_name,
                "execution_trace": trace,
            }
            return

        trace.append(
            AgentStepTrace(
                step_name=f"LLM: {resp.provider}:{resp.model}",
                tool_used="generate",
                status="SUCCESS",
                details=_llm_details(resp),
            )
        )
        messages.append(AssistantMessage(text=resp.text, tool_calls=resp.tool_calls, native=resp.native))

        if not resp.tool_calls:
            yield {
                "type": "done",
                "answer": resp.text or "I could not produce an answer. Please rephrase your question.",
                "provider": provider_name,
                "execution_trace": trace,
            }
            return

        if emitted_text:
            # The text streamed so far was a preamble to the tool calls, so the UI should discard it.
            yield {"type": "reset"}
        yield {"type": "status", "message": f"Querying {provider_name}..."}

        # Independent tool calls from one model turn run concurrently.
        outcomes = await asyncio.gather(*(_run_tool(handlers, call) for call in resp.tool_calls))
        for call, result, status in outcomes:
            trace.append(
                AgentStepTrace(
                    step_name=f"{provider_name}: {call.name}",
                    tool_used=f"{call.name}({json.dumps(call.args, ensure_ascii=False)})",
                    status=status,
                    details=_tool_content(result)[:500],
                )
            )
            messages.append(ToolResultMessage(call_id=call.id, name=call.name, content=_tool_content(result)))

    yield {
        "type": "done",
        "answer": "I could not finish within the tool-call limit. Please narrow your question.",
        "provider": provider_name,
        "execution_trace": trace,
    }


async def answer_question(session_id: str, message: str, history: List[ChatTurn]) -> ChatResponse:
    final: Dict[str, Any] = {}
    async for event in stream_answer(session_id, message, history):
        if event["type"] == "done":
            final = event
    return ChatResponse(
        answer=final.get("answer", ""),
        provider=final.get("provider"),
        execution_trace=final.get("execution_trace", []),
    )
