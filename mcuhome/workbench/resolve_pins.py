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
``index.json`` shape and hands back the selected package's entry.

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

from collections.abc import Iterable
from dataclasses import dataclass

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from mcuhome.model.errors import BuildError

__all__ = [
    "ResolvedPackage",
    "resolve_from_index",
    "resolve_version",
]


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
                "constraints are PEP 440 (ADR 0018): a compatible-release "
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
