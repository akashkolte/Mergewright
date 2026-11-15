"""Run a host command with a timeout, keeping only the tail of its output.

The sandbox drives the docker CLI through this. Output is read as it arrives and only the
last max_output_bytes are kept, so a command that prints gigabytes cannot exhaust host memory.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

DEFAULT_MAX_OUTPUT_BYTES = 64_000


@dataclass(frozen=True)
class CommandResult:
    code: int
    output: str  # stdout + stderr merged, tail only
    timed_out: bool  # the host-side timeout fired and the process was killed
    truncated: bool
    seconds: float


def run_command(
    argv: Sequence[str],
    input: str | None = None,
    timeout_s: float | None = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    clock: Callable[[], float] = time.monotonic,
) -> CommandResult:
    begin = clock()
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    tail = bytearray()
    truncated = False

    def read() -> None:
        nonlocal tail, truncated
        assert proc.stdout is not None
        for chunk in iter(lambda: proc.stdout.read1(65536), b""):
            tail += chunk
            if len(tail) > max_output_bytes:
                del tail[: len(tail) - max_output_bytes]
                truncated = True

    def write() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write((input or "").encode())
            proc.stdin.close()
        except BrokenPipeError:
            pass  # the command exited without reading all of its input

    threads = [threading.Thread(target=read, daemon=True)]
    if input is not None:
        threads.append(threading.Thread(target=write, daemon=True))
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        timed_out = True
    for thread in threads:
        thread.join(timeout=5)
    return CommandResult(
        code=proc.returncode,
        output=tail.decode("utf-8", errors="replace"),
        timed_out=timed_out,
        truncated=truncated,
        seconds=round(clock() - begin, 3),
    )


def docker_cli(
    args: Sequence[str],
    input: str | None = None,
    timeout_s: float | None = None,
    max_output_bytes: int | None = None,
) -> CommandResult:
    """Run `docker <args>` on the host."""
    return run_command(
        ["docker", *args],
        input=input,
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes or DEFAULT_MAX_OUTPUT_BYTES,
    )
