"""Credential resolution: direct env vars first, Secrets Manager JSON second."""
import base64

import pytest

from syncworker.secrets import credentials_from_env, fetch_sf_credentials

@pytest.fixture()
def no_default_key(monkeypatch):
    """Pin DEFAULT_KEY_PATH away from any real file.

    The deployed container HAS a key at that path, so without this these tests
    pass on a laptop and fail in the image — proving nothing either way.
    """
    monkeypatch.setattr("syncworker.secrets.DEFAULT_KEY_PATH", "/nonexistent/key.pem")


PEM = b"-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n"
B64 = base64.b64encode(PEM).decode()

FULL_ENV = {
    "SF_CLIENT_ID": "cid123",
    "SF_USERNAME": "integration@example.com",
    "SF_LOGIN_URL": "https://login.salesforce.com/",
    "SF_PRIVATE_KEY_B64": B64,
}


def test_env_first_path_builds_credentials():
    creds = credentials_from_env(FULL_ENV)
    assert creds is not None
    assert creds.client_id == "cid123"
    assert creds.username == "integration@example.com"
    assert creds.login_url == "https://login.salesforce.com"  # trailing / stripped
    assert creds.private_key_pem == PEM


def test_env_path_requires_all_four_keys(no_default_key):
    for missing in FULL_ENV:
        partial = {k: v for k, v in FULL_ENV.items() if k != missing}
        assert credentials_from_env(partial) is None


def test_env_path_rejects_bad_base64():
    bad = dict(FULL_ENV, SF_PRIVATE_KEY_B64="not-base64!!!")
    with pytest.raises(ValueError, match="SF_PRIVATE_KEY_B64"):
        credentials_from_env(bad)


def test_env_values_never_appear_in_repr():
    creds = credentials_from_env(FULL_ENV)
    assert "cid123" not in repr(creds)
    assert "redacted" in repr(creds)


def test_fetch_reads_the_environment(monkeypatch):
    for k, v in FULL_ENV.items():
        monkeypatch.setenv(k, v)
    assert fetch_sf_credentials().client_id == "cid123"


def test_fetch_without_credentials_names_the_fix(monkeypatch):
    """AWS Secrets Manager is gone, so the message must point at the env vars
    that now supply everything — not at a fallback that no longer exists."""
    for k in (*FULL_ENV, "SF_PRIVATE_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("syncworker.secrets.DEFAULT_KEY_PATH", "/no/such/key.pem")
    with pytest.raises(ValueError, match="SF_PRIVATE_KEY_FILE"):
        fetch_sf_credentials()


def test_nothing_imports_boto3_any_more():
    import pathlib as _p
    src = _p.Path(__file__).parent.parent / "syncworker" / "secrets.py"
    assert "boto3" not in src.read_text()


# ---------------------------------------------------------------------------
# SF_PRIVATE_KEY_FILE — the key as a path, so nothing secret lives in .env
# ---------------------------------------------------------------------------

IDENTITY = {
    "SF_CLIENT_ID": "cid123",
    "SF_USERNAME": "integration@example.com",
    "SF_LOGIN_URL": "https://login.salesforce.com/",
}

#: What Salesforce shows next to an uploaded certificate. It is a SHA-256
#: fingerprint — NOT a key, and NOT a path. Pasting it here is a common mix-up.
THUMBPRINT = "DC24D8F6BD8F232FAA52B4404E05403555E267D254F6833F5491F383C3ECF248"


def _key_file(tmp_path, content=PEM):
    path = tmp_path / "sf_jwt_key.pem"
    path.write_bytes(content)
    return str(path)


def test_a_key_file_path_is_read(tmp_path):
    creds = credentials_from_env(
        dict(IDENTITY, SF_PRIVATE_KEY_FILE=_key_file(tmp_path))
    )
    assert creds is not None
    assert creds.private_key_pem == PEM
    assert creds.client_id == "cid123"


def test_the_file_wins_when_both_forms_are_set(tmp_path):
    """A path is easier to rotate and keeps the key out of .env."""
    other = b"-----BEGIN PRIVATE KEY-----\nfromfile\n-----END PRIVATE KEY-----\n"
    creds = credentials_from_env(
        dict(IDENTITY, SF_PRIVATE_KEY_FILE=_key_file(tmp_path, other),
             SF_PRIVATE_KEY_B64=B64)
    )
    assert creds.private_key_pem == other


@pytest.mark.parametrize("value", [
    THUMBPRINT,
    THUMBPRINT.lower(),
    "DC:24:D8:F6:BD:8F:23:2F:AA:52:B4:40:4E:05:40:35:55:E2:67:D2",  # SHA-1, colons
])
def test_a_certificate_thumbprint_is_rejected_by_name(value):
    """The error has to SAY it is a fingerprint. 'file not found' would send
    someone hunting for a path that was never supposed to exist."""
    with pytest.raises(ValueError, match="THUMBPRINT"):
        credentials_from_env(dict(IDENTITY, SF_PRIVATE_KEY_FILE=value))


def test_a_missing_key_file_names_the_path(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        credentials_from_env(
            dict(IDENTITY, SF_PRIVATE_KEY_FILE=str(tmp_path / "nope.pem"))
        )


def test_a_certificate_instead_of_a_key_says_so(tmp_path):
    """Uploading the cert and keeping the key is the whole point of the flow;
    handing back the cert is the other half of the same mix-up."""
    cert = b"-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----\n"
    with pytest.raises(ValueError, match="CERTIFICATE, not a private key"):
        credentials_from_env(
            dict(IDENTITY, SF_PRIVATE_KEY_FILE=_key_file(tmp_path, cert))
        )


def test_base64_of_something_that_is_not_a_key_is_rejected():
    junk = base64.b64encode(b"just some bytes").decode()
    with pytest.raises(ValueError, match="not a PEM private key"):
        credentials_from_env(dict(IDENTITY, SF_PRIVATE_KEY_B64=junk))


def test_identity_without_any_key_returns_none(no_default_key):
    """Nothing to authenticate with — the caller turns this into a clear error."""
    assert credentials_from_env(dict(IDENTITY)) is None


def test_a_broken_key_raises_rather_than_being_ignored(tmp_path):
    """Treating a malformed key as "no key" would hide the real problem and
    make fixing it look like it had no effect."""
    with pytest.raises(ValueError):
        credentials_from_env(dict(IDENTITY, SF_PRIVATE_KEY_FILE="/no/such/key.pem"))


# ---------------------------------------------------------------------------
# SF_CLIENT_SECRET — the client-credentials grant (no key material at all)
# ---------------------------------------------------------------------------


def test_a_consumer_secret_is_enough_on_its_own():
    """The connected app authenticates as itself, so there is no key to sign
    with and none should be required."""
    creds = credentials_from_env(dict(IDENTITY, SF_CLIENT_SECRET="shhh"))
    assert creds is not None
    assert creds.client_secret == "shhh"
    assert creds.private_key_pem == b""


def test_the_secret_is_preferred_over_a_key(tmp_path):
    """Both configured means someone migrated; the simpler grant wins."""
    creds = credentials_from_env(
        dict(IDENTITY, SF_CLIENT_SECRET="shhh",
             SF_PRIVATE_KEY_FILE=_key_file(tmp_path))
    )
    assert creds.client_secret == "shhh"


def test_the_secret_is_never_shown_in_a_repr():
    creds = credentials_from_env(dict(IDENTITY, SF_CLIENT_SECRET="shhh"))
    assert "shhh" not in repr(creds) and "shhh" not in str(creds)


def test_a_blank_secret_falls_through_to_the_key(tmp_path):
    creds = credentials_from_env(
        dict(IDENTITY, SF_CLIENT_SECRET="   ",
             SF_PRIVATE_KEY_FILE=_key_file(tmp_path))
    )
    assert creds.client_secret == ""
    assert creds.private_key_pem == PEM
