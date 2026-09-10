import uuid

from fastapi import APIRouter, Depends, Query
from psycopg_pool import AsyncConnectionPool

from app.db import get_db
from app.schemas.products import ProductListOut, ProductOut
from app.services import products as products_service

router = APIRouter(prefix="/products", tags=["products"])


@router.get("", response_model=ProductListOut)
async def list_products(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    category: str | None = Query(default=None),
    pool: AsyncConnectionPool = Depends(get_db),
):
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            items, total = await products_service.list_products(
                cursor,
                limit=limit,
                offset=offset,
                category=category,
            )

    return ProductListOut(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{product_id}", response_model=ProductOut)
async def get_product(
    product_id: uuid.UUID,
    pool: AsyncConnectionPool = Depends(get_db),
):
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            product = await products_service.get_product(
                cursor,
                product_id=product_id,
            )

    return product
