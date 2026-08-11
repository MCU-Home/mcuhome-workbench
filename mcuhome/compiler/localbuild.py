# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Driving a ``local`` build from a device model, end to end (E54).

:mod:`mcuhome.compiler.localbackend` is the backend role of the
build-container contract — given a *locked context directory* it drives one
invocation through the ABI. This module is the thin composition above it
that a command line (or any in-process caller) needs: it turns a resolved
:class:`~mcuhome.model.model.DeviceModel` into a context directory and then
runs the backend over it.

**Why this is the default build path now (E54).** Until now the host
assembled a ``west build`` command and ran it inside a one-shot
``docker run`` that bind-mounted the developer's own west workspace
(:mod:`mcuhome.compiler.container`). That path signs *inline*, which means
the private MCUboot key is mounted into the build container — the §9.2
violation the build-container contract exists to remove. Here the
container only ever receives the **public** key (``keys/signing.pub`` in
the context, ADR 0015 decision 8); it generates, compiles and delivers an
*unsigned* image plus the §7.2.1 build report, and the signature happens
on the host afterwards, where the private key already is. This module is
the half that gets an unsigned image out of the container; the host signer
(:mod:`mcuhome.workbench.imgtool`) is the other half.

**The private key never reaches here.** Nothing in this module's surface
takes a private key: :func:`run_local_build` is handed the *public* key
PEM to place in the context and nothing else. That is the structural half
of the security invariant — a key that is never passed cannot be mounted —
and the backend's own docker-argv composition is the enforced half.

**Local, never networked.** The SDK is resolved against the operator's
own source directories (``--sdk-source``, contract v1's first tier), and
the image is resolved against this host's images and pulled by nobody: a
missing SDK package and a missing image are two typed refusals raised
before a container starts, not tracebacks ten minutes into a build.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mcuhome.compiler import container
from mcuhome.compiler import localbackend as lb
from mcuhome.model.context import ContainerResolution, SdkPin
from mcuhome.model.errors import BuildError
from mcuhome.model.model import DeviceModel
from mcuhome.model.toolchain import satisfies_line
from mcuhome.workbench.contextdir import create_context, lock_context
from mcuhome.workbench.resolve_pins import resolve_from_index

__all__ = [
    "SDK_ANY",
    "LocalBuildResult",
    "image_zephyr_version",
    "resolve_sdk_pin",
    "run_local_build",
]

#: The SDK constraint a local developer build resolves with: "the newest
#: the configured sources offer". A device configuration can pin the SDK
#: as a PEP 440 constraint (ADR 0018), but a plain ``mcuhome build`` on a
#: developer's own machine has no such intent — it takes whatever SDK
#: package the ``--sdk-source`` directories hold, exactly as the empty
#: :class:`~packaging.specifiers.SpecifierSet` matches every version.
SDK_ANY = ""


@dataclass(frozen=True)
class LocalBuildResult:
    """What one :func:`run_local_build` produced, from the caller's side.

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


def resolve_sdk_pin(sources: Sequence[Path], *, constraint: str = SDK_ANY) -> tuple[str, str, str]:
    """Resolve the SDK pin ``(constraint, version, sha256)`` from *sources*.

    The pin has to exist *before* the context can be created — the context
    is content-addressed over the resolved ``sha256`` — so this resolves it
    from the static ``index.json`` a source directory carries
    (``scripts/build_sdk_archive.py``), never from a network. Sources are
    searched in order and the first that holds a matching package wins,
    which is the same "first source wins" rule
    :func:`~mcuhome.compiler.localbackend.acquire_sdk` then fetches the
    bytes by.

    Raises a typed :class:`~mcuhome.model.errors.BuildError` — the
    ``sdk.unavailable`` spirit — when no source is configured or none holds
    the package, so the command line can render it as a clean refusal.
    """
    if not sources:
        raise BuildError(
            "The local build method needs an SDK source, and none is configured.",
            hint=(
                "the build container fetches the MCUHome SDK as a hash-pinned "
                "package from operator-configured directories only (ADR 0018) — "
                "point at one:\n"
                "    mcuhome build <device> --sdk-source <dir>\n"
                "or set MCUHOME_SDK_SOURCE. --method local-dev needs no SDK source."
            ),
        )
    searched: list[str] = []
    for source in sources:
        source = Path(source)
        searched.append(str(source))
        index_path = source / lb.INDEX_FILE
        if not index_path.is_file():
            continue
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        try:
            # SDK_ANY means "the newest package this source holds, whatever
            # it is" — and during development that is a dev release. The
            # E52 pre-release rule (a dev version satisfies only a
            # pre-release constraint) is right for a real pin like ~=2.3
            # but wrong for "any", which is literally any: so an empty
            # constraint admits pre-releases, and a stated one keeps the
            # rule.
            allow = True if constraint == SDK_ANY else None
            resolved = resolve_from_index(index, lb.SDK_PACKAGE_NAME, constraint, prereleases=allow)
        except BuildError:
            continue
        return constraint, resolved.version, resolved.sha256
    listed = ", ".join(searched) or "none"
    raise BuildError(
        f"No configured SDK source holds the {lb.SDK_PACKAGE_NAME} package.",
        hint=(
            f"the local build reads the SDK from source directories only, never "
            f"from a URL — put a {lb.SDK_PACKAGE_NAME} package and its index.json "
            f"(scripts/build_sdk_archive.py) in one of: {listed}"
        ),
    )


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
    (:data:`~mcuhome.compiler.container.ZEPHYR_LINE`). One existed and
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


def run_local_build(
    model: DeviceModel,
    *,
    signing_pub: str,
    sdk_sources: Sequence[Path],
    work_root: Path,
    env: dict[str, str],
    image: str | None = None,
    jobs: int = 1,
    mode: str = "clean",
    created: datetime | None = None,
    on_line: lb.LineSink | None = None,
    docker: lb.Docker | None = None,
) -> LocalBuildResult:
    """Build *model* in a build container and deliver an **unsigned** image.

    The whole composition, in order: resolve the image reference and its
    digest against this host (a missing image refuses here, before
    anything is created); check that the image carries the Zephyr line
    the model requires (a mismatch refuses here too, naming both);
    resolve the SDK pin against *sdk_sources* (a missing package refuses
    here as well); write and lock a context directory from the model and
    the **public** signing key, recording the resolved image in its
    manifest; and drive one ``build`` invocation through
    :class:`~mcuhome.compiler.localbackend.LocalBackend`.

    **This function plays both roles E61 separates**, which is what makes
    a local build a local build: the workbench half states the
    requirement (``zephyr`` in ``context.yaml``, from the model), and the
    backend half answers it out of what this host has. The answer is
    recorded in ``manifest.yaml`` exactly as a build server records its
    own, so a context built here and a context built there are read back
    the same way.

    *signing_pub* is the PEM text of the user's MCUboot **public** key — it
    becomes ``keys/signing.pub`` in the context, which is all a build ever
    sees of the key pair (ADR 0015 decision 8). No private key is a
    parameter here, on purpose.

    *work_root* is a scratch directory this owns: the context lives at
    ``work_root/context`` and the backend's session area under
    ``work_root/backend``. The returned :attr:`LocalBuildResult.out_dir`
    holds the delivered files; the caller is responsible for copying the
    ones it wants somewhere durable and for signing on the host.

    *docker* is the one seam — left ``None`` it drives real docker; a
    caller (or a test) injects a scripted
    :class:`~mcuhome.compiler.localbackend.Docker` to drive the whole thing
    without a container runtime.
    """
    sources = tuple(Path(source) for source in sdk_sources)
    reference = image or container.image_reference(env)
    seam = docker if docker is not None else lb.Docker(container.docker_program(env))

    profile = seam.inspect(reference)
    if profile is None:
        raise _image_not_found(reference)
    # Before anything is written: the container this host offers has to
    # carry the line the model asks for. Refused here rather than at the
    # lock, so a mismatch costs no context directory and no SDK lookup.
    required = model.toolchain.zephyr_line
    offered = image_zephyr_version(profile)
    if not satisfies_line(offered, line=required):
        raise _line_unsatisfied(reference, offered, required)

    constraint, version, sha256 = resolve_sdk_pin(sources)

    work_root = Path(work_root)
    context_dir = work_root / "context"
    if context_dir.exists():
        shutil.rmtree(context_dir)
    work_root.mkdir(parents=True, exist_ok=True)
    create_context(
        model,
        out_dir=context_dir,
        sdk=SdkPin(constraint=constraint, version=version, url="", sha256=sha256),
        signing_pub=signing_pub,
        created=created or datetime.now(UTC),
    )
    lock_context(
        context_dir,
        # The backend half's answer, recorded verbatim — including a
        # `None` digest for an image that was never pushed, which says
        # "these bytes are not fetchable anywhere" rather than inventing
        # a digest that looks as if they were.
        container=ContainerResolution.from_reference(reference, digest=profile.digest),
    )

    backend = lb.LocalBackend(
        lb.BackendConfig(sdk_sources=sources, jobs=jobs, image=reference), docker=seam
    )
    outcome = backend.run(
        context_dir=context_dir,
        action=lb.ACTION_BUILD,
        work_root=work_root / "backend",
        mode=mode,
        on_line=on_line,
    )
    out_dir = outcome.out if outcome.out is not None else work_root / "backend" / "inv" / "out"
    return LocalBuildResult(
        outcome=outcome, out_dir=out_dir, context_dir=context_dir, image=reference
    )
