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
from .executors import DEFAULT_EXECUTOR, Executor
from .vikunja import Ticket


class LaunchError(RuntimeError):
    pass


class AlreadyRunning(LaunchError):
    """A Claude run for this task is already in flight."""

    def __init__(self, task_id: int, reference: str, pid: int, started_at: str):
        # `task_id` is the lock key and stays an attribute for a caller that
        # holds one; the MESSAGE names the board (task 659). A row id printed
        # beside a board number is two numbers a reader can quote, and the one
        # they would quote is the wrong one.
        super().__init__(
            f"Claude is already working {reference} "
            f"(pid {pid}, started {started_at}). "
            "Refusing to launch a second run."
        )
        self.task_id = task_id
        self.reference = reference
        self.pid = pid
        self.started_at = started_at


@dataclass(frozen=True)
class LaunchRecord:
    """What a launch publishes about itself.

    The row id is deliberately NOT here (task 659). This record is spread
    straight into the `work` response and into `/launches`, and it was the last
    payload handing a caller the immutable `/tasks/<id>` number beside the board
    number that identifies the ticket. The id is still the lock key and the log
    filename — internal, where it is load-bearing — and `running()` recovers it
    from the lock's own filename rather than from its contents.
    """

    number: int | None
    reference: str
    pid: int
    started_at: str
    log_file: str
    workdir: str
    #: Which executor ran, and the model it drove (None for the harness's own
    #: default). Published because "which model wrote this commit" is the first
    #: question anyone asks of a finished run, and the log is the only place
    #: that can still answer it afterwards.
    executor: str = DEFAULT_EXECUTOR
    model: str | None = None


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

    def _lock_path(self, task_id: int) -> Path:
        # Keyed by the immutable task id, never by the editable #NN prefix.
        return self.config.lock_dir / f"task-{task_id}.json"

    def _read_lock(self, task_id: int) -> dict | None:
        path = self._lock_path(task_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def active_launch(self, task_id: int) -> dict | None:
        """The live lock for this task, clearing it if the PID is gone."""
        lock = self._read_lock(task_id)
        if lock is None:
            return None
        if self._is_alive(int(lock.get("pid", -1))):
            return lock
        self._release(task_id)
        return None

    def _acquire(self, task_id: int, reference: str, payload: dict) -> None:
        path = self._lock_path(task_id)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            existing = self._read_lock(task_id) or {}
            raise AlreadyRunning(
                task_id,
                reference,
                int(existing.get("pid", -1)),
                str(existing.get("started_at", "unknown")),
            ) from None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def _release(self, task_id: int) -> None:
        self._lock_path(task_id).unlink(missing_ok=True)

    def running(self) -> list[dict]:
        live = []
        for path in sorted(self.config.lock_dir.glob("task-*.json")):
            try:
                task_id = int(path.stem.removeprefix("task-"))
            except ValueError:
                continue
            lock = self.active_launch(task_id)
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

    def launch(
        self, ticket: Ticket, prompt: str, executor: Executor | None = None
    ) -> LaunchRecord:
        """One run for one ticket.

        ``executor`` decides only which model the launched Claude Code drives,
        as extra environment for the child. Everything else about a launch — the
        binary, the arguments, the working directory, the lock, the log, the
        reap — is the same whichever model is behind it, which is why there is
        one launch path rather than one per model.
        """
        executor = executor or Executor(name=DEFAULT_EXECUTOR)
        with self._mutex:
            existing = self.active_launch(ticket.task_id)
            if existing is not None:
                raise AlreadyRunning(
                    ticket.task_id,
                    ticket.board_reference,
                    int(existing.get("pid", -1)),
                    str(existing.get("started_at", "unknown")),
                )

            started = time.strftime(
                "%Y-%m-%dT%H:%M:%S%z", time.localtime(self._clock())
            )
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._clock()))
            log_file = self.config.run_log_dir / f"task-{ticket.task_id}-{stamp}.log"

            # Placeholder lock: claims the slot before the process exists, so two
            # concurrent requests cannot both reach spawn.
            self._acquire(
                ticket.task_id,
                ticket.board_reference,
                {
                    "number": ticket.task_number,
                    "reference": ticket.board_reference,
                    "pid": os.getpid(),
                    "started_at": started,
                    "log_file": str(log_file),
                    "state": "starting",
                    "executor": executor.name,
                    "model": executor.model,
                },
            )

            try:
                handle = log_file.open("w", encoding="utf-8")
                process = self._spawn(
                    [self.config.claude_bin, *self.config.claude_args, prompt],
                    cwd=str(self.config.workdir),
                    env=self._child_env(ticket, executor),
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                self._release(ticket.task_id)
                self._log(
                    "launch_failed",
                    number=ticket.task_number,
                    reference=ticket.board_reference,
                    error=str(exc),
                )
                raise LaunchError(
                    f"Could not start {self.config.claude_bin!r}: {exc}"
                ) from exc

            record = LaunchRecord(
                number=ticket.task_number,
                reference=ticket.board_reference,
                pid=process.pid,
                started_at=started,
                log_file=str(log_file),
                workdir=str(self.config.workdir),
                executor=executor.name,
                model=executor.model,
            )
            self._write_lock(ticket.task_id, {**asdict(record), "state": "running"})
            self._log("launched", **asdict(record))

        if self._reap:
            threading.Thread(
                target=self._wait_and_log,
                args=(ticket.task_id, ticket.board_reference, process),
                daemon=True,
                name=f"reaper-task-{ticket.task_id}",
            ).start()
        return record

    def _write_lock(self, task_id: int, payload: dict) -> None:
        self._lock_path(task_id).write_text(json.dumps(payload), encoding="utf-8")

    def _child_env(self, ticket: Ticket, executor: Executor) -> dict[str, str]:
        # The executor is applied FIRST, so what it removes is removed from what
        # the service inherited, and what the runner sets below cannot be
        # overwritten by a model's environment.
        env = executor.apply(dict(os.environ))
        env.update(
            {
                "VIKUNJA_API_URL": self.config.api_url,
                "VIKUNJA_API_TOKEN": self.config.token,
                "VIKUNJA_PROJECT": self.config.project_title,
            }
        )
        # The board number, and only where there is one. This handed the run
        # VIKUNJA_TASK_ID (the row id) and VIKUNJA_TICKET (the dead legacy
        # prefix); nothing read either, so they were two numbers of exposure
        # bought for nothing (task 659).
        if ticket.task_number is not None:
            env["VIKUNJA_TASK_NUMBER"] = str(ticket.task_number)
        return env

    def _wait_and_log(
        self, task_id: int, reference: str, process: subprocess.Popen
    ) -> None:
        # The id releases the lock; the reference is what the log says, because
        # `recent()` is rendered on the console a human reads (task 659).
        try:
            exit_status = process.wait(timeout=self.config.launch_timeout_seconds)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            exit_status = None
            self._log(
                "timeout",
                reference=reference,
                pid=process.pid,
                after_seconds=self.config.launch_timeout_seconds,
            )
        finally:
            self._release(task_id)
        self._log(
            "finished",
            reference=reference,
            pid=process.pid,
            exit_status=exit_status,
        )
