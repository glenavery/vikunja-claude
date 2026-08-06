"""Minimal Vikunja API client and ticket lookup.

Only the handful of calls this launcher needs. The HTTP transport is injectable
so tests can exercise lookup and error handling without a live Vikunja.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .html_text import html_to_text

# A ticket number is the "#NN" prefix of the Vikunja task title.
TICKET_RE = re.compile(r"^\s*#(\d+)(?:\b|\s|$)")

# What a comment looks like once it leaves this module. Named here so the three
# read surfaces -- `vkctl.py show`, the /task page, and the MCP's get_task --
# agree on the fields rather than each deriving its own from the raw API row.
# It is a projection for the same reason the MCP boundary projects everything
# else: a field Vikunja adds upstream is not published to a caller by accident.
COMMENT_FIELDS = ("id", "author", "created", "text")


def comment_view(row: dict[str, Any]) -> dict[str, Any]:
    """One raw comment row, reduced to the published shape.

    `text` is flattened out of the editor's HTML. Callers that render into a
    page must still escape it -- this makes the body readable, it does not make
    it safe to interpolate.
    """
    return {
        "id": row.get("id"),
        "author": (row.get("author") or {}).get("username"),
        "created": row.get("created"),
        "text": html_to_text(row.get("comment") or ""),
    }

# Vikunja pages tasks *inside* each kanban bucket, and the page size is its own:
# `per_page` is accepted and ignored on this endpoint. Measured against the live
# board, a bucket holding 170 tasks answers with 50 and reports `count: 170`.
# So one request is never a listing -- it is the first page of every bucket.
KANBAN_BUCKET_PAGE_SIZE = 50

# Vikunja's filter language. Server-side so a board with a long Done column is
# not walked page by page only to be discarded; every row is still checked
# client-side, so a Vikunja that ignored this would be slower and not wronger.
OPEN_TASKS_FILTER = "done = false"

# A bound on paging, not a limit on the answer: at 50 tasks a page this is
# 50,000 tasks, and reaching it means something is looping rather than that a
# board is large. It raises rather than returning what it has.
MAX_VIEW_PAGES = 1000

Transport = Callable[[str, str, dict[str, Any] | None], Any]


class VikunjaError(RuntimeError):
    """Any failure talking to Vikunja."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class DescriptionLost(VikunjaError):
    """A write shortened or emptied a description it was not meant to touch.

    Raised after the fact -- the damage is already in the database when this
    fires. It exists so the damage is never silent: an unnoticed wipe is only
    recoverable until vacuum reclaims the dead TOAST chunks.
    """


class TicketNotFound(VikunjaError):
    pass


class AmbiguousTicket(VikunjaError):
    """More than one task carries the same #NN prefix."""


@dataclass(frozen=True)
class Ticket:
    """A Vikunja task.

    Identity is ``task_id`` — immutable, assigned by Vikunja. ``number`` is the
    editable ``#NN`` prefix humans use; it may be absent, and it is only ever
    used for display and for the commit reference.
    """

    task_id: int
    title: str
    description_html: str
    bucket_id: int | None
    bucket_title: str | None
    done: bool
    created: str
    number: int | None = None
    labels: list[str] = field(default_factory=list)
    #: Vikunja's 0-5 scale, where 0 is "unset" and 5 is the most urgent. Kept as
    #: the number Vikunja stores; naming it is presentation, and lives above.
    priority: int = 0
    updated: str = ""

    @property
    def description(self) -> str:
        return html_to_text(self.description_html)

    @property
    def summary(self) -> str:
        """Title with the #NN prefix stripped."""
        return TICKET_RE.sub("", self.title).strip()

    @property
    def reference(self) -> str:
        """How a human refers to this ticket."""
        return f"#{self.number}" if self.number is not None else f"task {self.task_id}"

    @property
    def commit_ref(self) -> str:
        """What a commit message should carry to link back to the board."""
        if self.number is not None:
            return f"(#{self.number})"
        return f"(vikunja task {self.task_id})"

    def url(self, frontend_url: str) -> str:
        # /tasks/:id is Vikunja's task.detail route. /projects/:id/:viewId is a
        # board view — never a task.
        return f"{frontend_url.rstrip('/')}/tasks/{self.task_id}"


def ticket_number(title: str) -> int | None:
    """Return the #NN ticket number in a title, or None if it has no prefix."""
    match = TICKET_RE.match(title or "")
    return int(match.group(1)) if match else None


class VikunjaClient:
    def __init__(
        self,
        api_url: str,
        token: str,
        transport: Transport | None = None,
        timeout: int = 15,
    ):
        self.api_url = api_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._transport = transport or self._http
        self._project_id: int | None = None
        self._kanban_view_id: int | None = None

    # -- transport ---------------------------------------------------------

    def _http(self, method: str, path: str, body: dict[str, Any] | None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.api_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise VikunjaError(
                f"Vikunja returned HTTP {exc.code} for {method} {path}: {detail}",
                status=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise VikunjaError(
                f"Cannot reach Vikunja at {self.api_url}: {exc.reason}"
            ) from exc

        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VikunjaError(
                f"Vikunja sent a non-JSON response for {method} {path}"
            ) from exc

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._transport(method, path, body)

    # -- project / view resolution ----------------------------------------

    def project_id(self, title: str, override: int | None = None) -> int:
        if override is not None:
            return override
        if self._project_id is None:
            projects = self.call("GET", "/projects") or []
            matches = [p for p in projects if p.get("title") == title]
            if not matches:
                known = ", ".join(sorted(p.get("title", "?") for p in projects))
                raise VikunjaError(
                    f"No Vikunja project titled {title!r}. Known projects: {known}"
                )
            self._project_id = int(matches[0]["id"])
        return self._project_id

    def kanban_view_id(self, project_id: int) -> int:
        if self._kanban_view_id is None:
            project = self.call("GET", f"/projects/{project_id}") or {}
            views = project.get("views") or []
            kanban = [v for v in views if v.get("view_kind") == "kanban"]
            if not kanban:
                raise VikunjaError(f"Project {project_id} has no kanban view")
            self._kanban_view_id = int(kanban[0]["id"])
        return self._kanban_view_id

    # -- tickets -----------------------------------------------------------

    @staticmethod
    def _ticket(bucket: dict[str, Any], task: dict[str, Any]) -> Ticket:
        """One task as the board's kanban view reports it, tagged with its bucket."""
        # A missing #NN prefix is fine: identity is the task id.
        return Ticket(
            number=ticket_number(task.get("title", "")),
            task_id=int(task["id"]),
            title=task.get("title", ""),
            description_html=task.get("description") or "",
            bucket_id=int(bucket["id"]),
            bucket_title=bucket.get("title"),
            done=bool(task.get("done")),
            created=task.get("created") or "",
            labels=[label.get("title", "") for label in (task.get("labels") or [])],
            priority=int(task.get("priority") or 0),
            updated=task.get("updated") or "",
        )

    def _view_page(
        self, project_id: int, view_id: int, page: int, task_filter: str | None
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"page": page}
        if task_filter:
            query["filter"] = task_filter
        path = (
            f"/projects/{project_id}/views/{view_id}/tasks"
            f"?{urllib.parse.urlencode(query)}"
        )
        buckets = self.call("GET", path) or []
        if not isinstance(buckets, list):
            raise VikunjaError(f"GET {path} did not return a list of buckets")
        return buckets

    def _walk_view(
        self, project_id: int, view_id: int, task_filter: str | None
    ) -> list[Ticket]:
        """Every task the view returns for ``task_filter`` -- all of them, or an error.

        Paging is walked to exhaustion and then *checked*: Vikunja reports each
        bucket's true total alongside the page it served, so a short read is
        detectable, and a read that quietly dropped a ticket is the one failure
        these lookups must never have. It raises instead.

        The check counts every task a page carried, before any caller-side
        filtering, because the totals are counts of what the *server* matched --
        comparing them against what a second, client-side filter left would
        report a shortfall whenever the server ignored the filter.
        """
        tickets: dict[int, Ticket] = {}
        delivered: dict[int, int] = {}
        totals: dict[int, tuple[str | None, int]] = {}

        for page in range(1, MAX_VIEW_PAGES + 1):
            buckets = self._view_page(project_id, view_id, page, task_filter)
            arrived = 0
            for bucket in buckets:
                bucket_id = int(bucket["id"])
                totals[bucket_id] = (
                    bucket.get("title"),
                    int(bucket.get("count") or 0),
                )
                tasks = bucket.get("tasks") or []
                delivered[bucket_id] = delivered.get(bucket_id, 0) + len(tasks)
                for task in tasks:
                    ticket = self._ticket(bucket, task)
                    if ticket.task_id in tickets:
                        continue
                    tickets[ticket.task_id] = ticket
                    arrived += 1
            # Pages are consecutive slices, so one that carries nothing new is
            # the end of every bucket at once.
            if arrived == 0:
                break
        else:
            raise VikunjaError(
                f"still receiving tasks after {MAX_VIEW_PAGES} pages of project "
                f"{project_id}: refusing to return a partial listing"
            )

        short = [
            f"{title!r} served {delivered.get(bucket_id, 0)} of {count}"
            for bucket_id, (title, count) in sorted(totals.items())
            if delivered.get(bucket_id, 0) < count
        ]
        if short:
            raise VikunjaError(
                "Vikunja served fewer tasks than it says the board holds, so "
                "this read would silently omit tickets: " + "; ".join(short)
            )
        return list(tickets.values())

    def list_all_tickets(self, project_id: int, view_id: int) -> list[Ticket]:
        """Every task on the board, open and done. Complete or an error.

        There is deliberately no "first page" variant of this. A partial board
        is what made :meth:`find_by_task_id` deny task 35, which exists.
        """
        return self._walk_view(project_id, view_id, None)

    def list_open_tickets(self, project_id: int, view_id: int) -> list[Ticket]:
        """Every task in the project that is not done. Complete or an error.

        ``done = false`` goes to the server so a long Done column is not walked
        only to be discarded, and the result is filtered again here -- so a
        Vikunja that ignored the filter would be slower and not wronger.
        """
        return [
            ticket
            for ticket in self._walk_view(project_id, view_id, OPEN_TASKS_FILTER)
            if not ticket.done
        ]

    def find_by_task_id(self, task_id: int, project_id: int, view_id: int) -> Ticket:
        """Canonical lookup: Vikunja's immutable task id.

        Asks the view for that one id, which is a constant-cost request and
        keeps the project boundary structural -- it is *this project's* view, so
        a task belonging to another project is not in the answer to begin with.
        Fetching `GET /tasks/{id}` instead would be one request too, but it
        serves any task in any project and reports `bucket_id: 0`, so it can
        answer neither "is this mine" nor "which column".

        A filtered miss is not an absence, so it falls back to the complete walk
        before saying "no such task". Note which failure that guards: a filter
        the server *ignores* is harmless, because the walk then covers the whole
        board anyway. The harmful one is a filter that is applied and matches
        nothing -- the reply is well-formed and internally consistent, and
        nothing in it distinguishes "missing" from "unmatched". Paying for a
        second read on the not-found path is worth it; the bug this replaced
        reported a task that exists as absent, and blamed the caller for
        confusing a task id with a view id.
        """
        for ticket in self._walk_view(project_id, view_id, f"id = {int(task_id)}"):
            if ticket.task_id == task_id:
                return ticket
        for ticket in self.list_all_tickets(project_id, view_id):
            if ticket.task_id == task_id:
                return ticket
        raise TicketNotFound(
            f"No task {task_id} in this project. (Vikunja task URLs look like "
            f"/tasks/{task_id}; /projects/N/M is a board view, not a task.)",
            status=404,
        )

    def find_ticket(self, number: int, project_id: int, view_id: int) -> Ticket:
        # The whole board, not a filtered slice: #NN is a title prefix rather
        # than a field, and detecting that two tasks claim the same one means
        # seeing all of them.
        matches = [
            t for t in self.list_all_tickets(project_id, view_id) if t.number == number
        ]
        if not matches:
            raise TicketNotFound(f"No ticket #{number} in this project", status=404)
        if len(matches) > 1:
            ids = ", ".join(str(t.task_id) for t in matches)
            raise AmbiguousTicket(
                f"#{number} matches {len(matches)} tasks (ids: {ids}). "
                "Ticket numbers must be unique.",
                status=409,
            )
        return matches[0]

    def oldest_ready_ticket(
        self, project_id: int, view_id: int, bucket_title: str = "Ready"
    ) -> Ticket:
        ready = [
            t
            for t in self.list_open_tickets(project_id, view_id)
            if t.bucket_title == bucket_title
        ]
        if not ready:
            raise TicketNotFound(f"No tickets in the {bucket_title} bucket", status=404)
        return sorted(ready, key=lambda t: (t.created, t.task_id))[0]

    # -- buckets -----------------------------------------------------------

    def _buckets(self, project_id: int, view_id: int) -> list[dict[str, Any]]:
        return self.call("GET", f"/projects/{project_id}/views/{view_id}/buckets") or []

    def bucket_titles(self, project_id: int, view_id: int) -> list[str]:
        """The board's columns, in board order. Read-only.

        Read from the board rather than inferred from the tasks that came back,
        so an empty column stays a real column: "no open tickets in Ready" and
        "there is no Ready" are different answers.
        """
        return [bucket.get("title", "") for bucket in self._buckets(project_id, view_id)]

    # -- mutations ---------------------------------------------------------

    def bucket_id_by_title(self, project_id: int, view_id: int, title: str) -> int:
        buckets = self._buckets(project_id, view_id)
        for bucket in buckets:
            if bucket.get("title") == title:
                return int(bucket["id"])
        known = ", ".join(sorted(b.get("title", "?") for b in buckets))
        raise VikunjaError(f"No bucket titled {title!r}. Buckets: {known}")

    def move_to_bucket(
        self, project_id: int, view_id: int, task_id: int, bucket_title: str
    ) -> int:
        bucket_id = self.bucket_id_by_title(project_id, view_id, bucket_title)
        self.call(
            "POST",
            f"/projects/{project_id}/views/{view_id}/buckets/{bucket_id}/tasks",
            {"task_id": task_id},
        )
        return bucket_id

    def add_comment(self, task_id: int, text: str) -> dict[str, Any]:
        """Append a comment. Additive -- it replaces nothing on the task.

        Returns what Vikunja stored, so a caller can report the comment's own
        id rather than only that the call did not raise.
        """
        created = self.call("PUT", f"/tasks/{task_id}/comments", {"comment": text})
        return created if isinstance(created, dict) else {}

    def list_comments(self, task_id: int) -> list[dict[str, Any]]:
        """A task's comments, oldest first. Read-only."""
        comments = self.call("GET", f"/tasks/{task_id}/comments") or []
        if not isinstance(comments, list):
            raise VikunjaError(f"GET /tasks/{task_id}/comments did not return a list")
        return comments

    def comment_views(self, task_id: int) -> list[dict[str, Any]]:
        """A task's comments, projected, oldest first.

        The form every read surface should use. `list_comments` returns raw API
        rows and stays available for a caller that genuinely needs one -- the
        idempotency check in `mcp_service` does -- but a surface that *shows*
        comments wants them projected, and there is one definition of that.
        """
        return [comment_view(row) for row in self.list_comments(task_id)]

    # -- task mutation -----------------------------------------------------
    #
    # POST /tasks/{id} is a REPLACE, not a patch: every field absent from the
    # body is set to its zero value. A body of {"done": true} therefore closes
    # the ticket and blanks its description. That has destroyed three ticket
    # descriptions (tasks 5 and 9 on 2026-07-26, task 46 on 2026-07-27), twice
    # in sessions where the hazard was known and documented -- because the
    # mistake is made while thinking about the ticket's content, not the API.
    #
    # So there is exactly one way to change a task here, it reads before it
    # writes, and it checks afterwards. Do not add a second one, and do not
    # call POST /tasks/{id} directly.

    def update_task(
        self,
        task_id: int,
        mutate: Callable[[dict[str, Any]], None],
        *,
        description_may_change: bool = False,
    ) -> dict[str, Any]:
        """Read the whole task, apply ``mutate``, write the whole task back.

        ``mutate`` receives the full task dict and edits it in place. The
        description length is compared before and after; unless the caller says
        it is changing the description, a change means fields were dropped and
        :class:`DescriptionLost` is raised.
        """
        task = self.call("GET", f"/tasks/{task_id}")
        if not isinstance(task, dict):
            raise VikunjaError(f"GET /tasks/{task_id} did not return a task")
        before = len(task.get("description") or "")

        mutate(task)

        updated = self.call("POST", f"/tasks/{task_id}", task) or {}
        after = len(updated.get("description") or "")

        if not description_may_change and after != before:
            raise DescriptionLost(
                f"task {task_id}: description went from {before} to {after} chars "
                f"during a write that must not have touched it. The old value is "
                f"probably still recoverable from orphaned TOAST chunks -- see "
                f"/home/glen/stacks/vikunja/recovery-tools/ and act before vacuum."
            )
        return updated

    def close_task(self, task_id: int) -> dict[str, Any]:
        """Mark a task done, leaving every other field as it was."""

        def mutate(task: dict[str, Any]) -> None:
            task["done"] = True

        return self.update_task(task_id, mutate)

    def set_description(self, task_id: int, html: str) -> dict[str, Any]:
        """Replace a task's description, and verify the server stored it."""
        if not html.strip():
            raise VikunjaError("refusing to set an empty description")

        def mutate(task: dict[str, Any]) -> None:
            task["description"] = html

        updated = self.update_task(task_id, mutate, description_may_change=True)
        stored = updated.get("description") or ""
        if stored.strip() != html.strip():
            raise VikunjaError(
                f"task {task_id}: the stored description differs from what was sent "
                f"({len(stored)} vs {len(html)} chars). Vikunja may have rewritten "
                f"the markup; inspect it before assuming the write was clean."
            )
        return updated

    def set_task_fields(
        self,
        task_id: int,
        *,
        title: str | None = None,
        description_html: str | None = None,
    ) -> dict[str, Any]:
        """Replace a task's title, description, or both, and verify the result.

        Built on :meth:`update_task`, so it is the same read-modify-write and
        not a second way to change a task: a field left as ``None`` is carried
        across from what was read rather than sent as a zero value.

        One call for both fields on purpose. Two writes would leave a window in
        which the task carries the new title and the old description, and the
        caller is approving one change, not two.
        """
        if title is None and description_html is None:
            raise VikunjaError("set_task_fields was given nothing to change")
        if title is not None and not title.strip():
            raise VikunjaError("refusing to set an empty title")
        if description_html is not None and not description_html.strip():
            raise VikunjaError("refusing to set an empty description")

        def mutate(task: dict[str, Any]) -> None:
            if title is not None:
                task["title"] = title
            if description_html is not None:
                task["description"] = description_html

        updated = self.update_task(
            task_id, mutate, description_may_change=description_html is not None
        )

        # Read back what the server kept. Vikunja sanitises the description
        # markup, so a write that "succeeded" can still not hold what was sent.
        if title is not None and (updated.get("title") or "") != title:
            raise VikunjaError(
                f"task {task_id}: the stored title differs from what was sent "
                f"({(updated.get('title') or '')!r} vs {title!r})."
            )
        stored = updated.get("description") or ""
        if description_html is not None and stored.strip() != description_html.strip():
            raise VikunjaError(
                f"task {task_id}: the stored description differs from what was sent "
                f"({len(stored)} vs {len(description_html)} chars). Vikunja may have "
                f"rewritten the markup; inspect it before assuming the write was clean."
            )
        return updated

    def create_task(
        self, project_id: int, title: str, description: str = ""
    ) -> dict[str, Any]:
        """Create a task. PUT on the project is a create and replaces nothing."""
        body: dict[str, Any] = {"title": title}
        if description:
            body["description"] = description
        created = self.call("PUT", f"/projects/{project_id}/tasks", body) or {}
        if not created.get("id"):
            raise VikunjaError(f"create returned no task id: {created!r}")
        return created
