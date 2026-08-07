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
:func:`build_images` and :func:`parse_image_memory_report` included.

**Two images, since ADR 0015.** Every MCUHome device boots through
MCUboot, and vanilla Zephyr builds a bootloader only under sysbuild, so
what this module drives is ``west build --sysbuild``: one build directory
with one sub-directory per image, a signed application, and a bootloader
that verifies it against the user's own key (:mod:`mcuhome.signing`).

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
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from mcuhome.errors import BuildError
from mcuhome.generate import BOOTLOADER_IMAGE

__all__ = [
    "BOOTLOADER_IMAGE",
    "BUILD_SUBDIR",
    "CHIP_JOBS_VAR",
    "CMAKE_JOBS_VAR",
    "JOBS_VAR",
    "MERGED_IMAGE_GLOB",
    "MODULE_DIR",
    "PYSHIM_DIR",
    "SIGNING_KEY_OPTION",
    "TOOLS",
    "BuildPlan",
    "ImageArtifacts",
    "MemoryRegion",
    "ResolvedJobs",
    "ToolNeed",
    "artifacts",
    "auto_jobs",
    "available_ram_bytes",
    "build_environment",
    "build_images",
    "detect_jobs",
    "find_topdir",
    "merged_image",
    "missing_tools",
    "parse_image_memory_report",
    "parse_memory_report",
    "plan_build",
    "pristine_mode",
    "refuse_failed_build",
    "require_tools",
    "require_topdir",
    "resolve_jobs",
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

#: Environment variable that overrides job-count auto-detection outright
#: (see :func:`resolve_jobs`) — the escape hatch for a machine
#: :func:`auto_jobs` still guesses wrong for, e.g. a container with a
#: cgroup memory limit ``/proc/meminfo`` does not reflect. ``--jobs`` on
#: the command line beats it; it beats auto-detection.
JOBS_VAR = "MCUHOME_JOBS"

#: Environment variable the vendored CHIP GN sub-build reads to cap its own
#: inner ``ninja`` invocation (patch hunk in
#: ``patches/connectedhomeip-v1.5.1.0-vanilla-zephyr.patch``, applied to
#: ``config/common/cmake/chip_gn.cmake``). Upstream always runs a bare
#: ``ninja`` there, so without this the outer ``-o=-j{jobs}`` above is
#: invisible to it and the Matter sub-build regenerates ninja's default of
#: nproc+2 — the exact OOM risk the resolved job count (:func:`resolve_jobs`)
#: exists to avoid, just one process tree down.
CHIP_JOBS_VAR = "MCUHOME_CHIP_JOBS"

#: CMake's own cap on ``cmake --build`` parallelism. Under sysbuild the
#: ``-o=-j{jobs}`` below only reaches the *outer* ninja: each image is an
#: ExternalProject whose build step is a fresh ``cmake --build .``, and
#: nothing of the outer invocation is inherited by it. Without this the
#: inner ninja falls back to its own default of nproc+2, which is the
#: exact OOM the resolved job count exists to prevent. Images do not build
#: concurrently (sysbuild puts their build steps in ninja's ``console``
#: pool), so one cap serves the whole run.
CMAKE_JOBS_VAR = "CMAKE_BUILD_PARALLEL_LEVEL"

#: Sysbuild Kconfig symbol naming the MCUboot signing key. Passed on the
#: command line and never written into the generated tree: it is the path
#: of a per-user secret (ADR 0015 decision 8, :mod:`mcuhome.signing`).
SIGNING_KEY_OPTION = "SB_CONFIG_BOOT_SIGNATURE_KEY_FILE"

#: Sysbuild's combined image, at the top of the build directory: every
#: image at its own offset in one file, which is what a full-chip flash
#: over a debug probe wants. Sysbuild writes one per board target and
#: names it after that target, hence a pattern rather than a name.
MERGED_IMAGE_GLOB = "merged_*.hex"

#: Written by sysbuild and by nothing else. Its presence is how a build
#: directory says which kind of build made it (see :func:`pristine_mode`).
_DOMAINS_FILE = "domains.yaml"

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
# How many jobs to run in parallel
# --------------------------------------------------------------------------

#: Bytes in a gibibyte, for the RAM half of :func:`auto_jobs`.
_GIB = 1024**3

#: Where Linux publishes live memory figures. A parameter on
#: :func:`available_ram_bytes` rather than a hardcoded path only inside
#: it, so the test suite can point it at a fixture file instead of
#: monkeypatching the standard library.
_MEMINFO_PATH = Path("/proc/meminfo")


def available_ram_bytes(path: Path | None = None) -> int:
    """Best-effort available RAM right now, without a psutil dependency.

    Reads ``MemAvailable`` from ``/proc/meminfo`` — Linux (kernel >= 3.14)
    already discounts reclaimable page cache from it, which is closer to
    "usable before swapping starts" than ``MemFree``. Where that key is
    missing — an old kernel, or *path* pointing nowhere, which is every
    non-Linux platform — this falls back to half of ``MemTotal``, a rough
    "assume something else already claimed half of it" heuristic that
    needs no new dependency. Where neither key is readable at all, this
    assumes nothing is available, which drives :func:`auto_jobs` to its
    floor of 2 rather than guessing high on a machine it cannot see.
    """
    meminfo = _MEMINFO_PATH if path is None else path
    try:
        text = meminfo.read_text("utf-8")
    except OSError:
        text = ""
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name not in ("MemAvailable", "MemTotal"):
            continue
        fields = rest.strip().split()
        if fields and fields[0].isdigit():
            values[name] = int(fields[0])  # kB, per proc(5)
    if "MemAvailable" in values:
        return values["MemAvailable"] * 1024
    if "MemTotal" in values:
        return (values["MemTotal"] * 1024) // 2
    return 0


def auto_jobs(cpu_count: int, available_ram_bytes: int) -> int:
    """Parallelism this hardware can sustain without swapping.

    ``min(cpu_count, max(2, available_ram_gb // 2))``. Measured CHIP C++
    compiles peak around 1-1.5 GiB per job; the final link spikes higher,
    but only one link runs at a time (ninja serializes it), so it does not
    change the per-job budget. Budgeting 2 GiB per job keeps a no-swap
    machine safe with headroom, and ``cpu_count`` remains the hard ceiling
    underneath that — more jobs than cores never builds faster. This
    development machine (4 cores / 15 GiB) resolves to 4; a 24-thread /
    24 GiB WSL machine resolves to 12. The floor of 2 matches the
    previous static default, so even a RAM-starved machine can still
    overlap one compile with the next.

    :param cpu_count: usually ``os.cpu_count()``.
    :param available_ram_bytes: usually :func:`available_ram_bytes`.
    """
    ram_gb = available_ram_bytes // _GIB
    return min(cpu_count, max(2, ram_gb // 2))


def detect_jobs() -> int:
    """:func:`auto_jobs`, fed this machine's live CPU count and free RAM."""
    return auto_jobs(os.cpu_count() or 1, available_ram_bytes())


@dataclass(frozen=True)
class ResolvedJobs:
    """A job count together with why it was chosen — for the build summary."""

    value: int
    #: ``"flag"`` (``--jobs``), ``"env"`` (:data:`JOBS_VAR`), or ``"auto"``
    #: (:func:`detect_jobs`).
    source: str


def resolve_jobs(*, cli_jobs: int | None = None, env: dict[str, str] | None = None) -> ResolvedJobs:
    """The parallelism this build uses, and why — the single resolution point.

    Precedence, most specific wins: ``--jobs`` on the command line, then
    :data:`JOBS_VAR` in the environment, then :func:`detect_jobs`. This
    runs once, on the host, before a container build even starts docker —
    the container would see the host's CPU count either way, but its RAM
    budget is the host's (or the WSL VM's), not a figure guessed at from
    inside a container that may itself be memory-limited by a cgroup.
    Everything downstream then takes the resulting number as given rather
    than resolving it again: the outer ``-o=-jN`` (:func:`west_build_command`),
    :data:`CHIP_JOBS_VAR` for the inner CHIP GN sub-build
    (:func:`build_environment`), and the container path
    (:mod:`mcuhome.container`), which receives it as a plain ``jobs``
    argument like the native path does.

    A :data:`JOBS_VAR` that is not a positive whole number is treated as
    unset rather than refused: a typo in a shell rc file should not be
    able to break every build until someone finds it, and auto-detection
    is always a reasonable answer.
    """
    if cli_jobs is not None:
        return ResolvedJobs(cli_jobs, "flag")
    environment = os.environ if env is None else env
    raw = environment.get(JOBS_VAR)
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed >= 1:
            return ResolvedJobs(parsed, "env")
    return ResolvedJobs(detect_jobs(), "auto")


# --------------------------------------------------------------------------
# Environment and prerequisites
# --------------------------------------------------------------------------


def build_environment(env: dict[str, str] | None = None, *, jobs: int) -> dict[str, str]:
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
    # CHIP_JOBS_VAR — and so does each sysbuild image's own inner ninja,
    # see CMAKE_JOBS_VAR.
    prepared[CHIP_JOBS_VAR] = str(jobs)
    prepared[CMAKE_JOBS_VAR] = str(jobs)
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


def pristine_mode(build_dir: Path) -> str:
    """``"auto"``, or ``"always"`` for a build directory of the wrong kind.

    ``auto`` is what a normal rebuild wants: west re-runs CMake from
    scratch when the board or the application directory changed and keeps
    the object files otherwise, which is the difference between a
    ten-minute and a ten-second edit cycle on the Matter tree.

    It cannot cover one case, and that case is a migration every existing
    build directory hits exactly once: a directory built before ADR 0015
    holds a single-image CMake tree whose source directory is the
    application, and sysbuild's is Zephyr's own ``share/sysbuild``. CMake
    refuses that with a message about the source directory not matching,
    which is true and unhelpful. Sysbuild writes :data:`_DOMAINS_FILE` and
    a single-image build does not, so the two are told apart by looking.
    """
    if not (build_dir / "CMakeCache.txt").is_file():
        return "auto"
    return "auto" if (build_dir / _DOMAINS_FILE).is_file() else "always"


def west_build_command(
    *,
    app_dir: Path,
    build_dir: Path,
    board: str,
    snippets: tuple[str, ...] = (),
    bootloader_snippets: tuple[str, ...] = (),
    signing_key: Path | None = None,
    jobs: int,
    pristine: str = "auto",
) -> list[str]:
    """The ``west build --sysbuild`` invocation for one generated application.

    **Snippets are named per image, never once.** Sysbuild hands a bare
    ``SNIPPET`` down to every image it builds, and an application's
    snippets do not merely fail to apply in a bootloader — ``-S matter``
    puts CHIP's heap sizing and a symbol MCUboot has never heard of into
    MCUboot's Kconfig, and an assignment to an undefined symbol stops the
    build. So the application's list goes to ``<app>_SNIPPET`` (sysbuild
    names the main image after its directory) and the bootloader's to
    ``mcuboot_SNIPPET``, and the global name is left unset.

    **The signing key is an argument, not a file in the tree.** Leaving
    it out is not an error to sysbuild: MCUboot's default is its own demo
    key, whose private half is published. :mod:`mcuhome.signing` is what
    makes sure there is a real one to pass.
    """
    command = [
        "west",
        "build",
        "--board",
        board,
        "--build-dir",
        str(build_dir),
        "--pristine",
        pristine,
        "--sysbuild",
    ]
    # `-o=` and not `-o `: the value starts with a dash, and argparse reads
    # a separate token starting with a dash as the next option.
    command.append(f"-o=-j{jobs}")
    command.append(str(app_dir))

    options: list[str] = []
    if snippets:
        options.append(f"-D{app_dir.name}_SNIPPET={';'.join(snippets)}")
    if bootloader_snippets:
        options.append(f"-D{BOOTLOADER_IMAGE}_SNIPPET={';'.join(bootloader_snippets)}")
    if signing_key is not None:
        # The quotes are part of the value, not shell syntax: CMake writes
        # this straight into a Kconfig fragment, where an unquoted string
        # assignment is a "malformed string literal ... Assignment
        # ignored" warning. Zephyr turns Kconfig warnings into errors, so
        # today that stops the build rather than silently leaving the
        # symbol at MCUboot's default, which is the demo key. Do not rely
        # on that: quote it.
        options.append(f'-D{SIGNING_KEY_OPTION}="{signing_key}"')
    if options:
        command.append("--")
        command += options
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
    bootloader_snippets: tuple[str, ...] = (),
    signing_key: Path | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    jobs: int,
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
            bootloader_snippets=bootloader_snippets,
            signing_key=signing_key,
            jobs=jobs,
            pristine=pristine_mode(build_dir),
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
            f"is in {build_dir}, one sub-directory per image. If the failure is "
            "in generated code rather than in your configuration, that is a "
            "builder bug worth reporting."
        ),
    )


# --------------------------------------------------------------------------
# What came out
# --------------------------------------------------------------------------

#: Image formats Zephyr writes that MCUHome has a use for today, in
#: reporting order: the ELF first because that is what a debugger wants,
#: then the raw forms, then the signed ones. The signed forms only exist
#: for images MCUboot chain-loads — the bootloader itself is not signed,
#: it *is* the trust anchor.
_ARTIFACT_NAMES = (
    "zephyr.elf",
    "zephyr.hex",
    "zephyr.bin",
    "zephyr.signed.hex",
    "zephyr.signed.bin",
    "zephyr.signed.confirmed.hex",
    "zephyr.signed.confirmed.bin",
    "zephyr.uf2",
)

#: The file each image is reported by size on. Zephyr's own end-of-build
#: ``Memory region / FLASH / Used Size`` is byte-identical to the size of
#: this file, which makes it the one number available for every image of a
#: multi-image build regardless of what survived in the log.
_FLASH_ARTIFACT = "zephyr.bin"

#: Human-readable role per sysbuild image name. Anything else is reported
#: under its own name without a role, which is what a future third image
#: (a firmware loader, a network-core image) should do until it is named
#: here on purpose.
_IMAGE_ROLES = {BOOTLOADER_IMAGE: "bootloader"}


def artifacts(build_dir: Path) -> list[Path]:
    """The images one image directory contains, in reporting order."""
    output = build_dir / "zephyr"
    return [output / name for name in _ARTIFACT_NAMES if (output / name).is_file()]


@dataclass(frozen=True)
class ImageArtifacts:
    """Everything one image of a sysbuild build produced."""

    #: Sysbuild's name for the image, which is also its sub-directory.
    name: str
    #: ``"bootloader"``, ``"application"``, or the image name again.
    role: str
    files: list[Path]
    #: Size of ``zephyr.bin``, i.e. what the linker put in flash, or None
    #: for an image that produced no raw binary.
    flash_bytes: int | None

    def describe(self) -> str:
        size = "" if self.flash_bytes is None else f"  {self.flash_bytes / 1024:.1f} KiB"
        return f"{self.name} ({self.role}){size}"


def build_images(build_dir: Path, *, app_image: str) -> list[ImageArtifacts]:
    """One entry per sysbuild image that produced something, bootloader first.

    Order is boot order, which is also install order and the order the
    two things a user does with them happen in.
    """
    found: list[ImageArtifacts] = []
    for name in (BOOTLOADER_IMAGE, app_image):
        files = artifacts(build_dir / name)
        if not files:
            continue
        binary = build_dir / name / "zephyr" / _FLASH_ARTIFACT
        found.append(
            ImageArtifacts(
                name=name,
                role=_IMAGE_ROLES.get(name, "application" if name == app_image else name),
                files=files,
                flash_bytes=binary.stat().st_size if binary.is_file() else None,
            )
        )
    return found


def merged_image(build_dir: Path) -> Path | None:
    """Sysbuild's every-image-in-one-file hex, when it wrote one.

    One per board target, and MCUHome builds one board target per device,
    so more than one match means something changed upstream — reported as
    none rather than as an arbitrary pick.
    """
    merged = sorted(build_dir.glob(MERGED_IMAGE_GLOB))
    return merged[0] if len(merged) == 1 else None


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


#: ``[1/2] Performing build step for 'mcuboot'`` — what the outer ninja
#: prints when it hands over to one image's own build. It is the only
#: marker in a sysbuild log that says whose output follows; Zephyr's
#: memory report itself names no image.
_IMAGE_STEP = re.compile(r"Performing build step for '(?P<image>[^']+)'")


def parse_image_memory_report(log: str, *, images: Sequence[str]) -> dict[str, list[MemoryRegion]]:
    """The footprint table of each image, by sysbuild image name.

    A sysbuild build prints one report per image, and nothing in the
    report says which image it belongs to — so the reports are attributed
    by the build-step banner that precedes them. An incremental build
    that relinked only one image reports only that one, which is correct
    rather than incomplete: the other image did not change.

    *images* is required and is not a convenience: the same banner is
    printed for every nested ExternalProject, and the Matter build has
    one — ``chip-gn``, which runs inside the application's build and
    would otherwise be credited with the application's footprint.
    Banners for anything not named here are ignored, so what follows them
    still belongs to the image whose build they are part of.

    Output ordering follows the log, which is the order sysbuild happened
    to build in and deliberately not something the caller should rely on.
    """
    known = set(images)
    by_image: dict[str, list[MemoryRegion]] = {}
    current: str | None = None
    for line in log.splitlines():
        step = _IMAGE_STEP.search(line)
        if step is not None:
            if step["image"] in known:
                current = step["image"]
            continue
        match = _REGION.match(line)
        if match is None or current is None:
            continue
        by_image.setdefault(current, []).append(
            MemoryRegion(
                name=match["name"],
                used=int(match["used"]) * _UNITS[match["used_unit"]],
                total=int(match["total"]) * _UNITS[match["total_unit"]],
                percent=float(match["percent"]),
            )
        )
    return by_image
