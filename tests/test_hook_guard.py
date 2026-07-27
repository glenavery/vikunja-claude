"""The PreToolUse guard blocks a raw replace and nothing else.

Both halves matter. A guard that misses the destructive call is useless; a guard
that blocks ordinary reads and comments gets disabled within a day, which is the
same thing with extra steps.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "block_raw_task_post.py"

BLOCKED = [
    """curl -X POST -H 'Auth: x' http://127.0.0.1:3456/api/v1/tasks/46 -d '{"done":true}'""",
    # curl POSTs by default once -d is present, with no -X anywhere.
    """curl http://127.0.0.1:3456/api/v1/tasks/46 -d '{"done":true}'""",
    """python3 -c 'call("POST", f"/tasks/{tid}", body)'""",
    """python3 -c 'call("POST", "/tasks/46", {"done": True})'""",
    """curl -X POST "$BASE/tasks/$ID" -d '{"done":true}'""",
    # Stripping heredoc bodies must not become a way to smuggle the real call:
    # what follows the heredoc is still a command.
    """cat > body.html <<'HTML'
<p>ordinary text</p>
HTML
curl -X POST http://127.0.0.1:3456/api/v1/tasks/46 -d '{"done":true}'""",
]

ALLOWED = [
    # Comments are a separate relation: additive, no replace.
    """curl -X PUT http://127.0.0.1:3456/api/v1/tasks/46/comments -d '{"comment":"hi"}'""",
    """python3 -c 'call("PUT", f"/tasks/{tid}/comments", body)'""",
    "curl -H 'Auth: x' http://127.0.0.1:3456/api/v1/tasks/46",
    # Create: PUT on the project, replaces nothing.
    """curl -X PUT http://127.0.0.1:3456/api/v1/projects/2/tasks -d '{"title":"x"}'""",
    "curl -X DELETE http://127.0.0.1:3456/api/v1/tasks/46",
    "python3 vkctl.py close --task 46",
    "git commit -m 'closes tasks/46'",
    "docker exec vikunja-db psql -c 'select * from tasks where id=46'",
    # Writing ABOUT the hazard is not committing it. This exact command was the
    # guard's first real false positive: a closing comment quoting the
    # dangerous call, in a heredoc, blocked from being written.
    """cat > c.html <<'HTML'
<p>POST /tasks/{id} replaces the whole task.</p>
<p>Never <code>curl -X POST .../tasks/46 -d '{"done":true}'</code>.</p>
HTML
python3 vkctl.py close --task 61 --comment-file c.html""",
    # A heredoc that never terminates must not swallow a real command either way
    # -- it is still data as far as the shell is concerned.
    "cat > note.txt <<'EOF'\nsee POST /tasks/46\n",
]


def run(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("command", BLOCKED)
def test_destructive_commands_are_blocked(command):
    result = run(command)
    assert result.returncode == 2, result.stdout
    assert "vkctl" in result.stderr


@pytest.mark.parametrize("command", ALLOWED)
def test_ordinary_commands_pass(command):
    assert run(command).returncode == 0


def test_unparseable_payload_never_blocks():
    result = subprocess.run(
        [sys.executable, str(HOOK)], input="not json", capture_output=True, text=True
    )
    assert result.returncode == 0


def test_missing_command_never_blocks():
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_input": {}}),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
