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
from .run_output import tail as output_tail
from .worktree import WorktreeError, ensure_worktree
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


# How a run's own output is read back, and how far it is bounded, lives in
# `run_output` with the rendering it is inseparable from (task 755). The
# launcher owns launching; what one run wrote, and how much of it may travel
# out, is one job and it is that module's.

#: What a run that started can be, once it is no longer running. Two of these
#: name a run whose ending nobody watched, and neither reports an outcome:
#: `finished` would claim an exit nobody observed and `failed` a failure the
#: runner never saw. They differ in what has been DONE about it, which is what
#: a reader has to act on (task 757):
#:
#: * `lost` — the ending is still unaccounted for. The lock may still be held,
#:   nothing has been said on the board, and the next startup will act on it.
#: * `reconciled` — that same run, closed out (task 754): the ending is
#:   recorded as missed, the lock is released and nothing further will happen
#:   to it. The word is about the accounting, never about the outcome, which
#:   remains exactly as unknown as it was.
#:
#: The two used to be one word, so a run that had been closed out was reported
#: as one nothing had been done about — and the note read out for it said the
#: ticket was probably still In Progress, when reconciliation is what moved it.
RUN_RUNNING = "running"
#: A run that hit the time limit, was signalled, was escalated to SIGKILL, and
#: is STILL ALIVE (task 758). Separate from `running` because the two call for
#: opposite responses: a running run is working and will report on the board
#: itself, while this one has been told to stop twice, is not going to report
#: anything, and is holding its ticket's lock precisely so that nothing launches
#: a second run into the same worktree beside it. The runner has done everything
#: it can do to this process; what is left is a human with a kill.
RUN_UNKILLABLE = "unkillable"
RUN_FINISHED = "finished"
RUN_FAILED = "failed"
RUN_LOST = "lost"
RUN_RECONCILED = "reconciled"
RUN_NONE = "none"

#: Every state ``run_status`` can report, enumerated once so that a reader of
#: this vocabulary — the note table on the MCP side, and the test that pins it —
#: cannot be asked about a state that is not here or miss one that is.
RUN_STATES = (
    RUN_RUNNING,
    RUN_UNKILLABLE,
    RUN_FINISHED,
    RUN_FAILED,
    RUN_LOST,
    RUN_RECONCILED,
    RUN_NONE,
)

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
        finished, _, orphaned, _ = self._terminal(events, launched, index)
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
        finished, timed_out, orphaned, unkilled = self._terminal(
            events, launched, launch_index
        )

        lock = self._read_lock(task_id)
        alive = lock is not None and self._is_alive(int(lock.get("pid", -1)))

        if alive:
            # A run that outlived its own kill is not one that is working
            # (task 758). It keeps its lock — that is what stops a second run
            # being launched into the worktree it is still sitting in — but
            # reporting it as `running` would send a reader away to wait for a
            # report that is never coming.
            state = RUN_UNKILLABLE if unkilled else RUN_RUNNING
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
        elif orphaned is not None:
            # Started, gone, and closed out. Still no outcome — reconciliation
            # records that an ending was missed, it does not discover what the
            # ending was — but everything that was going to happen to this run
            # has happened: the ending is on the log, the lock is released,
            # and the startup pass that does this also tells the board.
            # Reported as `lost` it was indistinguishable from the case below,
            # which is the one thing a reader has to act on differently
            # (task 757).
            state = RUN_RECONCILED
        else:
            # Started and gone, with nothing yet said about it. The lock may
            # still be held; the next startup, or the next read that is allowed
            # to reclaim, will close it out.
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
    ) -> tuple[dict | None, bool, dict | None, bool]:
        """How that launch ended: what was seen, and what was reconciled.

        Keyed on the PID, because the closing records carry the reference and
        the PID and not the log file. A timeout is logged *and then* followed by
        a ``finished``, so both are read: the exit status alone cannot tell a
        run that was killed for running too long from one that failed.

        The orphan record is returned separately rather than as a third kind of
        `finished`, because it is a different claim. `finished` says what a
        process exited with; this says only that it is gone and nobody saw it
        go (task 754).

        The last value says the timeout record reports a process that did not
        die (task 758). `terminated` is only ever written by the reaper that
        watched the kill, so `is False` is read exactly: a timeout record with
        no such field is one written before the kill was waited for, and a
        missing observation must not be read as a live process.
        """
        if launched is None:
            return None, False, None, False
        pid = launched.get("pid")
        after = [r for r in events[launch_index + 1:] if r.get("pid") == pid]
        timeouts = [r for r in after if r.get("event") == "timeout"]
        timed_out = bool(timeouts)
        unkilled = any(r.get("terminated") is False for r in timeouts)
        finished = next((r for r in after if r.get("event") == "finished"), None)
        orphaned = next(
            (r for r in after if r.get("event") == EVENT_ORPHANED), None
        )
        return finished, timed_out, orphaned, unkilled

    def _output_tail(self, log_file) -> dict:
        """The end of this run's own output, read and bounded by ``run_output``.

        The redaction is passed in rather than lived there: the token is this
        process's, and the module that renders a log has no business holding a
        credential to compare against.
        """
        return output_tail(log_file, self._redact)

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
        self,
        ticket: Ticket,
        build_prompt: Callable[[Path], str],
        executor: Executor | None = None,
    ) -> LaunchRecord:
        """One run for one ticket, in a worktree of its own.

        ``executor`` decides only which model the launched Claude Code drives,
        as extra environment for the child. Everything else about a launch — the
        binary, the arguments, the working directory, the lock, the log, the
        reap — is the same whichever model is behind it, which is why there is
        one launch path rather than one per model.

        ``build_prompt`` is a callable rather than the prompt itself because the
        prompt names the directory the run works in, and that directory does not
        exist until this method makes it (task 756). Building it here also means
        the path in the prompt is the path the child is actually given: they
        cannot disagree.
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

            # Before the spawn and inside the mutex, so a failure here is a
            # refusal with the lock released rather than a run in the wrong
            # place. There is deliberately no fallback to the repository root:
            # that fallback is the defect (task 756).
            try:
                workdir = ensure_worktree(
                    self.config.workdir, ticket.task_number, ticket.task_id
                )
            except WorktreeError as exc:
                self._release(ticket.task_id)
                self._log(
                    "launch_failed",
                    number=ticket.task_number,
                    reference=ticket.board_reference,
                    error=str(exc),
                )
                raise LaunchError(str(exc)) from exc

            try:
                handle = log_file.open("w", encoding="utf-8")
                process = self._spawn(
                    [
                        self.config.claude_bin,
                        *self.config.claude_args,
                        build_prompt(workdir),
                    ],
                    cwd=str(workdir),
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
                workdir=str(workdir),
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
        """Watch one run to its end, then record the end and release its slot.

        Two orderings are the whole of the care here, and both used to be the
        other way round (task 758).

        **The ending is recorded before the lock goes**, the same way
        reconciliation does it, so that a crash between the two leaves a lock —
        which the next read reclaims — rather than a released lock whose ending
        was never written, which nothing would ever revisit. It was a `finally`,
        which released the lock on every path including the ones that had
        recorded nothing.

        **And the lock is not released until the process is actually dead.** The
        timeout path used to send SIGTERM and immediately call the run finished:
        `finished` meant a signal had been sent, not that anything had been
        reaped. A child that does not die on SIGTERM — Claude Code inside a long
        tool call is the realistic case — kept running in the ticket's worktree
        with nothing recording that it was there, and the same ticket could be
        launched straight into it again.

        The id releases the lock; the reference is what the log says, because
        `recent()` is rendered on the console a human reads (task 659).
        """
        try:
            exit_status = process.wait(timeout=self.config.launch_timeout_seconds)
        except subprocess.TimeoutExpired:
            # Never the status `wait()` hands back below: that is the signal
            # this method sent, not an outcome the run reached. A killed run has
            # no exit status, and None stays None.
            exit_status = None
            escalated, terminated = self._terminate(process)
            self._log(
                "timeout",
                reference=reference,
                pid=process.pid,
                after_seconds=self.config.launch_timeout_seconds,
                escalated=escalated,
                terminated=terminated,
            )
            if not terminated:
                # It survived SIGKILL. There is nothing further this thread can
                # do to it and nothing it may claim about it: no `finished`, and
                # the lock stays, which is what keeps the condition visible and
                # keeps a second run out of the worktree it is still in. When it
                # does eventually die, the next read of that lock reconciles it.
                return
        self._log(
            "finished",
            reference=reference,
            pid=process.pid,
            exit_status=exit_status,
        )
        self._release(task_id)

    def _terminate(self, process: subprocess.Popen) -> tuple[bool, bool]:
        """Stop the run's process group, escalating within a bounded grace.

        SIGTERM, a grace period, then SIGKILL and the same grace again. Both
        waits are bounded on purpose: waiting for a wedged child to die would
        hold this thread and the ticket's lock forever, which is a worse version
        of the failure this replaces.

        Returns whether SIGKILL was needed and whether the process is dead —
        both of which are observations rather than intentions, which is why they
        are what gets logged.
        """
        grace = self.config.kill_grace_seconds
        self._signal_group(process, signal.SIGTERM)
        if self._reaped(process, grace):
            return False, True
        self._signal_group(process, signal.SIGKILL)
        return True, self._reaped(process, grace)

    def _signal_group(self, process: subprocess.Popen, number: int) -> None:
        """Signal the whole group the run was started in.

        The group, not the process: the child is a harness that spawns its own
        children, and signalling only the leader leaves them. A group that is
        already gone is not an error — the wait is what establishes that a run
        ended, and this only asks it to.
        """
        try:
            os.killpg(os.getpgid(process.pid), number)
        except ProcessLookupError:
            pass

    def _reaped(self, process: subprocess.Popen, grace: float) -> bool:
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            return False
        return True
