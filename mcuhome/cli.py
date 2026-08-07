# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The ``mcuhome`` command line (builder-pipeline.md §8).

::

    mcuhome validate <device>          # stages 1-3, prints a summary
    mcuhome build    <device>          # stages 1-5
    mcuhome clean    <device|--all>

``validate`` is the only command this milestone implements; ``build`` and
``clean`` exist so the surface is stable and refuse cleanly rather than
being missing. Nothing is written to disk: validation is a read-only
operation, and the build directory is deliberately not part of the config
tree (builder-pipeline.md §2).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mcuhome import __version__, registry
from mcuhome.errors import MCUHomeError
from mcuhome.loader import load_config
from mcuhome.model import DeviceModel
from mcuhome.resolve import resolve
from mcuhome.schema import parse_config
from mcuhome.tree import ConfigTree, resolve_device
from mcuhome.validate import validate

__all__ = ["format_summary", "load_device_model", "main"]

_NOT_IMPLEMENTED = "is not implemented yet (builder phase 2, block C)"


def load_device_model(entry: Path, *, tree: ConfigTree) -> DeviceModel:
    """Run stages 1-3 on one device configuration."""
    data = load_config(entry, secrets_file=tree.secrets_file)
    config = parse_config(data, file=entry)
    validate(config)
    return resolve(config)


# --------------------------------------------------------------------------
# Summary rendering
# --------------------------------------------------------------------------


def _format_duration(milliseconds: int) -> str:
    if milliseconds % 60_000 == 0 and milliseconds >= 60_000:
        return f"{milliseconds // 60_000} min"
    if milliseconds % 1_000 == 0:
        return f"{milliseconds // 1_000} s"
    return f"{milliseconds} ms"


def _cluster_unit(cluster_id: int) -> tuple[str, float]:
    for definition in registry.CLUSTERS.values():
        if definition.id == cluster_id:
            return definition.unit, float(definition.raw_per_unit)
    return "", 1.0  # pragma: no cover - every generated cluster is known


def format_summary(model: DeviceModel) -> str:
    """The human-readable picture of a resolved device."""
    lines: list[str] = []
    device = model.device
    lines.append(f"Device     {device.name} ({device.friendly_name})")
    lines.append(f"Board      {device.board}")
    lines.append(f"Power      {device.power_source}")

    network = model.network
    if network.transport == "thread" and network.thread is not None:
        role = {"ftd": "router", "mtd": "end device"}.get(
            network.thread.device_role, network.thread.device_role
        )
        lines.append(f"Transport  Thread, {role}")
    elif network.transport:
        lines.append(f"Transport  {network.transport}")
    else:
        lines.append("Transport  none (standalone device)")
    lines.append(f"Matter     {'enabled' if network.matter_enabled else 'disabled'}")
    lines.append(f"Zephyr     {model.toolchain.zephyr_line}")
    blobs = ", ".join(f"{name}: {value}" for name, value in model.toolchain.blobs.items())
    lines.append(
        f"Blobs      {blobs or 'none integrated yet'} (blob_usage: {model.toolchain.blob_usage})"
    )

    if model.hardware.buses or model.hardware.peripherals:
        lines.append("")
        lines.append("Hardware")
        for bus in model.hardware.buses:
            detail = f" via {bus.controller}" if bus.controller else ""
            frequency = f", {bus.frequency_hz // 1000} kHz" if bus.frequency_hz else ""
            lines.append(f"  bus {bus.id} ({bus.kind}{detail}{frequency})")
        for peripheral in model.hardware.peripherals:
            where = f" on {peripheral.bus}" if peripheral.bus else ""
            address = f" @ {peripheral.reg:#04x}" if peripheral.reg is not None else ""
            lines.append(f"  {peripheral.id}: {peripheral.compatible}{address}{where}")

    if model.endpoints:
        lines.append("")
        lines.append("Endpoints")
        for endpoint in model.endpoints:
            types = ", ".join(
                f"{item.name} ({item.id:#06x} rev {item.revision})"
                for item in endpoint.device_types
            )
            alias = f" [{endpoint.alias}]" if endpoint.alias else ""
            lines.append(f"  endpoint {endpoint.id}{alias}: {types}")
            for cluster in endpoint.clusters:
                lines.append(
                    f"    {cluster.name} ({cluster.id:#06x} rev "
                    f"{cluster.cluster_revision}, {len(cluster.attrs)} attributes)"
                )

    if model.channels:
        lines.append("")
        lines.append("Channels")
        for channel in model.channels:
            unit, raw_per_unit = _cluster_unit(channel.cluster_id)
            if channel.report_delta:
                natural = channel.report_delta / raw_per_unit
                delta = f"report on {natural:g} {unit} change"
            else:
                delta = "report every sample"
            lines.append(
                f"  {channel.source.channel} -> endpoint {channel.endpoint_id} "
                f"{channel.cluster_id:#06x}/{channel.attr_id:#06x}, every "
                f"{_format_duration(channel.sample_period_ms)}, {delta}"
            )

    if model.build.snippets or model.build.kconfig:
        lines.append("")
        lines.append("Build")
        if model.build.snippets:
            lines.append(f"  snippets: {', '.join(model.build.snippets)}")
        lines.append(f"  {len(model.build.kconfig)} Kconfig settings")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _cmd_validate(args: argparse.Namespace) -> int:
    tree, entry = resolve_device(args.device, config_root=args.config_root)
    model = load_device_model(entry, tree=tree)
    print(format_summary(model))
    if args.verbose:
        print()
        print(model.to_json(), end="")
    print()
    print(f"{entry} is valid.")
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    del args
    print(f"mcuhome build {_NOT_IMPLEMENTED}.", file=sys.stderr)
    print("Use `mcuhome validate <device>` to check a configuration today.", file=sys.stderr)
    return 1


def _cmd_clean(args: argparse.Namespace) -> int:
    del args
    print(f"mcuhome clean {_NOT_IMPLEMENTED}.", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcuhome",
        description="Build Zephyr firmware from an MCUHome YAML device configuration.",
    )
    parser.add_argument("--version", action="version", version=f"mcuhome {__version__}")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="also print the resolved device model",
    )
    subparsers = parser.add_subparsers(dest="command")

    def add_common_options(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--config-root",
            type=Path,
            default=None,
            metavar="PATH",
            help="configuration tree root (default: found by searching upwards)",
        )
        # Also accepted after the subcommand, where people reach for it.
        # SUPPRESS so that leaving it out here does not overwrite the
        # value given before the subcommand.
        subparser.add_argument(
            "-v",
            "--verbose",
            action="store_true",
            default=argparse.SUPPRESS,
            help="also print the resolved device model",
        )

    validate_parser = subparsers.add_parser(
        "validate", help="check a device configuration and print what it resolves to"
    )
    validate_parser.add_argument(
        "device", help="device folder name, or the path of a device folder or YAML file"
    )
    add_common_options(validate_parser)
    validate_parser.set_defaults(func=_cmd_validate)

    build_parser_ = subparsers.add_parser("build", help="build firmware for a device")
    build_parser_.add_argument("device", help="device folder name or path")
    add_common_options(build_parser_)
    build_parser_.set_defaults(func=_cmd_build)

    clean_parser = subparsers.add_parser("clean", help="remove build output of a device")
    clean_parser.add_argument("device", nargs="?", help="device folder name or path")
    clean_parser.add_argument("--all", action="store_true", help="clean every device")
    add_common_options(clean_parser)
    clean_parser.set_defaults(func=_cmd_clean)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 0
    try:
        return int(args.func(args))
    except MCUHomeError as error:
        print(error.render(), file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
