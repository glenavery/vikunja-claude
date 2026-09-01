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
    """Stands in for the launcher, recording the paths it was asked for."""

    def __init__(self, answer=None, fail: Exception | None = None):
        self.answer = LAUNCHED if answer is None else answer
        self.fail = fail
        self.paths: list[str] = []

    def __call__(self, path: str):
        self.paths.append(path)
        if self.fail is not None:
            raise self.fail
        return self.answer


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
        client = RunnerClient("http://127.0.0.1:3460", lambda path: seen.append(path) or LAUNCHED)
        client.start_run(9, None)
        client.start_run(9, LOCAL_EXECUTOR)
        for path in seen:
            self.assertTrue(path.startswith("/task/9/work"))

    def test_an_executor_name_is_urlencoded_into_the_query(self):
        seen: list[str] = []
        client = RunnerClient("http://127.0.0.1:3460", lambda path: seen.append(path) or LAUNCHED)
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
        one layer down, the way `get(path)` would for the operational reads."""
        methods = {
            name
            for name in dir(RunnerClient)
            if not name.startswith("_") and callable(getattr(RunnerClient, name))
        }
        self.assertEqual(methods, {"start_run"})


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
