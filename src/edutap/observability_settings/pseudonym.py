"""Standing in for a person, or refusing to.

An error tracker is a machine that copies the state around a failure into a second
system. A ``person_uid`` at a university is not an opaque handle -- at the LMU it is
the LMU identifier with ``@lmu.de`` appended, and the circle able to turn that back
into a human being is far wider than the circle with directory administration rights.
Pseudonymised it is still personal data; the question a deployment answers here is not
*whether* but *who may see it*.
"""

import hashlib
import hmac

from pydantic import SecretStr

from .settings import ObservabilitySettings


def pseudonym(person_uid: str, salt: SecretStr | None) -> str | None:
    """Return a keyed, truncated stand-in for a person, or nothing without a key.

    Keyed rather than a plain digest: a ``person_uid`` comes from a directory, so the
    value space is small and enumerable, and an unsalted hash would be reversible by
    anyone able to read the error tracker -- simply by hashing the directory.

    Truncated to 12 hex characters, 48 bits: wide enough that two people in one
    installation colliding is not a practical concern, short enough that the result
    reads as a label rather than as an identifier worth storing.

    An *empty* salt counts as no salt. ``compose.yml`` writes ``${VAR:-}``, which sets
    a variable to the empty string rather than leaving it unset, and an HMAC under an
    empty key is a well-defined, entirely unkeyed digest -- precisely the
    hash-the-directory construction the key exists to prevent, and indistinguishable
    from a real pseudonym by eye.
    """
    if salt is None or not salt.get_secret_value():
        return None
    digest = hmac.new(
        salt.get_secret_value().encode("utf-8"),
        person_uid.encode("utf-8"),
        hashlib.sha256,
    )
    return digest.hexdigest()[:12]


def person_label(person_uid: str, settings: ObservabilitySettings) -> str | None:
    """Return what may stand for this person in what leaves the process.

    ``None`` means: attach nothing. Callers must treat that as an answer rather than
    as a reason to fall back to the raw value -- a deployment that asked for
    pseudonyms and forgot the key has to lose the datum, not publish it.
    """
    if settings.person_uid_mode == "omit":
        return None
    if settings.person_uid_mode == "plain":
        return person_uid
    return pseudonym(person_uid, settings.pseudonym_salt)
