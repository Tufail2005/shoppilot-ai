# centralizes order access checks so both the order-detail endpoint and ticket creation use the same authorization rules.

import uuid

from fastapi import HTTPException, status
from psycopg import AsyncCursor

from app.repositories import orders as orders_repository
from app.repositories import profiles as profiles_repository


async def assert_can_access_order(
    cursor: AsyncCursor,
    *,
    user_id: uuid.UUID,
    order_id: uuid.UUID,
) -> dict:
    """
    Return the order when the authenticated user is authorized.

    Customers can access their own orders.
    Support agents and admins can access all orders.

    Raises:
        HTTPException 403: User profile is missing or role is unauthorized.
        HTTPException 404: Order does not exist or is not accessible.
    """
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
        customer_id = user_id
    elif role in ("support_agent", "admin"):
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
        # Avoid revealing whether another customer's order exists.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found",
        )

    return order