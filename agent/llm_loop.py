"""Provider-neutral tool-calling loop used by the LLM text agent and by the evaluation baseline.

The loop never decides what a tool may return: it hands each call to an `executor`. For VoiceGate the
executor is the Trust Gate; for the eval baseline it is raw tool access. `Conversation.messages` is the
exact context the model receives, so tests can inspect it.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

MAX_TOOL_STEPS = 6


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    id: str | None = None


@dataclass
class Message:
    role: Literal["user", "model", "tool"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_name: str = ""
    tool_result: dict[str, Any] | None = None
    tool_call_id: str | None = None
    raw: Any = None  # provider-native content, replayed verbatim (keeps Gemini thought signatures)


@dataclass
class ModelReply:
    text: str
    tool_calls: list[ToolCall]
    raw: Any = None


class ChatModel(Protocol):
    name: str

    async def generate(self, system: str, messages: list[Message], tools: list[dict[str, Any]]) -> ModelReply: ...


@dataclass
class ExecResult:
    result: dict[str, Any]  # what goes into the model context
    display_args: dict[str, Any]  # safe to show/log
    blocked: bool = False
    reason: str = ""


Executor = Callable[[str, dict[str, Any]], Awaitable[ExecResult]]


@dataclass
class ToolCallRecord:
    tool: str
    args: dict[str, Any]
    result: dict[str, Any]
    blocked: bool
    reason: str


@dataclass
class Conversation:
    system: str
    tools: list[dict[str, Any]]
    messages: list[Message] = field(default_factory=list)


async def run_turn(model: ChatModel, convo: Conversation, executor: Executor, user_text: str) -> tuple[str, list[ToolCallRecord]]:
    convo.messages.append(Message(role="user", text=user_text))
    records: list[ToolCallRecord] = []
    for _ in range(MAX_TOOL_STEPS):
        reply = await model.generate(convo.system, convo.messages, convo.tools)
        convo.messages.append(Message(role="model", text=reply.text, tool_calls=reply.tool_calls, raw=reply.raw))
        if not reply.tool_calls:
            return reply.text, records
        for call in reply.tool_calls:
            ex = await executor(call.name, dict(call.args or {}))
            records.append(ToolCallRecord(call.name, ex.display_args, ex.result, ex.blocked, ex.reason))
            convo.messages.append(
                Message(role="tool", tool_name=call.name, tool_result=ex.result, tool_call_id=call.id)
            )
    return "Sorry, I couldn't finish that. Could you say it again?", records


# ---------------------------------------------------------------- Gemini (google-genai SDK)

# gemini-2.5-flash now returns 404 "no longer available to new users" (seen live, 2026-10-29); override per key with
# VOICEGATE_TEXT_MODEL in .env.
DEFAULT_TEXT_MODEL = "gemini-3.8-flash"
# One stuck Gemini call must not hang a dashboard request forever. Measured 2026-10-29: the same tiny
# gemini-3.8-flash request took 10 s and 77 s, so 60 s was too tight.
REQUEST_TIMEOUT_SECONDS = 90
# 503 (Google overloaded) is brief and server-side: bounded exponential backoff via the SDK's own
# HttpRetryOptions (google-genai 2.33; off by default = 1 attempt). Never retries 4xx errors such as a bad model.
RETRY_ATTEMPTS = 3  # total attempts for 503: 1 call + 2 retries
RETRY_STATUS_CODES = [503]
RETRY_INITIAL_DELAY_SECONDS = 1.0  # then 2 s (exp_base 2), capped at 8 s, plus up to 0.5 s jitter
RETRY_MAX_DELAY_SECONDS = 8.0
RETRY_JITTER_SECONDS = 0.5
# 429 (quota/rate limit) is NOT retried blindly: Gemini limits are per project per minute/day, so a 1-2 s retry
# only burns more quota. One retry at most, only if Google's suggested wait is short and the quota is not daily.
RATE_LIMIT_MAX_RETRIES = 1
RATE_LIMIT_MAX_WAIT_SECONDS = 15.0
RATE_LIMIT_DEFAULT_WAIT_SECONDS = 2.0  # when Google gives no retry delay


def quota_details(e: Exception) -> dict[str, Any]:
    """Quota facts from a 429 body (google.rpc QuotaFailure / RetryInfo), if Google included them. Never the key."""
    info: dict[str, Any] = {"quota_id": None, "quota_metric": None, "quota_limit": None, "retry_after_seconds": None}
    body = getattr(e, "details", None)
    details = (body or {}).get("error", {}).get("details", []) if isinstance(body, dict) else []
    for d in details if isinstance(details, list) else []:
        kind = str(d.get("@type", ""))
        if kind.endswith("QuotaFailure") and d.get("violations"):
            v = d["violations"][0]
            info.update(quota_id=v.get("quotaId"), quota_metric=v.get("quotaMetric"), quota_limit=v.get("quotaValue"))
        elif kind.endswith("RetryInfo") and str(d.get("retryDelay", "")).endswith("s"):
            try:
                info["retry_after_seconds"] = float(str(d["retryDelay"])[:-1])
            except ValueError:
                pass
    ident = f"{info['quota_id'] or ''} {info['quota_metric'] or ''}".lower()
    info["daily"] = "perday" in ident.replace("_", "") or "per_day" in ident
    return info


def gemini_http_options(httpx_async_client: Any = None) -> Any:
    from google.genai import types

    return types.HttpOptions(
        timeout=REQUEST_TIMEOUT_SECONDS * 1000,  # milliseconds, per attempt
        retry_options=types.HttpRetryOptions(
            attempts=RETRY_ATTEMPTS,
            initial_delay=RETRY_INITIAL_DELAY_SECONDS,
            max_delay=RETRY_MAX_DELAY_SECONDS,
            exp_base=2.0,
            jitter=RETRY_JITTER_SECONDS,
            http_status_codes=RETRY_STATUS_CODES,
        ),
        httpx_async_client=httpx_async_client,
    )


def credential_set(name: str) -> bool:
    """True only for a real value: the .env.example placeholders ("your-...") count as unset, so a fresh
    `copy .env.example .env` never triggers external API calls with a fake key."""
    value = (os.environ.get(name) or "").strip()
    return bool(value) and not value.startswith(("your-", "wss://your-project"))


def gemini_configured() -> bool:
    return credential_set("GOOGLE_API_KEY")


class GeminiChatModel:
    """Gemini via google-genai with manual function calling (automatic calling disabled)."""

    def __init__(
        self, model: str | None = None, api_key: str | None = None, temperature: float = 0.2,
        httpx_async_client: Any = None,  # tests inject a fake transport here
    ) -> None:
        from google import genai  # imported lazily so offline tests never need it configured

        self.name = model or os.environ.get("VOICEGATE_TEXT_MODEL", DEFAULT_TEXT_MODEL)
        self._client = genai.Client(
            api_key=api_key or os.environ.get("GOOGLE_API_KEY"),
            http_options=gemini_http_options(httpx_async_client),
        )
        self._temperature = temperature

    def _contents(self, messages: list[Message]) -> list[Any]:
        from google.genai import types

        out: list[Any] = []
        for m in messages:
            if m.role == "user":
                out.append(types.Content(role="user", parts=[types.Part.from_text(text=m.text)]))
            elif m.role == "model":
                if m.raw is not None:
                    out.append(m.raw)
                else:
                    parts = [types.Part.from_text(text=m.text)] if m.text else []
                    parts += [types.Part(function_call=types.FunctionCall(name=c.name, args=c.args, id=c.id)) for c in m.tool_calls]
                    out.append(types.Content(role="model", parts=parts))
            else:
                part = types.Part.from_function_response(name=m.tool_name, response=m.tool_result or {})
                if m.tool_call_id:
                    part.function_response.id = m.tool_call_id
                out.append(types.Content(role="user", parts=[part]))
        return out

    async def _generate_with_rate_limit(self, **kwargs: Any) -> Any:
        """One generateContent request (503s are retried inside the SDK). A 429 is retried at most
        RATE_LIMIT_MAX_RETRIES times, only for a short non-daily limit; otherwise it is raised at once."""
        from google.genai import errors

        retries = 0
        while True:
            try:
                return await self._client.aio.models.generate_content(model=self.name, **kwargs)
            except errors.APIError as e:
                if e.code != 429 or retries >= RATE_LIMIT_MAX_RETRIES:
                    raise
                q = quota_details(e)
                wait = q["retry_after_seconds"] if q["retry_after_seconds"] is not None else RATE_LIMIT_DEFAULT_WAIT_SECONDS
                if q["daily"] or wait > RATE_LIMIT_MAX_WAIT_SECONDS:
                    raise  # waiting cannot help within this request: fail fast, spend no more quota
                retries += 1
                await asyncio.sleep(wait)

    async def generate(self, system: str, messages: list[Message], tools: list[dict[str, Any]]) -> ModelReply:
        from google.genai import types

        decls = [
            types.FunctionDeclaration(name=t["name"], description=t["description"], parameters_json_schema=t["parameters"])
            for t in tools
        ]
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=decls)] if decls else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=self._temperature,
        )
        resp = await self._generate_with_rate_limit(contents=self._contents(messages), config=config)
        calls = [ToolCall(name=c.name, args=dict(c.args or {}), id=c.id) for c in (resp.function_calls or [])]
        raw = resp.candidates[0].content if resp.candidates and resp.candidates[0].content else None
        text = "" if calls else (resp.text or "")
        return ModelReply(text=text, tool_calls=calls, raw=raw)
