# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Driving a ``local-dev`` build on the host toolchain, end to end (E18).

The third of the three build methods, and the oldest: a contributor's own
west workspace, the tools already on their ``PATH``, ``west build``
straight into a build directory. :mod:`mcuhome.compiler.workspace` is the
machinery — the workspace lookup, the environment, the command, the
artifact and memory readers — and this module is the thin composition
above it, the counterpart of :mod:`mcuhome.compiler.localbuild` for the
``local`` method.

**Why it exists at all.** ADR 0020 decision 6 puts the three methods
behind one interface, and until now two of them had a composition
function and the third was a hundred lines inside the command line. A
dispatcher cannot call a command line, and the dashboard could not reach
this method at all. Nothing here is new behaviour: it is the build half
of the CLI's host-build path, moved to where the other two live.

**Unsigned, always (E56).** The plan is made with ``detached_signing``
on, so ``west`` links an image nobody signed and the private key is not a
parameter of this function, of the plan, or of the command. What the
build *does* receive is *bootloader_key* — the **public** half, which is
what MCUboot verifies against and is useless for signing. The signature
is the one shared host-side step afterwards, identical for all three
methods.

**What comes out.** A ``build-manifest.json`` describing the images, the
same report shape ``mcuhome sign`` has always read on this path — not
the build container's §7.2.1 ``build-report.json``, because no container
ran. Both shapes carry the imgtool parameters the host signer needs
(E55), which is why one signing step can serve either.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from mcuhome.compiler import report as report_module
from mcuhome.compiler import workspace
from mcuhome.compiler.generate import APP_DIR
from mcuhome.model.model import DeviceModel

__all__ = [
    "DevBuildResult",
    "drop_unsigned_lookalikes",
    "run_dev_build",
]


@dataclass(frozen=True)
class DevBuildResult:
    """What one :func:`run_dev_build` produced, from the caller's side.

    :attr:`plan` and :attr:`log` are what a renderer needs and no other
    method has: the west command that ran and its output, from which
    :func:`mcuhome.compiler.workspace.parse_image_memory_report` reads the
    footprint the container path reads out of a report instead.
    :attr:`images` and :attr:`merged` are the artifacts on disk, and
    :attr:`manifest_path` is the report the host signer consumes.
    """

    plan: workspace.BuildPlan
    log: str
    images: list[workspace.ImageArtifacts]
    merged: Path | None
    manifest_path: Path


def drop_unsigned_lookalikes(build_dir: Path, *, app_image: str) -> None:
    """Remove files a detached build must not leave behind.

    Two kinds, both named as though they were bootable and neither of
    them signed: a ``zephyr.signed.*`` left over from an earlier inline
    build of the same directory, and sysbuild's combined hex, which falls
    back to the *unsigned* application when there is no signed one to
    merge. Deleting them is not tidiness — it is the difference between
    "no flashable file yet" and "a flashable file that bricks the boot",
    and the host signing step is one call away from producing the real
    one.
    """
    output = build_dir / app_image / "zephyr"
    for name in (
        "zephyr.signed.bin",
        "zephyr.signed.hex",
        "zephyr.signed.confirmed.bin",
        "zephyr.signed.confirmed.hex",
    ):
        (output / name).unlink(missing_ok=True)
    for merged in build_dir.glob(workspace.MERGED_IMAGE_GLOB):
        merged.unlink(missing_ok=True)


def run_dev_build(
    model: DeviceModel,
    *,
    out_dir: Path,
    bootloader_key: Path,
    module_dir: Path,
    cwd: Path,
    env: dict[str, str],
    jobs: int = 1,
    snippets: tuple[str, ...] = (),
    bootloader_snippets: tuple[str, ...] = (),
    on_plan: Callable[[workspace.BuildPlan], None] | None = None,
    stream: TextIO | None = None,
) -> DevBuildResult:
    """Compile *model* on this machine's toolchain, unsigned, and describe it.

    *out_dir* already holds the generated Zephyr application (stage 4
    wrote it there under :data:`~mcuhome.compiler.generate.APP_DIR`); this
    is stage 5 over it. *bootloader_key* is the **public** key file
    compiled into MCUboot — the only key any build method is handed.

    *module_dir* and *cwd* are where the west workspace is looked for, and
    are required rather than discovered for the reason
    :func:`mcuhome.compiler.workspace.plan_build` states: only the caller
    knows where it is installed and where it was run.

    *on_plan* is called once with the resolved plan, after every refusal a
    user can hit before the compiler starts has been raised and before a
    single process runs — the moment a command line announces what it is
    about to do. *stream* is where the compiler's own output goes;
    ``None`` lets it inherit this process's.
    """
    out_dir = Path(out_dir)
    plan = workspace.plan_build(
        out_dir=out_dir,
        app_subdir=APP_DIR,
        board=model.device.board,
        snippets=snippets,
        bootloader_snippets=bootloader_snippets,
        signing_key=bootloader_key,
        # E56: no build signs itself, on any method.
        detached_signing=True,
        jobs=jobs,
        env=env,
        module_dir=module_dir,
        cwd=cwd,
    )
    if on_plan is not None:
        on_plan(plan)

    code, log = workspace.run_build(plan, stream=stream)
    if code != 0:
        raise workspace.refuse_failed_build(code, build_dir=plan.build_dir)

    drop_unsigned_lookalikes(plan.build_dir, app_image=plan.app_dir.name)
    images = workspace.build_images(plan.build_dir, app_image=plan.app_dir.name)
    merged = workspace.merged_image(plan.build_dir)
    manifest = report_module.build_manifest(
        model,
        out_dir=out_dir,
        build_dir=plan.build_dir,
        app_image=plan.app_dir.name,
        images=images,
        snippets=snippets,
        bootloader_snippets=bootloader_snippets,
        jobs=jobs,
        signed_by_the_build=False,
        merged=merged,
        ota_image=None,
    )
    manifest_path = report_module.write_manifest(manifest, out_dir=out_dir)
    return DevBuildResult(
        plan=plan, log=log, images=images, merged=merged, manifest_path=manifest_path
    )
