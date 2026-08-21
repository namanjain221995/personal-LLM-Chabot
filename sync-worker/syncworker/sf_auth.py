"""Salesforce OAuth2 JWT Bearer flow (spec §7).

Assertion: RS256, iss=client_id, sub=username, aud=login_url, exp=now+3min,
POSTed to {SF_LOGIN_URL}/services/oauth2/token. Tokens are cached and
refreshed proactively after a TTL and reactively on 401 (via invalidate()).
"""

from __future__ import annotations

import logging
import time

import httpx
import jwt

from .secrets import SalesforceCredentials

log = logging.getLogger("syncworker.sf_auth")

JWT_VALIDITY_SECONDS = 180  # 3 minutes, per spec
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"


class SalesforceAuthError(RuntimeError):
    """The org refused our credentials.

    Distinct from a generic RuntimeError so run_cycle can abort the whole
    cycle instead of re-attempting the same doomed token request once per
    object. Subclasses RuntimeError so existing callers keep catching it.
    """

    def __init__(self, message: str, error: str = "") -> None:
        super().__init__(message)
        #: Salesforce's machine-readable "error" field, e.g.
        #: "oauth_flow_disabled". Empty when the body was not JSON.
        self.error = error


def build_jwt_assertion(
    client_id: str,
    username: str,
    login_url: str,
    private_key_pem: bytes,
    now: float | None = None,
) -> str:
    """Build the signed RS256 JWT assertion for the token request."""
    issued_at = int(time.time() if now is None else now)
    claims = {
        "iss": client_id,
        "sub": username,
        "aud": login_url,
        "exp": issued_at + JWT_VALIDITY_SECONDS,
    }
    return jwt.encode(claims, private_key_pem, algorithm="RS256")


class TokenManager:
    """Caches the Salesforce access token; refreshes it when stale."""

    # Salesforce does not return expires_in for the JWT bearer grant, so we
    # refresh proactively after a conservative TTL and reactively on 401.
    TOKEN_TTL_SECONDS = 25 * 60

    def __init__(
        self, creds: SalesforceCredentials, http: httpx.Client | None = None
    ) -> None:
        self._creds = creds
        self._http = http or httpx.Client(timeout=30.0)
        self._access_token: str | None = None
        self._instance_url: str | None = None
        self._obtained_at: float = 0.0

    def get_token(self) -> tuple[str, str]:
        """Return (access_token, instance_url), refreshing if needed."""
        stale = (
            self._access_token is None
            or time.monotonic() - self._obtained_at > self.TOKEN_TTL_SECONDS
        )
        if stale:
            self._access_token, self._instance_url = self._request_token()
            self._obtained_at = time.monotonic()
        assert self._access_token is not None and self._instance_url is not None
        return self._access_token, self._instance_url

    def invalidate(self) -> None:
        """Drop the cached token (call after a 401) so the next call refreshes."""
        self._access_token = None
        self._instance_url = None

    def _request_token(self) -> tuple[str, str]:
        url = f"{self._creds.login_url}/services/oauth2/token"

        if self._creds.client_secret:
            # OAuth 2.0 client credentials: the connected app runs as its
            # configured "run as" user, so no per-user key or password exists.
            # NOTE: this grant is refused on login.salesforce.com ("request not
            # supported on this domain") — it must go to the org's My Domain.
            resp = self._http.post(
                url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._creds.client_id,
                    "client_secret": self._creds.client_secret,
                },
            )
        else:
            assertion = build_jwt_assertion(
                self._creds.client_id,
                self._creds.username,
                self._creds.login_url,
                self._creds.private_key_pem,
            )
            resp = self._http.post(
                url, data={"grant_type": GRANT_TYPE, "assertion": assertion}
            )
        if resp.status_code != 200:
            raise self._auth_error(resp)
        body = resp.json()
        log.info(
            "obtained salesforce access token",
            extra={"event": "sf_token_obtained"},
        )
        return body["access_token"], body["instance_url"].rstrip("/")

    def _auth_error(self, resp: httpx.Response) -> SalesforceAuthError:
        """Turn a non-200 token response into an actionable error.

        Hiding these was a false economy: an operator staring at a bare
        "HTTP 400" has no way to tell a disabled flow from a wrong secret from
        a missing Run As user, and every one of those is fixed in a different
        place. The OAuth error fields are diagnostics rather than secrets, but
        this message is bound for the logs, so the credential is scrubbed from
        them first — widening what we report must not widen what we leak.
        """
        try:
            body = resp.json()
            code = str(body.get("error", ""))
            description = str(body.get("error_description", ""))
        except Exception:
            code = description = ""

        # Named causes, because the raw description alone still sends people
        # looking in the wrong place. Matched on the stable "error" code where
        # Salesforce provides one, falling back to the prose.
        hints = {
            "oauth_flow_disabled": (
                "the connected app exists but this OAuth flow is turned off "
                "for it. In Setup → App Manager → your app → Manage → Edit "
                "Policies, enable the client-credentials flow and set a Run As "
                "user; on an External Client App the same switch lives under "
                "OAuth Settings → Flow Enablement. Nothing on this host can "
                "fix it — it is an org-side setting."
            ),
            "invalid_client_id": (
                "SF_CLIENT_ID does not match any connected app in this org "
                "(check you are pointed at the right org/sandbox)"
            ),
            "invalid_client": "SF_CLIENT_SECRET is wrong for this client_id",
        }
        hint = hints.get(code, "")
        if not hint:
            if "not supported on this domain" in description:
                hint = (
                    "the client-credentials grant is refused on "
                    "login.salesforce.com; SF_LOGIN_URL must be your org's My "
                    "Domain URL (https://<org>.my.salesforce.com)"
                )
            elif "no client credentials user" in description:
                hint = (
                    "the connected app has no 'Run As' user set for the "
                    "client-credentials flow"
                )

        description = self._redact(description)
        detail = " ".join(
            part for part in (code, f"({description})" if description else "") if part
        )
        message = f"Salesforce token request failed with HTTP {resp.status_code}"
        if detail:
            message += f": {detail}"
        if hint:
            message += f" — {hint}"
        return SalesforceAuthError(message, error=code)

    def _redact(self, text: str) -> str:
        """Scrub credential material out of text that is headed for a log."""
        for secret in (self._creds.client_secret,):
            if secret and secret in text:
                text = text.replace(secret, "<redacted>")
        return text
