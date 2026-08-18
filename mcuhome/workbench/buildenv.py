# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""What this host knows about the build image, before anything runs.

ADR 0007 makes the build container *the* build environment: the host
needs git and docker, everything else — Zephyr SDK, west, gn, zap,
ccache — lives in an image versioned in lockstep with the Zephyr pin.
This module is the small half of that which is decided **outside** a
build: which image to run (:func:`image_reference` over the default
:mod:`mcuhome.model.buildimage` states), which container program to
drive, whether that program and its daemon are there at all
(:func:`preflight`, three refusals with three different fixes), and where
this user's compiler cache lives (:func:`ccache_directory`).

Everything here is a **lookup on this host**, and that is the line
between this module and the two around it. What the image *is* — its
name, its revision, the Zephyr release it carries — is not this host's
to decide and is stated once, in the vocabulary package the repository
that builds the image also reads (:mod:`mcuhome.model.buildimage`).
Driving the container is not a lookup and is
:mod:`mcuhome.workbench.orchestrator`, which starts it, mounts what a
session needs at the paths of :mod:`mcuhome.model.containerpaths`, and
speaks the invocation ABI.

The constants are re-exported here for a caller that has one of these
functions in hand and wants the default it falls back to.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from mcuhome.model.buildimage import (
    CCACHE_DIR_VAR,
    CONTAINER_HOME,
    DOCKER_VAR,
    IMAGE,
    IMAGE_REPOSITORY,
    IMAGE_REVISION,
    IMAGE_TAG,
    IMAGE_VAR,
    ZEPHYR_RELEASE,
)
from mcuhome.model.errors import BuildError
from mcuhome.model.userpaths import expand, home

__all__ = [
    "CCACHE_DIR_VAR",
    "CONTAINER_HOME",
    "DOCKER_VAR",
    "IMAGE",
    "IMAGE_REPOSITORY",
    "IMAGE_REVISION",
    "IMAGE_TAG",
    "IMAGE_VAR",
    "ZEPHYR_RELEASE",
    "ccache_directory",
    "docker_program",
    "image_reference",
    "missing_image_refusal",
    "preflight",
]

# The image itself — what it is called, which Zephyr release it carries,
# which environment variables move it — is stated once, in the vocabulary
# package both this repository and a workbench read
# (:mod:`mcuhome.model.buildimage`). Re-exported here so that a caller
# asking this module about the image keeps getting an answer.


def image_reference(env: dict[str, str], *, override: str | None = None) -> str:
    """Which image to build in: ``--container-image``, then the variable, then the pin."""
    if override:
        return override
    return env.get(IMAGE_VAR) or IMAGE


def docker_program(env: dict[str, str]) -> str:
    """The container program to drive."""
    return env.get(DOCKER_VAR) or "docker"


def ccache_directory(env: dict[str, str]) -> Path:
    """Where the compiler cache lives on the host — the root of both roles.

    A host directory rather than a named docker volume, for reasons that
    decide it together: a fresh named volume is created root-owned and a
    container running as the calling user cannot write to it; the shared
    half is meant to be filled from outside, and there is no way into a
    named volume without starting a container; and the cache has to be
    listable, movable and deletable when docker is not running at all
    (the ``subprocess`` profile has no docker in the first place). On
    Linux the two are the same bind mount underneath, so nothing is
    traded away for it.

    One cache per user, not per project. Its keys are content addresses —
    the preprocessed source, the compiler's own bytes, the command line —
    so two projects share an entry exactly when the compilation is the
    same compilation, and a per-project split would cost the sharing
    while protecting nothing. Isolation between *parties* is a different
    question with a different answer, and it belongs to whoever serves
    more than one of them.
    """
    override = env.get(CCACHE_DIR_VAR)
    if override:
        return expand(override, env)
    if os.name == "nt":
        # LOCALAPPDATA, not APPDATA: the latter roams, and a five-gigabyte
        # compiler cache has no business being copied to a file server at
        # every logon. An environment that names neither falls through to
        # the POSIX form below, which is wrong on Windows but is a path
        # rather than a crash.
        local = env.get("LOCALAPPDATA")
        if local:
            return expand(local, env) / "mcuhome" / "ccache"
    cache_home = env.get("XDG_CACHE_HOME")
    base = expand(cache_home, env) if cache_home else home(env) / ".cache"
    return base / "mcuhome" / "ccache"


# --------------------------------------------------------------------------
# Is there a container runtime at all?
# --------------------------------------------------------------------------

#: Runs a command, discards its output, and answers with the exit status —
#: or ``None`` when the program does not exist. The one impure thing in
#: this module, injectable so the test suite never needs docker.
Runner = Callable[[Sequence[str], dict[str, str]], int | None]


def _run_quiet(command: Sequence[str], env: dict[str, str]) -> int | None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            list(command),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    return completed.returncode


def _refuse_no_docker(docker: str) -> BuildError:
    return BuildError(
        f"MCUHome compiles in a container and cannot find {docker} on your PATH.",
        hint=(
            "install Docker — https://docs.docker.com/engine/install/ — and run the "
            "same command again. That is the whole host setup: the Zephyr SDK, gn, "
            "zap and ccache live in the MCUHome builder image, not on your machine.\n"
            "Building without a container is a build mode: mcuhome device build --help."
        ),
    )


def _refuse_no_daemon(docker: str) -> BuildError:
    return BuildError(
        f"MCUHome found {docker}, but cannot talk to the Docker daemon.",
        hint=(
            "start it and run the same command again:\n"
            "    sudo systemctl start docker      # Linux, system service\n"
            "    open -a Docker                   # macOS, Docker Desktop\n"
            f"If {docker} only works under sudo, add yourself to the `docker` group "
            "and log in again. Building without a container is a build mode: "
            "mcuhome device build --help."
        ),
    )


def missing_image_refusal(docker: str, reference: str) -> BuildError:
    """The one missing-image refusal, wherever the absence is noticed.

    PO-worded (2026-08-15): what is missing, the pull command as the
    fix, and one pointer at the build command's help for choosing a
    different image or another build mode — nothing else. Both the
    docker preflight and the image-resolution seam raise exactly this,
    so the user reads one text however the build got there.
    """
    if reference == IMAGE:
        message = "The default build container is missing on this host."
    else:
        message = f"The build container {reference} is missing on this host."
    return BuildError(
        message,
        hint=(
            "pull the image, then rerun the build:\n"
            f"    {docker} pull {reference}\n"
            "mcuhome device build --help shows how to select a different "
            "container image or how to choose another build mode."
        ),
    )


def preflight(
    docker: str,
    reference: str,
    *,
    env: dict[str, str],
    runner: Runner | None = None,
) -> None:
    """Refuse before the build starts, naming the one thing that is wrong.

    Three failures with three different fixes — no docker, no daemon, no
    image — and a build that dies ten seconds in with somebody else's
    error text does not tell them apart.

    *runner* defaults to :func:`_run_quiet`, resolved here rather than in
    the signature: a default bound at definition time cannot be replaced
    by monkeypatching the module, and a test that thinks it stubbed
    docker out but did not is a test that starts a real build.
    """
    runner = _run_quiet if runner is None else runner
    status = runner([docker, "version", "--format", "{{.Server.Version}}"], env)
    if status is None:
        raise _refuse_no_docker(docker)
    if status != 0:
        raise _refuse_no_daemon(docker)
    if runner([docker, "image", "inspect", reference], env) != 0:
        raise missing_image_refusal(docker, reference)
