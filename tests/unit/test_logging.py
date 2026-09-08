"""Logging: levels, the structured envelope, and payload-free logs by default."""

from datetime import datetime

import pytest

from protobus import (
    BaseListener,
    Connection,
    EventListener,
    HandledError,
    InternalServiceError,
    Log,
    LogLevel,
    LogRecord,
    Logger,
    ServiceProxy,
    format_log_record,
    get_log_level,
    safe_error_summary,
    sanitize_error_for_client,
    set_diagnostics_serializer,
    set_log_level,
    set_logger,
)
from protobus.connection import ConsumeOptions, ConsumeRetryOptions
from protobus.logger import DefaultLogger, LogDiagnostics

from ..helpers import FakeChannel, FakeConnection, FakeContext, make_delivery, make_factory

SECRET = "MARKER-e7f1c2a9-do-not-log"


class CapturingLogger:
    def __init__(self):
        self.lines = []

    def _record(self, level, m):
        self.lines.append(f"{level}:{m}")

    def info(self, m):
        self._record("info", m)

    def warn(self, m):
        self._record("warn", m)

    def debug(self, m):
        self._record("debug", m)

    def error(self, m):
        self._record("error", m)

    @property
    def text(self):
        return "\n".join(self.lines)


class CapturingStructuredLogger(CapturingLogger):
    def __init__(self):
        super().__init__()
        self.records = []

    def log(self, record):
        self.records.append(record)

    @property
    def text(self):
        return "\n".join(self.lines) + "\n" + "\n".join(str(r.to_dict()) for r in self.records)


@pytest.fixture
def capture():
    original = get_log_level()
    log = CapturingLogger()
    set_logger(log)
    set_log_level(LogLevel.Info)
    yield log
    set_logger(DefaultLogger())
    set_log_level(original)


@pytest.fixture
def structured():
    original = get_log_level()
    log = CapturingStructuredLogger()
    set_logger(log)
    set_log_level(LogLevel.Info)
    set_diagnostics_serializer(None)
    yield log
    set_diagnostics_serializer(None)
    set_logger(DefaultLogger())
    set_log_level(original)


class TestLogLevels:
    def test_suppresses_debug_at_the_default_level(self, capture):
        Logger.debug("secret payload")
        assert [l for l in capture.lines if l.startswith("debug:")] == []

    def test_still_emits_warn_and_error_at_the_default_level(self, capture):
        Logger.warn("careful")
        Logger.error("broken")
        assert capture.lines == ["warn:careful", "error:broken"]

    def test_emits_debug_once_the_level_is_lowered(self, capture):
        set_log_level(LogLevel.Debug)
        Logger.debug("now visible")
        assert capture.lines == ["debug:now visible"]

    def test_silences_everything_at_the_silent_level(self, capture):
        set_log_level(LogLevel.Silent)
        Logger.debug("a")
        Logger.info("b")
        Logger.warn("c")
        Logger.error("d")
        assert capture.lines == []

    def test_installing_a_sink_alone_does_not_enable_debug_output(self, capture):
        Logger.debug("a debug line")
        Logger.info("an info line")
        assert capture.lines == ["info:an info line"]

    def test_overrides_the_logger(self, capture):
        Logger.info("test message")
        assert capture.lines == ["info:test message"]


class TestStructuredLogEnvelope:
    def test_delivers_a_record_to_a_sink_that_implements_log(self, structured):
        Log.info("published request", operation="publish", message_type="example.Service.DoThing",
                 message_id="mid-1", correlation_id="cid-1", size_bytes=1234, outcome="confirmed")
        assert len(structured.records) == 1
        record = structured.records[0]
        assert isinstance(record, LogRecord)
        assert record.to_dict() == {
            **record.to_dict(),
            "component": "protobus", "level": "info", "operation": "publish",
            "message": "published request", "message_type": "example.Service.DoThing",
            "message_id": "mid-1", "correlation_id": "cid-1", "size_bytes": 1234, "outcome": "confirmed",
        }
        datetime.fromisoformat(record.timestamp.replace("Z", "+00:00"))
        # A structured sink gets the record only; no duplicate string line.
        assert structured.lines == []

    def test_stamps_component_itself_so_fields_cannot_forge_it(self, structured):
        Log.warn("unroutable", operation="publish", component="not-protobus")
        assert structured.records[0].component == "protobus"

    def test_falls_back_to_the_string_path_for_a_sink_without_log(self, capture):
        Log.warn("nacked by broker", operation="publish", message_type="example.Service.DoThing", correlation_id="cid-2", outcome="failed")
        assert len(capture.lines) == 1
        line = capture.lines[0]
        for fragment in ("warn:", "protobus", "publish", "nacked by broker", "correlation_id=cid-2", "outcome=failed"):
            assert fragment in line

    def test_routes_each_level_to_the_matching_string_method(self, capture):
        set_log_level(LogLevel.Debug)
        Log.debug("a", operation="consume")
        Log.info("b", operation="consume")
        Log.warn("c", operation="consume")
        Log.error("d", operation="consume")
        assert [l.split(":")[0] for l in capture.lines] == ["debug", "info", "warn", "error"]

    def test_drops_fields_outside_the_safe_allowlist(self, structured):
        Log.error("connection failed", operation="connect", correlation_id="cid-3",
                  url=f"amqp://user:{SECRET}@host/vhost", headers={"authorization": SECRET},
                  payload={"card": SECRET}, body=SECRET.encode(), broker_error=f"ACCESS_REFUSED {SECRET}")
        assert SECRET not in structured.text
        record = structured.records[0].to_dict()
        for key in ("url", "headers", "payload", "body", "broker_error"):
            assert key not in record
        assert record["correlation_id"] == "cid-3"

    def test_drops_non_scalar_values_passed_in_allowlisted_fields(self, structured):
        class Sneaky:
            def __str__(self):
                return SECRET

        Log.info("odd", operation="publish", message_type=Sneaky(), size_bytes=float("nan"))
        assert SECRET not in structured.text
        assert structured.records[0].message_type is None
        assert structured.records[0].size_bytes is None

    def test_strips_control_characters_and_truncates_long_values(self, structured):
        Log.info("line one\nFAKE forged line", operation="publish", message_type="x" * 400)
        record = structured.records[0]
        assert "\n" not in record.message
        assert len(record.message_type) <= 256

    def test_applies_the_level_filter_before_touching_the_sink(self, structured):
        Log.debug("payload dump", operation="consume")
        assert structured.records == []
        set_log_level(LogLevel.Silent)
        Log.error("boom", operation="consume")
        assert structured.records == []

    def test_never_materialises_diagnostics_without_an_installed_serializer(self, structured):
        asked = []

        def thunk():
            asked.append(1)
            return LogDiagnostics(payload={"card": SECRET})

        Log.info("handled request", operation="consume", correlation_id="cid-4", diagnostics=thunk)
        assert asked == []
        assert structured.records[0].diagnostics is None
        assert SECRET not in structured.text

    def test_attaches_whatever_the_opt_in_serializer_returns(self, structured):
        set_diagnostics_serializer(lambda d, record: {"keys": list(d.payload.keys()), "operation": record.operation})
        Log.info("handled request", operation="consume", correlation_id="cid-5",
                 diagnostics=lambda: LogDiagnostics(payload={"card": SECRET}))
        assert structured.records[0].diagnostics == {"keys": ["card"], "operation": "consume"}
        assert SECRET not in structured.text

    def test_omits_diagnostics_when_the_serializer_returns_none(self, structured):
        set_diagnostics_serializer(lambda d, r: None)
        Log.info("handled request", operation="consume", diagnostics=lambda: LogDiagnostics(payload=1))
        assert structured.records[0].diagnostics is None

    def test_survives_a_throwing_serializer_without_losing_the_line(self, structured):
        def broken(d, r):
            raise RuntimeError("hook is broken")

        set_diagnostics_serializer(broken)
        Log.info("handled request", operation="consume", diagnostics=lambda: LogDiagnostics(payload={"card": SECRET}))
        assert len(structured.records) == 1
        assert structured.records[0].diagnostics is None
        assert SECRET not in structured.text

    def test_survives_a_throwing_thunk_without_losing_the_line(self, structured):
        set_diagnostics_serializer(lambda d, r: d)

        def thunk():
            raise RuntimeError("cannot decode")

        Log.info("handled request", operation="consume", diagnostics=thunk)
        assert len(structured.records) == 1

    def test_degrades_to_the_string_path_when_the_structured_sink_throws(self):
        lines = []

        class Sink:
            def log(self, record):
                raise RuntimeError("transport down")

            def info(self, m):
                lines.append(str(m))

            warn = debug = error = info

        original = get_log_level()
        set_logger(Sink())
        set_log_level(LogLevel.Info)
        try:
            Log.info("published", operation="publish", correlation_id="cid-6")
        finally:
            set_logger(DefaultLogger())
            set_log_level(original)
        assert len(lines) == 1 and "correlation_id=cid-6" in lines[0]

    def test_formats_a_record_deterministically(self):
        text = format_log_record(LogRecord(
            level="info", timestamp="2026-01-01T00:00:00.000Z", operation="publish", message="published request",
            message_type="example.Service.DoThing", correlation_id="cid-7", size_bytes=1234, outcome="confirmed",
        ))
        assert text == ("[protobus] publish: published request "
                        "(message_type=example.Service.DoThing correlation_id=cid-7 outcome=confirmed size_bytes=1234)")

    def test_keeps_the_string_logger_api_working_unchanged(self, capture):
        Logger.info("plain text message")
        assert capture.lines == ["info:plain text message"]


class TestLogsArePayloadFreeAtTheDefaultLevel:
    async def test_does_not_log_the_body_of_an_unhandled_message(self, capture):
        listener = BaseListener(FakeConnection())
        body = f'{{"password": "{SECRET}"}}'.encode()
        await listener._default_handler(body, "cid-1")
        assert SECRET not in capture.text
        assert "unhandled" in capture.text.lower()

    async def test_does_not_log_the_body_of_an_unhandled_event(self, capture):
        class Factory:
            def decode_event(self, _b):
                from protobus.message_factory import EventContainer

                return EventContainer(type="X", topic="", data={"token": SECRET})

        listener = EventListener(FakeConnection(), Factory())
        await listener._default_handler(b"encoded", "cid-2", {}, None)
        assert SECRET not in capture.text
        assert "event" in capture.text.lower()

    async def test_does_not_log_message_content_when_a_handler_fails(self, capture):
        conn = Connection()
        ch = FakeChannel()

        async def handler(*_a):
            raise RuntimeError(f"upstream rejected card {SECRET}")

        await conn.consume(ch, "Q", handler, ConsumeOptions(), True)
        body = f'{{"card": "{SECRET}"}}'.encode()
        await ch.deliver(make_delivery(body=body, correlation_id="cid-9"))
        # The handler's own error text is the service's to disclose, but the
        # message body must never be logged by the framework itself.
        assert body.decode() not in capture.text

    async def test_does_not_log_the_request_when_a_streaming_request_fails_to_build(self, capture):
        factory = make_factory(
            'syntax = "proto3"; package T; message Req { string token = 1; } message Chunk { string v = 1; }'
            "service Api { rpc play (T.Req) returns (stream T.Chunk); }", "T.Api",
        )
        ctx = FakeContext(factory)

        def fail(*_a, **_kw):
            raise RuntimeError("encode failed")

        proxy = ServiceProxy(ctx, "T.Api")
        await proxy.init()
        factory.build_request = fail  # type: ignore[method-assign]
        with pytest.raises(Exception):
            async for _chunk in proxy.play({"token": SECRET}):
                pass
        assert SECRET not in capture.text
        assert "failed building streaming request" in capture.text

    async def test_does_not_put_raw_error_text_into_retry_metadata_headers(self, capture):
        conn = Connection()
        ch = FakeChannel()

        async def handler(*_a):
            raise RuntimeError(f"connection failed for user admin password {SECRET}")

        retry = ConsumeRetryOptions(max_retries=2, retry_queue_name="Q.Retry", retry_exchange_name="Q.Retry.Ex", dlq_name="Q.DLQ")
        await conn.consume(ch, "Q", handler, ConsumeOptions(), True, retry)
        await ch.deliver(make_delivery(correlation_id="cid-10"))
        headers = "\n".join(str(p.headers) for p in ch.published)
        assert SECRET not in headers
        assert ch.published[0].headers["x-last-error"] == "RuntimeError"


class TestServiceErrorBoundary:
    def test_forwards_an_unhandled_error_message_to_the_caller_by_default(self):
        err = RuntimeError(f"db connect failed {SECRET}")
        assert SECRET in str(sanitize_error_for_client(err))

    def test_substitutes_a_generic_error_when_exposure_is_disabled(self, monkeypatch):
        monkeypatch.setenv("PROTOBUS_EXPOSE_INTERNAL_ERRORS", "false")
        wire = sanitize_error_for_client(RuntimeError(f"db connect failed {SECRET}"), "cid-77")
        assert isinstance(wire, InternalServiceError)
        assert SECRET not in str(wire)
        assert "cid-77" in str(wire)
        assert wire.code == "INTERNAL_ERROR"

    def test_always_forwards_a_handled_error_exposure_setting_notwithstanding(self, monkeypatch):
        monkeypatch.setenv("PROTOBUS_EXPOSE_INTERNAL_ERRORS", "false")
        handled = HandledError("invalid_params", "INVALID_PARAMS")
        assert sanitize_error_for_client(handled) is handled

    def test_never_puts_a_raw_message_in_safe_error_summary_but_keeps_handled_ones(self):
        assert SECRET not in safe_error_summary(RuntimeError(f"boom {SECRET}"))
        assert "invalid_params" in safe_error_summary(HandledError("invalid_params", "INVALID_PARAMS"))
        assert safe_error_summary(None) == "UnknownError"

        class Coded(Exception):
            code = "E42"

        assert safe_error_summary(Coded("x")) == "Coded[E42]"
