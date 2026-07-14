"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { createThread } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import { applyQuickStartThread } from "@/lib/quickStart";

export default function Home() {
  const router = useRouter();
  const didInitialize = useRef(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (didInitialize.current) return;
    didInitialize.current = true;
    void createThread({ claim_quick_start: true })
      .then((thread) => {
        applyQuickStartThread(thread, useAppStore.getState());
        router.replace(`/${thread.id}`);
      })
      .catch((reason) => {
        console.error("Failed to create startup thread:", reason);
        setError("Failed to create a new thread. Is the backend running?");
      });
  }, [router]);

  return (
    <main className="eggw-gate-shell">
      <div className={error ? "eggw-gate-state eggw-gate-error" : "eggw-gate-state"} role={error ? "alert" : "status"}>
        <h1>{error ? "Could not start EggW" : "Starting EggW"}</h1>
        <p>{error || "Creating a conversation thread…"}</p>
      </div>
    </main>
  );
}
