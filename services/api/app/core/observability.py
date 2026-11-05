"""Observability foundation for Phase 6.8 (steps 1-2).

Scope of this module (Plan v3, steps 1 and 2 only):

- the request ID context and the pure-ASGI correlation middleware,
- the structured JSON log formatter behind the ``LOG_FORMAT``
  setting, with an explicit structured-field allowlist,

Deliberately NOT in this module yet (later Phase 6.8 steps): the
provider/retry/concurrency/RAG/ingestion instrumentation at production
call sites, and the admin metrics endpoint. The in-process metrics
registry (:class:`ProviderMetrics`) is defined here — recording is
infrastructure, not instrumentation — but nothing records into it yet.

Correlation design (Plan v3 §4)
------------------------------

One server-generated request ID is assigned per HTTP request by
:class:`CorrelationMiddleware`, a pure ASGI middleware. A pure ASGI
middleware awaits the downstream app in the same task, so the context
variable is visible to every dependency and service coroutine below it
without ``BaseHTTPMiddleware``'s child-task copy quirks, and the
``finally`` reset covers success, exception, and cancellation equally.

``asyncio.to_thread`` copies the current ``contextvars`` at call time,
so provider worker threads see the same request ID in their log
records. Later instrumentation will additionally capture the request
ID explicitly at ``run_bounded`` entry (Plan v3 §6) and not rely on
ambient contextvars inside done callbacks.

Logging modes (Plan v3 §4.1, corrections C4)
--------------------------------------------

- ``"text"`` (default): no handler is installed at all — output is
  byte-identical to pre-6.8 behavior. INFO records remain invisible
  under a plain uvicorn runtime (no root handler exists); that is the
  preserved status quo, not a regression.
- ``"json"``: one stdout handler on the ``"app"`` logger renders every
  record as a single JSON object, with the request ID included when
  the filter has stamped the record. Uvicorn's own loggers are never
  touched. Any pre-existing handler on the ``"app"`` logger is muted
  while JSON mode is active (level ``CRITICAL + 1``), so output is
  never duplicated in mixed formats; its exact captured level is
  restored on uninstall.

Repeated JSON configuration is idempotent (at most one installed
handler; it is replaced, never stacked). Switching back to ``"text"``
restores the baseline captured at the most recent fresh install (the
state immediately before installing into an untouched logger): the
actual prior level, propagation flag, and handler list. Pre-existing
handlers are muted while JSON mode is active and restored to their
captured levels on uninstall.
"""

import contextvars
import datetime
import json
import logging
import math
import sys
import threading
import uuid
from dataclasses import dataclass

_REQUEST_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "shoppilot_request_id", default=None
)

# The logger this module owns. Every ``app.*`` module logger propagates
# to it, so one handler here formats the whole application.
_APP_LOGGER_NAME = "app"

# Marks the handler this module installed, so repeated configuration
# can find, replace, and remove exactly its own artifacts.
_HANDLER_MARKER = "_shoppilot_observability"

# While JSON mode is active every foreign handler on the ``"app"``
# logger is muted to this level — above every possible record level —
# so JSON mode never produces duplicate or mixed-format output.
_MUTED_LEVEL = logging.CRITICAL + 1

# Marks the baseline snapshot captured at the first JSON install
# (correction C4: restore the ACTUAL prior state, not assumed
# defaults).
_BASELINE: dict | None = None

_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Correlation context
# ---------------------------------------------------------------------------


def new_request_id() -> str:
    """Return one opaque, server-generated request correlation ID.

    The ID is derived from ``uuid4`` and carries no user data: it is
    correlation metadata only, never a metric label, and never echoed
    into responses by this phase.
    """
    return uuid.uuid4().hex


def current_request_id() -> str | None:
    """Return the current request ID, or ``None`` outside a request.

    ``None`` is also what one request task sees once the middleware has
    reset its token — the value cannot leak to the next request.
    """
    return _REQUEST_ID.get()


class CorrelationMiddleware:
    """Pure ASGI correlation middleware (one request ID per HTTP request).

    Non-HTTP scopes (lifespan, websocket) pass through without ever
    setting the context. Errors propagate unchanged: the reset runs in
    ``finally``, so cancellation and exceptions restore the prior
    context too.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        token = _REQUEST_ID.set(new_request_id())
        try:
            await self.app(scope, receive, send)
        finally:
            _REQUEST_ID.reset(token)


class _CorrelationFilter(logging.Filter):
    """Stamp the current request ID onto the record when one is active.

    Attached only to the JSON handler, so text mode never sees this
    attribute at all. Explicit ``request_id`` values already on a
    record (structured fields) are never overwritten.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # ``request_id`` is not a standard LogRecord attribute: use
        # getattr so explicit structured values are kept unstamped.
        if getattr(record, "request_id", None) is None:
            request_id = current_request_id()

            if request_id is not None:
                record.request_id = request_id

        return True


# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------

# Explicit allowlist (Plan v3 §4.2, correction C5): only these keys may
# reach output as structured ``event`` fields, and only with primitive
# value types. Anything else is dropped by the formatter.
ALLOWED_FIELD_KEYS = frozenset(
    {
        "request_id",
        "operation",
        "resource",
        "outcome",
        "kind",
        "stage",
        "attempts",
        "wait_seconds",
        "duration_seconds",
        "count",
        "error_type",
    }
)

_FALLBACK_MESSAGE = "Log serialization failed"


def _sanitized_event_value(value):
    """Return one allowlisted value, or ``None`` when it must be dropped.

    Accepted types: ``str``, finite ``int`` (never a bool), finite
    ``float`` (rounded, so a duration stays readable). Booleans,
    nested containers, non-finite numbers, and unhashable/exotic
    objects are all dropped.
    """
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (str, int)):
        return value

    if isinstance(value, float):
        if not math.isfinite(value):
            return None

        return round(value, 6)

    return None


class ObsJSONFormatter(logging.Formatter):
    """Render one ``LogRecord`` as exactly one JSON object per line.

    Output shape:

        {"time": "...", "level": "ERROR", "logger": "app.services.rag",
         "message": "...", "event": {...allowlisted...},
         "request_id": "..."}

    Redaction contract (correction C5):

    - ``getMessage()`` (the msg ``%``-formatted with its interpolation
      args) is rendered as-is; the args are trusted because they are
      pre-sanitized by the project convention (stage slugs, exception
      type names, counts, opaque UUIDs — an AST hygiene test enforces
      this), and the formatter never inspects or re-renders the raw
      ``record.args`` itself.
    - ``exc_info``/``exc_text``/``stack_info`` are NEVER serialized:
      tracebacks carry provider/database message text.
    - Structured context arrives only via the ``obs`` record attribute
      (``logger.info("...", extra={"obs": {...}})``), validated against
      :data:`ALLOWED_FIELD_KEYS` and :func:`_sanitized_event_value`.
    - ``format()`` never raises: any internal failure yields a static
      fallback line so a formatter bug cannot break logging itself.
    """

    def format(self, record: logging.LogRecord) -> str:
        try:
            return self._render(record)
        except Exception:
            return self._fallback(record)

    def _render(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        event = getattr(record, "obs", None)

        if isinstance(event, dict):
            rendered_event = {}
            # Sanitize FIRST, then drop empty results: a rejected value
            # (bool, container, non-finite float) must not leave its
            # key behind with a None payload.
            for key, raw_value in event.items():
                if key not in ALLOWED_FIELD_KEYS:
                    continue

                value = _sanitized_event_value(raw_value)

                if value is not None:
                    rendered_event[key] = value

            if rendered_event:
                payload["event"] = rendered_event

        request_id = self._request_id(record, event)

        if request_id is not None:
            payload["request_id"] = request_id

        return json.dumps(payload, separators=(",", ":"))

    @staticmethod
    def _request_id(record: logging.LogRecord, event) -> str | None:
        """The record's correlation ID.

        Priority: the structured ``obs`` field, the filter-stamped
        record attribute, then the ambient correlation context (the
        formatter is normally attached to a handler whose filter
        already ran, so the record attribute is the common path).
        """
        if isinstance(event, dict):
            candidate = _sanitized_event_value(event.get("request_id"))

            if candidate is not None and candidate != "":
                return candidate

        stamped = getattr(record, "request_id", None)

        if isinstance(stamped, str):
            return stamped

        return current_request_id()

    @staticmethod
    def _fallback(record: logging.LogRecord) -> str:
        """One line that is still parseable JSON, and never raises."""
        try:
            return json.dumps(
                {
                    "time": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": _FALLBACK_MESSAGE,
                },
                separators=(",", ":"),
            )
        except Exception:
            return '{"level": "ERROR", "message": "Log serialization failed"}'


def _mark(handler) -> None:
    setattr(handler, _HANDLER_MARKER, True)


def _installed_handlers(logger: logging.Logger) -> list:
    return [h for h in logger.handlers if getattr(h, _HANDLER_MARKER, False)]


def _make_handler() -> logging.StreamHandler:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ObsJSONFormatter())
    handler.addFilter(_CorrelationFilter())
    _mark(handler)

    return handler


def _install_json() -> None:
    global _BASELINE

    with _LOCK:
        logger = logging.getLogger(_APP_LOGGER_NAME)

        marked = _installed_handlers(logger)

        # Baseline captured at every FRESH install (no marked handler
        # present): the exact state immediately before installing into
        # this untouched logger. A ``json`` -> ``json`` re-install keeps
        # the existing baseline, so the true pre-observability state
        # survives any number of mode switches (correction C4).
        if not marked:
            _BASELINE = {
                "level": logger.level,
                "propagate": logger.propagate,
                "handlers": list(logger.handlers),
                # Exact levels of every foreign handler at capture
                # time, for faithful restoration after muting (never
                # assumed defaults).
                "handler_levels": [
                    (handler, handler.level)
                    for handler in logger.handlers
                    if not getattr(handler, _HANDLER_MARKER, False)
                ],
            }

        # Idempotency: remove what we installed before, never the
        # baseline handlers.
        for handler in marked:
            logger.removeHandler(handler)

        logger.addHandler(_make_handler())

        # No duplicate or mixed-format output: every handler we do not
        # own is muted for as long as JSON mode is active. The capture
        # above keeps their true levels for restoration. Muting is
        # idempotent for json -> json re-installs.
        for handler in logger.handlers:
            if not getattr(handler, _HANDLER_MARKER, False):
                handler.setLevel(_MUTED_LEVEL)

        if logger.level == logging.NOTSET:
            logger.setLevel(logging.INFO)

        logger.propagate = False


def _restore_text() -> None:
    with _LOCK:
        logger = logging.getLogger(_APP_LOGGER_NAME)

        marked = _installed_handlers(logger)

        for handler in marked:
            logger.removeHandler(handler)

        if not marked or _BASELINE is None:
            # Nothing of ours was installed in this cycle (pure text
            # mode, or text after text): a complete no-op, byte-identical
            # to pre-6.8 behavior. A stale baseline from an earlier
            # cycle is deliberately NOT re-applied here.
            return

        logger.setLevel(_BASELINE["level"])
        logger.propagate = _BASELINE["propagate"]

        # Faithfully restore every foreign handler we muted (only if
        # it is still attached; externally detached handlers are gone).
        present = {id(handler) for handler in logger.handlers}
        for handler, level in _BASELINE.get("handler_levels", ()):
            if id(handler) in present:
                handler.setLevel(level)
        # Handler rows other than our own are never added or removed in
        # either direction, so the captured list needs no restoration;
        # it is kept in `_BASELINE` only for tests that inspect it.


def configure_logging(log_format: str) -> None:
    """Apply the configured logging mode. Idempotent and switch-safe.

    ``"text"`` is the default and installs nothing (logging looks
    exactly like pre-6.8). ``"json"`` installs (or replaces) the
    single, marked JSON stdout handler on the ``"app"`` logger.
    Switching back to ``"text"`` removes our handler and restores the
    baseline captured at the most recent fresh install. When nothing of
    ours is installed, text mode is a complete no-op.

    Uvicorn's own logger configuration is never touched.
    """
    if log_format == "json":
        _install_json()
    elif log_format == "text":
        _restore_text()
    else:
        raise ValueError(
            f"log_format must be 'text' or 'json', got {log_format!r}"
        )


# ---------------------------------------------------------------------------
# In-process metrics registry (Plan v3 section 5, correction C6; step 3)
# ---------------------------------------------------------------------------

# Histogram boundaries for every duration series (seconds). Buckets are
# CUMULATIVE: a bucket counts observations <= its boundary. An
# observation larger than the last boundary lands only in the final
# "+Inf" bucket, which always equals the series' total count. Bucket
# keys are rendered as str(boundary): "0.05", "0.1", "0.25", "0.5",
# "1.0", "2.5", "5.0", "10.0", "30.0", "60.0", then "+Inf".
DURATION_BUCKET_BOUNDARIES = (
    0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0,
)
_INF_BUCKET_KEY = "+Inf"

# The deliberately unattributed value: internal work (startup,
# maintenance, anything that is not a request-path operation) MUST use
# this OPERATION value, never an ad-hoc one. It is valid for the
# operation label ONLY — resources are always one of the physical
# concurrency resources.
_UNATTRIBUTED = "unattributed"

# Bounded label vocabularies (Plan v3 section 5). An out-of-vocabulary
# value is a programming error at a call site: validation rejects it
# and the best-effort wrapper counts a recording failure. Vocabularies
# are duplicated from their owning modules (provider_retry's KIND_*,
# documents.py's ingestion stages) on purpose: those modules will
# import THIS module to instrument call sites, so observability must
# never import them back (acyclic graph).
_OPERATION_LABELS = frozenset(
    {
        "query_embedding",       # embed_query path
        "ingestion_embedding",   # embed_documents path
        "rag_generation",        # generation path
        _UNATTRIBUTED,
    }
)
_RESOURCE_LABELS = frozenset({"embedding", "generation", "ingestion_gate"})

# Admission outcomes: the METRIC-level vocabulary. The internal
# `_wait_for_permit` tri-state (acquired / timed out / failed) is never
# exposed as such: timeout and pending-cap refusal both map to
# "saturated"; the caller-drain distinctions map to caller_cancelled /
# shutdown / unexpected.
_ADMISSION_OUTCOMES = frozenset(
    {"acquired", "saturated", "caller_cancelled", "shutdown", "unexpected"}
)

# Caller-side outcomes are coarse buckets only (correction C1).
_CALLER_OUTCOMES = frozenset(
    {
        "succeeded",
        "provider_failure",
        "caller_cancelled",
        "saturated",
        "shutdown",
        "unexpected",
    }
)

# Worker-side outcomes: the retry layer's provider_outcome_of
# vocabulary plus the concurrency layer's worker-only outcomes.
_WORKER_OUTCOMES = frozenset(
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

# provider_retry.KIND_* vocabulary.
_KIND_LABELS = frozenset({"timeout", "rate_limited", "transient", "fatal"})

# Bounded "attempts" labels: exact counts 1-4, categorical "5+".
_ATTEMPTS_LABELS = frozenset({"1", "2", "3", "4", "5+"})

_RAG_OUTCOMES = frozenset({"succeeded", "failed", "caller_cancelled"})
_INGESTION_OUTCOMES = frozenset({"succeeded", "failed", "caller_cancelled"})

# documents.py's seven ingest_document-owned stage slugs (not
# request_cancelled: that is a documents-layer DB status, correction C3).
_INGESTION_STAGE_LABELS = frozenset(
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

_DOCUMENT_LIFECYCLE_OUTCOMES = frozenset(
    {"ready", "failed", "conflict", "cancelled", "unexpected"}
)

# The complete series schema: name -> {label name -> bounded vocabulary}.
# The admission / caller / worker / retry / RAG / ingestion-pipeline /
# document-lifecycle families are deliberately separate metric names.
# Full Plan v3 inventory: 11 counter + 5 duration series.
_SERIES_SCHEMA: dict[str, dict[str, frozenset[str]]] = {
    # admission family
    "provider_admissions_total": {
        "operation": _OPERATION_LABELS,
        "resource": _RESOURCE_LABELS,
        "admission_outcome": _ADMISSION_OUTCOMES,
    },
    "provider_admission_wait_seconds": {
        "operation": _OPERATION_LABELS,
        "resource": _RESOURCE_LABELS,
    },
    # caller family (coarse buckets only, correction C1)
    "provider_caller_outcomes_total": {
        "operation": _OPERATION_LABELS,
        "caller_outcome": _CALLER_OUTCOMES,
    },
    "provider_call_seconds": {
        "operation": _OPERATION_LABELS,
        "caller_outcome": _CALLER_OUTCOMES,
    },
    # worker family
    "provider_worker_outcomes_total": {
        "operation": _OPERATION_LABELS,
        "resource": _RESOURCE_LABELS,
        "worker_outcome": _WORKER_OUTCOMES,
    },
    "provider_worker_seconds": {
        "operation": _OPERATION_LABELS,
        "resource": _RESOURCE_LABELS,
        "worker_outcome": _WORKER_OUTCOMES,
    },
    # retry family. Counter semantics (Plan v3 section 5, corrected):
    # - provider_retry_calls_total counts every logical call entering
    #   the retry loop, by operation (first-attempt successes included),
    # - provider_retry_attempts_total counts ONLY failed attempts that
    #   actually trigger a retry, per failure kind — the initial
    #   attempt is NEVER counted as a retry,
    # - provider_retry_success_total counts ONLY successful logical
    #   calls recovered after at least one retry, per TOTAL attempts
    #   made (attempts >= 2; a first-attempt success is deliberately
    #   never recorded here),
    # - provider_retry_exhausted_total counts calls that exhausted the
    #   attempt budget, per final failure kind and total attempts
    #   (never a success).
    "provider_retry_calls_total": {
        "operation": _OPERATION_LABELS,
    },
    "provider_retry_attempts_total": {
        "operation": _OPERATION_LABELS,
        "kind": _KIND_LABELS,
    },
    "provider_retry_success_total": {
        "operation": _OPERATION_LABELS,
        "attempts": _ATTEMPTS_LABELS,
    },
    "provider_retry_exhausted_total": {
        "operation": _OPERATION_LABELS,
        "kind": _KIND_LABELS,
        "attempts": _ATTEMPTS_LABELS,
    },
    # RAG family
    "rag_requests_total": {"outcome": _RAG_OUTCOMES},
    "rag_request_seconds": {"outcome": _RAG_OUTCOMES},
    # ingestion-pipeline family (canonical names, correction C6)
    "ingestion_pipelines_total": {
        "ingestion_outcome": _INGESTION_OUTCOMES,
    },
    "ingestion_pipeline_seconds": {
        "ingestion_outcome": _INGESTION_OUTCOMES,
    },
    "ingestion_failure_stages_total": {"stage": _INGESTION_STAGE_LABELS},
    # document-lifecycle family
    "document_lifecycle_outcomes_total": {
        "document_lifecycle_outcome": _DOCUMENT_LIFECYCLE_OUTCOMES,
    },
}

_DURATION_SERIES = frozenset(
    {
        "provider_admission_wait_seconds",
        "provider_call_seconds",
        "provider_worker_seconds",
        "rag_request_seconds",
        "ingestion_pipeline_seconds",
    }
)


def attempts_label(attempts: int) -> str:
    """Return the bounded ``attempts`` label for an attempt count.

    Exact counts 1-4 keep their numeric label; anything at or above 5
    collapses into ``"5+"`` so the label cannot grow unboundedly.
    """
    if isinstance(attempts, bool) or not isinstance(attempts, int):
        raise ValueError(f"attempts must be an int, got {attempts!r}")

    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts!r}")

    if attempts <= 4:
        return str(attempts)

    return "5+"


def _labels_key(
    series: object, labels: object
) -> tuple[tuple[str, str], ...]:
    """Validate a series/label pair; return its canonical storage key.

    Raises on programming errors at call sites (caught by the
    best-effort wrappers afterwards): unknown series names, wrong
    label keys, missing labels, and out-of-vocabulary or non-string
    values are all rejected, so a bug can never grow an exported
    series' cardinality.
    """
    if not isinstance(series, str) or series not in _SERIES_SCHEMA:
        raise ValueError(f"unknown metric series: {series!r}")

    if not isinstance(labels, dict) or not all(
        isinstance(name, str) for name in labels
    ):
        raise TypeError(
            f"labels for {series!r} must be a dict of str -> str"
        )

    schema = _SERIES_SCHEMA[series]

    unknown_keys = sorted(set(labels) - set(schema))
    missing_keys = sorted(set(schema) - set(labels))

    if unknown_keys or missing_keys:
        raise ValueError(
            f"wrong labels for {series!r}: "
            f"unknown={unknown_keys}, missing={missing_keys}"
        )

    key_items: list[tuple[str, str]] = []

    for label_name in sorted(schema):
        value = labels[label_name]

        if not isinstance(value, str) or value not in schema[label_name]:
            raise ValueError(
                f"label {label_name!r} for {series!r} is out of "
                f"vocabulary: {value!r}"
            )

        key_items.append((label_name, value))

    return tuple(key_items)


def _validated_seconds(seconds: object) -> float:
    """Validate one duration observation (finite, >= 0)."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ValueError(f"duration must be a number, got {seconds!r}")

    value = float(seconds)

    if not math.isfinite(value) or value < 0.0:
        raise ValueError(f"duration must be finite and >= 0, got {value!r}")

    return round(value, 6)


def _validated_delta(delta: object) -> int:
    """Validate one counter increment (positive int, never bool)."""
    if isinstance(delta, bool) or not isinstance(delta, int) or delta <= 0:
        raise ValueError(
            f"counter delta must be a positive int, got {delta!r}"
        )

    return delta


class _DurationSeries:
    """One duration series' running statistics (one label combination).

    ``bucket_counts`` holds one entry per boundary in
    :data:`DURATION_BUCKET_BOUNDARIES`; each entry is cumulative
    (observations <= boundary). The final ``+Inf`` bucket is derived at
    copy time from the total count, so the ``+Inf == count`` invariant
    holds by construction and overflow observations need no slot.
    """

    __slots__ = (
        "count",
        "sum_seconds",
        "min_seconds",
        "max_seconds",
        "bucket_counts",
    )

    def __init__(self) -> None:
        self.count = 0
        self.sum_seconds = 0.0
        self.min_seconds: float | None = None
        self.max_seconds: float | None = None
        self.bucket_counts = [0] * len(DURATION_BUCKET_BOUNDARIES)

    def observe(self, seconds: float) -> None:
        self.count += 1
        self.sum_seconds += seconds

        if self.min_seconds is None or seconds < self.min_seconds:
            self.min_seconds = seconds

        if self.max_seconds is None or seconds > self.max_seconds:
            self.max_seconds = seconds

        for index, boundary in enumerate(DURATION_BUCKET_BOUNDARIES):
            if seconds <= boundary:
                for later in range(index, len(self.bucket_counts)):
                    self.bucket_counts[later] += 1
                return

        # Larger than every boundary: overflow. Only the derived final
        # "+Inf" bucket (== count) sees it.


@dataclass(frozen=True)
class DurationStats:
    """Copied statistics of one duration series' label combination."""

    labels: dict[str, str]
    count: int
    sum_seconds: float
    min_seconds: float | None
    max_seconds: float | None
    # Ordered cumulative buckets; the final entry is ("+Inf", count).
    buckets: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class MetricsSnapshot:
    """An atomic, point-in-time copy of every series (JSON-ready)."""

    counters: dict[str, list[dict[str, object]]]
    durations: dict[str, list[DurationStats]]
    recording_failures: dict[str, int]


class ProviderMetrics:
    """Thread-safe, per-process, in-memory metrics registry.

    Recording is BEST-EFFORT: every public recording method swallows
    its own validation and runtime failures, counting them under
    ``recording_failures`` in the snapshot, so a metrics bug can never
    change application behavior.

    Snapshots are the only read path: the state copy happens under the
    one registry lock (atomic), and the lock is released BEFORE the
    response object is built. ``snapshot()`` itself never swallows
    errors — a broken copy must fail visibly, never return an empty,
    misleading snapshot.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[tuple[tuple[str, str], ...], int]] = {}
        self._durations: dict[
            str, dict[tuple[tuple[str, str], ...], _DurationSeries]
        ] = {}
        self._recording_failures: dict[str, int] = {}

    # -- recording (best-effort; never raises) ---------------------------

    def record_counter(
        self, series: str, labels: dict[str, str], *, delta: int = 1
    ) -> None:
        try:
            if series in _DURATION_SERIES:
                raise ValueError(f"{series!r} is a duration series")

            key = _labels_key(series, labels)
            step = _validated_delta(delta)

            with self._lock:
                target = self._counters.setdefault(series, {})
                target[key] = target.get(key, 0) + step
        except Exception:
            self._note_recording_failure(series)

    def record_duration(
        self, series: str, labels: dict[str, str], seconds: object
    ) -> None:
        try:
            if series not in _DURATION_SERIES:
                raise ValueError(f"{series!r} is a counter series")

            value = _validated_seconds(seconds)
            key = _labels_key(series, labels)

            with self._lock:
                target = self._durations.setdefault(series, {})
                running = target.get(key)

                if running is None:
                    running = _DurationSeries()
                    target[key] = running

                running.observe(value)
        except Exception:
            self._note_recording_failure(series)

    def _note_recording_failure(self, series: object) -> None:
        # Itself more best-effort than anything it guards: never propagate.
        try:
            key = (
                series
                if isinstance(series, str) and series
                else _UNATTRIBUTED
            )

            with self._lock:
                self._recording_failures[key] = (
                    self._recording_failures.get(key, 0) + 1
                )
        except Exception:  # pragma: no cover - cannot practically happen
            pass

    # -- snapshot (atomic copy; visible failure) -------------------------

    def snapshot(self) -> MetricsSnapshot:
        """One atomic point-in-time copy of every series.

        The state copy happens under the single registry lock (so no
        torn state is observable); the lock is released BEFORE the
        response snapshot is built, so holding the copy never
        serializes against ongoing recording. Internal failures
        propagate (raise): never an empty, misleading snapshot.
        """
        copied = self._copy_state()

        counters: dict[str, list[dict[str, object]]] = {
            name: [
                {"labels": dict(key), "value": value}
                for key, value in series.items()
            ]
            for name, series in copied["counters"].items()
        }

        durations: dict[str, list[DurationStats]] = {
            name: [
                DurationStats(
                    labels=dict(key),
                    count=count,
                    sum_seconds=round(total, 6),
                    min_seconds=minimum,
                    max_seconds=maximum,
                    buckets=(
                        tuple(
                            (str(boundary), bucket_count)
                            for boundary, bucket_count in zip(
                                DURATION_BUCKET_BOUNDARIES, bucket_counts
                            )
                        )
                        + ((_INF_BUCKET_KEY, count),)
                    ),
                )
                for (
                    key,
                    count,
                    total,
                    minimum,
                    maximum,
                    bucket_counts,
                ) in series
            ]
            for name, series in copied["durations"].items()
        }

        return MetricsSnapshot(
            counters=counters,
            durations=durations,
            recording_failures=copied["recording_failures"],
        )

    def _copy_state(self) -> dict[str, object]:
        """Copy every series under the one registry lock (atomic)."""
        with self._lock:
            counters = {
                name: {key: value for key, value in series.items()}
                for name, series in self._counters.items()
            }
            durations = {
                name: [
                    (
                        key,
                        running.count,
                        running.sum_seconds,
                        running.min_seconds,
                        running.max_seconds,
                        list(running.bucket_counts),
                    )
                    for key, running in series.items()
                ]
                for name, series in self._durations.items()
            }
            recording_failures = dict(self._recording_failures)

        return {
            "counters": counters,
            "durations": durations,
            "recording_failures": recording_failures,
        }


# The per-process registry. Later Phase 6.8 steps instrument production
# call sites against this instance; recording into it is best-effort.
provider_metrics = ProviderMetrics()


def metrics_payload(snapshot: MetricsSnapshot) -> dict[str, object]:
    """Render one snapshot as the admin ``GET /admin/metrics`` payload.

    Aggregate values ONLY, mirroring :class:`MetricsSnapshot` exactly:

        {
          "counters":  {series: [{"labels": {...}, "value": int}]},
          "durations": {series: [{"labels": {...}, "count": int,
                                  "sum_seconds": float,
                                  "min_seconds": float | null,
                                  "max_seconds": float | null,
                                  "buckets": [{"boundary": str,
                                               "count": int}]}]},
          "recording_failures": {series: int},
        }

    No user IDs, document IDs, request IDs, prompts, or provider error
    messages exist in the registry, so none can appear here: labels are
    bounded vocabulary values and every number is an aggregate over
    observations the registry holds internally as sums, counts, and
    extremes. Snapshot construction is fail-fast (it raises); this
    function only renders an already-copied snapshot and never touches
    the live registry.
    """
    durations = {
        name: [
            {
                "labels": dict(stats.labels),
                "count": stats.count,
                "sum_seconds": stats.sum_seconds,
                "min_seconds": stats.min_seconds,
                "max_seconds": stats.max_seconds,
                "buckets": [
                    {"boundary": boundary, "count": count}
                    for boundary, count in stats.buckets
                ],
            }
            for stats in series
        ]
        for name, series in snapshot.durations.items()
    }

    return {
        "counters": {
            name: [
                {
                    "labels": dict(entry["labels"]),
                    "value": entry["value"],
                }
                for entry in entries
            ]
            for name, entries in snapshot.counters.items()
        },
        "durations": durations,
        "recording_failures": dict(snapshot.recording_failures),
    }
