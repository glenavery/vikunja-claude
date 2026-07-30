"""Test doubles: an in-memory Vikunja and a spawn stub."""

from __future__ import annotations

import re
import urllib.parse
from copy import deepcopy
from typing import Any

from vikunja_claude.vikunja import KANBAN_BUCKET_PAGE_SIZE, VikunjaError

PROJECT_ID = 2
VIEW_ID = 12

BUCKETS = [
    {"id": 10, "title": "Backlog"},
    {"id": 11, "title": "Ready"},
    {"id": 12, "title": "In Progress"},
    {"id": 13, "title": "Waiting"},
    {"id": 9, "title": "Done"},
]


def task(task_id: int, title: str, created: str, description: str = "", **extra):
    return {
        "id": task_id,
        "title": title,
        "created": created,
        "updated": extra.get("updated", created),
        "description": description,
        "done": extra.get("done", False),
        "priority": extra.get("priority", 0),
        "labels": [{"title": t} for t in extra.get("labels", [])],
    }


DEFAULT_LAYOUT = {
    "Backlog": [task(5, "#29 Admin: surface generation mode", "2026-07-26T05:01:00Z")],
    "Ready": [
        task(
            9,
            "#33 Back up Vikunja database",
            "2026-07-26T05:05:50Z",
            "<p>Vikunja is now the <strong>authoritative</strong> queue.</p>"
            "<ul><li>cover <code>vikunja-db</code></li></ul>",
            labels=["Operations"],
        ),
        task(10, "#34 Version the OpenClaw health-check skill", "2026-07-26T05:09:00Z"),
    ],
    "In Progress": [
        task(11, "#35 Add “Work with Claude” integration", "2026-07-26T05:20:00Z")
    ],
    "Waiting": [],
    "Done": [task(1, "#25 Wrap the reports step", "2026-07-26T04:59:00Z", done=True)],
}


class FakeVikunja:
    """Serves the handful of endpoints the client uses; records every call."""

    def __init__(
        self,
        layout: dict[str, list[dict]] | None = None,
        fail: Any = None,
        comments: dict[int, list[dict]] | None = None,
        foreign: list[dict] | None = None,
    ):
        # Copied: moves mutate the layout, and DEFAULT_LAYOUT is module state.
        self.layout = deepcopy(layout if layout is not None else DEFAULT_LAYOUT)
        self.fail = fail
        self.comments = deepcopy(comments or {})
        # Tasks that exist in Vikunja but on somebody else's board. `GET
        # /tasks/{id}` serves them -- the API has no notion of "my project" --
        # and they never appear in this project's view. That difference is what
        # makes "the boundary refuses another project's task" testable rather
        # than incidentally true because the id was unused.
        self.foreign = {int(item["id"]): deepcopy(item) for item in (foreign or [])}
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method: str, path: str, body: dict | None = None):
        self.calls.append((method, path, body))
        if self.fail is not None:
            raise self.fail

        if method == "GET" and path == "/projects":
            return [
                {"id": 1, "title": "Inbox"},
                {"id": PROJECT_ID, "title": "AI Alpha Engine"},
            ]
        if method == "GET" and path == f"/projects/{PROJECT_ID}":
            return {
                "id": PROJECT_ID,
                "title": "AI Alpha Engine",
                "views": [
                    {"id": 9, "title": "List", "view_kind": "list"},
                    {"id": VIEW_ID, "title": "Kanban", "view_kind": "kanban"},
                ],
            }
        if method == "GET" and path.split("?")[0] == (
            f"/projects/{PROJECT_ID}/views/{VIEW_ID}/tasks"
        ):
            return self._view_tasks(path)
        if method == "GET" and path == f"/projects/{PROJECT_ID}/views/{VIEW_ID}/buckets":
            return [dict(bucket) for bucket in BUCKETS]

        moved = re.match(
            rf"^/projects/{PROJECT_ID}/views/{VIEW_ID}/buckets/(\d+)/tasks$", path
        )
        if method == "POST" and moved:
            self._move(int(moved.group(1)), int((body or {})["task_id"]))
            return None

        added = re.match(r"^/tasks/(\d+)/comments$", path)
        if method == "PUT" and added:
            return deepcopy(
                self._comment(int(added.group(1)), (body or {}).get("comment", ""))
            )

        listed = re.match(r"^/tasks/(\d+)/comments$", path)
        if method == "GET" and listed:
            return list(self.comments.get(int(listed.group(1)), []))

        single = re.match(r"^/tasks/(\d+)$", path)
        if method == "GET" and single:
            return deepcopy(self._find(int(single.group(1))))

        # REPLACE semantics, modelled deliberately: the stored task becomes the
        # body, so a field the caller omitted comes back as its zero value. This
        # is the real Vikunja behaviour that wipes descriptions, and the tests
        # are only worth anything if the fake reproduces it rather than being
        # forgiving.
        if method == "POST" and single:
            return deepcopy(self._replace(int(single.group(1)), body or {}))

        created = re.match(rf"^/projects/{PROJECT_ID}/tasks$", path)
        if method == "PUT" and created:
            return deepcopy(self._create(body or {}))

        raise VikunjaError(f"FakeVikunja has no route for {method} {path}", status=404)

    def _view_tasks(self, path: str) -> list[dict]:
        """The kanban view, paged the way Vikunja actually pages it.

        Three behaviours are modelled on purpose, because each one is a way a
        listing can come back short while looking complete:

        * paging is *per bucket*, and a page holds at most
          ``KANBAN_BUCKET_PAGE_SIZE`` tasks;
        * ``per_page`` is accepted and ignored, so asking for more does nothing;
        * ``count`` reports the bucket's real total, not the slice served.

        Measured against the live board, where a Done column of 170 answered
        with 50 and said so.
        """
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        page = max(1, int(query.get("page", ["1"])[0]))
        wanted = query.get("filter", [""])[0].replace(" ", "")

        served = []
        for bucket in BUCKETS:
            tasks = list(self.layout.get(bucket["title"], []))
            if wanted == "done=false":
                tasks = [t for t in tasks if not t.get("done")]
            elif wanted.startswith("id="):
                tasks = [t for t in tasks if t["id"] == int(wanted[3:])]
            elif wanted:
                raise VikunjaError(f"FakeVikunja cannot apply filter {wanted!r}")
            start = (page - 1) * KANBAN_BUCKET_PAGE_SIZE
            served.append(
                {
                    **bucket,
                    "count": len(tasks),
                    "tasks": tasks[start : start + KANBAN_BUCKET_PAGE_SIZE],
                }
            )
        return served

    def _comment(self, task_id: int, html: str) -> dict:
        """Store a comment and hand back what was stored.

        Comments are kept rather than acknowledged, because "the same comment
        twice" is only recognisable to a caller that can read back what is
        already on the task. A fake that answered every write with a canned id
        would make duplicate suppression untestable.
        """
        stored = self.comments.setdefault(task_id, [])
        existing = [c["id"] for tasks in self.comments.values() for c in tasks]
        comment = {
            "id": max(existing, default=0) + 1,
            "comment": html,
            "created": "2026-07-30T12:00:00Z",
            "author": {"username": "mcp"},
        }
        stored.append(comment)
        return comment

    def _find(self, task_id: int) -> dict:
        for tasks in self.layout.values():
            for item in tasks:
                if item["id"] == task_id:
                    return item
        if task_id in self.foreign:
            return self.foreign[task_id]
        raise VikunjaError(f"no such task {task_id}", status=404)

    def _replace(self, task_id: int, body: dict) -> dict:
        stored = self._find(task_id)
        for key in ("title", "description"):
            stored[key] = body.get(key, "")
        stored["done"] = bool(body.get("done", False))
        for key, value in body.items():
            if key not in ("id", "title", "description", "done"):
                stored[key] = value
        return stored

    def _create(self, body: dict) -> dict:
        new_id = max(
            (t["id"] for tasks in self.layout.values() for t in tasks), default=0
        ) + 1
        item = task(
            new_id,
            body.get("title", ""),
            "2026-07-27T00:00:00Z",
            body.get("description", ""),
        )
        self.layout.setdefault("Backlog", []).append(item)
        return item

    def _move(self, bucket_id: int, task_id: int) -> None:
        target = next(b["title"] for b in BUCKETS if b["id"] == bucket_id)
        for title, tasks in self.layout.items():
            for item in list(tasks):
                if item["id"] == task_id:
                    tasks.remove(item)
                    self.layout.setdefault(target, []).append(item)
                    return
        raise VikunjaError(f"no such task {task_id}", status=404)

    def bucket_of(self, task_id: int) -> str | None:
        for title, tasks in self.layout.items():
            if any(t["id"] == task_id for t in tasks):
                return title
        return None


class FilterIgnoringVikunja(FakeVikunja):
    """A Vikunja that accepts ``filter`` and does nothing with it.

    Sending a filter is an optimisation -- it saves walking a long Done column,
    and it turns a lookup by id into one request. Every caller has to stay
    correct when it does nothing, so that is a fake rather than an assumption.
    """

    def _view_tasks(self, path: str) -> list[dict]:
        head, _, query = path.partition("?")
        kept = "&".join(
            part for part in query.split("&") if part and not part.startswith("filter=")
        )
        return super()._view_tasks(head + (f"?{kept}" if kept else ""))


class FilterMatchingNothingVikunja(FakeVikunja):
    """A Vikunja whose filter is applied and matches nothing, ever.

    The dangerous failure mode, and the reason a filtered miss is not an
    absence: an ignored filter still answers with the whole board, so a walk
    over it stays complete. A filter that silently matches nothing answers with
    a well-formed, internally consistent, empty board — `count` agrees with what
    was served — and concluding "no such task" from that is exactly the bug this
    lookup exists to not have.
    """

    def _view_tasks(self, path: str) -> list[dict]:
        served = super()._view_tasks(path)
        if "filter=" not in path:
            return served
        return [{**bucket, "count": 0, "tasks": []} for bucket in served]


class FakeProcess:
    def __init__(self, pid: int = 4242, returncode: int = 0):
        self.pid = pid
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode


class RecordingSpawn:
    """Stands in for subprocess.Popen."""

    def __init__(self, pid: int = 4242, error: Exception | None = None):
        self.pid = pid
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        if self.error is not None:
            raise self.error
        self.calls.append({"argv": argv, **kwargs})
        for stream in ("stdout", "stderr"):
            handle = kwargs.get(stream)
            if hasattr(handle, "close"):
                handle.close()
        return FakeProcess(self.pid)
