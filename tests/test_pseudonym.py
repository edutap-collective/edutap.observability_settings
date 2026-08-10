from pydantic import SecretStr

from edutap.observability_settings import ObservabilitySettings, person_label, pseudonym

SALT = SecretStr("a-key-nobody-outside-the-deployment-has")
OTHER_SALT = SecretStr("a-different-key")
UID = "abc123@lmu.de"


def test_the_same_person_always_gets_the_same_label():
    # This is the whole point of pseudonymising rather than dropping the value: forty
    # errors about one person still read as forty errors about one person.
    assert pseudonym(UID, SALT) == pseudonym(UID, SALT)
    assert pseudonym(UID, SALT) != pseudonym("other@lmu.de", SALT)


def test_two_deployments_do_not_share_labels():
    assert pseudonym(UID, SALT) != pseudonym(UID, OTHER_SALT)


def test_it_reads_as_a_label_rather_than_an_identifier():
    label = pseudonym(UID, SALT)
    assert label is not None
    assert len(label) == 12
    assert all(character in "0123456789abcdef" for character in label)


def test_no_salt_means_no_label_rather_than_a_reversible_hash():
    # An unsalted digest of a directory identifier is reversible by anyone who can
    # hash the directory, and the value space of a person_uid is small and
    # enumerable. An empty salt is no salt: compose writes ${VAR:-}, which sets a
    # variable to the empty string rather than leaving it unset.
    assert pseudonym(UID, None) is None
    assert pseudonym(UID, SecretStr("")) is None


def test_pseudonym_mode_without_a_salt_yields_nothing_not_the_raw_value():
    # The failure that matters: a deployment asks for pseudonyms, forgets the key,
    # and gets plain text without being told. It must lose the datum instead.
    settings = ObservabilitySettings(person_uid_mode="pseudonym", pseudonym_salt=None)
    assert person_label(UID, settings) is None


def test_plain_mode_returns_the_value_untouched():
    # Legitimate where the error tracker is read by the same people who may read the
    # directory anyway -- a deliberate, configured decision.
    settings = ObservabilitySettings(person_uid_mode="plain")
    assert person_label(UID, settings) == UID


def test_omit_mode_drops_the_value_even_with_a_salt():
    settings = ObservabilitySettings(person_uid_mode="omit", pseudonym_salt=SALT)
    assert person_label(UID, settings) is None


def test_pseudonym_mode_with_a_salt_labels_the_person():
    settings = ObservabilitySettings(person_uid_mode="pseudonym", pseudonym_salt=SALT)
    label = person_label(UID, settings)
    assert label == pseudonym(UID, SALT)
    assert UID not in str(label)
