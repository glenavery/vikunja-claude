"""A task is identified by its board and its number on that board (task 659).

Task 649 made ``project_id + task_number`` the identifier the tools accept,
and published Vikunja's immutable row id beside every answer as
``vikunja_task_id`` — named so it could not be mistaken for something
callable, on the reasoning that debug metadata is harmless.

It was not. A second number in the answer is a second number a reader can
quote, and one duly did: a branch and a commit message went out naming
"task 659" for the ticket the board shows as **#658**, because the answer
carried both numbers and only one of them is the ticket. Naming a field
carefully is not the same as not publishing it, and an id the tools refuse to
*accept* is an id they have no reason to *hand out*.

So the id is internal now. It is still load-bearing there — the resolver binds
it, the approval token binds it, the mutation ledger records it — and that is
the distinction these tests hold: a row is not an identity.

THE ``url`` EXEMPTION IS GONE (task 663). The first pass left one carrier and
named it: ``url`` was ``/tasks/<id>``, on the reasoning that Vikunja's only
task route is a locator rather than an identifier. That was the implementing
pass granting itself an exception to an explicit requirement, and the argument
against it was already written down a few files away — ``web._ticket_href``,
added by this same ticket's later pass, refuses the row id outright because
"a page that links the row id is a page that teaches the row id".

It duly bit. On 2026-08-24 a session read ``/tasks/663`` out of a
``search_tasks`` answer and handed it to the operator as the address of board
**#662** — the exact confusion the whole scheme exists to prevent, arriving
through the one hole left open on the grounds that nobody would quote it. A
locator is what a reader copies.

So there is no exemption now, on any surface, and the guards below ask the
strong form in BOTH directions: no published integer is the row id, and no
published string contains ``/tasks/<row id>``. ``Ticket.url()`` is deleted
rather than left unused, so there is no helper to rebuild it with.

THE SECOND PASS covers the LAUNCHER, which the first missed. The MCP is what a
connector reads; the launcher at :3460 is what a person reads and what every
run is launched from, and it was still publishing the row id in the preview
payload, in the launch record spread into ``work`` and ``/launches``, on the
console and preview pages, and in ``VIKUNJA_TASK_ID`` in the child's own
environment. Nothing read the last one — two numbers of exposure bought for
nothing. Same rule, same walk, applied to those surfaces.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from vikunja_claude import web

from .fakes import PROJECT_ID, task
from .support import McpTestCase, ServiceTestCase

#: The key task 649 published and task 659 removed.
RETIRED_KEY = "vikunja_task_id"


def _strings(payload, out=None):
    """Every string the payload publishes, with the key path that carries it.

    The integer walk below cannot see a row id spelled inside a URL, which is
    how the exemption survived task 659: ``url`` was a string, so the strong
    "no published integer is this row's id" guard passed straight over it.
    """
    out = [] if out is None else out
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                _strings(value, out)
            elif isinstance(value, str):
                out.append((key, value))
    elif isinstance(payload, list):
        for item in payload:
            _strings(item, out)
    return out


def _numbers(payload, out=None):
    """Every integer the payload publishes, with the key path that carries it."""
    out = [] if out is None else out
    if isinstance(payload, dict):
        for key, value in payload.items():
            _numbers(value, out) if isinstance(value, (dict, list)) else (
                out.append((key, value)) if isinstance(value, int)
                and not isinstance(value, bool) else None
            )
    elif isinstance(payload, list):
        for item in payload:
            _numbers(item, out)
    return out


class TestTheAnswerCarriesNoRowId(McpTestCase):
    """Read, list, search, create — none of them publishes the row id."""

    def _answers(self) -> dict[str, dict]:
        preview = self.service.update_task(8, title="Renamed")
        return {
            "get_task": self.service.get_task(8),
            "list_open_tasks": self.service.list_open_tasks(),
            "search_tasks": self.service.search_tasks("Vikunja", status="any"),
            "list_recently_done": self.service.list_recently_done(),
            "set_task_status (preview)": self.service.set_task_status(
                task_number=8, bucket="Done"),
            "create_task": self.service.create_task(
                PROJECT_ID, "A ticket", "A body."),
            "update_task (preview)": preview,
            "update_task (applied)": self.service.update_task(
                8, title="Renamed", approval_token=preview["approval_token"]),
        }

    def test_no_answer_carries_the_retired_key(self):
        for name, answer in self._answers().items():
            with self.subTest(tool=name):
                self.assertNotIn(RETIRED_KEY, str(answer.keys()), name)
                self.assertNotIn(RETIRED_KEY, repr(answer), name)

    def test_no_answer_carries_a_row_id_under_any_other_key(self):
        """The stronger form: renaming the key would not satisfy this ticket.

        Asked PER TASK, against that task's own row id, because the fixture
        deliberately overlaps the two sets — task 649 built it so that some
        board numbers ARE some other task's id, which is the confusion the
        whole thing exists to catch. So "no published integer is any row id"
        cannot be asserted; "no published integer is THIS row's id" can, and
        the fixture guarantees a task's number never equals its own id.

        ``url`` is a string and so is not reached by this walk — the one
        exemption, named in the module docstring and pinned by the test below.
        """
        for name, answer in self._answers().items():
            for entry in answer.get("tasks", [answer]):
                number = entry.get("task_number")
                if number is None:
                    continue
                own_id = self.vikunja.id_of(number)
                self.assertNotEqual(number, own_id, "fixture cannot catch a swap")
                for key, value in _numbers(entry):
                    with self.subTest(tool=name, key=key):
                        self.assertNotEqual(
                            value, own_id, f"{name}.{key} publishes the row id")

    def test_no_answer_spells_the_row_id_inside_a_string(self):
        """The half task 659 missed: ``url`` was a string, so the integer walk
        above passed straight over the one field that published the id.

        Asked as "no published string contains this row's ``/tasks/<id>``"
        rather than "no string contains these digits", because a description or
        a comment may legitimately mention a number. It is the ROUTE that names
        a task, and the route is what a reader copies.
        """
        for name, answer in self._answers().items():
            for entry in answer.get("tasks", [answer]):
                number = entry.get("task_number")
                if number is None:
                    continue
                route = f"/tasks/{self.vikunja.id_of(number)}"
                for key, value in _strings(entry):
                    with self.subTest(tool=name, key=key):
                        self.assertNotIn(
                            route, value, f"{name}.{key} publishes the row id")

    def test_no_answer_carries_a_url_key_at_all(self):
        """Stated separately from the substring guard above, which a link to
        some OTHER task's route would satisfy while still being wrong."""
        for name, answer in self._answers().items():
            for entry in answer.get("tasks", [answer]):
                with self.subTest(tool=name):
                    self.assertNotIn("url", entry, f"{name} republishes a url")


class TestTheIdentityIsTheBoardAndTheNumber(McpTestCase):
    def test_every_answer_names_both_halves(self):
        """A project-local number means nothing without its project."""
        for name, answer in {
            "get_task": self.service.get_task(8),
            "list_open_tasks": self.service.list_open_tasks(),
        }.items():
            with self.subTest(tool=name):
                self.assertIn("project_id", answer)
                self.assertIn("project", answer)


class TestTheLauncherPublishesNoRowId(ServiceTestCase):
    """The surfaces a person reads, and the environment a run inherits.

    Fixture task: row id 9, board number 8, legacy title prefix 33 — three
    numbers, all different, so a payload echoing the wrong one cannot pass by
    coincidence.
    """

    ROW_ID = 9

    def _ticket(self):
        return self.service.get_by_task_number(8)

    def test_the_preview_payload_carries_no_row_id(self):
        for key, value in _numbers(self.service.preview(self._ticket())):
            with self.subTest(key=key):
                self.assertNotEqual(
                    value, self.ROW_ID, f"preview.{key} publishes the row id")

    def test_the_launch_response_and_the_running_list_carry_no_row_id(self):
        self.alive_pids.add(4242)
        result = self.service.work(self._ticket())
        for name, payload in (("work", result),
                              ("running", self.launcher.running())):
            for key, value in _numbers(payload):
                with self.subTest(surface=name, key=key):
                    self.assertNotEqual(
                        value, self.ROW_ID, f"{name}.{key} publishes the row id")

    def test_the_run_inherits_the_board_number_and_not_the_row_id(self):
        """Nothing ever read VIKUNJA_TASK_ID, so it was pure exposure — and an
        env var is the most quotable form there is: it reaches the shell."""
        self.service.work(self._ticket())
        env = self.spawn.calls[0]["env"]
        self.assertEqual(env["VIKUNJA_TASK_NUMBER"], "8")
        self.assertNotIn("VIKUNJA_TASK_ID", env)
        self.assertNotIn("VIKUNJA_TICKET", env)

    def test_the_rendered_pages_address_the_board_number(self):
        """A page that links /task/<row id> is a page that teaches the row id,
        which is how it kept spreading. No exemption now (task 663): the
        "open in Vikunja" and "back to Vikunja" links carried the id in their
        HREF while their visible text said neither, and the href is the half a
        reader copies."""
        self.alive_pids.add(4242)
        data = self.service.preview(self._ticket())
        self.service.work(self._ticket())
        pages = {
            "ticket_page": web.ticket_page(data),
            "launch_page": web.launch_page(
                number=8, task_id=self.ROW_ID, reference="#8",
                summary="Back up Vikunja database"),
            "console": web.console(
                "AI Alpha Engine", "/repo", self.launcher.running(),
                self.launcher.recent()),
        }
        for name, html in pages.items():
            with self.subTest(page=name):
                self.assertNotIn(f"/task/{self.ROW_ID}", html)
                self.assertNotIn(f"/tasks/{self.ROW_ID}", html)
                self.assertNotIn(f"task id {self.ROW_ID}", html)
                self.assertNotIn(f"task {self.ROW_ID}", html)
                self.assertIn("#8", html, "the page still names the ticket")

    def test_the_preview_payload_publishes_no_url(self):
        self.assertNotIn("url", self.service.preview(self._ticket()))

    def test_the_launch_response_publishes_no_url(self):
        self.alive_pids.add(4242)
        self.assertNotIn("url", self.service.work(self._ticket()))

    def test_the_prompt_every_run_follows_names_no_task_route(self):
        """The highest-leverage carrier of all: the prompt is copied verbatim
        into every run, so whatever number it names is the number that reaches
        branches, commit messages and the closing vkctl call.

        It printed `Vikunja URL: …/tasks/<row id>` two lines above the rule
        telling the run never to read a number out of a /tasks/<id> URL — the
        warning and the hazard in one document.
        """
        prompt = self.service.prompt_for(self._ticket())
        self.assertNotIn(f"/tasks/{self.ROW_ID}", prompt)
        self.assertNotIn("Vikunja URL:", prompt)
        self.assertIn("#8", prompt, "the prompt still names the ticket")
        # The rule against reading a number out of that URL stays: the route
        # is still how a person arrives, it is just not printed here.
        self.assertIn("/tasks/<id>", prompt)


class TestARunIsNamedForTheBoardAndKeyedByTheRow(ServiceTestCase):
    """A run has one human name, and the row id is not in it (task 762).

    The launcher stopped publishing the row id as a NUMBER at task 659, but the
    run's log file was still *named* from it — and a path is the most quotable
    form there is: `work` returns it, `/launches` carries it and the console
    prints it. So board ticket #714's output went to `task-715-<stamp>.log`, a
    filename naming a different, real ticket, sitting one directory away from
    the `task-714` worktree the very same run was working in. Two spellings of
    one run is how a reader quotes the wrong number.

    What did NOT change is the lock. The row id is immutable and the board
    number is editable, so the key that must never collide keeps the id — which
    is the distinction these tests hold from both ends: the name a person reads
    is the board's, the key the runner locks on is the row's, and asking for one
    by the other finds nothing.

    Fixture task: row id 9, board number 8. Different numbers, so a name built
    from the wrong one cannot pass by coincidence.
    """

    ROW_ID = 9
    NUMBER = 8
    #: The fake serves this one with no project-local index — the only case with
    #: no board number to name a run with.
    NUMBERLESS = 91

    def work(self) -> dict:
        self.alive_pids.add(4242)
        return self.service.work(self.service.get_by_task_number(self.NUMBER))

    def test_the_run_log_is_named_for_the_board_number(self):
        name = Path(self.work()["log_file"]).name
        self.assertTrue(name.startswith(f"task-{self.NUMBER}-"), name)
        self.assertTrue(name.endswith(".log"), name)

    def test_the_run_log_is_named_like_the_worktree_it_is_the_log_of(self):
        """One function names both (`worktree.run_name`), so a run cannot end up
        called one thing in the checkout and another in the log directory —
        which is exactly what #714 saw."""
        record = self.work()
        self.assertTrue(
            Path(record["log_file"]).name.startswith(
                Path(record["workdir"]).name + "-"
            ),
            f'{record["log_file"]} is not the log of {record["workdir"]}',
        )

    def test_no_surface_a_person_reads_spells_a_run_named_from_the_row_id(self):
        """Asked as "no surface carries that NAME", not "these digits do not
        appear": the row id is a small integer and the timestamp is full of
        digits, so a substring test on `9` alone could not fail for the right
        reason. Both spellings are refused — `task-9-` is what the launcher
        wrote, and `row-9` is what the numberless fallback would spell.
        """
        record = self.work()
        surfaces = {
            "work": json.dumps(record),
            "running": json.dumps(self.launcher.running()),
            "launches": json.dumps(self.launcher.recent()),
            "run_status": json.dumps(self.launcher.run_status(self.ROW_ID)),
            "console": web.console(
                "AI Alpha Engine", "/repo",
                self.launcher.running(), self.launcher.recent()),
        }
        for surface, blob in surfaces.items():
            with self.subTest(surface=surface):
                self.assertNotIn(f"task-{self.ROW_ID}-", blob)
                self.assertNotIn(f"row-{self.ROW_ID}", blob)

    def test_the_lock_is_still_keyed_by_the_row_id(self):
        """Unchanged, and deliberately so. Two runs of one ticket must never
        both hold it, which needs a key that cannot be edited on the board."""
        self.work()
        self.assertTrue(
            (self.config.lock_dir / f"task-{self.ROW_ID}.json").exists())
        self.assertFalse(
            (self.config.lock_dir / f"task-{self.NUMBER}.json").exists())

    def test_the_ledger_records_the_row_id_and_the_published_view_drops_it(self):
        """The correlation key moved out of the filename and into a field.

        It has to live somewhere: a `launched` record is found again from the id
        its lock is keyed by, long after the lock is gone. So the launch log —
        this service's own ledger — records it, and `recent()`, the one place
        that ledger is published, projects it away.
        """
        self.work()
        events = [
            json.loads(line)
            for line in self.config.log_path.read_text(
                encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [r.get("task_id") for r in events if r.get("event") == "launched"],
            [self.ROW_ID],
        )
        for record in self.launcher.recent():
            with self.subTest(event=record.get("event")):
                self.assertNotIn("task_id", record)

    def test_a_status_read_asks_by_the_row_and_the_board_number_finds_nothing(self):
        """The filename used to BE the correlation key, so renaming it could
        have lost the run this read is asked about — silently, as `none`.

        The second half is the distinction stated the other way: 8 and 9 are
        both real numbers here, and only one of them addresses this run.
        """
        self.work()
        status = self.launcher.run_status(self.ROW_ID)
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["number"], self.NUMBER)
        self.assertEqual(self.launcher.run_status(self.NUMBER)["state"], "none")

    def test_a_ticket_with_no_board_number_is_spelled_row(self):
        """The fallback cannot be a bare `task-<id>`: the two numbering spaces
        overlap, so that is a different, real ticket's name."""
        self.vikunja.layout["Ready"].append(
            task(self.NUMBERLESS, "Indexless", "2026-07-26T07:00:00Z", "body",
                 index=None)
        )
        record = self.service.work(self.service.get_task(self.NUMBERLESS))
        self.assertTrue(
            Path(record["log_file"]).name.startswith(f"row-{self.NUMBERLESS}-"),
            record["log_file"],
        )


# "One number on two boards is two tasks" is task 649's property and lives in
# tests/test_mcp_projects.py, which has the fixture for it. Removing the row id
# does not touch it, and restating it here would be a second copy to keep true.

if __name__ == "__main__":
    unittest.main()
