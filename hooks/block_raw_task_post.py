#!/usr/bin/env python3
"""PreToolUse guard: refuse a raw ``POST /tasks/<id>`` from the shell.

Vikunja's ``POST /tasks/{id}`` is a REPLACE. A body that omits ``description``
blanks it, which has destroyed three ticket descriptions -- twice in sessions
where the hazard was documented and available, because the mistake happens while
thinking about the ticket's content rather than about the API.

The real fix is ``vkctl`` (read-modify-write, checks the description survived).
This hook is the backstop for when that is forgotten. It matches on command
text, so it is a second layer and never the primary defence: it cannot see
inside a script it merely invokes.

Blocks by exiting 2, which returns stderr to the caller as a refusal.

Not blocked, deliberately:
  PUT  /tasks/<id>/comments      comments are a separate relation, additive
  PUT  /projects/<id>/tasks      create, replaces nothing
  GET  anything
"""

from __future__ import annotations

import json
import re
import sys

# A POST aimed at one task. Trailing sub-resources (/comments, /labels...) are
# separate endpoints and are not replaces, so the id must end the path. The id
# may be interpolated -- `/tasks/{task_id}` in inline Python is the same call.
TASK_ENDPOINT = re.compile(r"/tasks/(?:\d+|\{[^}]*\}|\$\w+)(?![\w/])")

POST_FLAG = re.compile(r"(?:-X|--request)\s*=?\s*['\"]?POST['\"]?", re.I)
INLINE_POST = re.compile(r"""['"]POST['"]\s*,\s*f?['"][^'"]*?/tasks/""", re.I)

GUIDANCE = """BLOCKED: raw POST to /tasks/<id>.

Vikunja replaces the whole task on that call -- any field missing from the body
is blanked, and that is how three ticket descriptions have already been lost.

Use vkctl instead (read-modify-write, verifies the description survived):

  cd /home/glen/stacks/vikunja-claude
  python3 vkctl.py close  --task <id> [--comment-file closing.html]
  python3 vkctl.py edit   --task <id> --desc-file body.html
  python3 vkctl.py comment --task <id> "<p>text</p>"

If you genuinely need the raw call, do it from Python via
VikunjaClient.update_task(), which reads the task first and checks afterwards.
"""


def is_dangerous(command: str) -> bool:
    if not TASK_ENDPOINT.search(command):
        return False
    if INLINE_POST.search(command):
        return True
    # curl defaults to POST when -d/--data is present without -X, so a bare
    # `curl .../tasks/46 -d '{"done":true}'` is just as destructive.
    has_data = re.search(r"(?:^|\s)(?:-d|--data(?:-raw|-binary)?)\b", command)
    looks_like_curl = "curl" in command
    if looks_like_curl and (POST_FLAG.search(command) or has_data):
        return not re.search(r"(?:-X|--request)\s*=?\s*['\"]?(?:GET|PUT|DELETE)", command, re.I)
    return bool(POST_FLAG.search(command))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # never block on a payload we cannot parse

    command = (payload.get("tool_input") or {}).get("command") or ""
    if is_dangerous(command):
        print(GUIDANCE, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
