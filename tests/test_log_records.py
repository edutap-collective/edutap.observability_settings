"""What reaches the collector on the logs signal.

These tests run a real OTLP/HTTP receiver on a loopback port and let the real
exporters talk to it. Nothing on the export path is mocked, and that is the point:
the records only leave the process because logfire attaches an OTLP log exporter
when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set. That is logfire's internal wiring, not a
documented contract. If an upgrade changes it, these tests turn red instead of the
logs disappearing without anyone noticing.
"""

import gzip
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import logfire
import pytest
import structlog
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord

from edutap.observability_settings import ObservabilitySettings, install_observability


class Receiver:
    """An OTLP/HTTP endpoint that keeps every request body it is sent, by path."""

    def __init__(self) -> None:
        self.bodies: dict[str, list[bytes]] = {}
        received = self.bodies

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 -- the name http.server calls
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                received.setdefault(self.path, []).append(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def records(self) -> list[tuple[dict[str, str], LogRecord]]:
        """Every log record received, with the resource attributes it arrived under."""
        found = []
        for body in self.bodies.get("/v1/logs", []):
            request = ExportLogsServiceRequest.FromString(body)
            for resource_logs in request.resource_logs:
                resource = _attributes(resource_logs.resource.attributes)
                for scope_logs in resource_logs.scope_logs:
                    found.extend((resource, record) for record in scope_logs.log_records)
        return found

    def span_names(self) -> list[str]:
        names = []
        for body in self.bodies.get("/v1/traces", []):
            request = ExportTraceServiceRequest.FromString(body)
            for resource_spans in request.resource_spans:
                for scope_spans in resource_spans.scope_spans:
                    names.extend(span.name for span in scope_spans.spans)
        return names


def _value(value: AnyValue) -> object:
    return getattr(value, value.WhichOneof("value"))


def _attributes(pairs: "list[KeyValue]") -> dict:
    return {pair.key: _value(pair.value) for pair in pairs}


@pytest.fixture
def collector(monkeypatch) -> Iterator[Receiver]:
    receiver = Receiver()
    thread = threading.Thread(target=receiver.server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", receiver.url)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    try:
        yield receiver
    finally:
        receiver.server.shutdown()
        receiver.server.server_close()


def _only(receiver: Receiver, event: str) -> tuple[dict[str, str], LogRecord]:
    matching = [(res, rec) for res, rec in receiver.records() if rec.body.string_value == event]
    assert len(matching) == 1, receiver.records()
    return matching[0]


def test_an_event_leaves_as_a_log_record_under_the_service_name(collector):
    install_observability(ObservabilitySettings(environment="staging"), service_name="probe")

    structlog.get_logger().warning("moved to dlq", topic="pass.state", offset=17)
    logfire.force_flush()

    resource, record = _only(collector, "moved to dlq")
    assert resource["service.name"] == "probe"
    assert resource["deployment.environment.name"] == "staging"
    assert record.severity_number == 13  # WARN
    assert record.severity_text == "WARNING"
    attributes = _attributes(record.attributes)
    assert attributes["topic"] == "pass.state"
    assert attributes["offset"] == 17
    # The level travels as severity, the event as the body. Neither is repeated as an
    # attribute, or every query would have two places to look.
    assert "level" not in attributes
    assert "event" not in attributes


def test_a_record_carries_the_trace_it_was_written_in(collector):
    install_observability(ObservabilitySettings(), service_name="probe")

    with logfire.span("handle request") as span:
        structlog.get_logger().error("rejected")
        expected = span.get_span_context()
    logfire.force_flush()

    _, record = _only(collector, "rejected")
    assert int.from_bytes(record.trace_id, "big") == expected.trace_id
    assert int.from_bytes(record.span_id, "big") == expected.span_id


def test_the_log_span_is_still_written_next_to_the_record(collector):
    # Decided on purpose: the zero-duration spans logfire makes from each event stay,
    # so a trace in Tempo keeps showing what was logged inside it.
    install_observability(ObservabilitySettings(), service_name="probe")

    structlog.get_logger().warning("kept in the trace")
    logfire.force_flush()

    _only(collector, "kept in the trace")
    assert "kept in the trace" in collector.span_names()


def test_the_body_is_a_string_never_a_map(collector):
    # Collector-side redaction of one-time tokens typically matches string bodies and
    # attribute values; a map body would let a path segment slip past it.
    install_observability(ObservabilitySettings(), service_name="probe")

    structlog.get_logger().info("plain body", nested={"a": 1})
    logfire.force_flush()

    _, record = _only(collector, "plain body")
    assert record.body.WhichOneof("value") == "string_value"


def test_a_value_that_is_not_a_scalar_arrives_as_json(collector):
    install_observability(ObservabilitySettings(), service_name="probe")

    structlog.get_logger().info("with structure", nested={"a": 1}, items=[1, 2])
    logfire.force_flush()

    _, record = _only(collector, "with structure")
    attributes = _attributes(record.attributes)
    assert json.loads(attributes["nested"]) == {"a": 1}
    assert json.loads(attributes["items"]) == [1, 2]


def test_an_exception_arrives_as_the_semantic_attributes(collector):
    install_observability(ObservabilitySettings(), service_name="probe")

    try:
        raise ValueError("no such pass")
    except ValueError:
        structlog.get_logger().exception("lookup failed")
    logfire.force_flush()

    _, record = _only(collector, "lookup failed")
    attributes = _attributes(record.attributes)
    assert attributes["exception.type"] == "ValueError"
    assert attributes["exception.message"] == "no such pass"
    assert "raise ValueError" in attributes["exception.stacktrace"]
    assert record.severity_number == 17  # ERROR


def test_the_level_filter_applies_to_records_too(collector):
    install_observability(ObservabilitySettings(log_level="WARNING"), service_name="probe")

    structlog.get_logger().info("routine")
    structlog.get_logger().warning("worth a look")
    logfire.force_flush()

    events = [record.body.string_value for _, record in collector.records()]
    assert "worth a look" in events
    assert "routine" not in events


def test_records_can_be_switched_off(collector):
    install_observability(ObservabilitySettings(export_log_records=False), service_name="probe")

    structlog.get_logger().warning("stays in the container log")
    logfire.force_flush()

    assert collector.records() == []


def test_stdout_still_gets_the_line(collector, capsys):
    # Records are an addition, not a replacement: `docker service logs` has to keep
    # working, and a deployment that ships container logs still gets them.
    install_observability(ObservabilitySettings(), service_name="probe")

    structlog.get_logger().warning("in both places")
    logfire.force_flush()

    assert '"event": "in both places"' in capsys.readouterr().out
    _only(collector, "in both places")


def test_without_a_collector_no_record_is_made(monkeypatch, capsys):
    # With no endpoint logfire prints spans to the console. A record processor on top
    # would print every line a second time into the same terminal.
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    install_observability(ObservabilitySettings(), service_name="probe")

    from edutap.observability_settings.install import OTelLogRecordProcessor

    processors = structlog.get_config()["processors"]
    assert not any(isinstance(p, OTelLogRecordProcessor) for p in processors)
