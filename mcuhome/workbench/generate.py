# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Stage 4 on this machine: writing the Zephyr application out of a model.

A build does not need this. Code generation happens *inside* the build
environment, out of the device model the build context carries
(build-container-contract §6.1), which is what keeps the private signing
key and the toolchain on opposite sides of one boundary.

What needs it is the caller who wants the generated tree and nothing
else: ``mcuhome device build --generate-only``, and any embedder asking
the same question. So this is a seam and not a build step — one function,
resolved at call time against a distribution this package deliberately
does not depend on.

**Why the import is written this way.** ADR 0020 decision 3 forbids
``mcuhome-workbench`` a dependency on ``mcuhome-compiler`` (a dashboard
install must not carry a toolchain, ADR 0017 §2), and
``tests/python/test_packaging_workbench.py`` reads the dependency arrows out
of the syntax tree — an ``import`` statement here would be
indistinguishable from the hard edge that is forbidden. So the edge is
resolved through :func:`importlib.import_module` and refuses in words
when the distribution is absent, the same shape
:mod:`mcuhome.workbench.sessionclient` uses for the ``remote`` extra.
"""

from __future__ import annotations

from pathlib import Path

from mcuhome.model.errors import BuildError
from mcuhome.model.model import DeviceModel

__all__ = ["CompilerUnavailable", "generate_tree"]


class CompilerUnavailable(BuildError):
    """Code generation was asked for, and this installation cannot run it.

    Worded like every other missing-piece refusal in the family — state
    the fact, then name the exact install — because that is what it is:
    stages 4-5 are their own distribution, which a workbench that
    validates configurations and drives build environments does not
    carry.
    """


def _compiler(module: str):
    """Import a compiler-side module, or refuse naming the distribution.

    **Only the missing distribution is translated.** An ``ImportError``
    raised from *inside* an installed compiler — a broken ``zstandard``
    wheel, a C extension built for another interpreter — says nothing
    about ``mcuhome-compiler`` being absent, and answering it with "not
    installed, run pip install mcuhome-compiler" sends the reader to fix
    the one thing that is already right. So the refusal is made only when
    the failed import *is* ``mcuhome.compiler`` or something under it;
    anything else travels on untouched, with its own name in it.
    """
    import importlib

    name = f"mcuhome.compiler.{module}"
    try:
        return importlib.import_module(name)
    except ImportError as error:
        missing = error.name or ""
        if missing != "mcuhome.compiler" and not missing.startswith("mcuhome.compiler."):
            raise
        raise CompilerUnavailable(
            "Generating the Zephyr application here needs mcuhome-compiler, "
            "and it is not installed.",
            hint=(
                "code generation is its own distribution, so a workbench that only "
                "validates configurations or drives a build environment does not "
                "carry it. A normal build needs none of this — the build "
                "environment generates from the model the build context carries. "
                "To generate here anyway, install it with:\n"
                "    pip install mcuhome-compiler"
            ),
        ) from error


def generate_tree(model: DeviceModel, *, out_dir: Path, config_name: str) -> list[Path]:
    """Write *model*'s standalone Zephyr application into *out_dir*.

    Answers with every file written, in the order they were written.

    *config_name* is the configuration file's name as the generated
    headers state it, and it comes out of the model rather than out of a
    path the caller was given: stage 4 has to be a function of the model
    alone, or a build from an exported model could not reproduce a direct
    one byte for byte.
    """
    generate = _compiler("generate")
    return list(generate.write_tree(model, out_dir=Path(out_dir), config_name=config_name))
