import { useEffect, useState } from "react";
import { api } from "../api.js";
import ToolTrace from "./ToolTrace.jsx";

function Side({ title, subtitle, result, loading }) {
  return (
    <section className="card" aria-label={title}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "baseline" }}>
        <h2>{title}</h2>
        {result &&
          (result.leaked.length ? (
            <span className="badge badge-bad">leaked {result.leaked.length}</span>
          ) : (
            <span className="badge badge-ok">no leak</span>
          ))}
      </div>
      <p className="muted small" style={{ margin: "4px 0 12px" }}>{subtitle}</p>
      {loading && (
        <>
          <div className="skeleton" />
          <div className="skeleton" style={{ width: "70%" }} />
        </>
      )}
      {!loading && !result && <div className="state">Pick an attack and press Replay.</div>}
      {result && (
        <>
          {result.turns.map((t, i) => (
            <div key={i} className="chat" style={{ minHeight: 0, maxHeight: "none" }}>
              <div className="msg user">{t.caller}</div>
              <div className="msg agent">{t.reply}</div>
            </div>
          ))}
          <ToolTrace calls={result.tool_calls} />
          {result.leaked.length > 0 && (
            <p className="small" style={{ marginTop: 12 }}>
              Private data in replies: {result.leaked.map((c) => <mark key={c}>{c}</mark>).reduce((a, b) => [a, ", ", b])}
            </p>
          )}
        </>
      )}
    </section>
  );
}

export default function Compare() {
  const [attacks, setAttacks] = useState([]);
  const [attackId, setAttackId] = useState("RT01");
  const [results, setResults] = useState({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api("/api/attacks").then(setAttacks).catch((e) => setError(e.message));
  }, []);

  const attack = attacks.find((a) => a.id === attackId);

  const replay = async () => {
    setLoading(true);
    setError("");
    setResults({});
    try {
      const [baseline, voicegate] = await Promise.all(
        ["baseline", "voicegate"].map((mode) =>
          api("/api/simulate", { method: "POST", body: { caller_tier: "public", mode, attack_id: attackId } }),
        ),
      );
      setResults({ baseline, voicegate });
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Baseline vs VoiceGate</h1>
          <p>
            The same attack, the same model, the same synthetic data. The baseline has raw tools and rules in its
            prompt; VoiceGate filters in code before the model sees anything.
          </p>
        </div>
      </div>

      <section className="card">
        <div className="toolbar" style={{ marginBottom: 0 }}>
          <select
            className="select"
            aria-label="Attack"
            value={attackId}
            onChange={(e) => setAttackId(e.target.value)}
            style={{ flex: "1 1 260px" }}
          >
            {attacks.map((a) => (
              <option key={a.id} value={a.id}>
                {a.id} · {a.category.replace("_", " ")} · {a.turns[0].slice(0, 60)}
                {a.turns[0].length > 60 ? "…" : ""}
              </option>
            ))}
          </select>
          <button className="btn btn-primary" onClick={replay} disabled={loading || !attacks.length}>
            {loading ? "Replaying…" : "Replay"}
          </button>
        </div>
        {attack && (
          <p className="small muted" style={{ marginBottom: 0 }}>
            Caller says: “{attack.turns.join(" → ")}”
          </p>
        )}
        {results.voicegate && (
          <p className="small" style={{ marginBottom: 0 }}>
            Model: <strong>{results.voicegate.brain}</strong>
          </p>
        )}
        {error && <p className="error-text" role="alert">{error}</p>}
      </section>

      <div className="grid grid-2" style={{ marginTop: 16, alignItems: "start" }}>
        <Side title="Baseline" subtitle="Raw tool access, security rules in the prompt only." result={results.baseline} loading={loading} />
        <Side title="VoiceGate" subtitle="Trust Gate: tier check, authorization, field allowlist, audit." result={results.voicegate} loading={loading} />
      </div>
    </>
  );
}
