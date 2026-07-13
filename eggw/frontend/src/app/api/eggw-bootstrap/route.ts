import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

/**
 * Runtime-only provisioning for the loopback launcher.
 *
 * eggw.sh sets this server-side variable only when both servers bind loopback.
 * Public/manual deployments never expose a token through this route.
 */
export async function GET() {
  const token = process.env.EGGW_PRIVATE_BOOTSTRAP_TOKEN || "";
  // eggw.sh supplies this server-only value only in private mode and binds the
  // entire Next server explicitly to loopback. Do not authorize from forwarded
  // headers: clients can spoof them unless a trusted proxy strips them.
  if (!token) {
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
