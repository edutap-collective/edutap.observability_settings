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

from collections.abc import Mapping, MutableMapping
from typing import Any, cast

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
