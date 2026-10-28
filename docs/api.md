# VoiceGate API

The API is FastAPI, served by `uvicorn api.main:app --port 8000`. It runs on localhost only.

**Auth:**
1. Call `POST /api/login` with the owner password to get a token.
2. Send that token on every owner endpoint as `Authorization: Bearer <token>`.
3. Tokens live in server memory for 8 hours, so a restart logs everyone out.

**Trust Gate:** every data read below runs through it as the **owner** tier, which exists only on the **dashboard** channel. Each read has its own field allowlist in `gate/policy.yaml`. Even the owner never receives internal row IDs or phone digits, because the last 4 digits are half of a customer's credential.

## Endpoints

| Method and path | Auth | Description |
|---|---|---|
| `GET /api/health` | none | `{status, login_configured, gemini_configured, livekit_configured}`. Booleans only, never secrets. |
| `POST /api/login` | none | Body `{password}` returns `{token}`. See the login errors below. |
| `POST /api/logout` | token | Invalidates the token. |
| `GET /api/bookings?date=YYYY-MM-DD&status=confirmed\|cancelled` | owner | `[{id, code, customer, start, end, purpose, status}]`. `id` is the opaque public `booking_id` (`bk_…`). |
| `GET /api/availability?date=YYYY-MM-DD` | owner | `{status, date, slots:[{start, end}]}` with free 30-minute starts. Returns 400 for a closed day or past date. |
| `GET /api/events?limit=100` | owner | `[{ts, tier, tool, blocked, reason}]`, newest first. |
| `GET /api/metrics` | owner | The latest result for each eval suite, from `eval_runs`, with `mode` set to `offline` or `live_gemini`. Returns `{available:false}` before any run. |
| `GET /api/attacks` | owner | The 30 red-team attacks as `[{id, category, turns}]`. |
| `POST /api/simulate` | owner | See the next section. |

**Login errors:**
- **503:** `OWNER_PASSWORD` is unset, or still a placeholder such as `change-me`.
- **401:** wrong password.
- **429:** 5 failures within one minute from one client.

**Owner reads aren't audited:** reads from `bookings`, `events`, `availability` and `metrics` are **not** written to the audit log, because the dashboard polls them. Any blocked attempt is always written.

## POST /api/simulate

The request body is `{caller_tier: "public", text, mode: "voicegate"|"baseline", session_id?, attack_id?}`.

**`caller_tier`** must be `"public"`:
- `"owner"` returns 403.
- `"customer"` returns 400. The customer tier is granted only by `verify_booking`, using the booking code plus the last 4 digits, inside the conversation.

**Modes:**

| Request | Database | Brain |
|---|---|---|
| `mode: "voicegate"`, no `attack_id` | The **real** bookings database, through the Trust Gate. Bookings made here show up on the Bookings page. | Gemini if `GOOGLE_API_KEY` is set, otherwise the offline rule brain. |
| `mode: "baseline"`, no `attack_id` | A synthetic throwaway database. | Needs Gemini. Without it, returns 503. |
| `attack_id: "RT01"` (either mode) | A fresh synthetic database. | Gemini if configured, otherwise the **offline worst-case mock**: it obeys the attacker and is not an LLM. |

For an `attack_id` request, all of the attack's turns are replayed.

**Chat response:** `{session_id, mode, brain, reply, tool_calls:[{tool, args (redacted), blocked, reason, result}], blocked, database}`. Here `result` is exactly what the model received.

**Replay response:** adds `{attack, turns:[{caller, reply}], leaked:[canaries]}`.

The eval-only baseline is reachable only through this owner-authenticated endpoint, and only on synthetic data. `api/simulate.py` is the only production module allowed to import `eval/`, and a test enforces that.
