# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The configuration model: layers, precedence, channels, origins.

Everything here drives :func:`resolve_settings` with a *stated*
environment and throwaway directories: the user layer through
``XDG_CONFIG_HOME`` (which is how the real code finds it too), the
system layer through a monkeypatched :func:`system_config_dir` —
``/etc/mcuhome`` is not writable from a test, and the directory
*location* is a one-line convention while everything worth testing is
what happens with the file once found.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcuhome.model.errors import ConfigError

from mcuhome.workbench import configuration
from mcuhome.workbench.configuration import (
    CONFIG_FILE,
    OPTIONS,
    Settings,
    option,
    resolve_settings,
    system_config_dir,
    user_config_dir,
)
from mcuhome.workbench.project import Project, init_project


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return init_project(tmp_path / "project").project


def user_env(tmp_path: Path) -> dict[str, str]:
    """An environment whose user layer lives in the test's tmp_path."""
    (tmp_path / "xdg" / "mcuhome").mkdir(parents=True, exist_ok=True)
    return {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}


def write_user(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "xdg" / "mcuhome" / CONFIG_FILE
    path.write_text(text, encoding="utf-8")
    return path


def write_project(project: Project, text: str) -> Path:
    project.config_file.write_text(text, encoding="utf-8")
    return project.config_file


# --- the registry is the single source of every spelling --------------


def test_the_spellings_derive_from_the_declaration() -> None:
    declared = option("sdk_sources")
    assert declared.env_var == "MCUHOME_SDK_SOURCES"
    assert declared.flag == "--sdk-sources"


def test_the_bootstrap_options_are_declared_but_stand_outside() -> None:
    declared = option("project_dir")
    assert declared.bootstrap
    assert not declared.files
    assert declared.env_var == "MCUHOME_PROJECT_DIR"


def test_an_undeclared_option_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        option("does_not_exist")


def test_every_option_kind_is_one_the_parsers_know() -> None:
    assert {declared.kind for declared in OPTIONS} <= {
        "string",
        "path",
        "paths",
        "integer",
        "builders",
    }


# --- defaults and the five layers -------------------------------------


def test_defaults_answer_when_nothing_is_configured(project: Project) -> None:
    settings = resolve_settings(project=project, env={})
    assert settings.value("jobs") == 1
    assert settings.origin("jobs") == "default"
    assert settings.setting("jobs").source is None


def test_the_user_layer_beats_the_default(tmp_path: Path, project: Project) -> None:
    env = user_env(tmp_path)
    file = write_user(tmp_path, "jobs: 3\n")
    settings = resolve_settings(project=project, env=env)
    assert settings.value("jobs") == 3
    assert settings.origin("jobs") == "user"
    assert settings.setting("jobs").source == str(file)


def test_the_project_layer_beats_the_user_layer(tmp_path: Path, project: Project) -> None:
    env = user_env(tmp_path)
    write_user(tmp_path, "jobs: 3\n")
    write_project(project, "jobs: 5\n")
    settings = resolve_settings(project=project, env=env)
    assert settings.value("jobs") == 5
    assert settings.origin("jobs") == "project"


def test_the_environment_beats_every_file(tmp_path: Path, project: Project) -> None:
    env = user_env(tmp_path) | {"MCUHOME_JOBS": "7"}
    write_user(tmp_path, "jobs: 3\n")
    write_project(project, "jobs: 5\n")
    settings = resolve_settings(project=project, env=env)
    assert settings.value("jobs") == 7
    assert settings.origin("jobs") == "environment"
    assert settings.setting("jobs").source == "MCUHOME_JOBS"


def test_the_command_line_beats_the_environment(project: Project) -> None:
    settings = resolve_settings(project=project, env={"MCUHOME_JOBS": "7"}, args={"jobs": 2})
    assert settings.value("jobs") == 2
    assert settings.origin("jobs") == "arguments"
    assert settings.setting("jobs").source == "--jobs"


def test_the_system_layer_is_the_lowest_file(
    tmp_path: Path, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    system = tmp_path / "etc" / "mcuhome"
    system.mkdir(parents=True)
    (system / CONFIG_FILE).write_text("jobs: 9\n", encoding="utf-8")
    monkeypatch.setattr(configuration, "system_config_dir", lambda env: system)
    env = user_env(tmp_path)
    settings = resolve_settings(project=project, env=env)
    assert settings.value("jobs") == 9
    assert settings.origin("jobs") == "system"
    write_user(tmp_path, "jobs: 3\n")
    assert resolve_settings(project=project, env=env).value("jobs") == 3


def test_outside_a_project_the_project_layer_is_simply_absent(tmp_path: Path) -> None:
    settings = resolve_settings(project=None, env={})
    assert settings.value("jobs") == 1


def test_an_environment_without_a_home_has_no_user_layer(project: Project) -> None:
    """A service account is a normal caller, not a broken one."""
    settings = resolve_settings(project=project, env={})
    assert settings.origin("jobs") == "default"


def test_an_empty_environment_value_sets_nothing(project: Project) -> None:
    settings = resolve_settings(project=project, env={"MCUHOME_JOBS": ""})
    assert settings.origin("jobs") == "default"


def test_an_empty_configuration_file_is_an_empty_layer(tmp_path: Path, project: Project) -> None:
    write_project(project, "# nothing decided yet\n")
    settings = resolve_settings(project=project, env={})
    assert settings.origin("jobs") == "default"


# --- value parsing, per channel ---------------------------------------


def test_paths_from_a_file_are_relative_to_that_file(project: Project) -> None:
    write_project(project, "sdk_sources:\n  - ./packages\n")
    settings = resolve_settings(project=project, env={})
    assert settings.value("sdk_sources") == (project.root / "packages",)


def test_paths_from_the_environment_split_like_PATH(project: Project) -> None:
    env = {"MCUHOME_SDK_SOURCES": "/a:/b:"}
    settings = resolve_settings(project=project, env=env)
    assert settings.value("sdk_sources") == (Path("/a"), Path("/b"))


def test_a_tilde_in_a_path_uses_the_stated_home(tmp_path: Path, project: Project) -> None:
    env = {"HOME": str(tmp_path / "home"), "MCUHOME_SDK_SOURCES": "~/pkgs"}
    settings = resolve_settings(project=project, env=env)
    assert settings.value("sdk_sources") == (tmp_path / "home" / "pkgs",)


def test_a_single_string_where_a_list_belongs_is_explained(project: Project) -> None:
    write_project(project, "sdk_sources: ./packages\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "must be a list of paths" in caught.value.message
    assert "- path" in caught.value.message


def test_a_word_where_a_number_belongs_is_located(project: Project) -> None:
    write_project(project, "# a comment first\njobs: four\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "'jobs' must be a whole number" in caught.value.message
    assert caught.value.location is not None
    assert caught.value.location.line == 2


def test_a_boolean_is_not_a_whole_number(project: Project) -> None:
    write_project(project, "jobs: true\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "'jobs' must be a whole number" in caught.value.message


def test_environment_rubbish_names_the_variable(project: Project) -> None:
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={"MCUHOME_JOBS": "vier"})
    assert caught.value.message == "MCUHOME_JOBS must be a whole number, not 'vier'."


# --- channel rules ----------------------------------------------------


def test_an_unknown_key_lists_what_a_file_may_set(project: Project) -> None:
    write_project(project, "jobz: 4\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert caught.value.message == "There is no option called 'jobz'."
    hint = caught.value.hint or ""
    assert "jobs" in hint
    assert "sdk_sources" in hint
    assert "signing_key" not in hint  # not settable from files
    assert "project_dir" not in hint  # bootstrap


def test_a_file_cannot_set_a_bootstrap_option(project: Project) -> None:
    write_project(project, "project_dir: /elsewhere\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "'project_dir' cannot be set from a configuration file" in caught.value.message
    hint = caught.value.hint or ""
    assert "before any configuration file is read" in hint
    assert "--project-dir" in hint
    assert "MCUHOME_PROJECT_DIR" in hint


def test_a_file_cannot_set_a_per_invocation_option(project: Project) -> None:
    write_project(project, "signing_key: /some/key\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "'signing_key' cannot be set from a configuration file" in caught.value.message
    hint = caught.value.hint or ""
    assert "--signing-key" in hint
    assert "MCUHOME_SIGNING_KEY" in hint


def test_a_configuration_file_must_be_a_mapping(project: Project) -> None:
    write_project(project, "- a list\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "must be a mapping of `option: value` pairs" in caught.value.message


def test_arguments_for_undeclared_or_bootstrap_names_are_programming_errors(
    project: Project,
) -> None:
    with pytest.raises(ValueError):
        resolve_settings(project=project, env={}, args={"no_such": 1})
    with pytest.raises(ValueError):
        resolve_settings(project=project, env={}, args={"project_dir": "x"})


# --- config print -----------------------------------------------------


def test_print_data_shows_every_value_with_its_origin(tmp_path: Path, project: Project) -> None:
    env = user_env(tmp_path) | {"MCUHOME_JOBS": "7"}
    write_user(tmp_path, "sdk_sources:\n  - /pkgs\n")
    data = resolve_settings(project=project, env=env).print_data()
    assert data["jobs"] == {"value": 7, "origin": "environment", "source": "MCUHOME_JOBS"}
    assert data["sdk_sources"]["value"] == ["/pkgs"]  # JSON-ready, not Path
    assert data["sdk_sources"]["origin"] == "user"
    assert "project_dir" not in data  # bootstrap options are not settings


def test_settings_refuse_undeclared_names() -> None:
    with pytest.raises(ValueError):
        Settings({}).value("jobs")


# --- the layer directories --------------------------------------------


def test_the_posix_layer_directories_follow_the_conventions(tmp_path: Path) -> None:
    assert system_config_dir({}) == Path("/etc/mcuhome")
    assert user_config_dir({"XDG_CONFIG_HOME": str(tmp_path)}) == tmp_path / "mcuhome"
    assert user_config_dir({}) is None


# --- writing configuration (config set/unset, ADR 0022 §3) ------------


def test_set_writes_a_value_the_next_resolve_reads_back(project: Project) -> None:
    file = configuration.scope_config_file("project", project=project, env={})
    written = configuration.set_config_value(file, "jobs", "4", env={})
    assert written == 4
    resolved = resolve_settings(project=project, env={})
    assert resolved.value("jobs") == 4
    assert resolved.origin("jobs") == "project"


def test_set_preserves_comments_and_neighboring_keys(project: Project) -> None:
    write_project(project, "# my project\nsdk_sources:\n  - /pkgs  # pinned packages\n")
    configuration.set_config_value(project.config_file, "jobs", "2", env={})
    text = project.config_file.read_text(encoding="utf-8")
    assert "# my project" in text
    assert "# pinned packages" in text
    assert "jobs: 2" in text


def test_set_creates_the_file_and_its_directory(tmp_path: Path) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp_path / "fresh-xdg")}
    file = configuration.scope_config_file("user", project=None, env=env)
    configuration.set_config_value(file, "default_builder", "attic", env=env)
    assert file.is_file()
    assert "default_builder: attic" in file.read_text(encoding="utf-8")


def test_set_splits_a_paths_value_like_the_environment_does(project: Project) -> None:
    configuration.set_config_value(project.config_file, "sdk_sources", "/a:relative/b", env={})
    resolved = resolve_settings(project=project, env={})
    values = resolved.value("sdk_sources")
    assert values[0] == Path("/a")
    # The user's spelling is written; the file's own rule resolves it on read.
    assert values[1] == (project.root / "relative/b").resolve()
    assert "- relative/b" in project.config_file.read_text(encoding="utf-8")


def test_set_validates_the_value_before_touching_the_file(project: Project) -> None:
    write_project(project, "jobs: 2\n")
    before = project.config_file.read_text(encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        configuration.set_config_value(project.config_file, "jobs", "vier", env={})
    assert "whole number" in caught.value.message
    assert project.config_file.read_text(encoding="utf-8") == before


def test_set_refuses_an_undeclared_name_with_the_settable_list(project: Project) -> None:
    with pytest.raises(ConfigError) as caught:
        configuration.set_config_value(project.config_file, "jobz", "4", env={})
    assert caught.value.message == "There is no option called 'jobz'."
    assert "jobs" in (caught.value.hint or "")


def test_set_refuses_the_channels_a_file_may_not_carry(project: Project) -> None:
    with pytest.raises(ConfigError) as caught:
        configuration.set_config_value(project.config_file, "signing_key", "/k", env={})
    assert "'signing_key' cannot be set from a configuration file" in caught.value.message
    with pytest.raises(ConfigError) as caught:
        configuration.set_config_value(project.config_file, "project_dir", "/p", env={})
    assert "'project_dir' cannot be set from a configuration file" in caught.value.message


def test_set_refuses_builders_toward_the_file_itself(project: Project) -> None:
    with pytest.raises(ConfigError) as caught:
        configuration.set_config_value(project.config_file, "builders", "attic", env={})
    assert "structured configuration" in caught.value.message
    assert "builders:" in (caught.value.hint or "")


def test_set_refuses_an_empty_value_toward_unset(project: Project) -> None:
    with pytest.raises(ConfigError) as caught:
        configuration.set_config_value(project.config_file, "jobs", "", env={})
    assert "mcuhome config unset jobs" in (caught.value.hint or "")


def test_set_refuses_a_file_that_is_not_a_mapping(project: Project) -> None:
    write_project(project, "- a list\n")
    with pytest.raises(ConfigError) as caught:
        configuration.set_config_value(project.config_file, "jobs", "4", env={})
    assert "must be a mapping" in caught.value.message


def test_unset_removes_the_key_and_says_whether_it_did(project: Project) -> None:
    write_project(project, "# keep me\njobs: 4\ndefault_builder: attic\n")
    assert configuration.unset_config_value(project.config_file, "jobs") is True
    text = project.config_file.read_text(encoding="utf-8")
    assert "jobs" not in text
    assert "# keep me" in text
    assert "default_builder: attic" in text
    assert configuration.unset_config_value(project.config_file, "jobs") is False
    missing = project.root / "nowhere.yaml"
    assert configuration.unset_config_value(missing, "jobs") is False


def test_unset_refuses_a_typo_rather_than_confirming_nothing(project: Project) -> None:
    with pytest.raises(ConfigError) as caught:
        configuration.unset_config_value(project.config_file, "jobz")
    assert caught.value.message == "There is no option called 'jobz'."


def test_scope_files_answer_per_scope(tmp_path: Path, project: Project) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    assert (
        configuration.scope_config_file("project", project=project, env=env) == project.config_file
    )
    assert (
        configuration.scope_config_file("user", project=None, env=env)
        == tmp_path / "xdg" / "mcuhome" / CONFIG_FILE
    )
    assert (
        configuration.scope_config_file("system", project=None, env={})
        == Path("/etc/mcuhome") / CONFIG_FILE
    )


def test_the_project_scope_needs_a_project(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        configuration.scope_config_file("project", project=None, env={})
    assert "no project here" in caught.value.message
    assert "mcuhome init" in (caught.value.hint or "")


def test_an_unnameable_scope_directory_is_a_refusal_when_editing(project: Project) -> None:
    with pytest.raises(ConfigError) as caught:
        configuration.scope_config_file("user", project=project, env={})
    assert "names no user configuration directory" in caught.value.message
    with pytest.raises(ValueError):
        configuration.scope_config_file("galaxy", project=project, env={})
