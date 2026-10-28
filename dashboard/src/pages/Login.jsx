import { useState } from "react";
import { api, setToken } from "../api.js";

export default function Login({ health, onLogin }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const { token } = await api("/api/login", { method: "POST", body: { password } });
      setToken(token);
      onLogin();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <div className="card">
        <div className="brand" style={{ padding: 0 }}>
          <span className="brand-mark" aria-hidden="true">VG</span>
          <h1>VoiceGate owner</h1>
        </div>
        <p className="muted small" style={{ margin: "8px 0 0" }}>
          Bookings, security events and evaluation results. Owner access is dashboard-only, never by voice.
        </p>
        {health && !health.login_configured && (
          <p className="banner banner-warn" role="alert" style={{ marginTop: 16 }}>
            Login is disabled until OWNER_PASSWORD is set in .env.
          </p>
        )}
        <form onSubmit={submit}>
          <label className="field">
            Password
            <input
              className="input"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              autoFocus
            />
          </label>
          {error && <p className="error-text" role="alert">{error}</p>}
          <button className="btn btn-primary" disabled={busy || !password}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
