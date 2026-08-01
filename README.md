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
fallback: enter a `#NN` ticket number to preview or launch, or use
`Work next Ready ticket` for the oldest ticket in the **Ready** bucket.

Reachable over Tailscale by putting `tailscale serve` in front of it — the
service itself stays bound to loopback, and generated buttons pick up whichever
address you loaded them from. Note that anyone on your tailnet who can reach it
can start a Claude run on this host.

| Method | Path | Does |
|---|---|---|
| GET | `/` | Console: ticket input, Preview prompt, Work ticket, Work next Ready |
| GET | `/health` | Service + Vikunja reachability (`503` when Vikunja is down) |
| GET | `/task/{id}` | Ticket details and the generated prompt. **Never launches.** |
| POST | `/task/{id}/work` | Move to In Progress, then launch Claude Code |
| GET | `/task/{id}/launch` | Landing page for the browser button: launches on load |
| GET | `/ticket/{n}` | `#NN` convenience lookup; redirects to `/task/{id}` |
| POST | `/ticket/{n}/work` | Same, by `#NN` |
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

A ticket is identified by its **Vikunja task id** — immutable, assigned by
Vikunja, and what `/tasks/<id>` in the browser URL refers to. That is what the
launcher resolves, what the run lock is keyed on, and what `vkctl.py` takes.

The `#NN` prefix in the title is editable, so it is used only for display and
for the commit reference. A task with no `#NN` prefix still works; its commit
reference becomes `(vikunja task <id>)`.

`/ticket/{n}` remains as a convenience for humans who think in ticket numbers —
it resolves the prefix and redirects to the canonical `/task/{id}`. Two tasks
claiming the same `#NN` is a `409` there, not a coin flip; by task id it is
never ambiguous at all.

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
- restricts work to that one ticket;
- requires tests, and forbids weakening existing ones;
- requires a commit referencing `(#NN)`, or `(vikunja task <id>)` when the
  title carries no `#NN` prefix;
- **forbids pushing** — no push, no PR, no remote;
- tells Claude to comment and move the ticket to **Waiting** if blocked, or to
  comment and move it to **Done** if it finished.

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
- **One run per task.** A lock file keyed on the immutable task id (`O_EXCL`)
  blocks a second launch, and survives a restart of this service. A lock whose
  PID is dead is treated as stale and reclaimed. Keying on the task id means
  renaming a ticket cannot smuggle a second concurrent run past the guard.
- Every launch, failure, timeout and exit is appended as JSON to
  `~/.local/state/vikunja-claude/launches.jsonl`; each run's full output goes to
  `~/.local/state/vikunja-claude/runs/ticket-NN-<timestamp>.log`.

## The MCP boundary (ChatGPT)

A second, separate service that lets ChatGPT **read the board**, **create a
ticket**, **correct a ticket's wording**, **comment on one**, **read a few
operational facts about the AI Server** and **read a public page of its
website** — so project context does not have to be pasted in by hand, and a
ticket dictated in a conversation does not have to be retyped onto the board.

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
| `get_task(task_id)` | Title, full description, status, bucket, labels, timestamps and comments for one task on the **AI Alpha Engine** board |
| `list_open_tasks(bucket?, label?)` | Every task that is not done on that board — id, title, bucket, priority, labels and timestamps, most urgent first. No descriptions, no comments |
| `search_tasks(text, status?)` | Tasks whose title or description contains `text`. `status` is `open` (default), `done` or `any` — this is the one read that can see finished tasks, so it is what answers "is there already a ticket about this" |
| `create_task(project_id, title, description)` | Creates one task on that board and returns its id and URL |
| `update_task(task_id, title?, description?, approval_token?)` | Replaces one existing task's title, description or both. Two calls: the first returns the exact current and proposed values with an approval token and writes nothing; the second must carry that token |
| `add_task_comment(task_id, comment, approval_token?)` | Appends one plain-text comment to an existing task, behind the same two-call approval |

Plus three **operational reads**, present only when they are configured (see
[Operational reads](#operational-reads-ai-server-status) below):

| Tool | Does |
|---|---|
| `get_repository_state()` | Branch, commit, commit subject and whether the investment checkout's working tree is clean |
| `get_pipeline_status()` | The latest nightly-pipeline run: status, every stage's own outcome, what S7 did, whether each portfolio got a report |
| `get_system_health()` | Backup age, disks, Docker, scheduled jobs, database and API, with one folded overall status |

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

### Rules the reads hold

`get_task` needs an id you already have. `list_open_tasks` is what answers
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
- **A lookup by id asks for that id, and a miss is not an absence.**
  `find_by_task_id` filters the view to the one id, which is constant cost and
  keeps the project boundary structural — it is *this project's* view, so
  another project's task is not in the answer to begin with. (`GET /tasks/{id}`
  would be one request too, but it serves any task in any project and reports
  `bucket_id: 0`, so it can answer neither "is this mine" nor "which column".)
  If the filter yields nothing, the complete walk runs before "no such task" is
  said. Note which failure that guards: a filter the server *ignores* is
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
- **The order is total.** Most urgent first, then by task id — priority alone
  is not an order, since most of the board sits at 0.

It also has no shell, no database connection and no filesystem access beyond
its own ledger, because nothing in `mcp_service.py` has any of those.

### Rules the write path holds

- **One project, named not defaulted.** `project_id` is required, and a value
  other than the configured project is refused outright. It is never silently
  redirected — creating the ticket in the wrong place is the failure this
  exists to prevent.
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
- **The task is found through this project's board view**, exactly as the reads
  are, so a task on somebody else's board is not in the answer to begin with.
  The refusal does not depend on comparing a project id the caller supplied, and
  it cannot be bought with a valid approval for a different task.
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
| `VIKUNJA_MCP_OAUTH_REDIRECT_URIS` | Optional. Space- or comma-separated exact URIs. Defaults to `https://chatgpt.com/connector_platform_oauth_redirect`. ChatGPT's per-connector callback is admitted by shape as well and needs no entry here |
| `VIKUNJA_MCP_OAUTH_CLIENT_ID` / `_SECRET` | Optional. A pre-registered client, for a ChatGPT dialog that insists on a client id instead of registering one itself |

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
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_task","arguments":{"task_id":138}}}' \
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
  vikunja.py      Vikunja client, Ticket, #NN lookup
  html_text.py    description HTML ↔ plain text
  prompt.py       the prompt template
  launcher.py     locks, spawn, logging, reaping
  service.py      order of operations: move, then launch
  web.py          HTML console
  server.py       routes, status codes, loopback guard
  mcp.py          MCP protocol: JSON-RPC, handshake, fixed tool list
  mcp_service.py  the tools, the project rule, the dedup ledger
  investment.py   read-only client for the three AI Server operational reads
  website.py      anonymous, credential-free fetch of one public page
  paying_page.py  one page read as the server's test paying user (no session here)
  mcp_server.py   HTTP routing: the OAuth routes, one MCP route, loopback guard
  oauth.py        the OAuth 2.1 authorization server: metadata, PKCE, consent
  oauth_store.py  clients, codes and tokens as one 0600 JSON file
vkctl.py          board updates for the launched Claude
tests/            lookup, prompt, duplicate launches, API errors, MCP, OAuth
systemd/          two user units: launcher, MCP boundary
browser/          bookmarklet source
```

The two services are separate on purpose. The launcher starts host processes
and must stay on the tailnet; the MCP boundary holds a write credential and is
the only thing published to the internet. Neither can start, stop or break the
other, and `tests/test_mcp_config.py` fails if the launcher ever comes to
require the MCP boundary's OAuth configuration or vice versa.

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
which task resolves) and `#NN` lookup (missing, duplicate, unnumbered,
next-Ready selection); prompt generation (every required clause, and that the
token never appears); duplicate launch prevention (live, stale, cross-instance,
per-task isolation); API error handling (401/500/unreachable/bad bucket and the
HTTP status each maps to); the browser flow (task routes, `#NN` redirect, the
launch page's same-origin POST, and that neither generated button will read an
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
