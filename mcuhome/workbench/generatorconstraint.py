# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Does this build environment accept this build context?

The build environment specification (§9.1) makes the answer a version
check between two chains of the same shape:

* the **context** carries a generator chain in ``build-context.json`` —
  ``<product>:<version>`` entries separated by semicolons, most recent
  writer first;
* the **environment** declares a constraint —
  ``<product>:<specifier>`` entries, where several entries for one
  product are alternatives.

An entry matches when the constraint declares at least one specifier for
that product that the context's version satisfies. Two modes decide how
much of the context's chain is read: ``strict`` — the default — believes
only the leftmost entry, because everything to its right is a *claim* the
last writer makes about tools it did not run; ``chain`` walks left to
right and accepts at the first entry that matches.

The orchestrator runs this before **every** step, which is why it is a
function over two strings and touches nothing: an environment that
declares no constraint accepts nothing and can never pass, and an empty
specifier for a product accepts every version of it.

It lives in the workbench rather than beside the chain parser in
:mod:`mcuhome.model.context` for the reason that parser states: PEP 440
needs ``packaging``, and the shared vocabulary package has no
dependencies. Parsing a chain is reading names out of a document;
comparing a version against a specifier is this.
"""

from __future__ import annotations

from mcuhome.model.buildenvironment import MODE_CHAIN, MODE_STRICT
from mcuhome.model.context import GeneratorEntry, parse_generator_chain
from mcuhome.model.errors import BuildError
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

__all__ = ["accepts", "parse_constraint"]


def parse_constraint(constraint: str) -> dict[str, list[SpecifierSet]]:
    """``<product>:<specifier>;…`` as product → alternatives.

    Several entries for one product are alternatives rather than a
    conjunction, so they are collected into a list and any one of them
    passing is a match. An empty specifier is
    :class:`~packaging.specifiers.SpecifierSet`'s own "any version",
    which is exactly what the specification says it means.

    Raises a typed refusal for an entry this grammar cannot read: an
    environment whose constraint cannot be parsed cannot be checked, and
    starting it anyway would be starting it unchecked.
    """
    found: dict[str, list[SpecifierSet]] = {}
    for part in constraint.split(";"):
        entry = part.strip()
        if not entry:
            continue
        product, separator, specifier = entry.partition(":")
        if not separator or not product:
            raise BuildError(
                f'The build environment states "{entry}" as a build-context constraint.',
                hint="each entry is <product>:<version specifier>, separated by semicolons",
            )
        try:
            parsed = SpecifierSet(specifier.strip())
        except InvalidSpecifier as broken:
            raise BuildError(
                f'The build environment constrains {product} to "{specifier}", '
                "which is not a PEP 440 version specifier.",
                hint='for example mcuhome-workbench:~=1.0.5, or an empty one for "any"',
            ) from broken
        found.setdefault(product.strip(), []).append(parsed)
    return found


def accepts(constraint: str, generator: str, *, mode: str = MODE_STRICT) -> bool:
    """Whether an environment declaring *constraint* accepts *generator*.

    *generator* is the context's chain verbatim, as
    ``build-context.json`` carries it. A chain that cannot be parsed is a
    refusal from :func:`~mcuhome.model.context.parse_generator_chain`
    rather than a ``False``: "this context is malformed" and "this
    environment does not want it" are different answers and a caller has
    to be able to say which one it got.
    """
    entries = parse_generator_chain(generator)
    wanted = parse_constraint(constraint)
    candidates = entries[:1] if mode != MODE_CHAIN else entries
    return any(_matches(wanted, entry) for entry in candidates)


def _matches(wanted: dict[str, list[SpecifierSet]], entry: GeneratorEntry) -> bool:
    """One chain entry against the constraint's alternatives for its product."""
    alternatives = wanted.get(entry.product)
    if not alternatives:
        return False
    try:
        version = Version(entry.version)
    except InvalidVersion as broken:
        raise BuildError(
            f'The build context says it was written by {entry.product} "{entry.version}", '
            "which is not a version.",
            hint="recreate the context — its generator declaration is malformed",
        ) from broken
    # PEP 440's own rule, and it has to be spelled out rather than left
    # to the default: ``version in specifier`` calls ``contains`` with
    # ``prereleases=None``, which *admits* pre-releases — the opposite of
    # what the specification says ("a specifier excludes pre-releases
    # unless it asks for them"). Passing the specifier's own pre-release
    # nature is that rule: ``>=1.0`` does not match ``1.1rc1``,
    # ``>=1.0rc1`` does, and the empty specifier — which MCUHome's own
    # environment declares — matches everything including pre-releases,
    # because an empty SpecifierSet has none of the versions that would
    # make it stable.
    return any(
        specifier.contains(version, prereleases=bool(specifier.prereleases) or not specifier)
        for specifier in alternatives
    )
