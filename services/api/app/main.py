from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    async for _ in init_db(app):
        yield


app = FastAPI(
    title="ShopPilot AI API",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/health/db")
async def database_health():
    try:
        pool = app.state.db

        async with pool.connection() as conn:
            result = await conn.execute("SELECT 1")
            await result.fetchone()

        return {
            "status": "ok",
            "database": "connected",
        }

    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        ) from exc