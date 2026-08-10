# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""This distribution's dependencies, one of which cannot be written down.

Everything else about it is in ``pyproject.toml``. This file exists for
one field: ADR 0020 decision 8 gives the three distributions one shared
version, so the dependency between them is an **exact** pin — and a
literal ``==0.1.0.dev0`` here would be a second place the release version
lives, which is the one thing "one version, one tag" rules out.
Declaring ``dependencies`` dynamic and computing the pin from the same
attribute ``pyproject.toml`` reads the version from keeps the number in
exactly one file. The rest of the list has to come along, because
``dynamic`` is per field and not per entry.

The repository root goes on ``sys.path`` because the build runs in an
isolated environment where nothing of this repository is installed;
:mod:`mcuhome.model` is dependency-free and import-side-effect-free by
design (that is what the package is *for*), so importing it here reads
the value rather than guessing at it.
"""

import sys
from pathlib import Path

from setuptools import setup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mcuhome.model import __version__  # noqa: E402 - needs the path above

setup(
    install_requires=[
        f"mcuhome-model=={__version__}",
        # One third-party runtime dependency, on purpose: ruamel.yaml is
        # the only YAML parser that keeps line/column information on the
        # parsed tree, and every validation error in this package points
        # at a line. Everything else is the standard library plus
        # hand-written checks — error messages are user interface here,
        # and a schema library's generic wording does not meet that bar.
        "ruamel.yaml>=0.18",
        # PEP 440 constraint resolution for the SDK and container pins
        # (E52): mcuhome.workbench.resolve_pins parses constraints and
        # available versions with packaging.SpecifierSet/Version. It is
        # a near-ubiquitous transitive dependency, but the workbench uses
        # it directly, so it is declared directly.
        "packaging>=23",
    ]
)
