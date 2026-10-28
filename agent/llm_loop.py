"""Provider-neutral tool-calling loop used by the LLM text agent and by the evaluation baseline.

The loop never decides what a tool may return: it hands each call to an `executor`. For VoiceGate the
executor is the Trust Gate; for the eval baseline it is raw tool access. `Conversation.messages` is the
exact context the model receives, so tests can inspect it.
"""

from __future__ import annotations

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

DEFAULT_TEXT_MODEL = "gemini-2.5-flash"


def gemini_configured() -> bool:
    return bool(os.environ.get("GOOGLE_API_KEY"))


class GeminiChatModel:
    """Gemini via google-genai with manual function calling (automatic calling disabled)."""

    def __init__(self, model: str | None = None, api_key: str | None = None, temperature: float = 0.2) -> None:
        from google import genai  # imported lazily so offline tests never need it configured

        self.name = model or os.environ.get("VOICEGATE_TEXT_MODEL", DEFAULT_TEXT_MODEL)
        self._client = genai.Client(api_key=api_key or os.environ.get("GOOGLE_API_KEY"))
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
        resp = await self._client.aio.models.generate_content(model=self.name, contents=self._contents(messages), config=config)
        calls = [ToolCall(name=c.name, args=dict(c.args or {}), id=c.id) for c in (resp.function_calls or [])]
        raw = resp.candidates[0].content if resp.candidates and resp.candidates[0].content else None
        text = "" if calls else (resp.text or "")
        return ModelReply(text=text, tool_calls=calls, raw=raw)
