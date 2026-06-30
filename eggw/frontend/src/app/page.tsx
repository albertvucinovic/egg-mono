"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { createThread } from "@/lib/api";

export default function Home() {
  const router = useRouter();
  const didInitialize = useRef(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (didInitialize.current) return;
    didInitialize.current = true;

    const createAndRedirect = async () => {
      try {
        const thread = await createThread({});
        router.replace(`/${thread.id}`);
      } catch (err) {
        console.error("Failed to create startup thread:", err);
        setError("Failed to create a new thread. Is the backend running?");
      }
    };

    createAndRedirect();
  }, [router]);

  if (error) {
    return (
      <div className="h-screen flex items-center justify-center" style={{ background: "var(--background)", color: "var(--foreground)" }}>
        <div className="text-center">
          <div className="text-lg mb-2">Error</div>
          <div className="text-sm" style={{ color: "var(--muted)" }}>{error}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="h-screen flex items-center justify-center" style={{ background: "var(--background)", color: "var(--foreground)" }}>
      <div className="text-lg">Loading...</div>
    </div>
  );
}
