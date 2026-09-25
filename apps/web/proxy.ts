import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

import { getSupabaseServerConfig } from "@/lib/supabase/env";

export async function proxy(request: NextRequest) {
  const { url, anonKey } = getSupabaseServerConfig();

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

  const {
    data: { user },
  } = await supabase.auth.getUser();

const isDashboardRoute = request.nextUrl.pathname.startsWith("/dashboard");

 if (isDashboardRoute && !user) {
     const loginUrl = request.nextUrl.clone(); 
     loginUrl.pathname = "/login";
     loginUrl.searchParams.set("redirectTo", request.nextUrl.pathname);
    
    return NextResponse.redirect(loginUrl);
  
  }
  
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
