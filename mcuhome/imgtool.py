# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Detached signing: ``imgtool`` over a finished image (ADR 0015 §8).

MCUboot signing is a post-build step over the linked binary — the image
does not know it is going to be signed, and nothing about it changes
except a header that was already reserved and a trailer that is appended.
That is what makes ADR 0015 decision 8 possible at all: *the key lives
where the user's controlling instance runs, never on a build server*, so
a remote builder returns an unsigned image and the signature happens
somewhere else entirely.

**Two paths, one command.** Normally sysbuild signs inline: Zephyr's
``cmake/mcuboot.cmake`` runs ``imgtool sign`` as a post-build command with
arguments it derives from Kconfig and devicetree. ``mcuhome build
--no-sign`` leaves that step out and the manifest states those same
arguments (:mod:`mcuhome.manifest`); ``mcuhome sign`` reads them back and
runs the same tool with the same arguments over the same bytes. The
argument order below is Zephyr's, verbatim, so the two commands are
comparable line by line.

**What "identical" means, exactly.** Everything MCUboot verifies is
byte-identical between the two paths: the header, the payload, the
protected TLVs and the SHA-256 of all of it. The ECDSA signature itself
is not, and cannot be — ECDSA draws a random nonce per signature, so
signing the same bytes twice with the same key gives two different
(equally valid) signatures, of occasionally different DER length. The
test suite asserts exactly that: same image, same digest, different
signature, both verifying.

**Where imgtool comes from.** In order: :data:`IMGTOOL_VAR`, then the
west workspace's own MCUboot checkout — the same script the inline build
used, which is what keeps the two paths on one tool version — then an
``imgtool`` on ``PATH`` for a machine that has the package and no
workspace, which is what a dashboard host looks like (ADR 0003: the
dashboard never compiles). Signing does not run in the builder container:
handing a private key to a container to save a pip install is the wrong
trade, and this step needs no toolchain.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mcuhome import manifest as manifest_module
from mcuhome import signing, workspace
from mcuhome.errors import BuildError
from mcuhome.manifest import MANIFEST_FILE, SigningParameters

__all__ = [
    "IMGTOOL_VAR",
    "MCUBOOT_IMGTOOL",
    "Runner",
    "SignPlan",
    "find_imgtool",
    "plan_signing",
    "require_imgtool",
    "run_signing",
    "sign_command",
]

#: Overrides how imgtool is found: a path to ``imgtool.py``, or the name
#: of a program. The escape hatch for a machine where neither of the two
#: automatic answers is the right one.
IMGTOOL_VAR = "MCUHOME_IMGTOOL"

#: Where west puts the MCUboot module, and imgtool inside it. The same
#: file Zephyr's ``FindImgtool`` picks for the inline build.
MCUBOOT_IMGTOOL = Path("bootloader") / "mcuboot" / "scripts" / "imgtool.py"

#: Runs one imgtool invocation and answers with its exit status and
#: whatever it printed. Injectable so the test suite can watch the
#: commands without starting a process — the same shape
#: :mod:`mcuhome.container` uses for docker.
Runner = Callable[[list[str]], tuple[int, str]]


def find_imgtool(
    *, topdir: Path | None = None, env: dict[str, str] | None = None
) -> list[str] | None:
    """The argv prefix that runs imgtool, or None if there is none.

    A list rather than a path because the two answers have different
    shapes: a script needs an interpreter in front of it, a program does
    not.
    """
    environment = os.environ if env is None else env
    override = environment.get(IMGTOOL_VAR)
    if override:
        candidate = Path(override).expanduser()
        if candidate.suffix == ".py" or candidate.is_file():
            return [sys.executable, str(candidate)]
        return [override]
    if topdir is not None:
        script = topdir / MCUBOOT_IMGTOOL
        if script.is_file():
            return [sys.executable, str(script)]
    found = shutil.which("imgtool", path=environment.get("PATH"))
    if found:
        return [found]
    return None


def require_imgtool(*, topdir: Path | None = None, env: dict[str, str] | None = None) -> list[str]:
    """:func:`find_imgtool`, or a refusal that says where to get one."""
    program = find_imgtool(topdir=topdir, env=env)
    if program is not None:
        return program
    raise BuildError(
        "MCUHome cannot sign this image: imgtool is not available here.",
        hint=(
            "imgtool is MCUboot's signing tool. Install it\n"
            "    pip install imgtool\n"
            "or run this where the west workspace is, which already has it in "
            f"{MCUBOOT_IMGTOOL}.\n"
            f"{IMGTOOL_VAR} points at a specific one."
        ),
    )


def sign_command(
    program: list[str],
    *,
    parameters: SigningParameters,
    key: Path,
    source: Path,
    output: Path,
) -> list[str]:
    """``imgtool sign`` for one image, in Zephyr's own argument order.

    The order is not cosmetic. It is the one thing that makes "the same
    command the build would have run" checkable by reading two lines next
    to each other — Zephyr's ``cmake/mcuboot.cmake`` assembles
    ``--version --header-size --slot-size``, then prepends ``--align`` to
    the argument list that already carries ``--key``, then the two file
    names.
    """
    return [
        *program,
        "sign",
        "--version",
        parameters.version,
        "--header-size",
        str(parameters.header_size),
        "--slot-size",
        str(parameters.slot_size),
        "--align",
        str(parameters.align),
        "--key",
        str(key),
        str(source),
        str(output),
    ]


@dataclass(frozen=True)
class SignPlan:
    """Every command detached signing will run, decided before any of them.

    Same shape of promise as :class:`~mcuhome.workspace.BuildPlan`: every
    refusal a user can hit is raised while this is assembled, so the
    failure mode of the step itself is "imgtool said no", never "MCUHome
    could not find something".
    """

    #: Directory the manifest lives in; every path below is under it.
    out_dir: Path
    manifest_path: Path
    key: Path
    parameters: SigningParameters
    #: One entry per artifact format, in a stable order: format, command,
    #: and the file it produces.
    commands: tuple[tuple[str, tuple[str, ...], Path], ...]

    @property
    def outputs(self) -> list[Path]:
        return [path for _, _, path in self.commands]


def _resolve_manifest(target: Path) -> Path:
    """Accept a build directory or the manifest inside one."""
    if target.is_dir():
        return target / MANIFEST_FILE
    return target


def _require(data: dict, key: str, *, path: Path) -> object:
    if key not in data:
        raise BuildError(
            f"The build manifest {path} has no {key!r} section.",
            hint=(
                "it was written by a builder that did not know about detached "
                "signing yet, or for a board without an update scheme. Build again."
            ),
        )
    return data[key]


def plan_signing(
    target: Path,
    *,
    key: Path,
    env: dict[str, str] | None = None,
    topdir: Path | None = None,
) -> SignPlan:
    """Read a build manifest and decide how to sign what it describes.

    *target* is a build directory or the manifest file inside one — a
    user who has just run a build has the directory in their shell
    history and the file in the output, and both should work.
    """
    manifest_path = _resolve_manifest(target)
    out_dir = manifest_path.parent
    data = manifest_module.read_manifest(manifest_path)
    block = _require(data, "signing", path=manifest_path)
    if not isinstance(block, dict):
        raise BuildError(
            f"The build manifest {manifest_path} describes a build that cannot be signed.",
            hint=(
                "only images built for a board with an MCUboot update scheme "
                "carry signing parameters (ADR 0015)."
            ),
        )
    parameters = SigningParameters.from_dict(block.get("arguments", {}))
    inputs = block.get("inputs") or {}
    outputs = block.get("outputs") or {}

    program = require_imgtool(topdir=topdir, env=env)
    commands: list[tuple[str, tuple[str, ...], Path]] = []
    for form in sorted(inputs):
        source = out_dir / str(inputs[form])
        if not source.is_file():
            raise BuildError(
                f"The image {source} named by the build manifest is not there.",
                hint=(
                    "signing works on the artifacts of a finished build; a build "
                    "directory that was cleaned has to be built again."
                ),
            )
        if form not in outputs:
            continue  # pragma: no cover - the builder writes both or neither
        destination = out_dir / str(outputs[form])
        commands.append(
            (
                form,
                tuple(
                    sign_command(
                        program, parameters=parameters, key=key, source=source, output=destination
                    )
                ),
                destination,
            )
        )
    if not commands:
        raise BuildError(
            f"The build manifest {manifest_path} names no image to sign.",
            hint="build again; a build that produced no image is a build that failed.",
        )
    return SignPlan(
        out_dir=out_dir,
        manifest_path=manifest_path,
        key=key,
        parameters=parameters,
        commands=tuple(commands),
    )


def run_signing(plan: SignPlan, *, runner: Runner | None = None) -> list[Path]:
    """Run every command of *plan*, or raise with imgtool's own words.

    *runner* exists for the test suite; the default really does start
    imgtool. Whatever imgtool printed goes into the refusal, because for
    a wrong key or a too-small slot its message is the actionable part
    and MCUHome has nothing to add to it.
    """
    execute = _run if runner is None else runner
    written: list[Path] = []
    for _, command, destination in plan.commands:
        destination.parent.mkdir(parents=True, exist_ok=True)
        code, output = execute(list(command))
        if code != 0:
            raise BuildError(
                f"imgtool could not sign {destination.name} (exit {code}).",
                hint=(
                    f"{output.strip() or 'imgtool printed nothing'}\n"
                    f"The command was: {' '.join(command)}"
                ),
            )
        written.append(destination)
    return written


def _run(command: list[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    except OSError as error:
        raise BuildError(
            f"MCUHome could not start imgtool: {error.strerror}.",
            hint=f"the command was: {' '.join(command)}",
        ) from error
    return completed.returncode, completed.stdout


def sign_build(
    target: Path,
    *,
    key: Path | str | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    runner: Runner | None = None,
) -> SignPlan:
    """Sign the application image a build directory holds, and say where.

    The key is resolved exactly as a build resolves it (``--signing-key``,
    then :data:`~mcuhome.signing.KEY_VAR`, then the per-user default), but
    it is **not** generated on first need here: a build directory that was
    produced elsewhere has to be signed with the key its device's
    bootloader carries, and inventing one at this point would produce
    firmware that nothing accepts.
    """
    resolved = signing.signing_key(key, env=env, create=False)
    plan = plan_signing(
        target,
        key=resolved.path,
        env=env,
        topdir=workspace.find_topdir(workspace.MODULE_DIR, cwd or Path.cwd()),
    )
    run_signing(plan, runner=runner)
    return plan
