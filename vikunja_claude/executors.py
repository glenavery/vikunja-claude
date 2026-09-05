"""How a run is launched: which harness, and which model behind it.

The runner owns ticket resolution, the worktree, the prompt, the lock, the
launch, the log and the reap. An executor owns the two questions that are left —
*what binary is spawned* and *what model does it talk to* — as an argv and as
extra environment for the child process. That is the whole abstraction.

Task 821 added a third thing the local one may answer, and it is deliberately
narrow: *what the harness can reach*. OpenCode takes its whole configuration
from that environment, so the code graph a run navigates by is stated in the
same generated blob as the seat — see ``graphify_server``. It is still a
property of how the run is launched, not of the ticket, and the ``claude``
executor is untouched by it.

**It used to own only the second one, and task 810 is why it now owns both.**
The harness was Claude Code in every case, on the reasoning that everything the
runner leans on the harness for belongs to the harness rather than to the model,
so swapping the CLI would take all of it away. That reasoning was sound when it
was written and two of its three premises have since expired:

- the **worktree** stopped being one of those things in task 756, when the
  runner started making it itself, before the spawn, for every executor
  (``vikunja_claude/worktree.py``). It is now the runner's property, not any
  harness's.
- the **commit and the report-back** were never the harness's either. They are
  instructions in the prompt (``vikunja_claude/prompt.py``) and a helper script
  the child runs, and both are harness-neutral text.
- what actually remained was **running commands at all**, and there Claude Code
  headless was the problem rather than the guarantee: ``--permission-mode
  acceptEdits`` accepts file edits but *denies bash*, so a local run could edit
  code and then not run a test, not run ``git commit`` and not run ``vkctl.py``.
  Task #807 is what that looks like from outside.

So ``local`` is now **OpenCode**, driving the same approved seat, with command
execution unrestricted inside the ticket's worktree. ``claude`` is untouched and
stays Claude Code: this widened the abstraction, it did not migrate the default.

What did NOT change, and is the reason there is still one launcher: the
worktree, the branch, the prompt, the lock, the run log, the reap and the
``vkctl.py`` report-back are all the runner's, identical whichever executor ran.
An executor still knows nothing about tickets.

The local executor takes the model's identity from the investment repository's
``deploy/ollama/models.json`` — the tracked record of which local models are
approved and what job each holds. Moving the ``local_coding`` seat there moves
this executor with it, with no change to any code in this package. That is the
point: an approval belongs in one place, and a second copy of the model string
here would be a second thing to keep in step.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, annotation only
    from .config import Config

#: Where the approved-model record lives, relative to the repository the runner
#: works in. A path rather than a setting: it is a fact about that repository's
#: layout, and a configurable one would only let the two drift.
MANIFEST_PATH = Path("deploy") / "ollama" / "models.json"

#: The job this executor's model has been approved to hold. `models.json`
#: carries the key and a test there holds it to exactly one holder.
LOCAL_CODING_SEAT = "local_coding"

#: The default: Claude Code as installed, talking to whatever it normally talks
#: to. Named so a launch record can say so, rather than saying nothing.
DEFAULT_EXECUTOR = "claude"
LOCAL_EXECUTOR = "local"

#: How OpenCode is told which model to use: one provider block, one model, one
#: name. The provider id is ours to choose and is not read from anywhere else,
#: so it is a constant rather than a setting.
OLLAMA_PROVIDER = "ollama"

#: The adapter OpenCode loads to speak to that provider. Ollama serves the
#: OpenAI-compatible shape natively at ``/v1``, which is the whole reason this
#: path needs no transport shim where the Claude Code one did.
OLLAMA_PROVIDER_NPM = "@ai-sdk/openai-compatible"

#: Environment carrying a paid provider's credentials, removed from every local
#: run. The Claude Code executor removed one key for one reason — "the run
#: fails" must never quietly become "the run bills the frontier model" — and
#: OpenCode makes that reason bigger rather than smaller: it discovers providers
#: from the environment, and can address Anthropic, OpenAI, OpenRouter, Google
#: and Ollama's own hosted tier. Pinning ``--model`` is what chooses the seat;
#: this is what makes the alternatives unreachable rather than merely unchosen.
PAID_PROVIDER_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
        "OLLAMA_API_KEY",
    }
)

#: The output cap OpenCode's config schema requires beside the context window.
#:
#: Unlike the context, this is **not** read from the approved record, because
#: the record does not state it: no recipe in ``deploy/ollama/`` sets
#: ``num_predict``, so the seat imposes no output limit of its own and a model
#: may write until the window is full. OpenCode nonetheless refuses a config
#: without the key ("Missing key ... limit.output"), so a number has to be
#: stated, and it is bookkeeping for the harness rather than a claim about the
#: model.
#:
#: It matches what the investment repository's own ``opencode.json`` already
#: uses for this seat, so an interactive OpenCode session and a ticket run
#: behave the same way on the same model. If the record ever states an output
#: cap, this should be read from it the way the context is.
DEFAULT_OUTPUT_TOKENS = 32768

#: Where the repository's Graphify code graph lives, relative to the repository
#: root, and the interpreter record graphify writes beside it. Paths rather than
#: settings for the reason ``MANIFEST_PATH`` is one: both are facts about that
#: repository's layout, and a configurable copy would only be a second thing to
#: keep in step. The interpreter in particular is graphify's own answer to
#: "which python can import me" — under a venv or a ``uv tool`` install the
#: system ``python3`` cannot, and graphify records the one that can.
GRAPHIFY_DIR = Path("graphify-out")
GRAPHIFY_GRAPH = GRAPHIFY_DIR / "graph.json"
GRAPHIFY_INTERPRETER = GRAPHIFY_DIR / ".graphify_python"

#: What the graph server is called in the generated config. OpenCode namespaces
#: an MCP server's tools under its name, so this is also what a run sees.
GRAPHIFY_SERVER_NAME = "graphify"

#: The module that serves an existing graph over MCP on stdio. Serving the graph
#: the repository already has is the whole of this: nothing here builds, updates
#: or refreshes one, and a run that wants a fresher graph asks the same
#: ``graphify`` the humans do.
GRAPHIFY_SERVE_MODULE = "graphify.serve"


_NUM_CTX = re.compile(r"^PARAMETER\s+num_ctx\s+(\d+)", re.M)


class ExecutorError(RuntimeError):
    """The requested executor cannot be built.

    Always a refusal, never a fallback. The alternative to a local run is a run
    against a paid frontier model, so "the local configuration is broken, so I
    used the other one" would turn a typo into a bill.
    """


@dataclass(frozen=True)
class Executor:
    """One answer to "how is a run launched": an argv, and child environment."""

    name: str
    #: The binary to spawn. No default: a harness picked by omission is exactly
    #: the silent fallback the rest of this module refuses.
    binary: str
    #: Everything between the binary and the prompt.
    args: tuple[str, ...] = ()
    #: What is published about the run: the model id, or None for the harness's
    #: own default, which this package does not get to name.
    model: str | None = None
    #: Added to the child's environment.
    env: dict[str, str] = field(default_factory=dict)
    #: Removed from the child's environment, whatever the service inherited.
    unset: frozenset[str] = frozenset()

    def argv(self, prompt: str) -> list[str]:
        """The command line, with the prompt last.

        Last because both harnesses take it as a trailing positional, and
        because the launcher's own tests read ``argv[-1]`` to assert that the
        two executors are handed the same ticket context.
        """
        return [self.binary, *self.args, prompt]

    def apply(self, environ: dict[str, str]) -> dict[str, str]:
        child = {k: v for k, v in environ.items() if k not in self.unset}
        child.update(self.env)
        return child


def _manifest(workdir: Path) -> dict:
    path = workdir / MANIFEST_PATH
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ExecutorError(
            f"No approved-model record at {path}. The local executor reads the "
            "seat from the repository it works in; check CLAUDE_WORKDIR."
        ) from None
    except json.JSONDecodeError as exc:
        raise ExecutorError(f"{path} is not readable JSON: {exc}") from None


def _seat_entry(manifest: dict, workdir: Path) -> dict:
    holders = [m for m in manifest.get("models", []) if m.get("seat") == LOCAL_CODING_SEAT]
    if not holders:
        raise ExecutorError(
            f"No model in {workdir / MANIFEST_PATH} holds the "
            f"{LOCAL_CODING_SEAT!r} seat. Approving one is an edit there, not "
            "here."
        )
    if len(holders) > 1:
        # models.json's own tests forbid this. Refusing rather than picking
        # keeps the decision on the side that made it ambiguous.
        raise ExecutorError(
            f"{len(holders)} models hold the {LOCAL_CODING_SEAT!r} seat: "
            f"{[m.get('name') for m in holders]}. The seat names one model."
        )
    return holders[0]


def _model_id(entry: dict) -> str:
    """The name to send, preferring an alias over a floating ``:latest``.

    ``ollama create`` leaves ``latest`` as the only tag on a locally built
    model, and ``models.json`` gives such a model an explicit alias for exactly
    that reason. Both names resolve to one blob, so this is about what ends up
    written in a launch log a person reads months later.
    """
    aliases = entry.get("aliases") or []
    if aliases:
        return aliases[0]
    name = entry.get("name")
    if not name:
        raise ExecutorError("the seat holder has no name in models.json")
    return name


def _context_tokens(entry: dict, workdir: Path) -> int:
    """The context the model runs at for a caller that sends no ``num_ctx``.

    Which is what both harnesses are: they speak a chat API with no field for
    the runtime's context size, so the Modelfile's standalone default is the one
    that governs. Reading it here rather than copying the number into
    ``models.json`` is deliberate — that repository's README calls the
    duplication out as the mistake worth preventing.

    It is read so the harness can be told the truth. A harness that does not
    know a model's window assumes one and compacts to it, which on a 256k model
    throws away most of the context the ticket asked to have available.
    """
    modelfile = entry.get("modelfile")
    if not modelfile:
        raise ExecutorError(f"{entry.get('name')} names no modelfile in models.json")
    path = workdir / MANIFEST_PATH.parent / modelfile
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ExecutorError(f"{entry.get('name')} names {path}, which is missing") from None
    match = _NUM_CTX.search(text)
    if not match:
        raise ExecutorError(
            f"{path} states no `PARAMETER num_ctx`, so there is no approved "
            "context to run at. A guess here would be a silently smaller window."
        )
    return int(match.group(1))


def graphify_server(workdir: Path) -> dict:
    """The MCP entry that puts the repository's existing code graph in reach.

    A local run navigates by ``grep`` and ``read``: every question about who
    calls a function, what a module owns or where a rule is enforced is paid for
    in whole files pulled into the window. The repository already carries the
    answer as a graph — ``graphify-out/graph.json``, built and refreshed for the
    humans working the same checkout — and graphify already knows how to serve
    it over MCP. This exposes that server to the run; it does not build a second
    index, and it does not make the graph the only way to look something up.

    **The graph is the CHECKOUT's, named absolutely, and that is deliberate.**
    A run's cwd is the ticket's worktree, where ``graphify-out`` does not exist:
    it is untracked, so a worktree starts without one. A relative path would
    therefore resolve to nothing, and building one per worktree is the second
    indexer this is not allowed to be. The graph's nodes carry repository-
    relative sources (``api/db_config.py L50``), so a symbol it resolves is a
    symbol at that path inside the worktree.

    **Both checks below are load-bearing, and neither is redundant with
    OpenCode's own.** Measured against the real binary (1.18.27):

    - an interpreter that cannot import graphify is caught — the server exits at
      once and ``opencode mcp list`` reports ``✗ failed``;
    - a missing *graph* is not. The server starts, OpenCode reports
      ``✓ connected``, the tools are advertised, and a query comes back as
      ``isError: false`` carrying "graph.json not found" as its answer text.
      That is a query that failed and said it succeeded, which is the one
      outcome a run must never be handed.

    So the graph's existence is settled here, before the launch, where it can be
    a refusal that names its own repair.
    """
    graph = (workdir / GRAPHIFY_GRAPH).resolve()
    if not graph.is_file():
        raise ExecutorError(
            f"No Graphify graph at {graph}. The local executor serves the "
            "graph the repository already has; build it there with "
            "`graphify update .`. Left unchecked this is not an error at all: "
            "OpenCode reports the server connected and every query answers "
            "'graph.json not found' as though it had succeeded."
        )
    record = (workdir / GRAPHIFY_INTERPRETER).resolve()
    try:
        interpreter = record.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise ExecutorError(
            f"No interpreter record at {record}. Graphify writes it beside the "
            "graph, naming the python that can import it; rebuilding the graph "
            "writes it again."
        ) from None
    if not interpreter:
        raise ExecutorError(f"{record} is empty, so it names no interpreter.")
    if not Path(interpreter).exists():
        raise ExecutorError(
            f"{record} names {interpreter}, which is not there. That is the "
            "graph's interpreter having moved or its environment having been "
            "removed, not a path to guess at."
        )
    return {
        "type": "local",
        "command": [interpreter, "-m", GRAPHIFY_SERVE_MODULE, str(graph)],
        "enabled": True,
    }


def opencode_config(model: str, context: int, base_url: str, graphify: dict) -> dict:
    """The whole of what OpenCode is told, derived from the approved record.

    Handed to the child as ``OPENCODE_CONFIG_CONTENT`` rather than written to a
    file, for the same reason the model string is read rather than copied: a
    file is a second copy of the seat, and a second copy is a thing that goes
    stale. OpenCode merges this over the configs it finds on disk and this one
    is loaded last, so the run is governed by the record even on a host whose
    own ``opencode.json`` names something else.

    ``permission`` states the unrestricted execution the ticket run needs. It is
    stated here *and* as ``--auto`` on the argv, which is not redundancy by
    accident: this settles the three named capabilities so no ask is raised at
    all, and ``--auto`` answers any ask these three do not cover. Both matter,
    because of what OpenCode does with an unanswered ask — see
    ``DEFAULT_OPENCODE_ARGS``.

    ``limit`` carries both halves because OpenCode refuses a config missing
    either, and they come from different places on purpose: the context is read
    from the seat's own recipe, and the output cap is
    ``DEFAULT_OUTPUT_TOKENS`` because the record states none. Only the first is
    a fact about the approved model.

    ``mcp`` names the code graph the run may navigate by (task 821). It is
    stated *here*, in what the runner generates, rather than left to the
    checkout's own ``opencode.json`` or the host's ``~/.config/opencode``:
    OpenCode merges every config it finds and both of those are edited by
    people for their own sessions, so a run that inherited the graph from one
    of them would lose it the day somebody tidied up, and lose it silently.
    Merging is by key, so a project or host config naming other servers keeps
    them; only ``graphify`` is the runner's.
    """
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": f"{OLLAMA_PROVIDER}/{model}",
        "provider": {
            OLLAMA_PROVIDER: {
                "npm": OLLAMA_PROVIDER_NPM,
                "name": "Ollama",
                "options": {"baseURL": base_url},
                "models": {
                    model: {
                        "name": model,
                        "limit": {
                            "context": context,
                            "output": DEFAULT_OUTPUT_TOKENS,
                        },
                    }
                },
            }
        },
        "permission": {"edit": "allow", "bash": "allow", "webfetch": "allow"},
        "mcp": {GRAPHIFY_SERVER_NAME: graphify},
    }


def claude_executor(config: "Config") -> Executor:
    """Claude Code as installed, talking to whatever it normally talks to.

    Adds and removes nothing. This is the path task 810 deliberately left alone:
    the harness question was opened for the local seat, not answered again for
    the default one.
    """
    return Executor(
        name=DEFAULT_EXECUTOR,
        binary=config.claude_bin,
        args=tuple(config.claude_args),
    )


def local_executor(config: "Config") -> Executor:
    """OpenCode, driving the approved local seat, unrestricted in the worktree.

    ``config.local_executor_base_url`` is Ollama's own OpenAI-compatible
    endpoint, reached directly. The Anthropic transport shim this used to go
    through exists because Claude Code speaks the Anthropic message shape and
    appends a trailing ``role: system`` message Ollama refuses; OpenCode speaks
    the OpenAI shape, which Ollama serves natively, so the shim is not on this
    path at all any more.

    The graph server is resolved from ``config.workdir`` — the checkout, not the
    worktree the run will work in — and refuses rather than degrading, for the
    reasons in ``graphify_server``.
    """
    entry = _seat_entry(_manifest(config.workdir), config.workdir)
    model = _model_id(entry)
    context = _context_tokens(entry, config.workdir)
    settings = opencode_config(
        model,
        context,
        config.local_executor_base_url.rstrip("/"),
        graphify_server(config.workdir),
    )
    return Executor(
        name=LOCAL_EXECUTOR,
        binary=config.opencode_bin,
        # The model is pinned on the command line as well as in the config, and
        # it is appended after the configurable arguments so that it is the one
        # thing an `OPENCODE_ARGS` cannot take off. Which model a run may use is
        # the seat's answer, not a deployment's.
        args=(*config.opencode_args, "--model", f"{OLLAMA_PROVIDER}/{model}"),
        model=model,
        env={"OPENCODE_CONFIG_CONTENT": json.dumps(settings)},
        unset=PAID_PROVIDER_KEYS,
    )


def resolve(name: str, config: "Config") -> Executor:
    """The executor by name, or a refusal naming the ones that exist.

    Takes the whole config rather than the handful of settings each executor
    happens to need today: an executor now answers "how is a run launched",
    which is a config-shaped question, and a per-setting signature is a list to
    forget an entry from the next time one is added.
    """
    if name == DEFAULT_EXECUTOR:
        return claude_executor(config)
    if name == LOCAL_EXECUTOR:
        return local_executor(config)
    raise ExecutorError(
        f"Unknown executor {name!r}. Available: "
        f"{DEFAULT_EXECUTOR!r}, {LOCAL_EXECUTOR!r}."
    )
