"""Provider-neutral LLM types. The agent only speaks these; adapters translate to and from each vendor's API."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple, Union


@dataclass(frozen=True)
class ToolSpec:
    """A tool the model may call. `parameters` is standard JSON Schema with lowercase types."""

    name: str
    description: str
    parameters: Dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: Dict[str, Any]


@dataclass
class UserMessage:
    text: str


@dataclass
class AssistantMessage:
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    # (provider name, opaque vendor data) so the same vendor can echo its own turn back unchanged,
    # e.g. Gemini "thought signatures". Other providers ignore it.
    native: Optional[Tuple[str, Any]] = None


@dataclass
class ToolResultMessage:
    call_id: str
    name: str
    content: str


Message = Union[UserMessage, AssistantMessage, ToolResultMessage]


@dataclass
class LLMResponse:
    text: str
    tool_calls: List[ToolCall]
    provider: str
    model: str
    elapsed_ms: int
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    native: Optional[Tuple[str, Any]] = None


class LLMError(RuntimeError):
    """Base class for LLM failures."""


class LLMTransientError(LLMError):
    """Overloaded, rate limited, timed out or unreachable. Worth failing over to the next target."""


class LLMRequestError(LLMError):
    """The target rejected the request (bad key, bad model, bad payload). Skip this target."""


class LLMUnavailableError(LLMError):
    """Every target in the chain failed."""


StreamItem = Union[str, LLMResponse]


class LLMProvider(ABC):
    """One provider bound to one model."""

    name: str
    model: str

    @abstractmethod
    async def generate(self, system: str, messages: List[Message], tools: List[ToolSpec]) -> LLMResponse:
        """Run one model turn. Raise LLMTransientError or LLMRequestError on failure."""

    async def stream(self, system: str, messages: List[Message], tools: List[ToolSpec]) -> AsyncIterator[StreamItem]:
        """Yield text chunks as they arrive, then the final LLMResponse.

        Providers without native streaming fall back to a single chunk after a full generate().
        """
        response = await self.generate(system, messages, tools)
        if response.text:
            yield response.text
        yield response

    @property
    def target(self) -> str:
        return f"{self.name}:{self.model}"
