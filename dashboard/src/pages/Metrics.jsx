import { useEffect, useState } from "react";
import { api } from "../api.js";

const pct = (x) => (x == null ? "–" : `${Math.round(x * 100)}%`);
const OUTCOME = { defended: "badge-ok", succeeded: "badge-bad", error: "badge-warn" };

function Stat({ label, value, sub, bad }) {
  return (
    <div className="card stat">
      <div className="label">{label}</div>
      <div className="value" style={{ color: bad ? "var(--bad)" : undefined }}>{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );
}

function Codemix({ title, data, note }) {
  if (!data) return null;
  return (
    <section className="card">
      <h2>{title}</h2>
      <p className="muted small" style={{ margin: "4px 0 12px" }}>{note}</p>
      <div className="grid grid-4">
        <Stat label="Accuracy" value={pct(data.accuracy)} sub={`${data.correct}/${data.total} utterances`} />
        <Stat label="Tenglish" value={pct(data.by_lang?.tenglish)} />
        <Stat label="Hinglish" value={pct(data.by_lang?.hinglish)} />
        <Stat label="English" value={pct(data.by_lang?.english)} />
      </div>
      {data.failures?.length > 0 && (
        <details style={{ marginTop: 12 }}>
          <summary className="small">{data.failures.length} failures</summary>
          <ul className="small">
            {data.failures.map((f) => (
              <li key={f.id}><span className="mono">{f.id}</span> “{f.text}”</li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

export default function Metrics() {
  const [state, setState] = useState({ loading: true });

  useEffect(() => {
    api("/api/metrics")
      .then((m) => setState({ m }))
      .catch((e) => setState({ error: e.message }));
  }, []);

  if (state.loading) return <div className="card"><div className="skeleton" /><div className="skeleton" /></div>;
  if (state.error) return <div className="banner banner-bad" role="alert">{state.error}</div>;
  const m = state.m;
  if (!m.available)
    return (
      <div className="card state">
        <strong>No evaluation results yet</strong>
        Run <code>python -m eval.run_all</code> (offline) or <code>python -m eval.run_all --model gemini</code> (live).
      </div>
    );

  const rt = m.redteam;
  const live = m.mode === "live_gemini";
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Metrics</h1>
          <p>Latest evaluation run · {m.ts?.replace("T", " ").slice(0, 16)} UTC</p>
        </div>
        <span className={`badge ${live ? "badge-ok" : "badge-warn"}`}>
          {live ? "Live Gemini results" : "Offline results (mock / rule brain)"}
        </span>
      </div>

      {rt && (
        <section className="card">
          <h2>Red-team: {rt.attacks} attacks</h2>
          <p className="muted small" style={{ margin: "4px 0 12px" }}>{rt.measured_with}</p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th scope="col">Measure</th><th scope="col">Baseline</th><th scope="col">VoiceGate</th></tr>
              </thead>
              <tbody>
                {[
                  ["Attacks that succeeded", "succeeded"],
                  ["Private data in replies", "leaks"],
                  ["Unauthorized writes", "unauthorized_writes"],
                  ["Brute-forced verification", "forbidden_successes"],
                  ["Private data reached the model", "context_exposures"],
                  ["Tool calls blocked", "blocked_calls"],
                ].map(([label, key]) => (
                  <tr key={key}>
                    <td>{label}</td>
                    <td className="num">{rt.baseline[key]}</td>
                    <td className="num" style={{ fontWeight: 700 }}>{rt.voicegate[key]}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <details style={{ marginTop: 12 }}>
            <summary className="small">Per attack</summary>
            <div className="table-wrap">
              <table>
                <thead><tr><th>Attack</th><th>Category</th><th>Baseline</th><th>VoiceGate</th></tr></thead>
                <tbody>
                  {rt.rows.map((r) => (
                    <tr key={r.id}>
                      <td className="mono">{r.id}</td>
                      <td>{r.category.replace("_", " ")}</td>
                      <td><span className={`badge ${OUTCOME[r.baseline]}`}>{r.baseline}</span></td>
                      <td><span className={`badge ${OUTCOME[r.voicegate]}`}>{r.voicegate}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </section>
      )}

      {m.booking && (
        <section className="card">
          <h2>Booking correctness</h2>
          <p className="muted small" style={{ margin: "4px 0 12px" }}>{m.booking.measured_with}</p>
          <div className="grid grid-4">
            <Stat label="Correct outcome" value={`${m.booking.outcome_correct}/${m.booking.cases}`} />
            <Stat label="Double bookings" value={m.booking.double_bookings} sub="target 0" bad={m.booking.double_bookings > 0} />
            <Stat label="False confirmations" value={m.booking.false_confirmations} sub="target 0" bad={m.booking.false_confirmations > 0} />
            <Stat label="Errors" value={m.booking.errors} bad={m.booking.errors > 0} />
          </div>
          {m.booking.failures?.length > 0 && (
            <ul className="small">{m.booking.failures.map((f) => <li key={f.id}>{f.id}: {f.desc}</li>)}</ul>
          )}
        </section>
      )}

      <Codemix title="Code-mixed: main set (44)" data={m.codemix}
               note="Written before the normalizer, by the same author, so treat as optimistic." />
      <Codemix title="Code-mixed: held-out set (20)" data={m.codemix_heldout}
               note="The realistic number. Only HO04 and HO20 were targeted (on request); the rest were never tuned against." />

      <section className="card">
        <h2>Latency</h2>
        <p className="muted" style={{ marginBottom: 0 }}>Not measured yet. Time-to-first-response arrives with the next evaluation phase.</p>
      </section>
    </>
  );
}
