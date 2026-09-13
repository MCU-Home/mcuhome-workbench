# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The supported programmatic surface of the MCUHome builder.

**This module is the API. Everything else is an implementation detail.**
Names exported here are covered by the project's SemVer promise: they
do not change shape within a major version, and a breaking
change to one of them is a breaking change to the builder. Names anywhere
else in the package may move between releases without notice, and a
caller that imports them is on its own. The ``mcuhome`` command line is
such a caller too: it lives in its own repository
(github.com/mcu-home/mcuhome-cli) as a thin shell over this package, and it is
version-locked to the builder rather than the other way around.

The intended consumer is a program that embeds the builder rather than
running it: the MCUHome dashboard imports it in-process so that a
configuration error arrives in an editor's
gutter as a marker with a fix hint rather than in a log pane as a line of
text. The dependency has exactly one direction — the dashboard declares
the builder versions it supports and follows the builder's releases; the
builder never learns that a dashboard exists.

What is here, in the order a caller needs it:

``resolve_project`` / ``create_project`` / ``find_device``
    Where the user's work lives (the ``.mcuhome-project-root`` marker
    and its bootstrap ladder), how a project comes into
    being, and which file is a given device's. Resolution also enforces
    the project's **layout version**: a project older than
    ``PROJECT_VERSION`` is refused with ``ProjectUpgradeRequired``, a
    newer one with ``ProjectVersionUnsupported``, and one whose file an
    upgrade has renamed with ``UpgradeInProgress`` or
    ``UpgradeInterrupted`` — the four states a caller renders
    differently. ``resolve_project(..., require_version=False)`` is for
    the one caller that exists to fix the first of them.
``open_upgrade_session`` / ``UpgradeResult`` / ``Migration``
    Upgrading a project to the current layout. The session renames the
    project file for the whole run — so nothing else can start work on a
    project being rewritten — answers which build directories are still
    busy (``find_running_builds``), and applies the migrations of
    ``mcuhome.workbench.migrations`` in order. A caller drives the three
    apart on purpose: take the project, wait for what is still running,
    *then* ask the user, then apply.
``resolve_settings``
    The five-layer configuration model over the declared option
    registry (``OPTIONS``), each value with the layer it came from.
``resolve_builder``
    Which builder this invocation uses: an explicit name,
    the configured ``build.builder``, or the built-in ``local``
    fallback — credentials from ``secrets/builder/<name>.yaml``
    included.
``new_device`` / ``render_starter`` / ``DeviceOutline``
    A device's first ``main.yaml``. ``render_starter`` is pure — it
    returns the text — so a caller can show it before anything is
    written; ``new_device`` writes it, refusing rather than overwriting.
    Given a ``DeviceOutline`` (buses, peripherals, endpoints) both write
    those as real sections instead of the commented example, which is
    what a form that walked somebody through ``registry_data`` has to
    offer.
``init_pairing`` / ``PairingResult``
    Draw a device's commissioning credentials, once: ``!secret``
    references into ``main.yaml``, the values into the device's own
    secrets file. The one place randomness enters a configuration, and
    the reason a build is reproducible — so it is a command a user
    gives, never a step something else takes on the way past.
``load_model``
    Stages 1-3 on one device, raising on the first thing that is wrong.
``read_model``
    A canonical model back from JSON — the other end of the wire. A build
    server receives one of these and starts at stage 4; it never sees
    the project directory and never sees a secrets file.
    ``mcuhome device build --model <file>`` is the same thing
    as a command.
``validate_device``
    The same three stages, returning **every** problem as typed errors
    instead of raising — one pass, all markers.
``error_dicts`` / ``ConfigError.to_dict``
    Those errors as plain dictionaries: message, file (relative to the
    project), line, column, key, hint, kind.
``registry_data`` / ``config_json_schema``
    What the builder knows about hardware and Matter, and the shape of
    ``main.yaml``, as data an editor or a picker can consume.
``generate_tree`` / ``CompilerUnavailable``
    Stage 4 on this machine: the Zephyr application a device model
    describes, written out and nothing more. A build does not take this
    path — a build environment generates from the model its context
    carries — so this is the caller who wants the tree for its own sake,
    and it refuses in words where ``mcuhome-compiler`` is not installed.
``build_firmware`` / ``BuildRequest`` / ``BuildResult``
    Build a device behind one awaitable call, whichever target runs it:
    a target object, a target name, or nothing at all, which takes the
    request's builder and then ``build.target``. A build has two
    placement questions in it and only the first belongs to a caller:
    **where** it runs (``LocalBuild``, ``RemoteBuild``) and **how** the
    machine that runs it executes the work (``ContainerExecution`` in a
    build container, ``SubprocessExecution`` against a build environment
    unpacked on the host) — which is why ``LocalBuild`` carries an
    ``Execution`` and ``RemoteBuild`` does not. ``RemoteNotConfigured``
    is the typed refusal a caller renders.
``resolve_build_target`` / ``resolve_build_mode``
    A name into a value, for a caller whose choice arrived as a
    command-line flag or a configuration value:
    ``resolve_build_target`` answers one of ``TARGET_LOCAL``,
    ``TARGET_REMOTE`` (``BUILD_TARGETS``, ``DEFAULT_BUILD_TARGET``) or
    raises ``UnknownBuildTarget``, and ``resolve_build_mode`` does the
    same for the other axis — ``MODE_CONTAINER``, ``MODE_SUBPROCESS``
    (``BUILD_MODES``, ``DEFAULT_BUILD_MODE``), or ``UnknownBuildMode``.
    ``BuildRequest.mode`` is where a caller states the mode.
``BuildOptions`` / ``build_options``
    What the ``build`` section of the configuration says about *this
    machine*: the execution it uses, where it keeps unpacked build
    environments and which interpreter finalizes them, how much a package
    may unpack to, which directories each package is looked for in, and
    where the compiler cache tiers are. ``build_options`` turns resolved
    ``Settings`` into that object; a request that states none has them
    resolved from the environment and the project it names.
    ``build.target`` is in there too: where a build of this machine runs
    when nothing more explicit said otherwise, and so are the three
    package source lists both targets resolve their pins from. A caller
    that never touches any of it builds the way the machine is
    configured, which is the point: the registry derives no command-line
    flag for these keys.
``BuilderSession`` / ``StepResult``
    The **backend role**, for the caller that owns its own sessions
    rather than asking for a firmware: a build server. It is handed a
    context somebody else created and locked, plus the environment that
    context pins, and drives one step of the build-environment
    specification at a time — the tree, the request document, the result
    document, the verdict. The split into ``prepare`` and ``run`` is
    what makes a step cancellable: the sentinel whose existence means
    stop is known before the call that blocks. Everything the build
    methods above do goes through the same code, which is the point:
    what a local build does and what a build server does differ in who
    owns the session, not in what a build is.
``resolve_shutdown_seconds``
    How long stopping a step can take, from the decision to the last
    rung of the liveness ladder — the caller's grace period plus the
    fixed ones. For the caller that has to wait for a build it stopped
    instead of restating those numbers itself. A bound, not a promise.
``open_build_lock`` / ``BuildDirectoryBusy``
    One build directory, one operation at a time. ``build_firmware``
    takes the lock itself, so an embedder gets the guard for free; a
    caller that does more to the same directory — signing after the
    build, flashing what it produced, deleting it — holds it around the
    whole sequence instead, and the nested acquisition inside the build
    then costs nothing. What it keeps out is a *second process* working
    in that directory, which is how a build ends up overwriting the
    image another run is signing or flashing.

Synchrony is a property of each operation here, not of the whole
supported surface. Stages 1-3 are synchronous and CPU-bound (YAML
parsing, mostly), and this is deliberate:
making 40 ms of pure computation awaitable buys nothing against a build
that blocks for minutes, and a synchronous core is what keeps synchronous
embedding possible at all (an ``asyncio.run`` facade over an async core
raises inside a caller that already has a loop). What is made
awaitable is the *waiting* — :func:`build_firmware`, which drives a
subprocess, a container or a socket. So a caller with an event loop
awaits the build directly and offloads one of the synchronous operations
with ``asyncio.to_thread`` when it must.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcuhome.model.errors import (
    BuildError,
    ConfigError,
    ConfigErrorGroup,
    GenerationError,
    Location,
    MCUHomeError,
    error_dicts,
)
from mcuhome.model.export import registry_data
from mcuhome.model.model import MODEL_VERSION, DeviceModel
from mcuhome.model.modelfile import read_model

from mcuhome.workbench import __version__
from mcuhome.workbench.build import (
    BUILD_MODES,
    BUILD_TARGETS,
    DEFAULT_BUILD_MODE,
    DEFAULT_BUILD_TARGET,
    DEFAULT_MAX_WAIT_SECONDS,
    MODE_CONTAINER,
    MODE_SUBPROCESS,
    TARGET_LOCAL,
    TARGET_REMOTE,
    BuildOptions,
    BuildRequest,
    BuildResult,
    RemoteNotConfigured,
    UnknownBuildMode,
    UnknownBuildTarget,
    build_firmware,
    build_options,
    resolve_build_mode,
    resolve_build_target,
)
from mcuhome.workbench.buildenvsession import (
    BuilderSession,
    CacheTier,
    EnvironmentUnavailable,
    EnvironmentUnusable,
    Step,
    StepResult,
)
from mcuhome.workbench.builders import Builder, SelectedBuilder
from mcuhome.workbench.buildlock import BuildDirectoryBusy, open_build_lock
from mcuhome.workbench.buildprocess import resolve_shutdown_seconds
from mcuhome.workbench.buildtarget import (
    BuildTarget,
    ContainerExecution,
    Execution,
    LocalBuild,
    RemoteBuild,
    SubprocessExecution,
)
from mcuhome.workbench.configschema import config_json_schema
from mcuhome.workbench.configuration import (
    CONFIG_FILE,
    CONFIG_SCOPES,
    OPTION_KINDS,
    OPTIONS,
    Argument,
    Option,
    ProgramDefaults,
    Setting,
    Settings,
    option,
    resolve_builder,
    resolve_settings,
    scope_config_file,
    set_config_value,
    unset_config_value,
)
from mcuhome.workbench.generate import CompilerUnavailable, generate_tree
from mcuhome.workbench.loader import load_config
from mcuhome.workbench.migrations import Migration, plan_upgrade
from mcuhome.workbench.packagefetch import SdkUnavailable
from mcuhome.workbench.project import (
    BUILD_DIR,
    DEVICE_FILE,
    DEVICES_DIR,
    PROJECT_CONFIG_FILE,
    PROJECT_MARKER_FILE,
    PROJECT_VERSION,
    InitResult,
    Project,
    create_project,
    find_project_root,
    is_project_root,
    is_upgrading,
    read_project,
    resolve_device,
    resolve_project,
)
from mcuhome.workbench.projectfile import (
    UPGRADE_MARKER_FILE,
    ProjectFile,
    ProjectFileError,
    ProjectUpgradeRequired,
    ProjectVersionUnsupported,
)
from mcuhome.workbench.projectupgrade import (
    MigrationFailed,
    RunningBuild,
    UpgradeInProgress,
    UpgradeInterrupted,
    UpgradeResult,
    UpgradeSession,
    find_running_builds,
    open_upgrade_session,
)
from mcuhome.workbench.provision import PairingResult, init_pairing
from mcuhome.workbench.resolve import resolve
from mcuhome.workbench.scaffold import (
    BusChoice,
    ClusterChoice,
    DeviceOutline,
    EndpointChoice,
    NewDevice,
    PeripheralChoice,
    new_device,
    render_starter,
)
from mcuhome.workbench.schema import parse_config
from mcuhome.workbench.validate import validate

__all__ = [
    "BUILD_DIR",
    "BuildDirectoryBusy",
    "BuildError",
    "BuilderSession",
    "BuildOptions",
    "BUILD_MODES",
    "BUILD_TARGETS",
    "BuildRequest",
    "BuildResult",
    "BuildTarget",
    "Builder",
    "BusChoice",
    "Argument",
    "CONFIG_FILE",
    "CONFIG_SCOPES",
    "CacheTier",
    "ClusterChoice",
    "CompilerUnavailable",
    "ConfigError",
    "ConfigErrorGroup",
    "ContainerExecution",
    "DEFAULT_MAX_WAIT_SECONDS",
    "DEFAULT_BUILD_MODE",
    "DEFAULT_BUILD_TARGET",
    "DEVICES_DIR",
    "DEVICE_FILE",
    "DeviceModel",
    "DeviceOutline",
    "EndpointChoice",
    "EnvironmentUnavailable",
    "EnvironmentUnusable",
    "Execution",
    "GenerationError",
    "InitResult",
    "LocalBuild",
    "Location",
    "PROJECT_MARKER_FILE",
    "MCUHomeError",
    "MODE_CONTAINER",
    "MODE_SUBPROCESS",
    "TARGET_LOCAL",
    "TARGET_REMOTE",
    "MODEL_VERSION",
    "Migration",
    "MigrationFailed",
    "NewDevice",
    "OPTIONS",
    "OPTION_KINDS",
    "Option",
    "ProgramDefaults",
    "PROJECT_CONFIG_FILE",
    "PROJECT_VERSION",
    "PairingResult",
    "PeripheralChoice",
    "Project",
    "ProjectFile",
    "ProjectFileError",
    "ProjectUpgradeRequired",
    "ProjectVersionUnsupported",
    "RemoteBuild",
    "RemoteNotConfigured",
    "SubprocessExecution",
    "RunningBuild",
    "SdkUnavailable",
    "SelectedBuilder",
    "Setting",
    "Settings",
    "Step",
    "StepResult",
    "UPGRADE_MARKER_FILE",
    "UnknownBuildMode",
    "UnknownBuildTarget",
    "UpgradeInProgress",
    "UpgradeInterrupted",
    "UpgradeResult",
    "UpgradeSession",
    "VERSION",
    "ValidationResult",
    "build_firmware",
    "build_options",
    "open_build_lock",
    "config_json_schema",
    "error_dicts",
    "find_device",
    "find_project_root",
    "generate_tree",
    "init_pairing",
    "create_project",
    "is_project_root",
    "is_upgrading",
    "load_model",
    "new_device",
    "option",
    "read_project",
    "read_model",
    "registry_data",
    "render_starter",
    "resolve_builder",
    "resolve_build_mode",
    "resolve_build_target",
    "resolve_project",
    "resolve_settings",
    "resolve_shutdown_seconds",
    "find_running_builds",
    "scope_config_file",
    "set_config_value",
    "unset_config_value",
    "plan_upgrade",
    "open_upgrade_session",
    "validate_device",
]

#: The workbench's own version, for a consumer that declares a supported
#: range — deliberately not the model's, which versions with the SDK
#: repository.
VERSION = __version__


def find_device(
    spec: str,
    *,
    env: Mapping[str, str],
    cwd: Path,
    project_dir: Path | None = None,
) -> tuple[Project, Path]:
    """Resolve a device name or path to its project and its entry file.

    The same resolution the CLI's ``<device>`` argument gets: a folder
    name under the project's ``devices/``, or an explicit path to a
    device folder or a YAML file. The project itself comes from the
    bootstrap ladder — *project_dir* first,
    ``MCUHOME_PROJECT_DIR`` in *env* second, the upward marker search
    from *cwd* last.
    """
    return resolve_device(spec, env=env, cwd=cwd, project_dir=project_dir)


def load_model(
    entry: Path,
    *,
    project: Project,
    on_warning: Callable[[str], None] | None = None,
) -> DeviceModel:
    """Run stages 1-3 on one device configuration: load, validate, resolve.

    The result is the canonical device model (builder-pipeline.md §1.2) —
    the single representation between YAML and every generator, and the
    wire format of a remote build. Raises :class:`ConfigError` for a
    single problem and :class:`ConfigErrorGroup` when validation found
    several; :func:`validate_device` is the same work without the raise.
    *on_warning* receives the non-fatal findings of the run — today the
    secrets-file permission warning.
    """
    data = load_config(entry, secrets_file=project.secrets_file, on_warning=on_warning)
    config = parse_config(data, file=entry)
    validate(config)
    return resolve(config)


# read_model moved to mcuhome.model.modelfile (still re-exported here,
# this module stays the supported surface): the SDK entry point reads the
# model inside the build container, whose runtime is a bare interpreter
# with no third-party packages available — so the reader lives in the
# package that is dependency-free by construction, not behind this
# module's YAML imports.


@dataclass(frozen=True)
class ValidationResult:
    """The outcome of checking one configuration, problems and all."""

    entry: Path
    project: Project
    #: The resolved model, or None when the configuration was rejected.
    model: DeviceModel | None
    #: Every problem found, in file order. Empty exactly when *model* is
    #: not None. Almost always :class:`ConfigError` instances — the wider
    #: type is for the errors that have no place in a file to point at,
    #: which are still user-facing messages and still serialize the same
    #: way.
    errors: tuple[MCUHomeError, ...]

    @property
    def ok(self) -> bool:
        return self.model is not None

    def error_dicts(self) -> list[dict[str, Any]]:
        """The problems as dictionaries, with paths relative to the project."""
        return [error.to_dict(root=self.project.root) for error in self.errors]

    def raise_errors(self) -> None:
        """Raise what :func:`validate_device` caught, for a caller that wants it.

        The bridge back to the raising style: a command line renders an
        exception, a program inspects a result, and both should be able
        to use the same call. Does nothing when there is nothing wrong.
        """
        if not self.errors:
            return
        if len(self.errors) == 1:
            raise self.errors[0]
        raise ConfigErrorGroup([error for error in self.errors if isinstance(error, ConfigError)])

    def to_dict(self) -> dict[str, Any]:
        """The whole result as JSON-ready data — what ``-o json`` prints."""
        return {
            "ok": self.ok,
            "file": _relative(self.entry, self.project.root),
            "errors": self.error_dicts(),
            "model": None if self.model is None else self.model.to_dict(),
        }


def _relative(path: Path, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except (ValueError, OSError):
        return str(path)


def validate_device(
    entry: Path,
    *,
    project: Project,
    on_warning: Callable[[str], None] | None = None,
) -> ValidationResult:
    """Stages 1-3, reporting every problem instead of raising.

    Validation deliberately does not stop at the first error, and this is
    the entry point that keeps it that way for a caller: one pass over
    the configuration, every marker at once. Errors arrive as the builder
    raised them — typed, located, with their fix hints — so a caller can
    render them or serialize them with
    :meth:`~mcuhome.model.errors.ConfigError.to_dict`.

    An error the builder raised *without* a location (a missing secrets
    file, say) is reported the same way, because to the person reading it
    the difference does not matter.
    """
    try:
        model = load_model(entry, project=project, on_warning=on_warning)
    except ConfigErrorGroup as group:
        return ValidationResult(
            entry=entry, project=project, model=None, errors=tuple(group.errors)
        )
    except MCUHomeError as error:
        return ValidationResult(entry=entry, project=project, model=None, errors=(error,))
    return ValidationResult(entry=entry, project=project, model=model, errors=())
