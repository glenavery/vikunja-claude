"""The operations the MCP boundary exposes, and the rules around them.

The whole surface is here: two reads and one create. Nothing in this module can
edit, close, delete, comment on or move an existing task, and it reaches Vikunja
only through :class:`~vikunja_claude.vikunja.VikunjaClient` methods that cannot
do those things either.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from datetime import datetime, timezone
from typing import Any

from .config import McpConfig
from .html_text import html_to_text, text_to_html
from .mcp import Tool, ToolError
from .vikunja import VikunjaClient, VikunjaError

MAX_TITLE_CHARS = 250
MAX_DESCRIPTION_CHARS = 20000

#: Vikunja stores priority as 0-5. The number is what it holds; these are what a
#: reader means by it. Naming is the only thing added here -- the ordering is
#: the stored number's, not this table's.
PRIORITY_NAMES = {
    0: "unset",
    1: "low",
    2: "medium",
    3: "high",
    4: "urgent",
    5: "do now",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def idempotency_key(project_id: int, title: str, description: str) -> str:
    """Identity of a creation *request*, so a retry is recognisable as one.

    Derived from the content rather than from a caller-supplied id: a client
    retrying a tool call resends the same arguments, but nothing obliges it to
    resend the same request id, and the one thing it cannot vary while still
    meaning "the same ticket" is the ticket.
    """
    raw = "\0".join([str(project_id), title.strip(), description.strip()])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class McpService:
    def __init__(self, config: McpConfig, client: VikunjaClient):
        self.config = config
        self.client = client
        # One process, one port, one unit — so a lock is enough to make
        # check-then-create atomic against a concurrent retry.
        self._create_lock = threading.Lock()

    # -- resolution --------------------------------------------------------

    def allowed_project_id(self) -> int:
        """The one project this boundary may write to, or an explicit failure."""
        try:
            return self.client.project_id(
                self.config.project_title, self.config.project_id
            )
        except VikunjaError as exc:
            raise ToolError(
                f"Cannot resolve the project {self.config.project_title!r}: {exc}"
            ) from exc

    def _ids(self) -> tuple[int, int]:
        project_id = self.allowed_project_id()
        try:
            return project_id, self.client.kanban_view_id(project_id)
        except VikunjaError as exc:
            raise ToolError(str(exc)) from exc

    # -- read --------------------------------------------------------------

    def get_task(self, task_id: int) -> dict[str, Any]:
        project_id, view_id = self._ids()
        try:
            ticket = self.client.find_by_task_id(task_id, project_id, view_id)
            comments = self.client.list_comments(task_id)
        except VikunjaError as exc:
            raise ToolError(str(exc)) from exc

        return {
            "task_id": ticket.task_id,
            "ticket": ticket.number,
            "reference": ticket.reference,
            "title": ticket.title,
            "summary": ticket.summary,
            "description": ticket.description,
            "status": "done" if ticket.done else "open",
            "bucket": ticket.bucket_title,
            "labels": ticket.labels,
            "created": ticket.created,
            "project": self.config.project_title,
            "project_id": project_id,
            "url": ticket.url(self.config.frontend_url),
            "comments": [
                {
                    "id": comment.get("id"),
                    "author": (comment.get("author") or {}).get("username"),
                    "created": comment.get("created"),
                    "text": html_to_text(comment.get("comment") or ""),
                }
                for comment in comments
            ],
        }

    def list_open_tasks(
        self, bucket: str | None = None, label: str | None = None
    ) -> dict[str, Any]:
        """Every task on the board that is not done, in one answer.

        There is no limit argument and no default page size. The whole point of
        the action is "what is still open", and an answer that silently stopped
        at fifty would be indistinguishable from a board with fifty things left.
        """
        project_id, view_id = self._ids()
        wanted_bucket = (bucket or "").strip()
        wanted_label = (label or "").strip()

        try:
            tickets = self.client.list_open_tickets(project_id, view_id)
            known_buckets = (
                self.client.bucket_titles(project_id, view_id)
                if wanted_bucket
                else []
            )
        except VikunjaError as exc:
            raise ToolError(str(exc)) from exc

        if wanted_bucket:
            match = [t for t in known_buckets if t.casefold() == wanted_bucket.casefold()]
            if not match:
                # Refused rather than answered with an empty list: "no open
                # tickets in Redy" is a wrong answer to a mistyped question.
                raise ToolError(
                    f"There is no bucket named {wanted_bucket!r} on the "
                    f"{self.config.project_title} board. Buckets: "
                    + ", ".join(known_buckets)
                )
            wanted_bucket = match[0]
            tickets = [t for t in tickets if t.bucket_title == wanted_bucket]

        labels_in_use = sorted(
            {name for ticket in tickets for name in ticket.labels if name}
        )
        if wanted_label:
            # An unused label is a real observation, not a mistake, so this
            # narrows to nothing rather than refusing -- and the labels that are
            # in use come back regardless, so a typo is still recognisable.
            tickets = [
                t
                for t in tickets
                if any(name.casefold() == wanted_label.casefold() for name in t.labels)
            ]

        # Most urgent first, then by task id. Priority alone is not an order --
        # most of the board sits at 0 -- so the id is what makes it total, and
        # it is stable across calls in a way that position or title is not.
        tickets.sort(key=lambda t: (-t.priority, t.task_id))

        return {
            "project": self.config.project_title,
            "project_id": project_id,
            "count": len(tickets),
            "filters": {"bucket": wanted_bucket or None, "label": wanted_label or None},
            "labels_in_use": labels_in_use,
            "tasks": [
                {
                    "task_id": ticket.task_id,
                    "ticket": ticket.number,
                    "reference": ticket.reference,
                    "title": ticket.title,
                    "summary": ticket.summary,
                    "status": "open",
                    "bucket": ticket.bucket_title,
                    "priority": ticket.priority,
                    "priority_label": PRIORITY_NAMES.get(ticket.priority, "unknown"),
                    "labels": ticket.labels,
                    "created": ticket.created,
                    "updated": ticket.updated,
                    "url": ticket.url(self.config.frontend_url),
                }
                for ticket in tickets
            ],
        }

    # -- create ------------------------------------------------------------

    def _ledger(self) -> dict[str, dict[str, Any]]:
        path = self.config.ledger_path
        if not path.is_file():
            return {}
        records: dict[str, dict[str, Any]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = record.get("key")
            if key:
                records[key] = record
        return records

    def _record(self, record: dict[str, Any]) -> None:
        path = self.config.ledger_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
        print(
            f"mcp: created task {record['task_id']} in project "
            f"{record['project_id']} — {record['title']!r}",
            file=sys.stderr,
            flush=True,
        )

    def create_task(
        self, project_id: int, title: str, description: str
    ) -> dict[str, Any]:
        title = (title or "").strip()
        description = (description or "").strip()
        if not title:
            raise ToolError("title is required and cannot be blank")
        if not description:
            raise ToolError(
                "description is required and cannot be blank: a ticket with no "
                "description is not a ticket anyone can work"
            )
        if len(title) > MAX_TITLE_CHARS:
            raise ToolError(f"title is longer than {MAX_TITLE_CHARS} characters")
        if len(description) > MAX_DESCRIPTION_CHARS:
            raise ToolError(
                f"description is longer than {MAX_DESCRIPTION_CHARS} characters"
            )

        allowed = self.allowed_project_id()
        if int(project_id) != allowed:
            # Named, not substituted. Creating the ticket somewhere else would
            # be the failure this refusal exists to prevent.
            raise ToolError(
                f"Refusing to create a task in project {project_id}: this "
                f"connection may only create tasks in "
                f"{self.config.project_title!r} (project {allowed}). Nothing "
                "was created."
            )

        key = idempotency_key(allowed, title, description)
        with self._create_lock:
            existing = self._ledger().get(key)
            if existing is not None:
                return {
                    "created": False,
                    "reason": "an identical task was already created through "
                    "this connection; returning it instead of creating a second",
                    "task_id": existing["task_id"],
                    "title": existing["title"],
                    "project_id": existing["project_id"],
                    "url": existing["url"],
                    "created_at": existing["created_at"],
                }

            try:
                created = self.client.create_task(
                    allowed, title, text_to_html(description)
                )
            except VikunjaError as exc:
                raise ToolError(f"Vikunja refused the create: {exc}") from exc

            task_id = int(created["id"])
            url = f"{self.config.frontend_url.rstrip('/')}/tasks/{task_id}"
            record = {
                "created_at": _now(),
                "key": key,
                "project_id": allowed,
                "task_id": task_id,
                "title": title,
                "url": url,
            }
            self._record(record)

        return {
            "created": True,
            "task_id": task_id,
            "title": title,
            "project": self.config.project_title,
            "project_id": allowed,
            "url": url,
        }

    # -- tools -------------------------------------------------------------

    def tools(self) -> list[Tool]:
        """The complete, fixed set of operations this connection can perform."""
        return [
            Tool(
                name="get_task",
                title="Get a Vikunja task",
                description=(
                    "Read one task from the "
                    f"{self.config.project_title} board by its Vikunja task id "
                    "— the number in a /tasks/<id> URL, not the #NN prefix in "
                    "the title and not a /projects/<id>/<viewId> board view. "
                    "Returns the title, full description, status, bucket, "
                    "labels, timestamps and comments. Read-only."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "integer",
                            "description": "Vikunja's immutable task id.",
                        }
                    },
                    "required": ["task_id"],
                    "additionalProperties": False,
                },
                annotations={
                    "readOnlyHint": True,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                run=lambda arguments: self.get_task(int(arguments["task_id"])),
            ),
            Tool(
                name="list_open_tasks",
                title="List the open Vikunja tasks",
                description=(
                    "List every task that is not done on the "
                    f"{self.config.project_title} board — the whole open queue, "
                    "not a page of it. Use this for board-level questions such "
                    "as 'what is open', 'what is in Ready' or 'what is left to "
                    "do'. Returns each task's id, title, bucket, priority, "
                    "labels and timestamps, ordered most urgent first and then "
                    "by id. It does not return descriptions or comments: read "
                    "one task with get_task once you know which id you want. "
                    "Completed tasks are never included. Read-only."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "bucket": {
                            "type": "string",
                            "description": (
                                "Optional board column to narrow to, e.g. "
                                "'Ready'. Omit for every open task. A name that "
                                "is not a column on this board is refused "
                                "rather than answered with an empty list."
                            ),
                        },
                        "label": {
                            "type": "string",
                            "description": (
                                "Optional label to narrow to. Omit for every "
                                "open task."
                            ),
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                annotations={
                    "readOnlyHint": True,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                run=lambda arguments: self.list_open_tasks(
                    bucket=(
                        None
                        if arguments.get("bucket") is None
                        else str(arguments["bucket"])
                    ),
                    label=(
                        None
                        if arguments.get("label") is None
                        else str(arguments["label"])
                    ),
                ),
            ),
            Tool(
                name="create_task",
                title="Create a Vikunja task",
                description=(
                    "Create one new task on the "
                    f"{self.config.project_title} board. Only call this when "
                    "the user has explicitly asked for a ticket to be created, "
                    "and only after showing them the exact title and "
                    "description you are about to submit — this writes to a "
                    "real board. The description is plain text; blank lines "
                    "separate paragraphs. Creating the same title and "
                    "description twice returns the first task rather than "
                    "creating a duplicate. Cannot modify any existing task."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "integer",
                            "description": (
                                "The target project id. Must be the "
                                f"{self.config.project_title} project; any "
                                "other value is refused rather than redirected."
                            ),
                        },
                        "title": {
                            "type": "string",
                            "description": "One line, as the user approved it.",
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "Plain text body, as the user approved it."
                            ),
                        },
                    },
                    "required": ["project_id", "title", "description"],
                    "additionalProperties": False,
                },
                annotations={
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                run=lambda arguments: self.create_task(
                    int(arguments["project_id"]),
                    str(arguments["title"]),
                    str(arguments["description"]),
                ),
            ),
        ]
