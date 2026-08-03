"""The AI Server operational reads, as this boundary is allowed to see them.

Task 138 approved four operational reads beside the Vikunja ones: the
repository's branch and commit, whether its working tree is clean, the latest
nightly-pipeline status and the latest system-health result. It also excluded
shell execution and direct database access — so this module performs none of
those. It issues **GET requests to fixed paths** on the investment application,
which owns all of those facts already and decides there what an external
integration may see (``api/operational_reads.py`` in that repository).

Task 279 added three more of the same shape: one tracked file, a literal-text
search and one commit's diff. They are what let a ticket review inspect the
implementation being claimed rather than stopping at the completion comment.
Every rule about them — which revisions resolve, which paths are denied, what is
redacted, where the limits sit — lives in ``api/repository_read.py`` **there**,
not here. This module is a client, and a client that re-decided any of that
would be a second boundary that can disagree with the one that matters.

Three properties are structural rather than promised.

**There is no path parameter.** Each read is a method with a literal constant, so
there is no argument through which a caller could aim this client at another
endpoint of that API. A generic ``get(path)`` would have made the two-tool
discipline of :mod:`vikunja_claude.mcp` meaningless one layer down. The task 279
reads take *query* arguments — a repository-relative path, a commit id, a search
literal — and those are urlencoded into a fixed path, never concatenated onto
one. What the caller can vary is what to look for, never where to look.

**There is no write method.** Not a refused one — an absent one. The class cannot
POST, and the transport it is handed is only ever asked for GET.

**A refusal comes back as a refusal.** The application answers 400/404 for a path
or revision it will not read, and those arrive here as
:class:`InvestmentStatusError` carrying the application's own reason. They are
not retried, not softened, and not reported as an unknown state.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from typing import Any, Callable, Mapping, Optional

#: The reads, named once. Adding another is an edit here and a tool there.
PATH_REPOSITORY = "/operational/repository"
PATH_PIPELINE = "/operational/pipeline"
PATH_SYSTEM_HEALTH = "/operational/system-health"

#: Tracked Git content (task 279). Literal constants like the three above; the
#: caller supplies query arguments to them and never the path itself.
PATH_REPOSITORY_FILE = "/operational/repository/file"
PATH_REPOSITORY_SEARCH = "/operational/repository/search"
PATH_REPOSITORY_DIFF = "/operational/repository/diff"

READ_PATHS = (
    PATH_REPOSITORY,
    PATH_PIPELINE,
    PATH_SYSTEM_HEALTH,
    PATH_REPOSITORY_FILE,
    PATH_REPOSITORY_SEARCH,
    PATH_REPOSITORY_DIFF,
)

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
            # The body is repeated only when it is the application's own
            # deliberate refusal — see `_explain_status`.
            raise InvestmentStatusError(
                self._explain_status(exc.code, path, _read_detail(exc))
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

    def _explain_status(
        self, code: int, path: str, detail: Optional[str] = None
    ) -> str:
        """What an HTTP failure means, said as the thing to fix.

        401/403 and 404 are the two that get a sentence of their own, because
        they are the two that are configuration rather than weather, and because
        neither must ever read as "the status is fine".

        ``detail`` is the application's own refusal text, present only when it
        answered with a FastAPI error body. That distinction is what lets a 404
        be told apart: with a detail it is the application saying "no such file
        at that commit", without one it is the *route* being absent, which is a
        stale deployment and a completely different thing to fix. The repository
        reads (task 279) refuse by design — an absolute path, a revision
        expression, a denied path — and their reasons are written for the caller
        to act on, so withholding them would turn a precise refusal into a
        shrug.
        """
        if code in (401, 403):
            return (
                f"The investment API refused the operational read ({code}). The "
                "configured INVESTMENT_API_KEY is not accepted. Nothing was read "
                "— this is not a statement about the system's health."
            )
        if code == 400 and detail:
            return f"The investment API refused this read: {detail}"
        if code == 404 and detail:
            return f"The investment API found nothing to read: {detail}"
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
        if detail:
            return f"The investment API returned HTTP {code} for {path}: {detail}"
        return f"The investment API returned HTTP {code} for {path}."

    # -- the three reads ---------------------------------------------------

    def repository_state(self) -> dict[str, Any]:
        return self._object(PATH_REPOSITORY)

    def pipeline_status(self) -> dict[str, Any]:
        return self._object(PATH_PIPELINE)

    def system_health(self) -> dict[str, Any]:
        return self._object(PATH_SYSTEM_HEALTH)

    # -- tracked Git content (task 279) ------------------------------------

    def repository_file(
        self,
        path: Any,
        revision: Any = None,
        start_line: Any = None,
        end_line: Any = None,
    ) -> dict[str, Any]:
        """One tracked text file at a resolved commit."""
        return self._object(
            _with_query(
                PATH_REPOSITORY_FILE,
                {
                    "path": path,
                    "revision": revision,
                    "start_line": start_line,
                    "end_line": end_line,
                },
            )
        )

    def repository_search(
        self,
        query: Any,
        revision: Any = None,
        path_filter: Any = None,
        case_sensitive: Any = None,
    ) -> dict[str, Any]:
        """Where a literal string appears in tracked files at a commit."""
        return self._object(
            _with_query(
                PATH_REPOSITORY_SEARCH,
                {
                    "query": query,
                    "revision": revision,
                    "path_filter": path_filter,
                    "case_sensitive": case_sensitive,
                },
            )
        )

    def repository_diff(self, revision: Any, path_filter: Any = None) -> dict[str, Any]:
        """What one commit changed, against its first parent."""
        return self._object(
            _with_query(
                PATH_REPOSITORY_DIFF,
                {"revision": revision, "path_filter": path_filter},
            )
        )

    def _object(self, path: str) -> dict[str, Any]:
        payload = self._transport(path)
        if not isinstance(payload, dict):
            raise InvestmentStatusError(
                f"The investment API answered {path} with "
                f"{type(payload).__name__}, not an object."
            )
        return payload


def _read_detail(exc: urllib.error.HTTPError) -> Optional[str]:
    """The application's own refusal text, if it sent one.

    Only a FastAPI error body — ``{"detail": "..."}`` with a string — counts.
    An HTML error page from a proxy, or an empty body, returns None and the
    caller says the generic thing. Reading the body is best-effort by design: a
    failure to parse it must not replace the HTTP failure with a parsing
    failure.

    **A generic phrase is not a reason.** Starlette answers a request for a route
    it does not have with ``{"detail": "Not Found"}`` — a body, and a useless
    one. Taken as the application's reason it would replace the diagnosis that
    matters for exactly that case ("the admin instance is running code from
    before these endpoints existed, and has not been restarted") with the word
    "Not Found". So a detail equal to the status code's own reason phrase is
    treated as no detail at all. Derived from :mod:`http` rather than listed,
    because that is where Starlette gets it too.
    """
    try:
        payload = json.loads(exc.read() or b"")
    except Exception:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("detail"), str):
        return None
    detail = payload["detail"].strip()
    if not detail:
        return None
    try:
        generic = HTTPStatus(exc.code).phrase
    except ValueError:
        generic = ""
    if detail.casefold() == generic.casefold():
        return None
    return detail


def _with_query(path: str, arguments: Mapping[str, Any]) -> str:
    """A fixed path with the caller's arguments urlencoded onto it.

    ``path`` is always one of this module's constants and is never built from an
    argument. Values are dropped when omitted rather than sent empty, so an
    absent ``revision`` means "the server's default, HEAD" instead of "a
    revision named ''" — the two get different answers, and only one of them is
    what the caller meant.

    Booleans are lowercased because that is what FastAPI's bool parser accepts;
    Python's ``str(True)`` is ``"True"``, which it rejects.
    """
    query: dict[str, str] = {}
    for key, value in arguments.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            query[key] = "true" if value else "false"
        else:
            query[key] = str(value)
    if not query:
        return path
    return f"{path}?{urllib.parse.urlencode(query)}"
