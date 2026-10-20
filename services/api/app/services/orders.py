import uuid

from fastapi import HTTPException, status
from psycopg import AsyncCursor

from app.repositories import orders as orders_repository
from app.repositories import profiles as profiles_repository


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

    order = await orders_repository.get_order(
        cursor,
        order_id=order_id,
        customer_id=customer_id,
    )

    if order is None:
        # Use 404 to avoid leaking whether another customer's order exists
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found",
        )

    return order