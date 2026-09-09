# PostgreSQL Backend Skill

Design and implement production-ready PostgreSQL databases for FastAPI applications.

## Stack
- PostgreSQL
- SQLAlchemy 2.0
- Alembic
- asyncpg

## Rules
- Design normalized schemas with clear relationships.
- Use appropriate primary keys, foreign keys, unique constraints, and check constraints.
- Add indexes based on query patterns, not blindly.
- Use `TIMESTAMPTZ` for timestamps.
- Use transactions for multi-step writes.
- Prevent N+1 queries.
- Use parameterized queries; never construct SQL from untrusted input.
- Use SQLAlchemy 2.0 typed models and async sessions.
- Keep database logic out of API routers.
- Use Alembic for every schema change.
- Never edit existing migrations that have already been applied.
- Review destructive migrations carefully.
- Avoid unnecessary database queries.
- Use pagination for large datasets.
- Use `EXPLAIN ANALYZE` when investigating slow queries.
- Store secrets and connection strings in environment variables.
- Do not use `SELECT *` in performance-sensitive queries.

## Schema Design
Before implementation, consider:
1. Entities and relationships
2. Cardinality
3. Constraints
4. Indexes
5. Query patterns
6. Transaction boundaries
7. Data growth

Prefer database-enforced integrity over application-only validation.

## Migration Workflow
1. Modify SQLAlchemy models.
2. Generate/create an Alembic migration.
3. Review the generated SQL.
4. Check for data-loss risks.
5. Apply migration.
6. Test rollback when appropriate.

Never drop or alter production data without explicit confirmation.