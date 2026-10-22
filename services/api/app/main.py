from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.auth import get_current_user
from app.db import init_db
from app.routers import orders, products, tickets

@asynccontextmanager
async def lifespan(app: FastAPI):
    async for _ in init_db(app):
        yield


app = FastAPI(
    title="ShopPilot AI API",
    version="0.1.0",
    lifespan=lifespan,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(products.router)
app.include_router(orders.router)
app.include_router(tickets.router)


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


@app.get("/auth/me")
async def auth_me(user=Depends(get_current_user)):
    return user


