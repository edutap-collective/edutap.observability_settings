"""Instrumenting a FastAPI application without defeating `person_uid_mode`.

`logfire.instrument_fastapi()` exports the request path, so a service whose
identifier sits in its path exports that identifier on every span. Measured on
2026-08-17: it appears in `logfire.msg`, `http.target`, `http.url` and
`fastapi.arguments.values`. Three of those come from the OpenTelemetry HTTP
conventions rather than from FastAPI argument capture, so the mapper logfire offers
reaches only the fourth.

Two OpenTelemetry naming generations are in play, not one. `http.target` and
`http.url` are the legacy semantic conventions; `opentelemetry-instrumentation-asgi`
emits `url.path` and `url.full` instead once a deployment sets the documented
migration switch `OTEL_SEMCONV_STABILITY_OPT_IN` (OpenTelemetry's own opt-in path
off the legacy names, https://opentelemetry.io/docs/specs/otel/http/). Measured with
that variable set: the legacy names come back as `<unmatched>` -- decoys nothing
produced -- while the new ones carry the raw path straight through. So the hook
below overwrites both generations unconditionally, whichever one the active
instrumentation actually populated. Writing a name that stayed unused is cosmetic
span noise; leaving one unwritten is a disclosure the deployment's environment
controls, which is not a knob this package hands out.

The hook itself fails open. `logfire`'s ASGI instrumentation wraps
`server_request_hook` in error handling that only records an exception on the span
rather than propagating it -- if the hook raises, the request still succeeds and the
raw path attributes stay exactly as the base instrumentation set them, unscrubbed.
There is no hook into that failure path from here: it is OpenTelemetry's contract to
`server_request_hook`, not this module's to change. The mitigation this module
offers instead is that `route_template()` must stay total on any scope it is given
-- see its docstring -- so the hook never has a reason to raise in the first place.

The full reasoning, and why the route template rather than a redaction pattern, is
in `docs/superpowers/specs/2026-08-17-safe-fastapi-instrumentation-design.md`.
"""

from collections.abc import Mapping, MutableMapping
from typing import Any, cast

import logfire
from fastapi import FastAPI
from opentelemetry.trace import Span
from starlette.applications import Starlette
from starlette.routing import Match

from .settings import ObservabilitySettings

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
        # `route.matches()` is typed for starlette's own `Scope` alias, which is a
        # `MutableMapping` package-wide (starlette/types.py) for the benefit of other
        # code in the ASGI lifecycle -- not because `matches()` itself writes
        # anything. Read (`Route.matches`, `Mount.matches` in starlette/routing.py):
        # it only reads the scope it is given and returns a freshly built
        # `child_scope`; the write-back (`scope.update(child_scope)`) happens in
        # `BaseRoute.__call__`, which this function never calls. `matches()` being
        # read-only is what makes the cast sound here, regardless of whether the
        # concrete object passed in actually supports mutation. This function's
        # public signature stays read-only `Mapping` -- callers only ever read a
        # request's scope here -- so the cast, not a wider parameter type, absorbs
        # the mismatch.
        match, _ = route.matches(cast(MutableMapping[str, Any], scope))
        if match is Match.FULL:
            return getattr(route, "path", UNMATCHED)
    return UNMATCHED


def _overwrite_path_attributes(app: FastAPI) -> Any:
    """Build the hook that replaces the path-bearing attributes with the template."""

    def server_request_hook(span: Span | None, scope: Mapping[str, Any]) -> None:
        if span is None:
            return
        template = route_template(app, scope)
        # Set rather than delete: the OpenTelemetry API has no removal, and an
        # attribute left in place is one that still carries the path.
        #
        # Both http.* (legacy) and url.* (stable, opt-in via
        # OTEL_SEMCONV_STABILITY_OPT_IN) are written unconditionally, because which
        # pair the running instrumentation actually populated is a deployment's
        # environment variable, not something this module observes. Whichever pair
        # goes unused sits as inert noise; the alternative -- writing only the pair
        # believed active -- means the wrong guess exports the identifier.
        span.set_attribute("http.target", template)
        span.set_attribute("http.url", template)
        span.set_attribute("url.path", template)
        span.set_attribute("url.full", template)
        span.set_attribute("logfire.msg", f"{scope.get('method', '')} {template}".strip())

    return server_request_hook


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
        request_attributes_mapper=kwargs.pop(
            "request_attributes_mapper", _reduce_request_attributes
        ),
        **kwargs,
    )
