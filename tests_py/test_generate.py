# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Golden-file tests of pipeline stage 4 (builder-pipeline.md §9).

Two kinds of golden file live here, for one reason each:

* ``tests_py/data/golden/`` holds the overlay, the Kconfig fragment and
  the application ``CMakeLists.txt``. They exist so a change in generator
  output shows up as a reviewable diff instead of as a silent behavior
  change on someone's device.
* ``samples/matter-node/src/mcuhome_config.{c,h}`` **are** the expected
  output. ADR 0014 makes the phase-1 sample the codegen fixture, so the
  committed sample files and fresh generator output must be byte-equal —
  that is the mechanism that keeps the runtime contract, the sample and
  the generator from drifting apart.

Everything is compared byte for byte on purpose: whitespace, comment
wording and symbol names are what a user reads when they open the build
directory, so a change in them is a change worth reviewing.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess

import pytest
from conftest import EXAMPLES_DIR, GOLDEN_DIR, REPO_ROOT, resolve_file

from mcuhome import pairing, registry
from mcuhome.errors import GenerationError
from mcuhome.generate import (
    APP_DIR,
    BOOTLOADER_IMAGE,
    CHIP_PROJECT_CONFIG_PATH,
    DETACHED_SIGNING_CMAKE,
    MODEL_FILE,
    SYSBUILD_CMAKE,
    SYSBUILD_CONF,
    SYSBUILD_DIR,
    board_file_stem,
    generate,
    write_tree,
)
from mcuhome.model import (
    BusModel,
    DeviceMeta,
    DeviceModel,
    HardwareModel,
    NetworkModel,
    PeripheralModel,
    ToolchainModel,
)

EXAMPLE = EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml"
SAMPLE_DIR = REPO_ROOT / "samples" / "matter-node"
SAMPLE_SRC = SAMPLE_DIR / "src"

OVERLAY_PATH = f"{APP_DIR}/boards/nrf7002dk_nrf5340_cpuapp.overlay"
SOURCE_PATH = f"{APP_DIR}/src/mcuhome_config.c"
HEADER_PATH = f"{APP_DIR}/src/mcuhome_config.h"
CMAKE_PATH = f"{APP_DIR}/CMakeLists.txt"
CHIP_CONFIG_PATH = f"{APP_DIR}/{CHIP_PROJECT_CONFIG_PATH}"
SYSBUILD_PATH = f"{APP_DIR}/{SYSBUILD_CONF}"
SYSBUILD_CMAKE_PATH = f"{APP_DIR}/{SYSBUILD_CMAKE}"
DETACHED_SIGNING_PATH = f"{APP_DIR}/{SYSBUILD_DIR}/{DETACHED_SIGNING_CMAKE}"
MCUBOOT_CONF_PATH = f"{APP_DIR}/{SYSBUILD_DIR}/{BOOTLOADER_IMAGE}.conf"
MCUBOOT_OVERLAY_PATH = f"{APP_DIR}/{SYSBUILD_DIR}/{BOOTLOADER_IMAGE}.overlay"


def _example_files() -> dict[str, str]:
    return generate(resolve_file(EXAMPLE), config_name=EXAMPLE.name)


# --------------------------------------------------------------------------
# The tree
# --------------------------------------------------------------------------


def test_the_tree_has_exactly_the_designed_artifacts() -> None:
    assert sorted(_example_files()) == sorted(
        [
            CMAKE_PATH,
            OVERLAY_PATH,
            CHIP_CONFIG_PATH,
            f"{APP_DIR}/prj.conf",
            SOURCE_PATH,
            HEADER_PATH,
            SYSBUILD_PATH,
            SYSBUILD_CMAKE_PATH,
            DETACHED_SIGNING_PATH,
            MCUBOOT_CONF_PATH,
            MCUBOOT_OVERLAY_PATH,
            MODEL_FILE,
        ]
    )


def test_write_tree_puts_them_on_disk(tmp_path) -> None:
    model = resolve_file(EXAMPLE)
    written = write_tree(model, out_dir=tmp_path, config_name=EXAMPLE.name)

    assert [str(path.relative_to(tmp_path)) for path in written] == sorted(_example_files())
    assert all(path.is_file() for path in written)
    assert (tmp_path / MODEL_FILE).read_text(encoding="utf-8") == model.to_json()


def test_board_file_stem_matches_zephyrs_spelling() -> None:
    assert board_file_stem("nrf7002dk/nrf5340/cpuapp") == "nrf7002dk_nrf5340_cpuapp"


# --------------------------------------------------------------------------
# Determinism (builder-pipeline.md §1.4)
# --------------------------------------------------------------------------


def test_generating_twice_gives_the_same_bytes() -> None:
    assert _example_files() == _example_files()


def test_a_model_that_round_tripped_through_json_generates_the_same_bytes() -> None:
    """A remote build server generates from a model off the wire (§6)."""
    model = resolve_file(EXAMPLE)
    from_wire = DeviceModel.from_json(model.to_json())
    assert generate(from_wire, config_name=EXAMPLE.name) == _example_files()


def test_nothing_generated_mentions_the_host() -> None:
    """No absolute paths, no timestamps: the output travels between machines."""
    for name, text in _example_files().items():
        assert str(REPO_ROOT) not in text, name
        assert "2026-" not in text, name


# --------------------------------------------------------------------------
# Golden files
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("artifact", "golden"),
    [
        (OVERLAY_PATH, "00-bmp180-two-endpoints.overlay"),
        (f"{APP_DIR}/prj.conf", "00-bmp180-two-endpoints.prj.conf"),
        (CMAKE_PATH, "00-bmp180-two-endpoints.CMakeLists.txt"),
        (CHIP_CONFIG_PATH, "00-bmp180-two-endpoints.CHIPProjectConfig.h"),
        (SYSBUILD_PATH, "00-bmp180-two-endpoints.sysbuild.conf"),
        (MCUBOOT_CONF_PATH, "00-bmp180-two-endpoints.mcuboot.conf"),
        (MCUBOOT_OVERLAY_PATH, "00-bmp180-two-endpoints.mcuboot.overlay"),
    ],
)
def test_artifact_matches_its_golden_file(artifact: str, golden: str) -> None:
    expected = (GOLDEN_DIR / golden).read_text(encoding="utf-8")
    assert _example_files()[artifact] == expected


@pytest.mark.parametrize("name", ["mcuhome_config.c", "mcuhome_config.h"])
def test_the_sample_is_this_generators_output(name: str) -> None:
    """ADR 0014: samples/matter-node is the codegen regression fixture."""
    artifact = f"{APP_DIR}/src/{name}"
    committed = (SAMPLE_SRC / name).read_text(encoding="utf-8")
    assert _example_files()[artifact] == committed, (
        f"samples/matter-node/src/{name} is stale — regenerate it, see tests_py/README.md"
    )


# --------------------------------------------------------------------------
# What the emitted C says
# --------------------------------------------------------------------------


def test_generated_c_includes_nothing_from_chip() -> None:
    """The plain-C tables contract of ADR 0014, asserted on the output."""
    for artifact in (SOURCE_PATH, HEADER_PATH):
        text = _example_files()[artifact]
        includes = [line for line in text.splitlines() if line.startswith("#include")]
        assert includes
        for line in includes:
            assert "chip" not in line.lower()
            assert "app-common" not in line
            assert "ember" not in line.lower()
        assert "emberAf" not in text


def test_generated_c_emits_one_node_symbol_and_no_framework_attributes() -> None:
    source = _example_files()[SOURCE_PATH]
    assert source.count("const struct mcuhome_matter_node mcuhome_node_config") == 1
    # Global attributes and the Descriptor cluster belong to the framework;
    # declaring them here would duplicate wire-visible entries.
    assert ".id = 0xfff" not in source.lower()
    assert ".id = 0x001d" not in source.lower()


def test_generated_c_carries_the_generic_contract_knowledge() -> None:
    source = _example_files()[SOURCE_PATH]
    assert "nullable per the Matter specification" in source
    assert "constant (store == NULL), -40.00 °C." in source
    assert "constant (store == NULL), 1100 hPa." in source
    assert "REVISION SOURCING." in source
    assert "0.10 °C in the attribute's raw units." in source


def test_generated_c_does_not_carry_device_specific_prose() -> None:
    """Datasheet and part prose stays in the YAML the user wrote.

    The device's own name is not prose — it identifies the file, which is
    why "BMP180 Two-Endpoint Node" legitimately appears in the header.
    """
    source = _example_files()[SOURCE_PATH]
    for phrase in ("datasheet", "die IS the reference", "BST-BMP180", "Operating range"):
        assert phrase not in source


def test_every_channel_publishes_into_a_cell_the_tables_point_at() -> None:
    source = _example_files()[SOURCE_PATH]
    model = resolve_file(EXAMPLE)
    for channel in model.channels:
        assert f"static struct mcuhome_attr_store {channel.store};" in source
        assert f".store = &{channel.store}," in source


def test_a_device_without_channels_declares_no_bindings() -> None:
    model = DeviceModel(
        device=DeviceMeta(
            name="quiet-node",
            friendly_name="Quiet Node",
            board="nrf7002dk/nrf5340/cpuapp",
            power_source="mains",
        ),
        network=NetworkModel(transport=None, matter_enabled=False),
        toolchain=ToolchainModel(zephyr_line="4.4", blob_usage="auto"),
        hardware=HardwareModel(),
    )
    files = generate(model, config_name="main.yaml")
    header = files[HEADER_PATH]
    assert "mcuhome_sensor_binding" not in header
    assert "mcuhome/channel.h" not in header
    assert "mcuhome_node_config" in header
    # An empty node still emits the one symbol the framework looks for —
    # as a null pointer, because an empty C array initializer is not C.
    source = files[SOURCE_PATH]
    assert "const struct mcuhome_matter_node mcuhome_node_config" in source
    assert ".endpoints = NULL,\n\t.endpoint_count = 0," in source
    assert "endpoints[] = {}" not in source


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("clang-format") is None, reason="clang-format is not installed")
def test_generated_c_is_clang_format_clean(tmp_path) -> None:
    """The editor hook never sees generator output, so it must be clean itself."""
    shutil.copy(REPO_ROOT / ".clang-format", tmp_path / ".clang-format")
    files = _example_files()
    paths = []
    for artifact in (SOURCE_PATH, HEADER_PATH):
        path = tmp_path / artifact.rsplit("/", 1)[-1]
        path.write_text(files[artifact], encoding="utf-8")
        paths.append(str(path))

    result = subprocess.run(
        ["clang-format", "--dry-run", "-Werror", *paths],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_generated_c_never_nests_a_block_comment() -> None:
    """GCC's -Wcomment fires on "/*" inside a comment; a build must be silent."""
    for artifact in (SOURCE_PATH, HEADER_PATH):
        text = _example_files()[artifact]
        depth = 0
        index = 0
        while index < len(text) - 1:
            pair = text[index : index + 2]
            if pair == "/*":
                assert depth == 0, f"{artifact}: nested block comment at offset {index}"
                depth, index = 1, index + 2
                continue
            if pair == "*/":
                assert depth == 1, f"{artifact}: stray comment terminator at offset {index}"
                depth, index = 0, index + 2
                continue
            index += 1
        assert depth == 0, f"{artifact}: unterminated block comment"


def test_a_comment_that_would_nest_is_refused() -> None:
    model = _hardware_model(HardwareModel())
    broken = DeviceModel(
        device=DeviceMeta(
            name="odd",
            friendly_name="ends the /* comment */ early",
            board=model.device.board,
            power_source="mains",
        ),
        network=model.network,
        toolchain=model.toolchain,
        hardware=HardwareModel(),
    )
    error = _generate_failure(broken)
    assert "which C cannot nest" in error.message


def test_no_generated_line_is_longer_than_the_column_limit() -> None:
    for name, text in _example_files().items():
        if name == MODEL_FILE:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if line.startswith("CONFIG_"):
                # A Kconfig assignment cannot be wrapped: the base64
                # SPAKE2+ verifier is 132 characters and has to be one
                # line or none.
                continue
            width = len(line.expandtabs(8))
            assert width <= 100, f"{name}:{number} is {width} columns wide"


def test_every_generated_file_is_reuse_compliant_and_says_it_is_generated() -> None:
    for name, text in _example_files().items():
        if name == MODEL_FILE:  # JSON has no comment syntax; REUSE.toml covers it
            continue
        # REUSE-IgnoreStart — asserting on the tags, not declaring them.
        assert "SPDX-FileCopyrightText: 2026 The MCUHome Contributors" in text, name
        assert "SPDX-License-Identifier: Apache-2.0" in text, name
        # REUSE-IgnoreEnd
        assert "Generated by mcuhome from 00-bmp180-two-endpoints.yaml — do not edit." in text


# --------------------------------------------------------------------------
# Error paths — a model can reach stage 4 without the validator (§6)
# --------------------------------------------------------------------------


def _hardware_model(hardware: HardwareModel) -> DeviceModel:
    return DeviceModel(
        device=DeviceMeta(
            name="bench-node",
            friendly_name="Bench Node",
            board="nrf7002dk/nrf5340/cpuapp",
            power_source="mains",
        ),
        network=NetworkModel(transport=None, matter_enabled=False),
        toolchain=ToolchainModel(zephyr_line="4.4", blob_usage="auto"),
        hardware=hardware,
    )


def _generate_failure(model: DeviceModel) -> GenerationError:
    with pytest.raises(GenerationError) as caught:
        generate(model, config_name="main.yaml")
    return caught.value


def test_a_bus_without_a_controller_is_refused() -> None:
    model = _hardware_model(
        HardwareModel(
            buses=[BusModel(id="i2c0", kind="i2c")],
            peripherals=[
                PeripheralModel(id="baro", compatible="bosch,bmp180", bus="i2c0", reg=0x77)
            ],
        )
    )
    error = _generate_failure(model)
    assert error.message == 'The bus "i2c0" does not say which bus of the board it is.'
    assert "controller: arduino_i2c" in (error.hint or "")


def test_a_peripheral_without_a_bus_is_refused() -> None:
    model = _hardware_model(
        HardwareModel(
            peripherals=[PeripheralModel(id="baro", compatible="bosch,bmp180", bus=None, reg=0x77)]
        )
    )
    error = _generate_failure(model)
    assert error.message == ('The peripheral "baro" is not on a bus this configuration describes.')


def test_a_peripheral_without_an_address_is_refused() -> None:
    model = _hardware_model(
        HardwareModel(
            buses=[BusModel(id="i2c0", kind="i2c", controller="arduino_i2c")],
            peripherals=[
                PeripheralModel(id="baro", compatible="bosch,bmp180", bus="i2c0", reg=None)
            ],
        )
    )
    error = _generate_failure(model)
    assert error.message == 'The peripheral "baro" has no bus address.'
    assert "address:" in (error.hint or "")


def test_a_property_the_overlay_cannot_express_is_refused() -> None:
    model = _hardware_model(
        HardwareModel(
            buses=[BusModel(id="i2c0", kind="i2c", controller="arduino_i2c")],
            peripherals=[
                PeripheralModel(
                    id="baro",
                    compatible="bosch,bmp180",
                    bus="i2c0",
                    reg=0x77,
                    properties={"osr-press": 1.5},
                )
            ],
        )
    )
    error = _generate_failure(model)
    assert error.message.startswith('The property "osr-press" of peripheral "baro"')
    assert "whole numbers, text and yes/no flags" in (error.hint or "")


def test_an_unwritable_build_directory_is_refused(tmp_path) -> None:
    blocked = tmp_path / "readonly"
    blocked.mkdir(mode=0o500)
    with pytest.raises(GenerationError) as caught:
        write_tree(resolve_file(EXAMPLE), out_dir=blocked / "out", config_name=EXAMPLE.name)
    assert "cannot be written" in caught.value.message
    assert caught.value.hint == "pick a writable location with --build-dir"


# --------------------------------------------------------------------------
# Devicetree and Kconfig rendering
# --------------------------------------------------------------------------


def test_the_overlay_carries_the_boards_own_wiring_before_the_peripherals() -> None:
    """Board wiring is the board's, not the configuration's (registry.BoardDef).

    Without the entropy redirect the nRF5340 application core has no
    random source at all — its RNG belongs to the network core — so a
    generated image would build and then fail every commissioning attempt.
    That is a property of the board, true of every device on it, which is
    why it lives next to the board's Kconfig instead of in each YAML.
    """
    overlay = _example_files()[OVERLAY_PATH]
    entropy = overlay.index('compatible = "mcuhome,entropy-ipc";')
    assert "zephyr,entropy = &netcore_entropy;" in overlay
    assert entropy < overlay.index("&arduino_i2c {")


def test_a_board_without_wiring_gets_no_empty_block() -> None:
    model = _hardware_model(HardwareModel())
    object.__setattr__(model.device, "board", "some/unknown/board")
    overlay = generate(model, config_name="main.yaml")[
        f"{APP_DIR}/boards/some_unknown_board.overlay"
    ]
    assert overlay.rstrip().endswith("*/")


def test_the_generated_header_names_the_device_for_the_generic_main() -> None:
    """The one thing the shared main.c cannot know without being told."""
    assert '#define MCUHOME_DEVICE_NAME "bmp180-node"' in _example_files()[HEADER_PATH]


def test_a_device_name_that_cannot_be_a_c_string_is_refused() -> None:
    model = _hardware_model(HardwareModel())
    object.__setattr__(model.device, "name", 'odd"name')
    error = _generate_failure(model)
    assert error.message.startswith('The device name "odd"name" cannot be written')


def test_the_overlay_extends_the_boards_own_bus_node() -> None:
    overlay = _example_files()[OVERLAY_PATH]
    assert "&arduino_i2c {" in overlay
    assert "baro: baro@77 {" in overlay
    assert 'compatible = "bosch,bmp180";' in overlay
    assert "reg = <0x77>;" in overlay
    assert "osr-press = <1>;" in overlay
    # Chip drivers are enabled by their node, never by a Kconfig symbol.
    assert "CONFIG_BMP180" not in _example_files()[f"{APP_DIR}/prj.conf"]


def test_the_overlay_can_render_flags_frequencies_and_text() -> None:
    model = _hardware_model(
        HardwareModel(
            buses=[BusModel(id="i2c0", kind="i2c", controller="arduino_i2c", frequency_hz=400_000)],
            peripherals=[
                PeripheralModel(
                    id="baro",
                    compatible="bosch,bmp180",
                    bus="i2c0",
                    reg=0x77,
                    properties={"a-flag": True, "off-flag": False, "a-name": "wide"},
                )
            ],
        )
    )
    overlay = generate(model, config_name="main.yaml")[OVERLAY_PATH]
    assert "clock-frequency = <400000>;" in overlay
    assert "\t\ta-flag;\n" in overlay
    assert "off-flag" not in overlay  # a false devicetree boolean is absence
    assert '\t\ta-name = "wide";\n' in overlay


def test_the_kconfig_fragment_is_the_models_list_plus_the_trees_own_paths() -> None:
    """Everything about the *device* comes from the model, in model order.

    Two blocks are appended rather than interleaved, and both are derived
    rather than carried: the commissioning identity, which
    :mod:`mcuhome.pairing` computes from the model's pairing tuple in one
    call, and ``CONFIG_CHIP_PROJECT_CONFIG``, which names a file inside
    the generated tree and is therefore stage 4's fact. A remote build
    server derives both from what it received rather than being told.
    """
    model = resolve_file(EXAMPLE)
    assert model.network.pairing is not None
    fragment = _example_files()[f"{APP_DIR}/prj.conf"]
    body = [line for line in fragment.splitlines() if line and not line.startswith("#")]
    assert body[: len(model.build.kconfig)] == model.build.kconfig
    assert body[len(model.build.kconfig) :] == [
        *pairing.kconfig_lines(
            pairing.Pairing(
                discriminator=model.network.pairing.discriminator,
                passcode=model.network.pairing.passcode,
                salt=model.network.pairing.salt,
                iterations=model.network.pairing.iterations,
            )
        ),
        f'CONFIG_CHIP_PROJECT_CONFIG="{CHIP_PROJECT_CONFIG_PATH}"',
    ]
    assert not any(line.startswith("CONFIG_CHIP_PROJECT_CONFIG") for line in model.build.kconfig)
    # The snippets the app has to be built with, the board's included.
    assert "matter, boot-mode" in fragment


def test_the_chip_project_config_wrapper_only_forwards_to_the_framework() -> None:
    """It is a wrapper because CHIP resolves the path app-relative — nothing more."""
    text = _example_files()[CHIP_CONFIG_PATH]
    body = [line for line in text.splitlines() if line and not line.startswith((" *", "/*"))]
    assert body == [
        "#pragma once",
        "#include <mcuhome/matter/chip_project_config.h>",
    ]


# --------------------------------------------------------------------------
# The application CMakeLists
# --------------------------------------------------------------------------

#: Blocks of the Matter build glue that both the generated application and
#: the hand-written sample have to carry, byte for byte. Each is a
#: mechanism that took hardware bring-up to find (see the comments in
#: samples/matter-node/CMakeLists.txt), which is exactly why neither file
#: is allowed to have its own version of it.
_SHARED_CHIP_GLUE = (
    "list(APPEND ZEPHYR_EXTRA_MODULES ${CHIP_ROOT}/config/zephyr/chip-module)",
    # The compiler-launcher hand-off into CHIP's GN sub-build. Raw string:
    # the `\n` are literal two-character escapes CMake expands, not
    # newlines — get that wrong and gn is handed one unparsable argument.
    r"""if(NOT USE_CCACHE STREQUAL "0")
    find_program(MCUHOME_CCACHE ccache)
    if(MCUHOME_CCACHE)
        set(MATTER_GN_ARGS "--arg-string\npw_command_launcher\nccache\n")
    endif()
endif()""",
    "include(${CHIP_ROOT}/src/app/chip_data_model.cmake)",
    "target_link_libraries(chip INTERFACE $<TARGET_FILE:kernel>)",
    "target_include_directories(app PRIVATE\n    ${CHIP_ROOT}/zzz_generated/app-common)",
    (
        "chip_configure_data_model(app\n"
        "    ZAP_FILE ${ZEPHYR_MCUHOME_MODULE_DIR}/components/matter/zap/mcuhome-root.zap\n"
        "    ZCL_PATH ${CHIP_ROOT}/src/app/zap-templates/zcl/zcl.json\n"
        ")"
    ),
)


@pytest.mark.parametrize("block", _SHARED_CHIP_GLUE)
def test_the_sample_and_the_generated_app_share_one_matter_build_glue(block: str) -> None:
    """ADR 0014 makes the sample a generated device; its build must match one."""
    sample = (SAMPLE_DIR / "CMakeLists.txt").read_text(encoding="utf-8")
    assert block in sample, "samples/matter-node/CMakeLists.txt drifted from the generator"
    assert block in _example_files()[CMAKE_PATH]


def test_the_generated_app_names_no_path_of_the_machine_that_wrote_it() -> None:
    """The CHIP SDK is found from ZEPHYR_BASE, so a build tree can be moved."""
    cmake = _example_files()[CMAKE_PATH]
    assert '"$ENV{ZEPHYR_BASE}/../modules/lib/connectedhomeip" REALPATH' in cmake
    assert str(REPO_ROOT) not in cmake


def test_a_device_without_matter_gets_no_chip_glue_at_all() -> None:
    files = generate(_hardware_model(HardwareModel()), config_name="main.yaml")
    assert CHIP_CONFIG_PATH not in files
    cmake = files[CMAKE_PATH]
    assert "CHIP" not in cmake
    assert "chip" not in cmake
    # No CHIP means no CHIP-generated C++ in the app target, so the project
    # can say what it is and keep the C++ toolchain out of the build.
    assert "project(bench-node LANGUAGES C)" in cmake
    assert "CONFIG_CHIP_PROJECT_CONFIG" not in files[f"{APP_DIR}/prj.conf"]


# --------------------------------------------------------------------------
# The sysbuild half (ADR 0015)
# --------------------------------------------------------------------------


def test_both_images_are_given_the_same_flash_map() -> None:
    """The bootloader writes the slots; the application is linked into one.

    Two files carry the table because sysbuild's per-image devicetree
    overlay *replaces* the image's own rather than adding to it. Two
    copies that could disagree would be a device that boots into the
    middle of nothing, so both come from the one string in the registry.
    """
    files = _example_files()
    layout = registry.BOARDS["nrf7002dk/nrf5340/cpuapp"].update_scheme.partition_overlay
    assert layout in files[OVERLAY_PATH]
    assert layout in files[MCUBOOT_OVERLAY_PATH]


def test_the_bootloader_overlay_restates_what_replacing_the_overlay_costs() -> None:
    """Two upstream fragments, and the reason they had to be copied."""
    overlay = _example_files()[MCUBOOT_OVERLAY_PATH]
    assert "zephyr,code-partition = &boot_partition;" in overlay
    assert 'compatible = "zephyr,cdc-acm-uart";' in overlay
    assert "REPLACES" in overlay


def test_the_sysbuild_config_names_the_bootloader_and_the_mode() -> None:
    conf = _example_files()[f"{APP_DIR}/{SYSBUILD_CONF}"]
    body = [line for line in conf.splitlines() if line and not line.startswith("#")]
    assert body == [
        "SB_CONFIG_BOOTLOADER_MCUBOOT=y",
        "SB_CONFIG_MCUBOOT_MODE_SWAP_USING_OFFSET=y",
        "SB_CONFIG_BOOT_SIGNATURE_TYPE_ECDSA_P256=y",
        "SB_CONFIG_MERGED_HEX_FILES=y",
    ]


def test_no_generated_file_carries_a_signing_key_or_a_path_to_one() -> None:
    """It is a per-user secret; the tree is copied, reviewed and shared.

    Silence would be worse than absence, though — MCUboot's default is
    its own published demo key — so the file that would have carried it
    says out loud what happens without the argument.
    """
    files = _example_files()
    for name, text in files.items():
        assert "-----BEGIN" not in text, name
        for line in text.splitlines():
            # Comment leaders stripped: the two files that *mention* the
            # option do so in prose, showing the argument to pass.
            assert not line.lstrip("#/* \t").startswith("SB_CONFIG_BOOT_SIGNATURE_KEY_FILE="), name
    conf = files[f"{APP_DIR}/{SYSBUILD_CONF}"]
    assert "SB_CONFIG_BOOT_SIGNATURE_KEY_FILE" in conf
    assert "demo key" in conf


def test_the_bootloader_fragment_states_the_sector_count_itself() -> None:
    conf = _example_files()[MCUBOOT_CONF_PATH]
    assert "CONFIG_BOOT_MAX_IMG_SECTORS_AUTO=n" in conf
    assert "CONFIG_BOOT_MAX_IMG_SECTORS=228" in conf


def test_the_bootloader_fragment_carries_its_own_lto() -> None:
    """ADR 0015 amendment (2026-08-07): LTO is strictly per-image.

    The symbols land in the bootloader's Kconfig fragment and nowhere
    else — the application's fragment comes from a wholly separate model
    field, but this asserts the negative directly rather than trusting
    that.
    """
    files = _example_files()
    conf = files[MCUBOOT_CONF_PATH]
    assert "CONFIG_ISR_TABLES_LOCAL_DECLARATION=y" in conf
    assert "CONFIG_LTO=y" in conf
    assert "CONFIG_LTO_SINGLE_THREADED=y" in conf
    prj_conf = files[f"{APP_DIR}/prj.conf"]
    assert "CONFIG_LTO" not in prj_conf


def test_the_bootloader_overlay_drops_the_dead_uart_and_the_app_keeps_it() -> None:
    """ADR 0015 amendment (2026-08-07): MCUboot's dead UART, not the app's.

    ``&uart0`` only ever appears where MCUBOOT_SERIAL's upstream
    imprecision (UPSTREAM-BUGS.md M2) puts dead weight — the bootloader
    image. The application overlay never names it.
    """
    files = _example_files()
    overlay = files[MCUBOOT_OVERLAY_PATH]
    assert '&uart0 {\n\tstatus = "disabled";\n};' in overlay
    assert "&uart0" not in files[OVERLAY_PATH]


def test_the_build_command_in_the_cmakelists_names_the_snippets_per_image() -> None:
    cmake = _example_files()[CMAKE_PATH]
    assert "--sysbuild" in cmake
    assert f'-D{APP_DIR}_SNIPPET="matter;boot-mode"' in cmake
    assert f'-D{BOOTLOADER_IMAGE}_SNIPPET="boot-mode"' in cmake


def test_a_board_without_an_update_scheme_gets_no_sysbuild_files() -> None:
    """A fixture board, and the shape a not-yet-brought-up board has."""
    model = _hardware_model(HardwareModel())
    board = registry.BOARDS[model.device.board]
    files = generate(model, config_name="main.yaml")
    assert f"{APP_DIR}/{SYSBUILD_CONF}" in files  # the real board has one

    plain = dataclasses.replace(board, update_scheme=None)
    with_plain_board = dict(registry.BOARDS, **{model.device.board: plain})
    original = registry.BOARDS.copy()
    registry.BOARDS.clear()
    registry.BOARDS.update(with_plain_board)
    try:
        files = generate(model, config_name="main.yaml")
    finally:
        registry.BOARDS.clear()
        registry.BOARDS.update(original)
    assert not [name for name in files if "sysbuild" in name]
    assert "Flash layout" not in files[OVERLAY_PATH]


def test_an_unchanged_file_is_not_rewritten(tmp_path) -> None:
    """CMake watches these files; touching one re-runs the Matter build."""
    model = resolve_file(EXAMPLE)
    write_tree(model, out_dir=tmp_path, config_name=EXAMPLE.name)
    cmake = tmp_path / CMAKE_PATH
    stamp = cmake.stat().st_mtime_ns
    os.utime(cmake, ns=(stamp - 10**9, stamp - 10**9))
    before = cmake.stat().st_mtime_ns

    write_tree(model, out_dir=tmp_path, config_name=EXAMPLE.name)
    assert cmake.stat().st_mtime_ns == before


def test_a_changed_file_is_rewritten(tmp_path) -> None:
    model = resolve_file(EXAMPLE)
    write_tree(model, out_dir=tmp_path, config_name=EXAMPLE.name)
    cmake = tmp_path / CMAKE_PATH
    cmake.write_text("stale\n", encoding="utf-8")

    write_tree(model, out_dir=tmp_path, config_name=EXAMPLE.name)
    assert cmake.read_text(encoding="utf-8") == _example_files()[CMAKE_PATH]
