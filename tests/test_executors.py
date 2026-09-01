"""Task 690 — selecting which model a run drives.

The runner gained one thing: a choice of model. It did not gain a second
launcher, a second lock, a second log, or any notion of "a local run" as a
different kind of run. Most of what is asserted here is therefore about what
stayed the same, because that is the part a later change is likely to break.

Two properties are worth more than the rest:

- **the approval is read, never copied.** The model comes from the investment
  repository's `deploy/ollama/models.json`, so moving the seat there moves the
  runner with no edit in this package. A test moves it and expects the runner to
  follow.
- **a broken local configuration refuses.** The alternative to a local run is a
  run against a paid frontier model, so a fallback would answer "run this
  locally" with a bill.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vikunja_claude.launcher import AlreadyRunning
from vikunja_claude.executors import (
    DEFAULT_EXECUTOR,
    LOCAL_EXECUTOR,
    Executor,
    ExecutorError,
    local_executor,
    resolve,
)

from .support import ServiceTestCase, make_config, make_repo
from .test_browser_flow import HttpFlow

BASE_URL = "http://127.0.0.1:11440"

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


def write_repo(root: Path, manifest=None, recipes=None) -> Path:
    """A workdir shaped like the investment repository's ollama deployment.

    A real git repository as well as the right files, because a launch now
    creates a worktree in the workdir and refuses if it cannot (task 756).
    """
    make_repo(root)
    ollama = root / "deploy" / "ollama"
    ollama.mkdir(parents=True, exist_ok=True)
    (ollama / "models.json").write_text(
        json.dumps(manifest if manifest is not None else MANIFEST), encoding="utf-8"
    )
    for name, text in (recipes or {"qwen38-27b-abl-256k.Modelfile": RECIPE}).items():
        (ollama / name).write_text(text, encoding="utf-8")
    return root


class Repo(unittest.TestCase):
    """A throwaway workdir carrying an approved-model record."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workdir = write_repo(Path(self._tmp.name))

    def rewrite(self, manifest=None, recipes=None) -> None:
        write_repo(self.workdir, manifest, recipes)


# --------------------------------------------------------------------------- #
# The model comes from the approved record                                      #
# --------------------------------------------------------------------------- #

class ResolvingTheSeat(Repo):
    def test_the_model_is_the_one_holding_the_local_coding_seat(self):
        executor = local_executor(self.workdir, BASE_URL)
        self.assertEqual(executor.model, "qwen38-27b-abl:256k")
        self.assertEqual(executor.env["ANTHROPIC_MODEL"], "qwen38-27b-abl:256k")

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
        executor = local_executor(self.workdir, BASE_URL)
        self.assertEqual(executor.model, "some-other:256k")
        self.assertEqual(executor.env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "131072")

    def test_an_alias_is_preferred_over_a_floating_latest_tag(self):
        executor = local_executor(self.workdir, BASE_URL)
        self.assertNotIn(":latest", executor.model or "")

    def test_a_seat_holder_with_no_alias_is_named_by_its_built_name(self):
        manifest = json.loads(json.dumps(MANIFEST))
        manifest["models"][0]["aliases"] = []
        self.rewrite(manifest)
        self.assertEqual(
            local_executor(self.workdir, BASE_URL).model,
            "qwen38-27b-abl-256k:latest",
        )


# --------------------------------------------------------------------------- #
# The approved context, not a guess and not a default                           #
# --------------------------------------------------------------------------- #

class ApprovedContext(Repo):
    def test_the_window_is_read_from_the_seats_own_recipe(self):
        """Claude Code sends no num_ctx, so the Modelfile default is what runs.

        It has to be told, too: Claude Code assumes 200k for a model it does not
        recognise and auto-compacts to it, which on this seat would throw away a
        quarter of the context the ticket asks to have available.
        """
        executor = local_executor(self.workdir, BASE_URL)
        self.assertEqual(executor.env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "262144")

    def test_a_recipe_that_states_no_context_is_a_refusal_not_a_default(self):
        self.rewrite(recipes={"qwen38-27b-abl-256k.Modelfile": "FROM base\n"})
        with self.assertRaises(ExecutorError) as caught:
            local_executor(self.workdir, BASE_URL)
        self.assertIn("num_ctx", str(caught.exception))


# --------------------------------------------------------------------------- #
# A broken local configuration refuses; it never falls back                     #
# --------------------------------------------------------------------------- #

class Refusals(Repo):
    def test_a_missing_record_names_the_path_it_looked_at(self):
        with TemporaryDirectory() as empty:
            with self.assertRaises(ExecutorError) as caught:
                local_executor(Path(empty), BASE_URL)
        self.assertIn("models.json", str(caught.exception))

    def test_a_record_with_no_seat_holder_refuses(self):
        manifest = json.loads(json.dumps(MANIFEST))
        del manifest["models"][0]["seat"]
        self.rewrite(manifest)
        with self.assertRaises(ExecutorError):
            local_executor(self.workdir, BASE_URL)

    def test_two_seat_holders_refuse_rather_than_one_being_picked(self):
        manifest = json.loads(json.dumps(MANIFEST))
        manifest["models"][1]["seat"] = "local_coding"
        self.rewrite(manifest)
        with self.assertRaises(ExecutorError) as caught:
            local_executor(self.workdir, BASE_URL)
        self.assertIn("seat names one model", str(caught.exception))

    def test_an_unreadable_record_refuses(self):
        (self.workdir / "deploy" / "ollama" / "models.json").write_text(
            "{not json", encoding="utf-8"
        )
        with self.assertRaises(ExecutorError):
            local_executor(self.workdir, BASE_URL)

    def test_an_unknown_executor_name_names_the_ones_that_exist(self):
        with self.assertRaises(ExecutorError) as caught:
            resolve("qwen-code", self.workdir, BASE_URL)
        self.assertIn(DEFAULT_EXECUTOR, str(caught.exception))
        self.assertIn(LOCAL_EXECUTOR, str(caught.exception))


# --------------------------------------------------------------------------- #
# What the child process is given                                               #
# --------------------------------------------------------------------------- #

class ChildEnvironment(Repo):
    def test_every_model_tier_is_pinned_not_only_the_default(self):
        """Claude Code reaches for its small/fast model unasked.

        One unset tier is a request to a model this endpoint does not serve, on
        a code path nobody chose — summarisation, a conversation title — and it
        fails in the middle of a run rather than at the start.
        """
        env = local_executor(self.workdir, BASE_URL).env
        for key in (
            "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_SMALL_FAST_MODEL",
        ):
            self.assertEqual(env[key], "qwen38-27b-abl:256k", key)

    def test_the_endpoint_is_the_shim_and_a_trailing_slash_does_not_survive(self):
        executor = local_executor(self.workdir, "http://127.0.0.1:11440/")
        self.assertEqual(executor.env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:11440")

    def test_a_real_api_key_is_removed_from_a_local_runs_environment(self):
        """The one line between "the run fails" and "the run bills the frontier".

        The service may perfectly well have been started with a key in its
        environment, and the child inherits everything by default.
        """
        executor = local_executor(self.workdir, BASE_URL)
        child = executor.apply({"ANTHROPIC_API_KEY": "sk-real", "PATH": "/usr/bin"})
        self.assertNotIn("ANTHROPIC_API_KEY", child)
        self.assertEqual(child["PATH"], "/usr/bin")

    def test_the_default_executor_adds_and_removes_nothing(self):
        """`claude` is the harness as installed, and must stay untouched."""
        executor = resolve(DEFAULT_EXECUTOR, self.workdir, BASE_URL)
        environ = {"ANTHROPIC_API_KEY": "sk-real", "PATH": "/usr/bin"}
        self.assertEqual(executor.apply(environ), environ)
        self.assertIsNone(executor.model)


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

    def test_a_local_run_uses_the_same_binary_arguments_and_directory(self):
        """The executor chooses a model. It does not choose a launcher.

        Everything the runner relies on the harness for — its worktree mode, the
        branch, running the tests, the commit, reporting back — is a property of
        Claude Code, not of the model behind it. A local run that swapped the
        binary would lose all of it silently.
        """
        self.service.work(self.service.get_by_task_number(8))
        default_call = self.spawn.calls[0]
        self.launcher._release(9)
        self.work_local()
        local_call = self.spawn.calls[1]
        self.assertEqual(local_call["argv"], default_call["argv"])
        self.assertEqual(local_call["cwd"], default_call["cwd"])

    def test_the_model_reaches_the_child_through_the_environment(self):
        self.work_local()
        env = self.spawn.calls[0]["env"]
        self.assertEqual(env["ANTHROPIC_MODEL"], "qwen38-27b-abl:256k")
        self.assertEqual(env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "262144")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:11440")

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
            name="x", env={"A": "new"}, unset=frozenset({"A", "B"})
        )
        self.assertEqual(executor.apply({"A": "old", "B": "gone", "C": "kept"}),
                         {"A": "new", "C": "kept"})
