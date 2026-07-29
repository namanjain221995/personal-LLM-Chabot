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
            # Do not log the response body wholesale — keep it terse and safe.
            # Two Salesforce errors are worth naming, because the generic
            # status alone sends people looking in the wrong place entirely.
            hint = ""
            try:
                error = str(resp.json().get("error_description", ""))
            except Exception:
                error = ""
            if "not supported on this domain" in error:
                hint = (
                    " — the client-credentials grant is refused on "
                    "login.salesforce.com; SF_LOGIN_URL must be your org's My "
                    "Domain URL (https://<org>.my.salesforce.com)"
                )
            elif "no client credentials user" in error:
                hint = (
                    " — the connected app has no 'Run As' user set for the "
                    "client-credentials flow"
                )
            raise RuntimeError(
                f"Salesforce token request failed with HTTP {resp.status_code}{hint}"
            )
        body = resp.json()
        log.info(
            "obtained salesforce access token",
            extra={"event": "sf_token_obtained"},
        )
        return body["access_token"], body["instance_url"].rstrip("/")
