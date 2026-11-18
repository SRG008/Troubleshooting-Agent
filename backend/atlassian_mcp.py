"""Client for the Atlassian Rovo MCP server and the bridge that offers its tools to the agent.

The agent gets Rovo's own tools (listed by the server, never hardcoded). Each call opens a short MCP
session with the user's credentials from atlassian_auth. Tools that change Jira need the user's
approval, which the agent loop enforces before the call is sent (see requires_approval).
"""

import json
import logging
import time
import urllib.parse
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, TypeVar

from mcp import Client, types
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from backend.atlassian_auth import (
    API_TOKEN_MODE,
    AtlassianSite,
    authorization_header,
    can_refresh,
    drop_connection,
    get_atlassian_status,
    set_workspace,
)
from backend.config import settings
from backend.llm import ToolSpec

logger = logging.getLogger(__name__)

T = TypeVar("T")
TOOL_LIST_TTL_SECONDS = 600.0

# Tools that only read, even though their names do not start with a read verb.
READ_ONLY_TOOLS = {"atlassianUserInfo", "getAccessibleAtlassianResources", "discover", "executeRead", "getContentFormatGuide"}
# Rovo runs its deferred (discovered) tools through these; the tier says whether they change data.
APPROVAL_TOOLS = {"executeWrite", "executeDestructive"}
READ_PREFIXES = ("get", "list", "search", "lookup", "fetch")
WRITE_PREFIXES = (
    "create", "edit", "update", "delete", "add", "transition", "manage", "upload", "watch", "convert", "move", "remove",
)

_tool_cache: Dict[str, Tuple[float, List[types.Tool]]] = {}


class AtlassianMCPError(Exception):
    """A user-facing error from the Atlassian MCP server or the connection to it."""


class AtlassianRejectedError(AtlassianMCPError):
    """Atlassian answered 401: the token or API token is not accepted."""


def requires_approval(tool: types.Tool) -> bool:
    """True when a tool may change data. Unknown tools need approval, so a new write tool is never sent unasked."""
    name = tool.name
    if name in APPROVAL_TOOLS or name.startswith(WRITE_PREFIXES):
        return True
    if name in READ_ONLY_TOOLS:
        return False
    if tool.annotations is not None and tool.annotations.read_only_hint is True:
        return False
    return not name.startswith(READ_PREFIXES)


def _root_error(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


async def _with_client(session_id: str, action: Callable[[Client], Awaitable[T]]) -> T:
    """Runs one MCP session with the user's credentials. A rejected OAuth token is refreshed and retried once."""
    force_refresh = False
    for attempt in range(2):
        header = await authorization_header(session_id, force_refresh=force_refresh)
        rejected = False

        async def note_rejection(response: Any) -> None:
            nonlocal rejected
            if response.status_code == 401:
                rejected = True

        http = create_mcp_http_client(headers={"Authorization": header})
        http.event_hooks = {"response": [note_rejection]}
        try:
            async with http, Client(streamable_http_client(settings.atlassian_mcp_url, http_client=http)) as client:
                return await action(client)
        except Exception as exc:
            if not rejected:
                logger.exception("Atlassian MCP request failed")
                raise AtlassianMCPError(f"Could not reach the Atlassian MCP server: {_root_error(exc)}") from exc
            if attempt == 0 and can_refresh(session_id):
                force_refresh = True
                continue
            drop_connection(session_id)
            raise AtlassianRejectedError(
                "Atlassian rejected the saved sign-in. Connect Jira again from the sidebar."
            ) from exc
    raise AssertionError("unreachable")


async def list_tools(session_id: str) -> List[types.Tool]:
    """The tools the server offers this user, cached for a few minutes per session."""
    hit = _tool_cache.get(session_id)
    if hit and time.monotonic() - hit[0] < TOOL_LIST_TTL_SECONDS:
        return hit[1]

    async def fetch(client: Client) -> List[types.Tool]:
        tools: List[types.Tool] = []
        cursor: Optional[str] = None
        while True:
            page = await client.list_tools(cursor=cursor)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if not cursor:
                return tools

    tools = await _with_client(session_id, fetch)
    _tool_cache[session_id] = (time.monotonic(), tools)
    return tools


def forget_tools(session_id: str) -> None:
    _tool_cache.pop(session_id, None)


def _result_value(name: str, result: types.CallToolResult) -> Any:
    text = "\n".join(block.text for block in result.content if isinstance(block, types.TextContent))
    if result.is_error:
        raise AtlassianMCPError(text or f"{name} failed.")
    if result.structured_content is not None:
        return result.structured_content
    try:
        return json.loads(text)
    except ValueError:
        return text


def _resolve(schema: Any, root: Dict[str, Any]) -> Any:
    if isinstance(schema, dict) and isinstance(schema.get("$ref"), str):
        ref = schema["$ref"]
        if ref.startswith("#/"):
            target: Any = root
            for part in ref[2:].split("/"):
                target = target.get(part, {}) if isinstance(target, dict) else {}
            return target
    return schema


def coerce_arguments(schema: Any, value: Any, root: Optional[Dict[str, Any]] = None) -> Any:
    """Turns JSON text back into objects and arrays where the tool's schema expects them.

    Some LLM adapters must declare free-form object fields as strings, so the model may send them as JSON text.
    """
    root = root if root is not None else (schema if isinstance(schema, dict) else {})
    schema = _resolve(schema, root)
    if not isinstance(schema, dict):
        return value
    variants = schema.get("anyOf") or schema.get("oneOf")
    if variants and "type" not in schema:
        # Same choice as the Gemini schema conversion: the first variant that is not null.
        concrete = [v for v in variants if not (isinstance(v, dict) and v.get("type") == "null")]
        chosen = _resolve(concrete[0], root) if concrete else {}
        siblings = {k: v for k, v in schema.items() if k not in ("anyOf", "oneOf")}
        return coerce_arguments({**(chosen if isinstance(chosen, dict) else {}), **siblings}, value, root)
    types_ = schema.get("type")
    expects = set(types_ if isinstance(types_, list) else [types_]) if types_ else set()
    if isinstance(value, str) and expects & {"object", "array"}:
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        if isinstance(parsed, (dict, list)):
            value = parsed
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        return {key: coerce_arguments(properties.get(key), item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [coerce_arguments(schema.get("items"), item, root) for item in value]
    return value


async def call_tool(session_id: str, tool: types.Tool, arguments: Dict[str, Any]) -> Any:
    arguments = coerce_arguments(tool.input_schema, arguments or {})
    result = await _with_client(session_id, lambda client: client.call_tool(tool.name, arguments))
    return _result_value(tool.name, result)


SEARCH_TOOL = "searchJiraIssuesUsingJql"
MAX_SEARCH_ISSUES = 200
SEARCH_PAGE_SIZE = 100
# A JQL date restriction that matches every work item (Jira rejects JQL without any restriction).
EVERYTHING_DATE = '"2000-01-01"'


def _is_query_error(reply: str) -> bool:
    """True when Jira rejected the query itself (HTTP 400), which happens only after authentication and scopes passed."""
    compact = reply.replace(" ", "")
    return '"statusCode":400' in compact and "scope" not in reply.lower()
MISSING_SCOPES_MESSAGE = (
    "Atlassian accepted this API token, but it has no Rovo MCP Jira scopes, so Jira refuses every request. "
    "This happens with a classic token (made with 'Create API token') or one with classic scopes such as "
    "read:jira-work. Create a new one with 'Create API token with scopes', choose the Rovo MCP app, and select "
    "read:jira:agent-interface, search:jira:agent-interface, read:me and read:account (plus "
    "write:jira:agent-interface to create or edit work items)."
)
ACCOUNT_SCOPES_MESSAGE = (
    "Atlassian accepted this API token, but it cannot look up the sites and products of your account, so every "
    "Jira tool fails. Create a new token with scopes for the Rovo MCP app that also includes read:me and "
    "read:account, next to read:jira:agent-interface and search:jira:agent-interface."
)


def _label(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        return value.get("name") or value.get("displayName") or value.get("value")
    return value


def _compact_issue(issue: Dict[str, Any]) -> Dict[str, Any]:
    fields = issue.get("fields") or {}
    return {
        "key": issue.get("key"),
        "summary": fields.get("summary"),
        "type": _label(fields.get("issuetype")),
        "status": _label(fields.get("status")),
        "priority": _label(fields.get("priority")) or "None",
        "assignee": _label(fields.get("assignee")) or "Unassigned",
        "updated": str(fields.get("updated") or "")[:10],
    }


async def search_all_issues(session_id: str, tool: types.Tool, arguments: Dict[str, Any]) -> Any:
    """Runs a JQL search and follows every page, so "show all tasks" really lists all of them (up to a cap).

    Requests for counts, specific fields, or a richer view go to the server unchanged.
    """
    wants_rows = (arguments.get("searchResultMode") or "issues") == "issues"
    if not wants_rows or arguments.get("fields") or (arguments.get("view") or "compact") != "compact":
        return await call_tool(session_id, tool, arguments)

    args = {key: value for key, value in arguments.items() if key != "nextPageToken"}
    args["maxResults"] = SEARCH_PAGE_SIZE
    issues: List[Dict[str, Any]] = []
    complete = False
    while len(issues) < MAX_SEARCH_ISSUES:
        page = await call_tool(session_id, tool, args)
        data = page.get("data", page) if isinstance(page, dict) else {}
        batch = data.get("issues") if isinstance(data, dict) else None
        if not isinstance(batch, list):
            return page
        issues.extend(item for item in batch if isinstance(item, dict))
        token = data.get("nextPageToken")
        if data.get("isLast", True) or not token or not batch:
            complete = True
            break
        args["nextPageToken"] = token
    complete = complete and len(issues) <= MAX_SEARCH_ISSUES
    return {
        "returned": min(len(issues), MAX_SEARCH_ISSUES),
        "complete": complete,
        "note": "" if complete else f"Only the first {MAX_SEARCH_ISSUES} matching items are listed. Narrow the JQL to see the rest.",
        "issues": [_compact_issue(issue) for issue in issues[:MAX_SEARCH_ISSUES]],
    }


def tool_spec(tool: types.Tool) -> ToolSpec:
    parameters = tool.input_schema or {"type": "object", "properties": {}}
    return ToolSpec(name=tool.name, description=tool.description or tool.title or tool.name, parameters=parameters)


def _first_list(value: Any) -> List[Any]:
    """The first list of objects in a tool result, searching nested wrappers such as {"data": {"resources": [...]}}."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        if value.get("id") or value.get("cloudId"):
            return [value]
        for item in value.values():
            found = _first_list(item) if isinstance(item, (dict, list)) else []
            if found:
                return found
    return []


def parse_sites(value: Any) -> List[AtlassianSite]:
    sites: Dict[str, AtlassianSite] = {}
    for item in _first_list(value):
        if not isinstance(item, dict):
            continue
        cloud_id = item.get("cloudId") or item.get("id")
        if cloud_id and str(cloud_id) not in sites:
            url = str(item.get("url") or "")
            name = item.get("name") or urllib.parse.urlsplit(url).hostname or url or cloud_id
            sites[str(cloud_id)] = AtlassianSite(id=str(cloud_id), name=str(name), url=url)
    return list(sites.values())


def parse_account(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return ""
    if isinstance(value, dict) and isinstance(value.get("data"), dict):
        value = value["data"]
    if not isinstance(value, dict):
        return ""
    for key in ("name", "displayName", "nickname", "email"):
        if value.get(key):
            return str(value[key])
    return ""


async def _verify_api_token(session_id: str, cloud_id: str) -> None:
    """Checks an API token with a one-item Jira search. On failure the error names the tools the token
    allows and Atlassian's own reply, which tells a wrongly scoped token apart from an admin restriction."""
    # Jira refuses JQL without a restriction ("unbounded"), so the check filters on a date that matches everything.
    probe = {"cloudId": cloud_id, "jql": f"created >= {EVERYTHING_DATE} ORDER BY created DESC", "maxResults": 1}

    async def check(client: Client) -> Tuple[List[str], types.CallToolResult]:
        page = await client.list_tools()
        return [tool.name for tool in page.tools], await client.call_tool(SEARCH_TOOL, probe)

    names, result = await _with_client(session_id, check)
    try:
        _result_value(SEARCH_TOOL, result)
        return
    except AtlassianMCPError as e:
        reply = str(e)
    if _is_query_error(reply):
        # Jira only judges the query after the token and its scopes were accepted.
        logger.warning("API token check query was rejected, but the token was accepted: %s", reply)
        return
    logger.warning("API token check on %s failed. Tools: %s. Atlassian replied: %s", cloud_id, names, reply)
    allowed = ", ".join(names) or "none"
    if SEARCH_TOOL not in names:
        raise AtlassianMCPError(
            f"{MISSING_SCOPES_MESSAGE} Tools this token allows: {allowed}. Atlassian replied: {reply}"
        )
    if "accessible products" in reply.lower():
        raise AtlassianMCPError(f"{ACCOUNT_SCOPES_MESSAGE} Atlassian replied: {reply}")
    raise AtlassianMCPError(
        "Atlassian accepted this API token and lists the Jira tools for it, but refused the Jira search. "
        "Check that the token has search:jira:agent-interface, and that in Atlassian Administration > Connectors > "
        "Atlassian MCP server the Jira read and search permission groups are allowed for API tokens. "
        f"Tools this token allows: {allowed}. Atlassian replied: {reply}"
    )


async def load_workspace(session_id: str) -> None:
    """Verifies the credentials and saves the user's Atlassian sites and account name.

    API tokens cannot list sites (getAccessibleAtlassianResources needs an OAuth token), so a token
    connection keeps the site the user named and is verified with a one-item Jira search instead.
    """
    status = get_atlassian_status(session_id)
    forget_tools(session_id)
    if status.auth_mode == API_TOKEN_MODE and status.cloud_id:
        await _verify_api_token(session_id, status.cloud_id)
        return

    async def fetch(client: Client) -> Tuple[types.CallToolResult, Optional[types.CallToolResult]]:
        resources = await client.call_tool("getAccessibleAtlassianResources", {})
        try:
            user = await client.call_tool("atlassianUserInfo", {})
        except Exception:
            user = None
        return resources, user

    resources, user = await _with_client(session_id, fetch)
    sites = parse_sites(_result_value("getAccessibleAtlassianResources", resources))
    account = ""
    if user is not None and not user.is_error:
        account = parse_account(_result_value("atlassianUserInfo", user))
    set_workspace(session_id, sites, account)
