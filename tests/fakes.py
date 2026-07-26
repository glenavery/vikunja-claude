"""Test doubles: an in-memory Vikunja and a spawn stub."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from vikunja_claude.vikunja import VikunjaError

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
        "description": description,
        "done": extra.get("done", False),
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

    def __init__(self, layout: dict[str, list[dict]] | None = None, fail: Any = None):
        # Copied: moves mutate the layout, and DEFAULT_LAYOUT is module state.
        self.layout = deepcopy(layout if layout is not None else DEFAULT_LAYOUT)
        self.fail = fail
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
        if method == "GET" and path.startswith(
            f"/projects/{PROJECT_ID}/views/{VIEW_ID}/tasks"
        ):
            return [
                {**bucket, "tasks": list(self.layout.get(bucket["title"], []))}
                for bucket in BUCKETS
            ]
        if method == "GET" and path == f"/projects/{PROJECT_ID}/views/{VIEW_ID}/buckets":
            return [dict(bucket) for bucket in BUCKETS]

        moved = re.match(
            rf"^/projects/{PROJECT_ID}/views/{VIEW_ID}/buckets/(\d+)/tasks$", path
        )
        if method == "POST" and moved:
            self._move(int(moved.group(1)), int((body or {})["task_id"]))
            return None

        if method == "PUT" and re.match(r"^/tasks/\d+/comments$", path):
            return {"id": 1, "comment": (body or {}).get("comment", "")}

        raise VikunjaError(f"FakeVikunja has no route for {method} {path}", status=404)

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
