# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The supported programmatic surface of the MCUHome workbench.

**This module is the API. Everything else is an implementation detail.**
A consumer imports from ``mcuhome.workbench.api`` and from nothing else
under ``mcuhome.workbench``: names anywhere else in the package may move
between releases without notice, and a program that imports them is on
its own. That includes the ``mcuhome`` command line, which lives in its
own repository (github.com/mcu-home/mcuhome-cli) as a thin shell over
this package and is version-locked to it rather than the other way
around.

``docs/api.md`` in this repository is the reference: every exported name
with its signature, what it raises, the options, the environment
variables, the files it reads and writes, and the documents it answers.
It is the contract rather than a summary — this module's ``__all__`` is
built from its index of exported names and asserted against it, so the
two cannot drift apart.

What is here, in the order a caller needs it: find the project and the
device (``resolve_project``, ``resolve_device``), resolve the
configuration that applies to them (``resolve_settings``,
``resolve_build_options``, ``resolve_builder``), turn a device file into
the canonical model (``load_model``, or ``validate_device`` for every
problem at once instead of the first), build it (``build_firmware``),
and read or sign what the build produced. Upgrading a project, creating
devices and their commissioning credentials, the build-context and
build-environment seams a build server needs, and the package and
container registries a build resolves against are all reachable from
here as well.

The intended consumer is a program that embeds the workbench rather than
running it: the MCUHome dashboard imports it in-process so that a
configuration error arrives in an editor's gutter as a located marker
with a fix hint instead of in a log pane as a line of text. The
dependency has exactly one direction — the dashboard declares the
workbench versions it supports and follows its releases; the workbench
never learns that a dashboard exists.

Names of ``mcuhome.model`` that a caller needs are re-exported here
unchanged, so one import is enough. Four of them are renamed because the
bare name would say nothing in a flat namespace —
``device_registry``, ``parse_container_reference``,
``MODEL_PACKAGE_VERSION`` and ``expand_user_path``, the last a thin
wrapper so that its ``env`` is keyword-only like every other parameter
here. That package versions with the SDK rather than with this one, and
``MODEL_PACKAGE_VERSION`` answers which release is installed while
``MODEL_VERSION`` answers the model format it writes.

Synchrony is a property of each operation, not of the surface:
``build_firmware`` is the only awaitable here, because it is the only
one that waits on a subprocess, a container or a socket. Everything else
is synchronous — 40 ms of YAML parsing made awaitable buys nothing, and
a synchronous core is what keeps synchronous embedding possible at all
— so a caller with an event loop awaits the build and offloads the rest
with ``asyncio.to_thread``. Because the build itself runs in a worker
thread, cancelling the awaiting task does not stop it:
``BuildRequest.should_stop`` does.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcuhome.model import __version__ as MODEL_PACKAGE_VERSION
from mcuhome.model.artifacts import Artifact
from mcuhome.model.buildenvironment import (
    ENVIRONMENT_IMAGE_REPOSITORY,
    LABEL_PREFIX,
    SPEC_GENERATION,
    SPEC_GENERATION_MEMBER,
    Declaration,
    PackageMember,
)
from mcuhome.model.context import (
    BUILD_CONTEXT_FILE,
    CONTEXT_FILE,
    DEVELOPER_ENVIRONMENT,
    KEYS_DIR,
    MANIFEST_FILE,
    MODEL_FILE,
    PATCHES_DIR,
    ContextEnvironment,
    ContextFile,
    ContextManifest,
    ContextRequest,
    DeveloperEnvironment,
    EnvironmentPin,
    GeneratorEntry,
    PackagePin,
    SdkPin,
    context_id,
    format_generator_chain,
)
from mcuhome.model.errors import (
    BuildError,
    ConfigError,
    ConfigErrorGroup,
    GenerationError,
    Location,
    MCUHomeError,
    error_dicts,
)
from mcuhome.model.export import registry_data as device_registry
from mcuhome.model.export import to_json
from mcuhome.model.hashes import sha256_file
from mcuhome.model.imageref import DOCKER_HUB, Reference
from mcuhome.model.imageref import parse_reference as parse_container_reference
from mcuhome.model.jobs import BuildLimits
from mcuhome.model.model import (
    MODEL_VERSION,
    DeviceModel,
    PairingModel,
)
from mcuhome.model.modelfile import read_model
from mcuhome.model.ota import (
    OtaIdentity,
    OtaImage,
    ota_parameters,
)
from mcuhome.model.pairing import (
    Pairing,
    random_pairing,
)
from mcuhome.model.registry import (
    BOARDS,
    CLUSTERS,
    PLANNED_BOARDS,
    BoardDef,
    ClusterDef,
    PartitionDef,
    UpdateSchemeDef,
)
from mcuhome.model.sdkindex import SDK_PACKAGE_NAME
from mcuhome.model.userpaths import expand as _expand

from mcuhome.workbench import __version__
from mcuhome.workbench.build import (
    BUILD_STEPS,
    BuildOptions,
    BuildRequest,
    BuildResult,
    RemoteNotConfigured,
    UnknownBuildMode,
    UnknownBuildTarget,
    build_firmware,
    build_steps,
    create_context,
    resolve_build_mode,
    resolve_build_options,
    resolve_build_target,
)
from mcuhome.workbench.buildenvsession import (
    ACTION_BUILD,
    ARTIFACT_ROLES,
    CACHE_TIERS,
    RESULT_FILE_PREFIX,
    RESULT_FILE_SUFFIX,
    ROOT_OUT,
    STATUS_FAILURE,
    STATUS_SUCCESS,
    STATUS_UNSUPPORTED,
    STEP_STATUSES,
    BuilderSession,
    CacheTier,
    EnvironmentUnavailable,
    EnvironmentUnusable,
    Launcher,
    Step,
    StepResult,
    open_builder_session,
    parse_memory,
    resolve_cache_tiers,
    resolve_host_limits,
)
from mcuhome.workbench.buildenvstore import (
    BuildEnvironmentError,
    StoreEntry,
    provision_environment,
)
from mcuhome.workbench.builders import (
    Builder,
    SelectedBuilder,
)
from mcuhome.workbench.buildlock import (
    BUILD_LOCK_FILE,
    BuildDirectoryBusy,
    is_busy,
    open_build_lock,
)
from mcuhome.workbench.buildprocess import (
    Liveness,
    current_user,
    resolve_shutdown_seconds,
)
from mcuhome.workbench.buildrecord import (
    BuildRecord,
    clean_build,
    read_build,
)
from mcuhome.workbench.buildtarget import (
    BUILD_MODES,
    BUILD_TARGETS,
    DEFAULT_BUILD_MODE,
    DEFAULT_BUILD_TARGET,
    DEFAULT_CONTAINER_PROGRAM,
    DEFAULT_CONTAINER_REPOSITORIES,
    DEFAULT_MAX_WAIT_SECONDS,
    MODE_CONTAINER,
    MODE_SUBPROCESS,
    TARGET_LOCAL,
    TARGET_REMOTE,
    BuildTarget,
    ContainerExecution,
    Execution,
    LocalBuild,
    RemoteBuild,
    SubprocessExecution,
)
from mcuhome.workbench.configschema import device_schema
from mcuhome.workbench.configuration import (
    CONFIG_FILE,
    CONFIG_ORIGINS,
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
    resolve_config_file,
    resolve_settings,
    set_config_value,
    unset_config_value,
)
from mcuhome.workbench.containerbuild import (
    DEFAULT_CONTAINER_PIDS,
    ContainerLimits,
    ContainerRuntime,
    create_launcher,
    ensure_container_image,
    require_container_image,
    require_container_runtime,
    resolve_cache_root,
    resolve_container_program,
)
from mcuhome.workbench.contextdir import (
    ContextFormatVersionError,
    ContextVerification,
    FileMismatch,
    lock_context,
    read_context_facts,
    read_context_manifest,
    read_generator_chain,
    verify_context,
)
from mcuhome.workbench.devworkspace import WORKSPACE_LAYERS
from mcuhome.workbench.diagnostics import (
    SEVERITIES,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    WARNING_KINDS,
    Diagnostic,
)
from mcuhome.workbench.generate import (
    CompilerUnavailable,
    generate_application,
)
from mcuhome.workbench.hostcheck import (
    HOST_CHECKS,
    HostCheckResult,
    HostFinding,
    check_build_host,
)
from mcuhome.workbench.imgtool import (
    BUILD_REPORT_FILE,
    SIGNED_FIRMWARE_NAMES,
    SignedArtifact,
    SigningResult,
    SignPlan,
    plan_signing,
    read_build_report,
    sign_firmware,
)
from mcuhome.workbench.loader import (
    load_config,
    read_yaml_file,
)
from mcuhome.workbench.migrations import (
    Migration,
    plan_upgrade,
)
from mcuhome.workbench.ociregistry import (
    ImageRegistry,
    ImageRegistryError,
    ImageRegistryUnauthorized,
    ImageRegistryUnreachable,
)
from mcuhome.workbench.otafile import ota_file_name, write_ota_image
from mcuhome.workbench.packagefetch import (
    AcquiredPackage,
    SdkUnavailable,
    fetch_sdk_package,
)
from mcuhome.workbench.packageregistry import (
    BUNDLED_ANCHOR_DIR,
    OFFICIAL_BASE_DOMAIN,
    PackageRegistryError,
    RegistrySettings,
    RegistrySource,
    TrustAnchorMissing,
    open_package_registry,
)
from mcuhome.workbench.project import (
    BUILD_DIR,
    DEVICE_FILE,
    DEVICES_DIR,
    PROJECT_CONFIG_FILE,
    PROJECT_MARKER_FILE,
    PROJECT_VERSION,
    NewProject,
    Project,
    create_project,
    find_project_root,
    is_project_root,
    is_upgrading,
    read_project,
    require_secret_file,
    resolve_device,
    resolve_project,
)
from mcuhome.workbench.projectfile import (
    UPGRADE_MARKER_FILE,
    ProjectFile,
    ProjectFileError,
    ProjectUpgradeRequired,
    ProjectVersionUnsupported,
    UpgradeRecord,
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
from mcuhome.workbench.provision import NewPairing, create_pairing, read_pairing
from mcuhome.workbench.resolve import resolve
from mcuhome.workbench.resolve_image import (
    ContainerImageMatch,
    ContainerImagePin,
    parse_container_image,
    resolve_container_image,
)
from mcuhome.workbench.resolve_pins import (
    KIND_SDK,
    KIND_TOOLS,
    KIND_WORKSPACE,
    PACKAGE_KINDS,
    ResolvedPackage,
    resolve_package,
)
from mcuhome.workbench.scaffold import (
    BusChoice,
    ClusterChoice,
    DeviceOutline,
    EndpointChoice,
    NewDevice,
    PeripheralChoice,
    create_device,
    render_device_file,
)
from mcuhome.workbench.schema import parse_config
from mcuhome.workbench.secrets import (
    SECRET_KINDS,
    SecretFile,
    SecretKey,
    SecretScope,
    delete_secret_file,
    find_secret_scopes,
    read_secrets,
    reveal_secret,
    set_secret,
    unset_secret,
)
from mcuhome.workbench.sessionclient import (
    SESSION_VERBS,
    ContextIdMismatch,
    ContextTooLarge,
    PrivateKeyRefused,
    RemoteDependencyMissing,
    RemoteError,
    RemoteTransportError,
    SeatWait,
    ServerRefusal,
    WaitedTooLong,
)
from mcuhome.workbench.signing import (
    PUBLIC_KEY_FILE,
    SIGNING_KEY_FILE,
    SigningKey,
    create_signing_key,
    generate_key_pem,
    is_p256_private_key,
    is_p256_public_key,
    public_key_pem,
    resolve_signing_key,
)
from mcuhome.workbench.validate import validate

#: Every name this module supports, and nothing else. Sorted plainly,
#: which groups the constants, then the types, then the functions;
#: the reading order and what each name is for are in ``docs/api.md``.
__all__ = [
    "ACTION_BUILD",
    "ARTIFACT_ROLES",
    "AcquiredPackage",
    "Argument",
    "Artifact",
    "BOARDS",
    "BUILD_CONTEXT_FILE",
    "BUILD_DIR",
    "BUILD_LOCK_FILE",
    "BUILD_MODES",
    "BUILD_STEPS",
    "BUILD_REPORT_FILE",
    "BUILD_TARGETS",
    "BUNDLED_ANCHOR_DIR",
    "BoardDef",
    "BuildDirectoryBusy",
    "BuildEnvironmentError",
    "BuildError",
    "BuildLimits",
    "BuildOptions",
    "BuildRecord",
    "BuildRequest",
    "BuildResult",
    "BuildTarget",
    "Builder",
    "BuilderSession",
    "BusChoice",
    "CACHE_TIERS",
    "CLUSTERS",
    "CONFIG_FILE",
    "CONFIG_ORIGINS",
    "CONFIG_SCOPES",
    "CONTEXT_FILE",
    "CacheTier",
    "ClusterChoice",
    "ClusterDef",
    "CompilerUnavailable",
    "ConfigError",
    "ConfigErrorGroup",
    "ContainerExecution",
    "ContainerImageMatch",
    "ContainerImagePin",
    "ContainerLimits",
    "ContainerRuntime",
    "ContextEnvironment",
    "ContextFile",
    "ContextFormatVersionError",
    "ContextIdMismatch",
    "ContextManifest",
    "ContextRequest",
    "ContextTooLarge",
    "ContextVerification",
    "DEFAULT_BUILD_MODE",
    "DEFAULT_BUILD_TARGET",
    "DEFAULT_CONTAINER_PIDS",
    "DEFAULT_CONTAINER_PROGRAM",
    "DEFAULT_CONTAINER_REPOSITORIES",
    "DEFAULT_MAX_WAIT_SECONDS",
    "DEVELOPER_ENVIRONMENT",
    "DEVICES_DIR",
    "DEVICE_FILE",
    "DOCKER_HUB",
    "Declaration",
    "DeveloperEnvironment",
    "DeviceModel",
    "DeviceOutline",
    "Diagnostic",
    "ENVIRONMENT_IMAGE_REPOSITORY",
    "EndpointChoice",
    "EnvironmentPin",
    "EnvironmentUnavailable",
    "EnvironmentUnusable",
    "Execution",
    "FileMismatch",
    "GenerationError",
    "GeneratorEntry",
    "HOST_CHECKS",
    "HostCheckResult",
    "HostFinding",
    "ImageRegistry",
    "ImageRegistryError",
    "ImageRegistryUnauthorized",
    "ImageRegistryUnreachable",
    "KEYS_DIR",
    "KIND_SDK",
    "KIND_TOOLS",
    "KIND_WORKSPACE",
    "LABEL_PREFIX",
    "Launcher",
    "Liveness",
    "LocalBuild",
    "Location",
    "MANIFEST_FILE",
    "MCUHomeError",
    "MODEL_FILE",
    "MODEL_PACKAGE_VERSION",
    "MODEL_VERSION",
    "MODE_CONTAINER",
    "MODE_SUBPROCESS",
    "Migration",
    "MigrationFailed",
    "NewDevice",
    "NewPairing",
    "NewProject",
    "OFFICIAL_BASE_DOMAIN",
    "OPTIONS",
    "OPTION_KINDS",
    "Option",
    "OtaIdentity",
    "OtaImage",
    "PACKAGE_KINDS",
    "PATCHES_DIR",
    "PLANNED_BOARDS",
    "PROJECT_CONFIG_FILE",
    "PROJECT_MARKER_FILE",
    "PROJECT_VERSION",
    "PUBLIC_KEY_FILE",
    "PackageMember",
    "PackagePin",
    "PackageRegistryError",
    "Pairing",
    "PairingModel",
    "PartitionDef",
    "PeripheralChoice",
    "PrivateKeyRefused",
    "ProgramDefaults",
    "Project",
    "ProjectFile",
    "ProjectFileError",
    "ProjectUpgradeRequired",
    "ProjectVersionUnsupported",
    "RESULT_FILE_PREFIX",
    "RESULT_FILE_SUFFIX",
    "ROOT_OUT",
    "Reference",
    "RegistrySettings",
    "RegistrySource",
    "RemoteBuild",
    "RemoteDependencyMissing",
    "RemoteError",
    "RemoteNotConfigured",
    "RemoteTransportError",
    "ResolvedPackage",
    "RunningBuild",
    "SDK_PACKAGE_NAME",
    "SECRET_KINDS",
    "SESSION_VERBS",
    "SEVERITIES",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "SIGNED_FIRMWARE_NAMES",
    "SIGNING_KEY_FILE",
    "SPEC_GENERATION",
    "SPEC_GENERATION_MEMBER",
    "STATUS_FAILURE",
    "STATUS_SUCCESS",
    "STATUS_UNSUPPORTED",
    "STEP_STATUSES",
    "SdkPin",
    "SdkUnavailable",
    "SeatWait",
    "SecretFile",
    "SecretKey",
    "SecretScope",
    "SelectedBuilder",
    "ServerRefusal",
    "Setting",
    "Settings",
    "SignPlan",
    "SignedArtifact",
    "SigningKey",
    "SigningResult",
    "Step",
    "StepResult",
    "StoreEntry",
    "SubprocessExecution",
    "TARGET_LOCAL",
    "TARGET_REMOTE",
    "TrustAnchorMissing",
    "UPGRADE_MARKER_FILE",
    "UnknownBuildMode",
    "UnknownBuildTarget",
    "UpdateSchemeDef",
    "UpgradeInProgress",
    "UpgradeInterrupted",
    "UpgradeRecord",
    "UpgradeResult",
    "UpgradeSession",
    "VERSION",
    "ValidationResult",
    "WARNING_KINDS",
    "WORKSPACE_LAYERS",
    "WaitedTooLong",
    "build_firmware",
    "build_steps",
    "check_build_host",
    "clean_build",
    "context_id",
    "create_context",
    "create_device",
    "create_pairing",
    "create_project",
    "create_launcher",
    "create_signing_key",
    "current_user",
    "device_registry",
    "device_schema",
    "delete_secret_file",
    "ensure_container_image",
    "error_dicts",
    "expand_user_path",
    "fetch_sdk_package",
    "find_project_root",
    "find_running_builds",
    "find_secret_scopes",
    "format_generator_chain",
    "generate_application",
    "generate_key_pem",
    "is_busy",
    "is_p256_private_key",
    "is_p256_public_key",
    "is_project_root",
    "is_upgrading",
    "load_model",
    "lock_context",
    "open_build_lock",
    "open_builder_session",
    "open_package_registry",
    "open_upgrade_session",
    "option",
    "ota_file_name",
    "ota_parameters",
    "parse_container_image",
    "parse_container_reference",
    "parse_memory",
    "plan_signing",
    "plan_upgrade",
    "provision_environment",
    "public_key_pem",
    "random_pairing",
    "read_build",
    "read_build_report",
    "read_context_facts",
    "read_context_manifest",
    "read_generator_chain",
    "read_model",
    "read_pairing",
    "read_project",
    "read_secrets",
    "read_yaml_file",
    "render_device_file",
    "require_container_image",
    "require_container_runtime",
    "require_secret_file",
    "resolve_build_mode",
    "resolve_build_options",
    "resolve_build_target",
    "resolve_builder",
    "resolve_cache_root",
    "resolve_cache_tiers",
    "resolve_config_file",
    "resolve_container_image",
    "resolve_container_program",
    "resolve_device",
    "resolve_host_limits",
    "resolve_package",
    "resolve_project",
    "resolve_settings",
    "resolve_shutdown_seconds",
    "resolve_signing_key",
    "reveal_secret",
    "set_config_value",
    "set_secret",
    "sha256_file",
    "sign_firmware",
    "to_json",
    "unset_config_value",
    "unset_secret",
    "validate_device",
    "verify_context",
    "write_ota_image",
]

#: The workbench's own version, for a consumer that declares a supported
#: range — deliberately not the model's, which versions with the SDK
#: repository.
VERSION = __version__


def expand_user_path(path: Path | str, *, env: Mapping[str, str]) -> Path:
    """*path* with a leading ``~`` resolved against *env*.

    The one wrapper on this surface, and only because the parameter rule
    holds for every name here: the model's own spelling takes the
    environment positionally. It answers what that function answers —
    a path without a leading tilde comes back untouched.
    """
    return _expand(path, dict(env))


def load_model(
    entry: Path,
    *,
    project: Project,
    on_warning: Callable[[Diagnostic], None] | None = None,
) -> DeviceModel:
    """Run stages 1-3 on one device configuration: load, validate, resolve.

    The result is the canonical device model (builder-pipeline.md §1.2) —
    the single representation between YAML and every generator, and the
    wire format of a remote build. Raises :class:`ConfigError` for a
    single problem and :class:`ConfigErrorGroup` when validation found
    several; :func:`validate_device` is the same work without the raise.
    *on_warning* receives the non-fatal findings of the run as
    :class:`~mcuhome.workbench.diagnostics.Diagnostic` values — located,
    so a client can show one where the problem is; today the
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
    #: Every non-fatal finding the run reported, in the order it
    #: reported them. A warning does not make a configuration invalid —
    #: :attr:`ok` says nothing about these — but it is part of the same
    #: answer, and a caller that only listened to ``on_warning`` would
    #: have to keep its own list to render one.
    warnings: tuple[Diagnostic, ...] = ()

    @property
    def ok(self) -> bool:
        return self.model is not None

    def error_dicts(self) -> list[dict[str, Any]]:
        """The problems as dictionaries, with paths relative to the project."""
        return [error.to_dict(root=self.project.root) for error in self.errors]

    def diagnostics(self) -> list[dict[str, Any]]:
        """Errors and warnings as **one** list, each carrying its severity.

        The list a client renders. Two lists would make every client
        merge them itself — and the two are the same document apart from
        the severity, so a merge is exactly what nobody should have to
        write twice.

        Order is the file order a person reads in: by file, line and
        column, and findings without a place after the located ones,
        each group in the order it was reported.
        """
        entries = [
            {"severity": SEVERITY_ERROR, **error.to_dict(root=self.project.root)}
            for error in self.errors
        ]
        entries += [warning.to_dict(root=self.project.root) for warning in self.warnings]
        return sorted(entries, key=_in_file_order)

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
            "diagnostics": self.diagnostics(),
            "model": None if self.model is None else self.model.to_dict(),
        }


def _in_file_order(finding: dict[str, Any]) -> tuple[bool, str, int, int]:
    """Sort key for one finding document: where it is, unplaced last."""
    file = finding["file"]
    return (file is None, file or "", finding["line"] or 0, finding["column"] or 0)


def _relative(path: Path, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except (ValueError, OSError):
        return str(path)


def validate_device(
    entry: Path,
    *,
    project: Project,
    on_warning: Callable[[Diagnostic], None] | None = None,
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

    The non-fatal findings are collected as well, so the result carries
    the whole answer: they reach *on_warning* while the run is happening
    **and** stay in :attr:`ValidationResult.warnings` for a caller that
    renders the result afterwards.
    """
    findings: list[Diagnostic] = []

    def collect(finding: Diagnostic) -> None:
        findings.append(finding)
        if on_warning is not None:
            on_warning(finding)

    try:
        model = load_model(entry, project=project, on_warning=collect)
    except ConfigErrorGroup as group:
        return ValidationResult(
            entry=entry,
            project=project,
            model=None,
            errors=tuple(group.errors),
            warnings=tuple(findings),
        )
    except MCUHomeError as error:
        return ValidationResult(
            entry=entry, project=project, model=None, errors=(error,), warnings=tuple(findings)
        )
    return ValidationResult(
        entry=entry, project=project, model=model, errors=(), warnings=tuple(findings)
    )
