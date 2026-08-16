# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The project directory: marker, bootstrap ladder, layout, init, hygiene.

The successor of ``test_tree.py``: ADR 0022 replaced the config-tree
discovery (``devices/`` or ``mcuhome.yaml`` as markers) with the one
dedicated marker ``.mcuhome-project-root``, and this file pins the
consequences — most importantly the *negative* ones: the two things
that used to mark a root must not mark one any more, because that is
the entire argument for having a dedicated marker.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from conftest import FIXTURE_TREE
from mcuhome.model.errors import ConfigError

from mcuhome.workbench.project import (
    GITIGNORE_LINES,
    MARKER_FILE,
    PROJECT_DIR_VAR,
    Project,
    check_secret_file,
    ensure_secrets_dir,
    find_project_root,
    init_project,
    is_project_root,
    resolve_device,
    resolve_project,
)
from mcuhome.workbench.projectfile import (
    PROJECT_VERSION,
    ProjectFile,
    new_project_id,
    read_project_file,
    write_project_file,
)


def make_project(root: Path, *, devices: tuple[str, ...] = ()) -> Project:
    root.mkdir(parents=True, exist_ok=True)
    write_project_file(
        root / MARKER_FILE,
        ProjectFile(root=root, version=PROJECT_VERSION, id=new_project_id()),
    )
    for name in devices:
        folder = root / "devices" / name
        folder.mkdir(parents=True)
        (folder / "main.yaml").write_text("device:\n  name: x\n", encoding="utf-8")
    return Project(root=root, discovered=True)


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- the marker -------------------------------------------------------


def test_the_marker_marks_a_project_root(tmp_path: Path) -> None:
    make_project(tmp_path)
    assert is_project_root(tmp_path)
    assert find_project_root(tmp_path) == tmp_path


def test_a_devices_directory_alone_marks_nothing(tmp_path: Path) -> None:
    """The old implicit marker is gone — deliberately (ADR 0022 §1)."""
    (tmp_path / "devices" / "a").mkdir(parents=True)
    assert not is_project_root(tmp_path)
    assert find_project_root(tmp_path) is None


def test_a_mcuhome_yaml_alone_marks_nothing(tmp_path: Path) -> None:
    """A copied config snippet must never turn a folder into a project."""
    (tmp_path / "mcuhome.yaml").write_text("# just configuration\n", encoding="utf-8")
    assert not is_project_root(tmp_path)
    assert find_project_root(tmp_path) is None


def test_discovery_walks_upwards(tmp_path: Path) -> None:
    make_project(tmp_path, devices=("a",))
    deep = tmp_path / "devices" / "a"
    assert find_project_root(deep) == tmp_path


def test_discovery_stops_at_the_filesystem_root(tmp_path: Path) -> None:
    assert find_project_root(tmp_path) is None


# --- the bootstrap ladder (ADR 0022 §2) -------------------------------


def test_no_project_anywhere_is_a_refusal_naming_the_ways_out(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        resolve_project(env={}, cwd=tmp_path)
    assert caught.value.message == "No MCUHome project found here."
    hint = caught.value.hint or ""
    assert MARKER_FILE in hint
    assert "--project-dir" in hint
    assert "mcuhome project init" in hint


def test_an_explicit_project_dir_disables_the_search(tmp_path: Path) -> None:
    inner = tmp_path / "inner"
    make_project(inner)
    make_project(tmp_path)  # would win by discovery from inner
    project = resolve_project(inner, env={}, cwd=tmp_path)
    assert project.root == inner


def test_an_explicit_project_dir_without_marker_is_an_error(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ConfigError) as caught:
        resolve_project(plain, env={}, cwd=tmp_path)
    assert f"has no {MARKER_FILE}" in caught.value.message
    assert "--project-dir" in (caught.value.hint or "")
    assert "mcuhome project init" in (caught.value.hint or "")


def test_a_missing_explicit_project_dir_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        resolve_project(tmp_path / "nope", env={}, cwd=tmp_path)
    assert "does not exist" in caught.value.message
    assert "--project-dir" in caught.value.message


def test_the_environment_variable_is_the_fallback(tmp_path: Path) -> None:
    named = tmp_path / "named"
    make_project(named)
    project = resolve_project(env={PROJECT_DIR_VAR: str(named)}, cwd=tmp_path)
    assert project.root == named


def test_the_argument_beats_the_environment_variable(tmp_path: Path) -> None:
    by_arg = make_project(tmp_path / "by-arg").root
    by_env = make_project(tmp_path / "by-env").root
    project = resolve_project(by_arg, env={PROJECT_DIR_VAR: str(by_env)}, cwd=tmp_path)
    assert project.root == by_arg


def test_the_environment_variable_without_marker_names_itself(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ConfigError) as caught:
        resolve_project(env={PROJECT_DIR_VAR: str(plain)}, cwd=tmp_path)
    assert f"has no {MARKER_FILE}" in caught.value.message
    assert PROJECT_DIR_VAR in (caught.value.hint or "")


def test_a_relative_project_dir_is_relative_to_the_stated_cwd(tmp_path: Path) -> None:
    make_project(tmp_path / "here")
    project = resolve_project("here", env={}, cwd=tmp_path)
    assert project.root == tmp_path / "here"


def test_a_tilde_project_dir_uses_the_stated_home(tmp_path: Path) -> None:
    make_project(tmp_path / "home" / "work")
    project = resolve_project("~/work", env={"HOME": str(tmp_path / "home")}, cwd=tmp_path)
    assert project.root == tmp_path / "home" / "work"


# --- the layout -------------------------------------------------------


def test_the_layout_hangs_off_the_root(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    assert project.config_file == tmp_path / "mcuhome.yaml"
    assert project.secrets_file == tmp_path / "secrets" / "main.yaml"
    assert project.firmware_secrets_file == tmp_path / "secrets" / "firmware" / "mcuboot.yaml"
    assert project.builder_secrets_file("attic") == (
        tmp_path / "secrets" / "build-server" / "attic.yaml"
    )
    assert project.device_secrets_file("porch") == tmp_path / "secrets" / "devices" / "porch.yaml"
    assert project.device_entry("porch") == tmp_path / "devices" / "porch" / "main.yaml"


def test_the_fixture_project_lists_its_devices() -> None:
    project = Project(root=FIXTURE_TREE, discovered=True)
    assert project.device_names() == ["bench-node"]


# --- device resolution ------------------------------------------------


def test_device_resolves_by_name(tmp_path: Path) -> None:
    make_project(tmp_path, devices=("bedroom",))
    project, entry = resolve_device("bedroom", env={}, cwd=tmp_path)
    assert project.root == tmp_path
    assert entry == tmp_path / "devices" / "bedroom" / "main.yaml"


def test_unknown_device_name_lists_what_exists(tmp_path: Path) -> None:
    make_project(tmp_path, devices=("bedroom", "office"))
    with pytest.raises(ConfigError) as caught:
        resolve_device("kitchen", env={}, cwd=tmp_path)
    assert 'no device called "kitchen"' in caught.value.message
    assert "bedroom, office" in (caught.value.hint or "")


def test_device_resolves_by_folder_path(tmp_path: Path) -> None:
    make_project(tmp_path, devices=("bedroom",))
    project, entry = resolve_device("devices/bedroom", env={}, cwd=tmp_path)
    assert project.root == tmp_path
    assert entry == tmp_path / "devices" / "bedroom" / "main.yaml"


def test_device_folder_without_entry_point_is_reported(tmp_path: Path) -> None:
    make_project(tmp_path, devices=("bedroom",))
    (tmp_path / "devices" / "bedroom" / "main.yaml").unlink()
    with pytest.raises(ConfigError) as caught:
        resolve_device("devices/bedroom", env={}, cwd=tmp_path)
    assert "has no main.yaml" in caught.value.message


def test_bare_file_outside_a_project_uses_its_own_directory(tmp_path: Path) -> None:
    config = tmp_path / "thing.yaml"
    config.write_text("device:\n  name: x\n", encoding="utf-8")
    project, entry = resolve_device("thing.yaml", env={}, cwd=tmp_path)
    assert entry == config
    assert project.root == tmp_path
    assert project.discovered is False
    assert project.secrets_file == tmp_path / "secrets" / "main.yaml"


def test_missing_path_is_reported(tmp_path: Path) -> None:
    make_project(tmp_path)
    with pytest.raises(ConfigError) as caught:
        resolve_device("nowhere.yaml", env={}, cwd=tmp_path)
    assert "No configuration found" in caught.value.message


# --- mcuhome project init (ADR 0022 §1) ---------------------------------------


def test_init_creates_the_durable_layout(tmp_path: Path) -> None:
    target = tmp_path / "fresh"
    result = init_project(target)
    assert is_project_root(target)
    written = read_project_file(target / MARKER_FILE)
    assert written.version == PROJECT_VERSION
    assert written.id is not None
    assert (target / "mcuhome.yaml").is_file()
    assert (target / "devices").is_dir()
    assert (target / "secrets").is_dir()
    assert mode_of(target / "secrets") == 0o700
    assert (target / ".gitignore").read_text(encoding="utf-8") == "secrets/\nbuild/\n"
    names = [path.name for path in result.created]
    assert names == [MARKER_FILE, "mcuhome.yaml", "devices", "secrets", ".gitignore"]


def test_init_refuses_a_non_empty_directory_listing_what_is_there(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        init_project(tmp_path)
    assert "is not empty" in caught.value.message
    assert "notes.txt" in caught.value.message
    assert "--force" in (caught.value.hint or "")


def test_init_force_proceeds_but_keeps_the_users_configuration(tmp_path: Path) -> None:
    (tmp_path / "mcuhome.yaml").write_text("jobs: 4\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    init_project(tmp_path, force=True)
    assert is_project_root(tmp_path)
    assert (tmp_path / "mcuhome.yaml").read_text(encoding="utf-8") == "jobs: 4\n"


def test_init_force_completes_an_existing_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.pyc\nsecrets/\n", encoding="utf-8")
    init_project(tmp_path, force=True)
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert text == "*.pyc\nsecrets/\nbuild/\n"
    for line in GITIGNORE_LINES:
        assert line in text.splitlines()


def test_init_refuses_a_file_target(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        init_project(target)
    assert "is not a directory" in caught.value.message


def test_init_twice_with_force_changes_nothing_more(tmp_path: Path) -> None:
    init_project(tmp_path)
    result = init_project(tmp_path, force=True)
    assert result.created == ()


# --- secrets hygiene (ADR 0022 §5) ------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_an_owner_only_secrets_file_draws_no_warning(tmp_path: Path) -> None:
    path = tmp_path / "main.yaml"
    path.write_text("a: 1\n", encoding="utf-8")
    path.chmod(0o600)
    warnings: list[str] = []
    check_secret_file(path, key_material=False, on_warning=warnings.append)
    assert warnings == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_an_exposed_secrets_file_draws_a_warning_with_the_fix(tmp_path: Path) -> None:
    path = tmp_path / "main.yaml"
    path.write_text("a: 1\n", encoding="utf-8")
    path.chmod(0o644)
    warnings: list[str] = []
    check_secret_file(path, key_material=False, on_warning=warnings.append)
    assert len(warnings) == 1
    assert "readable by other users" in warnings[0]
    assert "mode 644" in warnings[0]
    assert f"chmod 600 {path}" in warnings[0]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_exposed_key_material_is_refused_not_warned_about(tmp_path: Path) -> None:
    path = tmp_path / "mcuboot.yaml"
    path.write_text("firmware_signing_key: x\n", encoding="utf-8")
    path.chmod(0o640)
    with pytest.raises(ConfigError) as caught:
        check_secret_file(path, key_material=True, on_warning=lambda _: None)
    assert "refuses to use the key material" in caught.value.message
    assert "mode 640" in caught.value.message
    assert f"chmod 600 {path}" in (caught.value.hint or "")


def test_a_missing_file_is_not_this_checks_problem(tmp_path: Path) -> None:
    check_secret_file(tmp_path / "absent.yaml", key_material=True)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_ensure_secrets_dir_creates_every_level_private(tmp_path: Path) -> None:
    directory = ensure_secrets_dir(tmp_path, "firmware")
    assert directory == tmp_path / "secrets" / "firmware"
    assert mode_of(tmp_path / "secrets") == 0o700
    assert mode_of(directory) == 0o700
