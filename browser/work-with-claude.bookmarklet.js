// "Work with Claude" bookmarklet — readable source.
//
// Install the *generated* one from http://<launcher>/bookmarklet instead of
// pasting this: that version is built from the address you loaded the page
// from, so it keeps working over Tailscale. This file is the same logic,
// unminified, for review.
//
// One click on an open Vikunja task launches Claude Code for it.

javascript: (function () {
  var service = 'http://127.0.0.1:3460';

  // Only /tasks/<id> identifies a task. Vikunja's other numeric route,
  // /projects/<projectId>/<viewId>, is a *board view* — the number there is a
  // view id (List/Gantt/Table/Kanban), not a task id. Reading it would launch
  // the wrong ticket, so it is rejected rather than guessed at.
  var match = location.pathname.match(/\/tasks\/(\d+)/);
  if (!match) {
    alert('Open a Vikunja task first — its URL must look like /tasks/123. ' +
          'A board view (/projects/2/11) is not a task.');
    return;
  }

  // The launch page POSTs same-origin once it loads, so no CORS is involved.
  window.open(service + '/task/' + match[1] + '/launch', '_blank');
})();
