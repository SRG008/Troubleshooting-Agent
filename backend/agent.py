import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from backend import atlassian_mcp
from backend.atlassian_auth import AtlassianAuthError, AtlassianStatus, get_atlassian_status
from backend.auth_github import get_github_connection
from backend.github_tools import GITHUB_TOOL_SPECS, build_github_handlers
from backend.github_workspace import review_for_question
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

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 15
MAX_HISTORY_TURNS = 10
MAX_TOOL_RESULT_CHARS = 15000
# Full work item lists (compact rows, up to 200 items) need more room than other tool results.
MAX_LIST_RESULT_CHARS = 45000
PENDING_ASK_TTL_SECONDS = 1800.0
ASK_TOOL = "ask_user"
ASK_INPUT_TYPES = {"choice", "text", "number", "date", "boolean", "folder"}
APPROVE = "Approve"
REJECT = "Reject"

Handler = Callable[[Dict[str, Any]], Awaitable[Any]]


@dataclass
class _PendingAsk:
    """An agent run paused for the user: an ask_user question, or the approval of a Jira change.

    For an approval, `approvals` holds the waiting tool calls; the first one is being asked about.
    """

    session_id: str
    messages: List[Message]
    trace: List[AgentStepTrace]
    call_id: str
    created: float
    approvals: List[ToolCall] = field(default_factory=list)


@dataclass
class _RunContext:
    handlers: Dict[str, Handler]
    tools: List[ToolSpec]
    # Tools that change data. They only run after the user approves the exact call.
    approval_required: Set[str]
    provider_name: str
    system_prompt: str


_pending_asks: Dict[str, _PendingAsk] = {}

ASK_TOOL_SPEC = ToolSpec(
    name=ASK_TOOL,
    description=(
        "Ask the user ONE question and wait for the answer. The question is shown to the user as a form field. "
        "Use it whenever a required piece of information is missing (for example which project or a required field "
        "value). Never ask more than one question per turn."
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question, written for the user."},
            "input_type": {
                "type": "string",
                "enum": ["choice", "text", "number", "date", "boolean", "folder"],
                "description": (
                    "How the user answers. Use choice for a fixed set of options. "
                    "Use folder for a folder on this machine; the user picks it with a Browse button."
                ),
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Options for input_type choice. Use the real names returned by tools.",
            },
            "allow_other": {
                "type": "boolean",
                "description": "For input_type choice only: true lets the user type an answer that is not in the options.",
            },
            "details": {
                "type": "string",
                "description": (
                    "Optional Markdown shown above the question, for example the explanation of a change "
                    "the user must review before answering."
                ),
            },
            "code": {
                "type": "string",
                "description": (
                    "Optional code shown in a code viewer with a copy button, for example the exact diff of a file. "
                    "Use the exact text from the tool result, with its line breaks and indentation."
                ),
            },
            "code_language": {
                "type": "string",
                "description": "Language of code for highlighting, for example diff, csharp, python or json.",
            },
            "required": {
                "type": "boolean",
                "description": "False if the user may skip the question. Default true.",
            },
        },
        "required": ["question", "input_type"],
    },
)


def _build_system_prompt(jira: Optional[Tuple[AtlassianStatus, str]], github: Optional[Tuple[str, List[str]]] = None) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts = [_jira_prompt(*jira, today) if jira else _no_tracker_prompt(today)]
    if github is not None:
        parts.append(_github_prompt(*github))
    return "\n".join(parts)


def _no_tracker_prompt(today: str) -> str:
    return f"""You are an AI assistant for issue tracking. Current date and time: {today}.

No issue tracker is connected right now, so you cannot query any workspace. Answer general questions directly from general knowledge.
If the user asks about their workspace (projects, boards, sprints, work items, counts, or details), tell them to connect their workspace in the sidebar first. Never invent workspace data.
Answer in clear Markdown.
"""


def _github_prompt(login: str, repositories: List[str]) -> str:
    selected = ", ".join(repositories)
    return f"""
GitHub is connected as "{login}". The user selected these repositories for you: {selected}.
Only use these repositories. Never read, change, or open a pull request in any other repository.

Repository tools: list_repositories, use_local_clone, find_previous_clone, list_branches, list_pull_requests, list_repo_files, get_repo_file, search_repo_code, get_repo_history, write_repo_file, get_repo_changes, discard_repo_file, create_repo_branch, commit_and_open_pr.
- Reading never needs a local clone. list_repo_files, get_repo_file, search_repo_code, get_repo_history, list_branches and list_pull_requests read GitHub directly, so answer those without asking about a clone.
- Before any change tool (write_repo_file, create_repo_branch, get_repo_changes, discard_repo_file, commit_and_open_pr), call list_repositories and check local_folder. If it is set, use it as is and never ask about the location again. If it is null, ask in chat, one question per turn: first ask_user with input_type boolean, asking whether that repository (use its full name) is already cloned on this machine. If yes, ask_user with input_type folder for the folder of the existing clone, then call use_local_clone with it. If no, call find_previous_clone. If it returns a checkout, tell the user the repository was already cloned previously at that folder and call use_local_clone with it. If it returns null, ask_user with input_type folder for the parent folder to clone it into, then call use_local_clone with that folder. Never choose a folder yourself and never guess a path.
- Ask ONE question per turn with ask_user, then wait for the answer.
- If a repository tool returns an error, show the exact error message to the user and stop. Do not answer as if the tool succeeded, and do not guess what the repository contains.
- To describe a repository's files, call list_repo_files and report its total_files; list only the names it returns, and say so if truncated. Pass the branch when the user names one, for list_repo_files and get_repo_file.
- For questions about pull requests, call list_pull_requests and report each pull request's number, title, author, branches, and link. If it returns none, say there are no matching pull requests.
- For questions about commits, history, or activity (for example how many commits there are), call get_repo_history and report its commit_count, then list the latest commits with each commit's short sha, author, date, and message. If no tool can answer a question, say so plainly instead of guessing.

Fixing a bug or building a feature:
1. Identify the task. If the user asks to fix an issue or build a feature without giving a task or bug ID, ask for the ID (ask_user, text). If the user already gave an ID in the message, do not ask for it again. When Jira is connected, read the item with getJiraIssue to understand the request.
2. If more than one repository is selected and the request does not name one, ask which repository (ask_user, choice).
3. Analyze the code: search_repo_code and get_repo_file. Read every file you need before you change it. Never guess file contents.
4. Before changing any file, ask whether to create a new branch for this work (ask_user, boolean). If yes, ask for the branch name (ask_user, text) and call create_repo_branch with it. Then make the smallest correct change with write_repo_file on the current branch. Do not change unrelated code. Before the first of these change calls, make sure the repository has a local clone, as described above.
5. Call get_repo_changes. Explain the problem and its cause in chat.
6. For each changed file, ask whether to keep or discard it (ask_user, choice with options "Keep" and "Discard"). Do not copy the code into the code field; the app shows each file's code before and after the change automatically. Put the explanation of what changed and why in the details field. Call discard_repo_file for each discarded file.
7. Ask whether to commit and open a pull request (ask_user, boolean).
8. If yes, ask which base branch the pull request should target (ask_user, choice; options come from list_branches). Always ask the base branch, every time, even if there is only one.
9. Only after the user explicitly confirmed in step 7 and chose the base branch in step 8, call commit_and_open_pr with the branch name (the one created in step 4, or a short new name if none was created), a commit message, a pull request title, and a description. Report the pull request link.

Creating a new project or a new file set in a repository (for example "create a new project"):
- Before writing any file, the request must state everything needed to build it. Check the request for each of these: kind of application, language, framework or tool, version, name, location, and features. Ask ask_user about every item the user did not state explicitly, one question per turn, and keep asking until nothing is missing. Use choice when you can list the options, otherwise text. For a language, framework, or tool question, list the main well-known options for that kind of application (about 10 to 15, not just a few) and set allow_other to true so the user can type any other choice. Never fill a gap with your own default, and never assume something because the repository already uses it unless the user confirms it.
- The location is asked as a folder choice, never as a free-text path, and only through the clone rule above. When local_folder is already set, create the project inside that folder without asking.
- Ask the remaining questions based on the location option the user chose. For an existing folder, do not ask again for anything the user already said, and ask only what is still missing. For a new project, ask every remaining item. The location question comes right before the first write, not before the requirement questions.
- When you have enough answers, call list_repo_files and get_repo_file for any existing files that the new code must match (these read GitHub, no clone needed). Then ensure the local clone as described above, and write every new file in one turn with parallel write_repo_file calls, instead of one file per turn.
- Then continue with steps 5 to 9 above.

Never commit, push, or open a pull request without the explicit confirmation from step 7 in this conversation.
Answers about a repository must be based on tool results. Answer in clear Markdown. Show any code only in fenced code blocks with a language name, keeping the exact line breaks and indentation from the tool result.
"""


def _jira_prompt(status: AtlassianStatus, load_error: str, today: str) -> str:
    if load_error:
        return f"""You are an AI assistant for issue tracking. Current date and time: {today}.

Jira is connected, but its tools could not be loaded: {load_error}
If the user asks about Jira, show this error and ask them to reconnect Jira from the sidebar. Never invent workspace data.
Answer in clear Markdown.
"""
    if status.cloud_id:
        site = f'Use the site "{status.site_name}" ({status.site_url}), cloudId "{status.cloud_id}".'
    else:
        site = "No site is selected yet; call getAccessibleAtlassianResources to find the cloudId."
    account = status.account or "the signed-in user"
    return f"""You are an AI assistant for issue tracking, connected to Jira through the Atlassian Rovo MCP server as {account}.
Current date and time: {today}.
{site} Pass this cloudId to every tool that asks for one.

Answer the user's question by calling the Jira tools to query the live workspace: projects (also called spaces), boards, sprints, work items, comments, counts, assignees, statuses, and details.

Rules:
- If the question is not about the workspace (for example general knowledge), answer briefly from general knowledge without calling any tool.
- Never invent keys, names, counts or statuses. Base every statement about the workspace on tool results. If nothing matches, say so.
- Do not assume a project, board or sprint. If the question is not scoped, search the whole site.
- For listing, filtering or counting, use searchJiraIssuesUsingJql with JQL. Use project keys in JQL and quote values that contain spaces. Jira rejects JQL without any restriction, so for "everything" use created >= "2000-01-01".
- Work item types: when the user asks for tasks, stories, bugs, epics or subtasks, filter by type, for example issuetype = Task, issuetype = Story, issuetype = Bug, issuetype = Epic, issuetype in subTaskIssueTypes(). If a type name is not valid on this site, look up the site's types with discover and use the exact name. Add ORDER BY updated DESC.
- "All" means every matching item in every status, across all projects unless the user names one. Add a status filter only when the user asks (for example open means statusCategory != Done).
- searchJiraIssuesUsingJql in this app already follows every page and returns the matching items as compact rows (key, summary, type, status, priority, assignee, updated). Do not page it yourself. Show every row it returns in one Markdown table and state the total (returned). If complete is false, repeat its note.
- Some tools are not listed directly. Call discover with a short description of what you need (for example "list boards", "list sprints of a board", "fields required to create a work item"), then run the tool it returns with executeRead for reads, or executeWrite / executeDestructive for changes, exactly as discover describes.
- If a tool returns an error, adjust the query or the approach and try again; explain the problem if you cannot resolve it.
- Answer in clear Markdown. Use a table for lists of work items. State totals, and mention when results were capped.

Asking the user (ask_user):
- Ask only when information is missing and no tool can find it. Ask ONE question per turn, then wait for the answer.
- If the user does not name a project and there is more than one, ask which project (choice, options are "Name (KEY)").
- Ask a question only when the user's request needs it. Do not ask about optional fields unless the user wants to set them.
- An answer of null in a tool result means the user skipped that question.

Changing Jira (creating, editing, transitioning, commenting, or any other change):
- The app shows every change to the user with its exact values and runs it only after the user approves it. Do not ask a separate yes/no confirmation; make the change call once you have every value.
- If a result says the user rejected the change, it was not made. Say so, and do not retry unless the user asks again.
- To create a work item: find the project key and the work item type, then look up the fields that type requires. Ask about each required field the user has not given, one question at a time, using choice when allowed values exist. For a user field, look up the account ID by name or email and let the user choose. Then create it and report the new key and link.
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


def _tool_content(result: Any, name: str = "") -> str:
    text = json.dumps(result, default=str, ensure_ascii=False)
    limit = MAX_LIST_RESULT_CHARS if name == atlassian_mcp.SEARCH_TOOL else MAX_TOOL_RESULT_CHARS
    if len(text) > limit:
        text = text[:limit] + " ...[truncated]"
    return text


async def _run_tool(handlers: Dict[str, Handler], call: ToolCall) -> Tuple[ToolCall, Any, str]:
    handler = handlers.get(call.name)
    try:
        if handler is None:
            raise ValueError(f"Unknown tool '{call.name}'.")
        return call, await handler(call.args), "SUCCESS"
    except Exception as e:
        logger.exception("Tool %s failed", call.name)
        return call, {"error": str(e) or type(e).__name__}, "FAILED"


def _llm_details(resp: LLMResponse) -> str:
    tokens = ""
    if resp.input_tokens is not None or resp.output_tokens is not None:
        tokens = f", tokens in/out {resp.input_tokens or 0}/{resp.output_tokens or 0}"
    return f"{resp.elapsed_ms} ms{tokens}"


def _jira_handler(session_id: str, tool: Any) -> Handler:
    if tool.name == atlassian_mcp.SEARCH_TOOL:
        async def search(args: Dict[str, Any]) -> Any:
            return await atlassian_mcp.search_all_issues(session_id, tool, args)

        return search

    async def handle(args: Dict[str, Any]) -> Any:
        return await atlassian_mcp.call_tool(session_id, tool, args)

    return handle


async def _context(session_id: str) -> _RunContext:
    connection = get_github_connection(session_id)
    github = (connection.login, sorted(connection.selected)) if connection and connection.selected else None
    status = get_atlassian_status(session_id)

    handlers: Dict[str, Handler] = {}
    tools: List[ToolSpec] = []
    approval_required: Set[str] = set()
    jira: Optional[Tuple[AtlassianStatus, str]] = None
    if status.connected:
        load_error = ""
        try:
            if not status.cloud_id:
                await atlassian_mcp.load_workspace(session_id)
            mcp_tools = await atlassian_mcp.list_tools(session_id)
        except (atlassian_mcp.AtlassianMCPError, AtlassianAuthError) as e:
            mcp_tools, load_error = [], str(e)
        for tool in mcp_tools:
            if tool.name == ASK_TOOL:
                continue
            handlers[tool.name] = _jira_handler(session_id, tool)
            tools.append(atlassian_mcp.tool_spec(tool))
            if atlassian_mcp.requires_approval(tool):
                approval_required.add(tool.name)
        jira = (get_atlassian_status(session_id), load_error)
    if github:
        github_handlers = build_github_handlers(session_id)
        # The app's own GitHub tools win over a server tool with the same name.
        tools = [spec for spec in tools if spec.name not in github_handlers]
        approval_required -= set(github_handlers)
        handlers.update(github_handlers)
        tools.extend(GITHUB_TOOL_SPECS)
    if handlers:
        tools.append(ASK_TOOL_SPEC)

    if jira:
        provider_name = "Jira"
    elif github:
        provider_name = "GitHub"
    else:
        provider_name = "general"
    return _RunContext(
        handlers=handlers,
        tools=tools,
        approval_required=approval_required,
        provider_name=provider_name,
        system_prompt=_build_system_prompt(jira, github),
    )


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
        "details": str(args.get("details") or ""),
        "code": str(args.get("code") or ""),
        "code_language": str(args.get("code_language") or ""),
        "input_type": input_type,
        "options": options if input_type == "choice" else [],
        "allow_other": input_type == "choice" and args.get("allow_other") is True,
        "required": args.get("required") is not False,
    }


def _approval_event(call: ToolCall, pending_id: str, provider_name: str) -> Dict[str, Any]:
    """The approval question for one change. The values come from the stored call, not from the model's wording."""
    return {
        "type": "ask",
        "pending_id": pending_id,
        "question": f"Approve this {provider_name} change?",
        "details": f"The assistant wants to run **{call.name}** with the values below. Nothing has been changed yet.",
        "code": json.dumps(call.args or {}, indent=2, ensure_ascii=False),
        "code_language": "json",
        "input_type": "choice",
        "options": [APPROVE, REJECT],
        "allow_other": False,
        "required": True,
    }


def _prune_pending_asks() -> None:
    now = time.monotonic()
    for pending_id in [k for k, v in _pending_asks.items() if now - v.created > PENDING_ASK_TTL_SECONDS]:
        del _pending_asks[pending_id]


def _pause_for_approval(
    session_id: str, messages: List[Message], trace: List[AgentStepTrace], queue: List[ToolCall], provider_name: str
) -> Dict[str, Any]:
    _prune_pending_asks()
    pending_id = uuid.uuid4().hex
    _pending_asks[pending_id] = _PendingAsk(
        session_id=session_id,
        messages=messages,
        trace=trace,
        call_id=queue[0].id,
        created=time.monotonic(),
        approvals=list(queue),
    )
    trace.append(
        AgentStepTrace(
            step_name=f"{provider_name}: approval requested",
            tool_used=queue[0].name,
            status="WAITING",
            details=_tool_content(queue[0].args)[:500],
        )
    )
    return _approval_event(queue[0], pending_id, provider_name)


async def _run_loop(
    session_id: str, ctx: _RunContext, messages: List[Message], trace: List[AgentStepTrace]
) -> AsyncIterator[Dict[str, Any]]:
    chain = get_llm_chain()
    provider_name = ctx.provider_name

    for _ in range(MAX_TOOL_ROUNDS):
        resp: Optional[LLMResponse] = None
        emitted_text = False
        try:
            async for item in chain.stream(ctx.system_prompt, messages, ctx.tools):
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
        gated_calls = [call for call in resp.tool_calls if call.name in ctx.approval_required]
        work_calls = [
            call for call in resp.tool_calls if call.name != ASK_TOOL and call.name not in ctx.approval_required
        ]

        if work_calls:
            yield {"type": "status", "message": f"Querying {provider_name}..."}
            # Independent tool calls from one model turn run concurrently.
            outcomes = await asyncio.gather(*(_run_tool(ctx.handlers, call) for call in work_calls))
            for call, result, status in outcomes:
                trace.append(
                    AgentStepTrace(
                        step_name=f"{provider_name}: {call.name}",
                        tool_used=f"{call.name}({json.dumps(call.args, ensure_ascii=False)})",
                        status=status,
                        details=_tool_content(result)[:500],
                    )
                )
                messages.append(
                    ToolResultMessage(call_id=call.id, name=call.name, content=_tool_content(result, call.name))
                )
            if not gated_calls and all(status == "FAILED" for _, _, status in outcomes):
                # Retrying the same failing call only burns the tool-call budget, so report the error now.
                errors = " | ".join(str(result.get("error", result)) for _, result, _ in outcomes)
                yield {
                    "type": "done",
                    "answer": f"The {provider_name} request failed: {errors}",
                    "provider": provider_name,
                    "execution_trace": trace,
                }
                return

        if gated_calls:
            # Changes never run here: the run pauses and resume_answer runs each one only after the user approves it.
            for extra in ask_calls:
                messages.append(
                    ToolResultMessage(
                        call_id=extra.id,
                        name=ASK_TOOL,
                        content=json.dumps({"status": "not asked, the user is reviewing a change first"}),
                    )
                )
            yield _pause_for_approval(session_id, messages, trace, gated_calls, provider_name)
            return

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
            event = _ask_event(ask_calls[0], pending_id)
            review = review_for_question(session_id, event["question"])
            if review:
                # The real before and after code for the file the question names, shown in the chat.
                event["review"] = review
            yield event
            return

    yield {
        "type": "done",
        "answer": "I could not finish within the tool-call limit. Please narrow your question.",
        "provider": provider_name,
        "execution_trace": trace,
    }


async def stream_answer(session_id: str, message: str, history: List[ChatTurn]) -> AsyncIterator[Dict[str, Any]]:
    """Run the agent and yield events: delta (answer text), reset (text was preamble before tool calls),
    status (tool calls in progress), ask (the agent needs an answer or an approval; resume with resume_answer)
    and a final done event with the full answer and trace."""
    ctx = await _context(session_id)
    messages = _to_messages(history)
    messages.append(UserMessage(text=message))
    async for event in _run_loop(session_id, ctx, messages, []):
        yield event


async def resume_answer(session_id: str, pending_id: str, answer: Any) -> AsyncIterator[Dict[str, Any]]:
    """Continue a paused run with the user's answer (None when the question was skipped).

    For an approval, the stored call runs only when the answer is exactly "Approve".
    """
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

    ctx = await _context(session_id)
    if not ctx.handlers:
        yield {
            "type": "done",
            "answer": "The workspace was disconnected before your answer was used. Connect it again and retry.",
            "provider": "general",
            "execution_trace": pending.trace,
        }
        return

    if pending.approvals:
        call, remaining = pending.approvals[0], pending.approvals[1:]
        if answer == APPROVE:
            yield {"type": "status", "message": f"Running {call.name} in {ctx.provider_name}..."}
            _, result, status = await _run_tool(ctx.handlers, call)
        else:
            result = {"status": "rejected", "message": "The user rejected this change, so it was not made."}
            status = "REJECTED"
        pending.trace.append(
            AgentStepTrace(
                step_name=f"{ctx.provider_name}: {call.name}",
                tool_used=f"{call.name}({json.dumps(call.args, ensure_ascii=False)})",
                status=status,
                details=_tool_content(result)[:500],
            )
        )
        pending.messages.append(
            ToolResultMessage(call_id=call.id, name=call.name, content=_tool_content(result, call.name))
        )
        if remaining:
            yield _pause_for_approval(session_id, pending.messages, pending.trace, remaining, ctx.provider_name)
            return
    else:
        pending.messages.append(
            ToolResultMessage(
                call_id=pending.call_id, name=ASK_TOOL, content=json.dumps({"answer": answer}, ensure_ascii=False)
            )
        )
        pending.trace.append(
            AgentStepTrace(
                step_name="User answer",
                tool_used=ASK_TOOL,
                status="SUCCESS",
                details=json.dumps(answer, ensure_ascii=False)[:500],
            )
        )
    async for event in _run_loop(session_id, ctx, pending.messages, pending.trace):
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
