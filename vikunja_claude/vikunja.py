"""Minimal Vikunja API client and ticket lookup.

Only the handful of calls this launcher needs. The HTTP transport is injectable
so tests can exercise lookup and error handling without a live Vikunja.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .html_text import html_to_text

# A ticket number is the "#NN" prefix of the Vikunja task title.
TICKET_RE = re.compile(r"^\s*#(\d+)(?:\b|\s|$)")

Transport = Callable[[str, str, dict[str, Any] | None], Any]


class VikunjaError(RuntimeError):
    """Any failure talking to Vikunja."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class TicketNotFound(VikunjaError):
    pass


class AmbiguousTicket(VikunjaError):
    """More than one task carries the same #NN prefix."""


@dataclass(frozen=True)
class Ticket:
    number: int
    task_id: int
    title: str
    description_html: str
    bucket_id: int | None
    bucket_title: str | None
    done: bool
    created: str
    labels: list[str] = field(default_factory=list)

    @property
    def description(self) -> str:
        return html_to_text(self.description_html)

    @property
    def summary(self) -> str:
        """Title with the #NN prefix stripped."""
        return TICKET_RE.sub("", self.title).strip()

    def url(self, frontend_url: str) -> str:
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

    def list_tickets(self, project_id: int, view_id: int) -> list[Ticket]:
        """Every task in the kanban view, tagged with its bucket."""
        buckets = self.call(
            "GET", f"/projects/{project_id}/views/{view_id}/tasks?per_page=250"
        ) or []
        tickets: list[Ticket] = []
        for bucket in buckets:
            for task in bucket.get("tasks") or []:
                number = ticket_number(task.get("title", ""))
                if number is None:
                    continue
                tickets.append(
                    Ticket(
                        number=number,
                        task_id=int(task["id"]),
                        title=task.get("title", ""),
                        description_html=task.get("description") or "",
                        bucket_id=int(bucket["id"]),
                        bucket_title=bucket.get("title"),
                        done=bool(task.get("done")),
                        created=task.get("created") or "",
                        labels=[
                            label.get("title", "")
                            for label in (task.get("labels") or [])
                        ],
                    )
                )
        return tickets

    def find_ticket(self, number: int, project_id: int, view_id: int) -> Ticket:
        matches = [
            t for t in self.list_tickets(project_id, view_id) if t.number == number
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
            for t in self.list_tickets(project_id, view_id)
            if t.bucket_title == bucket_title and not t.done
        ]
        if not ready:
            raise TicketNotFound(f"No tickets in the {bucket_title} bucket", status=404)
        return sorted(ready, key=lambda t: (t.created, t.task_id))[0]

    # -- mutations ---------------------------------------------------------

    def bucket_id_by_title(self, project_id: int, view_id: int, title: str) -> int:
        buckets = self.call(
            "GET", f"/projects/{project_id}/views/{view_id}/buckets"
        ) or []
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

    def add_comment(self, task_id: int, text: str) -> None:
        self.call("PUT", f"/tasks/{task_id}/comments", {"comment": text})
