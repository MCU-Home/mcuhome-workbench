# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The per-board update scheme and flash layout (ADR 0015).

Two things are checked here that nothing else can check:

* the layout tables in the ADR and the devicetree the builder emits say
  the same numbers — they are written twice by construction (a table of
  offsets and a block of devicetree) and the second copy is the one that
  reaches the linker;
* **no module of the builder branches on a board name**, which is
  decision 2 of that ADR in one sentence. Supporting a board has to be a
  registry row, or the promise that horizontal scaling is a table edit is
  not true.
"""

from __future__ import annotations

import re
import tokenize
from pathlib import Path

import pytest

from mcuhome import registry

BOARD = "nrf7002dk/nrf5340/cpuapp"
SCHEME = registry.BOARDS[BOARD].update_scheme

#: The layout of ADR 0015 decision 3, transcribed from the ADR's own
#: table rather than from the code it is checking.
ADR_CLASS_A_LAYOUT = [
    ("mcuboot", None, 0x00000, 64 * 1024),
    ("image-0", None, 0x10000, 928 * 1024),
    ("storage", None, 0xF8000, 32 * 1024),
    ("image-1", "mx25r64", 0x00000, 928 * 1024),
]


def test_every_supported_board_has_an_update_scheme() -> None:
    """A board without one cannot be given a bootloader, so it cannot ship."""
    for name, board in registry.BOARDS.items():
        assert board.update_scheme is not None, f"{name} has no update scheme"


def test_the_dk_carries_the_adr_class_a_layout() -> None:
    assert SCHEME is not None
    assert [
        (entry.fixed_label, entry.device, entry.offset, entry.size) for entry in SCHEME.partitions
    ] == ADR_CLASS_A_LAYOUT
    assert SCHEME.board_class == "A"
    assert SCHEME.staging == "external-flash"


def test_storage_stays_where_the_board_already_puts_it() -> None:
    """The one partition an update must not move (ADR 0015, Consequences).

    Matter fabric credentials and the Thread dataset live in it, so a
    layout that relocated it would turn every firmware update into a
    re-commissioning. 0xF8000/32 KiB is also the board's own upstream
    default (zephyr/dts/common/nordic/nrf5340_cpuapp_partition.dtsi), so
    devices flashed before MCUHome had a bootloader keep their fabric.
    """
    assert SCHEME is not None
    storage = SCHEME.partition("storage_partition")
    assert (storage.offset, storage.size) == (0xF8000, 32 * 1024)


def test_the_internal_partitions_tile_the_part_without_overlapping() -> None:
    assert SCHEME is not None
    internal = sorted(
        (entry for entry in SCHEME.partitions if entry.device is None),
        key=lambda entry: entry.offset,
    )
    assert internal[0].offset == 0
    for lower, upper in zip(internal, internal[1:], strict=False):
        assert lower.end == upper.offset, f"{lower.fixed_label} does not meet {upper.fixed_label}"
    assert internal[-1].end == 1024 * 1024


def test_the_two_slots_are_the_same_size() -> None:
    """Swap needs one sector layout across both slots (MCUboot design.md)."""
    assert SCHEME is not None
    assert SCHEME.partition("slot0_partition").size == SCHEME.partition("slot1_partition").size


#: ``reg = <0x00010000 0x000e8000>;`` — offset and size, as devicetree.
_REG = re.compile(r"reg = <(0x[0-9a-f]+) (0x[0-9a-f]+)>;")


def test_the_overlay_states_the_same_numbers_as_the_table() -> None:
    """The table is what a human reads; the overlay is what the linker gets."""
    assert SCHEME is not None
    in_overlay = {
        (int(offset, 16), int(size, 16)) for offset, size in _REG.findall(SCHEME.partition_overlay)
    }
    # Only the partitions the overlay has to change appear in it: the
    # boot partition and storage are already right in the board's own
    # devicetree, which is the whole reason they are not restated.
    changed = {
        (entry.offset, entry.size)
        for entry in SCHEME.partitions
        if entry.fixed_label in ("image-0", "image-1")
    }
    assert in_overlay == changed
    assert "/delete-node/ &slot1_partition;" in SCHEME.partition_overlay


def test_the_sector_count_covers_the_larger_slot() -> None:
    """CONFIG_BOOT_MAX_IMG_SECTORS, which AUTO derives wrongly for us."""
    assert SCHEME is not None
    assert SCHEME.max_image_sectors == (928 * 1024) // 4096 == 232


def test_the_mode_names_a_symbol_sysbuild_knows() -> None:
    assert SCHEME is not None
    assert SCHEME.mcuboot_mode in registry.MCUBOOT_MODE_SYMBOLS


def test_class_a_keeps_the_usb_rescue_path() -> None:
    """ADR 0015 decision 6: USB/SMP everywhere, whatever else a board gets.

    Rollback is what class A adds; it does not replace serial recovery,
    which is the only transport that reaches an uncommissioned node and
    the only one left when an update went wrong in a way rollback cannot
    see. The 64 KiB boot partition of decision 3 is sized for exactly
    this configuration.
    """
    assert SCHEME is not None
    assert "serial-recovery" in SCHEME.recovery
    assert "CONFIG_MCUBOOT_SERIAL=y" in SCHEME.bootloader_kconfig
    assert "CONFIG_BOOT_SERIAL_BOOT_MODE=y" in SCHEME.bootloader_kconfig
    # The retention area both images read has to be the same devicetree
    # node, which is what one shared snippet guarantees.
    assert "boot-mode" in SCHEME.bootloader_snippets
    assert "boot-mode" in SCHEME.application_snippets


def test_an_external_slot_brings_its_driver_and_its_erase_unit() -> None:
    """MCUboot's own board fragment switches SPI-NOR off; we need it on.

    And its layout page size defaults to the 64 KiB block erase, which
    would give the two slots different sector layouts.
    """
    assert SCHEME is not None
    assert "CONFIG_SPI_NOR=y" in SCHEME.bootloader_kconfig
    assert (
        f"CONFIG_SPI_NOR_FLASH_LAYOUT_PAGE_SIZE={SCHEME.erase_block_size}"
        in SCHEME.bootloader_kconfig
    )


# --------------------------------------------------------------------------
# ADR 0015 decision 2, as an invariant
# --------------------------------------------------------------------------

#: Every board name the registry knows, in both spellings a Python file
#: could plausibly use.
_BOARD_NAMES = tuple(registry.BOARDS) + tuple(registry.PLANNED_BOARDS)


def _code_of(module: Path) -> str:
    """*module* with its comments and string literals removed.

    The invariant below is about code, not about prose: an error message
    is allowed to quote a board name — that is what makes it useful —
    while a comparison against one is the thing that must not exist.
    """
    with module.open("rb") as handle:
        return " ".join(
            token.string
            for token in tokenize.tokenize(handle.readline)
            if token.type not in (tokenize.COMMENT, tokenize.STRING)
        )


@pytest.mark.parametrize("name", _BOARD_NAMES)
def test_no_module_outside_the_registry_names_a_board(name: str) -> None:
    """Nothing in the builder may branch on a board name (ADR 0015 §2).

    Supporting a new board is a table row plus a bring-up, exactly like
    adding a driver or a cluster. The moment a board name appears in
    code, that stops being true and nobody notices until the second
    board.
    """
    package = Path(registry.__file__).parent
    for module in sorted(package.glob("*.py")):
        if module.name == "registry.py":
            continue
        assert name not in _code_of(module), f"{module.name} names the board {name}"
