import time

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from syncworker.sf_auth import JWT_VALIDITY_SECONDS, build_jwt_assertion

CLIENT_ID = "3MVG9test.client.id"
USERNAME = "integration@techsarasolutions.com"
LOGIN_URL = "https://login.salesforce.com"


def _throwaway_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def test_assertion_decodes_with_expected_claims():
    private_pem, public_pem = _throwaway_keypair()
    now = int(time.time())

    token = build_jwt_assertion(CLIENT_ID, USERNAME, LOGIN_URL, private_pem, now=now)

    header = jwt.get_unverified_header(token)
    assert header["alg"] == "RS256"

    claims = jwt.decode(
        token, public_pem, algorithms=["RS256"], audience=LOGIN_URL
    )
    assert claims["iss"] == CLIENT_ID
    assert claims["sub"] == USERNAME
    assert claims["aud"] == LOGIN_URL
    assert claims["exp"] == now + JWT_VALIDITY_SECONDS  # now + 3 minutes


def test_assertion_rejects_wrong_key():
    private_pem, _ = _throwaway_keypair()
    _, other_public_pem = _throwaway_keypair()

    token = build_jwt_assertion(CLIENT_ID, USERNAME, LOGIN_URL, private_pem)

    try:
        jwt.decode(token, other_public_pem, algorithms=["RS256"], audience=LOGIN_URL)
    except jwt.InvalidSignatureError:
        pass
    else:
        raise AssertionError("signature verification should have failed")


def test_assertion_expiry_enforced():
    private_pem, public_pem = _throwaway_keypair()
    stale_now = int(time.time()) - JWT_VALIDITY_SECONDS - 120

    token = build_jwt_assertion(
        CLIENT_ID, USERNAME, LOGIN_URL, private_pem, now=stale_now
    )

    try:
        jwt.decode(token, public_pem, algorithms=["RS256"], audience=LOGIN_URL)
    except jwt.ExpiredSignatureError:
        pass
    else:
        raise AssertionError("expired assertion should have been rejected")


# ---------------------------------------------------------------------------
# The client-credentials grant
# ---------------------------------------------------------------------------


def test_a_secret_uses_client_credentials_and_signs_nothing(monkeypatch):
    """No key is configured at all, so any attempt to build a JWT would crash.
    Reaching the token endpoint at all proves the other branch was taken."""
    import httpx

    from syncworker.sf_auth import TokenManager
    from syncworker.secrets import SalesforceCredentials

    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={
            "access_token": "tok", "instance_url": "https://x.my.salesforce.com/"})

    creds = SalesforceCredentials(
        client_id="cid", username="u", login_url="https://x.my.salesforce.com",
        client_secret="shhh",
    )
    tm = TokenManager(creds, http=httpx.Client(transport=httpx.MockTransport(handler)))
    token, instance = tm.get_token()

    assert token == "tok"
    assert instance == "https://x.my.salesforce.com"  # trailing slash stripped
    assert sent["grant_type"] == "client_credentials"
    assert sent["client_id"] == "cid" and sent["client_secret"] == "shhh"
    assert "assertion" not in sent


def test_the_wrong_domain_error_says_which_url_to_use():
    """Salesforce's own message ("request not supported on this domain") does
    not mention My Domain, which is the whole fix."""
    import httpx
    import pytest

    from syncworker.sf_auth import TokenManager
    from syncworker.secrets import SalesforceCredentials

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": "invalid_grant",
            "error_description": "request not supported on this domain"})

    creds = SalesforceCredentials(
        client_id="cid", username="u",
        login_url="https://login.salesforce.com", client_secret="shhh",
    )
    tm = TokenManager(creds, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError, match="My Domain"):
        tm.get_token()


def test_a_missing_run_as_user_is_named():
    import httpx
    import pytest

    from syncworker.sf_auth import TokenManager
    from syncworker.secrets import SalesforceCredentials

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": "invalid_grant",
            "error_description": "no client credentials user enabled"})

    creds = SalesforceCredentials(
        client_id="cid", username="u",
        login_url="https://x.my.salesforce.com", client_secret="shhh",
    )
    tm = TokenManager(creds, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError, match="Run As"):
        tm.get_token()


def test_a_disabled_oauth_flow_is_named_and_not_blamed_on_the_secret():
    """The real 400 from this org. Salesforce's own text ("The external client
    app or the OAuth plugin is disabled") does not say WHERE to turn it on, and
    the same error is returned for a wrong secret — so an operator reading a
    bare HTTP 400 rotates credentials that were never the problem."""
    import httpx
    import pytest

    from syncworker.sf_auth import SalesforceAuthError, TokenManager
    from syncworker.secrets import SalesforceCredentials

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": "oauth_flow_disabled",
            "error_description":
                "The external client app or the OAuth plugin is disabled."})

    creds = SalesforceCredentials(
        client_id="cid", username="u",
        login_url="https://x.my.salesforce.com", client_secret="shhh",
    )
    tm = TokenManager(creds, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(SalesforceAuthError) as excinfo:
        tm.get_token()

    message = str(excinfo.value)
    assert excinfo.value.error == "oauth_flow_disabled"
    assert "oauth_flow_disabled" in message      # the machine-readable code
    assert "Edit Policies" in message            # where to actually fix it
    assert "org-side" in message                 # not fixable on this host


def test_the_token_error_carries_salesforce_own_words():
    """A code we have no hint for must still reach the operator intact, rather
    than being flattened to "HTTP 400"."""
    import httpx
    import pytest

    from syncworker.sf_auth import TokenManager
    from syncworker.secrets import SalesforceCredentials

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": "something_new", "error_description": "a novel failure"})

    creds = SalesforceCredentials(
        client_id="cid", username="u",
        login_url="https://x.my.salesforce.com", client_secret="shhh",
    )
    tm = TokenManager(creds, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError, match="something_new.*a novel failure"):
        tm.get_token()


def test_a_non_json_token_error_still_raises_cleanly():
    """An HTML error page from a proxy must not turn into a JSONDecodeError."""
    import httpx
    import pytest

    from syncworker.sf_auth import SalesforceAuthError, TokenManager
    from syncworker.secrets import SalesforceCredentials

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    creds = SalesforceCredentials(
        client_id="cid", username="u",
        login_url="https://x.my.salesforce.com", client_secret="shhh",
    )
    tm = TokenManager(creds, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(SalesforceAuthError, match="HTTP 502") as excinfo:
        tm.get_token()
    assert excinfo.value.error == ""


def test_the_secret_is_never_echoed_in_the_error():
    """Diagnostics are widened above; the credential must not widen with them."""
    import httpx
    import pytest

    from syncworker.sf_auth import SalesforceAuthError, TokenManager
    from syncworker.secrets import SalesforceCredentials

    def handler(request: httpx.Request) -> httpx.Response:
        # A hostile/echoing endpoint reflecting the secret back at us.
        return httpx.Response(400, json={
            "error": "invalid_client", "error_description": "secret topsecret123"})

    creds = SalesforceCredentials(
        client_id="cid", username="u",
        login_url="https://x.my.salesforce.com", client_secret="topsecret123",
    )
    tm = TokenManager(creds, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(SalesforceAuthError) as excinfo:
        tm.get_token()
    assert "topsecret123" not in str(excinfo.value)
