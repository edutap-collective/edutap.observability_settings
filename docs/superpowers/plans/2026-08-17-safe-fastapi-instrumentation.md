# Safe FastAPI Instrumentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give this package a FastAPI instrumentation call whose exported spans respect `person_uid_mode`, so a service with an identifier in its URL path can be instrumented without defeating the setting.

**Architecture:** One new module, `instrumentation.py`, with a pure route-template resolver and a wrapper around `logfire.instrument_fastapi()`. The wrapper installs two hooks: an OpenTelemetry `server_request_hook` that overwrites the three path-bearing span attributes, and a logfire `request_attributes_mapper` that reduces captured endpoint arguments and validation errors. In `plain` mode both hooks are omitted entirely and the call is a straight pass-through.

**Tech Stack:** Python 3.13+, logfire, opentelemetry-instrumentation-fastapi (via `logfire[fastapi]`), starlette routing, pytest.

**Spec:** `docs/superpowers/specs/2026-08-17-safe-fastapi-instrumentation-design.md`

## Global Constraints

- **English only** — code, comments, docstrings, commit messages, PR bodies. Repository rule.
- **Test-first.** Every task writes the failing test, runs it, then implements.
- **Real spans, never mocks**, for anything asserting what leaves the process. A mock cannot show that a future logfire version added a fifth attribute.
- **No dependency on any eduTAP service.** This package is imported before a service resolves its settings.
- `make lint` and `make test-local` green before the pull request.
- Package version stays `0.1.3` in this branch; the release bump is a separate step after review.

## Measured facts this plan rests on

All measured 2026-08-17 against the versions in `.venv`, with a real span exporter.

1. `logfire.instrument_fastapi()` exports the raw path in four attributes: `logfire.msg`, `http.target`, `http.url`, `fastapi.arguments.values`. The span **name** is the route template and is already safe.
2. `http.route` is present on the span and holds the template.
3. `server_request_hook` fires **before routing**, so `scope["route"]` is absent there — reading it yields nothing. The template must be resolved from the app's own route table.
4. `span.set_attribute()` inside `server_request_hook` successfully overwrites `http.target`, `http.url` and `logfire.msg`.
5. An unmatched path (a 404) has no template at all. This is the case that leaks if the fallback is the raw path.

---

### Task 1: Resolve a request's route template

**Files:**
- Create: `src/edutap/observability_settings/instrumentation.py`
- Test: `tests/test_instrumentation.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `route_template(app: Starlette, scope: Mapping[str, Any]) -> str` — returns the matched route's path template, or the constant `UNMATCHED` when nothing matches. Also exports `UNMATCHED: str = "<unmatched>"`.

- [ ] **Step 1: Write the failing test**

```python
"""What the instrumentation may say about a request."""

from fastapi import FastAPI

from edutap.observability_settings.instrumentation import UNMATCHED, route_template


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd uses_libraries/edutap.observability_settings && .venv/bin/python -m pytest tests/test_instrumentation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'edutap.observability_settings.instrumentation'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Instrumenting a FastAPI application without defeating `person_uid_mode`.

`logfire.instrument_fastapi()` exports the request path, so a service whose
identifier sits in its path exports that identifier on every span. Measured on
2026-08-17: it appears in `logfire.msg`, `http.target`, `http.url` and
`fastapi.arguments.values`. Three of those come from the OpenTelemetry HTTP
conventions rather than from FastAPI argument capture, so the mapper logfire offers
reaches only the fourth.

The full reasoning, and why the route template rather than a redaction pattern, is
in `docs/superpowers/specs/2026-08-17-safe-fastapi-instrumentation-design.md`.
"""

from collections.abc import Mapping
from typing import Any

from starlette.applications import Starlette
from starlette.routing import Match

#: Stands in for the template of a request that matched no route.
#:
#: A 404 has no template, and the raw path is precisely what must not be exported --
#: so this is a constant rather than a fallback to `scope["path"]`. It is the case a
#: "just use the template" rule silently gets wrong.
UNMATCHED = "<unmatched>"


def route_template(app: Starlette, scope: Mapping[str, Any]) -> str:
    """Return the path template of the route this request matches.

    Resolved from the application's own route table rather than read from
    `scope["route"]`: the hook this is called from runs *before* routing, so that key
    is not there yet. Measured -- reading it yields nothing at all rather than
    something stale.
    """
    for route in app.router.routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return getattr(route, "path", UNMATCHED)
    return UNMATCHED
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd uses_libraries/edutap.observability_settings && .venv/bin/python -m pytest tests/test_instrumentation.py -v`
Expected: PASS, 3 tests

- [ ] **Step 5: Commit**

```bash
git add src/edutap/observability_settings/instrumentation.py tests/test_instrumentation.py
git commit -m "feat(instrumentation): resolve a request's route template

Resolved from the application's route table rather than from scope['route']:
the hook it will be called from runs before routing, so that key is absent.

An unmatched path returns a constant and never the raw path -- a 404 has no
template, and falling back to the path is the case a 'just use the template'
rule silently gets wrong."
```

---

### Task 2: Keep the identifier out of the three path attributes

This is the load-bearing task. Its test is the one that found the leak.

**Files:**
- Modify: `src/edutap/observability_settings/instrumentation.py`
- Test: `tests/test_instrumentation.py`

**Interfaces:**
- Consumes: `route_template()`, `UNMATCHED` from Task 1.
- Produces: `instrument_fastapi_safely(app: FastAPI, settings: ObservabilitySettings | None = None, **kwargs: Any) -> None`. Reads `ObservabilitySettings()` when `settings` is omitted. Extra keyword arguments are forwarded to `logfire.instrument_fastapi`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_instrumentation.py`:

```python
import logfire
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from edutap.observability_settings import ObservabilitySettings
from edutap.observability_settings.instrumentation import instrument_fastapi_safely

PERSON = "ab12cd34@lmu.de"


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd uses_libraries/edutap.observability_settings && .venv/bin/python -m pytest tests/test_instrumentation.py -v`
Expected: FAIL — `ImportError: cannot import name 'instrument_fastapi_safely'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/edutap/observability_settings/instrumentation.py`:

```python
import logfire
from fastapi import FastAPI
from opentelemetry.trace import Span

from .settings import ObservabilitySettings


def _overwrite_path_attributes(app: FastAPI) -> Any:
    """Build the hook that replaces the path-bearing attributes with the template."""

    def server_request_hook(span: Span | None, scope: Mapping[str, Any]) -> None:
        if span is None:
            return
        template = route_template(app, scope)
        # Set rather than delete: the OpenTelemetry API has no removal, and an
        # attribute left in place is one that still carries the path.
        span.set_attribute("http.target", template)
        span.set_attribute("http.url", template)
        span.set_attribute("logfire.msg", f"{scope.get('method', '')} {template}".strip())

    return server_request_hook


def instrument_fastapi_safely(
    app: FastAPI,
    settings: ObservabilitySettings | None = None,
    **kwargs: Any,
) -> None:
    """Instrument `app`, honouring `person_uid_mode` in what the spans carry.

    A separate call rather than a parameter on `install_observability`: not every
    service in this estate is a FastAPI service, `logfire[fastapi]` is an optional
    extra, and a worker must not have to install it to configure its logging.

    In `plain` mode this is a straight pass-through to `logfire.instrument_fastapi`.
    That is not an oversight to be tightened later -- a package that redacted anyway
    would be taking a decision belonging to the deployment, and the operator who
    asked to see identifiers would have no way to get them back.
    """
    settings = settings or ObservabilitySettings()

    if settings.person_uid_mode == "plain":
        logfire.instrument_fastapi(app, **kwargs)
        return

    logfire.instrument_fastapi(
        app,
        server_request_hook=_overwrite_path_attributes(app),
        **kwargs,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd uses_libraries/edutap.observability_settings && .venv/bin/python -m pytest tests/test_instrumentation.py -v`
Expected: PASS for `test_the_route_is_still_identifiable`, `test_an_unmatched_path_does_not_leak_either` and `test_plain_keeps_the_identifier`.

`test_no_exported_attribute_carries_the_identifier` is expected to **still FAIL** at this point: `fastapi.arguments.values` is untouched until Task 3. Confirm the failure message names that attribute, then proceed — this is the one place in this plan where a test stays red across a task boundary, and it is deliberate: the two halves of the leak have different causes and deserve separate review.

- [ ] **Step 5: Commit**

```bash
git add src/edutap/observability_settings/instrumentation.py tests/test_instrumentation.py
git commit -m "feat(instrumentation): keep the identifier out of the path attributes

http.target, http.url and logfire.msg carry the raw request path, and three of
the four known leaks are therefore OpenTelemetry HTTP conventions rather than
FastAPI argument capture. In the two non-plain modes they are replaced by the
route template, which is safe by construction: no dynamic segment survives.

plain is a pass-through, asserted as loudly as the other modes.

fastapi.arguments.values is still open; the shape-free assertion stays red until
the next commit."
```

---

### Task 3: Reduce captured endpoint arguments and validation errors

**Files:**
- Modify: `src/edutap/observability_settings/instrumentation.py`
- Test: `tests/test_instrumentation.py`

**Interfaces:**
- Consumes: `instrument_fastapi_safely()` from Task 2.
- Produces: no new public name. `instrument_fastapi_safely` gains a `request_attributes_mapper` keyword whose default is this module's reducing mapper; passing your own replaces it and takes over the risk.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_instrumentation.py`:

```python
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

    assert not [value for value in _exported_values(captured_spans) if PERSON in value]


@pytest.mark.parametrize("mode", ["omit", "pseudonym"])
def test_a_validation_error_does_not_carry_the_rejected_input(captured_spans, mode):
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

    assert not [value for value in _exported_values(captured_spans) if PERSON in value]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd uses_libraries/edutap.observability_settings && .venv/bin/python -m pytest tests/test_instrumentation.py -v`
Expected: FAIL on both new tests, and on `test_no_exported_attribute_carries_the_identifier` from Task 2. The failing values come from `fastapi.arguments.values` and `fastapi.arguments.errors`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/edutap/observability_settings/instrumentation.py`, and change `instrument_fastapi_safely` to pass the mapper:

```python
def _reduce_request_attributes(request: object, attributes: dict) -> dict:
    """Replace every captured endpoint argument, and every rejected input, with its shape.

    **The default is drop, not pass-through.** Endpoint parameter *names* are not a
    boundary this package controls: a body parameter can be called anything a future
    endpoint author chooses, and a query parameter could be named `person_uid`
    outright. This package also cannot judge a value by its shape the way a single
    service can -- it does not know the service's models. So nothing survives, and a
    service that needs an argument on its spans passes its own mapper and owns that
    decision.

    `errors` is reduced separately because of a Pydantic detail rather than a FastAPI
    one: a "missing field" error's `input` is not the missing value but the whole
    enclosing dict, because the error is reported against the model. `type` and `loc`
    survive -- which field, what kind of problem, from Pydantic's own fixed
    vocabulary -- and `input` and `msg` do not; a custom validator can put a value
    into `msg`.
    """
    reduced = dict(attributes)

    values = reduced.get("values")
    if isinstance(values, dict):
        reduced["values"] = {"argument_count": len(values)}

    errors = reduced.get("errors")
    if isinstance(errors, list):
        reduced["errors"] = [
            {"type": entry.get("type"), "loc": entry.get("loc")}
            for entry in errors
            if isinstance(entry, dict)
        ]

    return reduced
```

Then in `instrument_fastapi_safely`, replace the non-`plain` branch:

```python
    logfire.instrument_fastapi(
        app,
        server_request_hook=_overwrite_path_attributes(app),
        request_attributes_mapper=kwargs.pop(
            "request_attributes_mapper", _reduce_request_attributes
        ),
        **kwargs,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd uses_libraries/edutap.observability_settings && .venv/bin/python -m pytest tests/test_instrumentation.py -v`
Expected: PASS, all tests including `test_no_exported_attribute_carries_the_identifier`.

- [ ] **Step 5: Commit**

```bash
git add src/edutap/observability_settings/instrumentation.py tests/test_instrumentation.py
git commit -m "feat(instrumentation): reduce captured arguments and rejected inputs

Default is drop, not pass-through: parameter names are not a boundary this
package controls, and unlike a single service it cannot judge a value by its
shape -- it does not know the service's models.

Validation errors are reduced separately. Measured in edutap.data_provider and
unchanged here: a missing-field error's `input` is the whole enclosing dict, so
an identifier reaches the span on a plain 422 with no exception in the picture."
```

---

### Task 4: Publish the call, its extra, and how to use it

**Files:**
- Modify: `src/edutap/observability_settings/__init__.py`
- Modify: `pyproject.toml:54` (the `[project.optional-dependencies]` table)
- Modify: `README.md`
- Test: `tests/test_instrumentation.py`

**Interfaces:**
- Consumes: `instrument_fastapi_safely` from Tasks 2 and 3.
- Produces: `from edutap.observability_settings import instrument_fastapi_safely` as public API.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_instrumentation.py`:

```python
def test_the_call_is_public_api():
    """A consumer imports it from the package, not from a private module path."""
    import edutap.observability_settings as package

    assert "instrument_fastapi_safely" in package.__all__
    assert package.instrument_fastapi_safely is instrument_fastapi_safely
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd uses_libraries/edutap.observability_settings && .venv/bin/python -m pytest tests/test_instrumentation.py::test_the_call_is_public_api -v`
Expected: FAIL — `AssertionError` on the `__all__` membership.

- [ ] **Step 3: Write minimal implementation**

In `src/edutap/observability_settings/__init__.py`, add the import and the `__all__` entry, keeping both lists alphabetical:

```python
from .instrumentation import instrument_fastapi_safely
```

```python
__all__ = [
    "OTLP_ENDPOINT_VARIABLE",
    "ObservabilitySettings",
    "PersonUidMode",
    "install_observability",
    "instrument_fastapi_safely",
    "logfire_options",
    "person_label",
    "pseudonym",
    "sentry_options",
]
```

In `pyproject.toml`, add the extra after the `dev` entry and before `docs`:

```toml
# The FastAPI half of logfire, kept out of the core install: a worker configures its
# logging from this package and must not have to carry a web framework's
# instrumentation to do it.
fastapi = ["logfire[fastapi]>=4.40"]
```

Add `"edutap.observability_settings[fastapi]"` to the `dev` extra so the test suite can import it.

In `README.md`, extend the usage block:

````markdown
### FastAPI services

```python
from edutap.observability_settings import install_observability, instrument_fastapi_safely

install_observability(service_name="edutap.image_service")
instrument_fastapi_safely(app)
```

Needs the `fastapi` extra. It exists because `logfire.instrument_fastapi()` exports
the request path: a service whose identifier sits in its URL exports that identifier
on every span, whatever `person_uid_mode` says. In `omit` and `pseudonym` the path
attributes are replaced by the route template and captured endpoint arguments are
dropped; in `plain` the call is a straight pass-through, because a deployment that
asked to see identifiers gets to see them.
````

- [ ] **Step 4: Run the whole suite and the linters**

Run: `cd uses_libraries/edutap.observability_settings && make test-local && make lint`
Expected: all tests PASS, `ruff check`, `ruff format --check` and `ty check` clean.

- [ ] **Step 5: Commit**

```bash
git add src/edutap/observability_settings/__init__.py pyproject.toml README.md tests/test_instrumentation.py
git commit -m "feat: publish instrument_fastapi_safely behind a fastapi extra

The extra is kept out of the core install: a worker configures its logging from
this package and must not have to carry a web framework's instrumentation."
```

---

## After the tasks

- [ ] Open the pull request against `main` of `edutap-collective/edutap.observability_settings`, linking the design record.
- [ ] Version bump and release are **not** part of this branch. `edutap.image_service` needs a released version to depend on, and its own plan cannot be written until that version exists — which is why the image_service work is a separate spec and plan rather than a later task here.

## Deliberately not in this plan

- **`edutap.data_provider` is not migrated onto this.** Recorded in the design record with its reasoning: its local scrubber solves the quarter of the problem it has, it is deployed, and folding it in is a third change to a service not otherwise being touched.
- **The `image_service` event catalogue.** Agreed in discussion — operational events only, the database stays the review register — but it belongs to that repository and gets its own spec and plan.
