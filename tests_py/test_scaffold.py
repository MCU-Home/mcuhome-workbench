# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""``mcuhome new``: the first file of a device, and what it is worth.

A scaffold that produces something the next command rejects is not a
scaffold, so the important test here is the round trip: create a device,
draw its credentials, validate it — with nothing edited in between.
"""

from __future__ import annotations

import pytest
from mcuhome.model import registry
from mcuhome.model.errors import ConfigError

from mcuhome.workbench import provision, scaffold
from mcuhome.workbench.api import load_model, validate_device
from mcuhome.workbench.project import DEVICE_ENTRY, DEVICES_DIR, init_project

BOARD = "nrf7002dk/nrf5340/cpuapp"


# --------------------------------------------------------------------------
# Where it writes
# --------------------------------------------------------------------------


def test_it_creates_the_device_folder(tmp_path) -> None:
    init_project(tmp_path)
    created = scaffold.new_device("bench-node", board=BOARD, cwd=tmp_path, env={})
    assert created.entry == tmp_path / DEVICES_DIR / "bench-node" / DEVICE_ENTRY
    assert created.entry.is_file()
    assert created.project.root == tmp_path


def test_without_a_project_it_refuses(tmp_path) -> None:
    """A directory must be a project; scaffold does not create one."""
    with pytest.raises(ConfigError) as caught:
        scaffold.new_device("bench-node", board=BOARD, cwd=tmp_path, env={})
    assert "No MCUHome project found" in caught.value.message
    assert "mcuhome init" in caught.value.hint


def test_it_finds_the_project_above_the_working_directory(tmp_path) -> None:
    init_project(tmp_path)
    deeper = tmp_path / DEVICES_DIR / "other"
    deeper.mkdir()
    created = scaffold.new_device("bench-node", board=BOARD, cwd=deeper, env={})
    assert created.project.root == tmp_path


# --------------------------------------------------------------------------
# What it refuses
# --------------------------------------------------------------------------


def test_it_never_overwrites_a_device(tmp_path) -> None:
    init_project(tmp_path)
    scaffold.new_device("bench-node", board=BOARD, cwd=tmp_path, env={})
    before = (tmp_path / DEVICES_DIR / "bench-node" / DEVICE_ENTRY).read_text("utf-8")
    with pytest.raises(ConfigError) as caught:
        scaffold.new_device("bench-node", board=BOARD, cwd=tmp_path, env={})
    assert "already a device" in caught.value.message
    assert (tmp_path / DEVICES_DIR / "bench-node" / DEVICE_ENTRY).read_text("utf-8") == before


@pytest.mark.parametrize("name", ["Bench Node", "bench_node", "bench-", "-bench", "x" * 40])
def test_a_name_that_cannot_be_a_hostname_is_refused(tmp_path, name: str) -> None:
    init_project(tmp_path)
    with pytest.raises(ConfigError) as caught:
        scaffold.new_device(name, board=BOARD, cwd=tmp_path, env={})
    assert "usable device name" in caught.value.message


def test_an_unknown_board_lists_the_ones_that_exist(tmp_path) -> None:
    init_project(tmp_path)
    with pytest.raises(ConfigError) as caught:
        scaffold.new_device("bench-node", board="nrf99dk", cwd=tmp_path, env={})
    assert BOARD in caught.value.hint


def test_a_planned_board_says_why_it_is_not_there_yet(tmp_path) -> None:
    init_project(tmp_path)
    planned = next(iter(registry.PLANNED_BOARDS))
    with pytest.raises(ConfigError) as caught:
        scaffold.new_device("bench-node", board=planned, cwd=tmp_path, env={})
    assert registry.PLANNED_BOARDS[planned] in caught.value.message


def test_an_explicit_project_dir_that_is_not_there_is_a_refusal(tmp_path) -> None:
    with pytest.raises(ConfigError) as caught:
        scaffold.new_device(
            "bench-node", board=BOARD, cwd=tmp_path, env={}, project_dir=tmp_path / "nope"
        )
    assert "does not exist" in caught.value.message


# --------------------------------------------------------------------------
# What it writes
# --------------------------------------------------------------------------


def test_the_starter_names_the_next_step_rather_than_taking_it() -> None:
    """Credentials are drawn once, by their own command, on purpose."""
    text = scaffold.render_starter("bench-node", board=BOARD)
    assert "mcuhome init-pairing bench-node" in text
    assert "discriminator:" not in text
    assert "passcode:" not in text


def test_the_starter_carries_a_complete_commented_example() -> None:
    text = scaffold.render_starter("bench-node", board=BOARD)
    for fragment in ("# hardware:", "# node:", "#   endpoints:", "#       device_type:"):
        assert fragment in text
    driver = next(iter(registry.DRIVERS))
    assert f"#       driver: {driver}" in text


def test_the_starter_uses_the_boards_own_transport() -> None:
    text = scaffold.render_starter("bench-node", board=BOARD)
    assert "thread" in registry.BOARDS[BOARD].transports
    assert "  thread:" in text
    assert "device_role: ftd" in text


def test_the_scaffold_is_deterministic() -> None:
    assert scaffold.render_starter("bench-node", board=BOARD) == scaffold.render_starter(
        "bench-node", board=BOARD
    )


# --------------------------------------------------------------------------
# The round trip
# --------------------------------------------------------------------------


def test_new_then_init_pairing_then_validate(tmp_path) -> None:
    """The three commands the scaffold's own header names, in that order."""
    init_project(tmp_path)
    created = scaffold.new_device("bench-node", board=BOARD, cwd=tmp_path, env={})
    project = created.project

    # Before the credentials exist, validation says exactly which command
    # writes them — which is what makes the scaffold's next step honest.
    result = validate_device(created.entry, project=project)
    assert not result.ok
    assert any("init-pairing bench-node" in (error.hint or "") for error in result.errors)

    provision.init_pairing(created.entry, secrets_file=project.secrets_file, use_secrets=False)

    model = load_model(created.entry, project=project)
    assert model.device.name == "bench-node"
    assert model.device.board == BOARD
    assert model.network.pairing is not None
    assert model.network.pairing.test_credentials is False
