"""Provider timeout helpers for the Phase 6.6 reliability step.

Small, pure, offline helpers shared by the Gemini embedding and
generation providers:

- ``validate_timeout_seconds`` enforces the configured timeout values
  (finite, positive, numeric, never a bool) — pydantic ``Field(gt=0)``
  alone cannot reject ``inf`` (``inf > 0`` is ``True``), so the
  production call sites validate explicitly here as defense in depth.
- ``timeout_milliseconds`` converts a configured seconds value into the
  **milliseconds** integer that ``google.genai.types.HttpOptions``
  expects. The SDK uses a DIFFERENT unit per surface — ``HttpOptions``
  (embeddings) is milliseconds while ``client.interactions.create(...,
  timeout=...)`` (generation) is seconds — so every ms conversion must
  go through this helper.
- ``is_timeout_error`` classifies raised provider exceptions as "the
  provider did not answer in time" so they can be reported with a
  distinct, sanitized error stage instead of a generic outage.

Verified against the installed ``google-genai`` SDK (>=2.33,
2026-10):

- ``HttpOptions.timeout`` is an int in milliseconds
  (``google/genai/types.py`` field description),
- ``interactions.create`` accepts ``timeout: float | httpx.Timeout``
  in seconds and forwards it to the API client request,
- on a stalled connection the SDK does NOT let ``httpx.TimeoutException``
  escape unchanged: it wraps it in its own client-side
  ``compat_errors.APITimeoutError`` (an ``APIError`` subclass) and keeps
  the original ``httpx.ReadTimeout`` as ``__cause__``. That wrapper is
  **not** a ``TimeoutError`` — its MRO is
  ``APITimeoutError -> APIConnectionError -> APIError -> Exception`` —
  so a plain ``isinstance(exc, TimeoutError)`` check misses the most
  common real-world timeout. ``is_timeout_error`` therefore also walks
  the ``__cause__``/``__context__`` chain and recognises the wrapper by
  class name (measured against the installed SDK, 2026-10),
- the SDK's default retry policy is a single attempt
  (``stop_after_attempt(1)``), so a configured timeout is a genuine
  upper bound for one provider call. Retries are deliberately NOT
  enabled here — Phase 6.7 owns bounded retries.

This module never touches the network and never imports the Gemini
SDK, so it is safely unit-testable offline (``httpx`` is imported
lazily inside the classification function for exactly that reason).
"""

import math
import typing


def validate_timeout_seconds(value: object, *, name: str) -> float:
    """Validate one timeout/deadline value in seconds and return it as float.

    Accepts a finite, strictly positive real number. Rejected:
    ``None``, booleans (a ``bool`` is an ``int`` subclass in Python and
    ``True == 1.0`` would otherwise be accepted as a real timeout),
    strings, non-finite values (``nan``, ``inf``), zero, and negatives.

    ``pydantic``'s ``Field(gt=0)`` alone is NOT sufficient: ``inf > 0``
    is ``True``, so an ``inf`` configuration would pass schema
    validation and silently disable the bound. This check runs again at
    every production call site.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"{name} must be a positive number of seconds, "
            f"got {type(value).__name__}"
        )

    numeric = float(value)

    if not math.isfinite(numeric):
        raise ValueError(
            f"{name} must be a finite number of seconds, got {numeric!r}"
        )

    if numeric <= 0:
        raise ValueError(
            f"{name} must be a positive number of seconds, got {numeric!r}"
        )

    return numeric


def coerce_timeout_seconds(value: object, *, name: str) -> float:
    """Coerce a config-supplied timeout to a validated float in seconds.

    Configuration reaches the application from several sources: the
    typed defaults are floats, while environment variables (and
    ``.env`` entries) arrive as strings. This helper performs the
    string->float conversion that pydantic-settings cannot do before a
    ``mode="before"`` validator runs, then delegates every numeric rule
    (positive, finite, non-bool) to :func:`validate_timeout_seconds`.

    Rejected inputs (``ValueError``): ``None``, booleans, empty /
    whitespace-only / non-numeric strings, zero, negatives, ``nan``, and
    ``inf``. A boolean is deliberately NOT accepted as ``1``/``0`` even
    though ``bool`` is an ``int`` subclass.
    """
    if isinstance(value, str):
        text = value.strip()

        try:
            numeric = float(text)
        except ValueError:
            raise ValueError(
                f"{name} must be a positive number of seconds, "
                f"got {text!r}"
            ) from None

        return validate_timeout_seconds(numeric, name=name)

    return validate_timeout_seconds(value, name=name)


def timeout_milliseconds(seconds: float, *, name: str) -> int:
    """Convert a validated seconds value to integer milliseconds.

    The result is always at least 1 (int truncation of a value valid
    per :func:`validate_timeout_seconds` can never reach zero below
    validation, but a sub-millisecond float like ``0.001`` must still
    round up rather than become ``0``, which the SDK reads as
    "unbounded").
    """
    validated = validate_timeout_seconds(seconds, name=name)

    return max(1, math.ceil(validated * 1000.0))


_TIMEOUT_STATUS_CODES = frozenset({408, 504})

# Timeout exception class names recognised by name. This exists because
# the google-genai SDK raises ``compat_errors.APITimeoutError``, an
# ``APIError`` subclass that is NOT a ``TimeoutError``, and importing
# that private SDK module at runtime is not acceptable here (this module
# must stay importable without the SDK). Matching the class name keeps
# the check version-tolerant and avoids a hard dependency on a private
# path. ``TimeoutError``/``TimeoutException`` and the httpx subclass
# names are listed for symmetry; they are normally already covered by
# the ``isinstance`` checks.
_TIMEOUT_TYPE_NAMES = frozenset(
    {
        "APITimeoutError",
        "Timeout",
        "TimeoutError",
        "TimeoutException",
        "ReadTimeout",
        "ConnectTimeout",
        "WriteTimeout",
        "PoolTimeout",
    }
)

# Bounded walk: an exception chain is normally 1-3 links deep. The bound
# and the id-based cycle guard keep a self-referential or unusually long
# chain from hanging error handling.
_MAX_CAUSE_DEPTH = 8


def _iter_exception_chain(exc: BaseException):
    """Yield `exc` and its ``__cause__``/``__context__`` ancestors."""
    seen: set[int] = set()
    current: BaseException | None = exc

    while current is not None and len(seen) < _MAX_CAUSE_DEPTH:
        if id(current) in seen:
            return

        seen.add(id(current))
        yield current

        cause = current.__cause__
        current = cause if cause is not None else current.__context__


def _is_timeout_like(exc: BaseException, httpx_module) -> bool:
    """Return True if this single exception looks like a timeout."""
    if isinstance(exc, TimeoutError):
        return True

    if httpx_module is not None and isinstance(
        exc, httpx_module.TimeoutException
    ):
        return True

    if type(exc).__name__ in _TIMEOUT_TYPE_NAMES:
        return True

    code = getattr(exc, "code", None)

    if isinstance(code, bool) or not isinstance(code, int):
        return False

    return code in _TIMEOUT_STATUS_CODES


def is_timeout_error(exc: BaseException) -> bool:
    """Classify `exc` as "the provider did not answer in time".

    True for the standard-library ``TimeoutError`` (in Python 3.11+
    ``asyncio.TimeoutError`` is an alias, so escaped deadline errors are
    covered too), every ``httpx.TimeoutException`` subclass
    (``ReadTimeout``, ``ConnectTimeout``, ``WriteTimeout``,
    ``PoolTimeout``), the SDK's own ``APITimeoutError`` wrapper, and a
    Gemini ``APIError`` carrying one of the canonical gateway timeout
    status codes (408 Request Timeout, 504 Gateway Timeout).

    The whole ``__cause__``/``__context__`` chain is inspected: the SDK
    reports a stalled HTTP call as ``APITimeoutError`` wrapping
    ``httpx.ReadTimeout``, and either link is sufficient evidence.

    Everything else is deliberately ``False``: only provable timeouts get
    the "timeout" stage; ordinary outages keep the generic failure stage
    they had before Phase 6.6. A connection/DNS failure is NOT a timeout
    and must not be reported as one. (Transient status-code policy is
    Phase 6.7 scope.)
    """
    # httpx is a hard dependency of the google-genai SDK; importing it
    # here (only) keeps the module importable without any SDK/httpx
    # specific state in offline unit tests.
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is always installed
        httpx = None

    return any(
        _is_timeout_like(candidate, httpx)
        for candidate in _iter_exception_chain(exc)
    )


def assert_no_unexpected_type(value: object) -> None:  # pragma: no cover
    """Documentation-only guard; intentionally not used at runtime."""
    raise NotImplementedError


if typing.TYPE_CHECKING:  # pragma: no cover
    _ = assert_no_unexpected_type
