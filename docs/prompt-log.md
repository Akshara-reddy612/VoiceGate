# Prompt Log

This is a record of the major prompts given to the AI pair-programmer (Claude Code, Claude Opus 5.5) while building VoiceGate, with what each one produced. Prompts are summarised; quoted text is verbatim.

---

## #1 Project brief and first plan request

**Prompt (summary):** Full VoiceGate spec, covering product, problem statements #70 and #56, stack, folder layout, caller tiers, tools, booking rules, code-mixed handling, API, schema, evaluation, dashboard and a 7-phase build order. It asked the AI to "Start by reading the workshop code, then give me a short plan for Phase 1 before writing any code."

**What it produced:**
- A workspace inspection. The `VoiceGate/` folder was empty, and no LiveKit or Gemini code was found anywhere in Documents, Downloads or Desktop.
- A check of the toolchain: Python 3.13.2, Node 24.11, git 2.55.
- A draft Phase 1 plan, plus a question about where the starter code was.
- **Decision:** no code was written, because the spec assumed starter code that did not exist.

## #2 No starter code; build from scratch

**Prompt (summary):** "We do NOT have any existing LiveKit workshop starter code. We need to build VoiceGate from scratch." Use the official LiveKit and Gemini docs, do not guess APIs, keep the booking core independent of the voice provider, add a text harness that needs no keys, add the Trust Gate early, and test before integrating anything external.

**What it produced:**
- The docs were read: the LiveKit Agents overview, the voice AI quickstart, function tools, and the Gemini realtime plugin page.
- Verified facts:
  - Install with `livekit-agents[google]~=1.8`. The latest on PyPI is 1.8.9.
  - The Gemini model is `google.realtime.RealtimeModel` and reads its key from `GOOGLE_API_KEY`.
  - Tools use `@function_tool()`, `RunContext` and `ToolError`, and the server uses `AgentServer` and `rtc_session`.
  - Gemini Live does not support asynchronous function calling.
- One discrepancy was flagged: the official quickstart uses `inference.RealtimeModel` (LiveKit Cloud's gateway), but this project uses the Google plugin directly, as the spec requires.
- A revised from-scratch Phase 1 plan.

## #3 Approve Phase 1, with two adjustments

**Prompt (summary):** Proceed with Phase 1 in the current folder. No credentials are available, so everything must work offline. The two adjustments:
1. "Use explicit allowlists for every tool response. Do not expose phone numbers, internal booking records, or unnecessary fields."
2. "Require a valid, short-lived confirmation ID tied to the exact booking details and an explicit caller confirmation. Reject modified, expired, reused, or unconfirmed requests."

**What it produced:**
- Each API was checked against the installed `livekit-agents==1.8.9` source before use: `AgentServer.rtc_session`, `AgentSession.start`, the `RealtimeModel` constructor, the `user_input_transcribed` event, and the `LIVEKIT_*` env var names.
- Phase 1 was built: the booking core, Trust Gate, shared tools, offline text harness, voice wiring, and tests. See the git history for each step.
- **Result:** 158 pytest tests pass offline. The live voice path is untested because no credentials were available.
- **Bugs the AI caught in its own code during the session:**
  - The phone-redaction regex masked "2026-10-29 10:00" (a date plus time) as a phone number. It now uses lookarounds.
  - The text harness read "2026-10-29 10am" as a 10-digit phone number. ISO dates are now masked before phone detection.
  - The spoken-"yes" check compared timestamps. Windows' monotonic clock ticks in about 15 ms steps, so "yes, book it" in the same message as the request could count as confirming the read-back. It now uses a strict per-caller turn counter.
  - A tool-level rejection, such as a forged confirmation ID, was audited as `blocked=0`. It is now audited as blocked, with the rejection code as the reason.
  - A heredoc patch turned `\b` into a literal backspace character in a regex. This was caught by inspecting the bytes and fixed.
- **Deviation from the spec, made on purpose:** `book_appointment` returns `{status, code, service, date, start, end}` with no `booking_id`. Prompt #3 asked for no internal records or unnecessary fields, and an auto-increment ID would leak how many bookings the business has. The booking code identifies the booking instead.
