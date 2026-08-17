# Safe FastAPI instrumentation

**Date:** 2026-08-17
**Status:** accepted, not yet implemented

A snapshot of a decision at its date. It is not rewritten as things change; a
different decision gets a new record.

## The problem

`logfire.instrument_fastapi()` exports the request path, and a service that carries
an identifier in its path therefore exports that identifier — on every span, whatever
`person_uid_mode` says.

Measured against the pinned versions on 2026-08-17, with a route
`GET /persons/{person_uid}/photos` and a real span exporter rather than a mock:

```
SPAN 'GET /persons/{person_uid}/photos'        <- the span NAME is the template, and is safe
   logfire.msg              = GET /persons/ab12cd34@lmu.de/photos
   http.target              = /persons/ab12cd34@lmu.de/photos
   http.url                 = http://testserver/persons/ab12cd34@lmu.de/photos
   fastapi.arguments.values = {"person_uid":"ab12cd34@lmu.de"}
```

Four attributes, three of which come from the OpenTelemetry HTTP conventions rather
than from FastAPI argument capture.

This matters because of what this package already promises. `person_uid_mode` offers
`omit` and `pseudonym`, and `person_label()` is careful enough to return `None`
rather than fall back to a raw value when a deployment asked for pseudonyms and
forgot the key. A deployment that sets `omit`, mounts no salt, and then instruments
FastAPI exports every identifier anyway. **The setting would be a promise the
package does not keep** — which is worse than not offering it.

`edutap.data_provider` met one quarter of this and solved it locally
(`src/edutap/data_provider/observability.py`): it scrubs `fastapi.arguments.values`
and the Pydantic `errors` mapping. It did not meet the other three quarters because
its `/lookup` takes the identifier in the request *body*, so its URLs are clean. The
first service whose identifier sits in the path is `edutap.image_service`, where
every route is `/persons/{person_uid}/...`.

## The decision

**The existing mode switch governs the URL as well.** No new setting, no second
concept:

| `person_uid_mode` | `http.target`, `http.url`, `logfire.msg` | `fastapi.arguments.values` |
| --- | --- | --- |
| `plain` | untouched | untouched |
| `pseudonym` | the route template | reduced, `person_uid` through `person_label()` |
| `omit` | the route template | reduced, identifier dropped |

Two things are worth stating about why it is shaped this way.

**The template rather than a redaction pattern.** In the two non-`plain` modes the
path attributes are replaced by the route template. The template is already on the
span and does not have to be reconstructed — measured in the same run as the leak
above:

```
http.route = /persons/{person_uid}/photos
```

This is safe *by construction*: no dynamic segment survives, whatever a future route
is called and whatever is in it. A redaction that
matched patterns would have to guess which segment is an identifier, and would be
wrong on the first route nobody anticipated. Nothing operational is lost — which
route was hit is still there, and the parameters worth keeping come back as explicit,
scrubbed attributes.

**`plain` really means untouched.** A deployment that chooses `plain` gets the raw
path. This is not an oversight to be tightened later: a package that redacts anyway
would be taking a decision that belongs to the deployment, and the operator who
asked to see identifiers has no way to get them back. The LMU deployment runs
`plain` — its telemetry stays on its own cluster, in its own Sentry and its own
collector — and for it this whole mechanism is a no-op.

The **default** stays non-`plain`, because this package belongs to eduTAP rather
than to one institution, and a default that exports identifiers is the wrong thing
for the next university to inherit silently.

### Argument and error attributes

Carried over from `edutap.data_provider`, whose reasoning holds here unchanged:

- **Default is drop, not pass-through.** Endpoint parameter *names* are not a
  boundary this package controls; a body parameter can be called anything a future
  endpoint author chooses. Values are judged by what they are, and unrecognised
  means dropped.
- **`input` and `msg` never survive a validation error.** Measured there: a "missing
  field" error's `input` is not the missing field's value but the *whole enclosing
  dict*, because the error is reported against the model. An identifier therefore
  sits in `errors[0]["input"]` on a plain 422, with no exception anywhere in the
  picture. `type` and `loc` survive — which field, what kind of problem, from
  Pydantic's own fixed vocabulary.

## The interface

```python
from edutap.observability_settings import install_observability, instrument_fastapi_safely

install_observability(service_name="edutap.image_service", service_version=__version__)
instrument_fastapi_safely(app)
```

A separate call rather than a parameter on `install_observability`: not every service
in the estate is a FastAPI service, `logfire[fastapi]` is an optional extra, and a
worker must not have to install it to configure its logging.

It reads `ObservabilitySettings` itself by default, and takes one explicitly for a
test.

## Testing

The load-bearing test is the one that found the leak: **real spans, no mocks.** Build
an app, instrument it, export through a capturing span processor, request a route
whose path carries an identifier, and assert on what actually left the process.

The assertion is deliberately shape-free — *the raw identifier appears in no
attribute of any exported span* — rather than an enumeration of the four known ones.
An enumerating test passes when a future logfire version adds a fifth attribute, which
is exactly the failure this record exists to prevent.

One pass per mode, and `plain` is asserted as loudly as the others: it must **keep**
the identifier. A mechanism that quietly redacted in every mode would satisfy a
one-sided test suite while breaking the deployment that asked to see identifiers.

## Consequences

- `logfire[fastapi]` becomes an optional extra of this package; the core install is
  unchanged for non-FastAPI services.
- `edutap.image_service` can adopt observability without its traces defeating
  `person_uid_mode`. That is a separate change in that repository, with its own
  event catalogue.

## Deliberately not done

**`edutap.data_provider` is not migrated onto this.** Its local scrubber solves the
quarter of the problem it has, it is deployed and running, and folding it in is a
third change to a service that is not otherwise being touched. It should happen —
the two implementations will drift — but as its own piece of work, not as a rider
here.

**Retention of telemetry is not addressed.** `edutap.image_service` has a worked-out
deletion path for photographs — an expiry, a legal hold, and the stated position that
a photo service which never forgets anything on its own is not one anybody else
should adopt. Traces and error reports have their own, separate retention, and an
identifier exported under `plain` outlives the row it came from. This is a deletion
question rather than an access question, it applies to a deployment that trusts
everyone who can read its collector, and it is the deployment's to answer. Recorded
here so that it is a known gap rather than an unnoticed one.
