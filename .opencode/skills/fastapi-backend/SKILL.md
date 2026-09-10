# FastAPI Backend Skill

Build production-ready Python backends using FastAPI.

## Stack

- Python 3.14+
- FastAPI
- Pydantic v2
- PostgreSQL
- psycopg 3 / psycopg_pool
- pytest
- uv


## Database

Use psycopg / psycopg_pool for PostgreSQL access.

Use transactions for operations that require atomicity.

Avoid N+1 queries.

Use explicit ordering for paginated queries.

Add indexes based on actual query patterns.

Never modify schema manually when the project uses migrations.

Keep database changes backward-compatible where practical.

## Rules
- Use async/await for I/O-bound operations.

- Keep route handlers thin.

- Keep business logic out of route handlers.

- Use Pydantic schemas for request and response validation.

- Use dependency injection for database access, authentication, and authorization.

- Use parameterized SQL; never interpolate user input into SQL.

- Use meaningful HTTP status codes and safe error responses.

- Never hardcode secrets; use environment variables.

- Add authentication and RBAC where required.

- Validate authorization on every protected resource.

- Enforce resource ownership where required.

- Keep functions small and focused.

- Add type hints throughout the codebase.

- Avoid unnecessary abstractions and dependencies.

- Preserve existing project conventions unless the task requires a change.

## Authentication & Security

- Never trust client-supplied identity or role fields.

- Derive the authenticated identity from the validated auth context.

- Check RBAC and resource ownership before protected operations.

- Never expose secrets, tokens, database credentials, or stack traces.

- Treat external input and model output as untrusted data.

- Validate tool parameters before execution.

## Testing

Write tests for:

- API endpoints
- services/business logic
- validation
- authentication and authorization
- resource ownership
- important database behavior
- error handling

Prefer integration tests for critical database-backed behavior.

Never claim tests passed unless they actually ran successfully.

## Workflow
1. Understand requirements and existing code.
2. Design DB schema and API contracts first.
3. Implement models + migrations.
4. Implement repositories/services.
5. Implement API routes.
6. Add auth/RBAC.
7. Add tests.
8. Run lint/type checks/tests.
9. Fix errors before declaring the task complete.

Never build unrelated features or change existing architecture without a reason.