import uuid

from fastapi import HTTPException, status
from psycopg import AsyncCursor

from app.repositories import orders as orders_repository
from app.repositories import profiles as profiles_repository
from app.repositories import tickets as tickets_repository


async def _get_role(
    cursor: AsyncCursor,
    *,
    user_id: uuid.UUID,
) -> str:
    role = await profiles_repository.get_profile_role(
        cursor,
        user_id=str(user_id),
    )

    if role is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User profile not found",
        )

    if role not in ("customer", "support_agent", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )

    return role


async def list_tickets(
    cursor: AsyncCursor,
    *,
    user_id: uuid.UUID,
    limit: int,
    offset: int,
) -> tuple[list[dict], int]:
    role = await _get_role(cursor, user_id=user_id)

    customer_id = user_id if role == "customer" else None

    return await tickets_repository.list_tickets(
        cursor,
        limit=limit,
        offset=offset,
        customer_id=customer_id,
    )


async def get_ticket(
    cursor: AsyncCursor,
    *,
    user_id: uuid.UUID,
    ticket_id: uuid.UUID,
) -> dict:
    role = await _get_role(cursor, user_id=user_id)

    customer_id = user_id if role == "customer" else None

    ticket = await tickets_repository.get_ticket(
        cursor,
        ticket_id=ticket_id,
        customer_id=customer_id,
    )

    if ticket is None:
        # Do not reveal whether another customer's ticket exists.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ticket not found",
        )

    return ticket


async def create_ticket(
    cursor: AsyncCursor,
    *,
    user_id: uuid.UUID,
    subject: str,
    order_id: uuid.UUID | None,
) -> dict:
    role = await _get_role(cursor, user_id=user_id)

    if role != "customer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only customers can create tickets",
        )

    # If an order is linked, it must belong to this customer.
    if order_id is not None:
        order = await orders_repository.get_order(
            cursor,
            order_id=order_id,
            customer_id=user_id,
        )

        if order is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found",
            )

    # The authenticated user determines customer_id.
    # Never accept customer_id from the request body.
    return await tickets_repository.create_ticket(
        cursor,
        customer_id=user_id,
        order_id=order_id,
        subject=subject,
    )


async def update_ticket(
    cursor: AsyncCursor,
    *,
    user_id: uuid.UUID,
    ticket_id: uuid.UUID,
    updates: dict,
) -> dict:
    role = await _get_role(cursor, user_id=user_id)

    if role not in ("support_agent", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only support agents or admins can update tickets",
        )

    # Refuse an empty PATCH request.
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one update field is required",
        )

    # Do not permit null status or priority values.
    for field in ("status", "priority"):
        if field in updates and updates[field] is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{field} cannot be null",
            )

    ticket = await tickets_repository.get_ticket(
        cursor,
        ticket_id=ticket_id,
    )

    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ticket not found",
        )

    # A ticket can only be assigned to another support agent or admin.
    if updates.get("assigned_to") is not None:
        assignee_role = await profiles_repository.get_profile_role(
            cursor,
            user_id=str(updates["assigned_to"]),
        )

        if assignee_role not in ("support_agent", "admin"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Assignee must be a support agent or admin",
            )

    updated_ticket = await tickets_repository.update_ticket(
        cursor,
        ticket_id=ticket_id,
        updates=updates,
    )

    if updated_ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ticket not found",
        )

    return updated_ticket