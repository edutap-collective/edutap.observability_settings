import structlog
from pydantic import SecretStr

from edutap.observability_settings import (
    ObservabilitySettings,
    install_observability,
    logfire_options,
    sentry_options,
)


def test_traces_do_not_travel_both_paths():
    # Traces go to the OTLP collector. Sentry's own tracing would be a second copy of
    # the same spans in a second system, and Bugsink -- the tracker this estate runs
    # -- states that it intentionally does not support them.
    assert sentry_options(ObservabilitySettings())["traces_sample_rate"] == 0


def test_nothing_that_carries_a_person_is_switched_on():
    # Each of these was chosen against a measurement in the data provider's
    # observability design record, and each contradicts the backend's own default.
    options = sentry_options(ObservabilitySettings())
    assert options["send_default_pii"] is False
    # With locals on, an Authorization header sits in the ASGI scope, which is a local
    # in most frames of an ASGI stack, and the bearer token then appears dozens of
    # times in an event whose rendered header reads [Filtered].
    assert options["include_local_variables"] is False
    # For a service whose request body *is* the identifying datum there is no partial
    # version of this.
    assert options["max_request_body_size"] == "never"
    # Sentry's LoggingIntegration is on by default and turns every WARNING/ERROR
    # record into a breadcrumb carrying the formatted message verbatim, on a path
    # none of the other options constrains.
    assert options["max_breadcrumbs"] == 0


def test_the_environment_labels_every_event():
    options = sentry_options(ObservabilitySettings(environment="staging"))
    assert options["environment"] == "staging"


def test_logfire_never_sends_to_the_hosted_backend():
    # This estate runs its own collector. logfire defaults send_to_logfire to True,
    # so leaving it unset would ship spans to a third party on the first deploy that
    # happened to carry a token.
    options = logfire_options(ObservabilitySettings(), service_name="worker")
    assert options["send_to_logfire"] is False
    assert options["service_name"] == "worker"


def test_the_console_stands_in_until_a_collector_exists(monkeypatch):
    # Measured against logfire 4.40: with send_to_logfire=False and no
    # OTEL_EXPORTER_OTLP_ENDPOINT, no exporter is installed at all. Without the
    # console, an instrumented service would then be indistinguishable from an
    # uninstrumented one -- which is how instrumentation reaches production broken.
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert logfire_options(ObservabilitySettings(), service_name="worker")["console"] is not False


def test_the_console_stands_down_once_the_collector_is_there(monkeypatch):
    # Printing every span into the container log is a development aid, not a
    # production one.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    assert logfire_options(ObservabilitySettings(), service_name="worker")["console"] is False


def test_installing_with_telemetry_off_touches_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "edutap.observability_settings.install.logfire.configure",
        lambda **kwargs: calls.append(kwargs),
    )
    settings = ObservabilitySettings(telemetry_enabled=False)
    install_observability(settings, service_name="worker")
    assert calls == []


def test_installing_without_a_dsn_does_not_start_the_error_tracker(monkeypatch):
    monkeypatch.setattr(
        "edutap.observability_settings.install.logfire.configure", lambda **kwargs: None
    )
    calls = []
    monkeypatch.setattr(
        "edutap.observability_settings.install.sentry_sdk.init",
        lambda **kwargs: calls.append(kwargs),
    )
    install_observability(ObservabilitySettings(), service_name="worker")
    assert calls == []


def test_installing_with_a_dsn_starts_it_with_the_measured_options(monkeypatch):
    monkeypatch.setattr(
        "edutap.observability_settings.install.logfire.configure", lambda **kwargs: None
    )
    calls = []
    monkeypatch.setattr(
        "edutap.observability_settings.install.sentry_sdk.init",
        lambda **kwargs: calls.append(kwargs),
    )
    settings = ObservabilitySettings(sentry_dsn=SecretStr("https://key@example.invalid/1"))
    install_observability(settings, service_name="worker")
    assert len(calls) == 1
    assert calls[0]["dsn"] == "https://key@example.invalid/1"
    assert calls[0]["max_breadcrumbs"] == 0


def test_a_real_install_renders_a_findable_line(capsys, monkeypatch):
    # The one test with nothing mocked. Everything above asserts what would be passed
    # to the backends; this asserts that passing it actually works -- a wrong
    # processor order or a level name the filter does not know shows up here and
    # nowhere else.
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    install_observability(ObservabilitySettings(), service_name="probe")

    structlog.get_logger().warning("moved to dlq", topic="pass.state", offset=17)

    rendered = capsys.readouterr().out
    assert '"event": "moved to dlq"' in rendered
    assert '"topic": "pass.state"' in rendered
    assert '"offset": 17' in rendered
    # Structured, not formatted: the fields have to survive as fields, or a DLQ entry
    # cannot be found again by the value that identifies it.
    assert '"level": "warning"' in rendered


def test_the_level_actually_filters(capsys, monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    install_observability(ObservabilitySettings(log_level="ERROR"), service_name="probe")

    structlog.get_logger().info("routine")

    assert "routine" not in capsys.readouterr().out
