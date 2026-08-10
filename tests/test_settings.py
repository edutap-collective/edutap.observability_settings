import pytest
from pydantic import SecretStr, ValidationError

from edutap.observability_settings import ObservabilitySettings


def test_nothing_is_required():
    # Observability is installed before a service resolves the settings it needs to
    # run, so that a process refusing to start is still reported. That ordering only
    # works if reading these can never fail for want of a value.
    settings = ObservabilitySettings()
    assert settings.sentry_dsn is None
    assert settings.pseudonym_salt is None


def test_it_inherits_what_every_service_knows_about_itself():
    settings = ObservabilitySettings()
    assert settings.environment == "production"
    assert settings.telemetry_enabled is True
    assert settings.log_level == "INFO"


def test_person_uid_is_pseudonymised_unless_told_otherwise():
    # The safe default travels with the package. A deployment whose error tracker is
    # read by the same people who may read the directory can say so; one that has not
    # thought about it gets the careful answer.
    assert ObservabilitySettings().person_uid_mode == "pseudonym"


def test_a_present_but_illegal_value_still_fails():
    # "Nothing is required" is about missing values, not malformed ones. A misspelled
    # mode must not be ignored: the whole point of the field is to decide what leaves
    # the process, and a typo falling back to a default would decide it silently.
    with pytest.raises(ValidationError):
        ObservabilitySettings(person_uid_mode="pseudonymous")


def test_it_reads_the_shared_edutap_prefix(monkeypatch):
    # One prefix for the whole estate: the settings are defined by an eduTAP package,
    # and another university deploying them should not have to learn an LMU name.
    monkeypatch.setenv("EDUTAP_SENTRY_DSN", "https://key@example.invalid/1")
    monkeypatch.setenv("EDUTAP_PERSON_UID_MODE", "plain")
    monkeypatch.setenv("EDUTAP_ENVIRONMENT", "staging")
    settings = ObservabilitySettings()
    assert settings.sentry_dsn is not None
    assert settings.sentry_dsn.get_secret_value() == "https://key@example.invalid/1"
    assert settings.person_uid_mode == "plain"
    assert settings.environment == "staging"


def test_credentials_do_not_appear_in_a_repr():
    # BaseSettings prints every plain field verbatim, and a DSN and an HMAC key are
    # both credentials. A settings object lands in a log line sooner or later.
    settings = ObservabilitySettings(
        sentry_dsn=SecretStr("https://key@example.invalid/1"),
        pseudonym_salt=SecretStr("s3cret"),
    )
    printed = repr(settings)
    assert "key@example.invalid" not in printed
    assert "s3cret" not in printed
