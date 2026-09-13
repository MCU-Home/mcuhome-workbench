# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""What a renamed refusal says, and whose words it says it in.

Renaming a function is invisible to the person using MCUHome — unless
the refusal it raises was worded around the old name. Every function
this suite covers was renamed, and every test here asserts the same
thing: the message still names what the *user* stated — the device they
asked for, the file they pointed at, the image they pinned, the amount
of memory they typed — rather than an internal name they never saw.

The refusals themselves are tested where they belong (the device
lookup in ``test_project.py``, the image search in
``test_resolve_image.py`` and so on); this file is the guard against a
rename that quietly takes the user's own word out of the answer.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import VALID_CONFIG
from mcuhome.model.buildenvironment import Declaration
from mcuhome.model.context import PackagePin
from mcuhome.model.errors import BuildError, ConfigError

from mcuhome.workbench import containerbuild
from mcuhome.workbench.buildenvsession import (
    EnvironmentUnusable,
    parse_memory,
    resolve_cache_tiers,
)
from mcuhome.workbench.loader import read_yaml_file
from mcuhome.workbench.packagefetch import SdkUnavailable, fetch_sdk_package
from mcuhome.workbench.project import (
    Project,
    create_project,
    read_project,
    require_secret_file,
    resolve_device,
    resolve_project,
)
from mcuhome.workbench.provision import create_pairing
from mcuhome.workbench.resolve_image import resolve_container_image
from mcuhome.workbench.resolve_pins import resolve_package
from mcuhome.workbench.scaffold import create_device

BOARD = "nrf7002dk/nrf5340/cpuapp"


# --------------------------------------------------------------------------
# Projects and devices
# --------------------------------------------------------------------------


def test_resolve_device_names_the_device_the_user_asked_for(tmp_path) -> None:
    project = create_project(tmp_path).project
    (project.root / "devices" / "bench-node").mkdir(parents=True)
    (project.root / "devices" / "bench-node" / "main.yaml").write_text(VALID_CONFIG, "utf-8")
    with pytest.raises(ConfigError) as caught:
        resolve_device("kitchen", env={}, cwd=tmp_path)
    assert "kitchen" in caught.value.message
    assert "bench-node" in (caught.value.hint or "")


def test_read_project_names_the_root_it_was_given(tmp_path) -> None:
    plain = tmp_path / "not-a-project"
    plain.mkdir()
    with pytest.raises(ConfigError) as caught:
        read_project(plain)
    assert str(plain) in str(caught.value)


def test_create_project_names_the_project_it_will_not_overwrite(tmp_path) -> None:
    create_project(tmp_path)
    with pytest.raises(ConfigError) as caught:
        create_project(tmp_path)
    assert str(tmp_path) in str(caught.value)


def test_create_device_names_the_name_that_cannot_be_one(tmp_path) -> None:
    project = create_project(tmp_path).project
    with pytest.raises(ConfigError) as caught:
        create_device("Bench Node", project=project, board=BOARD)
    assert "Bench Node" in caught.value.message


def test_create_pairing_names_the_device_file_it_refuses_to_replace(tmp_path) -> None:
    entry = tmp_path / "main.yaml"
    entry.write_text(VALID_CONFIG, "utf-8")
    project = Project(root=tmp_path, discovered=False)
    with pytest.raises(ConfigError) as caught:
        create_pairing(entry, project=project)
    assert caught.value.location is not None
    assert caught.value.location.file == entry


def test_read_yaml_file_names_the_file_that_will_not_parse(tmp_path) -> None:
    broken = tmp_path / "main.yaml"
    broken.write_text("device:\n  name: [unclosed\n", "utf-8")
    with pytest.raises(ConfigError) as caught:
        read_yaml_file(broken)
    assert broken.name in str(caught.value)


@pytest.mark.skipif(os.name != "posix", reason="the guard reads POSIX permission bits")
def test_require_secret_file_names_the_file_and_the_fix(tmp_path) -> None:
    exposed = tmp_path / "key.pem"
    exposed.write_text("-----BEGIN PRIVATE KEY-----\n", "utf-8")
    exposed.chmod(0o644)
    with pytest.raises(ConfigError) as caught:
        require_secret_file(exposed, key_material=True)
    assert str(exposed) in caught.value.message
    assert f"chmod 600 {exposed}" in (caught.value.hint or "")


# --------------------------------------------------------------------------
# Build environments and packages
# --------------------------------------------------------------------------


def test_parse_memory_names_the_key_and_the_text_it_was_given(tmp_path) -> None:
    with pytest.raises(ConfigError) as caught:
        parse_memory("eight gigs")
    assert "eight gigs" in caught.value.message
    assert "build.memory" in (caught.value.hint or "")
    with pytest.raises(ConfigError) as caught:
        parse_memory("eight gigs", key="server.memory")
    assert "server.memory" in (caught.value.hint or "")


def test_resolve_cache_tiers_names_the_shared_directory_that_is_not_one(tmp_path) -> None:
    missing = tmp_path / "nothing-here"
    with pytest.raises(ConfigError) as caught:
        resolve_cache_tiers(shared=missing)
    assert str(missing) in caught.value.message


def test_require_container_runtime_names_the_program_it_could_not_run(tmp_path) -> None:
    runtime = containerbuild.ContainerRuntime(
        "podman", runner=lambda argv, on_line=None: _Completed(None)
    )
    with pytest.raises(BuildError) as caught:
        containerbuild.require_container_runtime(runtime, env={})
    assert "podman" in caught.value.message


def test_require_container_image_names_the_image_that_does_not_fit() -> None:
    declaration = Declaration(
        spec_generation="1", zephyr_version="4.4.0", generator_constraint="", packages={}
    )
    with pytest.raises(EnvironmentUnusable) as caught:
        containerbuild.require_container_image(
            declaration, container_image="ghcr.io/mcu-home/build-environment:0.1.0-r1"
        )
    assert "ghcr.io/mcu-home/build-environment:0.1.0-r1" in caught.value.message


def test_ensure_container_image_names_the_image_and_its_registry() -> None:
    runtime = containerbuild.ContainerRuntime(
        "docker", runner=lambda argv, on_line=None: _Completed(1)
    )
    address = "ghcr.io/mcu-home/build-environment@sha256:" + "ab" * 32
    with pytest.raises(BuildError) as caught:
        containerbuild.ensure_container_image(runtime, address)
    assert address in caught.value.message
    # The whole line, so that a hint naming the *image* where the
    # registry belongs does not pass by substring.
    assert "    docker login ghcr.io\n" in (caught.value.hint or "")


def test_resolve_container_image_names_the_packages_it_looked_for() -> None:
    with pytest.raises(BuildError) as caught:
        resolve_container_image({}, repositories=())
    assert "build environment" in caught.value.message.lower()


def test_fetch_sdk_package_names_the_version_it_could_not_find(tmp_path) -> None:
    with pytest.raises(SdkUnavailable) as caught:
        fetch_sdk_package(
            version="9.9.9", sha256="ab" * 32, sources=(tmp_path,), into=tmp_path / "into"
        )
    assert "9.9.9" in caught.value.message


def test_resolve_package_names_the_package_this_machine_needs(tmp_path) -> None:
    with pytest.raises(BuildError) as caught:
        resolve_package(
            PackagePin(name="mcuhome-build-tools", version="0.1.0", sha256="ab" * 32),
            kind="build-tools",
            sources=(tmp_path,),
        )
    assert "mcuhome-build-tools" in caught.value.message


class _Completed:
    """What the container seam answers: a status and nothing else."""

    def __init__(self, status: int | None) -> None:
        self.status = status
        self.ok = status == 0
        self.output = ""


def test_resolve_project_still_points_at_the_marker(tmp_path: Path) -> None:
    """The bootstrap the creating functions no longer do themselves."""
    with pytest.raises(ConfigError) as caught:
        resolve_project(env={}, cwd=tmp_path)
    assert "mcuhome project init" in (caught.value.hint or "")
