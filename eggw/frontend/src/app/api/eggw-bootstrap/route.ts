import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";

/**
 * Runtime-only provisioning for the loopback launcher.
 *
 * eggw.sh sets this server-side variable only when both servers bind loopback.
 * Public/manual deployments never expose a token through this route.
 */
export async function GET(request: NextRequest) {
  const token = process.env.EGGW_PRIVATE_BOOTSTRAP_TOKEN || "";
  // Defense in depth: private bootstrap is launcher-only and local clients
  // only. Forwarded addresses are intentionally not trusted here.
  const remoteAddress = request.ip || "";
  const localClient = remoteAddress === "127.0.0.1" || remoteAddress === "::1" || remoteAddress === "::ffff:127.0.0.1";
  if (!token || !localClient) {
    return NextResponse.json(
      { detail: "Private token bootstrap is disabled" },
      { status: 404, headers: { "Cache-Control": "no-store" } },
    );
  }
  return NextResponse.json(
    { token },
    {
      headers: {
        "Cache-Control": "no-store, private",
        Pragma: "no-cache",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
        "X-Content-Type-Options": "nosniff",
      },
    },
  );
}
