# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Configuration-tree discovery (builder-pipeline.md §2).

A config tree looks like this::

    <root>/
    ├── devices/
    │   └── <name>/
    │       └── main.yaml      # entry point of one device
    ├── shared/                # reusable fragments (schema revision 2)
    ├── components/            # tree-wide custom components
    └── secrets.yaml           # one secrets store for the whole tree

**Root marker rule.** A directory is a config-tree root when it contains

* a ``devices/`` directory (the implicit, zero-ceremony marker), or
* a ``mcuhome.yaml`` file (the explicit marker; reserved as the future
  tree-level configuration file — its *content* is not read in v0.1,
  only its presence counts).

``--config-root`` overrides discovery. Without it the builder walks the
working directory upwards and takes the first directory that carries
either marker; the explicit marker wins when both appear at the same
level. The walk stops at the filesystem root.

**Device resolution.** ``mcuhome validate <device>`` accepts two forms,
tried in this order (builder-pipeline.md §8: "a folder name resolved
against the config tree root; an explicit path works too"):

1. a **device name** — ``<root>/devices/<name>/main.yaml``;
2. an **explicit path** — a device folder (``<path>/main.yaml``) or a
   bare YAML file (``<path>`` itself).

Form 2 is what makes ``mcuhome validate docs/design/examples/foo.yaml``
work on a directory that is not a config tree at all: when no root can be
discovered above an explicitly given file, the file's own directory
becomes the tree root, so ``!secret`` still resolves against a
``secrets.yaml`` sitting next to the config (yaml-schema.md §9).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mcuhome.errors import ConfigError, Location

__all__ = [
    "DEVICES_DIR",
    "DEVICE_ENTRY",
    "MARKER_FILE",
    "SECRETS_FILE",
    "ConfigTree",
    "find_config_root",
    "is_config_root",
    "open_tree",
    "resolve_device",
]

#: Explicit root marker; reserved for tree-level configuration.
MARKER_FILE = "mcuhome.yaml"
#: Implicit root marker and the home of every device folder.
DEVICES_DIR = "devices"
#: Entry point inside a device folder.
DEVICE_ENTRY = "main.yaml"
#: Secrets store, at tree root.
SECRETS_FILE = "secrets.yaml"


@dataclass(frozen=True)
class ConfigTree:
    """A resolved configuration tree."""

    root: Path
    #: True when the root carries a marker; False for the
    #: "bare YAML file, no tree around it" fallback.
    discovered: bool

    @property
    def secrets_file(self) -> Path:
        return self.root / SECRETS_FILE

    @property
    def devices_dir(self) -> Path:
        return self.root / DEVICES_DIR

    def device_names(self) -> list[str]:
        if not self.devices_dir.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.devices_dir.iterdir()
            if entry.is_dir() and (entry / DEVICE_ENTRY).is_file()
        )

    def device_entry(self, name: str) -> Path:
        return self.devices_dir / name / DEVICE_ENTRY


def is_config_root(path: Path) -> bool:
    return (path / MARKER_FILE).is_file() or (path / DEVICES_DIR).is_dir()


def find_config_root(start: Path) -> Path | None:
    """Walk *start* upwards and return the first directory carrying a marker."""
    current = start.resolve()
    if current.is_file():
        current = current.parent
    for candidate in [current, *current.parents]:
        if is_config_root(candidate):
            return candidate
    return None


def open_tree(config_root: Path | None, *, cwd: Path | None = None) -> ConfigTree:
    """Resolve the config tree from an explicit root or by discovery."""
    cwd = (cwd or Path.cwd()).resolve()
    if config_root is not None:
        root = config_root.resolve()
        if not root.is_dir():
            raise ConfigError(
                f'The configuration root "{config_root}" does not exist.',
                hint="point --config-root at the directory that contains your devices/ folder",
            )
        if not is_config_root(root):
            raise ConfigError(
                f'"{config_root}" does not look like an MCUHome configuration tree.',
                hint=(
                    f"a configuration root contains a {DEVICES_DIR}/ directory "
                    f"(or a {MARKER_FILE} file); create {config_root}/{DEVICES_DIR}/ "
                    "and put one folder per device in it"
                ),
            )
        return ConfigTree(root=root, discovered=True)

    found = find_config_root(cwd)
    if found is None:
        raise ConfigError(
            "No MCUHome configuration tree found here.",
            hint=(
                f"run the builder inside a directory that has a {DEVICES_DIR}/ folder, "
                "or pass --config-root /path/to/config, or give the path of a device "
                "configuration file directly"
            ),
        )
    return ConfigTree(root=found, discovered=True)


def _looks_like_path(spec: str) -> bool:
    return "/" in spec or "\\" in spec or spec.endswith((".yaml", ".yml"))


def _entry_for_path(path: Path, spec: str) -> Path:
    if path.is_dir():
        entry = path / DEVICE_ENTRY
        if not entry.is_file():
            raise ConfigError(
                f'The device folder "{spec}" has no {DEVICE_ENTRY}.',
                hint=(
                    f"every device is a folder with a {DEVICE_ENTRY} entry point; "
                    f"create {spec}/{DEVICE_ENTRY}"
                ),
            )
        return entry
    if path.is_file():
        return path
    raise ConfigError(
        f'No configuration found at "{spec}".',
        hint="check the path, or pass a device name that exists under devices/",
    )


def resolve_device(
    spec: str,
    *,
    config_root: Path | None = None,
    cwd: Path | None = None,
) -> tuple[ConfigTree, Path]:
    """Resolve a ``<device>`` CLI argument to its tree and entry file.

    Returns ``(tree, entry_file)``. See the module docstring for the rules.
    """
    cwd = (cwd or Path.cwd()).resolve()
    candidate_path = (cwd / spec).resolve()

    # 1. Device name against the config tree, unless the argument is
    #    obviously a path. An explicit --config-root makes this the only
    #    accepted form for plain names.
    if not _looks_like_path(spec):
        tree = open_tree(config_root, cwd=cwd)
        entry = tree.device_entry(spec)
        if entry.is_file():
            return tree, entry
        if config_root is not None or not candidate_path.exists():
            known = tree.device_names()
            listing = ", ".join(known) if known else "none yet"
            raise ConfigError(
                f'There is no device called "{spec}" in this configuration tree.',
                location=Location(file=tree.root),
                hint=f"devices in {tree.root}: {listing}",
            )

    # 2. Explicit path: a device folder or a bare YAML file.
    entry = _entry_for_path(candidate_path, spec)
    root = find_config_root(entry.parent)
    if root is None:
        # A config file outside any tree — its own directory is the tree,
        # which is what makes secrets.yaml next to the file work.
        return ConfigTree(root=entry.parent, discovered=False), entry
    return ConfigTree(root=root, discovered=True), entry
