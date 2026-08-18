# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Stage 5, container path (``mcuhome/workbench/buildenv.py``).

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
image (see containers/build-container/README.md).
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from mcuhome.model.errors import BuildError

from mcuhome.workbench import buildenv as container


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


# --------------------------------------------------------------------------
# Which image
# --------------------------------------------------------------------------


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
# The three ways it refuses
# --------------------------------------------------------------------------


def test_no_docker_at_all_says_what_to_install() -> None:
    runner = _Runner(None)
    with pytest.raises(BuildError) as caught:
        container.preflight("docker", "img:tag", env={}, runner=runner)
    assert "cannot find docker on your PATH" in caught.value.message
    hint = caught.value.hint or ""
    assert "docs.docker.com" in hint
    # The container-less alternative is one generic pointer (PO
    # 2026-08-15) — mode specifics belong to the build command's help.
    assert "mcuhome device build --help" in hint
    # Asking an absent program a second question tells nobody anything.
    assert len(runner.commands) == 1


def test_a_stopped_daemon_is_not_a_missing_docker() -> None:
    with pytest.raises(BuildError) as caught:
        container.preflight("docker", "img:tag", env={}, runner=_Runner(1))
    assert "cannot talk to the Docker daemon" in caught.value.message
    assert "systemctl start docker" in (caught.value.hint or "")


def test_a_missing_image_leads_with_the_pull_command() -> None:
    """PO 2026-08-15: the pull is the fix for almost everyone, so it is
    the whole hint apart from one pointer at the build command's help;
    building the image from source stays in the container README."""
    with pytest.raises(BuildError) as caught:
        container.preflight(
            "docker",
            "ghcr.io/mcu-home/build-container:x",
            env={},
            runner=_Runner(0, 1),
        )
    assert caught.value.message == (
        "The build container ghcr.io/mcu-home/build-container:x is missing on this host."
    )
    hint = caught.value.hint or ""
    assert "docker pull ghcr.io/mcu-home/build-container:x" in hint.splitlines()[1].strip()
    assert "mcuhome device build --help" in hint
    assert "docker build" not in hint


def test_the_default_image_missing_says_default() -> None:
    """The everyday case reads as the everyday case, not as a reference."""
    refusal = container.missing_image_refusal("docker", container.IMAGE)
    assert refusal.message == "The default build container is missing on this host."
    assert f"docker pull {container.IMAGE}" in (refusal.hint or "")


def test_a_working_docker_with_the_image_says_nothing() -> None:
    runner = _Runner(0, 0)
    container.preflight("docker", "img:tag", env={}, runner=runner)
    assert runner.commands == [
        ["docker", "version", "--format", "{{.Server.Version}}"],
        ["docker", "image", "inspect", "img:tag"],
    ]


def test_the_process_runner_is_resolved_at_call_time(monkeypatch) -> None:
    """The one impure thing is replaceable, and replacing it works.

    A default bound in a signature cannot be monkeypatched, and a test
    that thinks it stubbed docker out but did not is a test that starts a
    real container.
    """
    calls: list[list[str]] = []

    def fake(command: Sequence[str], env: dict[str, str]) -> int:
        calls.append(list(command))
        return 0

    monkeypatch.setattr(container, "_run_quiet", fake)
    container.preflight("docker", "img:tag", env={})
    assert [command[1] for command in calls] == ["version", "image"]


def test_the_swapped_program_is_the_one_that_gets_asked() -> None:
    runner = _Runner(0, 0)
    container.preflight("podman", "img:tag", env={}, runner=runner)
    assert all(command[0] == "podman" for command in runner.commands)
