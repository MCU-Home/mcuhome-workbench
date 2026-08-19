# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Resolving a version constraint to the one version that satisfies it.

A device configuration pins the SDK as a **constraint** — a range of
acceptable versions — which something must resolve to a single exact
version at context-creation time (ADR 0018 decision 3). This is that
step. Product-owner decision E52 fixes the grammar: constraints are
**PEP 440**, resolved with :class:`packaging.specifiers.SpecifierSet`,
because ``packaging`` is already a dependency and PEP 440 is the one
version grammar the Python ecosystem already agrees on — a caret/tilde
dialect of our own would be a second thing to specify, implement and get
wrong. (This is *not* ADR 0013, which is binary-blob policy and per-device
*Zephyr* pinning; the SDK-constraint grammar is recorded in ADR 0018's
PEP 440 amendment.)

**Local, never networked.** The input is a set of versions the caller
already has in hand — for the SDK, the keys of the static ``index.json``
a source directory carries (``scripts/build_sdk_archive.py``). Nothing
here fetches anything: resolution picks among versions that already
exist, and "where the bytes come from" is the operator's source-list
configuration, never a URL this module follows (ADR 0019 §8).

**Reusable.** :func:`resolve_version` knows nothing about the SDK — it
resolves any constraint against any set of version strings, so the same
rule serves the container coupling labels or a community registry later.
:func:`resolve_from_index` is the thin convenience that reads the
``index.json`` shape and hands back the selected package's entry, and
:func:`resolve_sdk_pin` is the one above it that walks a list of source
directories and answers with the pin a context is created from.

**Why the SDK resolver lives here and not beside a build method** (E65).
Both container-shaped methods need the same pin before a context can
exist, because ``mcuhome.package.sha256`` is a hashed identity input:
``local`` resolves it for the container it starts itself, and ``remote``
resolves it for a context it sends to a build server, which then
*re-resolves the version against its own sources and verifies the bytes
against this pin*. The version is the server's resolution key and the
hash is the byte-identity guard — same version number, other bytes, is a
typed refusal there rather than a silently different SDK. One resolver
for both is what makes the client's pin and the server's check statements
about the same rule; and it has to be in the workbench, because a
workbench must not import the compiler (ADR 0020 decision 3) and
``remote`` is a workbench-only method.

Pre-release rule (E52, stated so a reader need not reverse-engineer
``packaging``): a dev or pre-release version (``2.5.0.dev0``, ``2.5.0a1``)
satisfies a constraint **only** when the constraint is itself a
pre-release specifier (``==2.5.0.dev0``, ``>=2.5.0a1`` — anything for
which :attr:`SpecifierSet.prereleases <packaging.specifiers.SpecifierSet.prereleases>`
is true) or the caller passes ``prereleases=True``. A stable constraint
such as ``~=2.3`` never resolves to a pre-release. This is
``SpecifierSet``'s own semantics with its one surprise pinned down:
``contains(v, prereleases=None)`` admits pre-releases, so the default here
translates ``None`` to the constraint's own pre-release nature rather than
passing it through — which is exactly the rule above.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from mcuhome.model.errors import BuildError
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

__all__ = [
    "INDEX_FILE",
    "SDK_ANY",
    "SDK_PACKAGE_NAME",
    "ResolvedPackage",
    "SdkResolution",
    "resolve_from_index",
    "resolve_sdk",
    "resolve_sdk_pin",
    "resolve_version",
]

# The index file and package name are shared vocabulary — a backend
# re-reads the same directory to fetch the bytes (contract §9.1) and may
# not import this module to know the names, so both live in the model
# and are re-exported here under the names this module always offered.
from mcuhome.model.sdkindex import INDEX_FILE, SDK_PACKAGE_NAME  # noqa: E402

#: The SDK constraint a build resolves with when the caller states none:
#: "the newest the configured sources offer". A device configuration can
#: pin the SDK as a PEP 440 constraint (ADR 0018), but a plain ``mcuhome
#: build`` has no such intent — it takes whatever SDK package the
#: ``--sdk-sources`` directories hold, exactly as the empty
#: :class:`~packaging.specifiers.SpecifierSet` matches every version.
SDK_ANY = ""


def resolve_version(
    constraint: str,
    available: Iterable[str],
    *,
    prereleases: bool | None = None,
    name: str = "package",
) -> str:
    """The single highest version in *available* satisfying *constraint*.

    *constraint* is PEP 440 (E52); *available* is a set of version
    strings the caller already holds (never fetched). Returns the winning
    version as the exact string it appeared as in *available*, so a caller
    can map it straight back to whatever it keyed that version by (an
    ``index.json`` entry, say).

    *prereleases* selects the pre-release rule stated in the module
    docstring: ``None`` (the default) follows the constraint's own nature
    — a pre-release satisfies only a pre-release specifier; ``True`` admits
    pre-releases regardless; ``False`` forbids them even for a pre-release
    specifier. *name* names the thing being resolved in a refusal.

    Raises :class:`~mcuhome.model.errors.BuildError` — a typed refusal
    naming the constraint and what was available — when the constraint is
    not PEP 440, when *available* holds a version that is not, or when
    nothing satisfies it (an empty set included).
    """
    try:
        specifier = SpecifierSet(constraint)
    except InvalidSpecifier as error:
        raise BuildError(
            f'"{constraint}" is not a PEP 440 version constraint.',
            hint=(
                "constraints are PEP 440: a compatible-release "
                '"~=2.3", a range ">=2.3.6,<3", or an exact pin "==2.3.6". '
                "npm-style carets and tildes are not PEP 440."
            ),
        ) from error

    # None means "follow the constraint" — but SpecifierSet.contains reads
    # None as "admit pre-releases", the opposite of what E52 wants, so the
    # default is turned into the constraint's own pre-release nature here.
    allow = bool(specifier.prereleases) if prereleases is None else prereleases

    parsed: list[tuple[Version, str]] = []
    for raw in available:
        try:
            parsed.append((Version(raw), raw))
        except InvalidVersion as error:
            raise BuildError(
                f'{name} has an available version "{raw}" that is not a PEP 440 version.',
                hint="the version index is malformed — every version must be PEP 440",
            ) from error

    matching = [
        (version, raw) for version, raw in parsed if specifier.contains(version, prereleases=allow)
    ]
    if not matching:
        offered = ", ".join(raw for _, raw in sorted(parsed)) or "none"
        raise BuildError(
            f'No available version of {name} satisfies "{constraint}".',
            hint=f"available: {offered}. Loosen the constraint or add the version.",
        )
    winner = max(matching, key=lambda item: item[0])
    return winner[1]


@dataclass(frozen=True)
class ResolvedPackage:
    """One package the index resolved a constraint to.

    ``version``/``file``/``sha256``/``size`` are the selected version and
    the ``index.json`` entry beside it. There is deliberately no URL: the
    index carries none (``scripts/build_sdk_archive.py``) and a caller
    resolves the location from its own source list, so a
    :class:`~mcuhome.model.context.SdkPin` is built from this plus the
    caller's chosen ``url`` hint.
    """

    name: str
    version: str
    file: str
    sha256: str
    size: int


def resolve_from_index(
    index: object,
    name: str,
    constraint: str,
    *,
    prereleases: bool | None = None,
) -> ResolvedPackage:
    """Resolve *constraint* against a static package *index* for *name*.

    *index* is the ``{"packages": {<name>: {<version>: {"file", "sha256",
    "size"}}}}`` document ``scripts/build_sdk_archive.py`` writes. The
    available versions are that map's keys; :func:`resolve_version` picks
    the winner and this returns its entry as a :class:`ResolvedPackage`.

    Raises :class:`~mcuhome.model.errors.BuildError` when the index does
    not describe *name*, when its selected entry is malformed, or for any
    reason :func:`resolve_version` refuses.
    """
    packages = index.get("packages") if isinstance(index, dict) else None
    entries = packages.get(name) if isinstance(packages, dict) else None
    if not isinstance(entries, dict) or not entries:
        raise BuildError(
            f'The package index lists no versions of "{name}".',
            hint=(
                "point at an index.json written by scripts/build_sdk_archive.py "
                f'that carries a "{name}" package'
            ),
        )
    version = resolve_version(constraint, entries.keys(), prereleases=prereleases, name=name)
    entry = entries[version]
    try:
        return ResolvedPackage(
            name=name,
            version=version,
            file=str(entry["file"]),
            sha256=str(entry["sha256"]),
            size=int(entry["size"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise BuildError(
            f"The package index entry for {name} {version} is missing something: {error}.",
            hint="each entry carries file, sha256 and size — the index is malformed",
        ) from error


@dataclass(frozen=True)
class SdkResolution:
    """One SDK package a constraint resolved to, and where it was found.

    :attr:`stated` is what the caller asked for, verbatim — ``SDK_ANY``
    when it asked for nothing. :attr:`package` is what that selected, and
    :attr:`source` is the directory whose index selected it, which is the
    directory whose bytes :func:`~mcuhome.workbench.orchestrator.acquire_sdk`
    then reads.

    It exists because a :class:`~mcuhome.model.context.SdkPin` needs more
    than the three values a pin *is*: ``context.yaml`` also records the
    original intent and a location hint, and both are derived from here
    rather than invented by whoever writes the document.
    """

    stated: str
    package: ResolvedPackage
    source: Path

    @property
    def intent(self) -> str:
        """The constraint to record — verbatim, empty included.

        The empty specifier is PEP 440's own way of saying "any
        version", and the document records the caller's statement, not a
        paraphrase: ``mcuhome.constraint`` is informational by contract
        ("original intent — never hashed"), and the build server accepts
        it empty for exactly that reason. Rendering an unstated
        constraint as ``==<version>`` was considered and rejected — it
        destroys the one thing the field exists to preserve, the
        difference between "any version was fine" and "exactly this one
        was demanded".
        """
        return self.stated

    @property
    def url(self) -> str:
        """The location hint to record — empty for a local source.

        ``mcuhome.package.url`` is a hint only ("never hashed", and ADR
        0019 §8 forbids any backend to follow it). A package resolved
        from a local directory has no URL worth recording: a ``file://``
        URI of the source would carry the creator's local filesystem
        layout — home directory, username — into a document that is
        uploaded to a build server and archivable there. The field stays
        empty until a resolution actually comes from a registry with a
        public location.
        """
        return ""


def resolve_sdk(sources: Sequence[Path], *, constraint: str = SDK_ANY) -> SdkResolution:
    """Resolve *constraint* to one SDK package out of *sources*.

    The pin has to exist *before* a context can be created — a context is
    content-addressed over the resolved ``sha256`` — so this resolves it
    from the static :data:`INDEX_FILE` a source directory carries
    (``scripts/build_sdk_archive.py``), never from a network. Sources are
    searched in order and the first that holds a matching package wins,
    which is the same "first source wins" rule
    :func:`~mcuhome.workbench.orchestrator.acquire_sdk` then fetches the
    bytes by.

    **Both halves of the answer are load-bearing, and differently so**
    (E65). The *version* is a resolution key: whoever fetches the package
    looks it up by that number, here or on a build server whose sources
    are its operator's, not this machine's. The *sha256* is the
    byte-identity guard: it is what the context ID hashes, and what the
    fetching party checks the bytes it found against. That is the whole
    protection against a version number meaning different bytes in two
    places — a build server that quietly used its own copy under the
    pinned version would build against a different SDK than the identity
    claims, so a mismatch there is a typed refusal rather than a fallback.

    Raises a typed :class:`~mcuhome.model.errors.BuildError` — the
    ``sdk.unavailable`` spirit — when no source is configured or none holds
    the package, so a command line can render it as a clean refusal.
    """
    if not sources:
        raise BuildError(
            "The build needs the MCUHome SDK package, and no SDK source is configured.",
            hint=(
                "point at a directory holding one:\n"
                "    mcuhome config set sdk_sources <dir> --user\n"
                "or pass --sdk-sources <dir> for a single build."
            ),
        )
    searched: list[str] = []
    for source in sources:
        source = Path(source)
        searched.append(str(source))
        index_path = source / INDEX_FILE
        if not index_path.is_file():
            # A directory without an index simply is not an SDK source —
            # a legitimate not-here, the search continues.
            continue
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as broken:
            # An index that exists and cannot be read is different: the
            # caller named this source on purpose, and skipping it would
            # silently demote a higher-precedence source — a build
            # against the wrong SDK instead of an error message.
            raise BuildError(
                f"The SDK source {source} has an unreadable {INDEX_FILE}: {broken}.",
                hint=(
                    "the index is what scripts/build_sdk_archive.py writes next to "
                    "the archive — regenerate it, or drop the source from "
                    "--sdk-sources/MCUHOME_SDK_SOURCES"
                ),
            ) from broken
        try:
            # SDK_ANY means "the newest package this source holds, whatever
            # it is" — and during development that is a dev release. The
            # E52 pre-release rule (a dev version satisfies only a
            # pre-release constraint) is right for a real pin like ~=2.3
            # but wrong for "any", which is literally any: so an empty
            # constraint admits pre-releases, and a stated one keeps the
            # rule.
            allow = True if constraint == SDK_ANY else None
            resolved = resolve_from_index(index, SDK_PACKAGE_NAME, constraint, prereleases=allow)
        except BuildError:
            continue
        return SdkResolution(stated=constraint, package=resolved, source=source)
    listed = ", ".join(searched) or "none"
    raise BuildError(
        f"No configured SDK source holds the {SDK_PACKAGE_NAME} package.",
        hint=(
            f"the pin is resolved from source directories only, never from a URL — "
            f"put a {SDK_PACKAGE_NAME} package and its {INDEX_FILE} "
            f"(scripts/build_sdk_archive.py) in one of: {listed}"
        ),
    )


def resolve_sdk_pin(sources: Sequence[Path], *, constraint: str = SDK_ANY) -> tuple[str, str, str]:
    """The three values an SDK pin *is*: ``(constraint, version, sha256)``.

    :func:`resolve_sdk` with everything a document needs left out, for the
    callers that only want the pin — and, deliberately, returning the
    constraint the caller **stated** rather than
    :attr:`SdkResolution.intent`, so that "what was asked for" is
    recoverable here and the rendering decision belongs to whoever writes
    the document.
    """
    found = resolve_sdk(sources, constraint=constraint)
    return found.stated, found.package.version, found.package.sha256
