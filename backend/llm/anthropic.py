import json
import time
from typing import Any, AsyncIterator, Dict, List

import httpx

from backend.llm.base import (
    AssistantMessage,
    LLMProvider,
    LLMRequestError,
    LLMResponse,
    LLMTransientError,
    Message,
    StreamItem,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    UserMessage,
)
from backend.llm.http import post_json, stream_sse

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MAX_OUTPUT_TOKENS = 4096


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: str, api_key: str, timeout: float, temperature: float, client: httpx.AsyncClient):
        self.model = model
        self._api_key = api_key
        self._timeout = timeout
        self._temperature = temperature
        self._client = client

    def build_payload(self, system: str, messages: List[Message], tools: List[ToolSpec]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "system": system,
            "messages": _to_messages(messages),
            "temperature": self._temperature,
        }
        if tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
            ]
        return payload

    def parse_response(self, data: Dict[str, Any], elapsed_ms: int) -> LLMResponse:
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        calls = [
            ToolCall(id=b.get("id", ""), name=b.get("name", ""), args=b.get("input") or {})
            for b in blocks
            if b.get("type") == "tool_use"
        ]
        if not blocks and not calls:
            raise LLMTransientError(f"{self.target}: empty response")
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            tool_calls=calls,
            provider=self.name,
            model=self.model,
            elapsed_ms=elapsed_ms,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )

    async def generate(self, system: str, messages: List[Message], tools: List[ToolSpec]) -> LLMResponse:
        data, elapsed_ms = await post_json(
            self._client, self.target, ANTHROPIC_URL,
            headers={"x-api-key": self._api_key, "anthropic-version": ANTHROPIC_VERSION},
            body=self.build_payload(system, messages, tools),
            timeout=self._timeout,
        )
        return self.parse_response(data, elapsed_ms)

    async def stream(self, system: str, messages: List[Message], tools: List[ToolSpec]) -> AsyncIterator[StreamItem]:
        body = self.build_payload(system, messages, tools)
        body["stream"] = True
        started = time.monotonic()
        blocks: Dict[int, Dict[str, Any]] = {}
        partial_json: Dict[int, str] = {}
        usage: Dict[str, Any] = {}
        async for event in stream_sse(
            self._client, self.target, ANTHROPIC_URL,
            headers={"x-api-key": self._api_key, "anthropic-version": ANTHROPIC_VERSION},
            body=body,
            timeout=self._timeout,
        ):
            kind = event.get("type")
            if kind == "error":
                err = event.get("error") or {}
                message = f"{self.target}: stream error {err.get('type', '')} {err.get('message', '')}".strip()
                if err.get("type") in ("overloaded_error", "rate_limit_error", "api_error"):
                    raise LLMTransientError(message)
                raise LLMRequestError(message)
            if kind == "message_start":
                usage.update((event.get("message") or {}).get("usage") or {})
            elif kind == "content_block_start":
                index = event.get("index", 0)
                block = dict(event.get("content_block") or {})
                blocks[index] = block
                if block.get("type") == "tool_use":
                    partial_json[index] = ""
            elif kind == "content_block_delta":
                index = event.get("index", 0)
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    chunk = delta.get("text", "")
                    blocks.setdefault(index, {"type": "text", "text": ""})
                    blocks[index]["text"] = blocks[index].get("text", "") + chunk
                    if chunk:
                        yield chunk
                elif delta.get("type") == "input_json_delta":
                    partial_json[index] = partial_json.get(index, "") + delta.get("partial_json", "")
            elif kind == "message_delta":
                usage.update(event.get("usage") or {})

        elapsed_ms = int((time.monotonic() - started) * 1000)
        content: List[Dict[str, Any]] = []
        for index in sorted(blocks):
            block = blocks[index]
            if block.get("type") == "tool_use":
                raw = partial_json.get(index) or "{}"
                try:
                    block["input"] = json.loads(raw)
                except ValueError:
                    block["input"] = {}
            content.append(block)
        yield self.parse_response({"content": content, "usage": usage}, elapsed_ms)


def _to_messages(messages: List[Message]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, UserMessage):
            out.append({"role": "user", "content": [{"type": "text", "text": msg.text}]})
        elif isinstance(msg, AssistantMessage):
            blocks: List[Dict[str, Any]] = []
            if msg.text:
                blocks.append({"type": "text", "text": msg.text})
            for call in msg.tool_calls:
                blocks.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.args})
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "..."}]})
        elif isinstance(msg, ToolResultMessage):
            block = {"type": "tool_result", "tool_use_id": msg.call_id, "content": msg.content}
            # Anthropic wants all results of one turn in a single user message.
            last = out[-1] if out else None
            if last and last["role"] == "user" and last["content"] and last["content"][0].get("type") == "tool_result":
                last["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out
