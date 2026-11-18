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

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_MAX_SCHEMA_DEPTH = 12


def _gemini_schema(schema: Any, root: Any = None, depth: int = 0) -> Dict[str, Any]:
    """Converts standard JSON Schema to the OpenAPI subset Gemini accepts for function parameters.

    Tool schemas from MCP servers use features Gemini rejects ($ref, anyOf, type lists, const,
    additionalProperties, formats), so only the supported keywords are kept, local $refs are inlined,
    and type names are uppercased. A nested object without declared properties becomes a string holding
    JSON, which the MCP bridge parses back into an object.
    """
    root = schema if root is None else root
    if not isinstance(schema, dict) or depth > _MAX_SCHEMA_DEPTH:
        return {"type": "STRING"}
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        target: Any = root
        for part in ref[2:].split("/"):
            target = target.get(part, {}) if isinstance(target, dict) else {}
        merged = {**target, **{k: v for k, v in schema.items() if k != "$ref"}}
        return _gemini_schema(merged, root, depth + 1)

    nullable = False
    variants = schema.get("anyOf") or schema.get("oneOf")
    if variants and "type" not in schema:
        concrete = [v for v in variants if not (isinstance(v, dict) and v.get("type") == "null")]
        nullable = len(concrete) < len(variants)
        chosen = concrete[0] if concrete else {}
        merged = {**chosen, **{k: v for k, v in schema.items() if k not in ("anyOf", "oneOf")}}
        converted = _gemini_schema(merged, root, depth + 1)
        if nullable:
            converted["nullable"] = True
        return converted

    kind = schema.get("type")
    if isinstance(kind, list):
        nullable = "null" in kind
        kind = next((k for k in kind if k != "null"), "string")
    if "const" in schema and "enum" not in schema:
        schema = {**schema, "enum": [schema["const"]]}
    if not kind or kind == "null":
        kind = "object" if "properties" in schema else "array" if "items" in schema else "string"
    enum = schema.get("enum")
    if enum and kind not in ("string", "integer", "number", "boolean"):
        enum = None
    if enum and kind != "string":
        # Gemini only accepts enums on strings; keep the real type and list the values in the description.
        allowed = ", ".join(str(value) for value in enum)
        schema = {**schema, "description": f"{schema.get('description') or ''} Allowed values: {allowed}.".strip()}
        enum = None

    out: Dict[str, Any] = {}
    properties = schema.get("properties")
    if kind == "object" and not properties and depth > 0:
        kind = "string"
        out["description"] = ((schema.get("description") or "") + " (a JSON object, written as JSON text)").strip()
    elif schema.get("description"):
        out["description"] = str(schema["description"])
    out["type"] = str(kind).upper()
    if nullable:
        out["nullable"] = True
    if enum:
        out["enum"] = [str(value) for value in enum if value is not None]
    if kind == "object":
        out["properties"] = {name: _gemini_schema(value, root, depth + 1) for name, value in (properties or {}).items()}
        required = [name for name in schema.get("required") or [] if name in out["properties"]]
        if required:
            out["required"] = required
    if kind == "array":
        out["items"] = _gemini_schema(schema.get("items") or {"type": "string"}, root, depth + 1)
    return out


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, model: str, api_key: str, timeout: float, temperature: float, client: httpx.AsyncClient):
        self.model = model
        self._api_key = api_key
        self._timeout = timeout
        self._temperature = temperature
        self._client = client

    def build_payload(self, system: str, messages: List[Message], tools: List[ToolSpec]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": _to_contents(messages, self.name),
            "generationConfig": {"temperature": self._temperature},
        }
        if tools:
            payload["tools"] = [{
                "functionDeclarations": [
                    {"name": t.name, "description": t.description, "parameters": _gemini_schema(t.parameters)}
                    for t in tools
                ]
            }]
        return payload

    def parse_response(self, data: Dict[str, Any], elapsed_ms: int) -> LLMResponse:
        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMTransientError(f"{self.target}: empty candidates (blocked or no output)")
        content = candidates[0].get("content") or {"role": "model", "parts": []}
        parts = content.get("parts", [])
        calls: List[ToolCall] = []
        for index, part in enumerate(parts):
            if "functionCall" in part:
                fc = part["functionCall"]
                calls.append(ToolCall(id=f"{fc.get('name', '')}-{index}", name=fc.get("name", ""), args=fc.get("args") or {}))
        text = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought")).strip()
        usage = data.get("usageMetadata") or {}
        return LLMResponse(
            text=text,
            tool_calls=calls,
            provider=self.name,
            model=self.model,
            elapsed_ms=elapsed_ms,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
            native=(self.name, parts),
        )

    async def generate(self, system: str, messages: List[Message], tools: List[ToolSpec]) -> LLMResponse:
        url = f"{GEMINI_BASE_URL}/models/{self.model}:generateContent"
        data, elapsed_ms = await post_json(
            self._client, self.target, url,
            headers={"x-goog-api-key": self._api_key},
            body=self.build_payload(system, messages, tools),
            timeout=self._timeout,
        )
        return self.parse_response(data, elapsed_ms)

    async def stream(self, system: str, messages: List[Message], tools: List[ToolSpec]) -> AsyncIterator[StreamItem]:
        url = f"{GEMINI_BASE_URL}/models/{self.model}:streamGenerateContent"
        started = time.monotonic()
        parts: List[Dict[str, Any]] = []
        usage: Dict[str, Any] = {}
        saw_candidates = False
        async for event in stream_sse(
            self._client, self.target, url,
            headers={"x-goog-api-key": self._api_key},
            body=self.build_payload(system, messages, tools),
            timeout=self._timeout,
            params={"alt": "sse"},
        ):
            usage = event.get("usageMetadata") or usage
            candidates = event.get("candidates") or []
            if not candidates:
                continue
            saw_candidates = True
            for part in (candidates[0].get("content") or {}).get("parts", []):
                if _is_plain_text(part) and parts and _is_plain_text(parts[-1]) \
                        and bool(parts[-1].get("thought")) == bool(part.get("thought")):
                    parts[-1]["text"] += part["text"]
                else:
                    parts.append(dict(part))
                if _is_plain_text(part) and not part.get("thought") and part["text"]:
                    yield part["text"]

        elapsed_ms = int((time.monotonic() - started) * 1000)
        data = {"candidates": [{"content": {"role": "model", "parts": parts}}], "usageMetadata": usage} \
            if saw_candidates else {}
        yield self.parse_response(data, elapsed_ms)


def _is_plain_text(part: Dict[str, Any]) -> bool:
    """A text part with no function call and no signature, which can be merged with its neighbours."""
    return "text" in part and "thoughtSignature" not in part and "functionCall" not in part


def _to_contents(messages: List[Message], provider: str) -> List[Dict[str, Any]]:
    contents: List[Dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, UserMessage):
            contents.append({"role": "user", "parts": [{"text": msg.text}]})
        elif isinstance(msg, AssistantMessage):
            if msg.native and msg.native[0] == provider:
                # Echo this provider's own turn unchanged so its signatures stay valid.
                parts = msg.native[1]
            else:
                parts = []
                if msg.text:
                    parts.append({"text": msg.text})
                for call in msg.tool_calls:
                    parts.append({"functionCall": {"name": call.name, "args": call.args}})
                if not parts:
                    # Gemini rejects model turns with no parts.
                    parts.append({"text": "(no output)"})
            contents.append({"role": "model", "parts": parts})
        elif isinstance(msg, ToolResultMessage):
            part = {"functionResponse": {"name": msg.name, "response": {"result": msg.content}}}
            # Gemini wants all results of one turn in a single user content block.
            last = contents[-1] if contents else None
            if last and last["role"] == "user" and last["parts"] and "functionResponse" in last["parts"][0]:
                last["parts"].append(part)
            else:
                contents.append({"role": "user", "parts": [part]})
    return contents
