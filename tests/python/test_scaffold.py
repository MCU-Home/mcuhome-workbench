# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""``mcuhome device new``: the first file of a device, and what it is worth.

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
from mcuhome.workbench.project import DEVICE_FILE, DEVICES_DIR, create_project

BOARD = "nrf7002dk/nrf5340/cpuapp"


# --------------------------------------------------------------------------
# Where it writes
# --------------------------------------------------------------------------


def test_it_creates_the_device_folder(tmp_path) -> None:
    project = create_project(tmp_path).project
    created = scaffold.create_device("bench-node", project=project, board=BOARD)
    assert created.entry == tmp_path / DEVICES_DIR / "bench-node" / DEVICE_FILE
    assert created.entry.is_file()
    assert created.project.root == tmp_path


# --------------------------------------------------------------------------
# What it refuses
# --------------------------------------------------------------------------


def test_it_never_overwrites_a_device(tmp_path) -> None:
    project = create_project(tmp_path).project
    scaffold.create_device("bench-node", project=project, board=BOARD)
    before = (tmp_path / DEVICES_DIR / "bench-node" / DEVICE_FILE).read_text("utf-8")
    with pytest.raises(ConfigError) as caught:
        scaffold.create_device("bench-node", project=project, board=BOARD)
    assert "already a device" in caught.value.message
    assert (tmp_path / DEVICES_DIR / "bench-node" / DEVICE_FILE).read_text("utf-8") == before


@pytest.mark.parametrize(
    "name", ["Bench Node", "bench_node", "bench-", "-bench", "x" * 40, "1234", "1-2"]
)
def test_a_name_that_cannot_be_a_hostname_is_refused(tmp_path, name: str) -> None:
    project = create_project(tmp_path).project
    with pytest.raises(ConfigError) as caught:
        scaffold.create_device(name, project=project, board=BOARD)
    assert "usable device name" in caught.value.message


def test_a_single_letter_name_is_allowed(tmp_path) -> None:
    """The floor is non-empty plus one letter — no length minimum (PO 2026-08-15)."""
    project = create_project(tmp_path).project
    scaffold.create_device("a", project=project, board=BOARD)
    assert (tmp_path / DEVICES_DIR / "a" / DEVICE_FILE).is_file()


def test_an_unknown_board_lists_the_ones_that_exist(tmp_path) -> None:
    project = create_project(tmp_path).project
    with pytest.raises(ConfigError) as caught:
        scaffold.create_device("bench-node", project=project, board="nrf99dk")
    assert BOARD in caught.value.hint


def test_a_planned_board_says_why_it_is_not_there_yet(tmp_path) -> None:
    project = create_project(tmp_path).project
    planned = next(iter(registry.PLANNED_BOARDS))
    with pytest.raises(ConfigError) as caught:
        scaffold.create_device("bench-node", project=project, board=planned)
    assert registry.PLANNED_BOARDS[planned] in caught.value.message


# --------------------------------------------------------------------------
# What it writes
# --------------------------------------------------------------------------


def test_the_starter_names_the_next_step_rather_than_taking_it() -> None:
    """Credentials are drawn once, by their own command, on purpose."""
    text = scaffold.render_device_file("bench-node", board=BOARD)
    assert "mcuhome device matter-pairing --new bench-node" in text
    assert "discriminator:" not in text
    assert "passcode:" not in text


def test_the_starter_carries_a_complete_commented_example() -> None:
    text = scaffold.render_device_file("bench-node", board=BOARD)
    for fragment in ("# hardware:", "# node:", "#   endpoints:", "#       device_type:"):
        assert fragment in text
    driver = next(iter(registry.DRIVERS))
    assert f"#       driver: {driver}" in text


def test_the_starter_pins_no_package_version(tmp_path) -> None:
    """A new device carries no ``sources:`` — not even a commented one.

    Every entry of that block is an override, and the default is resolved
    at build time on purpose: a device created today must not be frozen
    onto the SDK and the build environment that happened to be current on
    the day somebody ran ``device new``. Asserted on the written file and
    not only on the rendered text, because the file is what a user keeps.
    """
    project = create_project(tmp_path).project
    created = scaffold.create_device("bench-node", project=project, board=BOARD)
    assert "sources" not in created.entry.read_text(encoding="utf-8")


def test_the_starter_uses_the_boards_own_transport() -> None:
    text = scaffold.render_device_file("bench-node", board=BOARD)
    assert "thread" in registry.BOARDS[BOARD].transports
    assert "  thread:" in text
    assert "device_role: ftd" in text


def test_the_scaffold_is_deterministic() -> None:
    assert scaffold.render_device_file("bench-node", board=BOARD) == scaffold.render_device_file(
        "bench-node", board=BOARD
    )


# --------------------------------------------------------------------------
# The round trip
# --------------------------------------------------------------------------


def test_new_then_init_pairing_then_validate(tmp_path) -> None:
    """The three commands the scaffold's own header names, in that order."""
    project = create_project(tmp_path).project
    created = scaffold.create_device("bench-node", project=project, board=BOARD)
    project = created.project

    # Before the credentials exist, validation says exactly which command
    # writes them — which is what makes the scaffold's next step honest.
    result = validate_device(created.entry, project=project)
    assert not result.ok
    assert any("matter-pairing --new bench-node" in (error.hint or "") for error in result.errors)

    provision.create_pairing(created.entry, project=project)

    model = load_model(created.entry, project=project)
    assert model.device.name == "bench-node"
    assert model.device.board == BOARD
    assert model.network.pairing is not None
    assert model.network.pairing.test_credentials is False


def test_the_starter_takes_a_friendly_name_and_quotes_it() -> None:
    """`device new --name`: the human name for the Matter identity."""
    text = scaffold.render_device_file("bench-node", board=BOARD, friendly_name='Bench: "A"')
    assert 'friendly_name: "Bench: \\"A\\""' in text
    plain = scaffold.render_device_file("bench-node", board=BOARD)
    assert 'friendly_name: "Bench Node"' in plain


def test_new_device_writes_the_friendly_name_through(tmp_path) -> None:
    project = create_project(tmp_path / "p").project
    created = scaffold.create_device(
        "bench-node",
        project=project,
        board=BOARD,
        friendly_name="Workbench Node",
    )
    assert 'friendly_name: "Workbench Node"' in created.entry.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# An outline: what a caller already knows
# --------------------------------------------------------------------------


def _outline() -> scaffold.DeviceOutline:
    """The reference device, expressed as a caller's picks.

    Built from the registry rather than from literals, so it describes
    whatever MCUHome supports on the day it runs.
    """
    driver = next(iter(registry.DRIVERS.values()))
    bus = next(entry for entry in registry.BOARDS[BOARD].buses if entry.kind == driver.bus)
    clusters = [
        (cluster, channel)
        for cluster in registry.CLUSTERS.values()
        for channel in driver.channels.values()
        if channel.quantity == cluster.quantity
    ]
    endpoints = tuple(
        scaffold.EndpointChoice(
            device_type=next(
                entry.name
                for entry in registry.DEVICE_TYPES.values()
                if entry.mandatory_clusters == (cluster.name,)
            ),
            clusters=(
                scaffold.ClusterChoice(cluster=cluster.name, source=f"probe.{channel.name}"),
            ),
        )
        for cluster, channel in clusters
    )
    return scaffold.DeviceOutline(
        buses=(scaffold.BusChoice(id="i2c0", controller=bus.controller),),
        peripherals=(scaffold.PeripheralChoice(id="probe", driver=driver.compatible, bus="i2c0"),),
        endpoints=endpoints,
    )


def test_an_outline_is_written_as_configuration_and_not_as_an_example() -> None:
    text = scaffold.render_device_file("bench-node", board=BOARD, outline=_outline())
    assert "hardware:" in text and "# hardware:" not in text
    assert "node:" in text and "# node:" not in text
    assert "  peripherals:" in text
    assert "    - id: 1" in text


def test_an_empty_outline_is_the_same_as_none() -> None:
    """A caller that collected nothing gets the command line's file."""
    plain = scaffold.render_device_file("bench-node", board=BOARD)
    empty = scaffold.render_device_file("bench-node", board=BOARD, outline=scaffold.DeviceOutline())
    assert empty == plain


def test_a_device_scaffolded_from_an_outline_validates(tmp_path) -> None:
    """The test that makes the outline worth having.

    A wizard that writes something the next command rejects has helped
    nobody, so this walks the whole way: pick, scaffold, draw
    credentials, resolve. Nothing is edited in between.
    """
    project = create_project(tmp_path).project
    created = scaffold.create_device("bench-node", project=project, board=BOARD, outline=_outline())
    provision.create_pairing(created.entry, project=created.project)

    result = validate_device(created.entry, project=created.project)
    assert result.ok, [error.message for error in result.errors]

    model = load_model(created.entry, project=created.project)
    assert [[entry.name for entry in endpoint.device_types] for endpoint in model.endpoints] == [
        [endpoint.device_type] for endpoint in _outline().endpoints
    ]
    assert model.channels, "the peripheral's channels reached the model"


def test_an_optional_value_is_written_only_when_it_was_chosen() -> None:
    """An explicit copy of a default is a value nobody picked."""
    driver = next(iter(registry.DRIVERS.values()))
    bare = scaffold.render_device_file(
        "bench-node",
        board=BOARD,
        outline=scaffold.DeviceOutline(
            peripherals=(scaffold.PeripheralChoice(id="probe", driver=driver.compatible),)
        ),
    )
    assert "address:" not in bare
    assert "bus:" not in bare
    assert "sampling:" not in bare

    chosen = scaffold.render_device_file(
        "bench-node",
        board=BOARD,
        outline=scaffold.DeviceOutline(
            peripherals=(
                scaffold.PeripheralChoice(id="probe", driver=driver.compatible, address=0x77),
            )
        ),
    )
    assert "      address: 0x77" in chosen


def test_an_outline_naming_a_driver_nobody_supports_is_refused() -> None:
    with pytest.raises(ConfigError) as caught:
        scaffold.render_device_file(
            "bench-node",
            board=BOARD,
            outline=scaffold.DeviceOutline(
                peripherals=(scaffold.PeripheralChoice(id="probe", driver="acme,nonesuch"),)
            ),
        )
    assert "not a driver MCUHome knows" in caught.value.message
    assert next(iter(registry.DRIVERS)) in (caught.value.hint or "")


def test_a_planned_driver_says_why_it_is_not_there_yet() -> None:
    name, reason = next(iter(registry.PLANNED_DRIVERS.items()))
    with pytest.raises(ConfigError) as caught:
        scaffold.render_device_file(
            "bench-node",
            board=BOARD,
            outline=scaffold.DeviceOutline(
                peripherals=(scaffold.PeripheralChoice(id="probe", driver=name),)
            ),
        )
    assert reason in caught.value.message


def test_a_source_pointing_at_nothing_is_refused_before_a_file_exists(tmp_path) -> None:
    """The check runs before the folder is touched, so a refusal leaves nothing."""
    project = create_project(tmp_path).project
    driver = next(iter(registry.DRIVERS.values()))
    cluster = next(iter(registry.CLUSTERS.values()))
    device_type = next(
        entry.name
        for entry in registry.DEVICE_TYPES.values()
        if cluster.name in entry.mandatory_clusters
    )
    with pytest.raises(ConfigError) as caught:
        scaffold.create_device(
            "bench-node",
            project=project,
            board=BOARD,
            outline=scaffold.DeviceOutline(
                peripherals=(scaffold.PeripheralChoice(id="probe", driver=driver.compatible),),
                endpoints=(
                    scaffold.EndpointChoice(
                        device_type=device_type,
                        clusters=(
                            scaffold.ClusterChoice(cluster=cluster.name, source="typo.temperature"),
                        ),
                    ),
                ),
            ),
        )
    assert "no peripheral called" in caught.value.message
    assert not (tmp_path / DEVICES_DIR / "bench-node").exists()


def test_a_channel_the_part_does_not_have_is_refused() -> None:
    driver = next(iter(registry.DRIVERS.values()))
    cluster = next(iter(registry.CLUSTERS.values()))
    device_type = next(
        entry.name
        for entry in registry.DEVICE_TYPES.values()
        if cluster.name in entry.mandatory_clusters
    )
    with pytest.raises(ConfigError) as caught:
        scaffold.render_device_file(
            "bench-node",
            board=BOARD,
            outline=scaffold.DeviceOutline(
                peripherals=(scaffold.PeripheralChoice(id="probe", driver=driver.compatible),),
                endpoints=(
                    scaffold.EndpointChoice(
                        device_type=device_type,
                        clusters=(
                            scaffold.ClusterChoice(cluster=cluster.name, source="probe.nonesuch"),
                        ),
                    ),
                ),
            ),
        )
    assert "no channel called" in caught.value.message
    assert next(iter(driver.channels)) in (caught.value.hint or "")


def test_a_peripheral_on_a_bus_the_outline_never_described_is_refused() -> None:
    driver = next(iter(registry.DRIVERS.values()))
    with pytest.raises(ConfigError) as caught:
        scaffold.render_device_file(
            "bench-node",
            board=BOARD,
            outline=scaffold.DeviceOutline(
                peripherals=(
                    scaffold.PeripheralChoice(id="probe", driver=driver.compatible, bus="i2c0"),
                )
            ),
        )
    assert "which the outline does not describe" in caught.value.message


def test_the_commented_example_names_a_bus_the_board_actually_has() -> None:
    """The example is looked up, not typed — a renamed node label follows it."""
    board = registry.BOARDS[BOARD]
    driver = next(iter(registry.DRIVERS.values()))
    bus = next(entry for entry in board.buses if entry.kind == driver.bus)
    assert f"#       controller: {bus.controller}" in scaffold.render_device_file(
        "bench-node", board=BOARD
    )
