import uuid

from fastapi import HTTPException, status
from psycopg import AsyncCursor

from app.repositories import orders as orders_repository
from app.repositories import profiles as profiles_repository

from app.services import ownership

async def list_orders(
    cursor: AsyncCursor,
    *,
    user_id: uuid.UUID,
    limit: int,
    offset: int,
) -> tuple[list[dict], int]:
    # Look up the role in the database.
    # Do not accept the role from the client.
    role = await profiles_repository.get_profile_role(
        cursor,
        user_id=str(user_id),
    )

    if role is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User profile not found",
        )

    if role == "customer":
        # Customers can see only their own orders.
        customer_id = user_id

    elif role in ("support_agent", "admin"):
        # Trusted employee roles can see all orders.
        customer_id = None

    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )

    return await orders_repository.list_orders(
        cursor,
        limit=limit,
        offset=offset,
        customer_id=customer_id,
    )


async def get_order(
    cursor: AsyncCursor,
    *,
    user_id: uuid.UUID,
    order_id: uuid.UUID,
) -> dict:
    return await ownership.assert_can_access_order(
        cursor,
        user_id=user_id,
        order_id=order_id,
    )