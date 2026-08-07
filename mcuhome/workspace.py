# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Pipeline stage 5, native path: compiling the generated application.

Stage 4 (:mod:`mcuhome.generate`) writes a standalone Zephyr application.
This module turns it into an image, by driving ``west build`` in the west
workspace the builder itself lives in.

**The escape hatch, not the normal path (ADR 0007).** ``mcuhome build``
compiles inside the versioned builder image (:mod:`mcuhome.container`);
``--native`` selects what is below, for people who already have a west
workspace with a toolchain in it — MCUHome's own contributors. The two
paths meet at :func:`plan_build` and :class:`BuildPlan`: a command plus an
environment. The container path assembles a different command in a
different environment — using :func:`west_build_command` for the inner
half — and reuses everything after that, :func:`run_build`,
:func:`artifacts` and :func:`parse_memory_report` included.

**Nothing here knows the device model.** The inputs are a board name, a
snippet list and two directories; whatever produced them is somebody
else's problem. That keeps the interesting parts — workspace discovery,
command assembly, prerequisite checking, memory-report parsing — pure
functions that the test suite exercises without ever running west.

**Why the builder has to set the environment at all.** Two build-time
tools of the Matter SDK are not part of a Zephyr installation: ``gn``
(CHIP builds its own libraries with it) and ``zap`` (it generates the
root-node data model from the framework's ``.zap``). And CHIP v1.5.1.0's
release tarball is missing the ``python_path`` helper its codegen scripts
import, which ``scripts/pyshim/`` stands in for — that one the builder can
fix on its own, by putting the shim on ``PYTHONPATH``; the other two it
can only check for and explain. The builder image provides all three,
which is why this checking only happens on the ``--native`` path.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from mcuhome.errors import BuildError

__all__ = [
    "BUILD_SUBDIR",
    "CHIP_JOBS_VAR",
    "JOBS",
    "MODULE_DIR",
    "PYSHIM_DIR",
    "TOOLS",
    "BuildPlan",
    "MemoryRegion",
    "ToolNeed",
    "artifacts",
    "build_environment",
    "find_topdir",
    "missing_tools",
    "parse_memory_report",
    "plan_build",
    "refuse_failed_build",
    "require_tools",
    "require_topdir",
    "run_build",
    "west_build_command",
]

#: The MCUHome repository this builder is part of — the Zephyr module that
#: carries the generic application main and the framework ZAP the generated
#: application refers to. ``mcuhome/mcuhome/workspace.py`` -> ``mcuhome/``.
MODULE_DIR = Path(__file__).resolve().parent.parent

#: Stand-in for CHIP's missing ``python_path`` helper (see the module
#: docstring and ``scripts/pyshim/README.md``).
PYSHIM_DIR = MODULE_DIR / "scripts" / "pyshim"

#: Sub-directory of the build directory that holds the CMake/ninja tree.
#: It is a sibling of the generated ``app/`` rather than the same
#: directory, so the two stay tellable apart: everything outside this
#: directory is builder output a human is meant to read
#: (builder-pipeline.md §1.3), everything inside it is machine spoil that
#: can be deleted at any time.
BUILD_SUBDIR = "build"

#: Parallelism handed to the build tool. Deliberately not the CPU count:
#: a Matter build peaks at well over a gigabyte per compiler process, and
#: on the 4-core/15-GB machines this is developed on, ``-j$(nproc)`` is an
#: OOM kill rather than a faster build. Overridable per invocation once
#: there is evidence that a fixed number is the wrong answer somewhere.
JOBS = 2

#: Environment variable the vendored CHIP GN sub-build reads to cap its own
#: inner ``ninja`` invocation (patch hunk in
#: ``patches/connectedhomeip-v1.5.1.0-vanilla-zephyr.patch``, applied to
#: ``config/common/cmake/chip_gn.cmake``). Upstream always runs a bare
#: ``ninja`` there, so without this the outer ``-o=-j{JOBS}`` above is
#: invisible to it and the Matter sub-build regenerates ninja's default of
#: nproc+2 — the exact OOM risk :data:`JOBS` exists to avoid, just one
#: process tree down.
CHIP_JOBS_VAR = "MCUHOME_CHIP_JOBS"

#: What the west workspace top directory is recognized by.
_WEST_MARKER = Path(".west") / "config"


@dataclass(frozen=True)
class ToolNeed:
    """One build-time tool the host has to provide, and how to say so."""

    #: What to call it in a message.
    name: str
    #: Executables that satisfy it; any one is enough.
    commands: tuple[str, ...]
    #: Environment variables that satisfy it instead of a PATH entry.
    env_vars: tuple[str, ...]
    #: Why the build needs it, in one clause.
    why: str
    #: Where it comes from, for the fix line.
    source: str

    def satisfied_by(self, env: dict[str, str]) -> bool:
        path = env.get("PATH")
        if any(shutil.which(command, path=path) for command in self.commands):
            return True
        return any(env.get(name) for name in self.env_vars)


#: Checked before west is invoked. West itself is first: without it none of
#: the others matter, and its absence is the one that a plain "command not
#: found" would explain worst.
TOOLS: tuple[ToolNeed, ...] = (
    ToolNeed(
        name="west",
        commands=("west",),
        env_vars=(),
        why="it is the Zephyr build front end",
        source="pip install west",
    ),
    ToolNeed(
        name="gn",
        commands=("gn",),
        env_vars=(),
        why="the Matter SDK builds its own libraries with GN",
        source="https://gn.googlesource.com/gn (a single binary; put it on PATH)",
    ),
    ToolNeed(
        name="zap",
        commands=("zap", "zap-cli"),
        env_vars=("ZAP_INSTALL_PATH",),
        why="it generates the Matter root-node data model from the framework .zap",
        source=(
            "https://github.com/project-chip/zap/releases (put the install "
            "directory on PATH, or point ZAP_INSTALL_PATH at it)"
        ),
    ),
)


# --------------------------------------------------------------------------
# Finding the workspace
# --------------------------------------------------------------------------


def find_topdir(*starts: Path) -> Path | None:
    """First west workspace top directory at or above any of *starts*.

    The order of *starts* is the order of preference. The caller offers
    the builder's own location first: a west workspace containing this
    package is by construction one that also contains the MCUHome Zephyr
    module the generated application consumes, which the working directory
    is not guaranteed to be.
    """
    for start in starts:
        current = start.resolve()
        if current.is_file():
            current = current.parent
        for candidate in [current, *current.parents]:
            if (candidate / _WEST_MARKER).is_file():
                return candidate
    return None


def require_topdir(*starts: Path) -> Path:
    """:func:`find_topdir`, or a plain-language refusal."""
    topdir = find_topdir(*starts)
    if topdir is not None:
        return topdir
    raise BuildError(
        "MCUHome cannot compile here: this is not a west workspace.",
        hint=(
            "compiling needs Zephyr and the Matter SDK, which live in a west "
            "workspace next to the MCUHome sources. Either create one\n"
            "    mkdir mcuhome-workspace && cd mcuhome-workspace\n"
            "    git clone https://github.com/mcu-home/mcuhome\n"
            "    west init -l mcuhome && west update\n"
            "or stop after code generation and build the application yourself:\n"
            "    mcuhome build <device> --generate-only"
        ),
    )


# --------------------------------------------------------------------------
# Environment and prerequisites
# --------------------------------------------------------------------------


def build_environment(env: dict[str, str] | None = None, *, jobs: int = JOBS) -> dict[str, str]:
    """*env* plus what the Matter build needs, without mutating the input."""
    prepared = dict(os.environ if env is None else env)
    existing = prepared.get("PYTHONPATH", "")
    entries = [str(PYSHIM_DIR), *[entry for entry in existing.split(os.pathsep) if entry]]
    # Deduplicate while keeping order: re-running the builder inside its own
    # environment must not grow PYTHONPATH one copy at a time.
    seen: dict[str, None] = {}
    for entry in entries:
        seen.setdefault(entry, None)
    prepared["PYTHONPATH"] = os.pathsep.join(seen)
    # Same job count as the outer `-o=-j{jobs}` (west_build_command): the
    # vendored CHIP GN sub-build otherwise ignores it entirely, see
    # CHIP_JOBS_VAR.
    prepared[CHIP_JOBS_VAR] = str(jobs)
    return prepared


def missing_tools(env: dict[str, str]) -> list[ToolNeed]:
    """The entries of :data:`TOOLS` *env* does not provide, in order."""
    return [tool for tool in TOOLS if not tool.satisfied_by(env)]


def _refuse_missing(tools: list[ToolNeed]) -> BuildError:
    names = ", ".join(tool.name for tool in tools)
    noun = "tool" if len(tools) == 1 else "tools"
    lines = [f"install the {noun}, then run the same command again:"]
    for tool in tools:
        lines.append(f"    {tool.name} — {tool.why}")
        lines.append(f"      from: {tool.source}")
    lines.append(
        "None of this is needed with --generate-only, and none of it is needed "
        "in the MCUHome builder container — which is what compiles when you "
        "leave --native off."
    )
    return BuildError(
        f"MCUHome cannot compile without {names}, which is not on your PATH."
        if len(tools) == 1
        else f"MCUHome cannot compile without {names}, which are not on your PATH.",
        hint="\n".join(lines),
    )


def require_tools(env: dict[str, str]) -> None:
    """Refuse, naming every missing tool at once, or return."""
    missing = missing_tools(env)
    if missing:
        raise _refuse_missing(missing)


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------


def west_build_command(
    *,
    app_dir: Path,
    build_dir: Path,
    board: str,
    snippets: tuple[str, ...] = (),
    jobs: int = JOBS,
) -> list[str]:
    """The ``west build`` invocation for one generated application.

    ``--pristine=auto`` rather than always-pristine: west re-runs CMake
    from scratch when the board or the application directory changed and
    keeps the object files otherwise, which is the difference between a
    ten-minute and a ten-second edit cycle on the Matter tree.
    """
    command = [
        "west",
        "build",
        "--board",
        board,
        "--build-dir",
        str(build_dir),
        "--pristine",
        "auto",
    ]
    for snippet in snippets:
        command += ["--snippet", snippet]
    # `-o=` and not `-o `: the value starts with a dash, and argparse reads
    # a separate token starting with a dash as the next option.
    command.append(f"-o=-j{jobs}")
    command.append(str(app_dir))
    return command


@dataclass(frozen=True)
class BuildPlan:
    """Everything stage 5 needs, decided before anything is executed.

    Shared by both paths: *command* is a ``west build`` invocation on the
    native path and a ``docker run`` wrapped around one in the container,
    and everything downstream treats it the same way. *image* is the one
    thing that differs enough to be worth naming — ``None`` means the
    build runs on this machine's own toolchain.
    """

    topdir: Path
    app_dir: Path
    build_dir: Path
    command: list[str]
    env: dict[str, str]
    image: str | None = None


def plan_build(
    *,
    out_dir: Path,
    app_subdir: str,
    board: str,
    snippets: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    jobs: int = JOBS,
) -> BuildPlan:
    """Resolve workspace, environment and command, or refuse with a reason.

    Executes nothing. Every refusal a user can hit before the compiler
    starts is raised here, which is also what makes stage 5 testable
    without a toolchain.
    """
    topdir = require_topdir(MODULE_DIR, cwd or Path.cwd())
    prepared = build_environment(env, jobs=jobs)
    # ``west build`` does not export ZEPHYR_BASE (it resolves Zephyr through
    # the manifest and the CMake package registry), yet CMake code outside
    # Zephyr's own — the generated application's search for the Matter SDK,
    # among others — reasonably expects it. Fill it in, but never overwrite
    # a value someone set on purpose: west would follow that value too, and
    # a builder that silently disagrees with the tool it drives is worse
    # than one that leaves the choice alone.
    zephyr = topdir / "zephyr"
    if not prepared.get("ZEPHYR_BASE") and zephyr.is_dir():
        prepared["ZEPHYR_BASE"] = str(zephyr)
    require_tools(prepared)
    app_dir = out_dir / app_subdir
    build_dir = out_dir / BUILD_SUBDIR
    return BuildPlan(
        topdir=topdir,
        app_dir=app_dir,
        build_dir=build_dir,
        command=west_build_command(
            app_dir=app_dir,
            build_dir=build_dir,
            board=board,
            snippets=snippets,
            jobs=jobs,
        ),
        env=prepared,
    )


# --------------------------------------------------------------------------
# Running it
# --------------------------------------------------------------------------


def run_build(plan: BuildPlan, *, stream: TextIO | None = None) -> tuple[int, str]:
    """Run *plan*, echoing the build log live, and return (code, log).

    The log is echoed rather than swallowed on purpose: a Matter build
    takes minutes, and a builder that prints nothing while it runs is
    indistinguishable from one that hung. It is captured as well, because
    the memory report at the end of it is part of the summary.
    """
    stream = sys.stdout if stream is None else stream
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            plan.command,
            cwd=str(plan.topdir),
            env=plan.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as error:
        raise BuildError(
            f"MCUHome could not start the build: {error.strerror}.",
            hint=f"the command was: {' '.join(plan.command)}",
        ) from error

    captured: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        captured.append(line)
        stream.write(line)
        stream.flush()
    return process.wait(), "".join(captured)


def refuse_failed_build(code: int, *, build_dir: Path) -> BuildError:
    """The error for a compiler that stopped, pointing at the log."""
    return BuildError(
        f"The firmware did not compile (west build exited with {code}).",
        hint=(
            "the build log above says what went wrong; the full CMake output "
            f"is in {build_dir}. If the failure is in generated code rather "
            "than in your configuration, that is a builder bug worth reporting."
        ),
    )


# --------------------------------------------------------------------------
# What came out
# --------------------------------------------------------------------------

#: Image formats Zephyr writes that MCUHome has a use for today. Order is
#: the order they are reported in: the ELF first because that is what a
#: debugger and ``west flash`` want, the hex because that is what a
#: standalone flashing tool wants.
_ARTIFACT_NAMES = ("zephyr.elf", "zephyr.hex", "zephyr.bin", "zephyr.uf2")


def artifacts(build_dir: Path) -> list[Path]:
    """The images *build_dir* actually contains, in reporting order."""
    output = build_dir / "zephyr"
    return [output / name for name in _ARTIFACT_NAMES if (output / name).is_file()]


@dataclass(frozen=True)
class MemoryRegion:
    """One line of Zephyr's ``Memory region`` table."""

    name: str
    used: int
    total: int
    percent: float

    def describe(self) -> str:
        return (
            f"{self.name} {self.used / 1024:.1f} KiB of "
            f"{self.total / 1024:.1f} KiB ({self.percent:.1f}%)"
        )


_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}

#: ``           FLASH:      859672 B         1 MB      81.99%``
_REGION = re.compile(
    r"^\s*(?P<name>\w+):\s+"
    r"(?P<used>\d+)\s*(?P<used_unit>[KMG]?B)\s+"
    r"(?P<total>\d+)\s*(?P<total_unit>[KMG]?B)\s+"
    r"(?P<percent>[\d.]+)%"
)


def parse_memory_report(log: str) -> list[MemoryRegion]:
    """The footprint table Zephyr prints at the end of a link.

    Read out of the build log rather than recomputed: it is the number the
    linker script actually enforced, and tracking it per build is what
    builder-pipeline.md §1 asks for. A log without the table (an
    incremental build that relinked nothing) yields an empty list, which is
    not an error — it is simply nothing to report.
    """
    regions: list[MemoryRegion] = []
    for line in log.splitlines():
        match = _REGION.match(line)
        if match is None:
            continue
        regions.append(
            MemoryRegion(
                name=match["name"],
                used=int(match["used"]) * _UNITS[match["used_unit"]],
                total=int(match["total"]) * _UNITS[match["total_unit"]],
                percent=float(match["percent"]),
            )
        )
    return regions
