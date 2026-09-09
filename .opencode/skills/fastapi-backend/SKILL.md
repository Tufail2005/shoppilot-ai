# FastAPI Backend Skill

Build production-ready Python backends using FastAPI.

## Stack
- Python 3.12+
- FastAPI
- PostgreSQL
- SQLAlchemy 2.0 + Alembic
- Pydantic v2
- JWT authentication
- pytest
- uv for dependency management

## Rules
- Use async/await for I/O-bound operations.
- Follow a modular architecture: routers → services → repositories → models.
- Keep business logic out of routers.
- Use Pydantic schemas for request/response validation.
- Use SQLAlchemy models only for database representation.
- Use Alembic for all schema changes; never modify production DB manually.
- Implement proper error handling with meaningful HTTP status codes.
- Never hardcode secrets; use environment variables.
- Add authentication and RBAC where required.
- Validate authorization on every protected resource.
- Use dependency injection for DB sessions and authentication.
- Write tests for services and API endpoints.
- Keep functions small and focused.
- Add type hints throughout the codebase.
- Do not over-engineer or add unnecessary abstractions.

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