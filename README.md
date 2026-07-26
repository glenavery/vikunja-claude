# vikunja-claude

A small local service that lets you work a Vikunja ticket with Claude Code.

Open a ticket on the board, click a button, and Claude Code starts in
`/home/glen/stacks/investment` with a prompt built from that ticket — scoped to
that ticket only, required to write tests, required to commit, forbidden from
pushing, and told to report back to the board when it stops.

Vikunja itself is not forked, patched or modified. The browser side is a
bookmarklet.

- Python 3.11+, **standard library only**. No pip install, no virtualenv.
- Binds to `127.0.0.1:3460` and refuses to bind anywhere else.
- Runs as a **systemd user service**, not a container: it has to start
  `claude` as a host process.

## Setup

```bash
cd /home/glen/stacks/vikunja-claude
cp .env.example .env
# put your Vikunja API token in .env (Vikunja → Settings → API tokens)
chmod 600 .env
```

The token needs read/write on tasks, projects and comments.

Install the user service:

```bash
mkdir -p ~/.config/systemd/user
ln -sf /home/glen/stacks/vikunja-claude/systemd/vikunja-claude.service \
       ~/.config/systemd/user/vikunja-claude.service
systemctl --user daemon-reload
systemctl --user enable --now vikunja-claude
systemctl --user status vikunja-claude
journalctl --user -u vikunja-claude -f
```

Keep it running when you are not logged in:

```bash
sudo loginctl enable-linger "$USER"
```

Or just run it in a terminal:

```bash
cd /home/glen/stacks/vikunja-claude && python3 -m vikunja_claude
```

### Why not Docker

The service's whole job is to start Claude Code on the host, in a host
repository, with the host's Claude credentials. From inside a container it
could not do that without mounting the docker socket or the user's home and
credentials into it — strictly worse than a user unit. So: systemd user
service, no compose file.

## Use

Open <http://127.0.0.1:3460/>. Enter a ticket number and either preview the
prompt or launch. `Work next Ready ticket` takes the oldest ticket in the
**Ready** bucket.

| Method | Path | Does |
|---|---|---|
| GET | `/` | Console: ticket input, Preview prompt, Work ticket, Work next Ready |
| GET | `/health` | Service + Vikunja reachability (`503` when Vikunja is down) |
| GET | `/ticket/{n}` | Ticket details and the generated prompt. **Never launches.** |
| POST | `/ticket/{n}/work` | Move to In Progress, then launch Claude Code |
| GET | `/next` | The oldest Ready ticket, same view as `/ticket/{n}` |
| POST | `/next/work` | Launch the oldest Ready ticket |
| GET | `/launches` | Recent launch log as JSON |
| GET | `/bookmarklet` | Install page for the browser button |

Any endpoint returns JSON instead of HTML with `Accept: application/json` or
`?format=json`.

```bash
curl -s localhost:3460/health | jq
curl -s -H 'Accept: application/json' localhost:3460/ticket/33 | jq .prompt -r
curl -s -X POST localhost:3460/ticket/33/work | jq
```

Tickets are resolved by the `#NN` prefix of the Vikunja task title
(`#33 Back up Vikunja database` → ticket 33). Two tasks claiming the same
number is a `409`, not a coin flip.

## The browser button

Vikunja is not modified. Open <http://127.0.0.1:3460/bookmarklet> and drag the
link to your bookmarks bar.

**Chrome**

1. `Ctrl+Shift+B` to show the bookmarks bar.
2. Drag the **Work with Claude** link from `/bookmarklet` onto the bar. If
   Chrome blocks the drag, right-click the bar → **Add page…**, name it
   `Work with Claude`, and paste the source from
   `browser/work-with-claude.bookmarklet.js` (the single-line version on the
   install page) as the URL.
3. Open a Vikunja task and click the bookmark. It reads the `#NN` from the task
   title and opens `http://127.0.0.1:3460/ticket/NN`.

The bookmarklet only *opens the preview page*. Launching is still a deliberate
click on that page.

## What Claude is told

`GET /ticket/{n}` shows the exact prompt. It always:

- identifies the ticket (number, task id, Vikunja URL, repository);
- includes the complete ticket description, inside explicit delimiters;
- restricts work to that one ticket;
- requires tests, and forbids weakening existing ones;
- requires a commit referencing `(#NN)`;
- **forbids pushing** — no push, no PR, no remote;
- tells Claude to comment and move the ticket to **Waiting** if blocked, or to
  comment and move it to **Done** if it finished.

## Token handling

The Vikunja token is never in the prompt and never on a command line. The
launcher puts it in the child process's **environment**, and the prompt tells
Claude to update the board through the bundled helper:

```bash
python3 /home/glen/stacks/vikunja-claude/vkctl.py show    33
python3 /home/glen/stacks/vikunja-claude/vkctl.py comment 33 "done: ..."
python3 /home/glen/stacks/vikunja-claude/vkctl.py move    33 Done
```

Moving to `Done` marks the task done — the Done bucket is the project's
configured done bucket.

## Permissions

The default `CLAUDE_ARGS` is `-p --permission-mode acceptEdits`: file edits are
accepted, but **bash commands are denied in headless mode**, so a run cannot
actually execute tests or `git commit`. That default is deliberately the safe
one. To let unattended runs finish, either allowlist the commands you want in
`/home/glen/stacks/investment/.claude/settings.json`, or — understanding what
it means — set:

```env
CLAUDE_ARGS=-p --dangerously-skip-permissions
```

Decide that consciously; the service will not decide it for you.

## Safety properties

- Loopback only; `build_server` refuses any non-loopback bind.
- The only value taken from a request is a ticket number, matched as `\d+` by
  the router. No request value ever reaches a shell; `Popen` is called with an
  argument list and `shell=False`.
- **One run per ticket.** A per-ticket lock file (`O_EXCL`) blocks a second
  launch, and survives a restart of this service. A lock whose PID is dead is
  treated as stale and reclaimed.
- Every launch, failure, timeout and exit is appended as JSON to
  `~/.local/state/vikunja-claude/launches.jsonl`; each run's full output goes to
  `~/.local/state/vikunja-claude/runs/ticket-NN-<timestamp>.log`.

## Layout

```
vikunja_claude/
  config.py     env-driven settings, optional .env
  vikunja.py    Vikunja client, Ticket, #NN lookup
  html_text.py  description HTML → plain text
  prompt.py     the prompt template
  launcher.py   locks, spawn, logging, reaping
  service.py    order of operations: move, then launch
  web.py        HTML console
  server.py     routes, status codes, loopback guard
vkctl.py        board updates for the launched Claude
tests/          lookup, prompt, duplicate launches, API errors
systemd/        user unit
browser/        bookmarklet source
```

## Tests

```bash
cd /home/glen/stacks/vikunja-claude
python3 -m unittest discover -s tests -t .
```

60 tests, no network and no live Vikunja: the API is faked through an
injectable transport, and process spawning through an injectable `spawn`.

Covered: `#NN` parsing and lookup (missing, duplicate, unnumbered, next-Ready
selection), prompt generation (every required clause, and that the token never
appears), duplicate launch prevention (live, stale, cross-instance, per-ticket
isolation), and API error handling (401/500/unreachable/bad bucket, and the
HTTP status each maps to).
