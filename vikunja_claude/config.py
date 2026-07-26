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

        token = os.environ.get("VIKUNJA_API_TOKEN", "").strip()
        if not token:
            raise ConfigError(
                "VIKUNJA_API_TOKEN is not set. Put it in "
                f"{DEFAULT_ENV_FILE} or export it before starting the service."
            )

        state_dir = Path(
            os.environ.get(
                "VIKUNJA_CLAUDE_STATE_DIR",
                Path.home() / ".local" / "state" / "vikunja-claude",
            )
        )
        raw_project_id = os.environ.get("VIKUNJA_PROJECT_ID", "").strip()

        return cls(
            api_url=os.environ.get(
                "VIKUNJA_API_URL", "http://127.0.0.1:3456/api/v1"
            ).rstrip("/"),
            token=token,
            project_title=os.environ.get("VIKUNJA_PROJECT", "AI Alpha Engine"),
            project_id=int(raw_project_id) if raw_project_id else None,
            workdir=Path(
                os.environ.get("CLAUDE_WORKDIR", "/home/glen/stacks/investment")
            ),
            claude_bin=os.environ.get("CLAUDE_BIN", "claude"),
            claude_args=shlex.split(
                os.environ.get("CLAUDE_ARGS", "-p --permission-mode acceptEdits")
            ),
            host=os.environ.get("VIKUNJA_CLAUDE_HOST", "127.0.0.1"),
            port=int(os.environ.get("VIKUNJA_CLAUDE_PORT", "3460")),
            state_dir=state_dir,
            frontend_url=os.environ.get(
                "VIKUNJA_FRONTEND_URL", "http://127.0.0.1:3456"
            ).rstrip("/"),
            launch_timeout_seconds=int(
                os.environ.get("CLAUDE_LAUNCH_TIMEOUT_SECONDS", "10800")
            ),
        )
