from psycopg import AsyncCursor


async def get_profile_role(
    cursor: AsyncCursor,
    *,
    user_id: str,
) -> str | None:
    sql = """
        SELECT role
        FROM public.profiles
        WHERE id = %s
    """

    await cursor.execute(sql, [user_id])
    row = await cursor.fetchone()

    if row is None:
        return None

    return row[0]