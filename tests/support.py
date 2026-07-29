"""Shared fixtures for the test suite."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vikunja_claude.config import Config, McpConfig
from vikunja_claude.launcher import Launcher
from vikunja_claude.mcp import McpProtocol
from vikunja_claude.mcp_service import McpService
from vikunja_claude.service import TicketService
from vikunja_claude.vikunja import VikunjaClient

from .fakes import FakeVikunja, RecordingSpawn

TOKEN = "test-token-never-in-prompts"
MCP_TOKEN = "test-mcp-token-long-enough-to-be-plausible"


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
    defaults = dict(
        api_url="http://127.0.0.1:3456/api/v1",
        token=TOKEN,
        mcp_token=MCP_TOKEN,
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
