import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";

import { getSupabaseClientConfig } from "./env";

export async function createClient() {
  const { url, anonKey } = getSupabaseClientConfig();
  const cookieStore = await cookies();

  return createServerClient(url, anonKey, {
    cookies: {
      getAll() {
        return cookieStore.getAll();
      },
      setAll(cookiesToSet) {
        try {
          for (const { name, value, options } of cookiesToSet) {
            cookieStore.set(name, value, options);
          }
        } catch {
          // Server Components cannot write cookies.
          // Session refresh is handled by middleware.
        }
      },
    },
  });
}
