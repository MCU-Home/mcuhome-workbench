# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""This distribution's dependencies, which cannot be written down.

The same one field, for the same reason as in
``packaging/workbench/setup.py``: ADR 0020 decision 8 makes the pin on
the sibling distribution exact, and an exact pin spelled out here would
be a second place the release version lives.

``mcuhome-workbench`` and not ``mcuhome-model``, because
:mod:`mcuhome.compiler.abi` imports the workbench at module level and
gets the model through it.
"""

import sys
from pathlib import Path

from setuptools import setup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mcuhome.model import __version__  # noqa: E402 - needs the path above

setup(
    install_requires=[
        f"mcuhome-workbench=={__version__}",
        # The local build method (mcuhome.compiler.localbackend) unpacks the
        # SDK package, and that package is a tar.zst (E41) — so it needs a
        # zstd binding. zstandard is the one the build server already uses
        # for the same archive family, kept the same here on purpose.
        "zstandard>=0.22",
    ]
)
