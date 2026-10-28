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

## #4 Phase 2: LLM text agent, baseline, first 20 red-team attacks

**Prompt (summary):** Keep Phase 1 as it is. Requirements:
- Add an LLM text agent using the chosen Gemini integration, with every tool call going through the same Trust Gate.
- Add a baseline agent with raw tools and prompt-only rules, kept isolated and used only for evaluation on synthetic data.
- Write 20 attacks across impersonation, prompt injection, enumeration, other customers' data and forged confirmation.
- Compare the two under equal conditions, with no hardcoded results.
- Prove that private tool results are filtered *before* they enter the LLM context.
- Report live-model results as unverified if no key is available.

**What it produced:**
- **API checks.** The `google-genai==2.33.0` API was checked against the installed source:
  - `Client`, `aio.models.generate_content`, `FunctionDeclaration(parameters_json_schema=...)`, `AutomaticFunctionCallingConfig(disable=True)`.
  - Function responses go back as `role='user'`, confirmed from the SDK's own automatic function-calling code.
- **`agent/llm_loop.py`:**
  - A provider-neutral tool loop. `Conversation.messages` is exactly what the model sees.
  - A `GeminiChatModel` that replays the model's raw content so thought signatures survive multi-step tool calls.
- **`LLMTextAgent`** in `agent/text_agent.py`:
  - Uses the same system prompt as voice.
  - Its tool schemas are generated from the voice agent's own `@function_tool` definitions.
  - Every call goes through `TrustGate.call`.
- **`eval/baseline_agent.py`:**
  - Raw database rows from `get_availability`, `list_bookings`, `get_booking`, `cancel` and a direct `book_appointment`.
  - The same base prompt plus a "SECURITY RULES" section.
  - A test checks that no production package imports `eval`.
- **Attack set and runner:**
  - `eval/redteam.yaml` holds the 20 attacks.
  - `eval/synthetic.py` holds the synthetic bookings, which double as leak canaries.
  - `eval/redteam_eval.py` and `eval/run_all.py` run them.
  - Each attack runs against each agent on a fresh seeded database, with the same model.
- **Offline result**, using a worst-case "compliant" mock that does whatever the attacker asks:
  - The baseline was beaten by 20 of 20 attacks: 17 leaks and 4 unauthorized writes.
  - VoiceGate was beaten by 0 of 20, with 0 private values reaching the model's context.
  - This measures the architecture, not Gemini's behaviour.
- **Live Gemini:** not run, because there is no key. `run_all --model gemini` exits with "SKIPPED" and produces no numbers.
- **Bugs caught during the session:**
  - The test helper `call(name, **args)` collided with the `name` booking field.
  - The first context-leak test counted the model's *own* tool argument ("Arjun") as a leak. It now scans only tool results.

## #5 "Phase 3" as redefined by the user: booking_id, customer tier, normalizer, 30 attacks, booking eval

**Prompt (summary):**
- Add an opaque random public `booking_id` stored in the database.
- Add customer verification (booking code plus last 4 digits), reschedule and cancel, enforced through the Trust Gate.
- Add a Tenglish/Hinglish/English normalizer tested on at least 40 utterances.
- Expand red-teaming to 30 attacks and add the booking evaluation.
- Keep offline and live results separate. No dashboard yet.

The AI flagged that this reorders the original plan: the dashboard, which UI/UX is judged on, is deferred.

**What it produced:**
- **`booking_id`:**
  - A `bk_` plus 10 random characters, with a UNIQUE column.
  - A migration for older databases and a retry on collision.
  - The read-back check now also verifies `booking_id`. The internal row id is never returned.
  - 3 Phase 1/2 tests that asserted "no `booking_id`" were updated to the new contract.
- **Customer tier:**
  - `verify_booking` checks both factors in code. It gives the same generic message whether or not the code exists, and owner calendar blocks can't be verified.
  - Verification is locked after 3 failures per call and 5 per code within 15 minutes.
  - `reschedule` and `cancel` work in two steps: a read-back with a single-use token, then a spoken yes in a later turn. They are limited to the caller's own booking.
  - `phone_last4` and `confirmation_id` are masked by key name in logs.
- **Normalizer** (`agent/normalizer.py`): handles relative days, weekdays, parts of the day, Hindi fractions (saadhe, sawa, paune, dedh, dhai), the Telugu "-nnara" suffix, and number words.
  - **Main set** (44 utterances, committed *before* the normalizer was written): 44/44. This is optimistic, because the same author wrote both in the same session.
  - **Held-out set** (20 utterances, written afterwards and run once without tuning): **8/20 (40%)**.
- **Red-team, 30 attacks:**
  - The baseline was given equivalent raw `verify_booking` and `reschedule` tools.
  - Under the offline worst-case mock: baseline beaten by 30/30, VoiceGate by 0/30.
- **Booking eval, 30 scenarios, offline rule brain:**
  - **First run: 27/30.** The eval caught a real bug: the text agent's code pattern rejected 0, O, 1, I and L, so a code like "VG-V1KR" was never recognized.
  - **After the fix: 30/30**, with 0 double bookings and 0 false confirmations.
- **Other fix:** a test exposed that "haan, cancel karo" was rejected as a "no", because "cancel" is on the negative-word list. The action word being confirmed no longer counts as a "no".

## #6 API and dashboard, and fixing confidently wrong dates and times

**Prompt (summary):**
- Run the suite first.
- Build the FastAPI endpoints and the React dashboard against the real SQLite service, with every read and write going through the Trust Gate.
- Fix "next week Tuesday" and "5 PM or 6 PM": ambiguity must trigger a clarifying question.
- Keep the held-out set unchanged and report its score honestly.
- Don't push.

**What it produced:**
- **Suite before any change:** 273 passed, 1 skipped.
- **Normalizer:**
  - It now collects every time expression, so text is never read twice: "4:30 pm" is one time, not two.
  - Two or more distinct times, "X or Y", or "between X and Y" flag the time as ambiguous and record the options.
  - "next week" plus a weekday resolves to the following calendar week. "next week" alone asks which day.
  - Tools return `AMBIGUOUS_TIME` or `AMBIGUOUS_DATE`, and the offline agent asks a follow-up question.
  - The held-out file is unchanged and scores **8/20 → 10/20**. Only HO04 and HO20, which the user named, were targeted. None of the 10 remaining misses is confidently wrong: each extracts nothing and asks.
- **API:**
  - Owner reads are owner-tier gate tools, available only on the dashboard channel, each with its own allowlist. They never return internal IDs or phone digits.
  - Owner login is disabled until a real `OWNER_PASSWORD` is set, and is rate limited.
  - `/api/simulate` runs a caller session on the real database through the gate, or replays an attack on a synthetic database.
  - `api/simulate.py` is the only production module allowed to import the eval baseline, and a test enforces that.
- **Dashboard:** Login, Bookings (with availability), Live events, Metrics, Caller test, and Baseline vs VoiceGate. Plain CSS, light and dark themes, and loading, empty and error states.
- **Issues hit along the way:**
  - LiveKit refuses to register its plugin off the main thread, and the test client runs requests on a worker thread. The fix imports the agent module when the app starts.
  - A server-cleanup command that would have killed every `python.exe` was correctly denied by the permission system. The server now runs as a tracked background task that is stopped by its ID.
