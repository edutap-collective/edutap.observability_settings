# Safe FastAPI instrumentation

**Date:** 2026-08-17
**Status:** accepted, implemented 2026-08-17

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
| `pseudonym` | the route template | reduced to a count |
| `omit` | the route template | reduced to a count |

A count rather than nothing at all: how many arguments an endpoint received is
operationally readable and carries none of them.

**No allow-list, and no `person_label()` here.** An earlier draft of this record had
`pseudonym` keep a pseudonymised identifier out of the captured arguments. It cannot:
recognising which argument *is* the identifier means recognising it by parameter
name, and `edutap.data_provider` measured that names are not a boundary worth
trusting — a body parameter is called whatever a future endpoint author chooses.
`data_provider` could judge by *shape* because it knows its own models; this package
knows nothing about any service's, by design.

So in the two non-`plain` modes every captured argument is dropped. A service that
wants a pseudonymous label on its spans attaches it itself, in its own code, where it
knows which value is the identifier — `person_label()` remains the tool for that, it
is simply not callable from here.

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

**The Sentry path is not covered, and still exports the raw URL.** *(Added
2026-08-17 during execution, from a measurement taken after the tracing fix was
working.)*

`sentry-sdk` auto-enables `FastApiIntegration` and `StarletteIntegration` whenever
`fastapi` is importable — no configuration on our side asks for them — and
`send_default_pii=False` does not touch `request.url`. That option governs the
things Sentry classifies as PII: request bodies, cookies, user IP, headers. A URL is
not on that list, because for most applications a URL is not personal data. For a
service whose identifier sits in its path, it is.

Measured on 2026-08-17, `person_uid_mode="omit"`, `send_default_pii=False`, an
endpoint on `GET /persons/{person_uid}/photos` raising an unhandled exception, with
a `before_send` capturing the event instead of transmitting it:

```
integrations auto-enabled: FastApiIntegration, StarletteIntegration, HttpxIntegration, ...
event["transaction"] = /persons/{person_uid}/photos        <- the template, correct
event["request"]["url"] = http://testserver/persons/ab12cd34@lmu.de/photos
```

So `person_uid_mode` is, as of this branch, honoured on the **tracing** path and not
on the **error-reporting** path. That asymmetry is stated plainly here because it is
exactly what a reader of the README could otherwise get wrong: the README's FastAPI
paragraph describes spans, and the natural inference — that the promise is now kept
everywhere a request is observed — is false.

The fix is known and small: a `before_send` that rebuilds `event["request"]["url"]`
from `event["transaction"]`, which already holds the template, in the two non-`plain`
modes. It is not done here for the same reason `edutap.data_provider` is not
migrated: it is a different mechanism, in a different backend, with its own failure
modes — `transaction` can be absent or itself be a raw path when the exception
escapes before routing, and a `before_send` installed by this package interacts with
any `before_send` a service installs itself. That deserves its own measurement and
its own record rather than a rider on this one.

## Amendments

*Appended 2026-08-17, after execution. The record above is left as it was written;
below are the two decisions taken while implementing it that the record did not
anticipate — (a) and (b) — followed by (c), which is not a decision at all but a
limit of the mechanism that the record should have stated from the start.*

*The `Status` header at the top was moved from "accepted, not yet implemented" to
"accepted, implemented 2026-08-17", and it is the one line above this section that
was changed. "Append, do not rewrite" protects the **reasoning** — the argument, the
measurements, the trade-offs — from being tidied up afterwards by somebody who
already knows how it turned out. `Status` is lifecycle metadata about the record
rather than part of the decision it captures, and left stale it is not a preserved
snapshot but a false statement, misleading the next reader in the opposite direction
from the one the rule exists to prevent.*

### (a) Both semantic-convention generations are overwritten, not one

The record assumed one set of path attribute names. There are two.
`opentelemetry-instrumentation-asgi` emits the legacy `http.target`/`http.url` by
default, and the stable `url.path`/`url.full` instead once a deployment sets
`OTEL_SEMCONV_STABILITY_OPT_IN` — OpenTelemetry's own documented migration switch
off the legacy names.

The first implementation overwrote only the legacy pair. Measured with the variable
set: the legacy names came back as the `<unmatched>` placeholder — two decoy
attributes nothing had produced — while `url.path` and `url.full` carried the raw
identifier straight through. A deployment could therefore reopen the leak with an
environment variable this package neither sets nor reads.

All five names (`http.target`, `http.url`, `url.path`, `url.full`, `logfire.msg`) are
now written unconditionally. Writing a name the active instrumentation never used is
cosmetic span noise; leaving one unwritten is a disclosure controlled by somebody
else's environment.

**Why this is not tested by setting the variable.** OpenTelemetry reads it exactly
once per process and caches the result (`_OpenTelemetrySemanticConventionStability`,
guarded by an `_initialized` flag with no reset). Measured: setting it after any
earlier `instrument_fastapi()` call in the same process has no effect. A test that
set it would either do nothing or pass for a reason that stops holding when test
order changes. The guard is one step earlier instead — assert that all five names are
present and all carry the template — which is what makes the fix correct under either
convention rather than under whichever one the test process happens to be running.

### (b) The hook fails open, so the code fails closed inside it

The record did not consider what happens when the hook itself raises. OpenTelemetry
wraps `server_request_hook` in a `failsafe` that records the exception on the span and
lets the request continue. Measured with `route_template` made to raise: the request
returned 200 and the span still carried the raw path in `logfire.msg`, `http.target`
and `http.url`. The scrubbing simply does not happen, and nothing about the response
says so.

The first implementation's stated mitigation was that `route_template()` stays total.
**That claim was wrong.** A scope without `"path"` raises `KeyError` out of
starlette's matcher, and `matches()` belongs to whatever `BaseRoute` subclasses the
application mounted — a third-party router may raise anything at all. Totality
delegated to third-party implementations is exactly the kind of trust this design
rejects everywhere else; it is the same reasoning that rules out judging an argument
by its parameter name.

The mitigation is now a `try`/`except Exception` inside the hook that falls back to
`UNMATCHED`. The attributes are still overwritten, so what leaves the process on a
failure is a useless placeholder rather than an identifier — fail closed, not fail
open. A bare `except Exception` rather than an enumeration, because the point is the
failure nobody enumerated.

### (c) A note the record should have carried from the start: span events

Everything this design touches is a span *attribute*. The hook and the mapper are the
only two seams logfire offers, and neither sees a span **event**. An endpoint raising
`RuntimeError(f"no such person {person_uid}")` puts that identifier in
`exception.message` on an event, and nothing in this package touches it. That text is
the calling service's, so it is the calling service's responsibility — but a service
whose identifier sits in its path is precisely the service whose "not found" and "not
permitted" handlers will reach for it, so the first consumer needs to be told rather
than left to discover it.
