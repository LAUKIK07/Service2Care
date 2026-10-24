// Connection handling. There is no login page: opening the link the assistant texts you stores the key
// in this browser, and the key is removed from the address bar straight away.

const KEY = "s2c.conn";

export class AuthError extends Error {}

function safeApi(url) {
  try {
    const u = new URL(url);
    const local = ["localhost", "127.0.0.1"].includes(u.hostname);
    if (u.protocol === "https:" || (u.protocol === "http:" && local)) return u.origin;
  } catch { /* fall through */ }
  return null;
}

export function loadConn() {
  const h = new URLSearchParams(location.hash.slice(1));
  if (h.get("api") && h.get("k")) {
    const api = safeApi(h.get("api"));
    if (api) {
      const conn = { api, key: h.get("k") };
      try { localStorage.setItem(KEY, JSON.stringify(conn)); } catch { /* private mode */ }
      history.replaceState(null, "", location.pathname + location.search);
      return conn;
    }
  }
  try {
    const conn = JSON.parse(localStorage.getItem(KEY) || "null");
    if (conn && safeApi(conn.api) && conn.key) return conn;
  } catch { /* ignore */ }
  return null;
}

export function forgetConn() {
  try { localStorage.removeItem(KEY); } catch { /* ignore */ }
}

export async function call(conn, path, { method = "GET", body } = {}) {
  const res = await fetch(conn.api + path, {
    method,
    headers: { Authorization: `Bearer ${conn.key}`, ...(body ? { "content-type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  if (res.status === 401) throw new AuthError("unauthorized");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
