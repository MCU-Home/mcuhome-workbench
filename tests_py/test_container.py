# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Stage 5, container path (``mcuhome/compiler/container.py``).

**Docker never runs here.** Same rule as ``test_workspace.py``: the pytest
half of the strategy (builder-pipeline.md §9) is the fast half, and a
suite that needs a container runtime is neither fast nor runnable on a
contributor's laptop. What is tested is everything the container path
decides before ``docker run`` is executed — which image, which mounts,
which environment, and which of the three ways it can refuse. The one
impure function is injected (``runner=``), so the refusals are exercised
without a daemon and without a network.

The end-to-end proof that the assembled command actually builds firmware
is a manual step, not a test: it takes minutes and a few gigabytes of
image (see containers/builder/README.md).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from mcuhome.compiler import container, workspace
from mcuhome.model.errors import BuildError

REPO_ROOT = Path(__file__).resolve().parent.parent


def _fake_workspace(root: Path) -> Path:
    (root / ".west").mkdir(parents=True)
    (root / ".west" / "config").write_text("[manifest]\npath = mcuhome\n", "utf-8")
    return root


class _Runner:
    """A stand-in for docker that answers whatever the test needs.

    Records the commands it was asked to run, so a test can assert that
    the preflight stopped at the first thing that was wrong instead of
    asking further questions it already knew the answer to.
    """

    def __init__(self, *answers: int | None) -> None:
        self.answers = list(answers)
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str], env: dict[str, str]) -> int | None:
        self.commands.append(list(command))
        return self.answers.pop(0) if self.answers else 0


def _plan(tmp_path: Path, monkeypatch, **kwargs):
    top = _fake_workspace(tmp_path / "ws")
    environment = {container.CCACHE_DIR_VAR: str(tmp_path / "cache")}
    environment.update(kwargs.pop("env", {}))
    kwargs.setdefault("jobs", 2)
    return top, container.plan_build(
        out_dir=kwargs.pop("out_dir", top / "build" / "node"),
        app_subdir="app",
        board=kwargs.pop("board", "nrf7002dk/nrf5340/cpuapp"),
        env=environment,
        module_dir=top / "mcuhome",
        cwd=top,
        runner=_Runner(0, 0),
        **kwargs,
    )


# --------------------------------------------------------------------------
# Which image
# --------------------------------------------------------------------------


def test_the_image_tag_is_the_zephyr_pin_plus_a_revision() -> None:
    """ADR 0007: images are versioned in lockstep with the Zephyr pin.

    Read out of west.yml rather than restated, because a lockstep rule
    nobody checks is a rule that holds until the first bump. Bumping
    Zephyr without rebuilding the image fails here, which is the point.
    """
    manifest = (REPO_ROOT / "west.yml").read_text(encoding="utf-8")
    pinned = re.search(r"name: zephyr\s+remote: \S+\s+revision: v(\S+)", manifest)
    assert pinned is not None, "west.yml no longer states the Zephyr revision as expected"
    assert pinned.group(1) == container.ZEPHYR_RELEASE
    assert f"zephyr-{pinned.group(1)}-r{container.IMAGE_REVISION}" == container.IMAGE_TAG


def test_the_image_is_never_latest() -> None:
    """A build environment that changes under a stable name is not one."""
    assert f"{container.IMAGE_REPOSITORY}:{container.IMAGE_TAG}" == container.IMAGE
    assert not container.IMAGE.endswith(":latest")


def test_the_dockerfile_is_where_the_error_message_says_it_is() -> None:
    assert (REPO_ROOT / container.DOCKERFILE_DIR / "Dockerfile").is_file()


def test_the_default_image_is_the_pinned_one() -> None:
    assert container.image_reference({}) == container.IMAGE


def test_the_environment_can_name_another_image() -> None:
    env = {container.IMAGE_VAR: "localhost/builder:wip"}
    assert container.image_reference(env) == "localhost/builder:wip"


def test_an_explicit_image_beats_the_environment() -> None:
    env = {container.IMAGE_VAR: "localhost/builder:wip"}
    assert container.image_reference(env, override="other:tag") == "other:tag"


def test_an_empty_variable_is_not_an_image() -> None:
    assert container.image_reference({container.IMAGE_VAR: ""}) == container.IMAGE


def test_the_container_program_can_be_swapped() -> None:
    assert container.docker_program({}) == "docker"
    assert container.docker_program({container.DOCKER_VAR: "podman"}) == "podman"


# --------------------------------------------------------------------------
# Where the cache lives
# --------------------------------------------------------------------------


def test_the_cache_follows_the_xdg_variable(tmp_path) -> None:
    env = {"XDG_CACHE_HOME": str(tmp_path / "xdg")}
    assert container.ccache_directory(env) == tmp_path / "xdg" / "mcuhome" / "ccache"


def test_without_xdg_the_cache_is_under_the_home_directory(tmp_path) -> None:
    env = {"HOME": str(tmp_path / "home")}
    assert container.ccache_directory(env) == tmp_path / "home" / ".cache" / "mcuhome" / "ccache"


def test_the_cache_can_be_put_anywhere(tmp_path) -> None:
    env = {container.CCACHE_DIR_VAR: str(tmp_path / "fast-disk")}
    assert container.ccache_directory(env) == tmp_path / "fast-disk"


def test_a_tilde_in_the_cache_path_is_a_home_directory(tmp_path) -> None:
    env = {"HOME": str(tmp_path / "home"), container.CCACHE_DIR_VAR: "~/c"}
    assert container.ccache_directory(env) == tmp_path / "home" / "c"


# --------------------------------------------------------------------------
# What the container sees
# --------------------------------------------------------------------------


def test_the_workspace_is_the_only_mount_it_normally_needs(tmp_path) -> None:
    top = tmp_path / "ws"
    (top / "build" / "node").mkdir(parents=True)
    assert container.mount_points(top, top / "build" / "node", top / "mcuhome") == [top]


def test_a_build_directory_somewhere_else_is_mounted_too(tmp_path) -> None:
    top = tmp_path / "ws"
    top.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert container.mount_points(top, elsewhere) == [top, elsewhere]


def test_the_same_outside_directory_is_not_mounted_twice(tmp_path) -> None:
    top = tmp_path / "ws"
    top.mkdir()
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "deeper").mkdir(parents=True)
    mounts = container.mount_points(top, elsewhere, elsewhere / "deeper", elsewhere)
    assert mounts == [top, elsewhere]


def test_the_container_environment_is_composed_not_inherited(tmp_path) -> None:
    """The point of a build container is independence from the shell.

    Only what depends on where the workspace is gets passed in; the SDK,
    the toolchain variant and the tool paths belong to the image.
    """
    env = container.container_environment(
        topdir=tmp_path / "ws", pyshim_dir=tmp_path / "shim", jobs=2
    )
    assert env == {
        "PYTHONPATH": str(tmp_path / "shim"),
        "ZEPHYR_BASE": str(tmp_path / "ws" / "zephyr"),
        "HOME": container.CONTAINER_HOME,
        "CCACHE_DIR": container.CONTAINER_CCACHE_DIR,
        "CCACHE_BASEDIR": str(tmp_path / "ws"),
        workspace.CHIP_JOBS_VAR: "2",
        workspace.CMAKE_JOBS_VAR: "2",
    }


def test_the_container_environment_caps_the_chip_gn_sub_build_too(tmp_path) -> None:
    """The inner CHIP GN/ninja build otherwise ignores the outer job cap.

    (patches/connectedhomeip-v1.5.1.0-vanilla-zephyr.patch,
    config/common/cmake/chip_gn.cmake.)
    """
    env = container.container_environment(
        topdir=tmp_path / "ws", pyshim_dir=tmp_path / "shim", jobs=2
    )
    assert env[workspace.CHIP_JOBS_VAR] == "2"


def test_a_different_jobs_value_reaches_the_chip_gn_sub_build_too(tmp_path) -> None:
    env = container.container_environment(
        topdir=tmp_path / "ws", pyshim_dir=tmp_path / "shim", jobs=4
    )
    assert env[workspace.CHIP_JOBS_VAR] == "4"


def test_the_run_command_mounts_paths_onto_themselves(tmp_path) -> None:
    """Identity mounts: a build tree is full of absolute paths.

    Mounting the workspace at /workspace would make every path in the
    CMake cache a lie the moment anyone looked at the build directory
    from the host — or ran the same build with --method local-dev.
    """
    command = container.docker_run_command(
        docker="docker",
        image="img:tag",
        mounts=[Path("/ws")],
        ccache_dir=Path("/home/u/.cache/mcuhome/ccache"),
        workdir=Path("/ws"),
        environment={"B": "2", "A": "1"},
        command=["west", "build"],
        user="1000:1000",
    )
    assert command == [
        "docker",
        "run",
        "--rm",
        "--init",
        "--network=none",
        "--user",
        "1000:1000",
        "--volume",
        "/ws:/ws",
        "--volume",
        f"/home/u/.cache/mcuhome/ccache:{container.CONTAINER_CCACHE_DIR}",
        "--workdir",
        "/ws",
        "--env",
        "A=1",
        "--env",
        "B=2",
        "img:tag",
        "west",
        "build",
    ]


def test_a_platform_without_uids_simply_does_not_map_one(tmp_path) -> None:
    command = container.docker_run_command(
        docker="docker",
        image="img:tag",
        mounts=[Path("/ws")],
        ccache_dir=Path("/c"),
        workdir=Path("/ws"),
        environment={},
        command=["true"],
        user=None,
    )
    assert "--user" not in command


# --------------------------------------------------------------------------
# The three ways it refuses
# --------------------------------------------------------------------------


def test_no_docker_at_all_says_what_to_install() -> None:
    runner = _Runner(None)
    with pytest.raises(BuildError) as caught:
        container.preflight("docker", "img:tag", env={}, runner=runner)
    assert "cannot find docker on your PATH" in caught.value.message
    hint = caught.value.hint or ""
    assert "docs.docker.com" in hint
    assert "--method local-dev" in hint
    # Asking an absent program a second question tells nobody anything.
    assert len(runner.commands) == 1


def test_a_stopped_daemon_is_not_a_missing_docker() -> None:
    with pytest.raises(BuildError) as caught:
        container.preflight("docker", "img:tag", env={}, runner=_Runner(1))
    assert "cannot talk to the Docker daemon" in caught.value.message
    assert "systemctl start docker" in (caught.value.hint or "")


def test_a_missing_image_says_how_to_get_one_both_ways() -> None:
    with pytest.raises(BuildError) as caught:
        container.preflight("docker", "ghcr.io/mcu-home/builder:x", env={}, runner=_Runner(0, 1))
    assert caught.value.message == (
        "The MCUHome builder image ghcr.io/mcu-home/builder:x is not on this machine."
    )
    hint = caught.value.hint or ""
    assert "docker pull ghcr.io/mcu-home/builder:x" in hint
    assert (
        f"docker build -t ghcr.io/mcu-home/builder:x -f {container.DOCKERFILE_DIR}/Dockerfile ."
        in hint
    )
    assert container.IMAGE_VAR in hint
    assert "--method local-dev" in hint


def test_a_working_docker_with_the_image_says_nothing() -> None:
    runner = _Runner(0, 0)
    container.preflight("docker", "img:tag", env={}, runner=runner)
    assert runner.commands == [
        ["docker", "version", "--format", "{{.Server.Version}}"],
        ["docker", "image", "inspect", "img:tag"],
    ]


def test_the_process_runner_is_resolved_at_call_time(tmp_path, monkeypatch) -> None:
    """Not a style point — a default bound in the signature is a real build.

    ``plan_build`` looks its runner up when it is called, so that
    replacing ``container._run_quiet`` actually replaces it. A default
    evaluated at definition time cannot be monkeypatched, and a test that
    believes it stubbed docker out but did not is a test that starts a
    Matter build on whoever ran pytest. That happened once.
    """
    calls: list[list[str]] = []

    def fake(command: Sequence[str], env: dict[str, str]) -> int:
        calls.append(list(command))
        return 0

    monkeypatch.setattr(container, "_run_quiet", fake)
    top = _fake_workspace(tmp_path / "ws")
    container.plan_build(
        out_dir=top / "build" / "node",
        app_subdir="app",
        board="x",
        env={container.CCACHE_DIR_VAR: str(tmp_path / "cache")},
        module_dir=top / "mcuhome",
        cwd=top,
        jobs=2,
    )
    assert [command[1] for command in calls] == ["version", "image"]


def test_the_swapped_program_is_the_one_that_gets_asked() -> None:
    runner = _Runner(0, 0)
    container.preflight("podman", "img:tag", env={}, runner=runner)
    assert all(command[0] == "podman" for command in runner.commands)


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


def test_the_plan_is_a_west_build_inside_a_docker_run(tmp_path, monkeypatch) -> None:
    top, plan = _plan(tmp_path, monkeypatch)

    assert plan.topdir == top
    assert plan.app_dir == top / "build" / "node" / "app"
    assert plan.build_dir == top / "build" / "node" / "build"
    assert plan.image == container.IMAGE
    assert plan.command[:4] == ["docker", "run", "--rm", "--init"]
    # The inner half is the local-dev path's command, character for character:
    # one build invocation, two ways of reaching a compiler.
    inner = plan.command[plan.command.index(container.IMAGE) + 1 :]
    assert inner == workspace.west_build_command(
        app_dir=plan.app_dir,
        build_dir=plan.build_dir,
        board="nrf7002dk/nrf5340/cpuapp",
        snippets=(),
        jobs=2,
    )


def test_the_plan_caps_the_chip_gn_sub_build_in_the_container_too(tmp_path, monkeypatch) -> None:
    """Same OOM risk as the local-dev path, one process tree down.

    (patches/connectedhomeip-v1.5.1.0-vanilla-zephyr.patch,
    config/common/cmake/chip_gn.cmake.)
    """
    _, plan = _plan(tmp_path, monkeypatch)
    assert f"{workspace.CHIP_JOBS_VAR}=2" in plan.command


def test_a_resolved_job_count_reaches_both_the_outer_and_inner_ninja(tmp_path, monkeypatch) -> None:
    """Both the outer west build and CHIP's inner GN sub-build agree.

    The host resolves jobs once; the container sees that same number for
    both — not a figure guessed at from inside the container, which may
    see a cgroup-limited view of the host's memory.
    """
    _, plan = _plan(tmp_path, monkeypatch, jobs=6)
    assert "-o=-j6" in plan.command
    assert f"{workspace.CHIP_JOBS_VAR}=6" in plan.command


def test_the_plan_needs_no_toolchain_on_the_host(tmp_path, monkeypatch) -> None:
    """The whole promise of ADR 0007, as an assertion.

    An empty PATH is what a user who installed nothing but docker has.
    The local-dev path refuses that by design; this one must not notice.
    """
    _, plan = _plan(tmp_path, monkeypatch, env={"PATH": ""})
    assert plan.image == container.IMAGE


def test_the_plan_mounts_the_workspace_and_the_cache(tmp_path, monkeypatch) -> None:
    top, plan = _plan(tmp_path, monkeypatch)
    volumes = [
        plan.command[index + 1] for index, item in enumerate(plan.command) if item == "--volume"
    ]
    assert f"{top}:{top}" in volumes
    assert f"{tmp_path / 'cache'}:{container.CONTAINER_CCACHE_DIR}" in volumes


def test_the_cache_directory_exists_before_docker_could_create_it(tmp_path, monkeypatch) -> None:
    """Docker would create the bind-mount source owned by root.

    Which is the one thing the UID mapping above exists to prevent, so
    the builder makes the directory itself, first.
    """
    cache = tmp_path / "cache"
    assert not cache.exists()
    _plan(tmp_path, monkeypatch)
    assert cache.is_dir()


def test_the_plan_can_be_pointed_at_another_image(tmp_path, monkeypatch) -> None:
    _, plan = _plan(tmp_path, monkeypatch, image="localhost/builder:wip")
    assert plan.image == "localhost/builder:wip"
    assert "localhost/builder:wip" in plan.command


def test_the_signing_key_is_mounted_read_only_and_by_itself(tmp_path, monkeypatch) -> None:
    """imgtool runs inside the container, so the key has to be reachable.

    One file, not its directory, and read-only: nothing in a build
    container has any business writing a signing key, and the rest of
    ~/.config is none of its business either (ADR 0015 decision 8).
    """
    key = tmp_path / "cfg" / "mcuhome" / "signing.key"
    key.parent.mkdir(parents=True)
    key.write_text("", "utf-8")
    _, plan = _plan(tmp_path, monkeypatch, signing_key=key)

    volumes = [
        plan.command[index + 1] for index, item in enumerate(plan.command) if item == "--volume"
    ]
    assert f"{key}:{key}:ro" in volumes
    assert f"{key.parent}:{key.parent}" not in volumes
    assert f'-D{workspace.SIGNING_KEY_OPTION}="{key}"' in plan.command


def test_without_a_key_nothing_extra_is_mounted(tmp_path, monkeypatch) -> None:
    _, plan = _plan(tmp_path, monkeypatch)
    assert not any(item.endswith(":ro") for item in plan.command)


def test_the_inner_command_is_a_sysbuild_build(tmp_path, monkeypatch) -> None:
    """The container path builds exactly what the local-dev path builds."""
    _, plan = _plan(tmp_path, monkeypatch, snippets=("matter",), bootloader_snippets=("boot-mode",))
    assert "--sysbuild" in plan.command
    assert "-Dapp_SNIPPET=matter" in plan.command
    assert "-Dmcuboot_SNIPPET=boot-mode" in plan.command


def test_a_build_directory_outside_the_workspace_is_still_visible(tmp_path, monkeypatch) -> None:
    outside = tmp_path / "elsewhere" / "node"
    outside.mkdir(parents=True)
    _, plan = _plan(tmp_path, monkeypatch, out_dir=outside)
    volumes = [
        plan.command[index + 1] for index, item in enumerate(plan.command) if item == "--volume"
    ]
    assert f"{outside}:{outside}" in volumes


def test_the_plan_refuses_before_it_decides_anything_else(tmp_path) -> None:
    top = _fake_workspace(tmp_path / "ws")
    with pytest.raises(BuildError) as caught:
        container.plan_build(
            out_dir=top / "build" / "node",
            app_subdir="app",
            board="x",
            env={container.CCACHE_DIR_VAR: str(tmp_path / "cache")},
            module_dir=top / "mcuhome",
            cwd=top,
            runner=_Runner(0, 1),
            jobs=2,
        )
    assert "is not on this machine" in caught.value.message
    # Nothing was created for a build that was never going to happen.
    assert not (tmp_path / "cache").exists()


def test_the_build_gets_no_network() -> None:
    """Contract §9 makes the isolation the backend's obligation.

    "Everything a build needs is mounted or in the image" cannot be read
    off a build log: a step that fetches something succeeds on the
    machine that has the network and fails on the one that does not.
    Three undeclared build inputs survived until 2026-08-09 for exactly
    that reason. Taking the network away is what turns the sentence into
    a property the build either has or does not.
    """
    command = container.docker_run_command(
        docker="docker",
        image="img:tag",
        mounts=[Path("/ws")],
        ccache_dir=Path("/c"),
        workdir=Path("/ws"),
        environment={},
        command=["west", "build"],
    )
    assert "--network=none" in command


def test_the_network_can_be_restored_for_a_bisect() -> None:
    """The escape hatch exists to find what reaches out, not to build with.

    A build that needs it is a build with an undeclared input, so the
    variable's only legitimate use is finding out which one.
    """
    command = container.docker_run_command(
        docker="docker",
        image="img:tag",
        mounts=[Path("/ws")],
        ccache_dir=Path("/c"),
        workdir=Path("/ws"),
        environment={},
        command=["west", "build"],
        network=True,
    )
    assert not any(argument.startswith("--network") for argument in command)
