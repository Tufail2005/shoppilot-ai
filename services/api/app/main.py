from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.db import init_db
from app.routers import products

from fastapi import Depends

from app.auth import get_current_user

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


@app.get("/health")
async def health():
    return {"status": "ok"}


# @app.get("/health/db")
# async def database_health():
#     try:
#         pool = app.state.db

#         async with pool.connection() as conn:
#             result = await conn.execute("SELECT 1")
#             await result.fetchone()

#         return {
#             "status": "ok",
#             "database": "connected",
#         }

#     except Exception as exc:
#         raise HTTPException(
#             status_code=503,
#             detail="Database unavailable",
#         ) from exc


@app.get("/auth/me")
async def auth_me(user=Depends(get_current_user)):
    return user


# Temporarily for testing

from fastapi import Depends

from app.auth import require_role

@app.get("/auth/customer-test")
async def customer_test(
    user=Depends(require_role("customer")),
):
    return {
        "message": "Customer access granted",
        "user": user,
    }


@app.get("/auth/agent-test")
async def agent_test(
    user=Depends(require_role("support_agent", "admin")),
):
    return {
        "message": "Agent access granted",
        "user": user,
    }


@app.get("/auth/admin-test")
async def admin_test(
    user=Depends(require_role("admin")),
):
    return {
        "message": "Admin access granted",
        "user": user,
    }



# delete it now : 
@app.get("/health/db")
async def database_health():
    try:
        pool = app.state.db

        async with pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    SELECT
                        current_database(),
                        current_user,
                        inet_server_addr(),
                        inet_server_port()
                """)

                row = await cursor.fetchone()

        return {
            "database": row[0],
            "user": row[1],
            "host": str(row[2]),
            "port": row[3],
        }

    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        ) from exc