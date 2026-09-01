"""The one-click browser flow: task-id routes, launch page, button sources."""

from __future__ import annotations

import json
import re
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from vikunja_claude import web
from vikunja_claude.mcp_service import McpService
from vikunja_claude.server import Handler, bookmarklet_for

from .fakes import task
from .support import ServiceTestCase, make_mcp_config


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
        self.assertEqual(data["number"], 8)
        self.assertEqual(data["reference"], "#8")
        self.assertEqual(self.spawn.calls, [])

    def test_task_work_launches(self):
        status, body, _ = self.fetch(
            "/task/9/work", method="POST", accept="application/json"
        )
        self.assertEqual(status, 202)
        data = json.loads(body)
        self.assertEqual(data["number"], 8)
        self.assertEqual(len(self.spawn.calls), 1)
        # The row-id ROUTE stays — a browser sitting on Vikunja's /tasks/<id>
        # page has only that number. What comes back names the board (task 659).
        self.assertNotIn("task_id", data)
        self.assertNotIn(9, [v for v in data.values() if isinstance(v, int)])

    def test_unknown_task_id_is_404(self):
        status, body, _ = self.fetch("/task/4242", accept="application/json")
        self.assertEqual(status, 404)
        self.assertIn("board view", json.loads(body)["error"])

    def test_ticket_number_redirects_to_the_canonical_task_url(self):
        status, _, headers = self.fetch("/ticket/8")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/task/9")

    def test_ticket_number_still_serves_json_directly(self):
        status, body, _ = self.fetch("/ticket/8", accept="application/json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["number"], 8)


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
        self.assertIn("fetch('/ticket/8/work'", body)
        self.assertIn("method: 'POST'", body)
        self.assertNotIn("fetch('http", body)

    def test_launch_page_reports_the_three_outcomes(self):
        _, body, _ = self.fetch("/task/9/launch")
        self.assertIn("202", body)
        self.assertIn("409", body)
        self.assertIn("Already running", body)

    def test_launch_page_links_no_row_id_back_to_vikunja(self):
        """It carried `<a href=".../tasks/9">back to Vikunja</a>` — the href
        was the row id, and the visible text hid that (task 663). The reader
        arrived from Vikunja's own task page, so Back returns there.
        """
        _, body, _ = self.fetch("/task/9/launch")
        self.assertNotIn("http://127.0.0.1:3456/tasks/9", body)
        self.assertNotIn("/tasks/9", body)
        self.assertIn("#8", body, "the page still names the ticket")

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


class OneIdentityFromTheButtonToTheLaunch(HttpFlow):
    """Task 748: the path the launch page renders must resolve the ticket it names.

    The defect this pins was split across two green tests. One asserted the
    page emits ``fetch('/ticket/8/work')``; another posted ``/ticket/33/work``,
    the legacy title prefix. Nothing ever posted the path the page actually
    renders, so the resolver behind it could answer a different scheme
    entirely -- and did. Every link this service renders had moved to the board
    number (task 659) while `/ticket/{n}` still resolved the ``#NN`` title
    prefix, and no AI Alpha board has carried one since 2026-07-26. So the
    button 404ed on every task: ``No ticket #714 in this project`` for the
    task the board shows as #714.
    """

    def _launched_rows(self) -> set[int]:
        """Which rows hold a launch lock. The lock is keyed on the row id, so
        this says *which task* ran rather than what the answer called it."""
        return {
            int(path.stem.removeprefix("task-"))
            for path in self.config.lock_dir.glob("task-*.json")
        }

    def _work_path_the_page_renders(self, row_id: int) -> str:
        _, body, _ = self.fetch(f"/task/{row_id}/launch")
        match = re.search(r"fetch\('([^']+)'", body)
        self.assertIsNotNone(match, "the launch page fires no POST at all")
        return match.group(1)

    def test_the_page_launches_through_the_path_it_renders(self):
        """The join the suite was missing: render, then follow what was rendered."""
        path = self._work_path_the_page_renders(9)
        status, body, _ = self.fetch(path, method="POST", accept="application/json")

        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body)["reference"], "#8")
        self.assertEqual(self._launched_rows(), {9})

    def test_a_task_carrying_no_legacy_prefix_launches(self):
        """The live boards' actual shape, and the reported failure.

        Row id 78, board number 77, and no ``#NN`` anywhere in the title -- so
        a resolver reading the prefix has nothing to find and reports the task
        absent. This is #714 in miniature.
        """
        self.vikunja.layout["Ready"].append(
            task(78, "Add dependency vulnerability scanning",
                 "2026-07-26T06:00:00Z", index=77)
        )
        path = self._work_path_the_page_renders(78)
        self.assertEqual(path, "/ticket/77/work")

        status, body, _ = self.fetch(path, method="POST", accept="application/json")
        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body)["reference"], "#77")
        self.assertEqual(self._launched_rows(), {78})

    def test_the_number_in_the_path_is_never_read_as_a_row_id(self):
        """Board number 9 is row 10, and row 9 is board number 8.

        Both exist, which is what the fixture is built for: a resolver falling
        through to the row id would launch a real, plausible, wrong ticket
        instead of failing where anyone could see it.
        """
        status, body, _ = self.fetch(
            "/ticket/9/work", method="POST", accept="application/json"
        )
        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body)["reference"], "#9")
        self.assertEqual(self._launched_rows(), {10})

    def test_an_unknown_board_number_is_a_404_that_launches_nothing(self):
        status, _, _ = self.fetch(
            "/ticket/4242/work", method="POST", accept="application/json"
        )
        self.assertEqual(status, 404)
        self.assertEqual(self._launched_rows(), set())


class TheTwoLaunchSurfacesNameTheSameTicket(HttpFlow):
    """Task 748: the browser button and the MCP runner agree on identity.

    They are reached differently -- the button from a ``/tasks/<id>`` URL the
    reader already has open, the connector from a ``#N`` read off a card -- and
    they may resolve differently *on the way in*. What they may not do is
    disagree about which ticket a board number names.
    """

    def setUp(self) -> None:
        super().setUp()
        self.mcp = McpService(make_mcp_config(self.state_dir), self.client)

    def test_one_board_number_names_one_task_on_both_surfaces(self):
        _, body, _ = self.fetch("/ticket/8", accept="application/json")
        launcher = json.loads(body)
        connector = self.mcp.get_task(task_number=8)

        self.assertEqual(launcher["number"], connector["task_number"])
        self.assertEqual(launcher["reference"], connector["reference"])
        self.assertEqual(launcher["title"], connector["title"])

    def test_the_number_they_agree_on_is_the_board_number_not_the_row_id(self):
        """Row 9 is board number 8, so agreeing on "9" would be agreeing wrongly."""
        _, body, _ = self.fetch("/ticket/8", accept="application/json")
        self.assertEqual(json.loads(body)["number"], 8)
        self.assertEqual(self.mcp.get_task(task_number=8)["task_number"], 8)

        # And the row id resolves neither surface: 9 is a different ticket on
        # both, not the same one under another name.
        _, other, _ = self.fetch("/ticket/9", accept="application/json")
        self.assertNotEqual(json.loads(other)["title"], json.loads(body)["title"])


if __name__ == "__main__":
    unittest.main()
