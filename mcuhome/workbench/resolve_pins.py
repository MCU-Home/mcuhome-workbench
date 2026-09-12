# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Resolving a version constraint to the one version that satisfies it.

A device configuration pins the SDK as a **constraint** — a range of
acceptable versions — which something must resolve to a single exact
version at context-creation time. This is that
step. The constraint grammar is fixed: constraints are
**PEP 440**, resolved with :class:`packaging.specifiers.SpecifierSet`,
because ``packaging`` is already a dependency and PEP 440 is the one
version grammar the Python ecosystem already agrees on — a caret/tilde
dialect of our own would be a second thing to specify, implement and get
wrong. (This is *not* the binary-blob policy and per-device
*Zephyr* pinning that governs firmware blobs; the SDK-constraint grammar
is its own, separate PEP 440 rule.)

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

**The build environment is a chain of those resolutions, not a list.**
The SDK, the build workspace and the build tools are released on lines of
their own, and each package states a *range* of the next one in its
``meta.json`` — inside the archive for the SDK, beside the archive as
``<archive>.meta.json`` for what an index publishes. So
:func:`resolve_environment` resolves twice: the SDK release's range to the
newest published workspace version, that package's range to the newest
published tools version, pinning each exactly by name, version and hash.
Only a version whose index entry records a meta file is a candidate,
because a version that does not say what it requires cannot be resolved
*through* — and a device may override any link of it, including outside
what the link above declared, which is built and noted rather than
refused.

**Why the SDK resolver lives here and not beside one build target.**
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
workbench must not import the compiler and
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
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from mcuhome.model.buildenvironment import (
    ARCH_SEPARATOR,
    META_FILE,
    TOOLS_FAMILY,
    TOOLS_SOURCE,
    WORKSPACE_PACKAGE,
    WORKSPACE_SOURCE,
    PackageMeta,
    family_of,
    parse_meta,
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
    "SDK_STAGE",
    "TOOLS_STAGE",
    "WORKSPACE_STAGE",
    "PackageReference",
    "PackageStage",
    "ResolvedPackage",
    "SdkResolution",
    "concrete_package",
    "package_reference",
    "resolve_environment",
    "resolve_from_entries",
    "resolve_from_index",
    "resolve_sdk",
    "resolve_sdk_pin",
    "resolve_version",
    "sdk_constraint",
    "sdk_package_meta",
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
    MetaFile,
    PackageRegistry,
    PackageRegistryError,
    RegistrySource,
    ResolvedEntry,
    VerifiedIndex,
    check_meta_bytes,
    host_platform,
    opened,
    pin_entry,
    resolve_entry,
)

#: The SDK constraint a build resolves with when the caller states none:
#: "the newest the configured sources offer". A device configuration can
#: pin the SDK as a PEP 440 constraint, but a plain ``mcuhome
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
class PackageStage:
    """One link of the chain, as the workbench expects to find it.

    A ``sources.*`` reference may leave out everything but the part its
    author cares about, and these three values are what the omissions
    mean: which shelf of a registry the package is published under, which
    package it is when the reference names none, and what to call it in a
    refusal. The stage is what makes ``:~=0.2`` a complete statement.
    """

    #: How the stage is named in a message ("build workspace package").
    what: str
    #: The source within a registry the stage's packages are published under.
    source: str
    #: The package name a reference that names none is understood as.
    family: str
    #: The device file key that states this reference, for a fix line.
    key: str


#: The three stages a build resolves, in the order the chain links them.
#: They are the defaults a ``sources.*`` reference is completed with, and
#: the vocabulary a refusal names the missing piece in.
SDK_STAGE = PackageStage(what="SDK package", source=SDK_SOURCE, family=SDK_PACKAGE_NAME, key="sdk")
WORKSPACE_STAGE = PackageStage(
    what="build workspace package",
    source=WORKSPACE_SOURCE,
    family=WORKSPACE_PACKAGE,
    key="build_workspace",
)
TOOLS_STAGE = PackageStage(
    what="build tools package",
    source=TOOLS_SOURCE,
    family=TOOLS_FAMILY,
    key="build_tools",
)

#: A content hash as every document spells it: 64 lowercase hex digits.
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class PackageReference:
    """A ``sources.*`` reference, taken apart once.

    Every one of them is spelled the same way —
    ``[registry/][source/]<package>[:<constraint>][@sha256:…]`` — and
    every part of it is read by somebody: the registry's base domain
    selects the trust anchor and the mirrors, the source is the shelf
    inside that registry, the package name is the index key, the
    constraint is the range of versions the device will accept, and a
    hash decides the bytes outright.

    **Everything but the package is optional, and so is the package.**
    What is left out is the stage's own default (:class:`PackageStage`) —
    a reference that states only ``:~=0.2`` narrows the range and says
    nothing else, exactly as ``:<tag>`` does for a container image.

    :attr:`custom` is the one derived answer that matters afterwards:
    whether this reference points somewhere other than the package the
    stage expects on the host it expects. "The device asked for another
    package" and "the device asked for nothing" resolve differently —
    only the first is honoured against a chain that says nothing about
    it, and only a device that stated *something* is ever told that it
    went outside what the stage above declared.

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
    #: The constraint to resolve with, as a PEP 440 specifier — a stated
    #: version appears here as ``==<version>``. Empty where the reference
    #: stated none and the chain decides.
    constraint: str = ""
    #: The bare version the reference stated, or ``""``. A reference that
    #: states a range states no version.
    version: str = ""
    #: The content hash the reference stated, or ``""``.
    sha256: str = ""
    #: Whether the reference points at another package, source or host
    #: than the stage's own — every device carries the stage's defaults
    #: written out, so "named" and "chosen" are not the same question.
    custom: bool = False
    #: Whether the reference named the registry rather than defaulting.
    hosted: bool = False

    @property
    def stated(self) -> bool:
        """Did this reference decide anything the stage would not have?"""
        return bool(self.custom or self.constraint or self.sha256)

    @property
    def pinned(self) -> bool:
        """Does this reference decide everything on its own?

        A reference stating both a version and a hash needs no index at
        all: it *is* the pin. That is the offline case — an operator who
        has the bytes and wants them, with nothing to look up.
        """
        return bool(self.version and self.sha256)


def package_reference(
    reference: str, *, what: str = "", stage: PackageStage | None = None
) -> PackageReference:
    """Take a ``sources.*`` reference apart, or refuse in plain language.

    The grammar is docker's, with the tag position carrying a **PEP 440
    constraint** instead of a tag: ``~=1.8.3`` and ``>=1.7,<2`` are as
    valid there as ``0.1.9``, and a bare version is read as the exact pin
    it obviously is (``:0.1.9`` resolves under ``==0.1.9``). Everything
    before the package is optional. A path of two components is
    ``<source>/<package>``; a path of one is the package on the stage's
    own shelf; none at all — a reference that opens with ``:`` or ``@`` —
    is the stage's own package, narrowed or pinned.

    *stage* supplies those defaults. Without one, a reference must name
    its source and its package outright, because nothing else can say
    which shelf of a registry to look on.
    """
    what = what or (stage.what if stage is not None else "package")
    stated = reference.strip() if isinstance(reference, str) else ""
    if not stated:
        raise BuildError(
            f"The {what} reference is empty.",
            hint=(
                "name it as [registry/][source/]<package>[:<constraint>][@sha256:…] — "
                "everything but the package is optional, and so is the package"
            ),
        )

    head, at_sign, digest = stated.partition("@")
    sha256 = ""
    if at_sign:
        if not digest.startswith("sha256:") or _SHA256_HEX.fullmatch(digest[7:]) is None:
            raise BuildError(
                f'The {what} "{reference}" names a hash that is not one: "{digest}".',
                hint="a hash is sha256: followed by 64 lowercase hex digits",
            )
        sha256 = digest[7:]

    # The constraint sits after the last path component's colon, which is
    # the one place a colon cannot belong to a registry's port.
    last = head.rpartition("/")[2]
    marker = last.find(":")
    path, text = head, ""
    if marker >= 0:
        text = last[marker + 1 :]
        path = head[: len(head) - len(last)] + last[:marker]
    constraint, version = _stated_constraint(text, what=what, reference=reference)

    if not path:
        if stage is None:
            raise BuildError(
                f'"{reference}" does not name a {what} MCUHome can resolve.',
                hint=(
                    "the form is <source>/<package>, optionally with a registry in "
                    "front, a :constraint and an @sha256: — for example "
                    "sdk/mcuhome-sdk:0.1.10"
                ),
            )
        return PackageReference(
            base_domain=OFFICIAL_BASE_DOMAIN,
            source=stage.source,
            name=stage.family,
            constraint=constraint,
            version=version,
            sha256=sha256,
        )

    parsed = parse_reference(path, default_registry=OFFICIAL_BASE_DOMAIN, what=what)
    components = parsed.path.split("/")
    if len(components) == 1 and stage is not None:
        source, name = stage.source, components[0]
    elif len(components) == 2:
        source, name = components
    else:
        raise BuildError(
            f'"{reference}" does not name a {what} MCUHome can resolve.',
            hint=(
                "the form is [registry/][source/]<package>, optionally with a "
                ":constraint and an @sha256: — for example sdk/mcuhome-sdk:0.1.10"
            ),
        )
    hosted = path.startswith(f"{parsed.registry}/")
    return PackageReference(
        base_domain=parsed.registry,
        source=source,
        name=name,
        constraint=constraint,
        version=version,
        sha256=sha256,
        custom=stage is None
        or (name, source) != (stage.family, stage.source)
        or parsed.registry != OFFICIAL_BASE_DOMAIN,
        hosted=hosted,
    )


def _stated_constraint(text: str, *, what: str, reference: str) -> tuple[str, str]:
    """What a reference states after its colon: the constraint, and the version.

    Two spellings, told apart by parsing rather than by looking for an
    operator: a bare **version** is the exact pin a device that froze
    itself means (``0.1.9`` → ``==0.1.9``, and the version is answered
    beside it so a fully pinned reference needs no index), and anything
    else is a **PEP 440 specifier** read verbatim. Nothing at all — an
    absent colon, or one with nothing behind it — is the empty
    constraint, which is this function's way of saying "the chain
    decides".
    """
    if not text:
        return "", ""
    try:
        Version(text)
    except InvalidVersion:
        pass
    else:
        return f"=={text}", text
    try:
        SpecifierSet(text)
    except InvalidSpecifier as error:
        raise BuildError(
            f'The {what} "{reference}" states "{text}", which is neither a version '
            "nor a version constraint.",
            hint=(
                "state a version (0.1.9) to pin one exactly, or a PEP 440 "
                'constraint — a compatible release "~=0.1.0", a range '
                '">=0.1,<0.2". npm-style carets and tildes are not PEP 440.'
            ),
        ) from error
    return text, ""


def sdk_constraint(reference: str = "") -> tuple[str, bool | None]:
    """How a device's ``sources.sdk`` reference resolves: constraint, and pre-releases.

    Naming a version is a device *pinning* itself and is honoured
    exactly: ``:0.1.9`` resolves under ``==0.1.9`` — a **stated**
    constraint, so the pre-release rule applies to it unchanged and a dev
    version satisfies it only if the pin itself names one. A range is
    honoured the same way and for the same reason. Naming none — the
    default, and what a device carries unless somebody asks otherwise —
    resolves under :data:`DEFAULT_SDK_CONSTRAINT`, so a device is not
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
    constraint = package_reference(reference, stage=SDK_STAGE).constraint
    if constraint:
        return constraint, None
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
    :func:`~mcuhome.workbench.packagefetch.acquire_package` searches the
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

        ``mcuhome.package.url`` is a hint only ("never hashed", and no
        backend may follow it). A package resolved
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
    wins" rule :func:`~mcuhome.workbench.packagefetch.acquire_package`
    then fetches the bytes by. Only when none of them holds one is
    *registry* asked, and what it answers with is an index a mirror
    served and the project's trust anchor accepted. A machine that has
    the package locally therefore never touches the network, which is
    what makes an air-gapped build a configuration rather than a mode.

    **Both halves of the answer are load-bearing, and differently so.**
    The *version* is a resolution key: whoever fetches the package
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


def sdk_package_meta(
    *,
    version: str,
    sha256: str,
    sources: Sequence[Path],
    into: Path,
    registry: RegistrySource | None = None,
    max_bytes: int | None = None,
) -> PackageMeta:
    """What the SDK release *version* says it needs below it.

    The first link of the chain, read out of the SDK package itself and
    deliberately not from anywhere else: the archive is acquired by
    ``(version, sha256)`` through the tiered, hash-checked path every
    package takes
    (:func:`~mcuhome.workbench.packagefetch.acquire_package`), so what a
    build derives its environment from comes out of bytes that were
    already verified against the pin the context is identified by. The
    sidecar beside the archive carries the same document, and for the SDK
    it is not the one read: a copy nobody checked would decide which
    build workspace a build resolves to.

    *into* is a scratch directory the caller owns; the SDK is small and
    the unpack costs milliseconds.

    Raises a typed refusal when the release carries no meta file at all —
    that is an SDK this workbench cannot derive an environment for, and
    guessing one would pin packages nobody tested together.
    """
    from mcuhome.workbench.packagefetch import acquire_package

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
    path = acquired.tree / META_FILE
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as missing:
        raise BuildError(
            f"The SDK package {SDK_PACKAGE_NAME} {version} carries no {META_FILE}.",
            hint=(
                "the SDK release states which build environment it was built and "
                "tested with, and this one does not. Use an SDK release that does, "
                "or name the packages in the device's sources.build_workspace and "
                "sources.build_tools."
            ),
        ) from missing
    except ValueError as broken:
        raise BuildError(
            f"The {META_FILE} in {SDK_PACKAGE_NAME} {version} is not readable JSON: {broken}.",
            hint="the package is damaged — remove it from the source directory and refetch it",
        ) from broken
    return parse_meta(document, what=f"The {META_FILE} of {SDK_PACKAGE_NAME} {version}")


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


# --------------------------------------------------------------------------
# Where a stage can be resolved from
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Shelf:
    """One place a stage may be resolved from: a directory, or a mirror.

    The two are deliberately the same shape. An operator's directory and
    a registry source both answer "which versions are there" out of an
    ``index.json`` and "what does this one require" out of the meta file
    recorded beside the archive; the difference is where the bytes come
    from and whether a signature was checked, and both of those are
    settled before this type exists.
    """

    #: What to call this place in a refusal — a path, or a mirror base.
    label: str
    entries: Mapping[str, Mapping[str, Mapping[str, object]]]
    directory: Path | None = None
    client: PackageRegistry | None = None
    index: VerifiedIndex | None = None

    def meta_document(self, meta_file: MetaFile, *, what: str) -> object:
        """The meta file beside an archive, read and checked as the index records it."""
        if self.client is not None and self.index is not None:
            payload = self.client.fetch_meta(self.index, meta_file)
        else:
            path = Path(self.directory or ".") / meta_file.file
            try:
                payload = path.read_bytes()
            except OSError as missing:
                raise BuildError(
                    f"{self.label} lists a meta file for {what} and does not carry it.",
                    hint=(
                        f"the index records {meta_file.file} beside the archive and the "
                        "file is not there — the copy is incomplete. Synchronise the "
                        "directory again."
                    ),
                ) from missing
            check_meta_bytes(payload, meta_file, where=self.label)
        try:
            return json.loads(payload)
        except ValueError as broken:
            raise BuildError(
                f"The meta file of {what} on {self.label} is not readable JSON: {broken}.",
                hint="the package source is damaged — synchronise it again, or use another",
            ) from broken

    def url_for(self, entry: object) -> str:
        """The location hint to record — an https mirror's, or nothing.

        A package found in a local directory has no URL worth recording:
        a ``file://`` of it would carry this machine's filesystem layout
        into a document that may be uploaded to a build server.
        """
        if self.index is None or not self.index.base.startswith("https://"):
            return ""
        return self.index.url_for(entry)  # type: ignore[arg-type]


def _shelves(
    *,
    sources: Sequence[Path],
    registry: RegistrySource | None,
    source_name: str,
) -> Iterable[_Shelf]:
    """The operator's directories, in order, and then the registry.

    Lazily, and that is the point: a machine that holds the packages
    never opens a socket, because the registry's index is only asked for
    when the directories have all answered "not here".
    """
    for directory in sources:
        entries = _entries_from_directory(Path(directory))
        if not entries:
            continue
        yield _Shelf(label=str(directory), entries=entries, directory=Path(directory))
    client = opened(registry)
    if client is not None:
        index = client.index(source_name)
        yield _Shelf(label=index.base, entries=index.entries, client=client, index=index)


# --------------------------------------------------------------------------
# What a stage demands, and what satisfies it
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Requirement:
    """One entry of a meta file's ``requires``: where, what, which versions."""

    host: str
    name: str
    specifier: str

    def describes(self, *, name: str, base_domain: str, default_host: str) -> bool:
        """Whether this requirement is about that package on that host.

        The name is compared as a **family**: a requirement is stated
        about ``mcuhome-build-tools`` and a device may name this
        platform's ``mcuhome-build-tools_linux-amd64`` of it, which is
        the same package with the coordinate written out — so the
        declared range still narrows it, and the note stays quiet.

        A key without a host prefix means the host the *requiring*
        package itself came from — so the same package name taken from
        somewhere else is not what was required, and is told so.
        """
        if family_of(self.name) != family_of(name):
            return False
        return (self.host or default_host) == base_domain

    def described(self) -> str:
        """How this requirement reads in a note."""
        return f"{self.host}/{self.name}" if self.host else self.name


def _requirements(meta: PackageMeta | None) -> tuple[_Requirement, ...]:
    """A meta file's ``requires``, with the optional host prefix split off.

    The key is ``[<host>/]<package name>``: a package may require
    something from another host, and where it names none the host is the
    one the requiring package itself came from.
    """
    if meta is None:
        return ()
    found = []
    for key, specifier in meta.requires.items():
        host, slash, name = key.rpartition("/")
        found.append(_Requirement(host=host if slash else "", name=name, specifier=specifier))
    return tuple(found)


@dataclass(frozen=True)
class _Demand:
    """One stage, as the build is about to resolve it.

    :attr:`constraint` is what will actually be resolved — the device's
    word where it stated one, the chain's where it did not.
    :attr:`required` is what the stage above declared, kept beside it so
    that the two can be compared once the version is known: an override
    outside the declared range is built, and said out loud.
    """

    reference: PackageReference
    stage: PackageStage
    name: str
    base_domain: str
    constraint: str
    #: What the stage above declared about this stage, or ``None``.
    required: _Requirement | None
    #: The package that declared it, for the note ("mcuhome-sdk 0.1.10").
    declared_by: str
    #: The base domain that package itself came from — what an unprefixed
    #: ``requires`` key means.
    declared_host: str = OFFICIAL_BASE_DOMAIN
    #: Whether the stage above said anything at all — ``False`` where its
    #: own reference was pinned outright and nothing was ever read.
    declared: bool = False
    #: Everything the stage above requires, for a note that has to say
    #: what it asked for instead.
    requirements: tuple[_Requirement, ...] = ()

    @property
    def identity(self) -> str:
        """The package this demand is about, with its host where that differs."""
        if self.base_domain != self.declared_host:
            return f"{self.base_domain}/{self.name}"
        return self.name

    def note_for(self, pin: PackagePin) -> str | None:
        """The one line a device override outside the declared range prints.

        Never a refusal: a device that names another version, another
        package or another host is making a deliberate statement, and the
        stage above it is in no position to forbid it — it only knows what
        it was tested against. So the build goes on and the log says which
        of the two it followed, because the difference is invisible in the
        result.

        Silence is the normal case, and deliberately so: a device that
        stated nothing, or stated something the stage above declared, has
        nothing worth a line in a build log.
        """
        if not self.reference.stated or not self.declared:
            return None
        if self.required is None:
            if not self.reference.custom:
                return None
            asked = (
                ", ".join(
                    f"{requirement.described()} {requirement.specifier}"
                    for requirement in self.requirements
                )
                or "no package at all"
            )
            return (
                f"Note: the device names {self.identity} in sources.{self.stage.key}, and "
                f"{self.declared_by} requires {asked} — building with "
                f"{pin.name} {pin.version} as the device asks."
            )
        if _satisfies(self.required.specifier, pin.version):
            return None
        return (
            f"Note: the device pins {pin.name} {pin.version} in sources.{self.stage.key}, "
            f'and {self.declared_by} was built and tested with "{self.required.specifier}" '
            f"— building with the version the device names."
        )


def _satisfies(specifier: str, version: str) -> bool:
    """Whether *version* is inside *specifier* — pre-releases included.

    Pre-releases count here because the question is "is this what the
    stage above declared", not "what should be resolved": a declared
    ``~=0.1.0`` and a device pinning ``0.1.1.dev3`` are the same
    intention, and a note saying otherwise would be noise.
    """
    try:
        return SpecifierSet(specifier).contains(Version(version), prereleases=True)
    except (InvalidSpecifier, InvalidVersion):
        return True


def _demand_for(
    reference: PackageReference,
    *,
    stage: PackageStage,
    chain: PackageMeta | None,
    declared_by: str,
    declared_host: str = OFFICIAL_BASE_DOMAIN,
) -> _Demand:
    """What to resolve for one stage: which package, from where, in which range.

    Three sources of an answer, in this order: what the **device** stated,
    what the **stage above** requires, and the stage's own defaults. A
    device that stated nothing defers to the chain entirely — including
    which package and which host, because a requirement may name both. A
    device that named a package the chain does not require is honoured
    with nothing to narrow it, which is what makes "any host, any package"
    an override rather than a refusal.

    Raises when the device deferred and the chain says nothing: "any
    version" and "this one forgot to say" look identical from here and
    mean entirely different things.
    """
    required = _requirements(chain)
    if reference.custom:
        base_domain = reference.base_domain if reference.hosted else declared_host
        match = next(
            (
                requirement
                for requirement in required
                if requirement.describes(
                    name=reference.name, base_domain=base_domain, default_host=declared_host
                )
            ),
            None,
        )
        name = reference.name
    else:
        # The device deferred: the chain decides the package and the host
        # as well as the range, because a requirement may name all three.
        match = next(
            (requirement for requirement in required if requirement.name == stage.family), None
        )
        if match is None and len(required) == 1:
            # The chain redirected: the stage above requires something
            # else, and a device that said nothing follows it there.
            match = required[0]
        name = match.name if match is not None else stage.family
        base_domain = (match.host if match is not None and match.host else "") or declared_host

    if reference.constraint:
        constraint = reference.constraint
    elif reference.sha256:
        # The hash decides which archive, and the index says which
        # version that archive is. Nothing to narrow.
        constraint = ""
    elif match is not None:
        constraint = match.specifier
    elif reference.custom:
        # An override naming a package nobody required: honoured, and the
        # note says so once the version is known.
        constraint = ""
    elif chain is not None:
        # The device deferred and the chain is silent about the stage it
        # is supposed to describe. The model's own refusal names what the
        # package does require.
        chain.constraint_on(name)
        raise AssertionError  # pragma: no cover - constraint_on always raises here
    else:
        raise BuildError(
            f"MCUHome cannot say which version of {name} to build with.",
            hint=(
                f"nothing states which one this build needs — name it in the device's "
                f"sources.{stage.key}, as {stage.source}/{name}:<version>."
            ),
        )
    return _Demand(
        reference=reference,
        stage=stage,
        name=name,
        base_domain=base_domain,
        constraint=constraint,
        required=match,
        declared_by=declared_by,
        declared_host=declared_host,
        declared=chain is not None,
        requirements=required,
    )


class _NotHere(Exception):
    """One shelf did not answer, and what it did have instead."""

    def __init__(self, reason: str, *, silent: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        #: ``True`` where the shelf has the package and no version of it
        #: says what it requires — the one miss worth its own refusal.
        self.silent = silent


@dataclass(frozen=True)
class _Found:
    """One stage resolved: the pin a context records, and the next link."""

    pin: PackagePin
    meta: PackageMeta | None


def _resolve_stage(
    demand: _Demand,
    *,
    sources: Sequence[Path],
    registry: RegistrySource | None,
    platform: str | None,
    on_line: Callable[[str], None] | None = None,
    chain_wanted: bool = False,
) -> _Found:
    """*demand* resolved to one package: the operator's directories, then the registry.

    The answer is the newest published version satisfying the constraint,
    pinned exactly — name, version, hash — plus that package's own meta
    file, which is what the next stage is resolved from. A reference that
    states a version **and** a hash decides everything on its own and its
    pin is answered without reading anything at all.

    *chain_wanted* is the one thing such a pin may still cost a lookup:
    the stage below it stated nothing, so somebody has to say what this
    package requires, and the answer — if a source publishes exactly
    these bytes — is that package's own meta file. Not finding one is no
    refusal here; the stage below refuses for itself, where the message
    can name the key to fix.
    """
    reference = demand.reference
    if reference.pinned:
        pin = PackagePin(name=demand.name, version=reference.version, sha256=reference.sha256)
        _say(on_line, demand.note_for(pin))
        meta = (
            _meta_for(
                pin,
                sources=sources,
                registry=registry,
                source_name=reference.source,
                platform=platform,
            )
            if chain_wanted
            else None
        )
        return _Found(pin=pin, meta=meta)

    problems: list[str] = []
    silent = 0
    for shelf in _shelves(sources=sources, registry=registry, source_name=reference.source):
        try:
            found = _select(shelf, demand, platform=platform)
        except _NotHere as miss:
            problems.append(f"  {shelf.label}: {miss.reason}")
            silent += 1 if miss.silent else 0
            continue
        _say(on_line, demand.note_for(found.pin))
        return found

    listed = "\n".join(problems) or f"  (no source and no registry offers {demand.name})"
    if silent and silent == len(problems):
        raise BuildError(
            f"No published version of {demand.name} says what it requires.",
            hint=(
                f"MCUHome reads that from the {META_FILE} a package index records beside "
                f"each archive, and no offered version has one — a package published "
                f"before MCUHome recorded them carries none:\n{listed}\n"
                f"Use a source that publishes them, or state the version and the hash "
                f"in the device's sources.{demand.stage.key}."
            ),
        )
    wanted = demand.constraint or "any version"
    raise BuildError(
        f'No package source offers a {demand.stage.what} matching "{wanted}".',
        hint=(
            f"{demand.name} was looked for in:\n{listed}\n"
            f"Add the package to one of them, configure the registry, or name a "
            f"version the sources have in the device's sources.{demand.stage.key}."
        ),
    )


def _meta_for(
    pin: PackagePin,
    *,
    sources: Sequence[Path],
    registry: RegistrySource | None,
    source_name: str,
    platform: str | None,
) -> PackageMeta | None:
    """What a package pinned outright says about itself, where a source has it.

    A device that pins one package by hash still overrides that package
    **and nothing else** — so the stage below it keeps resolving through
    the chain, as long as a source publishes exactly these bytes and
    records a meta file beside them. The hash is what decides: an entry
    of the same version under other bytes is a different package and its
    meta file describes something this build is not using.
    """
    try:
        for shelf in _shelves(sources=sources, registry=registry, source_name=source_name):
            try:
                pinned = pin_entry(shelf.entries, pin.name, pin.version, platform=platform)
                if pinned.sha256 != pin.sha256:
                    continue
                entry = resolve_entry(shelf.entries, pin.name, pin.version, platform=platform)
            except PackageRegistryError:
                continue
            if entry.meta_file is None:
                continue
            return _read_meta(shelf, entry)
    except PackageRegistryError:
        # A registry that cannot be reached is not an answer, and this
        # question is an opportunistic one.
        return None
    return None


def _select(shelf: _Shelf, demand: _Demand, *, platform: str | None) -> _Found:
    """One shelf's answer to *demand*, or :class:`_NotHere`.

    **Only a version that says what it requires is a candidate.** The
    chain is resolved through a package's meta file, so a version whose
    index entry records none cannot be resolved *through* — picking it
    would leave the next stage with nothing to go on, one fetch later and
    with a worse message.

    **A family a directory publishes only per platform is still that
    family.** An operator's directory holds the packages one machine
    needs, and a directory that carries ``mcuhome-build-tools_linux-amd64``
    and no family entry is a complete source for this host. It is then
    pinned by the concrete name, because that is the name those bytes are
    published under here and a family hash cannot be recomputed from one
    member.
    """
    name = demand.name
    versions = shelf.entries.get(name)
    if not versions:
        concrete = f"{name}{ARCH_SEPARATOR}{platform or host_platform()}"
        if not shelf.entries.get(concrete):
            raise _NotHere(f"publishes no {demand.name}")
        name = concrete
        versions = shelf.entries[concrete]

    candidates: dict[str, ResolvedEntry] = {}
    silent: list[str] = []
    for version in versions:
        try:
            resolved = resolve_entry(shelf.entries, name, version, platform=platform)
        except PackageRegistryError:
            # Not published for this platform, or an entry this client
            # cannot read. Another version may well be fine.
            continue
        if resolved.meta_file is None:
            silent.append(version)
            continue
        candidates[version] = resolved

    if demand.reference.sha256:
        return _by_hash(shelf, demand, name, versions, platform=platform)
    if not candidates:
        offered = ", ".join(sorted(silent)) or "nothing this machine can use"
        raise _NotHere(f"publishes {offered}, and no meta file for any of them", silent=True)
    try:
        version = resolve_version(
            demand.constraint,
            candidates,
            prereleases=_allow(demand.constraint, None),
            name=name,
        )
    except BuildError:
        raise _NotHere(f"publishes {', '.join(sorted(candidates))}") from None
    return _pinned(shelf, name, version, candidates[version], platform=platform)


def _by_hash(
    shelf: _Shelf,
    demand: _Demand,
    name: str,
    versions: Mapping[str, Mapping[str, object]],
    *,
    platform: str | None,
) -> _Found:
    """The version whose entry carries the hash the reference stated.

    A reference that names a hash and no version selects those bytes and
    lets the index say which release they are. The comparison is against
    the *pin* the index would hand out — a family's hash over its members
    as readily as one archive's — so ``@sha256:`` works for both.
    """
    stated = demand.reference.sha256
    for version in versions:
        try:
            pinned = pin_entry(shelf.entries, name, version, platform=platform)
        except PackageRegistryError:
            continue
        if pinned.sha256 != stated:
            continue
        try:
            resolved = resolve_entry(shelf.entries, name, version, platform=platform)
        except PackageRegistryError:  # pragma: no cover - pin_entry answered a moment ago
            continue
        return _pinned(shelf, name, version, resolved, platform=platform)
    raise _NotHere(f"publishes no {demand.name} with that hash")


def _pinned(
    shelf: _Shelf,
    name: str,
    version: str,
    entry: ResolvedEntry,
    *,
    platform: str | None,
) -> _Found:
    """The pin for one selected version, and the meta file behind it."""
    pinned = pin_entry(shelf.entries, name, version, platform=platform)
    meta = None if entry.meta_file is None else _read_meta(shelf, entry)
    pin = PackagePin(
        name=pinned.name,
        version=pinned.version,
        sha256=pinned.sha256,
        url=shelf.url_for(pinned),
    )
    return _Found(pin=pin, meta=meta)


def _read_meta(shelf: _Shelf, entry: ResolvedEntry) -> PackageMeta:
    """One package's meta file, verified as the index records it and as its own.

    Two checks, and the second is the one a mirror cannot fake its way
    past: the bytes against the hash the signed index states, and the
    document against the archive it sits beside. A sidecar describing
    another package would decide the next stage from something this build
    is not using.
    """
    what = f"{entry.name} {entry.version}"
    meta = parse_meta(
        shelf.meta_document(entry.meta_file, what=what),  # type: ignore[arg-type]
        what=f"The {META_FILE} of {what}",
    )
    if meta.package != entry.name or not _same_version(meta.version, entry.version):
        raise BuildError(
            f"{shelf.label} records a meta file for {what} that describes "
            f"{meta.package} {meta.version}.",
            hint=(
                "the package source is inconsistent — synchronise it again, or "
                "report it to whoever publishes it"
            ),
        )
    return meta


def _same_version(one: str, other: str) -> bool:
    """PEP 440 equality, falling back to the spelling for unparseable versions."""
    try:
        return Version(one) == Version(other)
    except InvalidVersion:
        return one == other


def _say(on_line: Callable[[str], None] | None, line: str | None) -> None:
    if on_line is not None and line:
        on_line(line)


def _registry_for(
    demand: _Demand,
    *,
    sdk: PackageReference,
    registry: RegistrySource | None,
    hosts: Callable[[str], PackageRegistry] | None,
    what: str,
) -> RegistrySource | None:
    """The registry this stage resolves through — its own host's.

    The host is the **demand's**, not the reference's: a reference that
    names none is understood against the package that required it, and
    that package may itself live somewhere other than the official
    domain. Reading the reference here instead would refuse a device
    that named one host for its SDK and nothing at all for the rest,
    over a second host the person never wrote.

    The ordinary case is one host for the whole build and the client the
    SDK's reference already opened. A device that points one package at
    **another** base domain gets another client, built for that domain
    from the project's own configuration: a trust anchor is per base
    domain, and so are the mirrors, so this is the only way such a
    reference can be honoured at all.

    Where no such factory was handed over — a build server resolves for
    one host by decision — a foreign domain is refused rather than looked
    up on the wrong one.
    """
    if demand.base_domain == sdk.base_domain or demand.reference.pinned:
        return registry
    if hosts is not None:
        domain = demand.base_domain
        return lambda: hosts(domain)
    raise BuildError(
        f"This device takes its SDK from {sdk.base_domain} and its {what} package "
        f"from {demand.base_domain}, and this build reads one package host.",
        hint=(
            "point sources.sdk, sources.build_workspace and sources.build_tools at "
            "the same registry, or state the version and the hash of the package "
            "outright so that nothing has to be looked up"
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
    hosts: Callable[[str], PackageRegistry] | None = None,
    platform: str | None = None,
    on_line: Callable[[str], None] | None = None,
) -> EnvironmentPin:
    """The two packages a context pins its build environment to.

    *workspace* and *tools* are the device's ``sources.build_workspace``
    and ``sources.build_tools`` references, *sdk_source* is its
    ``sources.sdk`` reference and *sdk* is what that already resolved to.

    **The chain decides, one link at a time.** The resolved SDK release's
    own ``meta.json`` states which *range* of build workspaces it was
    built and tested with; the newest published workspace version
    satisfying it wins and is pinned exactly, and *that* package's meta
    file states which range of build tools it needs, which is resolved the
    same way. Three release lines, two constraints, and no version
    anywhere in the middle: a workspace release that fixes something
    reaches an existing device without the SDK being re-cut.

    **A device overrides any of it, and is never refused for it.** A
    reference may narrow the range (``:~=0.2``), pin a version, name
    another package, another host, or exact bytes; what it states wins,
    and where it lands outside what the stage above declared the build
    says so in one line and goes on. The stage above knows what it was
    tested with, not what is allowed.

    **Versions come from an index, hashes with them.** A meta file
    carries no hashes — a package cannot state its own, and the one below
    it may not be built yet — so both halves of a pin come from a package
    index: an operator directory's, or the one a registry mirror served
    and the project's trust anchor accepted. Only a version whose index
    entry records a meta file is a candidate, because a version that does
    not say what it requires cannot be resolved through.

    **One kind, one set of directories.** *sources* holds the SDK's
    (``build.sdk_sources``), *workspace_sources* the build workspace's
    and *tools_sources* the build tools' — and no kind is ever looked for
    under another kind's directories. A directory that holds the SDK
    package is not thereby a claim about where build workspaces live,
    and a machine that keeps all three in one place says so in all three
    keys, which is the statement it is actually making. An empty set is
    "no operator directory for this kind", and the package is then
    resolved through the registry alone.

    *hosts* opens a registry for a base domain other than the SDK's;
    without it a foreign host is a refusal rather than a lookup on the
    wrong registry. *on_line* is where the override note goes — the build
    log, where the person watching is already looking.
    """
    workspace_reference = package_reference(workspace, stage=WORKSPACE_STAGE)
    tools_reference = package_reference(tools, stage=TOOLS_STAGE)
    sdk_reference = package_reference(sdk_source, stage=SDK_STAGE)

    sdk_meta: PackageMeta | None = None
    if not workspace_reference.pinned:
        # A reference that states a version *and* a hash decides
        # everything and reads nothing — that is the offline case. Every
        # other one wants the SDK's own statement: to resolve by, or to
        # hold a device's own choice against.
        sdk_meta = sdk_package_meta(
            version=sdk.package.version,
            sha256=sdk.package.sha256,
            sources=sources,
            into=Path(work_root) / "sdk-meta",
            registry=registry,
            max_bytes=max_bytes,
        )

    workspace_demand = _demand_for(
        workspace_reference,
        stage=WORKSPACE_STAGE,
        chain=sdk_meta,
        declared_by=f"{SDK_PACKAGE_NAME} {sdk.package.version}",
        declared_host=sdk_reference.base_domain,
    )
    workspace_found = _resolve_stage(
        workspace_demand,
        sources=tuple(workspace_sources),
        registry=_registry_for(
            workspace_demand,
            sdk=sdk_reference,
            registry=registry,
            hosts=hosts,
            what="build workspace",
        ),
        platform=platform,
        on_line=on_line,
        # The tools stage says nothing, so the workspace has to — even
        # where the device pinned it by hash and nothing was looked up
        # for the pin itself.
        chain_wanted=not tools_reference.stated,
    )
    tools_demand = _demand_for(
        tools_reference,
        stage=TOOLS_STAGE,
        chain=workspace_found.meta,
        declared_by=f"{workspace_found.pin.name} {workspace_found.pin.version}",
        declared_host=workspace_demand.base_domain,
    )
    tools_found = _resolve_stage(
        tools_demand,
        sources=tuple(tools_sources),
        registry=_registry_for(
            tools_demand,
            sdk=sdk_reference,
            registry=registry,
            hosts=hosts,
            what="build tools",
        ),
        platform=platform,
        on_line=on_line,
    )
    return EnvironmentPin(workspace=workspace_found.pin, tools=tools_found.pin)


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
