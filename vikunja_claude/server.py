"""HTTP front end: loopback only, fixed routes, no shell exposure.

The only value taken from a request is a ticket number, matched as ``\\d+`` by
the router. Nothing from a request ever reaches a shell.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import web
from .config import Config, ConfigError
from .launcher import AlreadyRunning, LaunchError, Launcher
from .service import TicketService
from .vikunja import AmbiguousTicket, TicketNotFound, VikunjaClient, VikunjaError

def bookmarklet_for(service_origin: str) -> str:
    """One-click launcher, pointed at whichever origin served this page.

    Only /tasks/<id> is treated as a task. /projects/<id>/<viewId> is a board
    view — reading an id from it would launch the wrong ticket.
    """
    return (
        "javascript:(function(){"
        f"var s='{service_origin}';"
        "var m=location.pathname.match(/\\/tasks\\/(\\d+)/);"
        "if(!m){alert('Open a Vikunja task first — its URL must look like "
        "/tasks/123. A board view (/projects/2/11) is not a task.');return;}"
        "window.open(s+'/task/'+m[1]+'/launch','_blank');"
        "})();"
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "vikunja-claude"
    service: TicketService  # injected on the server instance

    # -- plumbing ----------------------------------------------------------

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        sys.stderr.write(
            "%s - %s\n" % (self.log_date_time_string(), format % args)
        )

    def _wants_json(self) -> bool:
        if "application/json" in (self.headers.get("Accept") or ""):
            return True
        return self.path.split("?", 1)[-1].find("format=json") >= 0

    def _send(self, status: int, body: str, content_type: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data, indent=2, default=str), "application/json")

    def _html(self, status: int, markup: str) -> None:
        self._send(status, markup, "text/html")

    def _fail(self, status: int, message: str) -> None:
        if self._wants_json():
            self._json(status, {"error": message})
        else:
            self._html(status, web.error_page(status, message))

    # -- routing -----------------------------------------------------------

    ROUTES_GET = (
        (re.compile(r"^/$"), "index"),
        (re.compile(r"^/health$"), "health"),
        (re.compile(r"^/bookmarklet$"), "bookmarklet"),
        # Tampermonkey only offers to install from a .user.js URL.
        (re.compile(r"^/userscript(?:\.user\.js)?$"), "userscript"),
        (re.compile(r"^/launches$"), "launches"),
        (re.compile(r"^/next$"), "next"),
        (re.compile(r"^/task/(\d+)$"), "task"),
        (re.compile(r"^/task/(\d+)/launch$"), "task_launch_page"),
        (re.compile(r"^/ticket/(\d+)$"), "ticket"),
    )
    ROUTES_POST = (
        (re.compile(r"^/next/work$"), "work_next"),
        (re.compile(r"^/task/(\d+)/work$"), "work_task"),
        (re.compile(r"^/ticket/(\d+)/work$"), "work_ticket"),
    )

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch(self.ROUTES_GET)

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch(self.ROUTES_GET)

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch(self.ROUTES_POST)

    def _dispatch(self, routes) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        for pattern, name in routes:
            match = pattern.match(path)
            if match:
                try:
                    getattr(self, f"handle_{name}")(*match.groups())
                except TicketNotFound as exc:
                    self._fail(404, str(exc))
                except AmbiguousTicket as exc:
                    self._fail(409, str(exc))
                except AlreadyRunning as exc:
                    self._fail(409, str(exc))
                except VikunjaError as exc:
                    self._fail(502, str(exc))
                except LaunchError as exc:
                    self._fail(500, str(exc))
                return
        self._fail(404, f"No route for {self.command} {path}")

    # -- handlers ----------------------------------------------------------

    def handle_index(self) -> None:
        config = self.service.config
        self._html(
            200,
            web.console(
                project=config.project_title,
                workdir=str(config.workdir),
                running=self.service.launcher.running(),
                recent=self.service.launcher.recent(15),
            ),
        )

    def handle_health(self) -> None:
        config = self.service.config
        status = {
            "status": "ok",
            "project": config.project_title,
            "workdir": str(config.workdir),
            "vikunja": config.api_url,
            "running": self.service.launcher.running(),
        }
        try:
            status["project_id"] = self.service.client.project_id(
                config.project_title, config.project_id
            )
        except VikunjaError as exc:
            status["status"] = "degraded"
            status["vikunja_error"] = str(exc)
            self._json(503, status)
            return
        self._json(200, status)

    def _origin(self) -> str:
        """The origin the browser used, so generated links work over Tailscale."""
        host = self.headers.get("Host")
        if not host:
            config = self.service.config
            return f"http://{config.host}:{config.port}"
        # Host carries the port, so strip it before judging the hostname.
        hostname = re.sub(r":\d+$", "", host)
        scheme = self.headers.get("X-Forwarded-Proto") or (
            "https" if hostname.endswith(".ts.net") else "http"
        )
        return f"{scheme}://{host}"

    def handle_bookmarklet(self) -> None:
        origin = self._origin()
        self._html(200, web.bookmarklet_page(bookmarklet_for(origin), origin))

    def handle_userscript(self) -> None:
        origin = self._origin()
        # Vikunja sits on the same host as the launcher but on the default
        # port (Tailscale Serve proxies / → Vikunja, :3460 → this service).
        vikunja_origins = [self.service.config.frontend_url]
        without_port = re.sub(r":\d+$", "", origin)
        if without_port != origin:
            vikunja_origins.append(without_port)
        self._send(
            200, web.userscript(origin, vikunja_origins), "text/javascript"
        )

    def handle_launches(self) -> None:
        self._json(200, {"launches": self.service.launcher.recent(50)})

    def _preview(self, ticket) -> None:
        data = self.service.preview(ticket)
        if self._wants_json():
            self._json(200, data)
        else:
            self._html(200, web.ticket_page(data))

    def handle_task(self, task_id: str) -> None:
        self._preview(self.service.get_task(int(task_id)))

    def handle_ticket(self, number: str) -> None:
        """#NN is a convenience: resolve it, then show the canonical task URL."""
        ticket = self.service.get(int(number))
        if self._wants_json():
            self._json(200, self.service.preview(ticket))
            return
        self.send_response(302)
        self.send_header("Location", f"/task/{ticket.task_id}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def handle_next(self) -> None:
        self._preview(self.service.next_ready())

    def handle_task_launch_page(self, task_id: str) -> None:
        """Landing page for the browser button: launches on load, same-origin.

        The button navigates here cross-origin (always allowed); the POST that
        actually launches is same-origin, so no CORS is involved.
        """
        ticket = self.service.get_task(int(task_id))
        self._html(
            200,
            web.launch_page(
                number=ticket.task_number,
                task_id=ticket.task_id,
                reference=ticket.board_reference,
                summary=ticket.summary,
                vikunja_url=ticket.url(self.service.config.frontend_url),
            ),
        )

    def handle_work_task(self, task_id: str) -> None:
        self._json(202, self.service.work(self.service.get_task(int(task_id))))

    def handle_work_ticket(self, number: str) -> None:
        self._json(202, self.service.work(self.service.get(int(number))))

    def handle_work_next(self) -> None:
        self._json(202, self.service.work(self.service.next_ready()))


def build_server(config: Config) -> ThreadingHTTPServer:
    if config.host not in ("127.0.0.1", "::1", "localhost"):
        raise ConfigError(
            f"Refusing to bind to {config.host!r}: this service launches local "
            "processes and must stay on loopback."
        )
    client = VikunjaClient(config.api_url, config.token)
    service = TicketService(config, client, Launcher(config))
    handler = type("BoundHandler", (Handler,), {"service": service})
    return ThreadingHTTPServer((config.host, config.port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vikunja → Claude Code launcher")
    parser.add_argument("--host", help="override VIKUNJA_CLAUDE_HOST")
    parser.add_argument("--port", type=int, help="override VIKUNJA_CLAUDE_PORT")
    args = parser.parse_args(argv)

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.host or args.port:
        config = Config(
            **{
                **{
                    field: getattr(config, field)
                    for field in config.__dataclass_fields__
                },
                **({"host": args.host} if args.host else {}),
                **({"port": args.port} if args.port else {}),
            }
        )

    try:
        server = build_server(config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    print(
        f"vikunja-claude listening on http://{config.host}:{config.port} "
        f"— project {config.project_title!r}, workdir {config.workdir}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
