"""The one-click browser flow: task-id routes, launch page, button sources."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from vikunja_claude import web
from vikunja_claude.server import Handler, bookmarklet_for

from .support import ServiceTestCase


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class HttpFlow(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        handler = type("BoundHandler", (Handler,), {"service": self.service})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.httpd.server_close)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.opener = urllib.request.build_opener(NoRedirect)

    def fetch(self, path, method="GET", accept=None, host=None):
        headers = {}
        if accept:
            headers["Accept"] = accept
        if host:
            headers["Host"] = host
        request = urllib.request.Request(
            self.base + path, method=method, headers=headers
        )
        try:
            with self.opener.open(request, timeout=5) as response:
                return response.status, response.read().decode(), response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(), exc.headers


class TaskIdRoutes(HttpFlow):
    def test_task_route_previews_without_launching(self):
        status, body, _ = self.fetch("/task/9", accept="application/json")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["task_id"], 9)
        self.assertEqual(data["reference"], "#8")
        self.assertEqual(self.spawn.calls, [])

    def test_task_work_launches(self):
        status, body, _ = self.fetch(
            "/task/9/work", method="POST", accept="application/json"
        )
        self.assertEqual(status, 202)
        data = json.loads(body)
        self.assertEqual(data["task_id"], 9)
        self.assertEqual(data["ticket"], 33)
        self.assertEqual(len(self.spawn.calls), 1)

    def test_unknown_task_id_is_404(self):
        status, body, _ = self.fetch("/task/4242", accept="application/json")
        self.assertEqual(status, 404)
        self.assertIn("board view", json.loads(body)["error"])

    def test_ticket_number_redirects_to_the_canonical_task_url(self):
        status, _, headers = self.fetch("/ticket/33")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/task/9")

    def test_ticket_number_still_serves_json_directly(self):
        status, body, _ = self.fetch("/ticket/33", accept="application/json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["task_id"], 9)


class LaunchPage(HttpFlow):
    def test_launch_page_renders_without_launching_server_side(self):
        status, body, _ = self.fetch("/task/9/launch")
        self.assertEqual(status, 200)
        self.assertIn("Launching #8", body)
        # The page only *renders*; the browser fires the POST.
        self.assertEqual(self.spawn.calls, [])

    def test_launch_page_posts_same_origin_so_no_cors_is_needed(self):
        _, body, _ = self.fetch("/task/9/launch")
        # The fetch target must be a relative path, not an absolute origin.
        self.assertIn("fetch('/task/9/work'", body)
        self.assertIn("method: 'POST'", body)
        self.assertNotIn("fetch('http", body)

    def test_launch_page_reports_the_three_outcomes(self):
        _, body, _ = self.fetch("/task/9/launch")
        self.assertIn("202", body)
        self.assertIn("409", body)
        self.assertIn("Already running", body)

    def test_launch_page_links_back_to_vikunja(self):
        _, body, _ = self.fetch("/task/9/launch")
        self.assertIn("http://127.0.0.1:3456/tasks/9", body)

    def test_unknown_task_launch_page_is_404_and_launches_nothing(self):
        status, _, _ = self.fetch("/task/4242/launch")
        self.assertEqual(status, 404)
        self.assertEqual(self.spawn.calls, [])


class GeneratedButtons(HttpFlow):
    def test_bookmarklet_only_accepts_a_task_url(self):
        code = bookmarklet_for("http://127.0.0.1:3460")
        self.assertIn("/tasks/", code)
        self.assertIn("location.pathname.match", code)
        # A board view id must never be treated as a task id.
        self.assertNotIn("/projects/", code.split("alert(")[0])

    def test_bookmarklet_opens_the_launch_endpoint(self):
        code = bookmarklet_for("https://host.ts.net:3460")
        self.assertIn("https://host.ts.net:3460", code)
        self.assertIn("'/task/'+m[1]+'/launch'", code)

    def test_bookmarklet_warns_about_board_views(self):
        self.assertIn("/projects/2/11", bookmarklet_for("http://x"))

    def test_bookmarklet_page_uses_the_host_the_browser_used(self):
        _, body, _ = self.fetch("/bookmarklet", host="aiserver.tail36601d.ts.net:3460")
        self.assertIn("https://aiserver.tail36601d.ts.net:3460", body)
        self.assertNotIn("javascript:(function(){var s='http://127.0.0.1:3460'", body)

    def test_userscript_is_served_as_javascript(self):
        status, body, headers = self.fetch("/userscript")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/javascript"))
        self.assertIn("==UserScript==", body)

    def test_user_js_alias_serves_the_same_script(self):
        """Tampermonkey only offers to install from a .user.js URL."""
        status, body, headers = self.fetch("/userscript.user.js")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/javascript"))
        self.assertIn("==UserScript==", body)
        _, plain, _ = self.fetch("/userscript")
        self.assertEqual(body, plain)

    def test_userscript_matches_the_vikunja_origin_not_the_launcher(self):
        _, body, _ = self.fetch("/userscript", host="aiserver.tail36601d.ts.net:3460")
        self.assertIn("@match        https://aiserver.tail36601d.ts.net/*", body)
        self.assertIn("const SERVICE = 'https://aiserver.tail36601d.ts.net:3460'", body)

    def test_userscript_only_acts_on_task_urls(self):
        script = web.userscript("http://127.0.0.1:3460", ["http://127.0.0.1:3456"])
        self.assertIn("location.pathname.match(/\\/tasks\\/(\\d+)/)", script)
        self.assertIn("board view", script)

    def test_userscript_handles_spa_navigation(self):
        script = web.userscript("http://x", ["http://y"])
        self.assertIn("MutationObserver", script)
        self.assertIn("removeButton", script)


if __name__ == "__main__":
    unittest.main()
