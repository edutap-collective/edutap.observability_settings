"""Wiring the three backends the same way in every eduTAP service.

Three systems with three jobs, and the split is deliberate rather than historical:
**Sentry** takes errors, **the OTLP collector** takes traces, metrics and log
records, and **structlog** produces the events that go to both. Nothing travels two paths --
Sentry's own tracing stays off, because the spans already go to the collector and
Bugsink, the tracker this estate runs, states that it does not support traces.

The options are returned by pure functions before they are applied. That is what lets
a test assert the exact set rather than assert that *something* was configured, and it
is the same shape ``edutap.data_provider`` uses, from whose observability design
record the Sentry options and their measurements are taken.
"""

import json
import logging
import os
import sys
from typing import Any

import logfire
import sentry_sdk
import structlog
from opentelemetry._logs import SeverityNumber, get_logger_provider
from sentry_sdk.integrations.logging import ignore_logger

from .settings import OTLP_ENDPOINT_VARIABLE, ObservabilitySettings

#: Loggers whose ERROR records are transport chatter, not defects.
#:
#: Sentry's ``LoggingIntegration`` is on by default and turns every ERROR record into
#: an event. Kafka clients log at ERROR for every failed connection attempt while a
#: broker is unreachable -- and they retry, and they recover. Measured on the LMU
#: instance on 2026-08-28: of 7260 events, 1197 were retry chatter from three
#: services during a rolling update in which nothing was actually wrong.
#:
#: THE TRADE-OFF IS DELIBERATE and worth stating: a genuinely permanent broker outage
#: now produces no event in the tracker either. It produces log lines, it produces
#: metrics, and the consumer's own failures -- a message it cannot handle, a DLQ
#: entry -- report as before. An error tracker is for defects; an outage is for
#: monitoring, and mixing the two is how a tracker becomes unread.
NOISY_LOGGERS = (
    "aiokafka",
    "aiokafka.conn",
    "aiokafka.cluster",
    "aiokafka.consumer.group_coordinator",
    "aiokafka.consumer.fetcher",
    "kafka",
)


def sentry_options(
    settings: ObservabilitySettings, *, service_version: str | None = None
) -> dict[str, Any]:
    """Return the options that decide what leaves the process.

    Each contradicts the backend's own default, and each was chosen against a
    measurement recorded in the data provider's observability design record.

    ``include_local_variables=False`` -- with local variables on, a raw
    ``Authorization`` header sits in the ASGI scope, which is a local in most frames of
    an ASGI stack, and the bearer token then appears dozens of times in an event whose
    rendered ``authorization`` header says ``[Filtered]``. Sentry's scrubber matches
    key names; it does not walk a list of byte tuples.

    ``max_request_body_size="never"`` -- for a service whose request body *is* the
    identifying datum there is no partial version of this.

    ``max_breadcrumbs=0`` -- Sentry's ``LoggingIntegration`` is on by default and turns
    every WARNING/ERROR record into a breadcrumb carrying the record's formatted
    message verbatim and unscrubbed, on a path none of the other options constrains.
    A hook dropping only ``type == "log"`` breadcrumbs was the narrower alternative and
    was rejected: it would need re-auditing against every breadcrumb-producing
    integration added in the future, whereas turning them off closes the surface once.
    The cost is real -- an event carries no timeline of what happened earlier -- and it
    is the same trade already made for local variables and the request body.

    ``release`` -- the one option added rather than overridden. Without it an error
    tracker cannot answer the question that follows every fix: *is this still
    happening in what we shipped?* It comes from ``settings.release`` where a
    deployment names its artefact, and falls back to the ``service_version`` the
    caller passes. See the field's own documentation for why those are two different
    things.

    ``Any`` rather than ``object`` in the return type, which is the one place this
    package spends the escape hatch: the mapping is heterogeneous by nature and is
    unpacked into a third-party signature, and ``object`` makes a type checker reject
    every single key -- measured, 71 diagnostics for six options. Narrowing it to a
    ``TypedDict`` would restate sentry-sdk's signature here and go stale with its next
    release.
    """
    return {
        "environment": settings.environment,
        "release": settings.release or service_version,
        "traces_sample_rate": 0,
        "send_default_pii": False,
        "include_local_variables": False,
        "max_request_body_size": "never",
        "max_breadcrumbs": 0,
    }


def logfire_options(
    settings: ObservabilitySettings,
    *,
    service_name: str,
    service_version: str | None = None,
) -> dict[str, Any]:
    """Return how this service configures tracing, metrics and log export.

    ``send_to_logfire=False`` is not a detail. The library defaults it to ``True``, so
    leaving it unset would ship spans to a hosted third party the first time a token
    happened to be present. This estate exports to its own collector.

    The console stands in while there is no collector. Measured against logfire 4.40:
    with ``send_to_logfire=False`` and no ``OTEL_EXPORTER_OTLP_ENDPOINT``, no exporter
    is installed at all, so an instrumented service would be indistinguishable from an
    uninstrumented one -- which is how instrumentation reaches production broken. Once
    the endpoint is set the console stands down, because printing every span into the
    container log is a development aid and not a production one.
    """
    exports_to_a_collector = bool(os.environ.get(OTLP_ENDPOINT_VARIABLE))
    return {
        "send_to_logfire": False,
        "service_name": service_name,
        "service_version": service_version,
        "environment": settings.environment,
        "console": False if exports_to_a_collector else logfire.ConsoleOptions(),
    }


def install_observability(
    settings: ObservabilitySettings | None = None,
    *,
    service_name: str,
    service_version: str | None = None,
) -> None:
    """Install error reporting, tracing and structured logging for this process.

    Call it first, before the service resolves the settings it needs to run, so that a
    process refusing to start is reported rather than silently absent.

    Both backends are opt-in by configuration and not by code: no DSN means no error
    tracker, ``telemetry_enabled=False`` means no tracing. Structured logging is set up
    either way -- a service without a collector still has to be readable.
    """
    settings = settings or ObservabilitySettings()

    if settings.telemetry_enabled:
        logfire.configure(
            **logfire_options(settings, service_name=service_name, service_version=service_version)
        )

    _configure_structlog(settings)

    if settings.sentry_dsn is not None:
        sentry_sdk.init(
            dsn=settings.sentry_dsn.get_secret_value(),
            **sentry_options(settings, service_version=service_version),
        )
        # AFTER init, because that is when the integration exists. Each name is a
        # logger whose ERROR records are retries rather than defects; see
        # NOISY_LOGGERS for the measurement and the trade-off.
        for name in NOISY_LOGGERS:
            ignore_logger(name)


#: structlog's level names, mapped to the OTel severity they stand for and the text
#: that goes with it.
#:
#: The text is the stdlib level name, as OpenTelemetry's own logging handler sets it.
#: ``add_log_level`` has already turned ``exception`` into ``error`` by the time a
#: record is made, and ``warn`` is the alias structlog still accepts.
_SEVERITY = {
    "debug": (SeverityNumber.DEBUG, "DEBUG"),
    "info": (SeverityNumber.INFO, "INFO"),
    "warning": (SeverityNumber.WARN, "WARNING"),
    "warn": (SeverityNumber.WARN, "WARNING"),
    "error": (SeverityNumber.ERROR, "ERROR"),
    "critical": (SeverityNumber.FATAL, "CRITICAL"),
}

#: Keys that travel as a field of the record rather than as an attribute of it.
#: Repeating them as attributes would give every query two places to look.
_NOT_ATTRIBUTES = frozenset({"event", "level", "timestamp", "exc_info"})


class OTelLogRecordProcessor:
    """Emit each structlog event as an OTel log record, and pass it on unchanged.

    ``logfire.StructlogProcessor`` does not do this: it turns an event into a
    zero-duration *span*, so a log line reaches the collector on the traces signal and
    never on the logs signal. This processor writes to the global logger provider
    instead, through the public OpenTelemetry API. logfire has registered that
    provider, and attached an OTLP exporter to it, by the time this runs.

    The shape of the record is decided, not incidental:

    - The body is ``event`` as a string, never a map. Collector-side redaction of
      one-time tokens typically matches string bodies and attribute values, and a
      map body would let a path segment slip past it.
    - Every other field becomes a flat attribute. Scalars pass through; anything else
      is serialised to JSON, because an OTel attribute cannot hold a mapping and
      dropping the field would lose exactly what one debugs with.
    - ``exc_info`` is handed to the SDK as the exception, which sets
      ``exception.type``, ``exception.message`` and ``exception.stacktrace``. That is
      why the processor sits *before* ``format_exc_info``, which would otherwise have
      flattened the exception into a string already.
    - Trace and span id come from the current context, so a record and the span it
      was written in share a trace id without the caller doing anything.
    """

    def __init__(self) -> None:
        """Take the logger from whatever provider is registered at install time."""
        self._logger = get_logger_provider().get_logger("edutap.observability_settings")

    def __call__(
        self, logger: object, method_name: str, event_dict: structlog.typing.EventDict
    ) -> structlog.typing.EventDict:
        """Emit the record, then hand the event on for rendering."""
        level = str(event_dict.get("level", method_name))
        severity_number, severity_text = _SEVERITY.get(
            level, (SeverityNumber.UNSPECIFIED, level.upper())
        )
        self._logger.emit(
            severity_number=severity_number,
            severity_text=severity_text,
            body=str(event_dict.get("event", "")),
            attributes={
                key: _attribute_value(value)
                for key, value in event_dict.items()
                if key not in _NOT_ATTRIBUTES and value is not None
            },
            exception=_exception(event_dict.get("exc_info")),
        )
        return event_dict


def _attribute_value(value: object) -> str | bool | int | float:
    if isinstance(value, str | bool | int | float):
        return value
    try:
        return json.dumps(value, default=str)
    except ValueError:
        # A circular structure. Losing its shape is better than losing the line.
        return repr(value)


def _exception(exc_info: object) -> BaseException | None:
    """Resolve the three forms structlog accepts for ``exc_info``."""
    if isinstance(exc_info, BaseException):
        return exc_info
    if isinstance(exc_info, tuple):
        return exc_info[1]
    if exc_info is True:
        return sys.exc_info()[1]
    return None


def _configure_structlog(settings: ObservabilitySettings) -> None:
    """Point structlog at the collector and render what is left as JSON.

    Two bridges, and they produce different things:

    - :class:`OTelLogRecordProcessor` writes each event as a **log record** on the
      logs signal. That is what a log backend receives, with service name,
      environment, severity and trace id. It is only added where it can go somewhere:
      with telemetry on and an OTLP endpoint set. Without an endpoint logfire prints
      to the console, and a record processor would print every line a second time.
    - ``logfire.StructlogProcessor`` writes each event as a zero-duration **span** on
      the traces signal, so a trace keeps showing what was logged inside it. It does
      not produce log records, whatever its name suggests.

    Both sit in the chain rather than at the end, because the event still has to be
    rendered for the container log.

    JSON rather than the developer console renderer: these lines are read out of
    ``docker service logs`` and, later, out of the collector. A DLQ entry that cannot
    be found by ``edutap-event-id`` is a DLQ entry nobody replays.
    """
    exports_records = (
        settings.telemetry_enabled
        and settings.export_log_records
        and bool(os.environ.get(OTLP_ENDPOINT_VARIABLE))
    )
    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    if exports_records:
        processors.append(OTelLogRecordProcessor())
    processors.append(structlog.processors.format_exc_info)
    if settings.telemetry_enabled:
        processors.append(logfire.StructlogProcessor())
    processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[settings.log_level]
        ),
        cache_logger_on_first_use=True,
    )
