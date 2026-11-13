import asyncio
import json
import time
import uuid
from dataclasses import dataclass
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
PENDING_ASK_TTL_SECONDS = 1800.0
ASK_TOOL = "ask_user"
ASK_INPUT_TYPES = {"choice", "text", "number", "date", "boolean"}

_metadata_cache: Dict[Tuple[str, str, str], Tuple[float, Any]] = {}


@dataclass
class _PendingAsk:
    """An agent run paused on an ask_user call, waiting for the user's answer."""

    session_id: str
    messages: List[Message]
    trace: List[AgentStepTrace]
    call_id: str
    created: float


_pending_asks: Dict[str, _PendingAsk] = {}

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
    ToolSpec(
        name=ASK_TOOL,
        description=(
            "Ask the user ONE question and wait for the answer. The question is shown to the user as a form field. "
            "Use it whenever a required piece of information is missing (for example which project, a required field "
            "value, or a confirmation before creating or changing something). Never ask more than one question per turn."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question, written for the user."},
                "input_type": {
                    "type": "string",
                    "enum": ["choice", "text", "number", "date", "boolean"],
                    "description": "How the user answers. Use choice for a fixed set of options.",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Options for input_type choice. Use the real names returned by tools.",
                },
                "required": {
                    "type": "boolean",
                    "description": "False if the user may skip the question. Default true.",
                },
            },
            "required": ["question", "input_type"],
        },
    ),
    ToolSpec(
        name="list_issue_types",
        description="List the work item types (Bug, Task, Story, ...) that can be created in a project.",
        parameters={
            "type": "object",
            "properties": {"project_key": {"type": "string", "description": "Project key, e.g. KAN."}},
            "required": ["project_key"],
        },
    ),
    ToolSpec(
        name="get_required_fields",
        description=(
            "List the REQUIRED fields needed to create a work item of a type in a project, with allowed values "
            "where defined. Optional fields are not returned. Use it before create_work_item."
        ),
        parameters={
            "type": "object",
            "properties": {
                "project_key": {"type": "string", "description": "Project key, e.g. KAN."},
                "issue_type": {"type": "string", "description": "Issue type name from list_issue_types, e.g. Bug."},
            },
            "required": ["project_key", "issue_type"],
        },
    ),
    ToolSpec(
        name="search_users",
        description="Find users by name or email. Returns account ids needed for user fields such as assignee.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Part of the user's name or email."}},
            "required": ["query"],
        },
    ),
    ToolSpec(
        name="create_work_item",
        description=(
            "Create a work item. Only call it after the user asked to create one and confirmed the details. "
            "Pass every required field from get_required_fields as a field_id/value pair."
        ),
        parameters={
            "type": "object",
            "properties": {
                "project_key": {"type": "string", "description": "Project key, e.g. KAN."},
                "issue_type": {"type": "string", "description": "Issue type name, e.g. Task."},
                "values": {
                    "type": "array",
                    "description": "Field values to set.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field_id": {"type": "string", "description": "field_id from get_required_fields."},
                            "value": {"type": "string", "description": "The user's answer, as text."},
                        },
                        "required": ["field_id", "value"],
                    },
                },
            },
            "required": ["project_key", "issue_type", "values"],
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

Asking the user (ask_user):
- Ask only when information is missing and no tool can find it. Ask ONE question per turn, then wait for the answer.
- If the user does not name a project and list_projects returns more than one, ask which project (choice, options are "Name (KEY)").
- Ask a question only when the user's request needs it. Do not ask about optional fields unless the user wants to set them.

Creating a work item:
1. Make sure you know the project key (list_projects, then ask if several) and the issue type (list_issue_types, then ask if several).
2. Call get_required_fields. Ask about each required field that the user has not already given, one question at a time, using input_type choice with allowed_values as options when they exist. For a user field, ask for a name or email, call search_users, then ask the user to choose from the results.
3. Before creating, ask one confirmation question (choice Yes/No) that summarises the values.
4. Only if the user confirms, call create_work_item with field_id/value pairs. Report the new key and link. If it fails, explain the error and ask for the field that needs correcting.
- An answer of null in a tool result means the user skipped that question.
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

    async def issue_types(args: Dict[str, Any]) -> Any:
        return await tracker.list_issue_types(project_key=required(args, "project_key"))

    async def required_fields(args: Dict[str, Any]) -> Any:
        return await tracker.get_required_fields(
            project_key=required(args, "project_key"), issue_type=required(args, "issue_type")
        )

    async def users(args: Dict[str, Any]) -> Any:
        return await tracker.search_users(query=required(args, "query"))

    async def create(args: Dict[str, Any]) -> Any:
        values = {
            str(item["field_id"]): item.get("value")
            for item in args.get("values") or []
            if isinstance(item, dict) and item.get("field_id")
        }
        return await tracker.create_work_item(
            project_key=required(args, "project_key"), issue_type=required(args, "issue_type"), values=values
        )

    return {
        "list_projects": projects,
        "list_boards": list_boards,
        "list_sprints": list_sprints,
        "search_work_items": search,
        "count_work_items": count,
        "get_work_item": get_item,
        "list_issue_types": issue_types,
        "get_required_fields": required_fields,
        "search_users": users,
        "create_work_item": create,
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


def _context(session_id: str):
    tracker = get_active_tracker(session_id)
    handlers = _build_handlers(tracker, session_id) if tracker else {}
    tools = TOOL_SPECS if tracker else []
    provider_name = tracker.name if tracker else "general"
    return tracker, handlers, tools, provider_name, _build_system_prompt(tracker)


def _ask_event(call: ToolCall, pending_id: str) -> Dict[str, Any]:
    args = call.args or {}
    input_type = str(args.get("input_type") or "text")
    if input_type not in ASK_INPUT_TYPES:
        input_type = "text"
    options = [str(option) for option in args.get("options") or [] if str(option).strip()]
    if input_type == "choice" and not options:
        input_type = "text"
    return {
        "type": "ask",
        "pending_id": pending_id,
        "question": str(args.get("question") or "Please provide more information."),
        "input_type": input_type,
        "options": options if input_type == "choice" else [],
        "required": args.get("required") is not False,
    }


def _prune_pending_asks() -> None:
    now = time.monotonic()
    for pending_id in [k for k, v in _pending_asks.items() if now - v.created > PENDING_ASK_TTL_SECONDS]:
        del _pending_asks[pending_id]


async def _run_loop(
    session_id: str,
    provider_name: str,
    system_prompt: str,
    tools: List[ToolSpec],
    handlers: Dict[str, Callable[[Dict[str, Any]], Awaitable[Any]]],
    messages: List[Message],
    trace: List[AgentStepTrace],
) -> AsyncIterator[Dict[str, Any]]:
    chain = get_llm_chain()

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

        ask_calls = [call for call in resp.tool_calls if call.name == ASK_TOOL]
        work_calls = [call for call in resp.tool_calls if call.name != ASK_TOOL]

        if work_calls:
            yield {"type": "status", "message": f"Querying {provider_name}..."}
            # Independent tool calls from one model turn run concurrently.
            outcomes = await asyncio.gather(*(_run_tool(handlers, call) for call in work_calls))
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

        if ask_calls:
            # Pause here: the answer comes back through resume_answer, which appends the tool result.
            for extra in ask_calls[1:]:
                messages.append(
                    ToolResultMessage(
                        call_id=extra.id,
                        name=ASK_TOOL,
                        content=json.dumps({"status": "not asked, only one question per turn"}),
                    )
                )
            _prune_pending_asks()
            pending_id = uuid.uuid4().hex
            _pending_asks[pending_id] = _PendingAsk(
                session_id=session_id,
                messages=messages,
                trace=trace,
                call_id=ask_calls[0].id,
                created=time.monotonic(),
            )
            yield _ask_event(ask_calls[0], pending_id)
            return

    yield {
        "type": "done",
        "answer": "I could not finish within the tool-call limit. Please narrow your question.",
        "provider": provider_name,
        "execution_trace": trace,
    }


async def stream_answer(session_id: str, message: str, history: List[ChatTurn]) -> AsyncIterator[Dict[str, Any]]:
    """Run the agent and yield events: delta (answer text), reset (text was preamble before tool calls),
    status (tool calls in progress), ask (the agent needs an answer; resume with resume_answer) and a final
    done event with the full answer and trace."""
    _, handlers, tools, provider_name, system_prompt = _context(session_id)
    messages = _to_messages(history)
    messages.append(UserMessage(text=message))
    async for event in _run_loop(session_id, provider_name, system_prompt, tools, handlers, messages, []):
        yield event


async def resume_answer(session_id: str, pending_id: str, answer: Any) -> AsyncIterator[Dict[str, Any]]:
    """Continue a run paused on an ask event with the user's answer (None when the question was skipped)."""
    _prune_pending_asks()
    pending = _pending_asks.pop(pending_id, None)
    if pending is None or pending.session_id != session_id:
        yield {
            "type": "done",
            "answer": "This question has expired. Please ask your request again.",
            "provider": "general",
            "execution_trace": [],
        }
        return

    tracker, handlers, tools, provider_name, system_prompt = _context(session_id)
    if tracker is None:
        yield {
            "type": "done",
            "answer": "The workspace was disconnected before your answer was used. Connect it again and retry.",
            "provider": "general",
            "execution_trace": pending.trace,
        }
        return

    pending.messages.append(
        ToolResultMessage(call_id=pending.call_id, name=ASK_TOOL, content=json.dumps({"answer": answer}, ensure_ascii=False))
    )
    pending.trace.append(
        AgentStepTrace(
            step_name="User answer",
            tool_used=ASK_TOOL,
            status="SUCCESS",
            details=json.dumps(answer, ensure_ascii=False)[:500],
        )
    )
    async for event in _run_loop(
        session_id, provider_name, system_prompt, tools, handlers, pending.messages, pending.trace
    ):
        yield event


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
