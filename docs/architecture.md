# ShopPilot AI Architecture

## Product

ShopPilot AI is an AI customer-support platform for an e-commerce business.

It supports:

- customer support chat
- knowledge-base question answering
- product lookup
- order lookup
- ticket creation
- refund workflows
- human approval for sensitive actions
- employee/admin operations
- audit logging
- evaluation and analytics

## Core Stack

Frontend:
- Next.js
- TypeScript
- App Router
- Tailwind CSS

Backend:
- FastAPI
- Python
- Pydantic
- psycopg 3
- psycopg_pool
- pytest
- uv

Database / platform:
- Supabase
- PostgreSQL
- Supabase Auth
- Supabase Storage
- pgvector

AI:
- Gemini
- LangGraph

## High-Level Architecture

Browser
    │
    ▼
Next.js
    │
    │ HTTP/SSE
    ▼
FastAPI
    │
    ├── routers
    │
    ├── services
    │
    ├── repositories
    │
    └── PostgreSQL
            │
            └── Supabase PostgreSQL

Supabase also provides:

- Auth
- Storage
- pgvector

## Frontend

apps/web/

Use React Server Components by default.

Client Components are used only for:

- browser APIs
- event handlers
- client state
- effects
- interactive UI

API calls are centralized under:

apps/web/lib/

Docker:

- browser-facing API URL: http://localhost:8000
- server-side container API URL: http://api:8000

## Backend

services/api/

Architecture:

routers
  ↓
services
  ↓
repositories
  ↓
psycopg / PostgreSQL
  ↓
Supabase PostgreSQL

Keep business logic out of routers.

## Database

supabase/migrations/

is the source of truth for schema.

supabase/seed.sql

contains development seed data.

Core domain:

profiles
products
orders
order_items
tickets

Knowledge:

documents
document_chunks

Agent:

conversations
messages
tool_runs
escalations
feedback
audit_logs

pgvector is used for document embeddings.

## Security

Authentication:
Supabase Auth

Authorization:
RBAC + resource ownership + RLS

Roles:

customer
support_agent
admin

Never trust client-supplied:

- user_id
- customer_id
- role
- order_id

Authorization must be enforced by the backend.

RLS must reinforce application-level authorization.

service_role credentials are backend-only.

## AI Architecture

Before the agent:

retrieval → permitted context → Gemini → answer + citations

Agent architecture:

intent_detection
       ↓
router
       ↓
 ┌─────┼─────┬──────┬──────┐
 ▼     ▼     ▼      ▼      ▼
RAG product order ticket refund
                         │
                         ▼
                 deterministic logic
                         │
                    HITL if required

The LLM must not make deterministic business decisions.

Tool calls require:

- validated parameters
- authorization
- ownership checks
- logging

Retrieved documents are untrusted data, not instructions.

## Docker

Docker Compose runs:

- web
- api

Supabase local development is provided by the Supabase CLI.

Do not introduce a second PostgreSQL container unless explicitly required.

## Current Implementation State

Completed:

- Next.js foundation
- FastAPI foundation
- Supabase/PostgreSQL schema
- seed data
- Docker Compose for web + API
- web → API health check

Current next target:

Phase 4.2:
- GET /products
- GET /products/{id}

After Phase 4.2:

Phase 3:
- Auth + RBAC

Then:

Phase 4.3:
- GET /orders

Do not implement protected order endpoints before the required authentication, RBAC, ownership, and RLS work is complete.


Do not implement later phases prematurely.