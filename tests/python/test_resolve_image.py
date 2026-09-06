# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Finding the image that delivers a wanted package set.

The rule under test is "exactly, not compatibly": an image's ``packages.``
labels must state the wanted set and nothing else — every member present,
every hash equal, no extra member — and the near miss (same names and
versions, different bytes) is refused rather than accepted as a fallback.
Every test drives a scripted registry of its own, so nothing here talks to
a network; the autouse fixtures in ``conftest.py`` fail any test that
forgets to pass one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

import pytest
from mcuhome.model.buildenvironment import (
    GENERATOR_CONSTRAINT_MEMBER,
    LABEL_PREFIX,
    PACKAGE_MEMBER_PREFIX,
    SPEC_GENERATION_MEMBER,
    ZEPHYR_VERSION_MEMBER,
    PackageMember,
)
from mcuhome.model.errors import BuildError

from mcuhome.workbench.ociregistry import RegistryError
from mcuhome.workbench.resolve_image import image_for_packages

REPO = "ghcr.io/mcu-home/build-environment"
OTHER_REPO = "example.com/other/build-environment"

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64

#: The concrete set every "does it match" test wants — two packages, each
#: pinned to a version and a hash, because that is what an image contains
#: and therefore what :func:`image_for_packages` is asked to find.
WANTED = {
    "tool-a": PackageMember(name="tool-a", version="1.0.0", sha256=HASH_A),
    "tool-b": PackageMember(name="tool-b", version="2.0.0", sha256=HASH_B),
}


def digest(seed: str) -> str:
    """A distinct, **well-formed** digest per seed.

    Hashed rather than composed by repeating the seed character: a digest
    is 64 lowercase hex digits and a reference refuses anything else, so a
    seed outside ``0-9a-f`` would make every "this image is not taken"
    test pass for the wrong reason — the refusal would come from the
    malformed digest rather than from the labels not matching.
    """
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def wanted_values() -> dict[str, str]:
    """The ``packages.`` values an image would carry to state :data:`WANTED`."""
    return {name: member.value() for name, member in WANTED.items()}


def image_labels(
    *,
    spec_generation: str = "3",
    zephyr_version: str = "4.4.0",
    generator_constraint: str = "~=4.4",
    packages: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The ``org.mcuhome.build-environment.*`` labels one image carries.

    *packages* maps a bare package name to the raw member value the image
    states — usually a :class:`PackageMember`'s own
    :meth:`~PackageMember.value`, but a test that wants a version with no
    hash passes the version string directly, since that is exactly the
    malformed delivery the specification forbids. *extra* is labels
    outside the namespace, to prove they are never looked at.
    """
    found = {
        f"{LABEL_PREFIX}{SPEC_GENERATION_MEMBER}": spec_generation,
        f"{LABEL_PREFIX}{ZEPHYR_VERSION_MEMBER}": zephyr_version,
        f"{LABEL_PREFIX}{GENERATOR_CONSTRAINT_MEMBER}": generator_constraint,
    }
    for name, value in (packages or {}).items():
        found[f"{LABEL_PREFIX}{PACKAGE_MEMBER_PREFIX}{name}"] = value
    found.update(extra or {})
    return found


class ScriptedImages:
    """A registry scripted per repository and tag, counting what it was asked.

    Unlike :mod:`test_resolve_env`'s ``Fake`` — one repository, three fixed
    labels — this resolver searches an *allowlist* of repositories and
    matches on an open-ended ``packages.`` label set, so the stub needs a
    tag map per repository and the option to make one of them unreachable.
    """

    def __init__(
        self,
        repositories: Mapping[str, Mapping[str, tuple[str, Mapping[str, str]]]] | None = None,
        *,
        unreachable: frozenset[str] = frozenset(),
    ) -> None:
        self.repositories = {repo: dict(tags) for repo, tags in (repositories or {}).items()}
        self.unreachable = set(unreachable)
        #: Repositories a tag listing actually succeeded for, in order.
        self.tag_listings: list[str] = []
        self.label_reads: list[tuple[str, str]] = []

    def tags(self, reference):
        if reference.repository in self.unreachable:
            raise RegistryError(
                f"{reference.repository} did not answer.",
                hint="an allowlist exists to have alternatives in it",
            )
        self.tag_listings.append(reference.repository)
        return tuple(self.repositories.get(reference.repository, {}))

    def labels(self, reference):
        self.label_reads.append((reference.repository, reference.tag or ""))
        found = self.repositories.get(reference.repository, {}).get(reference.tag or "")
        return dict(found[1]) if found else {}

    def digest_of(self, reference):
        found = self.repositories.get(reference.repository, {}).get(reference.tag or "")
        return found[0] if found else None


class _UntouchedRegistry:
    """A registry that fails the test the moment anything asks it anything.

    Used for the empty-allowlist refusal, which has to happen before any
    of the three questions is put to a registry at all.
    """

    def tags(self, reference):
        raise AssertionError("the registry was asked despite an empty allowlist")

    def labels(self, reference):
        raise AssertionError("the registry was asked despite an empty allowlist")

    def digest_of(self, reference):
        raise AssertionError("the registry was asked despite an empty allowlist")


# --------------------------------------------------------------------------
# an exact match
# --------------------------------------------------------------------------


def test_an_exact_match_is_pinned_by_digest_and_carries_its_own_declaration() -> None:
    """A match is pinned by the digest the registry answers, not the tag.

    The tag is only where the search looked; :attr:`ImageMatch.found_under`
    keeps it as a location worth a log line, while
    :attr:`ImageMatch.declaration` is parsed from the same labels the match
    was decided from, so a caller learns the generation, Zephyr version and
    generator constraint the image states without a second read.
    """
    labels = image_labels(
        spec_generation="9",
        zephyr_version="9.9.9",
        generator_constraint="==9.9.9",
        packages=wanted_values(),
    )
    registry = ScriptedImages({REPO: {"v1": (digest("a"), labels)}})

    found = image_for_packages(WANTED, registry=registry, repositories=(REPO,))

    assert str(found.reference) == f"{REPO}:v1@{digest('a')}"
    assert found.reference.digest == digest("a")
    assert found.found_under == "v1"
    assert found.declaration.spec_generation == "9"
    assert found.declaration.zephyr_version == "9.9.9"
    assert found.declaration.generator_constraint == "==9.9.9"


def test_labels_outside_the_build_environment_prefix_are_ignored() -> None:
    """A decoy that merely contains ``packages.`` outside the namespace is not read.

    Filtering has to be on the whole ``org.mcuhome.build-environment.``
    prefix, not on the substring ``packages.`` appearing anywhere in a key
    — otherwise an unrelated label from another namespace could accidentally
    participate in the match.
    """
    labels = image_labels(
        packages=wanted_values(),
        extra={
            "org.opencontainers.image.title": "unrelated",
            "some.other.namespace.packages.tool-a": f"9.9.9@sha256:{HASH_D}",
        },
    )
    registry = ScriptedImages({REPO: {"v1": (digest("a"), labels)}})

    found = image_for_packages(WANTED, registry=registry, repositories=(REPO,))

    assert found.reference.digest == digest("a")


# --------------------------------------------------------------------------
# ways to be almost right, all of them a refusal
# --------------------------------------------------------------------------


def test_the_near_miss_a_different_hash_under_the_same_name_and_version_is_refused() -> None:
    """Same package names and versions, other bytes: a different environment.

    This is the case the specification calls out by name — an image built
    from the same versions but different bytes is never taken instead, and
    the refusal is the typed :class:`BuildError` every other "no image
    fits" case raises, never a silent fallback to the near miss.
    """
    mismatched = dict(wanted_values())
    mismatched["tool-b"] = f"2.0.0@sha256:{HASH_C}"
    labels = image_labels(packages=mismatched)
    registry = ScriptedImages({REPO: {"v1": (digest("z"), labels)}})

    with pytest.raises(BuildError):
        image_for_packages(WANTED, registry=registry, repositories=(REPO,))


def test_an_image_assembled_from_one_extra_package_does_not_match() -> None:
    """One more package is a different environment, even if it changes nothing.

    "The environment is its package set" is only a statement about
    identity if a superset counts as a different set.
    """
    extra = dict(wanted_values())
    extra["tool-c"] = f"3.0.0@sha256:{HASH_D}"
    labels = image_labels(packages=extra)
    registry = ScriptedImages({REPO: {"v1": (digest("z"), labels)}})

    with pytest.raises(BuildError):
        image_for_packages(WANTED, registry=registry, repositories=(REPO,))


def test_an_image_missing_one_wanted_package_does_not_match() -> None:
    partial = {"tool-a": WANTED["tool-a"].value()}
    labels = image_labels(packages=partial)
    registry = ScriptedImages({REPO: {"v1": (digest("z"), labels)}})

    with pytest.raises(BuildError):
        image_for_packages(WANTED, registry=registry, repositories=(REPO,))


def test_a_package_stated_without_a_hash_does_not_match() -> None:
    """A delivery must state hashes; a bare version is the abstract declaration.

    An SDK's own lock names versions with no hash because it cannot know
    one yet (see the module docstring of ``buildenvironment``); an image is
    a delivery of exact bytes and has no such excuse, so a member that
    carries no hash at all does not satisfy a wanted set that names one.
    """
    unhashed = dict(wanted_values())
    unhashed["tool-b"] = "2.0.0"
    labels = image_labels(packages=unhashed)
    registry = ScriptedImages({REPO: {"v1": (digest("z"), labels)}})

    with pytest.raises(BuildError):
        image_for_packages(WANTED, registry=registry, repositories=(REPO,))


# --------------------------------------------------------------------------
# several tags, several repositories
# --------------------------------------------------------------------------


def test_several_tags_are_searched_and_the_matching_one_wins_even_when_not_first() -> None:
    """The tags are only candidates to look at; the labels decide.

    The first tag here answers with a package set that does not match, and
    the search keeps going to the second rather than stopping at the first
    candidate it looked at.
    """
    wrong = image_labels(packages={"tool-a": WANTED["tool-a"].value()})
    right = image_labels(packages=wanted_values())
    registry = ScriptedImages({REPO: {"v1": (digest("1"), wrong), "v2": (digest("2"), right)}})

    found = image_for_packages(WANTED, registry=registry, repositories=(REPO,))

    assert found.found_under == "v2"
    assert found.reference.digest == digest("2")


def test_a_repository_that_raises_registryerror_is_skipped_not_fatal() -> None:
    """One unreachable repository is not the end of the search.

    An allowlist exists to have alternatives in it — the first repository
    here fails outright, and the second, searched next, still answers.
    """
    matching = image_labels(packages=wanted_values())
    registry = ScriptedImages(
        {OTHER_REPO: {"v1": (digest("b"), matching)}},
        unreachable=frozenset({REPO}),
    )

    found = image_for_packages(WANTED, registry=registry, repositories=(REPO, OTHER_REPO))

    assert found.reference.digest == digest("b")
    # Only the repository that answered ever got a tag listing counted;
    # the one that raised never reached that line.
    assert registry.tag_listings == [OTHER_REPO]


# --------------------------------------------------------------------------
# the allowlist itself
# --------------------------------------------------------------------------


def test_an_empty_allowlist_is_a_typed_refusal_that_never_touches_a_registry() -> None:
    """No repository configured is a refusal before any question is asked.

    Which repositories a build may take an environment from is a decision
    about trust, so an empty list is refused outright rather than falling
    back to some registry the caller did not name.
    """
    with pytest.raises(BuildError):
        image_for_packages(WANTED, registry=_UntouchedRegistry(), repositories=())


def test_the_refusal_names_the_wanted_package_set_when_nothing_matches() -> None:
    """The hint says what was needed, because that is the only thing left to try."""
    registry = ScriptedImages({REPO: {}})

    with pytest.raises(BuildError) as refusal:
        image_for_packages(WANTED, registry=registry, repositories=(REPO,))

    message = str(refusal.value)
    assert f"tool-a {WANTED['tool-a'].value()}" in message
    assert f"tool-b {WANTED['tool-b'].value()}" in message


def test_a_wanted_set_whose_member_states_no_hash_matches_nothing() -> None:
    """The other side of the delivery rule, and the one that decides.

    An image must state a hash on every package member; so must the set it
    is matched against. A wanted member without one names a version and a
    name, and matching on those alone would take **any** bytes ever
    published under them — which is the whole thing "exactly, not
    compatibly" exists to prevent. The image here is a perfectly correct
    delivery: what makes it not a match is that the caller did not say
    which bytes it wanted.
    """
    labels = image_labels(packages=wanted_values())
    registry = ScriptedImages({REPO: {"v1": (digest("z"), labels)}})
    hashless = {
        "tool-a": PackageMember(name="tool-a", version="1.0.0"),
        "tool-b": PackageMember(name="tool-b", version="2.0.0", sha256=HASH_B),
    }
    with pytest.raises(BuildError):
        image_for_packages(hashless, registry=registry, repositories=(REPO,))
    # And the same image is taken the moment the hash is stated.
    assert image_for_packages(WANTED, registry=registry, repositories=(REPO,)).found_under == "v1"
