# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""One build directory, one operation at a time (``buildlock.py``).

The failure this prevents is real and was observed: two builds of one
device ran at once, the second wiped the shared work tree, and the first
died mid-compile on a generated header that had been there a second
earlier. The same collision between a build and a *flash* would put half
of one image and half of another on a device.

So the subject here is a second **process**, and these tests use a real
one. An in-process nesting is deliberately allowed — a command line
holds the directory for the compile *and* the signing that follows —
which is exactly why nesting two locks would prove nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from conftest import REPO_ROOT

from mcuhome.workbench import buildlock
from mcuhome.workbench.buildlock import (
    BUILD_LOCK_FILE,
    BuildDirectoryBusy,
    holder_of,
    is_busy,
    open_build_lock,
)

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="the lock is a POSIX advisory lock")


def child_env() -> dict[str, str]:
    """The environment a peer process of this suite is started with.

    Public for the same reason :func:`held_elsewhere` is: every test
    that needs a second process needs this, and a second copy of it
    somewhere else is a second thing to keep in step.
    """
    return {
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": os.environ.get("PATH", ""),
    }


@contextmanager
def held_elsewhere(out_dir: Path, *, device: str, operation: str = "build") -> Iterator[None]:
    """A real second process holding *out_dir*, released on the way out.

    Public because it is the only way to hold a build directory against
    this one: the lock is re-entrant per process, so a thread of our own
    would be granted it rather than refused. ``test_buildrecord`` uses it
    for the same reason this module does.
    """
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from mcuhome.workbench.buildlock import open_build_lock\n"
        "held = open_build_lock(\n"
        f"    Path({str(out_dir)!r}), device={device!r}, operation={operation!r}\n"
        ")\n"
        "held.__enter__()\n"
        "print('holding', flush=True)\n"
        "sys.stdin.readline()\n"
        "held.__exit__(None, None, None)\n"
    )
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=child_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None and process.stdin is not None
    assert process.stdout.readline().strip() == "holding", "the holder never took the lock"
    try:
        yield
    finally:
        process.stdin.write("\n")
        process.stdin.flush()
        process.wait(timeout=60)


def _refused(out_dir: Path, *, operation: str = "build") -> str:
    """Try to work in a directory somebody else holds; return the refusal."""
    with (
        pytest.raises(BuildDirectoryBusy) as caught,
        open_build_lock(out_dir, device="bmp180-node", operation=operation),
    ):
        pytest.fail("a second run took a directory another process holds")
    return str(caught.value)


def _a_second_process_is_still_refused(out_dir: Path) -> bool:
    code = (
        "from pathlib import Path\n"
        "from mcuhome.workbench.buildlock import open_build_lock, BuildDirectoryBusy\n"
        "try:\n"
        f"    with open_build_lock(Path({str(out_dir)!r}), device='other', operation='flash'):\n"
        "        print('took it', flush=True)\n"
        "except BuildDirectoryBusy:\n"
        "    print('refused', flush=True)\n"
    )
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=child_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip() == "refused"


def test_a_second_build_of_the_same_directory_is_refused(tmp_path) -> None:
    with held_elsewhere(tmp_path, device="bmp180-node"):
        refusal = _refused(tmp_path)
    assert "A build of bmp180-node is already running" in refusal
    assert "process " in refusal  # which build, so the user can find it
    assert "--build-dir" in refusal  # and how to build in parallel anyway


def test_a_flash_is_refused_while_a_build_runs_and_says_which(tmp_path) -> None:
    """The point of the guard: not two builds, but any two runs.

    A build that rewrites the signed image while it is being flashed
    would put half of one image and half of another on the device.
    """
    with held_elsewhere(tmp_path, device="bmp180-node", operation="build"):
        refusal = _refused(tmp_path, operation="flash")
    assert "A build of bmp180-node is already running" in refusal
    # The escape hatch belongs to builds — a flash cannot go to another
    # directory, so it is not offered one.
    assert "--build-dir" not in refusal


def test_a_build_is_refused_while_the_device_is_being_flashed(tmp_path) -> None:
    with held_elsewhere(tmp_path, device="bmp180-node", operation="flash"):
        refusal = _refused(tmp_path)
    assert "bmp180-node is being flashed" in refusal


def test_one_run_holds_its_directory_through_several_steps(tmp_path) -> None:
    """A ``device build`` compiles and then signs, holding it throughout.

    The nesting is the command line's outer hold plus the build's
    own — the one case that must not refuse itself — and the directory
    stays taken for everybody else until the outer hold ends.
    """
    with open_build_lock(tmp_path, device="bmp180-node", operation="build"):
        with open_build_lock(tmp_path, device="bmp180-node", operation="build"):
            assert holder_of(tmp_path)["operation"] == "build"
        assert _a_second_process_is_still_refused(tmp_path)
    assert not _a_second_process_is_still_refused(tmp_path)


def test_another_directory_runs_at_the_same_time(tmp_path) -> None:
    """The lock is per build directory: two devices are two runs."""
    with (
        held_elsewhere(tmp_path / "one", device="a"),
        open_build_lock(tmp_path / "two", device="b"),
    ):
        assert (tmp_path / "one" / BUILD_LOCK_FILE).is_file()
        assert (tmp_path / "two" / BUILD_LOCK_FILE).is_file()


def test_the_directory_is_free_again_afterwards(tmp_path) -> None:
    with held_elsewhere(tmp_path, device="bmp180-node"):
        pass
    with open_build_lock(tmp_path, device="bmp180-node"):
        assert holder_of(tmp_path)["pid"] == str(os.getpid())


def test_a_failed_run_releases_the_directory(tmp_path) -> None:
    """However a run ends, the next one may start — the kernel sees to it."""
    with pytest.raises(RuntimeError), open_build_lock(tmp_path, device="bmp180-node"):
        raise RuntimeError("the compile blew up")
    with open_build_lock(tmp_path, device="bmp180-node"):
        pass


def test_a_lock_file_without_a_holder_stops_nothing(tmp_path) -> None:
    """The leftover of a crashed run is a file, and a file locks nothing.

    This is why the lock is the OS's and the file only its label: the
    record below names a process that is not there, and it must not be
    read as a running build.
    """
    (tmp_path / BUILD_LOCK_FILE).write_text(
        json.dumps(
            {
                "pid": "999999",
                "device": "gone",
                "operation": "build",
                "started": "2026-08-16 05:03:12",
            }
        ),
        encoding="utf-8",
    )
    with open_build_lock(tmp_path, device="bmp180-node"):
        assert holder_of(tmp_path)["device"] == "bmp180-node"


def test_a_garbled_record_costs_the_refusal_its_detail_not_its_correctness(tmp_path) -> None:
    with held_elsewhere(tmp_path, device="bmp180-node"):
        (tmp_path / BUILD_LOCK_FILE).write_text("{not json", encoding="utf-8")
        assert holder_of(tmp_path) == {}
        refusal = _refused(tmp_path)
    assert "Another MCUHome run is working" in refusal


def test_a_lock_file_replaced_between_the_open_and_the_lock_is_taken_again(
    tmp_path, monkeypatch
) -> None:
    """Locking the file that was there is not locking the path.

    The two steps are not one, and between them the file this descriptor
    points at can be removed — which is what the discard of an emptied
    build directory does. A lock on a file that is no longer at that
    path holds nothing: the path is free, and the next process creates
    one of its own there and is granted the same directory. Forced here
    by replacing the file inside the first ``flock`` call, and asserted
    from outside: while the lock is held, the file *at the path* is
    locked, which is what ``is_busy`` asks.
    """
    real_flock = buildlock.fcntl.flock
    calls: list[int] = []

    def flock(handle, operation):
        calls.append(handle)
        if len(calls) == 1:
            # Somebody took the file away and put another one there.
            path = tmp_path / BUILD_LOCK_FILE
            path.unlink()
            path.write_bytes(b"")
        return real_flock(handle, operation)

    monkeypatch.setattr(buildlock.fcntl, "flock", flock)

    with open_build_lock(tmp_path, device="bmp180-node"):
        assert is_busy(tmp_path), "the file at the path is not the one that was locked"

    assert len(calls) > 1, "the take was not repeated"


def test_a_lock_file_that_keeps_being_replaced_is_a_refusal(tmp_path, monkeypatch) -> None:
    """A path that changes under every attempt is somebody working there.

    Repeating forever would be a call that never answers; the bound is
    small and what it ends in is the refusal every other way of not
    getting the directory ends in.
    """
    real_flock = buildlock.fcntl.flock

    def flock(handle, operation):
        path = tmp_path / BUILD_LOCK_FILE
        path.unlink()
        path.write_bytes(b"")
        return real_flock(handle, operation)

    monkeypatch.setattr(buildlock.fcntl, "flock", flock)

    with pytest.raises(BuildDirectoryBusy), open_build_lock(tmp_path, device="bmp180-node"):
        pass
