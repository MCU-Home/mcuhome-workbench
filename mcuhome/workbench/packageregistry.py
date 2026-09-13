# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Reading a package registry: mirrors, signed documents, verified index.

A **registry** is a base domain and nothing more. Ask it for a source's
mirror list — ``https://<base-domain>/<source>/mirrors.json`` — and every
mirror it names serves that source as a plain directory of files: the
signed documents (``keys.json``, ``mirrors.json``, ``index.json``, each
beside its ``.sig``), the index parts a large source is split into, and
the package archives themselves. Nothing here is an API; it is files, so
a mirror can be a web server, a directory on a NAS, or a USB stick.

**The rules are not implemented here.** Signature thresholds, key
rotation, freshness, the meta-entry hash — all of it is
:mod:`mcuhome.packagetool.verify`, the same code the registry's own
publish pipeline runs before it publishes and the same code the SDK's
image build runs before it assembles. This module fetches bytes, hands
them to that verifier, and turns its verdicts into the workbench's typed
refusals. A second implementation of those rules would be a second thing
to get right, and the two would drift apart in exactly the way nobody
notices until it matters.

**Why the host is never trusted.** The mirror list is read from the base
domain before anything has been verified — it has to be, it is how a
client learns where to look. So it decides only *where to fetch from*,
never *what is true*: whatever a mirror serves is verified against the
trust anchor the project configured, and a mirror that serves something
else is skipped for the next one. The one thing a client must not take
from the network is the anchor itself.

**Trust anchors are per base domain**, in
``<project>/secrets/trust-anchor/<base-domain>.json``. MCUHome's own
anchor ships with this package and is written into a project at exactly
one moment: when the project is created (:func:`install_trust_anchors`,
called by ``mcuhome project init``). Never afterwards. A build that finds
the file missing refuses and says which file to create, and it does so
for MCUHome's own registry exactly as it does for anybody else's —
because "the tool put a key set there because it was not there" is not a
trust decision a user ever made, and a build that quietly acquired its
own trust root is the one failure mode this whole file exists to
prevent. An existing file is likewise never rewritten: a person who
edited theirs did so deliberately. The one way out is marking the
registry untrusted, which trades every guarantee here for the ability to
run against something unsigned, loudly.

**Local mirrors are first-class.** A mirror location may be a directory
path instead of a URL, and an operator who synchronises a mirror out of
band can point the workbench at it and build with no network at all. Such
a directory is verified in place, exactly as a fetched copy is.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from mcuhome.model.errors import BuildError, ConfigError, Location
from mcuhome.model.sdkindex import SDK_REGISTRY
from mcuhome.model.userpaths import expand
from mcuhome.packagetool.verify import (
    INDEX_FILE,
    KEYS_FILE,
    META_FILE_KEY,
    MIRRORS_FILE,
    SIGNATURE_SUFFIX,
    KeySet,
    Refused,
    all_entries,
    check_meta_entry,
    check_meta_file,
    load_anchor,
    verify_source,
)
from packaging.version import InvalidVersion, Version

from mcuhome.workbench.diagnostics import Diagnostic
from mcuhome.workbench.project import SECRETS_DIR, ensure_secrets_dir, require_secret_file

__all__ = [
    "BUNDLED_ANCHOR_DIR",
    "OFFICIAL_BASE_DOMAIN",
    "TRUST_ANCHOR_DIR",
    "MetaFile",
    "PackageRegistry",
    "PackageRegistryError",
    "RegistrySettings",
    "PinnedEntry",
    "ResolvedEntry",
    "TrustAnchorMissing",
    "VerifiedIndex",
    "RegistrySource",
    "anchor_file",
    "host_platform",
    "install_trust_anchors",
    "load_trust_anchor",
    "check_meta_bytes",
    "check_platform",
    "matching_version",
    "merge_registries",
    "meta_file_of",
    "opened",
    "pin_entry",
    "parse_registries",
    "open_package_registry",
    "registry_opener",
    "registry_for",
    "resolve_entry",
    "settings_for",
    "trust_anchor_for",
]

#: The base domain a reference that names none is understood against.
OFFICIAL_BASE_DOMAIN = SDK_REGISTRY

#: ``secrets/trust-anchor/`` — one file per base domain. Under
#: ``secrets/`` not because an anchor is secret (it is public material)
#: but because it is *this project's* trust decision and belongs with the
#: rest of what the project alone decides.
TRUST_ANCHOR_DIR = "trust-anchor"

#: Anchors shipped with the workbench, by base domain. Copied out once,
#: never consulted afterwards.
BUNDLED_ANCHOR_DIR = Path(__file__).parent / TRUST_ANCHOR_DIR

#: The dimension a meta package resolves the host through. The shape is
#: general — a meta entry may name any dimension — and this is the one
#: an environment package is published across today.
ARCH_DIMENSION = "arch"

#: Package names carry an optional architecture suffix after the first
#: underscore (``mcuhome-build-tools_linux-amd64``), which is what makes
#: a concrete package of a foreign platform recognisable by name alone.
ARCH_SEPARATOR = "_"

_DEFAULT_TIMEOUT = 30.0
_BLOCK = 1 << 20

#: How many generations of a rotated key set are followed back towards
#: the anchor before the chain is called a loop. Ten rotations is more
#: than a registry will do in a decade, and a client that followed an
#: unbounded chain would fetch whatever a mirror felt like serving.
_MAX_KEY_GENERATIONS = 10


_LOG = logging.getLogger(__name__)


class PackageRegistryError(BuildError):
    """A registry could not be read, or what it served did not verify."""


class TrustAnchorMissing(PackageRegistryError):
    """No trust anchor is configured for this registry, and none can be invented."""


# --------------------------------------------------------------------------
# This host
# --------------------------------------------------------------------------

#: What ``uname -m`` reports, mapped to the architecture half of a
#: platform name. Both spellings of each are accepted because both are
#: reported in the wild, and the answer is the one MCUHome publishes
#: under.
_ARCHITECTURES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


def host_platform(*, system: str | None = None, machine: str | None = None) -> str:
    """This host as a platform name: ``linux-amd64`` or ``linux-arm64``.

    The name a package's architecture suffix and a meta entry's ``arch``
    map are spelled in. *system* and *machine* default to what the
    running interpreter reports and are parameters so a test can ask
    about a host it is not running on.

    Raises a typed refusal on a platform MCUHome publishes nothing for,
    rather than composing a name no index will ever carry.
    """
    import platform as platform_module

    system = (system if system is not None else platform_module.system()).lower()
    machine = (machine if machine is not None else platform_module.machine()).lower()
    architecture = _ARCHITECTURES.get(machine)
    if system != "linux" or architecture is None:
        raise PackageRegistryError(
            f"MCUHome publishes no build environment for {system}/{machine}.",
            hint=(
                "the packages are built for 64-bit Linux on x86-64 and on ARM. "
                "Build in the container instead, or on a supported host."
            ),
        )
    return f"{system}-{architecture}"


# --------------------------------------------------------------------------
# Trust anchors
# --------------------------------------------------------------------------


def _usable_domain(base_domain: str) -> str:
    """*base_domain* as a file name component, or a refusal.

    The domain reaches this from a device model or a configuration file
    and is used to name a file under ``secrets/``; a separator or a
    ``..`` in it would name a file somewhere else entirely.
    """
    if (
        not base_domain
        or base_domain.startswith(".")
        or any(part in base_domain for part in ("/", "\\", "\x00", ".."))
    ):
        raise PackageRegistryError(
            f'"{base_domain}" is not a registry domain.',
            hint="a registry is named by its domain, for example packages.mcuhome.org",
        )
    return base_domain


def anchor_file(project_root: Path, base_domain: str) -> Path:
    """``<project>/secrets/trust-anchor/<base-domain>.json`` — the one file that decides."""
    return (
        Path(project_root) / SECRETS_DIR / TRUST_ANCHOR_DIR / f"{_usable_domain(base_domain)}.json"
    )


def install_trust_anchors(project_root: Path) -> tuple[Path, ...]:
    """Write the anchors this workbench ships into a project being created.

    Called once, by ``mcuhome project init``, and by nothing else. That
    is the whole point: a project's trust roots are decided when the
    project comes into existence, by whoever created it, and never later
    by a build that noticed a file was missing. A build acquiring its own
    trust root would be trusting whatever happened to be installed at the
    moment it looked.

    An anchor that is already there is left exactly as it is — including
    on a re-run of init over an existing project, where an edited file is
    somebody's decision and not a gap to fill. Returns the files it
    actually wrote, so init can report them with everything else it
    created.
    """
    project_root = Path(project_root)
    written: list[Path] = []
    for bundled in sorted(BUNDLED_ANCHOR_DIR.glob("*.json")):
        path = anchor_file(project_root, bundled.stem)
        if path.is_file():
            continue
        ensure_secrets_dir(project_root, TRUST_ANCHOR_DIR)
        path.write_bytes(bundled.read_bytes())
        if os.name == "posix":
            path.chmod(0o600)
        written.append(path)
    return tuple(written)


def trust_anchor_for(
    project_root: Path,
    base_domain: str,
    *,
    untrusted: bool = False,
    stated: Path | None = None,
    on_warning: Callable[[Diagnostic], None] | None = None,
) -> Path | None:
    """The project's trust anchor for *base_domain*, or a refusal.

    Reading only. Nothing is created here, for MCUHome's own registry as
    little as for anybody else's: the file is written when a project is
    created (:func:`install_trust_anchors`) and is a statement by whoever
    created it. A build that found it missing and wrote one would be
    deciding what to trust on the user's behalf, at the worst possible
    moment — while it is about to download something.

    So: the file is there and is used as it is, or *untrusted* says the
    project accepts an unsigned source and this answers ``None``, or the
    build refuses with the path to create.

    *stated* is the configuration naming this registry's anchor outright
    (``registry.<base-domain>.anchor``), for anchors that are deployed
    with a machine rather than kept in a project. It replaces the
    project's file entirely — it is not a fallback for a missing one —
    and a path that names nothing is refused rather than quietly
    ignored: a configured anchor that silently did not exist would leave
    a registry checked against something else than the operator chose.
    ``untrusted: true`` still wins over both, as it does over a project's
    own file — the setting is the statement, and a missing file does not
    turn it into a refusal.
    """
    if stated is not None:
        path = Path(stated)
        if not path.is_file():
            if untrusted:
                return None
            raise TrustAnchorMissing(
                f"The trust anchor configured for the registry {base_domain} is not there: {path}.",
                hint=(
                    "save the key set the registry's operator publishes at that path, "
                    "or drop the anchor entry to use the project's own copy under "
                    "secrets/trust-anchor/ instead."
                ),
            )
        require_secret_file(path, key_material=False, on_warning=on_warning)
        return path
    path = anchor_file(project_root, base_domain)
    if path.is_file():
        require_secret_file(path, key_material=False, on_warning=on_warning)
        return path
    if untrusted:
        return None

    bundled = BUNDLED_ANCHOR_DIR / f"{_usable_domain(base_domain)}.json"
    where = (
        "MCUHome writes this file when a project is created, so this project "
        "either predates that or the file was removed. Restore it by running "
        "init over the project again:\n"
        "    mcuhome project init . --force\n"
        if bundled.is_file()
        else (
            f"a registry is only worth what its signatures are checked against, and "
            f"that key set has to reach this project from somewhere other than the "
            f"registry itself. Ask whoever runs {base_domain} for it and save it as:\n"
            f"    {path}\n"
        )
    )
    raise TrustAnchorMissing(
        f"MCUHome has no trust anchor for the registry {base_domain}.",
        hint=(
            f"{where}"
            f"To build against this registry without checking anything — which "
            f"means trusting whatever it serves — mark it untrusted in "
            f"mcuhome.yaml:\n"
            f"    registry:\n"
            f"      {base_domain}:\n"
            f"        untrusted: true"
        ),
    )


def load_trust_anchor(path: Path) -> KeySet:
    """The anchor at *path*, as the verifier's key set."""
    try:
        return load_anchor(Path(path))
    except Refused as unusable:
        raise PackageRegistryError(
            f"The trust anchor {path} cannot be used: {unusable}.",
            hint=(
                "an anchor states the registry's root keys and how many of them a "
                "document needs. Replace the file with the one the registry's "
                "operator publishes."
            ),
        ) from unusable


# --------------------------------------------------------------------------
# Configuration: registry.<base-domain>.…
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RegistrySettings:
    """What a project says about one registry.

    :attr:`mirrors` replaces the served mirror list **for the sources it
    names** and only for those: a project that overrides ``sdk`` still
    discovers the mirrors of every other source normally. Replacement
    rather than addition is the point — an operator who lists a local
    mirror for an air-gapped build must not have the workbench fall back
    to a public host the moment the local copy is short of something.

    :attr:`anchor` names the trust anchor of *this* registry outright,
    for an operator whose anchors do not live in a project — a private
    registry whose key set is deployed with the machine, or a build that
    runs outside any project at all. It is stated per registry and never
    as one directory for all of them, because a setting that moved every
    anchor at once would move MCUHome's own along with the private one,
    and that is a decision nobody meant to make while configuring their
    own registry.
    """

    base_domain: str
    untrusted: bool = False
    mirrors: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: The anchor file to hold this registry's signatures against, or
    #: ``None`` for the project's own ``secrets/trust-anchor/`` copy.
    anchor: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready, every declared key present.

        What ``mcuhome config print`` shows for a registry. The key is
        ``base_domain`` — the same word the configuration key and this
        class use — rather than a second name for it.
        """
        return {
            "base_domain": self.base_domain,
            "untrusted": self.untrusted,
            "mirrors": {name: list(values) for name, values in self.mirrors.items()},
            "anchor": None if self.anchor is None else str(self.anchor),
        }


def parse_registries(
    value: Any, *, file: Path, origin: str, env: Mapping[str, str] | None = None
) -> tuple[RegistrySettings, ...]:
    """The ``registry:`` block of a configuration file.

    ::

        registry:
          packages.mcuhome.org:
            untrusted: false
            anchor: /etc/mcuhome/anchors/packages.example.org.json
            mirrors:
              sdk:
                - /srv/mirrors/sdk
                - https://mirror-2.example.org/sdk/

    A mirror entry is either an ``https://`` base URL ending in ``/`` or
    a path to a directory laid out like a served source. Relative paths
    are relative to the file that names them, like every other path in a
    configuration file: the file's author can see where the file is, and
    the reading process cannot — and ``anchor`` follows the same rule.
    """
    del origin

    def refuse(message: str, key: str | None = None) -> ConfigError:
        return ConfigError(message, location=Location(file=file, key=key))

    if not isinstance(value, dict):
        raise refuse(
            "The option 'registry' must be a mapping of registry domains to their settings.",
            "registry",
        )

    parsed: list[RegistrySettings] = []
    for domain, settings in value.items():
        base_domain = str(domain)
        if not isinstance(settings, dict):
            raise refuse(
                f"The registry {base_domain!r} must be a mapping — it may carry "
                "'untrusted', 'mirrors' and 'anchor'.",
                base_domain,
            )
        unknown = set(settings) - {"untrusted", "mirrors", "anchor"}
        if unknown:
            raise refuse(
                f"The registry {base_domain!r} carries {', '.join(sorted(map(str, unknown)))}, "
                "which is nothing MCUHome knows — it takes 'untrusted', 'mirrors' "
                "and 'anchor'.",
                base_domain,
            )
        untrusted = settings.get("untrusted", False)
        if not isinstance(untrusted, bool):
            raise refuse(
                f"The registry {base_domain!r} must state 'untrusted' as true or false.",
                base_domain,
            )
        stated = settings.get("anchor")
        if stated is not None and (not isinstance(stated, str) or not stated):
            raise refuse(
                f"The registry {base_domain!r} must state 'anchor' as the path of its "
                "trust anchor file.",
                base_domain,
            )
        anchor = (
            None if stated is None else _resolve_anchor(stated, env=env or {}, base=file.parent)
        )
        mirrors: dict[str, tuple[str, ...]] = {}
        declared = settings.get("mirrors", {})
        if not isinstance(declared, dict):
            raise refuse(
                f"The registry {base_domain!r} must state 'mirrors' as a mapping of "
                "source names to lists of mirrors.",
                base_domain,
            )
        for source, locations in declared.items():
            if isinstance(locations, str) or not isinstance(locations, list):
                raise refuse(
                    f"The mirrors of {base_domain}/{source} must be a list "
                    "(one `- location` line each).",
                    str(source),
                )
            resolved: list[str] = []
            for location in locations:
                if not isinstance(location, str) or not location:
                    raise refuse(
                        f"A mirror of {base_domain}/{source} must be an https base URL "
                        "or a directory path.",
                        str(source),
                    )
                resolved.append(_resolve_mirror(location, env=env or {}, base=file.parent))
            mirrors[str(source)] = tuple(resolved)
        parsed.append(
            RegistrySettings(
                base_domain=base_domain,
                untrusted=untrusted,
                mirrors=mirrors,
                anchor=anchor,
            )
        )
    return tuple(parsed)


def _resolve_anchor(location: str, *, env: Mapping[str, str], base: Path | None) -> Path:
    """A configured anchor path: expanded, and relative to the file that named it."""
    path = expand(location, dict(env))
    return path if base is None or path.is_absolute() else (base / path).resolve()


def merge_registries(
    below: Sequence[RegistrySettings], above: Sequence[RegistrySettings]
) -> tuple[RegistrySettings, ...]:
    """Merge two layers by base domain — a whole registry at a time.

    The named unit is the registry: a project that says something about
    ``packages.mcuhome.org`` states everything it wants to say about it,
    and does not inherit half a mirror list from the user layer. Domains
    the higher layer is silent about come through untouched.
    """
    merged = {settings.base_domain: settings for settings in below}
    for settings in above:
        merged[settings.base_domain] = settings
    return tuple(merged.values())


def settings_for(registries: Sequence[RegistrySettings], base_domain: str) -> RegistrySettings:
    """What the configuration says about *base_domain* — defaults when nothing."""
    for settings in registries:
        if settings.base_domain == base_domain:
            return settings
    return RegistrySettings(base_domain=base_domain)


def _resolve_mirror(location: str, *, env: Mapping[str, str], base: Path | None) -> str:
    """A configured mirror, resolved once: URLs normalised, paths made absolute.

    ``~`` and ``$VAR`` are expanded against the *stated* environment
    rather than the process's, because one process serves several
    sessions and each of them has its own. A relative path is relative to
    the file that named it, like every other path in a configuration
    file.
    """
    if _is_url(location):
        return _normalised(location)
    path = expand(location, dict(env))
    if base is not None and not path.is_absolute():
        path = (base / path).resolve()
    return str(path)


def _normalised(location: str) -> str:
    """A mirror location as the fetcher wants it: a base, so ending in a slash."""
    if _is_url(location) and not location.endswith("/"):
        return location + "/"
    return location


def _is_url(location: str) -> bool:
    return location.startswith("https://") or location.startswith("http://")


# --------------------------------------------------------------------------
# The index, and what a name resolves to in it
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MetaFile:
    """The sidecar an index records beside an archive: file, hash, size.

    ``<archive>.meta.json`` is what a package says about itself — what it
    requires of the stage below it above all — and the index records it
    exactly the way it records the archive: by name, hash and size, never
    by content. That is what keeps "which version satisfies this
    constraint" a question the index answers on its own, with one small
    fetch after it for the one version that won.

    The three members are the whole type. What is *in* the document is
    :class:`~mcuhome.model.buildenvironment.PackageMeta`'s business, and
    the two are deliberately apart: this one says where the bytes are and
    which bytes are right, that one says what they mean.
    """

    file: str
    sha256: str
    size: int


def meta_file_of(entry: Mapping[str, Any], *, name: str, version: str) -> MetaFile | None:
    """The ``meta_file`` record of *entry*, or ``None`` where it has none.

    Absent is a legitimate answer: the member is optional in the index
    format, and a package published before it existed simply has none.
    What a *caller* does with that absence is its own rule — a chain
    resolution refuses such a version as a candidate, because a version
    that does not say what it requires cannot be resolved through.

    Present and malformed is not absent: the shape is checked by the same
    verifier the registry's own publish pipeline runs
    (:func:`~mcuhome.packagetool.verify.check_meta_file`), so a damaged
    record is a refusal rather than a silently ignored member.
    """
    if META_FILE_KEY not in entry:
        return None
    try:
        check_meta_file(name, version, dict(entry))
    except Refused as broken:
        raise PackageRegistryError(
            f"The index entry for {name} {version} records a damaged meta file: {broken}.",
            hint=(
                "the index is damaged or was tampered with. Try another mirror, "
                "and report it to the registry's operator."
            ),
        ) from broken
    record = entry[META_FILE_KEY]
    return MetaFile(
        file=str(record["file"]), sha256=str(record["sha256"]), size=int(record["size"])
    )


def check_meta_bytes(payload: bytes, meta: MetaFile, *, where: str) -> bytes:
    """*payload* held against the size and hash the index records for it.

    The one check that makes the transport irrelevant, on the document
    rather than on an archive: a meta file decides which version of the
    next stage a build resolves to, so bytes that are not the ones the
    index was signed for are a refusal, not a fallback.
    """
    if len(payload) != meta.size:
        raise PackageRegistryError(
            f"{meta.file} is {len(payload)} bytes and the index says {meta.size}.",
            hint="the copy is incomplete, or the source serves something else",
        )
    measured = hashlib.sha256(payload).hexdigest()
    if measured != meta.sha256:
        raise PackageRegistryError(
            f"{meta.file} hashes to {measured} and the index says {meta.sha256}.",
            hint=(
                f"these are not the bytes {where} was signed for. Try another mirror; "
                "if every one says this, tell the registry's operator."
            ),
        )
    return payload


@dataclass(frozen=True)
class ResolvedEntry:
    """One concrete package: the name it is published under and its entry.

    The name is not necessarily the name that was asked for. A *meta*
    package names one concrete package per architecture, so asking for
    ``mcuhome-build-tools`` on this host answers with
    ``mcuhome-build-tools_linux-amd64`` — and the caller needs to know
    that, because that is the name and the file the bytes come under.
    """

    name: str
    version: str
    file: str
    sha256: str
    size: int
    #: ``True`` when the answer came out of a meta entry.
    through_meta: bool = False
    #: The ``<archive>.meta.json`` beside it, where the index records one.
    meta_file: MetaFile | None = None


def matching_version(
    entries: Mapping[str, Mapping[str, Mapping[str, Any]]], name: str, version: str
) -> str | None:
    """The key *entries* spells *version* under, or ``None`` if it has none.

    Version **equality is PEP 440's**, not string equality: an index that
    records ``0.1`` and a pin that says ``0.1.0`` are the same release,
    and a client that compared the two spellings as text would call a
    package it is looking straight at absent. The exact spelling is tried
    first, because that is the overwhelmingly common case and it costs a
    dictionary lookup.

    An unparseable version — on either side — is not an error here: it
    simply does not match, and the caller's own "carries no such version"
    refusal is the one a user should read.
    """
    versions = entries.get(name)
    if not versions:
        return None
    if version in versions:
        return version
    try:
        wanted = Version(version)
    except InvalidVersion:
        return None
    for candidate in versions:
        try:
            if Version(str(candidate)) == wanted:
                return str(candidate)
        except InvalidVersion:
            continue
    return None


def resolve_entry(
    entries: Mapping[str, Mapping[str, Mapping[str, Any]]],
    name: str,
    version: str,
    *,
    platform: str | None = None,
) -> ResolvedEntry:
    """The concrete package *name* at *version* is, on *platform*.

    The version is matched by PEP 440 equality
    (:func:`matching_version`), so an index that spells a release ``0.1``
    answers a pin of ``0.1.0``.

    *platform* defaults to this host and is resolved only where it is
    actually needed — following a meta entry, or holding a name's
    architecture suffix against it. A package that is not published per
    architecture resolves on any host, including one MCUHome ships no
    build environment for.

    Three cases, and each of them is a rule rather than a convenience:

    * **A concrete entry** answers for itself — after its architecture
      suffix is held against *platform*, because an index carries every
      architecture and a pin naming a foreign one would otherwise be
      fetched, hashed, unpacked and only then fail to run.
    * **A meta entry** names packages instead of bytes: it carries a
      ``meta`` map and no ``file``, and it is followed through
      ``meta.arch`` to this host's package. Its hash is recomputed from
      the members it points at before anything is followed, so pinning
      the meta package really does pin every member's bytes.
    * **Anything else** is a malformed index, refused as one.
    """
    versions = entries.get(name)
    key = matching_version(entries, name, version)
    if key is None:
        offered = ", ".join(sorted(versions or ())) or "no version at all"
        raise PackageRegistryError(
            f"The package index carries no {name} {version}; it carries {offered}.",
            hint="the version is not published (yet) — pick one the index names",
        )
    # From here on the *index's* spelling of the version is used, not the
    # caller's: a meta entry's members are recorded under the same key it
    # is, and looking them up under another spelling of the same release
    # would call them missing.
    version = key
    entry = versions[version]

    if "meta" in entry:
        try:
            check_meta_entry(entries, name, version, entry)
        except Refused as broken:
            raise PackageRegistryError(
                f"The index entry for {name} {version} does not describe the packages "
                f"it points at: {broken}.",
                hint=(
                    "the index is damaged or was tampered with. Try another mirror, "
                    "and report it to the registry's operator."
                ),
            ) from broken
        by_architecture = entry["meta"].get(ARCH_DIMENSION)
        if not isinstance(by_architecture, dict):
            raise PackageRegistryError(
                f"The index entry for {name} {version} names no architectures.",
                hint="MCUHome resolves this package per architecture, and this entry does not",
            )
        platform = platform or host_platform()
        if platform not in by_architecture:
            named = ", ".join(sorted(map(str, by_architecture))) or "none"
            raise PackageRegistryError(
                f"{name} {version} is not published for {platform}; it is published for {named}.",
                hint="build in the container instead, or on one of the platforms named",
            )
        concrete = str(by_architecture[platform])
        member = entries.get(concrete, {}).get(version)
        if member is None:  # pragma: no cover - check_meta_entry refused this already
            raise PackageRegistryError(
                f"The index names {concrete} {version} and does not carry it.",
                hint="the index is damaged — try another mirror",
            )
        return _concrete(concrete, version, member, through_meta=True)

    check_platform(name, platform=platform)
    return _concrete(name, version, entry, through_meta=False)


@dataclass(frozen=True)
class PinnedEntry:
    """One package **as a pin names it** — which may be a family.

    The difference to :class:`ResolvedEntry` is the whole point of it. A
    resolution answers with bytes this host can run, so it follows a meta
    entry to the concrete package of the platform. A *pin* is written
    into a build context and read on other machines, so it keeps the name
    that was pinned: a family name says "resolve this per platform" and
    is the normal case, because that is what lets one context build the
    same firmware on an amd64 host and on an arm64 one.

    :attr:`sha256` is what the index states for that name — a concrete
    package's archive hash, or a meta entry's hash over the members it
    points at, which has been recomputed and checked before this value is
    handed out. Either way the hash pins bytes: a meta hash covers every
    platform's archive.
    """

    name: str
    version: str
    sha256: str
    #: ``True`` when the name stands for a set of per-platform packages.
    meta: bool = False
    #: The archive this name maps to on a mirror — empty for a meta
    #: entry, which maps to a package per platform and to no file at all.
    file: str = ""


def pin_entry(
    entries: Mapping[str, Mapping[str, Mapping[str, Any]]],
    name: str,
    version: str,
    *,
    platform: str | None = None,
) -> PinnedEntry:
    """What a pin for *name* at *version* says — meta entry kept as one.

    Same lookup as :func:`resolve_entry` and the same PEP 440 version
    equality, stopping one step earlier: a meta entry is **verified** —
    its hash recomputed from the members it points at — and then answered
    with, rather than followed. A concrete name is answered with after
    its architecture suffix has been held against *platform*, because a
    pin naming a foreign platform's package is a mistake worth catching
    where it is written rather than where it is fetched.
    """
    versions = entries.get(name)
    key = matching_version(entries, name, version)
    if key is None:
        offered = ", ".join(sorted(versions or ())) or "no version at all"
        raise PackageRegistryError(
            f"The package index carries no {name} {version}; it carries {offered}.",
            hint="the version is not published (yet) — pick one the index names",
        )
    entry = versions[key]  # type: ignore[index]
    if "meta" in entry:
        try:
            check_meta_entry(entries, name, key, entry)
        except Refused as broken:
            raise PackageRegistryError(
                f"The index entry for {name} {key} does not describe the packages "
                f"it points at: {broken}.",
                hint=(
                    "the index is damaged or was tampered with. Try another mirror, "
                    "and report it to the registry's operator."
                ),
            ) from broken
        return PinnedEntry(name=name, version=key, sha256=str(entry["sha256"]), meta=True)
    check_platform(name, platform=platform)
    concrete = _concrete(name, key, entry, through_meta=False)
    return PinnedEntry(
        name=concrete.name,
        version=concrete.version,
        sha256=concrete.sha256,
        file=concrete.file,
    )


def _concrete(
    name: str, version: str, entry: Mapping[str, Any], *, through_meta: bool
) -> ResolvedEntry:
    try:
        return ResolvedEntry(
            name=name,
            version=version,
            file=str(entry["file"]),
            sha256=str(entry["sha256"]),
            size=int(entry["size"]),
            through_meta=through_meta,
            meta_file=meta_file_of(entry, name=name, version=version),
        )
    except (KeyError, TypeError, ValueError) as broken:
        raise PackageRegistryError(
            f"The index entry for {name} {version} is missing something: {broken}.",
            hint="each entry carries file, sha256 and size — the index is malformed",
        ) from broken


def check_platform(name: str, *, platform: str | None) -> None:
    """A concrete package named for another architecture, refused by its name.

    A package name may carry an architecture suffix after its first
    underscore. When it does and the suffix is not this host's, no amount
    of fetching will make it run here, and saying so by name costs
    nothing where downloading half a gigabyte to find out costs plenty.
    """
    _, separator, suffix = name.partition(ARCH_SEPARATOR)
    if not separator or not suffix:
        return
    platform = platform or host_platform()
    if suffix != platform:
        raise PackageRegistryError(
            f"{name} is built for {suffix}, and this machine is {platform}.",
            hint=(
                f"drop the architecture from the pin and let MCUHome pick this "
                f"machine's package, or pin {name.split(ARCH_SEPARATOR)[0]}"
                f"{ARCH_SEPARATOR}{platform}"
            ),
        )


@dataclass(frozen=True)
class VerifiedIndex:
    """One source's package index, after it verified — and where it came from.

    :attr:`entries` is the whole source: the head document and every part
    it names, merged into ``name -> version -> entry``. A source that has
    outgrown one file keeps most of its packages in the parts, so a
    reader that stopped at the head would call most of the registry
    absent.
    """

    #: The source within the registry — ``sdk``, ``build-tools``, ….
    source: str
    #: The mirror the documents came from: an https base URL ending in
    #: ``/``, or a local directory path.
    base: str
    #: Where the verified copy of the documents is on this machine.
    tree: Path
    entries: Mapping[str, Mapping[str, Mapping[str, Any]]]
    #: ``False`` only for a registry the project marked untrusted.
    verified: bool = True

    def versions(self, name: str) -> tuple[str, ...]:
        """Every version of *name* this source publishes, unordered."""
        return tuple(self.entries.get(name, {}))

    def resolve(self, name: str, version: str, *, platform: str | None = None) -> ResolvedEntry:
        """:func:`resolve_entry` against this index."""
        return resolve_entry(self.entries, name, version, platform=platform)

    def pin(self, name: str, version: str, *, platform: str | None = None) -> PinnedEntry:
        """:func:`pin_entry` against this index."""
        return pin_entry(self.entries, name, version, platform=platform)

    def url_for(self, entry: ResolvedEntry | PinnedEntry) -> str:
        """Where this package's bytes are, on the mirror this index came from.

        Empty for an entry that maps to no single file — a meta entry
        names a package per platform, and a location that pointed at one
        of them would be a hint about the wrong bytes.
        """
        if not entry.file:
            return ""
        if _is_url(self.base):
            return f"{self.base}{entry.file}"
        return str(Path(self.base) / entry.file)


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------


class PackageRegistry:
    """One base domain, read through one of its mirrors.

    *opener* is the seam every test uses: it takes a URL and a timeout
    and answers with a readable binary stream, so the whole module runs
    without a network. A stream rather than bytes because the documents
    are kilobytes and the packages are hundreds of megabytes, and only
    one of those fits in memory as a matter of course. A local mirror
    never reaches the seam at all — its files are read off the
    filesystem, which is what makes an offline build possible.

    *anchor* is the key set every document is held against. ``None`` is
    allowed only together with *untrusted*, and then nothing is checked:
    the source is read as served, and every read says so through
    *on_warning*.
    """

    def __init__(
        self,
        base_domain: str,
        *,
        anchor: KeySet | None,
        into: Path,
        mirrors: Mapping[str, Sequence[str]] | None = None,
        untrusted: bool = False,
        opener: Callable[[str, float], IO[bytes]] | None = None,
        on_warning: Callable[[Diagnostic], None] | None = None,
        now: datetime | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        if anchor is None and not untrusted:
            raise TrustAnchorMissing(
                f"MCUHome has no trust anchor for the registry {base_domain}.",
                hint=f"save the registry's key set as secrets/trust-anchor/{base_domain}.json",
            )
        self.base_domain = _usable_domain(base_domain)
        self.untrusted = untrusted
        self._anchor = anchor
        self._into = Path(into)
        self._mirrors = {str(name): tuple(value) for name, value in (mirrors or {}).items()}
        self._open = opener if opener is not None else _http_open
        self._warn = on_warning
        self._now = now
        self._timeout = timeout
        self._indexes: dict[str, VerifiedIndex] = {}

    # -- mirrors -------------------------------------------------------

    def mirrors_for(self, source: str) -> tuple[str, ...]:
        """Where *source* can be read from, in the order to try.

        A configured override replaces the served list outright; without
        one the base domain is asked. That answer is not verified and
        does not need to be: it decides where to look, and what is found
        there is verified against the anchor either way. What *is*
        checked is its shape, because an unverified document is about to
        name URLs this client will fetch from.
        """
        override = self._mirrors.get(source)
        if override:
            # Already resolved where the environment was known — the
            # configuration layer expands and absolutises, this only
            # spells a URL as the base it is used as.
            return tuple(_normalised(location) for location in override)

        url = f"https://{self.base_domain}/{_usable_name(source, source)}/{MIRRORS_FILE}"
        document = _as_json(self._read_url(url), url)
        listed = document.get("mirrors")
        found: list[str] = []
        for mirror in listed if isinstance(listed, list) else []:
            location = str(mirror.get("url", "")) if isinstance(mirror, dict) else ""
            if not location.startswith("https://") or not location.endswith("/"):
                raise PackageRegistryError(
                    f"{self.base_domain} names {location!r} as a mirror of {source}, "
                    "which is not an https address ending in a slash.",
                    hint=(
                        "the registry's mirror list is malformed. Report it to the "
                        "registry's operator, or name a mirror yourself in mcuhome.yaml "
                        f"under registry.{self.base_domain}.mirrors.{source}"
                    ),
                )
            found.append(location)
        if not found:
            raise PackageRegistryError(
                f"{self.base_domain} names no mirror for {source}.",
                hint=(
                    "nothing can be fetched without one. Name a mirror yourself in "
                    f"mcuhome.yaml under registry.{self.base_domain}.mirrors.{source}, "
                    "or ask the registry's operator."
                ),
            )
        return tuple(found)

    # -- the index -----------------------------------------------------

    def index(self, source: str) -> VerifiedIndex:
        """*source*'s package index, verified, from the first mirror that serves it.

        Every mirror is tried in order and the first whose documents
        verify is the one consumed; a mirror that cannot be reached or
        whose copy does not verify is a reason to try the next, not to
        stop — that is the entire point of there being several. When none
        of them works, the refusal names what each one did.
        """
        held = self._indexes.get(source)
        if held is not None:
            # Memoized, but not quieter for it: an unverified registry
            # says so at every read, so the sentence cannot scroll away
            # once and never come back.
            self._loudly_unverified(source, held.base)
            return held

        problems: list[str] = []
        for mirror in self.mirrors_for(source):
            try:
                verified = self._read(source, mirror)
            except (PackageRegistryError, Refused, OSError) as failure:
                problems.append(f"  {mirror}\n    {failure}")
                continue
            self._indexes[source] = verified
            return verified

        detail = "\n".join(problems) or "  (no mirror was named)"
        raise PackageRegistryError(
            f"No mirror of {self.base_domain} served a usable copy of {source}.",
            hint=(
                f"every mirror was tried and none of them worked:\n{detail}\n"
                "Check the network, or name a mirror you can reach in mcuhome.yaml "
                f"under registry.{self.base_domain}.mirrors.{source}"
            ),
        )

    def _read(self, source: str, mirror: str) -> VerifiedIndex:
        """One mirror: lay the documents down, verify them, merge the entries."""
        if _is_url(mirror):
            tree = self._into / source / _slug(mirror)
            self._lay_down(mirror, tree)
        else:
            # A local mirror is verified where it is. Copying a
            # multi-gigabyte tree to check it would double the disk cost
            # of the one setup that exists because disks are what there
            # is instead of a network.
            tree = Path(mirror)
            if not (tree / INDEX_FILE).is_file():
                raise PackageRegistryError(
                    f"{tree} is not a package source: it has no {INDEX_FILE}.",
                    hint=(
                        "a local mirror is a directory laid out exactly like a served "
                        f"one — {KEYS_FILE}, {MIRRORS_FILE}, {INDEX_FILE}, their .sig "
                        "files, and the package archives"
                    ),
                )

        if self.untrusted or self._anchor is None:
            self._loudly_unverified(source, mirror)
        else:
            verify_source(tree, self._anchor, now=self._now)

        document = _as_json((tree / INDEX_FILE).read_bytes(), str(tree / INDEX_FILE))
        return VerifiedIndex(
            source=source,
            base=mirror if _is_url(mirror) else str(tree),
            tree=tree,
            entries=all_entries(tree, document),
            verified=not self.untrusted,
        )

    def _lay_down(self, mirror: str, tree: Path) -> None:
        """Fetch everything the verifier needs from *mirror* into *tree*.

        The head documents and their signatures, the index parts the head
        names, and the previous key sets a rotated source is reached
        through. Every name that comes out of an unverified document is
        held to being a plain file name in the source before it is used
        as one — the fetch happens before the verification by necessity,
        so the names get checked instead.

        An **untrusted** registry is allowed to publish no signatures at
        all, which is what makes "unsigned source" a thing this can read:
        only the index is then required, and the documents that exist
        only in order to be checked are fetched if they are there and
        skipped if they are not.
        """
        if tree.exists():
            shutil.rmtree(tree)
        tree.mkdir(parents=True)

        required = not self.untrusted
        for name in (KEYS_FILE, MIRRORS_FILE):
            self._fetch_into(mirror, name, tree, required=required)
            self._fetch_into(mirror, name + SIGNATURE_SUFFIX, tree, required=required)
        self._fetch_into(mirror, INDEX_FILE, tree)
        self._fetch_into(mirror, INDEX_FILE + SIGNATURE_SUFFIX, tree, required=required)

        index = _as_json((tree / INDEX_FILE).read_bytes(), mirror + INDEX_FILE)
        for part in index.get("parts") or []:
            name = str(part.get("file", "")) if isinstance(part, dict) else ""
            self._fetch_into(mirror, _usable_name(name, f"{mirror}: index part"), tree)

        if not (tree / KEYS_FILE).is_file():
            return
        seen: set[str] = set()
        previous = _as_json((tree / KEYS_FILE).read_bytes(), mirror + KEYS_FILE).get("previous")
        while isinstance(previous, str) and previous:
            if previous in seen or len(seen) >= _MAX_KEY_GENERATIONS:
                raise PackageRegistryError(
                    f"{mirror} chains its key sets in a circle, or past "
                    f"{_MAX_KEY_GENERATIONS} generations.",
                    hint="the source is damaged — try another mirror",
                )
            seen.add(previous)
            name = _usable_name(previous, f"{mirror}: key set")
            self._fetch_into(mirror, name, tree)
            self._fetch_into(mirror, name + SIGNATURE_SUFFIX, tree)
            previous = _as_json((tree / name).read_bytes(), mirror + name).get("previous")

    def _fetch_into(self, mirror: str, name: str, tree: Path, *, required: bool = True) -> None:
        target = tree / name
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = self._read_url(f"{mirror}{name}")
        except PackageRegistryError:
            if required:
                raise
            return
        target.write_bytes(payload)

    def _read_url(self, url: str) -> bytes:
        """One small document, whole. Only ever used for the signed set."""
        with self._stream(url) as answer:
            return answer.read()

    def _stream(self, url: str) -> IO[bytes]:
        try:
            return self._open(url, self._timeout)
        except PackageRegistryError:
            raise
        except (urllib.error.URLError, OSError) as unreachable:
            raise PackageRegistryError(
                f"MCUHome cannot reach {url}: {unreachable}.",
                hint=(
                    "the package registry is read over the network. Check the "
                    "connection, or point MCUHome at a local mirror in mcuhome.yaml "
                    f"under registry.{self.base_domain}.mirrors"
                ),
            ) from unreachable

    def _loudly_unverified(self, source: str, mirror: str) -> None:
        """Say it, every time, for as long as the setting stands.

        Once at the top of a build would be a footnote. This is not a
        footnote: nothing in the chain below it means anything while the
        setting is on, and the person reading the log is the only
        remaining check.
        """
        if not self.untrusted and self._anchor is not None:
            return
        ignored = (
            " The trust anchor configured for it is deliberately ignored."
            if self._anchor is not None
            else ""
        )
        message = (
            f"NOTHING IS VERIFIED: {source} is being read from {mirror} without "
            f"checking any signature, because {self.base_domain} is configured as "
            f"untrusted.{ignored} Whatever that host serves is what this build "
            f"will use."
        )
        if self._warn is None:
            _LOG.warning(message)
        else:
            self._warn(
                Diagnostic.warning(
                    message,
                    kind="unverified_registry",
                    location=Location(key=f"registry.{self.base_domain}.untrusted"),
                    hint=(
                        f"nothing below this is checked while the setting stands. "
                        f"Drop `untrusted: true` under registry.{self.base_domain} "
                        f"in the configuration file that sets it, and put the key "
                        f"set the registry's operator publishes in "
                        f"secrets/trust-anchor/ instead."
                    ),
                )
            )

    # -- the bytes -----------------------------------------------------

    def fetch_meta(self, index: VerifiedIndex, meta: MetaFile) -> bytes:
        """One package's ``meta.json`` from the mirror *index* came from.

        The same mirror, the same anchor, the same arithmetic as an
        archive: the bytes are held against the size and the sha256 the
        **verified index** records, so a document that decides which
        version of the next stage a build resolves to is trusted for
        exactly as much as the archive beside it. Whole in memory rather
        than streamed to disk, because a meta file is kilobytes and the
        caller parses it immediately.
        """
        self._loudly_unverified(index.source, index.base)
        name = _usable_name(meta.file, "the package meta file")
        if _is_url(index.base):
            with self._stream(f"{index.base}{name}") as answer:
                payload = answer.read(meta.size + 1)
        else:
            origin = Path(index.base) / name
            if not origin.is_file():
                raise PackageRegistryError(
                    f"{origin} is missing from this mirror.",
                    hint=(
                        "the mirror's index records a meta file beside the archive and "
                        "the file is not there — the copy is incomplete. Synchronise "
                        "it again."
                    ),
                )
            payload = origin.read_bytes()
        return check_meta_bytes(payload, meta, where=index.base)

    def fetch_package(self, index: VerifiedIndex, entry: ResolvedEntry, *, into: Path) -> Path:
        """*entry*'s archive from the mirror *index* came from, into *into*.

        The bytes are hashed as they land and held against the size and
        the sha256 the verified index records; a file that does not match
        is deleted rather than handed back. That check is what makes the
        transport irrelevant — any wire, any cache, any USB stick — and
        it is why a local mirror is read exactly as a remote one is
        fetched, through the same arithmetic.
        """
        self._loudly_unverified(index.source, index.base)
        into = Path(into)
        into.mkdir(parents=True, exist_ok=True)
        target = into / _usable_name(entry.file, f"{entry.name} {entry.version}")

        if _is_url(index.base):
            source: IO[bytes] = self._stream(f"{index.base}{entry.file}")
        else:
            origin = Path(index.base) / entry.file
            if not origin.is_file():
                raise PackageRegistryError(
                    f"{origin} is missing from this mirror.",
                    hint=(
                        f"the mirror's index says it publishes {entry.name} "
                        f"{entry.version} and the file is not there — the copy is "
                        "incomplete. Synchronise it again."
                    ),
                )
            source = origin.open("rb")

        digest = hashlib.sha256()
        written = 0
        try:
            with source, target.open("wb") as handle:
                while block := source.read(_BLOCK):
                    written += len(block)
                    if written > entry.size:
                        break
                    digest.update(block)
                    handle.write(block)
        except OSError as broken:
            target.unlink(missing_ok=True)
            raise PackageRegistryError(
                f"{entry.file} could not be read from {index.base}: {broken}.",
                hint="the mirror's copy is incomplete or unreadable — try another one",
            ) from broken

        if written != entry.size:
            target.unlink(missing_ok=True)
            raise PackageRegistryError(
                f"{entry.file} is {written} bytes and the index says {entry.size}.",
                hint="the copy is incomplete, or the mirror serves something else",
            )
        measured = digest.hexdigest()
        if measured != entry.sha256:
            target.unlink(missing_ok=True)
            raise PackageRegistryError(
                f"{entry.file} hashes to {measured} and the index says {entry.sha256}.",
                hint=(
                    "these are not the bytes the registry signed for. Try another "
                    "mirror; if every mirror says this, tell the registry's operator."
                ),
            )
        return target


def registry_for(
    base_domain: str,
    *,
    project_root: Path,
    settings: Sequence[RegistrySettings] = (),
    into: Path,
    opener: Callable[[str, float], IO[bytes]] | None = None,
    on_warning: Callable[[Diagnostic], None] | None = None,
    now: datetime | None = None,
) -> PackageRegistry:
    """A registry ready to read *base_domain*, from a project and its settings.

    The one place the four decisions come together: what the project
    configured for this domain, which anchor file backs it — read, never
    written, because a project's trust roots are installed when the
    project is created and by nothing else — whether the project accepts
    an unsigned source, and which mirrors to ask. Everything below is
    mechanical; everything above it just needs a registry it can read.
    """
    configured = settings_for(settings, base_domain)
    anchor_path = trust_anchor_for(
        project_root,
        base_domain,
        untrusted=configured.untrusted,
        stated=configured.anchor,
        on_warning=on_warning,
    )
    # An anchor next to `untrusted: true` is read but never used: the
    # project said it does not want this registry checked, and honouring
    # a file over that statement would make the setting mean two things.
    # It is loaded anyway so the warning can say it is being ignored.
    anchor = None if anchor_path is None else load_trust_anchor(anchor_path)
    return PackageRegistry(
        base_domain,
        anchor=anchor,
        into=into,
        mirrors=configured.mirrors,
        untrusted=configured.untrusted or anchor is None,
        opener=opener,
        on_warning=on_warning,
        now=now,
    )


def open_package_registry(
    base_domain: str,
    *,
    project_root: Path,
    settings: Sequence[RegistrySettings] = (),
    into: Path,
    opener: Callable[[str, float], IO[bytes]] | None = None,
    on_warning: Callable[[Diagnostic], None] | None = None,
    now: datetime | None = None,
) -> RegistrySource:
    """:func:`registry_for`, deferred until something actually needs it.

    A build that finds its packages in the operator's own directories
    never reads a registry, and it must not be stopped by one either:
    refusing it for a missing trust anchor would turn "you have not
    decided who to trust" into "you cannot build offline", which is the
    opposite of what an anchor is for. So the anchor is read, and a
    missing one refused, at the first question actually asked of the
    registry — and never on a build that had no question.

    The registry is built once and reused, so a build that asks twice
    gets one anchor read and one set of documents.
    """
    held: list[PackageRegistry] = []

    def build() -> PackageRegistry:
        if not held:
            held.append(
                registry_for(
                    base_domain,
                    project_root=project_root,
                    settings=settings,
                    into=into,
                    opener=opener,
                    on_warning=on_warning,
                    now=now,
                )
            )
        return held[0]

    return build


def registry_opener(
    *,
    project_root: Path,
    settings: Sequence[RegistrySettings] = (),
    into: Path,
    opener: Callable[[str, float], IO[bytes]] | None = None,
    on_warning: Callable[[Diagnostic], None] | None = None,
    now: datetime | None = None,
) -> Callable[[str], PackageRegistry]:
    """:func:`registry_for` for whichever base domain is asked for, once each.

    A build reads one host almost always, and the exception is the reason
    this exists: a device may point one package at another registry, and
    that registry has a trust anchor and mirrors of its own — per base
    domain, which is what an anchor *is* per. So the answer cannot be one
    client; it is a function from a domain to its client, with each one
    built at the first question asked of it and kept for the rest of the
    build.

    Each domain lays its documents down in its own directory under
    *into*, so two registries that happen to publish a source of the same
    name cannot read each other's copies.
    """
    held: dict[str, PackageRegistry] = {}

    def open_for(base_domain: str) -> PackageRegistry:
        client = held.get(base_domain)
        if client is None:
            client = registry_for(
                base_domain,
                project_root=project_root,
                settings=settings,
                into=Path(into) / _usable_domain(base_domain),
                opener=opener,
                on_warning=on_warning,
                now=now,
            )
            held[base_domain] = client
        return client

    return open_for


def opened(source: RegistrySource | None) -> PackageRegistry | None:
    """A registry, built now if it was only promised. ``None`` stays ``None``."""
    if source is None or isinstance(source, PackageRegistry):
        return source
    return source()


#: What a caller may hand to anything that *might* need a registry: one,
#: or a promise of one (:func:`open_package_registry`). The promise is the
#: ordinary case — see there for why a build that needs no registry must
#: not pay for one.
RegistrySource = PackageRegistry | Callable[[], PackageRegistry]


def _http_open(url: str, timeout: float) -> IO[bytes]:
    """The real HTTP GET, as a stream. No authentication, and nothing sent."""
    request = urllib.request.Request(url, method="GET")  # noqa: S310
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310


def _as_json(payload: bytes, what: str) -> dict[str, Any]:
    try:
        document = json.loads(payload)
    except ValueError as broken:
        raise PackageRegistryError(
            f"{what} is not readable as JSON: {broken}.",
            hint="the host served something that is not a registry document",
        ) from broken
    if not isinstance(document, dict):
        raise PackageRegistryError(
            f"{what} is not a registry document.",
            hint="the host served something else — a redirect page, most likely",
        )
    return document


def _usable_name(name: str, what: str) -> str:
    """A name out of an unverified document, held to being a file in the source."""
    usable = (
        name
        and not name.startswith(".")
        and "/" not in name
        and "\\" not in name
        and "\x00" not in name
    )
    if not usable:
        raise PackageRegistryError(
            f"{what} names {name!r}, which is not a file name.",
            hint="the registry document is malformed — try another mirror",
        )
    return name


def _slug(location: str) -> str:
    """A mirror location as one directory name, reversibly enough to read."""
    return "".join(
        character if character.isalnum() or character in "._-" else "_" for character in location
    )
