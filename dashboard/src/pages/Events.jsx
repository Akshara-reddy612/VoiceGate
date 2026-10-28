import { useEffect, useState } from "react";
import { api } from "../api.js";

export default function Events() {
  const [state, setState] = useState({ loading: true, events: [] });
  const [blockedOnly, setBlockedOnly] = useState(false);

  useEffect(() => {
    let live = true;
    const load = () =>
      api("/api/events?limit=200")
        .then((events) => live && setState({ events }))
        .catch((e) => live && setState((s) => ({ ...s, loading: false, error: e.message })));
    load();
    const t = setInterval(load, 3000);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, []);

  const shown = blockedOnly ? state.events.filter((e) => e.blocked) : state.events;
  const blocked = state.events.filter((e) => e.blocked).length;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Live events</h1>
          <p>Every caller tool call through the Trust Gate. Arguments are stored only as a hash; phones are never logged.</p>
        </div>
        <div className="grid grid-4" style={{ minWidth: 300 }}>
          <div className="card stat">
            <div className="label">Events</div>
            <div className="value">{state.events.length}</div>
          </div>
          <div className="card stat">
            <div className="label">Blocked</div>
            <div className="value" style={{ color: blocked ? "var(--bad)" : undefined }}>{blocked}</div>
          </div>
        </div>
      </div>

      <section className="card" style={{ padding: 0 }} aria-live="polite">
        <div className="toolbar" style={{ padding: "12px 12px 0" }}>
          <label className="check">
            <input type="checkbox" checked={blockedOnly} onChange={(e) => setBlockedOnly(e.target.checked)} />
            Blocked only
          </label>
          <span className="muted small">Refreshes every 3 seconds</span>
        </div>
        {state.error && <div className="banner banner-bad" role="alert" style={{ margin: 12 }}>{state.error}</div>}
        {state.loading && !state.error && (
          <div style={{ padding: 12 }}>
            <div className="skeleton" />
            <div className="skeleton" style={{ width: "70%" }} />
          </div>
        )}
        {!state.loading && shown.length === 0 && (
          <div className="state">
            <strong>{blockedOnly ? "No blocked attempts yet" : "No events yet"}</strong>
            Try an attack on the Caller test page, e.g. <code>/call list_bookings {"{}"}</code>.
          </div>
        )}
        {shown.map((e, i) => (
          <div key={`${e.ts}-${i}`} className={`event${e.blocked ? " blocked" : ""}`}>
            <span className="mono muted">{e.ts.slice(11, 19)}</span>
            <span>
              <span className={`badge ${e.tier === "public" ? "" : "badge-warn"}`}>{e.tier}</span>
            </span>
            <span>
              <span className="mono">{e.tool}</span>{" "}
              {e.blocked ? <span className="badge badge-bad">blocked</span> : <span className="badge badge-ok">allowed</span>}
              {e.reason && <div className="reason">{e.reason}</div>}
            </span>
          </div>
        ))}
      </section>
    </>
  );
}
