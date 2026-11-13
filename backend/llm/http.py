import json
import time
from typing import Any, AsyncIterator, Dict, Optional, Tuple

import httpx

from backend.llm.base import LLMError, LLMRequestError, LLMTransientError

TRANSIENT_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def _status_error(target: str, status: int, text: str) -> LLMError:
    detail = text[:300].replace("\n", " ")
    message = f"{target}: HTTP {status} {detail}"
    if status in TRANSIENT_STATUS_CODES:
        return LLMTransientError(message)
    return LLMRequestError(message)


async def post_json(
    client: httpx.AsyncClient,
    target: str,
    url: str,
    *,
    headers: Dict[str, str],
    body: Dict[str, Any],
    timeout: float,
    params: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, Any], int]:
    """POST a JSON body and return (parsed JSON, elapsed ms). Raises the LLM error types."""
    started = time.monotonic()
    try:
        resp = await client.post(url, headers=headers, params=params, json=body, timeout=timeout)
    except httpx.TimeoutException as e:
        raise LLMTransientError(f"{target}: timeout after {timeout:.0f}s") from e
    except httpx.TransportError as e:
        raise LLMTransientError(f"{target}: network error ({type(e).__name__})") from e
    elapsed_ms = int((time.monotonic() - started) * 1000)

    if resp.status_code == 200:
        try:
            return resp.json(), elapsed_ms
        except ValueError as e:
            raise LLMTransientError(f"{target}: invalid JSON response") from e
    raise _status_error(target, resp.status_code, resp.text)


async def stream_sse(
    client: httpx.AsyncClient,
    target: str,
    url: str,
    *,
    headers: Dict[str, str],
    body: Dict[str, Any],
    timeout: float,
    params: Optional[Dict[str, str]] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """POST a JSON body and yield each parsed server-sent 'data:' event."""
    try:
        async with client.stream("POST", url, headers=headers, params=params, json=body, timeout=timeout) as resp:
            if resp.status_code != 200:
                raw = await resp.aread()
                raise _status_error(target, resp.status_code, raw.decode("utf-8", "replace"))
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    yield json.loads(payload)
                except ValueError as e:
                    raise LLMTransientError(f"{target}: invalid stream event") from e
    except httpx.TimeoutException as e:
        raise LLMTransientError(f"{target}: timeout after {timeout:.0f}s") from e
    except httpx.TransportError as e:
        raise LLMTransientError(f"{target}: network error ({type(e).__name__})") from e
