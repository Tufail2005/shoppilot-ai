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