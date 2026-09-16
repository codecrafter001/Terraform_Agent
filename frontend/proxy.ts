import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

// Injects the backend's shared API key server-side on every browser request
// proxied to /api/* (see next.config.js's rewrite to BACKEND_API_INTERNAL_URL).
// TERRAAGENT_API_KEY is deliberately NOT prefixed with NEXT_PUBLIC_ - that
// would bundle it into client-side JS, defeating the point. This runs in
// Next's own server (Proxy defaults to the Node.js runtime), so the browser
// never needs to know or set this header itself - covers plain fetch() calls,
// the native EventSource used for SSE log streaming (which cannot set custom
// headers at all), and plain <a href> download navigations alike, since all
// three are just HTTP requests to this same server before backend/services/auth.py
// ever sees them.
export function proxy(request: NextRequest) {
  const apiKey = process.env.TERRAAGENT_API_KEY;
  if (!apiKey) {
    return NextResponse.next();
  }
  const headers = new Headers(request.headers);
  headers.set("x-api-key", apiKey);
  return NextResponse.next({ request: { headers } });
}

export const config = {
  matcher: "/api/:path*",
};
