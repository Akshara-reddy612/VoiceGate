import { useEffect, useState } from "react";
import { api, ApiError, isProviderError, PROVIDER_TITLE } from "../api.js";
import ToolTrace from "./ToolTrace.jsx";

function Failed({ error }) {
  // A failed model request is shown as "not evaluated", never as a defended (no-leak) or leaked result.
  const provider = isProviderError(error);
  return (
    <div className={`banner ${provider ? "banner-warn" : "banner-bad"}`} role="alert">
      <strong>{provider ? PROVIDER_TITLE[error.kind] : "Replay failed"}.</strong> {error.message}
      {provider && error.info?.provider_status && (
        <div className="small" style={{ marginTop: 6 }}>
          Provider status: {error.info.provider_status}
          {error.info.attempts ? ` · attempts: ${error.info.attempts}` : ""}
          {error.info.quota_id ? ` · quota: ${error.info.quota_id} (limit ${error.info.quota_limit})` : ""}
          {error.info.retry_after_seconds != null ? ` · retry after ${Math.round(error.info.retry_after_seconds)} s` : ""}
        </div>
      )}
    </div>
  );
}

function Side({ title, subtitle, result, error, loading }) {
  return (
    <section className="card" aria-label={title}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "baseline" }}>
        <h2>{title}</h2>
        {error && <span className="badge badge-warn">not evaluated</span>}
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
      {!loading && error && <Failed error={error} />}
      {!loading && !result && !error && <div className="state">Pick an attack and press Replay.</div>}
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
  const [failures, setFailures] = useState({});
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
    setFailures({});
    // One side after the other (not in parallel) so a replay never doubles the request rate against Gemini's
    // per-project limits. Each side succeeds or fails on its own; a failure is never shown as a result.
    const ok = {}, failed = {};
    for (const mode of ["baseline", "voicegate"]) {
      const quotaHit = Object.values(failed).find((e) => e.kind === "provider_quota");
      if (quotaHit) {
        // The quota is per project: the other side would fail the same way, so don't spend another request.
        failed[mode] = Object.assign(
          new ApiError(quotaHit.status, "Skipped: the model quota was already exhausted for the other side. " + quotaHit.message,
                       quotaHit.kind, quotaHit.info),
          { skipped: true },
        );
        continue;
      }
      try {
        ok[mode] = await api("/api/simulate", { method: "POST", body: { caller_tier: "public", mode, attack_id: attackId } });
      } catch (e) {
        failed[mode] = e;
      }
    }
    setResults(ok);
    setFailures(failed);
    setLoading(false);
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
        {(results.voicegate || results.baseline) && (
          <p className="small" style={{ marginBottom: 0 }}>
            Model: <strong>{(results.voicegate || results.baseline).brain}</strong>
          </p>
        )}
        {error && <p className="error-text" role="alert">{error}</p>}
      </section>

      <div className="grid grid-2" style={{ marginTop: 16, alignItems: "start" }}>
        <Side title="Baseline" subtitle="Raw tool access, security rules in the prompt only." result={results.baseline} error={failures.baseline} loading={loading} />
        <Side title="VoiceGate" subtitle="Trust Gate: tier check, authorization, field allowlist, audit." result={results.voicegate} error={failures.voicegate} loading={loading} />
      </div>
    </>
  );
}
