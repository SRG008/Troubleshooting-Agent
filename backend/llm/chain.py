import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import AsyncIterator, Dict, List, Optional, Tuple

import httpx

from backend.config import Settings, settings
from backend.llm.anthropic import AnthropicProvider
from backend.llm.base import (
    LLMProvider,
    LLMRequestError,
    LLMResponse,
    LLMTransientError,
    LLMUnavailableError,
    Message,
    StreamItem,
    ToolSpec,
)
from backend.llm.gemini import GeminiProvider
from backend.llm.openai_compat import OpenAICompatProvider

logger = logging.getLogger(__name__)


@dataclass
class _Breaker:
    failures: int = 0
    open_until: float = 0.0


class LLMChain:
    """Runs a provider chain with failover. A target that fails repeatedly is skipped for a cooldown period."""

    def __init__(self, providers: List[LLMProvider], failure_threshold: int, cooldown_seconds: float):
        self._providers = providers
        self._failure_threshold = max(1, failure_threshold)
        self._cooldown = cooldown_seconds
        self._breakers: Dict[str, _Breaker] = {p.target: _Breaker() for p in providers}

    @property
    def targets(self) -> List[str]:
        return [p.target for p in self._providers]

    def _candidates(self) -> List[LLMProvider]:
        if not self._providers:
            raise LLMUnavailableError("no LLM provider is configured (check LLM_CHAIN and the API keys)")
        now = time.monotonic()
        # Skip targets whose breaker is open. If every target is open, try them all rather than fail outright.
        available = [p for p in self._providers if self._breakers[p.target].open_until <= now]
        return available or self._providers

    def _record_success(self, breaker: _Breaker) -> None:
        breaker.failures = 0
        breaker.open_until = 0.0

    async def generate(self, system: str, messages: List[Message], tools: List[ToolSpec]) -> LLMResponse:
        errors: List[str] = []
        for provider in self._candidates():
            breaker = self._breakers[provider.target]
            try:
                response = await provider.generate(system, messages, tools)
            except LLMTransientError as e:
                self._record_failure(breaker, provider.target)
                errors.append(str(e))
                logger.warning("LLM transient failure: %s", e)
                continue
            except LLMRequestError as e:
                errors.append(str(e))
                logger.error("LLM request rejected: %s", e)
                continue
            self._record_success(breaker)
            return response

        raise LLMUnavailableError("; ".join(errors))

    async def stream(self, system: str, messages: List[Message], tools: List[ToolSpec]) -> AsyncIterator[StreamItem]:
        """Yield text chunks and then the final LLMResponse.

        Failover only happens before any text has been sent to the user. After that, a failure is raised
        rather than letting another model restart the answer.
        """
        errors: List[str] = []
        for provider in self._candidates():
            breaker = self._breakers[provider.target]
            emitted = False
            try:
                async for item in provider.stream(system, messages, tools):
                    if isinstance(item, LLMResponse):
                        self._record_success(breaker)
                        yield item
                        return
                    emitted = True
                    yield item
            except LLMTransientError as e:
                self._record_failure(breaker, provider.target)
                logger.warning("LLM transient failure: %s", e)
                if emitted:
                    raise LLMUnavailableError(str(e)) from e
                errors.append(str(e))
                continue
            except LLMRequestError as e:
                logger.error("LLM request rejected: %s", e)
                if emitted:
                    raise LLMUnavailableError(str(e)) from e
                errors.append(str(e))
                continue

        raise LLMUnavailableError("; ".join(errors))

    def _record_failure(self, breaker: _Breaker, target: str) -> None:
        breaker.failures += 1
        if breaker.failures >= self._failure_threshold:
            breaker.open_until = time.monotonic() + self._cooldown
            breaker.failures = 0
            logger.warning("Circuit open for %s for %.0fs", target, self._cooldown)


def parse_chain_spec(spec: str) -> List[Tuple[str, str]]:
    """Parse 'provider:model,provider:model' into (provider, model) pairs."""
    entries: List[Tuple[str, str]] = []
    for raw in spec.split(","):
        raw = raw.strip()
        if not raw:
            continue
        if ":" not in raw:
            raise ValueError(f"Invalid LLM_CHAIN entry '{raw}'. Expected 'provider:model'.")
        provider, model = raw.split(":", 1)
        entries.append((provider.strip().lower(), model.strip()))
    return entries


def default_chain_spec(cfg: Settings) -> str:
    if cfg.llm_chain.strip():
        return cfg.llm_chain
    return f"gemini:{cfg.gemini_model},gemini:gemini-flash-lite-latest,gemini:gemini-3.1-flash-lite"


def _build_provider(name: str, model: str, cfg: Settings, client: httpx.AsyncClient) -> Optional[LLMProvider]:
    timeout, temperature = cfg.llm_timeout_seconds, cfg.llm_temperature
    if name == "gemini":
        if not cfg.gemini_api_key:
            return None
        return GeminiProvider(model, cfg.gemini_api_key, timeout, temperature, client)
    if name == "openai":
        if not cfg.openai_api_key:
            return None
        return OpenAICompatProvider(model, cfg.openai_api_key, cfg.openai_base_url, timeout, temperature, client)
    if name == "anthropic":
        if not cfg.anthropic_api_key:
            return None
        return AnthropicProvider(model, cfg.anthropic_api_key, timeout, temperature, client)
    raise ValueError(f"Unknown LLM provider '{name}'. Supported: gemini, openai, anthropic.")


def build_chain(cfg: Settings = settings) -> LLMChain:
    client = httpx.AsyncClient()
    providers: List[LLMProvider] = []
    for name, model in parse_chain_spec(default_chain_spec(cfg)):
        provider = _build_provider(name, model, cfg, client)
        if provider is None:
            logger.warning("Skipping %s:%s because its API key is not set", name, model)
            continue
        providers.append(provider)
    return LLMChain(providers, cfg.llm_circuit_failures, cfg.llm_circuit_cooldown_seconds)


@lru_cache(maxsize=1)
def get_llm_chain() -> LLMChain:
    return build_chain()
