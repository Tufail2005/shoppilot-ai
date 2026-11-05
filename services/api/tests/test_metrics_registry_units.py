"""Phase 6.8 unit tests: the in-process metrics registry.

Covers (Plan v3 section 5, correction C6; registry only â€” no call-site
instrumentation, no admin endpoint):

- counter increments and label/schema validation,
- the explicit ``unattributed`` value for internal operations,
- duration count / sum / min / max statistics,
- cumulative histogram buckets and the ``+Inf == count`` invariant,
- overflow observations above the last boundary,
- thread safety of recording and snapshot consistency under concurrent
  recording,
- best-effort recording (never raises, counts failures),
- visible snapshot failure (never an empty, misleading snapshot).

All tests are offline: no database, no network. Each test builds its
own ``ProviderMetrics()`` â€” the module-level ``provider_metrics``
singleton is never touched here.
"""

import threading

import pytest

from app.core.observability import (
    DURATION_BUCKET_BOUNDARIES,
    DurationStats,
    ProviderMetrics,
    _labels_key,
    attempts_label,
)


def _counter_value(snapshot, series, **labels):
    entries = snapshot.counters.get(series, [])
    for entry in entries:
        if entry["labels"] == labels:
            return entry["value"]
    return None


def _duration_stats(snapshot, series, **labels) -> DurationStats | None:
    for stats in snapshot.durations.get(series, []):
        if stats.labels == labels:
            return stats
    return None


def _bucket(stats: DurationStats, key: str) -> int:
    return dict(stats.buckets)[key]


class TestCounterRecording:
    def test_counter_increments_per_label_combination(self):
        registry = ProviderMetrics()

        for _ in range(3):
            registry.record_counter(
                "rag_requests_total", {"outcome": "succeeded"}
            )
        registry.record_counter(
            "rag_requests_total", {"outcome": "caller_cancelled"}
        )
        registry.record_counter(
            "rag_requests_total", {"outcome": "succeeded"}, delta=4
        )

        snapshot = registry.snapshot()

        assert (
            _counter_value(snapshot, "rag_requests_total", outcome="succeeded")
            == 7
        )
        assert (
            _counter_value(
                snapshot, "rag_requests_total", outcome="caller_cancelled"
            )
            == 1
        )

    def test_unknown_series_is_rejected_by_validation(self):
        registry = ProviderMetrics()

        registry.record_counter("made_up_series_total", {})

        snapshot = registry.snapshot()
        assert snapshot.counters == {}  # nothing recorded
        assert snapshot.recording_failures == {"made_up_series_total": 1}

        with pytest.raises(ValueError):
            _labels_key("made_up_series_total", {})

    def test_wrong_missing_and_out_of_vocabulary_labels_are_rejected(self):
        # wrong key
        with pytest.raises(ValueError):
            _labels_key(
                "rag_requests_total", {"outcome": "succeeded", "kind": "fatal"}
            )

        # missing key
        with pytest.raises(ValueError):
            _labels_key("rag_requests_total", {})

        # out-of-vocabulary value
        with pytest.raises(ValueError):
            _labels_key("rag_requests_total", {"outcome": "exploded"})

        # non-string value (bools/ints are never label values)
        with pytest.raises(ValueError):
            _labels_key("rag_requests_total", {"outcome": 5})

        with pytest.raises(TypeError):
            _labels_key("rag_requests_total", ["not", "a", "dict"])

    def test_duration_series_and_counter_series_are_type_checked(self):
        registry = ProviderMetrics()

        # Recording a counter into a duration series (and vice versa)
        # is a programming error, caught without raising.
        registry.record_counter(
            "rag_request_seconds", {"outcome": "succeeded"}
        )
        registry.record_duration(
            "rag_requests_total", {"outcome": "succeeded"}, 0.1
        )

        snapshot = registry.snapshot()
        assert snapshot.counters == {}
        assert snapshot.durations == {}
        assert snapshot.recording_failures == {
            "rag_request_seconds": 1,
            "rag_requests_total": 1,
        }

    def test_zero_negative_and_non_integer_deltas_are_rejected(self):
        registry = ProviderMetrics()

        for bad_delta in (0, -2):
            registry.record_counter(
                "rag_requests_total",
                {"outcome": "succeeded"},
                delta=bad_delta,
            )

        registry.record_counter(
            "rag_requests_total", {"outcome": "succeeded"}, delta=False
        )
        registry.record_counter(
            "rag_requests_total", {"outcome": "succeeded"}, delta=1.5
        )

        snapshot = registry.snapshot()
        assert snapshot.counters == {}
        assert snapshot.recording_failures["rag_requests_total"] == 4

    def test_attempts_label_vocabulary_is_bounded(self):
        assert attempts_label(1) == "1"
        assert attempts_label(2) == "2"
        assert attempts_label(3) == "3"
        assert attempts_label(4) == "4"
        assert attempts_label(5) == "5+"
        assert attempts_label(17) == "5+"

        with pytest.raises(ValueError):
            attempts_label(0)

        with pytest.raises(ValueError):
            attempts_label(True)

        with pytest.raises(ValueError):
            attempts_label("3")

    def test_retry_family_counter_semantics(self):
        registry = ProviderMetrics()

        def record_call(retry_kinds, final_outcome):
            """Simulate the corrected counter semantics of one call.

            - every logical call increments provider_retry_calls_total,
            - "attempts" labels are TOTAL attempts (initial + retries),
            - a first-attempt success (zero retries) is counted ONLY in
              provider_retry_calls_total: provider_retry_success_total
              is reserved for calls recovered after at least one retry,
            - only failed attempts that actually trigger a retry
              increment provider_retry_attempts_total,
            - provider_retry_exhausted_total counts ONLY calls whose
              retry budget is spent ("exhausted"): a non-retriable
              final failure ("fatal"/"timeout") is a non-retryable
              exit, NOT exhaustion, and records nothing beyond its
              call/attempt counters.
            """
            registry.record_counter(
                "provider_retry_calls_total", {"operation": "query_embedding"}
            )

            # A non-retriable final failure ends the call before any
            # retry, but the attempt budget itself is never spent, so
            # no retriable failed attempts can precede it either.
            if final_outcome in ("fatal", "timeout"):
                return

            # Each failed attempt triggers its retry under that kind.
            for kind in retry_kinds:
                registry.record_counter(
                    "provider_retry_attempts_total",
                    {"operation": "query_embedding", "kind": kind},
                )

            if final_outcome == "success":
                if retry_kinds:  # recovered after at least one retry
                    registry.record_counter(
                        "provider_retry_success_total",
                        {
                            "operation": "query_embedding",
                            "attempts": attempts_label(len(retry_kinds) + 1),
                        },
                    )
            elif final_outcome == "exhausted":
                registry.record_counter(
                    "provider_retry_exhausted_total",
                    {
                        "operation": "query_embedding",
                        "kind": "transient",
                        "attempts": attempts_label(len(retry_kinds) + 1),
                    },
                )

        # 1) First-try success: one logical call, ZERO retry attempts,
        #    and provider_retry_success_total is NEVER incremented.
        record_call([], "success")
        # 2) Success on the third attempt: recovered after two retries.
        record_call(["rate_limited", "transient"], "success")
        # 3) Retry budget spent on the third attempt (a retriable kind
        #    whose next attempt the budget denies).
        record_call(["rate_limited", "transient"], "exhausted")
        # 4) An immediate non-retriable fatal exit is NOT exhaustion:
        #    only the logical call is counted.
        record_call([], "fatal")

        snapshot = registry.snapshot()

        # calls: every logical call is counted by operation.
        assert (
            _counter_value(
                snapshot, "provider_retry_calls_total", operation="query_embedding"
            )
            == 4
        )
        # attempts: only failed attempts that trigger a retry, per kind.
        assert (
            _counter_value(
                snapshot,
                "provider_retry_attempts_total",
                operation="query_embedding",
                kind="rate_limited",
            )
            == 2
        )
        assert (
            _counter_value(
                snapshot,
                "provider_retry_attempts_total",
                operation="query_embedding",
                kind="transient",
            )
            == 2
        )
        assert (
            _counter_value(
                snapshot,
                "provider_retry_attempts_total",
                operation="query_embedding",
                kind="timeout",
            )
            is None  # no timeout retry was ever recorded
        )
        # success: ONLY calls recovered after at least one retry, per
        # TOTAL attempts. The first-try success must be absent.
        assert (
            _counter_value(
                snapshot,
                "provider_retry_success_total",
                operation="query_embedding",
                attempts="1",
            )
            is None  # first-attempt successes are never recorded here
        )
        assert (
            _counter_value(
                snapshot,
                "provider_retry_success_total",
                operation="query_embedding",
                attempts="3",
            )
            == 1
        )
        assert snapshot.counters["provider_retry_success_total"] == [
            {
                "labels": {"attempts": "3", "operation": "query_embedding"},
                "value": 1,
            }
        ]
        # exhausted: the ONLY recorded series entry is the budget-spent
        # call (retriable kind + total attempts); the non-retriable
        # fatal exit above records nothing here.
        assert snapshot.counters["provider_retry_exhausted_total"] == [
            {
                "labels": {
                    "attempts": "3",
                    "kind": "transient",
                    "operation": "query_embedding",
                },
                "value": 1,
            }
        ]
        assert (
            _counter_value(
                snapshot,
                "provider_retry_exhausted_total",
                operation="query_embedding",
                kind="fatal",
                attempts="1",
            )
            is None  # non-retriable exits are never exhaustion
        )

    def test_retry_labels_beyond_the_bounded_vocabulary_fail(self):
        registry = ProviderMetrics()

        registry.record_counter(
            "provider_retry_success_total",
            {"operation": "query_embedding", "attempts": "7"},  # not "5+"
        )

        snapshot = registry.snapshot()
        assert snapshot.counters == {}
        assert snapshot.recording_failures["provider_retry_success_total"] == 1


class TestUnattributed:
    def test_unattributed_is_a_valid_operation(self):
        registry = ProviderMetrics()

        # Internal (non-request-path) work must be recorded with the
        # explicit unattributed OPERATION value, which validation admits.
        registry.record_counter(
            "provider_caller_outcomes_total",
            {"operation": "unattributed", "caller_outcome": "shutdown"},
        )

        snapshot = registry.snapshot()

        assert (
            _counter_value(
                snapshot,
                "provider_caller_outcomes_total",
                operation="unattributed",
                caller_outcome="shutdown",
            )
            == 1
        )

    def test_resource_labels_use_only_the_physical_resources(self):
        registry = ProviderMetrics()

        labels = {
            "operation": "query_embedding",
            "resource": "embedding",
            "admission_outcome": "acquired",
        }
        registry.record_counter("provider_admissions_total", labels)

        snapshot = registry.snapshot()
        assert _counter_value(snapshot, "provider_admissions_total", **labels) == 1

        # "unattributed" is reserved for the OPERATION label only; the
        # resource vocabulary is exactly the concurrency resources.
        for bad_resource in ("unattributed", "database", "gemini"):
            registry.record_counter(
                "provider_admissions_total",
                {
                    "operation": "query_embedding",
                    "resource": bad_resource,
                    "admission_outcome": "acquired",
                },
            )

        snapshot = registry.snapshot()
        assert snapshot.recording_failures["provider_admissions_total"] == 3

    def test_admission_outcome_vocabulary_is_metric_level(self):
        import app.core.observability as obs

        assert obs._ADMISSION_OUTCOMES == frozenset(
            {
                "acquired",
                "saturated",
                "caller_cancelled",
                "shutdown",
                "unexpected",
            }
        )

        registry = ProviderMetrics()

        # The internal _wait_for_permit tri-state is never exposed as
        # such: timed_out/failed must be mapped to "saturated" by the
        # instrumentation before recording.
        for internal in ("timed_out", "failed"):
            registry.record_counter(
                "provider_admissions_total",
                {
                    "operation": "query_embedding",
                    "resource": "embedding",
                    "admission_outcome": internal,
                },
            )

        snapshot = registry.snapshot()
        assert snapshot.counters == {}  # internal states are not accepted
        assert snapshot.recording_failures["provider_admissions_total"] == 2

    def test_ad_hoc_operation_values_are_not_admitted(self):
        registry = ProviderMetrics()

        # An invented value instead of "unattributed" is validation
        # failure -> recording failure, never cardinality growth.
        registry.record_counter(
            "provider_caller_outcomes_total",
            {"operation": "startup_script", "caller_outcome": "unexpected"},
        )

        snapshot = registry.snapshot()
        assert snapshot.counters == {}
        assert snapshot.recording_failures[
            "provider_caller_outcomes_total"
        ] == 1


class TestDurationRecording:
    def test_count_sum_min_max(self):
        registry = ProviderMetrics()

        labels = {
            "operation": "query_embedding",
            "resource": "embedding",
            "worker_outcome": "succeeded",
        }
        for seconds in (0.2, 0.1, 0.7):
            registry.record_duration(
                "provider_worker_seconds", labels, seconds
            )

        stats = _duration_stats(
            registry.snapshot(), "provider_worker_seconds", **labels
        )

        assert stats is not None
        assert stats.count == 3
        assert stats.sum_seconds == pytest.approx(1.0)
        assert stats.min_seconds == pytest.approx(0.1)
        assert stats.max_seconds == pytest.approx(0.7)

    def test_invalid_durations_are_not_recorded(self):
        registry = ProviderMetrics()

        labels = {
            "operation": "ingest_document",
            "resource": "embedding",
            "worker_outcome": "unexpected",
        }
        for bad in (-0.5, float("nan"), float("inf"), "0.3", True):
            registry.record_duration("provider_worker_seconds", labels, bad)

        snapshot = registry.snapshot()
        assert snapshot.durations == {}
        assert (
            snapshot.recording_failures["provider_worker_seconds"]
            == 5
        )


class TestHistogramBuckets:
    def test_buckets_are_cumulative(self):
        registry = ProviderMetrics()

        labels = {"outcome": "succeeded"}
        registry.record_duration("rag_request_seconds", labels, 0.06)
        registry.record_duration("rag_request_seconds", labels, 0.04)

        stats = _duration_stats(registry.snapshot(), "rag_request_seconds", **labels)

        assert stats is not None
        # 0.04 <= 0.05 counts in EVERY bucket; 0.06 counts only from
        # the 0.1 bucket upward.
        assert _bucket(stats, "0.05") == 1
        assert _bucket(stats, "0.1") == 2
        assert _bucket(stats, "60.0") == 2

    def test_inf_bucket_equals_count(self):
        registry = ProviderMetrics()

        labels = {"outcome": "succeeded"}
        for seconds in (0.001, 0.2, 3.0, 45.0):
            registry.record_duration("rag_request_seconds", labels, seconds)

        stats = _duration_stats(registry.snapshot(), "rag_request_seconds", **labels)

        assert stats is not None
        assert stats.buckets[-1] == ("+Inf", 4)
        assert stats.count == 4
        assert _bucket(stats, "+Inf") == stats.count

    def test_boundaries_are_exactly_documented(self):
        assert DURATION_BUCKET_BOUNDARIES == (
            0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0,
        )

    def test_observations_larger_than_the_last_boundary_overflow(self):
        registry = ProviderMetrics()

        labels = {"outcome": "failed"}
        registry.record_duration("rag_request_seconds", labels, 60.0)
        registry.record_duration("rag_request_seconds", labels, 60.001)
        registry.record_duration("rag_request_seconds", labels, 500.0)

        stats = _duration_stats(registry.snapshot(), "rag_request_seconds", **labels)

        assert stats is not None
        # 60.0 is inside the last boundary; larger ones overflow.
        assert _bucket(stats, "60.0") == 1
        # Cumulative semantics: the two overflow observations touch NO
        # finite bucket at all, so every bucket below 60.0 stays 0
        # (nothing smaller was ever observed).
        for boundary in DURATION_BUCKET_BOUNDARIES[:-1]:
            assert _bucket(stats, str(boundary)) == 0

        assert _bucket(stats, "+Inf") == stats.count == 3

        # Overflow observations are still fully counted in the stats.
        assert stats.max_seconds == pytest.approx(500.0)
        assert stats.sum_seconds == pytest.approx(620.001)


class TestThreadSafety:
    def test_concurrent_counter_and_duration_recording(self):
        registry = ProviderMetrics()
        threads_count = 8
        iterations = 250

        labels_count = {"outcome": "succeeded"}
        labels_duration = {"outcome": "succeeded"}

        def recorder():
            for _ in range(iterations):
                registry.record_counter("rag_requests_total", labels_count)
                registry.record_duration(
                    "rag_request_seconds", labels_duration, 0.001
                )

        threads = [
            threading.Thread(target=recorder) for _ in range(threads_count)
        ]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        snapshot = registry.snapshot()

        assert (
            _counter_value(snapshot, "rag_requests_total", outcome="succeeded")
            == threads_count * iterations
        )

        stats = _duration_stats(
            snapshot, "rag_request_seconds", **labels_duration
        )
        assert stats is not None
        assert stats.count == threads_count * iterations
        assert _bucket(stats, "+Inf") == stats.count  # invariant holds
        assert stats.min_seconds == pytest.approx(0.001)
        assert stats.max_seconds == pytest.approx(0.001)

    def test_snapshots_are_never_torn_while_recording(self):
        registry = ProviderMetrics()
        stop = threading.Event()
        errors: list[str] = []

        labels_count = {"outcome": "failed"}
        labels_duration = {"outcome": "succeeded"}

        def recorder():
            while not stop.is_set():
                registry.record_counter("rag_requests_total", labels_count)
                registry.record_duration("rag_request_seconds", labels_duration, 0.01)

        def monitor():
            previous: dict[tuple, int] = {}

            while not stop.is_set():
                snapshot = registry.snapshot()

                for stats in snapshot.durations.get("rag_request_seconds", []):
                    counts = [count for _, count in stats.buckets]

                    # +Inf == count; and cumulative buckets are
                    # monotonically non-increasing left to right.
                    if stats.buckets[-1] != ("+Inf", stats.count):
                        errors.append("inf/count mismatch")
                    if any(
                        later < earlier
                        for earlier, later in zip(counts, counts[1:])
                    ):
                        errors.append("buckets not monotone")

                for entry in snapshot.counters.get(
                    "rag_requests_total", []
                ):
                    key = tuple(entry["labels"].items())
                    if entry["value"] < previous.get(key, 0):
                        errors.append("counter went backwards")
                    previous[key] = entry["value"]

        recorder_thread = threading.Thread(target=recorder)
        monitor_thread = threading.Thread(target=monitor)
        recorder_thread.start()
        monitor_thread.start()

        try:
            threading.Event().wait(0.2)
        finally:
            stop.set()
            recorder_thread.join()
            monitor_thread.join()

        assert errors == []


class TestBestEffortRecording:
    def test_internal_counter_failure_never_reaches_the_caller(
        self, monkeypatch
    ):
        import app.core.observability as obs

        registry = ProviderMetrics()

        # Simulate a recording-path failure (the delta-check helper
        # explodes) WITHOUT corrupting state snapshot() depends on.
        def boom(delta):
            raise RuntimeError("delta check exploded")

        monkeypatch.setattr(obs, "_validated_delta", boom)

        registry.record_counter("rag_requests_total", {"outcome": "succeeded"})

        snapshot = registry.snapshot()

        assert snapshot.counters == {}  # nothing recorded
        assert snapshot.recording_failures == {"rag_requests_total": 1}

    def test_internal_duration_failure_never_reaches_the_caller(
        self, monkeypatch
    ):
        import app.core.observability as obs

        registry = ProviderMetrics()

        def boom(seconds):
            raise RuntimeError("duration validation exploded")

        monkeypatch.setattr(obs, "_validated_seconds", boom)

        registry.record_duration(
            "rag_request_seconds", {"outcome": "succeeded"}, 0.1
        )

        snapshot = registry.snapshot()

        assert snapshot.durations == {}  # nothing recorded
        assert snapshot.recording_failures == {"rag_request_seconds": 1}

    def test_recording_failures_accumulate_per_series(self):
        registry = ProviderMetrics()

        registry.record_counter("rag_requests_total", {"outcome": "exploded"})
        registry.record_counter("other_series", {})

        snapshot = registry.snapshot()

        assert snapshot.recording_failures == {
            "rag_requests_total": 1,
            "other_series": 1,
        }


class TestCallerDuration:
    def test_provider_call_seconds_count_sum_min_max(self):
        registry = ProviderMetrics()

        labels = {"operation": "query_embedding", "caller_outcome": "succeeded"}
        for seconds in (0.3, 0.1, 0.9):
            registry.record_duration("provider_call_seconds", labels, seconds)

        stats = _duration_stats(registry.snapshot(), "provider_call_seconds", **labels)

        assert stats is not None
        assert stats.count == 3
        assert stats.sum_seconds == pytest.approx(1.3)
        assert stats.min_seconds == pytest.approx(0.1)
        assert stats.max_seconds == pytest.approx(0.9)
        assert stats.buckets[-1] == ("+Inf", 3)
        assert _bucket(stats, "0.25") == 1  # only the 0.1 observation
        assert _bucket(stats, "0.5") == 2   # 0.1 and 0.3
        assert _bucket(stats, "1.0") == 3   # all three

    def test_caller_outcome_per_label_combination_is_separate(self):
        registry = ProviderMetrics()

        registry.record_duration(
            "provider_call_seconds",
            {"operation": "query_embedding", "caller_outcome": "succeeded"},
            0.2,
        )
        registry.record_duration(
            "provider_call_seconds",
            {"operation": "query_embedding", "caller_outcome": "saturated"},
            2.0,
        )
        registry.record_duration(
            "provider_call_seconds",
            {"operation": "unattributed", "caller_outcome": "shutdown"},
            0.0,
        )

        snapshot = registry.snapshot()
        assert len(snapshot.durations["provider_call_seconds"]) == 3


class TestInventoryConsistency:
    """Every metric name, label, value, and boundary in one place."""

    def test_complete_plan_v3_inventory_of_16_series(self):
        import app.core.observability as obs

        assert set(obs._SERIES_SCHEMA) == {
            # admission family
            "provider_admissions_total",
            "provider_admission_wait_seconds",
            # caller family
            "provider_caller_outcomes_total",
            "provider_call_seconds",
            # worker family
            "provider_worker_outcomes_total",
            "provider_worker_seconds",
            # retry family
            "provider_retry_calls_total",
            "provider_retry_attempts_total",
            "provider_retry_success_total",
            "provider_retry_exhausted_total",
            # RAG family
            "rag_requests_total",
            "rag_request_seconds",
            # ingestion-pipeline family
            "ingestion_pipelines_total",
            "ingestion_pipeline_seconds",
            "ingestion_failure_stages_total",
            # document-lifecycle family
            "document_lifecycle_outcomes_total",
        }
        assert len(obs._SERIES_SCHEMA) == 16

        assert obs._DURATION_SERIES == frozenset(
            {
                "provider_admission_wait_seconds",
                "provider_call_seconds",
                "provider_worker_seconds",
                "rag_request_seconds",
                "ingestion_pipeline_seconds",
            }
        )
        # The other 11 series are counters.
        assert len(obs._SERIES_SCHEMA) - len(obs._DURATION_SERIES) == 11

    def test_every_bounded_vocabulary_matches_the_contract(self):
        import app.core.observability as obs

        assert obs._OPERATION_LABELS == frozenset(
            {"query_embedding", "ingestion_embedding", "rag_generation", "unattributed"}
        )
        assert obs._RESOURCE_LABELS == frozenset(
            {"embedding", "generation", "ingestion_gate"}
        )
        assert obs._ADMISSION_OUTCOMES == frozenset(
            {
                "acquired",
                "saturated",
                "caller_cancelled",
                "shutdown",
                "unexpected",
            }
        )
        assert obs._CALLER_OUTCOMES == frozenset(
            {
                "succeeded",
                "provider_failure",
                "caller_cancelled",
                "saturated",
                "shutdown",
                "unexpected",
            }
        )
        assert obs._WORKER_OUTCOMES == frozenset(
            {
                "succeeded",
                "timeout",
                "rate_limited",
                "transient",
                "fatal",
                "unexpected",
                "worker_aborted",
                "shutdown",
            }
        )
        assert obs._KIND_LABELS == frozenset(
            {"timeout", "rate_limited", "transient", "fatal"}
        )
        assert obs._ATTEMPTS_LABELS == frozenset(
            {"1", "2", "3", "4", "5+"}
        )
        assert obs._RAG_OUTCOMES == frozenset(
            {"succeeded", "failed", "caller_cancelled"}
        )
        assert obs._INGESTION_OUTCOMES == obs._RAG_OUTCOMES
        assert obs._INGESTION_STAGE_LABELS == frozenset(
            {
                "storage_download",
                "text_extraction",
                "chunking",
                "embedding_validation",
                "embedding_saturated",
                "embedding_generation",
                "database_insertion",
            }
        )
        assert obs._DOCUMENT_LIFECYCLE_OUTCOMES == frozenset(
            {"ready", "failed", "conflict", "cancelled", "unexpected"}
        )

    def test_every_series_accepts_one_observation_from_its_schema(self):
        """Schema constants, snapshot output, and recording agree."""
        import app.core.observability as obs

        registry = ProviderMetrics()

        for series, label_schema in obs._SERIES_SCHEMA.items():
            labels = {
                name: sorted(vocabulary)[0]
                for name, vocabulary in label_schema.items()
            }

            if series in obs._DURATION_SERIES:
                registry.record_duration(series, dict(labels), 0.01)
            else:
                registry.record_counter(series, dict(labels))

        snapshot = registry.snapshot()

        assert snapshot.recording_failures == {}
        assert set(snapshot.counters) | set(snapshot.durations) == set(
            obs._SERIES_SCHEMA
        )
        # Exact one-entry-per-series shape, with the schema's own labels.
        for series in snapshot.counters:
            assert snapshot.counters[series][0]["value"] == 1
            assert snapshot.counters[series][0]["labels"] == {
                name: sorted(vocabulary)[0]
                for name, vocabulary in obs._SERIES_SCHEMA[series].items()
            }
        for series in snapshot.durations:
            entry = snapshot.durations[series][0]
            assert entry.count == 1
            assert entry.buckets[-1] == ("+Inf", 1)
            assert entry.labels == {
                name: sorted(vocabulary)[0]
                for name, vocabulary in obs._SERIES_SCHEMA[series].items()
            }

class TestSnapshotFailure:
    def test_snapshot_failure_is_visible_not_empty(self):
        registry = ProviderMetrics()

        registry.record_counter("rag_requests_total", {"outcome": "succeeded"})

        def broken_copy():
            raise RuntimeError("registry state copy failed")

        registry._copy_state = broken_copy  # simulate internal breakage

        with pytest.raises(RuntimeError):
            registry.snapshot()

    def test_snapshot_contains_only_series_that_were_recorded(self):
        registry = ProviderMetrics()

        snapshot = registry.snapshot()

        # An untouched registry snapshots as a coherent empty copy â€”
        # not silently "failed", and not misleading either (nothing
        # was ever recorded into it).
        assert snapshot.counters == {}
        assert snapshot.durations == {}
        assert snapshot.recording_failures == {}
