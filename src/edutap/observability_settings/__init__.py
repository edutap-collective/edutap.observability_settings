"""How an eduTAP service reports what it is doing, and what it may say about people.

Three backends with three jobs: Sentry takes errors, an OTLP collector takes traces
and metrics, structlog produces the records that reach both. This package holds the
settings for all three and the one call that applies them.

It exists because the options are the point, not the code. Which Sentry options are
on decides whether a bearer token or a person's identifier leaves the process, and
each of them was chosen against a measurement rather than against a backend's
recommendation. Separated from the ``sentry_sdk.init()`` call that applies them those
measurements are worth nothing, which is why the settings live here and not in
``edutap.data_models``.

What must never appear here is a dependency on any eduTAP service. This is installed
before a service resolves its own settings; it can know nothing about them.
"""

from typing import TYPE_CHECKING

from .install import install_observability, logfire_options, sentry_options
from .pseudonym import person_label, pseudonym
from .settings import OTLP_ENDPOINT_VARIABLE, ObservabilitySettings, PersonUidMode

# `instrument_fastapi_safely` is published two ways, for two different readers, and
# both halves below are load-bearing:
#
# - The runtime `__getattr__` further down defers the actual import to first access,
#   so that a plain `import edutap.observability_settings` never imports `fastapi` --
#   see its docstring for why that matters. This is what a worker without the
#   `fastapi` extra depends on at runtime.
# - This `TYPE_CHECKING` import is never executed -- `TYPE_CHECKING` is `False` at
#   runtime -- so it costs nothing there, but it is what `ty`/`mypy` see, and it is
#   what gives a consumer's typechecker a real signature for the name instead of the
#   `object` that `__getattr__`'s return type would otherwise leave it with. Without
#   this, `edutap.image_service` -- the reason this call was published -- would fail
#   `ty check` on the exact usage this package's own README documents.
#
# Deleting the `__getattr__` re-introduces the hard dependency on a web framework;
# deleting this import silently turns the published name back into `object` for
# every downstream typechecker. Removing either one breaks a different consumer.
if TYPE_CHECKING:
    from .instrumentation import instrument_fastapi_safely

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


def __getattr__(name: str) -> object:
    """Resolve `instrument_fastapi_safely` on first access, not on import.

    `.instrumentation` imports `fastapi` and `starlette` at module level, so a plain
    `from .instrumentation import instrument_fastapi_safely` up top would make a web
    framework a hard dependency of every consumer of this package -- including a
    worker that installs it without the `fastapi` extra specifically so it does not
    have to carry one. Its plain `import edutap.observability_settings` would then
    raise `ImportError`, which defeats the reason the extra exists in the first
    place. PEP 562 defers the import to the moment something actually asks for the
    name, so a worker that never asks never pays for it.

    The `TYPE_CHECKING` import above this function carries the type a downstream
    typechecker sees for the same name; this function is the runtime behaviour
    behind it and is never consulted by a typechecker itself.
    """
    if name == "instrument_fastapi_safely":
        from .instrumentation import instrument_fastapi_safely

        return instrument_fastapi_safely
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
