# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Getting a pinned package onto this machine, and unpacking it safely.

A build context pins packages by ``(name, version, sha256)``: the SDK it
compiles, and the packages the build environment is assembled from. This
module turns such a pin into a tree on disk — found, hash-verified,
extracted under rules an archive cannot talk its way out of.

**Two tiers, in this order.** The operator's own directories first, in
order, so a machine that already has the package never opens a socket;
only then the package registry
(:mod:`mcuhome.workbench.packageregistry`), whose index the project's
trust anchor accepted. Either way the pinned hash decides: a file with
the right name and the wrong bytes is refused exactly as loudly as one
that is not there.

**The extraction is the untrusting half.** An archive is somebody else's
bytes: it may carry a member that climbs out of the tree, a member that
is a device node, a link that points at ``/etc``, or a stream that
expands until the disk is full. Every one of those is a typed refusal
here rather than a file on the host, and the bound on what a package may
unpack to is the caller's to state because the packages differ by orders
of magnitude.

Both build profiles use this module and neither owns it: the subprocess
profile unpacks the environment's packages into its store, both profiles
acquire the SDK the context pinned.
"""

from __future__ import annotations

import json
import posixpath
import shutil
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import zstandard
from mcuhome.model.errors import BuildError
from mcuhome.model.hashes import sha256_file

# The package name and the index file name are shared vocabulary and
# live in the model (`mcuhome.model.sdkindex`); they are re-exported
# here. The resolution against the index is deliberately this side's
# own: acquiring the pinned bytes happens by exact version — constraint
# resolution is the workbench's job, and by the time a context exists its
# pin is one version, not a range.
from mcuhome.model.sdkindex import INDEX_FILE, SDK_PACKAGE_NAME

from mcuhome.workbench.packageregistry import (
    RegistrySource,
    check_platform,
    matching_version,
    opened,
    resolve_entry,
)
from mcuhome.workbench.resolve_pins import SDK_SOURCE

__all__ = [
    "SDK_MAX_BYTES",
    "SDK_PACKAGE_NAME",
    "AcquiredPackage",
    "SdkUnavailable",
    "acquire_package",
    "acquire_sdk",
]

#: A generous bound on what the SDK archive unpacks to. Not a policy
#: anyone tunes — an operator who does not trust an SDK source should not
#: list it — but a corrupt or malicious archive comes out here as a
#: bounded read rather than an out-of-memory kill.
SDK_MAX_BYTES = 2 * 1024 * 1024 * 1024

#: How large a chunk the decompressor hands over at a time. Bounded
#: because a zstd frame can expand without limit, and a cap that only
#: fires after the expansion is not a cap.
_BLOCK = 1 << 20


@dataclass(frozen=True)
class AcquiredPackage:
    """One package, found and verified. The tree is already unpacked."""

    version: str
    sha256: str
    #: Where the archive was found, for the log and for a bug report. A
    #: path under the work root when it came off a registry mirror.
    source: Path
    #: The unpacked tree — for the SDK, what a step is handed at
    #: ``mcuhome/sdk``.
    tree: Path
    #: The package's own name, which is the concrete one: a family
    #: published per architecture was already resolved to this host's
    #: member before anything was fetched.
    name: str = SDK_PACKAGE_NAME


def acquire_package(
    *,
    kind: str = SDK_SOURCE,
    name: str = SDK_PACKAGE_NAME,
    version: str,
    sha256: str,
    sources: Sequence[Path],
    into: Path,
    registry: RegistrySource | None = None,
    platform: str | None = None,
    max_bytes: int | None = None,
    symlinks: bool = False,
) -> AcquiredPackage:
    """Find the pinned package, verify its bytes, unpack it safely.

    One rule, and it serves the SDK and every package a build
    environment is assembled from alike: the content of a tree matches
    the ``sha256`` that was pinned, and the package is acquired by
    ``(name, version, sha256)`` from operator-configured sources and the
    package registry only. The ``url`` a context carries beside a pin is
    a hint about where those bytes once came from, never an instruction
    to fetch them from there.

    **Two tiers, in this order.** The operator's own directories are
    searched first, in order, so a machine that already has the package
    never opens a socket — that is what makes an offline build a matter
    of configuration rather than a mode. Only when none of them holds it
    is *registry* asked, and then the bytes come off a mirror whose index
    the project's trust anchor accepted. *kind* names the source within
    that registry (``sdk``, ``build-workspace``, ``build-tools``); *name*
    is the concrete package, a family published per architecture having
    ordinarily been resolved to this host's member before a pin ever
    existed — *platform* overrides which host that is, and is only
    consulted where an answer depends on it.

    **The hash decides, not the name — on every path.** A local
    directory's ``index.json`` maps the version to a file; that file's
    bytes are hashed and the value must equal the pin. A registry's
    archive is hashed as it lands, against the entry in the index that
    verified. A file with the right name and the wrong bytes is refused
    exactly as loudly as one that is not there, and a registry entry
    whose sha256 is not the pinned one is refused before a byte is
    fetched. The unpack is :func:`_safe_extract`: regular files and
    directories only, with the executable bit preserved so a program the
    package carries can be spawned. *symlinks* widens that by exactly
    one member type, for the packages a build environment is assembled
    from — a toolchain and a third party's source world cannot be
    delivered without links — and only for links that stay inside their
    own tree.

    *max_bytes* is how much the archive may unpack to. It is a caller's
    decision because the packages differ by orders of magnitude, and a
    bound generous enough for the source world would be no bound at all
    for the SDK. ``None`` takes the SDK's own bound, which is the
    smallest of them: a caller that says nothing about a package's size
    is the one to be least generous with.

    A directory with **no index** is searched by the conventional
    filename, ``<name>-<version>.tar.zst``. That is not a weaker rule:
    what makes a candidate the pinned package is that its bytes hash to
    the pin, and the index only ever made it findable. An operator who
    drops one archive in a directory has said everything that has to be
    said, and requiring them to hand-write a manifest beside it would be
    a ceremony with nothing behind it.
    """
    limit = SDK_MAX_BYTES if max_bytes is None else max_bytes
    searched = [str(directory) for directory in sources]
    for directory in sources:
        found = _local_candidate(
            directory,
            name=name,
            version=version,
            sha256=sha256,
            searched=searched,
            platform=platform,
        )
        if found is None:
            continue
        archive, concrete = found
        measured = sha256_file(archive)
        if measured != sha256:
            raise _package_unavailable(
                name,
                version,
                sha256,
                searched,
                f"{archive} is named for this version and hashes to {measured}",
            )
        return _unpack(
            archive,
            into=into,
            name=concrete,
            version=version,
            sha256=sha256,
            limit=limit,
            symlinks=symlinks,
        )

    client = opened(registry)
    if client is not None:
        index = client.index(kind)
        entry = index.resolve(name, version, platform=platform)
        if entry.sha256 != sha256:
            raise _package_unavailable(
                name,
                version,
                sha256,
                [*searched, index.base],
                f"{index.base} publishes {entry.name} {version} with sha256 {entry.sha256}",
            )
        staging = into.parent / f"{into.name}.download"
        try:
            archive = client.fetch_package(index, entry, into=staging)
            return _unpack(
                archive,
                into=into,
                name=entry.name,
                version=version,
                sha256=sha256,
                limit=limit,
                symlinks=symlinks,
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    raise _package_unavailable(
        name, version, sha256, searched, f"no source directory holds {name} {version}"
    )


def acquire_sdk(
    *,
    version: str,
    sha256: str,
    sources: Sequence[Path],
    into: Path,
    registry: RegistrySource | None = None,
    max_bytes: int | None = None,
) -> AcquiredPackage:
    """:func:`acquire_package` for the SDK — the one package with a name of its own.

    The SDK is what a context pins and what a step is handed at
    ``mcuhome/sdk``, and it is the package acquired on every build, so it
    keeps a call of its own rather than making that path repeat the two
    constants that never vary for it.
    """
    return acquire_package(
        kind=SDK_SOURCE,
        name=SDK_PACKAGE_NAME,
        version=version,
        sha256=sha256,
        sources=sources,
        into=into,
        registry=registry,
        max_bytes=max_bytes,
    )


def _unpack(
    archive: Path,
    *,
    into: Path,
    name: str,
    version: str,
    sha256: str,
    limit: int,
    symlinks: bool = False,
) -> AcquiredPackage:
    """The archive on disk, expanded into *into* under the safe-extraction rules."""
    into.mkdir(parents=True, exist_ok=True)
    spool = into.parent / f"{into.name}.tar"
    try:
        _decompress(archive, spool, limit=limit, what=name)
        _safe_extract(spool, into=into, quota_bytes=limit, what=name, symlinks=symlinks)
    finally:
        spool.unlink(missing_ok=True)
    return AcquiredPackage(version=version, sha256=sha256, source=archive, tree=into, name=name)


def _local_candidate(
    directory: Path,
    *,
    name: str,
    version: str,
    sha256: str,
    searched: Sequence[str],
    platform: str | None = None,
) -> tuple[Path, str] | None:
    """The file in *directory* that is this package, and the name it is under.

    The index is consulted first because it can say two things a filename
    cannot. One: that this source holds the version and holds it with
    *other bytes*, which is a different situation from not having it and
    is worth a different refusal. Two: what a **family** name stands for
    here — an index published across architectures carries one name for a
    set of concrete packages, and following it is the only way to learn
    which file this host's member is. That is why the answer carries a
    name back: it may not be the one that was asked for.

    A directory with **no index** is searched by the conventional
    filename, ``<name>-<version>.tar.zst``, and the name is held against
    *platform* first. That check is what keeps the convention honest: a
    package built for another architecture says so in its name, and a
    directory with no index is the one place nothing else would catch it
    before the bytes were fetched, hashed and unpacked.

    A **family** has no conventional filename at all — a family is not
    bytes, and no publisher ever wrote ``<family>-<version>.tar.zst``. So
    a directory with no index cannot answer for one; what it can do is
    fail to find the file, which is what happens, and the search moves
    on. Learning that a name *is* a family needs an index, which is
    exactly what such a directory does not have.
    """
    index_path = directory / INDEX_FILE
    if index_path.is_file():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            index = None
        entries = _index_entries(index)
        # PEP 440 equality, not string equality: an index that spells a
        # release `0.1` holds the package a pin of `0.1.0` names, and a
        # lookup by text would walk past it.
        if matching_version(entries, name, version) is not None:
            try:
                resolved = resolve_entry(entries, name, version, platform=platform)
            except BuildError as unusable:
                # The index names this package and cannot describe it —
                # a broken meta hash, a foreign architecture. Skipping to
                # the next source would silently demote a source the
                # operator named on purpose.
                raise _package_unavailable(
                    name, version, sha256, list(searched), f"{index_path}: {unusable.message}"
                ) from unusable
            if resolved.sha256 != sha256:
                raise _package_unavailable(
                    name,
                    version,
                    sha256,
                    list(searched),
                    f"{index_path} lists {resolved.file} with sha256 {resolved.sha256}, "
                    f"and the context pins {sha256}",
                )
            candidate = directory / resolved.file
            if candidate.is_file():
                return candidate, resolved.name
    # Refused rather than skipped: a package built for another
    # architecture is not "not here", and finding that out after half a
    # gigabyte has been fetched and unpacked helps nobody.
    check_platform(name, platform=platform)
    named = directory / f"{name}-{version}.tar.zst"
    return (named, name) if named.is_file() else None


def _index_entries(index: object) -> dict[str, dict[str, dict]]:
    """A local ``index.json``'s packages, in the shape a resolution takes."""
    packages = index.get("packages") if isinstance(index, dict) else None
    if not isinstance(packages, dict):
        return {}
    return {
        str(name): versions for name, versions in packages.items() if isinstance(versions, dict)
    }


def _decompress(archive: Path, spool: Path, *, limit: int, what: str = SDK_PACKAGE_NAME) -> None:
    """zstd to a plain tar on disk, refusing an expansion mid-stream.

    Streaming rather than one-shot: a few kilobytes of zstd can expand to
    gigabytes, and a decompressor that returned its output as one
    ``bytes`` would have allocated the bomb before any check could run.
    """
    written = 0
    with archive.open("rb") as raw, spool.open("wb") as handle:
        reader = zstandard.ZstdDecompressor().stream_reader(raw)
        while block := reader.read(_BLOCK):
            written += len(block)
            if written > limit:
                raise BuildError(
                    f"The {what} package at {archive} unpacks to more than {limit} bytes.",
                    hint="the archive is corrupt or hostile — do not list a source you distrust",
                )
            handle.write(block)


def _safe_extract(
    archive: Path,
    *,
    into: Path,
    quota_bytes: int,
    what: str = SDK_PACKAGE_NAME,
    symlinks: bool = False,
) -> None:
    """Safe extraction: regular files and directories only.

    Absolute paths, ``..`` after normalization, symlinks, hardlinks and
    device nodes are rejected — each of them is a way out of the
    directory the tree is unpacked into. The archive's mode bits are
    discarded down to two values: 0600, or 0700 for a file the archive
    marked executable, because a package carries programs that are
    spawned as child processes and one unpacked without its exec bit
    answers exit 127 where the program should be.

    **Symlinks, for the packages that cannot be delivered without them.**
    The SDK package is unpacked with *symlinks* false and the rule above
    holds for it unchanged. A build environment package is a third
    party's source world and toolchain — the compiler driver reached
    through ``arm-zephyr-eabi-cc``, a source tree that links its own
    subdirectories — and refusing links there would refuse the package
    outright. They are then created, and only these: a **relative**
    target whose lexical resolution against the link's own directory
    stays inside the tree. An absolute target and one that climbs out are
    refused exactly as a ``..`` path component is, and for the same
    reason. Hardlinks and device nodes stay refused either way — a
    hardlink names an inode rather than a path, so nothing about it can
    be checked lexically.

    That check is lexical, and it is only sound while a member's path on
    disk means what the archive's path says: an archive that first links
    ``a`` to ``.`` and then writes ``a/b`` has the kernel resolve ``a``
    and land one level higher than the name suggests, and a chain of such
    links walks out of the tree with every member passing a lexical test.
    So **no entry is ever placed under a link**: every directory in a
    member's path is created here, remembered, and required to be one of
    those — a name that is already a link is refused rather than followed.
    With the two rules together the lexical path and the real path are the
    same path, and containment composes.
    """
    into.mkdir(parents=True, exist_ok=True)
    written = 0
    # The directories this extraction made itself, as archive paths. The
    # root is in it from the start; nothing else gets in without being
    # created as a directory here.
    made: set[str] = {""}
    try:
        with archive.open("rb") as raw, tarfile.open(fileobj=raw, mode="r|") as tar:
            for member in tar:
                name = _safe_member_name(member.name, what=what)
                target = into / name
                if member.isdir():
                    _make_directory(into, name, made=made, what=what)
                    continue
                _make_directory(into, posixpath.dirname(name), made=made, what=what)
                if symlinks and member.issym():
                    target.symlink_to(_safe_link_target(member, name, what=what))
                    continue
                if not member.isfile():
                    raise BuildError(
                        f'The {what} package carries "{member.name}", which is not a regular '
                        "file: a symlink, hardlink or device node is a way out of the tree.",
                        hint=f"a {what} package holds regular files and directories only",
                    )
                source = tar.extractfile(member)
                if source is None:  # pragma: no cover - isfile() was true a line ago
                    raise BuildError(f'The {what} entry "{member.name}" carries no data.', hint="")
                with target.open("wb") as handle:
                    while block := source.read(_BLOCK):
                        written += len(block)
                        if written > quota_bytes:
                            raise BuildError(
                                f"The {what} package unpacks to more than {quota_bytes} bytes.",
                                hint="the archive is corrupt or hostile",
                            )
                        handle.write(block)
                executable = bool(member.mode & 0o100)
                target.chmod(0o700 if executable else 0o600)
    except tarfile.TarError as error:
        raise BuildError(
            f"The {what} package at {archive} is not a readable tar ({error}).",
            hint="the archive is corrupt — re-fetch it or point at another source",
        ) from error
    except OSError as error:
        # A member the tar reads back fine but the filesystem refuses to
        # create — a component over NAME_MAX (ENAMETOOLONG), a path too
        # deep, no space — is not a `TarError` and would otherwise leave
        # this function as a bare `OSError`. It still falls safely, before
        # any container starts, but a typed refusal is the difference
        # between a fix ("the archive is hostile") and a traceback. The
        # `BuildError`s raised above (unsafe path, quota) are not `OSError`
        # and pass through this arm untouched.
        raise BuildError(
            f"The {what} package at {archive} holds an entry this filesystem cannot "
            f"unpack ({error}).",
            hint=f"a {what} entry names a path the filesystem rejects — a segment over "
            "NAME_MAX, or a tree too deep; the archive is corrupt or hostile",
        ) from error


def _safe_member_name(name: str, *, what: str = SDK_PACKAGE_NAME) -> str:
    """A tar member's path, or a refusal. Never normalized — refused.

    ``..`` and absolute paths are the escape, and rewriting ``./x`` to
    ``x`` would accept a tree whose entries a stricter reader then
    refuses. The kernel's own ``\\x00`` and the Windows separator are out
    too.
    """
    cleaned = name.rstrip("/")
    usable = (
        cleaned
        and "\\" not in cleaned
        and "\x00" not in cleaned
        and not cleaned.startswith("/")
        and all(part not in ("", ".", "..") for part in cleaned.split("/"))
    )
    if not usable:
        raise BuildError(
            f"The {what} package carries an unsafe path {name!r}.",
            hint=(
                f"a {what} entry is a relative path with forward slashes and no empty, . or .. "
                "segment — a traversal is refused, never normalized"
            ),
        )
    return cleaned


def _make_directory(into: Path, relative: str, *, made: set[str], what: str) -> None:
    """*relative* below *into*, created component by component, never followed.

    The one guarantee this gives the caller is that every component of
    the path is a real directory this extraction created — so a member's
    path on disk is the path the archive named, and a link the archive
    placed earlier cannot have moved the ground under it.
    """
    current = ""
    for part in relative.split("/") if relative else []:
        current = f"{current}/{part}" if current else part
        if current in made:
            continue
        path = into / current
        if path.is_symlink():
            raise BuildError(
                f'The {what} package puts entries under "{current}", which it made a link.',
                hint="a package writes into directories it creates as directories — "
                "the archive is corrupt or hostile",
            )
        path.mkdir(exist_ok=True)
        made.add(current)


def _safe_link_target(member: tarfile.TarInfo, name: str, *, what: str) -> str:
    """A symlink member's target, verbatim, once it is known to stay inside.

    Lexical and not :meth:`~pathlib.Path.resolve`: the tree is being
    written as this runs, so a target may name something that does not
    exist yet, and resolving would answer for a filesystem state that is
    not the final one. Lexical containment is enough because every link
    in the tree passes this check and no member is ever placed under a
    link (see :func:`_safe_extract`), so the real path of every link is
    the path the archive named and containment composes.
    """
    target = member.linkname
    # Segment-wise, not by string prefix: a file really called "..data"
    # (Kubernetes writes them, and so do a few build systems) is an
    # ordinary name and only "..", or a path that starts with it, climbs.
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    escapes = target.startswith("/") or resolved == ".." or resolved.startswith("../")
    if escapes or "\x00" in target:
        raise BuildError(
            f'The {what} package links "{member.name}" to {target!r}, which is outside '
            "the package.",
            hint=f"a {what} entry may link to a relative path inside its own tree and to "
            "nothing else — the archive is corrupt or hostile",
        )
    return target


class SdkUnavailable(BuildError):
    """The SDK package this context pins is not in any configured source.

    A typed refusal rather than a message to match on: the same
    condition is a command line printing a fix and a build server
    answering a structured frame over a socket, and neither should have
    to recognize it by its wording. The pin and the directories that
    were searched travel on the exception for the same reason — a
    caller that has to put them in a structured frame should not be
    parsing them back out of a sentence.
    """

    def __init__(
        self,
        message: str,
        *,
        hint: str = "",
        version: str = "",
        sha256: str = "",
        searched: Sequence[str] = (),
    ) -> None:
        super().__init__(message, hint=hint)
        self.version = version
        self.sha256 = sha256
        self.searched = tuple(searched)


def _package_unavailable(
    name: str, version: str, sha256: str, searched: Sequence[str], problem: str
) -> SdkUnavailable:
    """The pin names bytes this host cannot get.

    Not retryable in spirit. The local tier fetches nothing, so the same
    command a second later searches the same directories and finds the
    same nothing; and where a registry was asked, it answered with an
    index that does not carry these bytes, which a retry does not change
    either. What changes the answer is the package being put where this
    side looks, or the pin naming what is actually published.
    """
    listed = ", ".join(searched) or "none"
    return SdkUnavailable(
        f"MCUHome cannot supply the package this context pins ({problem}).",
        version=version,
        sha256=sha256,
        searched=tuple(searched),
        hint=(
            f"the bytes are taken from configured source directories and from the "
            f"package registry, never from the url in the context — add {name} "
            f"{version} (sha256 {sha256}) to one of: {listed}"
        ),
    )
