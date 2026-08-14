# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The ``mcuhome-workbench`` distribution, alone in its own repository.

One repository, one distribution, one project file — at the root, since
ADR 0024. There is no ``packaging/`` directory here any more: it existed
to keep three project files apart inside one repository, and two of the
three left with ``mcuhome-sdk``. What the SDK repository's
``test_packaging.py`` still holds is everything that is a claim about
*that* set — the partition of its subpackages, the one version its two
distributions share; nothing there says anything about this one.

The claims that matter here are the mirror image of the split. Before
ADR 0024 ``mcuhome.model`` and ``mcuhome.compiler`` were files in this
tree, and a project file could — and the old ``packaging/`` layout did —
reach across to them with a ``package-dir`` of ``../..``. They are
installed distributions now, and the difference has to be *declared*
rather than merely true on a developer's machine where all three happen
to be checked out side by side. So: this repository ships exactly
``mcuhome.workbench``, it names ``mcuhome-model`` as a dependency, and no
requirement of it points at a directory.

Read out of ``pyproject.toml`` with the standard library's own TOML
parser rather than out of a built artifact: a test that builds a wheel
needs a network for the build backend, and this suite is the fast half of
the strategy (builder-pipeline.md §9).
"""

from __future__ import annotations

import ast
import tomllib
from importlib import metadata

import pytest
from conftest import NAMESPACE, NAMESPACE_DIR, PACKAGES, REPO_ROOT

import mcuhome.workbench

#: The one distribution this repository publishes (ADR 0020 decision 1,
#: ADR 0024's assignment of it to the tools repository).
DISTRIBUTION = "mcuhome-workbench"

#: The import package it ships, and the only one.
PACKAGE = "mcuhome.workbench"

PROJECT_FILE = REPO_ROOT / "pyproject.toml"


def _project() -> dict:
    """The repository's one parsed ``pyproject.toml``."""
    return tomllib.loads(PROJECT_FILE.read_text(encoding="utf-8"))


def _requirement_names(requirements: list[str]) -> set[str]:
    """The distribution names out of a ``dependencies`` list."""
    names = set()
    for requirement in requirements:
        head = requirement.split(";", 1)[0].strip()
        for separator in ("@", "[", "=", "<", ">", "!", "~", " "):
            head = head.split(separator, 1)[0]
        names.add(head.strip().lower().replace("_", "-"))
    return names


# --------------------------------------------------------------------------
# One repository, one distribution, at the root
# --------------------------------------------------------------------------


def test_the_distribution_is_the_one_the_adr_names() -> None:
    assert _project()["project"]["name"] == DISTRIBUTION


def test_the_project_file_is_the_root_one_and_there_is_no_packaging_directory() -> None:
    """ADR 0024's shape: nothing to keep apart, so nothing keeping it apart.

    The root file used to ship nothing — it held the tool configuration
    and said so, because ADR 0020 decision 2 reserves the plain name
    ``mcuhome`` for the command line and the three distributions lived
    under ``packaging/``. With one distribution left in this repository
    the indirection buys nothing, so the project table moved up. The tool
    configuration is still here, which is the half that must not have
    been lost in the move.
    """
    root = _project()
    assert "project" in root
    assert "build-system" in root
    assert {"ruff", "pytest", "codespell"} <= set(root["tool"])
    assert not (REPO_ROOT / "packaging").exists(), (
        "packaging/ is back — one repository with one distribution declares "
        "it at the root (ADR 0024)"
    )


def test_the_distribution_and_the_package_carry_one_version() -> None:
    """Two literals, and they have to agree.

    Since the split the workbench states its own version in
    ``mcuhome/workbench/__init__.py`` — it no longer reads
    ``mcuhome.model.__version__``, which versions with the SDK repository
    now (the two are coupled at major.minor from v1.0, ``~=X.Y.0``). The
    project file states it a second time, so the second copy is checked
    against the first here rather than left to a release day. Making the
    project file's version ``dynamic`` from the attribute the way the SDK
    repository does would retire the first assertion; until then this is
    what stands between the two spellings.
    """
    declared = _project()["project"]["version"]
    assert declared == mcuhome.workbench.__version__
    assert metadata.version(DISTRIBUTION) == mcuhome.workbench.__version__


# --------------------------------------------------------------------------
# The split, declared rather than assumed (the inverse of the SDK's claim)
# --------------------------------------------------------------------------


def test_it_ships_exactly_the_workbench_subpackage() -> None:
    """The tree and the project file say the same one thing.

    Both halves are needed. A project file naming a second subpackage
    would ship files this repository does not have; a second subpackage
    appearing in the tree — a stray ``mcuhome/model/`` restored from a
    stale checkout, say — would ship in no wheel *and* shadow the
    installed distribution of that name, because a directory under a PEP
    420 namespace is a portion of it whether or not anybody meant it to
    be.
    """
    packages = _project()["tool"]["setuptools"]["packages"]
    assert packages == [PACKAGE]
    assert list(PACKAGES) == [PACKAGE]
    assert {path.name for path in NAMESPACE_DIR.iterdir() if path.is_dir()} - {"__pycache__"} == {
        PACKAGE.rpartition(".")[2]
    }
    assert not list(NAMESPACE_DIR.glob("*.py")), (
        "a module directly under the namespace ships in no distribution "
        "and PEP 420 forbids the __init__.py that would make it ship"
    )


def test_the_model_is_a_declared_dependency_and_not_a_neighbouring_directory() -> None:
    """ADR 0024's packaging half, from this side.

    ``mcuhome.model`` used to be a directory in this tree; it is a
    distribution now. The whole risk of a split like this is that the
    dependency keeps working on the machines where both repositories are
    checked out next to each other and is nowhere written down — so an
    installer that has only this one gets a package that imports a
    module nobody told it about.
    """
    project = _project()["project"]
    assert "mcuhome-model" in _requirement_names(project["dependencies"])

    # Unpinned while nothing is published; from v1.0 every cross-repo edge
    # becomes ~=X.Y.0. Either way it is a *version* constraint and never a
    # place: a `file://` or a relative path would be exactly the "it works
    # in my workspace" this test exists against.
    for requirement in project["dependencies"] + [
        item for extra in project["optional-dependencies"].values() for item in extra
    ]:
        assert "file://" not in requirement and "@" not in requirement, (
            f"{requirement!r} points at a location; a dependency is named and "
            "versioned, never located (it would not survive leaving this workspace)"
        )

    # And nothing maps a source directory outside this repository, which
    # is what the retired packaging/ layout did (`package-dir = ../..`).
    assert "package-dir" not in _project()["tool"]["setuptools"]


def test_the_declared_dependencies_are_the_ones_the_package_needs() -> None:
    """The runtime set: the model, a line-preserving YAML parser, PEP 440."""
    assert _requirement_names(_project()["project"]["dependencies"]) == {
        "mcuhome-model",
        "ruamel.yaml",
        "packaging",
    }


def test_the_import_edges_follow_the_dependency_arrows() -> None:
    """The workbench imports the model and itself. As syntax.

    The mirror of the SDK repository's assertion about its own two
    packages, and the reason ADR 0020 decision 3 can call the compiler
    edge optional at all: ``mcuhome-compiler`` is an *extra* here, so a
    dashboard install carries none of it, and one ``import
    mcuhome.compiler`` anywhere in this tree — module level or inside a
    function, which is why this reads the syntax tree rather than a fresh
    interpreter's ``sys.modules`` — turns that install into an
    ``ImportError`` at the first build. The call-time resolution lives in
    ``buildmethods.py`` and goes through :func:`importlib.import_module`,
    which is a string and not an import edge.
    """
    may_use = {"model", "workbench"}
    for module in sorted((NAMESPACE_DIR / "workbench").glob("*.py")):
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            found: str | None = None
            if isinstance(node, ast.ImportFrom) and node.module:
                parts = node.module.split(".")
                if parts[0] == NAMESPACE and len(parts) > 1:
                    found = parts[1]
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    if parts[0] == NAMESPACE and len(parts) > 1:
                        found = parts[1]
            if found is not None:
                assert found in may_use, (
                    f"workbench/{module.name} imports {NAMESPACE}.{found} — against "
                    f"the dependency arrow; the workbench may use {sorted(may_use)}"
                )


# --------------------------------------------------------------------------
# The extras
# --------------------------------------------------------------------------
#
# These two tests live in a packaging file, and not beside the
# session-client tests they are about, for one reason: that file is gated
# on the extra plus a sibling checkout of the build server, and a guard
# against a silently dropped extra is worth nothing in the only
# environment that would notice — the one where the extra is not
# installed. They need neither dependency (`sessionclient` imports
# nothing optional at module level), so they belong where the rest of the
# packaging invariants are.


def test_a_missing_extra_is_a_refusal_that_names_the_install() -> None:
    """The refusal a caller without the extra gets, worded like the rest.

    ``mcuhome.compiler.localbuild`` refuses a missing docker by stating
    the fact and naming the fix; a missing transport is the same kind of
    news, and an ``ImportError`` traceback is not.
    """
    from mcuhome.workbench import sessionclient as sc

    with pytest.raises(sc.RemoteDependencyMissing) as refusal:
        sc._require("mcuhome_nothing_like_this", "the test asked for a module nobody has")
    rendered = str(refusal.value)
    assert f"pip install '{DISTRIBUTION}[remote]'" in rendered
    assert "mcuhome_nothing_like_this" in rendered


def test_the_extras_are_declared_in_the_distribution() -> None:
    """The extra the refusal above names is the extra packaging declares.

    A refusal that names an extra nobody declared sends a user to a
    command that does nothing, so the message and the declaration are
    checked against each other rather than each against a reader's
    memory. ``local`` is here for the same reason from the other side:
    it is the only place ``mcuhome-compiler`` is named at all, and the
    build-method dispatch's ``MethodUnavailable`` sends people to it.

    Read out of ``pyproject.toml`` and not out of ``importlib.metadata``:
    an editable install's metadata is as fresh as the last ``pip install
    -e``, so a metadata read would pass or fail on when somebody last ran
    one instead of on what the distribution declares. The old
    ``setup.py``/``dynamic`` trap this test used to cover is gone with
    the ``packaging/`` directory — the guard against it is that the field
    is declared statically here, so it cannot be silently discarded.
    """
    project = _project()["project"]
    assert "optional-dependencies" not in project.get("dynamic", [])
    assert project["optional-dependencies"] == {
        "remote": ["aiohttp>=3.10,<4", "zstandard>=0.22"],
        "local": ["mcuhome-compiler"],
    }
