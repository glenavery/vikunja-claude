"""The OAuth 2.1 boundary in front of the MCP server.

ChatGPT's connector dialog authenticates with OAuth, so this service is its own
authorization server as well as the resource server. It implements the subset
the MCP authorization specification requires and nothing beyond it: protected
resource metadata (RFC 9728), authorization server metadata (RFC 8414), an
authorization code flow with mandatory PKCE, refresh, and dynamic client
registration (RFC 7591).

It is deliberately not an identity provider. There are no users, no accounts, no
roles and no sign-up: there is one operator, who proves it with one passphrase,
and every token this server issues carries the same single scope, which means
exactly the two operations the MCP boundary exposes. Authorising cannot widen
that — there is nothing wider to grant.

The module handles the protocol and returns :class:`Response` objects; the HTTP
server does the framing. That keeps the whole flow drivable in tests without a
socket, and keeps decisions like "is this token still valid" out of the request
handler, where they would be one early ``return`` away from not happening.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from .config import McpConfig
from .oauth_store import OAuthStore, digest, new_secret

# One scope, meaning the two tools and nothing else. It is not a permission
# system: it exists because the protocol has a place for the name of what was
# granted, and that name should say what it is.
SCOPE = "vikunja:tickets"

# Failed passphrase attempts before the consent screen stops accepting any, and
# how long that lasts. The endpoint is on the public internet; a passphrase
# with no rate limit in front of it is a password to be guessed at leisure.
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 300

REVOCABLE_GRANT_SUMMARY = (
    "read one Vikunja task by id, and create one task on the "
    "{project} board"
)


@dataclass(frozen=True)
class Response:
    status: int
    body: str = ""
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, status: int, payload: dict[str, Any]) -> "Response":
        return cls(
            status,
            json.dumps(payload, indent=1),
            "application/json",
            {"Cache-Control": "no-store", "Pragma": "no-cache"},
        )


@dataclass(frozen=True)
class TokenCheck:
    """The outcome of presenting a bearer token at ``/mcp``."""

    record: dict[str, Any] | None
    error: str = ""
    description: str = ""

    @property
    def ok(self) -> bool:
        return self.record is not None


class AuthorizationError(Exception):
    """An OAuth error that is safe to report to the client."""

    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status

    def payload(self) -> dict[str, str]:
        return {"error": self.error, "error_description": self.description}


class RedirectableError(AuthorizationError):
    """An authorization error the client is entitled to receive by redirect.

    Split from :class:`AuthorizationError` because the distinction is a security
    rule, not a formatting choice: a bad ``client_id`` or ``redirect_uri`` must
    never be redirected anywhere, since the only place to send it is the
    attacker-supplied address that caused the failure.
    """


def _form(raw: str) -> dict[str, str]:
    return {
        key: values[-1]
        for key, values in urllib.parse.parse_qs(raw, keep_blank_values=True).items()
    }


def _pkce_matches(verifier: str, challenge: str) -> bool:
    computed = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    return secrets.compare_digest(computed, challenge)


class AuthorizationServer:
    def __init__(self, config: McpConfig, store: OAuthStore, now=time.time):
        self.config = config
        self.oauth = config.oauth
        self.store = store
        self._now = now
        self._failures: list[float] = []

    # -- metadata ----------------------------------------------------------

    def protected_resource_metadata(self) -> Response:
        """RFC 9728. How a client discovers who issues tokens for ``/mcp``."""
        return Response.json(
            200,
            {
                "resource": self.oauth.resource,
                "authorization_servers": [self.oauth.issuer],
                "scopes_supported": [SCOPE],
                "bearer_methods_supported": ["header"],
                "resource_name": f"Vikunja {self.config.project_title} board",
                "resource_documentation": f"{self.oauth.issuer}/health",
            },
        )

    def authorization_server_metadata(self) -> Response:
        """RFC 8414. Only the grants that exist here are advertised."""
        return Response.json(
            200,
            {
                "issuer": self.oauth.issuer,
                "authorization_endpoint": self.oauth.authorization_endpoint,
                "token_endpoint": self.oauth.token_endpoint,
                "registration_endpoint": self.oauth.registration_endpoint,
                "scopes_supported": [SCOPE],
                "response_types_supported": ["code"],
                "response_modes_supported": ["query"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": [
                    "none",
                    "client_secret_post",
                    "client_secret_basic",
                ],
                "authorization_response_iss_parameter_supported": True,
                "service_documentation": (
                    "https://github.com/glenavery/vikunja-claude#the-mcp-boundary-chatgpt"
                ),
            },
        )

    # -- resource server ---------------------------------------------------

    def authenticate(self, header: str | None) -> TokenCheck:
        """Validate the bearer token on an MCP request.

        There is no other way in. A request that fails here gets nothing — not
        a reduced surface, not an anonymous one, and never the pre-OAuth static
        token, which this server no longer knows about.
        """
        scheme, _, presented = (header or "").partition(" ")
        presented = presented.strip()
        if scheme.lower() != "bearer" or not presented:
            return TokenCheck(None, "", "An OAuth access token is required")

        record = self.store.get_token(presented)
        if record is None or record.get("type") != "access":
            # Expired, revoked, rotated away, never issued, or a refresh token
            # presented as an access token: all the same answer, deliberately.
            return TokenCheck(
                None, "invalid_token", "The access token is expired or invalid"
            )
        if record.get("resource") != self.oauth.resource:
            return TokenCheck(
                None, "invalid_token", "The access token was issued for another resource"
            )
        if record.get("scope") != SCOPE:
            return TokenCheck(
                None, "invalid_token", "The access token does not carry this scope"
            )
        return TokenCheck(record)

    def challenge(self, check: TokenCheck | None = None) -> str:
        """The ``WWW-Authenticate`` value that points a client at the flow."""
        parts = [
            'Bearer realm="vikunja-claude-mcp"',
            f'resource_metadata="{self.oauth.protected_resource_metadata_url}"',
            f'scope="{SCOPE}"',
        ]
        if check is not None and check.error:
            parts.insert(1, f'error="{check.error}"')
            parts.append(f'error_description="{check.description}"')
        return ", ".join(parts)

    # -- client registration -----------------------------------------------

    def register(self, body: str) -> Response:
        """RFC 7591, narrowed to the redirect URIs this deployment allows.

        Registration is open in the sense that anyone who can reach the endpoint
        can obtain a ``client_id``. That grants nothing: a client id is not a
        credential here, every authorization still has to be approved by the
        operator at the consent screen, and a client whose redirect URI is not
        on the allow list cannot be registered at all — so there is nowhere for
        a code to be sent that the operator has not already named.
        """
        try:
            payload = json.loads(body or "{}")
        except json.JSONDecodeError:
            return Response.json(
                400,
                {
                    "error": "invalid_client_metadata",
                    "error_description": "The registration body is not JSON",
                },
            )
        if not isinstance(payload, dict):
            return Response.json(
                400,
                {
                    "error": "invalid_client_metadata",
                    "error_description": "The registration body is not an object",
                },
            )

        redirect_uris = payload.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return Response.json(
                400,
                {
                    "error": "invalid_redirect_uri",
                    "error_description": "redirect_uris is required",
                },
            )
        unknown = [
            uri for uri in redirect_uris if uri not in self.oauth.redirect_uris
        ]
        if unknown:
            return Response.json(
                400,
                {
                    "error": "invalid_redirect_uri",
                    "error_description": (
                        f"{unknown[0]!r} is not a permitted redirect URI for this "
                        "server. Permitted: "
                        + ", ".join(self.oauth.redirect_uris)
                    ),
                },
            )

        auth_method = payload.get("token_endpoint_auth_method") or "none"
        if auth_method not in ("none", "client_secret_post", "client_secret_basic"):
            return Response.json(
                400,
                {
                    "error": "invalid_client_metadata",
                    "error_description": (
                        f"token_endpoint_auth_method {auth_method!r} is not supported"
                    ),
                },
            )

        client_id = f"cid_{new_secret()}"
        record: dict[str, Any] = {
            "client_id": client_id,
            "client_name": str(payload.get("client_name") or "unnamed client"),
            "redirect_uris": list(redirect_uris),
            "token_endpoint_auth_method": auth_method,
            "issued_at": int(self._now()),
        }
        secret = None
        if auth_method != "none":
            secret = new_secret()
            record["client_secret_hash"] = digest(secret)
        self.store.register_client(record)

        response: dict[str, Any] = {
            "client_id": client_id,
            "client_id_issued_at": record["issued_at"],
            "client_name": record["client_name"],
            "redirect_uris": record["redirect_uris"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": auth_method,
            "scope": SCOPE,
        }
        if secret is not None:
            response["client_secret"] = secret
            # No expiry: rotation here is re-registration, and a secret that
            # silently stopped working would look like an outage.
            response["client_secret_expires_at"] = 0
        return Response.json(201, response)

    def _client(self, client_id: str) -> dict[str, Any] | None:
        configured = self.oauth.static_client(client_id)
        return configured or self.store.get_client(client_id)

    # -- authorization endpoint --------------------------------------------

    def _validated_request(self, params: dict[str, str]) -> dict[str, Any]:
        """Everything the authorization request must get right, in order.

        Client and redirect URI first, because until those are known good there
        is nowhere to report anything to.
        """
        client_id = (params.get("client_id") or "").strip()
        client = self._client(client_id) if client_id else None
        if client is None:
            raise AuthorizationError(
                "invalid_client",
                "Unknown client_id. Register the client first, or configure it "
                "on the server.",
            )

        redirect_uri = (params.get("redirect_uri") or "").strip()
        allowed = client["redirect_uris"]
        if not redirect_uri and len(allowed) == 1:
            redirect_uri = allowed[0]
        if redirect_uri not in allowed or redirect_uri not in self.oauth.redirect_uris:
            raise AuthorizationError(
                "invalid_request",
                "redirect_uri does not exactly match a registered redirect URI.",
            )

        state = params.get("state") or ""
        if (params.get("response_type") or "") != "code":
            raise RedirectableError(
                "unsupported_response_type",
                "Only the authorization code flow is supported",
            )

        challenge = (params.get("code_challenge") or "").strip()
        method = (params.get("code_challenge_method") or "").strip()
        if not challenge:
            raise RedirectableError(
                "invalid_request", "PKCE is required: code_challenge is missing"
            )
        if method != "S256":
            raise RedirectableError(
                "invalid_request",
                "PKCE code_challenge_method must be S256",
            )

        requested_scope = (params.get("scope") or "").split()
        if any(scope != SCOPE for scope in requested_scope):
            raise RedirectableError(
                "invalid_scope",
                f"The only scope this server issues is {SCOPE}",
            )

        resource = (params.get("resource") or "").strip()
        if resource and resource.rstrip("/") not in (
            self.oauth.resource,
            self.oauth.issuer,
        ):
            raise RedirectableError(
                "invalid_target",
                f"This authorization server only issues tokens for {self.oauth.resource}",
            )

        return {
            "client": client,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "state": state,
            "resource": self.oauth.resource,
        }

    def _redirect(self, redirect_uri: str, params: dict[str, str]) -> Response:
        query = urllib.parse.urlencode(
            {**params, "iss": self.oauth.issuer}, quote_via=urllib.parse.quote
        )
        joiner = "&" if "?" in redirect_uri else "?"
        return Response(
            302,
            "",
            "text/plain",
            {"Location": f"{redirect_uri}{joiner}{query}", "Cache-Control": "no-store"},
        )

    def authorize(self, params: dict[str, str]) -> Response:
        """The consent screen. Shows what is being granted, and to whom."""
        try:
            request = self._validated_request(params)
        except RedirectableError as exc:
            redirect_uri = self._safe_redirect(params)
            if redirect_uri is None:
                return self._error_page(exc)
            return self._redirect(
                redirect_uri,
                {
                    "error": exc.error,
                    "error_description": exc.description,
                    **({"state": params["state"]} if params.get("state") else {}),
                },
            )
        except AuthorizationError as exc:
            return self._error_page(exc)
        return self._consent_page(request, params)

    def _safe_redirect(self, params: dict[str, str]) -> str | None:
        """The redirect URI, only if it was already proved to be a legitimate one."""
        client = self._client((params.get("client_id") or "").strip())
        if client is None:
            return None
        redirect_uri = (params.get("redirect_uri") or "").strip()
        if not redirect_uri and len(client["redirect_uris"]) == 1:
            redirect_uri = client["redirect_uris"][0]
        if (
            redirect_uri in client["redirect_uris"]
            and redirect_uri in self.oauth.redirect_uris
        ):
            return redirect_uri
        return None

    def approve(self, params: dict[str, str]) -> Response:
        """The consent form's submission: check the operator, then issue a code.

        The whole request is re-validated here rather than trusted from the
        hidden fields. The form is just a way to carry the parameters back; it
        is not evidence that they were ever checked.
        """
        try:
            request = self._validated_request(params)
        except RedirectableError as exc:
            redirect_uri = self._safe_redirect(params)
            if redirect_uri is None:
                return self._error_page(exc)
            return self._redirect(
                redirect_uri,
                {"error": exc.error, "error_description": exc.description},
            )
        except AuthorizationError as exc:
            return self._error_page(exc)

        locked = self._lockout_remaining()
        if locked:
            return self._consent_page(
                request,
                params,
                message=(
                    f"Too many failed attempts. Try again in {locked} seconds, "
                    "or restart the service to clear the lockout."
                ),
                status=429,
            )

        if not secrets.compare_digest(
            params.get("passphrase") or "", self.oauth.passphrase
        ):
            self._failures.append(self._now())
            return self._consent_page(
                request, params, message="That passphrase is not correct.", status=401
            )
        self._failures.clear()

        code = new_secret()
        self.store.put_code(
            code,
            {
                "client_id": request["client_id"],
                "redirect_uri": request["redirect_uri"],
                "code_challenge": request["code_challenge"],
                "resource": request["resource"],
                "scope": SCOPE,
                "grant_id": new_secret(),
                "redeemed": False,
                "expires_at": self._now() + self.oauth.code_ttl_seconds,
            },
        )
        redirect_params = {"code": code}
        if request["state"]:
            redirect_params["state"] = request["state"]
        return self._redirect(request["redirect_uri"], redirect_params)

    def _lockout_remaining(self) -> int:
        cutoff = self._now() - LOCKOUT_SECONDS
        self._failures = [moment for moment in self._failures if moment > cutoff]
        if len(self._failures) < MAX_FAILED_ATTEMPTS:
            return 0
        return int(self._failures[0] + LOCKOUT_SECONDS - self._now()) + 1

    # -- token endpoint ----------------------------------------------------

    def token(self, body: str, authorization_header: str | None = None) -> Response:
        params = _form(body or "")
        try:
            client = self._authenticated_client(params, authorization_header)
            grant_type = params.get("grant_type") or ""
            if grant_type == "authorization_code":
                payload = self._exchange_code(client, params)
            elif grant_type == "refresh_token":
                payload = self._refresh(client, params)
            else:
                raise AuthorizationError(
                    "unsupported_grant_type",
                    f"grant_type {grant_type!r} is not supported. This server "
                    "issues tokens through the authorization code flow only.",
                )
        except AuthorizationError as exc:
            return Response.json(exc.status, exc.payload())
        return Response.json(200, payload)

    def _authenticated_client(
        self, params: dict[str, str], authorization_header: str | None
    ) -> dict[str, Any]:
        client_id = (params.get("client_id") or "").strip()
        secret = params.get("client_secret")

        scheme, _, encoded = (authorization_header or "").partition(" ")
        if scheme.lower() == "basic" and encoded:
            try:
                decoded = base64.b64decode(encoded.strip()).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise AuthorizationError(
                    "invalid_client", "Malformed Basic credentials", status=401
                ) from exc
            basic_id, _, basic_secret = decoded.partition(":")
            client_id = client_id or urllib.parse.unquote(basic_id)
            secret = secret or urllib.parse.unquote(basic_secret)

        client = self._client(client_id) if client_id else None
        if client is None:
            raise AuthorizationError("invalid_client", "Unknown client", status=401)

        expected = client.get("client_secret_hash")
        if expected:
            if not secret or not secrets.compare_digest(digest(secret), expected):
                raise AuthorizationError(
                    "invalid_client", "Wrong client secret", status=401
                )
        return client

    def _exchange_code(
        self, client: dict[str, Any], params: dict[str, str]
    ) -> dict[str, Any]:
        code = params.get("code") or ""
        record = self.store.take_code(code) if code else None
        if record is None:
            raise AuthorizationError(
                "invalid_grant",
                "The authorization code is unknown, expired, or has already "
                "been used.",
            )
        if record["client_id"] != client["client_id"]:
            self.store.revoke_grant(record["grant_id"])
            raise AuthorizationError(
                "invalid_grant", "The authorization code was issued to another client"
            )
        redirect_uri = params.get("redirect_uri")
        if redirect_uri is not None and redirect_uri != record["redirect_uri"]:
            raise AuthorizationError(
                "invalid_grant", "redirect_uri does not match the authorization request"
            )
        verifier = params.get("code_verifier") or ""
        if not verifier or not _pkce_matches(verifier, record["code_challenge"]):
            # The code was claimed before this check, so a wrong verifier spends
            # it: whoever holds a stolen code gets one attempt, not a guessing
            # game. A client that fumbles its own verifier re-authorizes, which
            # is the cheaper half of that trade.
            raise AuthorizationError(
                "invalid_grant", "The PKCE code_verifier does not match the challenge"
            )
        resource = (params.get("resource") or "").strip()
        if resource and resource.rstrip("/") not in (
            self.oauth.resource,
            self.oauth.issuer,
        ):
            raise AuthorizationError(
                "invalid_target", "resource does not match the authorization request"
            )
        return self._issue(client["client_id"], record["grant_id"])

    def _refresh(
        self, client: dict[str, Any], params: dict[str, str]
    ) -> dict[str, Any]:
        presented = params.get("refresh_token") or ""
        record = self.store.get_token(presented) if presented else None
        if record is None or record.get("type") != "refresh":
            raise AuthorizationError(
                "invalid_grant", "The refresh token is expired, revoked or unknown"
            )
        if record["client_id"] != client["client_id"]:
            self.store.revoke_grant(record["grant_id"])
            raise AuthorizationError(
                "invalid_grant", "The refresh token was issued to another client"
            )
        # Rotation, not reuse: the presented token stops working the moment its
        # replacement is issued, so a stolen copy is good for one call at most.
        self.store.drop_token(presented)
        return self._issue(client["client_id"], record["grant_id"])

    def _issue(self, client_id: str, grant_id: str) -> dict[str, Any]:
        now = self._now()
        access = new_secret()
        refresh = new_secret()
        common = {
            "client_id": client_id,
            "grant_id": grant_id,
            "scope": SCOPE,
            "resource": self.oauth.resource,
        }
        self.store.put_token(
            access,
            {
                **common,
                "type": "access",
                "expires_at": now + self.oauth.access_token_ttl_seconds,
            },
        )
        self.store.put_token(
            refresh,
            {
                **common,
                "type": "refresh",
                "expires_at": now + self.oauth.refresh_token_ttl_seconds,
            },
        )
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": self.oauth.access_token_ttl_seconds,
            "refresh_token": refresh,
            "scope": SCOPE,
        }

    # -- pages -------------------------------------------------------------

    def _page(self, title: str, body: str, status: int) -> Response:
        return Response(
            status,
            _PAGE.format(title=html.escape(title), body=body),
            "text/html",
            {"Cache-Control": "no-store"},
        )

    def _error_page(self, exc: AuthorizationError) -> Response:
        return self._page(
            "Authorization refused",
            f"<h1>Authorization refused</h1>"
            f"<p class='error'>{html.escape(exc.description)}</p>"
            f"<p class='muted'>Nothing was granted, and no code was issued.</p>",
            exc.status,
        )

    def _consent_page(
        self,
        request: dict[str, Any],
        params: dict[str, str],
        message: str = "",
        status: int = 200,
    ) -> Response:
        hidden = "".join(
            f"<input type='hidden' name='{html.escape(name)}' "
            f"value='{html.escape(params.get(name) or '')}'>"
            for name in (
                "response_type",
                "client_id",
                "redirect_uri",
                "code_challenge",
                "code_challenge_method",
                "state",
                "scope",
                "resource",
            )
            if params.get(name)
        )
        client_name = html.escape(str(request["client"].get("client_name", "client")))
        warning = (
            f"<p class='error'>{html.escape(message)}</p>" if message else ""
        )
        body = f"""
    <h1>Connect {client_name}?</h1>
    <p>Approving this gives it an access token for the
    <strong>{html.escape(self.config.project_title)}</strong> board, which lets it:</p>
    <ul>
      <li><strong>read</strong> a Vikunja task by its id;</li>
      <li><strong>create</strong> a task — only in the
          {html.escape(self.config.project_title)} project.</li>
    </ul>
    <p class='muted'>It cannot edit, close, delete, comment on or move any task,
    and it cannot see any other project. Tokens are short-lived and can be
    revoked at any time by stopping the service.</p>
    <p class='muted'>Code goes to <code>{html.escape(request['redirect_uri'])}</code></p>
    {warning}
    <form method="post">
      {hidden}
      <label for="passphrase">Operator passphrase</label>
      <input id="passphrase" name="passphrase" type="password" autocomplete="off"
             autofocus required>
      <button type="submit">Approve</button>
    </form>
"""
        return self._page("Authorize the Vikunja MCP boundary", body, status)


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.5 system-ui, sans-serif; max-width: 34rem;
         margin: 3rem auto; padding: 0 1.25rem; }}
  h1 {{ font-size: 1.4rem; }}
  code {{ font-size: 0.9em; word-break: break-all; }}
  .muted {{ opacity: 0.75; font-size: 0.9rem; }}
  .error {{ color: #b3261e; font-weight: 600; }}
  @media (prefers-color-scheme: dark) {{ .error {{ color: #f2b8b5; }} }}
  label {{ display: block; margin-top: 1.5rem; font-weight: 600; }}
  input {{ width: 100%; padding: 0.6rem; margin-top: 0.35rem;
           font: inherit; box-sizing: border-box; }}
  button {{ margin-top: 1rem; padding: 0.6rem 1.4rem; font: inherit; }}
</style></head>
<body>{body}</body></html>
"""
