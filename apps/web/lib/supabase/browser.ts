import { createBrowserClient } from "@supabase/ssr";

import { getSupabaseClientConfig } from "./env";

export function createClient() {
  const { url, anonKey } = getSupabaseClientConfig();

  return createBrowserClient(url, anonKey);
}
