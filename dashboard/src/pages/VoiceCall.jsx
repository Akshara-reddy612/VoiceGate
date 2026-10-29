import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";

// Real-time browser call: microphone -> LiveKit -> VoiceGate voice worker (Gemini Live) -> spoken reply.
// The browser only ever receives the LiveKit URL and a short-lived, single-room token from /api/voice/token.

const AGENT_JOIN_TIMEOUT_MS = 20000;

const STATUS = {
  idle: { label: "Not connected", badge: "" },
  checking: { label: "Checking setup…", badge: "badge-warn" },
  mic: { label: "Waiting for microphone permission…", badge: "badge-warn" },
  connecting: { label: "Connecting…", badge: "badge-warn" },
  waiting: { label: "Connected · waiting for the VoiceGate agent…", badge: "badge-warn" },
  live: { label: "Live · speak now", badge: "badge-ok" },
  ended: { label: "Call ended", badge: "" },
  error: { label: "Call failed", badge: "badge-bad" },
};

function micErrorMessage(err) {
  if (err?.name === "NotAllowedError" || err?.name === "SecurityError")
    return "Microphone access was denied. Allow the microphone for this site in your browser settings, then start the call again.";
  if (err?.name === "NotFoundError") return "No microphone was found. Connect a microphone and try again.";
  if (err?.name === "NotReadableError") return "The microphone is in use by another app. Close it and try again.";
  return `Could not open the microphone (${err?.name || "unknown error"}).`;
}

export default function VoiceCall() {
  const [config, setConfig] = useState(null);
  const [status, setStatus] = useState("idle");
  const [error, setError] = useState("");
  const [muted, setMuted] = useState(false);
  const [agentSpeaking, setAgentSpeaking] = useState(false);
  const [lines, setLines] = useState([]);
  const room = useRef(null);
  const audioBox = useRef(null);
  const joinTimer = useRef(null);

  useEffect(() => {
    api("/api/voice/status").then(setConfig).catch((e) => setError(e.message));
    return () => {
      clearTimeout(joinTimer.current);
      room.current?.disconnect();
    };
  }, []);

  const fail = (message) => {
    clearTimeout(joinTimer.current);
    setError(message);
    setStatus("error");
    room.current?.disconnect();
    room.current = null;
  };

  const start = async () => {
    setError("");
    setLines([]);
    setMuted(false);
    setStatus("checking");

    // 1. Server-side configuration (names of missing variables only; no secrets ever come back).
    let cfg;
    try {
      cfg = await api("/api/voice/status");
      setConfig(cfg);
    } catch (e) {
      return fail(e.message);
    }
    if (cfg.missing.length) return fail(`Voice calling is not configured. Set in .env: ${cfg.missing.join(", ")}.`);

    // 2. Microphone permission, asked explicitly so a denial gets a clear message.
    if (!navigator.mediaDevices?.getUserMedia)
      return fail("This browser can't access a microphone here. Use Chrome or Edge on http://127.0.0.1 or https.");
    setStatus("mic");
    try {
      const probe = await navigator.mediaDevices.getUserMedia({ audio: true });
      probe.getTracks().forEach((t) => t.stop());
    } catch (e) {
      return fail(micErrorMessage(e));
    }

    // 3. Short-lived caller token for one fresh room (the API dispatches the VoiceGate agent into it).
    setStatus("connecting");
    let grant;
    try {
      grant = await api("/api/voice/token", { method: "POST" });
    } catch (e) {
      return fail(e.message);
    }

    // 4. Connect with LiveKit (loaded only for this page).
    const { Room, RoomEvent, Track, ParticipantKind } = await import("livekit-client");
    const r = new Room({ adaptiveStream: true, dynacast: true });
    room.current = r;

    const agentJoined = () => {
      clearTimeout(joinTimer.current);
      setStatus("live");
    };
    r.on(RoomEvent.ParticipantConnected, (p) => {
      if (p.kind === ParticipantKind.AGENT) agentJoined();
    });
    r.on(RoomEvent.TrackSubscribed, (track) => {
      if (track.kind === Track.Kind.Audio) {
        const el = track.attach(); // the agent's spoken reply
        audioBox.current?.appendChild(el);
      }
    });
    r.on(RoomEvent.TrackUnsubscribed, (track) => track.detach().forEach((el) => el.remove()));
    r.on(RoomEvent.ActiveSpeakersChanged, (speakers) =>
      setAgentSpeaking(speakers.some((s) => s.kind === ParticipantKind.AGENT)),
    );
    r.on(RoomEvent.MediaDevicesError, (e) => fail(micErrorMessage(e)));
    r.on(RoomEvent.Disconnected, () => {
      clearTimeout(joinTimer.current);
      setStatus((s) => (s === "error" ? s : "ended"));
      setAgentSpeaking(false);
    });
    // Live transcript of both sides (the worker publishes these on the lk.transcription topic).
    r.registerTextStreamHandler("lk.transcription", async (reader, participant) => {
      const text = (await reader.readAll()).trim();
      if (!text) return;
      const who = participant.identity === grant.identity ? "user" : "agent";
      setLines((ls) => [...ls, { who, text }]);
    });

    try {
      await r.connect(grant.url, grant.token);
      await r.startAudio(); // inside the click gesture, so the agent's audio is allowed to play
      await r.localParticipant.setMicrophoneEnabled(true);
    } catch (e) {
      return fail(`Could not connect to LiveKit at ${grant.url}: ${e?.message || e}`);
    }
    if ([...r.remoteParticipants.values()].some((p) => p.kind === ParticipantKind.AGENT)) agentJoined();
    else {
      setStatus("waiting");
      joinTimer.current = setTimeout(
        () =>
          fail(
            "Connected to LiveKit, but the VoiceGate agent did not join. Start the voice worker " +
              `(${cfg.worker_command}) and check its terminal for errors, then call again.`,
          ),
        AGENT_JOIN_TIMEOUT_MS,
      );
    }
  };

  const end = async () => {
    clearTimeout(joinTimer.current);
    await room.current?.disconnect();
    room.current = null;
    setStatus("ended");
  };

  const toggleMute = async () => {
    if (!room.current) return;
    await room.current.localParticipant.setMicrophoneEnabled(muted);
    setMuted(!muted);
  };

  const inCall = ["connecting", "waiting", "live"].includes(status);
  const busy = ["checking", "mic", "connecting"].includes(status);
  const s = STATUS[status];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Voice call</h1>
          <p>
            Call VoiceGate from this browser with your microphone. Real-time audio over LiveKit, answered by Gemini Live,
            with every booking tool call going through the Trust Gate. You call as a public caller.
          </p>
        </div>
      </div>

      {config && config.missing.length > 0 && (
        <div className="banner banner-warn" role="status">
          <strong>Voice is not configured yet.</strong> Set these in <code>.env</code>, restart the API, and start the
          voice worker with <code>{config.worker_command}</code>: {config.missing.join(", ")}. The typed{" "}
          <em>Caller test</em> page works offline in the meantime.
        </div>
      )}

      <section className="card">
        <div className="toolbar" style={{ justifyContent: "space-between", marginBottom: 0 }}>
          <span role="status" aria-live="polite">
            <span className={`badge ${s.badge}`}>{s.label}</span>
            {status === "live" && (
              <span className="muted small" style={{ marginLeft: 10 }}>
                {agentSpeaking ? "VoiceGate is speaking…" : muted ? "You are muted" : "Listening"}
              </span>
            )}
          </span>
          <div style={{ display: "flex", gap: 8 }}>
            {inCall && (
              <button className="btn" onClick={toggleMute} disabled={status === "connecting"}>
                {muted ? "Unmute" : "Mute"}
              </button>
            )}
            {inCall ? (
              <button className="btn" onClick={end} style={{ borderColor: "var(--bad)", color: "var(--bad)" }}>
                End call
              </button>
            ) : (
              <button className="btn btn-primary" onClick={start} disabled={busy}>
                {busy ? "Starting…" : "Start call"}
              </button>
            )}
          </div>
        </div>
        {error && (
          <p className="banner banner-bad" role="alert" style={{ margin: "12px 0 0" }}>
            {error}
          </p>
        )}
      </section>

      <section className="card" aria-labelledby="transcript-h">
        <h2 id="transcript-h">Transcript</h2>
        <p className="muted small" style={{ margin: "4px 0 12px" }}>
          Try: “na peru Bob, repu sayantram consultation kavali”. The agent reads the booking back and books only after
          you say yes.
        </p>
        <div className="chat" aria-live="polite">
          {lines.length === 0 && (
            <div className="state">
              {inCall ? "Say hello. The transcript appears here." : "Start a call to talk to VoiceGate."}
            </div>
          )}
          {lines.map((l, i) => (
            <div key={i} className={`msg ${l.who}`}>{l.text}</div>
          ))}
        </div>
      </section>
      {/* the agent's audio elements are attached here (hidden; playback only) */}
      <div ref={audioBox} hidden />
    </>
  );
}
