# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Write the builder image's record of the west workspace it carries.

Runs once, inside the image build (``containers/builder/Dockerfile``),
after ``west update`` and after the patch set has been applied. It writes
``/mcuhome/workspace.json``: what the baked workspace is, which commit
each layer actually resolved to, and which patches were applied on top.

**Why it exists.** The image digest pins the workspace, which is the
point of baking it — but a digest says "the same", not "what". Two of the
revisions in ``west.yml`` are *tags*, and a tag is movable at the remote,
so a rebuild of the same Dockerfile can silently produce a different
workspace. Recording the resolved 40-character commit per layer turns
that from something to trust into something to check. The same argument
ADR 0018 makes with ``sdk.sha256``, and the reason a build container is
recorded by digest rather than by tag once a backend has chosen one.

**Why the layer names are the contract's.** ``zephyr``, ``sdk``, ``chip``
and ``mcuboot`` are the layer registry of
``docs/design/build-container-contract.md`` §2.1, so a later ``describe``
fills its ``trees`` block by lookup rather than by translation.

**Why patches are identified by digest.** ``patches/README.md`` says to
regenerate a patch *in place*, keeping its file name — so the name is not
an identity and a name alone cannot tell two patch sets apart. The
SHA-256 can.

The ``sdk`` layer is recorded with ``mounted: true`` and no version: it
is deliberately not in the image (ADR 0018 makes it a hash-pinned package
fetched per build), and the contract already models exactly that case as
a ``trees`` entry without a version.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import subprocess
import sys
from pathlib import Path

#: Contract layer name -> the name the west manifest gives that project.
#: ``sdk`` is not here: it is the manifest repository itself, and its path
#: comes from ``.west/config`` rather than from the project list.
LAYER_PROJECTS = {
    "zephyr": "zephyr",
    "chip": "connectedhomeip",
    "mcuboot": "mcuboot",
}

#: The layer the SDK mounts into — the manifest repository's directory.
SDK_LAYER = "sdk"


def _git(repository: Path, *arguments: str) -> str:
    """One git command in one repository, or a loud failure."""
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _west_projects(topdir: Path) -> dict[str, dict[str, str]]:
    """Every manifest project as ``name -> {path, revision}``.

    Read out of west rather than out of ``west.yml``: most of these
    projects are not in ``west.yml`` at all, they come from the ``import:``
    of the ``zephyr`` project, and their paths are Zephyr's choice.
    """
    completed = subprocess.run(
        ["west", "list", "-f", "{name}\t{abspath}\t{revision}"],
        check=True,
        capture_output=True,
        text=True,
        cwd=topdir,
    )
    projects: dict[str, dict[str, str]] = {}
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        name, path, revision = line.split("\t", 2)
        projects[name] = {"path": path, "revision": revision}
    return projects


def _manifest_location(topdir: Path) -> tuple[str, str]:
    """``manifest.path`` and ``manifest.file`` as ``west init`` wrote them."""
    config = configparser.ConfigParser()
    config.read(topdir / ".west" / "config")
    return config["manifest"]["path"], config["manifest"].get("file", "west.yml")


def _patch_record(patch: Path) -> dict[str, str]:
    """A patch as it is identified: file name for humans, digest for us."""
    return {
        "file": patch.name,
        "sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
    }


def _parse_patch_argument(value: str) -> tuple[str, Path]:
    layer, _, path = value.partition("=")
    if not layer or not path:
        raise argparse.ArgumentTypeError(f"expected <layer>=<path>, got {value!r}")
    if layer not in LAYER_PROJECTS:
        raise argparse.ArgumentTypeError(f"{layer!r} is not one of {sorted(LAYER_PROJECTS)}")
    return layer, Path(path)


def build_record(topdir: Path, clone: str, patches: list[tuple[str, Path]]) -> dict:
    """The whole document, in the order it is meant to be read."""
    projects = _west_projects(topdir)
    manifest_path, manifest_file = _manifest_location(topdir)

    layers: dict[str, dict] = {}
    for layer, project in LAYER_PROJECTS.items():
        if project not in projects:
            raise SystemExit(
                f"west does not know a project named {project!r} — "
                f"the {layer!r} layer cannot be recorded. Known: {sorted(projects)}"
            )
        path = Path(projects[project]["path"])
        layers[layer] = {
            "path": str(path),
            "revision": projects[project]["revision"],
            "commit": _git(path, "rev-parse", "HEAD"),
            "patches": [_patch_record(patch) for name, patch in patches if name == layer],
        }
    layers[SDK_LAYER] = {"path": str(topdir / manifest_path), "mounted": True}

    return {
        "workspace": 1,
        "topdir": str(topdir),
        "manifest": {"path": manifest_path, "file": manifest_file},
        "clone": clone,
        "layers": layers,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--topdir", type=Path, required=True, help="west workspace top directory")
    parser.add_argument("--output", type=Path, required=True, help="where to write the JSON")
    parser.add_argument(
        "--clone",
        default="narrow-depth-1",
        help="how the trees were fetched, as one token; consumers compare it, nothing parses it",
    )
    parser.add_argument(
        "--patch",
        dest="patches",
        action="append",
        default=[],
        type=_parse_patch_argument,
        metavar="LAYER=PATH",
        help="a patch that was applied to LAYER (repeatable)",
    )
    arguments = parser.parse_args(argv)

    record = build_record(arguments.topdir, arguments.clone, arguments.patches)
    arguments.output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
