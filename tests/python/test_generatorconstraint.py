# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Which build contexts a build environment accepts.

The check the build environment specification (§9.1) has an orchestrator
run before every step: the context says who wrote it, as a chain of
``<product>:<version>`` entries most recent first, and the environment
declares which of them it will take, as ``<product>:<specifier>`` entries
where several for one product are alternatives.

The two modes are the point of most of this file. ``strict`` — the
default — believes **only the leftmost** entry of the chain, because
everything to its right is a claim the last writer makes about tools it
did not run; ``chain`` walks the chain and accepts at the first entry
that matches. A test suite that never distinguished them would let an
implementation collapse both into "any entry matches", which is ``chain``
under the name ``strict`` and quietly trusts a claim nobody checked.
"""

from __future__ import annotations

import pytest
from mcuhome.model.buildenvironment import MODE_CHAIN, MODE_STRICT
from mcuhome.model.errors import BuildError

from mcuhome.workbench.generatorconstraint import accepts, parse_constraint

WORKBENCH = "mcuhome-workbench"


def test_an_empty_specifier_accepts_every_version_of_that_product() -> None:
    """MCUHome's own environment declares exactly this, so it is load-bearing.

    There is one generator, it is released together with the environment,
    and no version of it produces a context the environment could not
    read — which the specification spells "any build context the workbench
    produced". Pre-releases included: everything MCUHome publishes in the
    0.1 line is one.
    """
    assert accepts(f"{WORKBENCH}:", f"{WORKBENCH}:0.1.0.dev0")
    assert accepts(f"{WORKBENCH}:", f"{WORKBENCH}:9.9.9")


def test_a_version_outside_the_constraint_is_not_accepted() -> None:
    assert not accepts(f"{WORKBENCH}:~=1.0.5", f"{WORKBENCH}:0.1.0")
    assert accepts(f"{WORKBENCH}:~=1.0.5", f"{WORKBENCH}:1.0.9")


def test_a_product_the_constraint_does_not_name_is_not_accepted() -> None:
    """Absence is never read as permission."""
    assert not accepts(f"{WORKBENCH}:", "custom-tool:1.0.0")


def test_an_environment_that_declares_nothing_accepts_nothing() -> None:
    """The specification says so outright: such an environment can never pass."""
    assert not accepts("", f"{WORKBENCH}:0.1.0")


def test_several_specifiers_for_one_product_are_alternatives() -> None:
    """Any one of them passing is a match, not all of them."""
    constraint = "custom-tool:==0.5.3;custom-tool:~=0.6.2"
    assert accepts(constraint, "custom-tool:0.5.3")
    assert accepts(constraint, "custom-tool:0.6.4")
    assert not accepts(constraint, "custom-tool:0.7.0")


def test_strict_believes_only_the_leftmost_entry() -> None:
    """The default, and the reason the chain is not simply searched.

    A tool that prepends itself to the chain *claims* it kept the context
    compatible with the entries to its right. ``strict`` does not take
    the claim: the leftmost entry — the tool that touched the context
    last — is the only one that decides.
    """
    chain = f"custom-tool:4.2.3;{WORKBENCH}:0.1.0"
    assert not accepts(f"{WORKBENCH}:", chain)
    assert not accepts(f"{WORKBENCH}:", chain, mode=MODE_STRICT)
    # The same chain with the workbench on the left is accepted, which is
    # what makes the assertion above about the *position* and not about
    # the constraint.
    assert accepts(f"{WORKBENCH}:", f"{WORKBENCH}:0.1.0;custom-tool:4.2.3")


def test_chain_walks_the_chain_and_accepts_at_the_first_match() -> None:
    """The other mode, for an environment that does take the claim."""
    chain = f"custom-tool:4.2.3;{WORKBENCH}:0.1.0"
    assert accepts(f"{WORKBENCH}:", chain, mode=MODE_CHAIN)


def test_a_pre_release_needs_a_specifier_that_asks_for_it() -> None:
    """PEP 440's own rule, unchanged — a stable specifier excludes them."""
    assert not accepts(f"{WORKBENCH}:>=1.0", f"{WORKBENCH}:1.1rc1")
    assert accepts(f"{WORKBENCH}:>=1.0rc1", f"{WORKBENCH}:1.1rc1")


def test_an_unreadable_constraint_is_a_refusal_and_not_a_no() -> None:
    """An environment that cannot be checked is not one to start unchecked."""
    with pytest.raises(BuildError):
        accepts("mcuhome-workbench", f"{WORKBENCH}:0.1.0")
    with pytest.raises(BuildError):
        accepts(f"{WORKBENCH}:not a specifier", f"{WORKBENCH}:0.1.0")


def test_an_unreadable_chain_is_a_refusal_and_not_a_no() -> None:
    """ "This context is malformed" and "this environment does not want it"
    are different answers, and a caller has to be able to tell them apart.
    """
    with pytest.raises(BuildError):
        accepts(f"{WORKBENCH}:", "not-a-chain")
    with pytest.raises(BuildError):
        accepts(f"{WORKBENCH}:", f"{WORKBENCH}:not a version")


def test_the_constraint_parses_into_alternatives_per_product() -> None:
    parsed = parse_constraint(f"{WORKBENCH}:~=1.0.5;custom-tool:==0.5.3;custom-tool:~=0.6.2")
    assert set(parsed) == {WORKBENCH, "custom-tool"}
    assert len(parsed["custom-tool"]) == 2
