"""On-disk state for the OAuth boundary: clients, codes and tokens.

One JSON file in the existing state directory, and nothing else — no database,
no cache server, no second daemon. This is a single-user integration with one
client and one live session, so the whole store is small enough to read and
rewrite on every write, and being a plain file is what makes revocation a thing
you can do with ``rm`` rather than an endpoint that has to be reachable.

Secrets are stored as SHA-256 digests. A token is a random string the client
holds; the file holds only enough to recognise it, so a leaked copy of the state
file does not hand anyone a working credential.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

# A registered client is cheap but not free: registration is reachable from the
# internet, so the file must not be able to grow without bound.
MAX_CLIENTS = 20


class ClientStoreFull(RuntimeError):
    """The cap is reached and every client in the file has been authorized.

    Registration is refused rather than satisfied by evicting one of them.
    The cap exists to stop an open endpoint growing the file without bound;
    taking out a working connector to make room for an unknown one trades the
    thing the boundary is for against the thing it is defending from.
    """


def _identity(record: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    """What makes two registrations the same connector rather than two.

    Everything a client tells the server about itself, and nothing the server
    told it: the client_id cannot appear here, because being given a new one
    is what re-registering *is*.
    """
    return (
        str(record.get("client_name") or ""),
        tuple(sorted(str(uri) for uri in record.get("redirect_uris") or ())),
    )


def new_secret() -> str:
    """A credential value: 32 bytes of urandom, URL-safe."""
    return secrets.token_urlsafe(32)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class OAuthStore:
    """Clients, authorization codes and tokens, persisted as one JSON object."""

    def __init__(self, path: Path, now=time.time):
        self.path = path
        self._now = now
        self._lock = threading.Lock()

    # -- file ---------------------------------------------------------------

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            raw = {}
        for section in ("clients", "codes", "tokens"):
            raw.setdefault(section, {})
        return raw

    def _write(self, state: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        # 0600 from the moment it exists: it holds the shape of every live
        # grant, and the state directory is not otherwise protected.
        handle = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def _expire(self, state: dict[str, dict[str, Any]]) -> None:
        now = self._now()
        for section in ("codes", "tokens"):
            state[section] = {
                key: record
                for key, record in state[section].items()
                if record.get("expires_at", 0) > now
            }

    # -- clients ------------------------------------------------------------

    @staticmethod
    def _authorized(state: dict[str, dict[str, Any]]) -> set[str]:
        """The client ids the operator has approved.

        Two sources, because a stamp cannot see backwards. ``authorized_at``
        is written where a code is issued, which is downstream of the
        passphrase, so it means the operator approved this client — and it
        outlives every token it led to. The live codes and tokens cover the
        clients authorized before the stamp existed: a grant in the file is
        evidence of an approval whether or not the client record says so.
        """
        approved = {
            client_id
            for client_id, record in state["clients"].items()
            if record.get("authorized_at")
        }
        for section in ("codes", "tokens"):
            for record in state[section].values():
                client_id = record.get("client_id")
                if client_id:
                    approved.add(client_id)
        return approved

    def _supersede(
        self,
        state: dict[str, dict[str, Any]],
        client_id: str,
        client: dict[str, Any],
    ) -> None:
        """Retire the earlier registrations this connection has replaced.

        A connector that is re-added registers afresh, so the record it was
        using before is one nothing can reach: its id is gone from the only
        place that held it. Left in the file those records accumulate — nine
        for Qwen Code against a single live grant, three for OpenCode — until
        the cap has to choose between clients, and choosing wrongly is what
        stranded ChatGPT.

        Identity is what the connector says about itself, its name and its
        callbacks, because a re-registration is precisely the act of being
        given a new client_id. ChatGPT's callback carries a per-connector
        path, so two connectors of the same product stay distinct.

        Only from an authorization, never from a registration: until the new
        connection exists the old one is still the working one, and a
        connector that registers speculatively without ever authorizing must
        not be able to end a live session by asking.
        """
        identity = _identity(client)
        for other_id, other in list(state["clients"].items()):
            if other_id == client_id or _identity(other) != identity:
                continue
            del state["clients"][other_id]
            self._forget_grants(state, other_id)

    @staticmethod
    def _forget_grants(state: dict[str, dict[str, Any]], client_id: str) -> None:
        """Drop what a removed client held, in the same write that removes it.

        A token outliving its client record is the state the live file was
        found in: unreachable by the connector, because the id it would
        present is gone, yet still evidence of an authorization to everything
        that reads the file — so the cap goes on protecting a slot for a
        connection nobody can make.
        """
        for section in ("codes", "tokens"):
            state[section] = {
                key: record
                for key, record in state[section].items()
                if record.get("client_id") != client_id
            }

    def _note_authorization(
        self, state: dict[str, dict[str, Any]], client_id: str | None
    ) -> None:
        """Record that this client was authorized, durably.

        A configured static client is not in the file and needs no mark: it
        cannot be evicted from a store it was never in.
        """
        client = state["clients"].get(client_id or "")
        if client is None:
            return
        if not client.get("authorized_at"):
            client["authorized_at"] = int(self._now())
        self._supersede(state, client_id or "", client)

    def register_client(self, record: dict[str, Any]) -> dict[str, Any]:
        """Store a newly registered client, evicting only an unused one.

        Registration is reachable from the internet and a connector registers
        exactly once, when it is created — so "the oldest client" is the
        working one, not the disposable one, and evicting by age alone strands
        the connector with an id nothing recognises any more (task 840).
        Raises :class:`ClientStoreFull` rather than evicting an authorized
        client that is not this one.
        """
        with self._lock:
            state = self._read()
            self._expire(state)
            clients = state["clients"]
            authorized = self._authorized(state)

            def by_age(candidates) -> list[tuple[str, dict[str, Any]]]:
                return sorted(candidates, key=lambda item: item[1].get("issued_at", 0))

            # Oldest first, and never one holding a grant. A client that
            # registered and never came back is the one nobody misses.
            evictable = by_age(
                (client_id, held)
                for client_id, held in clients.items()
                if client_id not in authorized
            )
            # Then, and only once there is nothing else left to spend, this
            # connector's own earlier records. A store full of authorized
            # clients would otherwise lock out the one connector whose
            # registration costs the file nothing — the authorization to come
            # retires those records anyway. It can never reach another
            # connector's: same name, same callbacks, or not a candidate.
            identity = _identity(record)
            evictable += by_age(
                (client_id, held)
                for client_id, held in clients.items()
                if client_id in authorized and _identity(held) == identity
            )
            while len(clients) >= MAX_CLIENTS:
                if not evictable:
                    raise ClientStoreFull(
                        f"all {len(clients)} registered clients have been "
                        "authorized, so there is none to evict"
                    )
                victim = evictable.pop(0)[0]
                clients.pop(victim)
                self._forget_grants(state, victim)
            clients[record["client_id"]] = record
            self._write(state)
        return record

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        return self._read()["clients"].get(client_id)

    # -- authorization codes ------------------------------------------------

    def put_code(self, code: str, record: dict[str, Any]) -> None:
        with self._lock:
            state = self._read()
            self._expire(state)
            state["codes"][digest(code)] = record
            self._note_authorization(state, record.get("client_id"))
            self._write(state)

    def take_code(self, code: str) -> dict[str, Any] | None:
        """Redeem a code once.

        A second redemption returns ``None`` *and* revokes everything the first
        one issued: a replayed code means the code leaked, and the tokens it
        produced are the thing the leak was after.
        """
        key = digest(code)
        with self._lock:
            state = self._read()
            self._expire(state)
            record = state["codes"].get(key)
            if record is None:
                self._write(state)
                return None
            if record.get("redeemed"):
                grant = record.get("grant_id")
                state["tokens"] = {
                    token_key: token
                    for token_key, token in state["tokens"].items()
                    if token.get("grant_id") != grant
                }
                self._write(state)
                return None
            record["redeemed"] = True
            self._write(state)
        return record

    # -- tokens -------------------------------------------------------------

    def put_token(self, token: str, record: dict[str, Any]) -> None:
        with self._lock:
            state = self._read()
            self._expire(state)
            state["tokens"][digest(token)] = record
            self._write(state)

    def get_token(self, token: str) -> dict[str, Any] | None:
        record = self._read()["tokens"].get(digest(token))
        if record is None or record.get("expires_at", 0) <= self._now():
            return None
        return record

    def drop_token(self, token: str) -> None:
        with self._lock:
            state = self._read()
            self._expire(state)
            state["tokens"].pop(digest(token), None)
            self._write(state)

    def revoke_grant(self, grant_id: str) -> None:
        """Withdraw every token issued from one authorization."""
        with self._lock:
            state = self._read()
            self._expire(state)
            state["tokens"] = {
                key: record
                for key, record in state["tokens"].items()
                if record.get("grant_id") != grant_id
            }
            self._write(state)
