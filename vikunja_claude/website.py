"""One page of the public AI Server website, fetched as an anonymous visitor.

Task 204. Reviewing the site through this boundary rather than through a
browsing tool removes the dependency on external web browsing, which Cloudflare
and bot protection sit in front of; the request is made here, from the host the
application runs on, against the origin.

The rule the whole module is built around is that **this client carries no
credential**. Not a withheld one — an absent one. It is constructed with a base
URL and nothing else, there is no parameter through which a key, a cookie or an
``Authorization`` header could reach the request, and the transport it is handed
is only ever asked for GET. So "it must not bypass authentication or expose
protected pages" is not a list of paths maintained here and kept in step with the
application; it is arithmetic. A protected page answers this client exactly as it
answers a stranger, and what comes back is that answer — the 303 to the login
page, not the page behind it. The application decides what is public, in the one
place it already decides it.

Two further properties are structural rather than promised.

**The site cannot be changed by a caller.** The argument is a *path*: a value
that starts with ``/`` and carries no scheme, no authority and no backslash. The
host comes from configuration and from nowhere else, so no argument can aim this
at another site, at the admin instance, or at a link found on the page.

**Redirects are reported, not followed.** A visitor sent to ``/login`` has been
refused, and following that hop would replace the evidence of the refusal with a
page that looks like a successful fetch of the path that was asked for.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

#: A page is fetched to be read, not downloaded. Anything past this is cut and
#: said to have been cut — a truncated page that does not announce itself is
#: read as a page that ends there.
MAX_BODY_BYTES = 400_000

#: The application renders reports and cockpit pages that touch the database, so
#: this is not a static-file timeout. Bounded so a wedged worker cannot hold a
#: conversation open.
DEFAULT_TIMEOUT_SECONDS = 20

#: The longest path this will send. A URL longer than this is a mistake, and the
#: application would answer it with a 414 anyway.
MAX_PATH_CHARS = 2000

#: Body types that are text a model can read. Anything else (an image, a PDF, a
#: gzip) has its body omitted with a reason rather than being decoded into
#: nonsense — the status and headers are still worth having.
_TEXT_CONTENT_TYPES = frozenset(
    {
        "application/atom+xml",
        "application/javascript",
        "application/json",
        "application/rss+xml",
        "application/xhtml+xml",
        "application/xml",
        "image/svg+xml",
    }
)

#: Never returned. An anonymous fetch of a public page can still be handed a
#: fresh session cookie, and a session cookie is a credential regardless of who
#: it was minted for. The *names* are reported, because "this page sets a
#: session cookie" is a fact worth reviewing; the values are not.
_REDACTED_HEADER = "set-cookie"


class PublicPageError(RuntimeError):
    """The page could not be fetched, and why. Never a page."""


@dataclass(frozen=True)
class RawPage:
    """What the transport saw: an HTTP response, undecoded and unjudged."""

    status: int
    reason: str
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""


Transport = Callable[[str], RawPage]


def normalise_path(path: str) -> str:
    """The path this will request, or a refusal naming what is wrong with it.

    Everything refused here is refused because it would change *which server*
    is asked, or because it is not a thing an HTTP request can carry. This is
    not a list of pages that may be read: which pages may be read is the
    application's decision, and it makes it by answering.
    """
    if not isinstance(path, str):
        raise PublicPageError(
            f"path must be a string like \"/about\", not {type(path).__name__}."
        )
    value = path.strip()
    if not value:
        raise PublicPageError('path is empty. Use "/" for the home page.')
    if len(value) > MAX_PATH_CHARS:
        raise PublicPageError(
            f"path is {len(value)} characters; the limit is {MAX_PATH_CHARS}."
        )
    if any(character.isspace() or ord(character) < 0x20 for character in value):
        raise PublicPageError(
            "path contains whitespace or a control character. Percent-encode it "
            "(a space is %20)."
        )
    if "\\" in value:
        # A browser reads a backslash as a slash, urllib does not. `/\evil.com`
        # is a redirect trick, and it has no legitimate use in a path.
        raise PublicPageError("path may not contain a backslash.")
    if "#" in value:
        # A fragment is never sent to a server. Dropping it silently would
        # answer a question that was not asked.
        raise PublicPageError(
            "path may not contain a fragment (#…); a fragment never reaches the "
            "server, so it cannot be part of what is fetched."
        )
    if not value.startswith("/"):
        raise PublicPageError(
            f"path must start with \"/\" — it is a path on the configured site, "
            f"not a URL. Got {value!r}."
        )
    if value.startswith("//"):
        # `//example.com/x` is a protocol-relative URL: it would change the host.
        raise PublicPageError(
            "path may not start with \"//\" — that names another host, and this "
            "tool serves one configured site."
        )
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme or parsed.netloc:
        raise PublicPageError(
            "path may not carry a scheme or a host; it is a path on the "
            f"configured site. Got {value!r}."
        )
    return value


class PublicSiteClient:
    """Read one page of one configured site, with no credentials of any kind."""

    def __init__(
        self,
        site_url: str,
        transport: Transport | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.site_url = site_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport or self._get

    # -- transport ---------------------------------------------------------

    def _opener(self) -> urllib.request.OpenerDirector:
        """An opener that follows no redirect and keeps no cookie.

        Both matter. A followed redirect hides a refusal, and a cookie jar would
        turn a sequence of anonymous fetches into a session — which is the one
        thing an anonymous visitor is not.
        """
        return urllib.request.build_opener(_NoRedirects)

    def _get(self, url: str) -> RawPage:
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                # Said plainly, because a review request pretending to be a
                # browser is how a site's own logs stop being trustworthy.
                "User-Agent": "vikunja-claude-mcp/1.0 (page review; read-only)",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            },
        )
        try:
            response = self._opener().open(request, timeout=self._timeout)
        except urllib.error.HTTPError as exc:
            # A 3xx (redirects are not followed), 4xx or 5xx. Each is a real
            # answer from the application and is what a visitor would receive,
            # so it is returned rather than raised.
            response = exc
        except urllib.error.URLError as exc:
            raise PublicPageError(
                f"Cannot reach the site at {self.site_url}: {exc.reason}. Nothing "
                "was fetched, so nothing is known about what that page returns."
            ) from exc
        except TimeoutError as exc:
            raise PublicPageError(
                f"The site did not answer within {self._timeout}s. Nothing was "
                "fetched."
            ) from exc

        with response:
            # One byte past the cap, so "there was more" is observed rather than
            # inferred from a body that happens to land exactly on it.
            body = response.read(MAX_BODY_BYTES + 1)
        return RawPage(
            status=response.status,
            reason=response.reason or "",
            headers=list(response.headers.items()),
            body=body,
        )

    # -- the one read ------------------------------------------------------

    def fetch_page(self, path: str) -> dict:
        """Fetch one page and return its status, headers and body."""
        requested = normalise_path(path)
        url = self.site_url + requested
        page = self._transport(url)

        headers, cookie_names = _project_headers(page.headers)
        content_type = headers.get("content-type", "")
        truncated = len(page.body) > MAX_BODY_BYTES
        body = page.body[:MAX_BODY_BYTES] if truncated else page.body
        html, omitted = _decode(body, content_type)

        return {
            "url": url,
            "path": requested,
            "status": page.status,
            "status_text": page.reason,
            "headers": headers,
            "set_cookie_names": cookie_names,
            "html": html,
            "html_omitted_reason": omitted,
            "truncated": truncated,
            "bytes": len(page.body) if not truncated else None,
            "authenticated": False,
        }


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Returning None means "do not follow"; urllib then surfaces the 3xx."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _project_headers(items: list[tuple[str, str]]) -> tuple[dict[str, str], list[str]]:
    """Response headers, lower-cased, with cookie values held back.

    Repeated headers are joined rather than dropped, because two `link` or two
    `vary` headers are two facts and keeping only the last is a quiet edit of
    what the server said.
    """
    headers: dict[str, str] = {}
    cookie_names: list[str] = []
    for name, value in items:
        key = name.lower()
        if key == _REDACTED_HEADER:
            cookie_names.append(value.split("=", 1)[0].strip())
            continue
        headers[key] = f"{headers[key]}, {value}" if key in headers else value
    return headers, cookie_names


def _decode(body: bytes, content_type: str) -> tuple[str | None, str | None]:
    """The body as text, or None and the reason it is not being shown."""
    if not body:
        return "", None
    media_type, _, parameters = content_type.partition(";")
    media_type = media_type.strip().lower()
    if media_type and not (
        media_type.startswith("text/") or media_type in _TEXT_CONTENT_TYPES
    ):
        return None, (
            f"the response is {media_type}, which is not text; its status and "
            "headers are above."
        )
    charset = "utf-8"
    for parameter in parameters.split(";"):
        key, _, value = parameter.partition("=")
        if key.strip().lower() == "charset" and value.strip():
            charset = value.strip().strip('"')
    try:
        return body.decode(charset, errors="replace"), None
    except LookupError:
        return body.decode("utf-8", errors="replace"), None
