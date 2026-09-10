---
name: nextjs-frontend
slug: nextjs-frontend
description: Build and review the ShopPilot AI Next.js App Router frontend with correct Server/Client Component boundaries, API integration, state handling, and frontend security.
---

# Next.js Frontend Skill

Use this skill when changing `apps/web` or browser-facing behavior.

## Component model

Default to React Server Components.

Use a Client Component only when the component needs:

- browser APIs
- event handlers
- client state
- effects
- interactive widgets

Keep client components small and leaf-oriented where practical.

## API integration

Centralize backend API access under `apps/web/lib` or the existing API client location.

Use:

- `NEXT_PUBLIC_API_URL` for browser-visible requests
- an internal Docker service URL such as `http://api:8000` for server-side container-to-container requests

Never assume `localhost` means the API container. Inside Docker, `localhost` means the current container.

Do not put `SUPABASE_SERVICE_ROLE_KEY`, `GEMINI_API_KEY`, or database credentials in `NEXT_PUBLIC_*` variables or client code.

## Data loading

For each data-driven page, handle:

- loading state
- empty state
- error state
- success state

Do not hide backend failures behind fake fallback data unless explicitly requested for a prototype.

## UI architecture

Prefer:

```text
page
  -> feature component
  -> reusable UI component
  -> API client
```

Keep data-fetching and domain logic out of generic presentational components.

Create reusable components when repetition is real; avoid premature design-system abstraction.

## Forms

- Validate input on the client for UX.
- Treat backend validation as authoritative.
- Show actionable API validation errors.
- Disable duplicate submission while a mutation is in flight.
- Never assume a successful UI state means the database mutation succeeded.

## Auth/RBAC

Frontend route guards are for UX only.

The backend must enforce:

- authentication
- role checks
- resource ownership

Never hide sensitive data merely with conditional rendering.

## Performance

- Prefer Server Components for initial data fetching.
- Avoid unnecessary client-side JavaScript.
- Avoid duplicate API calls.
- Use pagination for unbounded lists.
- Do not add state-management libraries until the application actually requires shared client state.

## Verification

After frontend changes, verify:

```bash
npm run lint
npm run build
```

For API-connected pages, verify the actual backend request rather than relying only on mocked UI states.
