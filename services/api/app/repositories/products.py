from psycopg import AsyncCursor

_PRODUCT_COLUMNS = """
    id,
    sku,
    name,
    description,
    price,
    stock,
    category,
    created_at,
    updated_at
"""


async def list_products(
    cursor: AsyncCursor,
    *,
    limit: int,
    offset: int,
    category: str | None,
) -> tuple[list[dict], int]:
    
    params: list = []
    where_clauses: list[str] = []

    if category is not None:
        where_clauses.append("category = %s")
        params.append(category)

    where_sql = f" WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    count_sql = f"SELECT count(*) FROM public.products{where_sql}"
    await cursor.execute(count_sql, params)
    row = await cursor.fetchone()
    total = row[0] if row else 0

    page_sql = f""" 
        SELECT {_PRODUCT_COLUMNS}
        FROM public.products{where_sql}
        ORDER BY created_at DESC, id
        LIMIT %s OFFSET %s
    """
    page_params = params + [limit, offset]
    await cursor.execute(page_sql, page_params)

    rows = await cursor.fetchall()

    column_names = [desc.name for desc in cursor.description]
    items = [dict(zip(column_names, row, strict=True)) for row in rows]

    return items, total


async def get_product_by_id(
    cursor: AsyncCursor,
    *,
    product_id,
) -> dict | None:
    sql = f"""
        SELECT {_PRODUCT_COLUMNS}
        FROM public.products
        WHERE id = %s
    """
    await cursor.execute(sql, [product_id])
    row = await cursor.fetchone()

    if row is None:
        return None

    column_names = [desc.name for desc in cursor.description]
    return dict(zip(column_names, row, strict=True))
 