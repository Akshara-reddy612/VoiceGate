// Thin client for the VoiceGate API. The bearer token lives in sessionStorage (cleared when the tab closes).

const KEY = "voicegate.token";

export function getToken() {
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function setToken(token) {
  try {
    if (token) sessionStorage.setItem(KEY, token);
    else sessionStorage.removeItem(KEY);
  } catch {
    /* storage unavailable: token lives only in memory for this page */
  }
}

export class ApiError extends Error {
  constructor(status, message, kind = null, info = {}) {
    super(message);
    this.status = status;
    this.kind = kind; // "provider_unavailable" | "provider_timeout" | "provider_error" for model-provider failures
    this.info = info;
  }
}

export const isProviderError = (e) => Boolean(e?.kind?.startsWith("provider_"));
export const PROVIDER_TITLE = {
  provider_unavailable: "Model provider unavailable",
  provider_quota: "Model quota exhausted",
  provider_timeout: "Model provider timed out",
  provider_error: "Model provider error",
};

export async function api(path, { method = "GET", body } = {}) {
  const headers = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let res;
  try {
    res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  } catch {
    throw new ApiError(0, "Can't reach the VoiceGate API. Is the server running on port 8000?");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 401 && path !== "/api/login") window.dispatchEvent(new Event("voicegate:logout"));
    const d = data.detail;
    if (d && typeof d === "object" && d.message) {
      const { kind, message, ...info } = d;
      throw new ApiError(res.status, message, kind || null, info);
    }
    throw new ApiError(res.status, typeof d === "string" ? d : `Request failed (${res.status})`);
  }
  return data;
}

export const fmtTime = (iso) => iso.slice(11, 16);
export const fmtDate = (iso) =>
  new Date(iso.slice(0, 10) + "T00:00:00").toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
export const todayIso = () => {
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
};
