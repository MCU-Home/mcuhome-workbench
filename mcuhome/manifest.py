# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""``build-manifest.json`` — what a build produced (builder-pipeline.md §7).

A build directory is a pile of files with conventions attached. The
manifest is the machine-readable answer to "what is in here": which
device, built how, which images came out, what each one hashes to, and —
the part that makes detached signing possible at all — the exact
``imgtool`` parameters the application image is signed with, whether or
not this build signed it.

**Why hashes.** Dashboard ADR 0010 wants an artifact set whose integrity
can be checked after it crossed a network, and ADR 0006/0007 make that
network normal: the build server is a different machine, possibly a
different architecture. A SHA-256 per file is what turns "the build
returned some bytes" into "the build returned *these* bytes".

**Deterministic except for what it measures.** Everything in the manifest
either comes from the configuration, from the registry, or from the
builder's own version — the same inputs produce the same JSON, key order
included. The exceptions are the sizes and hashes, which are the point.
No timestamps, no host names, no build directory absolute paths: every
path is relative to the directory the manifest sits in, so the same
artifact set describes itself the same way wherever it is unpacked.

**The signing block is a contract, not a report.** It states the four
``imgtool sign`` arguments (``--version``, ``--header-size``,
``--align``, ``--slot-size``) with imgtool's own option names as keys, so
a consumer does not have to know how Zephyr derives them. Three of the
four come from the registry — ADR 0015 decision 2 makes the partition
table per-board data, and the builder is what wrote it into the overlay —
and the fourth from the application image's own Kconfig.
:mod:`mcuhome.imgtool` is what turns the block back into a command.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcuhome import __version__, ota, pairing, registry, workspace
from mcuhome.errors import BuildError
from mcuhome.model import DeviceModel

__all__ = [
    "MANIFEST_FILE",
    "MANIFEST_VERSION",
    "SIGN_VERSION_DEFAULT",
    "BuildManifest",
    "FileEntry",
    "ImageEntry",
    "OtaEntry",
    "SigningBlock",
    "SigningParameters",
    "build_manifest",
    "dump_manifest",
    "ota_entry",
    "ota_parameters",
    "read_manifest",
    "record_ota",
    "record_signature",
    "signing_parameters",
    "write_manifest",
]

#: Where the manifest lands: the top of the build directory, next to
#: ``device-model.json`` — the two files a human or a program reads to
#: know what a build directory is, both outside the CMake tree that can be
#: deleted at any time.
MANIFEST_FILE = "build-manifest.json"

#: Format revision of this file. Bumped when a consumer would break;
#: additive fields do not bump it (dashboard ADR 0011 decision 2 makes the
#: coupling a declared range, and a range is only useful if what is inside
#: it stays readable).
MANIFEST_VERSION = 1

#: Kconfig symbol carrying imgtool's ``--version`` on the application
#: image, and the value Zephyr defaults it to. Read from the built image
#: rather than assumed, because it is the one of the four parameters the
#: builder does not itself decide.
SIGN_VERSION_SYMBOL = "CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION"
SIGN_VERSION_DEFAULT = "0.0.0+0"

#: Kconfig symbol carrying the MCUboot header offset the application was
#: linked with — imgtool's ``--header-size``. Cross-checked against the
#: registry rather than trusted blindly: a disagreement between what the
#: builder thinks the layout is and what the image was actually linked
#: for is exactly the kind of silent mismatch that produces firmware
#: which builds, flashes and then does not boot.
HEADER_SIZE_SYMBOL = "CONFIG_ROM_START_OFFSET"

#: Sub-directory of an image's build directory that holds its artifacts.
_OUTPUT_DIR = "zephyr"

#: Read in blocks rather than whole: a Matter image is most of a megabyte
#: and there is no reason for it to be in memory twice.
_HASH_BLOCK = 1 << 20


# --------------------------------------------------------------------------
# Pieces
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FileEntry:
    """One artifact file: where it is, how big, and what it hashes to."""

    #: Relative to the build directory, with forward slashes on every
    #: platform — a manifest is read on the other side of a network.
    path: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class ImageEntry:
    """One sysbuild image and everything it produced."""

    #: Sysbuild's name for the image, which is also its sub-directory.
    name: str
    #: ``"bootloader"``, ``"application"``, or the image name again.
    role: str
    #: Size of the unsigned ``zephyr.bin``, i.e. what the linker put in
    #: flash. The signed file is larger by header and trailer, which is
    #: not what a footprint number means.
    flash_bytes: int | None
    files: tuple[FileEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "flash_bytes": self.flash_bytes,
            "files": [entry.to_dict() for entry in self.files],
        }


@dataclass(frozen=True)
class OtaEntry:
    """The Matter OTA identity of this build, and the file when there is one.

    Two things in one block on purpose. The identity — version, the
    SoftwareVersion derived from it, the vendor and product IDs the header
    carries — is known the moment the build is planned, and stating it even
    when no file exists yet is what lets ``mcuhome sign`` write the .ota on
    a machine that has the manifest, the signed image and nothing else
    (ADR 0015 decision 8 puts signing where the key is, which is not where
    the compiler is).

    :attr:`file` is None until then, and ``capable`` in the serialized form
    says why the block exists at all: a board with no staging slot gets no
    block, so a block with no file means "not yet", never "never".
    """

    version: str
    software_version: int
    vendor_id: int
    product_id: int
    file: FileEntry | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "version": self.version,
            "software_version": self.software_version,
            "vendor_id": self.vendor_id,
            "product_id": self.product_id,
        }
        data.update(
            self.file.to_dict()
            if self.file is not None
            else {"path": None, "size": None, "sha256": None}
        )
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OtaEntry:
        try:
            return cls(
                version=str(data["version"]),
                software_version=int(data["software_version"]),
                vendor_id=int(data["vendor_id"]),
                product_id=int(data["product_id"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise BuildError(
                f"The build manifest's Matter OTA parameters are incomplete: {error}.",
                hint=(
                    "the manifest is written by mcuhome build; an edited or "
                    "truncated one cannot be wrapped into an .ota file. Build again."
                ),
            ) from error


@dataclass(frozen=True)
class SigningParameters:
    """The four ``imgtool sign`` arguments an MCUHome image is signed with.

    Keys in the serialized form are imgtool's own option names, so the
    block reads as the command it stands for. ``header_size`` and
    ``slot_size`` are byte counts; ``version`` is imgtool's
    ``major.minor.revision+build`` string, which MCUboot compares
    monotonically.
    """

    header_size: int
    align: int
    slot_size: int
    version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "header-size": self.header_size,
            "align": self.align,
            "slot-size": self.slot_size,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SigningParameters:
        try:
            return cls(
                header_size=int(data["header-size"]),
                align=int(data["align"]),
                slot_size=int(data["slot-size"]),
                version=str(data["version"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise BuildError(
                f"The build manifest's signing parameters are incomplete: {error}.",
                hint=(
                    "the manifest is written by mcuhome build; an edited or "
                    "truncated one cannot be signed from. Build again."
                ),
            ) from error


@dataclass(frozen=True)
class SigningBlock:
    """Everything a detached signer needs, and nothing it does not.

    :attr:`inputs` and :attr:`outputs` name the same files the inline
    build would have written, which is what makes the two paths
    comparable at all: signing detached is the same tool over the same
    bytes with the same arguments, run somewhere else.
    """

    #: Sysbuild image name of the signed image (the application).
    image: str
    #: False when the build deliberately left the image unsigned
    #: (``--no-sign``), so that the key never reached the builder.
    signed_by_the_build: bool
    signature_type: str
    parameters: SigningParameters
    #: Unsigned artifact per format, relative to the build directory.
    inputs: dict[str, str]
    #: Where the signed artifact of each format goes.
    outputs: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": "imgtool",
            "image": self.image,
            # Two facts, not one: *signed* is whether a signature exists
            # in this directory now, and it is what a consumer checks
            # before flashing. *signed_by_the_build* is how the build ran
            # and never changes afterwards, which is what says whether a
            # private key was anywhere near the machine that compiled.
            "signed": self.signed_by_the_build,
            "signed_by_the_build": self.signed_by_the_build,
            "signature_type": self.signature_type,
            "arguments": self.parameters.to_dict(),
            "inputs": dict(sorted(self.inputs.items())),
            "outputs": dict(sorted(self.outputs.items())),
        }


@dataclass(frozen=True)
class BuildManifest:
    """``build-manifest.json``, as an object."""

    device: str
    friendly_name: str
    board: str
    #: SemVer of the firmware this build produced (ADR 0015 decision 9).
    version: str
    model_version: int
    builder_version: str
    snippets: tuple[str, ...]
    bootloader_snippets: tuple[str, ...]
    jobs: int
    images: tuple[ImageEntry, ...]
    signing: SigningBlock | None
    merged: FileEntry | None = None
    #: The Matter OTA file, when this build produced one. None for a board
    #: that cannot take one (ADR 0015 decision 5), for a device without
    #: Matter, and for a detached build until `mcuhome sign` has run —
    #: an .ota wraps the SIGNED image, so before that there is nothing to
    #: wrap.
    ota: OtaEntry | None = None
    manifest_version: int = MANIFEST_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "builder": {"version": self.builder_version, "model_version": self.model_version},
            "device": {
                "name": self.device,
                "friendly_name": self.friendly_name,
                "board": self.board,
                "version": self.version,
            },
            "build": {
                "snippets": list(self.snippets),
                "bootloader_snippets": list(self.bootloader_snippets),
                "jobs": self.jobs,
            },
            "images": [image.to_dict() for image in self.images],
            "merged": None if self.merged is None else self.merged.to_dict(),
            "signing": None if self.signing is None else self.signing.to_dict(),
            "ota": None if self.ota is None else self.ota.to_dict(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2) + "\n"

    def image(self, name: str) -> ImageEntry | None:
        return next((entry for entry in self.images if entry.name == name), None)


# --------------------------------------------------------------------------
# Reading a build directory
# --------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_HASH_BLOCK):
            digest.update(block)
    return digest.hexdigest()


def _file_entry(path: Path, *, out_dir: Path) -> FileEntry:
    return FileEntry(
        path=path.resolve().relative_to(out_dir.resolve()).as_posix(),
        size=path.stat().st_size,
        sha256=_sha256(path),
    )


def read_kconfig(path: Path) -> dict[str, str]:
    """A Zephyr ``.config`` as ``symbol -> value``, quotes stripped.

    Deliberately not a Kconfig parser: the file is already the *result*
    of one, one assignment per line, and the builder reads two symbols
    out of it.
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        if not line.startswith("CONFIG_") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip('"')
    return values


def _as_int(text: str, fallback: int) -> int:
    try:
        return int(text, 0)
    except (TypeError, ValueError):
        return fallback


def signing_parameters(
    scheme: registry.UpdateSchemeDef, *, kconfig: dict[str, str] | None = None
) -> SigningParameters:
    """The arguments the inline build signs with, from board data.

    Three of the four are the registry's: the header offset and the write
    alignment are properties of the part, and the slot size is the
    partition table ADR 0015 decision 2 makes per-board data — the same
    table the builder rendered into the overlay this image was linked
    against, which is why it can state them without asking the build.

    *kconfig* is the built application's ``.config`` when there is one.
    It carries imgtool's ``--version``, which the builder does not decide,
    and it is where the header offset is cross-checked: a linker that
    reserved a different offset than the layout assumes produces firmware
    that builds and does not boot, and that is worth a refusal rather than
    a manifest stating something untrue.
    """
    values = kconfig or {}
    header_size = _as_int(values.get(HEADER_SIZE_SYMBOL, ""), scheme.header_size)
    if header_size != scheme.header_size:
        raise BuildError(
            f"The application was linked with a {header_size}-byte MCUboot header "
            f"offset, but this board's layout says {scheme.header_size}.",
            hint=(
                f"{HEADER_SIZE_SYMBOL} and the board's header_size in "
                "mcuhome/registry.py have to agree — an image signed against the "
                "wrong offset boots nowhere. This is a builder bug worth reporting."
            ),
        )
    return SigningParameters(
        header_size=scheme.header_size,
        align=scheme.write_block_size,
        slot_size=scheme.imgtool_slot.size,
        version=values.get(SIGN_VERSION_SYMBOL) or SIGN_VERSION_DEFAULT,
    )


def _signing_block(
    *,
    out_dir: Path,
    build_dir: Path,
    app_image: str,
    scheme: registry.UpdateSchemeDef,
    signed_by_the_build: bool,
) -> SigningBlock:
    output = build_dir / app_image / _OUTPUT_DIR
    relative = (output.resolve().relative_to(out_dir.resolve())).as_posix()
    return SigningBlock(
        image=app_image,
        signed_by_the_build=signed_by_the_build,
        signature_type=registry.SIGNATURE_TYPE,
        parameters=signing_parameters(
            scheme, kconfig=read_kconfig(build_dir / app_image / _OUTPUT_DIR / ".config")
        ),
        inputs={"bin": f"{relative}/zephyr.bin", "hex": f"{relative}/zephyr.hex"},
        outputs={
            "bin": f"{relative}/zephyr.signed.bin",
            "hex": f"{relative}/zephyr.signed.hex",
        },
    )


def ota_parameters(model: DeviceModel) -> OtaEntry | None:
    """The OTA identity of a device, or None when it cannot take one.

    "Cannot" is two different facts and both are checked here: the board's
    update scheme has to allow Matter OTA (ADR 0015 decision 5 — a board
    with nowhere to stage an image cannot), and the device has to have a
    Matter stack to receive it with.
    """
    board = registry.BOARDS.get(model.device.board)
    scheme = None if board is None else board.update_scheme

    if scheme is None or not scheme.matter_ota or not model.network.matter_enabled:
        return None
    return OtaEntry(
        version=model.device.version,
        software_version=ota.software_version(model.device.version),
        vendor_id=pairing.VENDOR_ID,
        product_id=pairing.PRODUCT_ID,
    )


def ota_entry(image: ota.OtaImage, *, out_dir: Path) -> OtaEntry:
    """Describe a freshly written .ota file for the manifest."""
    return OtaEntry(
        version=image.version,
        software_version=image.software_version,
        vendor_id=image.vendor_id,
        product_id=image.product_id,
        file=_file_entry(image.path, out_dir=out_dir),
    )


def build_manifest(
    model: DeviceModel,
    *,
    out_dir: Path,
    build_dir: Path,
    app_image: str,
    images: list[workspace.ImageArtifacts],
    snippets: tuple[str, ...] = (),
    bootloader_snippets: tuple[str, ...] = (),
    jobs: int,
    signed_by_the_build: bool,
    merged: Path | None = None,
    ota_image: ota.OtaImage | None = None,
) -> BuildManifest:
    """Describe what came out of stage 5, hashing every file it names."""
    board = registry.BOARDS.get(model.device.board)
    scheme = None if board is None else board.update_scheme
    return BuildManifest(
        device=model.device.name,
        friendly_name=model.device.friendly_name,
        board=model.device.board,
        version=model.device.version,
        model_version=model.model_version,
        builder_version=__version__,
        snippets=tuple(snippets),
        bootloader_snippets=tuple(bootloader_snippets),
        jobs=jobs,
        images=tuple(
            ImageEntry(
                name=image.name,
                role=image.role,
                flash_bytes=image.flash_bytes,
                files=tuple(_file_entry(path, out_dir=out_dir) for path in image.files),
            )
            for image in images
        ),
        signing=(
            None
            if scheme is None
            else _signing_block(
                out_dir=out_dir,
                build_dir=build_dir,
                app_image=app_image,
                scheme=scheme,
                signed_by_the_build=signed_by_the_build,
            )
        ),
        merged=None if merged is None else _file_entry(merged, out_dir=out_dir),
        ota=(ota_parameters(model) if ota_image is None else ota_entry(ota_image, out_dir=out_dir)),
    )


def write_manifest(manifest: BuildManifest, *, out_dir: Path) -> Path:
    """Write the manifest into the build directory and return its path."""
    path = out_dir / MANIFEST_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(manifest.to_json(), encoding="utf-8")
    except OSError as error:
        raise BuildError(
            f"The build manifest {path} cannot be written: {error.strerror}.",
            hint="pick a writable location with --build-dir",
        ) from error
    return path


def record_signature(data: dict[str, Any], *, out_dir: Path, files: list[Path]) -> dict[str, Any]:
    """Fold freshly signed artifacts back into a manifest mapping.

    A manifest describes a directory, so a directory that has gained a
    signed image has a manifest that says so — otherwise the next reader
    has to guess whether the files it finds are the ones the manifest
    hashed. *data* is modified in place and returned.
    """
    signing = data.get("signing")
    image_name = signing.get("image") if isinstance(signing, dict) else None
    if isinstance(signing, dict):
        signing["signed"] = True
    for image in data.get("images", []):
        if not isinstance(image, dict) or image.get("name") != image_name:
            continue
        entries = list(image.get("files") or [])
        for path in files:
            fresh = _file_entry(path, out_dir=out_dir).to_dict()
            for index, entry in enumerate(entries):
                if isinstance(entry, dict) and entry.get("path") == fresh["path"]:
                    entries[index] = fresh
                    break
            else:
                entries.append(fresh)
        image["files"] = entries
    return data


def record_ota(data: dict[str, Any], image: ota.OtaImage, *, out_dir: Path) -> dict[str, Any]:
    """Fold a freshly written .ota file into a manifest mapping.

    The detached path's half of the story: a build that left the image
    unsigned could not wrap it, so `mcuhome sign` writes the .ota after
    signing and says so here. *data* is modified in place and returned.
    """
    data["ota"] = ota_entry(image, out_dir=out_dir).to_dict()
    return data


def dump_manifest(data: dict[str, Any], *, out_dir: Path) -> Path:
    """Write a manifest mapping back where :func:`read_manifest` found it."""
    path = out_dir / MANIFEST_FILE
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def read_manifest(path: Path) -> dict[str, Any]:
    """Load a ``build-manifest.json``, or refuse in plain language.

    Returns the raw mapping rather than a :class:`BuildManifest`: a
    consumer reading a manifest written by a *newer* builder should be
    able to look at ``manifest_version`` and decide, which it cannot do
    if loading already failed on an unknown field.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise BuildError(
            f"MCUHome cannot read the build manifest {path}: {error.strerror}.",
            hint=(
                f"every build writes one next to its generated application. Point "
                f"at a build directory that contains {MANIFEST_FILE}, or build again."
            ),
        ) from error
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise BuildError(
            f"The build manifest {path} is not valid JSON ({error.msg}, line {error.lineno}).",
            hint="it is generator output — delete it and build again rather than editing it",
        ) from error
    if not isinstance(data, dict):
        raise BuildError(
            f"The build manifest {path} does not describe a build.",
            hint="it is generator output — delete it and build again rather than editing it",
        )
    return data
