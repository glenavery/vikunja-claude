"""Launch Claude Code for one ticket, at most once at a time.

Concurrency is guarded by a per-ticket lock file created with O_EXCL, so the
guard survives a restart of this service. A lock whose PID is no longer alive
is stale and gets reclaimed.

The launched process receives the Vikunja token through its environment only —
never on the command line and never inside the prompt.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable

from .config import Config
from .vikunja import Ticket


class LaunchError(RuntimeError):
    pass


class AlreadyRunning(LaunchError):
    """A Claude run for this ticket is already in flight."""

    def __init__(self, ticket_number: int, pid: int, started_at: str):
        super().__init__(
            f"Claude is already working ticket #{ticket_number} "
            f"(pid {pid}, started {started_at}). Refusing to launch a second run."
        )
        self.ticket_number = ticket_number
        self.pid = pid
        self.started_at = started_at


@dataclass(frozen=True)
class LaunchRecord:
    ticket: int
    task_id: int
    pid: int
    started_at: str
    log_file: str
    workdir: str


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Launcher:
    def __init__(
        self,
        config: Config,
        spawn: Callable[..., subprocess.Popen] = subprocess.Popen,
        is_alive: Callable[[int], bool] = pid_alive,
        clock: Callable[[], float] = time.time,
        reap: bool = True,
    ):
        self.config = config
        self._spawn = spawn
        self._is_alive = is_alive
        self._clock = clock
        self._reap = reap
        self._mutex = threading.Lock()
        for directory in (config.state_dir, config.lock_dir, config.run_log_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # -- locks -------------------------------------------------------------

    def _lock_path(self, ticket_number: int) -> Path:
        return self.config.lock_dir / f"{ticket_number}.json"

    def _read_lock(self, ticket_number: int) -> dict | None:
        path = self._lock_path(ticket_number)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def active_launch(self, ticket_number: int) -> dict | None:
        """The live lock for this ticket, clearing it if the PID is gone."""
        lock = self._read_lock(ticket_number)
        if lock is None:
            return None
        if self._is_alive(int(lock.get("pid", -1))):
            return lock
        self._release(ticket_number)
        return None

    def _acquire(self, ticket_number: int, payload: dict) -> None:
        path = self._lock_path(ticket_number)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            existing = self._read_lock(ticket_number) or {}
            raise AlreadyRunning(
                ticket_number,
                int(existing.get("pid", -1)),
                str(existing.get("started_at", "unknown")),
            ) from None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def _release(self, ticket_number: int) -> None:
        self._lock_path(ticket_number).unlink(missing_ok=True)

    def running(self) -> list[dict]:
        live = []
        for path in sorted(self.config.lock_dir.glob("*.json")):
            try:
                number = int(path.stem)
            except ValueError:
                continue
            lock = self.active_launch(number)
            if lock:
                live.append(lock)
        return live

    # -- logging -----------------------------------------------------------

    def _log(self, event: str, **fields) -> None:
        record = {
            "event": event,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self._clock())),
            **fields,
        }
        self.config.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.config.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def recent(self, limit: int = 25) -> list[dict]:
        try:
            lines = self.config.log_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return list(reversed(out))

    # -- launching ---------------------------------------------------------

    def launch(self, ticket: Ticket, prompt: str) -> LaunchRecord:
        with self._mutex:
            existing = self.active_launch(ticket.number)
            if existing is not None:
                raise AlreadyRunning(
                    ticket.number,
                    int(existing.get("pid", -1)),
                    str(existing.get("started_at", "unknown")),
                )

            started = time.strftime(
                "%Y-%m-%dT%H:%M:%S%z", time.localtime(self._clock())
            )
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._clock()))
            log_file = self.config.run_log_dir / f"ticket-{ticket.number}-{stamp}.log"

            # Placeholder lock: claims the slot before the process exists, so two
            # concurrent requests cannot both reach spawn.
            self._acquire(
                ticket.number,
                {
                    "ticket": ticket.number,
                    "task_id": ticket.task_id,
                    "pid": os.getpid(),
                    "started_at": started,
                    "log_file": str(log_file),
                    "state": "starting",
                },
            )

            try:
                handle = log_file.open("w", encoding="utf-8")
                process = self._spawn(
                    [self.config.claude_bin, *self.config.claude_args, prompt],
                    cwd=str(self.config.workdir),
                    env=self._child_env(ticket),
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                self._release(ticket.number)
                self._log(
                    "launch_failed", ticket=ticket.number, error=str(exc)
                )
                raise LaunchError(
                    f"Could not start {self.config.claude_bin!r}: {exc}"
                ) from exc

            record = LaunchRecord(
                ticket=ticket.number,
                task_id=ticket.task_id,
                pid=process.pid,
                started_at=started,
                log_file=str(log_file),
                workdir=str(self.config.workdir),
            )
            self._acquire_overwrite(ticket.number, {**asdict(record), "state": "running"})
            self._log("launched", **asdict(record))

        if self._reap:
            threading.Thread(
                target=self._wait_and_log,
                args=(ticket.number, process),
                daemon=True,
                name=f"reaper-{ticket.number}",
            ).start()
        return record

    def _acquire_overwrite(self, ticket_number: int, payload: dict) -> None:
        self._lock_path(ticket_number).write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def _child_env(self, ticket: Ticket) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "VIKUNJA_API_URL": self.config.api_url,
                "VIKUNJA_API_TOKEN": self.config.token,
                "VIKUNJA_PROJECT": self.config.project_title,
                "VIKUNJA_TICKET": str(ticket.number),
                "VIKUNJA_TASK_ID": str(ticket.task_id),
            }
        )
        return env

    def _wait_and_log(self, ticket_number: int, process: subprocess.Popen) -> None:
        try:
            exit_status = process.wait(timeout=self.config.launch_timeout_seconds)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            exit_status = None
            self._log(
                "timeout",
                ticket=ticket_number,
                pid=process.pid,
                after_seconds=self.config.launch_timeout_seconds,
            )
        finally:
            self._release(ticket_number)
        self._log(
            "finished",
            ticket=ticket_number,
            pid=process.pid,
            exit_status=exit_status,
        )
