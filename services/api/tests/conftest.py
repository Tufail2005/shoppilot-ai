import asyncio
import os
import sys

import pytest

LOCAL_SUPABASE_DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"

os.environ["DATABASE_URL"] = (
    os.environ.get("TEST_DATABASE_URL") or LOCAL_SUPABASE_DATABASE_URL
)
os.environ.setdefault("SUPABASE_URL", "http://127.0.0.1:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "local-test-key")

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi.testclient import TestClient
from psycopg import Connection

from app.main import app


def _require_test_database() -> None:
    database_url = os.environ["DATABASE_URL"]

    try:
        with Connection.connect(database_url, connect_timeout=5) as conn:
            with conn.cursor() as cursor:
                cursor.execute("select 1")
                cursor.fetchone()
    except Exception as exc:
        pytest.exit(
            "Test database unavailable: "
            f"{database_url}\n"
            "Start the local Supabase stack first: `supabase start` "
            "(requires Docker Desktop), then `supabase db reset` if needed.\n"
            "Tests never fall back to the remote database in services/api/.env.\n"
            "To target a different test database explicitly, set TEST_DATABASE_URL.\n"
            f"Connection error: {exc}",
            returncode=1,
        )


@pytest.fixture(scope="session")
def client():
    _require_test_database()
    with TestClient(app) as test_client:
        yield test_client
