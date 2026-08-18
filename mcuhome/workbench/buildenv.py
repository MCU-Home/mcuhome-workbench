# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""What this host knows about the build image, before anything runs.

ADR 0007 makes the build container *the* build environment: the host
needs git and docker, everything else — Zephyr SDK, west, gn, zap,
ccache — lives in an image versioned in lockstep with the Zephyr pin.
This module is the small half of that which is decided **outside** a
build: which environment a build *states* it wants
(:func:`environment_reference`), which container program to drive,
whether that program and its daemon are there at all (:func:`preflight`,
two refusals with two different fixes), getting the pinned image onto
this host when it is not there yet (:func:`ensure_image`), and where this
user's compiler cache lives (:func:`ccache_directory`).

Choosing *which* image satisfies that statement is not a lookup on this
host — it is a question for a registry — and lives in
:mod:`mcuhome.workbench.resolve_env`.

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
from typing import Any

from mcuhome.model.buildimage import (
    CCACHE_DIR_VAR,
    CONTAINER_HOME,
    DEFAULT_ENVIRONMENT,
    DOCKER_VAR,
    IMAGE,
    IMAGE_REPOSITORY,
    IMAGE_REVISION,
    IMAGE_TAG,
    IMAGE_VAR,
    ZEPHYR_RELEASE,
)
from mcuhome.model.context import EnvironmentPin
from mcuhome.model.errors import BuildError
from mcuhome.model.userpaths import expand, home

__all__ = [
    "CCACHE_DIR_VAR",
    "CONTAINER_HOME",
    "DEFAULT_ENVIRONMENT",
    "DOCKER_VAR",
    "IMAGE",
    "IMAGE_REPOSITORY",
    "IMAGE_REVISION",
    "IMAGE_TAG",
    "IMAGE_VAR",
    "ZEPHYR_RELEASE",
    "ccache_directory",
    "docker_program",
    "ensure_image",
    "environment_reference",
    "local_address",
    "preflight",
]

# The image itself — what it is called, which Zephyr release it carries,
# which environment variables move it — is stated once, in the vocabulary
# package both this repository and a workbench read
# (:mod:`mcuhome.model.buildimage`). Re-exported here so that a caller
# asking this module about the image keeps getting an answer.


def environment_reference(
    env: dict[str, str],
    *,
    stated: str = "",
    override: str | None = None,
) -> str:
    """Which build environment a build *asks for*, most specific first.

    ``--container-image`` beats the variable, the variable beats what the
    device model states, and a model that states nothing falls back to
    the environment this MCUHome ships with. The answer is a reference,
    not an image: resolving it to one image is
    :func:`~mcuhome.workbench.resolve_env.resolve_environment`'s, and
    every rung above may be a bare repository just as the model's own
    value is.
    """
    if override:
        return override
    return env.get(IMAGE_VAR) or stated or DEFAULT_ENVIRONMENT


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

#: Asks this host about one image by name; anything falsy means "not
#: here". Structural so that a caller with a full ``docker image
#: inspect`` and a caller that only needs a yes or no can use the same
#: rule for *which name* to ask about.
Lookup = Callable[[str], Any]

#: Where a fetched image's progress goes, line by line — the build log.
LineSink = Callable[[str], None]

#: Runs a command and forwards its output line by line, answering with
#: the exit status (``None`` when the program does not exist). The second
#: impure thing, and separate from :data:`Runner` because the two differ
#: in exactly what a test wants to control: whether output is seen.
Puller = Callable[[Sequence[str], dict[str, str], "LineSink | None"], int | None]


def _pull_streaming(
    command: Sequence[str], env: dict[str, str], on_line: LineSink | None
) -> int | None:
    """Run *command*, forwarding every line it writes, and answer with its status.

    Both streams into one, because docker writes pull progress to stderr
    and the point is to show the user what is happening rather than to
    tell the two apart.
    """
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            list(command),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )
    except OSError:
        return None
    with process:
        assert process.stdout is not None
        for line in process.stdout:
            if on_line is not None:
                on_line(line.rstrip("\n"))
    return process.returncode


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


def preflight(
    docker: str,
    *,
    env: dict[str, str],
    runner: Runner | None = None,
) -> None:
    """Refuse before the build starts, naming the one thing that is wrong.

    Two failures with two different fixes — no docker, no daemon — and a
    build that dies ten seconds in with somebody else's error text does
    not tell them apart. A missing *image* is no longer one of them: it
    is fetched (:func:`ensure_image`) rather than complained about.

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


def ensure_image(
    docker: str,
    pin: EnvironmentPin,
    *,
    env: dict[str, str],
    runner: Runner | None = None,
    puller: Puller | None = None,
    on_line: LineSink | None = None,
) -> tuple[str, bool]:
    """Have the pinned image on this host, fetching it if it is not here.

    Answers the address this host knows it by (:func:`local_address`) and
    whether it had to fetch, so a caller can say the second out loud — a
    1.3 GB download is not something to do silently behind a progress bar
    that claims to be compiling.

    **A missing image stopped being a refusal here.** The reference is
    pinned to a digest by the time anything reaches this function, which
    is what makes fetching safe: there is exactly one set of bytes that
    answers to it, and either they arrive or the pull fails. Asking the
    user to type a pull command for a name MCUHome resolved itself was
    only ever right while the name came from a constant they could have
    chosen differently.

    The pull's own output is the progress report — docker writes layer
    counts and percentages, and forwarding them beats inventing a
    spinner over a five-minute silence.
    """
    runner = _run_quiet if runner is None else runner
    address, present = local_address(
        lambda name: runner([docker, "image", "inspect", name], env) == 0 or None, pin
    )
    if present:
        return address, False
    puller = _pull_streaming if puller is None else puller
    status = puller([docker, "pull", pin.reference], env, on_line)
    if status is None:
        raise _refuse_no_docker(docker)
    if status != 0:
        raise _refuse_pull_failed(docker, pin.reference)
    return pin.reference, True


def _refuse_pull_failed(docker: str, reference: str) -> BuildError:
    """The image could not be fetched, and the build cannot start without it."""
    return BuildError(
        f"MCUHome could not fetch the build container {reference}.",
        hint=(
            "the pull is above this message with the reason. The usual ones are no "
            "network, a registry that needs a login, and a private repository:\n"
            f"    {docker} login <registry>\n"
            "mcuhome device build --help shows how to select a different container "
            "image or how to choose another build mode."
        ),
    )


def local_address(inspect: Lookup, pin: EnvironmentPin) -> tuple[str, Any]:
    """How **this host** names the pinned environment, and what it knows about it.

    A pin is one string, and there are two kinds of host it has to work
    on. Where the image came from a registry, its full reference —
    ``repo:tag@sha256:…`` — is what docker resolves and what belongs in
    a log, so that is the address. Where it was **built here and never
    pushed**, no registry ever named those bytes: the pin then carries
    the image's own ID, docker has no manifest digest to match it
    against, and the address is the digest by itself, which is docker's
    ordinary "run this image by ID".

    Returned together with whatever the lookup answered, so the caller
    pays for one inspect rather than two and "not here at all" is one
    answer (``(address, None)``) rather than a second call to find out.

    *inspect* is structural: the backend hands it a real ``docker image
    inspect`` that returns image facts, the preparation hands it one that
    only answers whether the name exists. Both are asking the same
    question and the answer's shape is the caller's business.

    Written once and used from both sides — the preparation that fetches
    the image and the backend that runs it — because two spellings of
    "which name does this host know it by" is how one of them starts
    running an image the other could not find.
    """
    facts = inspect(pin.reference)
    if facts is not None:
        return pin.reference, facts
    digest = pin.digest
    return digest, inspect(digest)
