import uuid

from psycopg import AsyncCursor


_DOCUMENT_COLUMNS = """
    id,
    title,
    storage_path,
    visibility,
    status,
    version,
    created_at
"""


async def create_document(
    cursor: AsyncCursor,
    *,
    title: str,
    storage_path: str,
    visibility: str,
) -> dict:
    # Visibility is always explicit here.
    # The database's `public` default is never relied upon.
    await cursor.execute(
        f"""
        INSERT INTO public.documents (
            title,
            storage_path,
            visibility
        )
        VALUES (%s, %s, %s)
        RETURNING {_DOCUMENT_COLUMNS}
        """,
        [title, storage_path, visibility],
    )

    row = await cursor.fetchone()

    if row is None:
        raise RuntimeError("Document creation returned no row")

    column_names = [column.name for column in cursor.description]

    document = dict(zip(column_names, row, strict=True))

    return document
