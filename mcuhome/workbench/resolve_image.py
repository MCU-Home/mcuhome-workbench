# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Finding the container image that delivers a pinned package set.

A build context pins the build environment by its **packages**, and a
container image is one delivery of such a set: the specification (§5.2)
has every image repeat its package members as
``org.mcuhome.build-environment.packages.<package>`` labels, with a hash
on each, and says plainly what follows — "a build context references
packages, never an image; the orchestrator looks for the image whose
package labels are the set it wants and starts it."

This is that lookup. It is the inverse of
:mod:`mcuhome.workbench.resolve_env`, which picks an image by the Zephyr
release it declares and is what the container profile still uses; this
one picks an image by *being* the package set, which is what the profile
switches to once it runs the package-built image.

**Exactly, not compatibly.** A match means the image's ``packages.``
labels state the wanted set and nothing else: every member present, every
hash equal, and no member the set does not name. An image assembled from
one more package is a different environment even if the extra one changes
nothing, because "the environment is its package set" is only a statement
about identity if a superset is a different set.

**Tags are locations.** The match is over labels, so the tags are only the
list of candidates to look at — an image that answers is pinned by its
digest, never by the tag it was found under. Where the allowlist names
several repositories they are searched in order, and the first image whose
labels are the set wins.

**Why this costs a tag listing.** A registry cannot be queried by label:
the labels are in each image's configuration blob, which is one request
per candidate. So the cost is one tag listing plus one label read per tag
until a match, and the caller decides how wide the allowlist is. The
container profile's other resolver avoids the listing with a moving tag
whose name encodes the Zephyr release; there is no equivalent name for a
package set, because a set is not a single number.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from mcuhome.model.buildenvironment import (
    PACKAGE_MEMBER_PREFIX,
    Declaration,
    PackageMember,
    declaration_from_labels,
)
from mcuhome.model.buildimage import ENVIRONMENT_IMAGE_REPOSITORY
from mcuhome.model.errors import BuildError
from mcuhome.model.imageref import DOCKER_HUB, Reference, parse_reference

from mcuhome.workbench.ociregistry import Registry, RegistryError

__all__ = [
    "ImageMatch",
    "declares_exactly",
    "image_for_packages",
]


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
    stated = {
        key[len(PACKAGE_MEMBER_PREFIX) :]: value
        for key, value in _members(labels).items()
        if key.startswith(PACKAGE_MEMBER_PREFIX)
    }
    if set(stated) != set(wanted):
        return False
    for name, value in stated.items():
        member = wanted[name]
        if member.sha256 is None:
            return False
        if value != f"{member.version}@sha256:{member.sha256}":
            return False
    return True


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
) -> ImageMatch:
    """The image whose ``packages.`` labels are exactly *packages*.

    *packages* is the **concrete** set — one platform's packages, hashes
    and all — because that is what an image contains and therefore what
    it declares. A context's family pin is resolved to this host's
    package before it gets here.

    *repositories* is the operator's allowlist, searched in order. It is
    a parameter and not a lookup: which repositories a build may take an
    environment from is a decision about trust, and a resolver that
    searched wherever it liked would be making it.

    *registry* is the seam a test replaces; left ``None`` it talks to the
    real registries.

    Raises a typed refusal when no image in the allowlist declares the
    set — including the near miss the labels make visible, an image with
    the same package names under other hashes, which is a different
    environment and never a fallback.
    """
    if not repositories:
        raise BuildError(
            "No container repository is allowed to deliver this build environment.",
            hint=(
                "an image is chosen from the repositories the project allows — "
                "configure at least one, or build outside a container"
            ),
        )
    client = registry if registry is not None else Registry()
    searched: list[str] = []
    for repository in repositories:
        reference = parse_reference(
            repository, default_registry=DOCKER_HUB, what="build environment"
        )
        searched.append(str(reference))
        try:
            tags = client.tags(reference)
        except RegistryError:
            # One unreachable repository is not the end of the search:
            # an allowlist exists to have alternatives in it.
            continue
        for tag in tags:
            candidate = replace(reference, tag=tag, digest=None)
            try:
                labels = client.labels(candidate)
            except RegistryError:
                continue
            if not declares_exactly(labels, packages):
                continue
            digest = client.digest_of(candidate)
            if digest is None:  # pragma: no cover - a tag that vanished mid-search
                continue
            return ImageMatch(
                reference=candidate.with_digest(digest),
                declaration=declaration_from_labels(labels),
                found_under=tag,
            )
    wanted = ", ".join(f"{name} {member.value()}" for name, member in sorted(packages.items()))
    raise BuildError(
        "No build container delivers this build environment.",
        hint=(
            f"the build needs an image assembled from exactly {wanted}, and none of "
            f"{', '.join(searched)} declares it. An image built from the same "
            "versions but other bytes is a different environment and is not used "
            "instead. Provision the packages and build outside a container, or "
            "publish an image for this set."
        ),
    )
