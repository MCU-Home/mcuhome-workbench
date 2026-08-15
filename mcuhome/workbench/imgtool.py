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

**One signing path, whatever built the image (E56).** Zephyr's
``cmake/mcuboot.cmake`` *can* sign inline, deriving the arguments from
Kconfig and devicetree — but no MCUHome build method uses that any more:
every build (container, ``local-dev``, ``--no-sign``) produces an
**unsigned** image and states those same arguments in its report
(``build-manifest.json`` for a west build, the §7.2.1 ``build-report.json``
for a container build). This module is the one place they are turned back
into a command — run right after the build by ``mcuhome device build``, or later
by ``mcuhome device sign-firmware`` on another machine — so the private key lives in no
build at all (ADR 0015 decision 8). The argument order below is Zephyr's,
verbatim, so a signature made here is comparable line by line with the
inline one Zephyr would have produced.

**What "identical" means, exactly.** Everything MCUboot verifies is
byte-identical between the two paths: the header, the payload, the
protected TLVs and the SHA-256 of all of it. The ECDSA signature itself
is not, and cannot be — ECDSA draws a random nonce per signature, so
signing the same bytes twice with the same key gives two different
(equally valid) signatures, of occasionally different DER length. The
test suite asserts exactly that: same image, same digest, different
signature, both verifying.

**Where imgtool comes from.** It is a **declared dependency** of
``mcuhome-workbench`` — the package MCUboot publishes itself, pinned in
``pyproject.toml`` to the release line of the MCUboot revision the SDK's
west manifest carries. The lookup is: :data:`IMGTOOL_VAR` as the escape
hatch, then the installed package's console script (next to the running
interpreter first, then ``PATH``). Nothing here runs the west
workspace's checkout script any more: that script's requirements
(click, cryptography, …) belong to the Zephyr build environment, not to
whatever venv the workbench happens to run in — an inherited-environment
accident, not a contract (PO 2026-08-15). Signing does not run in the
build container either: handing a private key to a container to save a
dependency is the wrong trade, and this step needs no toolchain.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mcuhome.model import manifest as manifest_module
from mcuhome.model.errors import BuildError
from mcuhome.model.manifest import MANIFEST_FILE, SigningParameters
from mcuhome.model.registry import SIGNATURE_TYPE
from mcuhome.model.userpaths import expand

from mcuhome.workbench import signing
from mcuhome.workbench.project import Project

__all__ = [
    "BUILD_REPORT_FILE",
    "IMGTOOL_VAR",
    "REPORT_FIRMWARE",
    "REPORT_VERSION",
    "Runner",
    "SignPlan",
    "find_imgtool",
    "plan_report_signing",
    "plan_signing",
    "read_build_report",
    "require_imgtool",
    "run_signing",
    "sign_command",
    "sign_report",
]

#: The §7.2.1 build report a build container delivers next to its unsigned
#: firmware (the ``report`` artifact, ADR 0018 / build-container-contract
#: §7.2). The leaner sibling of ``build-manifest.json``: it exists for one
#: consumer — the client that signs detached — and carries only what that
#: client needs. The name mirrors ``mcuhome.compiler.abi.REPORT_ARTIFACT``,
#: restated here because this package must not import the compiler (it is
#: what the compiler imports).
BUILD_REPORT_FILE = "build-report.json"

#: The report format version this signer implements. "A consumer that does
#: not implement the version it finds MUST NOT sign from the document"
#: (§7.2.1), so a mismatch is a refusal that names both numbers.
REPORT_VERSION = 1

#: ``<unsigned firmware in out> -> <signed name beside it>`` for the two
#: encodings a build container delivers with role ``firmware`` (§7.2:
#: ``firmware.hex`` to flash, ``firmware.bin`` to sign). The §7.2.1
#: signing parameters "apply to **every** artifact declared with role
#: ``firmware``", so both are signed with the one set of arguments.
REPORT_FIRMWARE = (("firmware.bin", "firmware.signed.bin"), ("firmware.hex", "firmware.signed.hex"))

#: Overrides how imgtool is found: a path to an ``imgtool.py`` script,
#: or the name of a program. The escape hatch for a machine where the
#: installed package is not the right answer.
IMGTOOL_VAR = "MCUHOME_IMGTOOL"

#: Runs one imgtool invocation and answers with its exit status and
#: whatever it printed. Injectable so the test suite can watch the
#: commands without starting a process — the same shape
#: :mod:`mcuhome.compiler.container` uses for docker.
Runner = Callable[[list[str]], tuple[int, str]]


def find_imgtool(*, env: dict[str, str]) -> list[str] | None:
    """The argv prefix that runs imgtool, or None if there is none.

    A list rather than a path because an override may name a script that
    needs an interpreter in front of it.

    *env* is stated, never read from the process: which imgtool runs is
    part of what a build is, and one process may serve several callers
    with different answers.
    """
    override = env.get(IMGTOOL_VAR)
    if override:
        candidate = expand(override, env)
        if candidate.suffix == ".py" or candidate.is_file():
            return [sys.executable, str(candidate)]
        return [override]
    # The declared dependency: pip puts the console script next to the
    # interpreter it installed for — the venv this process runs in.
    beside = Path(sys.executable).parent / "imgtool"
    if beside.is_file():
        return [str(beside)]
    found = shutil.which("imgtool", path=env.get("PATH"))
    if found:
        return [found]
    return None


def require_imgtool(*, env: dict[str, str]) -> list[str]:
    """:func:`find_imgtool`, or a refusal that says where to get one."""
    program = find_imgtool(env=env)
    if program is not None:
        return program
    raise BuildError(
        "MCUHome cannot sign this image: imgtool is not available here.",
        hint=(
            "imgtool is MCUboot's signing tool and a declared dependency of "
            "mcuhome-workbench, so this installation is incomplete. Reinstall\n"
            "    pip install --force-reinstall mcuhome-workbench\n"
            f"or point {IMGTOOL_VAR} at a specific imgtool."
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

    Same shape of promise as :class:`~mcuhome.compiler.workspace.BuildPlan`: every
    refusal a user can hit is raised while this is assembled, so the
    failure mode of the step itself is "imgtool said no", never "MCUHome
    could not find something".
    """

    #: Directory the manifest lives in; every path below is under it.
    out_dir: Path
    manifest_path: Path
    #: The signing key file — the same durable file the commands below
    #: carry: the project's referenced ``mcuboot.pem`` or the override's
    #: plain PEM (:attr:`~mcuhome.workbench.signing.SigningKey.path`).
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


def _contained_name(value: str, *, role: str, manifest_path: Path) -> str:
    """A signing input/output name that stays inside the build directory.

    :func:`plan_signing` joins these onto *out_dir* to decide what to read
    and where to write, and ``out_dir / value`` follows ``Path`` join rules:
    an absolute right-hand side wins outright, and a ``..`` segment climbs
    out of the directory. A build writes its artifacts *beside* its
    manifest, so a name that is absolute or reaches upward was not written
    by one — it is the shape a hand-crafted ``build-manifest.json`` handed
    to ``mcuhome device sign-firmware`` would take to read and sign a file anywhere on the
    machine, or to write one outside the build directory. Refused by name,
    before it is ever joined, rather than trusted because it came from a
    file named like a manifest.
    """
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise BuildError(
            f"The build manifest {manifest_path} names {value!r} as a signing {role}, "
            "which is not a path inside the build directory.",
            hint=(
                "a build manifest names artifacts a build wrote next to it; an absolute "
                "path or one with a '..' segment would read or write outside the build "
                "directory, so it is refused — no build writes one."
            ),
        )
    return value


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
    env: dict[str, str],
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
                "carry signing parameters."
            ),
        )
    parameters = SigningParameters.from_dict(block.get("arguments", {}))
    inputs = block.get("inputs") or {}
    outputs = block.get("outputs") or {}

    program = require_imgtool(env=env)
    commands: list[tuple[str, tuple[str, ...], Path]] = []
    for form in sorted(inputs):
        source_name = _contained_name(str(inputs[form]), role="input", manifest_path=manifest_path)
        source = out_dir / source_name
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
        output_name = _contained_name(
            str(outputs[form]), role="output", manifest_path=manifest_path
        )
        destination = out_dir / output_name
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


def _resolve_report(target: Path) -> Path:
    """Accept a build directory or the build report inside one."""
    if target.is_dir():
        return target / BUILD_REPORT_FILE
    return target


def read_build_report(path: Path) -> dict:
    """Load a §7.2.1 ``build-report.json``, or refuse in plain language.

    The report is what a build container delivers instead of a full
    ``build-manifest.json`` (ADR 0018): it carries the ``report`` format
    version and the mandatory ``signing`` block, and nothing a signer does
    not need. This checks exactly what has to hold before a signature can
    be planned from it — that it parses, that the version is one this
    signer implements, that a ``signing.arguments`` object is there to turn
    into an ``imgtool sign`` command, and that ``signing.signature_type`` is
    the one algorithm MCUHome signs with. That last field is mandatory in
    §7.2.1 for a reason a signer feels directly: it lets the client refuse a
    key whose algorithm the bootloader would not verify, here, instead of
    producing an image the device silently will not boot.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise BuildError(
            f"MCUHome cannot read the build report {path}: {error.strerror}.",
            hint=(
                "the local build method delivers one next to the unsigned image "
                f"it produced. Point at a build directory that contains "
                f"{BUILD_REPORT_FILE}, or build again."
            ),
        ) from error
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise BuildError(
            f"The build report {path} is not valid JSON ({error.msg}, line {error.lineno}).",
            hint="it is generator output — delete it and build again rather than editing it",
        ) from error
    if not isinstance(data, dict):
        raise BuildError(
            f"The build report {path} does not describe a build.",
            hint="it is generator output — delete it and build again rather than editing it",
        )
    found = data.get("report")
    if found != REPORT_VERSION:
        raise BuildError(
            f"The build report {path} is report format version {found!r}, and this "
            f"signer implements version {REPORT_VERSION}.",
            hint=(
                "the report format is a versioned contract (§7.2.1): a mismatch is a "
                "refusal that names both numbers. Sign with a matching mcuhome version."
            ),
        )
    signing_block = data.get("signing")
    if not isinstance(signing_block, dict) or not isinstance(signing_block.get("arguments"), dict):
        raise BuildError(
            f"The build report {path} carries no signing parameters.",
            hint=(
                "a build report states the four imgtool arguments its image is "
                "signed with (§7.2.1) — a report without them is truncated. Build again."
            ),
        )
    signature_type = signing_block.get("signature_type")
    if signature_type != SIGNATURE_TYPE:
        raise BuildError(
            f"The build report {path} signs with signature_type {signature_type!r}, and "
            f"MCUHome images are {SIGNATURE_TYPE}.",
            hint=(
                "signature_type is mandatory in a §7.2.1 build report so a client can refuse "
                "a key whose algorithm the bootloader would not verify, instead of producing "
                "an image the device cannot boot. Build again with a matching mcuhome."
            ),
        )
    return data


def plan_report_signing(
    target: Path,
    *,
    key: Path,
    env: dict[str, str],
) -> SignPlan:
    """Read a §7.2.1 build report and decide how to sign the firmware beside it.

    The build-report counterpart of :func:`plan_signing`. *target* is the
    build directory the local method delivered into (or the report file
    itself); the unsigned ``firmware.bin``/``firmware.hex`` sit next to the
    report, and the report's ``signing.arguments`` are the exact imgtool
    parameters the build was linked for. Every refusal is raised here,
    before imgtool runs, so the step's own failure mode is "imgtool said
    no" and nothing else. *key* is already resolved — this plans the
    command, it does not choose the key (:func:`sign_report` does).
    """
    report_path = _resolve_report(target)
    out_dir = report_path.parent
    report = read_build_report(report_path)
    parameters = SigningParameters.from_dict(report["signing"]["arguments"])

    program = require_imgtool(env=env)
    commands: list[tuple[str, tuple[str, ...], Path]] = []
    for source_name, output_name in REPORT_FIRMWARE:
        source = out_dir / source_name
        if not source.is_file():
            continue
        destination = out_dir / output_name
        form = Path(source_name).suffix.lstrip(".")
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
            f"The build report {report_path} names a build whose firmware is not here.",
            hint=(
                "signing works on the artifacts of a finished build; a directory "
                "that holds a report but no firmware.bin/firmware.hex has to be built again."
            ),
        )
    return SignPlan(
        out_dir=out_dir,
        manifest_path=report_path,
        key=key,
        parameters=parameters,
        commands=tuple(commands),
    )


def sign_report(
    target: Path,
    *,
    env: dict[str, str],
    key: Path | str | None = None,
    project: Project | None = None,
    runner: Runner | None = None,
) -> SignPlan:
    """Sign the firmware a build container delivered, from its §7.2.1 report.

    The build-report counterpart of :func:`sign_build`. The key is
    resolved exactly as a build resolves it (``--signing-key``, then
    :data:`~mcuhome.workbench.signing.KEY_VAR`, then the *project*'s
    ``secrets/firmware/mcuboot.yaml`` reference) and, as there, **never
    generated** here: a delivered build has to be signed with the key
    its device's bootloader already carries. The resolved key is a file
    either way, and imgtool gets exactly that file.
    """
    resolved = signing.signing_key(key, env=env, project=project, create=False)
    plan = plan_report_signing(target, key=resolved.path, env=env)
    run_signing(plan, runner=runner)
    return plan


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
    env: dict[str, str],
    key: Path | str | None = None,
    project: Project | None = None,
    runner: Runner | None = None,
) -> SignPlan:
    """Sign the application image a build directory holds, and say where.

    The key is resolved exactly as a build resolves it (``--signing-key``,
    then :data:`~mcuhome.workbench.signing.KEY_VAR`, then the *project*'s
    ``secrets/firmware/mcuboot.yaml``), but it is **not** generated on
    first need here: a build directory that was produced elsewhere has to
    be signed with the key its device's bootloader carries, and inventing
    one at this point would produce firmware that nothing accepts.
    """
    resolved = signing.signing_key(key, env=env, project=project, create=False)
    plan = plan_signing(target, key=resolved.path, env=env)
    run_signing(plan, runner=runner)
    return plan
