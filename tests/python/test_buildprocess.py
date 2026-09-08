# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Starting a program, reading its log, and deciding when it has had enough
(``mcuhome/workbench/buildprocess.py``).

No container runtime here: every program is a real child of this
process, chosen short-lived on purpose so the whole suite's promise is
seconds, not minutes — the ladder's own poll interval is half a second,
and a test that wants to see a rung taken waits at most a few of them.
"""

from __future__ import annotations

import sys
import time

from mcuhome.workbench.buildprocess import Liveness, spawn_process

SLEEP_30S = (sys.executable, "-c", "import time; time.sleep(30)")


def test_spawn_process_streams_merged_output_and_answers_the_exit_status() -> None:
    """Standard output and standard error arrive on the one sink, in the
    order the program flushed them, and ``wait()`` is the exit status."""
    lines: list[str] = []
    argv = (
        sys.executable,
        "-c",
        "import sys\n"
        "print('out-line', flush=True)\n"
        "print('err-line', file=sys.stderr, flush=True)\n"
        "sys.exit(3)\n",
    )
    running = spawn_process(argv, on_line=lines.append)
    assert running.started
    assert running.wait() == 3
    assert lines == ["out-line", "err-line"]


def test_a_program_that_does_not_exist_answers_a_handle_that_says_so() -> None:
    """``started`` is what tells "never ran" from "still running" — ``poll()``
    and ``wait()`` alone would read as the latter."""
    running = spawn_process(("mcuhome-test-program-that-does-not-exist",))
    assert running.started is False
    assert running.poll() is None
    assert running.wait() is None


def test_liveness_supervise_returns_the_status_of_a_program_that_ends_on_its_own(
    tmp_path,
) -> None:
    child = spawn_process((sys.executable, "-c", "raise SystemExit(0)"))
    liveness = Liveness(cancel=tmp_path / "cancel", deadline_seconds=30, cancel_grace_seconds=30)
    assert liveness.supervise(child) == 0


def test_touching_the_cancel_sentinel_ends_a_sleeping_program(tmp_path) -> None:
    """The sentinel's *existence* is the whole signal: with no grace period
    the next tick of the ladder signals the child directly."""
    cancel = tmp_path / "cancel"
    cancel.touch()
    child = spawn_process(SLEEP_30S)
    liveness = Liveness(cancel=cancel, deadline_seconds=30, cancel_grace_seconds=0)
    started = time.monotonic()
    status = liveness.supervise(child)
    elapsed = time.monotonic() - started
    assert status is not None
    assert elapsed < 20  # stopped well short of the 30s sleep


def test_the_deadline_ends_a_program_nobody_cancelled(tmp_path) -> None:
    """``deadline_seconds`` is enforced here by touching the same sentinel a
    caller would touch, then walking the same ladder."""
    cancel = tmp_path / "cancel"
    child = spawn_process(SLEEP_30S)
    liveness = Liveness(cancel=cancel, deadline_seconds=0, cancel_grace_seconds=0)
    started = time.monotonic()
    status = liveness.supervise(child)
    elapsed = time.monotonic() - started
    assert status is not None
    assert cancel.exists(), "the deadline enforces itself by touching the sentinel"
    assert elapsed < 20  # stopped well short of the 30s sleep
