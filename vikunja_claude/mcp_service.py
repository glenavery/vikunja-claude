"""The operations the MCP boundary exposes, and the rules around them.

The whole surface is here: reads, one create, and — since task 196 — two ways to
change a task that already exists: its title/description, and a new comment.
Nothing in this module can close, delete, move, label, assign or reprioritise a
task, and it reaches Vikunja only through
:class:`~vikunja_claude.vikunja.VikunjaClient` methods that cannot do those
things either.

The two edits are **two-step**. A call with no ``approval_token`` writes
nothing: it reads the task and returns the exact current value beside the exact
proposed one, with a token naming that one change. Only a second call carrying
that token writes, and it writes only if the submitted text and the task's
current value are both still the ones the token was issued for.

What that does and does not buy is worth being exact about, because the
difference is where this kind of guard usually gets oversold. No server can see
the conversation, so this cannot prove a human said yes. What it does prove is
that no edit happens without a prior round trip that put the before and after
text in front of the client, and that the write is byte-for-byte the change that
round trip described — an approval cannot be carried over to different text, to
a different task, or to a task that has moved underneath it.

The operational reads (repository, pipeline, system health — task 138) are the
one part of this surface that can be **absent**: they are advertised only when
:class:`~vikunja_claude.config.InvestmentConfig` is configured, because a tool
that can never succeed is read by a model as a capability, and its failure
reported as a fact about the system rather than about the configuration. They
reach nothing directly — no shell, no database — only three fixed GETs handled by
:mod:`vikunja_claude.investment`.

The public page fetch (task 204) is absent in the same way and for the same
reason, from its own setting. It is the one tool here that takes a path, so it
is worth saying where the safety of that lives: not here. It is handled by
:mod:`vikunja_claude.website`, which holds no credential at all, so a protected
page answers it as it answers a stranger and the refusal is what comes back.

The authenticated page read (task 239) is the one tool that sees a page a
stranger cannot. It rides on the operational reads' setting, because it is the
same credential to the same admin instance, and its safety lives one layer
further away still: in the application, which owns the identity
(``TEST_PAYING_USER_ID``), checks that it is still a paying user, decides which
routes it will render and returns no session cookie. Nothing here chooses who
the page is read as, and no argument can — see :mod:`vikunja_claude.paying_page`.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
import threading
from datetime import datetime, timezone
from typing import Any

from .config import McpConfig
from .html_text import html_to_text, text_to_html
from .investment import InvestmentStatusClient, InvestmentStatusError
from .mcp import Tool, ToolError
from .paying_page import PayingPageError, PayingSiteClient
from .vikunja import TicketNotFound, VikunjaClient, VikunjaError
from .website import PublicPageError, PublicSiteClient

MAX_TITLE_CHARS = 250
MAX_DESCRIPTION_CHARS = 20000
MAX_COMMENT_CHARS = 20000

#: The two changes a token can be issued for. They are kept apart so an
#: approval for a comment can never be redeemed as an approval for an edit.
CHANGE_UPDATE = "update"
CHANGE_COMMENT = "comment"

#: How many previewed-but-uncommitted changes are remembered at once. A preview
#: costs nothing and a client is free to abandon one, so the table is bounded
#: rather than left to grow; the oldest is dropped, and losing one costs a fresh
#: preview and nothing else. Deliberately not persisted: an approval describes a
#: task as it was moments ago, and one that outlived a restart would be
#: describing a board nobody has looked at since.
MAX_PENDING_APPROVALS = 64

#: The statuses ``search_tasks`` accepts. A closed set, so a typo is a refusal
#: rather than a silently narrower search.
STATUS_OPEN = "open"
STATUS_DONE = "done"
STATUS_ANY = "any"
SEARCH_STATUSES = (STATUS_OPEN, STATUS_DONE, STATUS_ANY)

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
    def __init__(
        self,
        config: McpConfig,
        client: VikunjaClient,
        investment: InvestmentStatusClient | None = None,
        site: PublicSiteClient | None = None,
        paying_site: PayingSiteClient | None = None,
    ):
        self.config = config
        self.client = client
        # One process, one port, one unit — so a lock is enough to make
        # check-then-write atomic against a concurrent retry. One lock for all
        # three writes, not one each: the checks that make a retry harmless
        # (the ledger, a no-op edit, a duplicate comment) all read state a
        # concurrent write is about to change.
        self._write_lock = threading.Lock()
        # Changes that have been previewed and not yet committed, keyed by the
        # token that was handed out for each. See `_issue_approval`.
        self._pending: dict[str, dict[str, Any]] = {}
        # Built from configuration unless one is injected. None here and None in
        # the config mean the same thing, and it is checked in one place
        # (`operational_reads_enabled`) rather than at each call site.
        if investment is not None:
            self.investment: InvestmentStatusClient | None = investment
        elif config.investment is not None:
            self.investment = InvestmentStatusClient(
                config.investment.api_url, config.investment.api_key
            )
        else:
            self.investment = None
        # Same rule, its own setting: built from configuration unless injected,
        # and None in either place means the page tool is not advertised.
        if site is not None:
            self.site: PublicSiteClient | None = site
        elif config.public_site_url is not None:
            self.site = PublicSiteClient(config.public_site_url)
        else:
            self.site = None
        # The authenticated read rides on the operational reads' setting: it is
        # the same key to the same admin instance, and there is deliberately no
        # second switch here. Whether the *application* has a test paying
        # identity configured is the application's own setting, and it answers
        # so — a switch here as well would be a second source of truth that can
        # disagree with the one that decides.
        if paying_site is not None:
            self.paying_site: PayingSiteClient | None = paying_site
        elif config.investment is not None:
            self.paying_site = PayingSiteClient(
                config.investment.api_url, config.investment.api_key
            )
        else:
            self.paying_site = None

    @property
    def operational_reads_enabled(self) -> bool:
        return self.investment is not None

    @property
    def page_fetch_enabled(self) -> bool:
        return self.site is not None

    @property
    def paying_page_fetch_enabled(self) -> bool:
        return self.paying_site is not None

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

    def search_tasks(
        self, text: str, status: str = STATUS_OPEN
    ) -> dict[str, Any]:
        """Tasks whose title or description contains ``text``.

        Three deliberate choices.

        **Matching is done here, not by Vikunja's filter language.** That language
        is a string this boundary would have to build from model-supplied text; a
        filter is not SQL, but it is still an expression, and interpolating an
        untrusted fragment into one to search for a *literal* is the wrong shape
        for the job. The whole board is walked and compared in Python, where
        ``text`` can only ever be a substring.

        **Status is a closed set, and ``done`` is reachable.** "Search by status"
        was approved, and a search that could not see finished tasks would answer
        "has this been done before?" with a confident no. Unlike
        :meth:`list_open_tasks`, which is the *open queue* by definition, this one
        must be able to look at history.

        **The project is not a parameter.** This boundary is configured with one
        board, and it is named in the answer rather than chosen by the caller —
        so a search cannot silently be answered from somewhere else, for the same
        reason :meth:`create_task` refuses a project it was not given.
        """
        needle = (text or "").strip()
        if not needle:
            raise ToolError(
                "text is required and cannot be blank: an empty search would "
                "return the whole board, which is what list_open_tasks is for"
            )
        wanted = (status or STATUS_OPEN).strip().casefold() or STATUS_OPEN
        if wanted not in SEARCH_STATUSES:
            raise ToolError(
                f"status must be one of {', '.join(sorted(SEARCH_STATUSES))}, "
                f"not {status!r}"
            )

        project_id, view_id = self._ids()
        try:
            if wanted == STATUS_OPEN:
                tickets = self.client.list_open_tickets(project_id, view_id)
            else:
                tickets = self.client.list_all_tickets(project_id, view_id)
        except VikunjaError as exc:
            raise ToolError(str(exc)) from exc

        if wanted == STATUS_DONE:
            tickets = [t for t in tickets if t.done]

        folded = needle.casefold()
        matches = [
            t
            for t in tickets
            if folded in t.title.casefold() or folded in t.description.casefold()
        ]
        # Title matches first: a word in a title is what the task is about, a word
        # in a description may be a passing mention. Then most urgent, then id —
        # the same total order list_open_tasks uses, for the same reason.
        matches.sort(
            key=lambda t: (
                0 if folded in t.title.casefold() else 1,
                -t.priority,
                t.task_id,
            )
        )

        return {
            "project": self.config.project_title,
            "project_id": project_id,
            "query": needle,
            "status": wanted,
            "searched": len(tickets),
            "count": len(matches),
            "tasks": [
                {
                    "task_id": t.task_id,
                    "ticket": t.number,
                    "reference": t.reference,
                    "title": t.title,
                    "summary": t.summary,
                    "status": "done" if t.done else "open",
                    "bucket": t.bucket_title,
                    "priority": t.priority,
                    "priority_label": PRIORITY_NAMES.get(t.priority, "unknown"),
                    "labels": t.labels,
                    "matched_in": (
                        "title" if folded in t.title.casefold() else "description"
                    ),
                    "created": t.created,
                    "updated": t.updated,
                    "url": t.url(self.config.frontend_url),
                }
                for t in matches
            ],
        }

    # -- operational reads (task 138) --------------------------------------

    def _investment(self) -> InvestmentStatusClient:
        """The configured client, or a refusal that names the configuration.

        Reached only if a tool was called that should not have been advertised,
        so it says which setting is missing rather than reporting an unknown
        state as a healthy one.
        """
        if self.investment is None:
            raise ToolError(
                "The operational reads are not configured on this boundary. Set "
                "INVESTMENT_API_URL and INVESTMENT_API_KEY to enable them. "
                "Nothing was read, so nothing is known about the state this "
                "would have reported."
            )
        return self.investment

    def repository_state(self) -> dict[str, Any]:
        try:
            return self._investment().repository_state()
        except InvestmentStatusError as exc:
            raise ToolError(str(exc)) from exc

    def pipeline_status(self) -> dict[str, Any]:
        try:
            return self._investment().pipeline_status()
        except InvestmentStatusError as exc:
            raise ToolError(str(exc)) from exc

    def system_health(self) -> dict[str, Any]:
        try:
            return self._investment().system_health()
        except InvestmentStatusError as exc:
            raise ToolError(str(exc)) from exc

    # -- the public website (task 204) --------------------------------------

    def _site(self) -> PublicSiteClient:
        """The configured site client, or a refusal that names the setting."""
        if self.site is None:
            raise ToolError(
                "The public page fetch is not configured on this boundary. Set "
                "INVESTMENT_PUBLIC_URL to enable it. Nothing was fetched, so "
                "nothing is known about what that page returns."
            )
        return self.site

    def fetch_public_page(self, path: Any) -> dict[str, Any]:
        """One page of the public site, exactly as a visitor is served it."""
        try:
            return self._site().fetch_page(path)
        except PublicPageError as exc:
            raise ToolError(str(exc)) from exc

    # -- the site as the test paying user (task 239) ------------------------

    def _paying_site(self) -> PayingSiteClient:
        """The configured client, or a refusal that names the settings."""
        if self.paying_site is None:
            raise ToolError(
                "The authenticated page read is not configured on this "
                "boundary. It uses the same admin instance as the operational "
                "reads, so set INVESTMENT_API_URL and INVESTMENT_API_KEY to "
                "enable it. Nothing was read."
            )
        return self.paying_site

    def fetch_test_paying_page(self, path: Any) -> dict[str, Any]:
        """One page of the site as the test paying user, decided by the server."""
        try:
            return self._paying_site().fetch_page(path)
        except PayingPageError as exc:
            raise ToolError(str(exc)) from exc

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
        with self._write_lock:
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

    # -- approval (task 196) -----------------------------------------------

    def _issue_approval(
        self,
        kind: str,
        task_id: int,
        before: tuple[str, ...],
        after: tuple[str, ...],
    ) -> str:
        """A token naming one exact change to one task.

        Re-previewing the same change returns the token already issued for it
        rather than a second one, so a client that asks twice before showing the
        user has one approval outstanding and not two.
        """
        with self._write_lock:
            for token, record in self._pending.items():
                if (record["kind"], record["task_id"], record["before"], record["after"]) == (
                    kind,
                    task_id,
                    before,
                    after,
                ):
                    return token
            while len(self._pending) >= MAX_PENDING_APPROVALS:
                self._pending.pop(next(iter(self._pending)))
            token = secrets.token_urlsafe(24)
            self._pending[token] = {
                "kind": kind,
                "task_id": task_id,
                "before": before,
                "after": after,
                "issued_at": _now(),
            }
            return token

    def _redeem_approval(
        self,
        token: str,
        kind: str,
        task_id: int,
        before: tuple[str, ...],
        after: tuple[str, ...],
    ) -> None:
        """Spend an approval, or refuse and say which half stopped matching.

        Single use, and a mismatch spends it too: an approval describes one
        change, so a token that no longer describes it is not a token to retry
        with. The caller must already hold the write lock — the check and the
        write it authorises are one step, or a concurrent call could redeem the
        same token against a board that moved in between.
        """
        record = self._pending.get(token)
        if (
            record is None
            or record["kind"] != kind
            or record["task_id"] != task_id
        ):
            raise ToolError(
                f"That approval_token was not issued for this change to task "
                f"{task_id}. It may already have been used, it may belong to "
                "another task, or the service may have restarted since it was "
                "issued. Nothing was changed. Call again without approval_token "
                "to read the current value and get a fresh approval to show the "
                "user."
            )
        self._pending.pop(token, None)
        if record["after"] != after:
            raise ToolError(
                f"This is not the change that was approved for task {task_id}: "
                "the text submitted differs from the text the approval was "
                "issued for. Nothing was changed. Call again without "
                "approval_token, show the user the new text, and get a fresh "
                "approval for it."
            )
        if record["before"] != before:
            raise ToolError(
                f"Task {task_id} has changed since that approval was issued, so "
                "the value the user was shown is no longer the value on the "
                "board. Nothing was changed. Call again without approval_token "
                "to see what it holds now."
            )

    def _record_mutation(self, record: dict[str, Any], summary: str) -> None:
        path = self.config.mutation_ledger_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
        print(f"mcp: {summary}", file=sys.stderr, flush=True)

    def _own_ticket(self, task_id: int, verb: str):
        """The task, if it is on this board. The project boundary, structurally.

        Looked up through *this project's* view, so a task belonging to another
        project is not in the answer to begin with — the refusal does not depend
        on comparing a project id the caller supplied.
        """
        project_id, view_id = self._ids()
        try:
            return project_id, self.client.find_by_task_id(task_id, project_id, view_id)
        except TicketNotFound as exc:
            raise ToolError(
                f"Refusing to {verb} task {task_id}: it is not on the "
                f"{self.config.project_title} board. {exc} Nothing was changed."
            ) from exc
        except VikunjaError as exc:
            raise ToolError(f"{exc} Nothing was changed.") from exc

    # -- update (task 196) --------------------------------------------------

    def update_task(
        self,
        task_id: int,
        title: str | None = None,
        description: str | None = None,
        approval_token: str | None = None,
    ) -> dict[str, Any]:
        """Replace one task's title, description or both, after approval.

        Whole values only. There is no partial or inferred replacement — the
        caller submits the complete new text, which is the same thing the user
        is shown, so what was approved and what is stored cannot drift apart.
        """
        title = None if title is None else title.strip()
        description = None if description is None else description.strip()

        if title is None and description is None:
            raise ToolError(
                "update_task needs a complete replacement title, a complete "
                "replacement description, or both. It does not do partial "
                "replacements, and it cannot change status, bucket, labels, "
                "assignees, priority or due dates. Nothing was changed."
            )
        if title is not None and not title:
            raise ToolError("a blank title is refused: nothing was changed")
        if description is not None and not description:
            raise ToolError(
                "a blank description is refused: a ticket with no description is "
                "not a ticket anyone can work. Nothing was changed."
            )
        if title is not None and len(title) > MAX_TITLE_CHARS:
            raise ToolError(
                f"title is longer than {MAX_TITLE_CHARS} characters. Nothing was "
                "changed."
            )
        if description is not None and len(description) > MAX_DESCRIPTION_CHARS:
            raise ToolError(
                f"description is longer than {MAX_DESCRIPTION_CHARS} characters. "
                "Nothing was changed."
            )

        project_id, ticket = self._own_ticket(task_id, "update")
        proposal = self._proposed_update(ticket, title, description)

        if not proposal["changed_fields"]:
            # Idempotent: the board already says what was asked for. Reported as
            # a no-op rather than refused, because "make it say X" and "it says
            # X" are the same outcome, and a repeat of an applied change lands
            # here rather than needing a second approval.
            return {
                "applied": False,
                "reason": "the task already holds these values, so there was "
                "nothing to change",
                "task_id": ticket.task_id,
                "title": ticket.title,
                "changed_fields": [],
                "project": self.config.project_title,
                "project_id": project_id,
                "url": ticket.url(self.config.frontend_url),
            }

        if approval_token is None:
            return {
                "applied": False,
                "approval_required": True,
                "task_id": ticket.task_id,
                "title": ticket.title,
                "changed_fields": proposal["changed_fields"],
                "current": proposal["current"],
                "proposed": proposal["proposed"],
                "approval_token": self._issue_approval(
                    CHANGE_UPDATE, ticket.task_id, proposal["before"], proposal["after"]
                ),
                "project": self.config.project_title,
                "project_id": project_id,
                "url": ticket.url(self.config.frontend_url),
                "next_step": (
                    "Nothing has been changed. Show the user the exact current "
                    "and proposed values above. If they approve that exact "
                    "change, call update_task again with identical arguments "
                    "plus this approval_token."
                ),
            }

        with self._write_lock:
            # Re-read inside the lock. The preview's read is not the
            # precondition: the board can move between the two calls, and the
            # value the user approved replacing is the one that must still be
            # there.
            _, current = self._own_ticket(task_id, "update")
            proposal = self._proposed_update(current, title, description)
            if not proposal["changed_fields"]:
                return {
                    "applied": False,
                    "reason": "the task already holds these values, so there was "
                    "nothing to change",
                    "task_id": current.task_id,
                    "title": current.title,
                    "changed_fields": [],
                    "project": self.config.project_title,
                    "project_id": project_id,
                    "url": current.url(self.config.frontend_url),
                }

            self._redeem_approval(
                approval_token,
                CHANGE_UPDATE,
                current.task_id,
                proposal["before"],
                proposal["after"],
            )

            changed = proposal["changed_fields"]
            new_title = proposal["proposed"]["title"]
            new_description = proposal["proposed"]["description"]
            try:
                self.client.set_task_fields(
                    current.task_id,
                    title=new_title if "title" in changed else None,
                    description_html=(
                        text_to_html(new_description)
                        if "description" in changed
                        else None
                    ),
                )
            except VikunjaError as exc:
                raise ToolError(f"Vikunja refused the update: {exc}") from exc

            self._record_mutation(
                {
                    "at": _now(),
                    "kind": CHANGE_UPDATE,
                    "project_id": project_id,
                    "task_id": current.task_id,
                    "changed_fields": changed,
                    "replaced": proposal["current"],
                    "stored": proposal["proposed"],
                },
                f"updated task {current.task_id} in project {project_id} "
                f"({', '.join(changed)}) — {new_title!r}",
            )

        return {
            "applied": True,
            "task_id": current.task_id,
            "title": new_title,
            "changed_fields": changed,
            "project": self.config.project_title,
            "project_id": project_id,
            "url": current.url(self.config.frontend_url),
        }

    @staticmethod
    def _proposed_update(ticket, title: str | None, description: str | None) -> dict:
        """What this task holds, what it would hold, and which fields differ.

        Comparison is on the text a human reads, not on the stored markup: the
        caller submits plain text and Vikunja stores editor HTML, so comparing
        the two representations would call every no-op a change.
        """
        current = {"title": ticket.title, "description": ticket.description}
        proposed = {
            "title": current["title"] if title is None else title,
            "description": (
                current["description"] if description is None else description
            ),
        }
        changed = [
            field for field in ("title", "description")
            if proposed[field] != current[field]
        ]
        return {
            "current": current,
            "proposed": proposed,
            "changed_fields": changed,
            "before": (current["title"], current["description"]),
            "after": (proposed["title"], proposed["description"]),
        }

    # -- comment (task 196) -------------------------------------------------

    def add_task_comment(
        self, task_id: int, comment: str, approval_token: str | None = None
    ) -> dict[str, Any]:
        """Append one plain-text comment to a task on this board, after approval.

        Append-only: this cannot edit or delete a comment that is already there,
        and the tool that would do so does not exist.
        """
        text = (comment or "").strip()
        if not text:
            raise ToolError(
                "comment is required and cannot be blank. Nothing was written."
            )
        if len(text) > MAX_COMMENT_CHARS:
            raise ToolError(
                f"comment is longer than {MAX_COMMENT_CHARS} characters. Nothing "
                "was written."
            )

        project_id, ticket = self._own_ticket(task_id, "comment on")
        duplicate = self._existing_comment(ticket.task_id, text)
        if duplicate is not None:
            return self._duplicate_comment(project_id, ticket, duplicate)

        if approval_token is None:
            return {
                "added": False,
                "approval_required": True,
                "task_id": ticket.task_id,
                "title": ticket.title,
                "comment": text,
                "approval_token": self._issue_approval(
                    CHANGE_COMMENT, ticket.task_id, (), (text,)
                ),
                "project": self.config.project_title,
                "project_id": project_id,
                "url": ticket.url(self.config.frontend_url),
                "next_step": (
                    "Nothing has been written. Show the user this exact comment "
                    "and which task it would go on. If they approve it, call "
                    "add_task_comment again with the identical comment plus this "
                    "approval_token."
                ),
            }

        with self._write_lock:
            # Re-checked inside the lock, so two calls racing with the same text
            # cannot both find the task uncommented and both write.
            duplicate = self._existing_comment(ticket.task_id, text)
            if duplicate is not None:
                return self._duplicate_comment(project_id, ticket, duplicate)

            # A comment replaces nothing, so unlike an edit its approval binds no
            # prior value -- there is none to have moved underneath it.
            self._redeem_approval(
                approval_token, CHANGE_COMMENT, ticket.task_id, (), (text,)
            )
            try:
                created = self.client.add_comment(ticket.task_id, text_to_html(text))
            except VikunjaError as exc:
                raise ToolError(f"Vikunja refused the comment: {exc}") from exc

            comment_id = created.get("id")
            self._record_mutation(
                {
                    "at": _now(),
                    "kind": CHANGE_COMMENT,
                    "project_id": project_id,
                    "task_id": ticket.task_id,
                    "comment_id": comment_id,
                    "comment": text,
                },
                f"commented on task {ticket.task_id} in project {project_id}",
            )

        return {
            "added": True,
            "task_id": ticket.task_id,
            "title": ticket.title,
            "comment_id": comment_id,
            "project": self.config.project_title,
            "project_id": project_id,
            "url": ticket.url(self.config.frontend_url),
        }

    def _existing_comment(self, task_id: int, text: str) -> dict[str, Any] | None:
        """A comment already on the task with this exact text, if there is one.

        Read from the task rather than from a ledger of what this connection
        wrote. A comment has no natural identity, so a resubmission is only
        recognisable by its content — and the board is the copy that survives a
        restart, a second process, and a comment left by someone else.
        """
        try:
            existing = self.client.list_comments(task_id)
        except VikunjaError as exc:
            raise ToolError(f"{exc} Nothing was written.") from exc
        for comment in existing:
            if html_to_text(comment.get("comment") or "").strip() == text:
                return comment
        return None

    def _duplicate_comment(
        self, project_id: int, ticket, comment: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "added": False,
            "reason": "this exact comment is already on the task; returning it "
            "instead of writing a second copy",
            "task_id": ticket.task_id,
            "title": ticket.title,
            "comment_id": comment.get("id"),
            "project": self.config.project_title,
            "project_id": project_id,
            "url": ticket.url(self.config.frontend_url),
        }

    # -- tools -------------------------------------------------------------

    def tools(self) -> list[Tool]:
        """The complete, fixed set of operations this connection can perform.

        Fixed for the lifetime of the process — which is what
        ``capabilities.tools.listChanged: False`` promises a client. The
        operational tools are included or omitted once, from configuration read
        at startup, and never appear and disappear underneath a live session.
        """
        return [
            *self._vikunja_tools(),
            *self._operational_tools(),
            *self._website_tools(),
            *self._paying_page_tools(),
        ]

    def _paying_page_tools(self) -> list[Tool]:
        """The authenticated page read, or nothing at all when unconfigured."""
        if not self.paying_page_fetch_enabled:
            return []
        return [
            Tool(
                name="fetch_test_paying_page",
                title="Fetch a page of the AI Server website as the test paying user",
                description=(
                    "Fetch one page of the AI Server website by path — "
                    '"/cockpit/<slug>", "/report/<slug>", '
                    '"/portfolio-drivers/<slug>", "/thesis-validation/<slug>", '
                    '"/news-impact/<slug>", "/portfolio-analysis/<slug>" — '
                    "rendered for the **test paying user**, and read the HTML "
                    "the server returned with its HTTP status and response "
                    "headers. Use it to see paying-tier content that "
                    "fetch_public_page cannot reach, which answers as an "
                    "anonymous visitor and gets the redirect to the login page "
                    "instead. Which user this is, is fixed on the server: there "
                    "is no way to ask for a different one, it is never a "
                    "trusted or admin user, and the read is refused outright if "
                    "that user is no longer a paying one. Routes that manage "
                    "authentication (login, logout, OAuth, callbacks), billing, "
                    "uploads, imports, refreshes, report generation and "
                    "administration are refused, and the request is always a "
                    "GET — nothing here can change the application's state. No "
                    "session cookie, key or token is returned; cookie values "
                    "are withheld and their names listed. Redirects are "
                    "reported, not followed. A body over 400000 bytes is cut, "
                    "and says so."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                'A path on the site, starting with "/" and '
                                "optionally carrying a query string. Not a full "
                                "URL, and not a user: the site and the identity "
                                "are both fixed by server configuration."
                            ),
                        }
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                annotations={
                    "readOnlyHint": True,
                    "idempotentHint": True,
                    # One instance, one identity, both fixed by configuration.
                    "openWorldHint": False,
                },
                run=lambda arguments: self.fetch_test_paying_page(
                    arguments.get("path")
                ),
            ),
        ]

    def _website_tools(self) -> list[Tool]:
        """The public page fetch, or nothing at all when unconfigured."""
        if not self.page_fetch_enabled:
            return []
        return [
            Tool(
                name="fetch_public_page",
                title="Fetch a public page of the AI Server website",
                description=(
                    "Fetch one page of the public AI Server website by path — "
                    '"/", "/about", "/how-it-works", "/terms?lang=sv" — and read '
                    "the HTML the server actually returned, with its HTTP status "
                    "code and response headers. Use this to review the live site "
                    "without depending on web browsing, which Cloudflare and bot "
                    "protection sit in front of. The request is made as an "
                    "anonymous visitor carrying no cookie, no API key and no "
                    "session, so a page that requires a login answers with its "
                    "redirect to the login page and that redirect is what you "
                    "get: this cannot read a page a stranger cannot read. "
                    "Redirects are reported, not followed — fetch the Location "
                    "yourself if you want the next page. Cookie values are "
                    "withheld (their names are listed). A body over "
                    "400000 bytes is cut, and says so. Read-only; it changes "
                    "nothing on the site."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                'A path on the site, starting with "/" and '
                                "optionally carrying a query string. Not a full "
                                "URL: the site is fixed by configuration and "
                                "cannot be changed from here."
                            ),
                        }
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                annotations={
                    "readOnlyHint": True,
                    "idempotentHint": True,
                    # One host, fixed by configuration, with no argument that
                    # could aim it anywhere else — a closed world of one site.
                    "openWorldHint": False,
                },
                run=lambda arguments: self.fetch_public_page(arguments.get("path")),
            ),
        ]

    def _operational_tools(self) -> list[Tool]:
        """The three AI Server reads, or nothing at all when unconfigured."""
        if not self.operational_reads_enabled:
            return []
        return [
            Tool(
                name="get_repository_state",
                title="Get the AI Server repository state",
                description=(
                    "Read which commit the AI Server investment repository is "
                    "checked out at: branch, commit hash, the commit's subject "
                    "and timestamp, and whether the working tree is clean. Use "
                    "this to know what code is deployed. Counts of changed and "
                    "untracked files are returned, not filenames. Read-only; it "
                    "runs no commands you name and cannot write to the "
                    "repository."
                ),
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                annotations={
                    "readOnlyHint": True,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                run=lambda arguments: self.repository_state(),
            ),
            Tool(
                name="get_pipeline_status",
                title="Get the latest nightly pipeline status",
                description=(
                    "Read the most recent nightly investment pipeline run: its "
                    "overall status, when it started and finished, every stage "
                    "with that stage's own outcome, which stages did not "
                    "succeed, what S7 did to the intelligence chain, and whether "
                    "each portfolio got a report from this run. Note that a "
                    "degraded stage does not by itself make the run degraded — "
                    "only report generation does — so read the run status and "
                    "the stage list as two separate facts. `run_present: false` "
                    "means no run has ever been recorded, which is not a "
                    "failure. Read-only."
                ),
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                annotations={
                    "readOnlyHint": True,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                run=lambda arguments: self.pipeline_status(),
            ),
            Tool(
                name="get_system_health",
                title="Get the AI Server system health",
                description=(
                    "Read the AI Server system-health checks: database backup "
                    "age, disk usage, Docker containers, the scheduled jobs, "
                    "database connectivity and API status, plus one overall "
                    "status folded from them (worst wins; `unknown` means a "
                    "check could not be read, which is not `ok`). The checks run "
                    "when you ask — there is no cached result. Log output and "
                    "crontab lines are deliberately not included. Read-only."
                ),
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                annotations={
                    "readOnlyHint": True,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                run=lambda arguments: self.system_health(),
            ),
        ]

    def _vikunja_tools(self) -> list[Tool]:
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
                name="search_tasks",
                title="Search the Vikunja tasks",
                description=(
                    "Find tasks on the "
                    f"{self.config.project_title} board whose title or "
                    "description contains a piece of text. Use this to answer "
                    "'is there already a ticket about X' and 'has this been done "
                    "before' — unlike list_open_tasks, this one can see finished "
                    "tasks, and it is the right tool to check before asking for a "
                    "new ticket to be created. Matching is a plain "
                    "case-insensitive substring, not a query language: no "
                    "wildcards, no boolean operators. Each result says whether "
                    "the match was in the title or the description. Returns no "
                    "descriptions or comments — read one task with get_task. "
                    "Read-only."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": (
                                "The text to look for, matched as a "
                                "case-insensitive substring of the title or the "
                                "description."
                            ),
                        },
                        "status": {
                            "type": "string",
                            "enum": list(SEARCH_STATUSES),
                            "description": (
                                "Which tasks to search: 'open' (the default), "
                                "'done', or 'any'. Use 'done' or 'any' to check "
                                "whether something was already handled."
                            ),
                        },
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
                annotations={
                    "readOnlyHint": True,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                run=lambda arguments: self.search_tasks(
                    str(arguments["text"]),
                    status=(
                        STATUS_OPEN
                        if arguments.get("status") is None
                        else str(arguments["status"])
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
            Tool(
                name="update_task",
                title="Update a Vikunja task's title or description",
                description=(
                    "Replace the title, the description, or both, on one "
                    f"existing task on the {self.config.project_title} board. "
                    "This is a two-step tool and it writes to a real board. Call "
                    "it first without approval_token: it changes nothing and "
                    "returns the exact current value beside the exact proposed "
                    "one, with an approval_token for that one change. Show the "
                    "user both values, and only once they have explicitly "
                    "approved that exact change, call it again with the "
                    "identical arguments plus the approval_token. Submit "
                    "complete replacement text — there is no partial or "
                    "find-and-replace edit. Identify the task by its Vikunja "
                    "task id (the number in a /tasks/<id> URL), never by a #NN "
                    "title prefix or a board position. It cannot change status, "
                    "bucket, labels, assignees, priority or due dates, and it "
                    "cannot delete anything. Asking for the value a task already "
                    "holds changes nothing and is reported as such."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "integer",
                            "description": (
                                "Vikunja's immutable task id. A task outside "
                                f"{self.config.project_title} is refused."
                            ),
                        },
                        "title": {
                            "type": "string",
                            "description": (
                                "The complete new title, as the user approved "
                                "it. Omit to leave the title alone."
                            ),
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "The complete new description as plain text, as "
                                "the user approved it; blank lines separate "
                                "paragraphs. This replaces the whole "
                                "description. Omit to leave it alone."
                            ),
                        },
                        "approval_token": {
                            "type": "string",
                            "description": (
                                "The token returned by the preview call for this "
                                "exact change. Omit on the first call. Supply it "
                                "only after the user has approved the exact "
                                "values that preview showed."
                            ),
                        },
                    },
                    "required": ["task_id"],
                    "additionalProperties": False,
                },
                annotations={
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                run=lambda arguments: self.update_task(
                    int(arguments["task_id"]),
                    title=(
                        None
                        if arguments.get("title") is None
                        else str(arguments["title"])
                    ),
                    description=(
                        None
                        if arguments.get("description") is None
                        else str(arguments["description"])
                    ),
                    approval_token=(
                        None
                        if arguments.get("approval_token") is None
                        else str(arguments["approval_token"])
                    ),
                ),
            ),
            Tool(
                name="add_task_comment",
                title="Comment on a Vikunja task",
                description=(
                    "Add one plain-text comment to an existing task on the "
                    f"{self.config.project_title} board. This is a two-step tool "
                    "and it writes to a real board. Call it first without "
                    "approval_token: it writes nothing and returns the exact "
                    "comment and the task it would go on, with an "
                    "approval_token. Show the user that exact comment, and only "
                    "once they have explicitly approved it, call again with the "
                    "identical comment plus the approval_token. Comments are "
                    "append-only: nothing here can edit or delete an existing "
                    "one, and submitting a comment the task already carries "
                    "returns the one that is there rather than writing a second "
                    "copy."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "integer",
                            "description": (
                                "Vikunja's immutable task id. A task outside "
                                f"{self.config.project_title} is refused."
                            ),
                        },
                        "comment": {
                            "type": "string",
                            "description": (
                                "The complete comment as plain text, as the user "
                                "approved it; blank lines separate paragraphs."
                            ),
                        },
                        "approval_token": {
                            "type": "string",
                            "description": (
                                "The token returned by the preview call for this "
                                "exact comment. Omit on the first call."
                            ),
                        },
                    },
                    "required": ["task_id", "comment"],
                    "additionalProperties": False,
                },
                annotations={
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                run=lambda arguments: self.add_task_comment(
                    int(arguments["task_id"]),
                    str(arguments["comment"]),
                    approval_token=(
                        None
                        if arguments.get("approval_token") is None
                        else str(arguments["approval_token"])
                    ),
                ),
            ),
        ]
