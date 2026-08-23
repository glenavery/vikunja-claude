"""HTML rendering for the local console. No templates, no assets, no CDN."""

from __future__ import annotations

from html import escape

STYLE = """
:root { color-scheme: light dark; --fg:#111; --bg:#fff; --muted:#666;
        --line:#d8d8d8; --panel:#f6f6f6; --accent:#2f6f4f; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e6e6e6; --bg:#16181a; --muted:#9aa0a6;
          --line:#31353a; --panel:#1e2124; --accent:#79c0a0; }
}
* { box-sizing: border-box; }
body { margin:0; padding:2rem 1.25rem; background:var(--bg); color:var(--fg);
       font:15px/1.55 ui-sans-serif, system-ui, -apple-system, sans-serif; }
main { max-width: 60rem; margin: 0 auto; }
h1 { font-size:1.4rem; margin:0 0 .25rem; }
h2 { font-size:1rem; margin:2rem 0 .5rem; text-transform:uppercase;
     letter-spacing:.07em; color:var(--muted); }
a { color:var(--accent); }
.sub { color:var(--muted); margin:0 0 1.5rem; }
.row { display:flex; gap:.5rem; flex-wrap:wrap; align-items:center; }
input[type=number] { width:7rem; padding:.5rem .6rem; font:inherit;
       border:1px solid var(--line); border-radius:6px;
       background:var(--bg); color:var(--fg); }
button { padding:.5rem .9rem; font:inherit; border:1px solid var(--line);
         border-radius:6px; background:var(--panel); color:var(--fg);
         cursor:pointer; }
button:hover { border-color:var(--accent); }
button.primary { background:var(--accent); color:var(--bg); border-color:var(--accent); }
pre { background:var(--panel); border:1px solid var(--line); border-radius:8px;
      padding:1rem; overflow-x:auto; white-space:pre-wrap; word-wrap:break-word;
      font:13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
table { border-collapse:collapse; width:100%; }
td, th { text-align:left; padding:.35rem .6rem .35rem 0; vertical-align:top;
         border-bottom:1px solid var(--line); }
th { color:var(--muted); font-weight:500; width:10rem; }
.tag { display:inline-block; padding:.1rem .5rem; border:1px solid var(--line);
       border-radius:999px; font-size:.8rem; margin-right:.3rem; }
.warn { border-left:3px solid #c9812f; padding:.6rem .9rem; background:var(--panel);
        border-radius:0 6px 6px 0; margin:1rem 0; }
.comment { border-left:3px solid var(--accent); padding:0 0 0 .9rem; margin:1rem 0; }
.comment .sub { margin:0 0 .35rem; font-size:.85rem; }
.comment pre { margin:0; }
#out:empty { display:none; }
"""

SCRIPT = """
async function call(method, path) {
  const out = document.getElementById('out');
  out.textContent = method + ' ' + path + ' ...';
  try {
    const res = await fetch(path, {method, headers:{'Accept':'application/json'}});
    const body = await res.json();
    out.textContent = 'HTTP ' + res.status + '\\n' + JSON.stringify(body, null, 2);
  } catch (err) {
    out.textContent = 'Request failed: ' + err;
  }
}
function num() {
  const raw = document.getElementById('ticket').value.trim();
  if (!/^[0-9]+$/.test(raw)) { alert('Enter a ticket number, e.g. 33'); return null; }
  return raw;
}
function preview() { const n = num(); if (n) location.href = '/ticket/' + n; }
function work()    { const n = num(); if (n) call('POST', '/ticket/' + n + '/work'); }
function workNext(){ call('POST', '/next/work'); }
function showNext(){ location.href = '/next'; }
"""


def page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{escape(title)}</title><style>{STYLE}</style></head>"
        f"<body><main>{body}</main><script>{SCRIPT}</script></body></html>"
    )


def _work_path(number, task_id: int | None = None) -> str:
    """The POST that launches a run, addressed the way the board names it."""
    if number is not None:
        return f"/ticket/{int(number)}/work"
    return f"/task/{int(task_id)}/work" if task_id is not None else "/next/work"


def _ticket_href(number) -> str:
    """Where this service addresses a ticket: by the board number (task 659).

    Falls back to nothing rather than to /task/<row id> — a page that links the
    row id is a page that teaches the row id, which is how it kept spreading.
    """
    return f"/ticket/{int(number)}" if number is not None else "/"


def console(project: str, workdir: str, running: list[dict], recent: list[dict]) -> str:
    running_html = ""
    if running:
        items = "".join(
            "<li><a href=\"{href}\">{ref}</a> — pid {pid}, "
            "started {at}</li>".format(
                href=escape(_ticket_href(r.get("number"))),
                ref=escape(str(r.get("reference") or "?")),
                pid=escape(str(r.get("pid"))),
                at=escape(str(r.get("started_at"))),
            )
            for r in running
        )
        running_html = f"<h2>Running now</h2><ul>{items}</ul>"

    recent_html = ""
    if recent:
        rows = "".join(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                escape(str(r.get("at", ""))),
                escape(str(r.get("reference", "?"))),
                escape(str(r.get("event", ""))),
                escape(
                    "exit " + str(r["exit_status"])
                    if "exit_status" in r
                    else str(r.get("pid", ""))
                ),
            )
            for r in recent
        )
        recent_html = (
            "<h2>Recent launches</h2><table><tr><th>When</th><th>Ticket</th>"
            f"<th>Event</th><th>Detail</th></tr>{rows}</table>"
        )

    return page(
        "Work with Claude",
        f"""
<h1>Work with Claude</h1>
<p class="sub">{escape(project)} &middot; launches Claude Code in
<code>{escape(workdir)}</code></p>
<div class="row">
  <input type="number" id="ticket" min="1" placeholder="33" aria-label="Ticket number">
  <button onclick="preview()">Preview prompt</button>
  <button class="primary" onclick="work()">Work ticket</button>
  <button onclick="workNext()">Work next Ready ticket</button>
  <button onclick="showNext()">Show next Ready</button>
</div>
<pre id="out"></pre>
{running_html}
{recent_html}
<h2>Browser button</h2>
<p><a href="/bookmarklet">Install the “Work with Claude” bookmarklet</a> to jump
here straight from a Vikunja task.</p>
""",
    )


def comments_html(comments) -> str:
    """A task's comments, oldest first.

    "No comments yet" is written out rather than left blank. Blank is what the
    page showed while it did not read comments at all, so an empty section
    would be indistinguishable from the feature being absent -- which is the
    condition this replaces.

    Bodies arrive as text flattened from the Vikunja editor's HTML and are
    escaped here. They are the one part of this page that a person other than
    the operator wrote.
    """
    if not comments:
        return "<p class=\"sub\">No comments yet.</p>"
    items = []
    for comment in comments:
        who = escape(str(comment.get("author") or "unknown"))
        when = escape(str(comment.get("created") or ""))
        items.append(
            f"<div class=\"comment\"><p class=\"sub\">{who} &middot; {when}</p>"
            f"<pre>{escape(comment.get('text') or '')}</pre></div>"
        )
    return "".join(items)


def ticket_page(data: dict) -> str:
    work = escape(_work_path(data.get("number")))
    reference = escape(str(data["reference"]))
    labels = "".join(f"<span class=\"tag\">{escape(l)}</span>" for l in data["labels"])
    running = data.get("running")
    warn = ""
    if running:
        warn = (
            f"<div class=\"warn\">Claude is already working this ticket "
            f"(pid {escape(str(running.get('pid')))}, started "
            f"{escape(str(running.get('started_at')))}). A second launch will be "
            f"refused.</div>"
        )
    return page(
        f"{data['reference']} — Work with Claude",
        f"""
<h1>{reference} {escape(data['summary'])}</h1>
<p class="sub"><a href="{escape(data['url'])}">open in Vikunja</a></p>
{warn}
<table>
  <tr><th>Bucket</th><td>{escape(str(data['bucket']))}</td></tr>
  <tr><th>Labels</th><td>{labels or '—'}</td></tr>
  <tr><th>Done</th><td>{'yes' if data['done'] else 'no'}</td></tr>
  <tr><th>Repository</th><td><code>{escape(data['workdir'])}</code></td></tr>
</table>
<h2>Description</h2>
<pre>{escape(data['description'] or '(none)')}</pre>
<h2>Comments</h2>
{comments_html(data.get('comments'))}
<h2>Generated prompt</h2>
<pre>{escape(data['prompt'])}</pre>
<div class="row">
  <button class="primary" onclick="call('POST','{work}')">
    Work {reference}</button>
  <button onclick="location.href='/'">Back to console</button>
</div>
<pre id="out"></pre>
""",
    )


def launch_page(
    number: int | None, task_id: int, reference: str, summary: str,
    vikunja_url: str
) -> str:
    """Auto-launching landing page for the browser button.

    Navigating here is cross-origin and always allowed; the POST it fires is
    same-origin, so the one-click flow needs no CORS.

    Reached at ``/task/<row id>/launch`` — the browser is sitting on Vikunja's
    own ``/tasks/<id>`` page, so that id is what the bookmarklet has. Everything
    the rendered page then addresses is the BOARD number (task 659): the entry
    point is not a reason for the page to keep teaching the row id. ``task_id``
    is the fallback for a task Vikunja reported no index for, which is the only
    case where the row id is the sole address there is.
    """
    view = escape(_ticket_href(number) if number is not None else f"/task/{task_id}")
    work = escape(_work_path(number, task_id))
    return page(
        f"Launching {reference}",
        f"""
<h1 id="state">Launching {escape(reference)}…</h1>
<p class="sub">{escape(summary)} &middot;
<a href="{escape(vikunja_url)}">back to Vikunja</a></p>
<pre id="out">starting…</pre>
<div class="row">
  <button onclick="location.href='{view}'">View prompt</button>
  <button onclick="location.href='/'">Console</button>
</div>
<script>
(async function () {{
  const state = document.getElementById('state');
  const out = document.getElementById('out');
  try {{
    const res = await fetch('{work}', {{
      method: 'POST', headers: {{'Accept': 'application/json'}}
    }});
    const body = await res.json();
    if (res.status === 202) {{
      state.textContent = '✅ Claude is working {escape(reference)}';
    }} else if (res.status === 409) {{
      state.textContent = '⏳ Already running';
    }} else {{
      state.textContent = '❌ Launch failed (HTTP ' + res.status + ')';
    }}
    out.textContent = JSON.stringify(body, null, 2);
  }} catch (err) {{
    state.textContent = '❌ Could not reach the launcher';
    out.textContent = String(err);
  }}
}})();
</script>
""",
    )


def userscript(service_origin: str, vikunja_origins: list[str]) -> str:
    """A Tampermonkey userscript that puts a 🤖 button on Vikunja task pages."""
    matches = "\n".join(
        f"// @match        {origin}/*" for origin in dict.fromkeys(vikunja_origins)
    )
    return f"""\
// ==UserScript==
// @name         Work with Claude (Vikunja)
// @namespace    vikunja-claude
// @version      1.1
// @description  Adds a 🤖 Work with Claude button to Vikunja task pages.
// @author       vikunja-claude
{matches}
// @grant        none
// @run-at       document-idle
// ==/UserScript==

(function () {{
  'use strict';

  const SERVICE = '{service_origin}';
  const BUTTON_ID = 'work-with-claude-btn';

  // Only /tasks/<id> is a task. /projects/<id>/<viewId> is a board view, and
  // reading an id from it would launch the wrong ticket.
  function taskId() {{
    const m = location.pathname.match(/\\/tasks\\/(\\d+)/);
    return m ? m[1] : null;
  }}

  function removeButton() {{
    const existing = document.getElementById(BUTTON_ID);
    if (existing) existing.remove();
  }}

  function addButton(id) {{
    if (document.getElementById(BUTTON_ID)) return;

    const button = document.createElement('button');
    button.id = BUTTON_ID;
    button.type = 'button';
    button.textContent = '🤖 Work with Claude';
    button.title = 'Launch Claude Code for task ' + id;
    button.style.cssText = [
      'position:fixed', 'right:1.25rem', 'bottom:1.25rem', 'z-index:9999',
      'padding:.6rem 1rem', 'border:0', 'border-radius:999px',
      'background:#2f6f4f', 'color:#fff', 'font:600 14px/1 system-ui,sans-serif',
      'cursor:pointer', 'box-shadow:0 2px 10px rgba(0,0,0,.3)'
    ].join(';');
    button.addEventListener('click', function () {{
      window.open(SERVICE + '/task/' + id + '/launch', '_blank');
    }});

    // Prefer sitting next to the task's own actions; fall back to floating.
    const actions = document.querySelector('.task-view .action-buttons');
    if (actions) {{
      button.style.position = 'static';
      button.style.width = '100%';
      button.style.marginTop = '.5rem';
      actions.prepend(button);
    }} else {{
      document.body.appendChild(button);
    }}
  }}

  function sync() {{
    const id = taskId();
    if (id) {{
      addButton(id);
    }} else {{
      removeButton();
    }}
  }}

  // Vikunja is a single-page app: the URL changes without a reload.
  let lastPath = location.pathname;
  new MutationObserver(function () {{
    if (location.pathname !== lastPath) {{
      lastPath = location.pathname;
      removeButton();
    }}
    sync();
  }}).observe(document.body, {{ childList: true, subtree: true }});

  sync();
}})();
"""


def bookmarklet_page(bookmarklet: str, service_url: str) -> str:
    return page(
        "Work with Claude — browser button",
        f"""
<h1>🤖 Work with Claude</h1>
<p class="sub">One click on an open Vikunja task launches Claude Code for it.
Vikunja itself is not forked or modified.</p>

<h2>Option 1 — bookmarklet</h2>
<p>Drag this to your bookmarks bar:
<a href="{escape(bookmarklet, quote=True)}">🤖 Work with Claude</a></p>
<ol>
  <li>Show the bookmarks bar: <kbd>Ctrl+Shift+B</kbd>.</li>
  <li>Drag the link above onto the bar. If Chrome blocks the drag, right-click
      the bar → <em>Add page…</em>, name it “Work with Claude”, and paste the
      source below as the URL.</li>
  <li>Open a Vikunja <strong>task</strong> — the URL must look like
      <code>/tasks/123</code> — and click the bookmark. Claude launches for
      that task and this page shows the result.</li>
</ol>
<p>A board view (<code>/projects/2/11</code>) is deliberately rejected: the
number there is a <em>view</em> id, not a task id, so acting on it would launch
the wrong ticket.</p>
<pre>{escape(bookmarklet)}</pre>

<h2>Option 2 — userscript (adds a real button)</h2>
<p>With Tampermonkey installed,
<a href="/userscript.user.js">install the userscript</a> to get a persistent 🤖
button on every task page instead of a bookmark click.</p>
<ol>
  <li>Install the Tampermonkey extension in Chrome.</li>
  <li>Open <a href="/userscript.user.js"><code>{escape(service_url)}/userscript.user.js</code></a>
      — Tampermonkey offers to install it. (It only recognises
      <code>.user.js</code> URLs.)</li>
  <li>Open any Vikunja task; the button appears bottom-right.</li>
</ol>

<h2>Links point at</h2>
<p><code>{escape(service_url)}</code> — generated from the address you loaded
this page from, so the button keeps working over Tailscale.</p>
""",
    )


def error_page(status: int, message: str) -> str:
    return page(
        f"{status} — Work with Claude",
        f"""
<h1>{status}</h1>
<pre>{escape(message)}</pre>
<p><a href="/">Back to console</a></p>
""",
    )
