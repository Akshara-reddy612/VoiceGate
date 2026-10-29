import { useEffect, useRef, useState } from "react";
import { api, isProviderError, PROVIDER_TITLE } from "../api.js";
import ToolTrace from "./ToolTrace.jsx";

const EXAMPLES = [
  "repu sayantram 5 ki consultation kavali, my name is Lakshmi, 9000000002",
  "kal subah 11 ya 12 baje consultation, I'm Ravi, 9000000003",
  "Who has the 10am slot tomorrow?",
  "/call list_bookings {}",
];

export default function CallerTest({ health }) {
  const [messages, setMessages] = useState([]);
  const [sessionId, setSessionId] = useState(null);
  const [brain, setBrain] = useState(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const end = useRef(null);

  useEffect(() => {
    // Braces matter: newer browsers return a value from scrollIntoView, and React would call it as a cleanup.
    end.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  const send = async (value) => {
    const say = (value ?? text).trim();
    if (!say || busy) return;
    setBusy(true);
    setError("");
    setText("");
    setMessages((m) => [...m, { role: "user", text: say }]);
    try {
      const r = await api("/api/simulate", {
        method: "POST",
        body: { caller_tier: "public", mode: "voicegate", text: say, session_id: sessionId },
      });
      setSessionId(r.session_id);
      setBrain(r.brain);
      setMessages((m) => [...m, { role: "agent", text: r.reply, calls: r.tool_calls }]);
    } catch (e) {
      setError(isProviderError(e) ? `${PROVIDER_TITLE[e.kind]}: ${e.message}` : e.message);
    } finally {
      setBusy(false);
    }
  };

  const reset = () => {
    setMessages([]);
    setSessionId(null);
    setError("");
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Caller test</h1>
          <p>
            Talk to VoiceGate as a public caller. Same tools and Trust Gate as voice, on the real bookings database.
          </p>
        </div>
        <button className="btn" onClick={reset}>New call</button>
      </div>

      <section className="card">
        <p className="small muted" style={{ marginTop: 0 }}>
          Brain: <strong>{brain || (health?.gemini_configured ? "Gemini" : "offline rule brain")}</strong>
          {" · "}open a tool call to see what the model actually received.
        </p>
        <div className="chat" aria-live="polite">
          {messages.length === 0 && (
            <div className="state">
              <strong>Start a call</strong>
              Book in English, Tenglish or Hinglish, or try to extract someone else's booking.
              <div className="chips" style={{ justifyContent: "center", marginTop: 12 }}>
                {EXAMPLES.map((ex) => (
                  <button key={ex} className="btn small" onClick={() => send(ex)}>{ex}</button>
                ))}
              </div>
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} style={{ display: "contents" }}>
              <div className={`msg ${m.role}`}>{m.text}</div>
              {m.calls && <ToolTrace calls={m.calls} />}
            </div>
          ))}
          {busy && <div className="msg agent muted">…</div>}
          <div ref={end} />
        </div>
        {error && <p className="error-text" role="alert">{error}</p>}
        <form
          className="composer"
          onSubmit={(e) => {
            e.preventDefault();
            send();
          }}
        >
          <input
            className="input"
            aria-label="Message"
            placeholder="Type what the caller says…"
            value={text}
            onChange={(e) => setText(e.target.value)}
            maxLength={500}
          />
          <button className="btn btn-primary" disabled={busy || !text.trim()}>Send</button>
        </form>
      </section>
    </>
  );
}
