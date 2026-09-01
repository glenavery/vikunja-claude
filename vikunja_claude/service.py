"""The one place that knows the order of operations for working a ticket."""

from __future__ import annotations

from dataclasses import asdict

from .config import Config
from .executors import Executor, resolve
from .launcher import Launcher, LaunchRecord
from .prompt import build_prompt
from .vikunja import Ticket, VikunjaClient

IN_PROGRESS = "In Progress"
#: Where a ticket goes when the run that was working it was lost. Back to
#: waiting, not to done and not to a state of its own: nothing is known about
#: how far the run got, and the honest position is that it still needs doing.
READY = "Ready"


def _orphan_comment(record: dict) -> str:
    """What the ticket is told, in the terms of what is actually known.

    It does not say the run failed and it does not say it finished, because
    neither was observed. It says the run is gone, when it started, what it was
    on, and that nothing it may have done was reported — which is the only
    thing a reader can safely act on.
    """
    started = record.get("started_at") or "an unknown time"
    executor = record.get("executor") or "the default executor"
    model = record.get("model")
    on = f"{executor} ({model})" if model else executor
    return (
        "The Claude Code run for this ticket was lost.\n\n"
        f"It started at {started} on {on}, and its process is gone without "
        "having reported anything. The usual cause is the ticket launcher "
        "service being restarted while the run was in flight, which kills the "
        "run.\n\n"
        "Nothing is known about how far it got. It did not report a result, so "
        "treat any work it may have done as unverified and check the "
        "repository before starting again. This ticket has been moved back to "
        "Ready.\n\n"
        "Posted automatically by the ticket runner when it noticed the run was "
        "gone."
    )


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

    # -- reconciliation (task 754) -----------------------------------------

    def reconcile_orphaned_runs(self) -> list[dict]:
        """Close out the runs this service lost, on disk and on the board.

        Called once at startup. Stopping this service is what orphans a run —
        the launched process sits in this unit's control group and dies with
        it, and the thread that would have recorded its ending dies too — so
        starting is the moment to account for what the last stop destroyed.

        Two halves, deliberately in this order and with different failure
        rules. The launcher's half is local and always happens: the ending is
        recorded and the lock released, so the artifacts stop claiming a run is
        in flight. The board half is a network call to something that may not be
        up yet, and it is best-effort: a Vikunja that is down must not stop this
        service from starting, and it must not make the local half conditional
        on it either.

        Which is why each result says whether the board was actually told.
        A reconciliation that silently failed to report would leave exactly the
        condition this exists to end — a ticket sitting In Progress with nothing
        said on it — and the only difference would be that nothing was left to
        notice it.
        """
        results = []
        for record in self.launcher.reconcile_orphaned_runs():
            results.append({**record, "reported": self._report_orphan(record)})
        return results

    def _report_orphan(self, record: dict) -> bool:
        """Say on the ticket that its run was lost, and stop it claiming to run.

        The move is guarded on the column the ticket is in *now*. A ticket a
        human already moved on — closed it, sent it back, picked it up — is one
        where the board is no longer wrong, and moving it anyway would undo a
        decision made by someone who knew more than this does.
        """
        number = record.get("number")
        if number is None:
            return False
        try:
            project_id, view_id = self._ids()
            ticket = self.client.find_by_task_number(number, project_id, view_id)
            self.client.add_comment(ticket.task_id, _orphan_comment(record))
            if ticket.bucket_title == IN_PROGRESS:
                self.client.move_to_bucket(
                    project_id, view_id, ticket.task_id, READY
                )
            return True
        except Exception as exc:  # noqa: BLE001
            # Every failure mode here is "the board could not be told", and none
            # of them is worth refusing to start over. It is logged where the
            # local record already went, so the miss is visible in the same
            # place as the reconciliation it belongs to.
            self.launcher._log(
                "orphan_report_failed",
                reference=record.get("reference"),
                error=str(exc),
            )
            return False

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
