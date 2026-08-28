"""Wiring the three backends the same way in every eduTAP service.

Three systems with three jobs, and the split is deliberate rather than historical:
**Sentry** takes errors, **the OTLP collector** takes traces and metrics, and
**structlog** produces the records that go to both. Nothing travels two paths --
Sentry's own tracing stays off, because the spans already go to the collector and
Bugsink, the tracker this estate runs, states that it does not support traces.

The options are returned by pure functions before they are applied. That is what lets
a test assert the exact set rather than assert that *something* was configured, and it
is the same shape ``edutap.data_provider`` uses, from whose observability design
record the Sentry options and their measurements are taken.
"""

import logging
import os
from typing import Any

import logfire
import sentry_sdk
import structlog
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


def _configure_structlog(settings: ObservabilitySettings) -> None:
    """Point structlog at the collector and render what is left as JSON.

    ``logfire.StructlogProcessor`` is the bridge: it turns each event into a log
    record on the OTel side, so a log line and the span it happened inside share a
    trace id without the caller doing anything. It sits in the chain rather than at
    the end, because the record still has to be rendered for the container log.

    JSON rather than the developer console renderer: these lines are read out of
    ``docker service logs`` and, later, out of the collector. A DLQ entry that cannot
    be found by ``edutap-event-id`` is a DLQ entry nobody replays.
    """
    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
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
