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

from unittest import mock

from vikunja_claude.oauth import MAX_FAILED_ATTEMPTS, SCOPE, AuthorizationServer
from vikunja_claude.oauth_store import (
    MAX_CLIENTS,
    PENDING_CLIENT_TTL_SECONDS,
    OAuthStore,
)

from .fakes import PROJECT_ID
from .support import (
    CLIENT_NAME,
    CONNECTOR_REDIRECT_URI,
    PASSPHRASE,
    REDIRECT_URI,
    VIKUNJA_TOOLS,
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


class TestTheClientCapDoesNotStrandAConnector(HttpTestCase):
    """What the cap on registered clients may and may not remove (task 840).

    Registration is open to the internet, and a connector registers exactly
    once — when it is created — and then never again. So under eviction by age
    alone the working connector is always the oldest client and always the
    first to go, which is how the live boundary ended up answering ChatGPT's
    own id with "Unknown client_id". The cap still has to bound the file; it
    is only allowed to spend clients nobody ever authorized.
    """

    def stored(self) -> dict:
        return json.loads(self.config.oauth_state_path.read_text())["clients"]

    def flood(self, count: int) -> None:
        """What a scanner, a re-add or a run of test connectors does."""
        for number in range(count):
            registered = self.register_client(client_name=f"throwaway {number}")
            self.assertEqual(registered["status"], 201, registered)

    def connected_client(self, client_name: str = CLIENT_NAME) -> str:
        """A client carried all the way to a token, as a connector is.

        Then aged, deliberately, so that it is the *first* record any
        age-ordered eviction would reach. A connector registers once and never
        again, so being the oldest thing in the file is its normal condition —
        and a test that leaves it tied with the flood only proves the rule
        about 1 record in 20, which is to say it passes whether the rule holds
        or not.
        """
        code, verifier, client_id = self.obtain_code(client_name=client_name)
        status, payload = self.exchange(code, verifier, client_id)
        self.assertEqual(status, 200, payload)
        state = json.loads(self.config.oauth_state_path.read_text())
        state["clients"][client_id]["issued_at"] = 1
        self.config.oauth_state_path.write_text(json.dumps(state))
        return client_id

    def test_an_authorized_client_is_never_evicted(self):
        client_id = self.connected_client()
        self.flood(MAX_CLIENTS + 5)
        self.assertIn(client_id, self.stored())

    def test_it_still_reaches_the_consent_screen_after_the_flood(self):
        """Glen's acceptance gesture, end to end over the socket.

        The store keeping the record is not the claim; the claim is that
        re-authorizing from the connector dialog gets the consent screen. That
        is a different code path from the one that stored it, and it is the
        one that failed.
        """
        client_id = self.connected_client()
        self.flood(MAX_CLIENTS + 5)
        _, challenge = self.pkce()
        query = urllib.parse.urlencode(self.authorize_params(client_id, challenge))
        status, _, body = self.open(f"/oauth/authorize?{query}", token=None)
        self.assertEqual(status, 200, body)
        self.assertNotIn("Unknown client_id", body)

    def test_the_token_it_already_holds_still_opens_the_boundary(self):
        """The evicted-client failure reached /mcp too, once the token expired."""
        token = self.access_token()
        self.flood(MAX_CLIENTS + 5)
        status, _, body = self.rpc("tools/list", token=token)
        self.assertEqual(status, 200, body)

    def test_the_file_is_still_bounded_by_clients_nobody_authorized(self):
        """The cap has to keep working: an open endpoint, a file on disk.

        The record is aged by hand because every registration a test makes
        lands in the same second, and the file is written with sorted keys —
        so with equal timestamps "oldest" is alphabetical, and the claim about
        age would not be a claim about anything.
        """
        oldest = self.register_client(client_name="never came back")["client_id"]
        state = json.loads(self.config.oauth_state_path.read_text())
        state["clients"][oldest]["issued_at"] = 1
        self.config.oauth_state_path.write_text(json.dumps(state))

        self.flood(MAX_CLIENTS)
        clients = self.stored()
        self.assertLessEqual(len(clients), MAX_CLIENTS)
        self.assertNotIn(oldest, clients)

    def test_a_full_store_of_authorized_clients_refuses_the_registration(self):
        """No room and nothing spendable is a refusal, not a sacrifice.

        The cap is patched down because the rule is about the shape of the
        store, not about the number twenty, and reaching twenty authorizations
        is twenty consent screens. The two are named apart because two
        connections from one connector are not two clients any more: the
        second supersedes the first, and the store would hold one.
        """
        with mock.patch("vikunja_claude.oauth_store.MAX_CLIENTS", 2):
            first = self.connected_client("one connector")
            second = self.connected_client("another connector")
            refused = self.register_client(client_name="one too many")
        self.assertEqual(refused["status"], 503, refused)
        self.assertEqual(refused["error"], "temporarily_unavailable")
        self.assertIn("state file", refused["error_description"])
        self.assertEqual(set(self.stored()), {first, second})

    def test_it_survives_after_every_token_it_held_has_expired(self):
        """A connector left alone for longer than its refresh token lives.

        Nothing in the file names the client any more, and it has done nothing
        wrong: re-authorizing from the connector dialog is exactly the gesture
        that is supposed to bring it back, and it needs the client record to
        still be there to do it. This is what the stamp is for, and it is the
        half a scan of live grants cannot cover.
        """
        client_id = self.connected_client()
        state = json.loads(self.config.oauth_state_path.read_text())
        state["codes"] = {}
        state["tokens"] = {}
        self.config.oauth_state_path.write_text(json.dumps(state))

        self.flood(MAX_CLIENTS + 2)
        self.assertIn(client_id, self.stored())

    def test_a_grant_protects_a_client_registered_before_the_stamp_existed(self):
        """The clients already in the live file when this rule shipped.

        They were authorized before anything wrote it down, so the grant the
        file holds for them is the only record of it — and it has to be
        enough, or the fix protects nothing that is already connected.
        """
        client_id = self.connected_client()
        state = json.loads(self.config.oauth_state_path.read_text())
        del state["clients"][client_id]["authorized_at"]
        state["codes"] = {}
        self.config.oauth_state_path.write_text(json.dumps(state))

        self.flood(MAX_CLIENTS + 2)
        self.assertIn(client_id, self.stored())

    def test_authorizing_stamps_the_client_it_authorized_and_no_other(self):
        bystander = self.register_client(client_name="bystander")["client_id"]
        client_id = self.connected_client()
        clients = self.stored()
        self.assertTrue(clients[client_id]["authorized_at"])
        self.assertNotIn("authorized_at", clients[bystander])


class TestReconnectingRetiresTheConnectionItReplaces(HttpTestCase):
    """One connector holds one registration, however often it is re-added.

    Task 840 stopped the cap from spending a client somebody was using. It did
    not stop the file filling up in the first place, and the live store filled
    the way it did because every reconnection left its predecessor behind:
    nine Qwen Code records against one live grant, three for OpenCode, two
    duplicate ChatGPT verifications. Twenty slots bounded by attempts rather
    than by connectors is a cap that is always about to have to choose.
    """

    def stored(self) -> dict:
        return json.loads(self.config.oauth_state_path.read_text())["clients"]

    @staticmethod
    def connector_uri(identifier: str) -> str:
        """A callback of ChatGPT's per-connector shape, which anyone can mint."""
        return f"https://chatgpt.com/connector/oauth/{identifier}"

    def connect(
        self, client_name: str = CLIENT_NAME, redirect_uri: str = REDIRECT_URI
    ) -> tuple[str, str]:
        """Add one connector, the whole way. Returns (client_id, access token).

        Registration, consent and exchange, rather than the shared helper,
        because both halves of what the store calls one connector — the name
        and the callback — have to be a knob a test can turn.
        """
        client = self.register_client(
            client_name=client_name, redirect_uris=[redirect_uri]
        )
        self.assertEqual(client["status"], 201, client)
        verifier, challenge = self.pkce()
        params = self.authorize_params(
            client["client_id"], challenge, redirect_uri=redirect_uri
        )
        status, headers, body = self.approve(params)
        self.assertEqual(status, 302, body)
        status, payload = self.exchange(
            self.redirect_query(headers)["code"],
            verifier,
            client["client_id"],
            redirect_uri=redirect_uri,
        )
        self.assertEqual(status, 200, payload)
        return client["client_id"], str(payload["access_token"])

    def test_reconnecting_retires_the_earlier_registration(self):
        first, _ = self.connect()
        second, _ = self.connect()
        self.assertNotEqual(first, second)
        self.assertEqual(set(self.stored()), {second})

    def test_the_retired_registration_takes_its_grants_with_it(self):
        """The half that makes the slot actually free.

        A token outliving its client record is the exact state the live file
        was found in, and it reads as an authorization to everything that
        looks: the cap would go on protecting a connection nobody can reach.
        """
        retired, superseded = self.connect()
        self.connect()
        status, _, body = self.rpc("tools/list", token=superseded)
        self.assertEqual(status, 401, body)
        state = json.loads(self.config.oauth_state_path.read_text())
        for section in ("codes", "tokens"):
            self.assertEqual(
                [],
                [
                    key
                    for key, record in state[section].items()
                    if record.get("client_id") == retired
                ],
                section,
            )

    def test_registering_again_without_authorizing_leaves_the_live_one_alone(self):
        """Why this happens at the consent screen and not at /oauth/register.

        OpenCode registered a third time on 2026-09-06 while still holding a
        refresh token good until October. A connector that asks for an id and
        never comes back with it has replaced nothing, and must not be able to
        end a working session by asking.
        """
        client_id, token = self.connect()
        again = self.register_client()
        self.assertEqual(again["status"], 201, again)
        self.assertIn(client_id, self.stored())
        status, _, body = self.rpc("tools/list", token=token)
        self.assertEqual(status, 200, body)

    def test_the_chatgpt_connector_door_holds_exactly_one_client(self):
        """One ChatGPT connector, whatever callback or name it arrives with.

        Every other redirect URI is admitted by exact equality against the
        configured list, so each names a connector the operator wrote down.
        The per-connector callback is the one gate that cannot: the path does
        not exist until the connector does, so the door admits a shape rather
        than a value. A shape is not a whitelist entry, so the door is a
        single slot instead — a second ChatGPT client replaces the first
        rather than sitting beside it.
        """
        first, _ = self.connect(redirect_uri=CONNECTOR_REDIRECT_URI)
        second, _ = self.connect(
            "a different name", redirect_uri=self.connector_uri("SecondConnector")
        )
        self.assertNotEqual(first, second)
        self.assertEqual(set(self.stored()), {second})

    def test_invented_callbacks_cannot_grow_the_file(self):
        """The flood the door made possible, which the slot closes.

        `https://chatgpt.com/connector/oauth/<anything>` is admitted by shape,
        so a stranger can mint distinct callbacks without limit and no
        whitelist entry is being violated. One slot means twenty-five of them
        leave one record, not twenty-five.
        """
        for number in range(MAX_CLIENTS + 5):
            registered = self.register_client(
                client_name=f"impostor {number}",
                redirect_uris=[self.connector_uri(f"Invented{number}")],
            )
            self.assertEqual(registered["status"], 201, registered)
        self.assertEqual(len(self.stored()), 1)

    def test_an_invented_callback_cannot_displace_the_connected_one(self):
        """The slot is one, but only the consent screen may empty it.

        The door is open to anyone, so if merely registering through it could
        retire whatever it found there, stranding the live ChatGPT connector
        would be one unauthenticated POST — the failure this whole change is
        about, handed out as a feature.
        """
        client_id, token = self.connect(redirect_uri=CONNECTOR_REDIRECT_URI)
        registered = self.register_client(
            client_name="impostor",
            redirect_uris=[self.connector_uri("Invented")],
        )
        self.assertEqual(registered["status"], 201, registered)
        self.assertIn(client_id, self.stored())
        status, _, body = self.rpc("tools/list", token=token)
        self.assertEqual(status, 200, body)

    def test_the_file_is_bounded_by_connectors_not_by_reconnections(self):
        """The claim the cap needed: re-adding one connector costs no slots."""
        for _ in range(MAX_CLIENTS + 5):
            self.connect()
        self.assertEqual(len(self.stored()), 1)

    def test_claiming_a_connected_client_s_identity_evicts_nothing(self):
        """Why replacement may not also happen at /oauth/register.

        An identity is a thing a stranger can state: registration takes no
        passphrase, and "Qwen Code" at localhost:7777 is a guess rather than a
        credential. Were a matching registration allowed to make room by
        retiring the client it names, an unauthenticated caller would hold the
        one power the cap exists to deny — 840's rule, defeated by asking for
        it in the right words. The consent screen is the first point anything
        has proved which connector is speaking.
        """
        with mock.patch("vikunja_claude.oauth_store.MAX_CLIENTS", 2):
            first, token = self.connect("Qwen Code")
            second, _ = self.connect("another connector")
            impostor = self.register_client(client_name="Qwen Code")
        self.assertEqual(impostor["status"], 503, impostor)
        self.assertEqual(set(self.stored()), {first, second})
        status, _, body = self.rpc("tools/list", token=token)
        self.assertEqual(status, 200, body)


class TestAConnectorWhoseRowIsGone(HttpTestCase):
    """Recovering from a lost client record without touching the state file.

    This is the failure the boundary actually had, twice: ChatGPT holding a
    client_id the file no longer carried, replaying it at /oauth/authorize,
    and being told to register first — which is the one thing its dialog
    cannot do a second time. Both times it was fixed by hand-editing
    mcp_oauth.json, which is not a recovery path, it is an admission that
    there isn't one.

    The configured redirect URIs are what say which connections this server
    has. The file is where grants live. So an admitted callback is enough to
    know a connector, and losing the file costs a consent, not a connector.
    """

    def stored(self) -> dict:
        """The clients on disk, where "no file yet" is a real answer.

        Two of these tests assert that nothing was written, and until
        something is there is no file to read — so a missing one is the state
        they are looking for, not an error.
        """
        if not self.config.oauth_state_path.exists():
            return {}
        return json.loads(self.config.oauth_state_path.read_text())["clients"]

    def authorize(self, client_id: str, redirect_uri: str = REDIRECT_URI, **overrides):
        _, challenge = self.pkce()
        params = self.authorize_params(
            client_id, challenge, redirect_uri=redirect_uri, **overrides
        )
        return params, self.approve(params)

    def get_authorize(self, client_id: str, redirect_uri: str = REDIRECT_URI):
        """Just ask, without submitting the form."""
        _, challenge = self.pkce()
        return self.open(
            "/oauth/authorize?"
            + urllib.parse.urlencode(
                self.authorize_params(client_id, challenge, redirect_uri=redirect_uri)
            ),
            token=None,
        )

    def connect(self, client_name: str, redirect_uri: str) -> tuple[str, str]:
        """A connector added the ordinary way. Returns (client_id, access token)."""
        client = self.register_client(
            client_name=client_name, redirect_uris=[redirect_uri]
        )
        self.assertEqual(client["status"], 201, client)
        verifier, challenge = self.pkce()
        params = self.authorize_params(
            client["client_id"], challenge, redirect_uri=redirect_uri
        )
        status, headers, body = self.approve(params)
        self.assertEqual(status, 302, body)
        status, payload = self.exchange(
            self.redirect_query(headers)["code"],
            verifier,
            client["client_id"],
            redirect_uri=redirect_uri,
        )
        self.assertEqual(status, 200, payload)
        return client["client_id"], str(payload["access_token"])

    def test_it_gets_back_in_through_the_consent_screen(self):
        lost = "cid_" + "a" * 40
        _, (status, headers, body) = self.authorize(lost)
        self.assertEqual(status, 302, body)
        self.assertIn("code", self.redirect_query(headers))
        self.assertIn(lost, self.stored())

    def test_the_readmitted_row_is_marked_so_it_is_never_evicted_again(self):
        lost = "cid_" + "b" * 40
        self.authorize(lost)
        self.assertTrue(self.stored()[lost]["authorized_at"])

    def test_the_recovered_connector_can_reach_the_boundary(self):
        """End to end, because a row in the file is not the claim."""
        lost = "cid_" + "c" * 40
        verifier, challenge = self.pkce()
        params = self.authorize_params(lost, challenge)
        status, headers, body = self.approve(params)
        self.assertEqual(status, 302, body)
        status, payload = self.exchange(
            self.redirect_query(headers)["code"], verifier, lost
        )
        self.assertEqual(status, 200, payload)
        status, _, body = self.rpc("tools/list", token=payload["access_token"])
        self.assertEqual(status, 200, body)

    def test_the_chatgpt_connector_recovers_the_same_way(self):
        """The live case: a per-connector callback, admitted by shape."""
        lost = "cid_" + "d" * 40
        _, (status, headers, body) = self.authorize(
            lost, redirect_uri=CONNECTOR_REDIRECT_URI
        )
        self.assertEqual(status, 302, body)
        self.assertIn(lost, self.stored())

    def test_a_wrong_passphrase_writes_nothing_at_all(self):
        """The whole of the gate, and the reason this is not a way in.

        Re-admission decides nothing about who is asking — it cannot, at an
        endpoint anyone can reach. So it must not leave a trace behind when
        the answer is no, or an open endpoint would be writing to the file
        again by another name.
        """
        lost = "cid_" + "e" * 40
        before = self.stored()
        _, challenge = self.pkce()
        status, _, _ = self.approve(
            self.authorize_params(lost, challenge), passphrase="wrong"
        )
        self.assertEqual(status, 401)
        self.assertEqual(self.stored(), before)

    def test_merely_asking_writes_nothing(self):
        """A GET that repaired what it read would be a write on a read."""
        before = self.stored()
        status, _, _ = self.get_authorize("cid_" + "f" * 40)
        self.assertEqual(status, 200)
        self.assertEqual(self.stored(), before)

    def test_an_unadmitted_callback_is_still_refused(self):
        """Re-admission rests on the whitelist; it does not replace it."""
        _, challenge = self.pkce()
        status, _, page = self.open(
            "/oauth/authorize?"
            + urllib.parse.urlencode(
                self.authorize_params(
                    "cid_" + "g" * 40,
                    challenge,
                    redirect_uri="https://evil.example/callback",
                )
            ),
            token=None,
        )
        self.assertEqual(status, 400)
        self.assertIn("Unknown client_id", page)

    def test_it_cannot_displace_a_connector_that_holds_a_grant(self):
        """Re-admission goes through registration, so it spends nothing.

        Someone guessing an id against the connector callback must not be able
        to take out the ChatGPT client that is actually connected.
        """
        connected, token = self.connect("ChatGPT", CONNECTOR_REDIRECT_URI)
        self.get_authorize("cid_" + "h" * 40, redirect_uri=CONNECTOR_REDIRECT_URI)
        self.assertIn(connected, self.stored())
        status, _, body = self.rpc("tools/list", token=token)
        self.assertEqual(status, 200, body)


class TestARegistrationThatWasNeverAuthorized(HttpTestCase):
    """An attempt that did not become a connection does not stay in the file.

    Registering is one half of adding a connector and consenting is the other,
    and nothing reports the half that did not happen: an abandoned dialog, a
    passphrase given up on and a stranger's POST all look alike, which is to
    say they look like nothing at all. So the record carries a deadline rather
    than waiting for a signal that is never sent.
    """

    def stored(self) -> dict:
        return json.loads(self.config.oauth_state_path.read_text())["clients"]

    def age(self, client_id: str, seconds: int) -> None:
        """Move one registration back in time, since the suite runs in one."""
        state = json.loads(self.config.oauth_state_path.read_text())
        state["clients"][client_id]["issued_at"] = int(time.time()) - seconds
        self.config.oauth_state_path.write_text(json.dumps(state))

    def touch(self) -> None:
        """Any write to the store, which is when expiry is applied."""
        self.register_client(client_name="some other connector")

    def test_it_is_gone_once_its_deadline_passes(self):
        client_id = self.register_client(client_name="never came back")["client_id"]
        self.age(client_id, PENDING_CLIENT_TTL_SECONDS + 60)
        self.touch()
        self.assertNotIn(client_id, self.stored())

    def test_it_is_kept_until_then(self):
        """The deadline is a deadline, not a sweep of everything pending."""
        client_id = self.register_client(client_name="mid-flow")["client_id"]
        self.age(client_id, PENDING_CLIENT_TTL_SECONDS - 60)
        self.touch()
        self.assertIn(client_id, self.stored())

    def test_a_wrong_passphrase_leaves_the_attempt_alone(self):
        """The retry needs something to authorize.

        A rejected passphrase re-renders the consent page so the operator can
        try again — it is the one failure that is not the end of the attempt,
        and removing the record there would turn a typo into "register the
        client first".
        """
        client = self.register_client()
        _, challenge = self.pkce()
        params = self.authorize_params(client["client_id"], challenge)
        status, _, _ = self.approve(params, passphrase="wrong")
        self.assertEqual(status, 401)
        self.assertIn(client["client_id"], self.stored())
        status, headers, body = self.approve(params)
        self.assertEqual(status, 302, body)
        self.assertIn("code", self.redirect_query(headers))

    def test_a_connection_has_no_deadline(self):
        """What separates the two: the consent screen, and nothing else.

        Aged far past the deadline and with every grant it held removed, so
        the only thing keeping it is the record of having been authorized.
        """
        code, verifier, client_id = self.obtain_code()
        status, payload = self.exchange(code, verifier, client_id)
        self.assertEqual(status, 200, payload)
        state = json.loads(self.config.oauth_state_path.read_text())
        state["codes"], state["tokens"] = {}, {}
        state["clients"][client_id]["issued_at"] = 1
        self.config.oauth_state_path.write_text(json.dumps(state))

        self.touch()
        self.assertIn(client_id, self.stored())


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
        # The other callback goes first, and its own registration is what
        # shows the shape is registerable. It cannot be held open alongside:
        # the connector door is one slot, so the registration that follows
        # takes it — which is why this order, not the reverse.
        other = "https://chatgpt.com/connector/oauth/someOtherConnector"
        self.assertRegisters(other)
        client_id = self.assertRegisters(CONNECTOR_REDIRECT_URI)

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
        """Every board the token reaches, and every verb it can use.

        Both halves are asserted because either one alone would let the screen
        understate the grant. It previously said the token could not edit or
        comment, which stopped being true when task 196 added those two behind
        their approval step — a consent screen that undersells the grant is the
        same defect as one that oversells it.
        """
        status, _, page = self.get_authorize()
        self.assertEqual(status, 200)
        for verb in ("read", "list", "search", "create", "edit", "comment"):
            with self.subTest(verb=verb):
                self.assertIn(verb, page)
        for board in ("AI Alpha Engine", "AI Alpha Trader"):
            with self.subTest(board=board):
                self.assertIn(board, page)
        self.assertIn("cannot close, delete, move, label", page)
        self.assertIn("approval", page)

    def test_the_consent_screen_names_no_board_it_cannot_reach(self):
        """The screen is rendered from the same configured set the tools enforce."""
        _, _, page = self.get_authorize()
        self.assertNotIn("Inbox", page)
        self.assertIn(self.config.projects_phrase, page)

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
        """The only address on offer is the one the bad request supplied.

        An unknown client_id is now refused on the callback rather than on the
        id, since an admitted callback is enough to re-admit one; what may not
        happen either way is a bounce to the address the bad request named.
        """
        status, headers, _ = self.get_authorize(
            client_id="cid_invented", redirect_uri="https://evil.example/callback"
        )
        self.assertEqual(status, 400)
        self.assertNotIn("Location", headers)

    def test_an_unknown_client_on_an_admitted_callback_reaches_consent(self):
        """A connector whose row is gone gets the passphrase prompt, not a wall.

        It cannot register again — its dialog did that once, when it was
        created, and every attempt since replays the id it was given — so
        refusing here leaves it with no gesture that recovers it. Reaching the
        consent screen is not being let in: the passphrase still is.
        """
        status, headers, body = self.get_authorize(client_id="cid_invented")
        self.assertEqual(status, 200, body)
        self.assertNotIn("Location", headers)
        self.assertIn("passphrase", body)

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
            {"name": "get_task", "arguments": {"task_number": 8}},
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
    """What one successful authorization actually buys: the same fixed tools."""

    def tool_names(self) -> set[str]:
        _, _, text = self.rpc("tools/list")
        return {tool["name"] for tool in json.loads(text)["result"]["tools"]}

    def test_it_is_exactly_the_tools_the_boundary_defines(self):
        self.assertEqual(self.tool_names(), VIKUNJA_TOOLS)

    def test_a_valid_token_can_read_a_task(self):
        _, _, text = self.rpc(
            "tools/call", {"name": "get_task", "arguments": {"task_number": 8}}
        )
        result = json.loads(text)["result"]["structuredContent"]
        self.assertEqual(self.vikunja.id_of(result["task_number"]), 9)
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
        self.assertEqual(second_result["task_number"], first_result["task_number"])

    def test_the_unsupported_write_operations_are_still_unavailable(self):
        """Narrowed by task 196: editing and commenting were added deliberately.

        `edit_task` and `comment` came off this list because tools doing those
        things now exist under their own names — but closing, deleting, moving
        and reassigning are still nothing this token can buy, and that is what
        an authorization is now checked against.
        """
        for name in ("close_task", "delete_task", "move_task", "assign_task"):
            with self.subTest(name=name):
                _, _, text = self.rpc(
                    "tools/call", {"name": name, "arguments": {"task_number": 8}}
                )
                self.assertIn("error", json.loads(text))

    def call(self, name: str, **arguments) -> dict:
        _, _, text = self.rpc("tools/call", {"name": name, "arguments": arguments})
        return json.loads(text)["result"]

    def stored(self, task_id: int) -> dict:
        return self.vikunja._find(task_id)

    def test_a_valid_token_can_update_a_task_after_approving_the_change(self):
        """The whole two-step flow, over the socket, on a real authorization."""
        before = self.stored(9)["title"]
        new_title = "#33 Back up the Vikunja database nightly"

        previewed = self.call("update_task", task_number=8, title=new_title)[
            "structuredContent"
        ]
        self.assertFalse(previewed["applied"])
        self.assertEqual(previewed["current"]["title"], before)
        self.assertEqual(previewed["proposed"]["title"], new_title)
        self.assertEqual(self.stored(9)["title"], before, "the preview wrote")

        applied = self.call(
            "update_task",
            task_number=8,
            title=new_title,
            approval_token=previewed["approval_token"],
        )["structuredContent"]
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["changed_fields"], ["title"])
        self.assertEqual(self.stored(9)["title"], new_title)

    def test_a_valid_token_alone_does_not_buy_an_update(self):
        """Authorization is not approval: a token is not a token."""
        before = self.stored(9)["title"]
        result = self.call("update_task", task_number=8, title="#33 Renamed",
                           approval_token="a-token-nobody-issued")
        self.assertTrue(result["isError"])
        self.assertEqual(self.stored(9)["title"], before)

    def test_a_valid_token_can_comment_after_approving_the_comment(self):
        text = "Verified through the deployed endpoint."
        previewed = self.call("add_task_comment", task_number=8, comment=text)[
            "structuredContent"
        ]
        self.assertFalse(previewed["added"])
        self.assertEqual(previewed["comment"], text)
        self.assertEqual(self.vikunja.comments.get(9, []), [])

        added = self.call(
            "add_task_comment",
            task_number=8,
            comment=text,
            approval_token=previewed["approval_token"],
        )["structuredContent"]
        self.assertTrue(added["added"])
        self.assertIsNotNone(added["comment_id"])
        self.assertEqual(len(self.vikunja.comments[9]), 1)

        # And the read tools see it, which is what a user checking would do.
        read = self.call("get_task", task_number=8)["structuredContent"]
        self.assertEqual([c["text"] for c in read["comments"]], [text])

    def test_editing_a_task_outside_the_one_project_is_refused(self):
        result = self.call("update_task", task_number=4242, title="Somewhere else")
        self.assertTrue(result["isError"])
        self.assertIn("Nothing was changed", result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
