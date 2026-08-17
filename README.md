# edutap.observability_settings

Error reporting, tracing and structured logging, wired the same way in every eduTAP
service — and one decision written down: what a service may say about a person.

## Why this exists

**Because the options are the point, not the code.** Three lines would start Sentry.
Which options those three lines carry decides whether a bearer token or a person's
identifier leaves the process, and each of the options below was chosen against a
measurement rather than against a backend's recommendation. Separated from the
`sentry_sdk.init()` call that applies them, those measurements are worth nothing —
which is why this is a package and not a paragraph in a README.

**Because a person's identifier is not an opaque handle.** At a university a
`person_uid` resolves to a human being for far more people than hold directory
administration rights; at the LMU it is the LMU identifier with `@lmu.de` appended.
Pseudonymised it remains personal data. What a deployment decides here is not
*whether* it is personal data but *who may see it*.

## Usage

```python
from edutap.observability_settings import install_observability, person_label

install_observability(service_name="lmu_edutap_worker")   # first thing in main()

log.warning("no view for person", person=person_label(uid, settings))
```

Call it before the service resolves the settings it needs to run, so that a process
refusing to start is reported rather than silently absent. Nothing here can fail for
want of a value.

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

## Configuration

One prefix for the whole estate, `EDUTAP_`. These fields are defined by an eduTAP
package, and another university deploying them should not have to learn an LMU name.

| Variable | Default | Meaning |
|---|---|---|
| `EDUTAP_ENVIRONMENT` | `production` | Labels every event and every span. Unset must not masquerade as development. |
| `EDUTAP_TELEMETRY_ENABLED` | `true` | The deliberate off switch for tracing, metrics and log export. |
| `EDUTAP_LOG_LEVEL` | `INFO` | A closed set; a misspelled level fails at startup. |
| `EDUTAP_SENTRY_DSN` | unset | Unset means no error tracker. One project per service. |
| `EDUTAP_PSEUDONYM_SALT` | unset | The HMAC key behind the person pseudonym. Without it there is no pseudonym at all. |
| `EDUTAP_PERSON_UID_MODE` | `pseudonym` | `pseudonym` · `plain` · `omit` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | **Not** an `EDUTAP_` field — see below. |

### Why the endpoint is not one of ours

A Sentry DSN names a **project**, so it belongs to the service and every service gets
its own. An OTLP endpoint names a **receiver**, normally one per host or cluster, and
which service sent a span rides in the resource attributes rather than in the address.
Every OpenTelemetry SDK already reads `OTEL_EXPORTER_OTLP_ENDPOINT` by itself; a
field under `EDUTAP_` would be a second name for the same value.

```{note}
A shared prefix means shared *names*, not shared *values*. `EDUTAP_SENTRY_DSN` differs
per service by being set in that service's own compose `environment:` block. Only
genuinely stack-wide values belong in the shared `.env`.
```

### The three modes for a person

| Mode | What travels | When it is right |
|---|---|---|
| `pseudonym` | a keyed, 12-character label | The default. Correlation survives — forty errors about one person still read as one person — identification does not. |
| `plain` | the `person_uid` itself | Where the error tracker is read by exactly the people who may read the directory anyway. A configured decision, not an accident. |
| `omit` | nothing | Where the tracker is read more widely than the directory. |

`pseudonym` without `EDUTAP_PSEUDONYM_SALT` yields **nothing**, never the raw value: a
deployment that asked for pseudonyms and forgot the key has to lose the datum rather
than publish it. An empty salt counts as no salt — compose writes `${VAR:-}`, which
sets a variable to the empty string, and an HMAC under an empty key is a plain digest
of a small, enumerable value space and reversible by anyone who can hash the directory.

## What the three backends do

Sentry takes errors. An OTLP collector takes traces and metrics. structlog produces
the records that reach both, bridged by `logfire.StructlogProcessor`, so a log line
and the span it happened inside share a trace id without the caller doing anything.

Nothing travels two paths: Sentry's own tracing stays off (`traces_sample_rate=0`),
because the spans already go to the collector and Bugsink — the tracker this estate
runs — states that it does not support traces.

```{important}
`send_to_logfire=False` is not a detail. The library defaults it to `True`, so leaving
it unset would ship spans to a hosted third party the first time a token happened to
be present.
```

While no collector exists, the console stands in. Measured against logfire 4.40: with
`send_to_logfire=False` and no `OTEL_EXPORTER_OTLP_ENDPOINT`, no exporter is installed
at all, so an instrumented service would be indistinguishable from an uninstrumented
one — which is how instrumentation reaches production broken. Once the endpoint is
set, the console stands down.

## Development

```shell
make venv
make lint
make test-local
```

`tox` runs the suite across every supported Python version.

## Design records

Under [`docs/superpowers/specs/`](docs/superpowers/specs/).
