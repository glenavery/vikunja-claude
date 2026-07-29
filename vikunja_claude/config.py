"""Runtime configuration, resolved from the environment.

Every setting has exactly one name and one meaning. An optional ``.env`` file
next to the package is read first so manual runs work without exporting
anything; real environment variables always win.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PACKAGE_ROOT / ".env"

# Shared by both services, so they cannot drift apart: the launcher and the MCP
# server must talk to the same Vikunja and mean the same project by "project".
DEFAULT_API_URL = "http://127.0.0.1:3456/api/v1"
DEFAULT_FRONTEND_URL = "http://127.0.0.1:3456"
DEFAULT_PROJECT = "AI Alpha Engine"


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


@dataclass(frozen=True)
class McpConfig:
    """Settings for the MCP integration boundary.

    Deliberately a separate object from :class:`Config` rather than more fields
    on it. The launcher must start without an MCP token, the MCP server must
    refuse to start without one, and neither should be able to make the other
    fail to boot. They share the Vikunja settings because there is only one
    Vikunja and one board — not because the two services are one thing.
    """

    api_url: str
    token: str
    mcp_token: str
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

    @classmethod
    def from_env(cls, env_file: Path | None = DEFAULT_ENV_FILE) -> "McpConfig":
        if env_file is not None:
            load_env_file(env_file)

        mcp_token = os.environ.get("VIKUNJA_MCP_TOKEN", "").strip()
        if not mcp_token:
            # There is no unauthenticated mode. An integration that fell back to
            # one when its credential went missing would be at its widest
            # exactly when it was least understood.
            raise ConfigError(
                "VIKUNJA_MCP_TOKEN is not set, and the MCP server has no "
                "unauthenticated mode. Generate one with "
                "`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` "
                f"and put it in {DEFAULT_ENV_FILE}."
            )
        if len(mcp_token) < 32:
            raise ConfigError(
                f"VIKUNJA_MCP_TOKEN is {len(mcp_token)} characters. This is the "
                "only thing standing between the public internet and a write "
                "path into the board; use at least 32."
            )

        return cls(
            api_url=os.environ.get("VIKUNJA_API_URL", DEFAULT_API_URL).rstrip("/"),
            token=_vikunja_token(),
            mcp_token=mcp_token,
            project_title=os.environ.get("VIKUNJA_PROJECT", DEFAULT_PROJECT),
            project_id=_project_id_override(),
            frontend_url=os.environ.get(
                "VIKUNJA_FRONTEND_URL", DEFAULT_FRONTEND_URL
            ).rstrip("/"),
            host=os.environ.get("VIKUNJA_MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("VIKUNJA_MCP_PORT", "3461")),
            state_dir=_state_dir(),
        )
