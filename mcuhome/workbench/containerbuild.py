# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""A container build, from the outside: which image, and one invocation.

:mod:`mcuhome.workbench.orchestrator` speaks the build-container contract
— given a *locked context directory* it drives one invocation through the
ABI. This module is the thin surface above it:
:func:`prepare_environment` turns what a device *says* about its build
environment into one image sitting on this host, and
:func:`run_locked_build` drives one ``build`` invocation over a context
somebody else created and locked.

The composition that puts the two together — model in, context created
and locked, build driven — is
:func:`mcuhome.workbench.buildmethods.compose_local_build`, and it is one
level up because creating a context is not container work: pin resolution
and the context layout are the same calls the ``remote`` method makes
(E65). Splitting it that way is what lets a caller that already holds a
context skip straight to here.

**The private key never reaches here.** Nothing in this module's surface
takes a key at all: a locked context carries the **public** half as
``keys/signing.pub`` (ADR 0015 decision 8) and that is all a build ever
sees of the key pair. The backend delivers an *unsigned* image plus the
§7.2.1 build report; the signature happens on the host afterwards, where
the private key already is (:mod:`mcuhome.workbench.imgtool`).

**One network call, and it is the registry's.** Choosing an environment
asks a registry which image it recommends
(:mod:`mcuhome.workbench.resolve_env`) and then fetches those exact
bytes if this host does not have them; everything after that is local.
The SDK package is still acquired from the operator's own source
directories and from nowhere else (contract v1's first tier, §9.1). A
registry that cannot be reached, a repository offering nothing that
fits, and a missing SDK package are typed refusals, not tracebacks ten
minutes into a build.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from mcuhome.model.context import MANIFEST_FILE, EnvironmentPin
from mcuhome.model.errors import ConfigError

from mcuhome.workbench import buildenv as container
from mcuhome.workbench import orchestrator as lb
from mcuhome.workbench.contextdir import read_context_manifest
from mcuhome.workbench.ociregistry import Registry
from mcuhome.workbench.resolve_env import ResolvedEnvironment, resolve_environment

__all__ = [
    "LocalBuildResult",
    "fetch_environment",
    "prepare_environment",
    "run_locked_build",
]


@dataclass(frozen=True)
class LocalBuildResult:
    """What one :func:`run_locked_build` produced, from the caller's side.

    :attr:`outcome` is the backend's own seven-part §5.3 answer — a caller
    checks :attr:`~mcuhome.workbench.orchestrator.LocalOutcome.successful`
    before trusting anything else. :attr:`out_dir` is where the delivered
    artifacts actually are (the unsigned ``firmware.*`` and the
    ``build-report.json`` a host signer consumes); :attr:`context_dir` is
    the locked context the build was attributed to; :attr:`image` is the
    reference it was built in.
    """

    outcome: lb.LocalOutcome
    out_dir: Path
    context_dir: Path
    image: str


def prepare_environment(
    stated: str,
    *,
    constraint: str,
    env: dict[str, str],
    override: str | None = None,
    registry: Registry | None = None,
    docker_seam: lb.Docker | None = None,
    on_line: lb.LineSink | None = None,
) -> tuple[ResolvedEnvironment, bool]:
    """From "which environment" to "these bytes, here" — before anything is created.

    Three steps, in the order that makes each refusal cheap. Is there a
    container runtime at all (two refusals with two different fixes).
    Which image does the repository recommend for the Zephyr this device
    needs, and does that image's own declaration agree (a registry
    question, answered without pulling anything). And finally: is it on
    this host, or does it have to be fetched.

    Answers the resolution and whether it had to fetch, so a caller can
    say the second out loud — a gigabyte-scale download deserves a line
    of its own rather than a silence in the middle of a build.

    *stated* is what the device model says, *override* the
    ``--container-image`` that beats it; both may be a bare repository, a
    tag or a digest. *constraint* is the model's resolved Zephyr
    requirement. *registry* is the seam a test replaces to answer
    without a network.
    """
    docker = container.docker_program(env)
    seam = docker_seam if docker_seam is not None else lb.Docker(docker)
    container.preflight(docker, env=env, runner=_asks(seam))
    reference = container.environment_reference(env, stated=stated, override=override)
    resolved = resolve_environment(
        reference, constraint=constraint, registry=registry, local=seam.inspect
    )
    _, fetched = fetch_environment(resolved.pin, env=env, docker_seam=seam, on_line=on_line)
    return resolved, fetched


def fetch_environment(
    pin: EnvironmentPin,
    *,
    env: dict[str, str],
    docker_seam: lb.Docker | None = None,
    on_line: lb.LineSink | None = None,
) -> tuple[str, bool]:
    """Get an already-decided environment onto this host.

    The second half of :func:`prepare_environment` on its own, for the
    caller that has nothing to resolve: a context it received already
    names the image, pinned, and that pin is part of the context's
    identity — so the only question left is whether those bytes are here.
    """
    docker = container.docker_program(env)
    seam = docker_seam if docker_seam is not None else lb.Docker(docker)
    container.preflight(docker, env=env, runner=_asks(seam))
    return container.ensure_image(
        docker,
        pin,
        env=env,
        runner=_asks(seam),
        puller=_streams(seam),
        on_line=on_line,
    )


def _asks(seam: lb.Docker) -> container.Runner:
    """The yes/no docker call, answered through the backend's own seam.

    Everything before a build starts is a docker command — is the daemon
    up, is the image here — and a caller that replaced the seam replaced
    docker. Routing these through it too is what makes that true, rather
    than leaving a test that stubbed the build running a real
    ``docker version`` beside it.
    """

    def asks(command, env):
        del env  # the seam was constructed with the program and the environment
        return seam.run(list(command)).status

    return asks


def _streams(seam: lb.Docker) -> container.Puller:
    """The same, for the one call whose output is worth watching."""

    def streams(command, env, on_line):
        del env
        return seam.run(list(command), on_line).status

    return streams


def _cache_root(env: dict[str, str], stated: Path | None) -> Path | None:
    """Where this machine keeps its compiler cache, or ``None`` for nowhere.

    The compiler cache belongs to the person building rather than to the
    build directory: it holds the same objects for every device and every
    project, and the working area it used to live in is wiped before each
    build. A caller that resolved a location through the configuration
    layers states it; otherwise the user's cache directory answers.

    **A home directory nobody named is not a refusal here.** A cache is
    an optimization, and a caller with no ``HOME`` — a service, a
    container, a test — is entitled to a build that simply has no cache
    and dies with its container. The refusal
    :func:`~mcuhome.model.userpaths.home` raises is right where it was
    written for, the signing key, and wrong for this.
    """
    if stated:
        return Path(stated)
    try:
        return container.ccache_directory(env)
    except ConfigError:
        return None


def run_locked_build(
    context_dir: Path,
    *,
    sdk_sources: Sequence[Path],
    work_root: Path,
    env: dict[str, str],
    jobs: int = 1,
    mode: str = "clean",
    ccache_dir: Path | None = None,
    on_line: lb.LineSink | None = None,
    docker: lb.Docker | None = None,
) -> LocalBuildResult:
    """Drive one ``build`` invocation over a locked context directory.

    The backend role and nothing else: *context_dir* was created and
    locked by the workbench, *sdk_sources* are the operator's local
    package directories the backend acquires the pinned SDK from (§9.1 —
    a backend duty, the hash decides), and *work_root* is the backend's
    own scratch area.

    **Which image is not a parameter.** The locked context names it,
    pinned to a digest, and it is the only thing that may: a build
    driven into a different environment than the one its identity claims
    is exactly what the pin exists to prevent.

    *docker* is the one seam — left ``None`` it drives real docker; a
    caller (or a test) injects a scripted
    :class:`~mcuhome.workbench.orchestrator.Docker` to drive the whole
    thing without a container runtime.
    """
    context_dir = Path(context_dir)
    work_root = Path(work_root)
    # Read back rather than taken as an argument, for the reason above:
    # the manifest is where the environment is decided, so the reference
    # this reports is the one that ran by construction.
    pinned = read_context_manifest(context_dir / MANIFEST_FILE).build_environment.reference
    seam = docker if docker is not None else lb.Docker(container.docker_program(env))
    backend = lb.LocalBackend(
        lb.BackendConfig(
            sdk_sources=tuple(Path(source) for source in sdk_sources),
            jobs=jobs,
            ccache_dir=_cache_root(env, ccache_dir),
        ),
        docker=seam,
    )
    outcome = backend.run(
        context_dir=context_dir,
        action=lb.ACTION_BUILD,
        work_root=work_root,
        mode=mode,
        on_line=on_line,
    )
    out_dir = outcome.out if outcome.out is not None else work_root / "inv" / "out"
    return LocalBuildResult(
        outcome=outcome,
        out_dir=out_dir,
        context_dir=context_dir,
        image=pinned,
    )
