"""What the instrumentation may say about a request."""

import logfire
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from edutap.observability_settings import ObservabilitySettings
from edutap.observability_settings.instrumentation import (
    UNMATCHED,
    instrument_fastapi_safely,
    route_template,
)

PERSON = "ab12cd34@lmu.de"


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/persons/{person_uid}/photos")
    async def photos(person_uid: str) -> dict:
        return {}

    @app.get("/healthz")
    async def healthz() -> dict:
        return {}

    return app


def _scope(path: str) -> dict:
    return {"type": "http", "method": "GET", "path": path, "headers": []}


def test_a_matched_path_resolves_to_its_template():
    assert route_template(_app(), _scope("/persons/ab12@lmu.de/photos")) == (
        "/persons/{person_uid}/photos"
    )


def test_a_static_path_resolves_to_itself():
    assert route_template(_app(), _scope("/healthz")) == "/healthz"


def test_an_unmatched_path_resolves_to_a_constant_and_never_to_itself():
    """The 404 case is the one that leaks if the fallback is the raw path.

    A request to /persons/<identifier>/typo matches no route, so there is no
    template to fall back on -- and the raw path is exactly what must not be
    exported.
    """
    result = route_template(_app(), _scope("/persons/ab12@lmu.de/typo"))
    assert result == UNMATCHED
    assert "ab12" not in result


class _Capture:
    """Collect finished spans instead of shipping them anywhere."""

    def __init__(self) -> None:
        self.spans: list = []

    def export(self, spans):
        self.spans.extend(spans)
        return None

    def shutdown(self):
        return None

    def force_flush(self, timeout_millis=None):
        return True


@pytest.fixture
def captured_spans():
    capture = _Capture()
    logfire.configure(
        send_to_logfire=False,
        console=False,
        additional_span_processors=[SimpleSpanProcessor(capture)],
    )
    return capture


def _exported_values(capture) -> list[str]:
    """Every attribute value of every captured span, as text."""
    return [str(value) for span in capture.spans for value in dict(span.attributes).values()]


@pytest.mark.parametrize("mode", ["omit", "pseudonym"])
def test_no_exported_attribute_carries_the_identifier(captured_spans, mode):
    """Asserted shape-free, on purpose.

    A test that enumerated the four attributes known to leak on 2026-08-17 would
    pass on the day a logfire release adds a fifth -- which is the failure this
    whole module exists to prevent. So the claim is about every attribute of every
    span, not about a list of names.
    """
    app = _app()
    instrument_fastapi_safely(
        app,
        ObservabilitySettings(
            person_uid_mode=mode, pseudonym_salt="a-salt", _env_file=None, _secrets_dir=None
        ),
    )
    TestClient(app).get(f"/persons/{PERSON}/photos")

    assert captured_spans.spans, "no span was exported; the test proves nothing"
    assert not [value for value in _exported_values(captured_spans) if PERSON in value]


@pytest.mark.parametrize("mode", ["omit", "pseudonym"])
def test_the_route_is_still_identifiable(captured_spans, mode):
    """Scrubbing must not cost the operational answer: which route was hit."""
    app = _app()
    instrument_fastapi_safely(
        app,
        ObservabilitySettings(
            person_uid_mode=mode, pseudonym_salt="a-salt", _env_file=None, _secrets_dir=None
        ),
    )
    TestClient(app).get(f"/persons/{PERSON}/photos")

    assert "/persons/{person_uid}/photos" in _exported_values(captured_spans)


@pytest.mark.parametrize("mode", ["omit", "pseudonym"])
def test_an_unmatched_path_does_not_leak_either(captured_spans, mode):
    """The 404 case. No template exists, and the raw path must still not appear."""
    app = _app()
    instrument_fastapi_safely(
        app,
        ObservabilitySettings(
            person_uid_mode=mode, pseudonym_salt="a-salt", _env_file=None, _secrets_dir=None
        ),
    )
    TestClient(app).get(f"/persons/{PERSON}/typo")

    assert not [value for value in _exported_values(captured_spans) if PERSON in value]


def test_plain_keeps_the_identifier(captured_spans):
    """Asserted as loudly as the others.

    A mechanism that quietly redacted in every mode would satisfy a one-sided suite
    while breaking the deployment that asked to see identifiers. `plain` is a
    decision the deployment took, and the package honours it.
    """
    app = _app()
    instrument_fastapi_safely(
        app,
        ObservabilitySettings(person_uid_mode="plain", _env_file=None, _secrets_dir=None),
    )
    TestClient(app).get(f"/persons/{PERSON}/photos")

    assert [value for value in _exported_values(captured_spans) if PERSON in value]
