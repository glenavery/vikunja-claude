// "Work with Claude" bookmarklet — readable source.
// The minified one-liner served at http://127.0.0.1:3460/bookmarklet is this,
// and that page is the easiest way to install it.
//
// Reads the #NN ticket number from the open Vikunja task and opens the local
// launcher's preview page for it. It never launches anything by itself.

javascript: (function () {
  var service = 'http://127.0.0.1:3460';

  // Vikunja renders the task title in an h1; document.title carries it too.
  var heading = document.querySelector('h1, .task-heading, .task-title');
  var haystack = [
    (heading && heading.textContent) || '',
    document.title,
    window.getSelection ? String(window.getSelection()) : ''
  ].join(' ');

  var match = haystack.match(/#(\d+)/);
  if (!match) {
    alert('No #NN ticket number found on this page.');
    return;
  }
  window.open(service + '/ticket/' + match[1], '_blank');
})();
