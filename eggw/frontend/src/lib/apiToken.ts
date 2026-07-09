const SESSION_TOKEN_KEY = "eggw.apiToken";

type TokenListener = (token: string | null) => void;

let apiToken: string | null = null;
const listeners = new Set<TokenListener>();

function publish(token: string | null): void {
  apiToken = token;
  listeners.forEach((listener) => listener(token));
}

export function getApiToken(): string | null {
  return apiToken;
}

export function subscribeApiToken(listener: TokenListener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function setSessionApiToken(token: string): void {
  const value = token.trim();
  if (!value) throw new Error("API token is required");
  // Tab-scoped session storage deliberately avoids persistent browser storage,
  // cookies, URLs, and console output for this bearer capability.
  window.sessionStorage.setItem(SESSION_TOKEN_KEY, value);
  publish(value);
}

export function clearApiToken(): void {
  if (typeof window !== "undefined") {
    window.sessionStorage.removeItem(SESSION_TOKEN_KEY);
  }
  publish(null);
}

export async function initializeApiToken(): Promise<boolean> {
  const sessionToken = window.sessionStorage.getItem(SESSION_TOKEN_KEY)?.trim();
  if (sessionToken) {
    publish(sessionToken);
    return true;
  }

  // The launcher exposes this runtime endpoint only for its loopback-only mode.
  // Public/manual deployments receive 404 and require user-entered session state.
  try {
    const response = await fetch("/api/eggw-bootstrap", {
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) return false;
    const payload = await response.json();
    const token = typeof payload?.token === "string" ? payload.token.trim() : "";
    if (!token) return false;
    publish(token); // Private bootstrap remains memory-only.
    return true;
  } catch {
    return false;
  }
}
