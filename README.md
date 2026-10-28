# VoiceGate

A voice receptionist for small service businesses. It books appointments by voice in English, Tenglish and Hinglish, and it cannot be talked into leaking private data.

**Core idea:** the model never sees what it isn't allowed to say. Authorization and field filtering run in code at the tool layer (the Trust Gate), *before* any tool result reaches the LLM.

> Status: **customer tier, normalizer and evals done; API and dashboard not started.** Everything is tested offline (273 tests). The live voice path and live Gemini evals have **not** been run yet, because no credentials were available.

## Quick start (no API keys needed)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt

pytest                           # full test suite, fully offline
python -m agent.text_agent       # chat in the terminal (Gemini if GOOGLE_API_KEY is set, else the offline rule brain)
python -m eval.run_all                  # all evals offline: red-team (worst-case mock), booking (rule brain), code-mixed
python -m eval.run_all --model gemini   # same evals with real Gemini (needs GOOGLE_API_KEY)
```

In text mode, `/call <tool> {json}` sends a raw tool call through the gate as the current caller, for example `/call list_bookings {}`. Use it to demo blocked attempts.

## Voice mode (needs credentials)

Copy `.env.example` to `.env` and fill in the values:

| Variable | From | Used for |
|---|---|---|
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | LiveKit Cloud project (or a local `livekit-server`) | voice transport |
| `GOOGLE_API_KEY` | Google AI Studio | Gemini Live (`google.realtime.RealtimeModel`) |

```bash
python -m agent.agent console    # talk to it from the terminal
python -m agent.agent dev        # connect to your LiveKit project
```

## Layout

| Path | What it holds |
|---|---|
| `booking/` | Config (hours, services, FAQ), SQLite schema, validation, conflict-safe verified writes, read-back tokens |
| `gate/` | Tiers, `policy.yaml` (tool and field allowlists), authz, filters, redaction, audit |
| `agent/` | Shared tools, versioned prompts, LiveKit/Gemini agent, offline text harness |
| `tests/` | pytest suite |
| `docs/prompt-log.md` | AI prompt log |

Stack: Python 3.13 · LiveKit Agents 1.8.9 + Gemini Live · FastAPI · Pydantic · SQLite · React + Vite · pytest.
