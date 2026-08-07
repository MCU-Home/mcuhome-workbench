# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The public API surface, and the serialized shape of an error.

:mod:`mcuhome.api` is what a program embedding the builder imports, and
:meth:`mcuhome.errors.ConfigError.to_dict` is what it puts in an editor's
gutter. Both are covered by the SemVer promise, so both are pinned here
by name and by field, not only by behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import EXAMPLES_DIR, VALID_CONFIG

from mcuhome import api
from mcuhome.errors import (
    BuildError,
    ConfigError,
    ConfigErrorGroup,
    Location,
    MCUHomeError,
    error_dicts,
)

EXAMPLE = EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml"

#: Exactly what the serialized form of one error carries. The dashboard's
#: editor addresses a marker by these names (dashboard ADR 0011 decision
#: 4), so adding a field is additive and renaming one is breaking.
ERROR_FIELDS = {"message", "file", "line", "column", "key", "hint", "kind"}


def _tree(path: Path) -> api.ConfigTree:
    return api.ConfigTree(root=path.parent, discovered=False)


# --------------------------------------------------------------------------
# The surface
# --------------------------------------------------------------------------


def test_the_supported_names_are_all_there() -> None:
    """__all__ is the promise; every name in it has to resolve."""
    for name in api.__all__:
        assert hasattr(api, name), name


def test_the_version_is_the_package_version() -> None:
    from mcuhome import __version__

    assert __version__ == api.VERSION


def test_load_model_runs_stages_one_to_three(tmp_path) -> None:
    entry = tmp_path / "main.yaml"
    entry.write_text(VALID_CONFIG, "utf-8")
    model = api.load_model(entry, tree=_tree(entry))
    assert model.device.name == "bench-node"
    assert model.model_version == api.MODEL_VERSION


def test_find_device_resolves_a_name_against_the_tree(tmp_path) -> None:
    (tmp_path / "devices" / "bench-node").mkdir(parents=True)
    (tmp_path / "devices" / "bench-node" / "main.yaml").write_text(VALID_CONFIG, "utf-8")
    tree, entry = api.find_device("bench-node", config_root=tmp_path)
    assert tree.root == tmp_path
    assert entry == tmp_path / "devices" / "bench-node" / "main.yaml"


# --------------------------------------------------------------------------
# validate_device: every problem, no raise
# --------------------------------------------------------------------------


def test_validate_device_reports_a_good_configuration(tmp_path) -> None:
    entry = tmp_path / "main.yaml"
    entry.write_text(VALID_CONFIG, "utf-8")
    result = api.validate_device(entry, tree=_tree(entry))
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
    result = api.validate_device(entry, tree=_tree(entry))
    assert not result.ok
    assert result.model is None
    assert len(result.errors) >= 2
    assert any("nrf99dk" in error.message for error in result.errors)


def test_validate_device_does_not_raise_for_a_broken_file(tmp_path) -> None:
    entry = tmp_path / "main.yaml"
    entry.write_text("device: [this is not a mapping]\n", "utf-8")
    result = api.validate_device(entry, tree=_tree(entry))
    assert not result.ok
    assert result.errors


def test_raise_errors_puts_the_exception_back(tmp_path) -> None:
    entry = tmp_path / "main.yaml"
    entry.write_text(VALID_CONFIG.replace("nrf7002dk/nrf5340/cpuapp", "nrf99dk"), "utf-8")
    result = api.validate_device(entry, tree=_tree(entry))
    with pytest.raises((ConfigError, ConfigErrorGroup)):
        result.raise_errors()


def test_raise_errors_is_silent_when_nothing_is_wrong(tmp_path) -> None:
    entry = tmp_path / "main.yaml"
    entry.write_text(VALID_CONFIG, "utf-8")
    api.validate_device(entry, tree=_tree(entry)).raise_errors()


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
    result = api.validate_device(entry, tree=api.ConfigTree(root=tmp_path, discovered=True))
    data = result.to_dict()
    assert data["ok"] is True
    assert data["file"] == "devices/bench-node/main.yaml"
    assert data["errors"] == []
    assert data["model"]["device"]["name"] == "bench-node"


# --------------------------------------------------------------------------
# Registry and schema, through the API
# --------------------------------------------------------------------------


def test_registry_data_and_schema_are_reachable_from_the_api() -> None:
    assert api.registry_data()["registry_version"] >= 1
    assert api.config_json_schema()["type"] == "object"


def test_the_example_still_resolves_through_the_api() -> None:
    tree, entry = api.find_device(str(EXAMPLE))
    assert api.load_model(entry, tree=tree).device.name == "bmp180-node"
