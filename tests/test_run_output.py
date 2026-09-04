"""What a status read shows while a run is actually working (task 755).

Task 751 built the read: three artifacts a launch already writes, read
together, one of them the run's own output. What #714 then demonstrated is that
the third artifact was empty for the whole of a run. `claude -p` with the
default `text` output format composes its answer and prints it once, at the
end; until then the file held only the two warnings Claude Code puts on stderr
at startup. So a status read could prove a process existed and nothing else —
which is the one thing it was built not to be.

The failure is therefore in two halves and so are these tests.

**The write half.** The launch has to ask for output as the run happens, and
`--output-format stream-json` is the only thing that produces it. `--verbose`
travels with it because Claude Code refuses the pair without it, which makes
"stream-json implies --verbose" a property of the default worth pinning: a
default that lost `--verbose` would not degrade, it would refuse to start.

**The read half.** stream-json lines are JSON, and one of them is routinely
bigger than the whole of what a status read should hand back. Read through the
old single bound, a run in the middle of a large tool call produced a window
that landed mid-line, dropped the fragment, and returned *nothing* — a blank
status at the busiest moment. `STATUS_READ_BYTES` (how far back to look) and
the returned bounds are now separate numbers, and
`test_one_real_event_is_larger_than_everything_a_status_read_returns` is the
measurement that says why they have to be.
"""

from __future__ import annotations

import json
import os
import shlex
from unittest import mock

from vikunja_claude.config import (
    DEFAULT_CLAUDE_ARGS,
    DEFAULT_OPENCODE_ARGS,
    PACKAGE_ROOT,
)
from vikunja_claude.run_output import (
    STATUS_LINE_CHARS,
    STATUS_READ_BYTES,
    STATUS_TAIL_BYTES,
    STATUS_TAIL_LINES,
    render,
    tail,
)

from .support import TOKEN, ServiceTestCase

READY = 8
READY_ROW_ID = 9


def assistant(*blocks) -> str:
    return json.dumps({"type": "assistant", "message": {"content": list(blocks)}})


def tool_use(name: str, **arguments) -> dict:
    return {"type": "tool_use", "id": "t1", "name": name, "input": arguments}


def tool_result(content: str, is_error: bool = False) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": content,
                        "is_error": is_error,
                    }
                ]
            },
        }
    )


class TestTheLaunchAsksForOutputAsItHappens(ServiceTestCase):
    """The write half: a run that prints once at the end cannot be watched."""

    def test_the_default_asks_for_streaming_output(self):
        self.assertIn("--output-format", shlex.split(DEFAULT_CLAUDE_ARGS))
        self.assertIn("stream-json", shlex.split(DEFAULT_CLAUDE_ARGS))

    def test_streaming_output_always_travels_with_verbose(self):
        """Claude Code refuses `--output-format stream-json` without
        `--verbose`, so a default carrying one and not the other does not
        produce less output — it produces a run that will not start."""
        args = shlex.split(DEFAULT_CLAUDE_ARGS)
        if "stream-json" in args:
            self.assertIn("--verbose", args)

    def test_the_shipped_env_file_does_not_quietly_undo_the_default(self):
        """`DEFAULT_CLAUDE_ARGS` is only the default, and the service unit
        loads `.env` through `EnvironmentFile`. A `CLAUDE_ARGS` there overrides
        it entirely, so an `.env.example` that people copy from is a second
        place the launch arguments are decided — and it was left on the old
        value when the default moved, which made the whole of task 755 inert on
        the host until somebody noticed. The host's own `.env` is untracked and
        cannot be asserted here; the example it is copied from can.
        """
        self.assert_example_matches("CLAUDE_ARGS", DEFAULT_CLAUDE_ARGS)

    def test_the_shipped_env_file_does_not_quietly_undo_the_local_default(self):
        """The same trap, for the harness task 810 added.

        It bites harder here: `OPENCODE_ARGS` is where a local run's ability to
        run commands at all is decided, so an example that drifted from the
        default would hand somebody a copied `.env` that produces runs which
        read the ticket and change nothing.
        """
        self.assert_example_matches("OPENCODE_ARGS", DEFAULT_OPENCODE_ARGS)

    def assert_example_matches(self, name: str, default: str) -> None:
        example = PACKAGE_ROOT / ".env.example"
        declared = [
            line.split("=", 1)[1].strip()
            for line in example.read_text(encoding="utf-8").splitlines()
            if line.startswith(f"{name}=")
        ]

        self.assertEqual(len(declared), 1, f"one {name} line, or none to compare")
        self.assertEqual(shlex.split(declared[0]), shlex.split(default))

    def test_a_launch_hands_those_arguments_to_the_process(self):
        """The default is only worth anything if it reaches the child. It is
        read through the config, so a launch is what proves the whole path."""
        self.service.work(self.service.get_by_task_number(READY))

        argv = self.spawn.calls[0]["argv"]
        self.assertEqual(argv[1 : 1 + len(self.config.claude_args)],
                         self.config.claude_args)
        self.assertIn("--output-format", argv)
        self.assertIn("stream-json", argv)


class RunOutputTestCase(ServiceTestCase):
    """A launched run whose output file the test writes, as the run would."""

    def setUp(self):
        super().setUp()
        self.service.work(self.service.get_by_task_number(READY))
        self.log = self.launched_log_file()

    def launched_log_file(self):
        records = [
            json.loads(line)
            for line in self.config.log_path.read_text(encoding="utf-8").splitlines()
        ]
        for record in reversed(records):
            if record.get("event") == "launched":
                return self.config.run_log_dir / os.path.basename(record["log_file"])
        raise AssertionError("nothing was launched")

    def emit(self, *lines: str) -> None:
        """Append events the way a working run does: one line at a time."""
        with self.log.open("a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")

    def status(self) -> dict:
        return self.launcher.run_status(READY_ROW_ID)


class TestTheFailureModeOf714(RunOutputTestCase):
    def test_one_real_event_is_larger_than_everything_a_status_read_returns(self):
        """The measurement the two separate bounds exist for.

        Not an assumption about Claude Code: a tool call carrying a file's
        contents, or a tool result carrying a test run, is this size routinely.
        If one event can exceed the whole returned payload, then a single
        window sized to that payload cannot contain one event, and a reader
        using it sees a fragment — or, once the fragment is dropped, nothing.
        """
        event = tool_result("x" * (STATUS_TAIL_BYTES * 2))
        self.assertGreater(len(event), STATUS_TAIL_BYTES)
        self.assertLess(len(event), STATUS_READ_BYTES)

    def test_the_old_single_bound_showed_nothing_while_the_run_worked(self):
        """#714 as an artifact: output exists, the reader reports none of it.

        The lookback is pinned back to what it was when it also had to be the
        returned bound. The run has written a whole event; the read returns an
        empty tail, which from outside is indistinguishable from a run that has
        gone quiet.
        """
        self.emit(tool_result("x" * (STATUS_TAIL_BYTES * 2)))

        with mock.patch(
            "vikunja_claude.run_output.STATUS_READ_BYTES", STATUS_TAIL_BYTES
        ):
            stale = self.status()

        self.assertEqual(stale["output_tail"], [])

    def test_the_corrected_read_surfaces_that_same_output(self):
        self.emit(tool_result("x" * (STATUS_TAIL_BYTES * 2)))

        status = self.status()

        self.assertEqual(len(status["output_tail"]), 1)
        self.assertTrue(status["output_tail"][0].startswith("tool result: xxx"))
        self.assertTrue(status["output_truncated"])

    def test_repeated_reads_show_the_run_advancing(self):
        """The whole point: three reads of a working run, three answers.

        Each read is taken before the next event is written, because a test
        that wrote everything first could pass while the reader only ever
        showed the end of a finished file.
        """
        self.emit(assistant({"type": "text", "text": "Reading the ticket"}))
        first = self.status()

        self.emit(assistant(tool_use("Bash", command="python -m pytest tests/")))
        second = self.status()

        self.emit(tool_result("848 passed"))
        third = self.status()

        self.assertEqual(first["output_tail"][-1], "assistant: Reading the ticket")
        self.assertEqual(
            second["output_tail"][-1],
            'tool: Bash {"command": "python -m pytest tests/"}',
        )
        self.assertEqual(third["output_tail"][-1], "tool result: 848 passed")
        # Advancing, not merely different: nothing already shown was withdrawn.
        self.assertEqual(second["output_tail"][:1], first["output_tail"][:1])

    def test_the_timestamp_advances_with_the_output(self):
        """`recent_output_at` is the file's mtime, and the file is appended to
        as the run works. Set explicitly here because the field has a
        one-second resolution and a test writes faster than that."""
        self.emit(assistant({"type": "text", "text": "first"}))
        os.utime(self.log, (1_000_000, 1_000_000))
        before = self.status()["output_at"]

        self.emit(assistant({"type": "text", "text": "second"}))
        os.utime(self.log, (1_000_060, 1_000_060))
        after = self.status()["output_at"]

        self.assertNotEqual(before, after)
        self.assertGreater(after, before)

    def test_the_startup_warnings_are_still_shown(self):
        """What #714 did see. They arrive on stderr as plain text, they are the
        only thing there before the first event, and they are worth reading —
        the second one is how an unrecognised model gets noticed."""
        self.emit(
            "⚠ claude.ai connectors are disabled because ANTHROPIC_API_KEY is set",
            '[claude-code:unrecognized_model] {"model":"qwen38-27b-abl:256k"}',
        )

        tail_lines = self.status()["output_tail"]

        self.assertEqual(len(tail_lines), 2)
        self.assertTrue(tail_lines[0].startswith("⚠ claude.ai connectors"))
        self.assertIn("unrecognized_model", tail_lines[1])


class TestWhatAnEventIsRenderedAs(ServiceTestCase):
    """One event, one labelled line. Read as a vocabulary a person learns."""

    def test_the_model_speaking(self):
        self.assertEqual(
            render(assistant({"type": "text", "text": "Looking at the diff"})),
            ["assistant: Looking at the diff"],
        )

    def test_the_model_thinking(self):
        self.assertEqual(
            render(assistant({"type": "thinking", "thinking": "which test bites"})),
            ["thinking: which test bites"],
        )

    def test_a_tool_call_carries_what_it_was_called_with(self):
        self.assertEqual(
            render(assistant(tool_use("Edit", file_path="api/main.py"))),
            ['tool: Edit {"file_path": "api/main.py"}'],
        )

    def test_a_tool_result_and_a_tool_error_are_told_apart(self):
        self.assertEqual(render(tool_result("2 files changed")),
                         ["tool result: 2 files changed"])
        self.assertEqual(render(tool_result("command not found", is_error=True)),
                         ["tool error: command not found"])

    def test_a_turn_that_speaks_and_calls_a_tool_gives_both_lines(self):
        """Collapsing them would hide either what the run said it was doing or
        what it then did, and the pair is what makes a transcript legible."""
        self.assertEqual(
            render(
                assistant(
                    {"type": "text", "text": "Running the suite"},
                    tool_use("Bash", command="pytest"),
                )
            ),
            ["assistant: Running the suite", 'tool: Bash {"command": "pytest"}'],
        )

    def test_the_run_starting_and_the_run_ending(self):
        self.assertEqual(
            render(json.dumps(
                {"type": "system", "subtype": "init", "model": "claude-opus-5"}
            )),
            ["system: init claude-opus-5"],
        )
        self.assertEqual(
            render(json.dumps({
                "type": "result",
                "subtype": "success",
                "num_turns": 12,
                "result": "Ticket done and committed.",
            })),
            ["result: success after 12 turns: Ticket done and committed."],
        )

    def test_a_paragraph_becomes_one_line(self):
        """`output_tail` is a list of lines and an event is one of them. A
        model's paragraph split across entries would read as a burst of
        activity that never happened."""
        self.assertEqual(
            render(assistant({"type": "text", "text": "one\n\ntwo\nthree"})),
            ["assistant: one two three"],
        )

    def test_an_event_type_it_does_not_know_is_named_not_dropped(self):
        """A reader watching for output to advance must not be shown a stall
        that is really a Claude Code event this predates."""
        self.assertEqual(render(json.dumps({"type": "future_thing"})), ["future_thing:"])

    def test_a_line_that_is_not_json_is_shown_as_written(self):
        """Claude Code's stderr, and any run whose CLAUDE_ARGS still asks for
        the text format. Rendering enhances the read; it is not a requirement
        on what the writer produces."""
        self.assertEqual(render("Traceback (most recent call last):"),
                         ["Traceback (most recent call last):"])
        self.assertEqual(render("[1, 2, 3]"), ["[1, 2, 3]"])

    def test_an_event_carrying_nothing_says_nothing(self):
        self.assertEqual(render(assistant({"type": "text", "text": ""})), [])
        self.assertEqual(render(""), [])


class TestTheOtherHarnessRendersThroughTheSameSurface(ServiceTestCase):
    """OpenCode's vocabulary, rendered into the labels a reader already knows.

    Since task 810 the `local` executor is OpenCode, and `opencode run --format
    json` writes the same kind of artifact in the same way: one JSON object per
    line, as each step of the run completes. Only the vocabulary differs, and it
    does not collide with Claude Code's — nothing here is a top-level `type`
    Claude Code emits, and nothing Claude Code emits is one of these — so one
    renderer serves both and a status tail reads the same whichever ran.
    """

    @staticmethod
    def event(kind: str, **payload) -> str:
        """The envelope OpenCode writes: a type, a stamp, a session, a body."""
        return json.dumps(
            {"type": kind, "timestamp": 1, "sessionID": "ses_1", **payload}
        )

    def test_assistant_text_carries_the_same_label_as_claude_codes(self):
        self.assertEqual(
            render(self.event("text", part={"type": "text", "text": "Reading the diff"})),
            ["assistant: Reading the diff"],
        )

    def test_reasoning_is_shown_as_thinking(self):
        self.assertEqual(
            render(self.event("reasoning", part={"type": "reasoning", "text": "which test bites"})),
            ["thinking: which test bites"],
        )

    def test_one_completed_tool_renders_as_the_call_and_its_result(self):
        """Two lines from one event, on purpose.

        Claude Code reports a tool call on the assistant turn and its result on
        the next user turn; OpenCode reports both together when the tool
        finishes. Rendering the one event as two lines keeps a single vocabulary
        on the read side.
        """
        self.assertEqual(
            render(
                self.event(
                    "tool_use",
                    part={
                        "type": "tool",
                        "tool": "bash",
                        "state": {
                            "status": "completed",
                            "input": {"command": "pytest -q"},
                            "output": "38 passed",
                        },
                    },
                )
            ),
            ['tool: bash {"command": "pytest -q"}', "tool result: 38 passed"],
        )

    def test_a_failed_tool_is_labelled_as_an_error(self):
        lines = render(
            self.event(
                "tool_use",
                part={
                    "type": "tool",
                    "tool": "bash",
                    "state": {
                        "status": "error",
                        "input": {"command": "pytest -q"},
                        "error": "command not found",
                    },
                },
            )
        )
        self.assertEqual(lines[1], "tool error: command not found")

    def test_a_structured_tool_error_is_serialised_rather_than_dropped(self):
        """The point of the line is to say what happened.

        A tool error need not be a string, and a renderer that showed only
        strings would turn the most interesting event in a run into a blank.
        """
        lines = render(
            self.event(
                "tool_use",
                part={
                    "type": "tool",
                    "tool": "edit",
                    "state": {"status": "error", "error": {"code": 2}},
                },
            )
        )
        self.assertIn("tool error:", lines[1])
        self.assertIn("code", lines[1])

    def test_a_run_error_prefers_the_message_over_the_class_name(self):
        """What OpenCode shows a person, shown here too."""
        self.assertEqual(
            render(
                self.event(
                    "error",
                    error={"name": "ProviderError", "data": {"message": "model not found"}},
                )
            ),
            ["error: model not found"],
        )

    def test_a_run_error_with_no_message_falls_back_to_its_name(self):
        self.assertEqual(
            render(self.event("error", error={"name": "ProviderError"})),
            ["error: ProviderError"],
        )

    def test_the_step_markers_are_recognised_and_deliberately_silent(self):
        """Silence chosen for a known event, not a gap.

        `step_start` and `step_finish` bracket every model turn and say nothing
        about what the run is doing. A line each would be two contentless
        entries per turn, pushing the text, tool calls and errors a status read
        exists for out of the far end of a 40-line window.
        """
        self.assertEqual(render(self.event("step_start", part={"type": "step-start"})), [])
        self.assertEqual(render(self.event("step_finish", part={"type": "step-finish"})), [])

    def test_an_unknown_opencode_event_is_still_named(self):
        """The guarantee that survives a vocabulary change in either harness.

        A reader watching output advance must not be shown a stall that is only
        an event type this module has not met.
        """
        self.assertEqual(render(self.event("something_new")), ["something_new:"])

    def test_an_empty_text_part_says_nothing_rather_than_an_empty_label(self):
        self.assertEqual(render(self.event("text", part={"type": "text", "text": "  "})), [])


class TestWhatComesBackStaysBounded(RunOutputTestCase):
    def test_a_long_event_is_shortened_and_says_so(self):
        self.emit(assistant({"type": "text", "text": "y" * (STATUS_LINE_CHARS * 5)}))

        status = self.status()

        self.assertEqual(len(status["output_tail"][0]), STATUS_LINE_CHARS)
        self.assertTrue(status["output_tail"][0].endswith("…"))
        self.assertTrue(status["output_truncated"])

    def test_many_events_are_bounded_by_lines_and_by_characters(self):
        self.emit(*[
            assistant({"type": "text", "text": f"step {n}"})
            for n in range(STATUS_TAIL_LINES * 3)
        ])

        status = self.status()

        self.assertEqual(len(status["output_tail"]), STATUS_TAIL_LINES)
        self.assertLessEqual(
            sum(len(line) for line in status["output_tail"]), STATUS_TAIL_BYTES
        )
        # The END of the output: what the run is doing now.
        self.assertEqual(
            status["output_tail"][-1],
            f"assistant: step {STATUS_TAIL_LINES * 3 - 1}",
        )

    def test_the_two_returned_bounds_agree_with_each_other(self):
        """A character budget below what the line bounds can produce would trim
        full lines off every busy read; above it, it would never bite. They are
        the same number by construction and this is what says so."""
        self.assertEqual(STATUS_TAIL_BYTES, STATUS_TAIL_LINES * STATUS_LINE_CHARS)


class TestTheTokenNeverTravelsOut(RunOutputTestCase):
    def test_it_is_taken_out_of_a_rendered_tool_call(self):
        """The run holds it in its environment, so it reaches this file inside
        a tool input — where, before rendering, it is a fragment of JSON."""
        self.emit(assistant(tool_use("Bash", command=f"curl -H 'Bearer {TOKEN}'")))

        line = self.status()["output_tail"][0]

        self.assertNotIn(TOKEN, line)
        self.assertIn("[redacted]", line)

    def test_it_is_taken_out_before_the_line_is_shortened(self):
        """Shortening first would cut the token in half, and the half left
        behind no longer matches the value being searched for.

        The token is placed so the cut falls INSIDE it — half before, half
        after — because that is the only arrangement the ordering can be seen
        in. Placed anywhere else, both orderings agree.
        """
        half = len(TOKEN) // 2
        # `assistant: ` is the label the render adds; the padding puts the cut
        # exactly `half` characters into the token.
        padding = STATUS_LINE_CHARS - len("assistant: ") - half
        self.emit(assistant({"type": "text", "text": "x" * padding + TOKEN}))

        line = self.status()["output_tail"][0]

        self.assertNotIn(TOKEN[:half], line)
        self.assertIn("[redacted]", line)


class TestReadingChangesNothing(RunOutputTestCase):
    def test_the_run_log_is_not_rewritten_by_a_read(self):
        self.emit(assistant({"type": "text", "text": "working"}))
        before = self.log.read_bytes()

        self.status()

        self.assertEqual(self.log.read_bytes(), before)

    def test_a_log_that_is_not_there_is_not_an_error(self):
        self.assertEqual(
            tail(self.config.run_log_dir / "gone.log", lambda line: line),
            {"output_tail": [], "output_truncated": False, "output_at": None},
        )
