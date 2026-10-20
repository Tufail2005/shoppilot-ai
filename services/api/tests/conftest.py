import asyncio
import os
import sys
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from psycopg import Connection

from app.auth import get_current_user
from app.main import app


LOCAL_SUPABASE_DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"

os.environ["DATABASE_URL"] = (
    os.environ.get("TEST_DATABASE_URL") or LOCAL_SUPABASE_DATABASE_URL
)
os.environ.setdefault("SUPABASE_URL", "http://127.0.0.1:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "local-test-key")

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


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


# Seeded user IDs from supabase/seed.sql
TEST_USERS = {
    "customer": "a8add1a9-697d-405d-9870-1b7dd5ea4e3b",
    "admin": "4447a78a-8b83-4982-8e1e-18f894115645",
    "support_agent": "7a23da22-763c-4d79-a03d-70d044c44f38",
    "carol": "0b89b138-8b5f-4d7f-b5c2-ce673679c300",
    "david": "f5b1e608-eadb-40e2-a78b-aa02c14f4854",
    "unknown": "00000000-0000-0000-0000-000000000000",
}


def _override_get_current_user(user_id: str):
    """Override get_current_user to return a test user without JWT validation."""
    async def override():
        return {"id": user_id, "email": None, "claims": {"sub": user_id}}
    return override


@pytest.fixture(scope="session")
def client():
    _require_test_database()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def customer_client(client: TestClient) -> Generator[TestClient, None, None]:
    """TestClient authenticated as a customer."""
    app.dependency_overrides[get_current_user] = _override_get_current_user(TEST_USERS["customer"])
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def admin_client(client: TestClient) -> Generator[TestClient, None, None]:
    """TestClient authenticated as an admin."""
    app.dependency_overrides[get_current_user] = _override_get_current_user(TEST_USERS["admin"])
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def agent_client(client: TestClient) -> Generator[TestClient, None, None]:
    """TestClient authenticated as a support agent."""
    app.dependency_overrides[get_current_user] = _override_get_current_user(TEST_USERS["support_agent"])
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def carol_client(client: TestClient) -> Generator[TestClient, None, None]:
    """TestClient authenticated as Carol (another customer)."""
    app.dependency_overrides[get_current_user] = _override_get_current_user(TEST_USERS["carol"])
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def david_client(client: TestClient) -> Generator[TestClient, None, None]:
    """TestClient authenticated as David (another customer)."""
    app.dependency_overrides[get_current_user] = _override_get_current_user(TEST_USERS["david"])
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def unknown_user_client(client: TestClient) -> Generator[TestClient, None, None]:
    """TestClient authenticated as an unknown user (no profile)."""
    app.dependency_overrides[get_current_user] = _override_get_current_user(TEST_USERS["unknown"])
    yield client
    app.dependency_overrides.clear()
