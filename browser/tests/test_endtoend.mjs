// Full one-click flow in a real browser: Vikunja task page → button → launch
// page → POST → running. Points at a temp launcher (port 3461) whose CLAUDE_BIN
// is a stub, so no real Claude run starts.
import { chromium } from 'playwright';

const LAUNCHER = process.env.LAUNCHER || 'http://127.0.0.1:3461';
const VIKUNJA = process.env.VIKUNJA || 'http://127.0.0.1:3456';
const TASK = process.argv[2];

const results = [];
const check = (name, pass, detail = '') => {
  results.push({ name, pass, detail });
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`);
};

const bookmarkletRaw = await (await fetch(`${LAUNCHER}/bookmarklet`)).text();
const bookmarklet = bookmarkletRaw
  .match(/<pre>(javascript:[\s\S]*?)<\/pre>/)[1]
  .replace(/&#x27;/g, "'").replace(/&amp;/g, '&').replace(/&lt;/g, '<')
  .replace(/&gt;/g, '>').replace(/&quot;/g, '"')
  .replace(/^javascript:/, '');

const script = await (await fetch(`${LAUNCHER}/userscript`)).text();
const userscript = script.replace(/\/\/ ==UserScript==[\s\S]*?\/\/ ==\/UserScript==/, '');

const browser = await chromium.launch();
const context = await browser.newContext();
await context.route(`${VIKUNJA}/**`, (route) =>
  route.fulfill({
    status: 200, contentType: 'text/html',
    body: `<!doctype html><html><body><div class="task-view">
           <div class="column is-one-third action-buttons d-print-none"></div>
           </div></body></html>`,
  }));

// --- bookmarklet on a real task URL ----------------------------------------
const page = await context.newPage();
await page.goto(`${VIKUNJA}/tasks/${TASK}`);
const [popup] = await Promise.all([
  context.waitForEvent('page'),
  page.evaluate(bookmarklet),
]);
check('bookmarklet opens the launch page', popup.url() === `${LAUNCHER}/task/${TASK}/launch`,
  popup.url());

// --- the launch page actually launches --------------------------------------
await popup.waitForFunction(
  () => !document.getElementById('state').textContent.includes('Launching'),
  { timeout: 15000 });
const state = await popup.locator('#state').textContent();
const out = await popup.locator('#out').textContent();
check('launch page reports success', state.includes('✅') && state.includes('Claude is working'), state.trim());
const parsed = JSON.parse(out);
check('launch response carries the task id', parsed.task_id === Number(TASK), `task_id=${parsed.task_id}`);
check('ticket was moved to In Progress', parsed.moved_to === 'In Progress', String(parsed.moved_to));
check('a real pid was returned', Number.isInteger(parsed.pid) && parsed.pid > 0, String(parsed.pid));

// --- a second click is refused, visibly -------------------------------------
const page2 = await context.newPage();
await page2.goto(`${LAUNCHER}/task/${TASK}/launch`);
await page2.waitForFunction(
  () => !document.getElementById('state').textContent.includes('Launching'),
  { timeout: 15000 });
const state2 = await page2.locator('#state').textContent();
check('second launch shows "Already running"', state2.includes('Already running'), state2.trim());

// --- board view is refused by the bookmarklet ------------------------------
const page3 = await context.newPage();
await page3.goto(`${VIKUNJA}/projects/2/11`);
let alerted = null;
page3.on('dialog', async (d) => { alerted = d.message(); await d.dismiss(); });
await page3.evaluate(bookmarklet);
await page3.waitForTimeout(400);
check('bookmarklet refuses a board view', !!alerted && alerted.includes('not a task'),
  alerted || 'no alert');

// --- userscript button drives the same flow --------------------------------
const page4 = await context.newPage();
await page4.goto(`${VIKUNJA}/tasks/${TASK}`);
await page4.evaluate(userscript);
await page4.waitForTimeout(300);
const [popup2] = await Promise.all([
  context.waitForEvent('page'),
  page4.locator('#work-with-claude-btn').click(),
]);
check('userscript button opens the same launch page',
  popup2.url() === `${LAUNCHER}/task/${TASK}/launch`, popup2.url());

await browser.close();
const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
