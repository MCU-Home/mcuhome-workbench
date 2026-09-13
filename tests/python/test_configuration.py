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
    Argument,
    ProgramDefaults,
    Settings,
    option,
    resolve_settings,
    system_config_dir,
    user_config_dir,
)
from mcuhome.workbench.project import Project, create_project


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return create_project(tmp_path / "project").project


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
    declared = option("build.cache_root")
    assert declared.env_var == "MCUHOME_BUILD_CACHE_ROOT"
    assert declared.flag == "--build-cache-root"


def test_a_derived_flag_splits_back_into_its_key() -> None:
    """Reversible because an area is one word: the rule that makes it so."""
    for declared in OPTIONS:
        if not declared.flag:
            continue
        area, _, leaf = declared.flag[2:].partition("-")
        assert declared.area == area
        assert declared.leaf == leaf.replace("-", "_")


def test_a_closed_channel_derives_no_spelling() -> None:
    assert option("build.builder").flag == ""  # selection is --builder, not this key
    assert option("builder").env_var == ""  # the map is files-only
    assert option("registry").flag == ""


def test_the_bootstrap_option_is_declared_but_stands_outside() -> None:
    declared = option("project.dir")
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
        "strings",
        "integer",
        "number",
        "builder",
        "registry",
    }


# --- defaults and the five layers -------------------------------------


def test_defaults_answer_when_nothing_is_configured(project: Project) -> None:
    settings = resolve_settings(project=project, env={})
    assert settings.value("build.sdk_max_bytes") == 2 * 1024**3
    assert settings.origin("build.sdk_max_bytes") == "default"
    assert settings.setting("build.sdk_max_bytes").source is None


def test_the_user_layer_beats_the_default(tmp_path: Path, project: Project) -> None:
    env = user_env(tmp_path)
    file = write_user(tmp_path, "build:\n  sdk_max_bytes: 3\n")
    settings = resolve_settings(project=project, env=env)
    assert settings.value("build.sdk_max_bytes") == 3
    assert settings.origin("build.sdk_max_bytes") == "user"
    assert settings.setting("build.sdk_max_bytes").source == str(file)


def test_the_project_layer_beats_the_user_layer(tmp_path: Path, project: Project) -> None:
    env = user_env(tmp_path)
    write_user(tmp_path, "build:\n  sdk_max_bytes: 3\n")
    write_project(project, "build:\n  sdk_max_bytes: 5\n")
    settings = resolve_settings(project=project, env=env)
    assert settings.value("build.sdk_max_bytes") == 5
    assert settings.origin("build.sdk_max_bytes") == "project"


def test_the_environment_beats_every_file(tmp_path: Path, project: Project) -> None:
    env = user_env(tmp_path) | {"MCUHOME_BUILD_SDK_MAX_BYTES": "7"}
    write_user(tmp_path, "build:\n  sdk_max_bytes: 3\n")
    write_project(project, "build:\n  sdk_max_bytes: 5\n")
    settings = resolve_settings(project=project, env=env)
    assert settings.value("build.sdk_max_bytes") == 7
    assert settings.origin("build.sdk_max_bytes") == "environment"
    assert settings.setting("build.sdk_max_bytes").source == "MCUHOME_BUILD_SDK_MAX_BYTES"


def test_the_command_line_beats_the_environment(project: Project) -> None:
    settings = resolve_settings(
        project=project,
        env={"MCUHOME_BUILD_SDK_MAX_BYTES": "7"},
        args=[Argument("build.sdk_max_bytes", 2)],
    )
    assert settings.value("build.sdk_max_bytes") == 2
    assert settings.origin("build.sdk_max_bytes") == "arguments"
    # Every option settable from the command line derives a flag, so
    # that is the source: the spelling a person could have typed.
    assert settings.setting("build.sdk_max_bytes").source == "--build-sdk-max-bytes"


def test_an_argument_carries_the_spelling_the_tool_used(project: Project) -> None:
    """A tool with a flag of its own is quoted back in the user's words."""
    settings = resolve_settings(
        project=project,
        env={},
        args=[Argument("build.sdk_max_bytes", 2, flag="--limit")],
    )
    assert settings.setting("build.sdk_max_bytes").source == "--limit"


def test_a_program_states_its_own_defaults_below_every_file(
    tmp_path: Path, project: Project
) -> None:
    """A program's own default is visible, and an operator still wins."""
    server = ProgramDefaults("mcuhome-buildserver", {"build.memory": "8g"})
    settings = resolve_settings(project=project, env={}, program=server)
    assert settings.value("build.memory") == "8g"
    assert settings.origin("build.memory") == "program"
    assert settings.setting("build.memory").source == "mcuhome-buildserver"

    write_project(project, "build:\n  memory: 2g\n")
    settings = resolve_settings(project=project, env={}, program=server)
    assert settings.value("build.memory") == "2g"
    assert settings.origin("build.memory") == "project"


def test_the_system_layer_is_the_lowest_file(
    tmp_path: Path, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    system = tmp_path / "etc" / "mcuhome"
    system.mkdir(parents=True)
    (system / CONFIG_FILE).write_text("build:\n  sdk_max_bytes: 9\n", encoding="utf-8")
    monkeypatch.setattr(configuration, "system_config_dir", lambda env: system)
    env = user_env(tmp_path)
    settings = resolve_settings(project=project, env=env)
    assert settings.value("build.sdk_max_bytes") == 9
    assert settings.origin("build.sdk_max_bytes") == "system"
    write_user(tmp_path, "build:\n  sdk_max_bytes: 3\n")
    assert resolve_settings(project=project, env=env).value("build.sdk_max_bytes") == 3


def test_outside_a_project_the_project_layer_is_simply_absent(tmp_path: Path) -> None:
    settings = resolve_settings(project=None, env={})
    assert settings.value("build.sdk_max_bytes") == 2 * 1024**3


def test_an_environment_without_a_home_has_no_user_layer(project: Project) -> None:
    """A service account is a normal caller, not a broken one."""
    settings = resolve_settings(project=project, env={})
    assert settings.origin("build.sdk_max_bytes") == "default"


def test_an_empty_environment_value_sets_nothing(project: Project) -> None:
    settings = resolve_settings(project=project, env={"MCUHOME_BUILD_SDK_MAX_BYTES": ""})
    assert settings.origin("build.sdk_max_bytes") == "default"


def test_an_empty_configuration_file_is_an_empty_layer(tmp_path: Path, project: Project) -> None:
    write_project(project, "# nothing decided yet\n")
    settings = resolve_settings(project=project, env={})
    assert settings.origin("build.sdk_max_bytes") == "default"


# --- value parsing, per channel ---------------------------------------


def test_paths_from_a_file_are_relative_to_that_file(project: Project) -> None:
    write_project(project, "build:\n  sdk_sources:\n    - ./packages\n")
    settings = resolve_settings(project=project, env={})
    assert settings.value("build.sdk_sources") == (project.root / "packages",)


def test_paths_from_the_environment_split_like_PATH(project: Project) -> None:
    env = {"MCUHOME_BUILD_SDK_SOURCES": "/a:/b:"}
    settings = resolve_settings(project=project, env=env)
    assert settings.value("build.sdk_sources") == (Path("/a"), Path("/b"))


def test_a_tilde_in_a_path_uses_the_stated_home(tmp_path: Path, project: Project) -> None:
    env = {"HOME": str(tmp_path / "home"), "MCUHOME_BUILD_SDK_SOURCES": "~/pkgs"}
    settings = resolve_settings(project=project, env=env)
    assert settings.value("build.sdk_sources") == (tmp_path / "home" / "pkgs",)


def test_a_single_string_where_a_list_belongs_is_explained(project: Project) -> None:
    write_project(project, "build:\n  sdk_sources: ./packages\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "must be a list of paths" in caught.value.message
    assert "- path" in caught.value.message


def test_a_word_where_a_number_belongs_is_located(project: Project) -> None:
    write_project(project, "# a comment first\nbuild:\n  sdk_max_bytes: four\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "'build.sdk_max_bytes' must be a whole number" in caught.value.message
    assert caught.value.location is not None
    assert caught.value.location.line == 3


def test_a_boolean_is_not_a_whole_number(project: Project) -> None:
    write_project(project, "build:\n  sdk_max_bytes: true\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "'build.sdk_max_bytes' must be a whole number" in caught.value.message


def test_environment_rubbish_names_the_variable(project: Project) -> None:
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={"MCUHOME_BUILD_SDK_MAX_BYTES": "vier"})
    assert caught.value.message == "MCUHOME_BUILD_SDK_MAX_BYTES must be a whole number, not 'vier'."


# --- channel rules ----------------------------------------------------


def test_an_unknown_key_lists_what_a_file_may_set(project: Project) -> None:
    write_project(project, "jobz: 4\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert caught.value.message == "There is no option called 'jobz'."
    hint = caught.value.hint or ""
    assert "build.sdk_max_bytes" in hint
    assert "build.sdk_sources" in hint
    assert "signing.key" not in hint  # not settable from files
    assert "project.dir" not in hint  # bootstrap


def test_a_file_cannot_set_a_bootstrap_option(project: Project) -> None:
    write_project(project, "project:\n  dir: /elsewhere\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "'project.dir' cannot be set from a configuration file" in caught.value.message
    hint = caught.value.hint or ""
    assert "before any configuration file is read" in hint
    assert "--project-dir" in hint
    assert "MCUHOME_PROJECT_DIR" in hint


def test_a_file_cannot_set_a_per_invocation_option(project: Project) -> None:
    write_project(project, "signing:\n  key: /some/key\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "'signing.key' cannot be set from a configuration file" in caught.value.message
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
        resolve_settings(project=project, env={}, args=[Argument("no_such", 1)])
    with pytest.raises(ValueError):
        resolve_settings(project=project, env={}, args=[Argument("project.dir", "x")])


# --- config print -----------------------------------------------------


def test_the_document_shows_every_value_with_its_origin(tmp_path: Path, project: Project) -> None:
    env = user_env(tmp_path) | {"MCUHOME_BUILD_SDK_MAX_BYTES": "7"}
    write_user(tmp_path, "build:\n  sdk_sources:\n    - /pkgs\n")
    data = resolve_settings(project=project, env=env).to_dict()
    assert data["build.sdk_max_bytes"] == {
        "value": 7,
        "origin": "environment",
        "source": "MCUHOME_BUILD_SDK_MAX_BYTES",
    }
    assert data["build.sdk_sources"]["value"] == ["/pkgs"]  # JSON-ready, not Path
    assert data["build.sdk_sources"]["origin"] == "user"
    assert "project.dir" not in data  # the bootstrap option is not a setting


def test_settings_refuse_undeclared_names() -> None:
    with pytest.raises(ValueError):
        Settings({}).value("jobs")


# --- the layer directories --------------------------------------------


def test_the_posix_layer_directories_follow_the_conventions(tmp_path: Path) -> None:
    assert system_config_dir({}) == Path("/etc/mcuhome")
    assert user_config_dir({"XDG_CONFIG_HOME": str(tmp_path)}) == tmp_path / "mcuhome"
    assert user_config_dir({}) is None


def test_the_stated_environment_decides_the_system_layer_too(tmp_path: Path) -> None:
    """A resolution answers about the environment it was given, not about
    the machine the process runs on — the system layer included."""
    stated = {"XDG_CONFIG_DIRS": f"{tmp_path / 'first'}:{tmp_path / 'second'}"}
    # The first entry is the most important one, which is the only one a
    # single system layer can be.
    assert system_config_dir(stated) == tmp_path / "first" / "mcuhome"
    assert system_config_dir({"XDG_CONFIG_DIRS": ""}) == Path("/etc/mcuhome")
    assert system_config_dir({"XDG_CONFIG_DIRS": "~/etc", "HOME": str(tmp_path)}) == (
        tmp_path / "etc" / "mcuhome"
    )


def test_a_stated_system_directory_is_the_layer_that_is_read(
    tmp_path: Path, project: Project
) -> None:
    """End to end: the file under the stated directory is the system layer."""
    directory = tmp_path / "etc" / "mcuhome"
    directory.mkdir(parents=True)
    (directory / CONFIG_FILE).write_text("build:\n  sdk_max_bytes: 8\n", encoding="utf-8")
    env = {"XDG_CONFIG_DIRS": str(tmp_path / "etc")}
    settings = resolve_settings(project=project, env=env)
    assert settings.value("build.sdk_max_bytes") == 8
    assert settings.origin("build.sdk_max_bytes") == "system"
    # And an environment that points somewhere empty has no system layer,
    # whatever this machine's own /etc holds.
    empty = {"XDG_CONFIG_DIRS": str(tmp_path / "nothing")}
    assert resolve_settings(project=project, env=empty).origin("build.sdk_max_bytes") == "default"


# --- writing configuration (config set/unset) --------------------------


def test_set_writes_a_value_the_next_resolve_reads_back(project: Project) -> None:
    file = configuration.scope_config_file("project", project=project, env={})
    written = configuration.set_config_value(file, "build.sdk_max_bytes", "4", env={})
    assert written == 4
    resolved = resolve_settings(project=project, env={})
    assert resolved.value("build.sdk_max_bytes") == 4
    assert resolved.origin("build.sdk_max_bytes") == "project"


def test_set_preserves_comments_and_neighboring_keys(project: Project) -> None:
    write_project(project, "# my project\nbuild:\n  sdk_sources:\n    - /pkgs  # pinned packages\n")
    configuration.set_config_value(project.config_file, "build.sdk_max_bytes", "2", env={})
    text = project.config_file.read_text(encoding="utf-8")
    assert "# my project" in text
    assert "# pinned packages" in text
    assert "sdk_max_bytes: 2" in text


def test_set_creates_the_file_and_its_directory(tmp_path: Path) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp_path / "fresh-xdg")}
    file = configuration.scope_config_file("user", project=None, env=env)
    configuration.set_config_value(file, "build.builder", "attic", env=env)
    assert file.is_file()
    assert "builder: attic" in file.read_text(encoding="utf-8")


def test_set_splits_a_paths_value_like_the_environment_does(project: Project) -> None:
    configuration.set_config_value(
        project.config_file, "build.sdk_sources", "/a:relative/b", env={}
    )
    resolved = resolve_settings(project=project, env={})
    values = resolved.value("build.sdk_sources")
    assert values[0] == Path("/a")
    # The user's spelling is written; the file's own rule resolves it on read.
    assert values[1] == (project.root / "relative/b").resolve()
    assert "- relative/b" in project.config_file.read_text(encoding="utf-8")


def test_set_validates_the_value_before_touching_the_file(project: Project) -> None:
    write_project(project, "build:\n  sdk_max_bytes: 2\n")
    before = project.config_file.read_text(encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        configuration.set_config_value(project.config_file, "build.sdk_max_bytes", "vier", env={})
    assert "whole number" in caught.value.message
    assert project.config_file.read_text(encoding="utf-8") == before


def test_set_refuses_an_undeclared_name_with_the_settable_list(project: Project) -> None:
    with pytest.raises(ConfigError) as caught:
        configuration.set_config_value(project.config_file, "jobz", "4", env={})
    assert caught.value.message == "There is no option called 'jobz'."
    assert "build.sdk_max_bytes" in (caught.value.hint or "")


def test_set_refuses_the_channels_a_file_may_not_carry(project: Project) -> None:
    with pytest.raises(ConfigError) as caught:
        configuration.set_config_value(project.config_file, "signing.key", "/k", env={})
    assert "'signing.key' cannot be set from a configuration file" in caught.value.message
    with pytest.raises(ConfigError) as caught:
        configuration.set_config_value(project.config_file, "project.dir", "/p", env={})
    assert "'project.dir' cannot be set from a configuration file" in caught.value.message


def test_set_refuses_a_map_option_toward_the_file_itself(project: Project) -> None:
    with pytest.raises(ConfigError) as caught:
        configuration.set_config_value(project.config_file, "builder", "attic", env={})
    assert "structured configuration" in caught.value.message
    assert "builder:" in (caught.value.hint or "")


def test_set_refuses_an_empty_value_toward_unset(project: Project) -> None:
    with pytest.raises(ConfigError) as caught:
        configuration.set_config_value(project.config_file, "build.sdk_max_bytes", "", env={})
    assert "mcuhome config unset build.sdk_max_bytes" in (caught.value.hint or "")


def test_set_refuses_a_file_that_is_not_a_mapping(project: Project) -> None:
    write_project(project, "- a list\n")
    with pytest.raises(ConfigError) as caught:
        configuration.set_config_value(project.config_file, "build.sdk_max_bytes", "4", env={})
    assert "must be a mapping" in caught.value.message


def test_unset_removes_the_key_and_says_whether_it_did(project: Project) -> None:
    write_project(project, "# keep me\nbuild:\n  sdk_max_bytes: 4\n  builder: attic\n")
    assert configuration.unset_config_value(project.config_file, "build.sdk_max_bytes") is True
    text = project.config_file.read_text(encoding="utf-8")
    assert "sdk_max_bytes" not in text
    assert "# keep me" in text
    assert "builder: attic" in text
    assert configuration.unset_config_value(project.config_file, "build.sdk_max_bytes") is False
    missing = project.root / "nowhere.yaml"
    assert configuration.unset_config_value(missing, "build.sdk_max_bytes") is False


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
    # The system scope follows the same stated environment; where that
    # directory *is* by default is asserted where the directories are
    # (the suite never lets a test read the machine's own /etc).
    assert (
        configuration.scope_config_file(
            "system", project=None, env={"XDG_CONFIG_DIRS": str(tmp_path / "etc")}
        )
        == tmp_path / "etc" / "mcuhome" / CONFIG_FILE
    )


def test_the_project_scope_needs_a_project(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        configuration.scope_config_file("project", project=None, env={})
    assert "no project here" in caught.value.message
    assert "mcuhome project init" in (caught.value.hint or "")


def test_an_unnameable_scope_directory_is_a_refusal_when_editing(project: Project) -> None:
    with pytest.raises(ConfigError) as caught:
        configuration.scope_config_file("user", project=project, env={})
    assert "names no user configuration directory" in caught.value.message
    with pytest.raises(ValueError):
        configuration.scope_config_file("galaxy", project=project, env={})


#: Keys whose unset value is answered by whoever consumes them — the
#: user's cache directory, the interpreter this process runs on, the
#: machine's own cores. None of them may look configured.
DERIVED = (
    "build.cache_root",
    "build.env_store",
    "build.python",
    "build.cpus",
    "build.memory",
)


@pytest.mark.parametrize("name", DERIVED)
def test_a_derived_fallback_is_not_a_declared_default(name: str, project: Project) -> None:
    """`config print` shows these as `default`, with no value of their own.

    A key carrying its consumer's fallback would look configured when
    nobody configured it, and the fallback would then live in two places
    at once.
    """
    settings = resolve_settings(project=project, env={})
    assert settings.value(name) is None
    assert settings.origin(name) == "default"
    entry = settings.to_dict()[name]
    assert entry == {"value": None, "origin": "default", "source": None}
