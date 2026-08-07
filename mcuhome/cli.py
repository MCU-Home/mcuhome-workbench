# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The ``mcuhome`` command line (builder-pipeline.md §8).

::

    mcuhome new          <device>      # scaffold a device folder
    mcuhome validate     <device>      # stages 1-3, prints a summary
    mcuhome build        <device>      # stages 1-5
    mcuhome sign         <build-dir>   # apply the signature afterwards
    mcuhome init-pairing <device>      # draw commissioning credentials
    mcuhome public-key                 # the public half of the signing key
    mcuhome schema       [what]        # the schema and the registry, as JSON
    mcuhome clean        <device|--all>

``clean`` exists so the surface is stable and refuses cleanly rather than
being missing; everything else is implemented. ``build`` compiles in the
MCUHome builder image (ADR 0007, :mod:`mcuhome.container`); ``--native``
compiles on the host toolchain instead (:mod:`mcuhome.workspace`).

``validate`` and ``build`` take ``--json``, which replaces the whole
human rendering with one machine-readable document on stdout — the
resolved model or the build manifest on success, the errors of
:meth:`~mcuhome.errors.ConfigError.to_dict` on failure. Exit codes are the
same either way, and the build log still goes to stderr, so redirecting
stdout into a file leaves both halves intact.

``validate --json`` carries the **whole** canonical model, commissioning
credentials included, exactly as ``device-model.json`` does: it is the
output of stages 1-3 and a caller that asked for the model gets the
model. ``build --json`` carries the build manifest, which has none — a
manifest describes artifacts. Neither prints the human commissioning
block, which exists for a person holding a device they just built.

**This module is not an API.** Programs embed :mod:`mcuhome.api`, which
is the supported surface; everything here is a command line, free to
change its internals between releases.

``validate`` writes nothing at all. ``build`` writes only into its build
directory, which is deliberately outside the configuration tree
(builder-pipeline.md §2): ``<tree root>/build/<device>/`` unless
``--build-dir`` says otherwise. Inside it, the generated application is
``app/`` and the compiler's working tree is ``build/`` — everything a
human is meant to read on one side, machine spoil on the other.
``init-pairing`` is the one command that writes into the *configuration*
tree, and it writes into exactly one file: the device's own
(:mod:`mcuhome.provision`).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mcuhome import (
    __version__,
    api,
    container,
    export,
    imgtool,
    pairing,
    provision,
    registry,
    scaffold,
    signing,
    workspace,
)
from mcuhome import manifest as manifest_module
from mcuhome import tree as tree_module
from mcuhome.errors import BuildError, ConfigError, MCUHomeError
from mcuhome.generate import APP_DIR, write_tree
from mcuhome.model import DeviceModel, PairingModel
from mcuhome.tree import ConfigTree, resolve_device

__all__ = [
    "BUILD_DIR",
    "format_build_summary",
    "format_commissioning",
    "format_summary",
    "load_device_model",
    "main",
]

#: Directory the per-device build trees are created in, at the tree root.
#: A sibling of ``devices/``, never inside it — build output must not turn
#: up in the user's config diffs (builder-pipeline.md §2).
BUILD_DIR = "build"


def load_device_model(entry: Path, *, tree: ConfigTree) -> DeviceModel:
    """Run stages 1-3 on one device configuration.

    Kept as a name because the CLI is written in terms of it; the
    implementation is :func:`mcuhome.api.load_model`, which is the
    supported one.
    """
    return api.load_model(entry, tree=tree)


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


def format_commissioning(credentials: PairingModel) -> str:
    """The two strings a human needs to add the device to a controller.

    Printed, never written: the builder keeps no record of a device's
    codes beyond the configuration file the user owns and the firmware it
    compiles. Anyone holding either of those holds the passcode, which is
    what makes them worth saying out loud here.
    """
    tuple_ = pairing.Pairing(
        discriminator=credentials.discriminator,
        passcode=credentials.passcode,
        salt=credentials.salt,
        iterations=credentials.iterations,
    )
    lines = [
        "Commissioning",
        f"  manual code    {tuple_.manual_code}",
        f"  QR code        {tuple_.qr_payload}",
        f"  discriminator  {credentials.discriminator} (0x{credentials.discriminator:03X})",
    ]
    if credentials.test_credentials:
        lines.append(
            "  NOTE: these are the credentials published with the Matter SDK. Anyone "
            "who\n        knows them can commission this device — bench use only."
        )
    return "\n".join(lines)


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

    if model.network.pairing is not None:
        lines.append("")
        lines.append(format_commissioning(model.network.pairing))

    if model.build.snippets or model.build.kconfig:
        lines.append("")
        lines.append("Build")
        if model.build.snippets:
            lines.append(f"  snippets: {', '.join(model.build.snippets)}")
        lines.append(f"  {len(model.build.kconfig)} Kconfig settings")

    return "\n".join(lines)


def format_build_summary(
    name: str,
    *,
    images: list[workspace.ImageArtifacts],
    memory: dict[str, list[workspace.MemoryRegion]],
    merged: Path | None = None,
) -> str:
    """What came out of stage 5: which images, where, and what they cost.

    Two images, not one, since ADR 0015: a bootloader and an application
    signed for it. Both are reported, because "the firmware" is now both
    of them and a user installing only the second one has a brick.
    """
    lines = [f"Built {name}."]
    for image in images:
        lines.append("")
        lines.append(image.describe())
        for path in image.files:
            lines.append(f"  {path}")
        for region in memory.get(image.name, []):
            lines.append(f"  memory: {region.describe()}")
    if merged is not None:
        lines.append("")
        lines.append("Combined (every image at its own offset, for a full-chip flash)")
        lines.append(f"  {merged}")
    return "\n".join(lines)


def format_flash_layout(board: str) -> str:
    """The partition table the images were built against (ADR 0015)."""
    definition = registry.BOARDS.get(board)
    if definition is None or definition.update_scheme is None:
        return ""
    scheme = definition.update_scheme
    lines = [
        f"Flash layout (class {scheme.board_class}, MCUboot {scheme.mcuboot_mode}, "
        f"staging: {scheme.staging})"
    ]
    lines += [f"  {entry.describe()}" for entry in scheme.partitions]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _print_json(data: object) -> None:
    """The one place ``--json`` writes, so every document is shaped alike."""
    print(json.dumps(data, indent=2))


def _cmd_validate(args: argparse.Namespace) -> int:
    tree, entry = resolve_device(args.device, config_root=args.config_root)
    args.json_root = tree.root
    result = api.validate_device(entry, tree=tree)
    if getattr(args, "json", False):
        _print_json(result.to_dict())
        return 0 if result.ok else 1
    if not result.ok:
        result.raise_errors()
    assert result.model is not None  # noqa: S101 - ok means there is one
    print(format_summary(result.model))
    if args.verbose:
        print()
        print(result.model.to_json(), end="")
    print()
    print(f"{entry} is valid.")
    return 0


def _positive_int(text: str) -> int:
    """``--jobs``'s type=: a whole number of parallel build jobs, at least 1."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--jobs wants a whole number of parallel build jobs, not {text!r}."
        ) from None
    if value < 1:
        raise argparse.ArgumentTypeError(
            f"--jobs must be at least 1 ({value} would build nothing at all)."
        )
    return value


def _snippets_for(model: DeviceModel, extra: list[str] | None) -> tuple[str, ...]:
    """The configuration's own snippets, then anything the caller added.

    Order matters to Zephyr (later fragments override earlier ones), so
    ``--snippet`` deliberately appends: a development transport must be
    able to override what the configuration asks for, not the reverse.
    Duplicates are dropped rather than refused — asking twice for the
    snippet a device already needs is not a mistake worth stopping for.
    """
    ordered: dict[str, None] = {}
    for snippet in [*model.build.snippets, *(extra or [])]:
        ordered.setdefault(snippet, None)
    return tuple(ordered)


def _resolve_build_key(args: argparse.Namespace) -> tuple[Path, signing.SigningKey | None]:
    """Which key file the build gets, and whether it is the private one.

    Two shapes of the same argument (ADR 0015 decision 8). Normally it is
    the user's own private key, generated on first need; with
    ``--no-sign`` it is the **public** half, which is all the bootloader
    needs and all a machine that must not be able to sign may have.
    Either way it is resolved *before* the build: a missing or unusable
    key is a refusal a user should get in a second, not ten minutes into
    a Matter compile.
    """
    if not args.no_sign:
        key = signing.signing_key(args.signing_key)
        return key.path, key
    if args.public_key is None:
        raise BuildError(
            "--no-sign needs the public half of your signing key (--public-key).",
            hint=(
                "MCUboot verifies against a public key compiled into the "
                "bootloader, so a build that does not sign still has to be told "
                "which key the signature will come from. Write yours out and pass "
                "it:\n"
                f"    mcuhome public-key -o {signing.PUBLIC_KEY_FILE}\n"
                f"    mcuhome build <device> --no-sign --public-key {signing.PUBLIC_KEY_FILE}\n"
                "The private half stays where it is; mcuhome sign applies the "
                "signature afterwards."
            ),
        )
    path = Path(args.public_key).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise BuildError(
            f"MCUHome cannot read the public key {path}: {error.strerror}.",
            hint=f"write one with: mcuhome public-key -o {path}",
        ) from error
    except UnicodeDecodeError as error:
        raise BuildError(
            f"{path} is not a PEM public key.",
            hint=f"write one with: mcuhome public-key -o {path}",
        ) from error
    if signing.looks_like_p256_key(text):
        raise BuildError(
            f"{path} is a private key, and --public-key wants the public half.",
            hint=(
                "the whole point of --no-sign is that the private key never "
                "reaches the machine that builds (ADR 0015 decision 8). Write the "
                "public half out and pass that:\n"
                f"    mcuhome public-key --signing-key {path} -o {signing.PUBLIC_KEY_FILE}"
            ),
        )
    if not signing.looks_like_p256_public_key(text):
        raise BuildError(
            f"{path} is not an ECDSA P-256 public key in PEM form.",
            hint=(
                "MCUHome signs with ECDSA P-256 (ADR 0015 decision 8). Write the "
                "public half of your key with: mcuhome public-key -o <file>"
            ),
        )
    return path, None


def _cmd_build(args: argparse.Namespace) -> int:
    tree, entry = resolve_device(args.device, config_root=args.config_root)
    args.json_root = tree.root
    as_json = getattr(args, "json", False)
    model = load_device_model(entry, tree=tree)

    out_dir = args.build_dir or tree.root / BUILD_DIR / model.device.name
    written = write_tree(model, out_dir=out_dir, config_name=entry.name)
    generated = [str(path.relative_to(out_dir)) for path in written]

    if not as_json:
        print(f"Generated {len(written)} files for {model.device.name} in {out_dir}:")
        for name in generated:
            print(f"  {name}")

    if args.generate_only:
        if as_json:
            _print_json(
                {
                    "ok": True,
                    "device": model.device.name,
                    "build_dir": str(out_dir),
                    "generated": generated,
                    "manifest": None,
                }
            )
            return 0
        _print_commissioning(model)
        return 0

    snippets = _snippets_for(model, args.snippet)
    scheme = _update_scheme_of(model)
    key_path, key = _resolve_build_key(args)
    # The single resolution point (workspace.resolve_jobs): --jobs beats
    # MCUHOME_JOBS beats auto-detection. Resolved once, here, on the host —
    # the container path needs the same number for its own docker run, not
    # a second guess made from inside the container.
    resolved_jobs = workspace.resolve_jobs(cli_jobs=args.jobs)
    bootloader_snippets = () if scheme is None else scheme.bootloader_snippets
    # Absolute from here on: the build runs with the workspace top
    # directory as its working directory (that is how west finds the
    # manifest), so a relative --build-dir would land somewhere else
    # entirely for anyone who invoked the builder from a subdirectory —
    # and the container mounts host paths, which have to be real.
    common = {
        "out_dir": out_dir.resolve(),
        "app_subdir": APP_DIR,
        "board": model.device.board,
        "snippets": snippets,
        "bootloader_snippets": bootloader_snippets,
        "signing_key": key_path,
        "detached_signing": args.no_sign,
        "jobs": resolved_jobs.value,
    }
    # ADR 0007: the container is the build environment, --native is the
    # escape hatch for a contributor who already has a west workspace.
    if args.native:
        plan = workspace.plan_build(**common)
    else:
        plan = container.plan_build(**common, image=args.image)

    if not as_json:
        print()
        print(f"Building {model.device.name} for {model.device.board} in {plan.topdir}")
        if plan.image:
            print(f"  in {plan.image}")
        print(f"  jobs {resolved_jobs.value} ({resolved_jobs.source})")
        print(_signing_key_note(key) if key is not None else _detached_key_note(key_path))
        print(f"  {' '.join(plan.command)}")
        print()
    # The build log is written by a subprocess to the same terminal; flush
    # so the header above it is not still sitting in this process's buffer.
    sys.stdout.flush()

    # In --json mode the compiler's own output would break the document,
    # so it goes to stderr — where a log belongs anyway, and where a
    # caller redirecting stdout into a file still sees progress.
    code, log = workspace.run_build(plan, stream=sys.stderr if as_json else None)
    if code != 0:
        raise workspace.refuse_failed_build(code, build_dir=plan.build_dir)

    if args.no_sign:
        _drop_unsigned_lookalikes(plan.build_dir, app_image=plan.app_dir.name)

    images = workspace.build_images(plan.build_dir, app_image=plan.app_dir.name)
    merged = workspace.merged_image(plan.build_dir)
    manifest = manifest_module.build_manifest(
        model,
        out_dir=out_dir.resolve(),
        build_dir=plan.build_dir,
        app_image=plan.app_dir.name,
        images=images,
        snippets=snippets,
        bootloader_snippets=bootloader_snippets,
        jobs=resolved_jobs.value,
        signed_by_the_build=not args.no_sign,
        merged=merged,
    )
    manifest_path = manifest_module.write_manifest(manifest, out_dir=out_dir.resolve())

    if as_json:
        _print_json(
            {
                "ok": True,
                "device": model.device.name,
                "build_dir": str(out_dir),
                "generated": generated,
                "manifest_path": str(manifest_path),
                "manifest": manifest.to_dict(),
            }
        )
        return 0

    print()
    print(
        format_build_summary(
            model.device.name,
            images=images,
            memory=workspace.parse_image_memory_report(
                log, images=[image.name for image in images]
            ),
            merged=merged,
        )
    )
    print(f"  {manifest_path}")
    layout = format_flash_layout(model.device.board)
    if layout:
        print()
        print(layout)
    if args.no_sign:
        print()
        print(_detached_next_step(out_dir))
    _print_commissioning(model)
    return 0


def _drop_unsigned_lookalikes(build_dir: Path, *, app_image: str) -> None:
    """Remove files a detached build must not leave behind.

    Two kinds, both named as though they were bootable and neither of
    them signed: a ``zephyr.signed.*`` left over from an earlier inline
    build of the same directory, and sysbuild's combined hex, which falls
    back to the *unsigned* application when there is no signed one to
    merge. Deleting them is not tidiness — it is the difference between
    "no flashable file yet" and "a flashable file that bricks the boot",
    and ``mcuhome sign`` is one command away from producing the real one.
    """
    output = build_dir / app_image / "zephyr"
    for name in (
        "zephyr.signed.bin",
        "zephyr.signed.hex",
        "zephyr.signed.confirmed.bin",
        "zephyr.signed.confirmed.hex",
    ):
        (output / name).unlink(missing_ok=True)
    for merged in build_dir.glob(workspace.MERGED_IMAGE_GLOB):
        merged.unlink(missing_ok=True)


def _detached_next_step(out_dir: Path) -> str:
    return (
        "This build is UNSIGNED, and MCUboot boots nothing it cannot verify.\n"
        "Sign it where your private key is:\n"
        f"    mcuhome sign {out_dir}"
    )


def _update_scheme_of(model: DeviceModel) -> registry.UpdateSchemeDef | None:
    board = registry.BOARDS.get(model.device.board)
    return None if board is None else board.update_scheme


def _signing_key_note(key: signing.SigningKey) -> str:
    """Where the signing key is, and — loudly — when it is brand new.

    A new key is not a detail: MCUboot verifies against the public half
    compiled into the bootloader already on the device, so firmware
    signed with a key that was just generated is firmware an already
    bootstrapped device will refuse.
    """
    if not key.created:
        return f"  signing key {key.path}"
    return (
        f"  signing key {key.path}\n"
        "               NEW — MCUHome had none and generated one just now. Keep "
        "it: every\n"
        "               device bootstrapped with it only accepts firmware signed "
        "with it,\n"
        "               and replacing it means bootstrapping those devices again."
    )


def _detached_key_note(path: Path) -> str:
    """Where the *public* key came from, and what it does not let happen."""
    return (
        f"  public key  {path}\n"
        "              --no-sign: the bootloader gets this, the application is\n"
        "              left unsigned, and no private key is anywhere near this build."
    )


def _print_commissioning(model: DeviceModel) -> None:
    """The pairing codes, last, where a freshly built device needs them."""
    if model.network.pairing is None:
        return
    print()
    print(format_commissioning(model.network.pairing))


def _cmd_init_pairing(args: argparse.Namespace) -> int:
    tree, entry = resolve_device(args.device, config_root=args.config_root)
    result = provision.init_pairing(
        entry,
        secrets_file=tree.secrets_file,
        use_secrets=args.secrets,
        force=args.force,
    )
    verb = "Replaced the commissioning credentials in" if result.replaced else "Wrote"
    print(f"{verb} {result.entry}.")
    if result.secrets_file is not None:
        print(f"The values themselves are in {result.secrets_file}.")
    print()
    print(format_commissioning(_pairing_model(result.pairing)))
    print()
    print(
        "Keep the configuration safe: it is the only copy. Anyone who has it — or the "
        "firmware\nbuilt from it — can commission this device."
    )
    return 0


def _pairing_model(credentials: pairing.Pairing) -> PairingModel:
    return PairingModel(
        discriminator=credentials.discriminator,
        passcode=credentials.passcode,
        salt=credentials.salt,
        iterations=credentials.iterations,
        test_credentials=credentials.test_credentials,
    )


def _cmd_new(args: argparse.Namespace) -> int:
    created = scaffold.new_device(args.device, board=args.board, config_root=args.config_root)
    if created.created_tree:
        print(f"Started a configuration tree in {created.tree.root}.")
    print(f"Wrote {created.entry}.")
    print()
    print("Next:")
    print(f"  mcuhome init-pairing {created.name}    draw this device's commissioning codes")
    print(f"  mcuhome validate {created.name}        see what it resolves to")
    print(f"  mcuhome build {created.name}           compile it")
    print()
    print(
        "The configuration has no hardware in it yet — the file carries a complete, "
        "commented\nexample to uncomment and adjust."
    )
    return 0


def _cmd_sign(args: argparse.Namespace) -> int:
    plan = imgtool.sign_build(Path(args.target), key=args.signing_key)
    data = manifest_module.record_signature(
        manifest_module.read_manifest(plan.manifest_path),
        out_dir=plan.out_dir,
        files=plan.outputs,
    )
    manifest_module.dump_manifest(data, out_dir=plan.out_dir)
    print(f"Signed the application image of {plan.out_dir} with {plan.key}:")
    for path in plan.outputs:
        print(f"  {path}")
    print()
    print(
        f"imgtool sign --version {plan.parameters.version} "
        f"--header-size {plan.parameters.header_size} "
        f"--slot-size {plan.parameters.slot_size} --align {plan.parameters.align}\n"
        "  — the parameters the build manifest states, which are the ones the build "
        "would have\n    used itself."
    )
    if data.get("merged") is None:
        print()
        print(
            "There is no combined hex for a full-chip flash: a --no-sign build does "
            "not write\none, because sysbuild would fill it with the unsigned "
            "application. Install the\nbootloader and the signed application above "
            "separately, or build with signing on."
        )
    return 0


def _cmd_public_key(args: argparse.Namespace) -> int:
    key = signing.signing_key(args.signing_key, create=False)
    pem = signing.public_key_pem(key.path.read_text(encoding="utf-8"))
    if args.output is None:
        print(pem, end="")
        return 0
    output = Path(args.output).expanduser()
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(pem, encoding="utf-8")
    except OSError as error:
        raise ConfigError(
            f"MCUHome cannot write {output}: {error.strerror}.",
            hint="pick a writable location",
        ) from error
    print(f"Wrote the public half of {key.path} to {output}.")
    print("It is not a secret: it is what a build server needs and all it may have.")
    return 0


#: What ``mcuhome schema`` can emit, and what produces it.
SCHEMA_EXPORTS = {
    "config": export.config_json_schema,
    "registry": export.registry_data,
}


def _cmd_schema(args: argparse.Namespace) -> int:
    text = export.to_json(SCHEMA_EXPORTS[args.what]())
    if args.output is None:
        print(text, end="")
        return 0
    output = Path(args.output).expanduser()
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    except OSError as error:
        raise ConfigError(
            f"MCUHome cannot write {output}: {error.strerror}.",
            hint="pick a writable location",
        ) from error
    print(f"Wrote the {args.what} document to {output}.")
    return 0


def _cmd_clean(args: argparse.Namespace) -> int:
    del args
    print("mcuhome clean is not implemented yet.", file=sys.stderr)
    print(
        f"Build output is self-contained: delete the {BUILD_DIR}/ directory, or the "
        "one --build-dir pointed at, and nothing else is affected.",
        file=sys.stderr,
    )
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

    def add_json_option(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--json",
            action="store_true",
            help=(
                "print one machine-readable document on stdout instead of the "
                "human summary; errors come out the same way and exit codes do "
                "not change (validate emits the resolved model, build the build "
                "manifest; the build log goes to stderr)"
            ),
        )

    new_parser = subparsers.add_parser(
        "new", help="create a new device folder with a starter configuration"
    )
    new_parser.add_argument("device", help="device name; it becomes the folder and the hostname")
    new_parser.add_argument(
        "--board",
        required=True,
        metavar="TARGET",
        help=(
            "Zephyr board target this device runs on, verbatim "
            f"(supported today: {', '.join(sorted(registry.BOARDS))})"
        ),
    )
    add_common_options(new_parser)
    new_parser.set_defaults(func=_cmd_new)

    validate_parser = subparsers.add_parser(
        "validate", help="check a device configuration and print what it resolves to"
    )
    validate_parser.add_argument(
        "device", help="device folder name, or the path of a device folder or YAML file"
    )
    add_json_option(validate_parser)
    add_common_options(validate_parser)
    validate_parser.set_defaults(func=_cmd_validate)

    build_parser_ = subparsers.add_parser("build", help="build firmware for a device")
    build_parser_.add_argument("device", help="device folder name or path")
    build_parser_.add_argument(
        "--build-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"where to generate the application (default: <tree root>/{BUILD_DIR}/<device>)",
    )
    build_parser_.add_argument(
        "--generate-only",
        action="store_true",
        help="stop after writing the generated application, and succeed",
    )
    build_parser_.add_argument(
        "-S",
        "--snippet",
        action="append",
        metavar="NAME",
        help=(
            "Zephyr snippet to apply on top of the ones the configuration needs "
            "(repeatable); debug-rtt is the usual one during bring-up"
        ),
    )
    # ADR 0007: the builder container is the build environment, --native
    # is the escape hatch. This is the flag whose default flipped when the
    # image landed (phase 2 block D); nothing else about the surface did.
    build_parser_.add_argument(
        "--native",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "compile on this machine, in the west workspace MCUHome is installed "
            "in, instead of in the builder container (needs a Zephyr toolchain, "
            "gn and zap on the host)"
        ),
    )
    build_parser_.add_argument(
        "--image",
        metavar="REF",
        default=None,
        help=(
            f"builder image to compile in (default: {container.IMAGE}; the "
            f"{container.IMAGE_VAR} environment variable sets it too)"
        ),
    )
    build_parser_.add_argument(
        "--signing-key",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "ECDSA P-256 private key to sign the firmware with (default: "
            f"{signing.default_key_path()}, generated on first use; the "
            f"{signing.KEY_VAR} environment variable sets it too)"
        ),
    )
    build_parser_.add_argument(
        "--no-sign",
        action="store_true",
        help=(
            "build the application UNSIGNED and record the signing parameters in "
            "the build manifest, so that the private key never has to be on the "
            "machine that compiles (ADR 0015 decision 8); needs --public-key, and "
            "mcuhome sign applies the signature afterwards"
        ),
    )
    build_parser_.add_argument(
        "--public-key",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "public half of the signing key, compiled into the bootloader "
            "(required with --no-sign; write one with mcuhome public-key)"
        ),
    )
    build_parser_.add_argument(
        "--jobs",
        type=_positive_int,
        default=None,
        metavar="N",
        help=(
            "parallel build jobs (default: auto-detected from CPU count and "
            f"available RAM; {workspace.JOBS_VAR} overrides the auto-detection, "
            "--jobs overrides both)"
        ),
    )
    add_json_option(build_parser_)
    add_common_options(build_parser_)
    build_parser_.set_defaults(func=_cmd_build)

    sign_parser = subparsers.add_parser(
        "sign", help="sign the application image of a finished build"
    )
    sign_parser.add_argument(
        "target",
        help=f"build directory, or the {manifest_module.MANIFEST_FILE} inside one",
    )
    sign_parser.add_argument(
        "--signing-key",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "ECDSA P-256 private key to sign with (default: "
            f"{signing.default_key_path()}; the {signing.KEY_VAR} environment "
            "variable sets it too). Never generated here: a build has to be signed "
            "with the key its device's bootloader already carries."
        ),
    )
    add_common_options(sign_parser)
    sign_parser.set_defaults(func=_cmd_sign)

    public_key_parser = subparsers.add_parser(
        "public-key", help="print the public half of the firmware signing key"
    )
    public_key_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help="write it to a file instead of stdout",
    )
    public_key_parser.add_argument(
        "--signing-key",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"which key to take the public half of (default: {signing.default_key_path()})",
    )
    add_common_options(public_key_parser)
    public_key_parser.set_defaults(func=_cmd_public_key)

    schema_parser = subparsers.add_parser(
        "schema", help="print the configuration JSON Schema, or the registry, as JSON"
    )
    schema_parser.add_argument(
        "what",
        nargs="?",
        default="config",
        choices=sorted(SCHEMA_EXPORTS),
        help=(
            "config: a JSON Schema for main.yaml, for editor validation and "
            "autocomplete. registry: the boards, drivers, clusters and device "
            "types MCUHome knows, as data"
        ),
    )
    schema_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help="write it to a file instead of stdout",
    )
    add_common_options(schema_parser)
    schema_parser.set_defaults(func=_cmd_schema)

    init_parser = subparsers.add_parser(
        "init-pairing",
        help="draw this device's commissioning credentials and write them into its configuration",
    )
    init_parser.add_argument("device", help="device folder name or path")
    init_parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "replace credentials that are already there (every controller that knows "
            "the device has to commission it again)"
        ),
    )
    init_parser.add_argument(
        "--secrets",
        action="store_true",
        help=(
            f"put the values in the tree's {tree_module.SECRETS_FILE} and reference them "
            "with !secret, for a configuration that lives in version control"
        ),
    )
    add_common_options(init_parser)
    init_parser.set_defaults(func=_cmd_init_pairing)

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
        # Both streams end up in the same terminal, and a command that
        # printed progress before failing must not have its error appear
        # above the output it refers to. Only stdout is buffered when it is
        # a pipe, so flushing it here is what keeps the order right.
        sys.stdout.flush()
        if getattr(args, "json", False):
            # The same document shape as a successful --json run, so a
            # caller parses one thing and reads `ok`. The tree root, when
            # the command got as far as resolving one, is what makes the
            # file paths tree-relative rather than this machine's.
            _print_json(
                {"ok": False, "errors": error.to_dicts(root=getattr(args, "json_root", None))}
            )
            return 1
        print(error.render(), file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
