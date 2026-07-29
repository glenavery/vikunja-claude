"""HTTP front end for the MCP boundary: one route, one credential.

Separate process and separate port from the launcher, so this integration can
be stopped without stopping anything else, and so a request that reaches this
server cannot reach the launcher's routes even by accident.

Transport is MCP Streamable HTTP: JSON-RPC over `POST /mcp`, answered as JSON
or as a single SSE event depending on what the client says it accepts. There is
no server-initiated stream, so `GET /mcp` is a 405 rather than a half-working
channel.
"""

from __future__ import annotations

import hmac
import json
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import ConfigError, McpConfig
from .mcp import INTERNAL_ERROR, PARSE_ERROR, JsonRpcError, McpProtocol
from .mcp_service import McpService
from .vikunja import VikunjaClient

# Descriptions are the biggest thing that legitimately arrives here.
MAX_BODY_BYTES = 1 * 1024 * 1024


def _error_body(code: int, message: str) -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}
    )


class McpHandler(BaseHTTPRequestHandler):
    server_version = "vikunja-claude-mcp"
    protocol: McpProtocol  # injected on the server instance
    config: McpConfig

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
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def _send_empty(self, status: int, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _send_rpc(self, response: dict) -> None:
        """One JSON-RPC response, framed the way the client asked for it."""
        body = json.dumps(response, default=str)
        if "text/event-stream" in (self.headers.get("Accept") or ""):
            self._send(200, f"event: message\ndata: {body}\n\n", "text/event-stream")
        else:
            self._send(200, body, "application/json")

    # -- gates -------------------------------------------------------------

    def _path(self) -> str:
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    def _reject_browser_origin(self) -> bool:
        """Refuse anything that presents an Origin.

        An MCP client is server-to-server and sends none. A browser always
        sends one, so this closes DNS rebinding as a class instead of
        maintaining a list of origins that are supposed to be safe.
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
        header = self.headers.get("Authorization") or ""
        scheme, _, presented = header.partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(
            presented.strip(), self.config.mcp_token
        ):
            return True
        # No fallback path: a request that fails here gets nothing, not a
        # reduced or anonymous version of the same surface.
        self._send(
            401,
            _error_body(INTERNAL_ERROR, "A valid bearer token is required"),
            "application/json",
            extra={"WWW-Authenticate": 'Bearer realm="vikunja-claude-mcp"'},
        )
        return False

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = self._path()
        if path == "/health":
            # Deliberately says nothing about Vikunja, the board or the config:
            # it is reachable without the token, so it may only prove liveness.
            self._send(
                200,
                json.dumps({"status": "ok", "service": "vikunja-claude-mcp"}),
                "application/json",
            )
            return
        if path == "/mcp":
            self._send_empty(405, {"Allow": "POST"})
            return
        self._send_empty(404)

    def do_DELETE(self) -> None:  # noqa: N802
        self._send_empty(405, {"Allow": "POST"})

    def do_POST(self) -> None:  # noqa: N802
        if self._path() != "/mcp":
            self._send_empty(404)
            return
        if self._reject_browser_origin():
            return
        if not self._authorized():
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, _error_body(PARSE_ERROR, "Bad Content-Length"), "application/json")
            return
        if length > MAX_BODY_BYTES:
            self._send(
                413,
                _error_body(PARSE_ERROR, f"Body exceeds {MAX_BODY_BYTES} bytes"),
                "application/json",
            )
            return

        raw = self.rfile.read(length) if length else b""
        try:
            message = json.loads(raw or b"null")
        except json.JSONDecodeError:
            self._send(400, _error_body(PARSE_ERROR, "Body is not JSON"), "application/json")
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
    handler = type(
        "BoundMcpHandler",
        (McpHandler,),
        {"protocol": protocol, "config": config},
    )
    return ThreadingHTTPServer((config.host, config.port), handler)


def main() -> int:
    try:
        config = McpConfig.from_env()
        server = build_mcp_server(config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    print(
        f"vikunja-claude-mcp listening on http://{config.host}:{config.port}/mcp "
        f"— project {config.project_title!r}, bearer token required",
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
