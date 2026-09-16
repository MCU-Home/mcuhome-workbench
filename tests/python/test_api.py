# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The public API surface, the shape of an error, and the lists behind both.

:mod:`mcuhome.workbench.api` is what a program embedding the builder
imports, and :meth:`mcuhome.model.errors.ConfigError.to_dict` is what it
puts in an editor's gutter. Both are what a consumer writes code against,
so both are pinned here by name and by field, not only by behaviour.

Beside them, the two ``__all__`` rules that keep the surface and the
package from disagreeing about the same name: what ``api`` republishes is
public in the module it comes from, and what one module imports from
another is public there too.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import EXAMPLES_DIR, FIXTURE_TREE, REPO_ROOT, VALID_CONFIG
from mcuhome.model.errors import (
    BuildError,
    ConfigError,
    ConfigErrorGroup,
    Location,
    MCUHomeError,
    error_dicts,
)
from test_buildlock import held_elsewhere

from mcuhome.workbench import api

EXAMPLE = EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml"

#: Exactly what the serialized form of one error carries. The dashboard's
#: editor addresses a marker by these names, so adding a field is
#: additive and renaming one is breaking.
ERROR_FIELDS = {"message", "file", "line", "column", "key", "hint", "kind"}


#: A configuration that reads one secret, so that loading it opens the
#: project's secrets file — which is where the permission warning of
#: :func:`_exposed_secret_project` comes from.
CONFIG_WITH_A_SECRET = VALID_CONFIG.replace(
    "  board:", "  friendly_name: !secret device_label\n  board:"
)


def _project(path: Path) -> api.Project:
    return api.Project(root=path.parent, discovered=False)


def _exposed_secret_project(root: Path) -> api.Project:
    """A project whose ``secrets/main.yaml`` everyone can read."""
    (root / "devices" / "bench-node").mkdir(parents=True)
    project = api.Project(root=root, discovered=True)
    project.secrets_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    project.secrets_file.write_text("device_label: Bench Node\n", "utf-8")
    project.secrets_file.chmod(0o644)
    return project


# --------------------------------------------------------------------------
# The surface
# --------------------------------------------------------------------------


#: Every exported name that is callable — the functions an embedder
#: calls, the classes it constructs. One check each, so a name that goes
#: missing is named by the failure instead of being counted in a loop.
CALLABLES = sorted(name for name in api.__all__ if callable(getattr(api, name, None)))


@pytest.mark.parametrize("name", CALLABLES)
def test_an_exported_callable_is_reachable(name: str) -> None:
    """__all__ is the promise; every name in it has to resolve.

    ``docs/api.md`` and ``tests/python/test_surface.py`` hold the other
    half of this: that the list is the one the reference states, and that
    each of these has the signature it documents.
    """
    assert callable(getattr(api, name))


#: Names of the device-model package that a caller needs to call the
#: workbench or to render what it answered. The rule is re-export, never
#: re-implement: one object under one name, whichever import a caller
#: reaches it through. One row per model module, so a module that is
#: dropped from the surface fails here rather than silently.
MODEL_RE_EXPORTS = (
    ("DeviceModel", "mcuhome.model.model"),
    ("PairingModel", "mcuhome.model.model"),
    ("read_model", "mcuhome.model.modelfile"),
    ("to_json", "mcuhome.model.export"),
    ("Artifact", "mcuhome.model.artifacts"),
    ("BuildLimits", "mcuhome.model.jobs"),
    ("Pairing", "mcuhome.model.pairing"),
    ("random_pairing", "mcuhome.model.pairing"),
    ("OtaImage", "mcuhome.model.ota"),
    ("ota_parameters", "mcuhome.model.ota"),
    ("BOARDS", "mcuhome.model.registry"),
    ("CLUSTERS", "mcuhome.model.registry"),
    ("sha256_file", "mcuhome.model.hashes"),
    ("SDK_PACKAGE_NAME", "mcuhome.model.sdkindex"),
    ("DOCKER_HUB", "mcuhome.model.imageref"),
    ("Reference", "mcuhome.model.imageref"),
    ("context_id", "mcuhome.model.context"),
    ("CONTEXT_FILE", "mcuhome.model.context"),
    ("LABEL_PREFIX", "mcuhome.model.buildenvironment"),
    ("SPEC_GENERATION", "mcuhome.model.buildenvironment"),
    ("Location", "mcuhome.model.errors"),
    ("error_dicts", "mcuhome.model.errors"),
)

#: Names that were on this surface and are not any more. A consumer that
#: still uses one has to fail on the import rather than on a look-alike
#: that happens to be reachable.
RETIRED = (
    "find_device",
    "run_build",
    "build_target_for",
    "options_for",
    "BUILDER_TYPES",
    "PROJECT_DIR_VAR",
    "print_data",
    "registry_data",
    "parse_reference",
    "expand",
    "InitResult",
    "PairingResult",
)


@pytest.mark.parametrize("name, module", MODEL_RE_EXPORTS)
def test_a_model_name_is_re_exported_unchanged(name: str, module: str) -> None:
    """The same object, never a look-alike.

    Two shapes for one thing drift apart, and the model package versions
    with the SDK rather than with the workbench — so a caller that has
    both imports has to be talking about the same object, not about a
    copy this package keeps in step by hand.
    """
    assert getattr(api, name) is getattr(importlib.import_module(module), name)


def test_the_four_renamed_model_names_are_the_same_objects() -> None:
    """Renamed because the bare name says nothing in a flat namespace.

    ``registry`` means a package registry everywhere else on this
    surface, ``parse_reference`` does not say what it parses, and a bare
    ``__version__`` cannot be re-exported unambiguously beside the
    workbench's own. The objects are unchanged; only the spelling here
    is.
    """
    from mcuhome.model.export import registry_data
    from mcuhome.model.imageref import parse_reference

    from mcuhome import model

    assert api.device_registry is registry_data
    assert api.parse_container_reference is parse_reference
    assert model.__version__ == api.MODEL_PACKAGE_VERSION


def test_the_container_address_type_this_surface_answers_is_one_it_exports() -> None:
    """A value a caller receives is never a type it cannot name.

    ``parse_container_reference`` answers one and
    ``ContainerImageMatch.reference`` carries one, so `Reference` is on
    the surface — the same object the device-model package defines, not
    a copy: a caller that pins the model package beside this one has one
    type for one thing.
    """
    reference = api.parse_container_reference(
        "ghcr.io/mcu-home/build-environment:0.1.0", default_registry=api.DOCKER_HUB
    )
    assert isinstance(reference, api.Reference)

    match = api.ContainerImageMatch(
        reference=reference,
        declaration=api.Declaration(
            spec_generation=api.SPEC_GENERATION,
            zephyr_version="4.4.0",
            generator_constraint="",
            packages={},
        ),
        found_under="0.1.0",
    )
    assert isinstance(match.reference, api.Reference)


def test_expand_user_path_takes_its_environment_by_keyword() -> None:
    """The one wrapper on the surface, and what it is for.

    Every other parameter here is keyword-only past the subject of the
    call; the model's own spelling takes the environment positionally.
    The wrapper exists to make the signature obey that rule and for
    nothing else.
    """
    with pytest.raises(TypeError):
        api.expand_user_path("~/thing", {"HOME": "/home/someone"})  # type: ignore[misc]
    assert api.expand_user_path("~/thing", env={"HOME": "/home/someone"}) == Path(
        "/home/someone/thing"
    )


def test_expand_user_path_answers_what_the_model_answers() -> None:
    """A wrapper may change a signature, never what a name does."""
    from mcuhome.model.userpaths import expand

    env = {"HOME": "/home/someone"}
    for path in ("~/thing", "~", "relative/thing", "/absolute/thing"):
        assert api.expand_user_path(path, env=env) == expand(path, env)


@pytest.mark.parametrize("name", RETIRED)
def test_a_retired_name_is_gone_from_the_surface(name: str) -> None:
    assert name not in api.__all__
    assert not hasattr(api, name)


def test_the_version_is_the_package_version() -> None:
    """The API states the workbench's version, not the model's.

    The two version literals are equal today, so equality with the model
    would pass by coincidence — the import is the assertion: the
    workbench versions independently of the SDK repository.
    """
    from mcuhome.workbench import __version__

    assert __version__ == api.VERSION
    assert "from mcuhome.workbench import __version__" in Path(api.__file__).read_text("utf-8")


def test_the_stack_is_answered_by_package_name() -> None:
    """One call for the first line of every bug report.

    Keyed by distribution name, the two imported packages answering with
    their own ``__version__`` — what is running, rather than what some
    metadata file says about it.
    """
    stack = api.stack_versions()

    assert list(stack) == ["mcuhome-workbench", "mcuhome-model", "mcuhome-compiler"]
    assert stack["mcuhome-workbench"] == api.VERSION
    assert stack["mcuhome-model"] == api.MODEL_PACKAGE_VERSION
    assert json.dumps(stack)


def test_a_package_that_is_not_installed_is_the_empty_string(monkeypatch) -> None:
    """A package that is not here says so with a value, not with an absence."""
    import importlib.metadata

    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)

    assert api.stack_versions()["mcuhome-compiler"] == ""


def test_the_stack_does_not_carry_a_consumers_own_version() -> None:
    """A client prints its own version beside this answer, never inside it.

    The workbench cannot know what embeds it, and a client that added
    itself to a document it was given would be assembling one.
    """
    assert "mcuhome-cli" not in api.stack_versions()


def test_the_build_targets_are_part_of_the_surface() -> None:
    """Driving a build is supported, not an implementation detail.

    Re-exported rather than reimplemented — the same objects
    :mod:`mcuhome.workbench.build` defines, so a caller that
    monkeypatches or type-checks against either one is talking about the
    same thing.
    """
    from mcuhome.workbench import build

    exported = (
        "build_firmware",
        "resolve_build_target",
        "BuildRequest",
        "BuildResult",
        "TARGET_LOCAL",
        "TARGET_REMOTE",
        "BUILD_TARGETS",
        "DEFAULT_BUILD_TARGET",
        "UnknownBuildTarget",
        "RemoteNotConfigured",
    )
    for name in exported:
        assert name in api.__all__, name
        assert getattr(api, name) is getattr(build, name), name


def test_creating_a_device_is_part_of_the_surface() -> None:
    """An embedder scaffolds through the API, not through a module.

    The command line reaches into :mod:`mcuhome.workbench.scaffold` and
    :mod:`mcuhome.workbench.provision` directly and is version-locked, so
    it may; a second embedder — the dashboard's new-device wizard — would
    be importing an implementation detail. Same objects, re-exported.
    """
    from mcuhome.workbench import provision, scaffold

    for name in (
        "create_device",
        "render_device_file",
        "NewDevice",
        "DeviceOutline",
        "BusChoice",
        "PeripheralChoice",
        "ClusterChoice",
        "EndpointChoice",
    ):
        assert name in api.__all__, name
        assert getattr(api, name) is getattr(scaffold, name), name

    assert "create_pairing" in api.__all__
    assert api.create_pairing is provision.create_pairing


def test_load_model_runs_stages_one_to_three(tmp_path) -> None:
    entry = tmp_path / "main.yaml"
    entry.write_text(VALID_CONFIG, "utf-8")
    model = api.load_model(entry, project=_project(entry))
    assert model.device.name == "bench-node"
    assert model.model_version == api.MODEL_VERSION


def test_resolve_device_resolves_a_name_against_the_project(tmp_path) -> None:
    api.create_project(tmp_path, force=True)
    (tmp_path / "devices" / "bench-node").mkdir(parents=True)
    (tmp_path / "devices" / "bench-node" / "main.yaml").write_text(VALID_CONFIG, "utf-8")
    project, entry = api.resolve_device("bench-node", cwd=tmp_path, env={})
    assert project.root == tmp_path
    assert entry == tmp_path / "devices" / "bench-node" / "main.yaml"


# --------------------------------------------------------------------------
# validate_device: every problem, no raise
# --------------------------------------------------------------------------


def test_validate_device_reports_a_good_configuration(tmp_path) -> None:
    entry = tmp_path / "main.yaml"
    entry.write_text(VALID_CONFIG, "utf-8")
    result = api.validate_device(entry, project=_project(entry))
    assert result.ok
    assert result.errors == ()
    assert result.model is not None


def test_validate_device_returns_every_problem_at_once(tmp_path) -> None:
    """One pass, all markers — the reason the group type exists."""
    entry = tmp_path / "main.yaml"
    entry.write_text(
        VALID_CONFIG.replace("nrf7002dk/nrf5340/cpuapp", "nrf99dk").replace("baro.temp", "no.such"),
        "utf-8",
    )
    result = api.validate_device(entry, project=_project(entry))
    assert not result.ok
    assert result.model is None
    assert len(result.errors) >= 2
    assert any("nrf99dk" in error.message for error in result.errors)


def test_validate_device_does_not_raise_for_a_broken_file(tmp_path) -> None:
    entry = tmp_path / "main.yaml"
    entry.write_text("device: [this is not a mapping]\n", "utf-8")
    result = api.validate_device(entry, project=_project(entry))
    assert not result.ok
    assert result.errors


def test_raise_errors_puts_the_exception_back(tmp_path) -> None:
    entry = tmp_path / "main.yaml"
    entry.write_text(VALID_CONFIG.replace("nrf7002dk/nrf5340/cpuapp", "nrf99dk"), "utf-8")
    result = api.validate_device(entry, project=_project(entry))
    with pytest.raises((ConfigError, ConfigErrorGroup)):
        result.raise_errors()


def test_raise_errors_is_silent_when_nothing_is_wrong(tmp_path) -> None:
    entry = tmp_path / "main.yaml"
    entry.write_text(VALID_CONFIG, "utf-8")
    api.validate_device(entry, project=_project(entry)).raise_errors()


# --------------------------------------------------------------------------
# The serialized error shape
# --------------------------------------------------------------------------


def test_to_dict_carries_exactly_the_agreed_fields() -> None:
    error = ConfigError(
        "Board is not supported.",
        location=Location(
            file=Path("/tree/devices/x/main.yaml"), line=5, column=10, key="device.board"
        ),
        hint="use a board MCUHome supports",
    )
    data = error.to_dict()
    assert set(data) == ERROR_FIELDS
    assert data["message"] == "Board is not supported."
    assert data["line"] == 5
    assert data["column"] == 10
    assert data["key"] == "device.board"
    assert data["hint"] == "use a board MCUHome supports"
    assert data["kind"] == "ConfigError"


def test_the_file_is_relative_to_the_configuration_tree(tmp_path) -> None:
    """An editor opens a buffer by tree-relative path, never by server path."""
    entry = tmp_path / "devices" / "bench-node" / "main.yaml"
    entry.parent.mkdir(parents=True)
    entry.write_text(VALID_CONFIG, "utf-8")
    error = ConfigError("nope", location=Location(file=entry))
    assert error.to_dict(root=tmp_path)["file"] == "devices/bench-node/main.yaml"
    assert error.to_dict()["file"] == str(entry)


def test_a_path_outside_the_tree_stays_absolute(tmp_path) -> None:
    error = ConfigError("nope", location=Location(file=Path("/elsewhere/main.yaml")))
    assert error.to_dict(root=tmp_path)["file"] == "/elsewhere/main.yaml"


def test_the_kind_tells_error_classes_apart() -> None:
    assert BuildError("no toolchain").to_dict()["kind"] == "BuildError"


def test_an_error_without_a_location_still_serializes() -> None:
    """A BuildError about a missing tool is a message, not a traceback."""
    data = BuildError("gn is not on your PATH.", hint="install it").to_dict()
    assert set(data) == ERROR_FIELDS
    assert data["file"] is None and data["line"] is None
    assert data["hint"] == "install it"


def test_a_bare_builder_error_serializes_through_the_base_class() -> None:
    data = MCUHomeError("something went wrong").to_dict()
    assert set(data) == ERROR_FIELDS
    assert data["message"] == "something went wrong"
    assert data["kind"] == "MCUHomeError"


def test_error_dicts_flattens_a_group() -> None:
    group = ConfigErrorGroup(
        [
            ConfigError("first", location=Location(file=Path("a.yaml"), line=2)),
            ConfigError("second", location=Location(file=Path("a.yaml"), line=1)),
        ]
    )
    dicts = error_dicts(group)
    assert [entry["message"] for entry in dicts] == ["second", "first"]  # file order
    assert all(set(entry) == ERROR_FIELDS for entry in dicts)


def test_error_dicts_of_a_single_error_is_a_list_of_one() -> None:
    assert len(error_dicts(ConfigError("only one"))) == 1


def test_the_validation_result_serializes_whole(tmp_path) -> None:
    entry = tmp_path / "devices" / "bench-node" / "main.yaml"
    entry.parent.mkdir(parents=True)
    entry.write_text(VALID_CONFIG, "utf-8")
    result = api.validate_device(entry, project=api.Project(root=tmp_path, discovered=True))
    data = result.to_dict()
    assert set(data) == {"ok", "file", "diagnostics", "model"}
    assert list(data)[0] == "ok"
    assert data["ok"] is True
    assert data["file"] == "devices/bench-node/main.yaml"
    assert data["diagnostics"] == []
    assert data["model"]["device"]["name"] == "bench-node"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_the_validation_document_holds_errors_and_warnings_in_one_list(tmp_path) -> None:
    """One list, two severities — a client renders it with one code path.

    The whole reason the key is ``diagnostics`` and not ``errors``: a
    warning that arrives beside the errors can be shown where it belongs,
    and nobody has to merge two lists to get the picture.
    """
    project = _exposed_secret_project(tmp_path)
    entry = tmp_path / "devices" / "bench-node" / "main.yaml"
    entry.write_text(CONFIG_WITH_A_SECRET.replace("baro.temp", "no.such"), "utf-8")

    data = api.validate_device(entry, project=project).to_dict()

    assert data["ok"] is False
    severities = {finding["severity"] for finding in data["diagnostics"]}
    assert severities == {"error", "warning"}
    for finding in data["diagnostics"]:
        assert set(finding) == {"severity", *ERROR_FIELDS}
    warning = next(f for f in data["diagnostics"] if f["severity"] == "warning")
    assert warning["kind"] == "exposed_secret_file"
    assert warning["file"] == "secrets/main.yaml"  # relative to the project, like an error
    assert json.dumps(data)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_a_warning_reaches_the_callback_and_stays_in_the_result(tmp_path) -> None:
    """Both halves of the answer: while it happens, and afterwards.

    A caller that only listened would have to keep its own list to render
    the result later, and a caller that only reads the result could not
    say anything while the run is going.
    """
    project = _exposed_secret_project(tmp_path)
    entry = tmp_path / "devices" / "bench-node" / "main.yaml"
    entry.write_text(CONFIG_WITH_A_SECRET, "utf-8")

    heard: list[api.Diagnostic] = []
    result = api.validate_device(entry, project=project, on_warning=heard.append)

    assert result.ok  # a warning is not a rejection
    assert len(heard) == 1
    assert result.warnings == tuple(heard)
    finding = heard[0]
    assert finding.kind in api.WARNING_KINDS
    assert finding.location.file == project.secrets_file
    assert result.errors == ()
    assert result.error_dicts() == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_load_model_reports_the_same_finding(tmp_path) -> None:
    """The raising entry point has the same warning channel."""
    project = _exposed_secret_project(tmp_path)
    entry = tmp_path / "devices" / "bench-node" / "main.yaml"
    entry.write_text(CONFIG_WITH_A_SECRET, "utf-8")

    heard: list[api.Diagnostic] = []
    api.load_model(entry, project=project, on_warning=heard.append)

    assert [finding.kind for finding in heard] == ["exposed_secret_file"]


def test_the_findings_are_in_file_order(tmp_path) -> None:
    """Located findings by file, line and column; the unplaced ones last."""
    project = api.Project(root=tmp_path, discovered=True)
    result = api.ValidationResult(
        entry=tmp_path / "devices" / "bench-node" / "main.yaml",
        project=project,
        model=None,
        errors=(
            ConfigError("late", location=Location(file=tmp_path / "b.yaml", line=2, column=1)),
            BuildError("nowhere in particular"),
            ConfigError("early", location=Location(file=tmp_path / "b.yaml", line=1, column=1)),
        ),
        warnings=(
            api.Diagnostic.warning(
                "first file",
                kind="exposed_secret_file",
                location=Location(file=tmp_path / "a.yaml"),
            ),
        ),
    )

    assert [finding["message"] for finding in result.diagnostics()] == [
        "first file",
        "early",
        "late",
        "nowhere in particular",
    ]


# --------------------------------------------------------------------------
# find_devices: the listing, as one call answers it
# --------------------------------------------------------------------------


def _listed_project(root: Path, **devices: str) -> api.Project:
    """A project with one device folder per entry of *devices*."""
    project = api.create_project(root, force=True).project
    for name, text in devices.items():
        folder = root / "devices" / name
        folder.mkdir(parents=True)
        (folder / "main.yaml").write_text(text, "utf-8")
    return project


def test_find_devices_answers_one_row_per_device_in_name_order(tmp_path) -> None:
    project = _listed_project(
        tmp_path,
        thermostat=VALID_CONFIG.replace("bench-node", "thermostat"),
        attic=VALID_CONFIG.replace("bench-node", "attic"),
    )
    rows = api.find_devices(project)

    assert [row.name for row in rows] == ["attic", "thermostat"]
    assert [row.file for row in rows] == [
        tmp_path / "devices" / "attic" / "main.yaml",
        tmp_path / "devices" / "thermostat" / "main.yaml",
    ]
    assert all(row.ok and row.problems == 0 for row in rows)
    assert {row.board for row in rows} == {"nrf7002dk/nrf5340/cpuapp"}
    assert not any(row.built or row.signed or row.busy for row in rows)


def test_a_device_that_does_not_validate_is_a_row_and_not_a_refusal(tmp_path) -> None:
    """The listing is about every device, so one broken file cannot end it."""
    project = _listed_project(
        tmp_path,
        good=VALID_CONFIG.replace("bench-node", "good"),
        broken=VALID_CONFIG.replace("bench-node", "broken").replace("baro.temp", "no.such"),
    )
    rows = {row.name: row for row in api.find_devices(project)}

    assert rows["good"].ok
    assert not rows["broken"].ok
    assert rows["broken"].problems >= 1
    # And it still says what the device is for: the board is read off the
    # file, not off a model that was never resolved.
    assert rows["broken"].board == "nrf7002dk/nrf5340/cpuapp"


def test_a_device_file_nothing_can_parse_is_a_row_with_no_board(tmp_path) -> None:
    project = _listed_project(tmp_path, wrecked="device: [\n")
    (row,) = api.find_devices(project)

    assert row.name == "wrecked"
    assert not row.ok
    assert row.problems >= 1
    assert row.board == ""


def test_a_project_with_no_devices_is_an_empty_listing(tmp_path) -> None:
    assert api.find_devices(_listed_project(tmp_path)) == ()


def test_the_row_states_what_the_build_directory_holds(tmp_path) -> None:
    project = _listed_project(tmp_path, thermostat=VALID_CONFIG.replace("bench-node", "thermostat"))
    build_dir = project.device_build_dir("thermostat")
    build_dir.mkdir(parents=True)

    (row,) = api.find_devices(project)
    assert not row.built and not row.signed

    (build_dir / api.BUILD_REPORT_FILE).write_text("{}", "utf-8")
    (row,) = api.find_devices(project)
    assert row.built and not row.signed

    (build_dir / "firmware.signed.bin").write_bytes(b"\x00")
    (row,) = api.find_devices(project)
    assert row.built and row.signed


def test_the_row_says_when_somebody_is_working_in_the_build_directory(tmp_path) -> None:
    project = _listed_project(tmp_path, thermostat=VALID_CONFIG.replace("bench-node", "thermostat"))
    build_dir = project.device_build_dir("thermostat")

    with held_elsewhere(build_dir, device="thermostat", operation="build"):
        (row,) = api.find_devices(project)
        assert row.busy

    (row,) = api.find_devices(project)
    assert not row.busy


def test_the_row_is_a_document_with_its_verdict_first(tmp_path) -> None:
    project = _listed_project(tmp_path, thermostat=VALID_CONFIG.replace("bench-node", "thermostat"))
    (row,) = api.find_devices(project)
    document = row.to_dict()

    assert next(iter(document)) == "ok"
    assert document["name"] == "thermostat"
    assert document["file"] == str(tmp_path / "devices" / "thermostat" / "main.yaml")
    assert json.dumps(document)


# --------------------------------------------------------------------------
# Registry and schema, through the API
# --------------------------------------------------------------------------


def test_the_registry_and_the_schema_are_reachable_from_the_api() -> None:
    assert api.device_registry()["registry_version"] >= 1
    assert api.device_schema()["type"] == "object"


def test_the_example_still_resolves_through_the_api() -> None:
    project, entry = api.resolve_device(str(EXAMPLE), cwd=EXAMPLES_DIR, env={})
    assert api.load_model(entry, project=project).device.name == "bmp180-node"


# --------------------------------------------------------------------------
# read_model: the receiving end of the wire format
# --------------------------------------------------------------------------


def test_read_model_round_trips_a_resolved_model(tmp_path) -> None:
    """The model is the wire format, so it has to survive the trip."""
    project, entry = api.resolve_device("bench-node", cwd=FIXTURE_TREE, env={})
    original = api.load_model(entry, project=project)
    path = tmp_path / "device-model.json"
    path.write_text(original.to_json(), encoding="utf-8")

    assert api.read_model(path).to_dict() == original.to_dict()


def test_read_model_refuses_another_model_version(tmp_path) -> None:
    """Negotiated, never guessed."""
    path = tmp_path / "device-model.json"
    path.write_text(json.dumps({"model_version": api.MODEL_VERSION + 1}), encoding="utf-8")
    with pytest.raises(api.BuildError) as error:
        api.read_model(path)
    assert str(api.MODEL_VERSION + 1) in error.value.message
    assert str(api.MODEL_VERSION) in error.value.message
    # And it serializes, because a dashboard renders it rather than
    # printing it.
    assert error.value.to_dict()["message"].startswith("The device model")


def test_the_supported_surface_pulls_in_no_compiler() -> None:
    """The dashboard imports this module, and must not get a toolchain.

    Depending on the package must not drag
    in the toolchain, the C sources or the west manifest. Before this
    boundary work, ``import mcuhome.workbench.api`` reached
    ``manifest`` → ``workspace`` → ``generate``, so every dashboard
    install carried the code generator and the west driver it can never
    run — silently, because nothing failed.

    Measured in a subprocess rather than asserted about the source: an
    import graph is a runtime property, and reading the ``import`` lines
    of one module says nothing about what the modules it imports drag
    along. This is the one property the split exists to establish, and
    without a test it can fall back the first time somebody adds a
    convenience import.

    Since the packages became three distributions the assertion names no
    modules at all: ``mcuhome-compiler`` may not be *installed* in a
    dashboard, so any module of it appearing here is the defect,
    including one that does not exist yet.
    """
    probe = (
        "import sys; import mcuhome.workbench.api; "
        "print(' '.join(sorted(m for m in sys.modules if m.startswith('mcuhome.'))))"
    )
    loaded = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    ).stdout.split()
    compiler = sorted(name for name in loaded if name.startswith("mcuhome.compiler"))
    assert not compiler, (
        f"importing mcuhome.workbench.api now loads {compiler} — a consumer of "
        "the supported surface would need a distribution it can never run"
    )


def _where_api_takes_it_from() -> dict[str, tuple[str, str]]:
    """For every re-exported name: the module it comes from, and its name there.

    Read out of ``api.py`` with :mod:`ast` rather than from the objects,
    because a constant — a string, a tuple — carries no ``__module__``,
    and the constants are half of what the surface republishes.
    """
    source = ast.parse((REPO_ROOT / "mcuhome" / "workbench" / "api.py").read_text("utf-8"))
    taken: dict[str, tuple[str, str]] = {}
    for node in source.body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("mcuhome.workbench"):
            module = node.module.split(".")[-1]  # type: ignore[union-attr]
            for alias in node.names:
                taken[alias.asname or alias.name] = (module, alias.name)
    return taken


def test_every_re_exported_name_is_public_in_the_module_it_comes_from() -> None:
    """The surface republishes; it does not promote.

    Every module here states its own public names in ``__all__``, and a
    name `api` exports out of one of them has to be among them. Where it
    is not, two lists disagree about the same name: the module says
    "internal", the surface says "supported", and the next reader of that
    module moves or renames it in good faith.
    """
    disagreeing = []
    for exported, (module, name) in sorted(_where_api_takes_it_from().items()):
        if exported not in api.__all__:
            continue
        public = getattr(importlib.import_module(f"mcuhome.workbench.{module}"), "__all__", ())
        if name not in public:
            disagreeing.append(f"{module}.{name}")
    assert not disagreeing, f"{disagreeing} are exported by api and not in their module's __all__"


def test_every_name_one_module_takes_from_another_is_public_there() -> None:
    """``__all__`` is a module's statement of what its neighbours may use.

    The package's modules import from each other, and a list that says
    less than the imports do is a list nobody can act on: the next reader
    of a module cannot tell what may be renamed and what three files
    away depend on. Submodules are not names and are skipped — importing
    ``migrations.v2_secrets_layout`` reaches for a file, not for
    something ``migrations`` publishes.
    """
    package = REPO_ROOT / "mcuhome" / "workbench"
    undeclared: list[str] = []
    for source in sorted(package.rglob("*.py")):
        for node in ast.walk(ast.parse(source.read_text("utf-8"))):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if node.level:
                module = node.module
            elif node.module.startswith("mcuhome.workbench."):
                module = node.module.split("mcuhome.workbench.", 1)[1]
            else:
                continue
            if module == "api":
                continue
            imported = importlib.import_module(f"mcuhome.workbench.{module}")
            public = set(getattr(imported, "__all__", ()))
            for alias in node.names:
                if alias.name.startswith("_") or alias.name in public:
                    continue
                if inspect.ismodule(getattr(imported, alias.name, None)):
                    continue
                undeclared.append(f"{module}.{alias.name} (imported by {source.name})")
    assert not undeclared, f"{sorted(undeclared)} are imported across modules and not in __all__"
