# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Three distributions out of one source tree (ADR 0020 decisions 1, 8).

The tree under ``mcuhome/`` is a PEP 420 namespace with one subpackage
per distribution, and the project files that ship them live under
``packaging/``. Two properties hold that arrangement together, and
neither is visible from the code:

* **every subpackage is shipped by exactly one distribution.** Three
  project files claiming file subsets of one directory is a relationship
  pip cannot express (ADR 0020's consequence on the migration), so the
  claims have to partition — and a fourth subpackage that no project file
  names would simply never be installed by anyone.
* **all three carry the same version, from the same place.** ADR 0017 §3
  and ADR 0020 decision 8: one version, one tag, one release, and
  therefore no "which package works with which SDK" to answer. A second
  literal of the version number anywhere is the beginning of that
  question.

Read out of the ``pyproject.toml`` files with the standard library's own
TOML parser rather than out of built artifacts: a test that builds three
wheels needs a network for the build backend, and this suite is the fast
half of the strategy (builder-pipeline.md §9). What a built wheel
actually contains is proved once, at migration time, against the wheels.

What is *not* here is anything that is a claim about
``mcuhome-workbench`` alone — its ``remote`` extra lives in
``test_packaging_workbench.py``, on the other side of the ADR 0024 cut.
"""

from __future__ import annotations

import tomllib
from importlib import metadata

import pytest
from conftest import NAMESPACE, NAMESPACE_DIR, PACKAGES, REPO_ROOT

import mcuhome.model

PACKAGING_DIR = REPO_ROOT / "packaging"

#: ``<import package> -> <distribution>``. Both halves are asserted
#: against reality below; the mapping itself is what a reader needs.
DISTRIBUTIONS = {
    "mcuhome.compiler": "mcuhome-compiler",
    "mcuhome.model": "mcuhome-model",
    "mcuhome.workbench": "mcuhome-workbench",
}

#: Where the one version lives (``mcuhome/model/__init__.py``), spelled
#: the way ``[tool.setuptools.dynamic]`` has to spell it.
VERSION_ATTR = "mcuhome.model.__version__"


def _project_files() -> dict[str, dict]:
    """Every distribution's parsed ``pyproject.toml``, by directory name."""
    found = {
        path.parent.name: tomllib.loads(path.read_text(encoding="utf-8"))
        for path in sorted(PACKAGING_DIR.glob("*/pyproject.toml"))
    }
    assert found, f"no project files under {PACKAGING_DIR}"
    return found


def test_every_subpackage_is_shipped_by_exactly_one_distribution() -> None:
    shipped: dict[str, str] = {}
    for directory, project in _project_files().items():
        packages = project["tool"]["setuptools"]["packages"]
        assert packages == [f"{NAMESPACE}.{directory}"], (
            f"packaging/{directory} ships {packages}; a distribution owns its "
            "own subpackage and nothing else, or two of them claim one file"
        )
        for name in packages:
            assert name not in shipped, f"{name} is shipped twice ({shipped[name]}, {directory})"
            shipped[name] = directory
    assert set(shipped) == set(PACKAGES), (
        f"the tree holds {sorted(PACKAGES)} but packaging/ ships {sorted(shipped)} — "
        "a subpackage nothing ships is a subpackage nobody can install"
    )


def test_the_package_directory_maps_onto_the_shared_tree() -> None:
    """The project files are elsewhere; the code they ship is not copied.

    ``mcuhome/`` has to sit at the repository root because that root is
    also the SDK package: a build container puts it on ``PYTHONPATH``
    (``containers/build-container/run``) and ``bin/generate`` inserts it into
    ``sys.path``. So the project directories reach up into it instead of
    holding sources of their own.
    """
    for directory, project in _project_files().items():
        package_dir = project["tool"]["setuptools"]["package-dir"]
        assert package_dir == {"": "../.."}, f"packaging/{directory} maps {package_dir}"
        # `build/` and `*.egg-info/` are what a build backend leaves
        # behind; they are gitignored and mean nothing here.
        sources = [
            path.name
            for path in (PACKAGING_DIR / directory).iterdir()
            if path.is_dir() and path.name != "build" and not path.name.endswith(".egg-info")
        ]
        assert not sources, (
            f"packaging/{directory} has grown {sources} — the one source tree "
            "is mcuhome/, and a second copy of a module is not one"
        )


@pytest.mark.parametrize(("package", "distribution"), sorted(DISTRIBUTIONS.items()))
def test_the_distribution_name_is_the_one_the_adr_names(package: str, distribution: str) -> None:
    project = _project_files()[package.rpartition(".")[2]]
    assert project["project"]["name"] == distribution


def test_all_three_read_the_one_version_from_the_one_place() -> None:
    for name, project in _project_files().items():
        assert "version" in project["project"]["dynamic"], (
            f"packaging/{name} pins a version literal; ADR 0020 decision 8 "
            "gives the three distributions one shared version"
        )
        source = project["tool"]["setuptools"]["dynamic"]["version"]
        assert source == {"attr": VERSION_ATTR}, f"packaging/{name} reads {source}"


def test_the_installed_distributions_all_carry_that_version() -> None:
    """And it is the version the code answers with, not a stale install.

    The property ADR 0017 §3 buys is that "which package works with which
    SDK" cannot be asked. It stops being true the moment two of the three
    are installed from different releases, which no amount of metadata
    prevents — so it is checked where it can be seen, in the environment
    the suite runs in.
    """
    for distribution in sorted(DISTRIBUTIONS.values()):
        assert metadata.version(distribution) == mcuhome.model.__version__


def test_no_module_sits_outside_the_three_subpackages() -> None:
    """Restated against the tree, because ``package_modules`` is lazy.

    The whole-package invariants check this on their way past, but only
    when one of them runs. It is the layout rule of the migration and
    deserves a name of its own: a module directly under the namespace
    ships in no wheel, and PEP 420 forbids the ``__init__.py`` that would
    make it ship in all three.
    """
    assert not list(NAMESPACE_DIR.glob("*.py"))
    assert {path.name for path in NAMESPACE_DIR.iterdir() if path.is_dir()} - {"__pycache__"} == {
        name.rpartition(".")[2] for name in PACKAGES
    }


def test_the_sdk_entry_point_still_names_a_program_that_exists() -> None:
    """``mcuhome-sdk.json`` is unchanged by the split, and must stay true."""
    import json

    sdk = json.loads((REPO_ROOT / "mcuhome-sdk.json").read_text(encoding="utf-8"))
    program = REPO_ROOT / sdk["generate"]["program"]
    assert program.is_file()
    source = program.read_text(encoding="utf-8")
    assert "from mcuhome.compiler.sdkentry import main" in source


def test_the_container_launcher_execs_the_module_that_holds_the_abi() -> None:
    """``containers/build-container/run`` is image content and names an import path.

    It is the one file outside Python that spells the ABI module, and
    getting it wrong is an exit 70 on a real backend rather than a test
    failure — which is why the revision was bumped for it (r6).
    """
    launcher = (REPO_ROOT / "containers" / "build-container" / "run").read_text(encoding="utf-8")
    assert "-m mcuhome.compiler.abi" in launcher
    assert "$SDK/mcuhome/compiler/abi.py" in launcher


def test_the_pyproject_at_the_root_ships_nothing() -> None:
    """The plain name ``mcuhome`` belongs to the command line (decision 2).

    A ``[project]`` table here would either claim that name or invent a
    fourth distribution nobody publishes. What the file keeps is the tool
    configuration, which is why it still exists at all.
    """
    root = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "project" not in root
    assert "build-system" not in root
    assert {"ruff", "pytest", "codespell"} <= set(root["tool"])


def test_only_the_model_distribution_is_dependency_free() -> None:
    """The build server consumes ``mcuhome-model`` and nothing else.

    ADR 0020 decision 4. The package it consumes carrying no third-party
    dependency at all is not decoration: it is what makes "orchestrates,
    never builds" survive contact with a dependency resolver.
    """
    requires = {
        name: metadata.requires(distribution) or [] for name, distribution in DISTRIBUTIONS.items()
    }
    assert requires["mcuhome.model"] == []
    assert f"mcuhome-model=={mcuhome.model.__version__}" in requires["mcuhome.workbench"]
    assert f"mcuhome-workbench=={mcuhome.model.__version__}" in requires["mcuhome.compiler"]


def test_the_import_edges_follow_the_dependency_arrows() -> None:
    """model imports model; workbench never imports compiler. As syntax.

    The fresh-venv proof of the migration demonstrated both once, at
    install level; this is the same fact as a permanent invariant, read
    from the syntax tree so it holds on every run rather than on the day
    somebody installs a distribution alone. The dependency arrows are
    ADR 0020's: model depends on nothing, workbench on model, compiler
    on both — and an import against the arrow is a wheel that breaks
    only in the environment of whoever installed the smaller set, which
    is the quietest possible way to break.
    """
    import ast

    allowed = {
        "model": {"model"},
        "workbench": {"model", "workbench"},
        "compiler": {"model", "workbench", "compiler"},
    }
    for package, may_use in allowed.items():
        for module in (NAMESPACE_DIR / package).glob("*.py"):
            for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
                found: str | None = None
                if isinstance(node, ast.ImportFrom) and node.module:
                    parts = node.module.split(".")
                    if parts[0] == "mcuhome" and len(parts) > 1:
                        found = parts[1]
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        parts = alias.name.split(".")
                        if parts[0] == "mcuhome" and len(parts) > 1:
                            found = parts[1]
                if found is not None:
                    assert found in may_use, (
                        f"{package}/{module.name} imports mcuhome.{found} — against "
                        f"the dependency arrow; {package} may use {sorted(may_use)}"
                    )
