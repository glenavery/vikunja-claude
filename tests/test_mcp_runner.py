"""Starting a ticket through the runner from the connector (task 726).

The MCP could file work, comment on it, move it and close it, but not ask for
any of it to be *worked*: starting a run meant shell access to the launcher on
the host. Task 726 adds one action for that, and its whole design is what it
refuses to own. The runner already resolves the task, assembles the prompt,
chooses the executor, names the model, holds the per-task lock, drives the
Claude Code harness and its worktree, and reports back on the board. This side
sends one request and reads the answer.

So these tests are mostly about ABSENCE, and each names the thing that would
have to be true for the absence to be real:

* **The seat is not here.** The model the run drives is read back out of the
  runner's answer, never named on this side — so moving the approved
  ``local_coding`` seat in the investment repository's ``models.json`` moves
  this tool with it, with nothing here to edit. Asserted twice: behaviourally,
  by answering with a different model and reading it back, and by scanning the
  two modules for a model id, an Ollama setting or a context size.
* **Nothing is started until the task can be named**, and nothing at all is
  started without a second, approved call. Every other tool on this surface
  that changes an existing task is two-step; this one starts an agent editing a
  repository, so it is not the exception.
* **A refusal is a refusal.** Unknown executor, run already in flight, runner
  unreachable — each comes back as an error saying nothing was started. There
  is no path here that answers a refused launch with a launch on something
  else, which for an executor would mean answering "run this locally" by
  running it on a paid model.
* **The row id stays internal.** It is what the runner's route is addressed by,
  because Vikunja hands ids out globally and a board number does not (a number
  the runner does not work resolves to a *different real ticket*, which is the
  confusion the whole identifier scheme exists to prevent). It is in the
  request and in nothing that comes back — not in the answer, and not in the
  refusal text, whose 404 form spells it (task 663).
"""

from __future__ import annotations

import json
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from vikunja_claude.executors import DEFAULT_EXECUTOR, LOCAL_EXECUTOR
from vikunja_claude.launcher import RUN_STATES
from vikunja_claude.mcp import McpProtocol
from vikunja_claude.mcp_service import CHANGE_COMMENT, CHANGE_RUN, McpService, ToolError
from vikunja_claude.runner import RunnerClient, RunnerError
from vikunja_claude.vikunja import VikunjaClient

from .fakes import PROJECT_ID, TRADER_PROJECT_ID, FakeVikunja
from .support import McpTestCase, make_mcp_config

#: The fixture's Engine board: #8 is row id 9, sitting in Ready. The two
#: numbers differ, and #9 is a different real task, which is what makes an
#: assertion about which one travels worth making.
READY = 8
READY_ROW_ID = 9
#: The Trader board's #2 — a task on an approved board that the runner, which
#: works one board, does not have.
TRADER = 2

#: What the runner answers a launch with: `LaunchRecord` plus the two fields
#: `TicketService.work` adds. Copied from that shape rather than invented, so a
#: field this side reads is a field the runner actually sends.
LAUNCHED = {
    "launched": True,
    "moved_to": "In Progress",
    "number": READY,
    "reference": f"#{READY}",
    "pid": 4242,
    "started_at": "2026-09-01T10:15:00+0200",
    # The row id is in this path, which is why it is not in what we return.
    "log_file": f"/home/glen/.local/state/vikunja-claude/runs/task-{READY_ROW_ID}-20260901-101500.log",
    "workdir": "/home/glen/stacks/investment",
    "executor": DEFAULT_EXECUTOR,
    "model": None,
}


#: What the runner answers a status read with: `Launcher.run_status`'s shape,
#: pinned to that method below rather than trusted, so a field this side reads
#: is a field the runner actually sends. A `lost` run, because that is the one
#: this fixture exists for.
STATUS = {
    "number": READY,
    "reference": f"#{READY}",
    "state": "lost",
    "alive": False,
    "executor": LOCAL_EXECUTOR,
    "model": "qwen38-27b-abl:256k",
    "started_at": "2026-09-01T14:30:42+0000",
    "workdir": "/home/glen/stacks/investment",
    # Both of these are the runner's to send and this side's to withhold.
    "pid": 2918949,
    "log_file": f"/home/glen/.local/state/vikunja-claude/runs/task-{READY_ROW_ID}-20260901-143042.log",
    "finished_at": None,
    "exit_status": None,
    "timed_out": False,
    # Task 754: when this run's ending was recorded, having been missed. None
    # here, because this fixture is a run nobody has reconciled yet.
    "reconciled_at": None,
    "output_tail": ["[claude-code:unrecognized_model] {\"model\":\"m\"}"],
    "output_truncated": False,
    "output_at": "2026-09-01T14:30:45+0000",
}


def _code_without_prose(path: Path) -> str:
    """The module's code with its docstrings and comments removed.

    Comments are absent from the AST to begin with; the docstrings are dropped
    node by node. Every other string literal survives, because a hard-coded
    model id would BE a string literal and a scan that dropped those would be
    blind to the thing it is asking about.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body.pop(0)
            if not body:
                body.append(ast.Pass())
    return ast.unparse(ast.fix_missing_locations(tree))


class RecordingRunner:
    """Stands in for the launcher, recording what it was asked for.

    The verb is recorded beside the path (task 751) because the seam now
    carries two requests and the whole claim about the second one is that it
    starts nothing. A stand-in that saw only paths could not tell a launch from
    a read, so it could not witness that claim either.
    """

    def __init__(self, answer=None, fail: Exception | None = None,
                 status_answer=None):
        self.answer = LAUNCHED if answer is None else answer
        self.status_answer = STATUS if status_answer is None else status_answer
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    @property
    def paths(self) -> list[str]:
        """Just the paths, for the assertions that are about addressing."""
        return [path for _, path in self.calls]

    @property
    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    def __call__(self, path: str, method: str):
        self.calls.append((method, path))
        if self.fail is not None:
            raise self.fail
        return self.status_answer if method == "GET" else self.answer


class RunnerTestCase(McpTestCase):
    """An McpService whose runner is a recording stand-in."""

    runner_answer: dict | None = None
    runner_fails: Exception | None = None

    def setUp(self) -> None:
        super().setUp()
        self.runner_transport = RecordingRunner(
            answer=self.runner_answer, fail=self.runner_fails
        )
        self.service = McpService(
            self.config,
            self.client,
            runner=RunnerClient("http://127.0.0.1:3460", self.runner_transport),
        )
        self.protocol = McpProtocol(self.service.tools())

    def start(self, task_number: int = READY, **kwargs) -> dict:
        """Preview, then start with the token the preview issued."""
        preview = self.service.start_task_run(task_number=task_number, **kwargs)
        return self.service.start_task_run(
            task_number=task_number,
            approval_token=preview["approval_token"],
            **kwargs,
        )


class TestTheRequestTheRunnerGets(RunnerTestCase):
    def test_one_approved_call_sends_one_request_to_the_work_route(self):
        result = self.start()

        self.assertEqual(self.runner_transport.paths, [f"/task/{READY_ROW_ID}/work"])
        self.assertTrue(result["started"])
        self.assertEqual(result["reference"], f"#{READY}")
        self.assertEqual(result["moved_to"], "In Progress")

    def test_the_executor_is_forwarded_as_a_name(self):
        """Forwarded, not resolved: which names exist and what each one means
        is the runner's, and it refuses one it does not have."""
        self.start(executor=LOCAL_EXECUTOR)
        self.assertEqual(
            self.runner_transport.paths,
            [f"/task/{READY_ROW_ID}/work?executor={LOCAL_EXECUTOR}"],
        )

    def test_omitting_the_executor_sends_none_at_all(self):
        """So the runner applies its OWN configured default. Sending a name
        here would be this side deciding what that default is."""
        self.start()
        self.assertNotIn("executor", self.runner_transport.paths[0])

    def test_the_board_number_is_never_what_the_route_is_addressed_by(self):
        """#8 and row id 9 are both real tasks on this fixture's board, so a
        route built from the wrong one would launch a plausible wrong ticket
        rather than failing."""
        self.start()
        self.assertNotIn(f"/task/{READY}/", self.runner_transport.paths[0])


class TestNothingStartsWithoutAnApprovedSecondCall(RunnerTestCase):
    def test_the_preview_names_the_launch_and_sends_nothing(self):
        preview = self.service.start_task_run(task_number=READY, executor=LOCAL_EXECUTOR)

        self.assertFalse(preview["started"])
        self.assertTrue(preview["approval_required"])
        self.assertEqual(preview["bucket"], "Ready")
        self.assertEqual(preview["executor"], LOCAL_EXECUTOR)
        self.assertTrue(preview["approval_token"])
        self.assertEqual(self.runner_transport.paths, [])

    def test_a_token_for_one_executor_cannot_start_another(self):
        """The half a user was shown is which model this runs on. An approval
        that could be spent on a different one would be an approval for a
        different launch."""
        preview = self.service.start_task_run(
            task_number=READY, executor=LOCAL_EXECUTOR)
        with self.assertRaises(ToolError):
            self.service.start_task_run(
                task_number=READY,
                executor=DEFAULT_EXECUTOR,
                approval_token=preview["approval_token"],
            )
        self.assertEqual(self.runner_transport.paths, [])

    def test_a_token_is_single_use(self):
        preview = self.service.start_task_run(task_number=READY)
        token = preview["approval_token"]
        self.service.start_task_run(task_number=READY, approval_token=token)
        with self.assertRaises(ToolError):
            self.service.start_task_run(task_number=READY, approval_token=token)
        self.assertEqual(len(self.runner_transport.paths), 1)

    def test_a_comment_approval_cannot_be_redeemed_as_a_launch(self):
        preview = self.service.add_task_comment(task_number=READY, comment="hello")
        with self.assertRaises(ToolError):
            self.service.start_task_run(
                task_number=READY, approval_token=preview["approval_token"])
        self.assertEqual(self.runner_transport.paths, [])

    def test_a_launch_approval_cannot_be_redeemed_as_a_comment(self):
        preview = self.service.start_task_run(task_number=READY)
        with self.assertRaises(ToolError):
            self.service.add_task_comment(
                task_number=READY,
                comment="hello",
                approval_token=preview["approval_token"],
            )

    def test_the_kinds_are_distinct(self):
        self.assertNotEqual(CHANGE_RUN, CHANGE_COMMENT)


class TestNothingReachesTheRunnerUntilTheTaskIsNamed(RunnerTestCase):
    def test_a_number_no_board_carries_never_reaches_the_runner(self):
        with self.assertRaises(ToolError) as caught:
            self.service.start_task_run(task_number=4321)
        self.assertIn("Nothing was changed", str(caught.exception))
        self.assertEqual(self.runner_transport.paths, [])

    def test_a_project_this_connection_does_not_serve_is_refused_here(self):
        with self.assertRaises(ToolError):
            self.service.start_task_run(task_number=READY, project_id=7)
        self.assertEqual(self.runner_transport.paths, [])

    def test_a_number_is_resolved_on_the_board_it_was_read_from(self):
        """#2 is task 40 on the Trader board and task 1 on the Engine board.
        The row id that travels must be the one the caller named."""
        self.start(task_number=TRADER, project_id=TRADER_PROJECT_ID)
        self.assertEqual(self.runner_transport.paths, ["/task/40/work"])


class TestARefusalIsARefusal(RunnerTestCase):
    def test_a_run_already_in_flight_comes_back_in_the_runners_own_words(self):
        self.runner_transport.fail = RunnerError(
            "The ticket runner refused the run (409): Claude is already "
            f"working #{READY} (pid 4242, started 2026-09-01T09:00:00+0200). "
            "Refusing to launch a second run.",
            status=409,
        )
        with self.assertRaises(ToolError) as caught:
            self.start()
        message = str(caught.exception)
        self.assertIn("already working", message)
        self.assertIn(f"#{READY}", message)

    def test_an_executor_the_runner_does_not_have_is_refused_with_the_real_ones(self):
        """Never softened into a run on something else: the alternative to a
        local run is a paid one, so a fallback here would turn a typo into a
        bill."""
        self.runner_transport.fail = RunnerError(
            "The ticket runner refused the run (400): Unknown executor "
            f"'lokal'. Available: '{DEFAULT_EXECUTOR}', '{LOCAL_EXECUTOR}'.",
            status=400,
        )
        with self.assertRaises(ToolError) as caught:
            self.start(executor=LOCAL_EXECUTOR)
        self.assertIn("Unknown executor", str(caught.exception))
        self.assertIn(LOCAL_EXECUTOR, str(caught.exception))

    def test_a_runner_that_is_not_there_says_nothing_was_launched(self):
        """"The runner is down" and "the run was refused" are different facts,
        and neither may read as "it started"."""
        self.runner_transport.fail = RunnerError(
            "Cannot reach the ticket runner at http://127.0.0.1:3460: "
            "Connection refused. Nothing was launched and the ticket was not "
            "moved. Check that the vikunja-claude launcher service is running.",
            status=None,
        )
        with self.assertRaises(ToolError) as caught:
            self.start()
        self.assertIn("Nothing was launched", str(caught.exception))

    def test_a_task_the_runner_does_not_work_is_explained_without_the_row_id(self):
        """The runner's own 404 spells the id it was addressed by — it is a
        lookup failure, and its text is written for a caller reading a
        `/tasks/<id>` URL. That text is not republished (task 663)."""
        self.runner_transport.fail = RunnerError(
            f"The ticket runner refused the run (404): No task {READY_ROW_ID} "
            "in this project. (Vikunja task URLs look like "
            f"/tasks/{READY_ROW_ID}; /projects/N/M is a board view, not a "
            "task.)",
            status=404,
        )
        with self.assertRaises(ToolError) as caught:
            self.start()
        message = str(caught.exception)
        self.assertNotIn(f"/tasks/{READY_ROW_ID}", message)
        self.assertNotIn(f"No task {READY_ROW_ID}", message)
        self.assertIn("board it works", message)
        self.assertIn("Nothing was started", message)

    def test_a_refused_launch_is_never_answered_as_a_launch(self):
        for status in (400, 404, 409, 500, 502, None):
            with self.subTest(status=status):
                transport = RecordingRunner(
                    fail=RunnerError("refused", status=status))
                service = McpService(
                    self.config,
                    self.client,
                    runner=RunnerClient("http://127.0.0.1:3460", transport),
                )
                preview = service.start_task_run(task_number=READY)
                with self.assertRaises(ToolError):
                    service.start_task_run(
                        task_number=READY,
                        approval_token=preview["approval_token"],
                    )


class TestTheAnswerCarriesNoRowIdAndNoHostLog(RunnerTestCase):
    def test_neither_the_preview_nor_the_launch_publishes_the_row_id(self):
        preview = self.service.start_task_run(task_number=READY)
        started = self.service.start_task_run(
            task_number=READY, approval_token=preview["approval_token"])
        for name, answer in (("preview", preview), ("started", started)):
            with self.subTest(answer=name):
                blob = json.dumps(answer)
                self.assertNotIn(f"/tasks/{READY_ROW_ID}", blob)
                self.assertNotIn(READY_ROW_ID, [
                    value for value in answer.values() if isinstance(value, int)
                ])

    def test_the_log_path_the_runner_returns_is_not_republished(self):
        """Its filename is `task-<row id>-<stamp>.log`, so passing the runner's
        answer through would have published the id in a path a reader copies.

        Asked as "no value carries that path", not "the digits do not appear":
        a bare digit is in the timestamp too, and an assertion that cannot fail
        for the right reason cannot pass for one either.
        """
        started = self.start()
        self.assertNotIn("log_file", started)
        blob = json.dumps(started)
        self.assertNotIn(f"task-{READY_ROW_ID}-", blob)
        self.assertNotIn(".log", blob)

    def test_the_pid_is_not_republished_either(self):
        """Nothing a caller of this boundary can do with it."""
        self.assertNotIn("pid", self.start())


class TestTheSeatIsNotOnThisSide(RunnerTestCase):
    #: A model id, an Ollama setting, a context size, the approved-model record
    #: or the seat's name. Any of these here would be a second place to edit
    #: when the seat moves — which is the thing task 690 put in one place.
    FORBIDDEN = (
        "ollama",
        "num_ctx",
        "models.json",
        "ANTHROPIC_",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
        "local_coding",
        "MANIFEST_PATH",
        "LOCAL_CODING_SEAT",
    )

    def test_the_model_reported_is_whatever_the_runner_answered(self):
        """Read back, never named here. A seat change in the investment
        repository's models.json arrives through this without an edit."""
        for model in ("qwen3.8-coder:27b", "some-later-seat-holder:32b", None):
            with self.subTest(model=model):
                transport = RecordingRunner(
                    answer={**LAUNCHED, "executor": LOCAL_EXECUTOR, "model": model})
                service = McpService(
                    self.config,
                    self.client,
                    runner=RunnerClient("http://127.0.0.1:3460", transport),
                )
                preview = service.start_task_run(
                    task_number=READY, executor=LOCAL_EXECUTOR)
                started = service.start_task_run(
                    task_number=READY,
                    executor=LOCAL_EXECUTOR,
                    approval_token=preview["approval_token"],
                )
                self.assertEqual(started["model"], model)
                self.assertEqual(started["executor"], LOCAL_EXECUTOR)

    def test_neither_module_names_a_model_a_runtime_or_a_context_size(self):
        """Asked of the CODE, not of the file.

        Both modules discuss the seat at length — that it is read by the runner
        from ``deploy/ollama/models.json`` and never here is the design, and
        writing it down is how the next reader knows not to add it. A scan of
        the raw text cannot tell that prose from an implementation, and answers
        "found" either way. So docstrings and comments are removed and the
        remaining code is scanned, where a model id, an Ollama setting or a
        context size would have to appear as a literal or a name to do
        anything.
        """
        root = Path(__file__).resolve().parent.parent / "vikunja_claude"
        for name in ("mcp_service.py", "runner.py"):
            code = _code_without_prose(root / name)
            for token in self.FORBIDDEN:
                with self.subTest(module=name, token=token):
                    self.assertNotIn(token, code)

    def test_the_advertised_executors_come_from_the_runners_own_constants(self):
        """Published so a model knows what to ask for, single-sourced so the
        published set cannot drift from the one that is enforced."""
        tool = {t.name: t for t in self.service.tools()}["start_task_run"]
        self.assertEqual(
            tool.input_schema["properties"]["executor"]["enum"],
            [DEFAULT_EXECUTOR, LOCAL_EXECUTOR],
        )


class TestTheLaunchIsRecorded(RunnerTestCase):
    def test_an_approved_launch_is_written_to_the_mutation_ledger(self):
        self.start(executor=LOCAL_EXECUTOR)
        entries = [
            line
            for line in self.config.mutation_ledger_path.read_text(
                encoding="utf-8").splitlines()
            if line
        ]
        self.assertEqual(len(entries), 1)
        record = json.loads(entries[0])
        self.assertEqual(record["kind"], CHANGE_RUN)
        self.assertEqual(record["task_number"], READY)
        self.assertEqual(record["project_id"], PROJECT_ID)
        self.assertEqual(record["from_bucket"], "Ready")

    def test_a_preview_records_nothing(self):
        self.service.start_task_run(task_number=READY)
        self.assertFalse(self.config.mutation_ledger_path.exists())


class TestTheClientCannotBeAimedElsewhere(unittest.TestCase):
    """The transport itself: one route, one verb, and no path argument.

    Same property as the operational reads (task 138): what a caller can vary
    is which task, never where the request goes.
    """

    def test_the_only_thing_a_caller_varies_is_the_task_and_the_executor(self):
        seen: list[str] = []
        client = RunnerClient("http://127.0.0.1:3460", lambda path, method: seen.append(path) or LAUNCHED)
        client.start_run(9, None)
        client.start_run(9, LOCAL_EXECUTOR)
        for path in seen:
            self.assertTrue(path.startswith("/task/9/work"))

    def test_an_executor_name_is_urlencoded_into_the_query(self):
        seen: list[str] = []
        client = RunnerClient("http://127.0.0.1:3460", lambda path, method: seen.append(path) or LAUNCHED)
        client.start_run(9, "a name/with?separators")
        self.assertEqual(seen, ["/task/9/work?executor=a%20name/with%3Fseparators"])

    def test_it_posts_and_carries_the_runners_reason_and_status(self):
        error = urllib.error.HTTPError(
            "http://127.0.0.1:3460/task/9/work",
            409,
            "Conflict",
            {},
            None,
        )
        error.read = lambda: json.dumps(
            {"error": "Claude is already working #8 (pid 1, started now)."}
        ).encode()
        with mock.patch("urllib.request.urlopen", side_effect=error):
            client = RunnerClient("http://127.0.0.1:3460")
            with self.assertRaises(RunnerError) as caught:
                client.start_run(9, None)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("already working", str(caught.exception))

    def test_a_request_that_never_arrives_carries_no_status(self):
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            client = RunnerClient("http://127.0.0.1:3460")
            with self.assertRaises(RunnerError) as caught:
                client.start_run(9, None)
        self.assertIsNone(caught.exception.status)
        self.assertIn("Nothing was launched", str(caught.exception))

    def test_the_client_has_no_generic_request_method(self):
        """A `post(path)` would have made the one-route discipline meaningless
        one layer down, the way `get(path)` would for the operational reads.

        Two since task 751, and the discipline is unchanged: each names one
        route and takes a task, and neither takes a path. What a caller varies
        is still which task — never where the request goes.
        """
        methods = {
            name
            for name in dir(RunnerClient)
            if not name.startswith("_") and callable(getattr(RunnerClient, name))
        }
        self.assertEqual(methods, {"start_run", "run_status"})


class TestReadingWhatARunIsDoing(RunnerTestCase):
    """The question `start_task_run` left unanswerable (task 751).

    A launch answers long before the work is done, and the run reports on the
    board only at the end. Between the two there was nothing to ask, so a run
    still thinking and a run whose process died an hour ago were the same
    thing from here: In Progress, no comment.
    """

    def read(self, task_number: int = READY, **kwargs) -> dict:
        return self.service.get_task_run_status(task_number=task_number, **kwargs)

    def test_one_call_answers_and_no_approval_is_asked_for(self):
        """Every two-step tool on this surface is two-step because it changes
        something. This changes nothing, on either side."""
        result = self.read()

        self.assertNotIn("approval_token", result)
        self.assertNotIn("approval_required", result)
        self.assertEqual(result["state"], "lost")

    def test_it_sends_one_get_to_the_runs_read_route(self):
        self.read()
        self.assertEqual(
            self.runner_transport.calls, [("GET", f"/task/{READY_ROW_ID}/run")]
        )

    def test_reading_a_run_never_starts_one(self):
        """The whole claim of this tool, witnessed at the seam: the runner is
        never sent the verb that launches anything."""
        self.read()
        self.read()
        self.assertEqual(self.runner_transport.methods, ["GET", "GET"])
        self.assertNotIn("/work", "".join(self.runner_transport.paths))

    def test_it_touches_nothing_on_the_board(self):
        """It looks the ticket up to name it, and that is all it does there."""
        self.read()
        writes = [call for call in self.vikunja.calls if call[0] != "GET"]
        self.assertEqual(writes, [])

    def test_the_state_is_the_runners_word_passed_through(self):
        """Not re-derived here from the fields that came back. A second answer
        to "what is this run doing" is one that can disagree with the only
        place that knows."""
        for state in RUN_STATES:
            with self.subTest(state=state):
                self.runner_transport.status_answer = {**STATUS, "state": state}
                self.assertEqual(self.read()["state"], state)

    def test_every_state_the_runner_can_report_is_explained(self):
        """The note is what a connector reads out, so a state with no note
        would be reported as a bare word nobody can act on.

        Taken from the runner's own vocabulary rather than listed again here,
        so a state added there without a note fails this rather than arriving
        at a connector as the unknown-state fallback.
        """
        for state in RUN_STATES:
            with self.subTest(state=state):
                self.runner_transport.status_answer = {**STATUS, "state": state}
                self.assertIn(state, McpService.RUN_STATE_NOTES)
                self.assertTrue(self.read()["note"].strip())

    def test_a_lost_run_is_explained_as_neither_finished_nor_failed(self):
        """The case task 714 was actually in. A note that said "the run failed"
        would assert a failure nothing observed, and one that said it finished
        would assert an exit nobody saw."""
        note = self.read()["note"]
        self.assertIn("never recorded how it ended", note)
        self.assertIn("In Progress", note)

    def test_a_reconciled_run_is_not_described_as_unaccounted_for(self):
        """The other half of the same read, and the defect task 757 names.

        Both states describe a run nobody watched end, so one note covered
        both — and it told a caller asking about a closed-out run that the
        ending was never recorded and the ticket was probably still In
        Progress. Reconciliation is what recorded that ending and what moved
        the ticket, so the note was false of it in both halves.
        """
        self.runner_transport.status_answer = {
            **STATUS,
            "state": "reconciled",
            "reconciled_at": "2026-09-01T15:02:11+0000",
        }
        result = self.read()

        note = result["note"]
        self.assertNotIn("never recorded how it ended", note)
        self.assertNotIn("In Progress", note)
        # And still claims no outcome for it, because none was observed.
        self.assertIn("neither finished nor failed", note)
        self.assertIsNone(result["exit_status"])

    def test_when_a_lost_run_was_closed_out_travels_with_the_state(self):
        """A reconciled run has no `finished_at` — nothing watched it end — so
        this is the only time anybody can put on its ending."""
        self.runner_transport.status_answer = {
            **STATUS,
            "state": "reconciled",
            "reconciled_at": "2026-09-01T15:02:11+0000",
        }
        result = self.read()

        self.assertEqual(result["reconciled_at"], "2026-09-01T15:02:11+0000")
        self.assertIsNone(result["finished_at"])
        # And it is absent, not invented, for a run nothing has closed out.
        self.runner_transport.status_answer = STATUS
        self.assertIsNone(self.read()["reconciled_at"])

    def test_an_unknown_state_is_described_as_unknown_not_guessed(self):
        self.runner_transport.status_answer = {**STATUS, "state": "quiesced"}
        result = self.read()
        self.assertEqual(result["state"], "quiesced")
        self.assertIn("does not have a description", result["note"])

    def test_the_run_is_named_by_the_board_and_carries_the_ticket_title(self):
        result = self.read()
        self.assertEqual(result["reference"], f"#{READY}")
        self.assertEqual(result["project_id"], PROJECT_ID)
        self.assertTrue(result["title"])

    def test_the_recent_output_travels_as_text(self):
        result = self.read()
        self.assertEqual(result["recent_output"], STATUS["output_tail"])
        self.assertFalse(result["recent_output_truncated"])
        self.assertEqual(result["recent_output_at"], STATUS["output_at"])


class TestTheStatusCarriesNoRowIdAndNoHostLog(RunnerTestCase):
    """The same two fields `start_task_run` withholds, withheld again.

    A read is where they would come back by accident: the runner sends them,
    and passing its answer through would publish both.
    """

    def read(self) -> dict:
        return self.service.get_task_run_status(task_number=READY)

    def test_the_pid_is_not_republished(self):
        """It names nothing a caller of this boundary can act on, and the only
        thing it *could* be acted on through is the execution control this
        tool is defined by not having."""
        answer = self.read()
        self.assertNotIn("pid", answer)
        self.assertNotIn(STATUS["pid"], list(answer.values()))

    def test_the_log_path_is_not_republished(self):
        """`task-<row id>-<stamp>.log` spells the id this surface does not
        publish (task 663) — which is also why the tail of that file comes
        back as text rather than as somewhere to go and read it."""
        answer = self.read()
        self.assertNotIn("log_file", answer)
        self.assertNotIn(f"task-{READY_ROW_ID}-", json.dumps(answer))
        self.assertNotIn(".local/state", json.dumps(answer))

    def test_the_row_id_is_in_the_request_and_in_nothing_that_comes_back(self):
        answer = self.read()
        self.assertIn(f"/task/{READY_ROW_ID}/run", self.runner_transport.paths[0])
        self.assertNotIn(f"/tasks/{READY_ROW_ID}", json.dumps(answer))
        self.assertNotIn(
            READY_ROW_ID,
            [value for value in answer.values() if isinstance(value, int)],
        )


class TestAFailedStatusReadSaysNothingWasStarted(RunnerTestCase):
    """A read that failed changed nothing by construction, and saying "nothing
    was launched" of it would describe a launch nobody asked for."""

    def test_a_runner_that_is_not_there_is_an_error_about_the_read(self):
        self.runner_transport.fail = RunnerError(
            "Cannot reach the ticket runner at http://127.0.0.1:3460: "
            "Connection refused. No run status was read; nothing was changed."
        )
        with self.assertRaises(ToolError) as caught:
            self.service.get_task_run_status(task_number=READY)
        self.assertIn("Cannot reach the ticket runner", str(caught.exception))
        self.assertNotIn("was not moved", str(caught.exception))

    def test_a_task_the_runner_does_not_work_is_explained_without_the_row_id(self):
        self.runner_transport.fail = RunnerError("spelling task 9", status=404)
        with self.assertRaises(ToolError) as caught:
            self.service.get_task_run_status(task_number=READY)
        message = str(caught.exception)
        self.assertIn("does not have that task", message)
        self.assertIn("No run status was read", message)
        self.assertNotIn("spelling task 9", message)


class TestTheStatusFixtureIsTheRunnersOwnShape(unittest.TestCase):
    def test_the_fixture_carries_exactly_what_run_status_returns(self):
        """Built from the builder. A fixture invented on this side can assert
        that a field is read correctly while the runner has stopped sending it.
        """
        import tempfile

        from vikunja_claude.launcher import Launcher

        from .support import make_config

        with tempfile.TemporaryDirectory() as tmp:
            launcher = Launcher(make_config(Path(tmp)), reap=False)
            produced = launcher.run_status(READY_ROW_ID)

        self.assertEqual(set(STATUS), set(produced))


class TestTheDefaultServiceBuildsItsOwnRunner(unittest.TestCase):
    def test_the_tool_is_always_advertised(self):
        """Unlike the optional integrations, there is no configuration that
        switches this off: the runner is the process this system exists to
        start, and one that is down is a refusal a caller reads rather than a
        capability that quietly was not there."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config = make_mcp_config(Path(tmp))
            service = McpService(
                config, VikunjaClient(config.api_url, config.token, transport=FakeVikunja())
            )
            self.assertIn(
                "start_task_run", {tool.name for tool in service.tools()})
            self.assertEqual(service.runner.runner_url, config.runner_url)


if __name__ == "__main__":
    unittest.main()
