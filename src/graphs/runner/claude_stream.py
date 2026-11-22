"""The stream pump for a headless `claude -p`: its three pipes serviced while it runs, and its group killed and reaped."""

from __future__ import annotations

import codecs
import io
import json
import logging
import os
import selectors
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any, TextIO, cast

from runner.protocol import RunnerError

_log = logging.getLogger(__name__)


# How long to wait for pipes to drain after the group is killed.
_REAP_SECONDS = 5


def _kill_group(proc: subprocess.Popen[str]) -> None:
    """SIGKILL claude's whole process group; the group is gone already when the lookup fails."""
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


def _json_object(line: str) -> dict[str, Any] | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


# How often a reader looks up from a quiet pipe to see whether it was told to stop.
_POLL_SECONDS = 0.1


def _pump(pipe: TextIO, stop: threading.Event, parts: list[str], on_line: Callable[[str], None] | None = None) -> None:
    """Read `pipe` to EOF a line at a time; a read error raises, it is never taken for EOF.

    Imperative edge code: `parts` and the pending tail are mutated on purpose. Wrong belief: closing a
    pipe wakes its reader, so this polls the raw fd and leaves on `stop`. `select.select` is out: it
    raises ValueError on any fd of 1024 or more.
    """
    fd = pipe.fileno()
    decoder = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder(pipe.encoding or "utf-8")(errors="replace"), translate=True)
    pending = ""
    with selectors.DefaultSelector() as ready:
        ready.register(fd, selectors.EVENT_READ)
        while not stop.is_set():
            if not ready.select(_POLL_SECONDS):
                continue
            data = os.read(fd, 65536)
            *whole, pending = (pending + decoder.decode(data, final=not data)).split("\n")
            lines = [f"{line}\n" for line in whole] + ([pending] if not data and pending else [])
            parts.extend(lines)
            for line in lines:
                if on_line is not None:
                    try:
                        on_line(line)
                    except Exception:
                        _log.debug("stream line handler failed", exc_info=True)
            if not data:
                return


def _feed(pipe: TextIO, prompt: str) -> None:
    """Write the prompt to claude's stdin and close it. A claude that stopped reading is not an error."""
    with suppress(OSError, ValueError):
        data, fd = prompt.encode(pipe.encoding or "utf-8"), pipe.fileno()
        while data:
            data = data[os.write(fd, data):]
    with suppress(OSError, ValueError):
        pipe.close()


class _Streams:
    """claude's three pipes, serviced by daemon threads so stdout is read while the node runs, not once it ends."""

    def __init__(self, proc: subprocess.Popen[str], prompt: str, on_line: Callable[[str], None] | None) -> None:
        self.proc = proc
        self._stop = threading.Event()
        self._out: list[str] = []
        self._err: list[str] = []
        self._failures: list[str] = []
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        self._threads = [
            threading.Thread(target=self._read, args=(proc.stdout, self._out, on_line), daemon=True),
            threading.Thread(target=self._read, args=(proc.stderr, self._err), daemon=True),
            threading.Thread(target=_feed, args=(proc.stdin, prompt), daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _read(self, pipe: TextIO, parts: list[str], on_line: Callable[[str], None] | None = None) -> None:
        try:
            _pump(pipe, self._stop, parts, on_line)
        except (OSError, ValueError) as exc:
            if not self._stop.is_set():
                _log.warning("reading claude's output failed: %s", exc)
                self._failures.append(f"{type(exc).__name__}: {exc}")

    def collect(self, timeout: float | None) -> tuple[str, str]:
        """Wait for claude to exit and both pipes to reach EOF, as `communicate` did, inside one `timeout`."""
        deadline = None if timeout is None else time.monotonic() + timeout

        def left() -> float | None:
            return None if deadline is None else max(0.0, deadline - time.monotonic())

        self.proc.wait(timeout=left())
        for thread in self._threads:
            thread.join(left())
            if thread.is_alive():
                raise subprocess.TimeoutExpired(self.proc.args, cast(float, timeout))  # a None timeout joins until done
        if self._failures:
            raise RunnerError(f"reading claude's output failed: {'; '.join(self._failures)}")
        return "".join(self._out), "".join(self._err)

    def stop(self, drain: float = 0.0) -> None:
        """Give the readers `drain` seconds to reach EOF, then tell them to leave."""
        deadline = time.monotonic() + drain
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        self._stop.set()
        for thread in self._threads:
            thread.join(_POLL_SECONDS * 5)


def _kill_and_reap(proc: subprocess.Popen[str], streams: _Streams) -> None:
    """Kill a timed-out claude's group and reap it without waiting on pipes forever.

    Wrong belief: killing claude closes its pipes. A tool it started keeps them
    open, so the drain is bounded and the pipes are closed by hand if it times out.
    """
    _kill_group(proc)
    deadline = time.monotonic() + _REAP_SECONDS
    with suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=_REAP_SECONDS)
    streams.stop(drain=max(0.0, deadline - time.monotonic()))
    _close_pipes(proc)
    proc.wait()


def _close_pipes(proc: subprocess.Popen[str]) -> None:
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()
