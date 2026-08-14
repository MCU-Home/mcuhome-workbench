# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The compiler's half of a ``local`` build: the image, and a locked context.

:mod:`mcuhome.compiler.localbackend` is the backend role of the
build-container contract — given a *locked context directory* it drives one
invocation through the ABI. This module is the thin surface above it that
the ``local`` build method composes with:
:func:`resolve_checked_image` answers "which container, and does it carry
the line the device needs" out of what this host has, and
:func:`run_locked_build` drives one ``build`` invocation over a context
somebody else created and locked.

The composition itself — model in, context created and locked, build
driven — lives in the workbench
(:func:`mcuhome.workbench.buildmethods.compose_local_build`), because
creating a context is client machinery: pin resolution and the context
layout are the workbench's, deliberately the same calls the ``remote``
method makes (E65). This module imports nothing from the workbench —
it ships inside the SDK package and runs where the workbench neither
exists nor belongs (ADR 0024).

**The private key never reaches here.** Nothing in this module's surface
takes a key at all: a locked context carries the **public** half as
``keys/signing.pub`` (ADR 0015 decision 8) and that is all a build ever
sees of the key pair. The backend delivers an *unsigned* image plus the
§7.2.1 build report; the signature happens on the host afterwards, where
the private key already is (:mod:`mcuhome.workbench.imgtool`).

**Local, never networked.** The image is resolved against this host's
images and pulled by nobody; the SDK package is acquired from the
operator's own source directories (contract v1's first tier, §9.1). A
missing image and a missing package are typed refusals, not tracebacks
ten minutes into a build.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from mcuhome.compiler import container
from mcuhome.compiler import localbackend as lb
from mcuhome.model.errors import BuildError
from mcuhome.model.toolchain import satisfies_line

__all__ = [
    "LocalBuildResult",
    "ResolvedImage",
    "image_zephyr_version",
    "resolve_checked_image",
    "run_locked_build",
]


@dataclass(frozen=True)
class LocalBuildResult:
    """What one :func:`run_locked_build` produced, from the caller's side.

    :attr:`outcome` is the backend's own seven-part §5.3 answer — a caller
    checks :attr:`~mcuhome.compiler.localbackend.LocalOutcome.successful`
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


@dataclass(frozen=True)
class ResolvedImage:
    """One image this host answered with: the reference, and its profile.

    :attr:`profile` is what ``docker image inspect`` stated — labels and
    repo digest. :attr:`digest` is ``None`` for an image that was never
    pushed, which says "these bytes are not fetchable anywhere" rather
    than inventing a digest that looks as if they were.
    """

    reference: str
    profile: lb.ImageProfile

    @property
    def digest(self) -> str | None:
        return self.profile.digest


def _image_not_found(reference: str) -> BuildError:
    return BuildError(
        f"No build container on this host answers to {reference}.",
        hint=(
            "the local build compiles in the MCUHome builder image and pulls "
            "nothing — pull or build the image (docker pull "
            f"{container.IMAGE}), or point --image at one. --method local-dev "
            "needs no image."
        ),
    )


def image_zephyr_version(profile: lb.ImageProfile) -> str:
    """Which Zephyr release *profile* carries, by its own label (§2.1).

    The image's own claim and nothing else: ``--image`` may point at any
    image at all, and the whole point of the coupling label is that a
    container states what it builds against. An image that carries no
    ``org.mcuhome.zephyr`` label states nothing, and the empty string is
    exactly that — :func:`~mcuhome.model.toolchain.satisfies_line` reads
    it as satisfying no line, which is §2.1.1's rule verbatim: "a
    container that does not carry a named label does not qualify —
    absence is never read as compatible".

    There is deliberately **no** fallback to this module's own pin
    (:data:`~mcuhome.compiler.container.ZEPHYR_RELEASE`). One existed and
    was wrong twice over:
    :meth:`~mcuhome.compiler.localbackend.LocalBackend._resolve_image`
    performs this same match a few steps later with no fallback, so an
    unlabelled image was admitted here and refused there — after the
    context directory, the SDK lookup and the lock had been paid for —
    and a required line the invented value did not satisfy was refused
    with "carries Zephyr 4.4.0", a claim the image never made.
    """
    return profile.labels.get(lb.ZEPHYR_LABEL) or ""


def _line_unsatisfied(reference: str, offered: str, line: str) -> BuildError:
    """The image on this host does not carry the line the model requires.

    An absent label and a wrong one are one refusal with two sentences,
    worded as
    :meth:`~mcuhome.compiler.localbackend.LocalBackend._resolve_image`
    words its twin: both mean "this image does not serve that line", and
    naming the label rather than an empty value is what tells the
    operator of an unlabelled image what to fix.
    """
    says = f"carries Zephyr {offered}" if offered else f"carries no {lb.ZEPHYR_LABEL} label"
    return BuildError(
        f"The build container {reference} {says}, and this device needs the {line} line.",
        hint=(
            "the context states the Zephyr line its model was resolved against "
            "(zephyr_version, ADR 0013) and the build method picks a container of "
            "that line — this host offers none. Pull or build a "
            f"{line}.x builder image, point --image at one, or set zephyr_version "
            "to a line this host can serve."
        ),
    )


def resolve_checked_image(
    image: str | None,
    *,
    line: str,
    env: dict[str, str],
    docker: lb.Docker | None = None,
) -> ResolvedImage:
    """Resolve the image reference on this host and check its Zephyr line.

    Refuses — before anything is created anywhere — when no image on this
    host answers to the reference, and when the image it finds does not
    carry the line the device needs (E61's requirement, answered by the
    half that has the images). A refusal here costs no context directory
    and no SDK lookup, which is the composition's promise.
    """
    reference = image or container.image_reference(env)
    seam = docker if docker is not None else lb.Docker(container.docker_program(env))
    profile = seam.inspect(reference)
    if profile is None:
        raise _image_not_found(reference)
    offered = image_zephyr_version(profile)
    if not satisfies_line(offered, line=line):
        raise _line_unsatisfied(reference, offered, line)
    return ResolvedImage(reference=reference, profile=profile)


def run_locked_build(
    context_dir: Path,
    *,
    image: str,
    sdk_sources: Sequence[Path],
    work_root: Path,
    env: dict[str, str],
    jobs: int = 1,
    mode: str = "clean",
    on_line: lb.LineSink | None = None,
    docker: lb.Docker | None = None,
) -> LocalBuildResult:
    """Drive one ``build`` invocation over a locked context directory.

    The backend role and nothing else: *context_dir* was created and
    locked by the workbench (its manifest already records the image the
    caller resolved via :func:`resolve_checked_image`), *sdk_sources* are
    the operator's local package directories the backend acquires the
    pinned SDK from (§9.1 — a backend duty, the hash decides), and
    *work_root* is the backend's own scratch area.

    *docker* is the one seam — left ``None`` it drives real docker; a
    caller (or a test) injects a scripted
    :class:`~mcuhome.compiler.localbackend.Docker` to drive the whole
    thing without a container runtime.
    """
    context_dir = Path(context_dir)
    work_root = Path(work_root)
    seam = docker if docker is not None else lb.Docker(container.docker_program(env))
    backend = lb.LocalBackend(
        lb.BackendConfig(
            sdk_sources=tuple(Path(source) for source in sdk_sources), jobs=jobs, image=image
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
    return LocalBuildResult(outcome=outcome, out_dir=out_dir, context_dir=context_dir, image=image)
