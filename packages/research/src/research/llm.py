"""Chat calls to DeepSeek or Z.AI (GLM), picked by model name, with JSON parsing, usage
totals, a shared concurrency cap and a fallback model for when a plan runs out."""

import threading
from collections.abc import Callable

from llm import Completions, LLMError, chat_json, provider, provider_for

from research.config import LIMITS, MAX_TOKENS


def out_of_quota(e: BaseException) -> bool:
    """Z.AI's answer once the plan's usage is spent: 429, or 1113 "Insufficient balance"."""
    return (
        getattr(e, "status", None) == 429 or str(getattr(e, "code", "") or "") == "1113"
    )


def cached_tokens(usage: dict) -> int:
    """Prompt tokens the provider served from its prefix cache: DeepSeek says
    prompt_cache_hit_tokens, OpenAI-style APIs (Z.AI) prompt_tokens_details.cached_tokens."""
    return (
        usage.get("prompt_cache_hit_tokens")
        or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        or 0
    )


class LLM:
    """`client` has create(model, messages, max_tokens, think) -> (text, usage), like
    llm.Completions, or is a function giving the client for a model. `fallback` names the
    model to use instead of one whose provider says it's out of quota, for the rest of the run."""

    def __init__(
        self,
        client,
        fallback: dict[str, str] | None = None,
        on_fallback: Callable[[str, str, BaseException], None] = lambda *a: None,
    ):
        self.client_for = (
            client
            if callable(client) and not hasattr(client, "create")
            else (lambda model: client)
        )
        self.fallback = fallback or {}
        self.on_fallback = on_fallback
        # model -> the fallback it switched to; a spent plan won't recover within a run.
        self.switched: dict[str, str] = {}
        self.usage = {
            "calls": 0,
            "prompt": 0,
            "cached": 0,
            "completion": 0,
            "reasoning": 0,
        }
        self.clients: list[Completions] = []  # the ones for_models made, for close()
        self._slots = threading.BoundedSemaphore(LIMITS["llm"])
        self._lock = threading.Lock()

    @classmethod
    def for_models(cls, models: list[str], env_file: str, **kw) -> "LLM":
        """An LLM with a client per provider the models (and their fallbacks) need. Raises
        LLMError before any call if a provider's key is missing."""
        clients = {}
        for name in {
            provider_for(m) for m in [*models, *(kw.get("fallback") or {}).values()]
        }:
            prov = provider(name, env_file)
            if not prov.key:
                raise LLMError(f"{prov.key_name} is not set.")
            clients[name] = Completions(prov)
        llm = cls(lambda model: clients[provider_for(model)], **kw)
        llm.clients = list(clients.values())
        return llm

    def close(self) -> None:
        for c in self.clients:
            c.client.close()

    def chat(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int = MAX_TOKENS["json"],
        think: bool = True,
    ) -> str:
        """One completion; returns the reply text."""
        use = self.switched.get(model, model)
        try:
            return self._complete(use, messages, max_tokens, think)
        except Exception as e:
            to = self.fallback.get(model)
            if use != model or not to or not out_of_quota(e):
                raise
            with self._lock:
                first = model not in self.switched
                self.switched.setdefault(model, to)
            if first:
                self.on_fallback(model, to, e)
            return self._complete(to, messages, max_tokens, think)

    def _complete(
        self, model: str, messages: list[dict], max_tokens: int, think: bool
    ) -> str:
        with self._slots:
            text, usage = self.client_for(model).create(
                model, messages, max_tokens, think
            )
        with self._lock:
            self.usage["calls"] += 1
            self.usage["prompt"] += usage.get("prompt_tokens") or 0
            self.usage["cached"] += cached_tokens(usage)
            self.usage["completion"] += usage.get("completion_tokens") or 0
            self.usage["reasoning"] += (
                usage.get("completion_tokens_details") or {}
            ).get("reasoning_tokens") or 0
        return text

    def json(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int = MAX_TOKENS["json"],
        think: bool = True,
    ) -> dict:
        """A completion parsed as a JSON object, with one repair turn if it doesn't parse.
        DeepSeek's JSON mode isn't used: with it, deepseek-flash often answers with the
        wrong keys or just {"type": "json_object"}; plain prompts parse reliably."""
        return chat_json(lambda m: self.chat(model, m, max_tokens, think), messages)
