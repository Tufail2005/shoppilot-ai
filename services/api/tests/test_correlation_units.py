"""Phase 6.8 unit tests: request correlation and context propagation.

Covers (Plan v3 §4; steps 1-2 scope only):

- middleware request ID generation, visibility inside the downstream
  app, and context reset on success and exceptions,
- fresh IDs per request (no leakage), and non-HTTP pass-through,
- ``contextvars`` propagation through ``asyncio.to_thread()``,
- the JSON handler's filter stamping (text mode renders nothing),
- the registered middleware & text-mode guarantee on the real app.

All tests are offline: no database, no network.
"""

import asyncio
import json
import logging

import pytest

from app.core.observability import (
    CorrelationMiddleware,
    ObsJSONFormatter,
    _CorrelationFilter,
    current_request_id,
    new_request_id,
)


def _request_scope() -> dict:
    return {"type": "http", "method": "POST", "path": "/rag/query"}


class TestMiddleware:
    def test_request_id_is_visible_inside_and_reset_after(self):
        observed = []

        async def inner_app(scope, receive, send):
            observed.append(current_request_id())

        async def scenario():
            middleware = CorrelationMiddleware(inner_app)
            await middleware(_request_scope(), None, None)
            observed.append(current_request_id())

        asyncio.run(scenario())

        inside, after = observed
        assert isinstance(inside, str) and len(inside) == 32
        assert after is None

    def test_reset_happens_when_the_app_raises(self):
        async def inner_app(scope, receive, send):
            raise RuntimeError("downstream failure")

        async def scenario():
            middleware = CorrelationMiddleware(inner_app)
            try:
                await middleware(_request_scope(), None, None)
            except RuntimeError:
                pass
            return current_request_id()

        current = asyncio.run(scenario())
        assert current is None

    def test_each_request_gets_a_fresh_id(self):
        seen = []

        async def inner_app(scope, receive, send):
            seen.append(current_request_id())

        async def scenario():
            middleware = CorrelationMiddleware(inner_app)
            await middleware(_request_scope(), None, None)
            await middleware(_request_scope(), None, None)

        asyncio.run(scenario())

        assert seen[0] is not None
        assert seen[1] is not None
        assert seen[0] != seen[1]

    def test_non_http_scopes_pass_through_without_a_request_id(self):
        observed = []

        async def inner_app(scope, receive, send):
            observed.append((scope["type"], current_request_id()))

        async def scenario():
            middleware = CorrelationMiddleware(inner_app)
            await middleware({"type": "lifespan"}, None, None)
            await middleware({"type": "websocket", "type_hint": None}, None, None)

        asyncio.run(scenario())

        assert observed == [("lifespan", None), ("websocket", None)]


class TestToThreadPropagation:
    def test_contextvars_copy_covers_worker_threads(self):
        async def scenario():
            seen = {}

            async def inner_app(scope, receive, send):
                # The production pattern (retrieval.py:226, run_bounded
                # L720): worker threads receive a copy of the current
                # context at to_thread call time.
                def worker():
                    seen["thread"] = current_request_id()
                    seen["thread_two"] = current_request_id()

                seen["loop"] = current_request_id()
                await asyncio.to_thread(worker)

            await CorrelationMiddleware(inner_app)(
                _request_scope(), None, None
            )
            seen["after"] = current_request_id()

            return seen

        seen = asyncio.run(scenario())

        assert seen["loop"] is not None
        assert seen["thread"] == seen["loop"]
        assert seen["thread_two"] == seen["loop"]
        assert seen["after"] is None

    def test_no_request_outside_a_request(self):
        async def scenario():
            return await asyncio.to_thread(current_request_id)

        assert asyncio.run(scenario()) is None


class TestFilterStamping:
    def test_filter_stamps_id_from_context(self):
        async def scenario():
            probe = _CorrelationFilter()
            observed = {}

            async def inner_app(scope, receive, send):
                def worker():
                    record = logging.LogRecord(
                        name="app.test",
                        level=logging.INFO,
                        pathname="p",
                        lineno=1,
                        msg="m",
                        args=(),
                        exc_info=None,
                    )
                    probe.filter(record)
                    observed["value"] = record.request_id

                # The middleware call cannot carry a return value (ASGI
                # middlewares always return None); the observable goes
                # through the closure instead.
                await asyncio.to_thread(worker)

            await CorrelationMiddleware(inner_app)(
                _request_scope(), None, None
            )

            return observed["value"]

        stamped = asyncio.run(scenario())
        assert isinstance(stamped, str) and len(stamped) == 32

    def test_filter_leaves_explicit_values_untouched(self):
        class _Rec:
            request_id = "explicit"

        record = _Rec()
        _CorrelationFilter().filter(record)

        assert record.request_id == "explicit"

    def test_formatter_renders_request_id_when_set(self):
        formatter = ObsJSONFormatter()

        record = logging.LogRecord(
            name="app.test",
            level=logging.INFO,
            pathname="p",
            lineno=1,
            msg="m",
            args=(),
            exc_info=None,
        )
        record.request_id = "explicit-id"

        payload = json.loads(formatter.format(record))
        assert payload["request_id"] == "explicit-id"


class TestAppIntegration:
    def test_correlation_and_cors_are_registered(self):
        # Import the real app (text mode: no JSON handler installed).
        from app.main import app

        classes = [mw.cls for mw in app.user_middleware]

        assert CorrelationMiddleware in classes
        assert any("CORSMiddleware" in str(cls) for cls in classes)

    def test_importing_the_app_installs_no_json_handler(self):
        logger = logging.getLogger("app")

        assert logger.handlers == []
        assert logger.level == logging.NOTSET
        assert logger.propagate is True
