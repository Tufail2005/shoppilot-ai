"""Real-Storage verification of the Phase 5.8 deletion policies.

These tests run against the actual local Supabase Storage service
started by ``supabase start`` — never only against a fake — and skip
cleanly when the local stack is not running. Everything is confined
to 127.0.0.1 and the standard, documented Supabase CLI local
service_role key (the CLI's demo JWT; it only ever applies to local
stacks and is not a production credential).

Verified against supabase-py 2.31.0 / the local Storage service:

- the app-facing storage facade (``app.storage.get_storage``) really
  reaches Storage (``supabase.Client.from_`` is the PostgREST table
  accessor and does NOT provide Storage methods — see comment in
  app/storage.py);
- removing an existing object succeeds;
- removing a NEVER-EXISTED path does not raise (empty result): the
  real-API property that makes a repeated deletion heal a row whose
  object is already gone. This is verified live, not assumed by a
  fake;
- a genuine Storage failure (unreachable service) DOES raise, so at
  the SDK surface an exception reliably means "failed / uncertain",
  never "the object was missing";
- end to end: upload → object exists in the bucket → DELETE the
  endpoint → object gone, row gone, cascaded chunks gone (real
  database cascade through the real Storage facade).

Strictly faked in these tests: the Gemini embedding provider only
(no network, no credentials). Storage itself runs for real.
"""

import uuid

import pytest
from pydantic import SecretStr
from supabase import create_client

from app.core import config
from app.main import app
from app.services.embeddings import get_embedding_provider
from app.storage import DOCUMENTS_BUCKET, get_storage
from tests.ingestion_fakes import FakeEmbeddingProvider
from tests.test_admin_documents import (
    _INGESTABLE_PDF,
    _count_chunks,
    _delete_document,
    _fetch_document,
)


LOCAL_SUPABASE_URL = "http://127.0.0.1:54321"

# Standard documented Supabase CLI local service_role key (demo JWT
# signed with the CLI's fixed demo secret, printed by every local
# `supabase status`). Local stack on 127.0.0.1 only.
_LOCAL_SERVICE_ROLE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIs"
    "ImV4cCI6MTk4MzgxMjk5Nn0."
    "EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU"
)

_PROBE_PREFIX = "probe/_phase_5_8_"


def _local_bucket():
    """A Storage bucket proxy for the real local stack, or pytest.skip.

    The skip keeps suites green on machines without ``supabase start``
    running; the probes are otherwise real network operations against
    the local Storage service.
    """
    bucket = create_client(LOCAL_SUPABASE_URL, _LOCAL_SERVICE_ROLE_KEY)
    bucket = bucket.storage.from_(DOCUMENTS_BUCKET)

    try:
        bucket.list("probe/_reachability_check")
    except Exception as exc:
        pytest.skip(
            "Local Supabase Storage is not reachable: start it with "
            f"`supabase start` (checked via Storage API: {type(exc).__name__})"
        )

    return bucket


@pytest.fixture
def local_settings(monkeypatch: pytest.MonkeyPatch):
    """Pin the app Settings to the local stack for the request.

    The test process normally has no SUPABASE_* environment and the
    services/api/.env points elsewhere, so the Settings object used by
    get_supabase_client is pinned to the same local stack the rest of
    this module talks to. No remote service is ever contacted.
    """
    monkeypatch.setattr(
        config.settings,
        "supabase_url",
        LOCAL_SUPABASE_URL,
    )
    monkeypatch.setattr(
        config.settings,
        "supabase_service_role_key",
        SecretStr(_LOCAL_SERVICE_ROLE_KEY),
    )


@pytest.fixture
def fake_embedding_only(monkeypatch: pytest.MonkeyPatch):
    """Override ONLY the embedding provider: Storage stays real.

    Provider resolution before the upload is a Phase 5.7 contract, so
    a request running the real ingestion pipeline needs an injectable
    provider; the real Gemini dependency would attempt network access.
    get_storage is deliberately NOT overridden — the endpoint runs the
    real Storage facade against the real local bucket.
    """
    app.dependency_overrides[get_embedding_provider] = (
        lambda: FakeEmbeddingProvider()
    )

    try:
        yield
    finally:
        app.dependency_overrides.pop(get_embedding_provider, None)


def _cleanup_uploaded_document(document_id: str, bucket=None) -> None:
    """Best-effort cleanup after a failing assertion mid-test."""
    try:
        row = _fetch_document(document_id)
    except Exception:
        row = None

    if row is not None:
        path = row["storage_path"]

        _delete_document(document_id)

        if bucket is not None:
            try:
                bucket.remove([path])
            except Exception:
                pass


class TestLocalRemovalPolicy:
    def test_remove_existing_object_succeeds(self):
        bucket = _local_bucket()

        probe = f"{_PROBE_PREFIX}{uuid.uuid4()}.pdf"
        fake_pdf = b"%PDF-1.4 storage-policy probe"

        bucket.upload(
            path=probe,
            file=fake_pdf,
            file_options={"content-type": "application/pdf"},
        )

        try:
            assert bucket.exists(probe) is True

            result = bucket.remove([probe])

            assert isinstance(result, list)
            assert result and result[0]["name"] == probe
            assert bucket.exists(probe) is False
        finally:
            # If removal did not happen, do not leave probe litter.
            try:
                bucket.remove([probe])
            except Exception:
                pass

    def test_remove_nonexistent_object_does_not_raise(self):
        """Deleting a missing object is a success on the real API.

        This is the property that makes a repeated deletion (after an
        uncertain first attempt) heal a stale row-without-object
        state. A fake must not be trusted for this policy, so it is
        verified live here.
        """
        bucket = _local_bucket()

        never_existed = f"{_PROBE_PREFIX}{uuid.uuid4()}.pdf"

        assert bucket.exists(never_existed) is False

        result = bucket.remove([never_existed])

        # No exception; an empty result is the documented missing
        # outcome for this Supabase version.
        assert isinstance(result, list)
        assert result == []

    def test_genuine_storage_failure_raises(self):
        """An unreachable Storage service raises; exceptions never
        mean "the object was simply missing"."""
        dead_client = create_client("http://127.0.0.1:9", "local")
        dead_bucket = dead_client.storage.from_(DOCUMENTS_BUCKET)

        with pytest.raises(Exception):
            dead_bucket.remove([f"{_PROBE_PREFIX}unreachable.pdf"])

    def test_app_dependency_facade_reaches_real_storage(
        self,
        local_settings,
    ):
        """get_storage() must supply the Storage facade, addressing the
        verified supabase-py incompatibility with the top-level
        Client.from_ table accessor."""
        bucket = get_storage().from_(DOCUMENTS_BUCKET)

        probe = f"{_PROBE_PREFIX}{uuid.uuid4()}.pdf"

        bucket.upload(
            path=probe,
            file=b"%PDF-1.4 facade probe",
            file_options={"content-type": "application/pdf"},
        )
        try:
            assert bucket.exists(probe) is True

            bucket.remove([probe])

            assert bucket.exists(probe) is False
        finally:
            try:
                bucket.remove([probe])
            except Exception:
                pass


class TestRealStorageEndToEnd:
    def _document_url(self, document_id: str) -> str:
        return f"/admin/documents/{document_id}"

    def test_delete_endpoint_removes_object_chunks_and_row(
        self,
        admin_client,
        fake_embedding_only,
        local_settings,
    ):
        """The full real-Storage path: upload → bucket object exists
        → DELETE → object gone, row gone, chunks cascaded."""
        bucket = _local_bucket()

        response = admin_client.post(
            "/admin/documents",
            files={"file": ("predict.pdf", _INGESTABLE_PDF, "application/pdf")},
        )
        assert response.status_code == 201

        document = response.json()
        document_id = document["id"]
        try:
            assert document["status"] == "ready"

            # The internal storage path is never in any response body.
            assert "uploads/" not in response.text
            assert "storage_path" not in response.text

            row = _fetch_document(document_id)
            assert row is not None

            internal_path = row["storage_path"]
            assert bucket.exists(internal_path) is True

            delete_response = admin_client.delete(
                self._document_url(document_id)
            )

            assert delete_response.status_code == 204
            assert delete_response.content == b""
            assert "uploads/" not in delete_response.text

            # Object actually gone from the real bucket.
            assert bucket.exists(internal_path) is False

            # Row gone and its chunks cascaded away.
            assert _fetch_document(document_id) is None
            assert _count_chunks(document_id) == 0
        except AssertionError:
            _cleanup_uploaded_document(document_id, bucket)
            raise

    def test_delete_endpoint_heals_row_whose_object_is_missing(
        self,
        admin_client,
        fake_embedding_only,
        local_settings,
    ):
        """Row alive, object already removed from the bucket (the
        state a prior uncertain attempt may have left): the endpoint
        must still succeed — verified against the real Service,
        exercising the verified missing-object-removal behavior."""
        bucket = _local_bucket()

        response = admin_client.post(
            "/admin/documents",
            files={"file": ("heal.pdf", _INGESTABLE_PDF, "application/pdf")},
        )
        assert response.status_code == 201

        document = response.json()
        document_id = document["id"]
        try:
            row = _fetch_document(document_id)
            assert row is not None

            internal_path = row["storage_path"]

            # Simulate the row-without-object state out-of-band.
            bucket.remove([internal_path])
            assert bucket.exists(internal_path) is False

            heal_response = admin_client.delete(
                self._document_url(document_id)
            )

            assert heal_response.status_code == 204
            assert _fetch_document(document_id) is None
        except AssertionError:
            _cleanup_uploaded_document(document_id, bucket)
            raise
