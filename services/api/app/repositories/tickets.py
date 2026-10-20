import uuid

from psycopg import AsyncCursor


_TICKET_COLUMNS = """
    id,
    customer_id,
    order_id,
    subject,
    status,
    priority,
    assigned_to
"""


async def list_tickets(
    cursor: AsyncCursor,
    *,
    limit: int,
    offset: int,
    customer_id: uuid.UUID | None,
) -> tuple[list[dict], int]:
    params: list = []
    where_sql = ""

    # None means the service layer authorized access to all tickets.
    if customer_id is not None:
        where_sql = "WHERE customer_id = %s"
        params.append(customer_id)

    await cursor.execute(
        f"""
        SELECT count(*)
        FROM public.tickets
        {where_sql}
        """,
        params,
    )

    row = await cursor.fetchone()
    total = row[0] if row else 0

    await cursor.execute(
        f"""
        SELECT {_TICKET_COLUMNS}
        FROM public.tickets
        {where_sql}
        ORDER BY id
        LIMIT %s OFFSET %s
        """,
        params + [limit, offset],
    )

    rows = await cursor.fetchall()
    column_names = [column.name for column in cursor.description]

    items = [
        dict(zip(column_names, row, strict=True))
        for row in rows
    ]

    return items, total


async def get_ticket(
    cursor: AsyncCursor,
    *,
    ticket_id: uuid.UUID,
    customer_id: uuid.UUID | None = None,
) -> dict | None:
    params: list = [ticket_id]
    where_sql = "WHERE id = %s"

    if customer_id is not None:
        where_sql += " AND customer_id = %s"
        params.append(customer_id)

    await cursor.execute(
        f"""
        SELECT {_TICKET_COLUMNS}
        FROM public.tickets
        {where_sql}
        """,
        params,
    )

    row = await cursor.fetchone()

    if row is None:
        return None

    column_names = [column.name for column in cursor.description]

    return dict(zip(column_names, row, strict=True))


async def create_ticket(
    cursor: AsyncCursor,
    *,
    customer_id: uuid.UUID,
    order_id: uuid.UUID | None,
    subject: str,
) -> dict:
    await cursor.execute(
        f"""
        INSERT INTO public.tickets (
            customer_id,
            order_id,
            subject,
            status,
            priority
        )
        VALUES (%s, %s, %s, 'open', 'medium')
        RETURNING {_TICKET_COLUMNS}
        """,
        [customer_id, order_id, subject],
    )

    row = await cursor.fetchone()

    if row is None:
        raise RuntimeError("Ticket creation returned no row")

    column_names = [column.name for column in cursor.description]

    return dict(zip(column_names, row, strict=True))


async def update_ticket(
    cursor: AsyncCursor,
    *,
    ticket_id: uuid.UUID,
    updates: dict,
) -> dict | None:
    # Never use client-supplied keys as SQL column names.
    allowed_columns = {
        "status": "status",
        "priority": "priority",
        "assigned_to": "assigned_to",
    }

    if not updates:
        raise ValueError("At least one update field is required")

    set_clauses: list[str] = []
    params: list = []

    for field, value in updates.items():
        column = allowed_columns.get(field)

        if column is None:
            raise ValueError(f"Unsupported ticket field: {field}")

        set_clauses.append(f"{column} = %s")
        params.append(value)

    params.append(ticket_id)

    await cursor.execute(
        f"""
        UPDATE public.tickets
        SET {", ".join(set_clauses)}
        WHERE id = %s
        RETURNING {_TICKET_COLUMNS}
        """,
        params,
    )

    row = await cursor.fetchone()

    if row is None:
        return None

    column_names = [column.name for column in cursor.description]

    return dict(zip(column_names, row, strict=True))