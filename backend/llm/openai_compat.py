import json
import time
from typing import Any, AsyncIterator, Dict, List

import httpx

from backend.llm.base import (
    AssistantMessage,
    LLMProvider,
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


class OpenAICompatProvider(LLMProvider):
    """Chat Completions API with function calling: OpenAI, Azure OpenAI, Groq, Ollama and similar."""

    name = "openai"

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str,
        timeout: float,
        temperature: float,
        client: httpx.AsyncClient,
    ):
        self.model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._temperature = temperature
        self._client = client

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def build_payload(self, system: str, messages: List[Message], tools: List[ToolSpec]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *_to_messages(messages)],
            "temperature": self._temperature,
        }
        if tools:
            payload["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
        return payload

    def parse_response(self, data: Dict[str, Any], elapsed_ms: int) -> LLMResponse:
        choices = data.get("choices") or []
        if not choices:
            raise LLMTransientError(f"{self.target}: response had no choices")
        message = choices[0].get("message") or {}
        calls = [
            ToolCall(id=tc.get("id", ""), name=tc["function"]["name"], args=_parse_args(tc["function"].get("arguments")))
            for tc in (message.get("tool_calls") or [])
        ]
        usage = data.get("usage") or {}
        return LLMResponse(
            text=(message.get("content") or "").strip(),
            tool_calls=calls,
            provider=self.name,
            model=self.model,
            elapsed_ms=elapsed_ms,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )

    async def generate(self, system: str, messages: List[Message], tools: List[ToolSpec]) -> LLMResponse:
        data, elapsed_ms = await post_json(
            self._client, self.target, f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            body=self.build_payload(system, messages, tools),
            timeout=self._timeout,
        )
        return self.parse_response(data, elapsed_ms)

    async def stream(self, system: str, messages: List[Message], tools: List[ToolSpec]) -> AsyncIterator[StreamItem]:
        body = self.build_payload(system, messages, tools)
        body["stream"] = True
        started = time.monotonic()
        text_parts: List[str] = []
        calls: Dict[int, Dict[str, Any]] = {}
        saw_choices = False
        async for event in stream_sse(
            self._client, self.target, f"{self._base_url}/chat/completions",
            headers=self._headers(),
            body=body,
            timeout=self._timeout,
        ):
            choices = event.get("choices") or []
            if not choices:
                continue
            saw_choices = True
            delta = choices[0].get("delta") or {}
            if delta.get("content"):
                text_parts.append(delta["content"])
                yield delta["content"]
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(tc.get("index", 0), {"id": "", "function": {"name": "", "arguments": ""}})
                slot["id"] = tc.get("id") or slot["id"]
                fn = tc.get("function") or {}
                slot["function"]["name"] += fn.get("name") or ""
                slot["function"]["arguments"] += fn.get("arguments") or ""

        elapsed_ms = int((time.monotonic() - started) * 1000)
        if not saw_choices:
            raise LLMTransientError(f"{self.target}: response had no choices")
        message: Dict[str, Any] = {"content": "".join(text_parts)}
        if calls:
            message["tool_calls"] = [calls[i] for i in sorted(calls)]
        yield self.parse_response({"choices": [{"message": message}]}, elapsed_ms)


def _parse_args(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _to_messages(messages: List[Message]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, UserMessage):
            out.append({"role": "user", "content": msg.text})
        elif isinstance(msg, AssistantMessage):
            entry: Dict[str, Any] = {"role": "assistant", "content": msg.text or None}
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                    for c in msg.tool_calls
                ]
            out.append(entry)
        elif isinstance(msg, ToolResultMessage):
            out.append({"role": "tool", "tool_call_id": msg.call_id, "content": msg.content})
    return out
