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


#: How much of a run's own output a status read hands back. A tail rather than
#: the log: enough to tell model work from tests, git, a closing report or a
#: stall, bounded so a status read cannot be used to pull an arbitrary quantity
#: of a host file through a read surface. Whichever bound bites first wins, so a
#: run emitting very long lines is bounded too.
STATUS_TAIL_LINES = 40
STATUS_TAIL_BYTES = 8000

#: What a run that started can be, once it is no longer running. `lost` is the
#: one worth naming: the process is gone and the runner never recorded how it
#: ended, which is what a restart of this service leaves behind, because the
#: thread that writes the terminal event lives in this process and dies with it.
#: Reporting that as `finished` would claim an exit nobody observed, and as
#: `failed` would claim a failure the runner never saw.
RUN_RUNNING = "running"
RUN_FINISHED = "finished"
RUN_FAILED = "failed"
RUN_LOST = "lost"
RUN_NONE = "none"

#: The event a reconciliation writes for a run nobody saw end (task 754). It is
#: deliberately not `finished` and not `timeout`: both of those are *observed*
#: outcomes, written by the thread that watched the process, and reusing either
#: would record an exit status or a timeout that nothing measured. This one says
#: only what is actually known — the process is gone and the ending was missed.
EVENT_ORPHANED = "orphaned"


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
        """The live lock for this task, reconciling it if the PID is gone.

        The reclaim was always here; what it did not do was say so (task 754).
        A lock released in silence left the launch log with an opening record
        and no closing one, which is indistinguishable from a run still going —
        so the artifact that is supposed to answer "how did this end" answered
        "it has not". It now records the ending it is reclaiming.
        """
        lock = self._read_lock(task_id)
        if lock is None:
            return None
        if self._is_alive(int(lock.get("pid", -1))):
            return lock
        self._reconcile(task_id, lock)
        return None

    def _reconcile(self, task_id: int, lock: dict) -> dict | None:
        """Record that this launch was orphaned, then release its lock.

        Order matters and is the whole of the care here: the event is written
        *before* the lock goes, so a crash between the two leaves the lock —
        which is reconcilable again — rather than a released lock whose ending
        was never recorded, which nothing would ever revisit.

        Returns what was reconciled, or None when the launch already had an
        ending. That second case is not a no-op for nothing: a lock can outlive
        a properly reaped run if the release failed, and reconciling it twice
        would write a second, contradictory ending for a run that finished
        cleanly.
        """
        events = self._events()
        launched, index = self._last_launch(events, task_id)
        finished, _, orphaned = self._terminal(events, launched, index)
        if launched is not None and (finished is not None or orphaned is not None):
            self._release(task_id)
            return None

        record = {
            "number": lock.get("number"),
            "reference": lock.get("reference"),
            "pid": lock.get("pid"),
            "started_at": lock.get("started_at"),
            "executor": lock.get("executor"),
            "model": lock.get("model"),
        }
        self._log(EVENT_ORPHANED, **record)
        self._release(task_id)
        return record

    def reconcile_orphaned_runs(self) -> list[dict]:
        """Every launch this service lost track of, recorded and released.

        Called once at startup, which is where the losses happen: this service
        stopping is what kills the runs (they sit in its control group) and what
        destroys the threads that would have recorded their endings. A run whose
        lock is still held by a live process is left strictly alone — the point
        is to close what ended, never to disturb what is working.

        There is no scheduler behind this and there must not be one. Between
        startups the same reclaim happens whenever anything reads a lock, so a
        run that dies on its own is recorded the next time the launcher looks
        at it rather than waiting for a sweep.
        """
        reconciled = []
        for path in sorted(self.config.lock_dir.glob("task-*.json")):
            try:
                task_id = int(path.stem.removeprefix("task-"))
            except ValueError:
                continue
            lock = self._read_lock(task_id)
            if lock is None or self._is_alive(int(lock.get("pid", -1))):
                continue
            record = self._reconcile(task_id, lock)
            if record is not None:
                reconciled.append(record)
        return reconciled

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

    def _events(self) -> list[dict]:
        """Every launch event, oldest first.

        ``recent()`` answers "what happened lately" for a console and caps to a
        page; this answers "what happened to THIS task", which that cap can push
        out of reach the moment anything else runs. Same file, read whole.
        """
        try:
            lines = self.config.log_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                out.append(record)
        return out

    # -- reading one run ---------------------------------------------------

    def run_status(self, task_id: int) -> dict:
        """What the last run for this task is doing, from what is already kept.

        Nothing new is recorded to answer this. The lock says whether a process
        is alive, the append-only launch log says how the last run for this task
        ended, and the run's own log file says what it was doing — three
        artifacts a launch already writes, read together.

        Read-only in a way the neighbouring reads deliberately are not.
        ``active_launch()`` reclaims a lock whose PID is gone, which is right
        when something is about to launch and wrong here: that lock is the
        evidence that a run was left un-reaped, and a status read that cleared
        it would erase the very thing it was asked about. So the lock is read
        and liveness judged here, and the run's history is taken from the launch
        log, which no read rewrites.

        A run this service never managed to start is not a state here. It has no
        lock, no log and no process, and it was already refused synchronously to
        whoever asked for it — ``launch_failed`` is a record of that refusal, not
        of a run to ask after.
        """
        events = self._events()
        launched, launch_index = self._last_launch(events, task_id)
        finished, timed_out, orphaned = self._terminal(events, launched, launch_index)

        lock = self._read_lock(task_id)
        alive = lock is not None and self._is_alive(int(lock.get("pid", -1)))

        if alive:
            state = RUN_RUNNING
        elif launched is None:
            state = RUN_NONE
        elif finished is not None:
            # An observed ending always wins over a reconciled one. They should
            # never both exist — reconciliation refuses to write over an ending
            # that was actually seen — but if they did, the one somebody watched
            # is the true account.
            state = (
                RUN_FAILED
                if timed_out or finished.get("exit_status") != 0
                else RUN_FINISHED
            )
        else:
            # Started and gone. `lost` either way, and deliberately the same
            # word whether or not it has been reconciled yet: reconciliation
            # records the ending, it does not discover what the ending was.
            # What changes is that it is now a CLOSED fact with a time on it,
            # rather than an open-ended inference from a missing record
            # (task 754).
            state = RUN_LOST

        # The lock is the fallback only for the window between claiming the slot
        # and logging the launch; after it, both say the same thing.
        record = launched or lock or {}
        return {
            "number": record.get("number"),
            "reference": record.get("reference"),
            "state": state,
            "alive": alive,
            "executor": record.get("executor"),
            "model": record.get("model"),
            "started_at": record.get("started_at"),
            "workdir": record.get("workdir"),
            "pid": record.get("pid"),
            "log_file": record.get("log_file"),
            "finished_at": finished.get("at") if finished else None,
            "exit_status": finished.get("exit_status") if finished else None,
            "timed_out": timed_out,
            # When this service noticed the run was gone. Never an exit status:
            # reconciliation records that an ending was missed, and inventing
            # one would be the whole thing it exists to avoid.
            "reconciled_at": orphaned.get("at") if orphaned else None,
            **self._output_tail(record.get("log_file")),
        }

    def _last_launch(self, events: list[dict], task_id: int) -> tuple[dict | None, int]:
        """The most recent ``launched`` record for this task, and where it sits.

        Matched on the log filename, which is ``task-<id>-<stamp>.log`` and the
        only field of that record carrying the task. Adding the id to the event
        itself would be a second way to say the same thing, and this read is not
        allowed to change what a launch records.
        """
        prefix = f"task-{int(task_id)}-"
        found, index = None, -1
        for position, record in enumerate(events):
            if record.get("event") != "launched":
                continue
            if Path(str(record.get("log_file", ""))).name.startswith(prefix):
                found, index = record, position
        return found, index

    def _terminal(
        self, events: list[dict], launched: dict | None, launch_index: int
    ) -> tuple[dict | None, bool, dict | None]:
        """How that launch ended: what was seen, and what was reconciled.

        Keyed on the PID, because the closing records carry the reference and
        the PID and not the log file. A timeout is logged *and then* followed by
        a ``finished``, so both are read: the exit status alone cannot tell a
        run that was killed for running too long from one that failed.

        The orphan record is returned separately rather than as a third kind of
        `finished`, because it is a different claim. `finished` says what a
        process exited with; this says only that it is gone and nobody saw it
        go (task 754).
        """
        if launched is None:
            return None, False, None
        pid = launched.get("pid")
        after = [r for r in events[launch_index + 1:] if r.get("pid") == pid]
        timed_out = any(r.get("event") == "timeout" for r in after)
        finished = next((r for r in after if r.get("event") == "finished"), None)
        orphaned = next(
            (r for r in after if r.get("event") == EVENT_ORPHANED), None
        )
        return finished, timed_out, orphaned

    def _output_tail(self, log_file) -> dict:
        """The end of a run's own output, bounded, with the token taken out."""
        empty = {"output_tail": [], "output_truncated": False, "output_at": None}
        if not log_file:
            return empty
        path = Path(str(log_file))
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > STATUS_TAIL_BYTES:
                    handle.seek(size - STATUS_TAIL_BYTES)
                raw = handle.read()
            modified = path.stat().st_mtime
        except OSError:
            return empty

        truncated = size > STATUS_TAIL_BYTES
        text = raw.decode("utf-8", errors="replace")
        if truncated:
            # A byte seek lands mid-line. Drop that fragment rather than publish
            # it as though the run had written a line beginning there.
            text = text.split("\n", 1)[1] if "\n" in text else ""
        lines = text.splitlines()
        if len(lines) > STATUS_TAIL_LINES:
            lines = lines[-STATUS_TAIL_LINES:]
            truncated = True
        return {
            "output_tail": [self._redact(line) for line in lines],
            "output_truncated": truncated,
            "output_at": time.strftime(
                "%Y-%m-%dT%H:%M:%S%z", time.localtime(modified)
            ),
        }

    def _redact(self, line: str) -> str:
        """The Vikunja token never travels out in a run's output.

        The launched process holds it in its environment, so a traceback, a
        verbose HTTP log or a dump of the environment could put it in the file
        this returns. The value is known here exactly, so this is an equality
        test on the one secret this process holds rather than a guess at what a
        credential looks like.
        """
        token = self.config.token
        if token and token in line:
            return line.replace(token, "[redacted]")
        return line

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
