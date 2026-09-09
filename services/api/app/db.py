from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from psycopg_pool import AsyncConnectionPool

from app.core.config import settings


def create_pool() -> AsyncConnectionPool:
    return AsyncConnectionPool(
        conninfo=str(settings.database_url),
        min_size=1,
        max_size=10,
        open=False,
    )


async def init_db(app: FastAPI) -> AsyncIterator[None]:
    pool = create_pool()

    await pool.open()
    await pool.wait()

    app.state.db = pool

    try:
        yield
    finally:
        await pool.close()


def get_db(request: Request) -> AsyncConnectionPool:
    return request.app.state.db