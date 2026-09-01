"""Two requests to the ticket runner's own routes, and nothing learned.

Task 726: the MCP exposes starting a ticket through the runner as
``start_task_run``. Task 751 adds the one question that could not be asked
afterwards — *what is that run doing* — as a GET beside it, because a launch
that had gone quiet was indistinguishable from one still working. This module
is the *only* runner knowledge the MCP side carries — two routes on the
runner's own launcher, the URL shape it already builds for its browser button,
and the fact that a refused or unreachable request means nothing happened.

The second route reads; it does not control. There is no pause, kill, retry or
resume here, and adding one would make this a second place deciding what a run
does, which is the thing the rest of this docstring exists to prevent.

Everything the launch request *starts* happens over there: the task lookup,
the move to In Progress, the prompt,
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

#: The verb travels with the path, so a stand-in for this transport can tell
#: the two requests apart. It has to: one starts an agent editing a repository
#: and the other only reads what a run is doing, and a seam that could not
#: distinguish them could not prove the read path starts nothing.
Transport = Callable[[str, str], Any]


#: What a failed request left behind, said in the terms of what it was for. A
#: read that fails changed nothing by construction, and saying "nothing was
#: launched" of it would describe a launch nobody asked for.
LAUNCH = "Nothing was launched and the ticket was not moved."
READ = "No run status was read; nothing was changed."


def _outcome(method: str) -> str:
    return LAUNCH if method == "POST" else READ


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
        self._transport = transport or self._request

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
        return self._object(self._transport(path, "POST"), LAUNCH)

    def run_status(self, task_id: int) -> dict:
        """What the runner says the last run for this row is doing.

        A GET, and the only other request this side makes. It asks the runner
        the question rather than reading its state directory, for the reason
        every other line in this module exists: the runner owns what a run is,
        and a second reader of its lock files and logs would be a second
        opinion on that, out of step the day either changes. Nothing here
        interprets the answer either — ``running``, ``lost`` and the rest are
        the runner's words, not a state machine kept on this side.

        It starts nothing, and there is no counterpart to it: no pause, no
        kill, no retry, no resume. Reading is the whole of what it does.
        """
        return self._object(self._transport(f"/task/{int(task_id)}/run", "GET"), READ)

    def _request(self, path: str, method: str) -> dict:
        request = urllib.request.Request(
            self.runner_url + path,
            method=method,
            headers={"Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise RunnerError(
                self._explain_refusal(exc, _outcome(method)), status=exc.code
            ) from exc
        except urllib.error.URLError as exc:
            raise RunnerError(
                f"Cannot reach the ticket runner at {self.runner_url}: "
                f"{exc.reason}. {_outcome(method)} Check that the "
                "vikunja-claude launcher service is running."
            ) from exc
        except TimeoutError as exc:
            raise RunnerError(
                f"The ticket runner did not answer within {self._timeout}s. "
                f"{_outcome(method)}"
            ) from exc

        if not raw:
            raise RunnerError(
                f"The ticket runner sent an empty body. {_outcome(method)}"
            )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RunnerError(
                f"The ticket runner sent a non-JSON response. {_outcome(method)}"
            ) from exc

    @staticmethod
    def _object(result: Any, outcome: str) -> dict:
        if not isinstance(result, dict):
            raise RunnerError(
                f"The ticket runner answered with {type(result).__name__}, "
                f"not an object. {outcome}"
            )
        return result

    def _explain_refusal(self, exc: urllib.error.HTTPError, outcome: str) -> str:
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
            return f"The ticket runner refused the request ({exc.code}): {detail}"
        return (
            f"The ticket runner refused the request ({exc.code}) without a "
            f"reason. {outcome}"
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
