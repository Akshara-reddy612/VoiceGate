// One row per tool call: the gate's decision, and exactly what the model received.
export default function ToolTrace({ calls }) {
  if (!calls?.length) return null;
  return (
    <div className="trace">
      {calls.map((c, i) => (
        <details key={i} className={`call${c.blocked ? " blocked" : ""}`}>
          <summary>
            <span className="mono">{c.tool}</span>
            {c.blocked ? <span className="badge badge-bad">blocked</span> : <span className="badge badge-ok">allowed</span>}
            {c.reason && <span className="muted small">{c.reason}</span>}
          </summary>
          <div className="small muted" style={{ marginTop: 6 }}>Arguments (redacted)</div>
          <pre className="json">{JSON.stringify(c.args, null, 2)}</pre>
          <div className="small muted" style={{ marginTop: 6 }}>What the model received</div>
          <pre className="json">{JSON.stringify(c.result, null, 2)}</pre>
        </details>
      ))}
    </div>
  );
}
