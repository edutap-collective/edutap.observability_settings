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

from .install import install_observability, logfire_options, sentry_options
from .pseudonym import person_label, pseudonym
from .settings import OTLP_ENDPOINT_VARIABLE, ObservabilitySettings, PersonUidMode

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
    """
    if name == "instrument_fastapi_safely":
        from .instrumentation import instrument_fastapi_safely

        return instrument_fastapi_safely
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
