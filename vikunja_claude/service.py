"""The one place that knows the order of operations for working a ticket."""

from __future__ import annotations

from dataclasses import asdict

from .config import Config
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

    def get(self, number: int) -> Ticket:
        """Convenience lookup by the editable #NN title prefix."""
        project_id, view_id = self._ids()
        return self.client.find_ticket(number, project_id, view_id)

    def next_ready(self) -> Ticket:
        project_id, view_id = self._ids()
        return self.client.oldest_ready_ticket(project_id, view_id)

    def prompt_for(self, ticket: Ticket) -> str:
        return build_prompt(
            ticket,
            workdir=self.config.workdir,
            project_title=self.config.project_title,
            frontend_url=self.config.frontend_url,
        )

    def preview(self, ticket: Ticket) -> dict:
        active = self.launcher.active_launch(ticket.task_id)
        return {
            # The board number and its rendered form, and no row id (task 659).
            # The URL below is the one deliberate carrier: /tasks/<id> is
            # Vikunja's only task route, so a link exists to be opened rather
            # than quoted.
            "number": ticket.task_number,
            "reference": ticket.board_reference,
            "title": ticket.title,
            "summary": ticket.summary,
            "bucket": ticket.bucket_title,
            "labels": ticket.labels,
            "done": ticket.done,
            "url": ticket.url(self.config.frontend_url),
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

    def work(self, ticket: Ticket) -> dict:
        """Move the ticket to In Progress, then launch Claude Code for it."""
        prompt = self.prompt_for(ticket)
        moved_to = None
        if ticket.bucket_title != IN_PROGRESS:
            project_id, view_id = self._ids()
            self.client.move_to_bucket(
                project_id, view_id, ticket.task_id, IN_PROGRESS
            )
            moved_to = IN_PROGRESS

        record: LaunchRecord = self.launcher.launch(ticket, prompt)
        return {
            "launched": True,
            "moved_to": moved_to,
            "url": ticket.url(self.config.frontend_url),
            **asdict(record),
        }
