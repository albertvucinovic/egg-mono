"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { FormEvent, useEffect, useState } from "react";
import { verifyApiToken } from "@/lib/api";
import {
  initializeApiToken,
  setSessionApiToken,
  subscribeApiToken,
} from "@/lib/apiToken";

function ApiTokenGate({ children }: { children: React.ReactNode }) {
  const [initialized, setInitialized] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);
  const [candidate, setCandidate] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  useEffect(() => {
    let active = true;
    const unsubscribe = subscribeApiToken((token) => {
      if (active) setAuthenticated(Boolean(token));
    });
    void initializeApiToken().then((available) => {
      if (!active) return;
      setAuthenticated(available);
      setInitialized(true);
    });
    return () => {
      active = false;
      unsubscribe();
    };
  }, []);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const token = candidate.trim();
    if (token.length < 32) {
      setError("Enter the API token configured on the EggW server.");
      return;
    }
    setChecking(true);
    setError(null);
    try {
      if (!(await verifyApiToken(token))) {
        setError("The API token was rejected.");
        return;
      }
      setSessionApiToken(token);
      setCandidate("");
    } catch {
      setError("Could not reach the EggW API.");
    } finally {
      setChecking(false);
    }
  };

  if (!initialized) {
    return (
      <div className="flex h-screen items-center justify-center" style={{ background: "var(--background)", color: "var(--foreground)" }}>
        <div className="text-sm" style={{ color: "var(--muted)" }}>Connecting to EggW…</div>
      </div>
    );
  }

  if (!authenticated) {
    return (
      <div className="flex h-screen items-center justify-center px-4" style={{ background: "var(--background)", color: "var(--foreground)" }}>
        <form onSubmit={submit} className="w-full max-w-md space-y-4 rounded border p-6" style={{ background: "var(--panel-bg)", borderColor: "var(--panel-border)" }}>
          <div>
            <h1 className="text-lg font-semibold">Connect to EggW</h1>
            <p className="mt-1 text-sm" style={{ color: "var(--muted)" }}>
              Enter the API token configured by the server operator. It is kept only in this browser tab&apos;s session storage.
            </p>
          </div>
          <label className="block space-y-1 text-sm">
            <span>API token</span>
            <input
              type="password"
              value={candidate}
              onChange={(event) => setCandidate(event.target.value)}
              autoComplete="off"
              autoFocus
              className="w-full rounded border px-3 py-2 font-mono"
              style={{ background: "var(--background)", borderColor: "var(--panel-border)" }}
              data-testid="api-token-input"
            />
          </label>
          {error && <p className="text-sm text-red-400" role="alert">{error}</p>}
          <button
            type="submit"
            disabled={checking}
            className="w-full rounded px-3 py-2 text-sm font-medium disabled:opacity-50"
            style={{ background: "var(--accent)", color: "white" }}
          >
            {checking ? "Checking…" : "Connect"}
          </button>
        </form>
      </div>
    );
  }

  return children;
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 5000,
            refetchOnWindowFocus: false,
          },
        },
      })
  );

  return (
    <QueryClientProvider client={queryClient}>
      <ApiTokenGate>{children}</ApiTokenGate>
    </QueryClientProvider>
  );
}
