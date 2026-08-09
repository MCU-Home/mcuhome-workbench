# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Configuration-tree discovery and device resolution."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FIXTURE_TREE

from mcuhome.errors import ConfigError
from mcuhome.tree import ConfigTree, find_config_root, open_tree, resolve_device


def make_tree(root: Path, *, marker_file: bool = False, devices: tuple[str, ...] = ()) -> Path:
    if marker_file:
        (root / "mcuhome.yaml").write_text("# tree configuration\n", encoding="utf-8")
    for name in devices:
        folder = root / "devices" / name
        folder.mkdir(parents=True)
        (folder / "main.yaml").write_text("device:\n  name: x\n", encoding="utf-8")
    if devices == () and not marker_file:
        (root / "devices").mkdir()
    return root


def test_devices_directory_marks_a_root(tmp_path: Path) -> None:
    make_tree(tmp_path, devices=("a",))
    assert find_config_root(tmp_path) == tmp_path


def test_marker_file_marks_a_root_without_devices(tmp_path: Path) -> None:
    (tmp_path / "mcuhome.yaml").write_text("# tree configuration\n", encoding="utf-8")
    assert find_config_root(tmp_path) == tmp_path


def test_discovery_walks_upwards(tmp_path: Path) -> None:
    make_tree(tmp_path, devices=("a",))
    deep = tmp_path / "devices" / "a"
    assert find_config_root(deep) == tmp_path


def test_discovery_stops_at_the_filesystem_root(tmp_path: Path) -> None:
    assert find_config_root(tmp_path) is None


def test_open_tree_rejects_a_directory_that_is_not_a_tree(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        open_tree(tmp_path, cwd=tmp_path)
    assert "does not look like an MCUHome configuration tree" in caught.value.message
    assert "devices/" in (caught.value.hint or "")


def test_open_tree_rejects_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        open_tree(tmp_path / "nope", cwd=tmp_path)
    assert "does not exist" in caught.value.message


def test_device_resolves_by_name(tmp_path: Path) -> None:
    make_tree(tmp_path, devices=("bedroom",))
    tree, entry = resolve_device("bedroom", config_root=tmp_path, cwd=tmp_path)
    assert tree.root == tmp_path
    assert entry == tmp_path / "devices" / "bedroom" / "main.yaml"


def test_unknown_device_name_lists_what_exists(tmp_path: Path) -> None:
    make_tree(tmp_path, devices=("bedroom", "office"))
    with pytest.raises(ConfigError) as caught:
        resolve_device("kitchen", config_root=tmp_path, cwd=tmp_path)
    assert 'no device called "kitchen"' in caught.value.message
    assert "bedroom, office" in (caught.value.hint or "")


def test_device_resolves_by_folder_path(tmp_path: Path) -> None:
    make_tree(tmp_path, devices=("bedroom",))
    tree, entry = resolve_device("devices/bedroom", cwd=tmp_path)
    assert tree.root == tmp_path
    assert entry == tmp_path / "devices" / "bedroom" / "main.yaml"


def test_device_folder_without_entry_point_is_reported(tmp_path: Path) -> None:
    make_tree(tmp_path, devices=("bedroom",))
    (tmp_path / "devices" / "bedroom" / "main.yaml").unlink()
    with pytest.raises(ConfigError) as caught:
        resolve_device("devices/bedroom", cwd=tmp_path)
    assert "has no main.yaml" in caught.value.message


def test_bare_file_outside_a_tree_uses_its_own_directory(tmp_path: Path) -> None:
    config = tmp_path / "thing.yaml"
    config.write_text("device:\n  name: x\n", encoding="utf-8")
    tree, entry = resolve_device("thing.yaml", cwd=tmp_path)
    assert entry == config
    assert tree.root == tmp_path
    assert tree.discovered is False
    assert tree.secrets_file == tmp_path / "secrets.yaml"


def test_missing_path_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        resolve_device("nowhere.yaml", cwd=tmp_path)
    assert "No configuration found" in caught.value.message


def test_fixture_tree_lists_its_devices() -> None:
    tree = ConfigTree(root=FIXTURE_TREE, discovered=True)
    assert tree.device_names() == ["bench-node"]
