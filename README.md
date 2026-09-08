# vikunja-claude

A small local service that lets you work a Vikunja ticket with Claude Code.

Open a ticket on the board, click a button, and Claude Code starts in
`/home/glen/stacks/investment` with a prompt built from that ticket — scoped to
that ticket only, required to write tests, required to commit, forbidden from
pushing, and told to report back to the board when it stops.

Vikunja itself is not forked, patched or modified. The browser side is a
bookmarklet or userscript that adds a 🤖 button to a task page.

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

Normally you never open this directly — you click the 🤖 button on a Vikunja
task (see below). The console at <http://127.0.0.1:3460/> is the manual
fallback: enter the **board number** — the `#N` printed on the card — to
preview or launch, or use `Work next Ready ticket` for the oldest ticket in the
**Ready** bucket.

Reachable over Tailscale by putting `tailscale serve` in front of it — the
service itself stays bound to loopback, and generated buttons pick up whichever
address you loaded them from. Note that anyone on your tailnet who can reach it
can start a Claude run on this host.

| Method | Path | Does |
|---|---|---|
| GET | `/` | Console: ticket input, Preview prompt, Work ticket, Work next Ready |
| GET | `/health` | Service + Vikunja reachability (`503` when Vikunja is down) |
| GET | `/task/{id}` | Ticket details and the generated prompt. **Never launches.** |
| POST | `/task/{id}/work` | Move to In Progress, then launch the ticket's harness. `?executor=local` runs it on OpenCode against the approved local model |
| GET | `/task/{id}/run` | What the last run for that task is doing: state, liveness, executor, model, and a bounded tail of its output. **Reads only** |
| GET | `/task/{id}/launch` | Landing page for the browser button: launches on load |
| GET | `/ticket/{n}` | Board-number lookup (`#N` on the card); redirects to `/task/{id}` |
| POST | `/ticket/{n}/work` | Same, by board number — the path the launch page renders |
| GET | `/next` | The oldest Ready ticket, same view as `/task/{id}` |
| POST | `/next/work` | Launch the oldest Ready ticket |
| GET | `/launches` | Recent launch log as JSON |
| GET | `/bookmarklet` | Install page for the browser button |
| GET | `/userscript` | Tampermonkey userscript (host-aware) |

Any endpoint returns JSON instead of HTML with `Accept: application/json` or
`?format=json`.

```bash
curl -s localhost:3460/health | jq
curl -s -H 'Accept: application/json' localhost:3460/task/9 | jq .prompt -r
curl -s -X POST localhost:3460/task/9/work | jq
```

## Identity: task id, not `#NN`

**This section is about the launcher and `vkctl.py`. The MCP boundary answers
the question differently — see [A task is named the way the board names
it](#a-task-is-named-the-way-the-board-names-it-task-649).** That is not a
contradiction: the launcher is reached from a `/tasks/<id>` URL a human already
has open, and the MCP is reached from a `#N` a human read off a card.

A ticket is identified here by its **Vikunja task id** — immutable, assigned by
Vikunja, and what `/tasks/<id>` in the browser URL refers to. That is what the
launcher resolves, what the run lock is keyed on, and what `vkctl.py` takes.

There are **three** numbers in play, and only the first two are Vikunja's:

| Number | What it is | Who uses it |
|---|---|---|
| task id | global, immutable, `/tasks/<id>` | the launcher and the run lock — internal, never published (tasks 659, 663) |
| `index` | per project, rendered `#N` on the card | the MCP boundary (`task_number`) |
| `#NN` title prefix | editable text at the front of a title | display, commit references |

The `#NN` prefix is editable, so it is used only for display and for the commit
reference. A task with no `#NN` prefix still works; its commit reference becomes
`(vikunja task <id>)`. It is *not* the same thing as `index`, even though both
render as `#` and a number, and the AI Alpha boards no longer carry one at all.

`/ticket/{n}` remains as a convenience for humans who think in ticket numbers,
and `{n}` is the **board number** — Vikunja's `index`, the same identifier the
MCP boundary takes (task 748). It resolves through the same client call, then
redirects to the canonical `/task/{id}`. Two tasks claiming one board number is
a `409` there, not a coin flip; by task id it is never ambiguous at all.

#### The button addressed one number and the route resolved another (task 748)

Task 659 moved every link this service renders onto the board number —
`_ticket_href`, `_work_path`, the console input. The route they all point at
was not moved with them: `/ticket/{n}` still resolved the `#NN` **title
prefix**, which no AI Alpha board has carried since 2026-07-26. So the 🤖
button rendered `/ticket/714/work` for the task the board shows as #714 and got
`404 No ticket #714 in this project` — a task that plainly exists, reported
absent, by the one path a reader is meant to use.

Two green tests held the two halves apart: one asserted the launch page emits
`fetch('/ticket/8/work')`, another posted `/ticket/33/work` — the legacy
prefix. **Nothing ever posted the path the page actually renders**, so the
resolver behind it was free to answer a different scheme, and did.
`OneIdentityFromTheButtonToTheLaunch` closes that by reading the path out of
the rendered page and following it.

There is deliberately **no fallback** to the prefix when a board number misses.
The two schemes disagree by a few on a real board, so a retry under the other
one would usually find a real, plausible, wrong ticket — the same reasoning as
*never reinterpreted* at the MCP boundary. The prefix lookup survives only
where a human types which scheme they mean: `vkctl.py --ticket`.

### Which number is in the URL

| Vikunja URL | Meaning |
|---|---|
| `/tasks/11` | task 11 — **this** is a task id |
| `/projects/2/11` | project 2, **view** 11 (List/Gantt/Table/Kanban) |

Both are numbers in a URL and they are easy to confuse. The button only ever
reads an id from `/tasks/<id>`; on a board view it refuses rather than guessing,
because acting on a view id would launch the wrong ticket.

## The browser button

The flow is: **open the task in Vikunja → click 🤖 Work with Claude → Claude is
running.** No ticket number typed anywhere. Vikunja is not forked or modified.

Open `/bookmarklet` on the launcher for both options. Everything it generates is
built from the address you loaded it from, so it works over Tailscale as well as
loopback.

**Option 1 — bookmarklet (no extension)**

1. `Ctrl+Shift+B` to show the bookmarks bar.
2. Drag the **🤖 Work with Claude** link from `/bookmarklet` onto the bar. If
   Chrome blocks the drag, right-click the bar → **Add page…**, name it
   `Work with Claude`, and paste the source shown on that page as the URL.
3. Open a Vikunja task and click it.

**Option 2 — userscript (a real button on the page)**

1. Install Tampermonkey in Chrome.
2. Open `/userscript` on the launcher; Tampermonkey offers to install it.
3. Every Vikunja task page now shows a 🤖 button (bottom-right). It tracks
   single-page navigation, and disappears when you leave a task.

Clicking either opens `/task/<id>/launch`, which fires the launch immediately
and shows the outcome — launched, already running, or the error. Navigating
there is cross-origin (always allowed) and the POST it makes is same-origin, so
no CORS configuration is needed anywhere.

The userscript places its button inline in Vikunja's own task action column
(`.task-view .action-buttons`, confirmed against Vikunja's compiled
`TaskDetailView` chunk) and falls back to a floating button if that container
isn't found.

## What Claude is told

`GET /task/{id}` shows the exact prompt. It always:

- identifies the ticket (reference, task id, Vikunja URL, repository);
- includes the complete ticket description, inside explicit delimiters;
- includes **every comment on the ticket**, oldest first, in their own
  delimited block with each author and timestamp, and says that a later comment
  overrides an earlier one and overrides the description (task 818);
- restricts work to that one ticket;
- states the **implementation loop** — one change, validated before the next
  (task 823, below);
- says **when to ask the repository's code graph** rather than grep (task 825,
  below);
- requires tests, and forbids weakening existing ones;
- requires a commit referencing `(#NN)`, or `(vikunja task <id>)` when the
  title carries no `#NN` prefix;
- **forbids pushing** — no push, no PR, no remote;
- tells Claude to comment and move the ticket to **Waiting** whether it
  finished or is blocked — a finished run has committed only to its own
  worktree branch, so its comment names the sha and the merge into `main` that
  is still owed. **Done** is set by whoever merges, after the run has ended.

### One change at a time: the implementation loop (task 823)

Rule 2 of the prompt states a working order, and it is an order rather than a
list of good habits:

1. inspect, and identify the smallest coherent change; 2. apply that one change;
3. syntax- or type-check the files just touched; 4. run the narrowest existing
tests covering them; 5. fix any failure — bad edit, syntax or LSP error, failing
test — before making another change; 6. add regression tests one at a time,
running each as it is added; 7. run the broader suite only once the narrow
checks pass.

**What it came from.** The local Qwen run for #813 finished its implementation
and then lost substantial time to overlapping test edits: malformed text,
conflicting fixtures and helpers that were never defined, none of it caught
until several edits had piled up. Every individual step above is something a run
would say it already does; what it did was validate at the end.

**Narrow, not full.** The loop deliberately does *not* ask for the whole suite
after every edit. Steps 3 and 4 are what must follow each edit; making the
expensive check mandatory is how a run learns to skip the step entirely, which
is the failure this is trying to prevent rather than a stricter version of it.

**It is in the shared prompt, once.** No executor branch, no local-model
variant: `build_prompt` is never told which harness will run, so a per-executor
policy would have to appear as a parameter there first — which
`tests/test_prompt.py` asserts it does not. `claude` and `local` runs get the
identical text, for the same reason task 810 kept everything else harness-neutral.

**On measuring it.** A replay *could* demonstrate fewer repeated corrective
edits — relaunch a ticket of comparable shape under both prompts and count
corrective edits per run (an edit to a file that was edited in the immediately
preceding step, without an intervening validation command) from the run
transcripts, which `run_output.py` already captures. It is deliberately **not** a
prerequisite here, and the honest reason is that the comparison would be weak:
n=1 per arm, a nondeterministic model, and ticket difficulty as an uncontrolled
variable that swamps the effect being measured. Two runs proving nothing would be
worse than none, because the number would then be quoted. What is asserted here
is that the prompt *communicates* the order — the tests pin that, and the order's
usefulness is a claim about the model, which this repository cannot settle from
inside. If a benchmark is wanted later, the arm to build is the corrective-edit
count above, over many tickets, not a single replay of #813.

### Asking the code graph instead of grepping (task 825)

Task 821 connected Graphify to the local executor and proved the tools were
advertised, connected and callable. The restarted #813 run then navigated by
`grep` and `read` anyway — because **a tool a run does not know it should reach
for is not a capability, it is an unused connection**. Nothing in the shared
prompt mentioned the graph or said what it answers better.

Rule 3 says it, in one place, for every harness:

- **Relationship questions go to the graph first** — who calls this, what does
  this depend on, which module owns this behaviour, what else moves if this
  changes — and the run then reads the specific files the graph named.
- **Literal questions stay with text search** — where an exact string, flag or
  error message appears — as does confirming on the spot what the graph pointed
  at. A rule that only promoted the graph would push a run to ask it for an
  exact string, which search does well and a graph does not, and the run would
  conclude the graph is useless.
- **The resolved source is read before any edit.** The graph describes the code;
  only the code is the code, and an edit made on what a stale or partial index
  said is worse than the grepping this replaces.
- **It is conditional at both ends.** "If this repository has a code graph"
  opens it and an explicit search fallback closes it, so a repository without
  one — or a run whose graph tools failed to connect — navigates by search and
  says so once, rather than treating an absent tool as a prerequisite and
  stopping.

It adds no tool, index, executor branch or execution path: the graph is the one
task 821 already exposed, and `tests/test_executors.py` reads the rule out of
the prompt argument of **each spawned argv**, which is where a per-executor
brief would show up if one ever appeared.

### The comments are part of the brief

A relaunch is the normal case, and the reason a ticket is being relaunched is
almost always written in a comment on it. The prompt used to carry the
description alone, so a second run received the ticket as **filed** rather than
as it **stands** — task #813 was relaunched after a review rejected its first
commit, saw no review, re-checked the rejected commit, agreed with itself and
went back to Waiting. Nothing malfunctioned; the question was out of date.

Two rules hold this closed, and both live in `TicketService`:

- **They are read at launch time**, before the ticket is moved and before the
  worktree exists, so a comment added between two runs of one ticket is in the
  second run's prompt. A preview reads them once and renders the same objects
  into both the payload and the prompt it shows beside it, so a human cannot be
  shown one brief while the run gets another.
- **A read that fails refuses the launch.** Incomplete context is not a
  degraded run, it is the defect above, and it is invisible from the outside.
  The refusal happens before anything moves, so it leaves the board alone — the
  same reasoning as resolving the executor first. `build_prompt`'s `comments`
  argument has no default for the same reason: omitting it is a `TypeError`,
  not a silently empty section.

An uncommented ticket gets no block at all and the prompt it has always had.
That is unambiguous rather than blank, because a run only ever sees this prompt
when the comments were read successfully.

## Token handling

The Vikunja token is never in the prompt and never on a command line. The
launcher puts it in the child process's **environment**, and the prompt tells
Claude to update the board through the bundled helper:

```bash
python3 /home/glen/stacks/vikunja-claude/vkctl.py show    --task 11
python3 /home/glen/stacks/vikunja-claude/vkctl.py comment --task 11 "done: ..."
python3 /home/glen/stacks/vikunja-claude/vkctl.py move    --task 11 Done
python3 /home/glen/stacks/vikunja-claude/vkctl.py show    --ticket 35   # by #NN
python3 /home/glen/stacks/vikunja-claude/vkctl.py close   --task 11 --comment-file c.html
python3 /home/glen/stacks/vikunja-claude/vkctl.py create  "Title" --desc-file body.html
python3 /home/glen/stacks/vikunja-claude/vkctl.py edit    --task 11 --desc-file body.html
```

Moving to `Done` marks the task done — the Done bucket is the project's
configured done bucket.

## Never POST a partial task

`POST /tasks/{id}` is a **replace**, not a patch: every field missing from the
body is set to its zero value, so `-d '{"done":true}'` closes the ticket *and
blanks its description*. That has destroyed three descriptions (tasks 5 and 9 on
2026-07-26, task 46 on 2026-07-27), twice in sessions where the hazard was
already documented — the mistake is made while thinking about the ticket's
content, not about the API.

So there is one write path, `VikunjaClient.update_task()`: it reads the whole
task, applies your change, writes the whole task back, and raises
`DescriptionLost` if the description shrank when it should not have. `close`,
`edit` and `create` in `vkctl.py` all go through it. Do not add a second path.

`hooks/block_raw_task_post.py` is a `PreToolUse` backstop that refuses a raw
`POST /tasks/<id>` typed at a shell; it is wired into
`/home/glen/stacks/investment/.claude/settings.json`. It matches on command
text, so it is a second layer and never the primary defence.

If a description is lost anyway, it is recoverable from orphaned TOAST chunks
until vacuum reclaims them — tools and method in
`/home/glen/stacks/vikunja/recovery-tools/`. Act immediately.

## Which harness a run uses, and which model behind it

A run can execute on the approved **local** model instead of the hosted one.

| | |
|---|---|
| `claude` (default) | Claude Code as installed, talking to whatever it normally talks to. |
| `local` | **OpenCode**, driving the approved local coding seat, with command execution enabled inside the ticket's worktree. |

```bash
curl -s -X POST localhost:3460/ticket/33/work?executor=local | jq
RUNNER_EXECUTOR=local          # or make it the default for every run
```

The console and each ticket page carry a **“(local model)”** button beside the
normal one.

**The harness used to be Claude Code either way, and task 810 is why it is not.**
The reasoning for one harness was that everything the runner leans on the
harness for belongs to the harness rather than to the model, so swapping the CLI
would take all of it away. That was sound when it was written, and two of its
three premises have since expired:

- the **worktree and the branch** stopped being the harness's in task 756, when
  the runner started making them itself, before the spawn, for every executor.
- the **commit and the report-back** were never the harness's. They are
  instructions in the prompt and a helper script the child runs, and both are
  harness-neutral text.
- what actually remained was **running commands at all** — and there Claude Code
  headless was the problem rather than the guarantee. See *Permissions* below.

So there is still one launcher, one lock, one log, one prompt and one worktree,
and an executor is now an argv plus extra environment for the child. `claude` is
untouched: this widened the choice, it did not migrate the default.

**`opencode` has to be on the service's PATH, and it is not there by default.**
The user unit runs with `PATH=/home/glen/.local/bin:/usr/local/bin:/usr/bin:/bin`,
which is where `claude` is symlinked from; an npm-global install puts `opencode`
in `~/.npm-global/bin`, which is not on that list. Either symlink it into
`~/.local/bin` beside `claude`, or name it outright:

```env
OPENCODE_BIN=/home/glen/.npm-global/bin/opencode
```

Getting this wrong is a clean refusal rather than a bad run — the launch fails
with `Could not start 'opencode'`, the lock is released and the ticket is left
where it was — but the first local run after deploying is where it shows up.

**Nothing here names a model.** `local` resolves the model from
`deploy/ollama/models.json` in `CLAUDE_WORKDIR` — the investment repository's
tracked record of which local models are approved and what job each holds. The
one entry carrying `"seat": "local_coding"` is the model, and its Modelfile's
`num_ctx` is the context the run is given (currently 262,144; a harness that
does not know a model's window assumes one and compacts to it, so it has to be
told). Approving a different model is an edit there and nothing here.

That record is the **only** copy. OpenCode is configured through
`OPENCODE_CONFIG_CONTENT` in the child's environment, built fresh from the seat
at every launch — so no `opencode.json` is written, and a host whose own
`opencode.json` names something else does not change what a ticket run uses.
`limit` carries a context and an output cap, and they come from different places
on purpose. The context is read from the seat's recipe and is a fact about the
approved model. The output cap is not: no recipe under `deploy/ollama/` sets
`num_predict`, so the seat imposes no output limit of its own — but OpenCode
refuses a config missing the key, so one is stated as harness bookkeeping and
matched to the value the investment repository's own `opencode.json` already
uses for this seat.

**A local run that cannot be configured is refused, never downgraded.** A
missing record, no seat, two seats, or a recipe stating no context all give
`400` and launch nothing. The alternative to a local run is a run against a paid
frontier model, so falling back would answer “run this locally” with a bill.

For the same reason a local run's environment has every paid provider's
credentials removed — `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`,
`GEMINI_API_KEY`, `GOOGLE_GENERATIVE_AI_API_KEY`, `OLLAMA_API_KEY` — whatever the
service inherited. That list grew with the harness: OpenCode discovers providers
from the environment and can address Anthropic, OpenAI, Google, OpenRouter and
Ollama's own hosted tier, so one surviving key is one reachable paid provider.
Pinning `--model` is what chooses the seat; emptying these is what makes the
alternatives unreachable rather than merely unchosen, and the flag is appended
after `OPENCODE_ARGS` so a deployment cannot take it off.

`LOCAL_EXECUTOR_BASE_URL` is **Ollama itself**, on its OpenAI-compatible surface
(`http://127.0.0.1:11434/v1`). It used to be the Anthropic transport shim on
:11440, which exists only because Claude Code speaks the Anthropic message shape
and appends a trailing `role: system` message Ollama refuses. OpenCode speaks
the OpenAI shape, which Ollama serves natively, so the local path no longer goes
through the shim at all. The shim is still deployed and still serves the Claude
Code path; nothing in this package reaches it.

### Navigating the code: the repository's Graphify graph (task 821)

A local run used to answer every structural question — who calls this, what
owns that rule, where is this enforced — with `grep` and `read`, paying for each
one in whole files pulled into the window. The repository already holds the
answer as a graph, and graphify already knows how to serve it, so a local run is
handed that server:

```json
"mcp": {
  "graphify": {
    "type": "local",
    "command": ["<interpreter>", "-m", "graphify.serve", "<CLAUDE_WORKDIR>/graphify-out/graph.json"],
    "enabled": true
  }
}
```

It advertises `query_graph`, `get_node`, `get_neighbors`, `get_community`,
`god_nodes`, `graph_stats` and `shortest_path`. Nothing is mandatory: `grep`,
`read` and `glob` are untouched, and the graph is one more way to look something
up rather than the only one. **`claude` runs are not given this** — they reach
graphify their own way, through the investment repository's `CLAUDE.md` and its
`PreToolUse` hook — and this changes nothing about them.

**Two paths, neither of them a setting.** The graph is
`graphify-out/graph.json` under `CLAUDE_WORKDIR`, and the interpreter comes from
`graphify-out/.graphify_python`, which graphify writes beside the graph naming
the python that can import it (under a venv or a `uv tool` install the system
`python3` cannot). Both are facts about that repository's layout, the way
`deploy/ollama/models.json` is, and a configurable copy of either would only be
a second thing to keep in step.

**The graph is the checkout's, named absolutely, and that is deliberate.** A run
works in the ticket's worktree, where `graphify-out` does not exist — it is
untracked, so a worktree starts without one. A relative path would resolve to
nothing there, and building a graph per worktree would be a second index of the
same repository. The graph's nodes carry repository-relative sources
(`api/db_config.py L50`), so a symbol it resolves is a symbol at that path
inside the worktree.

**The runner states it; no config on disk supplies it.** It travels in
`OPENCODE_CONFIG_CONTENT` with the seat. OpenCode merges the configs it finds —
the checkout's own `opencode.json`, the host's `~/.config/opencode` — and both
are edited by people for their own sessions, so a run that inherited its graph
from one of them would lose it the day somebody tidied up. Merging is by key: a
project or host config naming other MCP servers keeps them, and only `graphify`
is the runner's.

**Nothing here builds or refreshes a graph.** A stale graph is refreshed the way
the humans working the same checkout refresh it, with `graphify update .` in
`CLAUDE_WORKDIR`.

#### Verifying it, and why the check is where it is

```bash
# From the checkout, with the config the runner would build:
OPENCODE_CONFIG_CONTENT="$(…)" opencode mcp list
#   ● ✓ graphify connected
#         /…/.venv/bin/python -m graphify.serve /…/graphify-out/graph.json
```

Measured against OpenCode 1.18.27 and graphify's own server, the two failures
are **not** caught in the same place:

| What is wrong | What OpenCode reports | Where it is caught |
|---|---|---|
| The interpreter cannot import graphify | `✗ graphify failed` — the server exits at once | OpenCode, visibly |
| `graph.json` is not there | `✓ graphify connected`, tools advertised, and a query answers `isError: false` carrying the text *graph.json not found* | **Nowhere** — so the runner checks it |

The second row is the reason a local launch reads both paths before it spawns. A
query that failed and reported success is the one outcome a run must never be
handed: it is indistinguishable, from inside the run, from a repository about
which the graph simply knows nothing.

**So a checkout that cannot serve its graph refuses the launch**, with a message
naming the repair, in the same way and for the same reason a missing seat does:
`400`, nothing spawned, the ticket left where it was. The `claude` executor is
unaffected — a checkout with no graph can still run a ticket, just not a local
one.

## Permissions

The two executors answer this differently, and neither failure mode is a stall —
**in headless mode neither harness ever waits for a person.** That is what makes
both of them quiet, and it is why the launch arguments are asserted in the suite
(`tests/test_executors.py`, `UnattendedExecution`) rather than only described
here.

**`claude`.** The default `CLAUDE_ARGS` is
`-p --verbose --output-format stream-json --permission-mode acceptEdits`: file
edits are accepted, but **bash commands are denied in headless mode**, so a run
cannot actually execute tests, `git commit`, or `vkctl.py`. That default is
deliberately the safe one, and #807 is what it looks like from outside — a run
that reads the ticket, edits code, and is refused everything after that. To let
unattended runs finish, either allowlist the commands you want in
`/home/glen/stacks/investment/.claude/settings.json`, or — understanding what it
means — set:

```env
CLAUDE_ARGS=-p --verbose --output-format stream-json --dangerously-skip-permissions
```

Decide that consciously; the service will not decide it for you.

**`local`.** The default `OPENCODE_ARGS` is `run --format json --auto`, and the
injected config allows `edit`, `bash` and `webfetch`. Both halves are needed and
they are not the same statement: the config settles those three capabilities so
no request is raised at all, and `--auto` answers anything they do not cover.
What `--auto` prevents is not a hang — `opencode run` answers a permission
request itself, allowing it with the flag and **refusing it without**, then
carrying on either way. So leaving the flag off produces a run that looks busy,
ends by itself, and has changed nothing.

**The containment is the worktree, not the permission prompt.** A local run is
unrestricted inside the per-ticket git worktree the runner made for it, which is
its cwd; that is the trade this makes deliberately, and it is why the worktree
became the runner's own property in task 756 rather than something a model chose.
The prompt tells the run to stay on its branch, not to merge, and not to push.

**Observability is the other half of both defaults.** `--output-format
stream-json` and `--format json` are counterparts: one JSON object per line,
written as the run happens. With Claude Code's default `text` format a run
prints once at the end, so `get_task_run_status` can prove a process started and
nothing more — that is what #714 looked like from outside (task 755). Keep the
streaming flag in any `CLAUDE_ARGS` or `OPENCODE_ARGS` you set (`--verbose` too
for Claude Code, which refuses the pair without it). Nothing else reads the run
log, so the format is free to be JSON: `vikunja_claude/run_output.py` renders
both vocabularies back to one set of labels, and passes any line that is not
JSON through untouched.

## Safety properties

- Loopback only; `build_server` refuses any non-loopback bind.
- The only value taken from a request is a ticket number, matched as `\d+` by
  the router. No request value ever reaches a shell; `Popen` is called with an
  argument list and `shell=False`.
- **One run per task.** A lock file keyed on the immutable task id (`O_EXCL`)
  blocks a second launch, and survives a restart of this service. A lock whose
  PID is dead is treated as stale and reclaimed. Keying on the task id means
  renaming a ticket cannot smuggle a second concurrent run past the guard.
- **A run is not recorded as ended until it has ended.** The reaper sends
  SIGTERM to a run that hits the time limit, escalates to SIGKILL after
  `CLAUDE_KILL_GRACE_SECONDS`, and only then writes the ending and releases the
  lock. One that survives both keeps its lock (task 758).
- Every launch, failure, timeout and exit is appended as JSON to
  `~/.local/state/vikunja-claude/launches.jsonl`; each run's full output goes to
  `~/.local/state/vikunja-claude/runs/task-<board number>-<timestamp>.log` —
  the same name as its worktree and its branch (task 762).

## The MCP boundary (ChatGPT)

A second, separate service that lets an outside assistant **read the boards**,
**create a ticket**, **correct a ticket's wording**, **comment on one**, **read
a few operational facts about the AI Server** and **read a public page of its
website** — so project context does not have to be pasted in by hand, and a
ticket dictated in a conversation does not have to be retyped onto the board.

It serves an **approved set of boards**, not every project on the Vikunja:
`AI Alpha Engine` (project 2) and `AI Alpha Trader` (project 3) by default,
named in `VIKUNJA_MCP_PROJECTS`. Every task tool takes an optional
`project_id` naming which one it means, and omitting it means the first —
so a caller written when there was one board keeps getting that board.

It was built for ChatGPT and the heading keeps that name because the published
`service_documentation` metadata links to this anchor, but the boundary is not
ChatGPT-specific: Claude connects to the same endpoint as a custom connector.
Only registration distinguishes them — see *Authentication* below.

It is a different unit on a different port with a different credential, and it
shares nothing with the launcher but the Vikunja settings. Stopping it stops
this integration and nothing else:

```bash
systemctl --user stop vikunja-claude-mcp     # ChatGPT is disconnected
systemctl --user status vikunja-claude       # still running
```

### What it can do

| Tool | Does |
|---|---|
| `get_task(task_number, project_id?)` | Title, full description, status, bucket, labels, timestamps and comments for one task on one approved board |
| `list_open_tasks(bucket?, label?, project_id?)` | Every task that is not done on one approved board — number, title, bucket, priority, labels and timestamps, most urgent first. No descriptions, no comments |
| `search_tasks(text, status?, project_id?)` | Tasks on one approved board whose title or description contains `text`. `status` is `open` (default), `done` or `any` — this is the one read that can see finished tasks, so it is what answers "is there already a ticket about this" |
| `create_task(project_id, title, description)` | Creates one task on the named approved board and returns its number and URL. `project_id` is **required** here |
| `update_task(task_number, title?, description?, approval_token?, project_id?)` | Replaces one existing task's title, description or both. Two calls: the first returns the exact current and proposed values with an approval token and writes nothing; the second must carry that token |
| `add_task_comment(task_number, comment, approval_token?, project_id?)` | Appends one plain-text comment to an existing task, behind the same two-call approval |
| `set_task_status(task_number, bucket, approval_token?, project_id?)` | Moves one existing task to a column, which is also how it is closed and reopened, behind the same two-call approval |
| `list_recently_done(limit?, project_id?)` | The tasks finished most recently on one approved board, newest first |
| `start_task_run(task_number, executor?, approval_token?, project_id?)` | Hands one existing task to the ticket runner, which moves it to In Progress and starts Claude Code on it, behind the same two-call approval. It only *starts* the run |
| `get_task_run_status(task_number, project_id?)` | What the runner's most recent run for that task is doing — `running`, `unkillable`, `finished`, `failed`, `lost`, `reconciled` or `none` — with a bounded tail of its output. One call, no approval: it reads and changes nothing |

#### A task is named the way the board names it (task 649)

**`task_number` is the `#N` shown on the card, and it is the only identifier
these tools take.** Vikunja keeps two numbers per task: `index`, counted per
project and rendered as `#N`, and the global `id` in a `/tasks/<id>` URL. They
disagree — on the AI Alpha Engine board `#647` is task id 648 — and they
disagree *by a few*, so almost every number is valid under both readings.
A connector that took the id while people read the number therefore had no
failure mode that looked like one: ask for "647", get a real ticket, on the
right board, with a plausible title, and comment on the wrong one.

Four rules follow, and `find_by_task_number` is where all four live so the
reads and the two writes cannot drift apart:

- **One resolver.** `get_task`, `update_task` and `add_task_comment` resolve
  through the same call. The two-step approval still binds the *immutable*
  task the number resolved to, because what a token promises is that the
  second call reaches the row the first one read — a claim about the task, not
  about how it was addressed.
- **Never reinterpreted.** A number no task on the named board carries is
  refused, and the refusal does not then try it as a `/tasks/<id>`. That
  fallback would usually succeed, and succeeding is the damage.
- **Never guessed.** Two tasks answering to one number is refused with both
  ids, not resolved to the first.
- **`task_id` is not an argument.** A call carrying one is refused by name
  rather than ignored, so a client on the old contract is told what to send
  instead of being silently right about half the time.

Answers carry `task_number` (and `reference`, its `#N` form) as the identifier.
Listings are ordered on the number for the same reason: an order keyed on a
field the answer does not publish is not an order its reader can read.

### The row id is not published at all (task 660)

Task 649 also returned the immutable id beside every answer, as
`vikunja_task_id` — named so it could not be mistaken for something to call
back with, on the reasoning that debug metadata is harmless.

It was not. **A second number in the answer is a second number a reader can
quote**, and one duly did: a branch and a commit message went out naming
"task 659" for the ticket the board shows as **#658**. Naming a field carefully
is not the same as not publishing it, and an id the tools refuse to *accept* is
an id they have no reason to *hand out*.

So the identity is the board and the number on it — `project_id` +
`task_number`, nothing else. The row id stays internal, where it is
load-bearing: the resolver binds it, the approval token binds it, and the
mutation ledger records it. **A row is not an identity.**

**No carrier is left** (task 663). One was, deliberately: `url` was
`/tasks/<id>`, on the reasoning that Vikunja's only task route is a locator
rather than an identifier. That reasoning did not survive contact — a locator
is precisely what a reader copies. On 2026-08-24 a session read `/tasks/663`
out of a `search_tasks` answer and handed it over as the address of board
**#662**, which is the same confusion, through the one hole left open on the
grounds that nobody would quote it.

The argument against the exemption was already in this repository when it was
granted: `web._ticket_href`, added by the second pass of the very same ticket,
refuses the row id with "a page that links the row id is a page that teaches
the row id". Two surfaces, one ticket, two standards, and the looser one
survived because nothing compared them.

So the id is now absent from every published payload — MCP answers, the
launcher's preview and launch responses, the rendered pages, and the prompt
every run is handed, which printed `Vikunja URL: …/tasks/<id>` two lines above
the rule telling the run never to read a number out of such a URL.
`Ticket.url()` is deleted rather than left unused, so there is no helper to
rebuild it with. The guard in `tests/test_task_identity_exposure.py` asks both
halves: no published **integer** is that task's own row id under any key, and
no published **string** contains its `/tasks/<id>` route. The string half is
what the first pass lacked — `url` was a string, so the integer walk went
straight over the one field that published the id.

The `/tasks/<id>` **routes** are untouched. A browser sitting on Vikunja's own
task page has only that number and the bookmarklet entry point depends on it;
this is about what the services hand out, not about which URLs work.

### Finishing a ticket from the connector (tasks 664, 669)

The MCP could file work and comment on it but not finish it. `update_task`
says so in its own contract — it cannot change status or bucket — and nothing
else covered them, so the only way to close a ticket was shell access to
`vkctl` on this host, and a close could not be undone. A ticket closed as the
last step of finishing it then looked, to someone reading the board, like a
ticket that had never existed. That is how this pair was filed.

**`set_task_status` is one control, over columns.** Vikunja couples `done` to
the board's done-bucket: a task moved into Done is marked done, and one moved
out of it is reopened. That was measured on the live board before the design
was chosen — `vkctl move` out of Done left `done` False, and `vkctl close`
left the task sitting in Done without ever naming the column. Publishing a
bucket knob beside a done knob would let a caller set them against each other,
which is the shape these reports spent a week removing.

**The coupling is verified, not trusted.** Every move is read back through the
board's own view, and two things are checked: that the task is in the column it
was sent to, and that its `done` matches that column. A board with no
configured done-bucket therefore reports "the column was changed, the closed
state was not" instead of returning a success that did half of what it said.
Reaching that check needs a fake that can accept a move and do something else,
which is why `FakeVikunja` grew `couple_done` and `misroute_moves_to` — without
them both guards pass every test while being unable to fail one.

**The three answers are declared, not only returned (task 853).** A tool may
advertise an `outputSchema` beside its `inputSchema`, and a client that reads
one validates `structuredContent` against it — so declaring one is a promise
about every successful answer, not documentation. `set_task_status` declares
one, read off its three returns: the task is already in that column, a preview
that changed nothing, and a completed move. Each is one branch of a `oneOf`,
and each branch names its **whole** key set rather than only what it requires.
A branch listing only its required fields would accept a completed move that
also handed back an `approval_token` — a contradiction rather than a shape, and
the thing a merge bug produces — so closing the set is what makes the two-step
contract legible from the schema alone. A refusal is deliberately not a fourth
branch: a `ToolError` comes back as `isError` with text and **no**
`structuredContent`, so there is nothing there for a schema to describe, and
describing one would advertise a failure shape that is never sent.

The key is **omitted** on the tools that declare no schema, never emptied: an
empty schema promises a shape nobody set. `tests/test_mcp_output_schema.py`
validates real answers — driven through the protocol — against the schema the
same protocol advertised in `tools/list`, so a constant edited without the
method changing fails there. Its checker is the small one in that file rather
than a dependency, because the default run is stdlib-only; it implements
exactly the keywords this schema uses and **raises** on any other, so a
keyword it could not enforce is a failure rather than a line it skips.

**`list_recently_done` is the read half.** `list_open_tasks` never includes a
done task, `get_task` needs a number and `search_tasks` needs text, so nothing
enumerated finished work. It is limited where `list_open_tasks` is not, and the
answer carries `done_total` and `truncated` — "the open queue" is a set with a
boundary, while "what finished recently" is a window onto one that only grows,
and a caller that cannot tell a window from the whole set will read the newest
twenty as all of them.

Comments were never broken. A connector reporting otherwise is holding a tool
list cached from before task 649, when the mutations took `task_id`.

`vkctl.py` came with it. It could only address a task by row id (`--task`) or
by the legacy `#NN` title prefix (`--ticket`), so once the MCP stopped
publishing the id, closing a ticket meant reading one off a `/tasks/<id>` URL —
the exact habit this removes. It takes `--number` now, and the usage text leads
with it. The other two stay, because "I have this task URL open" and "this
board still carries the old prefix" are both real questions; neither is the
ticket's identity.

The board number is per project, so it is only ever resolved together with one:
`#2` is a real task on both boards and a different one on each. That makes the
project boundary part of the identifier rather than a check applied to one.

**`project_id` selects among the approved boards; it never widens the set.**
An id outside it is refused by name rather than narrowed to the default, and
the task must be on the board that was named — ownership is never inferred
from a task number, which means nothing without its project. Widening the
boundary is a configuration change, and each configured id is checked against
the title Vikunja serves for it, so a project renumbered underneath the
configuration is refused instead of read.

Plus three **operational reads**, present only when they are configured (see
[Operational reads](#operational-reads-ai-server-status) below):

| Tool | Does |
|---|---|
| `get_repository_state(repository?)` | Branch, commit, commit subject and whether the named checkout's working tree is clean |
| `get_pipeline_status()` | The latest nightly-pipeline run: status, every stage's own outcome, what S7 did, whether each portfolio got a report |
| `get_system_health()` | Backup age, disks, Docker, scheduled jobs, database and API, with one folded overall status |

Plus three **tracked-content reads**, present whenever the operational reads are
(see [Reading tracked Git content](#reading-tracked-git-content) below):

| Tool | Does |
|---|---|
| `read_repository_file(path, revision?, start_line?, end_line?, repository?)` | One tracked text file at HEAD or at a commit you name |
| `search_repository_text(query, revision?, path_filter?, case_sensitive?, repository?)` | Where a **literal** string appears in tracked files, with paths and line numbers |
| `read_repository_commit_diff(revision, path_filter?, repository?)` | What one commit changed, against its first parent — including commits that were never pushed |

Those four take an optional `repository` — `"ai-alpha-engine"` (the default),
`"trader"` or `"vikunja-claude"`. See
[Which repository is read](#which-repository-is-read).

Plus one **public page fetch**, present only when it is configured (see
[Public page fetch](#public-page-fetch-website-review) below):

| Tool | Does |
|---|---|
| `fetch_public_page(path)` | The HTML, HTTP status and response headers of one page of the public website — `/`, `/about`, `/terms?lang=sv` — fetched as an anonymous visitor |

Plus one **authenticated page read**, present whenever the operational reads are
(see [Reading the site as the test paying user](#reading-the-site-as-the-test-paying-user)
below):

| Tool | Does |
|---|---|
| `fetch_test_paying_page(path)` | The same, for one page rendered as the **test paying user** — `/cockpit/<slug>`, `/report/<slug>` — so paying-tier content can be reviewed. The identity is fixed on the server and is never an argument |

That is the entire surface. The tools are a fixed list in the code, and there is
no generic passthrough — so "this connection cannot close, delete, move, label
or reassign a task" is a property of what exists, not a promise about what will
be asked for. `tests/test_mcp_protocol.py` asserts the tool set as an *exact*
set, which fails the day an unintended one appears.

The two edits were added by task 196, which moved that boundary deliberately;
the tests that used to say "nothing here can edit or comment" were re-anchored
rather than deleted, so they now say which two tools may write and hold those
two to a stricter rule than a name check could.

`search_tasks` matches in Python over the walked board, not through Vikunja's
filter language. A filter is an expression, and building one out of
model-supplied text to look for a *literal* is the wrong shape for the job; the
only filter this boundary ever sends is the constant `done = false`.

### Starting a ticket from the connector (task 726)

`set_task_status` closed the last gap in *bookkeeping*: a connector could file
work, comment on it, move it and finish it. It still could not ask for any of
it to be **worked** — starting a run meant a shell on this host, or the
launcher's own page over the tailnet.

`start_task_run` is that one action, and its whole design is what it does not
own. **The MCP boundary is an invocation surface, not a second runner.** One
request goes to the launcher's existing work route and one answer comes back.
Everything else stays where task 690 put it:

| Owned by the runner | Not here |
|---|---|
| Resolving the task it works, and moving it to In Progress | — |
| The prompt, and everything in it | — |
| Which executors exist, what each one means, and building one | No executor is resolved on this side |
| The model, its context size and the approved-model record | No model id, Ollama setting or context size exists in this package |
| The per-task lock, the worktree, the branch, the tests, the commit, the report | No queue, no scheduler, no second lock |

So **moving the approved `local_coding` seat needs no change here.** The seat
lives in the investment repository's `deploy/ollama/models.json`, the runner
reads it, and the model that ends up driving the run is *read back out of the
runner's answer* rather than named on this side.
`tests/test_mcp_runner.py` asserts that twice — behaviourally, by answering
with a different model and reading it back, and by scanning this side's code
(docstrings and comments removed, because both discuss the seat at length)
for a model id, a runtime setting or a context size.

**Two-step, like every other tool that changes a task that already exists.**
The first call starts nothing: it names the ticket, the column it is in and the
executor asked for, and returns a token for that one launch. Approving `local`
cannot be redeemed for a run on the default executor — the token binds the
executor, not just the task. This is the most consequential action on the
surface, so it is not the one that fires on a single call.

**Refusals are refusals.** A run already in flight, an executor the runner does
not have, a runner that is not running: each comes back as an error saying
nothing was started and the ticket was not moved. There is no fallback in
either direction, and the executor one matters most — the alternative to a
local run is a paid one, so answering "run this locally" by running it
elsewhere would turn a typo into a bill.

### Reading what a run is doing (task 751)

`start_task_run` answers long before the work is done, and the run reports on
the board only at the end. Between the two there was nothing to ask. A run
still thinking, a run in its tests, a run writing its closing report and a run
whose process died an hour ago were **the same thing from outside**: In
Progress, no comment. Task 714 spent eight minutes in that state, and finding
out which one it was meant reading files on the host.

`get_task_run_status` is that read. It reports:

| | |
|---|---|
| `running` | The process is alive and working |
| `unkillable` | It ran past the time limit and would not stop: signalled, escalated to SIGKILL, **still alive**. It is not working and will report nothing |
| `finished` | It exited cleanly — which is not by itself a claim that the ticket was completed; what it did is what it reported on the board |
| `failed` | It exited non-zero, or was stopped for exceeding the runner's time limit |
| `lost` | It started, its process is gone, **the runner never recorded how it ended**, and nothing has been done about that yet |
| `reconciled` | That same run, closed out: the missed ending is recorded and the lock released — and, when the startup pass is what did it, the ticket commented on and moved back to Ready |
| `none` | The runner has no record of a run for that task |

**`lost` is the state worth naming**, and the one task 714 was actually in. It
is what a restart of `vikunja-claude.service` leaves behind: the run dies with
the unit's control group, and the thread that would have recorded its ending
dies with the service. Calling that `finished` would claim an exit nobody
observed; calling it `failed` would claim a failure the runner never saw. It is
neither, and from the board it looks like a ticket sitting In Progress with
nothing reported on it — which is exactly the condition this exists to make
visible.

**`reconciled` is that run once task 754 has closed it out** — and the two are
separate words because they call for different responses (task 757). Neither
reports an outcome: nobody watched either run end, so both have no exit status
and are neither finished nor failed. But `lost` is an *open* condition — the
lock may still be held, nothing has been said on the board, and the next
startup will act on it — while `reconciled` is a *closed* one, where the ending
is on the log, the lock is gone, the ticket has been commented on and moved
back to Ready by the startup pass that closed it, and nothing further will
happen. Reported as one word, a caller
asking about a closed-out run was told the ending was never recorded and the
ticket was probably still In Progress; reconciliation is what recorded that
ending and what moved the ticket. The state is still derived, not stored — the
launch log and the lock are the only artifacts, and the read still writes
nothing.

**`unkillable` is the one that needs a person.** Every other state is
something the runner has already dealt with or will; this one is a process it
has done everything it can to and that is still there. Reporting it as
`running` — which is what it was, before the reaper waited for anything — sends
a reader away to wait for a report that is never coming, from a run holding a
lock nobody can see the reason for. It keeps that lock deliberately, so its
ticket cannot be started again into the worktree it is still sitting in, and
the lock goes the ordinary way once the process finally does: the next read
reclaims it and the run is `reconciled`, with no outcome invented for it.

**Nothing new is recorded to answer any of this.** A launch already writes three
artifacts — the lock naming its process, the append-only launch log, and the
run's own output — and the read puts those three together. The state is decided
on the runner, once, and passed through: a second derivation on the MCP side
would be a second answer to "what is this run doing", able to disagree with the
only place that knows.

**It reads, and there is nothing beside it that controls.** One call, no
approval token — this surface's two-step flow is for tools that change
something. There is deliberately no pause, kill, retry or resume, here or
anywhere on this connection: when a run stops is the runner's to decide.
`GET /task/{id}/run` answers only that verb; the address refuses a POST.

**Bounded and safe.** The output is the *end* of the run's log, capped by lines
and by bytes (whichever bites first, so long lines are bounded too), with a
byte-truncated first line dropped rather than published as a fragment. The
Vikunja token is removed by exact match — the run holds it in its environment,
so a traceback or a verbose HTTP log could otherwise carry it out. The `pid`
and the log's path are withheld for the same reason `start_task_run` withholds
them: a host path is not something this boundary hands out, and neither is a
process to signal. That is also why the output travels as text rather than as
somewhere to go and read it. The path no longer spells the row id (task 762),
which is what it was originally withheld for (task 663) — it is still withheld,
now on the narrower grounds that remain.

**A read that fails says so in its own terms.** An unreachable runner or a task
it does not work comes back as "no run status was read, and nothing was
changed" — never "nothing was started", which would describe a launch nobody
asked for.

### Where a run works (task 756)

**Every run gets its own git worktree, made by the runner before the spawn.**
`.claude/worktrees/task-<board number>` in the configured repository, on branch
`worktree-task-<board number>`, branched from **local `main`** — never
`origin/main`, which is only as fresh as the last fetch and would start runs
from a base that ages silently.

It did not always. `launch` spawned with the repository root as its cwd and
nothing created anything, so a run was isolated only if the launched model chose
to isolate itself. Claude Code driving Opus usually did. The local seat did not:
task 714's run spent an hour and a half editing the main checkout beside a human
editing the same files, and its uncommitted work could only be told apart from
theirs by asking. Task 690 already called this "the existing worktree-based
ticket runner" and `executors.py` still claimed the harness supplied "its
worktree mode, the branch it makes" — both true only by the model's good
manners, which is the one thing an executor is supposed not to change.

**The naming deliberately differs from the lock's, and the difference is the
point.** The lock is keyed by the immutable Vikunja row id, because two runs of
one ticket must never both hold it — a correctness requirement needing a key
that cannot change. A worktree is a directory someone will `cd` into and a
branch someone will merge, so it is named by the **board number**: what the
board shows, what every worktree already in the investment checkout uses, and
the only number a person asking "where did #714's run go" actually has. The row
id appears only for a ticket Vikunja reported no index for, spelled `row-` so
the two numbering spaces can never be read as one.

**One function names all three — the worktree, the branch and the run log**
(`worktree.run_name`, task 762). The log was the last artifact still named from
the row id, so board #714's output went to `task-715-<stamp>.log`: a filename
naming a different, real ticket, one directory away from the `task-714`
worktree the same run was working in. `/launches`, the console and the `work`
response all print that path, which makes it the most quotable form the row id
had left. The launch log keeps the id as a field instead — that is how a
`launched` record is found again from the lock's key once the lock is gone —
and `recent()`, the one place that ledger is published, drops it by name.

Three properties worth keeping:

- **An existing worktree is reused, never recreated.** That is load-bearing
  rather than an optimisation: a run that was orphaned (task 754) or stopped
  leaves partial work there, and recreating would start from a clean tree and
  discard it. Reuse is also what makes a concurrent double request harmless —
  one creates, the other reuses, and the lock decides which run starts.
- **A branch that outlived its worktree is attached to, not branched again.**
  `git worktree remove` keeps the branch; re-branching from `main` would abandon
  its commits where nothing names them.
- **A worktree that cannot be made refuses the launch** — lock released,
  `launch_failed` recorded, no process spawned. There is deliberately no
  fallback to the repository root, because running in the root is the defect
  this exists to stop, and a fallback would reintroduce it at exactly the moment
  nobody is watching.

**The runner does not merge and does not push.** The prompt tells the run which
branch it is on and not to switch branches, merge into `main`, or make another
worktree. Merging stays a human step. Cleanup is not this service's either:
`clean-merged-worktrees` in the investment repository's `deploy/bash_aliases`
already removes worktrees whose branch is merged and whose tree is clean.

Because the prompt names the directory and the directory does not exist until
the launch makes it, `Launcher.launch` takes a **prompt builder** rather than a
prompt. That is why: the path the run is told and the path the child is given
cannot disagree.

### Closing out a run this service lost (task 754)

Task 751 could see the orphaned run; it could not end it. Stopping this service
destroys two things at once: the launched Claude Code process, which sits in the
unit's control group and is the only thing that comments on the board, and the
thread that would have recorded the ending, which lives in the service process.
So the ticket stayed In Progress, the lock kept claiming a dead PID was running,
and the launch log kept an opening record with no closing one.

**Starting is when the last stop gets accounted for.** `build_server` reconciles
before it serves: for every lock whose PID is dead with no ending recorded, it
writes one, releases the lock, comments on the ticket and moves it out of In
Progress. A lock whose process is *alive* is left strictly alone — the point is
to close what ended, never to disturb what is working.

Two things this deliberately does not do:

- **It does not invent an outcome.** The event it writes is `orphaned`, never
  `finished` and never `timeout`. Those are *observed* endings written by the
  thread that watched the process; reusing either would record an exit status or
  a timeout that nothing measured. So a reconciled run still reports no exit
  status and no timeout — what changes is that its ending becomes closed and
  dated (`reconciled_at`), and that the status read says `reconciled` rather
  than `lost` (task 757), instead of an open-ended inference. The comment says
  the same thing: the run is gone, nothing is known about how far it got, treat
  any work as unverified.
- **It does not overrule a human.** The move back to Ready is guarded on the
  column the ticket is in *now*. Someone who already closed it, sent it back or
  picked it up knew more than a startup does.

**The two halves fail differently, on purpose.** The local half — record the
ending, release the lock — always happens, because a Vikunja that is down must
not leave a lock claiming a live run, and must not stop this service coming up.
The board half is best-effort, and each result carries whether it succeeded: a
reconciliation that *silently* failed to report would leave exactly the condition
this exists to end, with nothing left to notice it. A failure is logged as
`orphan_report_failed` beside the reconciliation it belongs to.

**There is no scheduler behind this and there must not be one.** Startup is where
the losses happen. Between startups the same reclaim runs whenever anything reads
a lock — that reclaim always existed, it just used to release in silence — so a
run that dies on its own is recorded the next time the launcher looks at it.

**Why not stop killing the run instead?** Measured, not assumed. `KillMode=process`
does keep the child alive, but leaves it **in the service's own cgroup**: after two
restarts the unit held three generations of orphaned children, all charged to it,
none supervised, none ever cleaned up. A transient `systemd-run --scope` isolates
them properly — but *neither* fixes the reporting, because a new service instance
has no reaper for a process it did not spawn, so the ending still goes unrecorded.
Reconciliation is needed either way, and it also covers a host reboot, an OOM kill
or a plain crash, which no cgroup setting does. Surviving a restart is a separate
policy question, and not obviously the one you want: a restart usually means the
launcher just changed, and an agent continuing against the old prompt while
holding a lock the new instance does not know about is worse than a clean loss
that gets reported.

**The row id addresses the request and appears in nothing that comes back.**
The runner's work route is addressed by Vikunja's global id on purpose: ids are
unique across projects, so a task the runner does not work cannot resolve to a
*different real ticket* the way a board number could. It stays internal, as
everywhere else (task 663) — the answer is projected rather than passed
through, which is why the runner's `log_file` and its `pid` are not in it, and
why the runner's 404 is re-framed here instead of republished: that refusal is
written for someone reading a `/tasks/<id>` URL and spells the id.

**There is no setting that switches it off.** The three optional integrations
are absent when unconfigured, because a tool that can never succeed reads as a
capability. The runner is not one of those: it is the process this system
exists to start, there is one of it, and `runner_url` is resolved from the
launcher's own `VIKUNJA_CLAUDE_HOST` / `VIKUNJA_CLAUDE_PORT` so the two cannot
name different places. A launcher that is down is a refusal the caller reads.

### How long a run may take: as long as it takes (task 817)

**There is no elapsed-time ceiling.** A run ends when its executor exits, or
when something stops it explicitly; the runner has no opinion about how long
that should be, because it has no way to form one — a ticket that needs four
hours of test runs is not thereby unhealthy.

`CLAUDE_LAUNCH_TIMEOUT_SECONDS` used to default to 10,800 seconds, so every run
carried a three-hour ceiling whether or not anyone had asked for one. **Task 813
was killed by that default at the moment it reached final full-suite
validation** — the work was valid and uncommitted, and a run stopped mid-flight
reports nothing, so the ticket was left stranded In Progress. That is the shape
of the cost: the ceiling does its damage at the end of a long run, which is
precisely where the most has been invested and the least is recoverable.

Unset now, and **blank and absent mean the same thing**, so emptying or
commenting out the line is how a ceiling is removed. `None` reaches
`subprocess.wait(timeout=None)` unchanged, where it already means *wait for the
process* — so "no limit" is the absence of a deadline rather than a very large
one. There is no number to be reached, and nothing below runs.

Orphan reconciliation is unaffected and is what still covers the case this used
to be reached for: a run whose process is gone is reclaimed from its lock on the
next read, exactly as before.

### Stopping a run that overstayed, when a ceiling is set (task 758)

Everything in this section applies **only when an operator sets
`CLAUDE_LAUNCH_TIMEOUT_SECONDS`**. It is a capability now, not the lifetime of
every run; setting a number is how you ask for it, and `0` is a ceiling already
spent when the run starts, which is how the tests reach this path without
waiting out a real one.

A run that reaches that ceiling is stopped by the same thread
that has been watching it. What that thread used to do was send SIGTERM to the
run's process group, release the ticket's lock and log `finished` — none of
which waited for anything. **`finished` there meant a signal had been sent**,
not that a process had been reaped.

A child that does not die on SIGTERM is not a hypothetical: Claude Code inside a
long tool call is the realistic case. It kept running in the ticket's worktree,
kept its environment, and kept editing — while the lock was gone, so nothing
recorded that anything was running and the same ticket could be launched
straight back into the directory it was still writing to; the status read said
`failed`, because a timeout implies it; and reconciliation could not help,
because it acts on locks and this run no longer had one. Found by reading, and
unobserved at the time — the three-hour ceiling had not yet been reached by any
run. It was reached later, by task 813, which is what removed the ceiling from
the default path (task 817); this failure mode was already fixed by then.

**Two orderings, and they are the whole of the fix.**

- **The ending is written before the lock goes**, the way reconciliation already
  does it, so a crash between the two leaves a lock the next read reclaims
  rather than a released lock whose ending nothing ever wrote. It was a
  `finally`, which released on every path including the ones that had recorded
  nothing.
- **The lock is not released until the process is dead.** SIGTERM, a grace
  period, SIGKILL, the same grace again — then the ending. Both waits are
  bounded, because waiting for a wedged child to die would hold the reaper
  thread and the lock forever, which is a worse version of the failure being
  fixed.

**What is logged is what was observed.** The `timeout` record carries
`escalated` (SIGKILL was needed) and `terminated` (the process is gone), and it
is written after the attempt rather than before it, because before the attempt
neither is known. The exit status stays `None` throughout: the status `wait()`
hands back after a SIGKILL is the signal the runner sent, not an outcome the run
reached, and a killed run has none.

**A run that survives SIGKILL keeps everything.** No `finished`, no release —
its lock is what makes the condition visible, refuses a second launch into its
worktree, and gets reclaimed in the ordinary way when the process finally goes.
That state is reported as `unkillable`.

`CLAUDE_KILL_GRACE_SECONDS` (default 30) is the grace, per signal. It is not the
time limit and says nothing about how long a run may take.

The tests drive the real path with a real child that really ignores SIGTERM,
because a fake that returns from `wait()` cannot be the thing that was wrong —
`tests/test_launch_timeout.py`.

### Rules the reads hold

`get_task` needs a number you already have. `list_open_tasks` is what answers
"what is open". Both read the board through the same paging, and the only
failure that matters for either is a quiet one, so:

- **No limit argument, and no default page size.** An answer that stopped at
  fifty would be indistinguishable from a board with fifty things left.
- **Paging is walked, then checked.** Vikunja pages tasks *inside* each kanban
  column, caps a page at 50 and **ignores `per_page`** — the live board answers
  a request for 250 with 50 and reports `count: 170`. So one request is the
  first page of each column, not a listing. `_walk_view` walks the pages and
  compares what arrived against the totals Vikunja reports; a short read raises
  instead of returning a shorter board. **There is deliberately no "first page"
  read left in the client** — that one was what made `get_task` deny task 35,
  which exists, while blaming the caller for confusing a task id with a view id.
- **A lookup asks for the one task, and a miss is not an absence.**
  `find_by_task_number` filters the view to `index = N` and `find_by_task_id`
  to `id = N` — the same shape, one per question. Either is constant cost and
  keeps the project boundary structural — it is *this project's* view, so
  another project's task is not in the answer to begin with. (`GET /tasks/{id}`
  would be one request too, but it serves any task in any project and reports
  `bucket_id: 0`, so it can answer neither "is this mine" nor "which column".)
  If the filter yields nothing, the complete walk runs before "no such task" is
  said — and for the number, that walk is also what makes a duplicate visible
  rather than silently taking the first match. Note which failure that guards: a filter the server *ignores* is
  harmless, because the walk then covers the whole board anyway — the harmful
  one is a filter that is applied and matches nothing, whose reply is
  well-formed and consistent and says nothing about the difference between
  "missing" and "unmatched". Both modes have a fake and a test.
- **`done = false` is sent to save a walk, never to decide the answer.** Every
  row is checked again client-side, so a Vikunja that ignored the filter would
  be slower and not wronger.
- **A mistyped column is refused, not answered.** "No open tickets in Redy"
  would be a wrong answer to a mistyped question, so an unknown bucket comes
  back as an error naming the real ones. An *empty* column is a real, empty
  answer. An unused label is likewise a real observation, and the labels
  actually in use come back with it.
- **The order is total.** Most urgent first, then by the board number the
  answer publishes — priority alone is not an order, since most of the board
  sits at 0. A task Vikunja gave no number sorts last, on its id, which keeps
  the key total without inventing one.

It also has no shell, no database connection and no filesystem access beyond
its own ledger, because nothing in `mcp_service.py` has any of those.

### Rules the write path holds

- **An approved project, named not defaulted.** `project_id` is required on
  the create — the one tool where it is — and a value outside the approved set
  is refused outright. It is never silently redirected, and never falls back
  to the default board: creating the ticket in the wrong place is the failure
  this exists to prevent, and unlike a read it cannot be taken back.
- **A retry is not a second ticket.** Identity is the content: the same
  project, title and description returns the first task and reports
  `created: false`. The ledger is on disk, so a restart does not reopen the
  door. Deliberately not caller-supplied — a client retrying a tool call
  resends the same arguments, but nothing obliges it to resend the same id.
- **Descriptions are text, and escaped.** Vikunja stores editor HTML; the tool
  takes plain text with blank lines between paragraphs and escapes everything,
  so the stored description reads back as the description that was approved
  (`test_the_stored_description_is_the_one_that_was_asked_for`), and a
  description containing `<script>` is content rather than markup.
- **Failures are explicit.** An unresolvable project, an unreachable Vikunja, a
  blank title, a rejected create — each comes back as a visible error saying
  nothing was created. There is no partial or best-effort path.
- **Every creation is recorded** in `~/.local/state/vikunja-claude/mcp_created_tasks.jsonl`
  and in the journal. The audit record and the duplicate check are the same
  file, so a creation cannot be deduplicated without also being logged.

**What is not enforced here:** that the user explicitly asked for the ticket.
No server can see the conversation. What the server does is require the exact
title and description as arguments, and declare `create_task` as a non-read-only
tool so the client asks for confirmation and shows those arguments first. The
tool description states the requirement in the same words. Keep ChatGPT's
confirmation prompt on for this connector.

### Rules the two edits hold (task 196)

`update_task` and `add_task_comment` are the only tools that can change
something that already exists. Everything above still applies to them; these are
the rules they add.

- **Two calls, and the first one writes nothing.** A call without
  `approval_token` reads the task and returns the exact current value beside the
  exact proposed one — plus a token naming that one change. Only a second call
  carrying that token writes. So the before-and-after text has to pass through
  the conversation, where the user can see it, before anything can be written.
- **An approval is for one exact change.** The token binds the task, the kind of
  change, the submitted text *and* the value the task held when it was issued.
  Different text, a different task, a second field added afterwards, or a task
  somebody edited in between — each is refused with the reason, and nothing is
  written. It is single use, and a mismatch spends it too: an approval that no
  longer describes the change is not one to retry with.
- **This is not proof that a human said yes, and is not sold as one.** No server
  can see the conversation. What it proves is that no edit happens without a
  prior round trip that put the before and after in front of the client, and
  that what lands is byte-for-byte what that round trip described. The approval
  table is in memory and dies with the process, because an approval describes a
  task as it was moments ago.
- **Whole values only.** There is no partial replacement and no find-and-replace:
  the caller submits the complete new title or description, which is the same
  text the user is shown, so what was approved and what is stored cannot drift.
- **Two fields, in one write.** Title and description, and nothing else — no
  status, bucket, label, assignee, priority, due date or deletion, and
  `additionalProperties: false` means an unlisted field cannot be smuggled into
  a whole-task replace. Both fields go in one `set_task_fields` call, built on
  the client's single read-modify-write path, so the task never sits with a new
  title and an old description.
- **The task is found through the named board's view**, exactly as the reads
  are, so a task on any other board — approved or not — is not in the answer to
  begin with. The refusal does not depend on comparing a project id Vikunja
  reported, it cannot be bought with a valid approval for a different task, and
  the board is re-resolved inside the lock on the second call, so an approval
  cannot be redeemed against a task on a different board either.
- **Idempotent, in the way each one can be.** An update to the value a task
  already holds changes nothing and says so — which is also what a repeat of an
  applied change lands on. A comment identical to one already on the task
  returns that comment instead of writing a second copy; that is read from the
  board rather than from a ledger, so it survives a restart and also catches a
  comment somebody else left.
- **Every change is recorded**, with the value it replaced, in
  `~/.local/state/vikunja-claude/mcp_task_mutations.jsonl` and in the journal.
  Once Vikunja has taken the write that record is the only copy of the old text
  left, which is the point — this repo has lost three ticket descriptions to
  `POST /tasks/{id}` already.

### Authentication: OAuth, and only OAuth

ChatGPT's connector dialog authenticates with OAuth, so this service is its own
OAuth 2.1 authorization server as well as the MCP resource server. It implements
the subset the MCP authorization specification requires and nothing more.

| Endpoint | Is |
|---|---|
| `/.well-known/oauth-protected-resource` | RFC 9728 — names the resource and who issues tokens for it. Also served at `…/mcp` |
| `/.well-known/oauth-authorization-server` | RFC 8414 — the endpoints and grants that exist. Also served at `…/mcp` |
| `/oauth/register` | RFC 7591 dynamic client registration, restricted to the redirect URIs this deployment admits |
| `/oauth/authorize` | The consent screen. States the two grants, asks for the operator passphrase |
| `/oauth/token` | Authorization code (PKCE `S256` required) and refresh. No other grant exists |

What holds:

- **PKCE is required**, `S256` only. No `code_challenge`, no code.
- **Redirect URIs match exactly, everywhere it matters.** From `/oauth/authorize`
  onward the only question ever asked is whether the URI *is* one this client
  registered — never a prefix, a pattern or the shape that admitted it.
- **Registration is the one gate that is not exact equality**, and it allows one
  extra shape. ChatGPT now mints a callback per connector —
  `https://chatgpt.com/connector/oauth/<connector-id>` — so the address does not
  exist until the connector does and cannot be named in configuration ahead of
  time. `/oauth/register` recognises that form: `https` only, host exactly
  `chatgpt.com`, no userinfo, no port, exactly one more path segment of
  unreserved characters, no query and no fragment, and the value must equal the
  canonical URI rebuilt from those parts — so a stray `?`, an uppercase host or
  a `%2F` is a difference from the reconstruction and is refused without having
  to be enumerated. Subdomains, lookalike hosts, `http`, other `chatgpt.com`
  paths, empty identifiers and dot segments are all refused. The **complete
  submitted URI** is then stored on the client, and a second connector's
  callback — a perfectly valid shape in its own right — is refused for the first
  connector's client. The configured list (below) is still matched exactly.
- **Every other client has to be named in configuration**, because that shape is
  ChatGPT's and nothing else is admitted by form. Claude's connector registers
  `https://claude.ai/api/mcp/auth_callback` (measured 2026-08-05 against the live
  deployment; the `claude.com` spelling registers happily if listed, but is not
  what claude.ai sends), so it works only once that URI is in
  `VIKUNJA_MCP_OAUTH_REDIRECT_URIS`. Setting that variable **replaces** the
  default rather than adding to it, so ChatGPT's fixed callback has to be listed
  alongside anything new or that connector silently stops being able to register.
- **A refusal here reaches the operator as whatever the client decides to say.**
  claude.ai reports only "Couldn't register with … sign-in service" and an
  opaque reference id; the access log records `POST /oauth/register 400` and no
  body. The actual reason — `invalid_redirect_uri`, and the permitted list — is
  in the response body, so reproduce the registration with `curl` rather than
  reading the log and guessing.
- **The client store is capped, and the cap may only spend clients nobody
  authorized** (task 840). `/oauth/register` is reachable from the internet, so
  the file cannot grow without bound — but a connector registers when it is
  *created*, not when it is used, so its record is among the oldest in the file
  for as long as it keeps working, and under eviction by age alone it is always
  the first to go. A client the operator carried through the consent screen is
  therefore never evicted: it is marked when its code is issued, which is
  downstream of the passphrase, and a client the file still holds a code or
  token for counts as marked whether or not it says so — and is then *written*
  as marked, in the next write to the store, so the half of the evidence that
  expires is never the only half that remembers. That reading happens before
  the lapsed grants are pruned, because nothing runs between writes: a token is
  not noticed expiring at its expiry, it is found expired by whatever touches
  the file next, and pruning first would discard in one pass the evidence that
  same pass exists to record. If the cap is reached
  with nothing unauthorized to remove, the registration is refused — `503`,
  `temporarily_unavailable` — rather than a working connector being taken out
  to make room for an unknown one.
- **One connector holds one registration, however often it is re-added.**
  Re-adding a connector registers it afresh, and the record it was using before
  is then unreachable — the only copy of that id was the one it just replaced.
  Left in the file those pile up: the live store reached its cap of twenty
  holding nine Qwen Code records against a single live grant, three for
  OpenCode and two duplicate ChatGPT verifications, which is how a cap sized
  for connectors ran out on connection attempts. So authorizing a client
  retires the earlier registrations carrying the same identity, and their
  grants go with them — a token outliving its client record reads as an
  authorization to everything that looks, and holds a slot open for a
  connection nobody can make. Identity is what the connector states about
  itself, its `client_name` and its `redirect_uris`, because being issued a new
  `client_id` is what re-registering *is*.
- **The ChatGPT connector door is one slot, and only one client is ever in
  it.** Every other redirect URI is admitted by exact equality against
  `VIKUNJA_MCP_OAUTH_REDIRECT_URIS`, so each one names a connector the operator
  wrote down. ChatGPT's cannot be written down — the per-connector path does
  not exist until the connector does — so that door admits a *shape*,
  `https://chatgpt.com/connector/oauth/<identifier>`, and a shape is not a
  whitelist entry: anyone can mint callbacks through it without limit. So every
  client holding a connector-shaped callback shares one identity, whatever
  `client_name` it supplies, and the rules above then hold the door to a single
  record. Registering an invented callback displaces only what nobody
  authorized, so a stranger's twenty-five attempts leave one junk record and
  never touch the connected one; replacing the connected one still takes the
  consent screen.
- **Replacement happens at the consent screen, and only there.** Two reasons,
  and the second is the one with teeth. Until the new connection exists the old
  one is still the working one — OpenCode registered a third time while holding
  a refresh token good for another month — so a connector that asks for an id
  and never returns with it has replaced nothing. And an identity is a thing a
  stranger can *state*: `/oauth/register` takes no passphrase, and `Qwen Code`
  at `http://localhost:7777/oauth/callback` is a guess rather than a
  credential. A same-identity eviction at registration would therefore hand an
  unauthenticated caller the one power the cap exists to deny, which is the
  rule above defeated by asking for it in the right words. So a full store
  still refuses a registration that names a client already in it — `503` — and
  the consent screen stays the first point at which anything has proved which
  connector is speaking. The two rules are complementary rather than
  overlapping: replacement removes the way the file filled in practice, and the
  cap governs what happens when it fills anyway.
- **A connector whose row is gone is taken back at the consent screen**, not
  told to register first. The configured redirect URIs say which connections
  this server has; the file says which ones hold grants. So a `client_id`
  presented with an admitted callback names a connector this server knows,
  whatever became of its row — and refusing it was a dead end, because the
  connector cannot register again: its dialog did that once, when it was
  created, and every attempt since replays the id it was given. Re-admission
  decides nothing about who is asking and grants nothing: the record is built
  in memory for the request to be checked against, and is written only where
  the code is issued, which is past the passphrase. A wrong guess therefore
  leaves the file byte-for-byte as it was, and asking at all writes nothing —
  a read that repaired what it read would be an open endpoint writing to the
  store under another name. A callback that is *not* admitted is still
  refused: this rests on the whitelist rather than replacing it.
- **A registration that was never authorized expires after an hour**
  (`PENDING_CLIENT_TTL_SECONDS`). Adding a connector is a registration and a
  consent, and nothing reports the half that did not happen — an abandoned
  dialog, a passphrase given up on and a stranger's POST all look identical
  from here, which is to say they look like nothing at all. So the record
  carries a deadline instead of waiting for a signal that never comes, and the
  file empties itself rather than filling until the cap has to make a decision.
  A rejected passphrase is deliberately *not* that failure: the consent page
  comes back for another go, and the record has to still be there for the retry
  to have something to authorize. What lifts the deadline is being authorized,
  which is permanent — `authorized_at` outlives every token it led to, so a
  connector left alone for longer than its refresh token lives still finds its
  record where it left it.
- **A code is single use**, lives 60 seconds, and is bound to the client, the
  redirect URI, the challenge and the resource. Redeeming one twice fails *and*
  revokes every token the first redemption issued — a replayed code means it
  leaked, and the tokens are what it leaked for. A *wrong* verifier spends the
  code too: whoever holds a stolen one gets a single attempt.
- **Access tokens live an hour** and are bound to this server as their audience;
  refresh tokens rotate on every use, so a stolen one is good for one call.
- **There is no password grant, no client-credentials grant and no implicit
  flow.** Nothing may skip the consent screen: approval is a person.
- **No fallback.** A request to `/mcp` without a valid access token gets `401`
  and nothing else — not a narrower surface, not an anonymous one, and not the
  static bearer token this replaced, which no code here can accept any more.
  Incomplete configuration is a service that refuses to start.

The authorization is still one operator and one grant. There are no accounts, no
roles, no sign-up, and one scope (`vikunja:tickets`) that means the same two
tools. Approving cannot widen it, because there is nothing wider to grant.

### Setup

**1. Generate the credential.** The passphrase is what proves an authorization
request is yours; it is the thing between the public internet and a write path
into the board.

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Put it in `.env` (`chmod 600 .env`), along with the public URL the server is
published on:

```env
VIKUNJA_MCP_OAUTH_ISSUER=https://aiserver.tail36601d.ts.net:8443
VIKUNJA_MCP_OAUTH_PASSPHRASE=<the value generated above>
```

| Variable | Meaning |
|---|---|
| `VIKUNJA_MCP_OAUTH_ISSUER` | **Required.** Public HTTPS base URL. Every metadata document is published under it and tokens are bound to `<issuer>/mcp`. Must be `https` unless it is loopback |
| `VIKUNJA_MCP_OAUTH_PASSPHRASE` | **Required.** At least 32 characters. Typed at the consent screen; five wrong answers lock it for five minutes |
| `VIKUNJA_MCP_OAUTH_REDIRECT_URIS` | Optional. Space- or comma-separated exact URIs. Defaults to `https://chatgpt.com/connector_platform_oauth_redirect`, and **setting it replaces that default** — list the ChatGPT callback again alongside whatever you add. ChatGPT's per-connector callback is admitted by shape as well and needs no entry here; every other client does need one, e.g. `https://claude.ai/api/mcp/auth_callback` for Claude |
| `VIKUNJA_MCP_OAUTH_CLIENT_ID` / `_SECRET` | Optional. A pre-registered client, for a ChatGPT dialog that insists on a client id instead of registering one itself |
| `VIKUNJA_MCP_PROJECTS` | Optional. The boards this connection may reach, as `id:title` entries, comma separated — `2:AI Alpha Engine, 3:AI Alpha Trader` is the default. The first is the board a call that names none is answered from. Id **and** title, because each checks the other: the id is what a caller names, and the title is compared against what Vikunja serves for that id, so a renumbered project is refused rather than read. This is the MCP's own setting — the launcher's `VIKUNJA_PROJECT` still names the one board it works |

The server refuses to start if the issuer or the passphrase is missing, blank,
short, or not a publishable URL. There is no unauthenticated mode and no
environment variable that adds one; `tests/test_mcp_config.py` fails if one is
ever introduced.

**2. Start the unit.**

```bash
ln -sf /home/glen/stacks/vikunja-claude/systemd/vikunja-claude-mcp.service \
       ~/.config/systemd/user/vikunja-claude-mcp.service
systemctl --user daemon-reload
systemctl --user enable --now vikunja-claude-mcp
systemctl --user status vikunja-claude-mcp
curl -s localhost:3461/health | jq
```

The unit is hardened, but it is a *user* unit: `PrivateDevices` and
`ProtectKernelModules` need privileges the user manager does not have and fail
the service with `218/CAPABILITIES` before Python starts, so they are absent by
decision. Check any addition with
`systemd-run --user -p <Directive>=true --wait /bin/true` first — a rejected
directive kills the unit rather than being ignored.

**3. Publish it.** ChatGPT reaches connectors from OpenAI's servers, so a
tailnet-only address is not enough — it needs a public HTTPS URL. The server
itself stays bound to loopback and *refuses* to bind anywhere else; Funnel
terminates TLS in front of it.

```bash
tailscale funnel --bg --https 8443 http://127.0.0.1:3461
tailscale funnel status
```

**This is the one step that puts something of yours on the public internet.**
The Funnel hostname and port must match `VIKUNJA_MCP_OAUTH_ISSUER` exactly, or
the metadata documents will advertise endpoints that are not there. Skip this
step entirely if ChatGPT is not actually being connected — everything else works
over the tailnet without it.

Confirm the two documents ChatGPT reads first are reachable from outside:

```bash
curl -s https://aiserver.tail36601d.ts.net:8443/.well-known/oauth-protected-resource | jq
curl -s https://aiserver.tail36601d.ts.net:8443/.well-known/oauth-authorization-server | jq
```

**4. Add the connector.** ChatGPT → Settings → Connectors → enable Developer
mode → Create. URL is `https://<host>.ts.net:8443/mcp`, authentication is
**OAuth**. Leave the client id and secret blank unless the dialog insists — the
server registers ChatGPT dynamically. If it does insist, generate a client id,
put it in `VIKUNJA_MCP_OAUTH_CLIENT_ID`, restart, and paste the same value.

ChatGPT opens the consent screen in a browser. It names the three operations and
asks for the passphrase; approving returns a code and the connector finishes.
**Never choose "no authentication"** — an open write path into the board is
worse than pasting tickets by hand.

**Claude** connects the same way: claude.ai → Settings → Connectors → Add custom
connector, the same `<issuer>/mcp` URL, client id and secret left blank. The one
prerequisite is configuration rather than anything in the dialog — its callback
must already be in `VIKUNJA_MCP_OAUTH_REDIRECT_URIS`, because registration
happens before the consent screen and fails without ever showing it. Then the
same passphrase gates approval. A completed flow leaves a client named `Claude`
in the OAuth state file holding an access and a refresh token, which is how you
confirm it from this side; the file also names the redirect URI that was
actually used, so an unused spelling can be dropped from the list afterwards.

Verify the boundary from the command line first, which is faster than debugging
in a chat window. An unauthenticated call must be refused, and must say where a
token comes from:

```bash
curl -si localhost:3461/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head -3
# HTTP/1.0 401 Unauthorized
# WWW-Authenticate: Bearer realm="vikunja-claude-mcp", resource_metadata="…", scope="vikunja:tickets"
```

Then, with an access token from a completed flow (ChatGPT's, or one obtained by
hand through the same endpoints):

```bash
curl -s localhost:3461/mcp -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools[].name'

curl -s localhost:3461/mcp -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_task","arguments":{"task_number":138}}}' \
  | jq -r '.result.structuredContent.title'
```

### Rotating and revoking

Live grants — registered clients, unredeemed codes, access and refresh tokens —
are one JSON file, `~/.local/state/vikunja-claude/mcp_oauth.json`, mode `0600`.
It holds SHA-256 digests, not tokens, so a copy of it is not a way in.

**Revoke everything, now.** Deleting the file invalidates every live token
immediately; no restart is needed, and the next request is a `401`:

```bash
rm ~/.local/state/vikunja-claude/mcp_oauth.json
```

**Rotate the passphrase** (stops new authorizations; existing tokens keep
working until they expire, so pair it with the delete above):

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # new value
${EDITOR:-vim} /home/glen/stacks/vikunja-claude/.env            # replace VIKUNJA_MCP_OAUTH_PASSPHRASE
systemctl --user restart vikunja-claude-mcp
```

Revoking the connection itself, in increasing order of severity:

```bash
systemctl --user stop vikunja-claude-mcp     # disconnect now, keep the config
tailscale funnel --https 8443 off            # off the public internet, keep the service
systemctl --user disable --now vikunja-claude-mcp   # and do not come back after a reboot
```

Blanking `VIKUNJA_MCP_OAUTH_PASSPHRASE` is *not* a revocation step — it stops
the service from starting, which is a failure to boot rather than a closed door,
and it leaves issued tokens alone. Delete the state file instead.

The Vikunja API token is separate and unaffected: the MCP server uses the same
one as the launcher, so revoking *that* (Vikunja → Settings → API tokens) cuts
off both services and every ticket the board has open.

#### Recovering a connector whose `client_id` the server no longer knows

**This recovers itself now, and the rest of this section is background.** A
client_id presented alongside a redirect URI this deployment admits is taken
back on the spot: the consent screen appears, the passphrase is asked for as
usual, and issuing the code writes the row again and marks it authorized. The
configured redirect URIs are what say which connections this server has; the
state file is only where the grants live, and a `client_id` is a name this
server handed out rather than a secret it keeps. Nothing is written before the
passphrase, so an unknown id at an admitted callback costs a consent, not a
connector — and an id at a callback that is *not* admitted is still refused,
because re-admission rests on the whitelist rather than replacing it.

That matters because the connector has no other move. Its dialog registers
once, when it is created, and every attempt afterwards replays the id it was
given, so being refused was a dead end that only hand-editing
`mcp_oauth.json` could open — which happened twice, on 2026-09-03 and
2026-09-07, and is what "if the fix was sound we wouldn't have to restore
stuff" is about.

The symptom was `/oauth/authorize` answering the connector's own id with
**"Unknown client_id."** The client record is gone from the state file while
ChatGPT still presents the id it was issued — either because the file was
deleted, or, before task 840, because the cap evicted it. An access token
already issued keeps working until it expires, so it showed up as a
re-authorization that failed rather than as the connector going dark.

Two things still worth knowing:

- **Re-add the connector in ChatGPT.** It registers again on creation, gets a
  fresh `client_id` and a fresh per-connector callback, and the authorization
  that follows marks it as one that must not be evicted — and retires the
  record it replaced, so coming back this way costs the file nothing. Nothing
  on the server needs changing.
- **Pin the id instead**, if the same `client_id` has to keep working — a
  configured client is not in the state file at all, so nothing can evict it.
  Both variables are needed: the id ChatGPT presents, and *that connector's*
  callback, because a configured client's redirect URIs are matched exactly and
  setting the list replaces the default rather than adding to it.

  ```bash
  ${EDITOR:-vim} /home/glen/stacks/vikunja-claude/.env
  #   VIKUNJA_MCP_OAUTH_CLIENT_ID=cid_…                       # what ChatGPT presents
  #   VIKUNJA_MCP_OAUTH_REDIRECT_URIS=https://chatgpt.com/connector/oauth/<connector-id> \
  #                                   https://chatgpt.com/connector_platform_oauth_redirect
  systemctl --user restart vikunja-claude-mcp
  ```

  The connector id cannot be derived: it is only ever visible in the failing
  authorization request itself. It is in the access log, but **URL-encoded** —
  `redirect_uri=https%3A%2F%2Fchatgpt.com%2Fconnector%2Foauth%2F<id>` — so a
  grep for the readable form finds nothing and the value looks lost when it is
  not:

  ```bash
  journalctl --user -u vikunja-claude-mcp --since -30d \
    | grep -o "client_id=[^&]*&redirect_uri=[^&]*" | sort -u
  ```

### Operational reads (AI Server status)

Three read-only facts about the AI Server itself: what code is deployed, what
last night's pipeline did, and whether the machine is healthy. They exist so a
conversation can start from the real state instead of from a paste.

**This boundary computes none of them.** It runs no commands and touches no
database. It issues `GET` to three fixed paths on the investment application,
which owns all three facts already and decides *there* what an external
integration may see (`api/operational_reads.py` in that repository). There is no
path argument anywhere between a tool and the request, so the client cannot be
aimed at any other endpoint of that API — `tests/test_mcp_operational.py` asserts
the client's public surface is exactly those three methods and that it has no
write method at all.

What is deliberately **not** returned, even though the health check computes it:
`journalctl` output, and raw crontab lines. Both can carry environment variables
and secrets. The projection on the investment side is a named allow-list rather
than a passthrough, so a field added to a health check upstream is not published
here until somebody publishes it.

Two other things it will not do. It will not report a state it failed to read as
a healthy one — an unreachable API, a rejected key and a 404 each come back as an
error saying *nothing was read*. And it repairs nothing: the admin System page
fails a timed-out pipeline run as a side effect of being loaded, and this read
reports `running_past_timeout` instead, because a read that mutates what it reads
is a write path wearing a read's name.

#### Enabling them

Both settings or neither. Unset, the three tools — and the authenticated page
read that shares these settings — are **not advertised at all** —
a tool that can never succeed is read by a model as a capability, and its failure
reported as a fact about the system rather than about the configuration.
Half-configured is a refusal to start, so "off" has exactly one meaning.

```bash
${EDITOR:-vim} /home/glen/stacks/vikunja-claude/.env
#   INVESTMENT_API_URL=http://100.105.117.8:8002      # the ADMIN instance
#   INVESTMENT_API_KEY=<the investment API_KEY>
systemctl --user restart vikunja-claude-mcp
systemctl --user status vikunja-claude-mcp | grep operational
#   … operational reads via http://100.105.117.8:8002
```

The URL must be the **admin** instance (`INSTANCE_MODE=admin`, Tailscale
`:8002`). These paths do not exist on the public instance at all — no route is
registered, so they `404` there rather than `401`, with or without a valid key.
Pointing at `:8001` gets a 404 whose message says so.

Plain `http` is accepted only to loopback or a Tailscale address, because the
tailnet is the encryption; anywhere else it would put `INVESTMENT_API_KEY` on the
wire in clear text and configuration refuses to load.

#### Turning them off

```bash
${EDITOR:-vim} /home/glen/stacks/vikunja-claude/.env   # remove BOTH settings
systemctl --user restart vikunja-claude-mcp            # the tools disappear
```

Nothing else changes: the Vikunja tools, the board, the ledger and the OAuth
grants are untouched, and the investment application is not modified or
restarted. `tests/test_mcp_operational.py` asserts that switching them off
removes exactly the names these settings add — the three reads and
`fetch_test_paying_page` — and nothing more.

#### The investment credential: rotation and revocation

`INVESTMENT_API_KEY` is the investment application's own `API_KEY` — the same
value its scripts use. It is a **read-only** capability *here* (this boundary
issues GET to four paths and has no method that could do otherwise), but the key
itself is not read-only elsewhere, so treat it as a live credential.

**Revoke this integration's use of it** without touching the key at all — remove
the two settings and restart, as above. That is the cheapest and usually the
right move.

**Rotate the key** (affects every consumer of the investment API, not just this
one):

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))'      # new value
${EDITOR:-vim} /home/glen/stacks/investment/.env               # replace API_KEY
sudo systemctl restart investment-api-public investment-api-admin
${EDITOR:-vim} /home/glen/stacks/vikunja-claude/.env           # replace INVESTMENT_API_KEY
systemctl --user restart vikunja-claude-mcp
```

Rotate in that order. The API refuses the old key the moment it restarts, so a
boundary still holding it reports "the configured INVESTMENT_API_KEY is not
accepted" — a named configuration failure rather than a silent gap or a false
clean bill of health.

The two credentials are independent: revoking the Vikunja API token cuts the
board off and leaves the operational reads working, and removing
`INVESTMENT_API_KEY` does the reverse.

### Public page fetch (website review)

One read-only tool, `fetch_public_page(path)`, that returns the HTML the public
site actually served for one path, with its HTTP status code and response
headers. It exists so the site can be reviewed from the conversation — "does
`/terms?lang=sv` still carry the Swedish anchors", "what does `/` send as its
canonical link" — without depending on web browsing, which Cloudflare and bot
protection sit in front of. The request is made here, from the host the
application runs on, against the origin.

**It carries no credential.** Not a withheld one — an absent one. The client
takes a base URL and nothing else: there is no parameter through which a key, a
cookie or an `Authorization` header could reach the request, and no cookie jar,
so two fetches are two visitors rather than one session. That is what makes
"public routes only" arithmetic rather than a list kept in step with the
application by hand. A page that needs a login answers this the way it answers a
stranger, and **that answer is what comes back** — the `303` to `/login`, with
its `Location`, not the page behind it. The application decides what is public,
in the one place it already decides it.

**Redirects are reported, not followed.** Following the hop would replace the
evidence of a refusal with a page that looks like a successful fetch of the path
that was asked for. Fetch the `Location` yourself if you want the next page.

**It cannot be aimed.** The site comes from configuration; the argument is a
*path*. A value that carries a scheme, an authority (`//example.com/x`), a
backslash or a fragment is refused, so no argument can point this at another
host, at the admin instance, or at a link found on a page.

Three smaller rules. `Set-Cookie` **values** are never returned — an anonymous
fetch can still be handed a session cookie, and a session cookie is a credential
regardless of who it was minted for; the cookie *names* are listed, because "this
page sets a session cookie" is worth reviewing. A body over 400000 bytes is cut
and `truncated: true` says so. A response that is not text (an image, a PDF) has
its body omitted with a reason, and its status and headers returned anyway.

#### Enabling it

```bash
${EDITOR:-vim} /home/glen/stacks/vikunja-claude/.env
#   INVESTMENT_PUBLIC_URL=http://127.0.0.1:8001      # the PUBLIC instance
systemctl --user restart vikunja-claude-mcp
systemctl --user status vikunja-claude-mcp | grep 'page fetch'
#   … public page fetch via http://127.0.0.1:8001
```

One setting, and no key — there is nothing here to keep secret, so plain `http`
to any host is accepted. It is **independent of the operational reads**: either
capability can be switched on without the other, and unset, the tool is not
advertised at all — the same rule, for the same reason, as the three reads
above.

Point it at the **public** instance (`:8001`), the reverse proxy in front of it,
or the public hostname. Setting it to the same URL as `INVESTMENT_API_URL` — the
admin instance — is a refusal to start: anonymity already means an admin page
answers this like it answers a stranger, but a *public page* tool aimed at the
admin surface is a configuration mistake worth catching at startup rather than
discovering from a page of login redirects.

#### Turning it off

```bash
${EDITOR:-vim} /home/glen/stacks/vikunja-claude/.env   # remove the setting
systemctl --user restart vikunja-claude-mcp            # the tool disappears
```

### Reading tracked Git content

Three read-only tools — `read_repository_file`, `search_repository_text` and
`read_repository_commit_diff` — that let a ticket review inspect the
implementation being claimed instead of stopping at the completion comment.
`get_repository_state` already says *that* the checkout is on commit X; these
say what is in it. A GitHub connector would not do the job, because AI Alpha
Engine and AI Alpha Trader commits are usually local and never pushed.

**Almost none of this is here either.** These are one GET each to a fixed
endpoint of the admin instance. Which revisions resolve, which paths are denied,
what counts as binary, what gets redacted and where the limits sit are all
decided by `api/repository_read.py` in the investment repository, beside the
files they are about. Re-deciding any of it here would create a second boundary
that can drift from the one that actually guards them.

What that boundary holds, in short:

- **It reads Git objects, never the filesystem.** So an untracked file, an
  uncommitted edit and a file outside the repository are invisible by
  construction rather than by refusal, and a symlink cannot be followed out.
- **A revision is a commit id or `HEAD`** — never a branch, tag or expression
  like `HEAD~3` — and it must be reachable from a local branch. An unpushed
  task-branch commit works; one that only the reflog remembers does not.
- **`.env` files, credentials, keys, certificates, databases, backups, logs,
  uploads and run artefacts are refused by path**, including per file inside a
  diff, where the caller does not choose the paths. Refused and binary files are
  *named* with a reason rather than silently dropped.
- **Secret-looking values are redacted** on the way out of all three, and the
  count comes back with the result.
- **Search is fixed-string.** A caller-supplied regular expression is a program,
  and running one over a whole tree is its own denial-of-service.
- **Every limit is reported** beside the result it bounded, so a truncated
  answer is never mistaken for a complete one.

The three tools appear whenever the operational reads are configured — same
credential, same admin instance, no third switch. The endpoints they call are
registered on the admin instance only, so on the public one they do not exist.

#### Which repository is read

Three repositories. `get_repository_state` and the three tracked-content tools
each take an optional `repository`:

| Name | Repository |
|---|---|
| `"ai-alpha-engine"` (default) | the AI Server investment application |
| `"trader"` | the AI Alpha Trader day-trading project |
| `"vikunja-claude"` | this ticket runner — the launcher, `vkctl` and this MCP |

The third exists because a runner ticket is implemented *here* (task 763):
without it a review could read the ticket and the run status and not the commit
that closed them, and a runner commit is local and unpushed until it is merged,
so no hosted connector can reach it either.

**One set of tools, parameterised — there is no repository-specific tool.** A
parallel set would be a second place where the refusals are described, and they
could come to advertise different rules for the same boundary.

It is a **name from a fixed list, never a filesystem path.** Which names exist
is decided and enforced by `api/repository_read.py` in the investment
repository, exactly like every other rule here; this process forwards the name
it was given and refuses nothing, so an unknown one comes back with the
application's own reason naming the valid values. The enum in the tool schemas
is a copy for a model to read, not a second gate.

Everything above holds for all of them: the same denial list, the same
redaction, the same revision grammar, the same limits, and unpushed local
commits working in each. Reachability is per repository — a commit id from one
does not exist in another — so the diff tool asks for the repository the commit
is in, and every response names the repository it resolved.

### Reading the site as the test paying user

One read-only tool, `fetch_test_paying_page(path)`, that returns the HTML the
site served for one path **rendered as the designated test paying user**. It
exists because `fetch_public_page` cannot see a paying-tier page by
construction — it carries no credential, so a cockpit or a report answers it
with the `303` to `/login` — and reviewing what a paying customer is actually
shown is otherwise a manual browser job.

**Almost none of this tool is here.** This process holds no session, mints none
and knows no password. It asks the investment application's admin instance for
"one page as the test paying user" and the application decides everything that
matters: who that is (`TEST_PAYING_USER_ID` on the server), whether they are
still a paying user, which routes it will render, and what may come back. The
rules live in `api/paying_page_read.py` in that repository, beside the
application they are about; `vikunja_claude/paying_page.py` is one GET to one
endpoint.

**The identity is not an argument.** There is no parameter through which a
caller could name a user, and adding a user id to the call changes nothing —
there is nowhere for it to go. So this is one fixed read, not an impersonation
facility with a default.

**It is only ever a paying user.** The application resolves the configured
user's effective level with its own helper and refuses unless it is exactly
`paying`. A trusted or admin identity is refused by name, and a test user whose
subscription lapsed stops the tool rather than quietly downgrading what it
shows.

**It cannot change anything.** The request is always a `GET`, and every write in
the application is a POST. On top of that the application refuses the public GET
routes that manage authentication (login, logout, OAuth, callbacks), billing,
uploads, imports, refreshes, report generation and administration — and that
refusal list is held to the route table by a test, so a route added later has to
be classified before the suite passes.

**No credential comes back.** The session cookie is minted on the server, sent
on the wire and never returned; the page's own `Set-Cookie` — which every
authenticated response carries — is reduced to its name, the same way the
anonymous fetch does it. Redirects are reported, not followed. A body over
400000 bytes is cut and says so.

#### Enabling it

It rides on the operational reads' two settings: same credential, same admin
instance, no third switch here. What it also needs is the application's own
setting, on the **admin** instance:

```bash
${EDITOR:-vim} /home/glen/stacks/investment/.env
#   TEST_PAYING_USER_ID=7                  # the designated test paying user
sudo systemctl restart investment-api-admin
```

Unset there, the tool is advertised and answers with a refusal naming
`TEST_PAYING_USER_ID` — deliberately, because whether the application has a test
identity is the application's fact, and a copy of it in this repository would be
a second source of truth that can disagree with the one that decides.

`TEST_PAYING_PAGE_ORIGIN` on that side names the origin the page is read from,
and defaults to `http://127.0.0.1:8001` — the public unit. It must be loopback
if it is plain `http`: the read carries a live session cookie, and that must not
go over a network in clear text.

### Transport

MCP Streamable HTTP: JSON-RPC over `POST /mcp`, answered as JSON or as a single
SSE event depending on `Accept`. `GET /mcp` is a `405` because there is no
server-initiated stream — a half-working channel would be worse than none.
`GET /health` says nothing about Vikunja, the board or the configuration,
because it is reachable without a token — as are the two metadata documents,
which are public by specification and name only endpoints and one scope.

Requests to `/mcp` carrying an `Origin` header are refused outright. An MCP
client is server-to-server and sends none; a browser always does. That closes
DNS rebinding as a class rather than maintaining a list of origins believed
safe. The rule stops at `/mcp`: the consent screen *is* a browser page, and what
guards the OAuth endpoints is PKCE and the passphrase.

### Deliberately not built yet

Searching tasks, reading repository or pipeline state, labels and status on
create. All are approved in principle (task 138) and none are here: this is the
vertical slice, and every one of them widens either the read surface or the
write surface. Add them one at a time, each with the test that says what it may
not reach.

## Layout

```
vikunja_claude/
  config.py       env-driven settings for both services, optional .env
  vikunja.py      Vikunja client, Ticket, lookup by task id, board number, #NN
  html_text.py    description HTML ↔ plain text
  prompt.py       the prompt template
  executors.py    which harness a run uses, which model behind it, and what it can reach
  launcher.py     locks, spawn, logging, reaping
  service.py      order of operations: move, then launch
  web.py          HTML console
  server.py       routes, status codes, loopback guard
  mcp.py          MCP protocol: JSON-RPC, handshake, fixed tool list
  mcp_service.py  the tools, the approved-project rule, the dedup ledger
  runner.py       one request to the ticket runner's work route, and no more
  investment.py   read-only client for the three AI Server operational reads
  website.py      anonymous, credential-free fetch of one public page
  paying_page.py  one page read as the server's test paying user (no session here)
  mcp_server.py   HTTP routing: the OAuth routes, one MCP route, loopback guard
  oauth.py        the OAuth 2.1 authorization server: metadata, PKCE, consent
  oauth_store.py  clients, codes and tokens as one 0600 JSON file
vkctl.py          board updates for the launched Claude
tests/            lookup, prompt, duplicate launches, API errors, MCP, OAuth
                  test_mcp_runner.py: what starting a run does NOT own
                  test_mcp_task_numbers.py: the board number is the identifier
systemd/          two user units: launcher, MCP boundary
browser/          bookmarklet source
```

The two services are separate on purpose. The launcher starts host processes
and must stay on the tailnet; the MCP boundary holds a write credential and is
the only thing published to the internet. Neither can start, stop or break the
other, and `tests/test_mcp_config.py` fails if the launcher ever comes to
require the MCP boundary's OAuth configuration or vice versa.

Task 726 added the one arrow between them, and it is a request rather than a
coupling: `start_task_run` POSTs to the launcher's work route over loopback.
Task 751 added a second request on that same arrow and no second arrow —
`get_task_run_status` GETs the launcher's run-status route. It reads; the
direction and the confinement below are unchanged by it.
The MCP boundary still starts, serves and refuses without the launcher — a
launcher that is not running is one tool answering "nothing was started", not a
boundary that fails — and the launcher does not know this side exists. The
confinement in `systemd/vikunja-claude-mcp.service` is what makes the direction
load-bearing: that unit starts no host processes, so a run is started by the
service that is allowed to start one, never by the one on the internet.

Within the boundary, `mcp_server.py` decides *routing* and `oauth.py` decides
whether a credential is good. That split is deliberate: "is this token valid"
is produced in one place, so it cannot be one early `return` away from not
happening on a route someone adds later.

## Tests

```bash
cd /home/glen/stacks/vikunja-claude
python3 -m unittest discover -s tests -t .
```

Optional browser tests that drive the real bookmarklet and userscript in
Chromium live in `browser/tests/` — see the README there. They need Playwright
and are deliberately not part of the stdlib-only default run.

267 tests, no network and no live Vikunja: the API is faked through an
injectable transport, and process spawning through an injectable `spawn`. The
MCP and OAuth HTTP tests do bind a real socket, on loopback and an ephemeral
port, and drive the whole authorization flow through it.

Covered: task-id lookup (including that renumbering a title does not change
which task resolves) and board-number lookup (missing, duplicate, unnumbered,
next-Ready selection, and that a legacy `#NN` prefix is not accepted as one); prompt generation (every required clause, and that the
token never appears); duplicate launch prevention (live, stale, cross-instance,
per-task isolation); API error handling (401/500/unreachable/bad bucket and the
HTTP status each maps to); the browser flow (task routes, the board-number redirect, the
launch page's same-origin POST, that the path that page renders actually
launches the task it names, and that neither generated button will read an
id from a board-view URL); and the MCP boundary — the exact tool set, the
project refusal, retry deduplication across a restart, description round-trip,
`Origin` rejection, and that a refused request reaches Vikunja not at all.

The page fetch is tested against a real local `http.server` rather than a mocked
`urlopen`, because the two properties most worth proving are behaviours of
urllib's opener: that no redirect is followed, and that a `Set-Cookie` handed
back by one fetch is not presented by the next. The rest of
`tests/test_mcp_website.py` holds the wire itself — no `Cookie`, no
`Authorization`, no `X-API-Key` on any request — every path shape that would
name another host, and that the tool is absent when unconfigured.

The OAuth boundary is tested as one claim: **the only way to reach `/mcp` is an
access token this server issued through the authorization code flow, with PKCE,
approved by the operator.** So `tests/test_oauth_flow.py` covers the metadata
documents, registration with an unlisted redirect URI, a missing or `plain`
`code_challenge`, a mismatched verifier, a replayed code and the tokens it
revokes, refresh rotation, a token for another audience, a token that has
expired where it sat, the state file being deleted, and the absence of the
password, client-credentials and implicit paths — each ending in the same `401`
or `invalid_grant`.

`TestTheChatGptConnectorCallback` holds the per-connector callback to the same
claim from both sides: that the current form registers and completes a real
authorization, and that the shape which admitted it stops at registration —
every rejected variant (subdomain, lookalike host, `http`, userinfo, a port,
extra segments, an empty identifier, a dot segment, a query, a fragment, a
percent-encoded separator), plus the decisive one, a **second connector's
callback refused for the first connector's client**. That last test fails if
`/oauth/authorize` is ever changed to re-apply the registration rule instead of
comparing against the stored value.

Several MCP tests assert on **the calls the fake Vikunja saw**, not only on
return values: a refusal that still issued the write would satisfy a return
value and fail those. Each guard was also checked by deleting it and confirming
a test goes red — the project restriction, the `Origin` refusal, the dedup
ledger, the required-argument check, and every OAuth guard: PKCE verification,
`S256`-only, single-use codes, the passphrase, its lockout, redirect exact
match at both registration and authorization, refresh rotation, audience
binding, token expiry, and the access-token requirement on `/mcp` itself.

That last one matters more than the count: expiry initially passed with its
check deleted, because the store prunes expired records whenever it is written
and no test let time pass without a write. The test that pins it now advances a
clock the test owns, and fails when the check is removed.
