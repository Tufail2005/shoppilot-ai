from app.repositories import profiles as profiles_repository


async def get_user_role(
    cursor,
    *,
    user_id: str,
) -> str | None:
    return await profiles_repository.get_profile_role(
        cursor,
        user_id=user_id,
    )