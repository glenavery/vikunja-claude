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

A second, separate service that lets ChatGPT **read a ticket** and **create a
ticket** — so project context does not have to be pasted in by hand, and a
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
| `create_task(project_id, title, description)` | Creates one task on that board and returns its id and URL |

That is the entire surface. There are two tools, they are a fixed list in the
code, and there is no generic passthrough — so "this connection cannot edit,
close, delete, comment on or move a task" is a property of what exists, not a
promise about what will be asked for. `tests/test_mcp_protocol.py` asserts the
tool set as an *exact* set, which fails the day a third one appears.

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

ChatGPT opens the consent screen in a browser. It names the two operations and
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
  mcp_service.py  the two tools, the project rule, the dedup ledger
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
