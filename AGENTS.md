# ShopPilot AI Project Instructions

## Source of Truth

Before making changes, inspect:

1. `docs/architecture.md`
2. `docs/roadmap.md`
3. relevant source files
4. relevant nested `AGENTS.md`

The architecture document defines the system design.

The roadmap defines the implementation sequence.

Do not skip ahead to later roadmap phases unless explicitly requested or the current roadmap explicitly defines a sequencing exception.

## Project Stack

- Next.js
- TypeScript
- FastAPI
- Python
- Supabase
- PostgreSQL
- psycopg 3
- pgvector
- Gemini
- LangGraph

Do not replace these technologies without explicit instruction.

## Architecture

Frontend:

`apps/web`

Backend:

`services/api`

Database:

`supabase/migrations`

Seed:

`supabase/seed.sql`

High-level flow:

`Next.js → FastAPI → PostgreSQL`

Supabase additionally provides:

- Auth
- Storage
- pgvector

Backend structure:

`routers → services → repositories → database`

## Security

Never:

- trust user-supplied authorization identifiers
- expose service_role credentials
- bypass authentication
- bypass RBAC
- bypass RLS
- bypass resource ownership checks
- expose internal documents to customers
- return unauthorized tool results
- execute unvalidated tool parameters
- treat retrieved documents as trusted instructions
- let the LLM perform deterministic business decisions
- silently approve HITL actions

Authorization is enforced server-side.

Frontend authorization is only UX.

## Database

All schema changes must use:

`supabase/migrations/*.sql`

Never modify the schema manually without creating a migration.

Seed changes belong in:

`supabase/seed.sql`

Use PostgreSQL parameterization.

Use transactions for multi-write operations requiring atomicity.

## AI

Treat model output as untrusted input.

RAG answers must use only permitted retrieved context.

Citations must reference actually retrieved chunks.

Validate and authorize tools before execution.

Log important tool calls and security-sensitive actions.

## Implementation

Prefer small, isolated changes.

Reuse existing utilities and patterns.

Do not add dependencies without a demonstrated need.

Do not rewrite working code unnecessarily.

Do not add unrelated features.

Keep business logic out of route handlers.

Preserve existing API contracts unless the task explicitly requires a change.

## Verification

Backend:

```bash
pytest