import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from psycopg_pool import AsyncConnectionPool

from app.auth import get_current_user
from app.db import get_db
from app.schemas.orders import OrderListOut
from app.services import orders as orders_service


router = APIRouter(
    prefix="/orders",
    tags=["orders"],
)


@router.get("", response_model=OrderListOut)
async def list_orders(
    current_user: Annotated[
        dict,
        Depends(get_current_user),
    ],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    pool: AsyncConnectionPool = Depends(get_db),
):
    # Identity comes from the verified Supabase JWT.
    user_id = uuid.UUID(current_user["id"])

    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            items, total = await orders_service.list_orders(
                cursor,
                user_id=user_id,
                limit=limit,
                offset=offset,
            )

    return OrderListOut(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )