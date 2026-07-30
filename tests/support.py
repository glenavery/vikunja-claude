"""Shared fixtures for the test suite."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from vikunja_claude.config import Config, McpConfig, OAuthConfig
from vikunja_claude.launcher import Launcher
from vikunja_claude.mcp import McpProtocol
from vikunja_claude.mcp_server import build_mcp_server
from vikunja_claude.mcp_service import McpService
from vikunja_claude.service import TicketService
from vikunja_claude.vikunja import VikunjaClient

from .fakes import FakeVikunja, RecordingSpawn

TOKEN = "test-token-never-in-prompts"
PASSPHRASE = "test-operator-passphrase-long-enough-to-be-plausible"
ISSUER = "http://127.0.0.1:8443"
REDIRECT_URI = "https://chatgpt.com/connector_platform_oauth_redirect"
#: The per-connector callback ChatGPT actually submits now. The identifier is
#: the one from the live failure, because a made-up one would not show that the
#: value is opaque and unknown until the connector exists.
CONNECTOR_REDIRECT_URI = "https://chatgpt.com/connector/oauth/jFpZaNIKITJA"


def make_oauth_config(**overrides) -> OAuthConfig:
    defaults = dict(
        issuer=ISSUER,
        passphrase=PASSPHRASE,
        redirect_uris=(REDIRECT_URI,),
    )
    defaults.update(overrides)
    return OAuthConfig(**defaults)


def make_config(state_dir: Path, **overrides) -> Config:
    defaults = dict(
        api_url="http://127.0.0.1:3456/api/v1",
        token=TOKEN,
        project_title="AI Alpha Engine",
        workdir=Path("/home/glen/stacks/investment"),
        claude_bin="claude",
        claude_args=["-p", "--permission-mode", "acceptEdits"],
        host="127.0.0.1",
        port=3460,
        state_dir=state_dir,
        frontend_url="http://127.0.0.1:3456",
        launch_timeout_seconds=60,
        project_id=None,
    )
    defaults.update(overrides)
    return Config(**defaults)


def make_mcp_config(state_dir: Path, **overrides) -> McpConfig:
    oauth = overrides.pop("oauth", None) or make_oauth_config()
    defaults = dict(
        api_url="http://127.0.0.1:3456/api/v1",
        token=TOKEN,
        oauth=oauth,
        project_title="AI Alpha Engine",
        frontend_url="http://127.0.0.1:3456",
        host="127.0.0.1",
        # 0 asks the OS for a free port, so tests never collide with the
        # running service or with each other.
        port=0,
        state_dir=state_dir,
        project_id=None,
    )
    defaults.update(overrides)
    return McpConfig(**defaults)


class McpTestCase(unittest.TestCase):
    """An McpService and protocol wired to a fake Vikunja."""

    layout = None
    comments: dict | None = None
    vikunja_fail: Exception | None = None

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)
        self.config = make_mcp_config(self.state_dir)
        self.vikunja = FakeVikunja(
            layout=self.layout, fail=self.vikunja_fail, comments=self.comments
        )
        self.client = VikunjaClient(
            self.config.api_url, self.config.token, transport=self.vikunja
        )
        self.service = McpService(self.config, self.client)
        self.protocol = McpProtocol(self.service.tools())

    def call_tool(self, name: str, **arguments) -> dict:
        """Drive a tool the way a client does, through the protocol."""
        response = self.protocol.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        assert response is not None
        return response


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Let the test see the redirect rather than chase it to chatgpt.com."""

    def redirect_request(self, *args, **kwargs):  # noqa: D102
        return None


#: `open(token=...)` was not given, so use the access token from a real flow.
UNSET = object()


class HttpTestCase(unittest.TestCase):
    """A live MCP server on an ephemeral port, plus the OAuth flow to reach it.

    The OAuth helpers here drive the same endpoints a client does, over the
    socket, because the thing being tested is what the server accepts — and a
    flow assembled by calling the authorization server's methods directly can
    pass while the routes in front of them do not.
    """

    oauth_overrides: dict = {}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)
        self.config = make_mcp_config(
            self.state_dir, oauth=make_oauth_config(**self.oauth_overrides)
        )
        self.vikunja = FakeVikunja()
        client = VikunjaClient(
            self.config.api_url, self.config.token, transport=self.vikunja
        )
        self.server = build_mcp_server(self.config, client=client)
        self.addCleanup(self.server.server_close)
        # A short poll interval only so shutdown() is prompt: the default 0.5s
        # is per test, not per request, and dominates the run.
        thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.shutdown)
        self.origin = "http://127.0.0.1:%d" % self.server.server_address[1]
        self._opener = urllib.request.build_opener(_NoRedirects)
        self._access_token: str | None = None

    # -- transport ---------------------------------------------------------

    def open(
        self,
        path: str = "/mcp",
        body: dict | str | None = None,
        method: str | None = None,
        token=UNSET,
        accept: str = "application/json",
        headers: dict | None = None,
        form: dict | None = None,
    ):
        """Returns (status, headers, body-text). Never raises on 3xx/4xx/5xx."""
        payload = None
        content_type = None
        if form is not None:
            payload = urllib.parse.urlencode(form).encode()
            content_type = "application/x-www-form-urlencoded"
        elif body is not None:
            payload = (body if isinstance(body, str) else json.dumps(body)).encode()
            content_type = "application/json"
        request = urllib.request.Request(
            self.origin + path,
            data=payload,
            method=method or ("POST" if payload is not None else "GET"),
        )
        request.add_header("Accept", accept)
        if content_type is not None:
            request.add_header("Content-Type", content_type)
        if token is UNSET:
            token = self.access_token()
        if token is not None:
            request.add_header("Authorization", f"Bearer {token}")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with self._opener.open(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read().decode()

    def rpc(self, method: str, params: dict | None = None, **kwargs):
        message = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            message["params"] = params
        return self.open(body=message, **kwargs)

    # -- the OAuth flow ----------------------------------------------------

    def register_client(self, **overrides) -> dict:
        payload = {
            "client_name": "ChatGPT (test)",
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }
        payload.update(overrides)
        status, _, text = self.open("/oauth/register", body=payload, token=None)
        return {"status": status, **json.loads(text)}

    @staticmethod
    def pkce() -> tuple[str, str]:
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        return verifier, challenge

    def authorize_params(self, client_id: str, challenge: str, **overrides) -> dict:
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "opaque-state",
            "scope": "vikunja:tickets",
            "resource": self.config.oauth.resource,
        }
        params.update(overrides)
        return {key: value for key, value in params.items() if value is not None}

    def approve(self, params: dict, passphrase: str = PASSPHRASE):
        """Submit the consent form. Returns (status, headers, body)."""
        return self.open(
            "/oauth/authorize", form={**params, "passphrase": passphrase}, token=None
        )

    @staticmethod
    def redirect_query(headers: dict) -> dict[str, str]:
        location = headers["Location"]
        return {
            key: values[-1]
            for key, values in urllib.parse.parse_qs(
                urllib.parse.urlsplit(location).query
            ).items()
        }

    def obtain_code(self, **overrides) -> tuple[str, str, str]:
        """Run the flow up to the authorization code. Returns (code, verifier, client_id)."""
        client = self.register_client()
        verifier, challenge = self.pkce()
        params = self.authorize_params(client["client_id"], challenge, **overrides)
        status, headers, _ = self.approve(params)
        assert status == 302, status
        return self.redirect_query(headers)["code"], verifier, client["client_id"]

    def exchange(self, code: str, verifier: str, client_id: str, **overrides):
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": self.config.oauth.resource,
        }
        form.update(overrides)
        status, _, text = self.open(
            "/oauth/token",
            form={k: v for k, v in form.items() if v is not None},
            token=None,
        )
        return status, json.loads(text)

    def issue_tokens(self) -> dict:
        """One complete authorization, from registration to a token response."""
        code, verifier, client_id = self.obtain_code()
        status, payload = self.exchange(code, verifier, client_id)
        assert status == 200, payload
        return payload

    def access_token(self) -> str:
        if self._access_token is None:
            self._access_token = str(self.issue_tokens()["access_token"])
        return self._access_token


class ServiceTestCase(unittest.TestCase):
    """A TicketService wired to fakes, with a throwaway state directory."""

    layout = None
    vikunja_fail: Exception | None = None

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)
        self.config = make_config(self.state_dir)
        self.vikunja = FakeVikunja(layout=self.layout, fail=self.vikunja_fail)
        self.client = VikunjaClient(
            self.config.api_url, self.config.token, transport=self.vikunja
        )
        self.spawn = RecordingSpawn()
        # Only PIDs added here look alive, so stale locks are testable.
        self.alive_pids: set[int] = set()
        self.launcher = Launcher(
            self.config,
            spawn=self.spawn,
            is_alive=lambda pid: pid in self.alive_pids,
            reap=False,
        )
        self.service = TicketService(self.config, self.client, self.launcher)
