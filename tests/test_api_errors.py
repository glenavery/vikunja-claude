"""Vikunja API error handling, at the client and over HTTP."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from vikunja_claude.server import Handler
from vikunja_claude.vikunja import VikunjaClient, VikunjaError

from .fakes import FakeVikunja
from .support import ServiceTestCase


class ClientErrorHandling(ServiceTestCase):
    def test_http_error_from_vikunja_surfaces_as_vikunja_error(self):
        self.vikunja.fail = VikunjaError("HTTP 401: bad token", status=401)
        with self.assertRaises(VikunjaError) as caught:
            self.service.get(33)
        self.assertEqual(caught.exception.status, 401)

    def test_unreachable_vikunja_surfaces_as_vikunja_error(self):
        self.vikunja.fail = VikunjaError("Cannot reach Vikunja: refused")
        with self.assertRaises(VikunjaError):
            self.service.next_ready()

    def test_a_failed_bucket_move_never_launches_claude(self):
        ticket = self.service.get(33)
        self.vikunja.fail = VikunjaError("HTTP 500: boom", status=500)
        with self.assertRaises(VikunjaError):
            self.service.work(ticket)
        self.assertEqual(self.spawn.calls, [])
        self.assertIsNone(self.launcher.active_launch(33))

    def test_missing_project_names_what_it_looked_for(self):
        client = VikunjaClient("http://x/api/v1", "t", transport=self.vikunja)
        with self.assertRaises(VikunjaError) as caught:
            client.project_id("Nonexistent Project")
        self.assertIn("Nonexistent Project", str(caught.exception))
        self.assertIn("AI Alpha Engine", str(caught.exception))

    def test_project_without_a_kanban_view_is_an_error(self):
        def transport(method, path, body=None):
            if path == "/projects/2":
                return {"id": 2, "views": [{"id": 1, "view_kind": "list"}]}
            return self.vikunja(method, path, body)

        client = VikunjaClient("http://x/api/v1", "t", transport=transport)
        with self.assertRaises(VikunjaError) as caught:
            client.kanban_view_id(2)
        self.assertIn("no kanban view", str(caught.exception))

    def test_unknown_bucket_lists_the_known_ones(self):
        with self.assertRaises(VikunjaError) as caught:
            self.client.move_to_bucket(2, 12, 9, "Nowhere")
        self.assertIn("Nowhere", str(caught.exception))
        self.assertIn("Ready", str(caught.exception))

    def test_non_json_response_is_reported_clearly(self):
        client = VikunjaClient("http://127.0.0.1:1/api/v1", "t")
        with self.assertRaises(VikunjaError) as caught:
            client.call("GET", "/projects")
        self.assertIn("Cannot reach Vikunja", str(caught.exception))


class HttpErrorMapping(ServiceTestCase):
    """The HTTP layer must translate failures into honest status codes."""

    def setUp(self) -> None:
        super().setUp()
        handler = type("BoundHandler", (Handler,), {"service": self.service})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.httpd.server_close)
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.httpd.shutdown)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def get(self, path, method="GET"):
        request = urllib.request.Request(
            self.base + path, method=method, headers={"Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_unknown_ticket_is_404(self):
        status, body = self.get("/ticket/999")
        self.assertEqual(status, 404)
        self.assertIn("No ticket #999", body["error"])

    def test_vikunja_failure_is_502_not_500(self):
        self.vikunja.fail = VikunjaError("HTTP 401: bad token", status=401)
        status, body = self.get("/ticket/33")
        self.assertEqual(status, 502)
        self.assertIn("401", body["error"])

    def test_duplicate_launch_is_409(self):
        self.get("/ticket/33/work", method="POST")
        self.alive_pids.add(4242)
        status, body = self.get("/ticket/33/work", method="POST")
        self.assertEqual(status, 409)
        self.assertIn("already working ticket #33", body["error"])

    def test_successful_launch_is_202(self):
        status, body = self.get("/ticket/33/work", method="POST")
        self.assertEqual(status, 202)
        self.assertEqual(body["ticket"], 33)

    def test_unknown_route_is_404(self):
        status, _ = self.get("/nope")
        self.assertEqual(status, 404)

    def test_non_numeric_ticket_does_not_reach_the_service(self):
        status, _ = self.get("/ticket/33;rm%20-rf%20~")
        self.assertEqual(status, 404)
        self.assertEqual(self.spawn.calls, [])

    def test_health_reports_degraded_when_vikunja_is_down(self):
        self.vikunja.fail = VikunjaError("Cannot reach Vikunja: refused")
        status, body = self.get("/health")
        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "degraded")

    def test_health_is_ok_when_vikunja_answers(self):
        status, body = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["project_id"], 2)

    def test_next_resolves_the_oldest_ready_ticket(self):
        status, body = self.get("/next")
        self.assertEqual(status, 200)
        self.assertEqual(body["ticket"], 33)


class EmptyVikunja(ServiceTestCase):
    def test_no_tickets_at_all_is_not_found_not_a_crash(self):
        self.vikunja = FakeVikunja(
            layout={"Backlog": [], "Ready": [], "In Progress": [],
                    "Waiting": [], "Done": []}
        )
        client = VikunjaClient("http://x/api/v1", "t", transport=self.vikunja)
        with self.assertRaises(VikunjaError):
            client.oldest_ready_ticket(2, 12)


if __name__ == "__main__":
    unittest.main()
