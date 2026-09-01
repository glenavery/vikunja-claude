"""One request to the ticket runner's own work route, and nothing learned.

Task 726: the MCP exposes starting a ticket through the runner as
``start_task_run``. This module is the *only* runner knowledge the MCP side
carries — one HTTP verb, the URL shape of the route the runner's launcher
already builds for its own browser button, and the fact that a refused or
unreachable request means nothing was launched. Everything the request *starts*
happens over there: the task lookup, the move to In Progress, the prompt,
the executor and model selection (``deploy/ollama/models.json`` is read by
the runner, not by this side), the per-task lock, the worktree, the tests,
the commit. So task 726's criterion "changing the approved ``local_coding``
seat requires no MCP change" holds by construction — there is no seat, model
string or context number in this package to change (task 690 put those where
they belong, in the runner).

There is no second runner here. No queue, no lock, no scheduler, no model
launch: the runner's per-task lock is the one that decides whether a run
starts concurrently with anything else, and its refusal — already running,
unknown executor, no such ticket — is surfaced to the MCP caller *verbatim*
rather than re-described or fallen back from. A boundary that re-judged any of
that would be a second boundary that can disagree with the one that matters,
which is the same rule :mod:`vikunja_claude.investment` is built on.

Transport follows that module's seam: anything callable as ``(path) -> dict``
stands in for the HTTP round trip, and a test then asserts the exact path —
``/task/<id>/work`` with or without ``?executor=<name>`` — rather than a URL
assembly it has to reconstruct.

The row id in that path is the one place it is used, and it is used the way
task 659 left it: internally, where it is load-bearing. It is what makes the
address unambiguous — Vikunja hands ids out globally, so a task the runner does
not work cannot resolve to a *different* real ticket the way a board number
could. Nothing it addresses is published: the id is not in what this returns,
and the caller of this module frames the runner's lookup failures itself rather
than republishing text that spells it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

#: An idempotent launch request that wedges is a hang, not a retry. Generous
#: enough not to fail on a loaded host, bounded so a wedged runner cannot hold
#: a conversation open.
DEFAULT_TIMEOUT_SECONDS = 30

Transport = Callable[[str], Any]


class RunnerError(RuntimeError):
    """The runner refused the run, or could not be reached.

    Either way the MCP caller hears it as an error, and when the runner
    answered, its own words travel with it — a boundary that re-described a
    refusal in its own phrasing would be a second opinion on a decision that
    is the runner's.

    ``status`` is the HTTP status the runner answered with, or None when it
    never answered at all. It is carried because the two are different facts —
    "the runner said no" and "the runner was not there" — and because one
    status is special: the runner addresses tasks internally by their row id,
    so its *lookup* failures spell that id, and the boundary re-frames those in
    its own words rather than republishing it (task 663).
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RunnerClient:
    """Talks one request to the runner's work route at ``runner_url``.

    There is one runner, and the loopback bind it is checked for is the
    launcher's (``build_server`` refuses a non-loopback host), so the base
    URL here is the plain host:port of the same machine. No credential:
    like the page fetch, a request the runner refuses because it is someone
    else's arrives as the runner's own refusal, which is the whole of the
    protection this route needs.
    """

    def __init__(
        self,
        runner_url: str,
        transport: Transport | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.runner_url = runner_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport or self._post

    def start_run(self, task_id: int, executor: str | None = None) -> dict:
        """Ask the runner to start a run for the row with this id.

        ``task_id`` is the row's immutable Vikunja id — the same id the
        ticket reads carry — not the board number, because the runner's
        work route is addressed by id, exactly the way the launcher's own
        button calls it. ``executor`` is forwarded *as a name*: the runner
        is the one that knows what a name means and what the available ones
        are, and an unknown one is its to refuse (a 400 here), not this
        side's to validate or translate.
        """
        path = f"/task/{int(task_id)}/work"
        if executor:
            path += f"?executor={urllib.parse.quote(str(executor))}"
        result = self._transport(path)
        if not isinstance(result, dict):
            raise RunnerError(
                f"The ticket runner answered with {type(result).__name__}, "
                "not an object. Nothing was launched."
            )
        return result

    def _post(self, path: str) -> dict:
        request = urllib.request.Request(
            self.runner_url + path,
            method="POST",
            headers={"Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise RunnerError(self._explain_refusal(exc), status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise RunnerError(
                f"Cannot reach the ticket runner at {self.runner_url}: "
                f"{exc.reason}. Nothing was launched and the ticket was not "
                "moved. Check that the vikunja-claude launcher service is "
                "running."
            ) from exc
        except TimeoutError as exc:
            raise RunnerError(
                f"The ticket runner did not answer within {self._timeout}s. "
                "Nothing was launched and the ticket was not moved."
            ) from exc

        if not raw:
            raise RunnerError(
                "The ticket runner sent an empty body. Nothing was launched."
            )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RunnerError(
                "The ticket runner sent a non-JSON response. Nothing was "
                "launched."
            ) from exc

    def _explain_refusal(self, exc: urllib.error.HTTPError) -> str:
        """The runner's refusal, with its own reason when it gave one.

        The runner answers its refusals as ``{"error": "..."}`` — written for
        a caller to act on ("Claude is already working on it", "Unknown
        executor '…'. Available: …"). Withholding those would turn a precise
        refusal into "HTTP 409", which tells the caller nothing it can fix.
        Reading the body is best-effort, as in :mod:`vikunja_claude.investment`:
        a parse failure must not replace the HTTP failure with a parsing one.
        """
        detail = _error_phrase(exc)
        if detail:
            return f"The ticket runner refused the run ({exc.code}): {detail}"
        return (
            f"The ticket runner refused the run ({exc.code}) without a "
            "reason. Nothing was launched and the ticket was not moved."
        )


def _error_phrase(exc: urllib.error.HTTPError) -> str | None:
    """The runner's own refusal text, if it sent one.

    Only a runner error body — ``{"error": "..."}`` with a non-empty string —
    counts. A prose error page from a proxy, an empty body or a body that is
    not this shape returns None and the caller says the generic thing.
    """
    try:
        payload = json.loads(exc.read() or b"")
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    detail = payload.get("error")
    if not isinstance(detail, str) or not detail.strip():
        return None
    return detail.strip()
