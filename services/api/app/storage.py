from storage3 import SyncStorageClient

from app.auth import get_supabase_client


# The private Storage bucket that holds uploaded knowledge-base PDFs
# (see supabase/config.toml). Server-side code reaches it through the
# service-role client; the bucket name is internal and must never be
# exposed to customers or logged.
DOCUMENTS_BUCKET = "documents"


def get_storage() -> SyncStorageClient:
    """FastAPI dependency for the server-side Supabase Storage client.

    Returns the *Storage facade* of the service-role Supabase client:
    ``Client.storage``. Every call site (upload, download, cleanup,
    deletion) and every test fake addresses Storage through
    ``storage.from_(bucket).upload/download/remove``.

    The full ``supabase.Client`` must deliberately NOT be returned
    here: on supabase-py 2.31.0 its ``from_`` is the PostgREST *table*
    accessor (verify: it exposes no ``upload``/``remove``/``download``
    methods), which raises ``AttributeError`` against the real Storage
    service. JWT verification keeps its own full client through
    ``app.auth.get_supabase_client``.

    Initialized from the existing Settings configuration and the
    service-role key, which never leaves the backend.
    """
    return get_supabase_client().storage
