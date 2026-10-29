import { useEffect, useState } from "react";
import { api, getToken, setToken } from "./api.js";
import Login from "./pages/Login.jsx";
import Bookings from "./pages/Bookings.jsx";
import Events from "./pages/Events.jsx";
import Metrics from "./pages/Metrics.jsx";
import CallerTest from "./pages/CallerTest.jsx";
import VoiceCall from "./pages/VoiceCall.jsx";
import Compare from "./pages/Compare.jsx";

const PAGES = [
  { id: "bookings", label: "Bookings", Component: Bookings },
  { id: "events", label: "Live events", Component: Events },
  { id: "metrics", label: "Metrics", Component: Metrics },
  { id: "voice", label: "Voice call", Component: VoiceCall },
  { id: "caller", label: "Caller test (offline)", Component: CallerTest },
  { id: "compare", label: "Baseline vs VoiceGate", Component: Compare },
];

function HealthBanner({ health }) {
  if (!health) return null;
  if (health.gemini_configured) return null;
  return (
    <div className="banner banner-warn" role="status">
      <strong>Offline mode.</strong> GOOGLE_API_KEY is not set, so the caller test uses the offline rule brain and
      attack replays use the worst-case mock model (not an LLM). Live voice is{" "}
      {health.livekit_configured ? "configured but untested here" : "not configured"}.
    </div>
  );
}

export default function App() {
  const [authed, setAuthed] = useState(Boolean(getToken()));
  const [page, setPage] = useState("bookings");
  const [health, setHealth] = useState(null);

  useEffect(() => {
    const onLogout = () => {
      setToken(null);
      setAuthed(false);
    };
    window.addEventListener("voicegate:logout", onLogout);
    return () => window.removeEventListener("voicegate:logout", onLogout);
  }, []);

  useEffect(() => {
    api("/api/health").then(setHealth).catch(() => setHealth(null));
  }, []);

  if (!authed) return <Login health={health} onLogin={() => setAuthed(true)} />;

  const { Component } = PAGES.find((p) => p.id === page);
  const logout = async () => {
    await api("/api/logout", { method: "POST" }).catch(() => {});
    setToken(null);
    setAuthed(false);
  };

  return (
    <div className="shell">
      <nav className="side" aria-label="Dashboard sections">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">VG</span>
          VoiceGate
        </div>
        {PAGES.map((p) => (
          <button
            key={p.id}
            className="nav-btn"
            aria-current={page === p.id ? "page" : undefined}
            onClick={() => setPage(p.id)}
          >
            {p.label}
          </button>
        ))}
        <div className="side-foot">
          <button className="btn" onClick={logout}>Log out</button>
        </div>
      </nav>
      <main className="main">
        <HealthBanner health={health} />
        <Component health={health} />
      </main>
    </div>
  );
}
