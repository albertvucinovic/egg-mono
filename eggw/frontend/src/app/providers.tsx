"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { FormEvent, useEffect, useState } from "react";
import { verifyApiToken } from "@/lib/api";
import {
  initializeApiToken,
  setSessionApiToken,
  subscribeApiToken,
} from "@/lib/apiToken";
import { Button } from "@/components/ui/primitives";

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
      <main className="eggw-gate-shell"><div className="eggw-gate-state" role="status"><h1>Connecting to EggW</h1><p>Checking this browser session…</p></div></main>
    );
  }

  if (!authenticated) {
    return (
      <main className="eggw-gate-shell">
        <form onSubmit={submit} className="eggw-auth-form">
          <div>
            <h1 className="text-lg font-semibold">Connect to EggW</h1>
            <p className="eggw-ui-muted mt-1 text-sm">
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
              className="eggw-form-control px-3 py-2 font-mono"
              data-testid="api-token-input"
            />
          </label>
          {error && <p className="eggw-form-error" role="alert">{error}</p>}
          <Button
            variant="primary"
            type="submit"
            disabled={checking}
            className="w-full"
          >
            {checking ? "Checking…" : "Connect"}
          </Button>
        </form>
      </main>
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
