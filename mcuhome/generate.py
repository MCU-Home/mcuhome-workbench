# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Pipeline stage 4: the per-device build tree (builder-pipeline.md §3).

Everything here reads the canonical model and nothing else — no YAML, no
filesystem lookups, no environment. That is what makes the stage
reproducible by construction (§1.4) and golden-file testable byte for byte
(§9), and it is also what lets a remote build server generate from a model
that arrived over the wire (§6).

The layout the pipeline design fixes::

    <build dir>/
    ├── device-model.json          the canonical model, for inspection
    └── app/                       a standalone Zephyr application
        ├── CMakeLists.txt
        ├── prj.conf
        ├── boards/<board>.overlay
        ├── include/
        │   └── CHIPProjectConfig.h    (Matter devices only)
        └── src/
            ├── mcuhome_config.c
            └── mcuhome_config.h

"Standalone" is meant literally: ``west build -b <board> -S <snippets>
<build dir>/app`` from a west workspace produces the same image
``mcuhome build`` does. That is why the CMakeLists carries the whole
Matter build glue rather than a call into something the builder owns —
and why the *only* thing it needs from the MCUHome module is the module
directory Zephyr hands it, never a path this generator wrote down.

**Thin codegen.** The C artifacts are *data*: struct initializers for the
two runtime contracts, ``<mcuhome/matter_tables.h>`` (ADR 0014) and
``<mcuhome/channel.h>``. No control flow, no CHIP include, no logic — all
behavior lives in the framework, which interprets the tables. A generator
that emitted logic would be untestable and undiffable, which is exactly
the ESPHome property MCUHome does not want (§1.1).

**Comments are generated too, and only from generic knowledge.** Every
comment below is either a fact of the contract (the ``store == NULL``
convention, the revision-sourcing rule, why MeasuredValue is nullable) or
a rendering of a model value (``-40.00 °C`` for a raw ``-4000``). Prose
about a specific chip belongs in the YAML the user wrote, not in the file
the builder overwrites.

**Determinism.** No timestamps, no absolute paths, no version numbers, no
dictionary iteration order: the same model produces byte-identical files
forever. The SPDX header names the source configuration by file name
only, for exactly that reason.

**clang-format.** The emitters produce Zephyr-style C (tabs, 100 columns,
``.clang-format`` at the repository root) *as written*. The repository's
editor hook does not see generator output, so the output has to be clean
on its own; ``tests_py/test_generate.py`` checks that with clang-format
when it is installed.
"""

from __future__ import annotations

import textwrap
from fractions import Fraction
from pathlib import Path

from mcuhome import registry
from mcuhome.errors import GenerationError
from mcuhome.model import (
    AttrModel,
    ChannelModel,
    ClusterModel,
    DeviceModel,
    EndpointModel,
    PeripheralModel,
)

__all__ = [
    "APP_DIR",
    "CHIP_PROJECT_CONFIG_PATH",
    "CONFIG_BASENAME",
    "MODEL_FILE",
    "board_file_stem",
    "generate",
    "write_tree",
]

#: Sub-directory of the build tree holding the standalone Zephyr app.
APP_DIR = "app"
#: The canonical model, written next to the app for inspection (§1.3).
MODEL_FILE = "device-model.json"
#: Stem of the two generated C files, in ``<app>/src/``.
CONFIG_BASENAME = "mcuhome_config"
#: Where the CHIP project-configuration wrapper goes, relative to the
#: application directory. CHIP resolves ``CONFIG_CHIP_PROJECT_CONFIG``
#: against the application source directory, so this path is both the
#: file's location and the value of that Kconfig symbol
#: (:mod:`mcuhome.resolve` emits it).
CHIP_PROJECT_CONFIG_PATH = "include/CHIPProjectConfig.h"

#: Column limit of the repository's ``.clang-format``.
_COLUMNS = 100
#: ``IndentWidth``/``TabWidth`` of the same file.
_TAB = 8

#: Order the ``MCUHOME_ATTR_F_*`` flags are OR-ed in, so a flag set has
#: exactly one spelling. Mirrors the order they are defined in
#: ``<mcuhome/matter_tables.h>``.
_FLAG_ORDER = ("writable", "nullable")


# --------------------------------------------------------------------------
# Small rendering helpers
# --------------------------------------------------------------------------


def board_file_stem(board: str) -> str:
    """``nrf7002dk/nrf5340/cpuapp`` -> ``nrf7002dk_nrf5340_cpuapp``.

    The spelling Zephyr looks for under an application's ``boards/``.
    """
    return board.replace("/", "_")


def _file_header(kind: str, config_name: str, paragraphs: list[str]) -> str:
    """The SPDX + "generated, do not edit" block every artifact starts with.

    *kind* is ``"c"`` (``/* … */``) or ``"hash"`` (``# …``). The header
    carries no version and no timestamp: two runs of the same builder on
    the same configuration must produce the same bytes. Returned without
    a trailing newline, like every other block the emitters join.
    """
    lead = f"Generated by mcuhome from {config_name} — do not edit."
    body = [lead, *paragraphs]

    # REUSE-IgnoreStart — the tags below are the ones the generated files
    # get; without this marker `reuse lint` reads them as a second license
    # declaration of this Python module and rejects the trailing quote.
    if kind == "hash":
        lines = [
            "# SPDX-FileCopyrightText: 2026 The MCUHome Contributors",
            "# SPDX-License-Identifier: Apache-2.0",
            "#",
        ]
        for index, paragraph in enumerate(body):
            if index:
                lines.append("#")
            lines += [f"# {line}".rstrip() for line in _wrap(paragraph, _COLUMNS - 2)]
        return "\n".join(lines)

    lines = [
        "/*",
        " * SPDX-FileCopyrightText: 2026 The MCUHome Contributors",
        " * SPDX-License-Identifier: Apache-2.0",
        " *",
    ]
    # REUSE-IgnoreEnd
    for index, paragraph in enumerate(body):
        if index:
            lines.append(" *")
        wrapped = _wrap(_check_comment_safe(paragraph), _COLUMNS - 3)
        lines += [f" * {line}".rstrip() for line in wrapped]
    lines.append(" */")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    """Wrap one paragraph, honoring hard line breaks the caller put in.

    ``break_on_hyphens`` is off because most hyphens in these comments sit
    inside a path or an identifier (``*-cluster.xml``, ``min_measured``),
    and splitting one across two lines makes it unsearchable.
    """
    out: list[str] = []
    for chunk in text.split("\n"):
        out += textwrap.wrap(chunk, width=width, break_on_hyphens=False) or [""]
    return out


def _check_comment_safe(text: str) -> str:
    """Reject text that would nest or close a C block comment.

    GCC warns on ``/*`` inside a comment (-Wcomment) and ``*/`` would end
    the comment early — either way the generated file stops being clean.
    Comment text comes from this module and from :mod:`mcuhome.registry`,
    so hitting this is a builder bug, and a loud one beats a warning
    nobody reads in a 900-target build log.
    """
    for marker in ("/*", "*/"):
        if marker in text:
            raise GenerationError(
                f'A generated comment would contain "{marker}", which C cannot nest.',
                hint=(
                    "this is a builder bug — the offending text is: "
                    + (text[:120] + "…" if len(text) > 120 else text)
                ),
            )
    return text


def _comment(text: str, tabs: int) -> list[str]:
    """A C block comment indented by *tabs*, wrapped to the column limit."""
    indent = "\t" * tabs
    width = _COLUMNS - tabs * _TAB - 3
    lines = _wrap(_check_comment_safe(text), width)
    if len(lines) == 1:
        return [f"{indent}/* {lines[0]} */"]
    body = [f"{indent}/* {lines[0]}"] + [f"{indent} * {line}" for line in lines[1:]]
    body[-1] = f"{body[-1]} */"
    return body


def _banner(title: str) -> str:
    """``/* --- title ------------------------------------------------ */``."""
    prefix = f"/* --- {title} "
    return prefix + "-" * max(1, 76 - len(prefix) - 3) + " */"


def _cluster_def(cluster_id: int) -> registry.ClusterDef | None:
    for definition in registry.CLUSTERS.values():
        if definition.id == cluster_id:
            return definition
    return None


def _decimals(raw_per_unit: Fraction) -> int:
    """Digits after the point needed to show one raw step of a unit."""
    if raw_per_unit.denominator != 1:
        return 0
    digits = 0
    value = raw_per_unit.numerator
    while value > 1 and value % 10 == 0:
        value //= 10
        digits += 1
    return digits if value == 1 else 0


def _render_quantity(raw: int, cluster_id: int) -> str | None:
    """``-4000`` in cluster 0x0402 -> ``-40.00 °C``.

    Returns None for a cluster the registry does not know, which cannot
    happen for a resolved model but keeps the emitter total.
    """
    definition = _cluster_def(cluster_id)
    if definition is None:  # pragma: no cover - every model cluster is known
        return None
    value = Fraction(raw) / definition.raw_per_unit
    return f"{float(value):.{_decimals(definition.raw_per_unit)}f} {definition.unit}"


def _attr_type(name: str) -> str:
    if name not in registry.ATTR_SIZES:
        raise GenerationError(
            f'The attribute type "{name}" has no representation in the generated tables.',
            hint=(
                "the tables contract knows "
                + ", ".join(sorted(registry.ATTR_SIZES))
                + " — this is a builder bug, please report the configuration"
            ),
        )
    return f"MCUHOME_ATTR_TYPE_{name.upper()}"


def _attr_flags(flags: list[str]) -> str:
    known = [flag for flag in _FLAG_ORDER if flag in flags]
    unknown = sorted(set(flags) - set(_FLAG_ORDER))
    if unknown:
        raise GenerationError(
            f'The attribute flag "{unknown[0]}" has no representation in the generated tables.',
            hint=(
                "the tables contract knows the flags "
                + ", ".join(_FLAG_ORDER)
                + " — this is a builder bug, please report the configuration"
            ),
        )
    return " | ".join(f"MCUHOME_ATTR_F_{flag.upper()}" for flag in known) or "0"


# --------------------------------------------------------------------------
# mcuhome_config.h
# --------------------------------------------------------------------------

_HEADER_INTRO = [
    "Declarations of this device's generated configuration. The application "
    "glue includes this header and hands the symbols below to the framework; "
    "it never defines them itself.",
]


def _c_string(text: str) -> str:
    """Render *text* as a C string literal, or refuse if it cannot be one.

    Only values that need no escaping at all are accepted. Everything that
    reaches here has passed the schema, whose identifiers are plain text;
    a value that still needs escaping means the schema changed and the
    escaping rules were not thought through, which is worth a loud stop.
    """
    bad = [character for character in '"\\\n\t' if character in text]
    if bad:
        raise GenerationError(
            f'The device name "{text}" cannot be written into the generated C header.',
            hint=(
                "device names are plain text — quotes, backslashes and line breaks "
                "have no place in one"
            ),
        )
    return f'"{text}"'


def render_config_header(model: DeviceModel, *, config_name: str) -> str:
    """``mcuhome_config.h`` — what the application glue may refer to."""
    has_channels = bool(model.channels)

    out = [_file_header("c", config_name, _HEADER_INTRO), ""]
    out += [
        "#ifndef MCUHOME_CONFIG_H_",
        "#define MCUHOME_CONFIG_H_",
        "",
    ]
    if has_channels:
        # size_t for the binding count; <mcuhome/channel.h> for the binding
        # struct, which unlike the tables contract does depend on Zephyr.
        out += ["#include <stddef.h>", "", "#include <mcuhome/channel.h>"]
    out += [
        "#include <mcuhome/matter_tables.h>",
        "",
        "#ifdef __cplusplus",
        'extern "C" {',
        "#endif",
        "",
        "/** This device's name, as its configuration spells it. */",
        f"#define MCUHOME_DEVICE_NAME {_c_string(model.device.name)}",
        "",
        "/** This device's Matter data model: endpoints, clusters, attributes. */",
        "extern const struct mcuhome_matter_node mcuhome_node_config;",
    ]
    if has_channels:
        out += [
            "",
            "/** Sensor readings bound to the attribute store cells of the model above. */",
            "extern const struct mcuhome_sensor_binding mcuhome_sensor_bindings[];",
            "/** Number of entries in mcuhome_sensor_bindings. */",
            "extern const size_t mcuhome_sensor_binding_count;",
        ]
    out += [
        "",
        "#ifdef __cplusplus",
        "}",
        "#endif",
        "",
        "#endif /* MCUHOME_CONFIG_H_ */",
    ]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# mcuhome_config.c
# --------------------------------------------------------------------------

#: The generic, contract-level knowledge the generated tables carry. Every
#: paragraph is true of *every* generated device — nothing here depends on
#: the configuration being compiled.
_TABLES_INTRO = [
    "The Matter data model of this device as plain-C tables (ADR 0014, "
    "<mcuhome/matter_tables.h>), plus the sensor bindings that feed them "
    "(<mcuhome/channel.h>). Dumb, reviewable, diffable data: one node "
    "symbol, zero CHIP includes, zero logic. All behavior lives in the "
    "framework, which interprets what is below.",
    "The Descriptor cluster and the global attributes FeatureMap (0xFFFC) "
    "and ClusterRevision (0xFFFD) are deliberately absent: the framework "
    "appends the first and serves the other two from the cluster fields "
    "below.",
    "An attribute with store == NULL is a constant: the framework always "
    "serves its def value and a controller can never write it. That is how "
    "fixed cluster metadata such as MinMeasuredValue / MaxMeasuredValue is "
    "expressed without spending RAM on it. Their values state the sensor's "
    "own operating range, which a controller uses to size gauges and to "
    "sanity-check readings.",
    "REVISION SOURCING. Every cluster and device-type revision below comes "
    "from CHIP's own implementation data model — cluster revisions from the "
    "per-cluster XML under src/app/zap-templates/zcl/data-model/chip, "
    "device-type revisions from the matter-devices.xml next to it — because "
    "that is what CHIP-based nodes report in the field. The specification "
    "scrape shipped alongside it runs ahead of those numbers; they are what "
    "a future SDK release grows into, not what its code implements today.",
]


def render_config_source(model: DeviceModel, *, config_name: str) -> str:
    """``mcuhome_config.c`` — the whole device configuration as data."""
    device = model.device
    intro = [
        f'Device "{device.name}" ({device.friendly_name}) for {device.board}.',
        *_TABLES_INTRO,
    ]
    out = [_file_header("c", config_name, intro), ""]

    out.append("#include <zephyr/sys/util.h>")
    out.append("")
    if model.channels:
        out.append("#include <mcuhome/channel.h>")
    out.append("#include <mcuhome/matter_tables.h>")
    out += ["", f'#include "{CONFIG_BASENAME}.h"']

    stores = [
        attr.store
        for endpoint in model.endpoints
        for cluster in endpoint.clusters
        for attr in cluster.attrs
        if attr.store is not None
    ]
    if stores:
        out.append("")
        out += _comment(
            "RAM cells behind the value-carrying attributes. A cell belongs to "
            "whoever produces the value — the channel layer publishes into it, "
            "the tables below only point at it.",
            0,
        )
        out += [f"static struct mcuhome_attr_store {name};" for name in stores]

    for endpoint in model.endpoints:
        out += _render_endpoint(endpoint)

    out += ["", _banner("the node")]
    if model.endpoints:
        out.append("")
        out.append("static const struct mcuhome_matter_endpoint endpoints[] = {")
        for endpoint in model.endpoints:
            parent = (
                "directly under the root node"
                if endpoint.parent_id == 0
                else f"under endpoint {endpoint.parent_id}"
            )
            out += [
                "\t{",
                f"\t\t.endpoint_id = {endpoint.id},",
                f"\t\t.parent_id = {endpoint.parent_id}, /* {parent} */",
                f"\t\t.device_types = {_ep(endpoint)}_device_types,",
                f"\t\t.device_type_count = ARRAY_SIZE({_ep(endpoint)}_device_types),",
                f"\t\t.clusters = {_ep(endpoint)}_clusters,",
                f"\t\t.cluster_count = ARRAY_SIZE({_ep(endpoint)}_clusters),",
                "\t},",
            ]
        out.append("};")
        endpoints = ["\t.endpoints = endpoints,", "\t.endpoint_count = ARRAY_SIZE(endpoints),"]
    else:
        # A node with no application endpoints is still a valid node: the
        # root endpoint is the framework's and always there. An empty array
        # initializer is not valid C, so the pointer says "none" instead.
        endpoints = ["\t.endpoints = NULL,", "\t.endpoint_count = 0,"]
    out += [
        "",
        "const struct mcuhome_matter_node mcuhome_node_config = {",
        "\t.tables_version = MCUHOME_MATTER_TABLES_VERSION,",
        *endpoints,
        "};",
    ]

    if model.channels:
        out += _render_channels(model)

    return "\n".join(out) + "\n"


def _ep(endpoint: EndpointModel) -> str:
    return f"ep{endpoint.id}"


def _endpoint_title(endpoint: EndpointModel) -> str:
    types = ", ".join(item.name for item in endpoint.device_types)
    alias = f' "{endpoint.alias}"' if endpoint.alias else ""
    return f"endpoint {endpoint.id}{alias}: {types}"


def _render_endpoint(endpoint: EndpointModel) -> list[str]:
    out = ["", _banner(_endpoint_title(endpoint))]

    for cluster in endpoint.clusters:
        out.append("")
        out.append(
            f"static const struct mcuhome_matter_attr {_attrs_symbol(endpoint, cluster)}[] = {{"
        )
        for attr in cluster.attrs:
            out += _render_attr(attr, cluster)
        out.append("};")

    out.append("")
    out.append(f"static const struct mcuhome_matter_cluster {_ep(endpoint)}_clusters[] = {{")
    for cluster in endpoint.clusters:
        symbol = _attrs_symbol(endpoint, cluster)
        out += [
            "\t{",
            f"\t\t.id = {cluster.id:#06x}, /* {cluster.name} */",
            f"\t\t.feature_map = {cluster.feature_map:#010x},",
        ]
        out += _comment(
            f"ClusterRevision {cluster.cluster_revision} — see the "
            f"revision-sourcing note at the top of this file.",
            2,
        )
        out += [
            f"\t\t.cluster_revision = {cluster.cluster_revision},",
            f"\t\t.attrs = {symbol},",
            f"\t\t.attr_count = ARRAY_SIZE({symbol}),",
            "\t},",
        ]
    out.append("};")

    out.append("")
    out.append(
        f"static const struct mcuhome_matter_device_type {_ep(endpoint)}_device_types[] = {{"
    )
    for device_type in endpoint.device_types:
        out += [
            "\t{",
            f"\t\t.id = {device_type.id:#06x}, /* {device_type.name} */",
            f"\t\t.revision = {device_type.revision},",
            "\t},",
        ]
    out.append("};")
    return out


def _attrs_symbol(endpoint: EndpointModel, cluster: ClusterModel) -> str:
    return f"{_ep(endpoint)}_{cluster.name}_attrs"


def _render_attr(attr: AttrModel, cluster: ClusterModel) -> list[str]:
    rendered = _render_quantity(attr.default, cluster.id)
    if attr.store is None:
        note = f"{attr.name}: constant (store == NULL)"
        note = f"{note}, {rendered}." if rendered else f"{note}."
    elif "nullable" in attr.flags:
        plausible = f"a plausible {rendered}" if rendered else "a plausible zero"
        note = (
            f"{attr.name}: nullable per the Matter specification — until a value "
            f"has been published this attribute reports null, not {plausible}."
        )
    else:
        note = f"{attr.name}: served from a RAM cell the channel layer publishes into."

    return [
        "\t{",
        *_comment(note, 2),
        f"\t\t.id = {attr.id:#06x},",
        f"\t\t.type = {_attr_type(attr.type)},",
        f"\t\t.size = {attr.size},",
        f"\t\t.flags = {_attr_flags(attr.flags)},",
        f"\t\t.store = {'&' + attr.store if attr.store else 'NULL'},",
        f"\t\t.def = {attr.default},",
        "\t},",
    ]


def _channel_symbol(channel: ChannelModel) -> str:
    return f"ep{channel.endpoint_id}_{_cluster_name(channel.cluster_id)}_channel"


def _cluster_name(cluster_id: int) -> str:
    definition = _cluster_def(cluster_id)
    return definition.name if definition else f"cluster_{cluster_id:04x}"


def _render_channels(model: DeviceModel) -> list[str]:
    out = ["", _banner("channels: sensor readings bound to attributes")]
    out.append("")
    out += _comment(
        "Everything in a channel is in the attribute's own raw unit, never the "
        "sensor's; the binding below holds the integer scale between the two. "
        "A converted sample is published when it differs from the last "
        "published one by at least report_delta.",
        0,
    )

    for channel in model.channels:
        out.append("")
        out.append(f"static const struct mcuhome_channel {_channel_symbol(channel)} = {{")
        out += [
            f"\t.store = &{channel.store},",
            f"\t.type = {_attr_type(channel.type)},",
            f"\t.endpoint_id = {channel.endpoint_id},",
            f"\t.cluster_id = {channel.cluster_id:#06x},",
            f"\t.attr_id = {channel.attr_id:#06x},",
            f"\t.sample_period_ms = {channel.sample_period_ms},",
        ]
        if not channel.report_delta:
            out += _comment("No threshold: every sample is published.", 1)
        else:
            rendered = _render_quantity(channel.report_delta, channel.cluster_id)
            if rendered:
                out += _comment(f"{rendered} in the attribute's raw units.", 1)
        out += [
            f"\t.report_delta = {channel.report_delta},",
            "};",
        ]

    out.append("")
    out += _comment(
        "Bindings that share a device and fall due in the same cycle fetch once, "
        "not once each — which is what makes a multi-quantity chip cost one "
        "conversion per cycle instead of one per reading.",
        0,
    )
    out.append("const struct mcuhome_sensor_binding mcuhome_sensor_bindings[] = {")
    for channel in model.channels:
        source = channel.source
        out += [
            "\t{",
            f"\t\t.dev = DEVICE_DT_GET(DT_NODELABEL({source.peripheral})),",
            f"\t\t.fetch_channel = {source.fetch_channel},",
            f"\t\t.zephyr_channel = {source.zephyr_channel},",
        ]
        out += _comment("Zephyr's sensor unit -> the attribute's raw unit.", 2)
        out += [
            f"\t\t.scale_num = {source.scale_num},",
            f"\t\t.scale_den = {source.scale_den},",
            f"\t\t.offset = {source.offset},",
            f"\t\t.channel = &{_channel_symbol(channel)},",
            "\t},",
        ]
    out.append("};")
    out.append("")
    out.append("const size_t mcuhome_sensor_binding_count = ARRAY_SIZE(mcuhome_sensor_bindings);")
    return out


# --------------------------------------------------------------------------
# Devicetree overlay
# --------------------------------------------------------------------------

_OVERLAY_INTRO = [
    "Devicetree description of this device's peripherals. Hardware belongs "
    "in devicetree, never in C constants — which is also why a Zephyr sensor "
    "driver is enabled by its node here and not by a Kconfig symbol in the "
    "fragment next to this file.",
    "The first block, if there is one, is board wiring rather than device "
    "configuration: what every MCUHome node on this board needs, whatever "
    "its YAML says.",
]


def render_overlay(model: DeviceModel, *, config_name: str) -> str:
    """The board overlay: board wiring, then one node per peripheral."""
    intro = [f"Board: {model.device.board}.", *_OVERLAY_INTRO]
    out = [_file_header("c", config_name, intro)]

    board = registry.BOARDS.get(model.device.board)
    if board is not None and board.overlay:
        out.append("")
        out += _comment(board.overlay_note, 0)
        out.append(board.overlay)

    bus_ids = {bus.id for bus in model.hardware.buses}
    for peripheral in model.hardware.peripherals:
        if peripheral.bus is None or peripheral.bus not in bus_ids:
            raise GenerationError(
                f'The peripheral "{peripheral.id}" is not on a bus this configuration describes.',
                hint=(
                    "MCUHome can only place peripherals on a bus from "
                    "hardware.buses today; give the peripheral a bus: key"
                ),
            )

    for bus in model.hardware.buses:
        attached = [item for item in model.hardware.peripherals if item.bus == bus.id]
        if not attached:
            continue
        if bus.controller is None:
            raise GenerationError(
                f'The bus "{bus.id}" does not say which bus of the board it is.',
                hint=(
                    "name the board's bus node so the overlay can extend it:\n"
                    f"    {bus.id}:\n      controller: arduino_i2c"
                ),
            )
        out.append("")
        out.append(f"&{bus.controller} {{")
        out.append('\tstatus = "okay";')
        if bus.frequency_hz is not None:
            out.append(f"\tclock-frequency = <{bus.frequency_hz}>;")
        for peripheral in attached:
            out.append("")
            out += _render_peripheral(peripheral)
        out.append("};")

    return "\n".join(out) + "\n"


def _render_peripheral(peripheral: PeripheralModel) -> list[str]:
    if peripheral.reg is None:
        raise GenerationError(
            f'The peripheral "{peripheral.id}" has no bus address.',
            hint=(
                "add address: <the chip's address on the bus> — the builder only "
                "knows the fixed address of chips that have exactly one"
            ),
        )
    out = [
        f"\t{peripheral.id}: {peripheral.id}@{peripheral.reg:x} {{",
        f'\t\tcompatible = "{peripheral.compatible}";',
        f"\t\treg = <{peripheral.reg:#04x}>;",
    ]
    for name, value in sorted(peripheral.properties.items()):
        out += _render_property(peripheral, name, value)
    out.append('\t\tstatus = "okay";')
    out.append("\t};")
    return out


def _render_property(peripheral: PeripheralModel, name: str, value: object) -> list[str]:
    # bool first: in Python a bool is an int, and in devicetree a true
    # boolean property is its bare presence, never a value.
    if isinstance(value, bool):
        return [f"\t\t{name};"] if value else []
    if isinstance(value, int):
        return [f"\t\t{name} = <{value}>;"]
    if isinstance(value, str):
        return [f'\t\t{name} = "{value}";']
    raise GenerationError(
        f'The property "{name}" of peripheral "{peripheral.id}" cannot be written '
        f"to a devicetree overlay.",
        hint=(
            "devicetree properties the builder can emit are whole numbers, text "
            "and yes/no flags — this is a builder bug, please report the configuration"
        ),
    )


# --------------------------------------------------------------------------
# prj.conf fragment
# --------------------------------------------------------------------------


def render_prj_conf(model: DeviceModel, *, config_name: str) -> str:
    """The Kconfig fragment: model.build.kconfig, one symbol per line."""
    intro = [
        f'Kconfig fragment for device "{model.device.name}". Every symbol below '
        f"follows from the configuration; board-level and snippet-level settings "
        f"are not repeated here.",
    ]
    if model.build.snippets:
        intro.append(
            "This application needs the snippets it was generated for:\n"
            + "  "
            + " ".join(f"-S {snippet}" for snippet in model.build.snippets)
        )
    out = [_file_header("hash", config_name, intro), ""]
    out += list(model.build.kconfig)
    if model.network.matter_enabled:
        # Not part of the device model on purpose: this symbol names a file
        # inside the generated tree, so it is a fact of stage 4's layout
        # rather than of the device. CHIP resolves the path against the
        # application source directory.
        out += [
            "",
            "# Framework-owned CHIP settings, reached through the wrapper stage 4",
            f"# writes next to this file ({CHIP_PROJECT_CONFIG_PATH}).",
            f'CONFIG_CHIP_PROJECT_CONFIG="{CHIP_PROJECT_CONFIG_PATH}"',
        ]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# CMakeLists.txt
# --------------------------------------------------------------------------

#: The Matter (CHIP) build glue, verbatim, exactly as
#: ``samples/matter-node/CMakeLists.txt`` carries it. Two files state these
#: mechanics — the hand-written sample and every generated application —
#: and ``tests_py/test_generate.py`` asserts they stay identical, because a
#: fix found on the bench must not live in only one of them.
#:
#: Split in two because everything up to and including the CHIP module
#: registration has to run *before* ``find_package(Zephyr)``, and the rest
#: only works after it.
_CHIP_PRELUDE = """\
# Matter (CHIP) is a Zephyr module the application registers, not one west
# discovers: upstream connectedhomeip has no zephyr/module.yml at its root,
# only the chip-module subdirectory below. It therefore has to be appended
# to ZEPHYR_EXTRA_MODULES before find_package(Zephyr).
#
# CHIP_ROOT is searched for rather than written out, so this file names no
# path belonging to the machine that generated it and keeps working
# wherever the build tree is moved. What it looks for is the west T2
# workspace layout MCUHome pins (west.yml). Three ways, in order:
#
#   1. -DCHIP_ROOT=... or the CHIP_ROOT environment variable — say it.
#   2. next to ZEPHYR_BASE, when the environment has it. `west build` does
#      NOT export ZEPHYR_BASE, so this is the path for environments that
#      set it deliberately (the MCUHome builder is one).
#   3. upwards from this file, for the normal case of a build directory
#      inside the workspace it is built in.
if(NOT DEFINED CHIP_ROOT)
    if(DEFINED ENV{CHIP_ROOT})
        set(CHIP_ROOT $ENV{CHIP_ROOT})
    elseif(DEFINED ENV{ZEPHYR_BASE})
        get_filename_component(CHIP_ROOT
            "$ENV{ZEPHYR_BASE}/../modules/lib/connectedhomeip" REALPATH)
    else()
        set(_mcuhome_search ${CMAKE_CURRENT_SOURCE_DIR})
        while(NOT DEFINED CHIP_ROOT)
            if(EXISTS "${_mcuhome_search}/modules/lib/connectedhomeip")
                set(CHIP_ROOT "${_mcuhome_search}/modules/lib/connectedhomeip")
            endif()
            get_filename_component(_parent ${_mcuhome_search} DIRECTORY)
            if(_parent STREQUAL _mcuhome_search)
                break()
            endif()
            set(_mcuhome_search ${_parent})
        endwhile()
        unset(_mcuhome_search)
        unset(_parent)
    endif()
endif()

if(NOT EXISTS "${CHIP_ROOT}/config/zephyr/chip-module")
    message(FATAL_ERROR
        "The Matter SDK (connectedhomeip) was not found.\\n"
        "Looked for a west workspace containing modules/lib/connectedhomeip, "
        "starting at ${CMAKE_CURRENT_SOURCE_DIR}; CHIP_ROOT resolved to "
        "'${CHIP_ROOT}'.\\n"
        "The SDK is in the optional west group `matter`, so a workspace that "
        "was never updated with it has no copy:\\n"
        "  west config manifest.group-filter +matter && west update\\n"
        "If it lives somewhere else entirely, say so: -DCHIP_ROOT=<path>")
endif()

list(APPEND ZEPHYR_EXTRA_MODULES ${CHIP_ROOT}/config/zephyr/chip-module)"""

_CHIP_GLUE = """\
include(${CHIP_ROOT}/src/app/chip_data_model.cmake)

# Link-order fix: the Matter libraries form their own --start/end-group,
# placed after libkernel.a in the final link line, so k_msgq_* references
# from libCHIP.a stay unresolved. Appending the kernel as a *file path*
# (not the target — CMake would dedupe that against the earlier
# occurrence) puts it after the group.
target_link_libraries(chip INTERFACE $<TARGET_FILE:kernel>)

target_include_directories(app PRIVATE
    ${CHIP_ROOT}/zzz_generated/app-common)"""

#: The data-model call. Placed last in both files, after the application's
#: own sources, and formatted identically in both for the same reason as
#: the blocks above.
_CHIP_DATA_MODEL = """\
# Framework-owned data model: endpoint 0 (root node) only — every device
# endpoint is registered at runtime (ADR 0014, native composed node).
# ZCL_PATH must be passed explicitly: the zcl.json path stored inside the
# .zap is relative to CHIP's own example directories, so it cannot resolve
# for a .zap that lives outside the CHIP tree (documented escape hatch in
# chip_data_model.cmake). The .matter IDL is inferred from the .zap name."""


def render_cmakelists(model: DeviceModel, *, config_name: str) -> str:
    """The application skeleton: generated data plus the framework's main."""
    snippets = ", ".join(model.build.snippets) or "none"
    matter = model.network.matter_enabled
    intro = [
        f'Zephyr application for device "{model.device.name}". The MCUHome '
        f"runtime is consumed as a Zephyr module, so this file only names the "
        f"generated device configuration and the framework's generic "
        f"application main — all behavior lives in the module.",
        f"Generated for board {model.device.board}, snippets: {snippets}. "
        f"Building it for anything else is not a supported configuration: the "
        f"tables reference this board's devicetree nodes by name.",
    ]
    if snippets != "none":
        intro.append(
            "Build it the way it was generated, from a west workspace that has "
            "the MCUHome module:\n"
            "  west build -b "
            + model.device.board
            + " "
            + " ".join(f"-S {snippet}" for snippet in model.build.snippets)
            + " <this directory>"
        )
    out = [_file_header("hash", config_name, intro), ""]
    out.append("cmake_minimum_required(VERSION 3.20.0)")
    if matter:
        out += ["", _CHIP_PRELUDE]
    out += ["", "find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})"]
    if matter:
        # No LANGUAGES clause: chip_configure_data_model() adds CHIP's own
        # C++ sources to the `app` target, so the project has to enable C++
        # even though nothing MCUHome generates or ships as app glue is C++.
        out.append(f"project({model.device.name})")
        out += ["", _CHIP_GLUE]
    else:
        out.append(f"project({model.device.name} LANGUAGES C)")
    out += [
        "",
        "target_sources(app PRIVATE",
        "    ${ZEPHYR_MCUHOME_MODULE_DIR}/app/src/main.c",
        f"    ${{CMAKE_CURRENT_SOURCE_DIR}}/src/{CONFIG_BASENAME}.c",
        ")",
        "",
        "target_include_directories(app PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)",
    ]
    if matter:
        out += [
            "",
            _CHIP_DATA_MODEL,
            "chip_configure_data_model(app",
            "    ZAP_FILE ${ZEPHYR_MCUHOME_MODULE_DIR}/components/matter/zap/mcuhome-root.zap",
            "    ZCL_PATH ${CHIP_ROOT}/src/app/zap-templates/zcl/zcl.json",
            ")",
        ]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# include/CHIPProjectConfig.h
# --------------------------------------------------------------------------

_CHIP_PROJECT_CONFIG_INTRO = [
    "CHIP's project configuration is framework-owned: the values and the "
    "reasoning behind them live in "
    "<mcuhome/matter/chip_project_config.h>, one copy for every MCUHome "
    "device ever built.",
    "This wrapper exists only because CHIP resolves "
    "CONFIG_CHIP_PROJECT_CONFIG relative to the application source "
    "directory, so a generated application needs one file of its own at a "
    "predictable app-relative path. Nothing device-specific belongs here — "
    "if a value has to differ per device, it becomes a Kconfig symbol in "
    "the fragment next to this file, not a line in this one.",
]


def render_chip_project_config(model: DeviceModel, *, config_name: str) -> str:
    """The one-line wrapper CHIP's ``CONFIG_CHIP_PROJECT_CONFIG`` points at."""
    del model
    out = [_file_header("c", config_name, _CHIP_PROJECT_CONFIG_INTRO), ""]
    out += [
        "#pragma once",
        "",
        "#include <mcuhome/matter/chip_project_config.h>",
    ]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# The tree
# --------------------------------------------------------------------------


def generate(model: DeviceModel, *, config_name: str) -> dict[str, str]:
    """Stage 4: every generated artifact as ``relative path -> content``.

    Pure: it touches no filesystem, so the same model always produces the
    same mapping. :func:`write_tree` is the thin part that puts it on disk.
    """
    board = board_file_stem(model.device.board)
    files = {
        MODEL_FILE: model.to_json(),
        f"{APP_DIR}/CMakeLists.txt": render_cmakelists(model, config_name=config_name),
        f"{APP_DIR}/prj.conf": render_prj_conf(model, config_name=config_name),
        f"{APP_DIR}/boards/{board}.overlay": render_overlay(model, config_name=config_name),
        f"{APP_DIR}/src/{CONFIG_BASENAME}.c": render_config_source(model, config_name=config_name),
        f"{APP_DIR}/src/{CONFIG_BASENAME}.h": render_config_header(model, config_name=config_name),
    }
    if model.network.matter_enabled:
        files[f"{APP_DIR}/{CHIP_PROJECT_CONFIG_PATH}"] = render_chip_project_config(
            model, config_name=config_name
        )
    return files


def write_tree(model: DeviceModel, *, out_dir: Path, config_name: str) -> list[Path]:
    """Write :func:`generate`'s artifacts under *out_dir*, sorted paths returned."""
    files = generate(model, config_name=config_name)
    written: list[Path] = []
    for relative in sorted(files):
        path = out_dir / relative
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(files[relative], encoding="utf-8")
        except OSError as error:
            raise GenerationError(
                f"The build directory {out_dir} cannot be written: {error.strerror}.",
                hint="pick a writable location with --build-dir",
            ) from error
        written.append(path)
    return written
