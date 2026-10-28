import { useCallback, useEffect, useMemo, useState } from "react";
import { api, fmtDate, fmtTime, todayIso } from "../api.js";

const STATUS_BADGE = { confirmed: "badge-ok", cancelled: "badge-bad" };

function Availability({ date }) {
  const [state, setState] = useState({ loading: true });

  useEffect(() => {
    let live = true;
    setState({ loading: true });
    api(`/api/availability?date=${date}`)
      .then((r) => live && setState({ slots: r.slots }))
      .catch((e) => live && setState({ error: e.message }));
    return () => {
      live = false;
    };
  }, [date]);

  return (
    <section className="card" aria-labelledby="avail-h">
      <h2 id="avail-h">Free slots · {fmtDate(date)}</h2>
      <p className="muted small" style={{ margin: "4px 0 12px" }}>
        30-minute starts. This is what a public caller is allowed to see.
      </p>
      {state.loading && <div className="skeleton" style={{ width: "60%" }} />}
      {state.error && <p className="muted">{state.error}</p>}
      {state.slots && state.slots.length === 0 && <p className="muted">Fully booked or no remaining slots today.</p>}
      {state.slots && state.slots.length > 0 && (
        <div className="chips">
          {state.slots.map((s) => (
            <span className="chip" key={s.start}>{s.start}</span>
          ))}
        </div>
      )}
    </section>
  );
}

export default function Bookings() {
  const [date, setDate] = useState(todayIso());
  const [allDates, setAllDates] = useState(true);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [state, setState] = useState({ loading: true, rows: [] });

  const load = useCallback(async () => {
    try {
      const qs = new URLSearchParams();
      if (!allDates) qs.set("date", date);
      if (status) qs.set("status", status);
      const rows = await api(`/api/bookings?${qs}`);
      setState({ rows });
    } catch (e) {
      setState((s) => ({ ...s, loading: false, error: e.message }));
    }
  }, [allDates, date, status]);

  useEffect(() => {
    setState((s) => ({ ...s, loading: true }));
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, [load]);

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return state.rows;
    return state.rows.filter((b) =>
      [b.customer, b.code, b.purpose, b.id].some((v) => String(v).toLowerCase().includes(q)),
    );
  }, [state.rows, query]);

  const confirmed = state.rows.filter((b) => b.status === "confirmed").length;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Bookings</h1>
          <p>Live from the bookings database, read through the Trust Gate as the owner tier.</p>
        </div>
        <div className="grid grid-4" style={{ minWidth: 300 }}>
          <div className="card stat">
            <div className="label">Shown</div>
            <div className="value">{state.rows.length}</div>
          </div>
          <div className="card stat">
            <div className="label">Confirmed</div>
            <div className="value">{confirmed}</div>
          </div>
        </div>
      </div>

      <div className="grid grid-2" style={{ gridTemplateColumns: "minmax(0, 2fr) minmax(260px, 1fr)", alignItems: "start" }}>
        <section className="card" aria-labelledby="bk-h">
          <h2 id="bk-h" className="sr-only" style={{ position: "absolute", left: -9999 }}>Booking list</h2>
          <div className="toolbar">
            <input
              className="input"
              type="search"
              placeholder="Search name, code or service"
              aria-label="Search bookings"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              style={{ flex: "1 1 200px" }}
            />
            <select className="select" aria-label="Filter by status" value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">All statuses</option>
              <option value="confirmed">Confirmed</option>
              <option value="cancelled">Cancelled</option>
            </select>
            <input
              className="input"
              type="date"
              aria-label="Date"
              value={date}
              onChange={(e) => {
                setDate(e.target.value || todayIso());
                setAllDates(false);
              }}
            />
            <label className="check">
              <input type="checkbox" checked={allDates} onChange={(e) => setAllDates(e.target.checked)} />
              All dates
            </label>
          </div>

          {state.error && <div className="banner banner-bad" role="alert">{state.error}</div>}
          {state.loading && state.rows.length === 0 && !state.error && (
            <div aria-busy="true">
              <div className="skeleton" />
              <div className="skeleton" style={{ width: "80%" }} />
              <div className="skeleton" style={{ width: "65%" }} />
            </div>
          )}
          {!state.loading && rows.length === 0 && !state.error && (
            <div className="state">
              <strong>{query || status ? "No bookings match these filters" : "No bookings yet"}</strong>
              {query || status
                ? "Try clearing the search or status filter."
                : "Make one from the Caller test page, or by voice once LiveKit is configured."}
            </div>
          )}
          {rows.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">When</th>
                    <th scope="col">Customer</th>
                    <th scope="col">Service</th>
                    <th scope="col">Status</th>
                    <th scope="col">Code</th>
                    <th scope="col">Booking ID</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((b) => (
                    <tr key={b.id}>
                      <td className="num">
                        {fmtDate(b.start)} · {fmtTime(b.start)}–{fmtTime(b.end)}
                      </td>
                      <td>{b.customer}</td>
                      <td>{b.purpose}</td>
                      <td>
                        <span className={`badge ${STATUS_BADGE[b.status] || ""}`}>{b.status}</span>
                      </td>
                      <td className="mono">{b.code}</td>
                      <td className="mono muted">{b.id}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
        <Availability date={date} />
      </div>
    </>
  );
}
