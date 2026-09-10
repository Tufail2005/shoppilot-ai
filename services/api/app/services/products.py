from fastapi import HTTPException

from app.repositories import products as products_repository


async def list_products(
    cursor,
    *,
    limit: int,
    offset: int,
    category: str | None,
) -> tuple[list[dict], int]:
    return await products_repository.list_products(
        cursor,
        limit=limit,
        offset=offset,
        category=category,
    )


async def get_product(
    cursor,
    *,
    product_id,
) -> dict:
    product = await products_repository.get_product_by_id(
        cursor,
        product_id=product_id,
    )

    if product is None:
        raise HTTPException(
            status_code=404,
            detail="Product not found",
        )

    return product
