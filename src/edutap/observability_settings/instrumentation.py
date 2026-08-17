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

The hook itself fails open, and this module has to fail closed inside it.
OpenTelemetry wraps `server_request_hook` in a `failsafe` that records a raising
exception on the span and lets the request continue -- so if the hook raises, the
request still succeeds and the raw path attributes stay exactly as the base
instrumentation set them, unscrubbed. Measured on 2026-08-17 with `route_template`
made to raise: the request returned 200 and the span still carried the raw path in
`logfire.msg`, `http.target` and `http.url`. That failure path is OpenTelemetry's
contract, not this module's to change.

An earlier version of this file claimed the mitigation was that `route_template()`
stays total. It is not total, and the claim was wrong twice over: a scope without
`"path"` raises `KeyError` inside starlette's matcher, and any `BaseRoute` subclass
in `app.router.routes` -- a third-party mount, a custom router -- whose `matches()`
raises propagates straight out. Totality delegated to third-party implementations is
exactly the kind of trust this module rejects everywhere else. So the hook catches
instead and fails **closed**: any exception out of `route_template()` becomes
`UNMATCHED`, the attributes are still overwritten, and what leaves the process is a
useless placeholder rather than an identifier.

**Span events are outside this module's reach.** Everything here operates on span
*attributes* -- the hook and the mapper are the only two seams logfire offers, and
neither sees an event. An endpoint that raises
`RuntimeError(f"no such person {person_uid}")` records that text in
`exception.message` on a span event, and nothing in this file touches it. That text
is the calling service's, so keeping identifiers out of it is the calling service's
responsibility -- but a service whose identifier sits in its path is exactly the
service whose "not found" and "not permitted" handlers will reach for it, so it is
worth saying rather than leaving to be discovered.

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

    **This is not total, and callers must not assume it is.** A scope without
    `"path"` raises `KeyError` out of starlette's matcher, and `matches()` belongs to
    whatever `BaseRoute` subclasses the application mounted -- a third-party router
    may raise anything at all. The caller inside this module handles that by failing
    closed to `UNMATCHED`; a caller elsewhere has to make the same decision
    deliberately.
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
        try:
            template = route_template(app, scope)
        except Exception:
            # Fail closed. OpenTelemetry's `failsafe` around this hook records a
            # raising exception on the span and lets the request through, which
            # leaves the base instrumentation's raw path attributes standing --
            # measured: 200 returned, identifier still in `logfire.msg`,
            # `http.target` and `http.url`. `route_template()` is not total: a scope
            # without `"path"` raises `KeyError`, and any third-party `BaseRoute`
            # subclass in the route table may raise out of its own `matches()`.
            #
            # A bare `except Exception` rather than an enumeration of what can go
            # wrong, because the whole point is the failure nobody enumerated. The
            # exception is deliberately not re-raised and not logged here: this runs
            # inside span creation for every request, and the safe outcome -- a
            # placeholder where the path would have been -- is achieved by
            # continuing.
            template = UNMATCHED
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

    **Implemented as an allow-list, not as a copy with two keys rewritten.** The
    earlier version did `dict(attributes)` and then overwrote `values` and `errors`,
    which contradicted the paragraph above in two ways: a third key that a future
    logfire release adds to this mapping would pass straight through unread, and a
    `values` that is not a `dict` or an `errors` that is not a `list` would be
    *kept* rather than dropped -- the type checks guarded the reduction rather than
    the survival. The path half of this module is safe by construction and this half
    was not. It builds a fresh mapping now, so a key nobody here recognises does not
    exist in the result.

    Nothing operational is lost by that. Confirmed against
    `logfire/_internal/integrations/fastapi.py` on 2026-08-17: the mapper is handed
    exactly `{'values': ..., 'errors': ...}`, built literally at the call site, and
    logfire reads back only those two names afterwards.
    """
    values = attributes.get("values")
    errors = attributes.get("errors")

    return {
        "values": {"argument_count": len(values) if isinstance(values, dict) else 0},
        "errors": [
            {"type": entry.get("type"), "loc": entry.get("loc")}
            for entry in (errors if isinstance(errors, list) else [])
            if isinstance(entry, dict)
        ],
    }


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

    The two hooks are handled asymmetrically on purpose. `request_attributes_mapper`
    may be overridden through `**kwargs` -- a service that knows its own models can
    judge its arguments by shape, which this package cannot -- but
    `server_request_hook` may not: it is the whole path defence, and a consumer that
    replaced it would silently get the raw URL back in five attributes while still
    calling something named "safely". Passing one therefore raises `TypeError: got
    multiple values for keyword argument 'server_request_hook'`, which is loud and at
    the call site. A service that genuinely needs its own request hook calls
    `logfire.instrument_fastapi()` directly and owns the outcome.
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
