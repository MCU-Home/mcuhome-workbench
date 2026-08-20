# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""No workbench module reads process state.

The workbench half of the invariant ``mcuhome-sdk``'s
``tests/python/test_userpaths.py`` states, created from the recipe its module
docstring left behind. What that file *answers* — what
:mod:`mcuhome.model.userpaths` does with an environment — is the model's
and stays there; what it forbids is bigger than one module and has to be
asserted once per repository, because the search runs over
``conftest.PACKAGES`` and the two lists no longer overlap.

ADR 0020 turns this library into one process serving several sessions,
and a call-time read of ``os.environ``, ``Path.home()`` or ``Path.cwd()``
is what makes two sessions in one process answer each other's questions.
The workbench is where that is most tempting: it is the half that runs in
the dashboard's process and in the CLI's, it resolves a configuration
tree from *somewhere*, and it is the half that knows where a private
signing key lives. So this is not a copy kept for symmetry — the module
that had to have the read taken out of it, ``signing.py``, is this
repository's, and it is named below as the canary.

Like the original this reads the syntax tree rather than the text, unlike
the identity invariant in ``test_pairing_workbench.py``. That one forbids
a *name* from appearing anywhere, comments included, because a second
spelling of a Kconfig symbol is the hazard whatever surrounds it. Here
the hazard is a real access: a module that explains in prose why it takes
``env`` instead of reading ``os.environ`` is doing the right thing, and a
text search would fail it for saying so.
"""

from __future__ import annotations

import ast

from conftest import package_modules

#: ``attribute name`` → what to use instead. ``expanduser`` is on the list
#: because it reads ``HOME`` out of the process just as ``Path.home()``
#: does. The one module allowed to do any of this is
#: :mod:`mcuhome.model.userpaths`, which is not in this repository — so
#: unlike the SDK's copy this one has no exemption list at all.
FORBIDDEN_ATTRIBUTES = {
    "environ": "take an env: dict[str, str] argument",
    "getenv": "take an env: dict[str, str] argument",
    "getcwd": "take the directory as an argument",
    "cwd": "take the directory as an argument",
    "home": "mcuhome.model.userpaths.home(env)",
    "expanduser": "mcuhome.model.userpaths.expand(path, env)",
}

#: The modules these names mean something dangerous in. ``from
#: mcuhome.model.userpaths import home`` is the *fix*, not the defect, so the
#: import check asks where a name comes from rather than what it is.
PROCESS_MODULES = {"os", "os.path", "pathlib", "posixpath", "ntpath"}


def _is_main_guard(node: ast.stmt) -> bool:
    """``if __name__ == "__main__":`` — a module's process boundary.

    The guard only runs when the module *is* the process, and then it is
    "whoever started this program": the one caller that owes its callee a
    stated environment and has nowhere to take it from but the process.
    Library imports never execute it, so a read inside it can never make
    two sessions answer each other's questions — which is the hazard this
    whole check exists against.

    No module in this package has one today. The exemption is carried
    anyway, because it is the rule rather than a dispensation for a file:
    the day the workbench grows an entry point, the boundary is where the
    environment may be read and nowhere else.
    """
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def _process_reads(source: str) -> list[str]:
    """Every access in *source* that reaches the process for an answer."""
    found: list[str] = []
    tree = ast.parse(source)
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt) and _is_main_guard(node):
            guarded.update(id(inner) for inner in ast.walk(node))
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRIBUTES:
            found.append(node.attr)
        elif isinstance(node, ast.ImportFrom) and node.module in PROCESS_MODULES:
            found.extend(a.name for a in node.names if a.name in FORBIDDEN_ATTRIBUTES)
    return found


def test_no_workbench_module_reads_process_state() -> None:
    """One process, several sessions, one environment each (ADR 0020).

    ``signing.py`` is asserted to be among the modules examined because
    it is the module that used to hold exactly this read — it resolved
    the per-user key out of whatever environment it happened to run in —
    so a search that stopped reaching it would pass while checking less
    than it did yesterday. It is also the worst place for the read to
    come back: two sessions in one process would sign with each other's
    key, or with a key drawn into the wrong home directory.
    """
    modules = package_modules()
    names = {path.name for path in modules}
    assert "signing.py" in names, (
        "the search no longer reaches the module that used to read the "
        "process — extend conftest.PACKAGES"
    )
    for module in modules:
        reads = _process_reads(module.read_text(encoding="utf-8"))
        assert not reads, (
            f"{module.name} reaches the process for {sorted(set(reads))}; "
            f"use {FORBIDDEN_ATTRIBUTES[reads[0]]} instead"
        )


def test_the_main_guard_is_a_boundary_and_nothing_else_is() -> None:
    """The guard exemption is exactly as wide as the guard.

    Pinned here and not only in the SDK's copy: the helper above is a
    second implementation, and a second implementation that quietly
    treated every ``if`` as a boundary would make the test above pass
    over a package that reads the process everywhere.
    """
    inside = "if __name__ == '__main__':\n    import os\n    x = os.environ\n"
    outside = "import os\nx = os.environ\n"
    assert _process_reads(inside) == []
    assert _process_reads(outside) == ["environ"]
    # Not a boundary, and the two spellings that most look like one.
    assert _process_reads("if __name__ == 'other':\n    import os\n    x = os.environ\n") == [
        "environ"
    ]
    assert _process_reads("if True:\n    import os\n    x = os.environ\n") == ["environ"]


def test_an_import_of_a_forbidden_name_counts_as_a_read() -> None:
    """``from os import environ`` hides the attribute access, not the read.

    The check has two halves for this reason, and the second one is the
    easier to lose in a refactor: after ``from pathlib import Path`` the
    hazard is spelled ``Path.home()`` and the attribute half catches it,
    but after ``from os import getcwd`` there is no attribute left to see.
    """
    assert _process_reads("from os import environ\n") == ["environ"]
    assert _process_reads("from pathlib import Path\n") == []
    assert _process_reads("from mcuhome.model.userpaths import home\n") == []
