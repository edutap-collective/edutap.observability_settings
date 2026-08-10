"""Where a service reports to, and how much of a person may travel with it."""

from typing import Literal

from edutap.data_models.settings import ServiceSettings
from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

#: What may stand in for a ``person_uid`` in an error report or a span.
#:
#: ``pseudonym`` keeps correlation without identification, ``plain`` is the honest
#: choice where the tracker is read by exactly the people who may read the directory
#: anyway, and ``omit`` drops the datum entirely.
PersonUidMode = Literal["pseudonym", "plain", "omit"]


class ObservabilitySettings(ServiceSettings):
    """Everything the reporting side of a service needs, and nothing it can fail on.

    Inherits :class:`~edutap.data_models.settings.ServiceSettings` rather than sitting
    beside it: ``environment``, ``telemetry_enabled`` and ``log_level`` are read here,
    and a service should not have to assemble two objects to start reporting.

    **No field is required.** Observability is installed *before* a service resolves
    the settings it needs to run, so that a process refusing to start is reported
    rather than silently absent. That ordering only holds if reading this can never
    fail for want of a value. A value that is present but not a legal one still
    fails -- ignoring it would decide in silence what leaves the process.

    The prefix is ``EDUTAP_`` and deliberately not per-service. These fields are
    defined by an eduTAP package, another university deploying them should not have to
    learn an LMU name, and the one field whose value genuinely differs per service --
    the Sentry DSN, which identifies one project -- differs by being set in that
    service's own environment block, not by carrying a different name.
    """

    model_config = SettingsConfigDict(
        env_prefix="EDUTAP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    #: A DSN identifies one project, so every service gets its own rather than sharing
    #: one. It is a write-only ingest key and thus low-sensitivity -- a plain
    #: environment variable is enough and a Docker secret would be effort without
    #: gain -- but it is still a credential, hence ``SecretStr``: ``BaseSettings``
    #: prints every plain field verbatim, and a settings object reaches a log line
    #: sooner or later. Unset means the integration stays off.
    sentry_dsn: SecretStr | None = None

    #: The HMAC key behind :func:`~edutap.observability_settings.pseudonym.pseudonym`.
    #: Without it there is no pseudonym at all, rather than one computed from an empty
    #: key -- that would be a plain digest of a small, enumerable value space, and
    #: reversible by anyone able to hash the directory.
    pseudonym_salt: SecretStr | None = None

    #: Whether a person may be named in what leaves the process. The default is the
    #: careful one, so that a deployment which has not thought about it does not
    #: publish identifiers by omission.
    person_uid_mode: PersonUidMode = "pseudonym"


#: Where the OTLP endpoint comes from -- the OpenTelemetry specification's own
#: variable, not a field of this class.
#:
#: An endpoint names a *receiver*, normally one per host or cluster, and which service
#: sent a span is carried in the resource attributes rather than in the address. A
#: field under ``EDUTAP_`` would be a second name for a value every OTel SDK already
#: reads by itself. This is the asymmetry with the Sentry DSN, which names a project
#: and therefore does belong to the service.
OTLP_ENDPOINT_VARIABLE = "OTEL_EXPORTER_OTLP_ENDPOINT"
