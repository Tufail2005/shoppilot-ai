# Backend Rules

This directory contains the ShopPilot AI FastAPI backend.

Use:
- FastAPI
- Pydantic v2
- psycopg 3
- psycopg_pool
- uv
- pytest

Architecture:

routers → services → repositories → database

Do not introduce SQLAlchemy/Alembic.

Database migrations belong in:

../../supabase/migrations/

Never:
- trust client-supplied user_id/customer_id/role
- bypass authorization
- expose service_role credentials
- interpolate user input into SQL

Keep routers thin.

Run relevant pytest checks after backend changes.