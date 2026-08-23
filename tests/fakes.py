"""Test doubles: an in-memory Vikunja and a spawn stub."""

from __future__ import annotations

import re
import urllib.parse
from copy import deepcopy
from typing import Any

from vikunja_claude.vikunja import KANBAN_BUCKET_PAGE_SIZE, VikunjaError

PROJECT_ID = 2
PROJECT_TITLE = "AI Alpha Engine"
VIEW_ID = 12

#: The second approved board. Its ids are all distinct from the first board's
#: — project, view and buckets — because a fake that reused them would let a
#: request built for one board answer for the other, which is the whole class
#: of bug the two-project boundary has to not have.
TRADER_PROJECT_ID = 3
TRADER_TITLE = "AI Alpha Trader"
TRADER_VIEW_ID = 22

BUCKETS = [
    {"id": 10, "title": "Backlog"},
    {"id": 11, "title": "Ready"},
    {"id": 12, "title": "In Progress"},
    {"id": 13, "title": "Waiting"},
    {"id": 9, "title": "Done"},
]

TRADER_BUCKETS = [
    {"id": 20, "title": "Backlog"},
    {"id": 21, "title": "Ready"},
    {"id": 22, "title": "In Progress"},
    {"id": 23, "title": "Done"},
]


def task(
    task_id: int,
    title: str,
    created: str,
    description: str = "",
    *,
    index: int,
    **extra,
):
    """One stored task, with both of its numbers.

    ``index`` is keyword-only and has **no default** on purpose. Vikunja's
    project-local number and its global task id are different numbers, and
    every fixture in this suite states both — a default would let them agree
    by accident, and a fixture where ``#N`` happens to be ``/tasks/N`` cannot
    fail the confusion these tests exist to catch.
    """
    return {
        "id": task_id,
        "index": index,
        "title": title,
        "created": created,
        "updated": extra.get("updated", created),
        "description": description,
        "done": extra.get("done", False),
        "priority": extra.get("priority", 0),
        "labels": [{"title": t} for t in extra.get("labels", [])],
    }


#: The board, with **no task whose number is its own id**, and two numbers that
#: are some *other* task's id. Task 10 is #9 while task 9 exists as #8, so a
#: lookup that quietly fell back to `/tasks/<id>` would not error — it would
#: return a real, plausible, wrong task, which is the failure the project-local
#: identifier exists to make impossible. Live boards look like this: on the AI
#: Alpha Engine board #647 is task id 648.
DEFAULT_LAYOUT = {
    "Backlog": [
        task(
            5,
            "#29 Admin: surface generation mode",
            "2026-07-26T05:01:00Z",
            index=4,
        )
    ],
    "Ready": [
        task(
            9,
            "#33 Back up Vikunja database",
            "2026-07-26T05:05:50Z",
            "<p>Vikunja is now the <strong>authoritative</strong> queue.</p>"
            "<ul><li>cover <code>vikunja-db</code></li></ul>",
            labels=["Operations"],
            index=8,
        ),
        task(
            10,
            "#34 Version the OpenClaw health-check skill",
            "2026-07-26T05:09:00Z",
            index=9,
        ),
    ],
    "In Progress": [
        task(
            11,
            "#35 Add “Work with Claude” integration",
            "2026-07-26T05:20:00Z",
            index=10,
        )
    ],
    "Waiting": [],
    "Done": [
        task(
            1,
            "#25 Wrap the reports step",
            "2026-07-26T04:59:00Z",
            done=True,
            index=2,
        )
    ],
}

#: The Trader board. Task 1 is deliberately *its* task 1, not a copy of the
#: Engine board's — Vikunja task ids are unique across projects, so the two
#: boards never share one, and a test that asks the wrong board for a task id
#: must miss rather than match something plausible.
#:
#: Its **numbers**, though, do collide with the Engine board's, because they
#: are per project: #2 is task 40 here and task 1 there. That is not a quirk
#: of the fake — it is why a project-local number is only ever resolved
#: together with the board it was read from.
TRADER_LAYOUT = {
    "Backlog": [
        task(
            41,
            "Size a position from the S7 conviction band",
            "2026-08-19T09:10:00Z",
            "<p>Trader sizing rules.</p>",
            index=3,
        )
    ],
    "Ready": [
        task(
            40,
            "Define the execution architecture",
            "2026-08-18T08:00:00Z",
            "<p>How orders reach a broker.</p>",
            labels=["Investigation"],
            priority=4,
            index=2,
        )
    ],
    "In Progress": [],
    "Done": [
        task(
            39,
            "Pick the paper-trading venue",
            "2026-08-17T07:00:00Z",
            done=True,
            index=1,
        )
    ],
}


class FakeVikunja:
    """Serves the handful of endpoints the client uses; records every call."""

    def __init__(
        self,
        layout: dict[str, list[dict]] | None = None,
        fail: Any = None,
        comments: dict[int, list[dict]] | None = None,
        foreign: list[dict] | None = None,
        trader_layout: dict[str, list[dict]] | None = None,
        projects: dict[int, dict] | None = None,
    ):
        # Copied: moves mutate the layout, and DEFAULT_LAYOUT is module state.
        self.layout = deepcopy(layout if layout is not None else DEFAULT_LAYOUT)
        # Both approved boards are served by default, because that is what the
        # configured boundary expects to find. `projects` replaces the set
        # outright, which is how "this Vikunja does not have that project" and
        # "that id is titled something else" become testable conditions.
        self.projects = (
            deepcopy(projects)
            if projects is not None
            else {
                PROJECT_ID: {
                    "title": PROJECT_TITLE,
                    "view_id": VIEW_ID,
                    "buckets": BUCKETS,
                    "layout": self.layout,
                },
                TRADER_PROJECT_ID: {
                    "title": TRADER_TITLE,
                    "view_id": TRADER_VIEW_ID,
                    "buckets": TRADER_BUCKETS,
                    "layout": deepcopy(
                        trader_layout
                        if trader_layout is not None
                        else TRADER_LAYOUT
                    ),
                },
            }
        )
        # The first board's layout stays reachable as `.layout`, and stays the
        # same object the routes serve, so a test that inspects it after a
        # write sees the write.
        if PROJECT_ID in self.projects:
            self.projects[PROJECT_ID]["layout"] = self.layout
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
            return [{"id": 1, "title": "Inbox"}] + [
                {"id": number, "title": board["title"]}
                for number, board in self.projects.items()
            ]

        one = re.match(r"^/projects/(\d+)$", path)
        if method == "GET" and one:
            board = self._board(int(one.group(1)))
            return {
                "id": int(one.group(1)),
                "title": board["title"],
                "views": [
                    {"id": 9, "title": "List", "view_kind": "list"},
                    {
                        "id": board["view_id"],
                        "title": "Kanban",
                        "view_kind": "kanban",
                    },
                ],
            }

        viewed = re.match(r"^/projects/(\d+)/views/(\d+)/tasks$", path.split("?")[0])
        if method == "GET" and viewed:
            board = self._view(int(viewed.group(1)), int(viewed.group(2)))
            return self._view_tasks(path, board)

        listed_buckets = re.match(r"^/projects/(\d+)/views/(\d+)/buckets$", path)
        if method == "GET" and listed_buckets:
            board = self._view(
                int(listed_buckets.group(1)), int(listed_buckets.group(2))
            )
            return [dict(bucket) for bucket in board["buckets"]]

        moved = re.match(
            r"^/projects/(\d+)/views/(\d+)/buckets/(\d+)/tasks$", path
        )
        if method == "POST" and moved:
            board = self._view(int(moved.group(1)), int(moved.group(2)))
            self._move(int(moved.group(3)), int((body or {})["task_id"]), board)
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

        created = re.match(r"^/projects/(\d+)/tasks$", path)
        if method == "PUT" and created:
            board = self._board(int(created.group(1)))
            return deepcopy(self._create(body or {}, board))

        raise VikunjaError(f"FakeVikunja has no route for {method} {path}", status=404)

    def _board(self, project_id: int) -> dict:
        """One board, or the 404 Vikunja serves for a project that is not there."""
        board = self.projects.get(project_id)
        if board is None:
            raise VikunjaError(f"project {project_id} not found", status=404)
        return board

    def _view(self, project_id: int, view_id: int) -> dict:
        """One board addressed by project *and* view, as every listing route is."""
        board = self._board(project_id)
        if view_id != board["view_id"]:
            raise VikunjaError(
                f"project {project_id} has no view {view_id}", status=404
            )
        return board

    def _view_tasks(self, path: str, board: dict | None = None) -> list[dict]:
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
        board = board if board is not None else self.projects[PROJECT_ID]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        page = max(1, int(query.get("page", ["1"])[0]))
        wanted = query.get("filter", [""])[0].replace(" ", "")

        served = []
        for bucket in board["buckets"]:
            tasks = list(board["layout"].get(bucket["title"], []))
            if wanted == "done=false":
                tasks = [t for t in tasks if not t.get("done")]
            elif wanted.startswith("id="):
                tasks = [t for t in tasks if t["id"] == int(wanted[3:])]
            elif wanted.startswith("index="):
                # Served the way Vikunja serves it: per project, so the same
                # number matches a different task on the other board.
                tasks = [t for t in tasks if t.get("index") == int(wanted[6:])]
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
        # Every board, because `GET /tasks/{id}` has no notion of "my project":
        # it serves any task in any project, which is exactly why a boundary
        # cannot use it to decide ownership.
        for board in self.projects.values():
            for tasks in board["layout"].values():
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
            if key not in ("id", "index", "title", "description", "done"):
                stored[key] = value
        return stored

    def _create(self, body: dict, board: dict | None = None) -> dict:
        board = board if board is not None else self.projects[PROJECT_ID]
        # Ids are unique across boards, as Vikunja's are: a new task on one
        # board must never collide with an existing task on the other.
        new_id = max(
            (
                t["id"]
                for other in self.projects.values()
                for tasks in other["layout"].values()
                for t in tasks
            ),
            default=0,
        ) + 1
        # The number, unlike the id, is per project and counts that board's
        # own tasks — so a create on the second board gets a low number while
        # its id is high, which is what the live boards do.
        next_index = max(
            (
                t.get("index") or 0
                for tasks in board["layout"].values()
                for t in tasks
            ),
            default=0,
        ) + 1
        item = task(
            new_id,
            body.get("title", ""),
            "2026-07-27T00:00:00Z",
            body.get("description", ""),
            index=next_index,
        )
        board["layout"].setdefault("Backlog", []).append(item)
        return item

    def _move(self, bucket_id: int, task_id: int, board: dict | None = None) -> None:
        board = board if board is not None else self.projects[PROJECT_ID]
        target = next(b["title"] for b in board["buckets"] if b["id"] == bucket_id)
        for title, tasks in board["layout"].items():
            for item in list(tasks):
                if item["id"] == task_id:
                    tasks.remove(item)
                    board["layout"].setdefault(target, []).append(item)
                    return
        raise VikunjaError(f"no such task {task_id}", status=404)

    def _layout_of(self, project_id: int) -> dict[str, list[dict]]:
        return self.projects[project_id]["layout"]

    def number_of(self, task_id: int, project_id: int = PROJECT_ID) -> int:
        """This store's board number for the row with this immutable id.

        Reads the layout the test actually configured, and on the board it
        names — a project-local number means nothing without its project, so
        neither does the mapping to one. ``task()`` refuses to default the
        index, which is what stops a fixture from having the two agree.
        """
        for tasks in self._layout_of(project_id).values():
            for stored in tasks:
                if stored["id"] == task_id:
                    return int(stored["index"])
        raise AssertionError(f"no row with id {task_id} on project {project_id}")

    def id_of(self, task_number: int, project_id: int = PROJECT_ID) -> int:
        """This store's immutable id for the row the board shows as ``#N``.

        The inverse of :meth:`number_of`, and the one a test reaches for more
        often: the connector answers in board numbers now (task 660), while
        fixtures are written in ids. Mapping back here keeps an expectation
        readable as the row it names, without the answer having to publish an
        id to make it so.
        """
        for tasks in self._layout_of(project_id).values():
            for stored in tasks:
                if stored["index"] == task_number:
                    return int(stored["id"])
        raise AssertionError(f"no row shown as #{task_number} on project {project_id}")

    def row(self, task_id: int, project_id: int = PROJECT_ID) -> dict | None:
        """The stored row with this immutable id, or None.

        Task 660 stopped the connector publishing that id, so a test can no
        longer prove "the number resolved to the right row" by reading it back
        out of the answer. It asks the store instead, which is the stronger
        question: not "did the answer echo an id" but "is this the row that
        was actually read, or written to".
        """
        for tasks in self._layout_of(project_id).values():
            for stored in tasks:
                if stored["id"] == task_id:
                    return stored
        return None

    def bucket_of(self, task_id: int, project_id: int = PROJECT_ID) -> str | None:
        for title, tasks in self.projects[project_id]["layout"].items():
            if any(t["id"] == task_id for t in tasks):
                return title
        return None


class FilterIgnoringVikunja(FakeVikunja):
    """A Vikunja that accepts ``filter`` and does nothing with it.

    Sending a filter is an optimisation -- it saves walking a long Done column,
    and it turns a lookup by id into one request. Every caller has to stay
    correct when it does nothing, so that is a fake rather than an assumption.
    """

    def _view_tasks(self, path: str, board: dict | None = None) -> list[dict]:
        head, _, query = path.partition("?")
        kept = "&".join(
            part for part in query.split("&") if part and not part.startswith("filter=")
        )
        return super()._view_tasks(head + (f"?{kept}" if kept else ""), board)


class FilterMatchingNothingVikunja(FakeVikunja):
    """A Vikunja whose filter is applied and matches nothing, ever.

    The dangerous failure mode, and the reason a filtered miss is not an
    absence: an ignored filter still answers with the whole board, so a walk
    over it stays complete. A filter that silently matches nothing answers with
    a well-formed, internally consistent, empty board — `count` agrees with what
    was served — and concluding "no such task" from that is exactly the bug this
    lookup exists to not have.
    """

    def _view_tasks(self, path: str, board: dict | None = None) -> list[dict]:
        served = super()._view_tasks(path, board)
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

