import asyncio
import unittest

import httpx

from backend.llm import (
    AssistantMessage,
    LLMRequestError,
    LLMResponse,
    LLMTransientError,
    LLMUnavailableError,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    UserMessage,
    parse_chain_spec,
)
from backend.llm.anthropic import AnthropicProvider
from backend.llm.chain import LLMChain
from backend.llm.gemini import GeminiProvider, _gemini_schema
from backend.llm.http import post_json
from backend.llm.openai_compat import OpenAICompatProvider

SEARCH_TOOL = ToolSpec(
    name="search_work_items",
    description="Search work items.",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
        "required": ["query"],
    },
)
HTTP = httpx.AsyncClient()


def _gemini() -> GeminiProvider:
    return GeminiProvider("gemini-test", "key", 5, 0.1, HTTP)


def _openai() -> OpenAICompatProvider:
    return OpenAICompatProvider("gpt-test", "key", "https://example.test/v1/", 5, 0.1, HTTP)


def _anthropic() -> AnthropicProvider:
    return AnthropicProvider("claude-test", "key", 5, 0.1, HTTP)


class FakeProvider:
    def __init__(self, name: str, outcome):
        self.name = name
        self.model = "m"
        self.target = f"{name}:m"
        self.outcome = outcome
        self.calls = 0

    async def generate(self, system, messages, tools):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return LLMResponse(text=self.outcome, tool_calls=[], provider=self.name, model="m", elapsed_ms=1)

    async def stream(self, system, messages, tools):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        yield self.outcome[:2]
        yield self.outcome[2:]
        yield LLMResponse(text=self.outcome, tool_calls=[], provider=self.name, model="m", elapsed_ms=1)


class PartialThenFailProvider(FakeProvider):
    async def stream(self, system, messages, tools):
        self.calls += 1
        yield "partial"
        raise LLMTransientError(f"{self.name}: dropped mid-answer")


class ParseChainSpecTests(unittest.TestCase):
    def test_parses_entries_and_keeps_colons_in_model(self):
        self.assertEqual(
            parse_chain_spec(" gemini:gemini-3.6-flash, openai:gpt-4.1-mini ,ollama:llama3:8b"),
            [("gemini", "gemini-3.6-flash"), ("openai", "gpt-4.1-mini"), ("ollama", "llama3:8b")],
        )

    def test_rejects_entry_without_model(self):
        with self.assertRaises(ValueError):
            parse_chain_spec("gemini")


class GeminiAdapterTests(unittest.TestCase):
    def test_schema_types_are_uppercased_recursively(self):
        out = _gemini_schema(SEARCH_TOOL.parameters)
        self.assertEqual(out["type"], "OBJECT")
        self.assertEqual(out["properties"]["max_results"]["type"], "INTEGER")

    def test_tool_results_of_one_turn_share_one_user_content(self):
        messages = [
            UserMessage("q"),
            AssistantMessage(tool_calls=[ToolCall("a", "search_work_items", {"query": "x"}),
                                         ToolCall("b", "search_work_items", {"query": "y"})]),
            ToolResultMessage("a", "search_work_items", "[1]"),
            ToolResultMessage("b", "search_work_items", "[2]"),
        ]
        payload = _gemini().build_payload("sys", messages, [SEARCH_TOOL])
        self.assertEqual(payload["contents"][-1]["role"], "user")
        self.assertEqual(len(payload["contents"][-1]["parts"]), 2)
        self.assertEqual(payload["tools"][0]["functionDeclarations"][0]["parameters"]["type"], "OBJECT")

    def test_parse_extracts_function_call_and_keeps_native_parts(self):
        data = {
            "candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"name": "search_work_items", "args": {"query": "x"}}, "thoughtSignature": "sig"},
            ]}}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3},
        }
        resp = _gemini().parse_response(data, 42)
        self.assertEqual(resp.tool_calls[0].name, "search_work_items")
        self.assertEqual(resp.tool_calls[0].args, {"query": "x"})
        self.assertEqual(resp.input_tokens, 10)
        self.assertEqual(resp.native[0], "gemini")

    def test_native_turn_is_echoed_only_for_same_provider(self):
        native_parts = [{"functionCall": {"name": "n", "args": {}}, "thoughtSignature": "sig"}]
        msg = AssistantMessage(native=("gemini", native_parts))
        same = _gemini().build_payload("s", [msg], [])["contents"][0]["parts"]
        other = _openai().build_payload("s", [msg], [])["messages"][1]
        self.assertEqual(same, native_parts)
        self.assertIsNone(other.get("content"))


class OpenAIAdapterTests(unittest.TestCase):
    def test_payload_uses_function_format_and_tool_messages(self):
        messages = [
            UserMessage("q"),
            AssistantMessage(tool_calls=[ToolCall("call_1", "search_work_items", {"query": "x"})]),
            ToolResultMessage("call_1", "search_work_items", "[]"),
        ]
        payload = _openai().build_payload("sys", messages, [SEARCH_TOOL])
        self.assertEqual(payload["tools"][0]["type"], "function")
        self.assertEqual(payload["messages"][2]["tool_calls"][0]["function"]["arguments"], '{"query": "x"}')
        self.assertEqual(payload["messages"][3], {"role": "tool", "tool_call_id": "call_1", "content": "[]"})

    def test_parse_reads_tool_calls_and_usage(self):
        data = {
            "choices": [{"message": {"content": None, "tool_calls": [
                {"id": "call_9", "type": "function",
                 "function": {"name": "search_work_items", "arguments": '{"query": "project = A"}'}},
            ]}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2},
        }
        resp = _openai().parse_response(data, 5)
        self.assertEqual(resp.tool_calls[0].id, "call_9")
        self.assertEqual(resp.tool_calls[0].args, {"query": "project = A"})
        self.assertEqual(resp.output_tokens, 2)


class AnthropicAdapterTests(unittest.TestCase):
    def test_tool_results_are_grouped_into_one_user_message(self):
        messages = [
            UserMessage("q"),
            AssistantMessage(tool_calls=[ToolCall("t1", "search_work_items", {"query": "x"}),
                                         ToolCall("t2", "search_work_items", {"query": "y"})]),
            ToolResultMessage("t1", "search_work_items", "[]"),
            ToolResultMessage("t2", "search_work_items", "[]"),
        ]
        payload = _anthropic().build_payload("sys", messages, [SEARCH_TOOL])
        self.assertEqual(payload["tools"][0]["input_schema"]["type"], "object")
        self.assertEqual(len(payload["messages"]), 3)
        self.assertEqual(len(payload["messages"][2]["content"]), 2)
        self.assertEqual(payload["messages"][2]["content"][0]["type"], "tool_result")

    def test_parse_reads_tool_use_blocks(self):
        data = {
            "content": [{"type": "tool_use", "id": "t1", "name": "search_work_items", "input": {"query": "x"}}],
            "usage": {"input_tokens": 4, "output_tokens": 1},
        }
        resp = _anthropic().parse_response(data, 9)
        self.assertEqual(resp.tool_calls[0].args, {"query": "x"})
        self.assertEqual(resp.input_tokens, 4)


class HttpErrorMappingTests(unittest.TestCase):
    def _run(self, handler):
        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await post_json(client, "t:m", "https://x.test", headers={}, body={}, timeout=1)
        return asyncio.run(go())

    def test_503_is_transient(self):
        with self.assertRaises(LLMTransientError):
            self._run(lambda req: httpx.Response(503, text="high demand"))

    def test_400_is_request_error(self):
        with self.assertRaises(LLMRequestError):
            self._run(lambda req: httpx.Response(400, text="bad request"))

    def test_timeout_is_transient(self):
        def handler(req):
            raise httpx.ReadTimeout("slow", request=req)
        with self.assertRaises(LLMTransientError):
            self._run(handler)

    def test_success_returns_json(self):
        data, _ = self._run(lambda req: httpx.Response(200, json={"ok": True}))
        self.assertEqual(data, {"ok": True})


class LLMChainTests(unittest.TestCase):
    def _generate(self, chain: LLMChain) -> LLMResponse:
        return asyncio.run(chain.generate("sys", [UserMessage("q")], []))

    def test_fails_over_to_next_provider(self):
        first = FakeProvider("a", LLMTransientError("a: 503"))
        second = FakeProvider("b", "answer from b")
        resp = self._generate(LLMChain([first, second], failure_threshold=2, cooldown_seconds=60))
        self.assertEqual(resp.text, "answer from b")

    def test_circuit_opens_after_threshold_and_skips_target(self):
        first = FakeProvider("a", LLMTransientError("a: 503"))
        second = FakeProvider("b", "ok")
        chain = LLMChain([first, second], failure_threshold=2, cooldown_seconds=60)
        self._generate(chain)
        self._generate(chain)
        calls_before = first.calls
        self._generate(chain)
        self.assertEqual(first.calls, calls_before, "open circuit should skip the failing target")

    def test_request_error_skips_target_without_opening_circuit(self):
        first = FakeProvider("a", LLMRequestError("a: 401"))
        second = FakeProvider("b", "ok")
        chain = LLMChain([first, second], failure_threshold=1, cooldown_seconds=60)
        self._generate(chain)
        self.assertEqual(chain._breakers["a:m"].open_until, 0.0)

    def test_all_targets_failing_raises_unavailable(self):
        chain = LLMChain([FakeProvider("a", LLMTransientError("a")), FakeProvider("b", LLMRequestError("b"))], 2, 60)
        with self.assertRaises(LLMUnavailableError):
            self._generate(chain)

    def test_empty_chain_raises_unavailable(self):
        with self.assertRaises(LLMUnavailableError):
            self._generate(LLMChain([], 2, 60))


def _collect_stream(chain: LLMChain):
    async def go():
        return [item async for item in chain.stream("sys", [UserMessage("q")], [])]

    return asyncio.run(go())


class LLMChainStreamTests(unittest.TestCase):
    def test_streams_chunks_then_final_response(self):
        chain = LLMChain([FakeProvider("a", "hello world")], failure_threshold=2, cooldown_seconds=60)
        items = _collect_stream(chain)
        self.assertEqual("".join(i for i in items if isinstance(i, str)), "hello world")
        self.assertIsInstance(items[-1], LLMResponse)

    def test_fails_over_when_nothing_was_emitted(self):
        first = FakeProvider("a", LLMTransientError("a: 503"))
        second = FakeProvider("b", "from b")
        items = _collect_stream(LLMChain([first, second], 2, 60))
        self.assertEqual(items[-1].provider, "b")

    def test_does_not_fail_over_after_text_was_sent(self):
        first = PartialThenFailProvider("a", None)
        second = FakeProvider("b", "from b")
        with self.assertRaises(LLMUnavailableError):
            _collect_stream(LLMChain([first, second], 2, 60))
        self.assertEqual(second.calls, 0)


def _sse(*events: str) -> bytes:
    return "".join(f"data: {e}\n\n" for e in events).encode()


class StreamParserTests(unittest.TestCase):
    def _run(self, provider, handler_body: bytes):
        async def go():
            async def handler(request):
                return httpx.Response(200, content=handler_body)

            provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            return [item async for item in provider.stream("sys", [UserMessage("q")], [SEARCH_TOOL])]

        return asyncio.run(go())

    def test_openai_stream_merges_text_and_tool_call_fragments(self):
        body = _sse(
            '{"choices":[{"delta":{"content":"Hel"}}]}',
            '{"choices":[{"delta":{"content":"lo"}}]}',
            '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"search_work_items","arguments":"{\\"query\\":"}}]}}]}',
            '{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"x\\"}"}}]}}]}',
            "[DONE]",
        )
        items = self._run(_openai(), body)
        self.assertEqual(items[:2], ["Hel", "lo"])
        final = items[-1]
        self.assertEqual(final.text, "Hello")
        self.assertEqual(final.tool_calls[0].name, "search_work_items")
        self.assertEqual(final.tool_calls[0].args, {"query": "x"})

    def test_anthropic_stream_yields_text_and_parses_tool_input(self):
        body = _sse(
            '{"type":"message_start","message":{"usage":{"input_tokens":5}}}',
            '{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
            '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi"}}',
            '{"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"t1","name":"search_work_items"}}',
            '{"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"query\\":"}}',
            '{"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\\"y\\"}"}}',
            '{"type":"message_delta","usage":{"output_tokens":7}}',
            '{"type":"message_stop"}',
        )
        items = self._run(_anthropic(), body)
        self.assertEqual(items[0], "Hi")
        final = items[-1]
        self.assertEqual(final.text, "Hi")
        self.assertEqual(final.tool_calls[0].args, {"query": "y"})
        self.assertEqual(final.output_tokens, 7)

    def test_anthropic_stream_error_event_is_transient_when_overloaded(self):
        body = _sse('{"type":"error","error":{"type":"overloaded_error","message":"busy"}}')
        with self.assertRaises(LLMTransientError):
            self._run(_anthropic(), body)


if __name__ == "__main__":
    unittest.main()
