# Frontend Rules

This directory contains the ShopPilot AI Next.js frontend.

Use:
- Next.js App Router
- TypeScript
- React Server Components by default

Use Client Components only when required for:
- state
- effects
- browser APIs
- event handlers
- interactive UI

API integration belongs in:
apps/web/lib/

Never expose:
- DATABASE_URL
- SUPABASE_SERVICE_ROLE_KEY
- GEMINI_API_KEY

NEXT_PUBLIC_* values are browser-visible.

Backend authorization is authoritative.
Frontend guards are only UX.

After frontend changes run:

npm run lint
npm run build

<!-- BEGIN:nextjs-agent-rules -->

# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->
