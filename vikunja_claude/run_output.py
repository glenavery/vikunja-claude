"""The end of one run's own output, made readable and bounded (task 755).

A launch redirects Claude Code's stdout and stderr into a file of its own, and
``Launcher.run_status`` hands the end of that file back. That file is the only
run-output artifact there is, and this module is the whole of what reads it.

**Why anything had to change.** ``claude -p`` with the default ``text`` output
format writes nothing at all until the run is over: the answer is composed and
then printed once. So through the whole of a run — the interesting part, the
part a status read is asked about — the file held only whatever Claude Code had
put on *stderr* at startup, and #714 spent eight minutes looking at exactly two
such warning lines while the process was demonstrably working. No amount of
care on the read side can surface activity that was never written, which is why
the launch now asks for ``--output-format stream-json`` (``config.py``) and this
module renders it. The artifact, the capture path and the read surface are the
ones that were already there.

**Two harnesses, one artifact.** Since task 810 the ``local`` executor is
OpenCode rather than Claude Code, and ``opencode run --format json`` writes the
same kind of file in the same way: one JSON object per line, as the run happens.
The vocabularies differ and do not overlap, so this module renders both into one
set of labels and a reader never has to know which harness produced the line.
What follows describes Claude Code's; OpenCode's is ``text``, ``reasoning``,
``tool_use``, ``error`` and the two step markers, handled in
``_render_opencode``.

**What stream-json is.** One JSON object per line, written as the run happens:
a ``system``/``init`` line, an ``assistant`` line per model turn (its content
blocks carry text, thinking and tool calls), a ``user`` line carrying each tool
result, and a final ``result`` line. Rendering turns each into one short line
of the form ``<what it is>: <the first of it>``.

Three properties are load-bearing.

**A line that is not JSON passes through untouched.** Claude Code's own startup
warnings arrive on stderr as plain text and are worth seeing; so is the output
of a run launched with the text format by a ``CLAUDE_ARGS`` that predates this.
Rendering is therefore an enhancement of the read, never a requirement on the
writer, and the reader cannot be made to show nothing by output it did not
expect.

**An unrecognised event type is named, not dropped.** A future Claude Code
event this does not know how to render still produces a line, because a reader
watching for advancing output must not be shown a stall that is really just a
vocabulary gap.

**How far back it reads and how much it returns are two different bounds.** They
used to be one number, and one number cannot do both jobs here: a single
stream-json event is routinely larger than the whole of what a status read
should return, so a window sized to the answer lands mid-event and, after the
half-line at its start is dropped, can return nothing at all — a blank status
in the middle of the busiest part of a run. ``STATUS_READ_BYTES`` is how much
of the file's end is examined, and it is generous. ``STATUS_TAIL_LINES``,
``STATUS_LINE_CHARS`` and ``STATUS_TAIL_BYTES`` bound what comes back, and they
are what keeps a status read from pulling an arbitrary quantity of a host file
through it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

#: How much of the end of a run's log is examined. Sized for the *writer*: a
#: single assistant event carrying a long tool input, or a user event carrying a
#: tool result, is tens of kilobytes, and a window that cannot hold one whole
#: event returns nothing rather than something.
STATUS_READ_BYTES = 262_144

#: How much comes back. Sized for the *reader*: enough to tell model work from
#: tests, git, a closing report or a stall. Whichever bound bites first wins, so
#: a run emitting very long lines is bounded too.
STATUS_TAIL_LINES = 40
STATUS_LINE_CHARS = 200
STATUS_TAIL_BYTES = STATUS_TAIL_LINES * STATUS_LINE_CHARS

#: What a rendered line is called, by the event and content-block type it came
#: from. Named once so the vocabulary a reader learns is in one place, and so a
#: type that is missing from here is visibly missing rather than silently
#: rendered as something else.
LABELS = {
    "text": "assistant",
    "thinking": "thinking",
    "tool_use": "tool",
    "tool_result": "tool result",
}

#: OpenCode event types that are recognised and deliberately render nothing
#: (task 810). ``step_start`` and ``step_finish`` bracket every model turn and
#: carry no account of what the run is doing, so a line each would be two
#: contentless entries per turn — pushing the text, tool calls and errors that
#: a status read exists for out of the far end of a 40-line window.
#:
#: Being listed here is what separates them from an event type this module has
#: never heard of, which is still named rather than dropped: silence is a
#: decision taken about a known event, not a gap.
OPENCODE_SILENT = frozenset({"step_start", "step_finish"})


def _flatten(text: str) -> str:
    """One line, whatever the model wrote.

    ``output_tail`` is a list of lines and each rendered event is one of them;
    a model's paragraph would otherwise become several entries and make one
    event look like a burst of activity.
    """
    return " ".join(str(text).split())


def _shorten(line: str) -> tuple[str, bool]:
    if len(line) <= STATUS_LINE_CHARS:
        return line, False
    return line[: STATUS_LINE_CHARS - 1] + "…", True


def _blocks(record: dict) -> list:
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


def _render_block(block) -> str | None:
    """One content block as one labelled line, or None if it says nothing."""
    if isinstance(block, str):
        return f"{LABELS['text']}: {_flatten(block)}" if block.strip() else None
    if not isinstance(block, dict):
        return None

    kind = block.get("type")
    if kind == "text":
        body = _flatten(block.get("text", ""))
    elif kind == "thinking":
        body = _flatten(block.get("thinking", ""))
    elif kind == "tool_use":
        # The input verbatim rather than a per-tool summary: a table of which
        # field matters for which tool is a second place to keep in step with
        # Claude Code's tools, and the line bound already does the shortening.
        try:
            arguments = json.dumps(block.get("input", {}), ensure_ascii=False)
        except (TypeError, ValueError):
            arguments = str(block.get("input", ""))
        body = _flatten(f"{block.get('name', 'tool')} {arguments}")
    elif kind == "tool_result":
        content = block.get("content")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict)
            )
        body = _flatten(content if content is not None else "")
        label = "tool error" if block.get("is_error") else LABELS["tool_result"]
        return f"{label}: {body}" if body else f"{label}:"
    else:
        return None

    return f"{LABELS[kind]}: {body}" if body else None


def _as_text(value) -> str:
    """Whatever a harness put here, as something a reader can be shown.

    Tool output is a string, a tool error may be a structured object, and the
    point of the line is to say what happened — so a dict is serialised rather
    than dropped for not being a string.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _render_opencode_tool(part: dict) -> list[str]:
    """One completed OpenCode tool call as the two lines Claude Code takes two
    events to say.

    OpenCode reports a tool once, when it finishes, with the call and its result
    in one object; Claude Code reports the call on the assistant turn and the
    result on the next user turn. Rendering the one event as two lines keeps a
    single vocabulary on the read side, so a person watching a status tail does
    not have to know which harness produced it.
    """
    state = part.get("state")
    state = state if isinstance(state, dict) else {}
    try:
        arguments = json.dumps(state.get("input", {}), ensure_ascii=False)
    except (TypeError, ValueError):
        arguments = str(state.get("input", ""))
    lines = [_flatten(f"{LABELS['tool_use']}: {part.get('tool', 'tool')} {arguments}")]

    if state.get("status") == "error":
        body = _flatten(_as_text(state.get("error")))
        lines.append(f"tool error: {body}" if body else "tool error:")
    else:
        body = _flatten(_as_text(state.get("output")))
        label = LABELS["tool_result"]
        lines.append(f"{label}: {body}" if body else f"{label}:")
    return lines


def _render_opencode(record: dict) -> list[str] | None:
    """An ``opencode run --format json`` event, or None if this is not one.

    OpenCode writes the same artifact in the same way — one JSON object per
    line, as the run happens — with a vocabulary of its own, and the two do not
    collide: nothing here is a top-level ``type`` Claude Code emits, and nothing
    Claude Code emits is one of these. So one renderer serves both executors,
    and a reader learns one set of labels (task 810).
    """
    kind = record.get("type")
    if kind in OPENCODE_SILENT:
        return []

    part = record.get("part")
    part = part if isinstance(part, dict) else {}

    if kind == "text":
        body = _flatten(part.get("text", ""))
        return [f"{LABELS['text']}: {body}"] if body else []
    if kind == "reasoning":
        body = _flatten(part.get("text", ""))
        return [f"{LABELS['thinking']}: {body}"] if body else []
    if kind == "tool_use":
        return _render_opencode_tool(part)
    if kind == "error":
        # Shaped the way OpenCode reports it to a person: the nested message
        # when there is one, and the error's name when there is not.
        error = record.get("error")
        error = error if isinstance(error, dict) else {}
        data = error.get("data")
        if isinstance(data, dict) and data.get("message"):
            body = _flatten(_as_text(data["message"]))
        else:
            body = _flatten(_as_text(error.get("name") or record.get("error")))
        return [f"error: {body}" if body else "error:"]
    return None


def render(raw: str) -> list[str]:
    """One line of a run's log as the lines a reader should see.

    Zero lines for an event that carries nothing worth showing, and more than
    one for an assistant turn that both said something and called a tool —
    which is the shape that makes a run legible, so it is not collapsed.
    """
    stripped = raw.strip()
    if not stripped:
        return []
    try:
        record = json.loads(stripped)
    except json.JSONDecodeError:
        # Not stream-json: Claude Code's startup warnings on stderr, or a run
        # launched with the text output format. Shown as written.
        return [raw]
    if not isinstance(record, dict):
        return [raw]

    kind = record.get("type")
    if kind in ("assistant", "user"):
        return [
            line
            for line in (_render_block(block) for block in _blocks(record))
            if line
        ]
    if kind == "system":
        model = record.get("model") or ""
        subtype = record.get("subtype") or "event"
        return [_flatten(f"system: {subtype} {model}")]
    if kind == "result":
        subtype = record.get("subtype") or "done"
        body = _flatten(record.get("result") or "")
        turns = record.get("num_turns")
        head = f"result: {subtype}"
        if turns is not None:
            head = f"{head} after {turns} turns"
        return [f"{head}: {body}" if body else head]

    opencode = _render_opencode(record)
    if opencode is not None:
        return opencode

    if kind:
        # Named rather than dropped: a reader watching output advance must not
        # be shown a stall that is only an event type this does not know.
        return [f"{kind}:"]
    return [raw]


def tail(log_file, redact: Callable[[str], str]) -> dict:
    """The end of a run's output: rendered, bounded, and with secrets removed.

    ``redact`` is applied to the rendered text and *before* the line bound,
    both deliberately. A secret in a run's environment reaches this file inside
    a tool input or a tool result, where it is a fragment of JSON until
    rendering has taken it out; and a cut applied first would leave the leading
    half of a token in a line the equality test no longer matches.
    """
    empty = {"output_tail": [], "output_truncated": False, "output_at": None}
    if not log_file:
        return empty
    path = Path(str(log_file))
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > STATUS_READ_BYTES:
                handle.seek(size - STATUS_READ_BYTES)
            raw = handle.read()
        modified = path.stat().st_mtime
    except OSError:
        return empty

    truncated = size > STATUS_READ_BYTES
    text = raw.decode("utf-8", errors="replace")
    if truncated:
        # A byte seek lands mid-line. Drop that fragment rather than publish it
        # as though the run had written a line beginning there.
        text = text.split("\n", 1)[1] if "\n" in text else ""

    lines: list[str] = []
    for raw_line in text.splitlines():
        lines.extend(render(raw_line))

    if len(lines) > STATUS_TAIL_LINES:
        lines = lines[-STATUS_TAIL_LINES:]
        truncated = True

    shortened = []
    for line in lines:
        line, cut = _shorten(redact(line))
        truncated = truncated or cut
        shortened.append(line)

    # The character budget last, dropping from the front: the end of the output
    # is where a run says what it is doing now.
    while sum(len(line) for line in shortened) > STATUS_TAIL_BYTES:
        shortened.pop(0)
        truncated = True

    return {
        "output_tail": shortened,
        "output_truncated": truncated,
        "output_at": time.strftime(
            "%Y-%m-%dT%H:%M:%S%z", time.localtime(modified)
        ),
    }
