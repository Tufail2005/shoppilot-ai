import uuid

from psycopg import AsyncCursor


_ORDER_COLUMNS = """
    id,
    customer_id,
    status,
    total,
    created_at
"""


async def list_orders(
    cursor: AsyncCursor,
    *,
    limit: int,
    offset: int,
    customer_id: uuid.UUID | None, 
) -> tuple[list[dict], int]:
    
    params: list = []
    where_sql = ""

    # A customer_id means customer-scoped access.
    # None means all orders, decided by the service layer.
    if customer_id is not None:
        where_sql = "WHERE customer_id = %s"
        params.append(customer_id)

    # Count the orders in the permitted scope.
    count_sql = f"""
        SELECT count(*)
        FROM public.orders
        {where_sql}
    """

    await cursor.execute(count_sql, params)
    row = await cursor.fetchone()
    total = row[0] if row else 0

    # Fetch the requested page.
    page_sql = f"""
        SELECT {_ORDER_COLUMNS}
        FROM public.orders
        {where_sql}
        ORDER BY created_at DESC, id
        LIMIT %s OFFSET %s
    """

    page_params = params + [limit, offset]

    await cursor.execute(page_sql, page_params)
    rows = await cursor.fetchall()

    column_names = [column.name for column in cursor.description]

    items = [
        dict(zip(column_names, row, strict=True))
        for row in rows
    ]

    return items, total


async def get_order(
    cursor: AsyncCursor,
    *,
    order_id: uuid.UUID,
    customer_id: uuid.UUID | None = None,
) -> dict | None:
    """
    Fetch a single order by ID with optional customer_id filtering.
    
    If customer_id is provided, the order must belong to that customer.
    If customer_id is None, any order matching the ID is returned (admin/support_agent).
    """
    params: list = [order_id]
    where_sql = "WHERE id = %s"

    if customer_id is not None:
        where_sql += " AND customer_id = %s"
        params.append(customer_id)

    sql = f"""
        SELECT {_ORDER_COLUMNS}
        FROM public.orders
        {where_sql}
    """

    await cursor.execute(sql, params)
    row = await cursor.fetchone()

    if row is None:
        return None

    column_names = [column.name for column in cursor.description]
    return dict(zip(column_names, row, strict=True))