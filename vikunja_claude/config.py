"""Runtime configuration, resolved from the environment.

Every setting has exactly one name and one meaning. An optional ``.env`` file
next to the package is read first so manual runs work without exporting
anything; real environment variables always win.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
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

# ChatGPT now mints a callback per connector, so the address is not knowable
# until the connector exists: the live dialog registered
# https://chatgpt.com/connector/oauth/jFpZaNIKITJA and was refused, because a
# configured list can only name the fixed path above.
#
# That shape is therefore admitted by its form rather than by name — at dynamic
# client registration, and nowhere else. What is stored on the client is the
# complete URI that was submitted; every check after registration is exact
# equality against that string, so this predicate never runs on the path that
# decides where an authorization code is sent.
CHATGPT_CONNECTOR_HOST = "chatgpt.com"
CHATGPT_CONNECTOR_PATH = "/connector/oauth"

# Unreserved characters only (RFC 3986 §2.3). It is deliberately narrower than
# "one path segment": a segment may legally hold percent-escapes and sub-delims,
# and `%2F` is a path separator to whatever normalises it later even though it
# is one segment here. An opaque identifier ChatGPT generated needs none of them.
_CONNECTOR_IDENTIFIER = re.compile(r"[A-Za-z0-9._~-]+")


def is_chatgpt_connector_redirect_uri(value: str) -> bool:
    """Is this ChatGPT's current per-connector OAuth callback?

    Every clause is a refusal in its own right, and the last one is the backstop
    that makes the set total: the value must be **identical** to the canonical
    URI rebuilt from the parts that were checked. Anything the checks did not
    look at — userinfo, a port, a stray ``?`` or ``#``, an uppercase host — is a
    difference from that reconstruction, so it is refused without having to be
    anticipated.
    """
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc != CHATGPT_CONNECTOR_HOST:
        return False
    if parsed.query or parsed.fragment:
        return False
    prefix, _, identifier = parsed.path.rpartition("/")
    if prefix != CHATGPT_CONNECTOR_PATH:
        return False
    if not _CONNECTOR_IDENTIFIER.fullmatch(identifier):
        return False
    if not identifier.strip("."):
        # "." and ".." are unreserved characters that are not an identifier.
        return False
    return value == (
        f"https://{CHATGPT_CONNECTOR_HOST}{CHATGPT_CONNECTOR_PATH}/{identifier}"
    )

# Short enough that a leaked access token is a small window, long enough that a
# conversation does not stop mid-way. Refresh covers the rest.
ACCESS_TOKEN_TTL_SECONDS = 3600
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600
# An authorization code is redeemed by a server that already has it; a minute
# is generous for a round trip and short for anyone who found one in a log.
CODE_TTL_SECONDS = 60

MIN_PASSPHRASE_CHARS = 32

# The investment application's admin instance, which owns the operational reads
# task 138 approved. There is deliberately **no default URL**: the operational
# endpoints exist only on the admin instance, and a default pointing at the
# public one would turn "not configured" into a stream of 404s that read like a
# broken API rather than like an unconfigured integration.
INVESTMENT_API_URL_ENV = "INVESTMENT_API_URL"
INVESTMENT_API_KEY_ENV = "INVESTMENT_API_KEY"


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


#: Tailscale's address space. Traffic inside a tailnet is WireGuard-encrypted, so
#: plain http to one of these carries the API key over an encrypted link — which
#: is why this is not the same rule as :func:`_absolute_https_url`, whose concern
#: is OAuth metadata published to the public internet.
_TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_TAILNET_SUFFIX = ".ts.net"


def _is_private_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    host = hostname.lower().rstrip(".")
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    if host.endswith(_TAILNET_SUFFIX):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    return address in _TAILNET_V4 or address in _TAILNET_V6


def _private_api_url(name: str, value: str) -> str:
    """An http(s) URL that an API key may be sent to, or an explicit refusal.

    The investment admin instance is bound to a Tailscale address and serves
    plain http, which is correct — the tailnet is the encryption. So http is
    permitted **only** to loopback or a tailnet address. Anywhere else it would
    put ``INVESTMENT_API_KEY`` on the wire in clear text, and a typo in a hostname
    is exactly how that happens.
    """
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise ConfigError(f"{name} must be an absolute http(s) URL, not {value!r}.")
    if parsed.query or parsed.fragment:
        raise ConfigError(f"{name} must have no query string or fragment: {value!r}")
    if parsed.path.rstrip("/"):
        # The three read paths are appended to this base. A base carrying a path
        # of its own would silently produce a different URL than the one named in
        # `vikunja_claude.investment`.
        raise ConfigError(
            f"{name} must be a bare scheme://host:port with no path: {value!r}"
        )
    if parsed.scheme == "http" and not _is_private_host(parsed.hostname):
        raise ConfigError(
            f"{name} is plain http to {parsed.hostname!r}, which is neither "
            "loopback nor a Tailscale address. That would send "
            f"{INVESTMENT_API_KEY_ENV} over the network in clear text; use https, "
            "or the tailnet address the admin instance is bound to."
        )
    return value


@dataclass(frozen=True)
class InvestmentConfig:
    """Where the AI Server operational reads live, and the key to read them with.

    Both settings or neither. Half-configured is a startup failure rather than a
    tool that is advertised and always refuses: the operational reads are the
    only part of this boundary that can be *absent*, and "absent" has to mean one
    thing.
    """

    api_url: str
    api_key: str

    @classmethod
    def from_env(cls) -> "InvestmentConfig | None":
        """The configured investment reads, or None when they are switched off.

        None is the disabled state task 138 asks for ("the integration can be
        disabled without affecting Vikunja or AI Alpha Engine"). It is honoured by
        **not advertising** the operational tools at all — a tool that cannot be
        performed should not appear in ``tools/list``, because a model reads an
        advertised tool as a capability and will report its failure as a fact
        about the system rather than about the configuration.
        """
        url = os.environ.get(INVESTMENT_API_URL_ENV, "").strip().rstrip("/")
        key = os.environ.get(INVESTMENT_API_KEY_ENV, "").strip()
        if not url and not key:
            return None
        if not url or not key:
            missing = INVESTMENT_API_URL_ENV if not url else INVESTMENT_API_KEY_ENV
            present = INVESTMENT_API_KEY_ENV if not url else INVESTMENT_API_URL_ENV
            raise ConfigError(
                f"{present} is set but {missing} is not. Set both to enable the "
                "operational reads, or neither to leave them switched off — "
                "half-configured would advertise reads that can never succeed."
            )
        _private_api_url(INVESTMENT_API_URL_ENV, url)
        return cls(api_url=url, api_key=key)


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

    def registerable_redirect_uri(self, value: str) -> bool:
        """May a client register this redirect URI?

        Two ways in, and the difference between them is the point. A URI named
        in configuration is matched exactly, because the operator wrote it down.
        ChatGPT's per-connector callback cannot be written down in advance — the
        identifier does not exist until the connector is created — so it is
        recognised by shape instead, once, here.

        This is the only gate that is not exact equality, and it guards the only
        moment at which a redirect URI can enter the system. After it, the
        submitted string is stored verbatim and every later check compares
        against that.
        """
        return value in self.redirect_uris or is_chatgpt_connector_redirect_uri(value)

    @property
    def registerable_redirect_uri_summary(self) -> str:
        """What to tell a client that just tried to register something else."""
        return ", ".join(
            (
                *self.redirect_uris,
                f"https://{CHATGPT_CONNECTOR_HOST}{CHATGPT_CONNECTOR_PATH}/"
                "<connector-id>",
            )
        )

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
    #: None means the operational reads are switched off, and their tools are not
    #: advertised. See :meth:`InvestmentConfig.from_env`.
    investment: InvestmentConfig | None = None

    @property
    def operational_reads_enabled(self) -> bool:
        return self.investment is not None

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
            investment=InvestmentConfig.from_env(),
        )
