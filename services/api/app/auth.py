from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg_pool import AsyncConnectionPool
from supabase import Client, create_client

from app.core.config import settings
from app.db import get_db
from app.services import auth as auth_service


security = HTTPBearer(auto_error=False)


def get_supabase_client() -> Client:
    return create_client(
        str(settings.supabase_url),
        settings.supabase_service_role_key.get_secret_value(),
    )


async def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(security),
    ],
):
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication scheme",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Extract JWT from:
    # Authorization: Bearer <token>
    token = credentials.credentials

    try:
        supabase = get_supabase_client()

        response = supabase.auth.get_claims(token)

        if response is None or "claims" not in response:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        claims = response["claims"]

        user_id = claims.get("sub")

        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token missing user identity",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return {
            "id": user_id,
            "email": claims.get("email"),
            "claims": claims,
        }

    except HTTPException:
        
        raise
    except Exception as exc:
        print(
            f"JWT verification error: "
            f"{type(exc).__name__}: {exc}"
        )

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="JWT verification failed",
        ) from exc



def require_role(*roles: str):

    async def role_dependency(
        current_user=Depends(get_current_user),
        pool: AsyncConnectionPool = Depends(get_db),
    ):
        async with pool.connection() as conn:
            async with conn.cursor() as cursor:
                user_role = await auth_service.get_user_role(
                    cursor,
                    user_id=current_user["id"],
                )

        if user_role is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User profile not found",
            )

        if user_role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )

        return {
            **current_user,
            "role": user_role,
        }

    return role_dependency