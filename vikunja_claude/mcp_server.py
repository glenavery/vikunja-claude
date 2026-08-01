"""HTTP front end for the MCP boundary: the OAuth flow, and one MCP route.

Separate process and separate port from the launcher, so this integration can
be stopped without stopping anything else, and so a request that reaches this
server cannot reach the launcher's routes even by accident.

Transport is MCP Streamable HTTP: JSON-RPC over `POST /mcp`, answered as JSON
or as a single SSE event depending on what the client says it accepts. There is
no server-initiated stream, so `GET /mcp` is a 405 rather than a half-working
channel.

Authentication is OAuth 2.1 (`oauth.py`): the well-known metadata documents and
the `/oauth/*` endpoints exist so ChatGPT can obtain a token, and `/mcp` accepts
nothing else. This handler decides *routing*; every decision about whether a
credential is good belongs to the authorization server, so that answer is
produced in one place rather than one per route.
"""

from __future__ import annotations

import json
import sys
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import ConfigError, McpConfig
from .mcp import INTERNAL_ERROR, PARSE_ERROR, JsonRpcError, McpProtocol
from .mcp_service import McpService
from .oauth import AuthorizationServer, Response
from .oauth_store import OAuthStore
from .vikunja import VikunjaClient

# Descriptions are the biggest thing that legitimately arrives here.
MAX_BODY_BYTES = 1 * 1024 * 1024
# Form posts and registration bodies are small; nothing legitimate is large.
MAX_FORM_BYTES = 64 * 1024

# Both the bare and the resource-path form of each metadata document: a client
# that treats the resource path as part of the issuer looks for it under the
# path, and answering both costs one tuple entry.
PROTECTED_RESOURCE_PATHS = (
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
)
AUTHORIZATION_SERVER_PATHS = (
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-authorization-server/mcp",
)


def _error_body(code: int, message: str) -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}
    )


def _form(raw: str) -> dict[str, str]:
    return {
        key: values[-1]
        for key, values in urllib.parse.parse_qs(raw, keep_blank_values=True).items()
    }


class McpHandler(BaseHTTPRequestHandler):
    server_version = "vikunja-claude-mcp"
    protocol: McpProtocol  # injected on the server instance
    config: McpConfig
    oauth: AuthorizationServer

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), format % args))

    # -- plumbing ----------------------------------------------------------

    def _send(
        self, status: int, body: str, content_type: str, extra: dict | None = None
    ) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        extra = extra or {}
        if "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-store")
        for name, value in extra.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def _send_empty(self, status: int, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _send_response(self, response: Response) -> None:
        """Hand back what the authorization server decided, unaltered."""
        if not response.body:
            self._send_empty(response.status, response.headers)
            return
        self._send(
            response.status, response.body, response.content_type, response.headers
        )

    def _send_rpc(self, response: dict) -> None:
        """One JSON-RPC response, framed the way the client asked for it."""
        body = json.dumps(response, default=str)
        if "text/event-stream" in (self.headers.get("Accept") or ""):
            self._send(200, f"event: message\ndata: {body}\n\n", "text/event-stream")
        else:
            self._send(200, body, "application/json")

    def _read_body(self, limit: int) -> str | None:
        """The body as text, or None once a refusal has already been sent."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(
                400, _error_body(PARSE_ERROR, "Bad Content-Length"), "application/json"
            )
            return None
        if length > limit:
            self._send(
                413,
                _error_body(PARSE_ERROR, f"Body exceeds {limit} bytes"),
                "application/json",
            )
            return None
        raw = self.rfile.read(length) if length else b""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            self._send(
                400, _error_body(PARSE_ERROR, "Body is not UTF-8"), "application/json"
            )
            return None

    # -- gates -------------------------------------------------------------

    def _path(self) -> str:
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    def _query(self) -> dict[str, str]:
        return _form(self.path.split("?", 1)[1] if "?" in self.path else "")

    def _reject_browser_origin(self) -> bool:
        """Refuse anything that presents an Origin.

        An MCP client is server-to-server and sends none. A browser always
        sends one, so this closes DNS rebinding as a class instead of
        maintaining a list of origins that are supposed to be safe. It guards
        `/mcp` only: the consent screen *is* a browser page, and what protects
        the OAuth endpoints is PKCE and the operator passphrase.
        """
        if self.headers.get("Origin") is None:
            return False
        self._send(
            403,
            _error_body(INTERNAL_ERROR, "Requests carrying an Origin are refused"),
            "application/json",
        )
        return True

    def _authorized(self) -> bool:
        check = self.oauth.authenticate(self.headers.get("Authorization"))
        if check.ok:
            return True
        # No fallback path: a request that fails here gets nothing — not a
        # reduced or anonymous version of the same surface, and not the static
        # bearer token this replaced, which no code here can accept.
        self._send(
            401,
            _error_body(
                INTERNAL_ERROR, check.description or "An OAuth access token is required"
            ),
            "application/json",
            extra={"WWW-Authenticate": self.oauth.challenge(check)},
        )
        return False

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = self._path()
        if path == "/health":
            # Deliberately says nothing about Vikunja, the board or the config:
            # it is reachable without a token, so it may only prove liveness.
            self._send(
                200,
                json.dumps({"status": "ok", "service": "vikunja-claude-mcp"}),
                "application/json",
            )
            return
        if path in PROTECTED_RESOURCE_PATHS:
            self._send_response(self.oauth.protected_resource_metadata())
            return
        if path in AUTHORIZATION_SERVER_PATHS:
            self._send_response(self.oauth.authorization_server_metadata())
            return
        if path == "/oauth/authorize":
            self._send_response(self.oauth.authorize(self._query()))
            return
        if path == "/mcp":
            self._send_empty(405, {"Allow": "POST"})
            return
        self._send_empty(404)

    def do_DELETE(self) -> None:  # noqa: N802
        self._send_empty(405, {"Allow": "POST"})

    def do_POST(self) -> None:  # noqa: N802
        path = self._path()
        if path in ("/oauth/authorize", "/oauth/token", "/oauth/register"):
            body = self._read_body(MAX_FORM_BYTES)
            if body is None:
                return
            if path == "/oauth/authorize":
                self._send_response(self.oauth.approve(_form(body)))
            elif path == "/oauth/token":
                self._send_response(
                    self.oauth.token(body, self.headers.get("Authorization"))
                )
            else:
                self._send_response(self.oauth.register(body))
            return
        if path != "/mcp":
            self._send_empty(404)
            return
        if self._reject_browser_origin():
            return
        if not self._authorized():
            return

        raw = self._read_body(MAX_BODY_BYTES)
        if raw is None:
            return
        try:
            message = json.loads(raw or "null")
        except json.JSONDecodeError:
            self._send(
                400, _error_body(PARSE_ERROR, "Body is not JSON"), "application/json"
            )
            return

        try:
            response = self.protocol.handle(message)
        except JsonRpcError as exc:
            self._send(
                400,
                _error_body(exc.code, exc.message),
                "application/json",
            )
            return
        except Exception:
            # The traceback goes to the journal, never to the client: it can
            # carry request bodies and client internals.
            traceback.print_exc(file=sys.stderr)
            self._send(
                500,
                _error_body(INTERNAL_ERROR, "The MCP server failed to handle that"),
                "application/json",
            )
            return

        if response is None:
            # A notification. Accepted, nothing to say back.
            self._send_empty(202)
            return
        self._send_rpc(response)


def build_mcp_server(
    config: McpConfig, client: VikunjaClient | None = None
) -> ThreadingHTTPServer:
    if config.host not in ("127.0.0.1", "::1", "localhost"):
        raise ConfigError(
            f"Refusing to bind to {config.host!r}: this server holds a write "
            "credential for the board and must be published by a TLS front end "
            "(Tailscale Funnel), never by listening on a public interface itself."
        )
    client = client or VikunjaClient(config.api_url, config.token)
    service = McpService(config, client)
    protocol = McpProtocol(service.tools())
    authorization = AuthorizationServer(config, OAuthStore(config.oauth_state_path))
    handler = type(
        "BoundMcpHandler",
        (McpHandler,),
        {"protocol": protocol, "config": config, "oauth": authorization},
    )
    return ThreadingHTTPServer((config.host, config.port), handler)


def main() -> int:
    try:
        config = McpConfig.from_env()
        server = build_mcp_server(config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    # The two AI Server capabilities are the parts of the surface that can be
    # absent, so which way each was resolved is said at startup rather than
    # discovered from a tool that is missing.
    operational = (
        # One line for both, because they are one setting: the three reads and
        # the test-paying page read (task 239) are switched on together, with
        # the same key against the same admin instance.
        f"operational reads and test-paying page read via {config.investment.api_url}"
        if config.investment is not None
        else "operational reads and test-paying page read OFF "
        "(INVESTMENT_API_URL/INVESTMENT_API_KEY unset)"
    )
    page_fetch = (
        f"public page fetch via {config.public_site_url}"
        if config.public_site_url is not None
        else "public page fetch OFF (INVESTMENT_PUBLIC_URL unset)"
    )
    print(
        f"vikunja-claude-mcp listening on http://{config.host}:{config.port}/mcp "
        f"— project {config.project_title!r}, OAuth issuer {config.oauth.issuer}, "
        f"{operational}, {page_fetch}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
