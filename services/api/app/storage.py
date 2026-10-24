from supabase import Client

from app.auth import get_supabase_client


def get_storage() -> Client:
    """FastAPI dependency for the server-side Supabase Storage client.

    Initialized from the existing Settings configuration and the
    service-role key, which never leaves the backend.
    """
    return get_supabase_client()
