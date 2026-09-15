# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Renaming and deleting a device: :mod:`mcuhome.workbench.device`.

A device is its folder, and a rename has to move everything that is
keyed on that folder at once — the folder, the name inside the file, the
device's secrets — or the device ends up being two devices, each
answering to half of what it owns. So the test that matters here is the
round trip: create a device, draw its credentials, give it a patch,
rename it, and then *load* it under the new name. Nothing but a complete
rename gets through that, because the loader refuses a file that
disagrees with its folder and the model needs the credentials that live
in the file the rename moved.

Beside it: the build output goes rather than travelling (it names the
device inside it), a delete keeps the credentials only when it is asked
to, and both refuse while another process is working in the device's
build directory.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest
from mcuhome.model.errors import ConfigError
from test_buildlock import held_elsewhere

from mcuhome.workbench import api, device
from mcuhome.workbench.buildlock import BuildDirectoryBusy

BOARD = "nrf7002dk/nrf5340/cpuapp"


def make_project(tmp_path: Path) -> api.Project:
    return api.create_project(tmp_path / "project").project


def make_device(project: api.Project, name: str = "bench-node") -> Path:
    """A device with its commissioning credentials drawn, as a user has it."""
    created = api.create_device(name, project=project, board=BOARD)
    api.create_pairing(created.entry, project=project)
    return created.entry


def make_patch(project: api.Project, name: str) -> Path:
    """One source patch beside the device file, as the convention has it."""
    patch = project.device_patches_dir(name) / "zephyr" / "0001-fix-uart.patch"
    patch.parent.mkdir(parents=True)
    patch.write_text("--- a\n+++ b\n", encoding="utf-8")
    return patch


def make_build_output(project: api.Project, name: str) -> Path:
    """A build directory with something in it, as a build leaves it."""
    directory = project.device_build_dir(name)
    directory.mkdir(parents=True)
    report = directory / "build-report.json"
    report.write_text(json.dumps({"device": name}), encoding="utf-8")
    return directory


def stated_name(entry: Path) -> str:
    return str(api.read_yaml_file(entry)["device"]["name"])


# --------------------------------------------------------------------------
# What a rename moves
# --------------------------------------------------------------------------


def test_a_rename_moves_the_folder_the_secrets_and_the_patches(tmp_path: Path) -> None:
    """Everything keyed on the device folder travels with it, at once."""
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    make_patch(project, "bench-node")

    changed = api.rename_device("bench-node", project=project, to="kitchen")

    assert project.device_entry("kitchen").is_file()
    assert (project.device_patches_dir("kitchen") / "zephyr" / "0001-fix-uart.patch").is_file()
    assert project.device_secrets_file("kitchen").is_file()
    assert changed == (
        project.devices_dir / "kitchen",
        project.device_entry("kitchen"),
        project.device_secrets_file("kitchen"),
    )


def test_a_rename_writes_the_new_name_into_the_file(tmp_path: Path) -> None:
    """The folder and ``device.name`` are one word — the rename keeps it so.

    Without this the moved device is a file the loader refuses: it lives
    in one folder and calls itself another.
    """
    project = make_project(tmp_path)
    make_device(project, "bench-node")

    api.rename_device("bench-node", project=project, to="kitchen")

    assert stated_name(project.device_entry("kitchen")) == "kitchen"


def test_the_renamed_device_still_loads_and_validates(tmp_path: Path) -> None:
    """The whole point, asserted the only way it can be: load the thing.

    A folder that moved without the name, or a name that moved without
    the secrets, fails here — the loader refuses the first and the model
    has no commissioning credentials in the second.
    """
    project = make_project(tmp_path)
    make_device(project, "bench-node")

    api.rename_device("bench-node", project=project, to="kitchen")

    entry = project.device_entry("kitchen")
    result = api.validate_device(entry, project=project)
    assert result.ok, [error.message for error in result.errors]
    model = api.load_model(entry, project=project)
    assert model.device.name == "kitchen"
    assert model.network.pairing is not None


def test_a_rename_changes_nothing_else_in_the_file(tmp_path: Path) -> None:
    """A round trip: comments, order, quoting and tags survive the edit.

    The device file is the user's, and a rename is allowed to change one
    word in it.
    """
    project = make_project(tmp_path)
    entry = make_device(project, "bench-node")
    before = entry.read_text(encoding="utf-8")

    api.rename_device("bench-node", project=project, to="kitchen")

    after = project.device_entry("kitchen").read_text(encoding="utf-8")
    assert after == before.replace("  name: bench-node\n", "  name: kitchen\n", 1)


def test_a_rename_leaves_a_file_that_states_no_name_alone(tmp_path: Path) -> None:
    """Nothing to bring in line, so nothing is written.

    A key the user does not have is not one this call invents — and the
    answer says so by not naming the file.
    """
    project = make_project(tmp_path)
    entry = project.device_entry("bench-node")
    entry.parent.mkdir(parents=True)
    entry.write_text("# a device that never got around to it\nnetwork: {}\n", encoding="utf-8")
    before = entry.read_text(encoding="utf-8")

    changed = api.rename_device("bench-node", project=project, to="kitchen")

    assert changed == (project.devices_dir / "kitchen",)
    assert project.device_entry("kitchen").read_text(encoding="utf-8") == before


def test_nothing_is_left_under_the_old_name(tmp_path: Path) -> None:
    """The old name is free afterwards — every place that keyed on it."""
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    make_build_output(project, "bench-node")

    api.rename_device("bench-node", project=project, to="kitchen")

    assert not (project.devices_dir / "bench-node").exists()
    assert not project.device_secrets_file("bench-node").exists()
    assert not project.device_build_dir("bench-node").exists()
    assert project.device_names() == ["kitchen"]


# --------------------------------------------------------------------------
# What a rename removes
# --------------------------------------------------------------------------


def test_a_rename_removes_the_build_directory(tmp_path: Path) -> None:
    """Build output names the device inside it, so it goes rather than moves."""
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    build_dir = make_build_output(project, "bench-node")

    changed = api.rename_device("bench-node", project=project, to="kitchen")

    assert not build_dir.exists()
    assert changed[0] == build_dir
    # And the new name starts without one: the directory the lock
    # created for the target is removed again.
    assert not project.device_build_dir("kitchen").exists()


def test_a_rename_of_a_device_that_was_never_built_names_no_build_directory(
    tmp_path: Path,
) -> None:
    """Holding a directory is not the same as the user having had one."""
    project = make_project(tmp_path)
    make_device(project, "bench-node")

    changed = api.rename_device("bench-node", project=project, to="kitchen")

    assert project.device_build_dir("bench-node") not in changed
    assert not project.device_build_dir("bench-node").exists()


# --------------------------------------------------------------------------
# What a rename refuses
# --------------------------------------------------------------------------


def test_a_rename_refuses_a_device_the_project_does_not_have(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    make_device(project, "bench-node")

    with pytest.raises(ConfigError) as caught:
        api.rename_device("porch", project=project, to="kitchen")

    assert 'no device called "porch"' in str(caught.value)
    assert "bench-node" in (caught.value.hint or "")


@pytest.mark.parametrize("name", ["Kitchen", "kitchen-", "-kitchen", "kit chen", "1234", "k" * 33])
def test_a_rename_refuses_a_target_that_is_not_a_device_name(tmp_path: Path, name: str) -> None:
    """The rule `create_device` follows, at the other end of a device's life.

    A name that is legal for one and not the other would be a device
    that can be renamed into a shape the next command refuses.
    """
    project = make_project(tmp_path)
    make_device(project, "bench-node")

    with pytest.raises(ConfigError) as caught:
        api.rename_device("bench-node", project=project, to=name)

    assert "not a usable device name" in str(caught.value)
    assert project.device_entry("bench-node").is_file()


def test_a_rename_refuses_the_name_the_device_already_has(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    make_device(project, "bench-node")

    with pytest.raises(ConfigError) as caught:
        api.rename_device("bench-node", project=project, to="bench-node")

    assert "already called that" in str(caught.value)


def test_a_rename_refuses_a_target_another_device_has(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    make_device(project, "kitchen")

    with pytest.raises(ConfigError) as caught:
        api.rename_device("bench-node", project=project, to="kitchen")

    assert 'name "kitchen" is taken' in str(caught.value)
    assert stated_name(project.device_entry("kitchen")) == "kitchen"


def test_a_rename_refuses_a_target_whose_secrets_file_is_there(tmp_path: Path) -> None:
    """A leftover file of a device that once existed still owns the name.

    Renaming onto it would hand this device somebody else's
    commissioning credentials — silently, because the file is read by
    the folder's name alone.
    """
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    orphan = project.device_secrets_file("kitchen")
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("passcode: 20202021\n", encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        api.rename_device("bench-node", project=project, to="kitchen")

    assert str(orphan) in str(caught.value)
    assert orphan.read_text(encoding="utf-8") == "passcode: 20202021\n"
    assert project.device_entry("bench-node").is_file()


def test_a_rename_refuses_a_target_whose_build_directory_is_there(tmp_path: Path) -> None:
    """The same rule for the third place a device name occupies."""
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    leftover = make_build_output(project, "kitchen")

    with pytest.raises(ConfigError) as caught:
        api.rename_device("bench-node", project=project, to="kitchen")

    assert str(leftover) in str(caught.value)
    assert leftover.is_dir()


def test_a_rename_refuses_a_device_file_it_cannot_read(tmp_path: Path) -> None:
    """Refused before anything moves, because the name has to be rewritten."""
    project = make_project(tmp_path)
    entry = project.device_entry("bench-node")
    entry.parent.mkdir(parents=True)
    entry.write_text("device:\n  name: bench-node\n   board: [\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        api.rename_device("bench-node", project=project, to="kitchen")

    assert entry.is_file()
    assert not (project.devices_dir / "kitchen").exists()


def test_a_rename_refuses_while_somebody_is_in_the_build_directory(tmp_path: Path) -> None:
    """A build in flight keeps its device, and hears about it in words."""
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    build_dir = make_build_output(project, "bench-node")

    with (
        held_elsewhere(build_dir, device="bench-node"),
        pytest.raises(BuildDirectoryBusy) as caught,
    ):
        api.rename_device("bench-node", project=project, to="kitchen")

    assert "A build of bench-node is already running" in str(caught.value)
    assert project.device_entry("bench-node").is_file()
    assert (build_dir / "build-report.json").is_file()
    assert not (project.devices_dir / "kitchen").exists()


# --------------------------------------------------------------------------
# Deleting
# --------------------------------------------------------------------------


def test_a_delete_removes_the_device_its_output_and_its_secrets(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    make_patch(project, "bench-node")
    build_dir = make_build_output(project, "bench-node")

    removed = api.delete_device("bench-node", project=project)

    assert removed == (
        build_dir,
        project.devices_dir / "bench-node",
        project.device_secrets_file("bench-node"),
    )
    assert project.device_names() == []
    assert not build_dir.exists()
    assert not project.device_secrets_file("bench-node").exists()


def test_a_delete_keeps_the_secrets_when_it_is_asked_to(tmp_path: Path) -> None:
    """Commissioning credentials are drawn once; keeping them is a choice."""
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    secrets = project.device_secrets_file("bench-node")
    before = secrets.read_text(encoding="utf-8")

    removed = api.delete_device("bench-node", project=project, keep_secrets=True)

    assert removed == (project.devices_dir / "bench-node",)
    assert secrets.read_text(encoding="utf-8") == before
    assert [scope.name for scope in api.find_secret_scopes(project) if scope.kind == "device"] == [
        "bench-node"
    ]


def test_a_delete_of_a_device_without_secrets_names_only_the_folder(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    api.create_device("bench-node", project=project, board=BOARD)

    removed = api.delete_device("bench-node", project=project)

    assert removed == (project.devices_dir / "bench-node",)


def test_a_delete_refuses_a_device_the_project_does_not_have(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    make_device(project, "bench-node")

    with pytest.raises(ConfigError) as caught:
        api.delete_device("porch", project=project)

    assert 'no device called "porch"' in str(caught.value)
    assert project.device_names() == ["bench-node"]


def test_a_delete_refuses_while_somebody_is_in_the_build_directory(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    build_dir = make_build_output(project, "bench-node")

    with (
        held_elsewhere(build_dir, device="bench-node", operation="sign"),
        pytest.raises(BuildDirectoryBusy) as caught,
    ):
        api.delete_device("bench-node", project=project)

    assert "bench-node is being signed" in str(caught.value)
    assert project.device_entry("bench-node").is_file()
    assert project.device_secrets_file("bench-node").is_file()


# --------------------------------------------------------------------------
# When the filesystem says no half-way through
# --------------------------------------------------------------------------


def _rename_that_refuses(which: Path):
    """``os.rename`` that refuses for one path and works for every other."""
    real = os.rename

    def rename(source, destination):
        if Path(source) == which:
            raise OSError(errno.EACCES, "Permission denied")
        return real(source, destination)

    return rename


def test_a_folder_that_cannot_be_moved_refuses_in_this_package_s_words(
    tmp_path: Path, monkeypatch
) -> None:
    """Not a bare ``OSError``: the device is still there and is named."""
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    monkeypatch.setattr(os, "rename", _rename_that_refuses(project.devices_dir / "bench-node"))

    with pytest.raises(ConfigError) as caught:
        api.rename_device("bench-node", project=project, to="kitchen")

    assert "cannot move" in str(caught.value)
    assert "The device itself is untouched" in (caught.value.hint or "")
    assert project.device_entry("bench-node").is_file()
    assert not (project.devices_dir / "kitchen").exists()


def test_a_name_that_cannot_be_rewritten_says_what_moved(tmp_path: Path, monkeypatch) -> None:
    """The one state that would not load, named with the line that fixes it."""
    project = make_project(tmp_path)
    make_device(project, "bench-node")

    def refuse(*_args, **_kwargs):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(device, "_replace_atomically", refuse)

    with pytest.raises(ConfigError) as caught:
        api.rename_device("bench-node", project=project, to="kitchen")

    assert 'still says "bench-node"' in str(caught.value)
    assert "name: kitchen" in (caught.value.hint or "")
    assert project.device_entry("kitchen").is_file()


def test_secrets_that_could_not_follow_are_named_with_the_command(
    tmp_path: Path, monkeypatch
) -> None:
    """The device moved, its credentials did not — and are still readable.

    The order is deliberate: what is left is a file the secrets surface
    lists as a leftover scope and a `mv` away from where it belongs, not
    a device nothing can find.
    """
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    secrets = project.device_secrets_file("bench-node")
    monkeypatch.setattr(os, "rename", _rename_that_refuses(secrets))

    with pytest.raises(ConfigError) as caught:
        api.rename_device("bench-node", project=project, to="kitchen")

    assert f"mv {secrets}" in (caught.value.hint or "")
    assert project.device_entry("kitchen").is_file()
    assert stated_name(project.device_entry("kitchen")) == "kitchen"
    assert secrets.is_file()


def test_a_device_file_that_is_a_symlink_is_followed(tmp_path: Path) -> None:
    """The file somebody pointed at is the file that gets the new name.

    Every other write in this package follows a symlink in the layout
    rather than replacing it, and a rename is not the place to start
    breaking somebody's arrangement.
    """
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    entry = project.device_entry("bench-node")
    elsewhere = tmp_path / "shared" / "bench-node.yaml"
    elsewhere.parent.mkdir()
    entry.replace(elsewhere)
    entry.symlink_to(elsewhere)

    api.rename_device("bench-node", project=project, to="kitchen")

    moved = project.device_entry("kitchen")
    assert moved.is_symlink()
    assert moved.readlink() == elsewhere
    assert stated_name(elsewhere) == "kitchen"
