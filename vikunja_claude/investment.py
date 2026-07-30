"""The AI Server operational reads, as this boundary is allowed to see them.

Task 138 approved four operational reads beside the Vikunja ones: the
repository's branch and commit, whether its working tree is clean, the latest
nightly-pipeline status and the latest system-health result. It also excluded
shell execution and direct database access — so this module performs none of
those. It issues **GET requests to three fixed paths** on the investment
application, which owns all four facts already and decides there what an external
integration may see (``api/operational_reads.py`` in that repository).

Two properties are structural rather than promised.

**There is no path parameter.** Each read is a method with a literal constant, so
there is no argument through which a caller could aim this client at another
endpoint of that API. A generic ``get(path)`` would have made the two-tool
discipline of :mod:`vikunja_claude.mcp` meaningless one layer down.

**There is no write method.** Not a refused one — an absent one. The class cannot
POST, and the transport it is handed is only ever asked for GET.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

#: The three reads, named once. Adding a fourth is an edit here and a tool there.
PATH_REPOSITORY = "/operational/repository"
PATH_PIPELINE = "/operational/pipeline"
PATH_SYSTEM_HEALTH = "/operational/system-health"

READ_PATHS = (PATH_REPOSITORY, PATH_PIPELINE, PATH_SYSTEM_HEALTH)

#: System health shells out to docker, systemctl and crontab, so it is the slow
#: one. Generous enough not to fail on a loaded host, bounded so a wedged service
#: cannot hold a conversation open indefinitely.
DEFAULT_TIMEOUT_SECONDS = 25

Transport = Callable[[str], Any]


class InvestmentStatusError(RuntimeError):
    """The operational read could not be performed, and why."""


class InvestmentStatusClient:
    """Read-only client for the investment application's operational endpoints."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        transport: Transport | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.api_url = api_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport or self._get

    # -- transport ---------------------------------------------------------

    def _get(self, path: str) -> Any:
        request = urllib.request.Request(
            self.api_url + path,
            method="GET",
            headers={
                "X-API-Key": self._api_key,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # The body is not repeated. It is the API's own error text and may
            # name internals; the status code is what the model needs to act on,
            # and 401/403 is a configuration problem rather than a transient one.
            raise InvestmentStatusError(
                self._explain_status(exc.code, path)
            ) from exc
        except urllib.error.URLError as exc:
            raise InvestmentStatusError(
                f"Cannot reach the investment API at {self.api_url}: {exc.reason}. "
                "The operational read was not performed; nothing is known about "
                "the state it would have reported."
            ) from exc
        except TimeoutError as exc:
            raise InvestmentStatusError(
                f"The investment API did not answer {path} within "
                f"{self._timeout}s."
            ) from exc

        if not raw:
            raise InvestmentStatusError(
                f"The investment API sent an empty body for {path}."
            )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InvestmentStatusError(
                f"The investment API sent a non-JSON response for {path}."
            ) from exc

    def _explain_status(self, code: int, path: str) -> str:
        """What an HTTP failure means, said as the thing to fix.

        401/403 and 404 are the two that get a sentence of their own, because
        they are the two that are configuration rather than weather, and because
        neither must ever read as "the status is fine".
        """
        if code in (401, 403):
            return (
                f"The investment API refused the operational read ({code}). The "
                "configured INVESTMENT_API_KEY is not accepted. Nothing was read "
                "— this is not a statement about the system's health."
            )
        if code == 404:
            # Two causes, and naming only the first is how a stale deployment gets
            # misdiagnosed as a misconfiguration — which is what happened the
            # first time these reads were switched on against a live admin
            # instance that had not been restarted since they were merged.
            return (
                f"The investment API has no {path} ({code}). Either "
                "INVESTMENT_API_URL points at the public instance — these "
                "endpoints are registered on the admin one only (INSTANCE_MODE="
                "admin) — or the admin instance is running code from before they "
                "existed and has not been restarted. Nothing was read."
            )
        return f"The investment API returned HTTP {code} for {path}."

    # -- the three reads ---------------------------------------------------

    def repository_state(self) -> dict[str, Any]:
        return self._object(PATH_REPOSITORY)

    def pipeline_status(self) -> dict[str, Any]:
        return self._object(PATH_PIPELINE)

    def system_health(self) -> dict[str, Any]:
        return self._object(PATH_SYSTEM_HEALTH)

    def _object(self, path: str) -> dict[str, Any]:
        payload = self._transport(path)
        if not isinstance(payload, dict):
            raise InvestmentStatusError(
                f"The investment API answered {path} with "
                f"{type(payload).__name__}, not an object."
            )
        return payload
