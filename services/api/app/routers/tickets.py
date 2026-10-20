import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from psycopg_pool import AsyncConnectionPool

from app.auth import get_current_user
from app.db import get_db
from app.schemas.tickets import (
    TicketCreateIn,
    TicketListOut,
    TicketOut,
    TicketPatchIn,
)
from app.services import tickets as tickets_service


router = APIRouter(
    prefix="/tickets",
    tags=["tickets"],
)


@router.get("", response_model=TicketListOut)
async def list_tickets(
    current_user: Annotated[dict, Depends(get_current_user)],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    pool: AsyncConnectionPool = Depends(get_db),
):
    user_id = uuid.UUID(current_user["id"])

    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            items, total = await tickets_service.list_tickets(
                cursor,
                user_id=user_id,
                limit=limit,
                offset=offset,
            )

    return TicketListOut(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    response_model=TicketOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_ticket(
    payload: TicketCreateIn,
    current_user: Annotated[dict, Depends(get_current_user)],
    pool: AsyncConnectionPool = Depends(get_db),
):
    user_id = uuid.UUID(current_user["id"])

    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            ticket = await tickets_service.create_ticket(
                cursor,
                user_id=user_id,
                subject=payload.subject,
                order_id=payload.order_id,
            )

    return ticket


@router.get("/{ticket_id}", response_model=TicketOut)
async def get_ticket(
    ticket_id: uuid.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    pool: AsyncConnectionPool = Depends(get_db),
):
    user_id = uuid.UUID(current_user["id"])

    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            ticket = await tickets_service.get_ticket(
                cursor,
                user_id=user_id,
                ticket_id=ticket_id,
            )

    return ticket


@router.patch("/{ticket_id}", response_model=TicketOut)
async def update_ticket(
    ticket_id: uuid.UUID,
    payload: TicketPatchIn,
    current_user: Annotated[dict, Depends(get_current_user)],
    pool: AsyncConnectionPool = Depends(get_db),
):
    user_id = uuid.UUID(current_user["id"])

    # Only explicitly provided fields are included in the update.
    updates = payload.model_dump(exclude_unset=True)

    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            ticket = await tickets_service.update_ticket(
                cursor,
                user_id=user_id,
                ticket_id=ticket_id,
                updates=updates,
            )

    return ticket