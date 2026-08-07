# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""``mcuhome new``: the first file of a device, and what it is worth.

A scaffold that produces something the next command rejects is not a
scaffold, so the important test here is the round trip: create a device,
draw its credentials, validate it — with nothing edited in between.
"""

from __future__ import annotations

import pytest

from mcuhome import provision, registry, scaffold
from mcuhome.api import load_model, open_config_tree, validate_device
from mcuhome.errors import ConfigError
from mcuhome.tree import DEVICE_ENTRY, DEVICES_DIR, is_config_root

BOARD = "nrf7002dk/nrf5340/cpuapp"


# --------------------------------------------------------------------------
# Where it writes
# --------------------------------------------------------------------------


def test_it_creates_the_device_folder(tmp_path) -> None:
    (tmp_path / DEVICES_DIR).mkdir()
    created = scaffold.new_device("bench-node", board=BOARD, config_root=tmp_path)
    assert created.entry == tmp_path / DEVICES_DIR / "bench-node" / DEVICE_ENTRY
    assert created.entry.is_file()
    assert created.created_tree is False


def test_without_a_tree_it_starts_one(tmp_path) -> None:
    """A directory with devices/ in it is a tree; making the folder makes it."""
    created = scaffold.new_device("bench-node", board=BOARD, cwd=tmp_path)
    assert created.created_tree is True
    assert is_config_root(tmp_path)
    assert open_config_tree(tmp_path).root == tmp_path


def test_it_finds_the_tree_above_the_working_directory(tmp_path) -> None:
    (tmp_path / DEVICES_DIR).mkdir()
    deeper = tmp_path / DEVICES_DIR / "other"
    deeper.mkdir()
    created = scaffold.new_device("bench-node", board=BOARD, cwd=deeper)
    assert created.tree.root == tmp_path


# --------------------------------------------------------------------------
# What it refuses
# --------------------------------------------------------------------------


def test_it_never_overwrites_a_device(tmp_path) -> None:
    scaffold.new_device("bench-node", board=BOARD, config_root=tmp_path)
    before = (tmp_path / DEVICES_DIR / "bench-node" / DEVICE_ENTRY).read_text("utf-8")
    with pytest.raises(ConfigError) as caught:
        scaffold.new_device("bench-node", board=BOARD, config_root=tmp_path)
    assert "already a device" in caught.value.message
    assert (tmp_path / DEVICES_DIR / "bench-node" / DEVICE_ENTRY).read_text("utf-8") == before


@pytest.mark.parametrize("name", ["Bench Node", "bench_node", "bench-", "-bench", "x" * 40])
def test_a_name_that_cannot_be_a_hostname_is_refused(tmp_path, name: str) -> None:
    with pytest.raises(ConfigError) as caught:
        scaffold.new_device(name, board=BOARD, config_root=tmp_path)
    assert "usable device name" in caught.value.message
    assert not (tmp_path / DEVICES_DIR).exists()


def test_an_unknown_board_lists_the_ones_that_exist(tmp_path) -> None:
    with pytest.raises(ConfigError) as caught:
        scaffold.new_device("bench-node", board="nrf99dk", config_root=tmp_path)
    assert BOARD in caught.value.hint


def test_a_planned_board_says_why_it_is_not_there_yet(tmp_path) -> None:
    planned = next(iter(registry.PLANNED_BOARDS))
    with pytest.raises(ConfigError) as caught:
        scaffold.new_device("bench-node", board=planned, config_root=tmp_path)
    assert registry.PLANNED_BOARDS[planned] in caught.value.message


def test_a_configuration_root_that_is_not_there_is_a_refusal(tmp_path) -> None:
    with pytest.raises(ConfigError) as caught:
        scaffold.new_device("bench-node", board=BOARD, config_root=tmp_path / "nope")
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
    created = scaffold.new_device("bench-node", board=BOARD, config_root=tmp_path)
    tree = created.tree

    # Before the credentials exist, validation says exactly which command
    # writes them — which is what makes the scaffold's next step honest.
    result = validate_device(created.entry, tree=tree)
    assert not result.ok
    assert any("init-pairing bench-node" in (error.hint or "") for error in result.errors)

    provision.init_pairing(created.entry, secrets_file=tree.secrets_file, use_secrets=False)

    model = load_model(created.entry, tree=tree)
    assert model.device.name == "bench-node"
    assert model.device.board == BOARD
    assert model.network.pairing is not None
    assert model.network.pairing.test_credentials is False
