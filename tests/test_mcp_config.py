"""The OAuth configuration is required, and there is no way to configure it away.

An integration whose authentication is optional is unauthenticated on the day
someone's `.env` loses a line — which is exactly the day nobody is looking. So
everything the boundary needs is a startup failure when it is missing, and none
of it has a permissive default.
"""

from __future__ import annotations

import unittest
from unittest import mock

from vikunja_claude.config import (
    DEFAULT_REDIRECT_URIS,
    Config,
    ConfigError,
    McpConfig,
)

from .support import ISSUER, PASSPHRASE, REDIRECT_URI, TOKEN

BASE_ENV = {
    "VIKUNJA_API_TOKEN": TOKEN,
    "VIKUNJA_MCP_OAUTH_ISSUER": "https://aiserver.example.ts.net:8443",
    "VIKUNJA_MCP_OAUTH_PASSPHRASE": PASSPHRASE,
}


def env(**overrides) -> dict[str, str]:
    values = dict(BASE_ENV)
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


class ConfigTestCase(unittest.TestCase):
    def from_env(self, **overrides) -> McpConfig:
        with mock.patch.dict("os.environ", env(**overrides), clear=True):
            return McpConfig.from_env(env_file=None)


class TestTheOauthConfigurationIsRequired(ConfigTestCase):
    def test_a_missing_issuer_refuses_to_start(self):
        with self.assertRaises(ConfigError) as caught:
            self.from_env(VIKUNJA_MCP_OAUTH_ISSUER=None)
        self.assertIn("no unauthenticated mode", str(caught.exception))

    def test_a_missing_passphrase_refuses_to_start(self):
        with self.assertRaises(ConfigError) as caught:
            self.from_env(VIKUNJA_MCP_OAUTH_PASSPHRASE=None)
        self.assertIn("VIKUNJA_MCP_OAUTH_PASSPHRASE", str(caught.exception))

    def test_a_blank_passphrase_refuses_to_start(self):
        with self.assertRaises(ConfigError):
            self.from_env(VIKUNJA_MCP_OAUTH_PASSPHRASE="   ")

    def test_a_short_passphrase_refuses_to_start(self):
        with self.assertRaises(ConfigError) as caught:
            self.from_env(VIKUNJA_MCP_OAUTH_PASSPHRASE="hunter2")
        self.assertIn("at least 32", str(caught.exception))

    def test_a_public_issuer_must_be_https(self):
        with self.assertRaises(ConfigError) as caught:
            self.from_env(VIKUNJA_MCP_OAUTH_ISSUER="http://mcp.example.com")
        self.assertIn("must be https", str(caught.exception))

    def test_a_loopback_issuer_may_be_http(self):
        """Only because nothing leaves the host: it is how the flow is tested."""
        config = self.from_env(VIKUNJA_MCP_OAUTH_ISSUER=ISSUER)
        self.assertEqual(config.oauth.issuer, ISSUER)

    def test_an_issuer_that_is_not_a_url_refuses_to_start(self):
        for value in ("aiserver.example.ts.net", "https://x.example?a=1", "https://x#f"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    self.from_env(VIKUNJA_MCP_OAUTH_ISSUER=value)

    def test_a_client_secret_without_a_client_id_refuses_to_start(self):
        with self.assertRaises(ConfigError) as caught:
            self.from_env(VIKUNJA_MCP_OAUTH_CLIENT_SECRET="s" * 40)
        self.assertIn("VIKUNJA_MCP_OAUTH_CLIENT_ID", str(caught.exception))

    def test_no_environment_variable_turns_authentication_off(self):
        """There is no VIKUNJA_MCP_ALLOW_ANONYMOUS, and adding one is the bug."""
        for name in (
            "VIKUNJA_MCP_ALLOW_ANONYMOUS",
            "VIKUNJA_MCP_NO_AUTH",
            "VIKUNJA_MCP_OAUTH_OPTIONAL",
            "ALLOW_DEV_AUTH",
            "ENVIRONMENT",
        ):
            with self.subTest(name=name):
                with self.assertRaises(ConfigError):
                    self.from_env(VIKUNJA_MCP_OAUTH_ISSUER=None, **{name: "true"})

    def test_the_static_bearer_token_no_longer_starts_the_server(self):
        """The credential it replaced is not a fallback: it is not read at all."""
        with self.assertRaises(ConfigError):
            self.from_env(
                VIKUNJA_MCP_OAUTH_ISSUER=None,
                VIKUNJA_MCP_TOKEN="a-perfectly-long-static-token-value-here",
            )

    def test_a_complete_configuration_is_accepted(self):
        oauth = self.from_env().oauth
        self.assertEqual(oauth.passphrase, PASSPHRASE)
        self.assertEqual(oauth.resource, oauth.issuer + "/mcp")


class TestRedirectUris(ConfigTestCase):
    def test_it_defaults_to_the_chatgpt_callback(self):
        self.assertEqual(self.from_env().oauth.redirect_uris, DEFAULT_REDIRECT_URIS)
        self.assertIn(REDIRECT_URI, DEFAULT_REDIRECT_URIS)

    def test_they_can_be_named_explicitly(self):
        config = self.from_env(
            VIKUNJA_MCP_OAUTH_REDIRECT_URIS=f"{REDIRECT_URI} https://other.example/cb"
        )
        self.assertEqual(
            config.oauth.redirect_uris, (REDIRECT_URI, "https://other.example/cb")
        )

    def test_a_redirect_uri_over_plain_http_is_refused(self):
        with self.assertRaises(ConfigError):
            self.from_env(VIKUNJA_MCP_OAUTH_REDIRECT_URIS="http://evil.example/cb")


class TestDefaults(ConfigTestCase):
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

    def test_the_oauth_state_lives_beside_the_ledger(self):
        config = self.from_env()
        self.assertEqual(config.oauth_state_path.name, "mcp_oauth.json")
        self.assertEqual(config.oauth_state_path.parent, config.state_dir)

    def test_both_services_mean_the_same_board(self):
        with mock.patch.dict("os.environ", env(), clear=True):
            mcp = McpConfig.from_env(env_file=None)
            launcher = Config.from_env(env_file=None)
        self.assertEqual(mcp.project_title, launcher.project_title)
        self.assertEqual(mcp.api_url, launcher.api_url)
        self.assertEqual(mcp.frontend_url, launcher.frontend_url)

    def test_the_default_board_is_the_first_approved_one(self):
        config = self.from_env()
        self.assertEqual(config.default_project.project_id, 2)
        self.assertEqual(config.project_title, "AI Alpha Engine")

    def test_the_approved_boards_can_be_named_by_id_and_title(self):
        config = self.from_env(VIKUNJA_MCP_PROJECTS="7:Ops, 8:Research")
        self.assertEqual(config.allowed_project_ids, (7, 8))
        self.assertEqual(config.project_title, "Ops")
        self.assertIsNone(config.project_for(2))


class TestTheLauncherDoesNotNeedTheOauthConfiguration(unittest.TestCase):
    def test_the_launcher_starts_without_it(self):
        """Neither service may be able to stop the other from booting."""
        with mock.patch.dict("os.environ", {"VIKUNJA_API_TOKEN": TOKEN}, clear=True):
            self.assertEqual(Config.from_env(env_file=None).token, TOKEN)

    def test_the_mcp_server_still_needs_the_vikunja_token(self):
        with mock.patch.dict("os.environ", env(VIKUNJA_API_TOKEN=None), clear=True):
            with self.assertRaises(ConfigError) as caught:
                McpConfig.from_env(env_file=None)
        self.assertIn("VIKUNJA_API_TOKEN", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
