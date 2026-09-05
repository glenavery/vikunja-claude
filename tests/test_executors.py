"""Tasks 690 and 810 — which harness a run uses, and which model behind it.

The runner gained one thing in task 690: a choice of model. It did not gain a
second launcher, a second lock, a second log, or any notion of "a local run" as
a different kind of run. Task 810 changed what an executor may choose — the
local one is now OpenCode rather than Claude Code — and the list of things that
must NOT have changed is the same list, which is why most of what is asserted
here is still about what stayed the same.

Three properties are worth more than the rest:

- **the approval is read, never copied.** The model comes from the investment
  repository's `deploy/ollama/models.json`, so moving the seat there moves the
  runner with no edit in this package. A test moves it and expects the runner to
  follow.
- **a broken local configuration refuses.** The alternative to a local run is a
  run against a paid frontier model, so a fallback would answer "run this
  locally" with a bill.
- **a local run can actually carry out commands.** This is what task 810 is for,
  and it is asserted rather than assumed because the failure it replaces is a
  quiet one: see `UnattendedExecution`.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vikunja_claude.launcher import AlreadyRunning
from vikunja_claude.executors import (
    DEFAULT_EXECUTOR,
    LOCAL_EXECUTOR,
    DEFAULT_OUTPUT_TOKENS,
    GRAPHIFY_SERVER_NAME,
    GRAPHIFY_SERVE_MODULE,
    PAID_PROVIDER_KEYS,
    Executor,
    ExecutorError,
    local_executor,
    resolve,
)

from .support import ServiceTestCase, make_config, make_repo
from .test_browser_flow import HttpFlow

#: Ollama's own OpenAI-compatible endpoint. OpenCode speaks that shape, so the
#: local path reaches the seat directly rather than through the Anthropic
#: transport shim Claude Code needed (task 810).
BASE_URL = "http://127.0.0.1:11434/v1"

#: The shape of the real manifest, cut down to what the runner reads. Built as a
#: dict rather than copied as text so a test can move the seat by editing it.
MANIFEST = {
    "base": "qwen3.6:35b",
    "models": [
        {
            "name": "qwen38-27b-abl-256k:latest",
            "modelfile": "qwen38-27b-abl-256k.Modelfile",
            "aliases": ["qwen38-27b-abl:256k"],
            "role": "production",
            "seat": "local_coding",
        },
        {
            "name": "some-other-model:latest",
            "modelfile": "some-other-model.Modelfile",
            "aliases": ["some-other:256k"],
            "role": "benchmark",
        },
    ],
}

RECIPE = "FROM base\nPARAMETER temperature 1\nPARAMETER num_ctx 262144\n"


def write_graphify(root: Path, graph=True, interpreter=sys.executable) -> Path:
    """The graphify artifacts a checkout carries: a graph and its interpreter.

    Both are content the runner only reads, so the graph is a stub — what is
    under test is which path is handed to OpenCode, never what the graph says.
    The interpreter defaults to the one running the tests because the runner
    checks that the recorded path exists, and this one demonstrably does.

    `graph=False` and `interpreter=None` are how a test asks for a checkout
    missing one of them, which is a refusal rather than a quieter run.
    """
    out = root / "graphify-out"
    out.mkdir(parents=True, exist_ok=True)
    if graph:
        (out / "graph.json").write_text(
            json.dumps({"nodes": [], "links": []}), encoding="utf-8"
        )
    if interpreter is not None:
        (out / ".graphify_python").write_text(f"{interpreter}\n", encoding="utf-8")
    return root


def write_repo(root: Path, manifest=None, recipes=None) -> Path:
    """A workdir shaped like the investment repository's ollama deployment.

    A real git repository as well as the right files, because a launch now
    creates a worktree in the workdir and refuses if it cannot (task 756), and
    a graphify graph beside them, because a local launch now reads that too
    (task 821). Both are properties of the checkout the runner works in.
    """
    make_repo(root)
    ollama = root / "deploy" / "ollama"
    ollama.mkdir(parents=True, exist_ok=True)
    (ollama / "models.json").write_text(
        json.dumps(manifest if manifest is not None else MANIFEST), encoding="utf-8"
    )
    for name, text in (recipes or {"qwen38-27b-abl-256k.Modelfile": RECIPE}).items():
        (ollama / name).write_text(text, encoding="utf-8")
    write_graphify(root)
    return root


def config_for(workdir: Path, **overrides):
    """A Config pointed at one throwaway workdir.

    An executor now answers "how is a run launched", so it is resolved from the
    config rather than from a hand-picked pair of settings (task 810). Built
    through `make_config` so the arguments a test reasons about are the
    production ones.
    """
    overrides.setdefault("local_executor_base_url", BASE_URL)
    return make_config(workdir / "_state", workdir=workdir, **overrides)


def opencode_settings(executor: Executor) -> dict:
    """What the child is actually told, decoded from its environment."""
    return json.loads(executor.env["OPENCODE_CONFIG_CONTENT"])


class Repo(unittest.TestCase):
    """A throwaway workdir carrying an approved-model record."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workdir = write_repo(Path(self._tmp.name))

    def rewrite(self, manifest=None, recipes=None) -> None:
        write_repo(self.workdir, manifest, recipes)

    def local(self, **overrides) -> Executor:
        return local_executor(config_for(self.workdir, **overrides))


# --------------------------------------------------------------------------- #
# The model comes from the approved record                                      #
# --------------------------------------------------------------------------- #

class ResolvingTheSeat(Repo):
    def test_the_model_is_the_one_holding_the_local_coding_seat(self):
        executor = self.local()
        self.assertEqual(executor.model, "qwen38-27b-abl:256k")
        self.assertEqual(
            opencode_settings(executor)["model"], "ollama/qwen38-27b-abl:256k"
        )

    def test_moving_the_seat_moves_the_runner_with_no_code_change(self):
        """The acceptance criterion, exercised rather than argued.

        Nothing in this package names a model, so approving a different one is
        an edit in the record and nowhere else.
        """
        manifest = json.loads(json.dumps(MANIFEST))
        del manifest["models"][0]["seat"]
        manifest["models"][1]["seat"] = "local_coding"
        manifest["models"][1]["role"] = "production"
        self.rewrite(
            manifest,
            {
                "qwen38-27b-abl-256k.Modelfile": RECIPE,
                "some-other-model.Modelfile": "FROM x\nPARAMETER num_ctx 131072\n",
            },
        )
        executor = self.local()
        self.assertEqual(executor.model, "some-other:256k")
        settings = opencode_settings(executor)
        self.assertEqual(settings["model"], "ollama/some-other:256k")
        self.assertEqual(
            settings["provider"]["ollama"]["models"]["some-other:256k"]["limit"],
            {"context": 131072, "output": DEFAULT_OUTPUT_TOKENS},
        )

    def test_an_alias_is_preferred_over_a_floating_latest_tag(self):
        executor = self.local()
        self.assertNotIn(":latest", executor.model or "")

    def test_a_seat_holder_with_no_alias_is_named_by_its_built_name(self):
        manifest = json.loads(json.dumps(MANIFEST))
        manifest["models"][0]["aliases"] = []
        self.rewrite(manifest)
        self.assertEqual(self.local().model, "qwen38-27b-abl-256k:latest")


# --------------------------------------------------------------------------- #
# The approved context, not a guess and not a default                           #
# --------------------------------------------------------------------------- #

class ApprovedContext(Repo):
    def test_the_window_is_read_from_the_seats_own_recipe(self):
        """The harness sends no num_ctx, so the Modelfile default is what runs.

        It has to be told, too: a harness that does not know a model's window
        assumes one and compacts to it, which on this seat would throw away most
        of the context the ticket asks to have available.
        """
        settings = opencode_settings(self.local())
        model = settings["provider"]["ollama"]["models"]["qwen38-27b-abl:256k"]
        self.assertEqual(model["limit"]["context"], 262144)

    def test_the_output_cap_is_stated_because_the_schema_demands_one(self):
        """The two halves of `limit` come from different places, on purpose.

        The context is a fact about the approved model, read from its recipe.
        The output cap is not: no recipe under `deploy/ollama/` sets
        `num_predict`, so the seat imposes no output limit of its own. OpenCode
        refuses a config without the key all the same ("Missing key ...
        limit.output"), so a number has to be stated, and it is the harness's
        bookkeeping rather than a claim the record makes.

        Both are asserted because a config missing either is rejected at
        startup — which is a launch that fails, not a run that degrades.
        """
        settings = opencode_settings(self.local())
        model = settings["provider"]["ollama"]["models"]["qwen38-27b-abl:256k"]
        self.assertEqual(
            model["limit"], {"context": 262144, "output": DEFAULT_OUTPUT_TOKENS}
        )

    def test_a_recipe_that_states_no_context_is_a_refusal_not_a_default(self):
        self.rewrite(recipes={"qwen38-27b-abl-256k.Modelfile": "FROM base\n"})
        with self.assertRaises(ExecutorError) as caught:
            self.local()
        self.assertIn("num_ctx", str(caught.exception))


# --------------------------------------------------------------------------- #
# A broken local configuration refuses; it never falls back                     #
# --------------------------------------------------------------------------- #

class Refusals(Repo):
    def test_a_missing_record_names_the_path_it_looked_at(self):
        with TemporaryDirectory() as empty:
            with self.assertRaises(ExecutorError) as caught:
                local_executor(config_for(Path(empty)))
        self.assertIn("models.json", str(caught.exception))

    def test_a_record_with_no_seat_holder_refuses(self):
        manifest = json.loads(json.dumps(MANIFEST))
        del manifest["models"][0]["seat"]
        self.rewrite(manifest)
        with self.assertRaises(ExecutorError):
            self.local()

    def test_two_seat_holders_refuse_rather_than_one_being_picked(self):
        manifest = json.loads(json.dumps(MANIFEST))
        manifest["models"][1]["seat"] = "local_coding"
        self.rewrite(manifest)
        with self.assertRaises(ExecutorError) as caught:
            self.local()
        self.assertIn("seat names one model", str(caught.exception))

    def test_an_unreadable_record_refuses(self):
        (self.workdir / "deploy" / "ollama" / "models.json").write_text(
            "{not json", encoding="utf-8"
        )
        with self.assertRaises(ExecutorError):
            self.local()

    def test_an_unknown_executor_name_names_the_ones_that_exist(self):
        with self.assertRaises(ExecutorError) as caught:
            resolve("qwen-code", config_for(self.workdir))
        self.assertIn(DEFAULT_EXECUTOR, str(caught.exception))
        self.assertIn(LOCAL_EXECUTOR, str(caught.exception))


# --------------------------------------------------------------------------- #
# What the child process is given                                               #
# --------------------------------------------------------------------------- #

class ChildEnvironment(Repo):
    def test_the_seat_is_pinned_on_the_command_line_as_well_as_in_the_config(self):
        """Two statements of one model, and the argv one is load-bearing.

        OpenCode can address providers this run must never reach — Anthropic,
        OpenAI, Ollama's own hosted tier — and a config key it merges over is a
        weaker claim than a flag. The flag is appended after the configurable
        arguments so an `OPENCODE_ARGS` cannot take it off.
        """
        executor = self.local()
        self.assertEqual(
            executor.args[-2:], ("--model", "ollama/qwen38-27b-abl:256k")
        )
        self.assertEqual(
            opencode_settings(executor)["model"], "ollama/qwen38-27b-abl:256k"
        )

    def test_the_provider_points_at_ollama_and_a_trailing_slash_does_not_survive(self):
        """Ollama itself, not the Anthropic shim.

        The shim exists because Claude Code speaks the Anthropic message shape
        and appends a trailing system message Ollama refuses. OpenCode speaks
        the OpenAI shape, which Ollama serves natively, so this path does not go
        through the shim at all (task 810).
        """
        executor = self.local(local_executor_base_url="http://127.0.0.1:11434/v1/")
        provider = opencode_settings(executor)["provider"]["ollama"]
        self.assertEqual(provider["options"]["baseURL"], "http://127.0.0.1:11434/v1")

    def test_a_real_api_key_is_removed_from_a_local_runs_environment(self):
        """The one line between "the run fails" and "the run bills the frontier".

        The service may perfectly well have been started with a key in its
        environment, and the child inherits everything by default. Asserted over
        the whole set rather than one key, because OpenCode discovers providers
        from the environment and one survivor is one reachable paid provider.
        """
        environ = {key: "sk-real" for key in PAID_PROVIDER_KEYS}
        environ["PATH"] = "/usr/bin"
        child = self.local().apply(environ)
        for key in PAID_PROVIDER_KEYS:
            self.assertNotIn(key, child)
        self.assertEqual(child["PATH"], "/usr/bin")

    def test_the_config_reaches_the_child_without_a_file_being_written(self):
        """The record stays the only copy of the seat; nothing is left on disk.

        A written config would be a second copy, and a second copy is a thing
        that goes stale — the same reasoning that keeps the model string out of
        this package.
        """
        executor = self.local()
        self.assertIn("OPENCODE_CONFIG_CONTENT", executor.env)
        self.assertEqual(list(self.workdir.glob("**/opencode.json")), [])

    def test_the_default_executor_adds_and_removes_nothing(self):
        """`claude` is the harness as installed, and must stay untouched."""
        executor = resolve(DEFAULT_EXECUTOR, config_for(self.workdir))
        environ = {"ANTHROPIC_API_KEY": "sk-real", "PATH": "/usr/bin"}
        self.assertEqual(executor.apply(environ), environ)
        self.assertIsNone(executor.model)


# --------------------------------------------------------------------------- #
# A local run can actually carry out the commands the ticket needs              #
# --------------------------------------------------------------------------- #

class UnattendedExecution(Repo):
    """Task 810's reason to exist, asserted rather than assumed.

    **The failure being prevented is not a stall, and that is the whole point.**
    It is tempting to write this as "the run does not hang waiting for
    approval", and such a test would pass forever without testing anything,
    because neither harness ever blocks on a person in headless mode. What they
    do instead differs, and both spellings of the old failure are quiet:

    - Claude Code headless under `--permission-mode acceptEdits` accepts file
      edits and **denies bash**. A run reads the ticket, edits code, and is then
      refused the test, the `git commit` and the `vkctl.py` report-back. That is
      task #807.
    - `opencode run` answers a permission request itself: it allows it with
      `--auto` and **refuses it without**, then carries on regardless. So a
      missing flag produces a run that looks busy, ends by itself, and has
      changed nothing.

    Neither shows up as a hang, a non-zero exit or an empty log. What separates
    a working configuration from both is the launched command line and the
    permissions in the config the child is handed, so those are what is pinned
    here.
    """

    def test_the_local_run_is_launched_with_command_execution_enabled(self):
        argv = self.local().argv("the prompt")
        self.assertIn("--auto", argv)

    def test_the_config_hands_the_run_the_capabilities_a_ticket_needs(self):
        """Editing files, running commands, and reading what a ticket cites.

        Stated in the config as well as on the command line: this settles the
        named capabilities so no request is raised at all, and `--auto` answers
        anything these three do not cover.
        """
        permission = opencode_settings(self.local())["permission"]
        self.assertEqual(permission["edit"], "allow")
        self.assertEqual(permission["bash"], "allow")
        self.assertEqual(permission["webfetch"], "allow")

    def test_it_is_the_headless_subcommand_writing_events_as_it_goes(self):
        """`run --format json`, the counterpart of Claude Code's stream-json.

        Without it a status read can prove a process started and nothing more,
        which is what #714 looked like from outside (task 755). `run` must also
        come first: it is the subcommand, not a flag.
        """
        argv = self.local().argv("the prompt")
        self.assertEqual(argv[1], "run")
        self.assertIn("--format", argv)
        self.assertEqual(argv[argv.index("--format") + 1], "json")

    def test_the_prompt_is_the_last_argument_and_is_passed_whole(self):
        """One argv element, not split, and after every flag.

        A prompt that arrived split across positionals would reach the model as
        a different ticket, and one placed before a flag would be parsed as its
        value.
        """
        prompt = "You are working a single ticket.\n\nWith a blank line in it."
        argv = self.local().argv(prompt)
        self.assertEqual(argv[-1], prompt)
        self.assertEqual(argv.count(prompt), 1)

    def test_the_claude_executor_is_left_on_its_own_arguments(self):
        """Task 810 widened the choice; it did not migrate the default.

        The ticket says so explicitly, and this is the assertion that would fail
        if the two harnesses were ever collapsed into one.
        """
        config = config_for(self.workdir)
        claude = resolve(DEFAULT_EXECUTOR, config)
        self.assertEqual(claude.binary, config.claude_bin)
        self.assertEqual(list(claude.args), list(config.claude_args))
        self.assertNotIn("--auto", claude.argv("the prompt"))
        self.assertEqual(claude.env, {})


# --------------------------------------------------------------------------- #
# The code graph a local run may navigate by                                    #
# --------------------------------------------------------------------------- #

class GraphNavigation(Repo):
    """Task 821 — the repository's existing graph, reachable from a local run.

    Two things are worth more than the rest of what is asserted here.

    **The graph is the CHECKOUT's, named absolutely.** A run works in the
    ticket's worktree, and `graphify-out` is untracked, so a worktree starts
    without one: a relative path would resolve to nothing, and a per-worktree
    graph would be the second indexer this ticket is not allowed to build.

    **A missing graph refuses the launch, and that is not belt-and-braces.**
    Measured against OpenCode 1.18.27 and graphify's own server: an interpreter
    that cannot import graphify is caught downstream — the server exits and
    `opencode mcp list` reports it failed — but a missing *graph* is not. The
    server starts, OpenCode reports it connected, the tools are advertised, and
    a query answers `isError: false` with the text "graph.json not found". A
    query that failed and reported success is the outcome this refusal exists
    to make impossible, and nothing downstream would have caught it.
    """

    def server(self, **overrides) -> dict:
        return opencode_settings(self.local(**overrides))["mcp"][GRAPHIFY_SERVER_NAME]

    def test_the_run_is_offered_the_graph_server_and_it_is_enabled(self):
        """Advertised, not merely present: a disabled entry serves nothing."""
        settings = opencode_settings(self.local())
        self.assertIn(GRAPHIFY_SERVER_NAME, settings["mcp"])
        self.assertTrue(settings["mcp"][GRAPHIFY_SERVER_NAME]["enabled"])
        self.assertEqual(settings["mcp"][GRAPHIFY_SERVER_NAME]["type"], "local")

    def test_the_command_serves_the_existing_graph_with_its_own_interpreter(self):
        """Graphify's server, graphify's interpreter, the checkout's graph.

        The interpreter is read from the record graphify writes beside the
        graph rather than guessed or configured: under a venv or a `uv tool`
        install the system `python3` cannot import graphify, and a second copy
        of that answer here would be a second thing to keep in step.
        """
        command = self.server()["command"]
        interpreter = (
            self.workdir / "graphify-out" / ".graphify_python"
        ).read_text(encoding="utf-8").strip()
        self.assertEqual(
            command,
            [
                interpreter,
                "-m",
                GRAPHIFY_SERVE_MODULE,
                str((self.workdir / "graphify-out" / "graph.json").resolve()),
            ],
        )

    def test_the_graph_is_named_absolutely_because_the_run_works_elsewhere(self):
        """The run's cwd is the worktree; a relative path would find nothing."""
        graph = Path(self.server()["command"][-1])
        self.assertTrue(graph.is_absolute())
        self.assertTrue(graph.is_file())

    def test_naming_the_graph_does_not_displace_the_rest_of_the_config(self):
        """The seat, its window and the permissions are all still stated.

        `mcp` is an addition to what the record already governs, and a config
        that lost the seat to gain a graph would run the wrong model quietly.
        """
        settings = opencode_settings(self.local())
        self.assertEqual(settings["model"], "ollama/qwen38-27b-abl:256k")
        self.assertEqual(
            settings["provider"]["ollama"]["models"]["qwen38-27b-abl:256k"]["limit"],
            {"context": 262144, "output": DEFAULT_OUTPUT_TOKENS},
        )
        self.assertEqual(settings["permission"]["bash"], "allow")

    def test_the_graph_is_stated_by_the_runner_not_left_to_a_host_config(self):
        """It travels in the generated blob, so no config on disk supplies it.

        OpenCode merges the configs it finds — the checkout's `opencode.json`,
        the host's `~/.config/opencode` — and both are edited by people for
        their own sessions. A run that inherited its graph from one of them
        would lose it the day somebody tidied up, and lose it silently.
        """
        executor = self.local()
        self.assertIn(
            GRAPHIFY_SERVER_NAME,
            json.loads(executor.env["OPENCODE_CONFIG_CONTENT"])["mcp"],
        )
        self.assertEqual(list(self.workdir.glob("**/opencode.json*")), [])

    def test_the_default_executor_is_not_given_a_graph_server(self):
        """`claude` is untouched: it reaches the graph its own way, or not.

        The ticket widened the local harness only, and this is the assertion
        that would fail if the two were ever collapsed into one.
        """
        claude = resolve(DEFAULT_EXECUTOR, config_for(self.workdir))
        self.assertEqual(claude.env, {})


class GraphRefusals(Repo):
    """A checkout that cannot serve its graph stops the run, loudly."""

    def rebuild_graphify(self, **kwargs) -> None:
        for name in ("graph.json", ".graphify_python"):
            path = self.workdir / "graphify-out" / name
            if path.exists():
                path.unlink()
        write_graphify(self.workdir, **kwargs)

    def test_a_checkout_with_no_graph_refuses_and_names_the_repair(self):
        self.rebuild_graphify(graph=False)
        with self.assertRaises(ExecutorError) as caught:
            self.local()
        message = str(caught.exception)
        self.assertIn("graphify-out/graph.json", message.replace(str(self.workdir), ""))
        self.assertIn("graphify update .", message)

    def test_a_checkout_with_no_interpreter_record_refuses(self):
        self.rebuild_graphify(interpreter=None)
        with self.assertRaises(ExecutorError) as caught:
            self.local()
        self.assertIn(".graphify_python", str(caught.exception))

    def test_an_empty_interpreter_record_refuses_rather_than_defaulting(self):
        """No fallback to `python3`: the one that can import graphify is named.

        A default here would produce a server that exits on start, which reads
        from the run as tools that were never there.
        """
        self.rebuild_graphify(interpreter="")
        with self.assertRaises(ExecutorError) as caught:
            self.local()
        self.assertIn("names no interpreter", str(caught.exception))

    def test_an_interpreter_that_is_not_there_refuses(self):
        self.rebuild_graphify(interpreter="/nowhere/bin/python")
        with self.assertRaises(ExecutorError) as caught:
            self.local()
        self.assertIn("/nowhere/bin/python", str(caught.exception))

    def test_the_refusal_does_not_fall_back_to_the_default_executor(self):
        """The alternative to a local run is a paid one, so nothing is retried.

        `resolve` is asked for the local executor by name and must raise, not
        hand back the Claude Code one with a warning.
        """
        self.rebuild_graphify(graph=False)
        with self.assertRaises(ExecutorError):
            resolve(LOCAL_EXECUTOR, config_for(self.workdir))

    def test_the_default_executor_still_resolves_without_a_graph(self):
        """A graph is the local harness's dependency, not the runner's.

        `claude` runs were explicitly left alone, so a checkout with no graph
        must not become a checkout that cannot run a ticket at all.
        """
        self.rebuild_graphify(graph=False)
        claude = resolve(DEFAULT_EXECUTOR, config_for(self.workdir))
        self.assertEqual(claude.name, DEFAULT_EXECUTOR)


# --------------------------------------------------------------------------- #
# Through the runner, where it has to keep everything else the same             #
# --------------------------------------------------------------------------- #

class ThroughTheRunner(ServiceTestCase):
    """The launch path, with a workdir that carries an approved-model record."""

    def setUp(self) -> None:
        super().setUp()
        self._repo = TemporaryDirectory()
        self.addCleanup(self._repo.cleanup)
        workdir = write_repo(Path(self._repo.name))
        self.config = make_config(self.state_dir, workdir=workdir)
        self.service.config = self.config
        self.launcher.config = self.config

    def work_local(self):
        return self.service.work(self.service.get_by_task_number(8), LOCAL_EXECUTOR)

    def test_a_local_run_uses_a_different_harness_in_the_same_directory(self):
        """The executor chooses a harness. It does not choose a launcher.

        This test used to assert the opposite half — that the two argvs were
        identical — on the reasoning that the worktree, the branch, the tests,
        the commit and the report-back all belonged to Claude Code, so swapping
        the binary would lose them. Task 810 is the ticket that checked that
        list. The worktree and the branch became the runner's in task 756; the
        commit and the report-back are instructions in the prompt and a helper
        script, both harness-neutral. What was left was running commands at all,
        and there Claude Code headless was the defect rather than the guarantee.

        So the binary now differs on purpose, and what must not differ is
        everything the runner owns: the directory, and one launch path through
        it.
        """
        self.service.work(self.service.get_by_task_number(8))
        default_call = self.spawn.calls[0]
        self.launcher._release(9)
        self.work_local()
        local_call = self.spawn.calls[1]

        self.assertEqual(local_call["cwd"], default_call["cwd"])
        self.assertNotEqual(local_call["argv"][0], default_call["argv"][0])
        self.assertEqual(local_call["argv"][0], self.config.opencode_bin)
        self.assertEqual(default_call["argv"][0], self.config.claude_bin)

    def test_both_executors_go_through_one_launch_path(self):
        """One lock, one log, one record shape, whichever harness ran.

        The launch record is what `/launches`, the console and the `work`
        response all read, so a harness that produced a differently shaped one
        would be a second runner wearing the first one's clothes.
        """
        default = self.service.work(self.service.get_by_task_number(8))
        self.launcher._release(9)
        local = self.work_local()
        self.assertEqual(sorted(default), sorted(local))
        self.assertEqual(default["reference"], local["reference"])
        self.assertEqual(default["workdir"], local["workdir"])

    def test_both_harnesses_are_told_how_to_navigate_the_code(self):
        """Task 825, asserted where the two harnesses are actually spawned.

        The rule is only worth anything if it reaches the run, and the run that
        needed it was the local one: #821 connected Graphify to OpenCode and the
        restarted #813 run kept grepping, because the prompt never mentioned it.
        So this reads the navigation rule out of the prompt argument of each
        spawned argv rather than out of `build_prompt` — the same place the
        launcher's other cross-harness assertions read, and the only place that
        can show a per-executor prompt if one ever appears.
        """
        self.service.work(self.service.get_by_task_number(8))
        self.launcher._release(9)
        self.work_local()

        default_prompt = self.spawn.calls[0]["argv"][-1]
        local_prompt = self.spawn.calls[1]["argv"][-1]
        for prompt in (default_prompt, local_prompt):
            self.assertIn("NAVIGATING THE CODE", prompt)
            self.assertIn("ask it first for RELATIONSHIP", prompt)
            self.assertIn("Text search is still the right tool", prompt)
        # Not merely "both contain it": the brief is one text, so the rule
        # cannot be present in both and yet differ between them.
        self.assertEqual(default_prompt, local_prompt)

    def test_the_model_reaches_the_child_through_the_environment(self):
        self.work_local()
        call = self.spawn.calls[0]
        settings = json.loads(call["env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(settings["model"], "ollama/qwen38-27b-abl:256k")
        provider = settings["provider"]["ollama"]
        self.assertEqual(
            provider["models"]["qwen38-27b-abl:256k"]["limit"]["context"], 262144
        )
        self.assertEqual(
            provider["options"]["baseURL"], self.config.local_executor_base_url
        )
        self.assertIn("--auto", call["argv"])

    def test_the_graph_the_child_gets_is_the_checkouts_not_the_worktrees(self):
        """The one assertion that needs a real launch to make (task 821).

        Everything else about the graph can be read off the generated config,
        but this is about the gap between where the config is built and where
        the run works: the runner makes a worktree and spawns into it, and that
        worktree has no `graphify-out` of its own — it is untracked, so a fresh
        one never does. A graph named relatively, or named from the run's own
        directory, would resolve to nothing there; and nothing is what a
        missing graph looks like from inside the run, because the server still
        starts and still answers.
        """
        self.work_local()
        call = self.spawn.calls[0]
        settings = json.loads(call["env"]["OPENCODE_CONFIG_CONTENT"])
        graph = Path(settings["mcp"][GRAPHIFY_SERVER_NAME]["command"][-1])
        self.assertEqual(
            graph, (self.config.workdir / "graphify-out" / "graph.json").resolve()
        )
        self.assertTrue(graph.is_file())

        worktree = Path(call["cwd"])
        self.assertNotEqual(worktree, self.config.workdir)
        self.assertFalse((worktree / "graphify-out").exists())

    def test_the_runners_own_environment_is_not_displaced_by_the_executors(self):
        """The Vikunja settings are applied after the executor, on purpose."""
        self.work_local()
        env = self.spawn.calls[0]["env"]
        self.assertEqual(env["VIKUNJA_PROJECT"], self.config.project_title)
        self.assertEqual(env["VIKUNJA_TASK_NUMBER"], "8")

    def test_the_ticket_context_is_the_same_prompt_the_default_executor_gets(self):
        """No second prompt, and nothing trimmed for being a local model.

        Built against the run's own worktree, because that is what the launched
        prompt names: comparing it with a prompt naming the repository root
        would fail on the directory rather than on the ticket context, which is
        what this is about.
        """
        self.work_local()
        worktree = Path(self.spawn.calls[0]["cwd"])
        expected = self.service.prompt_for(
            self.service.get_by_task_number(8), worktree
        )
        self.assertEqual(self.spawn.calls[0]["argv"][-1], expected)

    def test_the_run_records_which_executor_and_model_it_used(self):
        result = self.work_local()
        self.assertEqual(result["executor"], LOCAL_EXECUTOR)
        self.assertEqual(result["model"], "qwen38-27b-abl:256k")

    def test_a_default_run_records_the_default_executor_and_no_model(self):
        result = self.service.work(self.service.get_by_task_number(8))
        self.assertEqual(result["executor"], DEFAULT_EXECUTOR)
        self.assertIsNone(result["model"])

    def test_the_one_run_per_ticket_lock_does_not_care_which_model(self):
        """A second launch is refused whichever executor asked for it.

        Two runs of one ticket on two models is still two runs editing one
        checkout, which is the thing the lock exists to prevent.
        """
        result = self.service.work(self.service.get_by_task_number(8))
        self.alive_pids.add(result["pid"])
        with self.assertRaises(AlreadyRunning):
            self.work_local()

    def test_the_configured_default_executor_is_used_when_none_is_named(self):
        self.config = make_config(
            self.state_dir, workdir=self.config.workdir, executor=LOCAL_EXECUTOR
        )
        self.service.config = self.config
        self.launcher.config = self.config
        result = self.service.work(self.service.get_by_task_number(8))
        self.assertEqual(result["executor"], LOCAL_EXECUTOR)

    def test_an_unusable_executor_leaves_the_ticket_where_it_was(self):
        """Resolution happens before the bucket move, so a refusal changes nothing.

        Otherwise a typo parks a ticket in In Progress with nothing running, and
        the board says work is happening that is not.
        """
        with self.assertRaises(ExecutorError):
            self.service.work(self.service.get_by_task_number(8), "qwen-code")
        self.assertEqual(self.vikunja.bucket_of(9), "Ready")
        self.assertEqual(self.spawn.calls, [])


class DefaultExecutorUnchanged(ServiceTestCase):
    """The existing path, asserted from the outside as still doing nothing new."""

    def test_no_model_setting_is_added_to_or_removed_from_a_default_run(self):
        """A default run inherits the service's environment untouched.

        Asserted as a set difference rather than as "no ANTHROPIC_* present":
        the service's own environment may legitimately carry such keys, and a
        test that forbade them outright would fail for the wrong reason.
        """
        self.service.work(self.service.get_by_task_number(8))
        env = self.spawn.calls[0]["env"]

        def model_keys(mapping):
            return {
                k for k in mapping if k.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))
            }

        self.assertEqual(model_keys(env), model_keys(os.environ))


class OverTheSocket(HttpFlow):
    """`?executor=` on the work routes, and what an unknown one does."""

    def setUp(self) -> None:
        super().setUp()
        self._repo = TemporaryDirectory()
        self.addCleanup(self._repo.cleanup)
        self.config = make_config(
            self.state_dir, workdir=write_repo(Path(self._repo.name))
        )
        self.service.config = self.config
        self.launcher.config = self.config

    def test_the_query_parameter_selects_the_local_model(self):
        status, body, _ = self.fetch("/ticket/8/work?executor=local", method="POST")
        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body)["model"], "qwen38-27b-abl:256k")

    def test_a_work_request_naming_nothing_still_takes_the_default(self):
        status, body, _ = self.fetch("/ticket/8/work", method="POST")
        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body)["executor"], DEFAULT_EXECUTOR)

    def test_an_unknown_executor_is_a_400_and_launches_nothing(self):
        """The caller's mistake, and never quietly the default executor.

        Answering "run this on the local model" by running it on a paid one is
        the one wrong way to handle this, so the fallback is a refusal.
        """
        status, body, _ = self.fetch("/ticket/8/work?executor=gpt", method="POST")
        self.assertEqual(status, 400)
        self.assertIn("Unknown executor", body)
        self.assertEqual(self.spawn.calls, [])

    def test_the_console_offers_the_local_model(self):
        _, body, _ = self.fetch("/")
        self.assertIn("executor=local", body)


class ExecutorDataclass(unittest.TestCase):
    def test_apply_removes_before_it_adds(self):
        executor = Executor(
            name="x", binary="x-bin", env={"A": "new"}, unset=frozenset({"A", "B"})
        )
        self.assertEqual(executor.apply({"A": "old", "B": "gone", "C": "kept"}),
                         {"A": "new", "C": "kept"})
