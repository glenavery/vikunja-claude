// Drives the real userscript in a real Chromium against the real Vikunja
// origin. Where Vikunja needs a login we serve a stand-in document at the
// *same origin and path* so the script sees a genuine /tasks/<id> URL.
import { chromium } from 'playwright';

const LAUNCHER = process.env.LAUNCHER || 'http://127.0.0.1:3460';
const VIKUNJA = process.env.VIKUNJA || 'http://127.0.0.1:3456';

const results = [];
const check = (name, pass, detail = '') => {
  results.push({ name, pass, detail });
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`);
};

// The userscript as actually served to a browser.
const script = await (await fetch(`${LAUNCHER}/userscript`)).text();
// Tampermonkey strips the metadata block; emulate that.
const body = script.replace(/\/\/ ==UserScript==[\s\S]*?\/\/ ==\/UserScript==/, '');

const browser = await chromium.launch();
const context = await browser.newContext();

// Stand-in document, served at the real Vikunja origin.
await context.route(`${VIKUNJA}/**`, async (route) => {
  const path = new URL(route.request().url()).pathname;
  await route.fulfill({
    status: 200,
    contentType: 'text/html',
    body: `<!doctype html><html><body>
      <div class="app"><h1 class="task-title">#33 Back up Vikunja database</h1>
      <div class="task-view"><div class="action-buttons"></div></div></div>
      <script>window.__path = ${JSON.stringify(path)};</script>
    </body></html>`,
  });
});

const page = await context.newPage();
const consoleErrors = [];
page.on('pageerror', (e) => consoleErrors.push(String(e)));

// --- 1. a real task URL -----------------------------------------------------
await page.goto(`${VIKUNJA}/tasks/11`);
await page.evaluate(body);
await page.waitForTimeout(300);

const btn = page.locator('#work-with-claude-btn');
check('button appears on /tasks/11', (await btn.count()) === 1);
check('button is labelled', (await btn.count()) === 1 &&
  (await btn.textContent()).includes('Work with Claude'),
  (await btn.count()) === 1 ? await btn.textContent() : 'no button');
check('no page errors from the userscript', consoleErrors.length === 0,
  consoleErrors.join('; '));

const title = (await btn.count()) === 1 ? await btn.getAttribute('title') : '';
check('button carries the task id from the URL', title === 'Launch Claude Code for task 11', title);

// placement: inline next to the task's own actions, when that container exists
const placedInline = await page.evaluate(() =>
  !!document.querySelector('.task-view .action-buttons #work-with-claude-btn'));
check('button placed inline in .task-view .action-buttons', placedInline);

// --- 2. clicking opens the launcher's launch page ---------------------------
const [popup] = await Promise.all([
  context.waitForEvent('page'),
  btn.click(),
]);
check('click opens the launcher launch URL', popup.url() === `${LAUNCHER}/task/11/launch`,
  popup.url());
await popup.close();

// --- 3. a board view must NOT get a button ---------------------------------
const page2 = await context.newPage();
await page2.goto(`${VIKUNJA}/projects/2/11`);
await page2.evaluate(body);
await page2.waitForTimeout(300);
check('no button on a board view /projects/2/11',
  (await page2.locator('#work-with-claude-btn').count()) === 0);

// --- 4. SPA navigation: button follows the URL ------------------------------
const page3 = await context.newPage();
await page3.goto(`${VIKUNJA}/tasks/9`);
await page3.evaluate(body);
await page3.waitForTimeout(300);
const before = await page3.locator('#work-with-claude-btn').count();
await page3.evaluate(() => {
  history.pushState({}, '', '/projects/2/12');
  document.body.appendChild(document.createElement('span')); // trigger observer
});
await page3.waitForTimeout(300);
const after = await page3.locator('#work-with-claude-btn').count();
check('button present on task, removed after SPA nav to a board view',
  before === 1 && after === 0, `before=${before} after=${after}`);

// --- 5. only one button, even on repeated observer runs ---------------------
const page4 = await context.newPage();
await page4.goto(`${VIKUNJA}/tasks/11`);
await page4.evaluate(body);
await page4.evaluate(() => {
  for (let i = 0; i < 5; i++) document.body.appendChild(document.createElement('span'));
});
await page4.waitForTimeout(300);
check('never duplicates the button',
  (await page4.locator('#work-with-claude-btn').count()) === 1);

await browser.close();

const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
