# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Stand-in for CHIP's `python_path` helper, which is missing from the
v1.5.1.0 release tarball (their CI provides it via the pigweed bootstrap
environment). Provides the context manager used by scripts/codegen.py and
scripts/codegen_paths.py to import py_matter_idl from the source tree."""

import contextlib
import os
import sys


class PythonPath:
    def __init__(self, *paths, relative_to=None):
        base = os.path.dirname(os.path.abspath(relative_to)) if relative_to else os.getcwd()
        self.paths = [os.path.normpath(os.path.join(base, p)) for p in paths]

    def __enter__(self):
        for p in reversed(self.paths):
            sys.path.insert(0, p)
        return self

    def __exit__(self, *exc):
        for p in self.paths:
            with contextlib.suppress(ValueError):
                sys.path.remove(p)
        return False
