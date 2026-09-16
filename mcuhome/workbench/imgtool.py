# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Detached signing: ``imgtool`` over a finished image.

MCUboot signing is a post-build step over the linked binary — the image
does not know it is going to be signed, and nothing about it changes
except a header that was already reserved and a trailer that is appended.
That is what makes it possible for the key to live where the user's
controlling instance runs, never on a build server: a remote builder
returns an unsigned image and the signature happens somewhere else
entirely.

**One signing path, whatever built the image.** Zephyr's
``cmake/mcuboot.cmake`` *can* sign inline, deriving the arguments from
Kconfig and devicetree — but no MCUHome build uses that any more: every
build produces an **unsigned** image and states those same arguments in
its ``build-report.json`` (mcuhome-sdk ``docs/spec/build-actions.md``
§2.2). This module is the one place they are
turned back into a command — run right after the build by ``mcuhome device build``, or later
by ``mcuhome device sign-firmware`` on another machine — so the private key lives in no
build at all. The argument order below is Zephyr's,
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

**Deciding, then running.** :func:`plan_signing` answers every command
signing will run, every file it will write and every file it will delete
first, and raises everything the run itself could raise — so a caller can
show all of that to a user before anything happens, and so that
:func:`sign_firmware`'s own failure mode is "the signing program said
no". What the run answers is a :class:`SigningResult`: the key it used
and the files it produced, which is what a client prints — it does not
assemble that out of the plan and a directory listing of its own.

**The whole act lives here, not half of it.** Signing a build directory
is three things and a client does none of them itself: the previous
signature of that directory is removed, the images are signed, and — for
a device that takes updates over the air, which this package decides from
the device model — the signed binary is wrapped in the Matter OTA image
(:mod:`mcuhome.workbench.otafile`). Splitting that between a library and
its clients is how a stale ``firmware.signed.bin`` survives beside a
fresh unsigned one, and how two clients end up disagreeing about which
devices can be updated at all.

**Where imgtool comes from.** It is a **declared dependency** of
``mcuhome-workbench`` — the package MCUboot publishes itself, pinned in
``pyproject.toml`` to the release line of the MCUboot revision the SDK's
west manifest carries. The lookup is: the option ``signing.imgtool`` as the escape
hatch, then the installed package's console script (next to the running
interpreter first, then ``PATH``). Nothing here runs the west
workspace's checkout script any more: that script's requirements
(click, cryptography, …) belong to the Zephyr build environment, not to
whatever venv the workbench happens to run in — an inherited-environment
accident, not a contract. Signing does not run in the
build container either: handing a private key to a container to save a
dependency is the wrong trade, and this step needs no toolchain.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcuhome.model.errors import BuildError
from mcuhome.model.model import DeviceModel
from mcuhome.model.ota import ota_parameters
from mcuhome.model.registry import SIGNATURE_TYPE
from mcuhome.model.signing import SigningParameters
from mcuhome.model.userpaths import expand

from mcuhome.workbench import signing
from mcuhome.workbench.otafile import ota_file_name, write_ota_image
from mcuhome.workbench.project import Project

__all__ = [
    "BUILD_REPORT_FILE",
    "REPORT_VERSION",
    "SIGNED_FIRMWARE_NAMES",
    "MemoryRegion",
    "SigningRunner",
    "SignPlan",
    "SignedArtifact",
    "SigningResult",
    "find_imgtool",
    "memory_footprint",
    "plan_signing",
    "read_build_report",
    "require_imgtool",
    "run_signing",
    "sign_command",
    "sign_firmware",
]

#: The build report a build container delivers next to its unsigned
#: firmware (the ``report`` artifact; mcuhome-sdk
#: ``docs/spec/build-actions.md`` §2.1/§2.2). It exists for one consumer —
#: the client that signs detached — and carries only what that client
#: needs. The name mirrors
#: ``mcuhome.compiler.abi.REPORT_ARTIFACT``,
#: restated here because this package must not import the compiler (it is
#: what the compiler imports).
BUILD_REPORT_FILE = "build-report.json"

#: The report format version this signer implements. "A consumer that does
#: not implement the version it finds MUST NOT sign from the document"
#: (§2.2), so a mismatch is a refusal that names both numbers.
REPORT_VERSION = 1

#: ``<unsigned firmware in out> -> <signed name beside it>`` for the two
#: encodings a build container delivers with role ``firmware`` (§2.1:
#: ``firmware.hex`` to flash, ``firmware.bin`` to sign). The §2.2
#: signing parameters "apply to **every** artifact declared with role
#: ``firmware``", so both are signed with the one set of arguments.
SIGNED_FIRMWARE_NAMES = (
    ("firmware.bin", "firmware.signed.bin"),
    ("firmware.hex", "firmware.signed.hex"),
)

#: Runs one imgtool invocation and answers with its exit status and
#: whatever it printed. Injectable so the test suite can watch the
#: commands without starting a process. Named for what it runs:
#: :data:`mcuhome.workbench.buildprocess.Runner` is the other callable of
#: this kind in the package and answers a different type, and two aliases
#: under one word would be read as one thing.
SigningRunner = Callable[[list[str]], tuple[int, str]]


def find_imgtool(*, env: dict[str, str], stated: str | None = None) -> list[str] | None:
    """The argv prefix that runs imgtool, or None if there is none.

    A list rather than a path because *stated* may name a script that
    needs an interpreter in front of it.

    *stated* is the resolved ``signing.imgtool`` — a path or a program
    name — and this module reads no variable of its own: the
    configuration layer reads that key once, through whichever channel
    its user set it in. Without it the declared dependency answers (the
    console script beside this interpreter), and failing that ``PATH``.

    *env* is stated too, never read from the process: which imgtool runs
    is part of what a build is, and one process may serve several
    callers with different answers.
    """
    if stated:
        candidate = expand(stated, env)
        if candidate.suffix == ".py" or candidate.is_file():
            return [sys.executable, str(candidate)]
        return [stated]
    # The declared dependency: pip puts the console script next to the
    # interpreter it installed for — the venv this process runs in.
    beside = Path(sys.executable).parent / "imgtool"
    if beside.is_file():
        return [str(beside)]
    found = shutil.which("imgtool", path=env.get("PATH"))
    if found:
        return [found]
    return None


def require_imgtool(*, env: dict[str, str], stated: str | None = None) -> list[str]:
    """:func:`find_imgtool`, or a refusal that says where to get one."""
    program = find_imgtool(env=env, stated=stated)
    if program is not None:
        return program
    raise BuildError(
        "MCUHome cannot sign this image: imgtool is not available here.",
        hint=(
            "imgtool is MCUboot's signing tool and a declared dependency of "
            "mcuhome-workbench, so this installation is incomplete. Reinstall\n"
            "    pip install --force-reinstall mcuhome-workbench\n"
            "or point the option signing.imgtool at a specific imgtool."
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

    Every refusal a user can hit is raised while this is assembled, so the
    failure mode of the step itself is "imgtool said no", never "MCUHome
    could not find something".
    """

    #: Directory the build report lives in; every path below is under it.
    out_dir: Path
    report_path: Path
    #: The signing key file — the same durable file the commands below
    #: carry: the one the project's secrets YAML references, or the plain
    #: PEM a caller named
    #: (:attr:`~mcuhome.workbench.signing.SigningKey.path`).
    key: Path
    parameters: SigningParameters
    #: One entry per artifact format, in a stable order: format, command,
    #: and the file it produces.
    commands: tuple[tuple[str, tuple[str, ...], Path], ...]
    #: The Matter OTA image this signature will be wrapped in, or ``None``
    #: — for a caller that stated no device model, and for a device that
    #: takes no over-the-air update. It is in :attr:`outputs` as well: it
    #: is a file signing writes.
    ota: Path | None = None
    #: What signing removes **before** it writes, because it is there
    #: now: the signed images and the OTA image of whatever was signed in
    #: this directory last. A file that is not there is not in the list —
    #: this is what a person is shown before the act, not a list of names
    #: this package knows.
    removes: tuple[Path, ...] = ()

    @property
    def outputs(self) -> list[Path]:
        """Every file signing will write, the OTA image last."""
        written = [path for _, _, path in self.commands]
        return written if self.ota is None else [*written, self.ota]

    def to_dict(self) -> dict[str, Any]:
        """What a caller shows before it signs: the commands themselves.

        The imgtool parameters are in every command already — printing
        them twice would be two places to read one fact — so the document
        carries the argv as it will be run. ``outputs`` and ``removes``
        are the two facts the commands do *not* carry: the OTA image is
        written by this package rather than by imgtool, and what signing
        deletes first appears in no command line at all — which made the
        one destructive part of the act the one part a preview could not
        show.
        """
        return {
            "out_dir": str(self.out_dir),
            "report_path": str(self.report_path),
            "key": str(self.key),
            "commands": [
                {"format": form, "argv": list(argv), "output": str(output)}
                for form, argv, output in self.commands
            ],
            "outputs": [str(path) for path in self.outputs],
            "removes": [str(path) for path in self.removes],
        }


@dataclass(frozen=True)
class SignedArtifact:
    """One file signing produced, and which encoding it holds."""

    #: ``bin`` or ``hex`` — the encoding of the artifact that was signed,
    #: taken from its own file name rather than invented here.
    format: str
    path: Path

    def to_dict(self) -> dict[str, Any]:
        return {"format": self.format, "path": str(self.path)}


@dataclass(frozen=True)
class SigningResult:
    """What signing a build directory produced.

    Answered only when every command ran: a signing program that says no
    is a refusal carrying its own words, because for a wrong key or a
    too-small slot that message is the actionable part and MCUHome has
    nothing to add to it. :attr:`ok` is therefore not a second way of
    reporting failure — it states that every file the plan named is
    there, the OTA image included, which is the one thing a caller would
    otherwise have to go and check itself.

    What is **not** in here is what signing removed: that is the plan's
    (:attr:`SignPlan.removes`), because it is a statement about the act
    somebody is about to authorise rather than about its result, and
    because after the act the files are gone either way.
    """

    ok: bool
    out_dir: Path
    report_path: Path
    #: The key file the images were signed with — the durable path, the
    #: one a caller can still show a user afterwards.
    key: Path
    signed: tuple[SignedArtifact, ...]
    #: The Matter OTA image wrapped around the signed binary, or ``None``
    #: for a device that takes no over-the-air update and for a caller
    #: that stated no model. Whether a device can take one is this
    #: package's answer (:func:`~mcuhome.model.ota.ota_parameters`), not
    #: a rule a client is left to implement.
    ota: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "out_dir": str(self.out_dir),
            "report_path": str(self.report_path),
            "key": str(self.key),
            "signed": [artifact.to_dict() for artifact in self.signed],
            "ota": None if self.ota is None else str(self.ota),
        }


def _resolve_report(target: Path) -> Path:
    """Accept a build directory or the build report inside one."""
    if target.is_dir():
        return target / BUILD_REPORT_FILE
    return target


def read_build_report(path: Path) -> dict:
    """Load a §2.2 ``build-report.json``, or refuse in plain language.

    The report is what a build environment delivers beside the unsigned
    firmware: it carries the ``report`` format version and the
    mandatory ``signing`` block, and nothing a signer does not need.
    This checks exactly what has to hold before a signature can
    be planned from it — that it parses, that the version is one this
    signer implements, that a ``signing.arguments`` object is there to turn
    into an ``imgtool sign`` command, and that ``signing.signature_type`` is
    the one algorithm MCUHome signs with. That last field is mandatory in
    §2.2 for a reason a signer feels directly: it lets the client refuse a
    key whose algorithm the bootloader would not verify, here, instead of
    producing an image the device silently will not boot.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise BuildError(
            f"MCUHome cannot read the build report {path}: {error.strerror}.",
            hint=(
                "a local build delivers one next to the unsigned image "
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
                "the report format is a versioned contract: a mismatch is a refusal "
                "that names both numbers. Sign with a matching mcuhome version."
            ),
        )
    signing_block = data.get("signing")
    if not isinstance(signing_block, dict) or not isinstance(signing_block.get("arguments"), dict):
        raise BuildError(
            f"The build report {path} carries no signing parameters.",
            hint=(
                "a build report states the four imgtool arguments its image is "
                "signed with — a report without them is truncated. Build again."
            ),
        )
    signature_type = signing_block.get("signature_type")
    if signature_type != SIGNATURE_TYPE:
        raise BuildError(
            f"The build report {path} signs with signature_type {signature_type!r}, and "
            f"MCUHome images are {SIGNATURE_TYPE}.",
            hint=(
                "a build report states its signature type so a client can refuse a key "
                "whose algorithm the bootloader would not verify, instead of producing an "
                "image the device cannot boot. Build again with a matching mcuhome."
            ),
        )
    return data


def _is_count(value: Any) -> bool:
    """Whether *value* is a byte count the report actually measured."""
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class MemoryRegion:
    """How much of one image's one memory region a build used.

    What a build environment measured when it relinked, read back out of
    the build report it delivered. The keys keep the report's own
    spelling, because the report is that specification's document and
    this package reads it rather than renaming it.
    """

    #: The image the figure is about, as the report names it.
    image: str
    #: The region, as the report names it (``FLASH``, ``RAM``, …).
    region: str
    #: Bytes used.
    used: int
    #: Bytes the region holds.
    total: int

    def to_dict(self) -> dict[str, Any]:
        """One region as a document, JSON-ready and complete."""
        return {
            "image": self.image,
            "region": self.region,
            "used": self.used,
            "total": self.total,
        }


def memory_footprint(report: Mapping[str, Any]) -> tuple[MemoryRegion, ...]:
    """The memory figures *report* states, typed, in the order it states them.

    A plain noun, because it derives from its argument alone and touches
    nothing: :func:`read_build_report` reads the document, this reads the
    one part of it that is written for a person rather than for the
    signer. Without it every client parses the same list itself, and the
    figures a build measured would be shaped differently in each of them.

    ``memory`` is **optional** in the report (a build that relinked
    nothing states none), and an entry that is not an object or whose
    ``used`` and ``total`` are not whole numbers — the specification's
    own type for them — is left out rather than answered as zero: a
    figure this package invented would be read as one a build measured.
    The percentage the report also carries is not here — it is the two
    numbers divided, and a document that states a derived value twice
    can contradict itself.
    """
    entries = report.get("memory")
    if not isinstance(entries, list):
        return ()
    found: list[MemoryRegion] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        used, total = entry.get("used"), entry.get("total")
        # ``bool`` is an ``int`` in Python and is not a byte count
        # anywhere: a report that states one is malformed, not measured.
        if not _is_count(used) or not _is_count(total):
            continue
        found.append(
            MemoryRegion(
                image=str(entry.get("image", "")),
                region=str(entry.get("region", "")),
                used=used,
                total=total,
            )
        )
    return tuple(found)


def _removable(out_dir: Path) -> tuple[Path, ...]:
    """What a previous signature of *out_dir* left, and this one replaces.

    The signed images by name and the OTA images by pattern — an ``.ota``
    carries the device and the version it wraps in its own name, so it
    cannot be named here, and nothing but this package writes one into a
    build directory. Only what is actually there: a plan states what will
    happen, not what might have.
    """
    found = [out_dir / signed for _unsigned, signed in SIGNED_FIRMWARE_NAMES]
    found += sorted(out_dir.glob("*.ota"))
    return tuple(path for path in found if path.is_file())


def plan_signing(
    out_dir: Path,
    *,
    env: Mapping[str, str],
    key: Path | str | None = None,
    project: Project | None = None,
    imgtool: str | None = None,
    model: DeviceModel | None = None,
) -> SignPlan:
    """Every command signing *out_dir* will run, decided before any of them.

    *out_dir* is the build directory the build delivered into (or the
    build report inside it); the unsigned ``firmware.bin`` /
    ``firmware.hex`` sit next to the report, and the report's
    ``signing.arguments`` are the exact imgtool parameters the build was
    linked for. Every refusal is raised here, before imgtool runs, so the
    signing step's own failure mode is "imgtool said no" and nothing
    else: a missing signing program, an unreadable key, an artifact the
    report names and the directory does not hold.

    The key is resolved exactly as a build resolves it (*key* — the
    resolved ``signing.key`` — then the *project*'s reference) and, as
    there, **never generated**: a delivered build has to be signed with
    the key its device's bootloader already carries. The resolved key is
    a file either way, and imgtool gets exactly that file. *imgtool* is
    the resolved ``signing.imgtool``, stated for the same reason as the
    key: this module reads no configuration channel of its own.

    *model* is the device this build is of, and what it adds is the
    over-the-air half of the act: given it, the plan carries the Matter
    OTA image the signed binary will be wrapped in
    (:attr:`SignPlan.ota`), for a device that can take one at all.
    Whether it can is a property of the board and of the device's own
    stack and therefore this package's answer
    (:func:`~mcuhome.model.ota.ota_parameters`) — a client that decided
    it would be implementing a product rule of its own. Without a model
    there is no identity to put in the header and no name to give the
    file, and signing wraps nothing.

    :attr:`SignPlan.removes` is what a **previous** signature of this
    directory left and this one replaces — the signed images and the OTA
    image beside them. It is part of the plan because it is part of the
    act, and because a preview that showed the commands but not the
    deletions would hide the one destructive thing signing does.
    """
    resolved = signing.resolve_signing_key(key, env=env, project=project)
    report_path = _resolve_report(out_dir)
    out_dir = report_path.parent
    report = read_build_report(report_path)
    parameters = SigningParameters.from_dict(report["signing"]["arguments"])

    program = require_imgtool(env=env, stated=imgtool)
    commands: list[tuple[str, tuple[str, ...], Path]] = []
    for source_name, output_name in SIGNED_FIRMWARE_NAMES:
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
                        program,
                        parameters=parameters,
                        key=resolved.path,
                        source=source,
                        output=destination,
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
    # The OTA image wraps the signed *binary*: a directory that only
    # holds a hex image has nothing to wrap, and a device that takes no
    # over-the-air update has nowhere to send one.
    ota = None
    if model is not None and any(form == "bin" for form, _argv, _output in commands):
        identity = ota_parameters(model)
        if identity is not None:
            ota = out_dir / ota_file_name(model.device.name, identity.version)
    return SignPlan(
        out_dir=out_dir,
        report_path=report_path,
        key=resolved.path,
        parameters=parameters,
        commands=tuple(commands),
        ota=ota,
        removes=_removable(out_dir),
    )


def sign_firmware(
    out_dir: Path,
    *,
    env: Mapping[str, str],
    key: Path | str | None = None,
    project: Project | None = None,
    imgtool: str | None = None,
    model: DeviceModel | None = None,
) -> SigningResult:
    """Sign the firmware a build delivered, from its §2.2 build report.

    What :func:`plan_signing` decided, run: the same call for a build
    that has just finished and for one a build server delivered weeks
    ago, because everything it needs is in the directory and the key is
    wherever the user keeps it. A caller that wants to show the commands
    beforehand asks for the plan and calls this afterwards — the plan is
    then decided twice, over a directory nothing touched in between, and
    it is the same plan both times.

    **The previous signature goes first.** What this directory was signed
    to last — the signed images, the OTA image wrapped around one of them
    — is removed before anything is written, because otherwise a run that
    signs one encoding, or a build of another version, leaves an image
    from an earlier signature beside the fresh one. That image is
    flashable, looks current, and belongs to no build that is there any
    more; the hygiene therefore belongs to the act that creates such
    files and not to whoever happens to call it.

    **The over-the-air image is part of signing**, given *model*: a
    Matter device takes updates as an ``.ota`` wrapped around the
    **signed** binary, which only exists here — signing may happen on a
    machine that has no compiler and no build. So this writes it, names
    it after the device and its version, and answers it in
    :attr:`SigningResult.ota`; for a device that takes no over-the-air
    update, and for a caller that stated no model, the answer is
    ``None``. A payload that is missing or empty is a
    :class:`~mcuhome.model.errors.BuildError` from the writer, not a
    quiet ``None``.
    """
    plan = plan_signing(out_dir, env=env, key=key, project=project, imgtool=imgtool, model=model)
    for path in plan.removes:
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            raise BuildError(
                f"MCUHome cannot replace the earlier signature {path}: {error.strerror}.",
                hint=(
                    "signing removes what a previous run of this build directory signed "
                    "before it writes, so nothing flashable is left over from an image "
                    "that is no longer there. Remove the file yourself, or sign in a "
                    "directory you can write in."
                ),
            ) from error
    written = run_signing(plan)
    signed = tuple(
        SignedArtifact(format=form, path=path)
        for (form, _argv, _output), path in zip(plan.commands, written, strict=True)
    )
    ota = None
    binary = next((artifact.path for artifact in signed if artifact.format == "bin"), None)
    if plan.ota is not None and model is not None and binary is not None and binary.is_file():
        # Only over an image that is really there: a signing program that
        # wrote nothing is answered with `ok` false below, not with a
        # refusal about a payload the caller never asked about.
        image = write_ota_image(model, payload=binary, out_dir=plan.out_dir)
        ota = None if image is None else image.path
    return SigningResult(
        # Every file the plan named, which is the one thing a caller
        # would otherwise have to go and check itself.
        ok=all(path.is_file() for path in plan.outputs),
        out_dir=plan.out_dir,
        report_path=plan.report_path,
        key=plan.key,
        signed=signed,
        ota=ota,
    )


def run_signing(plan: SignPlan, *, runner: SigningRunner | None = None) -> list[Path]:
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
