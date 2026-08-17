"""What the instrumentation may say about a request."""

import json
import subprocess
import sys

import logfire
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from edutap.observability_settings import ObservabilitySettings, instrumentation
from edutap.observability_settings.instrumentation import (
    UNMATCHED,
    _reduce_request_attributes,
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
    """Every attribute value of every captured span, as text -- plus the span name.

    `span.name` is exported alongside `span.attributes`, not inside it, so a helper
    that only walked `.attributes` would let an identifier leaking into the name
    itself pass unnoticed. It does not happen to leak there today (the name is
    `f"{method} {route_template}"`, logfire's own doing, not this module's), but the
    claim this helper backs is "every attribute of every span", and the name is
    part of what a span exports.
    """
    values = [str(span.name) for span in capture.spans]
    values += [str(value) for span in capture.spans for value in dict(span.attributes).values()]
    return values


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

    assert captured_spans.spans, "no span was exported; the test proves nothing"
    assert not [value for value in _exported_values(captured_spans) if PERSON in value]


@pytest.mark.parametrize("mode", ["omit", "pseudonym"])
def test_both_semantic_convention_generations_are_overwritten(captured_spans, mode):
    """Guards the leak an env var can silently reopen.

    `opentelemetry-instrumentation-asgi` writes `http.target`/`http.url` (the legacy
    OTel HTTP semantic conventions) by default, and `url.path`/`url.full` (the
    stable ones) instead once a deployment sets
    `OTEL_SEMCONV_STABILITY_OPT_IN=http`. Measured with that variable set: the
    legacy names come back as the `<unmatched>` placeholder -- decoys nothing
    produced -- while the new ones carry the raw identifier straight through.

    That variable cannot be exercised from inside this suite: OpenTelemetry's own
    stability class reads it exactly once per process and caches the result
    (`_OpenTelemetrySemanticConventionStability._initialize`, guarded by an
    `_initialized` flag with no reset). Measured directly -- setting the variable
    after any earlier `instrument_fastapi()` call in the same process, including an
    earlier test in this same suite, has no effect on the names that call produces.
    A test that set the variable here would therefore either do nothing (if an
    earlier test already initialised the default) or pass for a reason that stops
    holding the moment test order changes -- neither is a real guard.

    So the guard is one step earlier: both attribute-name generations are written
    unconditionally by the hook, regardless of which one the active instrumentation
    populated. This asserts exactly that -- both are present, both carry the
    template -- which is what makes the fix correct under either convention rather
    than under whichever one happens to be running in this test process.
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
    attributes = dict(captured_spans.spans[0].attributes)
    template = "/persons/{person_uid}/photos"
    for name in ("http.target", "http.url", "url.path", "url.full", "logfire.msg"):
        assert name in attributes, f"{name} was not written by the hook"
    assert attributes["http.target"] == template
    assert attributes["http.url"] == template
    assert attributes["url.path"] == template
    assert attributes["url.full"] == template


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


def _app_with_body() -> FastAPI:
    """An app whose validation errors can carry an identifier.

    Pydantic reports a missing field against the *model*, so the error's `input` is
    the whole enclosing dict -- identifier included -- on a plain 422 with no
    exception anywhere in the picture.
    """
    from pydantic import BaseModel

    class Lookup(BaseModel):
        person_uid: str
        fields: list[str]

    app = FastAPI()

    @app.post("/lookup")
    async def lookup(body: Lookup) -> dict:
        return {}

    return app


@pytest.mark.parametrize("mode", ["omit", "pseudonym"])
def test_endpoint_arguments_do_not_reach_the_span(captured_spans, mode):
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
def test_a_validation_error_does_not_carry_the_rejected_input(captured_spans, mode):
    """The sole guard on the 422 path, so it asserts both halves of the contract.

    Without the `captured_spans.spans` guard this test would go green on the day
    logfire stopped creating a span for a rejected request -- proving nothing while
    being the only test that covers `errors` at all. And a reducer that dropped
    `errors` outright would also pass a leak-only assertion, so what survives is
    asserted too: `type` and `loc` are the operational half of the contract, and
    losing them would make a 422 unreadable.
    """
    app = _app_with_body()
    instrument_fastapi_safely(
        app,
        ObservabilitySettings(
            person_uid_mode=mode, pseudonym_salt="a-salt", _env_file=None, _secrets_dir=None
        ),
    )
    # `fields` is missing, so the error is reported against the model and its
    # `input` is the whole body -- with the identifier in it.
    TestClient(app).post("/lookup", json={"person_uid": PERSON})

    assert captured_spans.spans, "no span was exported; the test proves nothing"
    assert not [value for value in _exported_values(captured_spans) if PERSON in value]

    errors = _sole_attribute(captured_spans, "fastapi.arguments.errors")
    assert errors, "the reducer dropped the errors entirely; a 422 would be unreadable"
    # logfire serialises a non-scalar attribute to JSON on its way onto the span, so
    # what comes back is text rather than the list the reducer returned.
    entries = json.loads(errors) if isinstance(errors, str) else errors
    for entry in entries:
        assert entry.get("type"), "the error kind did not survive the reducer"
        assert entry.get("loc"), "the rejected field's location did not survive the reducer"


def _sole_attribute(capture, name: str) -> object:
    """Return the one value of `name` across all captured spans.

    logfire sets `fastapi.arguments.*` on the endpoint span rather than on the
    request span, and which index that is depends on export order -- so this looks
    for the attribute rather than for a span.
    """
    found = [dict(span.attributes)[name] for span in capture.spans if name in dict(span.attributes)]
    assert found, f"no exported span carried {name}"
    return found[0]


@pytest.mark.parametrize("mode", ["omit", "pseudonym"])
def test_an_unrecognised_attribute_does_not_survive_the_reducer(captured_spans, mode):
    """The reducer is an allow-list, and this is what makes that claim testable.

    A reducer built as `dict(attributes)` with two keys rewritten passes every other
    test in this file while letting a third key through untouched. logfire hands the
    mapper exactly `values` and `errors` today -- checked against
    `logfire/_internal/integrations/fastapi.py` on 2026-08-17 -- so the extra key is
    injected here rather than provoked: the point is the posture, not a leak that
    exists in the pinned version.
    """
    reduced = _reduce_request_attributes(
        object(),
        {"values": {"person_uid": PERSON}, "errors": [], "headers": {"authorization": PERSON}},
    )

    assert "headers" not in reduced
    assert set(reduced) == {"values", "errors"}
    assert PERSON not in str(reduced)


def test_a_non_mapping_values_is_dropped_rather_than_kept():
    """The type checks guard survival, not merely the reduction.

    In the previous shape `isinstance(values, dict)` decided whether to *reduce* a
    value that had already been copied into the result -- so anything of an
    unexpected type was kept verbatim. That is the opposite of what the docstring
    promises, and it is precisely the case where the value is least understood.
    """
    reduced = _reduce_request_attributes(object(), {"values": PERSON, "errors": PERSON})

    assert PERSON not in str(reduced)
    assert reduced["values"] == {"argument_count": 0}
    assert reduced["errors"] == []


@pytest.mark.parametrize("mode", ["omit", "pseudonym"])
def test_a_raising_route_template_fails_closed(captured_spans, monkeypatch, mode):
    """The hook fails open, so this module has to fail closed inside it.

    OpenTelemetry wraps `server_request_hook` in a `failsafe` that records the
    exception on the span and lets the request continue -- so before the try/except
    a raising `route_template()` meant a 200 response and the raw path still sitting
    in `logfire.msg`, `http.target` and `http.url`. `route_template()` is not total:
    a scope without `"path"` raises `KeyError`, and any third-party `BaseRoute` in
    the route table may raise out of its own `matches()`. This simulates that whole
    class of failure with the bluntest possible instance of it.
    """
    app = _app()
    instrument_fastapi_safely(
        app,
        ObservabilitySettings(
            person_uid_mode=mode, pseudonym_salt="a-salt", _env_file=None, _secrets_dir=None
        ),
    )

    def _explode(*args, **kwargs):
        raise RuntimeError("a third-party route matcher did something unexpected")

    monkeypatch.setattr(instrumentation, "route_template", _explode)
    TestClient(app).get(f"/persons/{PERSON}/photos")

    assert captured_spans.spans, "no span was exported; the test proves nothing"
    assert not [value for value in _exported_values(captured_spans) if PERSON in value]


def test_the_call_is_public_api():
    """A consumer imports it from the package, not from a private module path."""
    import edutap.observability_settings as package

    assert "instrument_fastapi_safely" in package.__all__
    assert package.instrument_fastapi_safely is instrument_fastapi_safely


def test_importing_the_package_does_not_import_fastapi():
    """The PEP 562 `__getattr__` in `__init__.py`, which had no test at all.

    A worker installs this package without the `fastapi` extra precisely so it does
    not have to carry a web framework, and its plain
    `import edutap.observability_settings` must not raise. The deferral is what makes
    that true, and it is one line away from being silently undone by somebody
    tidying the module-level imports.

    In a subprocess, not in this process. By the time this test runs, `fastapi` is in
    `sys.modules` several times over -- this very file imports it at module level --
    so an in-process assertion would be either vacuously false or artificially
    arranged. A fresh interpreter is the only place the claim means anything. `-E`
    and `-s` keep a developer's `PYTHONSTARTUP`/user site-packages out of it; the
    interpreter is `sys.executable`, so it is this venv either way.
    """
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, no external input
        [
            sys.executable,
            "-E",
            "-s",
            "-c",
            "import sys; import edutap.observability_settings; "
            "print('fastapi' in sys.modules or 'starlette' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "False", (
        "importing the package pulled in a web framework; the PEP 562 __getattr__ in "
        "__init__.py is what prevents that"
    )


def test_the_deferred_name_still_resolves_in_a_fresh_interpreter():
    """The other half: laziness must not have turned the name into a broken one.

    A `__getattr__` that raised, or that was deleted along with the `TYPE_CHECKING`
    companion, would pass the laziness test above while making the documented usage
    fail. Again in a subprocess, so that nothing this suite already imported props
    the answer up.
    """
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, no external input
        [
            sys.executable,
            "-E",
            "-s",
            "-c",
            "import edutap.observability_settings as p; "
            "print(p.instrument_fastapi_safely.__name__)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "instrument_fastapi_safely"
