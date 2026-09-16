# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Stage 4 on this machine: writing the Zephyr application out of a model.

A build does not need this. Code generation happens *inside* the build
environment, out of the device model the build context carries
(mcuhome-sdk ``docs/spec/build-context-format.md`` §7), which is what
keeps the private signing key and the toolchain on opposite sides of one
boundary.

What needs it is the caller who wants the generated tree and nothing
else: ``mcuhome device build --generate-only``, and any embedder asking
the same question. So this is a seam and not a build step — one function,
resolved at call time against a distribution this package deliberately
does not depend on.

**Why the import is written this way.** The workbench must not depend on
``mcuhome-compiler`` — a dashboard install must not carry a toolchain —
and ``tests/python/test_packaging_workbench.py`` reads the dependency
arrows out of the syntax tree — an ``import`` statement here would be
indistinguishable from the hard edge that is forbidden. So the edge is
resolved through :func:`importlib.import_module` and refuses in words
when the distribution is absent, the same shape
:mod:`mcuhome.workbench.sessionclient` uses for the ``remote`` extra.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcuhome.model.errors import BuildError
from mcuhome.model.model import DeviceModel

__all__ = ["CompilerUnavailable", "GenerationResult", "generate_application"]


@dataclass(frozen=True)
class GenerationResult:
    """What one generation wrote, and for which device.

    The bare tuple of paths this call used to answer left the client to
    say which device the tree belongs to and where it went — which it
    knew only from the arguments it had passed in. Both are facts of the
    act, so they travel with it.
    """

    #: The device the tree was generated for, by its own name.
    device: str
    #: The directory it was written into.
    out_dir: Path
    #: Every file written, in the order it was written.
    files: tuple[Path, ...]

    def to_dict(self) -> dict[str, Any]:
        """This generation as a document, JSON-ready and complete."""
        return {
            "device": self.device,
            "out_dir": str(self.out_dir),
            "files": [str(path) for path in self.files],
        }


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


def generate_application(model: DeviceModel, *, out_dir: Path) -> GenerationResult:
    """Write *model*'s standalone Zephyr application into *out_dir*.

    Answers a :class:`GenerationResult`: the device, the directory, and
    every file written in the order they were written.

    The configuration file's name the generated headers state comes out
    of the model (``model.device.source``) rather than out of a path the
    caller was given: this stage has to be a function of the model alone,
    or a build from an exported model could not reproduce a direct one
    byte for byte.
    """
    generate = _compiler("generate")
    directory = Path(out_dir)
    written = generate.write_tree(model, out_dir=directory, config_name=model.device.source)
    return GenerationResult(device=model.device.name, out_dir=directory, files=tuple(written))
