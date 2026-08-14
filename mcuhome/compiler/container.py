# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Pipeline stage 5, container path: the normal way MCUHome compiles.

ADR 0007 makes the builder image *the* build environment: the host needs
git and docker, everything else — Zephyr SDK, west, gn, zap, ccache —
lives in an image versioned in lockstep with the Zephyr pin
(``containers/build-container/``). :mod:`mcuhome.compiler.workspace` keeps the
``local-dev`` escape hatch for people who already have a west workspace
with a toolchain in it, which is MCUHome's own contributors and nobody else.

The seam block C left behind is exactly the one used here:
:func:`plan_build` returns the same :class:`~mcuhome.compiler.workspace.BuildPlan`
the local-dev path returns — a command plus an environment — so everything
after it (running the build, reading the memory report, listing the
images) is shared code. The command happens to start with ``docker run``
and the west invocation inside it is assembled by the very same
:func:`~mcuhome.compiler.workspace.west_build_command`.

**Same paths inside and outside.** The workspace is bind-mounted at the
absolute path it has on the host, not at some ``/workspace``. A CMake
build tree is full of absolute paths, so this is what makes a build
directory usable from both sides — ``local-dev`` after a container build,
a debugger on the host, an error message a human can copy. The price is
that container paths mirror host paths, which is a price worth paying.

**Nothing is left behind owned by root.** The container runs as the
calling user's UID/GID, so the build directory, the generated files and
the ccache belong to the person who asked for them.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from mcuhome.compiler import workspace
from mcuhome.model.errors import BuildError
from mcuhome.model.userpaths import expand, home

__all__ = [
    "CCACHE_DIR_VAR",
    "CONTAINER_CCACHE_DIR",
    "CONTAINER_HOME",
    "DOCKER_VAR",
    "IMAGE",
    "IMAGE_REPOSITORY",
    "IMAGE_REVISION",
    "IMAGE_TAG",
    "IMAGE_VAR",
    "NETWORK_VAR",
    "ZEPHYR_RELEASE",
    "ccache_directory",
    "container_environment",
    "docker_program",
    "docker_run_command",
    "image_reference",
    "mount_points",
    "plan_build",
    "preflight",
]

# --------------------------------------------------------------------------
# What the image is called
# --------------------------------------------------------------------------

#: Zephyr release the image is built for. **Lockstep rule (ADR 0007/0008):
#: this is the ``revision:`` of the ``zephyr`` project in ``west.yml``
#: without the leading ``v``.** Bumping one without the other is a bug.
ZEPHYR_RELEASE = "4.4.0"

#: Rebuilds of the image for the same Zephyr release: a new tool version,
#: a new Python dependency, a fix in the Dockerfile. Starts at 1 and is
#: bumped by whoever changes ``containers/build-container/``, in the same commit.
#:
#: r2 adds ``cryptography`` — MCUboot's imgtool imports it at module load,
#: so r1 could not build the MCUboot image at all: sysbuild runs
#: ``imgtool getpub`` to generate ``autogen-pubkey.c`` on every build,
#: signing or not. Every Matter image built before 2026-08-09 therefore
#: came from the host method, where the host interpreter supplied it.
#:
#: r3 adds a real west workspace at ``/mcuhome/workspace`` (ADR 0020
#: decision E5): Zephyr, its modules, MCUboot and the Matter SDK at the
#: revisions ``west.yml`` pins, with ``patches/`` applied, plus a record
#: of what that is at ``/mcuhome/workspace.json``. The manifest
#: repository's directory is there and empty — ADR 0018 makes the SDK a
#: hash-pinned package fetched per build, so it is mounted, not baked.
#: The point is that ``git describe`` in the workspace decides
#: ``BUILD_VERSION`` and therefore the firmware bytes, so the workspace's
#: git state is a build input; baked, it is a property of the image
#: digest. Nothing reads it yet — this module still mounts the host's
#: workspace — which is why r3 changes no build output.
#:
#: From r3 on, ``west.yml`` and ``patches/`` are image inputs too: a
#: change to either needs a revision bump just as ``containers/build-container/``
#: does.
#:
#: r4 installs the program of the build-container contract at
#: ``/mcuhome/run`` (§2.2) — a thin launcher over :mod:`mcuhome.compiler.abi`,
#: which is where the invocation ABI and the actions of §7 live. That
#: module is **not** image content: it arrives with the SDK mount, so
#: adding an action to it does not change this image and does not bump
#: this number.
#:
#: r5 adds the ``org.mcuhome.*`` labels of §2.1 and nothing else. The
#: conformance claim became true elsewhere — all three actions of §7
#: implemented, and §4's D1 erratum sanctioning the declared SDK mount
#: point — but a label is image metadata, and image metadata is an image
#: change (the tag is the content identity, so a label under an old tag
#: would make two different images answer to one name).
#:
#: r6 = the launcher follows the package split, nothing else. ADR 0020's
#: migration moved the invocation ABI to :mod:`mcuhome.compiler.abi`, and
#: ``/mcuhome/run`` is the one file that names it by its import path —
#: image content, unlike the module it launches, which still arrives with
#: the SDK mount.
#:
#: r7 = ``/mcuhome/describe.json`` and the label grammar, which are the
#: same subject seen twice: everything a backend may learn about this
#: image *before* it starts a container. The file is §2.2's optional
#: static self-description, and the Dockerfile generates it by running
#: the program's own ``describe`` at image build time, so it cannot say
#: anything the program would not. It matters because §6.1 splits this
#: program in two — the launcher is image content, the body arrives with
#: the SDK mount — and a backend that has not chosen a mount point yet
#: therefore has no way to ask. The labels are the older half of the same
#: promise and were unusable in all three spellings until here: a name
#: ADR 0020's rename walked into, a Zephyr value carrying the ``v``
#: §2.1.1 forbids, and a toolchain value with a ``/`` outside the
#: permitted character class. A constraint is evaluated against those
#: values, and "a container that does not carry a named label does not
#: qualify" — so this image satisfied no SDK release's constraint at all.
IMAGE_REVISION = 8

#: GitHub Container Registry under the MCUHome organization. The package
#: is private while the repositories are; ``docker pull`` then needs a
#: ``docker login ghcr.io`` with a token that can read packages.
IMAGE_REPOSITORY = "ghcr.io/mcu-home/build-container"

#: ``zephyr-<line>-r<revision>``, and never ``latest``: a build
#: environment that changes under a stable name is not one. CI also
#: publishes the moving ``zephyr-<line>`` alias for people who want the
#: newest revision of a Zephyr line, but the builder never asks for it.
IMAGE_TAG = f"zephyr-{ZEPHYR_RELEASE}-r{IMAGE_REVISION}"

#: The image this version of the builder compiles in.
IMAGE = f"{IMAGE_REPOSITORY}:{IMAGE_TAG}"

#: Overrides :data:`IMAGE`. For a locally built image, a mirror, or a
#: bisect across image revisions. ``--image`` beats it, it beats the
#: default.
IMAGE_VAR = "MCUHOME_BUILDER_IMAGE"

#: Overrides where the ccache lives on the host. Useful for putting it on
#: a faster disk, or for sharing one cache between checkouts.
CCACHE_DIR_VAR = "MCUHOME_CCACHE_DIR"

#: Overrides the container program. ``podman`` is command-line compatible
#: for everything used here; it is not tested, hence a variable and not a
#: documented feature.
DOCKER_VAR = "MCUHOME_DOCKER"

#: Lets a build see the network. **Not a feature — a bisect tool.**
#: ``build-container-contract.md`` §9 makes "no network during commands" a
#: backend-enforced guarantee the build is allowed to rely on: everything
#: a build needs is mounted or in the image, and anything that reaches out
#: is by definition an undeclared input. Set it to ``1`` only to find out
#: *what* reaches out when a build unexpectedly needs the network; leave
#: it alone otherwise, because a build that quietly succeeds only with
#: network access is not reproducible anywhere else.
NETWORK_VAR = "MCUHOME_BUILD_NETWORK"

#: Where the ccache is mounted inside the container. Matches the
#: ``CCACHE_DIR`` the image already sets, so ``docker run <image> ccache
#: -s`` with the same mount reports on the same cache.
CONTAINER_CCACHE_DIR = "/ccache"

#: A writable ``HOME`` for a UID that has no entry in the container's
#: ``/etc/passwd`` — which is the normal case, because the UID comes from
#: the host. Without it, tools that cache in ``$HOME`` fail obscurely.
CONTAINER_HOME = "/tmp/mcuhome-home"

#: Where the Dockerfile lives, relative to the repository root — quoted
#: in the "you have no image" message, so it has to stay true.
DOCKERFILE_DIR = "containers/build-container"


def image_reference(env: dict[str, str], *, override: str | None = None) -> str:
    """Which image to build in: ``--image``, then the variable, then the pin."""
    if override:
        return override
    return env.get(IMAGE_VAR) or IMAGE


def docker_program(env: dict[str, str]) -> str:
    """The container program to drive."""
    return env.get(DOCKER_VAR) or "docker"


def ccache_directory(env: dict[str, str]) -> Path:
    """Where the compiler cache lives on the host.

    A host directory rather than a named docker volume, for one reason
    that decides it: a fresh named volume is created root-owned, and a
    container running as the calling user cannot write to it. A directory
    the builder creates itself has the right owner from the start — and
    can be inspected, backed up and deleted with ordinary tools.
    """
    override = env.get(CCACHE_DIR_VAR)
    if override:
        return expand(override, env)
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
            "If you already have a west workspace with a Zephyr toolchain in it, "
            "--method local-dev compiles there instead."
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
            "and log in again. --method local-dev compiles on the host instead."
        ),
    )


def _refuse_missing_image(docker: str, reference: str) -> BuildError:
    return BuildError(
        f"The MCUHome builder image {reference} is not on this machine.",
        hint=(
            "pull it — it is published with every MCUHome release:\n"
            f"    {docker} pull {reference}\n"
            "or build it from the root of this repository (the context is the\n"
            "repository, because west.yml and patches/ are image inputs):\n"
            f"    {docker} build -t {reference} -f {DOCKERFILE_DIR}/Dockerfile .\n"
            f"{IMAGE_VAR} selects a different image, --image does it per build, and "
            "--method local-dev skips the container altogether."
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
        raise _refuse_missing_image(docker, reference)


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------


def mount_points(topdir: Path, *extras: Path) -> list[Path]:
    """The host directories the container has to see, outermost first.

    The workspace is always one of them. Anything else is only mounted
    when it is not already inside the workspace, which is the case for a
    ``--build-dir`` somewhere else entirely, and for a builder installed
    next to the workspace rather than in it.
    """
    mounts = [topdir]
    for extra in extras:
        resolved = extra.resolve()
        if any(resolved == mount or mount in resolved.parents for mount in mounts):
            continue
        mounts.append(resolved)
    return mounts


def container_environment(
    *,
    topdir: Path,
    pyshim_dir: Path,
    jobs: int,
) -> dict[str, str]:
    """What the build needs set inside the container, and nothing else.

    Deliberately not the caller's environment: the point of a build
    container is that the result does not depend on the shell it was
    started from. The Zephyr SDK, the toolchain variant and the tool
    paths are baked into the image; what has to be said here is only what
    depends on where the workspace is.

    The variables a Matter build needs wherever it runs — the codegen
    shim on ``PYTHONPATH``, ``ZEPHYR_BASE``, ``HOME``, and the two job
    caps that nothing inherits — come from
    :func:`~mcuhome.compiler.workspace.build_environment`, which is their one
    definition. What this function adds is the two values that are true
    of a ``docker run`` and of nothing else: the cache is at the fixed
    mount point, not wherever the host keeps it.

    ``PATH`` is deliberately **not** here. It is the image's, set by the
    Dockerfile and inherited by the process docker starts; stating a
    second one would replace the toolchain the image installed with
    whatever this host happens to have.
    """
    env = workspace.build_environment(
        {},
        jobs=jobs,
        # west does not export ZEPHYR_BASE, and the generated CMakeLists
        # looks for the Matter SDK next to it. Stated unconditionally: the
        # workspace is the one this command mounts, not one to go looking
        # for.
        zephyr_base=topdir / "zephyr",
        # CHIP's codegen imports a helper its release tarball is missing
        # (scripts/pyshim/README.md).
        pyshim_dir=pyshim_dir,
        home=Path(CONTAINER_HOME),
    )
    env["CCACHE_DIR"] = CONTAINER_CCACHE_DIR
    # Absolute paths under the workspace are hashed relative to it, so one
    # cache serves several build directories — and, because the mount is
    # path-identical, the same cache serves a local-dev build.
    env["CCACHE_BASEDIR"] = str(topdir)
    return env


def docker_run_command(
    *,
    docker: str,
    image: str,
    mounts: Sequence[Path],
    ccache_dir: Path,
    workdir: Path,
    environment: dict[str, str],
    command: Sequence[str],
    user: str | None = None,
    read_only_mounts: Sequence[Path] = (),
    network: bool = False,
) -> list[str]:
    """``docker run`` around *command*, with the workspace mounted.

    ``--rm`` because the container is a process, not a place: everything
    worth keeping is on a mount. ``--init`` because a build spawns
    hundreds of short-lived children and PID 1 has to reap them.

    ``--network none`` because the contract says so and because it is the
    only way the statement can be true. "Everything a build needs is
    mounted or in the image" is not a property one can read off a build
    log: a step that fetches something succeeds silently on the machine
    that has the network and fails on the one that does not, which is
    exactly the class of defect that hid three undeclared build inputs
    until 2026-08-09. Taking the network away turns the claim into
    something the build either satisfies or does not. *network* is the
    bisect escape hatch of :data:`NETWORK_VAR`, not an option.

    *read_only_mounts* are single files rather than trees, and there is
    exactly one today: the key file the build is given. On a normal build
    that is the firmware signing key, mounted because imgtool runs inside
    the container and read-only because nothing in there has any business
    writing a key — the file itself stays where :mod:`mcuhome.workbench.signing`
    put it, outside every repository and every build directory. On a
    ``--no-sign`` build it is the *public* half instead, which the
    bootloader compiles in and which nothing can sign with; that is the
    whole point of ADR 0015 decision 8, and it is why the mount is a
    single named file rather than a directory in either case.
    """
    argv = [docker, "run", "--rm", "--init"]
    if not network:
        argv.append("--network=none")
    if user is not None:
        argv += ["--user", user]
    for mount in mounts:
        argv += ["--volume", f"{mount}:{mount}"]
    for mount in read_only_mounts:
        argv += ["--volume", f"{mount}:{mount}:ro"]
    argv += ["--volume", f"{ccache_dir}:{CONTAINER_CCACHE_DIR}"]
    argv += ["--workdir", str(workdir)]
    for name in sorted(environment):
        argv += ["--env", f"{name}={environment[name]}"]
    argv.append(image)
    argv += list(command)
    return argv


def _current_user() -> str | None:
    """``uid:gid`` of whoever is asking, where that is a thing."""
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is None or getgid is None:  # pragma: no cover - not POSIX
        return None
    return f"{getuid()}:{getgid()}"


def plan_build(
    *,
    out_dir: Path,
    app_subdir: str,
    board: str,
    snippets: tuple[str, ...] = (),
    bootloader_snippets: tuple[str, ...] = (),
    signing_key: Path | None = None,
    detached_signing: bool = False,
    env: dict[str, str],
    module_dir: Path,
    cwd: Path,
    jobs: int,
    image: str | None = None,
    runner: Runner | None = None,
) -> workspace.BuildPlan:
    """Resolve image, mounts and command, or refuse with a reason.

    Compiles nothing. The one thing it writes is the ccache directory,
    and only because docker would otherwise create the bind-mount source
    itself, owned by root, in a build that is meant to leave no
    root-owned anything behind.

    *module_dir* and *cwd* mean what they mean in
    :func:`mcuhome.compiler.workspace.plan_build`, and are required for the same
    reason: on this path the module directory is additionally a mount,
    because the builder may be installed next to the workspace rather
    than in it.
    """
    topdir = workspace.require_topdir(module_dir, cwd)
    host_env = dict(env)
    docker = docker_program(host_env)
    reference = image_reference(host_env, override=image)
    preflight(docker, reference, env=host_env, runner=runner)

    cache = ccache_directory(host_env)
    cache.mkdir(parents=True, exist_ok=True)

    app_dir = out_dir / app_subdir
    build_dir = out_dir / workspace.BUILD_SUBDIR
    inner = workspace.west_build_command(
        app_dir=app_dir,
        build_dir=build_dir,
        board=board,
        snippets=snippets,
        bootloader_snippets=bootloader_snippets,
        signing_key=signing_key,
        detached_signing=detached_signing,
        jobs=jobs,
        pristine=workspace.pristine_mode(build_dir),
    )
    return workspace.BuildPlan(
        topdir=topdir,
        app_dir=app_dir,
        build_dir=build_dir,
        command=docker_run_command(
            docker=docker,
            image=reference,
            mounts=mount_points(topdir, out_dir, module_dir),
            ccache_dir=cache,
            workdir=topdir,
            environment=container_environment(
                topdir=topdir,
                pyshim_dir=module_dir / workspace.PYSHIM_SUBDIR,
                jobs=jobs,
            ),
            command=inner,
            user=_current_user(),
            # Contract §9 makes the isolation the backend's job, so it is
            # the default here and the variable is the way out, not in.
            network=host_env.get(NETWORK_VAR, "") not in ("", "0"),
            # The key file itself, not its directory: the container needs
            # to read exactly one secret and gets exactly that one.
            read_only_mounts=() if signing_key is None else (signing_key,),
        ),
        env=host_env,
        image=reference,
    )
