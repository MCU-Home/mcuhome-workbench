# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The SDK entry point's import closure is stdlib plus ``mcuhome``.

The build-container contract promises the entry point exactly the
runtime the SDK package declares (§6.1) — today ``python3``, a bare
interpreter. The image provides its *own* program a full environment,
but ``bin/generate`` runs as a child with the mounted SDK on
``sys.path`` and nothing else, so every module it imports, transitively,
must come from the standard library or from the SDK tree itself.

This broke for real before it was pinned: ``mcuhome.compiler.abi``
imported ``read_model`` off ``mcuhome.workbench.api``, whose module
level pulls the YAML loader — and the first in-container ``build`` died
on ``ModuleNotFoundError: ruamel`` before the request document was even
parsed. The reader moved to ``mcuhome.model.modelfile`` (the package
that is dependency-free by construction, because it serves both sides
of the contract), and this test holds the door shut behind it.

A subprocess, not an in-process import: the suite's own imports have
long since dragged third-party modules into ``sys.modules``, so only a
fresh interpreter shows what the entry point alone pulls in.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: What a fresh interpreter loads after importing the entry point's
#: body, minus the standard library — computed inside the subprocess so
#: the parent's environment cannot leak into the answer.
_PROBE = """
import json
import sys

import mcuhome.compiler.sdkentry  # noqa: F401 - the import IS the probe

stdlib = set(sys.stdlib_module_names)
tops = {name.split(".")[0] for name in sys.modules}
print(json.dumps(sorted(t for t in tops if t not in stdlib and not t.startswith("_"))))
"""


def test_sdk_entry_point_closure_is_stdlib_plus_mcuhome():
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=REPO_ROOT,
        env={"PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, (
        "importing mcuhome.compiler.sdkentry failed — under §6.1's bare "
        f"runtime this is the container's crash:\n{completed.stderr}"
    )
    loaded = json.loads(completed.stdout)
    assert loaded == ["mcuhome"], (
        "the SDK entry point's import closure reaches beyond the standard "
        f"library and the SDK tree: {[m for m in loaded if m != 'mcuhome']} — "
        "the build container provides none of these (contract §6.1)"
    )
