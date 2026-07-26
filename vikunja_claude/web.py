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


def console(project: str, workdir: str, running: list[dict], recent: list[dict]) -> str:
    running_html = ""
    if running:
        items = "".join(
            f"<li>#{escape(str(r.get('ticket')))} — pid {escape(str(r.get('pid')))}"
            f", started {escape(str(r.get('started_at')))}</li>"
            for r in running
        )
        running_html = f"<h2>Running now</h2><ul>{items}</ul>"

    recent_html = ""
    if recent:
        rows = "".join(
            "<tr><td>{}</td><td>#{}</td><td>{}</td><td>{}</td></tr>".format(
                escape(str(r.get("at", ""))),
                escape(str(r.get("ticket", "?"))),
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


def ticket_page(data: dict) -> str:
    number = escape(str(data["ticket"]))
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
        f"#{data['ticket']} — Work with Claude",
        f"""
<h1>#{number} {escape(data['summary'])}</h1>
<p class="sub"><a href="{escape(data['url'])}">open in Vikunja</a> &middot;
task id {escape(str(data['task_id']))}</p>
{warn}
<table>
  <tr><th>Bucket</th><td>{escape(str(data['bucket']))}</td></tr>
  <tr><th>Labels</th><td>{labels or '—'}</td></tr>
  <tr><th>Done</th><td>{'yes' if data['done'] else 'no'}</td></tr>
  <tr><th>Repository</th><td><code>{escape(data['workdir'])}</code></td></tr>
</table>
<h2>Description</h2>
<pre>{escape(data['description'] or '(none)')}</pre>
<h2>Generated prompt</h2>
<pre>{escape(data['prompt'])}</pre>
<div class="row">
  <button class="primary" onclick="call('POST','/ticket/{number}/work')">
    Work ticket #{number}</button>
  <button onclick="location.href='/'">Back to console</button>
</div>
<pre id="out"></pre>
""",
    )


def bookmarklet_page(bookmarklet: str, service_url: str) -> str:
    return page(
        "Work with Claude — bookmarklet",
        f"""
<h1>“Work with Claude” bookmarklet</h1>
<p class="sub">Adds a button to any Vikunja task without forking Vikunja.</p>
<p>Drag this link to your bookmarks bar:
<a href="{escape(bookmarklet, quote=True)}">Work with Claude</a></p>
<h2>Install in Chrome</h2>
<ol>
  <li>Show the bookmarks bar: <kbd>Ctrl+Shift+B</kbd>.</li>
  <li>Drag the link above onto the bar. (Chrome blocks dragging in some
      versions — if so, right-click the bar → <em>Add page…</em>, name it
      “Work with Claude”, and paste the code below as the URL.)</li>
  <li>Open a Vikunja task, click the bookmark. It reads the <code>#NN</code>
      from the task title and opens <code>{escape(service_url)}/ticket/NN</code>.</li>
</ol>
<h2>Bookmarklet source</h2>
<pre>{escape(bookmarklet)}</pre>
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
