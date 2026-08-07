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
        ├── sysbuild.conf              which bootloader, which mode
        ├── sysbuild/
        │   ├── mcuboot.conf           the bootloader image's Kconfig
        │   └── mcuboot.overlay        the bootloader image's devicetree
        ├── boards/<board>.overlay
        ├── include/
        │   └── CHIPProjectConfig.h    (Matter devices only)
        └── src/
            ├── mcuhome_config.c
            └── mcuhome_config.h

"Standalone" is meant literally: ``west build -b <board> --sysbuild
<build dir>/app`` from a west workspace produces the same images
``mcuhome build`` does — the file names above are the ones sysbuild
discovers by itself. That is why the CMakeLists carries the whole Matter
build glue rather than a call into something the builder owns — and why
the *only* thing it needs from the MCUHome module is the module directory
Zephyr hands it, never a path this generator wrote down.

**One thing is deliberately missing from the tree: the signing key.** It
is a per-user secret living outside every repository and build directory
(ADR 0015 decision 8, :mod:`mcuhome.signing`), so its path is passed on
the command line and never written here. A build that forgets it does not
fail — MCUboot's own default is its published demo key — which is why the
generated ``sysbuild.conf`` says so where a reader will see it.

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

from mcuhome import pairing, registry
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
    "BOOTLOADER_IMAGE",
    "CHIP_PROJECT_CONFIG_PATH",
    "CONFIG_BASENAME",
    "DETACHED_SIGNING_CMAKE",
    "DETACHED_SIGNING_VAR",
    "MODEL_FILE",
    "SYSBUILD_CMAKE",
    "SYSBUILD_CONF",
    "SYSBUILD_DIR",
    "board_file_stem",
    "generate",
    "write_tree",
]

#: Sub-directory of the build tree holding the standalone Zephyr app.
#: Sysbuild names the main image after this directory, which is why the
#: per-image CMake variables below spell it out (``-Dapp_SNIPPET=…``).
APP_DIR = "app"
#: Sysbuild's name for the MCUboot image. Fixed by
#: ``zephyr/share/sysbuild/images/bootloader/CMakeLists.txt``; the config
#: file names under :data:`SYSBUILD_DIR` are derived from it.
BOOTLOADER_IMAGE = "mcuboot"
#: Where sysbuild looks for per-image configuration, relative to the app.
SYSBUILD_DIR = "sysbuild"
#: Sysbuild's own Kconfig fragment, relative to the app.
SYSBUILD_CONF = "sysbuild.conf"
#: Sysbuild includes this from the main application's directory, which is
#: the one place an application can change how sysbuild configures its
#: *images* rather than itself.
SYSBUILD_CMAKE = "sysbuild.cmake"
#: The image-configuration script :data:`SYSBUILD_CMAKE` appends, under
#: :data:`SYSBUILD_DIR`. Sysbuild reads that directory by exact file name
#: (``<image>.conf``, ``<image>.overlay``), so a third file with another
#: name sits there without being picked up by accident.
DETACHED_SIGNING_CMAKE = "mcuhome-detached-signing.cmake"
#: CMake variable that turns detached signing on for one build
#: (``mcuhome build --no-sign``). A plain ``-D`` variable rather than a
#: sysbuild Kconfig symbol: it selects how *this* build runs, it is not a
#: property of the device, and nothing about the generated tree changes
#: with it — the same tree signs inline when the flag is absent.
DETACHED_SIGNING_VAR = "MCUHOME_DETACHED_SIGNING"
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
    "The blocks before the peripherals are board wiring rather than device "
    "configuration: the flash layout and whatever every MCUHome node on this "
    "board needs, whatever its YAML says.",
]

#: Why the builder owns the flash layout instead of taking the board's.
#: Generic knowledge about MCUHome on any board, so it is generated
#: comment text like the rest (see the module docstring).
_PARTITION_NOTE = (
    "Flash layout (ADR 0015). MCUHome fixes the partition table itself rather "
    "than inheriting the board's, because no upstream default holds an MCUHome "
    "image: the two-slot table this board ships with gives the application "
    "464 KiB, and a commissioned Matter node is over 550 KiB.\n"
    "The storage partition keeps the address and size the board's own devicetree "
    "gives it. That is what makes an update not a re-commissioning: the Matter "
    "fabric credentials and the Thread dataset live there, and every layout in "
    "ADR 0015 preserves them."
)

#: Appended to :data:`_PARTITION_NOTE` in the application's board overlay.
_PARTITION_NOTE_APP = (
    "The bootloader image is given the same table, in "
    f"{SYSBUILD_DIR}/{BOOTLOADER_IMAGE}.overlay: the two have to agree on the "
    "flash map byte for byte."
)


def _layout_comment(scheme: registry.UpdateSchemeDef, note: str) -> list[str]:
    """*note* as a block comment, with the layout table under it verbatim.

    The table is appended rather than wrapped into the prose because it
    is a table: :func:`_comment` would reflow the columns into one
    paragraph and the numbers would stop lining up.
    """
    wrapped = _wrap(_check_comment_safe(note), _COLUMNS - 3)
    body = [f"/* {wrapped[0]}"] + [f" * {line}".rstrip() for line in wrapped[1:]]
    body.append(" *")
    body += [f" *   {entry.describe()}" for entry in scheme.partitions]
    body.append(" */")
    return body


def _partition_block(board: registry.BoardDef | None) -> list[str]:
    """The flash layout, or nothing for a board that has no scheme yet."""
    if board is None or board.update_scheme is None:
        return []
    scheme = board.update_scheme
    note = f"{_PARTITION_NOTE}\n{_PARTITION_NOTE_APP}"
    return ["", *_layout_comment(scheme, note), scheme.partition_overlay]


def render_overlay(model: DeviceModel, *, config_name: str) -> str:
    """The board overlay: flash layout, board wiring, one node per peripheral."""
    intro = [f"Board: {model.device.board}.", *_OVERLAY_INTRO]
    out = [_file_header("c", config_name, intro)]

    board = registry.BOARDS.get(model.device.board)
    out += _partition_block(board)
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
            "This application needs the snippets it was generated for: "
            + ", ".join(model.build.snippets)
            + ". CMakeLists.txt next to this file spells out the build command "
            "that applies them."
        )
    out = [_file_header("hash", config_name, intro), ""]
    out += list(model.build.kconfig)
    if model.network.pairing is not None:
        credentials = model.network.pairing
        out += [
            "",
            "# This device's commissioning identity, emitted as one indivisible group",
            "# by mcuhome.pairing.kconfig_lines(). CHIP takes the passcode and the",
            "# SPAKE2+ verifier derived from it as two unrelated symbols and checks",
            "# neither against the other on Zephyr, so a build that changed one and",
            "# not the other would flash, boot, advertise itself and then refuse",
            "# every commissioner. Nothing here may be edited by hand: change the",
            "# credentials in the device configuration and rebuild.",
            *pairing.kconfig_lines(
                pairing.Pairing(
                    discriminator=credentials.discriminator,
                    passcode=credentials.passcode,
                    salt=credentials.salt,
                    iterations=credentials.iterations,
                )
            ),
        ]
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
# sysbuild: which bootloader, and how it is configured
# --------------------------------------------------------------------------


def _scheme_of(model: DeviceModel) -> registry.UpdateSchemeDef | None:
    board = registry.BOARDS.get(model.device.board)
    return None if board is None else board.update_scheme


def render_sysbuild_conf(model: DeviceModel, *, config_name: str) -> str:
    """``sysbuild.conf`` — the bootloader, its mode and the signature type.

    Vanilla Zephyr integrates MCUboot through sysbuild only (ADR 0015
    decision 1), and sysbuild picks this file up by name from the
    application directory, so the choice travels with the generated tree
    rather than living in the command line that built it.
    """
    scheme = _scheme_of(model)
    if scheme is None:  # pragma: no cover - guarded by the caller
        raise GenerationError(
            f'The board "{model.device.board}" has no update scheme in the registry.',
            hint="this is a builder bug — every buildable board carries one (ADR 0015)",
        )
    mode_symbol = registry.MCUBOOT_MODE_SYMBOLS[scheme.mcuboot_mode]
    staging = {
        "external-flash": (
            "the second copy of the image lives on the board's external flash "
            "part, so a failed update can be rolled back"
        ),
        "none": (
            "there is no second copy: this board updates over USB through the "
            "bootloader's serial recovery, and cannot roll back"
        ),
    }.get(scheme.staging, scheme.staging)
    intro = [
        f'Sysbuild configuration for device "{model.device.name}". Every MCUHome '
        f"image boots through MCUboot (ADR 0015 decision 1), and vanilla Zephyr "
        f"builds a bootloader only under sysbuild — which is why this file "
        f"exists and why the application is built with --sysbuild.",
        f"Board class {scheme.board_class}: {staging}. The flash layout that "
        f"follows from it is in boards/{board_file_stem(model.device.board)}.overlay "
        f"and in {SYSBUILD_DIR}/{BOOTLOADER_IMAGE}.overlay, and the bootloader's own "
        f"settings are in {SYSBUILD_DIR}/{BOOTLOADER_IMAGE}.conf.",
        "THE SIGNING KEY IS NOT IN THIS FILE, AND NOT IN THIS TREE. It is a "
        "per-user secret (ADR 0015 decision 8), so mcuhome build passes it on the "
        "command line:\n"
        '  -DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="<path to your signing.key>"\n'
        "Building this tree by hand without that argument does not fail — it "
        "signs with MCUboot's demo key, whose private half is published in the "
        "MCUboot repository. An image signed with it is not signed; it only "
        "looks signed. Pass the key.",
    ]
    out = [_file_header("hash", config_name, intro), ""]
    out += [
        "SB_CONFIG_BOOTLOADER_MCUBOOT=y",
        f"{mode_symbol}=y",
        "SB_CONFIG_BOOT_SIGNATURE_TYPE_ECDSA_P256=y",
        "",
        "# One hex file with every image at its own offset. Off upstream,",
        "# on here: bringing a board into the MCUHome standard state writes",
        "# the bootloader and the application together (ADR 0016 decision 2),",
        "# and on a development kit that is one debug-probe flash of one file.",
        "SB_CONFIG_MERGED_HEX_FILES=y",
    ]
    return "\n".join(out) + "\n"


def render_sysbuild_cmake(model: DeviceModel, *, config_name: str) -> str:
    """``sysbuild.cmake`` — the one hook an application has into sysbuild.

    It does nothing on its own. All it does is append
    :data:`DETACHED_SIGNING_CMAKE` to the application image's
    configuration scripts, so that the script runs *after* Zephyr's own
    ``MAIN_image_default.cmake`` and can therefore have the last word on
    a symbol that file also sets. Order is the whole mechanism: sysbuild
    turns the accumulated settings into one forced Kconfig fragment, and
    in a fragment the last assignment wins.
    """
    del model
    intro = [
        "Sysbuild hook for the generated application. Sysbuild includes this file "
        "from the application directory before it configures any image, which is "
        "the only place an application can influence how its own image is "
        "configured.",
        f"Everything it does is behind {DETACHED_SIGNING_VAR}, which mcuhome build "
        "--no-sign passes and nothing else sets: a normal build reads this file, "
        "appends one script, and that script does nothing. That is deliberate — "
        "the generated tree is the same tree whether the image is signed during "
        "the build or afterwards, so the two paths cannot drift apart.",
    ]
    return "\n".join(
        [
            _file_header("hash", config_name, intro),
            "",
            "# Appended, not assigned: Zephyr's own MAIN_image_default.cmake is",
            "# already in this property, and this script has to run after it to",
            "# override a symbol that file sets.",
            "set_property(TARGET ${DEFAULT_IMAGE} APPEND PROPERTY IMAGE_CONF_SCRIPT",
            f"             ${{CMAKE_CURRENT_LIST_DIR}}/{SYSBUILD_DIR}/{DETACHED_SIGNING_CMAKE})",
            "",
        ]
    )


def render_detached_signing_cmake(model: DeviceModel, *, config_name: str) -> str:
    """The image-configuration script that leaves the application unsigned.

    ADR 0015 decision 8 in one file. Sysbuild derives the application's
    ``CONFIG_MCUBOOT_SIGNATURE_KEY_FILE`` from the one key file it is
    given, and that file is a *private* key — which is exactly what must
    not exist on a build server (ADR 0007). So a detached build is given
    the **public** half instead: the bootloader compiles it in, unchanged
    and byte-identical to what the private key would have produced, and
    the application's copy of the setting is cleared here.

    Clearing it rather than switching to Zephyr's
    ``CONFIG_MCUBOOT_GENERATE_UNSIGNED_IMAGE`` is the deliberate half:
    with an empty key file and unsigned-image generation off, Zephyr's
    ``cmake/mcuboot.cmake`` warns and skips the signing step entirely, so
    the build produces no ``zephyr.signed.*`` at all. The alternative
    would have produced files with ``signed`` in their names and no
    signature in them, which is worse than producing nothing.
    """
    del model
    intro = [
        "Detached signing (ADR 0015 decision 8): leave the application unsigned so "
        "that the private key never has to be where the build runs. mcuhome sign "
        "applies the signature afterwards, wherever the key is, using the "
        "parameters the build manifest states.",
        "The bootloader is unaffected. It is given the public half of the same key "
        "pair and compiles it in exactly as it would the public half of the private "
        "key, so a detached build and an inline build produce the same bootloader.",
    ]
    return "\n".join(
        [
            _file_header("hash", config_name, intro),
            "",
            f"if(NOT {DETACHED_SIGNING_VAR})",
            "  return()",
            "endif()",
            "",
            "# An empty key file with unsigned-image generation left off is what",
            "# makes Zephyr's cmake/mcuboot.cmake skip signing altogether instead",
            "# of writing an unsigned file called zephyr.signed.bin.",
            'set_config_string(${ZCMAKE_APPLICATION} CONFIG_MCUBOOT_SIGNATURE_KEY_FILE "")',
            "",
        ]
    )


def render_bootloader_conf(model: DeviceModel, *, config_name: str) -> str:
    """``sysbuild/mcuboot.conf`` — Kconfig for the bootloader image.

    Sysbuild adds this to the MCUboot image's ``EXTRA_CONF_FILE``, which
    is merged after MCUboot's own per-board fragment — so a value here
    overrides one MCUboot ships, which is exactly what an external
    secondary slot needs (upstream switches the SPI-NOR driver off for
    this board, correctly, for a bootloader that has no external slot).
    """
    scheme = _scheme_of(model)
    if scheme is None:  # pragma: no cover - guarded by the caller
        raise GenerationError(
            f'The board "{model.device.board}" has no update scheme in the registry.',
            hint="this is a builder bug — every buildable board carries one (ADR 0015)",
        )
    intro = [
        "Kconfig fragment for the MCUboot image. Sysbuild derives the swap mode "
        "and the signature type from sysbuild.conf next to this file; what is "
        "below is everything that does not follow from those two.",
        "Recovery: " + ", ".join(scheme.recovery) + ".",
    ]
    out = [_file_header("hash", config_name, intro), ""]
    out += list(scheme.bootloader_kconfig)
    slot = scheme.partition("slot0_partition")
    out += [
        "",
        "# How many flash sectors MCUboot has to be able to track per slot.",
        "# Not left to CONFIG_BOOT_MAX_IMG_SECTORS_AUTO: it reads the block size",
        "# of slot0's flash node and applies it to slot1 as well, which is wrong",
        "# the moment the two slots live on different parts — as they do here",
        "# (ADR 0015 decision 3; upstream bug candidate). The number below is",
        f"# {slot.size // 1024} KiB of slot divided by the {scheme.erase_block_size} B erase unit.",
        "CONFIG_BOOT_MAX_IMG_SECTORS_AUTO=n",
        f"CONFIG_BOOT_MAX_IMG_SECTORS={scheme.max_image_sectors}",
    ]
    return "\n".join(out) + "\n"


#: MCUboot's own ``boot/zephyr/app.overlay`` is one chosen entry, and
#: sysbuild's per-image overlay *replaces* the image's devicetree overlay
#: rather than adding to it — so the entry has to be restated here or the
#: bootloader links at the wrong address. Small and stable, but a real
#: coupling to upstream, hence the comment in the generated file.
_BOOTLOADER_CODE_PARTITION = """\
/ {
\tchosen {
\t\tzephyr,code-partition = &boot_partition;
\t};
};"""

#: The CDC-ACM function on the board's USB device controller. Same three
#: lines as MCUboot's own ``usb_cdc_acm.overlay``, restated for the same
#: reason as the chosen entry above.
_BOOTLOADER_CDC_ACM = """\
&zephyr_udc0 {
\tcdc_acm_uart0 {
\t\tcompatible = "zephyr,cdc-acm-uart";
\t};
};"""

_BOOTLOADER_OVERLAY_INTRO = [
    "Devicetree for the MCUboot image. Sysbuild finds this file by name and "
    "hands it to the bootloader image as its devicetree overlay.",
    "It REPLACES the overlay MCUboot would otherwise pick up rather than adding "
    "to it, which is why the two upstream fragments it needs are restated below "
    "instead of referenced: the chosen entry that links MCUboot into its own "
    "partition (MCUboot's boot/zephyr/app.overlay) and the CDC-ACM function "
    "serial recovery talks over (its usb_cdc_acm.overlay).",
    "The flash layout below is byte-identical to the one in the application's "
    "board overlay. Both images have to see the same flash map: the bootloader "
    "writes the slots, the application is linked into one of them.",
]


def render_bootloader_overlay(model: DeviceModel, *, config_name: str) -> str:
    """``sysbuild/mcuboot.overlay`` — devicetree for the bootloader image."""
    scheme = _scheme_of(model)
    if scheme is None:  # pragma: no cover - guarded by the caller
        raise GenerationError(
            f'The board "{model.device.board}" has no update scheme in the registry.',
            hint="this is a builder bug — every buildable board carries one (ADR 0015)",
        )
    intro = [f"Board: {model.device.board}.", *_BOOTLOADER_OVERLAY_INTRO]
    out = [_file_header("c", config_name, intro), "", _BOOTLOADER_CODE_PARTITION]
    if "serial-recovery" in scheme.recovery:
        out += ["", _BOOTLOADER_CDC_ACM]
    out += ["", *_layout_comment(scheme, _PARTITION_NOTE), scheme.partition_overlay]
    if scheme.bootloader_overlay:
        out += ["", *_comment(scheme.bootloader_overlay_note, 0), scheme.bootloader_overlay]
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

list(APPEND ZEPHYR_EXTRA_MODULES ${CHIP_ROOT}/config/zephyr/chip-module)

# ccache for the Matter half of the build as well (builder-pipeline.md §5:
# ccache is a hard requirement of the builder, not an optimization).
# Zephyr finds ccache by itself and sets the global RULE_LAUNCH_COMPILE
# property, but CHIP's inner GN build is a second build system that never
# sees it — without the two lines below, every clean build directory
# recompiles the whole Matter stack from source.
#
# Pigweed's toolchain templates take a compiler launcher as the GN
# argument `pw_command_launcher` (third_party/pigweed/repo/pw_toolchain/
# toolchain_args.gni), and CHIP's chip_gn_args.cmake appends to
# MATTER_GN_ARGS instead of resetting it when it is already set. So
# pre-seeding that variable here — before find_package(Zephyr) brings the
# chip-module in — is the whole mechanism, with no patch to the SDK. The
# `\\n`-separated form is the response-file syntax make_gn_args.py reads;
# it must not contain a semicolon, which CMake would turn into a list.
#
# USE_CCACHE=0 is Zephyr's own opt-out and switches off both halves.
if(NOT USE_CCACHE STREQUAL "0")
    find_program(MCUHOME_CCACHE ccache)
    if(MCUHOME_CCACHE)
        set(MATTER_GN_ARGS "--arg-string\\npw_command_launcher\\nccache\\n")
    endif()
endif()"""

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
    scheme = _scheme_of(model)
    if scheme is not None:
        # Snippets are named per image, not once for the whole build:
        # sysbuild passes a bare SNIPPET down to every image, and the
        # application's snippets are meaningless — often fatal — in a
        # bootloader. The image names are the application directory's
        # name (this one) and sysbuild's fixed name for MCUboot.
        intro.append(
            "Build it the way it was generated, from a west workspace that has "
            "the MCUHome module. --sysbuild is what builds the bootloader "
            "alongside the application (ADR 0015); the signing key is yours and "
            "lives outside this tree (ADR 0015 decision 8):\n"
            f"  west build -b {model.device.board} --sysbuild <this directory> -- \\\n"
            f'      -D{APP_DIR}_SNIPPET="{";".join(model.build.snippets)}" \\\n'
            f"      -D{BOOTLOADER_IMAGE}_SNIPPET="
            f'"{";".join(scheme.bootloader_snippets)}" \\\n'
            '      -DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="<path to your signing.key>"'
        )
    elif snippets != "none":
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
    if _scheme_of(model) is not None:
        files[f"{APP_DIR}/{SYSBUILD_CONF}"] = render_sysbuild_conf(model, config_name=config_name)
        files[f"{APP_DIR}/{SYSBUILD_CMAKE}"] = render_sysbuild_cmake(model, config_name=config_name)
        files[f"{APP_DIR}/{SYSBUILD_DIR}/{DETACHED_SIGNING_CMAKE}"] = render_detached_signing_cmake(
            model, config_name=config_name
        )
        files[f"{APP_DIR}/{SYSBUILD_DIR}/{BOOTLOADER_IMAGE}.conf"] = render_bootloader_conf(
            model, config_name=config_name
        )
        files[f"{APP_DIR}/{SYSBUILD_DIR}/{BOOTLOADER_IMAGE}.overlay"] = render_bootloader_overlay(
            model, config_name=config_name
        )
    if model.network.matter_enabled:
        files[f"{APP_DIR}/{CHIP_PROJECT_CONFIG_PATH}"] = render_chip_project_config(
            model, config_name=config_name
        )
    return files


def write_tree(model: DeviceModel, *, out_dir: Path, config_name: str) -> list[Path]:
    """Write :func:`generate`'s artifacts under *out_dir*, sorted paths returned.

    A file whose content is already what it should be is left alone, mtime
    and all. That is not tidiness: CMake watches the application's files,
    so rewriting an unchanged ``CMakeLists.txt`` reconfigures every image
    and re-runs the Matter sub-build — which turns "change one Kconfig
    line" from a two-minute rebuild into a twenty-minute one. The
    generator is deterministic (§1.4), so "same content" is a fact it can
    rely on rather than a guess.
    """
    files = generate(model, config_name=config_name)
    written: list[Path] = []
    for relative in sorted(files):
        path = out_dir / relative
        content = files[relative]
        try:
            if not (path.is_file() and path.read_text(encoding="utf-8") == content):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover - not text, so not ours
            path.write_text(content, encoding="utf-8")
        except OSError as error:
            raise GenerationError(
                f"The build directory {out_dir} cannot be written: {error.strerror}.",
                hint="pick a writable location with --build-dir",
            ) from error
        written.append(path)
    return written
