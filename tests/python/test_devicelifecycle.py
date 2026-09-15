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
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from mcuhome.model.errors import ConfigError
from test_buildlock import child_env, held_elsewhere

from mcuhome.workbench import api, build, device
from mcuhome.workbench.buildlock import (
    BUILD_LOCK_FILE,
    BuildDirectoryBusy,
    discard_build_directory,
)

BOARD = "nrf7002dk/nrf5340/cpuapp"

#: The tests that need a real peer: the lock is a POSIX advisory lock
#: and re-entrant per process, so nothing else can hold a directory
#: against this one — where there is no ``fork`` there is no way to ask.
needs_a_peer = pytest.mark.skipif(
    not hasattr(os, "fork"), reason="the lock is a POSIX advisory lock"
)


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


def taken_elsewhere(out_dir: Path) -> bool:
    """Whether a real second process can take *out_dir* right now.

    The other half of ``held_elsewhere``, and needed for the same
    reason: the lock is re-entrant per process, so only a second one can
    answer whether a directory is really held. It probes and lets go
    again immediately.
    """
    code = (
        "from pathlib import Path\n"
        "from mcuhome.model.errors import BuildError\n"
        "from mcuhome.workbench.buildlock import open_build_lock\n"
        "try:\n"
        f"    with open_build_lock(Path({str(out_dir)!r}), device='peer'):\n"
        "        print('took')\n"
        "except BuildError:\n"
        "    print('refused')\n"
    )
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=child_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.stdout.strip() in ("took", "refused"), done.stderr
    return done.stdout.strip() == "took"


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


@needs_a_peer
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


@needs_a_peer
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


def test_a_refusal_leaves_no_build_directory_holding_only_a_lock(
    tmp_path: Path, monkeypatch
) -> None:
    """A rename that failed must not block the rename back.

    Both build directories are held — and therefore created — before the
    work starts. A refusal that left them standing would leave two
    directories holding nothing but a lock file, and the next call would
    refuse the name they occupy as taken, which for a half-done rename
    is exactly the call somebody makes next.
    """
    project = make_project(tmp_path)
    make_device(project, "bench-node")

    def refuse(*_args, **_kwargs):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(device, "_replace_atomically", refuse)
    with pytest.raises(ConfigError):
        api.rename_device("bench-node", project=project, to="kitchen")
    monkeypatch.undo()

    assert not project.device_build_dir("bench-node").exists()
    assert not project.device_build_dir("kitchen").exists()
    assert not (project.root / api.BUILD_DIR).exists()

    # The call somebody makes next, and the reason this matters: with the
    # two directories left standing it would refuse the name as taken.
    api.rename_device("bench-node", project=project, to="kitchen")
    assert stated_name(project.device_entry("kitchen")) == "kitchen"


def test_a_name_that_cannot_be_rewritten_says_what_moved(tmp_path: Path, monkeypatch) -> None:
    """The one state that would not load, named with the line that fixes it."""
    project = make_project(tmp_path)
    make_device(project, "bench-node")

    def refuse(*_args, **_kwargs):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(device, "_replace_atomically", refuse)

    with pytest.raises(ConfigError) as caught:
        api.rename_device("bench-node", project=project, to="kitchen")

    assert "cannot write the new name" in str(caught.value)
    assert 'still called "bench-node"' in (caught.value.hint or "")
    assert project.device_entry("bench-node").is_file()
    assert not (project.devices_dir / "kitchen").exists()
    assert api.load_model(project.device_entry("bench-node"), project=project).device.name == (
        "bench-node"
    )


def test_a_folder_that_cannot_be_moved_back_leaves_the_line_to_write(
    tmp_path: Path, monkeypatch
) -> None:
    """When even taking the step back fails, the message finishes the job."""
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    target = project.devices_dir / "kitchen"

    def refuse_write(*_args, **_kwargs):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(device, "_replace_atomically", refuse_write)
    monkeypatch.setattr(os, "rename", _rename_that_refuses(target))

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


# --------------------------------------------------------------------------
# The lock holds for the whole operation
# --------------------------------------------------------------------------


def _probing_rename(build_dir: Path, probes: list[bool]):
    """``os.rename`` that asks a second process for the lock first."""
    real = os.rename

    def rename(source, destination):
        probes.append(taken_elsewhere(build_dir))
        return real(source, destination)

    return rename


@needs_a_peer
def test_nobody_gets_the_build_directory_while_a_rename_is_working(
    tmp_path: Path, monkeypatch
) -> None:
    """The removal must not take the lock file with it.

    A build directory is emptied by the rename, and the lock file is the
    one thing left in it: unlink it and a second process opening the
    same path creates a second inode and is granted a second
    "exclusive" lock — a build starting in the directory of a device
    that is half-way through being renamed. Probed at both of the
    moments the rename is still working: moving the folder, and moving
    the secrets.
    """
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    build_dir = make_build_output(project, "bench-node")
    probes: list[bool] = []
    monkeypatch.setattr(os, "rename", _probing_rename(build_dir, probes))

    api.rename_device("bench-node", project=project, to="kitchen")

    assert probes == [False, False], "a second process took the build directory mid-rename"
    assert not build_dir.exists()


@needs_a_peer
def test_nobody_gets_the_build_directory_while_a_delete_is_working(
    tmp_path: Path, monkeypatch
) -> None:
    """The same for the delete, probed while the device folder goes."""
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    build_dir = make_build_output(project, "bench-node")
    probes: list[bool] = []
    real = shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if Path(path) == project.devices_dir / "bench-node":
            probes.append(taken_elsewhere(build_dir))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(device.shutil, "rmtree", rmtree)

    api.delete_device("bench-node", project=project)

    assert probes == [False], "a second process took the build directory mid-delete"
    assert not build_dir.exists()


@needs_a_peer
def test_a_build_directory_somebody_took_afterwards_is_left_alone(tmp_path: Path) -> None:
    """The last removal happens under the lock too, or not at all.

    Between the release and the removal of the emptied directory a new
    run may have taken it. Removing it then would unlink the lock file
    another process is holding, which is the same hazard one step later
    — so the directory stays and is theirs.
    """
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    build_dir = make_build_output(project, "bench-node")
    (build_dir / "build-report.json").unlink()  # emptied, as the rename leaves it

    with held_elsewhere(build_dir, device="peer", operation="build"):
        assert not discard_build_directory(build_dir)
        assert (build_dir / BUILD_LOCK_FILE).is_file(), "the holder's lock file was unlinked"

    assert build_dir.is_dir()
    assert discard_build_directory(build_dir), "and it goes once nobody is in it"
    assert not build_dir.exists()


def test_a_build_directory_that_will_not_empty_is_named(tmp_path: Path, monkeypatch) -> None:
    """The refusal names the directory that failed, not the first one.

    A rename empties two directories, the device's and the target's, and
    a message that always named the first would send somebody to look at
    the wrong one.
    """
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    make_build_output(project, "bench-node")
    target_dir = project.device_build_dir("kitchen")
    real = device._empty_build_dir  # noqa: SLF001 - the seam whose refusal is under test

    def refuse(directory: Path) -> None:
        if directory == target_dir:
            raise OSError(errno.EACCES, "Permission denied")
        real(directory)

    monkeypatch.setattr(device, "_empty_build_dir", refuse)

    with pytest.raises(ConfigError) as caught:
        api.rename_device("bench-node", project=project, to="kitchen")

    assert str(target_dir) in str(caught.value)
    assert str(project.device_build_dir("bench-node")) not in str(caught.value)


@pytest.mark.parametrize("call", ["rename", "delete"])
def test_a_file_where_the_build_directory_belongs_is_refused_in_words(
    tmp_path: Path, call: str
) -> None:
    """Taking the lock would `mkdir` over it and raise `FileExistsError`.

    Everything else on this path refuses in this package's words; a
    standard-library exception out of the middle of a rename is not an
    answer anybody can act on.
    """
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    build_dir = project.device_build_dir("bench-node")
    build_dir.parent.mkdir(parents=True, exist_ok=True)
    build_dir.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        if call == "rename":
            api.rename_device("bench-node", project=project, to="kitchen")
        else:
            api.delete_device("bench-node", project=project)

    assert "not a directory" in str(caught.value)
    assert str(build_dir) in str(caught.value)
    assert project.device_entry("bench-node").is_file()
    assert build_dir.is_file()


@pytest.mark.parametrize("call", ["rename", "delete"])
def test_a_link_pointing_nowhere_where_the_build_directory_belongs_is_refused(
    tmp_path: Path, call: str
) -> None:
    """`mkdir` over a dangling link raises the same way, so it is the same case."""
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    build_dir = project.device_build_dir("bench-node")
    build_dir.parent.mkdir(parents=True, exist_ok=True)
    build_dir.symlink_to(tmp_path / "gone")

    with pytest.raises(ConfigError) as caught:
        if call == "rename":
            api.rename_device("bench-node", project=project, to="kitchen")
        else:
            api.delete_device("bench-node", project=project)

    assert str(build_dir) in str(caught.value)
    assert build_dir.is_symlink()


# --------------------------------------------------------------------------
# What the rest of the package sees afterwards
# --------------------------------------------------------------------------


def test_the_patches_are_picked_up_under_the_new_name(tmp_path: Path) -> None:
    """The convention keys on the device folder, and the folder moved.

    A build of the renamed device has to carry the patches that
    travelled with it — the same folder, found under the name it has
    now. Asked of the build layer's own lookup rather than of the
    filesystem, because that is what decides it.
    """
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    make_patch(project, "bench-node")

    api.rename_device("bench-node", project=project, to="kitchen")

    model = api.load_model(project.device_entry("kitchen"), project=project)
    found = build._device_patches_dir(  # noqa: SLF001 - the convention under test
        model, patches_dir=None, project_root=project.root
    )
    assert found == project.device_patches_dir("kitchen")
    assert (found / "zephyr" / "0001-fix-uart.patch").is_file()


def test_a_target_name_a_broken_link_occupies_is_taken(tmp_path: Path) -> None:
    """A link pointing nowhere owns the name as much as a folder does.

    Renaming onto it would follow it and write outside the project.
    """
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    link = project.devices_dir / "kitchen"
    link.symlink_to(tmp_path / "nowhere")

    with pytest.raises(ConfigError) as caught:
        api.rename_device("bench-node", project=project, to="kitchen")

    assert 'name "kitchen" is taken' in str(caught.value)
    assert link.is_symlink()
    assert project.device_entry("bench-node").is_file()


def test_the_device_file_keeps_the_mode_its_owner_gave_it(tmp_path: Path) -> None:
    """The rename writes the file back; it does not re-create it.

    A file somebody restricted stays restricted — the replace takes the
    mode that was there rather than the one a new file would get.
    """
    project = make_project(tmp_path)
    entry = make_device(project, "bench-node")
    entry.chmod(0o640)

    api.rename_device("bench-node", project=project, to="kitchen")

    moved = project.device_entry("kitchen")
    assert stat.S_IMODE(moved.stat().st_mode) == 0o640
    assert stated_name(moved) == "kitchen"


@pytest.mark.parametrize("call", ["rename", "delete"])
def test_neither_call_leaves_a_build_directory_nobody_asked_for(tmp_path: Path, call: str) -> None:
    """Holding a directory creates it; a device that never built has none."""
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    assert not (project.root / api.BUILD_DIR).exists()

    if call == "rename":
        api.rename_device("bench-node", project=project, to="kitchen")
    else:
        api.delete_device("bench-node", project=project)

    assert not (project.root / api.BUILD_DIR).exists()


@pytest.mark.parametrize("call", ["rename", "delete"])
def test_a_build_directory_that_was_already_there_stays(tmp_path: Path, call: str) -> None:
    """What this call did not create, it does not tidy away.

    Another device's output is the ordinary case; an empty `build/`
    somebody made is theirs as well.
    """
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    make_device(project, "porch")
    other = make_build_output(project, "porch")

    if call == "rename":
        api.rename_device("bench-node", project=project, to="kitchen")
    else:
        api.delete_device("bench-node", project=project)

    assert other.is_dir()
    assert (other / "build-report.json").is_file()
    assert (project.root / api.BUILD_DIR).is_dir()


@pytest.mark.parametrize("call", ["rename", "delete"])
def test_an_empty_build_directory_somebody_made_is_not_tidied_away(
    tmp_path: Path, call: str
) -> None:
    """The rule is "what this call created", not "what is empty now"."""
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    (project.root / api.BUILD_DIR).mkdir()

    if call == "rename":
        api.rename_device("bench-node", project=project, to="kitchen")
    else:
        api.delete_device("bench-node", project=project)

    assert (project.root / api.BUILD_DIR).is_dir()


@pytest.mark.parametrize("call", ["rename", "delete"])
def test_a_linked_build_directory_is_refused_rather_than_followed(
    tmp_path: Path, call: str
) -> None:
    """Somebody's link is not this call's to empty.

    A link to a real directory is the one of the three that *works*:
    the removal would take the contents of a directory somewhere else on
    the disk and then report the link as what it removed.
    """
    project = make_project(tmp_path)
    make_device(project, "bench-node")
    elsewhere = tmp_path / "scratch"
    elsewhere.mkdir()
    (elsewhere / "build-report.json").write_text("{}", encoding="utf-8")
    build_dir = project.device_build_dir("bench-node")
    build_dir.parent.mkdir(parents=True, exist_ok=True)
    build_dir.symlink_to(elsewhere)

    with pytest.raises(ConfigError) as caught:
        if call == "rename":
            api.rename_device("bench-node", project=project, to="kitchen")
        else:
            api.delete_device("bench-node", project=project)

    assert "is a link" in str(caught.value)
    assert build_dir.is_symlink()
    assert (elsewhere / "build-report.json").is_file()
    assert project.device_entry("bench-node").is_file()
