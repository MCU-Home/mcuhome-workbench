# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Resolving a PEP 440 constraint to the one version that satisfies it.

The constraint grammar is PEP 440 and the pre-release rule is E52's (ADR
0018's amendment): a pre-release satisfies a constraint only when the
constraint is itself a pre-release specifier or pre-releases are
explicitly allowed. These tests pin both the happy path and every refusal
the resolver owes a caller.
"""

from __future__ import annotations

import pytest

from mcuhome.model.errors import BuildError
from mcuhome.workbench.resolve_pins import (
    ResolvedPackage,
    resolve_from_index,
    resolve_version,
)

# A stable set with two pre-releases mixed in, so the pre-release rule has
# something to include or drop in every direction.
AVAILABLE = ["2.2.0", "2.3.5", "2.3.6", "2.4.0", "2.4.1", "3.0.0", "2.5.0.dev0", "2.5.0a1"]

# A set whose *highest* version is a pre-release, so admitting or dropping
# pre-releases changes the answer rather than being masked by a later final.
PRE_SET = ["2.4.0", "2.4.1", "2.5.0.dev0"]


def test_resolve_version_picks_the_highest_satisfying() -> None:
    """A constraint resolves to the single highest version in range, not the first."""
    assert resolve_version(">=2.3.6,<3", AVAILABLE) == "2.4.1"


def test_an_exact_pin_resolves_to_that_version() -> None:
    """An exact pin resolves to exactly it, and to nothing else in range."""
    assert resolve_version("==2.3.6", AVAILABLE) == "2.3.6"


def test_the_selected_string_is_returned_verbatim() -> None:
    """The winner comes back as its original spelling, so a caller can key it back."""
    # "2.4" and "2.4.0" are the same PEP 440 version; the string given wins.
    assert resolve_version("==2.4.0", ["2.4"]) == "2.4"


def test_a_prerelease_is_excluded_from_a_stable_constraint() -> None:
    """A stable constraint never resolves to a dev/pre-release (E52).

    ``2.5.0.dev0`` is the highest version in range, but ``>=2.4`` is not a
    pre-release specifier, so the pre-release is not a candidate at all and
    the highest *final* version wins.
    """
    assert resolve_version(">=2.4", PRE_SET) == "2.4.1"


def test_a_prerelease_constraint_admits_the_prerelease() -> None:
    """A pre-release specifier makes pre-releases candidates (E52).

    ``>=2.5.0a1`` is itself a pre-release specifier, so ``2.5.0a1`` is in
    range — the other direction of the rule.
    """
    assert resolve_version(">=2.5.0a1,<3", AVAILABLE) == "2.5.0a1"


def test_explicitly_allowing_prereleases_admits_them() -> None:
    """prereleases=True admits pre-releases a stable constraint would drop."""
    assert resolve_version(">=2.4", PRE_SET, prereleases=True) == "2.5.0.dev0"


def test_an_empty_version_set_is_refused() -> None:
    """Nothing to choose from is a typed refusal, not an empty answer."""
    with pytest.raises(BuildError) as caught:
        resolve_version(">=1.0", [], name="mcuhome-sdk")
    assert "mcuhome-sdk" in caught.value.message
    assert "none" in (caught.value.hint or "")


def test_no_version_in_range_is_refused_and_names_what_was_available() -> None:
    """A constraint nothing satisfies names the constraint and the options."""
    with pytest.raises(BuildError) as caught:
        resolve_version(">=9.0", ["1.0", "2.0"])
    assert ">=9.0" in caught.value.message
    assert "1.0" in (caught.value.hint or "") and "2.0" in (caught.value.hint or "")


def test_a_malformed_constraint_is_refused() -> None:
    """A caret is npm-style, not PEP 440 — refused by grammar, before resolving."""
    with pytest.raises(BuildError) as caught:
        resolve_version("^2.3.6", AVAILABLE)
    assert "PEP 440" in caught.value.message


def test_a_malformed_available_version_is_refused() -> None:
    """A version in the index that is not PEP 440 is a malformed-index refusal."""
    with pytest.raises(BuildError) as caught:
        resolve_version(">=1.0", ["1.0", "not-a-version"])
    assert "PEP 440" in caught.value.message


# --------------------------------------------------------------------------
# Resolving against the static index.json
# --------------------------------------------------------------------------


def _index(**versions: dict[str, object]) -> dict:
    """A package index shaped like scripts/build_sdk_archive.py writes."""
    return {"packages": {"mcuhome-sdk": dict(versions)}}


def test_resolve_from_index_returns_the_pinned_package() -> None:
    """The index resolver picks the version and hands back its entry."""
    index = _index(
        **{
            "0.1.0": {"file": "mcuhome-sdk-0.1.0.tar.zst", "sha256": "a" * 64, "size": 100},
            "0.2.0": {"file": "mcuhome-sdk-0.2.0.tar.zst", "sha256": "b" * 64, "size": 200},
        }
    )
    resolved = resolve_from_index(index, "mcuhome-sdk", ">=0.1")
    assert resolved == ResolvedPackage(
        name="mcuhome-sdk",
        version="0.2.0",
        file="mcuhome-sdk-0.2.0.tar.zst",
        sha256="b" * 64,
        size=200,
    )


def test_an_index_without_the_package_is_refused() -> None:
    """A constraint for a package the index does not carry is a refusal."""
    with pytest.raises(BuildError) as caught:
        resolve_from_index({"packages": {}}, "mcuhome-sdk", ">=0")
    assert "mcuhome-sdk" in caught.value.message
