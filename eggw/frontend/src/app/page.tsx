"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { fetchRootThreads, createThread } from "@/lib/api";

export default function Home() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const initAndRedirect = async () => {
      try {
        const roots = await fetchRootThreads();
        if (roots && roots.length > 0) {
          // Redirect to most recent thread
          const latest = roots[roots.length - 1];
          router.replace(`/${latest.id}`);
        } else {
          // No threads exist - create one and redirect
          const thread = await createThread({});
          router.replace(`/${thread.id}`);
        }
      } catch (err) {
        console.error("Failed to initialize:", err);
        setError("Failed to connect to server. Is the backend running?");
      }
    };

    initAndRedirect();
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
