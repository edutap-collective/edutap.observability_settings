# `edutap.observability_settings` — design

**Date:** 2026-08-10
**Status:** decided (A. Loechel), first implementation in this repository

A package for how an eduTAP service reports what it is doing — and for one decision
that is not a technical one: what a service may say about a person.

## Why a package and not three lines

Three lines start Sentry. Which options those three lines carry decides whether a
bearer token or a person's identifier leaves the process, and every option below was
chosen against a **measurement** rather than against the backend's recommendation.
The measurements are recorded in `edutap.data_provider`'s own observability design
record, `docs/superpowers/specs/2026-08-04-observability-design.md`, from which the
Sentry half of this package is taken.

Separated from the `sentry_sdk.init()` call that applies them, those measurements are
worth nothing — the next service copies the three lines and not the reasoning. That
is the whole argument for a package.

It also settles where the settings live. `edutap.data_models` keeps `ServiceSettings`
— `environment`, `telemetry_enabled`, `log_level`, the fields a service has whether or
not it reports anywhere — and everything about *reporting* moved here, `SentrySettings`
included.

## Three backends, three jobs, no overlap

| Backend | Takes | Configured by |
|---|---|---|
| Sentry (Bugsink) | errors | `EDUTAP_SENTRY_DSN`, one project per service |
| OTLP collector | traces, metrics, exported logs | `OTEL_EXPORTER_OTLP_ENDPOINT` |
| structlog | the records that reach both | `EDUTAP_LOG_LEVEL` |

Nothing travels two paths: `traces_sample_rate=0` keeps Sentry's own tracing off,
because the spans already go to the collector and Bugsink states that it does not
support traces.

`logfire.StructlogProcessor` is the bridge between the third and the second, so a log
line and the span it happened inside share a trace id without the caller doing
anything.

## Two variables, two shapes, one asymmetry

A **Sentry DSN names a project**, so it belongs to the service and every service gets
its own. An **OTLP endpoint names a receiver**, normally one per host or cluster, and
which service sent a span rides in the resource attributes — `service.name` — not in
the address.

Hence: `EDUTAP_SENTRY_DSN` is a field of this package; the OTLP endpoint is not. Every
OpenTelemetry SDK already reads `OTEL_EXPORTER_OTLP_ENDPOINT`, and a field under
`EDUTAP_` would be a second name for the same value.

```{important}
The prefix `EDUTAP_` is shared across the estate, which means shared **names**, not
shared **values**. `EDUTAP_SENTRY_DSN` differs per service by being set in that
service's own compose `environment:` block. Only genuinely stack-wide values belong in
a shared `.env` — the same rule that applies to a Kafka consumer group.
```

The prefix is deliberately not per-service: the fields are defined by an eduTAP
package, and another university deploying them should not have to learn an LMU name.

## What may be said about a person

A `person_uid` at a university is not an opaque handle. At the LMU it is the LMU
identifier with `@lmu.de` appended; the circle able to turn that back into a human
being is far wider than the circle holding directory administration rights. Even
pseudonymised it stays personal data. The question a deployment answers is therefore
not *whether* but *who may see it*.

| Mode | What travels | When it is right |
|---|---|---|
| `pseudonym` | a keyed, 12-character label | The default. Correlation survives, identification does not. |
| `plain` | the `person_uid` | Where the tracker is read by exactly the people who may read the directory anyway. |
| `omit` | nothing | Where the tracker is read more widely than the directory. |

**The decision is configuration, not code.** The package default is `pseudonym`,
because a deployment that has not thought about it must not publish identifiers by
omission. A deployment that has thought about it says so. At the LMU the circle is the
directory administrators and is expected to stay that way, which makes `plain`
defensible there — and it is set there, not here.

```{warning}
`pseudonym` without a salt yields **nothing**, never the raw value. A deployment that
asked for pseudonyms and forgot the key has to lose the datum rather than publish it.
An empty salt counts as no salt: compose writes `${VAR:-}`, which sets a variable to
the empty string, and an HMAC under an empty key is a plain digest of a small,
enumerable value space — reversible by anyone able to hash the directory.
```

The label is truncated to 12 hex characters, 48 bits: wide enough that a collision
within one installation is not a practical concern, short enough that it reads as a
label rather than as an identifier worth storing.

## Measured, not assumed

Everything in this section was established by running it against the versions named,
and describes what those versions did on 2026-08-10 — not what they guarantee.

**logfire honours the OpenTelemetry endpoint variable.** With `send_to_logfire=False`
and `OTEL_EXPORTER_OTLP_ENDPOINT` set, logfire 4.40 installs a `BatchSpanProcessor`
carrying an `OTLPSpanExporter`. Nothing has to be built for the export path.

**Without the endpoint, nothing is exported at all.** Same configuration, variable
unset: the processor chain holds only `DirectBaggageAttributesSpanProcessor` and no
exporter. An instrumented service would be indistinguishable from an uninstrumented
one — which is how instrumentation reaches production broken. Hence the console stands
in while there is no collector, and stands down once there is one.

**`send_to_logfire` defaults to `True`.** Leaving it unset would ship spans to a hosted
third party the first time a token happened to be present. It is set to `False`
explicitly, and that is a guard rail rather than a preference.

## Shape of the API

`sentry_options()` and `logfire_options()` are pure and return the mapping *before* it
is applied. That is what lets a test assert the exact set rather than assert that
something was configured, and it is the shape `edutap.data_provider` already uses.

`install_observability()` applies them. Both backends are opt-in by configuration
rather than by code: no DSN means no error tracker, `telemetry_enabled=False` means no
tracing. Structured logging is configured either way — a service without a collector
still has to be readable.

Nothing in the settings is **required**, because observability is installed *before* a
service resolves the settings it needs to run, so that a process refusing to start is
still reported. A value that is present but not a legal one does still fail: ignoring
it would decide in silence what leaves the process.

## Open points

* **The LMU deployment has to set `EDUTAP_PERSON_UID_MODE=plain`** if it wants the raw
  identifier. Until it does, the careful default applies.
* **No collector exists yet.** The endpoint is to be set centrally once it does; until
  then every service prints to its console.
* **Instrumentation of specific libraries** — `aiokafka`, `sqlalchemy` — is not here.
  It belongs to the service that uses them, and the first such service is
  `lmu_edutap_worker`.
* **Sentry versus Bugsink** is settled as "the same SDK against a self-hosted
  endpoint"; a second target would need a second field, not a second value.
