"""AnythingLLM's default model, DeepSeek, for the servers that ask it something themselves.

The key and the model are AnythingLLM's own (its .env, at ANYTHINGLLM_ENV or the caller's
default), so changing the model there changes it everywhere.

`deepseek` is a chat function for one model. `provider` and `Completions` are for callers
that pick their own models, on DeepSeek or on Z.AI (GLM), and need the usage and the API's
error codes: deep research.
"""

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from hostenv import env_values

DEFAULT_MODEL = "deepseek-flash"
DEEPSEEK_BASE = "https://api.deepseek.com/v1"
# A GLM Coding Plan key only works on the coding endpoint; elsewhere it gets
# "1113 Insufficient balance".
ZAI_BASE = "https://api.z.ai/api/coding/paas/v4"
PROVIDER_KEYS = (
    "DEEPSEEK_API_KEY",
    "GENERIC_OPEN_AI_BASE_PATH",
    "GENERIC_OPEN_AI_API_KEY",
    "ZAI_API_KEY",
)

Chat = Callable[[list[dict]], str]
REPAIR = {
    "role": "user",
    "content": "That was not a valid JSON object. Reply with only the JSON object.",
}


class LLMError(RuntimeError):
    """The model didn't answer usably; the message can be shown as it is."""


def settings(default_env: str) -> tuple[str, str]:
    """The DeepSeek key ("" without one) and AnythingLLM's model. The key may also come from
    DEEPSEEK_API_KEY in the environment; the .env is at ANYTHINGLLM_ENV, else `default_env`."""
    env = env_values(
        os.environ.get("ANYTHINGLLM_ENV", default_env),
        ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL_PREF"),
        environ=False,
    )
    key = os.environ.get("DEEPSEEK_API_KEY") or env.get("DEEPSEEK_API_KEY", "")
    return key, env.get("DEEPSEEK_MODEL_PREF") or DEFAULT_MODEL


def parse_json(text: str) -> dict:
    """The first JSON object in a reply, tolerating code fences and stray prose; ValueError
    (json.JSONDecodeError is one) if there's none."""
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        out = json.loads(text[start : end + 1])
    if not isinstance(out, dict):
        raise ValueError("the reply wasn't a JSON object")  # noqa: TRY004 - a parse failure
    return out


def chat_json(chat: Chat, messages: list[dict]) -> dict:
    """The reply parsed as a JSON object, with one repair turn if it doesn't parse;
    ValueError if the second reply doesn't either."""
    reply = chat(messages)
    try:
        return parse_json(reply)
    except ValueError:
        return parse_json(
            chat([*messages, {"role": "assistant", "content": reply}, REPAIR])
        )


def provider_for(model: str) -> str:
    """Which provider serves a model: glm-* is Z.AI, anything else DeepSeek."""
    return "zai" if re.match(r"glm-", str(model), re.IGNORECASE) else "deepseek"


@dataclass(frozen=True)
class Provider:
    name: str  # "deepseek" or "zai", as provider_for names them
    base_url: str
    key: str  # "" when it isn't set
    key_name: str

    @property
    def label(self) -> str:
        return {"deepseek": "DeepSeek", "zai": "Z.AI"}.get(self.name, self.name)


def provider(name: str, default_env: str) -> Provider:
    """Where a provider's API is and the key for it, from AnythingLLM's .env (at
    ANYTHINGLLM_ENV, else `default_env`); the same names in the environment win."""
    env = env_values(os.environ.get("ANYTHINGLLM_ENV", default_env), PROVIDER_KEYS)
    if name == "deepseek":
        return Provider(
            "deepseek",
            DEEPSEEK_BASE,
            env.get("DEEPSEEK_API_KEY", ""),
            "DEEPSEEK_API_KEY",
        )
    # When chat runs on Z.AI through the Generic OpenAI provider, that's the current
    # key; ZAI_API_KEY is left over from the built-in Z.AI provider and can be stale.
    if re.search(r"//api\.z\.ai/", env.get("GENERIC_OPEN_AI_BASE_PATH", "")):
        return Provider(
            "zai",
            ZAI_BASE,
            env.get("GENERIC_OPEN_AI_API_KEY", ""),
            "GENERIC_OPEN_AI_API_KEY",
        )
    return Provider("zai", ZAI_BASE, env.get("ZAI_API_KEY", ""), "ZAI_API_KEY")


class APIError(LLMError):
    """The API refused a request: `status` is the HTTP status (None when it couldn't be
    reached) and `code` the error code in its answer, e.g. Z.AI's "1113"."""

    def __init__(self, message: str, status: int | None = None, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


# Worth asking again: the connection, a timeout, a conflict or the server's own trouble.
# Not 429: on Z.AI that's a spent plan, which waiting a second won't fix.
RETRY_STATUS = {408, 409, 500, 502, 503, 504}


class Completions:
    """Chat completions on one provider, with usage. Retries a failed connection and a
    server error `retries` times, a second and then two apart."""

    def __init__(
        self,
        prov: Provider,
        timeout: float = 15 * 60,
        transport: httpx.BaseTransport | None = None,
        retries: int = 2,
        backoff: float = 1.0,
    ):
        self.provider = prov
        self.retries = retries
        self.backoff = backoff
        self.client = httpx.Client(
            base_url=prov.base_url,
            headers={"Authorization": f"Bearer {prov.key}"},
            timeout=timeout,
            transport=transport,
        )

    def create(
        self, model: str, messages: list[dict], max_tokens: int, think: bool = True
    ) -> tuple[str, dict]:
        """The reply's text and its usage (prompt_tokens, completion_tokens, …)."""
        body = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if not think:
            body["thinking"] = {"type": "disabled"}
        for attempt in range(self.retries + 1):
            try:
                resp = self.client.post("/chat/completions", json=body)
            except httpx.HTTPError as e:
                error = APIError(
                    f"couldn't reach {self.provider.label}: {e or type(e).__name__}"
                )
            else:
                if resp.status_code == 200:
                    break
                error = _api_error(self.provider.label, resp)
                if resp.status_code not in RETRY_STATUS:
                    raise error
            if attempt == self.retries:
                raise error
            time.sleep(self.backoff * 2**attempt)
        try:
            data = resp.json()
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
        except (ValueError, LookupError, TypeError, AttributeError):
            raise LLMError(
                f"{self.provider.label}'s answer wasn't the JSON expected: {resp.text[:200]}"
            ) from None
        if choice.get("finish_reason") == "length":
            raise LLMError(
                f"{model} ran out of output tokens (max_tokens {max_tokens})."
            )
        return content, data.get("usage") or {}


def _api_error(name: str, resp: httpx.Response) -> APIError:
    code, message = "", resp.text[:200]
    try:
        err = resp.json().get("error") or {}
        if isinstance(err, dict):
            code = str(err.get("code") or "")
            message = str(err.get("message") or message)
    except (ValueError, AttributeError):
        pass
    return APIError(
        f"{name} answered {resp.status_code}{f' ({code})' if code else ''}: {message}",
        resp.status_code,
        code,
    )


def deepseek(
    api_key: str,
    model: str,
    max_tokens: int = 8_000,
    timeout: float = 300,
    transport: httpx.BaseTransport | None = None,
) -> Chat:
    """A chat function for DeepSeek, run with thinking off and no retries: someone is
    waiting. Any failure, the connection's included, is an LLMError."""
    completions = Completions(
        Provider("deepseek", DEEPSEEK_BASE, api_key, "DEEPSEEK_API_KEY"),
        timeout=timeout,
        transport=transport,
        retries=0,
    )
    return lambda messages: completions.create(
        model, messages, max_tokens, think=False
    )[0]
