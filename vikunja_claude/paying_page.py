"""One page of the AI Server website, read as the test paying user.

Task 239. :mod:`vikunja_claude.website` fetches a page as an anonymous visitor
and carries no credential at all, which is what makes "it cannot read a page a
stranger cannot read" arithmetic. This is the one authenticated read beside it,
and it is deliberately built the other way round: **this module holds no session
and mints none.** It asks the investment application's admin instance for a page
"as the test paying user", and the application decides who that is, whether they
are still a paying user, which routes it will render and what may come back
(``api/paying_page_read.py`` in that repository).

That split is the point. The identity lives on the server, in
``TEST_PAYING_USER_ID``; there is no argument here through which a caller could
name a user, no password anywhere in this process, and no session cookie in the
answer — so this is one fixed read, not an impersonation facility.

Three properties are structural rather than promised.

**There is one endpoint.** A literal constant, like the three operational reads,
so no argument can aim this at another endpoint of that API. The path a caller
supplies is a *query argument to that one endpoint*, not a path on it, and it is
required to be a path — a value carrying a scheme, an authority, a backslash or
a fragment is refused here before anything is sent.

**There is no write method.** Not a refused one — an absent one. The class
cannot POST, and the endpoint it calls only ever issues a GET of its own.

**The answer is projected, not passed through.** Every field an external caller
can see is named below. A field added to the application's response reaches
nobody until somebody adds it here on purpose — the same rule the operational
reads hold, for the same reason.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .website import PublicPageError, normalise_path

#: The one endpoint. Adding a second is an edit here and a tool there.
PATH_TEST_PAYING_PAGE = "/operational/test-paying-page"

#: Cockpit and report pages touch the database and render a full analysis, so
#: this is a page timeout rather than a status one. Bounded so a wedged worker
#: cannot hold a conversation open.
DEFAULT_TIMEOUT_SECONDS = 30

#: Every field the tool returns. The application's response is projected onto
#: this list; anything it grows later is not published by accident.
RESULT_FIELDS = (
    "url",
    "path",
    "status",
    "status_text",
    "headers",
    "set_cookie_names",
    "html",
    "html_omitted_reason",
    "truncated",
    "bytes",
    "authenticated",
    "identity",
)


class PayingPageError(RuntimeError):
    """The page could not be read, and why. Never a page."""


Transport = Callable[[str], Any]


class PayingSiteClient:
    """Read one page as the server-configured test paying user."""

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

    def _get(self, url: str) -> Any:
        request = urllib.request.Request(
            url,
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
            raise PayingPageError(self._explain(exc)) from exc
        except urllib.error.URLError as exc:
            raise PayingPageError(
                f"Cannot reach the investment API at {self.api_url}: {exc.reason}. "
                "Nothing was read, so nothing is known about what that page "
                "returns."
            ) from exc
        except TimeoutError as exc:
            raise PayingPageError(
                f"The investment API did not answer within {self._timeout}s. "
                "Nothing was read."
            ) from exc

        if not raw:
            raise PayingPageError("The investment API sent an empty body.")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PayingPageError(
                "The investment API sent a non-JSON response."
            ) from exc

    def _explain(self, exc: urllib.error.HTTPError) -> str:
        """What an HTTP failure means, said as the thing to fix.

        Unlike the operational reads, the body *is* repeated for 4xx and 503:
        those are this application's own refusals — a route this read may not
        ask for, or a test identity that is not configured or is no longer a
        paying user — and each is written to be acted on. It names no internals
        because the endpoint returns none.
        """
        detail = self._detail(exc)
        if exc.code in (401, 403):
            return (
                f"The investment API refused the read ({exc.code}). The "
                "configured INVESTMENT_API_KEY is not accepted. Nothing was read."
            )
        if exc.code == 404:
            return (
                f"The investment API has no {PATH_TEST_PAYING_PAGE} (404). "
                "Either INVESTMENT_API_URL points at the public instance — this "
                "endpoint is registered on the admin one only — or the admin "
                "instance is running code from before it existed and has not "
                "been restarted. Nothing was read."
            )
        if detail:
            return detail
        return f"The investment API returned HTTP {exc.code}. Nothing was read."

    @staticmethod
    def _detail(exc: urllib.error.HTTPError) -> str | None:
        try:
            payload = json.loads(exc.read())
        except (ValueError, OSError):
            return None
        detail = payload.get("detail") if isinstance(payload, dict) else None
        return detail if isinstance(detail, str) and detail.strip() else None

    # -- the one read ------------------------------------------------------

    def fetch_page(self, path: Any) -> dict[str, Any]:
        """Fetch one page as the test paying user and return what it answered."""
        try:
            requested = normalise_path(path)
        except PublicPageError as exc:
            # One notion of "a path, not a URL", shared with the anonymous
            # fetch. A second copy of those rules is how the two tools come to
            # accept different things.
            raise PayingPageError(str(exc)) from exc

        query = urllib.parse.urlencode({"path": requested})
        payload = self._transport(
            f"{self.api_url}{PATH_TEST_PAYING_PAGE}?{query}"
        )
        if not isinstance(payload, dict):
            raise PayingPageError(
                f"The investment API answered with {type(payload).__name__}, "
                "not an object."
            )
        return {field: payload.get(field) for field in RESULT_FIELDS}
