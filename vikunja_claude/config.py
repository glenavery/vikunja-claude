"""Runtime configuration, resolved from the environment.

Every setting has exactly one name and one meaning. An optional ``.env`` file
next to the package is read first so manual runs work without exporting
anything; real environment variables always win.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PACKAGE_ROOT / ".env"

# Shared by both services, so they cannot drift apart: the launcher and the MCP
# server must talk to the same Vikunja and mean the same project by "project".
DEFAULT_API_URL = "http://127.0.0.1:3456/api/v1"
DEFAULT_FRONTEND_URL = "http://127.0.0.1:3456"
DEFAULT_PROJECT = "AI Alpha Engine"

# Where ChatGPT sends the browser back to after the operator approves. It is a
# default rather than a required setting because it is a property of ChatGPT,
# not of this deployment — but it is still matched exactly, and anything else
# has to be named here on purpose.
DEFAULT_REDIRECT_URIS = ("https://chatgpt.com/connector_platform_oauth_redirect",)

# Short enough that a leaked access token is a small window, long enough that a
# conversation does not stop mid-way. Refresh covers the rest.
ACCESS_TOKEN_TTL_SECONDS = 3600
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600
# An authorization code is redeemed by a server that already has it; a minute
# is generous for a round trip and short for anyone who found one in a log.
CODE_TTL_SECONDS = 60

MIN_PASSPHRASE_CHARS = 32


def load_env_file(path: Path) -> None:
    """Populate ``os.environ`` from a simple KEY=VALUE file, without overriding."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


class ConfigError(RuntimeError):
    """Raised when the service is not configured well enough to start."""


def _vikunja_token() -> str:
    token = os.environ.get("VIKUNJA_API_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "VIKUNJA_API_TOKEN is not set. Put it in "
            f"{DEFAULT_ENV_FILE} or export it before starting the service."
        )
    return token


def _state_dir() -> Path:
    return Path(
        os.environ.get(
            "VIKUNJA_CLAUDE_STATE_DIR",
            Path.home() / ".local" / "state" / "vikunja-claude",
        )
    )


def _project_id_override() -> int | None:
    raw = os.environ.get("VIKUNJA_PROJECT_ID", "").strip()
    return int(raw) if raw else None


@dataclass(frozen=True)
class Config:
    api_url: str
    token: str
    project_title: str
    workdir: Path
    claude_bin: str
    claude_args: list[str]
    host: str
    port: int
    state_dir: Path
    frontend_url: str
    launch_timeout_seconds: int
    project_id: int | None = None
    _log_path: Path | None = field(default=None, repr=False)

    @property
    def log_path(self) -> Path:
        return self._log_path or self.state_dir / "launches.jsonl"

    @property
    def lock_dir(self) -> Path:
        return self.state_dir / "locks"

    @property
    def run_log_dir(self) -> Path:
        return self.state_dir / "runs"

    @classmethod
    def from_env(cls, env_file: Path | None = DEFAULT_ENV_FILE) -> "Config":
        if env_file is not None:
            load_env_file(env_file)

        return cls(
            api_url=os.environ.get("VIKUNJA_API_URL", DEFAULT_API_URL).rstrip("/"),
            token=_vikunja_token(),
            project_title=os.environ.get("VIKUNJA_PROJECT", DEFAULT_PROJECT),
            project_id=_project_id_override(),
            workdir=Path(
                os.environ.get("CLAUDE_WORKDIR", "/home/glen/stacks/investment")
            ),
            claude_bin=os.environ.get("CLAUDE_BIN", "claude"),
            claude_args=shlex.split(
                os.environ.get("CLAUDE_ARGS", "-p --permission-mode acceptEdits")
            ),
            host=os.environ.get("VIKUNJA_CLAUDE_HOST", "127.0.0.1"),
            port=int(os.environ.get("VIKUNJA_CLAUDE_PORT", "3460")),
            state_dir=_state_dir(),
            frontend_url=os.environ.get(
                "VIKUNJA_FRONTEND_URL", DEFAULT_FRONTEND_URL
            ).rstrip("/"),
            launch_timeout_seconds=int(
                os.environ.get("CLAUDE_LAUNCH_TIMEOUT_SECONDS", "10800")
            ),
        )


def _absolute_https_url(name: str, value: str, allow_loopback: bool) -> str:
    """A URL that can be published, or an explicit refusal.

    Every one of these ends up in a metadata document a client trusts, so a
    value that is nearly right — a path, a fragment, plain http — is worth more
    as a startup failure than as a flow that half works.
    """
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise ConfigError(f"{name} must be an absolute http(s) URL, not {value!r}.")
    loopback = parsed.hostname in ("127.0.0.1", "::1", "localhost")
    if parsed.scheme != "https" and not (allow_loopback and loopback):
        raise ConfigError(
            f"{name} must be https: {value!r} would carry authorization codes "
            "and tokens over the public internet in clear text."
        )
    if parsed.query or parsed.fragment:
        raise ConfigError(f"{name} must have no query string or fragment: {value!r}")
    return value


@dataclass(frozen=True)
class OAuthConfig:
    """The OAuth boundary ChatGPT authenticates through.

    This is an authorization server for exactly one operator and one grant. It
    holds no user records and issues one scope, so "who is this" is a
    passphrase and "what may they do" is not a question with more than one
    answer.
    """

    issuer: str
    passphrase: str
    redirect_uris: tuple[str, ...]
    client_id: str | None = None
    client_secret: str | None = None
    access_token_ttl_seconds: int = ACCESS_TOKEN_TTL_SECONDS
    refresh_token_ttl_seconds: int = REFRESH_TOKEN_TTL_SECONDS
    code_ttl_seconds: int = CODE_TTL_SECONDS

    @property
    def resource(self) -> str:
        """The canonical URI of the MCP server, which tokens are bound to."""
        return f"{self.issuer}/mcp"

    @property
    def authorization_endpoint(self) -> str:
        return f"{self.issuer}/oauth/authorize"

    @property
    def token_endpoint(self) -> str:
        return f"{self.issuer}/oauth/token"

    @property
    def registration_endpoint(self) -> str:
        return f"{self.issuer}/oauth/register"

    @property
    def protected_resource_metadata_url(self) -> str:
        return f"{self.issuer}/.well-known/oauth-protected-resource"

    def static_client(self, client_id: str) -> dict[str, Any] | None:
        """The pre-registered client, if one is configured and this is it."""
        if not self.client_id or client_id != self.client_id:
            return None
        record: dict[str, Any] = {
            "client_id": self.client_id,
            "client_name": "configured client",
            "redirect_uris": list(self.redirect_uris),
            "token_endpoint_auth_method": (
                "client_secret_post" if self.client_secret else "none"
            ),
        }
        if self.client_secret:
            record["client_secret_hash"] = hashlib.sha256(
                self.client_secret.encode("utf-8")
            ).hexdigest()
        return record

    @classmethod
    def from_env(cls) -> "OAuthConfig":
        issuer = os.environ.get("VIKUNJA_MCP_OAUTH_ISSUER", "").strip().rstrip("/")
        if not issuer:
            # There is no unauthenticated mode and no fallback to the static
            # bearer token this replaced. Missing configuration is a service
            # that does not start, which is the safe end of that choice.
            raise ConfigError(
                "VIKUNJA_MCP_OAUTH_ISSUER is not set, and the MCP server has no "
                "unauthenticated mode. It is the public HTTPS base URL clients "
                "reach this server on, e.g. "
                "https://aiserver.tail36601d.ts.net:8443 — the OAuth metadata "
                "documents are published under it."
            )
        issuer = _absolute_https_url(
            "VIKUNJA_MCP_OAUTH_ISSUER", issuer, allow_loopback=True
        )

        passphrase = os.environ.get("VIKUNJA_MCP_OAUTH_PASSPHRASE", "").strip()
        if not passphrase:
            raise ConfigError(
                "VIKUNJA_MCP_OAUTH_PASSPHRASE is not set. It is what proves an "
                "authorization request is yours; without it the consent screen "
                "would approve anybody. Generate one with "
                "`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`."
            )
        if len(passphrase) < MIN_PASSPHRASE_CHARS:
            raise ConfigError(
                f"VIKUNJA_MCP_OAUTH_PASSPHRASE is {len(passphrase)} characters. "
                "It is on the public internet in front of a write path into the "
                f"board; use at least {MIN_PASSPHRASE_CHARS}."
            )

        raw = os.environ.get("VIKUNJA_MCP_OAUTH_REDIRECT_URIS", "").strip()
        redirect_uris = (
            tuple(
                _absolute_https_url(
                    "VIKUNJA_MCP_OAUTH_REDIRECT_URIS", uri, allow_loopback=True
                )
                for uri in raw.replace(",", " ").split()
            )
            if raw
            else DEFAULT_REDIRECT_URIS
        )
        if not redirect_uris:
            raise ConfigError(
                "VIKUNJA_MCP_OAUTH_REDIRECT_URIS is set but empty. Leave it "
                "unset for the ChatGPT default rather than clearing it."
            )

        client_secret = os.environ.get("VIKUNJA_MCP_OAUTH_CLIENT_SECRET", "").strip()
        client_id = os.environ.get("VIKUNJA_MCP_OAUTH_CLIENT_ID", "").strip()
        if client_secret and not client_id:
            raise ConfigError(
                "VIKUNJA_MCP_OAUTH_CLIENT_SECRET is set without "
                "VIKUNJA_MCP_OAUTH_CLIENT_ID, so nothing would ever present it."
            )

        return cls(
            issuer=issuer,
            passphrase=passphrase,
            redirect_uris=redirect_uris,
            client_id=client_id or None,
            client_secret=client_secret or None,
        )


@dataclass(frozen=True)
class McpConfig:
    """Settings for the MCP integration boundary.

    Deliberately a separate object from :class:`Config` rather than more fields
    on it. The launcher must start without any OAuth configuration, the MCP
    server must refuse to start without it, and neither should be able to make
    the other fail to boot. They share the Vikunja settings because there is
    only one Vikunja and one board — not because the two services are one thing.
    """

    api_url: str
    token: str
    oauth: OAuthConfig
    project_title: str
    frontend_url: str
    host: str
    port: int
    state_dir: Path
    project_id: int | None = None

    @property
    def ledger_path(self) -> Path:
        """Every task this boundary created: the audit record and the dedup key."""
        return self.state_dir / "mcp_created_tasks.jsonl"

    @property
    def oauth_state_path(self) -> Path:
        """Registered clients, live codes and live tokens.

        Beside the ledger on purpose: deleting this file is the revocation
        step, and it should be somewhere an operator can find without reading
        the source.
        """
        return self.state_dir / "mcp_oauth.json"

    @classmethod
    def from_env(cls, env_file: Path | None = DEFAULT_ENV_FILE) -> "McpConfig":
        if env_file is not None:
            load_env_file(env_file)

        return cls(
            api_url=os.environ.get("VIKUNJA_API_URL", DEFAULT_API_URL).rstrip("/"),
            token=_vikunja_token(),
            oauth=OAuthConfig.from_env(),
            project_title=os.environ.get("VIKUNJA_PROJECT", DEFAULT_PROJECT),
            project_id=_project_id_override(),
            frontend_url=os.environ.get(
                "VIKUNJA_FRONTEND_URL", DEFAULT_FRONTEND_URL
            ).rstrip("/"),
            host=os.environ.get("VIKUNJA_MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("VIKUNJA_MCP_PORT", "3461")),
            state_dir=_state_dir(),
        )
