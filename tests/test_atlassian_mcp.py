import asyncio
import base64
import hashlib
import json
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import httpx
from mcp import Client as MCPClient
from mcp import types
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from backend import agent, atlassian_auth, atlassian_mcp
from backend.atlassian_auth import AtlassianAuthError, APITokenConnectRequest
from backend.config import settings
from backend.llm import LLMResponse, ToolCall, ToolResultMessage
from backend.llm.gemini import _gemini_schema

MCP_URL = "https://mcp.example.com/v2/mcp"
ISSUER = "https://auth.example.com/issuer"


class _TempStore(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.store_file = base / "connections.enc"
        for target, value in (
            ("ATLASSIAN_STATE_DIR", base),
            ("ATLASSIAN_KEY_FILE", base / "store.key"),
            ("ATLASSIAN_STORE_FILE", self.store_file),
        ):
            patcher = patch.object(atlassian_auth, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for store in (
            atlassian_auth.ATLASSIAN_SESSION_STORE,
            atlassian_auth.PENDING_LOGINS,
            atlassian_auth._registration,
            atlassian_auth._server_metadata,
        ):
            store.clear()
            self.addCleanup(store.clear)
        patcher = patch.object(settings, "atlassian_mcp_url", MCP_URL)
        patcher.start()
        self.addCleanup(patcher.stop)


class OAuthTests(_TempStore):
    def setUp(self):
        super().setUp()
        self.registrations = 0
        self.token_requests: List[Dict[str, str]] = []
        self.unexpected: List[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == "https://mcp.example.com/.well-known/oauth-protected-resource/v2/mcp":
                return httpx.Response(200, json={"resource": MCP_URL, "authorization_servers": [ISSUER]})
            if url == "https://auth.example.com/.well-known/oauth-authorization-server/issuer":
                return httpx.Response(
                    200,
                    json={
                        "authorization_endpoint": "https://auth.example.com/authorize",
                        "token_endpoint": "https://auth.example.com/oauth/token",
                        "registration_endpoint": "https://auth.example.com/issuer/dcr/register",
                    },
                )
            if url == "https://auth.example.com/issuer/dcr/register":
                self.registrations += 1
                self.registration_body = json.loads(request.content)
                return httpx.Response(201, json={"client_id": f"client-{self.registrations}"})
            if url == "https://auth.example.com/oauth/token":
                form = dict(urllib.parse.parse_qsl(request.content.decode()))
                self.token_requests.append(form)
                if form["grant_type"] == "refresh_token":
                    return httpx.Response(200, json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600})
                return httpx.Response(200, json={"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600})
            if url == "https://acme.atlassian.net/_edge/tenant_info":
                return httpx.Response(200, json={"cloudId": "cloud-9"})
            self.unexpected.append(url)
            return httpx.Response(404)

        patcher = patch.object(
            atlassian_auth, "_http_client", side_effect=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _login(self, session_id: str) -> Dict[str, str]:
        url = asyncio.run(atlassian_auth.build_oauth_url(session_id))
        return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))

    def test_login_url_uses_pkce_resource_and_a_new_random_state_each_time(self):
        first = self._login("session-1")
        second = self._login("session-1")

        self.assertEqual(first["code_challenge_method"], "S256")
        self.assertEqual(first["resource"], MCP_URL)
        self.assertEqual(first["client_id"], "client-1")
        self.assertNotEqual(first["state"], "session-1")
        self.assertNotEqual(first["state"], second["state"])
        self.assertNotEqual(first["code_challenge"], second["code_challenge"])
        self.assertEqual(self.registrations, 1, "the dynamic client registration is reused")

    def test_consent_scopes_are_registered_and_requested_even_when_not_configured(self):
        with patch.object(settings, "atlassian_mcp_scopes", "read:jira:agent-interface search:jira:agent-interface"):
            params = self._login("session-1")

        for scope in ("read:me", "read:account", "offline_access", "read:jira:agent-interface"):
            self.assertIn(scope, params["scope"].split())
            self.assertIn(scope, self.registration_body["scope"].split())
        self.assertEqual(self.registration_body["grant_types"], ["authorization_code", "refresh_token"])

    def test_changing_scopes_registers_a_new_client(self):
        self._login("session-1")
        with patch.object(settings, "atlassian_mcp_scopes", "read:jira:agent-interface"):
            params = self._login("session-1")

        self.assertEqual(self.registrations, 2)
        self.assertEqual(params["client_id"], "client-2")

    def test_callback_proves_the_code_verifier_and_the_state_works_only_once(self):
        params = self._login("session-1")

        session_id = asyncio.run(atlassian_auth.complete_oauth(code="code-1", state=params["state"]))

        self.assertEqual(session_id, "session-1")
        verifier = self.token_requests[0]["code_verifier"]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(challenge, params["code_challenge"])
        with self.assertRaises(AtlassianAuthError):
            asyncio.run(atlassian_auth.complete_oauth(code="code-1", state=params["state"]))

    def test_forged_state_cannot_attach_an_account_to_another_session(self):
        with self.assertRaises(AtlassianAuthError):
            asyncio.run(atlassian_auth.complete_oauth(code="code-1", state="victim-session"))
        self.assertEqual(atlassian_auth.ATLASSIAN_SESSION_STORE, {})

    def test_tokens_are_hidden_from_status_and_encrypted_on_disk(self):
        params = self._login("session-1")
        asyncio.run(atlassian_auth.complete_oauth(code="code-1", state=params["state"]))

        status = atlassian_auth.get_atlassian_status("session-1")
        self.assertTrue(status.connected)
        self.assertNotIn("access-1", status.model_dump_json())
        self.assertNotIn(b"access-1", self.store_file.read_bytes())
        self.assertNotIn(b"refresh-1", self.store_file.read_bytes())

    def test_expiring_token_is_refreshed_before_use(self):
        params = self._login("session-1")
        asyncio.run(atlassian_auth.complete_oauth(code="code-1", state=params["state"]))
        atlassian_auth.ATLASSIAN_SESSION_STORE["session-1"].expires_at = time.time() + 5

        header = asyncio.run(atlassian_auth.authorization_header("session-1"))

        self.assertEqual(header, "Bearer access-2")
        self.assertEqual(self.token_requests[-1]["refresh_token"], "refresh-1")

    def test_api_token_is_sent_as_basic_auth_for_the_named_site(self):
        request = APITokenConnectRequest(site_url="acme.atlassian.net/jira", email="a@acme.com", api_token="tok")
        asyncio.run(atlassian_auth.connect_api_token("session-1", request))

        header = asyncio.run(atlassian_auth.authorization_header("session-1"))
        self.assertEqual(header, "Basic " + base64.b64encode(b"a@acme.com:tok").decode())
        status = atlassian_auth.get_atlassian_status("session-1")
        self.assertEqual((status.cloud_id, status.site_url), ("cloud-9", "https://acme.atlassian.net"))

    def test_api_token_site_must_be_an_atlassian_cloud_host(self):
        for site_url in ("https://evil.example.com", "http://acme.atlassian.net", "https://acme.atlassian.net.evil.com"):
            request = APITokenConnectRequest(site_url=site_url, email="a@acme.com", api_token="tok")
            with self.assertRaises(AtlassianAuthError, msg=site_url):
                asyncio.run(atlassian_auth.connect_api_token("session-1", request))
        self.assertEqual(self.unexpected, [], "no request is sent to a host that is not Atlassian Cloud")
        self.assertFalse(atlassian_auth.is_connected("session-1"))


def _fake_rovo(received: List[Dict[str, Any]]) -> MCPServer:
    server = MCPServer("fake-rovo")

    @server.tool(name="getAccessibleAtlassianResources")
    def resources() -> list[dict]:
        return [{"id": "cloud-1", "name": "Acme", "url": "https://acme.atlassian.net", "scopes": []}]

    @server.tool(name="atlassianUserInfo")
    def user_info() -> dict:
        return {"name": "Alice", "email": "alice@acme.com"}

    @server.tool(name="getJiraIssue", annotations=types.ToolAnnotations(read_only_hint=True))
    def get_issue(cloudId: str, issueIdOrKey: str) -> dict:
        raise ToolError(f"Issue {issueIdOrKey} does not exist")

    @server.tool(name="createJiraIssue")
    def create_issue(cloudId: str, projectKey: str, summary: str, additional_fields: dict) -> dict:
        received.append({"cloudId": cloudId, "projectKey": projectKey, "summary": summary, "fields": additional_fields})
        return {"key": "KAN-9"}

    @server.tool(name="searchJiraIssuesUsingJql")
    def search(cloudId: str, jql: str, maxResults: int = 50) -> dict:
        if cloudId == "no-scopes":
            raise ToolError('{"error":true,"message":"Unable to resolve user scopes from the user-context token","statusCode":401}')
        if cloudId == "bad-query":
            raise ToolError('{"error":true,"message":"JQL validation failed: Date value is invalid.","statusCode":400}')
        received.append({"search": cloudId, "jql": jql, "maxResults": maxResults})
        return {"issues": [], "isLast": True}

    return server


class MCPBridgeTests(_TempStore):
    def setUp(self):
        super().setUp()
        self.received: List[Dict[str, Any]] = []
        server = _fake_rovo(self.received)
        # The real SDK client, connected in-process to the fake server instead of over HTTP.
        patcher = patch.object(atlassian_mcp, "Client", side_effect=lambda _transport: MCPClient(server))
        patcher.start()
        self.addCleanup(patcher.stop)
        atlassian_auth.ATLASSIAN_SESSION_STORE["session-1"] = atlassian_auth._Connection(
            auth_mode=atlassian_auth.OAUTH_MODE, access_token="tok"
        )
        atlassian_mcp.forget_tools("session-1")
        self.addCleanup(atlassian_mcp.forget_tools, "session-1")

    def _tool(self, name: str) -> types.Tool:
        tools = asyncio.run(atlassian_mcp.list_tools("session-1"))
        return next(tool for tool in tools if tool.name == name)

    def test_workspace_sites_and_account_come_from_the_server(self):
        asyncio.run(atlassian_mcp.load_workspace("session-1"))

        status = atlassian_auth.get_atlassian_status("session-1")
        self.assertEqual(status.account, "Alice")
        self.assertEqual(status.cloud_id, "cloud-1")
        self.assertEqual(status.site_url, "https://acme.atlassian.net")

    def test_api_token_connection_is_verified_with_a_search_on_its_own_site(self):
        site = atlassian_auth.AtlassianSite(id="cloud-9", name="acme.atlassian.net", url="https://acme.atlassian.net")
        atlassian_auth.ATLASSIAN_SESSION_STORE["session-1"] = atlassian_auth._Connection(
            auth_mode=atlassian_auth.API_TOKEN_MODE, access_token="tok", email="a@acme.com", account="a@acme.com",
            sites=[site], cloud_id="cloud-9",
        )

        asyncio.run(atlassian_mcp.load_workspace("session-1"))

        self.assertEqual(
            self.received,
            [{"search": "cloud-9", "jql": 'created >= "2000-01-01" ORDER BY created DESC', "maxResults": 1}],
        )
        status = atlassian_auth.get_atlassian_status("session-1")
        self.assertEqual((status.cloud_id, status.account), ("cloud-9", "a@acme.com"))

    def test_api_token_is_accepted_when_jira_only_rejects_the_check_query(self):
        site = atlassian_auth.AtlassianSite(id="bad-query", name="acme.atlassian.net", url="https://acme.atlassian.net")
        atlassian_auth.ATLASSIAN_SESSION_STORE["session-1"] = atlassian_auth._Connection(
            auth_mode=atlassian_auth.API_TOKEN_MODE, access_token="tok", email="a@acme.com", sites=[site], cloud_id="bad-query",
        )

        asyncio.run(atlassian_mcp.load_workspace("session-1"))

        self.assertTrue(atlassian_auth.is_connected("session-1"))

    def test_api_token_refused_by_jira_shows_atlassians_reply_and_the_allowed_tools(self):
        site = atlassian_auth.AtlassianSite(id="no-scopes", name="acme.atlassian.net", url="https://acme.atlassian.net")
        atlassian_auth.ATLASSIAN_SESSION_STORE["session-1"] = atlassian_auth._Connection(
            auth_mode=atlassian_auth.API_TOKEN_MODE, access_token="tok", email="a@acme.com", sites=[site], cloud_id="no-scopes",
        )

        with self.assertRaises(atlassian_mcp.AtlassianMCPError) as caught:
            asyncio.run(atlassian_mcp.load_workspace("session-1"))

        message = str(caught.exception)
        self.assertIn("refused the Jira search", message)
        self.assertIn("Unable to resolve user scopes", message)
        self.assertIn("searchJiraIssuesUsingJql", message)

    def test_object_fields_sent_as_json_text_reach_the_server_as_objects(self):
        result = asyncio.run(
            atlassian_mcp.call_tool(
                "session-1",
                self._tool("createJiraIssue"),
                {"cloudId": "cloud-1", "projectKey": "KAN", "summary": "Login fails", "additional_fields": '{"priority": {"name": "High"}}'},
            )
        )

        self.assertEqual(self.received[0]["fields"], {"priority": {"name": "High"}})
        self.assertEqual(result["key"], "KAN-9")

    def test_tool_error_is_raised_with_the_server_message(self):
        with self.assertRaisesRegex(atlassian_mcp.AtlassianMCPError, "KAN-404 does not exist"):
            asyncio.run(
                atlassian_mcp.call_tool(
                    "session-1", self._tool("getJiraIssue"), {"cloudId": "cloud-1", "issueIdOrKey": "KAN-404"}
                )
            )

    def test_only_tools_that_can_change_data_need_approval(self):
        self.assertTrue(atlassian_mcp.requires_approval(self._tool("createJiraIssue")))
        self.assertFalse(atlassian_mcp.requires_approval(self._tool("getJiraIssue")))
        self.assertFalse(atlassian_mcp.requires_approval(self._tool("atlassianUserInfo")))
        for name, expected in (
            ("executeWrite", True),
            ("executeDestructive", True),
            ("executeRead", False),
            ("searchJiraIssuesUsingJql", False),
            ("someNewTool", True),
        ):
            tool = types.Tool(name=name, input_schema={"type": "object", "properties": {}})
            self.assertEqual(atlassian_mcp.requires_approval(tool), expected, name)


class _ScriptedChain:
    """Returns the next scripted model turn on every call."""

    def __init__(self, turns: List[LLMResponse]):
        self.turns = list(turns)
        self.seen_messages: List[List[Any]] = []

    async def stream(self, system, messages, tools):
        self.seen_messages.append(list(messages))
        yield self.turns.pop(0)


def _turn(*calls: ToolCall, text: str = "") -> LLMResponse:
    return LLMResponse(text=text, tool_calls=list(calls), provider="fake", model="fake", elapsed_ms=1)


class ApprovalGateTests(unittest.TestCase):
    def setUp(self):
        self.created: List[Dict[str, Any]] = []
        self.searched: List[Dict[str, Any]] = []

        async def create(args):
            self.created.append(args)
            return {"key": "KAN-9"}

        async def search(args):
            self.searched.append(args)
            return {"issues": []}

        async def context(_session_id):
            return agent._RunContext(
                handlers={"createJiraIssue": create, "searchJiraIssuesUsingJql": search},
                tools=[],
                approval_required={"createJiraIssue"},
                provider_name="Jira",
                system_prompt="",
            )

        patcher = patch.object(agent, "_context", side_effect=context)
        patcher.start()
        self.addCleanup(patcher.stop)
        agent._pending_asks.clear()

    def _run(self, chain: _ScriptedChain, events_source) -> List[Dict[str, Any]]:
        async def collect():
            return [event async for event in events_source]

        with patch.object(agent, "get_llm_chain", return_value=chain):
            return asyncio.run(collect())

    def _start(self, chain: _ScriptedChain) -> Dict[str, Any]:
        events = self._run(chain, agent.stream_answer("session-1", "Create a bug", []))
        return events[-1]

    def _resume(self, chain: _ScriptedChain, pending_id: str, answer: Any) -> List[Dict[str, Any]]:
        return self._run(chain, agent.resume_answer("session-1", pending_id, answer))

    def _create_call(self) -> ToolCall:
        return ToolCall(id="call-1", name="createJiraIssue", args={"projectKey": "KAN", "summary": "Login fails"})

    def test_change_waits_for_approval_and_runs_with_the_stored_arguments(self):
        chain = _ScriptedChain([_turn(self._create_call()), _turn(text="Created KAN-9.")])

        ask = self._start(chain)

        self.assertEqual(ask["type"], "ask")
        self.assertEqual(ask["options"], ["Approve", "Reject"])
        self.assertIn('"summary": "Login fails"', ask["code"])
        self.assertEqual(self.created, [], "nothing runs before approval")

        events = self._resume(chain, ask["pending_id"], "Approve")

        self.assertEqual(self.created, [{"projectKey": "KAN", "summary": "Login fails"}])
        self.assertEqual(events[-1]["answer"], "Created KAN-9.")

    def test_rejected_or_ambiguous_answers_never_run_the_change(self):
        for answer in ("Reject", True, "approve", None):
            chain = _ScriptedChain([_turn(self._create_call()), _turn(text="Not created.")])
            ask = self._start(chain)

            self._resume(chain, ask["pending_id"], answer)

            self.assertEqual(self.created, [], repr(answer))
            result = next(m for m in chain.seen_messages[-1] if isinstance(m, ToolResultMessage))
            self.assertIn("rejected", result.content)

    def test_reads_in_the_same_turn_run_while_the_change_waits(self):
        search = ToolCall(id="call-0", name="searchJiraIssuesUsingJql", args={"jql": "project = KAN"})
        chain = _ScriptedChain([_turn(search, self._create_call())])

        ask = self._start(chain)

        self.assertEqual(ask["type"], "ask")
        self.assertEqual(self.searched, [{"jql": "project = KAN"}])
        self.assertEqual(self.created, [])

    def test_approval_belongs_to_the_session_that_asked(self):
        chain = _ScriptedChain([_turn(self._create_call())])
        ask = self._start(chain)

        events = self._run(chain, agent.resume_answer("session-2", ask["pending_id"], "Approve"))

        self.assertIn("expired", events[-1]["answer"])
        self.assertEqual(self.created, [])


class WorkItemListTests(unittest.TestCase):
    SEARCH = types.Tool(name="searchJiraIssuesUsingJql", input_schema={"type": "object", "properties": {}})

    def _issue(self, n: int, kind: str = "Task") -> Dict[str, Any]:
        return {
            "key": f"KAN-{n}",
            "fields": {
                "summary": f"Item {n}",
                "issuetype": {"name": kind},
                "status": {"name": "To Do", "statusCategory": {"name": "To Do"}},
                "priority": {"name": "Medium"},
                "assignee": None,
                "updated": "2026-11-13T21:41:33.668+0530",
            },
        }

    def _search(self, pages: List[Dict[str, Any]], arguments: Dict[str, Any]):
        sent: List[Dict[str, Any]] = []

        async def fake_call(_session_id, _tool, args):
            sent.append(dict(args))
            return pages[len(sent) - 1]

        with patch.object(atlassian_mcp, "call_tool", side_effect=fake_call):
            result = asyncio.run(atlassian_mcp.search_all_issues("session-1", self.SEARCH, arguments))
        return result, sent

    def test_sites_are_read_from_the_nested_rovo_response(self):
        value = {"data": {"resources": [{"cloudId": "c-1", "url": "https://acme.atlassian.net", "products": []}]}}
        sites = atlassian_mcp.parse_sites(value)
        self.assertEqual([(s.id, s.name, s.url) for s in sites], [("c-1", "acme.atlassian.net", "https://acme.atlassian.net")])

    def test_every_page_is_followed_and_rows_are_compact(self):
        pages = [
            {"issues": [self._issue(n) for n in range(1, 101)], "isLast": False, "nextPageToken": "p2"},
            {"issues": [self._issue(n) for n in range(101, 131)], "isLast": True},
        ]

        result, sent = self._search(pages, {"cloudId": "c-1", "jql": "issuetype = Task", "maxResults": 10})

        self.assertEqual([call.get("nextPageToken") for call in sent], [None, "p2"])
        self.assertEqual(sent[0]["maxResults"], 100)
        self.assertTrue(result["complete"])
        self.assertEqual(result["returned"], 130)
        self.assertEqual(
            result["issues"][0],
            {"key": "KAN-1", "summary": "Item 1", "type": "Task", "status": "To Do", "priority": "Medium",
             "assignee": "Unassigned", "updated": "2026-11-13"},
        )

    def test_long_lists_are_capped_and_say_so(self):
        pages = [
            {"issues": [self._issue(n) for n in range(p * 100, p * 100 + 100)], "isLast": False, "nextPageToken": f"p{p + 1}"}
            for p in range(3)
        ]

        result, sent = self._search(pages, {"cloudId": "c-1", "jql": "issuetype = Story"})

        self.assertEqual(len(sent), 2)
        self.assertFalse(result["complete"])
        self.assertEqual(result["returned"], atlassian_mcp.MAX_SEARCH_ISSUES)
        self.assertIn("first 200", result["note"])

    def test_counts_and_custom_fields_go_to_the_server_unchanged(self):
        for arguments in ({"jql": "issuetype = Task", "searchResultMode": "count"}, {"jql": "x", "fields": ["summary"]}):
            result, sent = self._search([{"count": 12}], arguments)
            self.assertEqual(sent, [arguments])
            self.assertEqual(result, {"count": 12})


class GeminiSchemaTests(unittest.TestCase):
    ALLOWED = {"type", "description", "nullable", "enum", "properties", "required", "items"}

    def _keys(self, schema: Any) -> set:
        found = set()
        if isinstance(schema, dict):
            found |= set(schema)
            for value in (schema.get("properties") or {}).values():
                found |= self._keys(value)
            found |= self._keys(schema.get("items"))
        return found

    def test_mcp_schema_is_reduced_to_what_gemini_accepts(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "$defs": {"Key": {"type": "string", "pattern": "^[A-Z]+-\\d+$"}},
            "properties": {
                "issue": {"$ref": "#/$defs/Key"},
                "repos": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                "limit": {"type": ["integer", "null"], "default": 10},
                "kind": {"const": "bug"},
                "url": {"type": "string", "format": "uri"},
                "fields": {"type": "object", "additionalProperties": True, "description": "Extra fields."},
            },
            "required": ["issue", "missing"],
        }

        out = _gemini_schema(schema)

        self.assertLessEqual(self._keys(out), self.ALLOWED)
        props = out["properties"]
        self.assertEqual(props["issue"]["type"], "STRING")
        self.assertEqual(props["repos"]["type"], "STRING")
        self.assertEqual(props["limit"], {"type": "INTEGER", "nullable": True})
        self.assertEqual(props["kind"]["enum"], ["bug"])
        self.assertEqual(props["fields"]["type"], "STRING")
        self.assertEqual(out["required"], ["issue"])

    def test_tool_without_parameters_keeps_an_empty_object(self):
        self.assertEqual(_gemini_schema({"type": "object", "properties": {}}), {"type": "OBJECT", "properties": {}})

    def test_nullable_free_form_object_sent_as_json_text_is_turned_back_into_an_object(self):
        schema = {
            "type": "object",
            "$defs": {"Fields": {"type": "object", "additionalProperties": {}}},
            "properties": {
                "fields": {"anyOf": [{"type": "object", "additionalProperties": {}}, {"type": "null"}]},
                "extra": {"oneOf": [{"type": "null"}, {"$ref": "#/$defs/Fields"}]},
            },
        }
        self.assertEqual(_gemini_schema(schema)["properties"]["fields"]["type"], "STRING")

        args = atlassian_mcp.coerce_arguments(schema, {"fields": '{"priority": {"name": "High"}}', "extra": '{"a": 1}'})

        self.assertEqual(args, {"fields": {"priority": {"name": "High"}}, "extra": {"a": 1}})


if __name__ == "__main__":
    unittest.main()
