# CLAUDE.md — edutap.observability_settings

Repository-specific rules. They take precedence over the global defaults.

## Language

**English only.** This repository belongs to eduTAP proper, not to any single
institution: README, changelog, documentation, docstrings, code comments, commit
messages, pull request titles and bodies, and replies to review comments.

The language follows the repository, not the conversation. A discussion held in
German still produces English artefacts here.

## What this package is

Error reporting, tracing and structured logging, wired the same way in every eduTAP
service, plus the decision about what a service may say about a person. It is
installed before a service resolves its own settings.

## Guard rails

**Every option here is a decision about what leaves the process.** The Sentry options
are not defaults someone liked; each was chosen against a measurement and each
contradicts the backend's own recommendation. Changing one means repeating the
measurement and recording it, not reasoning about it.

**`pseudonym` without a salt yields nothing, never the raw value.** A deployment that
asked for pseudonyms and forgot the key has to lose the datum. Any code path that
falls back to the plain `person_uid` defeats the whole package.

**`send_to_logfire` stays `False`.** The library defaults it to `True`. This estate
exports to its own collector, and an unset value ships spans to a hosted third party
the first time a token happens to be present.

**Never import from an eduTAP service.** This is installed before a service resolves
its settings; it can know nothing about them. Depending on `edutap.data_models` is
the one exception, and it is a library.

**Options are returned before they are applied.** `sentry_options` and
`logfire_options` are pure so a test can assert the exact set rather than assert that
something was configured. Do not inline them into the `init` call.

**No `uv.lock`.** This is a library; pinning here would push a resolution onto every
consumer.

## Working practice

Branch first, never commit on `main`. Push only when asked. `make lint` and
`make test-local` green before opening a pull request.

Design records live under `docs/superpowers/`. They are records of a decision at a
point in time — do not rewrite them to match a later state; write a new one.

## Sources and confidentiality


**No vendor internals — from any vendor, not just the ones currently in play.**
Neither in files nor in commit messages.

The standard is academic: a statement counts as reliable only where it can be
evidenced from public information, with a link. Everything else was obtained either
by our own testing or through insider knowledge, and the three are not
interchangeable:

* **Documented** — public source, linked. May be written as fact.
* **Verified, not citable** — obtained by a person from an access-protected area and
  checked there; the reference is recorded internally but must not be published; and
  the statement has been reduced to what is not confidential. May be written as fact,
  carrying this label. It is the rule journalism uses for source protection: the claim
  stands, we know where it comes from, the reader does not get the source.

  The four conditions hold together. A statement for which nobody can name the
  internal reference does not fall here — that is insider knowledge.
* **Measured** — established by our own tests. May be written down, but always marked
  as such, because it describes what a platform did on the day we looked, not what it
  guarantees. It can change with the next release, without notice and without an
  entry in any changelog.
* **Insider knowledge** — is not written down at all.

What a platform's behaviour *means for us* stays documentable even where the
mechanism does not: "the platform enforces a deadline, it is self-healing, it is
outside our control" carries the design consequence without disclosing anything.

Contract and regulatory material is wanted and citable: eduPersonAssurance, GÉANT and
eduGAIN terms, published wallet programme obligations.
