# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Finding the container image that delivers a pinned package set.

A build context pins the build environment by its **packages**, and a
container image is one delivery of such a set: the build environment
specification (§5.2) has every image repeat its package members as
``org.mcuhome.build-environment.packages.<package>`` labels, with a hash
on each, and says plainly what follows — "a build context references
packages, never an image; the orchestrator looks for the image whose
package labels are the set it wants and starts it."

This is that lookup, and it is the only way the container profile
chooses what to run.

**Exactly, not compatibly.** A match means the image's ``packages.``
labels state the wanted set and nothing else: every member present, every
hash equal, and no member the set does not name. An image assembled from
one more package is a different environment even if the extra one changes
nothing, because "the environment is its package set" is only a statement
about identity if a superset is a different set.

**Tags are locations.** The match is over labels, so the tags are only the
list of candidates to look at — an image that answers is pinned by its
digest, never by the tag it was found under. Where the search list names
several repositories they are searched in order, and the first image whose
labels are the set wins.

**Where several candidates in one repository declare the set**, the
highest assembly revision wins: MCUHome tags an image
``<workspace-package-version>-r<n>``, and "newest unless pinned" means
the highest ``-r<n>``. That is a preference and never a correctness
gate — the labels decide what an image *is*, and two tags declaring the
same set are the same environment whichever is taken. Candidates of
equal revision are therefore tried in ascending byte order of the tag,
so that the choice is at least the same one twice.

**A pin narrows the candidates and never widens them.** Whatever form it
takes, the labels of what it names are still checked against the package
set, and a mismatch is refused rather than built.

**Why this costs a tag listing.** A registry cannot be queried by label:
the labels are in each image's configuration blob, which is one request
per candidate. So the cost is one tag listing plus one label read per tag
until a match — which is why a pin that names a tag or a digest is worth
having, and why the caller decides how wide the search list is.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from mcuhome.model.buildenvironment import (
    ENVIRONMENT_IMAGE_REPOSITORY,
    PACKAGE_MEMBER_PREFIX,
    Declaration,
    PackageMember,
    declaration_from_labels,
)
from mcuhome.model.errors import BuildError
from mcuhome.model.imageref import DOCKER_HUB, Reference, parse_reference

from mcuhome.workbench.ociregistry import ImageFacts, Registry, RegistryError

__all__ = [
    "ImageMatch",
    "ImagePin",
    "declares_exactly",
    "image_for_packages",
    "parse_image_pin",
    "revision_of",
]

#: The image-assembly revision at the end of a tag, as the container
#: image naming scheme writes it: the package version, then ``-r<n>``,
#: optionally followed by a platform suffix (``…-r1-amd64``). A tag that
#: carries none is not refused — it is simply the oldest candidate.
_REVISION = re.compile(r"-r(\d+)(?:-[A-Za-z0-9._-]+)?\Z")


@dataclass(frozen=True)
class ImageMatch:
    """One image that delivers the wanted package set.

    :attr:`reference` carries the digest — that is what runs.
    :attr:`found_under` is the tag it was reached through, which is a
    location and worth a log line rather than a decision.
    """

    reference: Reference
    declaration: Declaration
    found_under: str


@dataclass(frozen=True)
class ImagePin:
    """What a caller pinned, in one of the forms it may be written in.

    The four forms of the build-environment image pin, and each of them
    narrows the search differently:

    ``ImagePin()``
        Nothing pinned. The configured repositories are searched in
        order, newest revision first.
    :attr:`repository` alone — a bare name
        That repository is searched, in place of the configured list,
        and no other.
    :attr:`tag` alone (written ``:<tag>``) or :attr:`digest` alone
        (written ``@sha256:…``)
        The configured repositories are searched in order, but only that
        one name is looked at in each.
    :attr:`repository` with a tag or a digest — the canonical form
        Exactly one candidate.

    The labels are checked in every one of them: a pin says *which* image
    to look at, never that it may be run without being what it claims.
    """

    repository: str = ""
    tag: str = ""
    digest: str = ""

    @property
    def stated(self) -> bool:
        """Whether anything was pinned at all."""
        return bool(self.repository or self.tag or self.digest)

    @property
    def canonical(self) -> bool:
        """Whether this pin names exactly one image."""
        return bool(self.repository) and bool(self.tag or self.digest)

    def described(self) -> str:
        """The pin as it was written, for a refusal that quotes it back."""
        if not self.stated:
            return ""
        if self.digest and self.repository:
            return f"{self.repository}@{self.digest}"
        if self.tag and self.repository:
            return f"{self.repository}:{self.tag}"
        return self.repository or self.tag or self.digest


def parse_image_pin(text: str | None) -> ImagePin:
    """Read one of the four pin forms out of *text*.

    The forms are told apart by what the value **starts** with, so that
    nothing has to be guessed from a name that could be either:

    * empty — nothing pinned;
    * ``@sha256:…`` — a digest, looked for in whatever repositories are
      searched;
    * ``:<tag>`` — a tag, in the same repositories (``:0.1.10.dev2-r1``);
    * anything else — a repository, with the tag or digest it carries:
      ``ghcr.io/mcu-home/build-environment``,
      ``…/build-environment:0.1.10.dev2-r1`` or ``…@sha256:…``.

    The leading marker is what makes a bare word unambiguous: written
    plainly it is a repository, and a tag or a digest of its own says so
    with the character a reference separates it by anyway.
    """
    stated = (text or "").strip()
    if not stated:
        return ImagePin()
    if stated.startswith("@"):
        digest = stated[1:]
        parse_reference(
            f"{ENVIRONMENT_IMAGE_REPOSITORY}@{digest}",
            default_registry=DOCKER_HUB,
            what="build environment",
        )
        return ImagePin(digest=digest)
    if stated.startswith(":"):
        tag = stated[1:]
        # Checked as a tag by parsing it in a reference, so that a
        # refusal names the part that is wrong.
        parse_reference(
            f"{ENVIRONMENT_IMAGE_REPOSITORY}:{tag}",
            default_registry=DOCKER_HUB,
            what="build environment",
        )
        return ImagePin(tag=tag)
    reference = parse_reference(stated, default_registry=DOCKER_HUB, what="build environment")
    return ImagePin(
        repository=reference.repository,
        tag=reference.tag or "",
        digest=reference.digest or "",
    )


def revision_of(tag: str) -> int:
    """The assembly revision *tag* states, or ``-1`` where it states none.

    ``-1`` rather than ``0`` because an image tagged ``-r0`` is a real
    thing and a tag that carries no revision at all is not the same
    statement.
    """
    found = _REVISION.search(tag)
    return int(found.group(1)) if found else -1


def declares_exactly(labels: Mapping[str, str], wanted: Mapping[str, PackageMember]) -> bool:
    """Whether *labels* state exactly the package set *wanted*.

    Three ways to be wrong, all of them a no: a member missing, a member
    the set does not name, and a member whose hash differs. The last is
    the one worth naming — an image built from the same package
    *versions* but different bytes carries the same names and other
    hashes, and it is a different environment.

    An image whose members carry no hash at all does not match either. A
    delivery **must** state one (§5.1); a set of names and versions is
    the abstract declaration a package's own metadata carries, and
    matching against it would accept any bytes ever published under those
    numbers.
    """
    stated = _stated_packages(labels)
    if set(stated) != set(wanted):
        return False
    for name, value in stated.items():
        member = wanted[name]
        if member.sha256 is None:
            return False
        if value != f"{member.version}@sha256:{member.sha256}":
            return False
    return True


def _stated_packages(labels: Mapping[str, str]) -> dict[str, str]:
    """The ``packages.`` members of a declaration, by package name."""
    return {
        key[len(PACKAGE_MEMBER_PREFIX) :]: value
        for key, value in _members(labels).items()
        if key.startswith(PACKAGE_MEMBER_PREFIX)
    }


def _members(labels: Mapping[str, str]) -> dict[str, str]:
    """The declaration members an image's labels mirror (§5.2)."""
    from mcuhome.model.buildenvironment import LABEL_PREFIX

    return {
        key[len(LABEL_PREFIX) :]: value
        for key, value in labels.items()
        if key.startswith(LABEL_PREFIX)
    }


def image_for_packages(
    packages: Mapping[str, PackageMember],
    *,
    registry: Registry | None = None,
    repositories: Sequence[str] = (ENVIRONMENT_IMAGE_REPOSITORY,),
    pin: ImagePin | None = None,
    platform: str | None = None,
) -> ImageMatch:
    """The image whose ``packages.`` labels are exactly *packages*.

    *packages* is the **concrete** set — one platform's packages, hashes
    and all — because that is what an image contains and therefore what
    it declares. A context's family pin is resolved to this host's
    package before it gets here.

    *repositories* is the operator's search list, walked in order. It is
    a parameter and not a lookup: which repositories a build may take an
    environment from is a decision about trust, and a resolver that
    searched wherever it liked would be making it.

    *pin* narrows what is looked at (:class:`ImagePin`) and never what is
    accepted.

    *platform* is the host the image has to run on, in the package
    index's spelling (``linux-amd64``). It is the same name the package
    pins were resolved against, and it decides which manifest of a
    multi-architecture image is read and pinned — the labels of an image
    are per platform, because the tools package is.

    *registry* is the seam a test replaces; left ``None`` it talks to the
    real registries.

    Raises a typed refusal when no candidate declares the set — with
    every candidate that was tried and the reason it was rejected,
    including the near miss the labels make visible: an image with the
    same package names under other hashes, which is a different
    environment and never a fallback.
    """
    pin = pin or ImagePin()
    searched = list(repositories) if not pin.repository else [pin.repository]
    if not searched:
        raise BuildError(
            "No container repository is allowed to deliver this build environment.",
            hint=(
                "an image is chosen from the repositories the project allows — "
                "configure at least one, or build outside a container"
            ),
        )
    client = registry if registry is not None else Registry()
    rejected: list[str] = []
    for repository in searched:
        reference = parse_reference(
            repository, default_registry=DOCKER_HUB, what="build environment"
        )
        for candidate in _candidates(client, reference, pin, rejected):
            facts = _facts_of(client, candidate, platform, rejected)
            if facts is None:
                continue
            if not declares_exactly(facts.labels, packages):
                rejected.append(f"{candidate} declares {_described(facts.labels)}")
                continue
            # The digest of the manifest whose labels were just checked —
            # for a multi-architecture image that is this host's manifest
            # and not the index that lists it.
            return ImageMatch(
                reference=candidate.with_digest(facts.digest),
                declaration=declaration_from_labels(facts.labels),
                found_under=candidate.tag or "",
            )
    raise _no_image_declares(packages, pin, searched, rejected)


def _candidates(
    client: Registry, reference: Reference, pin: ImagePin, rejected: list[str]
) -> list[Reference]:
    """Which images of *reference*'s repository are worth a label read.

    A pinned digest or tag is one candidate and costs no listing. Without
    one, the repository's tags are listed and ordered newest first — the
    highest ``-r<n>``, then the tag itself, so that the order is a fact
    rather than the registry's mood.
    """
    if pin.digest:
        return [replace(reference, tag=None, digest=pin.digest)]
    if pin.tag:
        return [replace(reference, tag=pin.tag, digest=None)]
    try:
        tags = client.tags(reference)
    except RegistryError as unreachable:
        # One unreachable repository is not the end of the search: a
        # search list exists to have alternatives in it.
        rejected.append(f"{reference.repository} could not be asked ({unreachable})")
        return []
    if not tags:
        rejected.append(f"{reference.repository} publishes no image")
    return [
        replace(reference, tag=tag, digest=None)
        for tag in sorted(tags, key=lambda tag: (-revision_of(tag), tag))
    ]


def _facts_of(
    client: Registry, candidate: Reference, platform: str | None, rejected: list[str]
) -> ImageFacts | None:
    """This host's manifest of *candidate* and its labels, or a reason why not.

    An image published for other architectures only lands here too: the
    registry refuses to pick a foreign manifest, and that refusal is one
    more candidate rejected rather than the end of the search.
    """
    try:
        return client.facts(candidate, platform=platform)
    except RegistryError as unreadable:
        rejected.append(f"{candidate} could not be read ({unreadable})")
        return None


def _described(labels: Mapping[str, str]) -> str:
    """What an image says it is made of, for a refusal to print."""
    stated = _stated_packages(labels)
    if not stated:
        return "no build environment packages"
    return ", ".join(f"{name} {value}" for name, value in sorted(stated.items()))


def _no_image_declares(
    packages: Mapping[str, PackageMember],
    pin: ImagePin,
    searched: Sequence[str],
    rejected: Sequence[str],
) -> BuildError:
    """The typed refusal, after every candidate was tried.

    It lists what was wanted, what was looked at and why each candidate
    was not it, because the three together are what a person needs: an
    image built from the same versions but other bytes and an image that
    was never published read identically in a one-line message and have
    nothing else in common.
    """
    wanted = ", ".join(f"{name} {member.value()}" for name, member in sorted(packages.items()))
    tried = "\n".join(f"    {reason}" for reason in rejected) or "    nothing was found to try"
    if pin.canonical:
        where = f"{pin.described()} does not declare it"
        fix = (
            "the image this build was pinned to is a different environment. Drop the "
            "pin to search the configured repositories, or pin one that declares "
            "these packages."
        )
    else:
        where = f"none of {', '.join(searched)} declares it"
        fix = (
            "an image built from the same versions but other bytes is a different "
            "environment and is not used instead. Publish an image for this set, "
            "point build.container_repositories at the repository that has one, or "
            "build without a container."
        )
    return BuildError(
        "No container image declares the build environment this build needs.",
        hint=(
            f"the build needs an image assembled from exactly {wanted}, and {where}. "
            f"Tried:\n{tried}\n{fix}"
        ),
    )
