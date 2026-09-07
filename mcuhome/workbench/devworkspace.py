# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""A west workspace somebody maintains, described the way a builder reads one.

Development builds exist for working on the SDK itself: the person has a
west workspace of their own — sources checked out, the SDK as its manifest
repository, tools installed wherever they like and on ``PATH`` — and wants
a device of theirs compiled against exactly that, without MCUHome
provisioning anything or touching a byte of it.

The builder, though, is told where its trees are by the **workspace
package** it runs out of: ``MCUHOME_BUILD_ENV_WORKSPACE`` names a
directory, ``build-workspace.json`` in it says where the west workspace
and the workspace record are, and the record says where each layer is. A
workspace somebody checked out by hand carries neither document, and it
must not be given them — writing into the tree is exactly what this mode
promises not to do.

So this module writes those two documents **into the session**, pointing
at the workspace from outside it, and the builder reads a normal
environment. Nothing is written into the workspace, nothing is copied out
of it, and the builder needs no idea that this build is different from
any other — which is the whole point: one code path in the builder, the
difference paid for here.

**Where the layer paths come from.** From west, asked in the workspace
itself, because most of the projects are not in the SDK's ``west.yml`` at
all — they arrive through the ``import:`` of the ``zephyr`` project and
their paths are Zephyr's choice. The SDK carries a second implementation
of this document, ``containers/build-container/workspace-record.py``,
which writes it while a *package* is being built. The two are not shared
code: this package depends on ``mcuhome-model`` and not on the compiler,
and that script runs inside an image build where no MCUHome package is on
the interpreter's path. What they do share is the mapping below, and it is
the SDK's manifest that decides it.
"""

from __future__ import annotations

import configparser
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

from mcuhome.workbench.buildenvstore import BuildEnvironmentError

__all__ = [
    "LAYER_PROJECTS",
    "MANIFEST_DEFAULT_FILE",
    "RECORD_FILE",
    "WEST_CONFIG",
    "WORKSPACE_MANIFEST",
    "manifest_checkout",
    "write_environment",
]

#: What makes a directory a west workspace: west writes it on ``west
#: init`` and reads the manifest repository's location out of it. The one
#: thing this mode checks for, because it is the one thing it needs.
WEST_CONFIG = Path(".west") / "config"

#: The manifest file name west assumes when ``.west/config`` names none.
MANIFEST_DEFAULT_FILE = "west.yml"

#: Layer name -> the name the west manifest gives that project. ``sdk`` is
#: not here: it is the manifest repository itself, and its path comes from
#: ``.west/config`` rather than from the project list.
LAYER_PROJECTS = {
    "zephyr": "zephyr",
    "chip": "connectedhomeip",
    "mcuboot": "mcuboot",
}

#: The two documents the builder reads, written into the session.
WORKSPACE_MANIFEST = "build-workspace.json"
RECORD_FILE = "workspace.json"

#: What the record says about how its trees were fetched. Consumers
#: compare this token and nothing parses it; a developer's workspace was
#: fetched by the developer, and that is all anybody can say about it.
CLONE_KIND = "developer"


def manifest_checkout(workspace: Path) -> Path:
    """*workspace* is a west workspace with a manifest repository, or a refusal.

    The only check this mode makes, and deliberately the only one: a
    development build is a build against trees nobody vouches for, so
    there is nothing else here that could be verified rather than
    guessed. What has to hold is that west can work in the directory and
    that the SDK this build compiles is actually there — the manifest
    repository *is* the SDK under development.

    Returns the manifest repository's directory.
    """
    workspace = Path(workspace)
    config = workspace / WEST_CONFIG
    if not config.is_file():
        raise BuildEnvironmentError(
            f"There is no west workspace at {workspace}.",
            hint=(
                "build.dev_workspace names the top directory of a west workspace you "
                "maintain — the directory holding .west/, zephyr/ and your SDK "
                "checkout. Unset it to build against the build environment MCUHome "
                "unpacks itself."
            ),
        )
    path, _file = _manifest_location(workspace, config)
    checkout = workspace / path
    if not checkout.is_dir():
        raise BuildEnvironmentError(
            f"The west workspace at {workspace} has no manifest repository at {checkout}.",
            hint=(
                "the manifest repository of that workspace is the SDK a development "
                "build compiles — run west update in the workspace, or point "
                "build.dev_workspace at a workspace whose manifest repository is "
                "checked out"
            ),
        )
    return checkout


def write_environment(workspace: Path, into: Path, *, env: Mapping[str, str]) -> Path:
    """Describe *workspace* under *into*, and answer with the root to name.

    Two files, and neither of them goes anywhere near the workspace:

    ``build-workspace.json``
        Where the west workspace and the record are. The workspace is
        named by its **absolute** path — a package states a path inside
        itself and this states one outside, which is the one difference
        between a described workspace and a packaged one, and it is why
        the builder's messages name the developer's own directories
        rather than a scratch path under a build directory.
    ``workspace.json``
        The record: the top directory, the manifest repository's
        location, and one entry per layer.

    No ``matter-pregen-chip-root``: the pre-generated Matter data model is
    something the *package* build produces, and a workspace somebody
    checked out has none. A build that needs it runs the generator the
    ordinary way, with the tools the developer has.
    """
    workspace = Path(workspace).resolve()
    into = Path(into)
    into.mkdir(parents=True, exist_ok=True)
    checkout = manifest_checkout(workspace)
    path, file = _manifest_location(workspace, workspace / WEST_CONFIG)
    layers: dict[str, dict[str, object]] = {
        name: {"path": str(tree)} for name, tree in _layer_paths(workspace, env=env).items()
    }
    layers["sdk"] = {"path": str(checkout), "mounted": True}
    _write(
        into / RECORD_FILE,
        {
            "workspace": 1,
            "topdir": str(workspace),
            "manifest": {"path": path, "file": file},
            "clone": CLONE_KIND,
            "layers": layers,
        },
    )
    _write(
        into / WORKSPACE_MANIFEST,
        {
            # Neither is a package: these bytes were never published, so
            # there is no name and no version anybody could have checked
            # them against, and an invented one would be read as a claim.
            "package": "",
            "version": "",
            # Absolute on purpose — see the docstring. A reader joins it
            # onto this directory, and joining an absolute path yields the
            # absolute path, which is the workspace itself.
            "workspace": str(workspace),
            "workspace-record": RECORD_FILE,
            "manifest-directory": str(checkout),
        },
    )
    return into


def _manifest_location(workspace: Path, config: Path) -> tuple[str, str]:
    """``manifest.path`` and ``manifest.file``, as ``west init`` wrote them."""
    parser = configparser.ConfigParser()
    try:
        parser.read(config)
        path = parser["manifest"]["path"]
    except (configparser.Error, KeyError) as unusable:
        raise BuildEnvironmentError(
            f"The west configuration at {config} does not say where the manifest "
            f"repository is ({unusable}).",
            hint=(
                "a usable west workspace states manifest.path in .west/config — run "
                "west init in the workspace, or point build.dev_workspace somewhere else"
            ),
        ) from unusable
    return path, parser["manifest"].get("file", MANIFEST_DEFAULT_FILE)


def _layer_paths(workspace: Path, *, env: Mapping[str, str]) -> dict[str, Path]:
    """Where the three patchable layers are, asked of west in *workspace*.

    ``west list`` rather than the manifest file, because most projects of
    this workspace are not in the SDK's manifest: they come through the
    ``import:`` of the ``zephyr`` project and where each one sits is
    Zephyr's decision. West is the only thing that knows the answer, and
    it is on the developer's ``PATH`` by the same token that makes this
    mode work at all.
    """
    try:
        completed = subprocess.run(
            ["west", "list", "-f", "{name}\t{abspath}"],
            check=False,
            capture_output=True,
            text=True,
            cwd=workspace,
            env=dict(env),
        )
    except OSError as unusable:
        raise BuildEnvironmentError(
            f"MCUHome cannot run west in {workspace}: {unusable.strerror}.",
            hint=(
                "a development build asks west where the workspace's projects are, so "
                "west has to be on the PATH the build is started from"
            ),
        ) from unusable
    if completed.returncode != 0:
        raise BuildEnvironmentError(
            f"west cannot read the workspace at {workspace}: "
            f"{(completed.stderr or completed.stdout).strip()}",
            hint=(
                "MCUHome builds what west resolves in that workspace — fix the "
                "workspace (west update), or point build.dev_workspace elsewhere"
            ),
        )
    projects = {}
    for line in completed.stdout.splitlines():
        name, _, absolute = line.partition("\t")
        if name and absolute:
            projects[name] = Path(absolute)
    found = {
        layer: projects[project] for layer, project in LAYER_PROJECTS.items() if project in projects
    }
    missing = sorted(set(LAYER_PROJECTS) - set(found))
    if missing:
        raise BuildEnvironmentError(
            f"The west workspace at {workspace} has no "
            f"{', '.join(LAYER_PROJECTS[layer] for layer in missing)} project.",
            hint=(
                "MCUHome builds firmware out of that workspace's Zephyr, MCUboot and "
                "Matter trees — run west update in it, or point build.dev_workspace at "
                "a workspace whose manifest is MCUHome's"
            ),
        )
    return found


def _write(path: Path, document: dict[str, object]) -> None:
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
