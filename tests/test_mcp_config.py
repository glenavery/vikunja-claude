"""The MCP credential is required, and there is no way to configure it away.

An integration whose authentication is optional is unauthenticated on the day
someone's `.env` loses a line — which is exactly the day nobody is looking.
"""

from __future__ import annotations

import unittest
from unittest import mock

from vikunja_claude.config import Config, ConfigError, McpConfig

from .support import MCP_TOKEN, TOKEN

BASE_ENV = {"VIKUNJA_API_TOKEN": TOKEN, "VIKUNJA_MCP_TOKEN": MCP_TOKEN}


def env(**overrides) -> dict[str, str]:
    values = dict(BASE_ENV)
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


class TestTheCredentialIsRequired(unittest.TestCase):
    def from_env(self, **overrides) -> McpConfig:
        with mock.patch.dict("os.environ", env(**overrides), clear=True):
            return McpConfig.from_env(env_file=None)

    def test_a_missing_token_refuses_to_start(self):
        with self.assertRaises(ConfigError) as caught:
            self.from_env(VIKUNJA_MCP_TOKEN=None)
        self.assertIn("no unauthenticated mode", str(caught.exception))

    def test_a_blank_token_refuses_to_start(self):
        with self.assertRaises(ConfigError):
            self.from_env(VIKUNJA_MCP_TOKEN="   ")

    def test_a_short_token_refuses_to_start(self):
        with self.assertRaises(ConfigError) as caught:
            self.from_env(VIKUNJA_MCP_TOKEN="hunter2")
        self.assertIn("at least 32", str(caught.exception))

    def test_no_environment_variable_turns_authentication_off(self):
        """There is no VIKUNJA_MCP_ALLOW_ANONYMOUS, and adding one is the bug."""
        for name in (
            "VIKUNJA_MCP_ALLOW_ANONYMOUS",
            "VIKUNJA_MCP_NO_AUTH",
            "ALLOW_DEV_AUTH",
            "ENVIRONMENT",
        ):
            with self.subTest(name=name):
                with self.assertRaises(ConfigError):
                    self.from_env(VIKUNJA_MCP_TOKEN=None, **{name: "true"})

    def test_a_good_token_is_accepted(self):
        config = self.from_env()
        self.assertEqual(config.mcp_token, MCP_TOKEN)


class TestDefaults(unittest.TestCase):
    def from_env(self, **overrides) -> McpConfig:
        with mock.patch.dict("os.environ", env(**overrides), clear=True):
            return McpConfig.from_env(env_file=None)

    def test_it_defaults_to_loopback_and_its_own_port(self):
        config = self.from_env()
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 3461)

    def test_its_port_is_not_the_launchers(self):
        """Two units, two ports: stopping one must not stop the other."""
        with mock.patch.dict("os.environ", env(), clear=True):
            self.assertNotEqual(
                McpConfig.from_env(env_file=None).port,
                Config.from_env(env_file=None).port,
            )

    def test_it_writes_its_ledger_beside_the_launcher_state(self):
        config = self.from_env()
        self.assertEqual(config.ledger_path.name, "mcp_created_tasks.jsonl")
        self.assertEqual(config.ledger_path.parent, config.state_dir)

    def test_both_services_mean_the_same_board(self):
        with mock.patch.dict("os.environ", env(), clear=True):
            mcp = McpConfig.from_env(env_file=None)
            launcher = Config.from_env(env_file=None)
        self.assertEqual(mcp.project_title, launcher.project_title)
        self.assertEqual(mcp.api_url, launcher.api_url)
        self.assertEqual(mcp.frontend_url, launcher.frontend_url)

    def test_the_project_can_be_pinned_by_id(self):
        self.assertEqual(self.from_env(VIKUNJA_PROJECT_ID="2").project_id, 2)


class TestTheLauncherDoesNotNeedTheMcpToken(unittest.TestCase):
    def test_the_launcher_starts_without_it(self):
        """Neither service may be able to stop the other from booting."""
        with mock.patch.dict(
            "os.environ", {"VIKUNJA_API_TOKEN": TOKEN}, clear=True
        ):
            self.assertEqual(Config.from_env(env_file=None).token, TOKEN)

    def test_the_mcp_server_still_needs_the_vikunja_token(self):
        with mock.patch.dict(
            "os.environ", {"VIKUNJA_MCP_TOKEN": MCP_TOKEN}, clear=True
        ):
            with self.assertRaises(ConfigError) as caught:
                McpConfig.from_env(env_file=None)
        self.assertIn("VIKUNJA_API_TOKEN", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
