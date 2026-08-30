"""Which model a run drives, and nothing else.

The runner owns ticket resolution, the prompt, the lock, the launch, the log and
the reap. An executor owns exactly one question — *which model does the launched
Claude Code talk to* — expressed as extra environment for the child process.
That is the whole abstraction, and it is deliberately this small:

- the **harness stays Claude Code** in every case. Everything the runner relies
  on the harness for — its worktree mode, the branch it makes, running the
  tests, the commit, reporting back through ``vkctl.py`` — is a property of that
  harness, not of the model behind it. Swapping in a different CLI would take
  those away; swapping the model does not touch them.
- so there is no second launcher, no per-model process handling, and nothing
  here that knows about tickets.

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

_NUM_CTX = re.compile(r"^PARAMETER\s+num_ctx\s+(\d+)", re.M)


class ExecutorError(RuntimeError):
    """The requested executor cannot be built.

    Always a refusal, never a fallback. The alternative to a local run is a run
    against a paid frontier model, so "the local configuration is broken, so I
    used the other one" would turn a typo into a bill.
    """


@dataclass(frozen=True)
class Executor:
    """One answer to "which model", as environment for the launched process."""

    name: str
    #: What is published about the run: the model id, or None for the harness's
    #: own default, which this package does not get to name.
    model: str | None = None
    #: Added to the child's environment.
    env: dict[str, str] = field(default_factory=dict)
    #: Removed from the child's environment, whatever the service inherited.
    unset: frozenset[str] = frozenset()

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

    Which is what Claude Code is: it speaks the Anthropic message shape and has
    no field for the runtime's context size, so the Modelfile's standalone
    default is the one that governs. Reading it here rather than copying the
    number into ``models.json`` is deliberate — that repository's README calls
    the duplication out as the mistake worth preventing.

    It is read so the harness can be told the truth. Claude Code assumes a 200k
    window for a model it does not recognise and auto-compacts to it, which on a
    256k model throws away a quarter of the context the ticket asked to have
    available.
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


def local_executor(workdir: Path, base_url: str) -> Executor:
    """Claude Code, pointed at the approved local seat through the shim.

    ``base_url`` is the transport shim, not Ollama directly: Ollama refuses the
    trailing ``role: system`` message Claude Code always appends, so the two
    cannot talk without it. See ``deploy/ollama/anthropic_shim.py`` in the
    investment repository.
    """
    entry = _seat_entry(_manifest(workdir), workdir)
    model = _model_id(entry)
    context = _context_tokens(entry, workdir)
    return Executor(
        name=LOCAL_EXECUTOR,
        model=model,
        env={
            "ANTHROPIC_BASE_URL": base_url.rstrip("/"),
            # Any non-empty value: the shim authenticates nothing, and Claude
            # Code refuses to start without one.
            "ANTHROPIC_AUTH_TOKEN": "local",
            # Every tier, not just the default. Claude Code reaches for the
            # small/fast model on its own for summarisation and titles, and one
            # unset tier is a request to a model this endpoint does not serve.
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "ANTHROPIC_SMALL_FAST_MODEL": model,
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(context),
        },
        # The service may well have been started with a real key in its
        # environment. A local run must not carry one: if the base URL is ever
        # wrong or unreachable in the wrong way, the difference between "the run
        # fails" and "the run silently bills the frontier model" is this line.
        unset=frozenset({"ANTHROPIC_API_KEY"}),
    )


def resolve(name: str, workdir: Path, local_base_url: str) -> Executor:
    """The executor by name, or a refusal naming the ones that exist."""
    if name == DEFAULT_EXECUTOR:
        return Executor(name=DEFAULT_EXECUTOR)
    if name == LOCAL_EXECUTOR:
        return local_executor(workdir, local_base_url)
    raise ExecutorError(
        f"Unknown executor {name!r}. Available: "
        f"{DEFAULT_EXECUTOR!r}, {LOCAL_EXECUTOR!r}."
    )
