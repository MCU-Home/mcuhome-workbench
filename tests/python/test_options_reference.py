# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The option registry against the reference that publishes it.

``docs/api.md`` is the contract an embedder and every client read, and
its options table states each key's kind, its channels, its variable and
its flag. This file holds the registry to that table: a spelling that
drifts from the document is a broken promise, not an implementation
detail, and the two can only stay equal if something compares them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mcuhome.workbench.configuration import OPTION_KINDS, OPTIONS

REFERENCE = Path(__file__).resolve().parents[2] / "docs" / "api.md"

#: One row of the options table: key, kind, declared default, derived
#: fallback, channels, variable, flag.
_ROW = re.compile(
    r"^\| `([a-z_.<>-]+)` \| (.+?) \| (.+?) \| (.+?) \| "
    r"([yn])/([yn])/([yn]) \| (.+?) \| (.+?) \|$"
)


def _documented() -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for line in REFERENCE.read_text(encoding="utf-8").splitlines():
        found = _ROW.match(line.strip())
        if found is None:
            continue
        key, kind, _declared, _derived, files, environment, arguments, variable, flag = (
            found.groups()
        )
        rows[key] = {
            "kind": kind,
            "files": files,
            "environment": environment,
            "arguments": arguments,
            "variable": variable,
            "flag": flag,
        }
    return rows


def _stated(cell: str) -> str:
    """A table cell as a value: a dash means "none"."""
    bare = cell.strip().strip("`")
    return "" if bare == "–" else bare


@pytest.fixture(scope="module")
def documented() -> dict[str, dict[str, str]]:
    rows = _documented()
    assert rows, f"no options table found in {REFERENCE}"
    return rows


def test_the_reference_documents_every_declared_option(documented):
    # The map options are documented per key a user writes
    # (`builder.<name>.target`), so they are matched by their area below.
    scalar = {opt.name for opt in OPTIONS if opt.leaf}
    assert scalar <= set(documented)


def test_every_declared_option_has_the_documented_spellings(documented):
    for opt in OPTIONS:
        if not opt.leaf:
            continue
        row = documented[opt.name]
        assert opt.env_var == _stated(row["variable"]), opt.name
        assert opt.flag == _stated(row["flag"]), opt.name
        assert opt.files is (row["files"] == "y"), opt.name
        assert opt.environment is (row["environment"] == "y"), opt.name
        assert opt.arguments is (row["arguments"] == "y"), opt.name


def test_every_map_option_is_documented_by_its_keys(documented):
    for opt in OPTIONS:
        if opt.leaf:
            continue
        inside = [key for key in documented if key.startswith(f"{opt.name}.")]
        assert inside, f"{opt.name} documents none of its keys"
        for key in inside:
            assert _stated(documented[key]["variable"]) == ""
            assert _stated(documented[key]["flag"]) == ""


def test_every_kind_is_one_the_reference_knows(documented):
    for opt in OPTIONS:
        assert opt.kind in OPTION_KINDS


#: Variables MCUHome used to read behind the registry's back. Each one is
#: an option now, and the point of this list is that the literal is gone:
#: a module that reads a variable no option declares is a spelling
#: `mcuhome config print` cannot show and nobody can find.
RETIRED_VARIABLES = (
    "MCUHOME_DOCKER",
    "MCUHOME_IMGTOOL",
    "MCUHOME_CCACHE_DIR",
    "MCUHOME_DEFAULT_BUILDER",
)

PACKAGE = Path(__file__).resolve().parents[2] / "mcuhome" / "workbench"

#: What MCUHome sets *for* a build environment it starts, rather than
#: reads: never options, and told apart from them by their prefix. These
#: two still carry the configuration prefix and belong to the builder
#: family with the rest — the environment package that reads them is
#: released from another repository, so both sides move together.
SET_FOR_THE_BUILD_ENVIRONMENT = frozenset(
    {"MCUHOME_BUILD_ENV_TOOLS", "MCUHOME_BUILD_ENV_WORKSPACE"}
)


@pytest.mark.parametrize("variable", RETIRED_VARIABLES)
def test_no_module_carries_a_retired_variable(variable: str) -> None:
    for source in PACKAGE.rglob("*.py"):
        assert variable not in source.read_text(encoding="utf-8"), source


def test_every_variable_the_package_names_is_a_declared_one() -> None:
    """No ``MCUHOME_*`` literal anywhere but the declarations that own it.

    The registry derives every variable from a key, so a literal in a
    module is either a second declaration of one or a variable nothing
    declares — and both are the drift this rule exists against. The
    builder-side family is the stated exception: those are what MCUHome
    *sets* for a build environment it starts, never what it reads.
    """
    declared = {opt.env_var for opt in OPTIONS if opt.env_var}
    pattern = re.compile(r"\bMCUHOME_[A-Z0-9_]+\b")
    for source in PACKAGE.rglob("*.py"):
        for found in pattern.findall(source.read_text(encoding="utf-8")):
            if found.startswith("MCUHOME_BUILDER_") or found in SET_FOR_THE_BUILD_ENVIRONMENT:
                continue
            assert found in declared, f"{source}: {found}"
