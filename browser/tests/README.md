# Browser tests (optional)

The main suite (`python3 -m unittest discover -s tests -t .`) is standard
library only and needs no browser. These two extra harnesses drive the actual
bookmarklet and userscript in a real Chromium, and are **not** part of it.

```bash
npm install playwright
npx playwright install chromium      # no root needed
```

If Chromium refuses to start with `error while loading shared libraries`, the
usual fix is `npx playwright install-deps` (needs root). Without root you can
stage the libraries locally instead:

```bash
mkdir -p debs libs && cd debs
for p in libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 libasound2t64 \
         libatspi2.0-0t64 libxdamage1 libxres1; do apt-get download $p; done
for d in *.deb; do dpkg-deb -x "$d" ../libs; done
cd .. && export LD_LIBRARY_PATH=$PWD/libs/usr/lib/x86_64-linux-gnu
```

## `test_userscript.mjs`

Loads the userscript exactly as `/userscript` serves it (metadata block
stripped, as Tampermonkey does) and checks: the button appears on `/tasks/<id>`
and nowhere else, carries the right task id, is placed inline in
`.task-view .action-buttons`, opens the right launch URL, survives single-page
navigation, and never duplicates itself.

```bash
node test_userscript.mjs
```

## `test_endtoend.mjs`

Drives the whole click-to-launch path against a **temporary** launcher whose
`CLAUDE_BIN` is a stub, so no real Claude run starts. Create a throwaway Vikunja
task first and pass its task id.

```bash
printf '#!/bin/bash\necho stub; sleep 6\n' > /tmp/stub-claude.sh
chmod +x /tmp/stub-claude.sh

VIKUNJA_CLAUDE_PORT=3461 CLAUDE_BIN=/tmp/stub-claude.sh CLAUDE_ARGS=--stub \
  VIKUNJA_CLAUDE_STATE_DIR=/tmp/vkc-test-state python3 -m vikunja_claude &

node test_endtoend.mjs <task-id>
```

Delete the throwaway task afterwards.

## What these cannot cover

Vikunja requires a login, so both harnesses serve a stand-in document at the
real Vikunja origin and path rather than the authenticated task page. The
`.task-view .action-buttons` selector the userscript targets was confirmed
against Vikunja's compiled `TaskDetailView` chunk, not against a live rendered
page. Confirming the button in the real DOM needs a logged-in browser session.
