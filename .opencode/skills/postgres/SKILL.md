---
name: postgresql
slug: postgres
description: Design, migrate, query, seed, and review ShopPilot AI PostgreSQL/Supabase schemas with strong data integrity, indexing, transactions, RLS, and pgvector boundaries.
---

# PostgreSQL Skill

Use this skill for `supabase/migrations`, seed data, schema design, SQL, indexes, RLS, and PostgreSQL performance work.

## Source of truth

Committed schema changes belong in:

```text
supabase/migrations/*.sql
```

Seed data belongs in:

```text
supabase/seed.sql
```

Do not make undocumented schema changes in the Supabase dashboard and assume the repository now represents them.

## Schema rules

- Use UUID primary keys consistently with the existing schema.
- Use `timestamptz` for timestamps.
- Use `numeric(12,2)` or a similarly explicit numeric type for money.
- Use foreign keys to encode relationships.
- Use `on delete` behavior deliberately.
- Add `not null` when the domain requires a value.
- Add `check` constraints for invariants that should never be invalid.
- Use enums only where the value set is stable enough to justify them.

## Commerce integrity

For orders:

- `order_items.price` is the historical unit price at purchase time.
- Do not recalculate historical order totals from current product prices.
- Quantities must be positive.
- Monetary amounts must not be negative unless a future explicit refund/credit model requires it.

## Indexing

Index:

- foreign keys used in joins/filtering
- common status filters
- common ownership lookups
- conversation/message ordering paths
- escalation queues

Do not blindly index every column. Consider selectivity and query patterns.

## Transactions

Use transactions when an operation requires multiple writes that must either all succeed or all fail.

Typical examples:

- creating an order and its items
- changing refund state plus audit/event records
- creating escalation records tied to an agent workflow

## RLS

Treat Row Level Security as a security boundary.

Core intended rules:

- customers can access only their own commerce/support/conversation data
- support agents can access operational support data allowed by role
- admins can access administrative data
- public knowledge documents are visible according to document visibility rules
- internal documents are never exposed to customer-role retrieval

Backend authorization and RLS should reinforce each other rather than replacing one another.

## pgvector

- Keep vector dimensionality consistent with the embedding model.
- Do not silently change embedding dimensions in one migration.
- Scope retrieval by document visibility before returning customer-facing results.
- Verify citation permissions again when rendering results.
- Choose vector indexes based on the retrieval workload; do not add an index without considering dataset size and query behavior.

## Migrations

Before applying a migration:

1. inspect dependent tables and foreign keys
2. check existing seed data
3. consider backwards compatibility
4. verify constraints do not invalidate existing rows

Then test locally:

```bash
supabase db reset
```

For production deployment, use the repository migration history and Supabase CLI rather than ad hoc SQL.

## Query safety

- Parameterize values.
- Never build SQL by interpolating user input.
- Select only required columns when practical.
- Avoid N+1 query patterns.
- Use explicit ordering for paginated queries.

## Verification

For schema changes, verify:

- migration applies cleanly
- seed still works
- affected foreign keys/constraints behave as expected
- representative queries return the intended rows
- RLS behavior is tested for each relevant role
