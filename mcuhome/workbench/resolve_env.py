# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Resolving a build-environment reference to the one image a build runs.

A device names its build environment the way anyone names a container —
``ghcr.io/mcu-home/build-container``, optionally with a tag, optionally
with a digest — and states, through its SDK, which Zephyr it needs. This
turns the two into **one pinned reference**: the same repository with a
digest, checked against what the image says about itself.

**The publisher recommends, we verify.** A repository of build
environments carries immutable revision tags (``zephyr-4.4.0-r10``) and,
beside them, moving ``-latest`` tags per Zephyr release. We choose the
*release* — the SDK knows what it needs — and the publisher chooses the
*revision*, because he is the one who knows his container was fixed
yesterday. Revisions are therefore never sorted here; following his tag
is the whole selection.

The tag is only a cheap prefilter, though. **The labels are the
authority** (:data:`~mcuhome.model.buildimage.REQUIRED_LABELS`): a
registry cannot be queried by label, so a tag narrows the field and the
image's own declaration decides. An image whose labels do not satisfy the
constraint is not built in, whatever it is called — and an image carrying
no label at all does not qualify either, because absence is never read as
compatible.

The resolution path, in the order it is walked:

1. A reference that already **names a digest** is the answer; its labels
   are still checked, because a pin is not a permission — but they are
   read off **this host** when the image is here, so a pinned build needs
   no network at all. That is what makes a digest the answer for an
   air-gapped machine as well as for a reproducible one.
2. A reference that names a **tag** is resolved to that tag's digest.
   Also the ``--container-image`` case: whoever names one has decided.
3. A **bare repository** goes to the ladder. The constraint implies a
   version prefix — ``~=4.4.0`` admits exactly the ``4.4`` releases — and
   ``zephyr-<prefix>-latest`` is one request away. Where that tag exists
   and its labels satisfy the constraint, the resolution is done in two
   requests and **never lists tags at all**, which matters because a tag
   listing is paginated, rate limited and expensive on a large repository.
3b. Otherwise the tags are listed, the ``zephyr-<X.Y.Z>-latest`` ones
    filtered, the highest version satisfying the constraint tried first.
    That is also the path an aggregate tag that points too low lands in:
    ``zephyr-4.4-latest`` cannot know a constraint's lower bound.

**An image that exists only on this machine is resolved on this
machine.** A repository has no tag for a container somebody just built,
and neither has it for one CI built for a pull request — so a reference
naming a tag falls back to the local image of that name, and pins it by
the only identity it has: its own image ID. Such a pin is honest rather
than portable, which is exactly right, because those bytes are not
fetchable anywhere and a build server told to use them will say so.

Two builds of one configuration on two days may therefore get two
images. That is deliberate — it is how a security-fixed container reaches
existing devices — and it is why a resolution is worth **saying out
loud**: the context records the digest, so any one build stays
reproducible forever, but the configuration alone does not name it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mcuhome.model.buildimage import (
    CONTRACT_LABEL,
    LATEST_SUFFIX,
    TOOLCHAIN_LABEL,
    ZEPHYR_LABEL,
)
from mcuhome.model.context import EnvironmentPin
from mcuhome.model.errors import BuildError
from mcuhome.model.imageref import DOCKER_HUB, Reference, parse_reference
from mcuhome.model.invocation import CONTRACT_VERSION
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from mcuhome.workbench.ociregistry import Registry, RegistryError

__all__ = [
    "LocalLookup",
    "ResolvedEnvironment",
    "implied_tag",
    "resolve_environment",
]

#: Asks this host about an image by name and answers with its facts, or
#: ``None`` when it has none. Structural on purpose: what it is given is
#: :meth:`mcuhome.workbench.orchestrator.Docker.inspect`, and naming that
#: type here would pull the whole container driver into a module whose
#: subject is a registry.
LocalLookup = Callable[[str], Any]


@dataclass(frozen=True)
class ResolvedEnvironment:
    """One build environment, pinned and vouched for.

    :attr:`pin` is what a build context records and what a build runs.
    :attr:`zephyr` and :attr:`toolchain` are the image's own declarations,
    kept because a build report that cannot say what compiled the bytes is
    worth less than carrying two strings costs. :attr:`found_under` is the
    tag the digest was reached through, or ``""`` when the reference
    already named the digest — the difference between "this is what the
    publisher currently recommends" and "this is what you asked for".
    """

    reference: Reference
    zephyr: str
    toolchain: str
    found_under: str = ""

    @property
    def pin(self) -> EnvironmentPin:
        return EnvironmentPin(reference=str(self.reference))

    @property
    def digest(self) -> str:
        return self.reference.digest or ""


def resolve_environment(
    stated: str,
    *,
    constraint: str,
    registry: Registry | None = None,
    local: LocalLookup | None = None,
) -> ResolvedEnvironment:
    """Pin *stated* to one image whose labels satisfy *constraint*.

    *stated* is the reference as a device model carries it; *constraint*
    is the PEP 440 Zephyr requirement the model resolved
    (``toolchain.zephyr_constraint``). *registry* is the seam: a test
    passes a scripted one and never touches a network. *local* is how
    this host is asked about an image a registry does not have — passed
    by a build that runs here, left out by one that sends its context
    somewhere else, because a machine's own images are not an answer for
    somebody else's builder.

    Raises a typed :class:`~mcuhome.model.errors.BuildError` — never a
    traceback and never a silent fallback — when the repository offers
    nothing satisfying the constraint, when the image that answers does
    not declare itself, or when the registry cannot be read.
    """
    reference = parse_reference(stated, default_registry=DOCKER_HUB, what="build environment")
    specifier = _specifier(constraint)
    client = registry if registry is not None else Registry()

    if reference.pinned:
        # This host first, and only then the registry: the labels are the
        # image's own either way, and an image that is already here is a
        # build that does not need a network.
        found = _from_this_host(reference, local, specifier=specifier, constraint=constraint)
        if found is not None:
            return found
        labels = client.labels(reference)
        _require_labels(reference, labels, specifier=specifier, constraint=constraint, stated=True)
        return _resolved(reference, labels, found_under="")

    if reference.tag:
        try:
            digest = client.digest_of(reference)
        except RegistryError:
            # A registry that cannot be read is not the end of it when the
            # image may be sitting here: re-raised below only if it is not.
            found = _from_this_host(reference, local, specifier=specifier, constraint=constraint)
            if found is None:
                raise
            return found
        if digest is None:
            found = _from_this_host(reference, local, specifier=specifier, constraint=constraint)
            if found is not None:
                return found
            raise BuildError(
                f"The build environment {reference} does not exist.",
                hint=(
                    f"{reference.repository} has no tag {reference.tag}, and this "
                    "machine has no image of that name either. Check the spelling, or "
                    "name the repository without a tag and let MCUHome choose the "
                    "environment its SDK needs."
                ),
            )
        pinned = reference.with_digest(digest)
        labels = client.labels(pinned)
        _require_labels(pinned, labels, specifier=specifier, constraint=constraint, stated=True)
        return _resolved(pinned, labels, found_under=reference.tag)

    return _follow_the_ladder(reference, client, specifier=specifier, constraint=constraint)


def _from_this_host(
    reference: Reference,
    local: LocalLookup | None,
    *,
    specifier: SpecifierSet,
    constraint: str,
) -> ResolvedEnvironment | None:
    """The image of that name sitting on this machine, pinned by its own ID.

    ``None`` when there is no such image or nobody offered to look, which
    is what lets the caller keep its registry-shaped refusal. When there
    *is* one, its labels are checked exactly as a fetched image's are:
    being local buys an image no exemption from saying what it carries.

    The digest is its ID rather than a repository digest, because a
    repository digest is a registry's name for bytes and no registry has
    named these. A context pinned this way is reproducible on this
    machine and nowhere else, which is a true statement about a container
    somebody built by hand.
    """
    if local is None:
        return None
    facts = local(str(reference)) or (local(reference.digest) if reference.digest else None)
    if facts is None:
        return None
    labels = dict(getattr(facts, "labels", {}) or {})
    digest = getattr(facts, "digest", None) or getattr(facts, "image_id", "")
    if not digest:
        raise BuildError(
            f"The image {reference} on this machine has no identity to pin.",
            hint=(
                "a build context names the exact bytes it is compiled in, and this "
                "image reports neither a repository digest nor an ID. Rebuild or "
                "re-pull it."
            ),
        )
    # A reference that already carried a digest keeps it: this host is
    # being asked to confirm a pin, not to make one.
    pinned = reference if reference.pinned else reference.with_digest(digest)
    _require_labels(pinned, labels, specifier=specifier, constraint=constraint, stated=True)
    return _resolved(pinned, labels, found_under=reference.tag or "")


def implied_tag(constraint: str) -> str:
    """The one moving tag *constraint* points at, or ``""`` for none.

    PEP 440 has exactly the prefix structure the tag scheme is built on,
    so the common case needs no search: ``==4.4.2`` admits one release
    and lands on ``zephyr-4.4.2-latest``; ``~=4.4.2`` admits the ``4.4``
    releases and lands on ``zephyr-4.4-latest``; ``~=4.4`` admits the
    ``4`` releases and lands on ``zephyr-4-latest``.

    A constraint with no upper bound (``>=4.4``) implies no prefix and
    gets ``""`` — there is no tag that means "anything from here up", and
    inventing one would send the resolution to a name nobody publishes.
    """
    try:
        specifier = SpecifierSet(constraint)
    except InvalidSpecifier:
        return ""
    prefix = _implied_prefix(specifier)
    return f"zephyr-{prefix}{LATEST_SUFFIX}" if prefix else ""


# --------------------------------------------------------------------------
# the ladder
# --------------------------------------------------------------------------


def _follow_the_ladder(
    reference: Reference,
    client: Registry,
    *,
    specifier: SpecifierSet,
    constraint: str,
) -> ResolvedEnvironment:
    """Fast path first, tag listing second — the bare-repository case."""
    tried: list[str] = []

    fast = implied_tag(constraint)
    if fast:
        found = _try_tag(reference, client, fast, specifier=specifier)
        if found is not None:
            return found
        tried.append(fast)

    for tag in _listed_tags(reference, client, specifier=specifier):
        if tag in tried:
            continue
        found = _try_tag(reference, client, tag, specifier=specifier)
        if found is not None:
            return found
        tried.append(tag)

    raise BuildError(
        f"{reference.repository} offers no build environment for Zephyr {constraint}.",
        hint=(
            "MCUHome asks the repository which environment it recommends for the "
            "Zephyr its SDK needs, and checks what the image says it carries. "
            + (f"Nothing matched (tried: {', '.join(tried)}). " if tried else "")
            + "Name a different repository, or pin an image directly:\n"
            "    mcuhome device build --container-image <repository>:<tag>"
        ),
    )


def _try_tag(
    reference: Reference,
    client: Registry,
    tag: str,
    *,
    specifier: SpecifierSet,
) -> ResolvedEnvironment | None:
    """Resolve one candidate tag, or ``None`` when it is not the answer.

    Absence and disagreement are one answer here on purpose: a tag the
    publisher does not keep and a tag pointing at an image that does not
    satisfy the constraint both mean "keep looking", and the caller says
    what it tried if the search runs out.
    """
    candidate = Reference(registry=reference.registry, path=reference.path, tag=tag)
    digest = client.digest_of(candidate)
    if digest is None:
        return None
    pinned = candidate.with_digest(digest)
    labels = client.labels(pinned)
    if not _labels_satisfy(labels, specifier=specifier):
        return None
    return _resolved(pinned, labels, found_under=tag)


def _listed_tags(reference: Reference, client: Registry, *, specifier: SpecifierSet) -> list[str]:
    """The repository's ``-latest`` tags that satisfy the constraint, highest first.

    The releases are ordered, the revisions behind them are not: each tag
    names one Zephyr release and the publisher decides which revision of
    it is current. Sorting releases is therefore ours to do and sorting
    revisions is not.
    """
    candidates: list[tuple[Version, str]] = []
    for tag in client.tags(reference):
        if not tag.startswith("zephyr-") or not tag.endswith(LATEST_SUFFIX):
            continue
        middle = tag[len("zephyr-") : -len(LATEST_SUFFIX)]
        try:
            version = Version(middle)
        except InvalidVersion:
            continue
        # `prereleases=True` because the *tag* is only the prefilter: a
        # publisher who tags a pre-release environment is entitled to,
        # and whether it satisfies the constraint is the specifier's own
        # question a line below.
        if specifier.contains(version, prereleases=bool(specifier.prereleases)):
            candidates.append((version, tag))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [tag for _, tag in candidates]


def _implied_prefix(specifier: SpecifierSet) -> str:
    """The longest version prefix every version *specifier* admits shares.

    Only the two operators that bound a version from both sides say
    anything about a prefix — ``==`` names one release or one wildcard
    family, ``~=`` names everything from a release up to its parent
    component's end. A set combining several takes the most specific,
    which is what an intersection of ranges means.
    """
    best = ""
    for one in specifier:
        prefix = ""
        if one.operator == "==":
            value = one.version
            if value.endswith(".*"):
                prefix = value[: -len(".*")]
            elif "*" not in value:
                prefix = value
        elif one.operator == "~=":
            parts = one.version.split(".")
            if len(parts) > 1:
                prefix = ".".join(parts[:-1])
        # A prefix has to be dotted decimals to be a tag component; an
        # epoch, a local version or a pre-release suffix is not part of
        # any tag this scheme publishes.
        if prefix and all(part.isdigit() for part in prefix.split(".")) and len(prefix) > len(best):
            best = prefix
    return best


# --------------------------------------------------------------------------
# the authority
# --------------------------------------------------------------------------


def _labels_satisfy(labels: dict[str, str], *, specifier: SpecifierSet) -> bool:
    """Does this image declare itself, and does the declaration fit?"""
    if labels.get(CONTRACT_LABEL) != str(CONTRACT_VERSION):
        return False
    if not labels.get(TOOLCHAIN_LABEL):
        return False
    stated = labels.get(ZEPHYR_LABEL) or ""
    try:
        version = Version(stated)
    except InvalidVersion:
        return False
    return specifier.contains(version, prereleases=bool(specifier.prereleases))


def _require_labels(
    reference: Reference,
    labels: dict[str, str],
    *,
    specifier: SpecifierSet,
    constraint: str,
    stated: bool,
) -> None:
    """The same check as :func:`_labels_satisfy`, with a sentence for each way it fails.

    Used where the image was **named** rather than searched for: somebody
    asked for this one, so "keep looking" is not an answer and the
    refusal has to say which of the three things is wrong.
    """
    del stated
    contract = labels.get(CONTRACT_LABEL)
    if not contract:
        raise BuildError(
            f"{reference.repository} is not a MCUHome build environment.",
            hint=(
                "an image says what it is through its labels, and this one carries "
                f"no {CONTRACT_LABEL}. Point at a build environment, or build "
                "against the one MCUHome ships by removing --container-image."
            ),
        )
    if contract != str(CONTRACT_VERSION):
        raise BuildError(
            f"{reference.repository} implements build-environment version {contract}, "
            f"and this MCUHome speaks version {CONTRACT_VERSION}.",
            hint=(
                "the two cannot work together. Use an environment built for this "
                "MCUHome release, or update MCUHome to one that speaks that version."
            ),
        )
    if not labels.get(TOOLCHAIN_LABEL):
        raise BuildError(
            f"{reference.repository} does not say which toolchain it carries.",
            hint=(
                f"a build environment declares it in {TOOLCHAIN_LABEL}, so a build "
                "report can name what compiled the firmware. This image declares nothing."
            ),
        )
    declared = labels.get(ZEPHYR_LABEL) or ""
    try:
        version = Version(declared)
    except InvalidVersion:
        raise BuildError(
            f"{reference.repository} does not say which Zephyr it carries."
            if not declared
            else f'{reference.repository} states "{declared}" as its Zephyr release, '
            "which is not a release.",
            hint=(
                f"a build environment declares it in {ZEPHYR_LABEL}, as a plain "
                "release such as 4.4.0, and MCUHome needs one satisfying "
                f"{constraint}. An image that does not say is never assumed to fit."
            ),
        ) from None
    if not specifier.contains(version, prereleases=bool(specifier.prereleases)):
        raise BuildError(
            f"{reference.repository} carries Zephyr {declared}, and this device "
            f"needs {constraint}.",
            hint=(
                "the SDK the device builds with states which Zephyr releases it "
                "supports. Use an environment carrying one of them, or build with "
                "an SDK that supports this one."
            ),
        )


def _resolved(
    reference: Reference, labels: dict[str, str], *, found_under: str
) -> ResolvedEnvironment:
    return ResolvedEnvironment(
        reference=reference,
        zephyr=labels.get(ZEPHYR_LABEL) or "",
        toolchain=labels.get(TOOLCHAIN_LABEL) or "",
        found_under=found_under,
    )


def _specifier(constraint: str) -> SpecifierSet:
    try:
        return SpecifierSet(constraint)
    except InvalidSpecifier as error:
        raise BuildError(
            f'"{constraint}" is not a PEP 440 version constraint.',
            hint=(
                "the Zephyr requirement is written as a compatible release "
                '"~=4.4.0", a range ">=4.4,<5", or an exact pin "==4.4.2".'
            ),
        ) from error
