# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Resolving a version constraint to the one version that satisfies it.

A device configuration pins the SDK as a **constraint** — a range of
acceptable versions — which something must resolve to a single exact
version at context-creation time (ADR 0018 decision 3). This is that
step. The constraint grammar is fixed: constraints are
**PEP 440**, resolved with :class:`packaging.specifiers.SpecifierSet`,
because ``packaging`` is already a dependency and PEP 440 is the one
version grammar the Python ecosystem already agrees on — a caret/tilde
dialect of our own would be a second thing to specify, implement and get
wrong. (This is *not* ADR 0013, which is binary-blob policy and per-device
*Zephyr* pinning; the SDK-constraint grammar is recorded in ADR 0018's
PEP 440 amendment.)

**Resolution picks; it does not follow.** The input is a set of versions
that already exist somewhere the caller can name — the keys of the static
``index.json`` a source directory carries
(``scripts/build_sdk_archive.py``), or of the index a registry mirror
served and the project's trust anchor accepted
(:mod:`mcuhome.workbench.packageregistry`). Operator directories are
always asked first, so a machine that already has the package resolves
without a network at all; and a package's ``url`` stays a hint that no
backend ever follows either way — a location cannot make bytes correct,
and the sha256 is what decides which bytes are the right ones.

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

Pre-release rule (stated so a reader need not reverse-engineer
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

**The workbench's own default is the one place that overrides it**
(:func:`sdk_constraint`). ``DEFAULT_SDK_CONSTRAINT`` is not a range
somebody chose; it names MCUHome's own SDK line for the minor this
workbench was released alongside, and during 0.1 everything MCUHome
publishes in it is a ``.devN`` release. Applying the stable-constraint
rule there would resolve to nothing at all — not a pin, an outage — so
that resolution passes ``prereleases=True`` and takes the newest release
of the minor, dev included. The minor bound does the work it was chosen
for either way: 0.2 is refused. A constraint a **user** states, in a
device's ``sources.sdk`` or on a command line, keeps the rule above
untouched; somebody who wants a dev version says so, exactly as the
pre-release rule above asks.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from mcuhome.model.buildenvironment import (
    LOCK_FILE,
    TOOLS_SOURCE,
    WORKSPACE_SOURCE,
    EnvironmentLock,
    family_of,
    parse_lock,
)
from mcuhome.model.context import EnvironmentPin, PackagePin
from mcuhome.model.errors import BuildError
from mcuhome.model.imageref import parse_reference
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

__all__ = [
    "DEFAULT_SDK_CONSTRAINT",
    "INDEX_FILE",
    "SDK_ANY",
    "SDK_PACKAGE_NAME",
    "PackageReference",
    "ResolvedPackage",
    "SdkResolution",
    "concrete_package",
    "environment_lock",
    "package_reference",
    "resolve_environment",
    "resolve_from_entries",
    "resolve_from_index",
    "resolve_sdk",
    "resolve_sdk_pin",
    "resolve_version",
    "sdk_constraint",
]

# The index file and package name are shared vocabulary — a backend
# re-reads the same directory to fetch the bytes, a duty the legacy
# container invocation (retired at the switchover) assigns to backends,
# and may not import this module to know the names, so both live in the model
# and are re-exported here under the names this module always offered.
from mcuhome.model.sdkindex import DEFAULT_SDK, INDEX_FILE, SDK_PACKAGE_NAME  # noqa: E402

#: The source the SDK is published under inside a registry — the first
#: component of the reference every device carries by default.
SDK_SOURCE = DEFAULT_SDK.split("/")[0]

from mcuhome.workbench.packageregistry import (  # noqa: E402
    OFFICIAL_BASE_DOMAIN,
    PackageRegistry,
    PackageRegistryError,
    RegistrySource,
    VerifiedIndex,
    opened,
    pin_entry,
    resolve_entry,
)

#: The SDK constraint a build resolves with when the caller states none:
#: "the newest the configured sources offer". A device configuration can
#: pin the SDK as a PEP 440 constraint (ADR 0018), but a plain ``mcuhome
#: build`` has no such intent — it takes whatever SDK package the
#: ``--sdk-sources`` directories hold, exactly as the empty
#: :class:`~packaging.specifiers.SpecifierSet` matches every version.
SDK_ANY = ""

#: Which SDK a device that names none is built with: the newest release
#: of the minor this workbench was released alongside.
#:
#: The workbench follows the SDK at release time. It can guarantee it
#: works with the SDK version published when it was released and with
#: older ones; it cannot guarantee anything about SDK versions that did
#: not exist yet, and a default of "the newest there is" would make every
#: build a bet on that. Pinning the *minor* keeps patch releases — fixes
#: — flowing in automatically while a feature release waits for a
#: workbench that knows about it. Maintained per workbench release.
DEFAULT_SDK_CONSTRAINT = "==0.1.*"


@dataclass(frozen=True)
class PackageReference:
    """A ``sources.*`` reference, taken apart once.

    Every one of them is spelled the same way —
    ``[base-domain/]<source>/<package>[:version][@sha256:…]`` — and every
    part of it is read by somebody: the base domain selects the registry
    and its trust anchor, the source is the shelf inside that registry,
    the package name is the index key, and the two optional halves are
    what a device pinned itself to.

    It exists because those parts used to be read in three places with
    three partial parsers, and the one that resolved the version dropped
    the base domain — which meant a device pointing at a foreign registry
    resolved its constraint correctly and then looked the answer up
    somewhere else.
    """

    #: The registry's base domain — the official one where the reference
    #: named none.
    base_domain: str
    #: The source within the registry (``sdk``, ``build-workspace``, …).
    source: str
    #: The package name, architecture suffix and all.
    name: str
    #: The version the reference stated, or ``""``.
    version: str = ""
    #: The content hash the reference stated, or ``""``.
    sha256: str = ""

    @property
    def pinned(self) -> bool:
        """Does this reference decide everything on its own?

        A reference stating both a version and a hash needs no index at
        all: it *is* the pin. That is the offline case — an operator who
        has the bytes and wants them, with nothing to look up.
        """
        return bool(self.version and self.sha256)


def package_reference(reference: str, *, what: str = "package") -> PackageReference:
    """Take a ``sources.*`` reference apart, or refuse in plain language.

    The path is split at its first component: that is the source, and
    what follows is the package. A reference naming only a package —
    without a source — cannot be resolved, because a registry has no
    single shelf and no default one.
    """
    parsed = parse_reference(reference, default_registry=OFFICIAL_BASE_DOMAIN, what=what)
    source, separator, name = parsed.path.partition("/")
    if not separator or not name or "/" in name:
        raise BuildError(
            f'"{reference}" does not name a {what} MCUHome can resolve.',
            hint=(
                "the form is <source>/<package>, optionally with a registry in "
                "front, a :version and an @sha256: — for example "
                "sdk/mcuhome-sdk:0.1.10"
            ),
        )
    digest = parsed.digest or ""
    return PackageReference(
        base_domain=parsed.registry,
        source=source,
        name=name,
        version=parsed.tag or "",
        sha256=digest.removeprefix("sha256:"),
    )


def sdk_constraint(reference: str = "") -> tuple[str, bool | None]:
    """How a device's ``sources.sdk`` reference resolves: constraint, and pre-releases.

    Naming a version is a device *pinning* itself and is honoured
    exactly: ``:0.1.9`` resolves under ``==0.1.9`` — a **stated**
    constraint, so the pre-release rule applies to it unchanged and a dev
    version satisfies it only if the pin itself names one. Naming none —
    the default, and what a device carries unless somebody asks otherwise
    — resolves under :data:`DEFAULT_SDK_CONSTRAINT`, so a device is not
    frozen onto whatever version happened to be current on the day it was
    created.

    **The default admits pre-releases**, and the second half of the
    answer is that decision. The default names MCUHome's own SDK line
    rather than a range somebody chose, and everything MCUHome publishes
    in 0.1 is a ``.devN`` release; a default that applied the
    stable-constraint rule to it would resolve to nothing at all, which
    is not a pin, it is an outage. Whatever MCUHome publishes within the
    pinned minor is acceptable and the newest wins, dev included — and
    the minor bound still holds, so a 0.2 release is refused exactly as a
    stable constraint would refuse it.

    The reference is read by :func:`package_reference`, which is the
    reader everything else about it goes through as well — including the
    base domain this function used to drop on the floor.
    """
    if not reference:
        return DEFAULT_SDK_CONSTRAINT, True
    version = package_reference(reference, what="SDK package").version
    if version:
        return f"=={version}", None
    return DEFAULT_SDK_CONSTRAINT, True


def resolve_version(
    constraint: str,
    available: Iterable[str],
    *,
    prereleases: bool | None = None,
    name: str = "package",
) -> str:
    """The single highest version in *available* satisfying *constraint*.

    *constraint* is PEP 440; *available* is a set of version
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
    # None as "admit pre-releases", the opposite of what the pre-release
    # rule above wants, so the
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
    platform: str | None = None,
) -> ResolvedPackage:
    """Resolve *constraint* against a static package *index* for *name*.

    *index* is the ``{"packages": {<name>: {<version>: …}}}`` document a
    source directory or a registry source carries. The available versions
    are that map's keys; :func:`resolve_version` picks the winner and
    this answers with the package that version *is*.

    **A meta entry is followed, not returned.** An index published across
    architectures carries one name standing for a set of concrete
    packages — no ``file``, no ``size``, a ``meta`` map and a hash over
    it. Asking for that name answers with this host's concrete package,
    after the hash has been recomputed from the members it points at, so
    resolving the family really does pin one package's bytes. *platform*
    overrides which host that is; it is only consulted where an answer
    depends on it.

    Raises :class:`~mcuhome.model.errors.BuildError` when the index does
    not describe *name*, when its selected entry is malformed or does not
    describe what it points at, when nothing is published for this
    platform, or for any reason :func:`resolve_version` refuses.
    """
    packages = index.get("packages") if isinstance(index, dict) else None
    if not isinstance(packages, dict):
        packages = {}
    return resolve_from_entries(
        {
            str(package): versions
            for package, versions in packages.items()
            if isinstance(versions, dict)
        },
        name,
        constraint,
        prereleases=prereleases,
        platform=platform,
    )


def resolve_from_entries(
    entries: Mapping[str, Mapping[str, Mapping[str, object]]],
    name: str,
    constraint: str,
    *,
    prereleases: bool | None = None,
    platform: str | None = None,
) -> ResolvedPackage:
    """:func:`resolve_from_index` over an already-merged package map.

    The registry hands its callers one map of every package in a source,
    head document and index parts together, because a source that has
    outgrown one file keeps most of its packages in the parts. This is
    the same resolution over that shape, and the reason a local directory
    and a registry source resolve by one rule rather than two.
    """
    versions = entries.get(name)
    if not isinstance(versions, dict) or not versions:
        raise BuildError(
            f'The package index lists no versions of "{name}".',
            hint=(
                "point at an index.json written by scripts/build_sdk_archive.py "
                f'that carries a "{name}" package'
            ),
        )
    version = resolve_version(constraint, versions.keys(), prereleases=prereleases, name=name)
    found = resolve_entry(entries, name, version, platform=platform)
    return ResolvedPackage(
        name=found.name,
        version=found.version,
        file=found.file,
        sha256=found.sha256,
        size=found.size,
    )


@dataclass(frozen=True)
class SdkResolution:
    """One SDK package a constraint resolved to, and where it was found.

    :attr:`stated` is what the caller asked for, verbatim — ``SDK_ANY``
    when it asked for nothing. :attr:`package` is what that selected.
    Where it came from is one of two: :attr:`source`, an operator
    directory whose index selected it, or :attr:`base`, the registry
    mirror that served the index — and
    :func:`~mcuhome.workbench.orchestrator.acquire_package` searches the
    directories again before it touches the network either way.

    It exists because a :class:`~mcuhome.model.context.SdkPin` needs more
    than the three values a pin *is*: ``context.yaml`` also records the
    original intent and a location hint, and both are derived from here
    rather than invented by whoever writes the document.
    """

    stated: str
    package: ResolvedPackage
    #: The operator directory the package was found in, or ``None`` when
    #: the registry answered.
    source: Path | None = None
    #: The mirror base the registry answered from — an https URL ending
    #: in ``/``, or a local mirror's directory path. Empty for a local
    #: source directory.
    base: str = ""

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
        """The location hint to record — empty unless a public one exists.

        ``mcuhome.package.url`` is a hint only ("never hashed", and ADR
        0019 §8 forbids any backend to follow it). A package resolved
        from a local directory has no URL worth recording: a ``file://``
        URI of the source would carry the creator's local filesystem
        layout — home directory, username — into a document that is
        uploaded to a build server and archivable there. A resolution
        that came from a registry over https does have one, and records
        it; a local mirror is a local directory again and does not.
        """
        if self.base.startswith("https://"):
            return f"{self.base}{self.package.file}"
        return ""


def resolve_sdk(
    sources: Sequence[Path],
    *,
    constraint: str = SDK_ANY,
    prereleases: bool | None = None,
    registry: RegistrySource | None = None,
    source_name: str = SDK_SOURCE,
    platform: str | None = None,
) -> SdkResolution:
    """Resolve *constraint* to one SDK package: local sources, then *registry*.

    The pin has to exist *before* a context can be created — a context is
    content-addressed over the resolved ``sha256`` — so it is resolved
    from an index, never from whatever a host happens to hand over.

    **Two tiers, in this order.** The operator's own directories first:
    each carries the static :data:`INDEX_FILE`
    (``scripts/build_sdk_archive.py``), they are searched in order, and
    the first that holds a matching package wins — the same "first source
    wins" rule :func:`~mcuhome.workbench.orchestrator.acquire_package`
    then fetches the bytes by. Only when none of them holds one is
    *registry* asked, and what it answers with is an index a mirror
    served and the project's trust anchor accepted. A machine that has
    the package locally therefore never touches the network, which is
    what makes an air-gapped build a configuration rather than a mode.

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
    ``sdk.unavailable`` spirit — when neither a source nor a registry is
    configured, when none of them holds the package, and for every way a
    registry can fail to answer, so a command line can render any of it
    as a clean refusal.
    """
    if not sources and registry is None:
        raise BuildError(
            "The build needs the MCUHome SDK package, and no SDK source is configured.",
            hint=(
                "point at a directory holding one:\n"
                "    mcuhome config set build.sdk_sources <dir> --user\n"
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
                    "--sdk-sources/MCUHOME_BUILD_SDK_SOURCES"
                ),
            ) from broken
        try:
            # SDK_ANY means "the newest package this source holds, whatever
            # it is" — and during development that is a dev release. The
            # pre-release rule above (a dev version satisfies only a
            # pre-release constraint) is right for a real pin like ~=2.3
            # but wrong for "any", which is literally any: so an empty
            # constraint admits pre-releases, and a stated one keeps the
            # rule.
            resolved = resolve_from_index(
                index, SDK_PACKAGE_NAME, constraint, prereleases=_allow(constraint, prereleases)
            )
        except BuildError:
            continue
        return SdkResolution(stated=constraint, package=resolved, source=source)

    client = opened(registry)
    if client is not None:
        return _from_registry(
            client,
            constraint=constraint,
            prereleases=prereleases,
            source_name=source_name,
            platform=platform,
        )

    listed = ", ".join(searched) or "none"
    raise BuildError(
        f"No configured SDK source holds the {SDK_PACKAGE_NAME} package.",
        hint=(
            f"the pin is resolved from source directories only when no registry is "
            f"configured — put a {SDK_PACKAGE_NAME} package and its {INDEX_FILE} "
            f"(scripts/build_sdk_archive.py) in one of: {listed}"
        ),
    )


def _allow(constraint: str, prereleases: bool | None) -> bool | None:
    """The pre-release rule for one resolution.

    A caller that stated one is obeyed. Otherwise the empty specifier —
    "any version at all", which is literally any — admits pre-releases,
    and every other constraint follows the pre-release rule above for itself.
    """
    if prereleases is not None:
        return prereleases
    return True if constraint == SDK_ANY else None


def _from_registry(
    registry: PackageRegistry,
    *,
    constraint: str,
    prereleases: bool | None,
    source_name: str,
    platform: str | None,
) -> SdkResolution:
    """The same resolution against a registry's verified index.

    Same rule, other shelf: the versions come from the index a mirror
    served and the anchor accepted, and the PEP 440 arithmetic over them
    is the one the local sources went through a moment ago. What differs
    is only that the answer records the mirror it came from, so the
    location hint in a context can say something true.
    """
    index: VerifiedIndex = registry.index(source_name)
    resolved = resolve_from_entries(
        index.entries,
        SDK_PACKAGE_NAME,
        constraint,
        prereleases=_allow(constraint, prereleases),
        platform=platform,
    )
    return SdkResolution(stated=constraint, package=resolved, source=None, base=index.base)


def resolve_sdk_pin(
    sources: Sequence[Path], *, constraint: str = SDK_ANY, prereleases: bool | None = None
) -> tuple[str, str, str]:
    """The three values an SDK pin *is*: ``(constraint, version, sha256)``.

    :func:`resolve_sdk` with everything a document needs left out, for the
    callers that only want the pin — and, deliberately, returning the
    constraint the caller **stated** rather than
    :attr:`SdkResolution.intent`, so that "what was asked for" is
    recoverable here and the rendering decision belongs to whoever writes
    the document.
    """
    found = resolve_sdk(sources, constraint=constraint, prereleases=prereleases)
    return found.stated, found.package.version, found.package.sha256


# --------------------------------------------------------------------------
# The build environment's packages
# --------------------------------------------------------------------------


def environment_lock(
    *,
    version: str,
    sha256: str,
    sources: Sequence[Path],
    into: Path,
    registry: RegistrySource | None = None,
    max_bytes: int | None = None,
) -> EnvironmentLock:
    """What the SDK release *version* states about its build environment.

    Read out of the SDK package itself, and deliberately not from
    anywhere else: the archive is acquired by ``(version, sha256)``
    through the tiered, hash-checked path every package takes
    (:func:`~mcuhome.workbench.orchestrator.acquire_package`), so the
    lock a build derives its environment from comes out of bytes that
    were already verified against the pin the context is identified by.
    A sidecar beside the archive would be a second copy nobody checked.

    *into* is a scratch directory the caller owns; the SDK is small and
    the unpack costs milliseconds.

    Raises a typed refusal when the release carries no lock at all — that
    is an SDK this workbench cannot derive an environment for, and
    guessing one would pin packages nobody tested together.
    """
    from mcuhome.workbench.orchestrator import acquire_package

    acquired = acquire_package(
        kind=SDK_SOURCE,
        name=SDK_PACKAGE_NAME,
        version=version,
        sha256=sha256,
        sources=sources,
        into=Path(into),
        registry=registry,
        max_bytes=max_bytes,
    )
    path = acquired.tree / LOCK_FILE
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as missing:
        raise BuildError(
            f"The SDK package {SDK_PACKAGE_NAME} {version} carries no {LOCK_FILE}.",
            hint=(
                "the SDK release states which build environment it was built and "
                "tested with, and this one does not. Use an SDK release that does, "
                "or name the packages in the device's sources.build_workspace and "
                "sources.build_tools."
            ),
        ) from missing
    except ValueError as broken:
        raise BuildError(
            f"The {LOCK_FILE} in {SDK_PACKAGE_NAME} {version} is not readable JSON: {broken}.",
            hint="the package is damaged — remove it from the source directory and refetch it",
        ) from broken
    return parse_lock(document)


def _entries_from_directory(directory: Path) -> Mapping[str, Mapping[str, Mapping[str, object]]]:
    """The package map of a source directory's ``index.json``, or an empty one.

    A directory without an index simply is not a package source — a
    legitimate not-here. One that has an unreadable index is different:
    the caller named it on purpose, and skipping it would silently demote
    a higher-precedence source.
    """
    path = Path(directory) / INDEX_FILE
    if not path.is_file():
        return {}
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as broken:
        raise BuildError(
            f"The package source {directory} has an unreadable {INDEX_FILE}: {broken}.",
            hint=(
                "the index is what the packaging scripts write next to the archive "
                "— regenerate it, or drop the source from the configured directories"
            ),
        ) from broken
    packages = index.get("packages") if isinstance(index, dict) else None
    if not isinstance(packages, dict):
        return {}
    return {
        str(name): versions for name, versions in packages.items() if isinstance(versions, dict)
    }


def _same_registry(reference: PackageReference, sdk: PackageReference, *, what: str) -> None:
    """Both references have to name one registry, or the pin is refused.

    A registry client is built for **one** base domain — that is what a
    trust anchor is per — and a build resolves its packages through the
    one its SDK reference names. A device that pointed its environment
    packages at a second domain would have them looked up on the first,
    which is a pin resolved against a registry nobody chose. Until a
    build can hold two clients, that is a refusal rather than a silent
    substitution.
    """
    if reference.base_domain == sdk.base_domain:
        return
    raise BuildError(
        f"This device takes its SDK from {sdk.base_domain} and its {what} package "
        f"from {reference.base_domain}, and a build reads one package host.",
        hint=(
            "point sources.sdk, sources.build_workspace and sources.build_tools at "
            "the same registry, or state the version and the hash of the package "
            "outright so that nothing has to be looked up"
        ),
    )


def _pin_package(
    reference: PackageReference,
    version: str,
    *,
    sources: Sequence[Path],
    registry: RegistrySource | None,
    platform: str | None,
    what: str,
) -> PackagePin:
    """One environment package as a context pins it: name, version, hash.

    Two tiers in the order every other acquisition uses — the operator's
    own directories first, the registry's verified index second — so a
    machine that already holds the packages never opens a socket.

    A **meta** entry is kept as one rather than followed: the family name
    and its hash over every platform's package are what a context pins,
    which is what lets one context build the same firmware on hosts of
    two architectures. A concrete name is checked against this host's
    platform, because a pin naming a foreign one is a mistake worth
    catching where it is written.
    """
    if reference.sha256:
        # The reference decided both halves; nothing to look up, and
        # nothing that could disagree with it.
        return PackagePin(name=reference.name, version=version, sha256=reference.sha256)
    for directory in sources:
        entries = _entries_from_directory(Path(directory))
        if reference.name not in entries:
            continue
        try:
            found = pin_entry(entries, reference.name, version, platform=platform)
        except PackageRegistryError:
            # The directory carries the package but not this version — a
            # legitimate not-here, exactly as a directory without an index
            # is. The search goes on, the same way the SDK's does; a
            # stale mirror must not be able to stop a build that the
            # registry could have answered.
            continue
        return PackagePin(name=found.name, version=found.version, sha256=found.sha256)

    client = opened(registry)
    if client is not None:
        index = client.index(reference.source)
        found = pin_entry(index.entries, reference.name, version, platform=platform)
        return PackagePin(
            name=found.name,
            version=found.version,
            sha256=found.sha256,
            url=index.url_for(found) if index.base.startswith("https://") else "",
        )

    listed = ", ".join(str(directory) for directory in sources) or "none"
    raise BuildError(
        f"MCUHome cannot pin the {what} package {reference.name} {version}: "
        "no configured source publishes it.",
        hint=(
            f"searched: {listed}. Put the package and its {INDEX_FILE} in one of "
            "them, configure the registry, or state the hash in the device's "
            f"sources reference as {reference.name}:{version}@sha256:<hash>."
        ),
    )


def resolve_environment(
    *,
    workspace: str,
    tools: str,
    sdk_source: str,
    sdk: SdkResolution,
    sources: Sequence[Path],
    work_root: Path,
    workspace_sources: Sequence[Path] = (),
    tools_sources: Sequence[Path] = (),
    max_bytes: int | None = None,
    registry: RegistrySource | None = None,
    platform: str | None = None,
) -> EnvironmentPin:
    """The two packages a context pins its build environment to.

    *workspace* and *tools* are the device's ``sources.build_workspace``
    and ``sources.build_tools`` references, *sdk_source* is its
    ``sources.sdk`` reference and *sdk* is what that already resolved to.

    All three references have to name the same registry, because a build
    reads one package host: the base domain is what a trust anchor is per,
    and the client this resolution is handed was built for the SDK's. A
    reference that states both a version and a hash is exempt — it needs
    no host at all.

    **The versions come from the SDK, the hashes from an index.** A
    reference that names no version — the default every device carries —
    is answered by the resolved SDK release's own
    ``build-environment.lock.json``: the SDK and its environment are
    released together, so the release states which environment it was
    built and tested with, and a device that says nothing gets exactly
    that pair. A reference that *does* name a version overrides that
    derivation for its package alone; one that also names a hash decides
    the whole pin and no index is consulted at all.

    The lock cannot carry hashes — the workspace package is built from
    the SDK's own tag, and the tools package's bytes differ per platform
    — so the hash always comes from a package index: an operator
    directory's, or the one a registry mirror served and the project's
    trust anchor accepted.

    *sources* are the operator directories, and *workspace_sources* /
    *tools_sources* replace them for their own package when the
    environment packages are kept somewhere else than the SDK — they are
    two orders of magnitude larger, and a machine may well keep them on
    another disk. Empty means "the same directories the SDK comes from".
    """
    workspace_reference = package_reference(workspace, what="build workspace package")
    tools_reference = package_reference(tools, what="build tools package")
    sdk_reference = package_reference(sdk_source, what="SDK package")
    # A reference that decides its own pin needs no registry at all, so
    # only the ones that will be looked up have to agree about where.
    if not workspace_reference.pinned:
        _same_registry(workspace_reference, sdk_reference, what="build workspace")
    if not tools_reference.pinned:
        _same_registry(tools_reference, sdk_reference, what="build tools")
    lock: EnvironmentLock | None = None
    if not (workspace_reference.version and tools_reference.version):
        lock = environment_lock(
            version=sdk.package.version,
            sha256=sdk.package.sha256,
            sources=sources,
            into=Path(work_root) / "sdk-lock",
            registry=registry,
            max_bytes=max_bytes,
        )
    return EnvironmentPin(
        workspace=_pin_package(
            workspace_reference,
            workspace_reference.version or _locked(lock, workspace_reference.name),
            sources=tuple(workspace_sources) or sources,
            registry=registry,
            platform=platform,
            what="build workspace",
        ),
        tools=_pin_package(
            tools_reference,
            tools_reference.version or _locked(lock, family_of(tools_reference.name)),
            sources=tuple(tools_sources) or sources,
            registry=registry,
            platform=platform,
            what="build tools",
        ),
    )


def _locked(lock: EnvironmentLock | None, package: str) -> str:
    """The version *lock* names for *package* — with the lock guaranteed present."""
    if lock is None:  # pragma: no cover - the caller reads the lock whenever it needs one
        raise BuildError(
            f"MCUHome cannot say which version of {package} to build with.",
            hint="name it in the device's sources, or use an SDK release that states one",
        )
    return lock.version_of(package)


#: The registry sources the two environment packages are published under,
#: re-exported here beside :data:`SDK_SOURCE` so a caller has one place
#: to read the vocabulary from.
BUILD_WORKSPACE_SOURCE = WORKSPACE_SOURCE
BUILD_TOOLS_SOURCE = TOOLS_SOURCE


def concrete_package(
    pin: PackagePin,
    *,
    source: str,
    sources: Sequence[Path] = (),
    registry: RegistrySource | None = None,
    platform: str | None = None,
) -> ResolvedPackage:
    """The package **this host** has to unpack for *pin*.

    A pin that already names one platform's package is that package. A
    pin that names a family is resolved through the index — and the
    pinned hash is checked against the family's own entry **before** the
    resolution is followed, so a meta pin really does pin the bytes every
    platform gets rather than merely naming a set somebody may have
    changed since.

    Two tiers again, operator directories first, so a machine that holds
    the packages resolves without a network.
    """
    for directory in sources:
        entries = _entries_from_directory(Path(directory))
        if pin.name not in entries:
            continue
        try:
            return _concrete_from(entries, pin, platform=platform, where=str(directory))
        except PackageRegistryError:
            # "This source does not publish that version" — not here, so
            # keep looking. A source that publishes it under a DIFFERENT
            # hash is not this: that is a plain BuildError from
            # `_concrete_from` and it propagates, because same version
            # other bytes is the one thing that must never be shopped
            # around for.
            continue
    client = opened(registry)
    if client is not None:
        index = client.index(source)
        return _concrete_from(index.entries, pin, platform=platform, where=index.base)
    listed = ", ".join(str(directory) for directory in sources) or "none"
    raise BuildError(
        f"MCUHome cannot find out which {pin.name} package this machine needs.",
        hint=(
            f"the pin names a family that an index resolves per platform, and none "
            f"of the configured sources carries it (searched: {listed}). Configure "
            "the registry, or point a source directory at the packages."
        ),
    )


def _concrete_from(
    entries: Mapping[str, Mapping[str, Mapping[str, object]]],
    pin: PackagePin,
    *,
    platform: str | None,
    where: str,
) -> ResolvedPackage:
    """*pin* resolved against one index, hash-checked as it was pinned."""
    pinned = pin_entry(entries, pin.name, pin.version, platform=platform)
    if pinned.sha256 != pin.sha256:
        raise BuildError(
            f"{where} publishes {pin.name} {pin.version} with hash {pinned.sha256}, "
            f"and this build is pinned to {pin.sha256}.",
            hint=(
                "the same version names different bytes here than where the context "
                "was created. Use the source the context was pinned against, or "
                "recreate the context."
            ),
        )
    found = resolve_entry(entries, pin.name, pin.version, platform=platform)
    return ResolvedPackage(
        name=found.name,
        version=found.version,
        file=found.file,
        sha256=found.sha256,
        size=found.size,
    )
