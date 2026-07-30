"""The OAuth boundary, driven end to end over a socket.

The claim these tests exist to hold up is narrow and total: **the only way to
reach `/mcp` is an access token this server issued through the authorization
code flow, with PKCE, approved by the operator**. Everything else — a missing
token, an expired one, one issued to another resource, one obtained with a
replayed code, one obtained without the passphrase — gets the same nothing.

They drive the real routes rather than the authorization server's methods,
because a rule enforced in a method that no route calls is not enforced.
"""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path

from vikunja_claude.oauth import MAX_FAILED_ATTEMPTS, SCOPE, AuthorizationServer
from vikunja_claude.oauth_store import OAuthStore

from .fakes import PROJECT_ID
from .support import (
    CONNECTOR_REDIRECT_URI,
    PASSPHRASE,
    REDIRECT_URI,
    HttpTestCase,
    make_mcp_config,
)


class TestMetadata(HttpTestCase):
    """Discovery: the documents a client reads before it can do anything."""

    def metadata(self, path: str) -> dict:
        status, _, text = self.open(path, token=None)
        self.assertEqual(status, 200, text)
        return json.loads(text)

    def test_the_protected_resource_names_this_server_and_its_issuer(self):
        document = self.metadata("/.well-known/oauth-protected-resource")
        self.assertEqual(document["resource"], self.config.oauth.resource)
        self.assertEqual(
            document["authorization_servers"], [self.config.oauth.issuer]
        )
        self.assertEqual(document["scopes_supported"], [SCOPE])

    def test_it_is_also_served_under_the_resource_path(self):
        """A client that appends the resource path must find the same document."""
        self.assertEqual(
            self.metadata("/.well-known/oauth-protected-resource"),
            self.metadata("/.well-known/oauth-protected-resource/mcp"),
        )

    def test_the_authorization_server_advertises_only_what_exists(self):
        document = self.metadata("/.well-known/oauth-authorization-server")
        self.assertEqual(document["issuer"], self.config.oauth.issuer)
        self.assertEqual(
            document["authorization_endpoint"], self.config.oauth.authorization_endpoint
        )
        self.assertEqual(document["token_endpoint"], self.config.oauth.token_endpoint)
        self.assertEqual(document["code_challenge_methods_supported"], ["S256"])
        self.assertEqual(document["response_types_supported"], ["code"])
        self.assertEqual(
            set(document["grant_types_supported"]),
            {"authorization_code", "refresh_token"},
        )

    def test_no_implicit_and_no_password_grant_is_advertised(self):
        document = self.metadata("/.well-known/oauth-authorization-server")
        advertised = set(document["grant_types_supported"]) | set(
            document["response_types_supported"]
        )
        for forbidden in ("password", "implicit", "token", "client_credentials"):
            self.assertNotIn(forbidden, advertised)

    def test_metadata_carries_no_credential(self):
        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-authorization-server",
        ):
            _, _, text = self.open(path, token=None)
            self.assertNotIn(PASSPHRASE, text)
            self.assertNotIn(self.config.token, text)


class TestClientRegistration(HttpTestCase):
    def test_a_client_can_register_itself(self):
        client = self.register_client()
        self.assertEqual(client["status"], 201)
        self.assertTrue(client["client_id"])
        self.assertEqual(client["redirect_uris"], [REDIRECT_URI])

    def test_a_public_client_is_issued_no_secret(self):
        """PKCE is the authentication; a secret ChatGPT cannot keep is theatre."""
        self.assertNotIn("client_secret", self.register_client())

    def test_an_unlisted_redirect_uri_cannot_be_registered(self):
        client = self.register_client(redirect_uris=["https://evil.example/callback"])
        self.assertEqual(client["status"], 400)
        self.assertEqual(client["error"], "invalid_redirect_uri")

    def test_a_near_miss_redirect_uri_cannot_be_registered(self):
        """Exact match: not a prefix, not the same host, not a longer path."""
        for uri in (
            REDIRECT_URI + "/",
            REDIRECT_URI + "?x=1",
            REDIRECT_URI.replace("https", "http"),
            "https://chatgpt.com/connector_platform_oauth_redirect_evil",
            "https://chatgpt.com.evil.example/connector_platform_oauth_redirect",
        ):
            with self.subTest(uri=uri):
                self.assertEqual(self.register_client(redirect_uris=[uri])["status"], 400)

    def test_registration_without_redirect_uris_is_refused(self):
        self.assertEqual(self.register_client(redirect_uris=[])["status"], 400)


class TestTheChatGptConnectorCallback(HttpTestCase):
    """ChatGPT's per-connector callback: admitted by shape, then pinned by value.

    The connector's identifier is minted when the connector is created, so the
    address cannot be named in configuration ahead of time — which is exactly
    how the live dialog failed. Registration therefore recognises the *shape*.

    The whole point of these tests is that the allowance stops there. What the
    client registered is stored complete, and from that moment the only question
    ever asked about a redirect URI is whether it is that string — so a second
    connector's callback, which has an identically valid shape, is refused for
    the first connector's client.
    """

    def register_with(self, uri: str) -> dict:
        return self.register_client(redirect_uris=[uri])

    def assertRegisters(self, uri: str) -> str:
        client = self.register_with(uri)
        self.assertEqual(client["status"], 201, client)
        self.assertEqual(client["redirect_uris"], [uri])
        return client["client_id"]

    def assertRefused(self, uri: str) -> None:
        client = self.register_with(uri)
        self.assertEqual(client["status"], 400, client)
        self.assertEqual(client["error"], "invalid_redirect_uri")

    # -- what registers ----------------------------------------------------

    def test_the_current_per_connector_callback_registers(self):
        self.assertRegisters(CONNECTOR_REDIRECT_URI)

    def test_the_complete_submitted_uri_is_what_is_stored(self):
        """Not a prefix and not a normalised form: the string, as submitted."""
        uri = "https://chatgpt.com/connector/oauth/A-different_one.~2"
        client = self.register_with(uri)
        self.assertEqual(client["redirect_uris"], [uri])

    def test_the_legacy_fixed_callback_still_registers(self):
        """Compatibility is retained: the old dialog is not broken by the new one."""
        self.assertRegisters(REDIRECT_URI)

    def test_both_forms_register_together_and_one_bad_one_refuses_all(self):
        both = [REDIRECT_URI, CONNECTOR_REDIRECT_URI]
        client = self.register_client(redirect_uris=both)
        self.assertEqual(client["status"], 201, client)
        self.assertEqual(client["redirect_uris"], both)
        # And the list is judged whole: an admitted URI does not carry a
        # refused one in beside it.
        self.assertEqual(
            self.register_client(
                redirect_uris=[CONNECTOR_REDIRECT_URI, "https://evil.example/cb"]
            )["status"],
            400,
        )

    # -- and completes a real authorization ---------------------------------

    def test_the_registered_connector_uri_completes_authorization(self):
        client_id = self.assertRegisters(CONNECTOR_REDIRECT_URI)
        verifier, challenge = self.pkce()
        params = self.authorize_params(
            client_id, challenge, redirect_uri=CONNECTOR_REDIRECT_URI
        )
        status, headers, _ = self.approve(params)
        self.assertEqual(status, 302)
        self.assertTrue(headers["Location"].startswith(CONNECTOR_REDIRECT_URI + "?"))

        code = self.redirect_query(headers)["code"]
        status, payload = self.exchange(
            code, verifier, client_id, redirect_uri=CONNECTOR_REDIRECT_URI
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.rpc("tools/list", token=payload["access_token"])[0], 200)

    # -- the shape does not survive registration ----------------------------

    def test_another_connectors_callback_is_refused_after_registration(self):
        """The decisive one: same shape, registerable in its own right, not this client's.

        If the registration rule were re-applied at ``/oauth/authorize`` this
        would be accepted, and a code for this client would be sent to an
        address it never registered.
        """
        client_id = self.assertRegisters(CONNECTOR_REDIRECT_URI)
        other = "https://chatgpt.com/connector/oauth/someOtherConnector"
        self.assertRegisters(other)  # so it is not the shape that is refusing

        _, challenge = self.pkce()
        status, headers, page = self.open(
            "/oauth/authorize?"
            + urllib.parse.urlencode(
                self.authorize_params(client_id, challenge, redirect_uri=other)
            ),
            token=None,
        )
        self.assertEqual(status, 400)
        self.assertNotIn("Location", headers)
        self.assertIn("redirect_uri", page)

    def test_the_legacy_client_cannot_authorize_to_a_connector_callback(self):
        client_id = self.assertRegisters(REDIRECT_URI)
        _, challenge = self.pkce()
        status, headers, _ = self.open(
            "/oauth/authorize?"
            + urllib.parse.urlencode(
                self.authorize_params(
                    client_id, challenge, redirect_uri=CONNECTOR_REDIRECT_URI
                )
            ),
            token=None,
        )
        self.assertEqual(status, 400)
        self.assertNotIn("Location", headers)

    def test_the_token_endpoint_still_pins_the_authorized_uri(self):
        client_id = self.assertRegisters(CONNECTOR_REDIRECT_URI)
        verifier, challenge = self.pkce()
        params = self.authorize_params(
            client_id, challenge, redirect_uri=CONNECTOR_REDIRECT_URI
        )
        _, headers, _ = self.approve(params)
        code = self.redirect_query(headers)["code"]
        status, payload = self.exchange(
            code,
            verifier,
            client_id,
            redirect_uri="https://chatgpt.com/connector/oauth/somethingElse",
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_grant")

    # -- what the shape does not admit --------------------------------------

    def test_an_empty_identifier_is_refused(self):
        for uri in (
            "https://chatgpt.com/connector/oauth/",
            "https://chatgpt.com/connector/oauth",
            "https://chatgpt.com/connector/oauth//",
        ):
            with self.subTest(uri=uri):
                self.assertRefused(uri)

    def test_extra_path_segments_are_refused(self):
        for uri in (
            CONNECTOR_REDIRECT_URI + "/",
            CONNECTOR_REDIRECT_URI + "/more",
            CONNECTOR_REDIRECT_URI + "/../evil",
            "https://chatgpt.com/connector/oauth/a/b",
        ):
            with self.subTest(uri=uri):
                self.assertRefused(uri)

    def test_a_dot_segment_is_not_an_identifier(self):
        """Unreserved characters, but they normalise to a different path."""
        for uri in (
            "https://chatgpt.com/connector/oauth/.",
            "https://chatgpt.com/connector/oauth/..",
        ):
            with self.subTest(uri=uri):
                self.assertRefused(uri)

    def test_a_query_string_or_fragment_is_refused(self):
        for uri in (
            CONNECTOR_REDIRECT_URI + "?x=1",
            CONNECTOR_REDIRECT_URI + "?",
            CONNECTOR_REDIRECT_URI + "#frag",
            CONNECTOR_REDIRECT_URI + "#",
            CONNECTOR_REDIRECT_URI + "?next=https://evil.example",
        ):
            with self.subTest(uri=uri):
                self.assertRefused(uri)

    def test_percent_encoded_path_separators_are_refused(self):
        """`%2F` is one segment here and a separator to whatever decodes it next."""
        for uri in (
            "https://chatgpt.com/connector/oauth/a%2Fb",
            "https://chatgpt.com/connector/oauth/..%2F..%2Fevil",
            "https://chatgpt.com/connector/oauth/a%252Fb",
            "https://chatgpt.com/connector%2Foauth/abc",
            "https://chatgpt.com%2Fconnector/oauth/abc",
            "https://chatgpt.com/connector/oauth/%2E%2E",
        ):
            with self.subTest(uri=uri):
                self.assertRefused(uri)

    def test_another_host_is_refused(self):
        for uri in (
            "https://evil.chatgpt.com/connector/oauth/abc",
            "https://chatgpt.com.evil.example/connector/oauth/abc",
            "https://chatgpt.co/connector/oauth/abc",
            "https://chatgpt.example/connector/oauth/abc",
            "https://xn--chatgpt-1234.com/connector/oauth/abc",
            "https://evil.example/connector/oauth/abc",
        ):
            with self.subTest(uri=uri):
                self.assertRefused(uri)

    def test_userinfo_cannot_smuggle_another_host_past_the_check(self):
        for uri in (
            "https://chatgpt.com@evil.example/connector/oauth/abc",
            "https://user@chatgpt.com/connector/oauth/abc",
            "https://user:pass@chatgpt.com/connector/oauth/abc",
        ):
            with self.subTest(uri=uri):
                self.assertRefused(uri)

    def test_a_port_is_refused(self):
        for uri in (
            "https://chatgpt.com:8443/connector/oauth/abc",
            "https://chatgpt.com:443/connector/oauth/abc",
        ):
            with self.subTest(uri=uri):
                self.assertRefused(uri)

    def test_plain_http_is_refused(self):
        self.assertRefused("http://chatgpt.com/connector/oauth/abc")

    def test_an_arbitrary_chatgpt_path_is_refused(self):
        """The allowance is one path, not the host."""
        for uri in (
            "https://chatgpt.com/",
            "https://chatgpt.com/connector/oauth2/abc",
            "https://chatgpt.com/connector/oauthx/abc",
            "https://chatgpt.com/oauth/abc",
            "https://chatgpt.com/backend-api/abc",
            "https://chatgpt.com/CONNECTOR/OAUTH/abc",
        ):
            with self.subTest(uri=uri):
                self.assertRefused(uri)

    def test_a_registration_body_that_is_not_a_list_of_strings_is_refused(self):
        for value in ([None], [{"uri": CONNECTOR_REDIRECT_URI}], [123]):
            with self.subTest(value=value):
                self.assertEqual(
                    self.register_client(redirect_uris=value)["status"], 400
                )


class TestTheAuthorizationRequest(HttpTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client_id = self.register_client()["client_id"]
        self.verifier, self.challenge = self.pkce()

    def get_authorize(self, **overrides):
        client_id = overrides.pop("client_id", self.client_id)
        params = self.authorize_params(client_id, self.challenge, **overrides)
        return self.open(
            "/oauth/authorize?" + urllib.parse.urlencode(params), token=None
        )

    def test_the_consent_screen_states_exactly_what_is_granted(self):
        status, _, page = self.get_authorize()
        self.assertEqual(status, 200)
        self.assertIn("read", page)
        self.assertIn("create", page)
        self.assertIn("AI Alpha Engine", page)
        self.assertIn("cannot edit, close, delete, comment on or move", page)

    def test_the_consent_screen_asks_for_the_passphrase_and_never_shows_it(self):
        _, _, page = self.get_authorize()
        self.assertIn('name="passphrase"', page)
        self.assertNotIn(PASSPHRASE, page)

    def test_pkce_is_required(self):
        status, headers, _ = self.get_authorize(code_challenge=None)
        self.assertEqual(status, 302)
        query = self.redirect_query(headers)
        self.assertEqual(query["error"], "invalid_request")
        self.assertIn("PKCE", query["error_description"])
        self.assertNotIn("code", query)

    def test_plain_pkce_is_refused(self):
        _, headers, _ = self.get_authorize(code_challenge_method="plain")
        self.assertEqual(self.redirect_query(headers)["error"], "invalid_request")

    def test_the_implicit_flow_is_refused(self):
        _, headers, _ = self.get_authorize(response_type="token")
        self.assertEqual(
            self.redirect_query(headers)["error"], "unsupported_response_type"
        )

    def test_an_unknown_client_is_never_redirected_anywhere(self):
        """The only address on offer is the one the bad request supplied."""
        status, headers, _ = self.get_authorize(client_id="cid_invented")
        self.assertEqual(status, 400)
        self.assertNotIn("Location", headers)

    def test_a_redirect_uri_that_was_not_registered_is_not_redirected_to(self):
        status, headers, page = self.get_authorize(
            redirect_uri="https://evil.example/callback"
        )
        self.assertEqual(status, 400)
        self.assertNotIn("Location", headers)
        self.assertIn("redirect_uri", page)

    def test_a_token_is_never_issued_for_another_resource(self):
        _, headers, _ = self.get_authorize(resource="https://someone-else.example/mcp")
        self.assertEqual(self.redirect_query(headers)["error"], "invalid_target")

    def test_an_unknown_scope_is_refused(self):
        _, headers, _ = self.get_authorize(scope="vikunja:admin")
        self.assertEqual(self.redirect_query(headers)["error"], "invalid_scope")

    def test_the_state_survives_the_round_trip(self):
        params = self.authorize_params(
            self.client_id, self.challenge, state="the-clients-state"
        )
        _, headers, _ = self.approve(params)
        self.assertEqual(self.redirect_query(headers)["state"], "the-clients-state")

    def test_the_issuer_is_named_in_the_response(self):
        """RFC 9207: the client checks this to detect a mix-up attack."""
        params = self.authorize_params(self.client_id, self.challenge)
        _, headers, _ = self.approve(params)
        self.assertEqual(
            self.redirect_query(headers)["iss"], self.config.oauth.issuer
        )


class TestTheOperatorMustApprove(HttpTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client_id = self.register_client()["client_id"]
        self.verifier, self.challenge = self.pkce()
        self.params = self.authorize_params(self.client_id, self.challenge)

    def test_a_wrong_passphrase_issues_no_code(self):
        status, headers, _ = self.approve(self.params, passphrase="wrong")
        self.assertEqual(status, 401)
        self.assertNotIn("Location", headers)

    def test_a_missing_passphrase_issues_no_code(self):
        status, headers, _ = self.open(
            "/oauth/authorize", form=self.params, token=None
        )
        self.assertEqual(status, 401)
        self.assertNotIn("Location", headers)

    def test_repeated_guessing_is_locked_out(self):
        for _ in range(MAX_FAILED_ATTEMPTS):
            self.approve(self.params, passphrase="wrong")
        status, headers, page = self.approve(self.params, passphrase="wrong")
        self.assertEqual(status, 429)
        self.assertIn("Too many failed attempts", page)
        # And the lockout is not a formality that the right passphrase walks past.
        status, headers, _ = self.approve(self.params)
        self.assertEqual(status, 429)
        self.assertNotIn("Location", headers)

    def test_the_right_passphrase_issues_a_code(self):
        status, headers, _ = self.approve(self.params)
        self.assertEqual(status, 302)
        self.assertTrue(self.redirect_query(headers)["code"])
        self.assertTrue(headers["Location"].startswith(REDIRECT_URI))


class TestTheTokenEndpoint(HttpTestCase):
    def test_the_verifier_must_match_the_challenge(self):
        code, _, client_id = self.obtain_code()
        other_verifier, _ = self.pkce()
        status, payload = self.exchange(code, other_verifier, client_id)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_grant")

    def test_a_failed_verifier_spends_the_code(self):
        """One attempt, not a guessing game: the code is claimed before it is checked."""
        code, verifier, client_id = self.obtain_code()
        other_verifier, _ = self.pkce()
        self.exchange(code, other_verifier, client_id)
        status, payload = self.exchange(code, verifier, client_id)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_grant")

    def test_a_missing_verifier_is_refused(self):
        code, _, client_id = self.obtain_code()
        status, payload = self.exchange(code, "", client_id, code_verifier=None)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_grant")

    def test_a_code_cannot_be_used_twice(self):
        code, verifier, client_id = self.obtain_code()
        first_status, _ = self.exchange(code, verifier, client_id)
        self.assertEqual(first_status, 200)
        second_status, second = self.exchange(code, verifier, client_id)
        self.assertEqual(second_status, 400)
        self.assertEqual(second["error"], "invalid_grant")

    def test_replaying_a_code_revokes_what_the_first_use_issued(self):
        """A replayed code means it leaked, and the tokens are what leaked for."""
        code, verifier, client_id = self.obtain_code()
        _, first = self.exchange(code, verifier, client_id)
        self.exchange(code, verifier, client_id)
        status, _, _ = self.rpc("tools/list", token=first["access_token"])
        self.assertEqual(status, 401)

    def test_a_code_cannot_be_redeemed_by_another_client(self):
        code, verifier, _ = self.obtain_code()
        other = self.register_client(client_name="another client")["client_id"]
        status, payload = self.exchange(code, verifier, other)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_grant")

    def test_a_changed_redirect_uri_is_refused_at_the_token_endpoint(self):
        code, verifier, client_id = self.obtain_code()
        status, payload = self.exchange(
            code, verifier, client_id, redirect_uri="https://evil.example/callback"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_grant")

    def test_an_unknown_code_is_refused(self):
        client_id = self.register_client()["client_id"]
        status, payload = self.exchange("not-a-code", "not-a-verifier", client_id)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_grant")

    def test_there_is_no_password_grant(self):
        client_id = self.register_client()["client_id"]
        status, _, text = self.open(
            "/oauth/token",
            form={
                "grant_type": "password",
                "username": "glen",
                "password": PASSPHRASE,
                "client_id": client_id,
            },
            token=None,
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(text)["error"], "unsupported_grant_type")

    def test_there_is_no_client_credentials_grant(self):
        """No grant may skip the consent screen: approval is a person, not a key."""
        client_id = self.register_client()["client_id"]
        status, _, text = self.open(
            "/oauth/token",
            form={"grant_type": "client_credentials", "client_id": client_id},
            token=None,
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(text)["error"], "unsupported_grant_type")

    def test_the_token_response_is_a_bearer_token_with_the_one_scope(self):
        payload = self.issue_tokens()
        self.assertEqual(payload["token_type"], "Bearer")
        self.assertEqual(payload["scope"], SCOPE)
        self.assertGreater(payload["expires_in"], 0)
        self.assertTrue(payload["refresh_token"])


class TestRefresh(HttpTestCase):
    def refresh(self, token: str, client_id: str):
        status, _, text = self.open(
            "/oauth/token",
            form={
                "grant_type": "refresh_token",
                "refresh_token": token,
                "client_id": client_id,
            },
            token=None,
        )
        return status, json.loads(text)

    def test_a_refresh_token_buys_a_working_access_token(self):
        code, verifier, client_id = self.obtain_code()
        _, first = self.exchange(code, verifier, client_id)
        status, second = self.refresh(first["refresh_token"], client_id)
        self.assertEqual(status, 200)
        self.assertNotEqual(second["access_token"], first["access_token"])
        self.assertEqual(self.rpc("tools/list", token=second["access_token"])[0], 200)

    def test_a_used_refresh_token_is_rotated_away(self):
        code, verifier, client_id = self.obtain_code()
        _, first = self.exchange(code, verifier, client_id)
        self.refresh(first["refresh_token"], client_id)
        status, payload = self.refresh(first["refresh_token"], client_id)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_grant")

    def test_another_client_cannot_refresh_it(self):
        code, verifier, client_id = self.obtain_code()
        _, first = self.exchange(code, verifier, client_id)
        other = self.register_client(client_name="another client")["client_id"]
        status, payload = self.refresh(first["refresh_token"], other)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_grant")


class TestExpiry(HttpTestCase):
    # Issued already expired, which is the same code path a token that has been
    # alive for an hour takes — the clock is the only difference.
    oauth_overrides = {"access_token_ttl_seconds": 0}

    def test_an_expired_access_token_is_refused(self):
        token = self.issue_tokens()["access_token"]
        status, _, _ = self.rpc("tools/list", token=token)
        self.assertEqual(status, 401)

    def test_an_expired_token_reaches_nothing(self):
        self.rpc(
            "tools/call",
            {"name": "get_task", "arguments": {"task_id": 9}},
            token=self.issue_tokens()["access_token"],
        )
        self.assertEqual(self.vikunja.calls, [])


class TestExpiredCodes(HttpTestCase):
    oauth_overrides = {"code_ttl_seconds": 0}

    def test_an_expired_authorization_code_is_refused(self):
        code, verifier, client_id = self.obtain_code()
        status, payload = self.exchange(code, verifier, client_id)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_grant")


class TestTokensExpireWithTime(unittest.TestCase):
    """Time alone must invalidate a token: no request, no write, no restart.

    Driven against the authorization server with a clock the test controls,
    because over the socket the store rewrites itself on every issuance and
    prunes what has expired — which would let the resource server's own expiry
    check be deleted with the suite still green.
    """

    VERIFIER = "a-verifier-of-entirely-adequate-length-for-pkce"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.clock = 1_000_000.0
        self.config = make_mcp_config(Path(tmp.name))
        store = OAuthStore(self.config.oauth_state_path, now=lambda: self.clock)
        self.server = AuthorizationServer(self.config, store, now=lambda: self.clock)
        self.client_id = self.register()["client_id"]
        self.tokens = self.exchange(self.authorize())

    # -- driving the flow without a socket ---------------------------------

    def register(self) -> dict:
        return json.loads(
            self.server.register(
                json.dumps({"client_name": "clock", "redirect_uris": [REDIRECT_URI]})
            ).body
        )

    def authorize(self) -> str:
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(self.VERIFIER.encode()).digest())
            .decode()
            .rstrip("=")
        )
        approved = self.server.approve(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": REDIRECT_URI,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "passphrase": PASSPHRASE,
            }
        )
        assert approved.status == 302, approved.body
        return urllib.parse.parse_qs(
            urllib.parse.urlsplit(approved.headers["Location"]).query
        )["code"][0]

    def post_token(self, **form):
        return self.server.token(urllib.parse.urlencode(form))

    def exchange(self, code: str) -> dict:
        response = self.post_token(
            grant_type="authorization_code",
            code=code,
            redirect_uri=REDIRECT_URI,
            client_id=self.client_id,
            code_verifier=self.VERIFIER,
        )
        assert response.status == 200, response.body
        return json.loads(response.body)

    def bearer(self, token: str):
        return self.server.authenticate(f"Bearer {token}")

    # -- what the clock does -----------------------------------------------

    def test_the_access_token_works_until_it_does_not(self):
        self.assertTrue(self.bearer(self.tokens["access_token"]).ok)
        self.clock += self.config.oauth.access_token_ttl_seconds + 1
        check = self.bearer(self.tokens["access_token"])
        self.assertFalse(check.ok)
        self.assertEqual(check.error, "invalid_token")

    def test_the_refresh_token_outlives_the_access_token(self):
        """Two lifetimes, not one: that is what makes the short one affordable."""
        self.clock += self.config.oauth.access_token_ttl_seconds + 1
        self.assertFalse(self.bearer(self.tokens["access_token"]).ok)
        response = self.post_token(
            grant_type="refresh_token",
            refresh_token=self.tokens["refresh_token"],
            client_id=self.client_id,
        )
        self.assertEqual(response.status, 200, response.body)
        self.assertTrue(self.bearer(json.loads(response.body)["access_token"]).ok)

    def test_a_refresh_token_does_not_last_forever_either(self):
        self.clock += self.config.oauth.refresh_token_ttl_seconds + 1
        response = self.post_token(
            grant_type="refresh_token",
            refresh_token=self.tokens["refresh_token"],
            client_id=self.client_id,
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.body)["error"], "invalid_grant")

    def test_an_authorization_code_expires_where_it_sits(self):
        code = self.authorize()
        self.clock += self.config.oauth.code_ttl_seconds + 1
        response = self.post_token(
            grant_type="authorization_code",
            code=code,
            redirect_uri=REDIRECT_URI,
            client_id=self.client_id,
            code_verifier=self.VERIFIER,
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.body)["error"], "invalid_grant")


class TestRevocation(HttpTestCase):
    def test_deleting_the_state_file_revokes_every_live_token(self):
        """The documented revocation step, held to actually revoking."""
        token = self.access_token()
        self.assertEqual(self.rpc("tools/list", token=token)[0], 200)
        self.config.oauth_state_path.unlink()
        self.assertEqual(self.rpc("tools/list", token=token)[0], 401)

    def test_a_token_from_another_deployment_is_refused(self):
        """Audience binding: a token minted for another resource is not ours."""
        store = OAuthStore(self.config.oauth_state_path)
        store.put_token(
            "borrowed-token",
            {
                "type": "access",
                "client_id": "cid_elsewhere",
                "grant_id": "g",
                "scope": SCOPE,
                "resource": "https://another-server.example/mcp",
                "expires_at": time.time() + 3600,
            },
        )
        self.assertEqual(self.rpc("tools/list", token="borrowed-token")[0], 401)


class TestNoFallbackAuthentication(HttpTestCase):
    def test_the_pre_oauth_static_token_is_just_an_invalid_token(self):
        """Nothing here knows what VIKUNJA_MCP_TOKEN was, and that is the point."""
        for candidate in (
            "test-mcp-token-long-enough-to-be-plausible",
            self.config.token,
            PASSPHRASE,
        ):
            with self.subTest(candidate=candidate):
                self.assertEqual(self.rpc("tools/list", token=candidate)[0], 401)

    def test_no_header_grants_anonymous_access(self):
        for headers in (
            {"X-Api-Key": PASSPHRASE},
            {"X-Vikunja-Mcp-Token": PASSPHRASE},
            {"Authorization": "Basic " + PASSPHRASE},
        ):
            with self.subTest(headers=headers):
                status, _, _ = self.rpc("tools/list", token=None, headers=headers)
                self.assertEqual(status, 401)

    def test_the_source_holds_no_second_way_in(self):
        """A grep-shaped assertion, because the risk is a helpful addition later."""
        from pathlib import Path

        import vikunja_claude

        source = Path(next(iter(vikunja_claude.__path__)))
        text = "\n".join(
            path.read_text(encoding="utf-8") for path in source.glob("*.py")
        )
        self.assertNotIn("VIKUNJA_MCP_TOKEN", text)
        self.assertNotIn("ALLOW_ANONYMOUS", text)


class TestTheGrantedSurface(HttpTestCase):
    """What one successful authorization actually buys: the same two tools."""

    def tool_names(self) -> set[str]:
        _, _, text = self.rpc("tools/list")
        return {tool["name"] for tool in json.loads(text)["result"]["tools"]}

    def test_it_is_exactly_get_task_and_create_task(self):
        self.assertEqual(self.tool_names(), {"get_task", "create_task"})

    def test_a_valid_token_can_read_a_task(self):
        _, _, text = self.rpc(
            "tools/call", {"name": "get_task", "arguments": {"task_id": 9}}
        )
        result = json.loads(text)["result"]["structuredContent"]
        self.assertEqual(result["task_id"], 9)
        self.assertIn("Back up Vikunja database", result["title"])

    def test_a_valid_token_can_create_a_task(self):
        _, _, text = self.rpc(
            "tools/call",
            {
                "name": "create_task",
                "arguments": {
                    "project_id": PROJECT_ID,
                    "title": "A ticket dictated in a conversation",
                    "description": "With a body, because a ticket needs one.",
                },
            },
        )
        result = json.loads(text)["result"]["structuredContent"]
        self.assertTrue(result["created"])
        self.assertEqual(result["project_id"], PROJECT_ID)

    def test_creating_outside_the_one_project_is_still_refused(self):
        _, _, text = self.rpc(
            "tools/call",
            {
                "name": "create_task",
                "arguments": {
                    "project_id": PROJECT_ID + 99,
                    "title": "Somewhere else",
                    "description": "Should not be created.",
                },
            },
        )
        result = json.loads(text)["result"]
        self.assertTrue(result["isError"])
        self.assertIn("Refusing to create", result["content"][0]["text"])

    def test_an_identical_retry_still_returns_the_first_task(self):
        arguments = {
            "project_id": PROJECT_ID,
            "title": "Exactly the same ticket",
            "description": "Exactly the same body.",
        }
        _, _, first = self.rpc(
            "tools/call", {"name": "create_task", "arguments": arguments}
        )
        _, _, second = self.rpc(
            "tools/call", {"name": "create_task", "arguments": arguments}
        )
        first_result = json.loads(first)["result"]["structuredContent"]
        second_result = json.loads(second)["result"]["structuredContent"]
        self.assertTrue(first_result["created"])
        self.assertFalse(second_result["created"])
        self.assertEqual(second_result["task_id"], first_result["task_id"])

    def test_the_unsupported_write_operations_are_still_unavailable(self):
        for name in ("edit_task", "close_task", "delete_task", "move_task", "comment"):
            with self.subTest(name=name):
                _, _, text = self.rpc(
                    "tools/call", {"name": name, "arguments": {"task_id": 9}}
                )
                self.assertIn("error", json.loads(text))


if __name__ == "__main__":
    unittest.main()
