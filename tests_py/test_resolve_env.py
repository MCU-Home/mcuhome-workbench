# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Choosing the one container a device's firmware is compiled in.

The rule under test is "the publisher recommends, we verify": a moving
``-latest`` tag narrows the field to one image and the image's own labels
decide whether it is taken. Every test drives a scripted registry, so
nothing here talks to a network — and the counting tests are the point of
several of them, because the cheap path is only cheap if it really does
not list tags.
"""

from __future__ import annotations

import pytest
from mcuhome.model.buildimage import CONTRACT_LABEL, TOOLCHAIN_LABEL, ZEPHYR_LABEL
from mcuhome.model.errors import BuildError

from mcuhome.workbench.ociregistry import RegistryUnauthorized
from mcuhome.workbench.resolve_env import implied_tag, resolve_environment

REPO = "ghcr.io/mcu-home/build-container"


def labels(zephyr: str = "4.4.0", *, contract: str = "1", toolchain: str = "zephyr-sdk-1.0.1"):
    """A conforming environment's label set, with one value at a time moved."""
    found = {CONTRACT_LABEL: contract, ZEPHYR_LABEL: zephyr, TOOLCHAIN_LABEL: toolchain}
    return {name: value for name, value in found.items() if value}


def digest(seed: str) -> str:
    return "sha256:" + (seed * 64)[:64]


class Fake:
    """A registry with a fixed tag map, counting what it was asked."""

    def __init__(self, images: dict[str, tuple[str, dict[str, str]]], *, tags_extra=()) -> None:
        #: tag -> (digest, labels)
        self.images = images
        self.extra = tuple(tags_extra)
        self.tag_listings = 0
        self.asked: list[str] = []

    def tags(self, reference):
        del reference
        self.tag_listings += 1
        return tuple(self.images) + self.extra

    def digest_of(self, reference):
        self.asked.append(reference.tag or reference.digest or "")
        if reference.digest:
            return reference.digest
        found = self.images.get(reference.tag or "")
        return found[0] if found else None

    def labels(self, reference):
        for known, (known_digest, known_labels) in self.images.items():
            del known
            if reference.digest == known_digest:
                return dict(known_labels)
        return {}


# --------------------------------------------------------------------------
# the fast path
# --------------------------------------------------------------------------


def test_a_constraint_lands_on_one_tag_without_listing_anything() -> None:
    """PEP 440 has the prefix structure the tag scheme is built on.

    ``~=4.4.0`` admits exactly the 4.4 releases, so ``zephyr-4.4-latest``
    is one request away — and a tag listing is paginated, rate limited
    and expensive on a large repository, so not needing it is the point
    rather than the saved round trip.
    """
    registry = Fake({"zephyr-4.4-latest": (digest("a"), labels("4.4.0"))})
    found = resolve_environment(REPO, constraint="~=4.4.0", registry=registry)

    assert found.reference.digest == digest("a")
    assert found.found_under == "zephyr-4.4-latest"
    assert found.zephyr == "4.4.0"
    assert found.toolchain == "zephyr-sdk-1.0.1"
    assert registry.tag_listings == 0


@pytest.mark.parametrize(
    ("constraint", "tag"),
    [
        ("==4.4.2", "zephyr-4.4.2-latest"),
        ("~=4.4.2", "zephyr-4.4-latest"),
        ("~=4.4", "zephyr-4-latest"),
        ("~=4.4.0,==4.4.*", "zephyr-4.4-latest"),
        ("==4.4.*", "zephyr-4.4-latest"),
        (">=4.4", ""),
        ("", ""),
        ("nonsense", ""),
    ],
)
def test_which_tag_a_constraint_implies(constraint: str, tag: str) -> None:
    """A constraint with no upper bound implies no tag, and says so.

    There is no name that means "anything from here up", and inventing
    one would send the resolution to a tag nobody publishes.
    """
    assert implied_tag(constraint) == tag


def test_the_pin_carries_the_tag_it_was_found_under_and_the_digest() -> None:
    """The tag is documentation, the digest is what a build fetches.

    Dropping the tag would lose the only human-readable part of a record
    somebody reads back a year later.
    """
    registry = Fake({"zephyr-4.4-latest": (digest("b"), labels())})
    found = resolve_environment(REPO, constraint="~=4.4.0", registry=registry)
    assert str(found.reference) == f"{REPO}:zephyr-4.4-latest@{digest('b')}"
    assert found.pin.digest == digest("b")


# --------------------------------------------------------------------------
# the fallback
# --------------------------------------------------------------------------


def test_a_publisher_who_keeps_no_aggregate_tag_is_answered_by_the_listing() -> None:
    """Aggregate tags are recommended, not required."""
    registry = Fake(
        {
            "zephyr-4.4.0-latest": (digest("c"), labels("4.4.0")),
            "zephyr-4.4.2-latest": (digest("d"), labels("4.4.2")),
        }
    )
    found = resolve_environment(REPO, constraint="~=4.4.0", registry=registry)

    assert registry.tag_listings == 1
    # The highest release satisfying the constraint, and only releases are
    # ordered here — which revision of it is current is the publisher's
    # own answer, carried by the moving tag.
    assert found.reference.digest == digest("d")
    assert found.found_under == "zephyr-4.4.2-latest"


def test_an_aggregate_tag_pointing_below_the_constraint_falls_through() -> None:
    """``zephyr-4.4-latest`` cannot know a constraint's lower bound.

    The tag is the prefilter and the label is the authority, so an
    aggregate that currently points at 4.4.1 does not answer ``~=4.4.2``
    — and the listing does.
    """
    registry = Fake(
        {
            "zephyr-4.4-latest": (digest("e"), labels("4.4.1")),
            "zephyr-4.4.3-latest": (digest("f"), labels("4.4.3")),
        }
    )
    found = resolve_environment(REPO, constraint="~=4.4.2", registry=registry)
    assert found.reference.digest == digest("f")


def test_revision_tags_are_never_candidates() -> None:
    """Revisions are the publisher's to order, and he orders them with -latest.

    An immutable ``-r`` tag is a name for one build of the environment.
    Sorting those here would be this side deciding which revision is
    current, which is exactly what the moving tag exists to avoid.
    """
    registry = Fake(
        {"zephyr-4.4.0-latest": (digest("1"), labels("4.4.0"))},
        tags_extra=("zephyr-4.4.0-r10", "zephyr-4.4.0-r11", "latest", "zephyr-4.4.0"),
    )
    found = resolve_environment(REPO, constraint="~=4.4.0", registry=registry)
    assert found.found_under == "zephyr-4.4.0-latest"


def test_a_repository_offering_nothing_that_fits_names_what_was_tried() -> None:
    registry = Fake({"zephyr-4.4-latest": (digest("9"), labels("4.5.0"))})
    with pytest.raises(BuildError) as refusal:
        resolve_environment(REPO, constraint="~=4.4.0", registry=registry)
    assert "zephyr-4.4-latest" in str(refusal.value)
    assert "--container-image" in str(refusal.value)


# --------------------------------------------------------------------------
# the labels are the authority
# --------------------------------------------------------------------------


def test_an_image_that_names_no_zephyr_release_is_not_a_candidate() -> None:
    """Absence is never read as compatible.

    An environment that does not say what it builds against has not made
    the declaration a constraint is written against.
    """
    registry = Fake({"zephyr-4.4-latest": (digest("2"), labels(""))})
    with pytest.raises(BuildError):
        resolve_environment(REPO, constraint="~=4.4.0", registry=registry)


def test_an_image_that_is_not_a_build_environment_is_not_a_candidate() -> None:
    registry = Fake({"zephyr-4.4-latest": (digest("3"), labels(contract=""))})
    with pytest.raises(BuildError):
        resolve_environment(REPO, constraint="~=4.4.0", registry=registry)


def test_an_image_that_names_no_toolchain_is_not_a_candidate() -> None:
    """A build report that cannot say what compiled the bytes is worth less."""
    registry = Fake({"zephyr-4.4-latest": (digest("4"), labels(toolchain=""))})
    with pytest.raises(BuildError):
        resolve_environment(REPO, constraint="~=4.4.0", registry=registry)


# --------------------------------------------------------------------------
# a reference that already decided
# --------------------------------------------------------------------------


def test_a_named_tag_is_resolved_to_its_digest_and_still_checked() -> None:
    """Somebody asked for this one, so "keep looking" is not an answer.

    The refusal has to say which of the three declarations is wrong,
    because there is nothing else for the person to try.
    """
    registry = Fake({"dev": (digest("5"), labels("4.5.0"))})
    with pytest.raises(BuildError) as refusal:
        resolve_environment(f"{REPO}:dev", constraint="~=4.4.0", registry=registry)
    assert "4.5.0" in str(refusal.value)
    assert "~=4.4.0" in str(refusal.value)
    assert registry.tag_listings == 0


def test_a_named_tag_that_fits_is_taken_as_it_stands() -> None:
    registry = Fake({"dev": (digest("6"), labels("4.4.7"))})
    found = resolve_environment(f"{REPO}:dev", constraint="~=4.4.0", registry=registry)
    assert found.reference.digest == digest("6")
    assert found.found_under == "dev"


def test_a_reference_that_already_names_a_digest_is_verified_not_resolved() -> None:
    """A pin is not a permission: the labels still have to agree."""
    pinned = f"{REPO}:zephyr-4.4.0-r10@{digest('7')}"
    registry = Fake({"zephyr-4.4.0-r10": (digest("7"), labels("4.4.0"))})
    found = resolve_environment(pinned, constraint="~=4.4.0", registry=registry)
    assert str(found.reference) == pinned
    assert found.found_under == ""
    assert registry.asked == []
    assert registry.tag_listings == 0


def test_a_pinned_digest_whose_image_does_not_fit_is_refused() -> None:
    pinned = f"{REPO}:old@{digest('8')}"
    registry = Fake({"old": (digest("8"), labels("4.3.0"))})
    with pytest.raises(BuildError) as refusal:
        resolve_environment(pinned, constraint="~=4.4.0", registry=registry)
    assert "4.3.0" in str(refusal.value)


def test_a_tag_that_does_not_exist_anywhere_says_both_places_were_looked() -> None:
    registry = Fake({})
    with pytest.raises(BuildError) as refusal:
        resolve_environment(f"{REPO}:nope", constraint="~=4.4.0", registry=registry)
    assert "nope" in str(refusal.value)


# --------------------------------------------------------------------------
# an image that exists only here
# --------------------------------------------------------------------------


class LocalImage:
    """What ``docker image inspect`` answers, in the shape the seam reads."""

    def __init__(self, image_id: str, found_labels: dict[str, str], repo_digest=None) -> None:
        self.image_id = image_id
        self.labels = found_labels
        self.digest = repo_digest


def test_an_image_built_here_is_pinned_by_its_own_id() -> None:
    """A repository has no tag for a container somebody just built.

    Neither has it for one CI built for a pull request — which is the
    case that made this necessary. Such a pin is honest rather than
    portable: those bytes are not fetchable anywhere, and a build server
    told to use them will say so.
    """
    registry = Fake({})
    found = resolve_environment(
        f"{REPO}:local",
        constraint="~=4.4.0",
        registry=registry,
        local=lambda name: LocalImage(digest("0"), labels("4.4.0")) if ":local" in name else None,
    )
    assert found.reference.digest == digest("0")
    assert found.found_under == "local"


def test_a_local_image_is_checked_exactly_like_a_fetched_one() -> None:
    """Being local buys an image no exemption from saying what it carries."""
    registry = Fake({})
    with pytest.raises(BuildError):
        resolve_environment(
            f"{REPO}:local",
            constraint="~=4.4.0",
            registry=registry,
            local=lambda name: LocalImage(digest("0"), labels("4.6.0")),
        )


def test_a_repository_digest_beats_the_image_id_when_there_is_one() -> None:
    """A pulled image names bytes a registry can serve; its ID does not."""
    registry = Fake({})
    found = resolve_environment(
        f"{REPO}:local",
        constraint="~=4.4.0",
        registry=registry,
        local=lambda name: LocalImage(digest("0"), labels(), repo_digest=digest("e")),
    )
    assert found.reference.digest == digest("e")


def test_a_private_registry_is_answered_from_this_host_when_it_can_be() -> None:
    """ "Log in and name a tag" is the fix, and a logged-in docker has it.

    MCUHome reads registries without credentials of its own; a private
    one is answered by the local container program, which already has
    the operator's.
    """

    class Refusing(Fake):
        def digest_of(self, reference):
            raise RegistryUnauthorized("private", hint="docker login")

    found = resolve_environment(
        f"{REPO}:dev",
        constraint="~=4.4.0",
        registry=Refusing({}),
        local=lambda name: LocalImage(digest("0"), labels(), repo_digest=digest("d")),
    )
    assert found.reference.digest == digest("d")


def test_a_private_registry_with_nothing_here_keeps_its_own_refusal() -> None:
    class Refusing(Fake):
        def digest_of(self, reference):
            raise RegistryUnauthorized("private", hint="docker login <registry>")

    with pytest.raises(RegistryUnauthorized):
        resolve_environment(
            f"{REPO}:dev", constraint="~=4.4.0", registry=Refusing({}), local=lambda name: None
        )


def test_a_bare_repository_is_never_answered_from_this_host() -> None:
    """Choosing needs a tag list, and a machine's images are not one.

    A local image answers a *name*; which environment currently fits a
    constraint is a question only the publisher's tags answer.
    """
    registry = Fake({})
    with pytest.raises(BuildError):
        resolve_environment(
            REPO,
            constraint="~=4.4.0",
            registry=registry,
            local=lambda name: LocalImage(digest("0"), labels()),
        )


def test_a_pinned_image_that_is_here_is_verified_without_a_registry() -> None:
    """A digest plus a local image is a build that needs no network at all.

    The labels are the image's own either way, so reading them off this
    host is the same check against a cheaper source — and it is what
    makes ``--container-image <repo>@sha256:…`` the answer for an
    air-gapped machine as well as for a reproducible one.
    """

    class Never(Fake):
        def labels(self, reference):
            raise AssertionError("the registry was asked about an image that is here")

        def digest_of(self, reference):
            raise AssertionError("the registry was asked to resolve an already-pinned reference")

    pinned = f"{REPO}:zephyr-4.4.0-r10@{digest('7')}"
    found = resolve_environment(
        pinned,
        constraint="~=4.4.0",
        registry=Never({}),
        local=lambda name: LocalImage(digest("7"), labels(), repo_digest=digest("7")),
    )
    assert str(found.reference) == pinned


def test_a_pinned_image_that_is_not_here_falls_back_to_the_registry() -> None:
    """The other order, so the local path is an optimization and not a rule."""
    pinned = f"{REPO}:zephyr-4.4.0-r10@{digest('7')}"
    registry = Fake({"zephyr-4.4.0-r10": (digest("7"), labels())})
    found = resolve_environment(
        pinned, constraint="~=4.4.0", registry=registry, local=lambda name: None
    )
    assert str(found.reference) == pinned
