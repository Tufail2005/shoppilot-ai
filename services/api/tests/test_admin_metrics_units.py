"""Offline unit tests for the Phase 6.8 admin metrics endpoint.

No database and no network: the admin role dependency and the
connection pool are replaced with FastAPI dependency overrides, and the
application's ``TestClient`` runs without entering its lifespan (the
established Phase 6.4 harness pattern). A fresh ``ProviderMetrics`` is
monkeypatched onto ``app.core.observability.provider_metrics`` — the
handler resolves the registry (and the payload renderer) at call time,
so module monkeypatching is effective.

Covered: the route exists and is admin-only (real ``get_current_user``
still enforces the 401 surface), the response schema (recursive
structure + bounded-label redaction: no user IDs, document IDs, request
IDs, prompts, or provider error messages exist in the registry so none
can be exposed), 403 for non-admins, fail-fast snapshot construction
(never a 200 with an empty, misleading payload), and the ``metrics_payload``
renderer itself (empty snapshot, populated snapshot, final ``+Inf``
bucket equal to the count).
"""

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient

from app.core import observability
from app.core.observability import ProviderMetrics, metrics_payload
from app.db import get_db
from app.main import app
from app.routers import admin as admin_router


# The bounded label key vocabulary matches the registry schema; the red
# flag here would be any OTHER key showing up in an entry.
_LABEL_KEYS = {
    "operation",
    "resource",
    "admission_outcome",
    "caller_outcome",
    "worker_outcome",
    "kind",
    "attempts",
    "outcome",
    "ingestion_outcome",
    "stage",
    "document_lifecycle_outcome",
}

_TOP_KEYS = {"counters", "durations", "recording_failures"}
_COUNTER_ENTRY_KEYS = {"labels", "value"}
_DURATION_ENTRY_KEYS = {
    "labels",
    "count",
    "sum_seconds",
    "min_seconds",
    "max_seconds",
    "buckets",
}
_BUCKET_KEYS = {"boundary", "count"}


# Every bounded label value in the registry, derived from the schema
# itself so the test follows the source of truth, not a copy of it.
def _bounded_label_values() -> set[str]:
    values: set[str] = set()

    for schema in observability._SERIES_SCHEMA.values():
        for vocabulary in schema.values():
            values.update(vocabulary)

    return values


def _label_vocabulary_keys() -> set[str]:
    keys: set[str] = set()

    for schema in observability._SERIES_SCHEMA.values():
        keys.update(schema)

    return keys


@pytest.fixture
def registry(monkeypatch):
    """A fresh registry patched where the handler resolves it."""
    fresh = ProviderMetrics()
    monkeypatch.setattr(observability, "provider_metrics", fresh)
    return fresh


@pytest.fixture
def client():
    test_client = TestClient(app)

    try:
        yield test_client
    finally:
        app.dependency_overrides.pop(admin_router.metrics_access, None)
        app.dependency_overrides.pop(get_db, None)


# -- structure / redaction walk ------------------------------------------


def _assert_snapshot_shape(payload: dict) -> None:
    """Recursive structural check of the whole payload.

    Every key must belong to the bounded sets above and every label must
    use only the bounded key and value vocabularies.
    """
    assert set(payload) == _TOP_KEYS

    # The expected label-key vocabulary and the schema-derived one agree.
    assert _label_vocabulary_keys() == _LABEL_KEYS

    allowed_label_keys = _label_vocabulary_keys()
    allowed_label_values = _bounded_label_values()

    for entries in payload["counters"].values():
        assert isinstance(entries, list)

        for entry in entries:
            assert set(entry) == _COUNTER_ENTRY_KEYS
            assert isinstance(entry["value"], int)
            assert set(entry["labels"]) <= allowed_label_keys

            for value in entry["labels"].values():
                assert isinstance(value, str)
                assert value in allowed_label_values

    for entries in payload["durations"].values():
        assert isinstance(entries, list)

        for stats in entries:
            assert set(stats) == _DURATION_ENTRY_KEYS
            assert isinstance(stats["count"], int)
            assert set(stats["labels"]) <= allowed_label_keys

            for value in stats["labels"].values():
                assert isinstance(value, str)
                assert value in allowed_label_values

            for bucket in stats["buckets"]:
                assert set(bucket) == _BUCKET_KEYS
                assert isinstance(bucket["count"], int)

    failures = payload["recording_failures"]
    assert isinstance(failures, dict)

    for name, count in failures.items():
        assert isinstance(name, str)
        assert isinstance(count, int)


# -- tests ---------------------------------------------------------------


class TestRouteContract:
    @staticmethod
    def _metrics_route():
        from fastapi.routing import APIRoute

        for route in admin_router.router.routes:
            if (
                isinstance(route, APIRoute)
                and route.path == "/admin/metrics"
                and "GET" in route.methods
            ):
                return route

        raise AssertionError("GET /admin/metrics route is not registered")

    def test_route_exists_and_is_admin_only(self):
        assert self._metrics_route().methods == {"GET"}

        # `require_role("admin")` is bound at import time; its closure
        # carries the trusted role tuple resolved server-side.
        cells = [
            cell.cell_contents
            for cell in admin_router.metrics_access.__closure__
        ]
        assert cells == [("admin",)]

    def test_unauthenticated_request_returns_401(
        self, client, registry, monkeypatch
    ):
        # The role dependency is NOT overridden: the real
        # `get_current_user` runs first (before the pool is ever used)
        # and refuses the request.
        app.dependency_overrides[get_db] = lambda: None

        response = client.get("/admin/metrics")

        assert response.status_code == 401
        assert response.json() == {
            "detail": "Missing Authorization header"
        }
        assert registry.snapshot().counters == {}

    def test_non_admin_role_returns_403(self, client, registry):
        def customer_access():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )

        app.dependency_overrides[admin_router.metrics_access] = customer_access

        response = client.get("/admin/metrics")

        assert response.status_code == 403
        assert response.json() == {"detail": "Insufficient permissions"}
        # The snapshot was never materialized for an refused caller.
        assert registry.snapshot().counters == {}

    def test_admin_role_returns_the_snapshot_schema(
        self, client, registry, monkeypatch
    ):
        # Seed through the real recording API, including at least one
        # recording failure (a labels schema violation is swallowed and
        # counted, never raised) so the failure counters are exercised.
        registry.record_counter("rag_requests_total", {"outcome": "succeeded"})
        registry.record_duration(
            "rag_request_seconds", {"outcome": "succeeded"}, 0.012
        )
        registry.record_counter("rag_requests_total", {"bogus": "value"})

        app.dependency_overrides[
            admin_router.metrics_access
        ] = lambda: {"role": "admin"}

        response = client.get("/admin/metrics")

        assert response.status_code == 200

        payload = response.json()
        _assert_snapshot_shape(payload)

        assert "bogus" not in response.text
        assert payload["recording_failures"]["rag_requests_total"] == 1

        succeeded_counters = payload["counters"]["rag_requests_total"]
        assert succeeded_counters == [
            {"labels": {"outcome": "succeeded"}, "value": 1}
        ]

        durations = payload["durations"]["rag_request_seconds"]
        assert durations[0]["labels"] == {"outcome": "succeeded"}
        assert durations[0]["count"] == 1
        # The final bucket is always the total count.
        assert durations[0]["buckets"][-1] == {"boundary": "+Inf", "count": 1}

    def test_snapshot_failure_never_returns_a_successful_empty_response(
        self, client, registry, monkeypatch
    ):
        app.dependency_overrides[
            admin_router.metrics_access
        ] = lambda: {"role": "admin"}

        class _BrokenRegistry:
            def snapshot(self):
                raise RuntimeError("registry exploded")

        monkeypatch.setattr(observability, "provider_metrics", _BrokenRegistry)

        # Unhandled server exceptions become 500s instead of raised
        # exceptions. The client is deliberately NOT entered as a
        # context manager: entering the lifespan would poison other
        # suites with a shut-down `app.state.provider_limiters`.
        leaking_client = TestClient(app, raise_server_exceptions=False)

        response = leaking_client.get("/admin/metrics")

        assert response.status_code >= 500
        assert response.status_code != 200
        # The response is an error rendering, never an empty JSON
        # success or an empty snapshot payload.
        assert response.text.strip() != ""


class TestMetricsPayload:
    def test_empty_snapshot_renders_empty_collections(self):
        snapshot = ProviderMetrics().snapshot()

        payload = metrics_payload(snapshot)

        assert payload == {
            "counters": {},
            "durations": {},
            "recording_failures": {},
        }

    def test_populated_snapshot_renders_full_bucket_ladder(self):
        registry = ProviderMetrics()

        registry.record_counter(
            "provider_admissions_total",
            {
                "operation": "query_embedding",
                "resource": "embedding",
                "admission_outcome": "acquired",
            },
        )
        # Two observations, one above the 0.5 boundary, one below 0.05.
        registry.record_duration(
            "rag_request_seconds", {"outcome": "succeeded"}, 0.6
        )
        registry.record_duration(
            "rag_request_seconds", {"outcome": "succeeded"}, 0.02
        )

        payload = metrics_payload(registry.snapshot())

        assert payload["counters"]["provider_admissions_total"] == [
            {
                "labels": {
                    "operation": "query_embedding",
                    "resource": "embedding",
                    "admission_outcome": "acquired",
                },
                "value": 1,
            }
        ]

        [stats] = payload["durations"]["rag_request_seconds"]
        assert stats["labels"] == {"outcome": "succeeded"}
        assert stats["count"] == 2
        assert stats["sum_seconds"] == pytest.approx(0.62)
        assert stats["min_seconds"] == pytest.approx(0.02)
        assert stats["max_seconds"] == pytest.approx(0.6)

        boundaries = [bucket["boundary"] for bucket in stats["buckets"]]
        # The ten finite boundaries plus the derived final bucket.
        assert boundaries == [
            "0.05",
            "0.1",
            "0.25",
            "0.5",
            "1.0",
            "2.5",
            "5.0",
            "10.0",
            "30.0",
            "60.0",
            "+Inf",
        ]

        # Cumulative buckets: 0.02 is inside the first, 0.6 up to 1.0.
        assert stats["buckets"][0] == {"boundary": "0.05", "count": 1}
        assert stats["buckets"][3] == {"boundary": "0.5", "count": 1}
        assert stats["buckets"][4] == {"boundary": "1.0", "count": 2}
        # The final +Inf bucket always equals the total count.
        assert stats["buckets"][-1] == {"boundary": "+Inf", "count": 2}

    def test_payload_never_touches_the_live_registry(self):
        registry = ProviderMetrics()

        snapshot = registry.snapshot()

        payload = metrics_payload(snapshot)

        # Mutating the rendered payload cannot corrupt the snapshot, and
        # the empty registry was neither queried nor mutated again.
        payload["counters"]["injected"] = [{"labels": {}, "value": 0}]

        assert registry.snapshot().counters == {}
        assert "injected" not in snapshot.counters
