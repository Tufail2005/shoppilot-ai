import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

import { getSupabaseClientConfig } from "@/lib/supabase/env";

export async function proxy(request: NextRequest) {
  const { url, anonKey } = getSupabaseClientConfig();

  let supabaseResponse = NextResponse.next({
    request,
  });

  const supabase = createServerClient(url, anonKey, {
    cookies: {
      getAll() {
        return request.cookies.getAll();
      },
      setAll(cookiesToSet) {
        for (const { name, value } of cookiesToSet) {
          request.cookies.set(name, value);
        }
        supabaseResponse = NextResponse.next({
          request,
        });
        for (const { name, value, options } of cookiesToSet) {
          supabaseResponse.cookies.set(name, value, options);
        }
      },
    },
  });

  // Do not run code between createServerClient and auth.getUser().
  // A simple mistake can make the session appear logged in for a stale JWT.
  const {
    data: { user },
  } = await supabase.auth.getUser();

  // Phase 3.7 will add role/dashboard redirects here.
  // This middleware currently only refreshes and propagates the session.
  void user;

  return supabaseResponse;
}

export const config = {
  matcher: [
    /*
     * Run on all request paths except:
     * - _next/static (static files)
     * - _next/image (image optimization files)
     * - favicon.ico and other static assets
     */
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)",
  ],
};
