"""The one place that knows the order of operations for working a ticket."""

from __future__ import annotations

from dataclasses import asdict

from .config import Config
from .executors import Executor, resolve
from .launcher import Launcher, LaunchRecord
from .prompt import build_prompt
from .vikunja import Ticket, VikunjaClient

IN_PROGRESS = "In Progress"


class TicketService:
    def __init__(self, config: Config, client: VikunjaClient, launcher: Launcher):
        self.config = config
        self.client = client
        self.launcher = launcher

    # -- resolution --------------------------------------------------------

    def _ids(self) -> tuple[int, int]:
        project_id = self.client.project_id(
            self.config.project_title, self.config.project_id
        )
        return project_id, self.client.kanban_view_id(project_id)

    def get_task(self, task_id: int) -> Ticket:
        """Canonical lookup, by Vikunja's immutable task id."""
        project_id, view_id = self._ids()
        return self.client.find_by_task_id(task_id, project_id, view_id)

    def get_by_task_number(self, task_number: int) -> Ticket:
        """Lookup by the board number: Vikunja's ``index``, the ``#N`` on the card.

        The same identifier the MCP boundary takes, resolved through the same
        client call, so the two ways of starting a run cannot disagree about
        which ticket a number names (task 748).

        It used to resolve the legacy ``#NN`` *title prefix* instead, while
        everything that addresses a ticket here had already moved to the board
        number: `web._ticket_href`, `web._work_path` and the console input all
        emit ``/ticket/<board number>`` (task 659). Nothing joined the two --
        one test asserted the launch page emits ``/ticket/8/work`` and another
        posted ``/ticket/33/work``, and no test ever posted the path the page
        actually emits. On a board carrying no title prefix at all, which is
        every AI Alpha board since 2026-07-26, that made the browser button a
        404: ``No ticket #714 in this project`` for the task the board shows
        as #714.

        There is deliberately no fallback to the prefix when the number misses.
        The two schemes disagree *by a few* on a real board, so a retry under
        the other one would usually find a real, plausible, wrong ticket --
        and succeeding is the damage, the same reasoning as "never
        reinterpreted" at the MCP boundary. The prefix lookup survives where a
        human types which scheme they mean: ``vkctl.py --ticket``.
        """
        project_id, view_id = self._ids()
        return self.client.find_by_task_number(task_number, project_id, view_id)

    def next_ready(self) -> Ticket:
        project_id, view_id = self._ids()
        return self.client.oldest_ready_ticket(project_id, view_id)

    def prompt_for(self, ticket: Ticket) -> str:
        return build_prompt(
            ticket,
            workdir=self.config.workdir,
            project_title=self.config.project_title,
        )

    def preview(self, ticket: Ticket) -> dict:
        active = self.launcher.active_launch(ticket.task_id)
        return {
            # The board number and its rendered form, and no row id (task 659).
            "number": ticket.task_number,
            "reference": ticket.board_reference,
            "title": ticket.title,
            "summary": ticket.summary,
            "bucket": ticket.bucket_title,
            "labels": ticket.labels,
            "done": ticket.done,
            "workdir": str(self.config.workdir),
            "description": ticket.description,
            "prompt": self.prompt_for(ticket),
            "running": active,
            # The ticket as it stands NOW, not as it was filed. A comment is
            # where the filer corrects or redirects a brief mid-flight, and a
            # preview that showed only the description let a run work from a
            # version the human had already moved on from.
            "comments": self.client.comment_views(ticket.task_id),
        }

    # -- action ------------------------------------------------------------

    def executor_for(self, name: str | None) -> Executor:
        """The executor a request asked for, or the configured default.

        Resolved here, before anything is moved or launched, so a bad executor
        name or a broken local seat is a refusal that leaves the board alone —
        rather than a ticket sitting in In Progress with nothing running.
        """
        return resolve(
            name or self.config.executor,
            workdir=self.config.workdir,
            local_base_url=self.config.local_executor_base_url,
        )

    def work(self, ticket: Ticket, executor: str | None = None) -> dict:
        """Move the ticket to In Progress, then launch Claude Code for it."""
        chosen = self.executor_for(executor)
        prompt = self.prompt_for(ticket)
        moved_to = None
        if ticket.bucket_title != IN_PROGRESS:
            project_id, view_id = self._ids()
            self.client.move_to_bucket(
                project_id, view_id, ticket.task_id, IN_PROGRESS
            )
            moved_to = IN_PROGRESS

        record: LaunchRecord = self.launcher.launch(ticket, prompt, chosen)
        return {
            "launched": True,
            "moved_to": moved_to,
            **asdict(record),
        }
