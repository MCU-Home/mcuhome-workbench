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
from conftest import line_of
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


def test_an_undeclared_option_is_refused_in_the_words_a_file_is_refused_with() -> None:
    """This is the lookup a person's typing reaches — `mcuhome config get`.

    So it refuses the way a configuration file carrying that key is
    refused, with the same sentence and the same hint, rather than as a
    programming error a client would have to word for the user itself.
    """
    with pytest.raises(ConfigError) as caught:
        option("does_not_exist")
    assert caught.value.message == "There is no option called 'does_not_exist'."
    assert "options settable from a configuration file" in (caught.value.hint or "")

    # A key that was an option once names its successor here too.
    with pytest.raises(ConfigError) as caught:
        option("ccache_dir")
    assert "build.cache_root" in (caught.value.hint or "")


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


# --- retired names ----------------------------------------------------
#
# A configuration file carries no version, so nothing can migrate it: the
# successor is named where the old key is written instead. The variables
# are the deliberate asymmetry — a warning, because a stale export would
# otherwise refuse the command that fixes it.


@pytest.mark.parametrize(
    ("key", "successor", "written"),
    [
        ("builders", "builder", "builders:\n  - name: attic\n    type: remote\n"),
        ("ccache_dir", "build.cache_root", "ccache_dir: /var/cache/ccache\n"),
        ("default_builder", "build.builder", "default_builder: attic\n"),
        ("project_dir", "project.dir", "project_dir: /elsewhere\n"),
        ("signing_key", "signing.key", "signing_key: /keys/mine.pem\n"),
    ],
)
def test_a_retired_key_is_refused_with_the_option_it_is_now(
    project: Project, key: str, successor: str, written: str
) -> None:
    text = f"# my configuration\n{written}"
    file = write_project(project, text)
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})

    assert caught.value.message == f"There is no option called {key!r}."
    assert successor in (caught.value.hint or "")
    assert caught.value.location is not None
    assert caught.value.location.file == file
    assert caught.value.location.line == line_of(text, key)


def test_the_hint_of_a_retired_key_is_the_line_to_write(project: Project) -> None:
    """Not only the name: the shape, so the fix is a copy of the hint."""
    write_project(project, "ccache_dir: /var/cache/ccache\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "build:\n      cache_root:" in (caught.value.hint or "")


def test_a_retired_key_whose_successor_no_file_may_set_says_so(project: Project) -> None:
    """`signing_key` is `signing.key`, and that one is per-invocation."""
    write_project(project, "signing_key: /keys/mine.pem\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    hint = caught.value.hint or ""
    assert "--signing-key" in hint
    assert "MCUHOME_SIGNING_KEY" in hint


def test_the_builder_map_hint_carries_the_renamed_entry_keys(project: Project) -> None:
    """The list became a map, and two of its keys changed with it."""
    write_project(project, "builders:\n  - name: attic\n    type: remote\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    hint = caught.value.hint or ""
    assert "target:" in hint
    assert "container_image:" in hint


def test_every_retired_key_names_a_successor_that_exists() -> None:
    """A hint pointing at a key nobody declares would be worse than none."""
    declared = {opt.name for opt in OPTIONS}
    for key, successor in configuration.RETIRED_OPTIONS.items():
        assert successor in declared, key
        assert key not in declared, f"{key} is retired and declared at the same time"


def test_a_retired_key_is_refused_in_every_file_layer(tmp_path: Path, project: Project) -> None:
    """The user file too — it is the one this is most likely to sit in."""
    env = user_env(tmp_path)
    file = write_user(tmp_path, "ccache_dir: /var/cache/ccache\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=None, env=env)
    assert caught.value.location is not None and caught.value.location.file == file
    assert "build.cache_root" in (caught.value.hint or "")


def test_config_set_refuses_a_retired_name_the_same_way(project: Project) -> None:
    with pytest.raises(ConfigError) as caught:
        configuration.set_config_value(project.config_file, "ccache_dir", "/tmp/x", env={})
    assert caught.value.message == "There is no option called 'ccache_dir'."
    assert "build.cache_root" in (caught.value.hint or "")


@pytest.mark.parametrize(("retired", "successor"), sorted(configuration.RETIRED_VARIABLES.items()))
def test_a_retired_variable_warns_and_does_not_refuse(
    project: Project, retired: str, successor: str
) -> None:
    found: list[object] = []
    settings = resolve_settings(
        project=project, env={retired: "something"}, on_warning=found.append
    )
    assert settings.value("build.target") == "local", "the resolution went through"
    assert len(found) == 1
    warning = found[0]
    assert warning.kind == "retired_environment_variable"
    assert warning.severity == "warning"
    assert retired in warning.message
    assert successor in (warning.hint or "")


def test_a_retired_variable_without_a_channel_is_not_an_error(project: Project) -> None:
    """A caller that takes no findings still gets its settings."""
    settings = resolve_settings(project=project, env={"MCUHOME_DOCKER": "podman"})
    assert settings.value("build.container_program") == "docker", "the old name sets nothing"


def test_nothing_is_warned_about_when_no_retired_variable_is_set(project: Project) -> None:
    found: list[object] = []
    resolve_settings(
        project=project,
        env={"MCUHOME_BUILD_CONTAINER_PROGRAM": "podman"},
        on_warning=found.append,
    )
    assert found == []


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
    file = configuration.resolve_config_file("project", project=project, env={})
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
    file = configuration.resolve_config_file("user", project=None, env=env)
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


def test_set_refuses_a_map_option_toward_its_entry_keys(project: Project) -> None:
    with pytest.raises(ConfigError) as caught:
        configuration.set_config_value(project.config_file, "builder", "attic", env={})
    assert "is a map of entries and not settable as one value" in caught.value.message
    assert "mcuhome config set builder.<name>.target <value>" in (caught.value.hint or "")


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
        configuration.resolve_config_file("project", project=project, env=env)
        == project.config_file
    )
    assert (
        configuration.resolve_config_file("user", project=None, env=env)
        == tmp_path / "xdg" / "mcuhome" / CONFIG_FILE
    )
    # The system scope follows the same stated environment; where that
    # directory *is* by default is asserted where the directories are
    # (the suite never lets a test read the machine's own /etc).
    assert (
        configuration.resolve_config_file(
            "system", project=None, env={"XDG_CONFIG_DIRS": str(tmp_path / "etc")}
        )
        == tmp_path / "etc" / "mcuhome" / CONFIG_FILE
    )


def test_the_project_scope_needs_a_project(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        configuration.resolve_config_file("project", project=None, env={})
    assert "no project here" in caught.value.message
    assert "mcuhome project init" in (caught.value.hint or "")


def test_an_unnameable_scope_directory_is_a_refusal_when_editing(project: Project) -> None:
    with pytest.raises(ConfigError) as caught:
        configuration.resolve_config_file("user", project=project, env={})
    assert "names no user configuration directory" in caught.value.message
    with pytest.raises(ValueError):
        configuration.resolve_config_file("galaxy", project=project, env={})


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


# --- one entry of a map, key by key -----------------------------------
#
# A builder and a package registry are maps, and until they could be
# written key by key the only way to configure one was to open the YAML
# and type it. What these hold is the whole round trip: the key a person
# names is the key the resolution answers, the entry keys carry their own
# declaration, and removing the last of anything takes the empty section
# with it.


def test_an_entry_key_answers_its_own_declaration() -> None:
    """`option()` is what a client shows the kind and the help from."""
    declared = option("builder.attic.target")
    assert declared.name == "builder.attic.target", "named the way it was asked for"
    assert declared.kind == "string"
    assert declared.choices == ("local", "remote")
    assert declared.help
    # No channel but the file, like the map it lives in.
    assert declared.env_var == ""
    assert declared.flag == ""

    mirrors = option("registry.packages.mcuhome.org.mirrors.sdk")
    assert mirrors.name == "registry.packages.mcuhome.org.mirrors.sdk"
    assert mirrors.kind == "strings"
    assert option("registry.packages.mcuhome.org.untrusted").kind == "boolean"


def test_a_builder_is_written_and_read_back_key_by_key(project: Project) -> None:
    """The round trip: what `config set` writes is what a build resolves."""
    configuration.set_config_value(project.config_file, "builder.attic.target", "remote", env={})
    configuration.set_config_value(
        project.config_file, "builder.attic.server", "10.0.0.5:8291", env={}
    )

    builders = resolve_settings(project=project, env={}).value("builder")
    assert [(one.name, one.target, one.server) for one in builders] == [
        ("attic", "remote", "10.0.0.5:8291")
    ]
    assert [one.origin for one in builders] == ["project"]


def test_a_registry_is_written_key_by_key_including_its_mirrors(project: Project) -> None:
    domain = "packages.example.org"
    configuration.set_config_value(
        project.config_file, f"registry.{domain}.untrusted", "true", env={}
    )
    configuration.set_config_value(
        project.config_file,
        f"registry.{domain}.mirrors.sdk",
        "https://mirror.example/sdk/,mirrors/sdk",
        env={},
    )

    registries = resolve_settings(project=project, env={}).value("registry")
    assert len(registries) == 1
    assert registries[0].base_domain == domain
    assert registries[0].untrusted is True
    # A relative mirror is resolved against the file that named it, like
    # every other path in a configuration file; a URL is left alone.
    assert registries[0].mirrors["sdk"] == (
        "https://mirror.example/sdk/",
        str(project.root / "mirrors" / "sdk"),
    )


def test_writing_an_entry_key_leaves_the_rest_of_the_file_alone(project: Project) -> None:
    write_project(
        project,
        "# what this project builds like\nbuild:\n  mode: subprocess\n"
        "builder:\n  # the machine in the attic\n  attic:\n    target: local\n",
    )

    configuration.set_config_value(
        project.config_file, "builder.attic.container_image", "ghcr.io/mcu-home/x:1", env={}
    )
    configuration.set_config_value(project.config_file, "builder.bench.target", "local", env={})

    text = project.config_file.read_text(encoding="utf-8")
    assert "# what this project builds like" in text
    assert "# the machine in the attic" in text
    assert resolve_settings(project=project, env={}).value("build.mode") == "subprocess"
    written = resolve_settings(project=project, env={}).value("builder")
    assert [one.name for one in written] == ["attic", "bench"]
    assert written[0].container_image == "ghcr.io/mcu-home/x:1"


def test_an_entry_value_is_parsed_through_the_entry_s_declaration(project: Project) -> None:
    """A word outside the vocabulary is refused here, not at the next build."""
    before = project.config_file.read_text(encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        configuration.set_config_value(
            project.config_file, "builder.attic.target", "somewhere", env={}
        )
    assert "must be one of local, remote" in caught.value.message
    assert project.config_file.read_text(encoding="utf-8") == before

    with pytest.raises(ConfigError) as caught:
        configuration.set_config_value(
            project.config_file, "registry.packages.example.org.untrusted", "yes", env={}
        )
    assert "either true or false" in caught.value.message
    assert project.config_file.read_text(encoding="utf-8") == before


def test_a_key_no_entry_takes_is_refused_with_the_ones_it_does(project: Project) -> None:
    for name in ("builder.attic.typo", "registry.packages.example.org.mirrors"):
        with pytest.raises(ConfigError) as caught:
            configuration.set_config_value(project.config_file, name, "x", env={})
        assert caught.value.message == f"There is no option called {name!r}."
        hint = caught.value.hint or ""
        assert "is a map, written one entry key at a time" in hint
        assert "builder.<name>.target" in hint or "registry.<base-domain>.mirrors.<source>" in hint


def test_unset_takes_the_entry_and_the_map_with_the_last_key(project: Project) -> None:
    """A `builder:` with nothing under it reads as an unfinished edit."""
    write_project(project, "build:\n  mode: subprocess\n")
    configuration.set_config_value(project.config_file, "builder.attic.target", "remote", env={})
    configuration.set_config_value(
        project.config_file, "builder.attic.server", "10.0.0.5:8291", env={}
    )

    assert configuration.unset_config_value(project.config_file, "builder.attic.server")
    text = project.config_file.read_text(encoding="utf-8")
    assert "server:" not in text
    assert "attic:" in text, "the entry stays while it still holds a key"

    assert configuration.unset_config_value(project.config_file, "builder.attic.target")
    text = project.config_file.read_text(encoding="utf-8")
    assert "attic" not in text
    assert "builder" not in text, "the map goes with its last entry"
    assert "mode: subprocess" in text, "and nothing else moved"
    assert resolve_settings(project=project, env={}).value("builder") == ()


def test_unset_takes_the_mirrors_section_with_its_last_source(project: Project) -> None:
    domain = "packages.example.org"
    configuration.set_config_value(
        project.config_file, f"registry.{domain}.mirrors.sdk", "https://a.example/sdk/", env={}
    )
    configuration.set_config_value(
        project.config_file,
        f"registry.{domain}.mirrors.build-workspace",
        "https://a.example/build-workspace/",
        env={},
    )
    configuration.set_config_value(
        project.config_file, f"registry.{domain}.untrusted", "true", env={}
    )

    assert configuration.unset_config_value(project.config_file, f"registry.{domain}.mirrors.sdk")
    assert "sdk:" not in project.config_file.read_text(encoding="utf-8")
    assert configuration.unset_config_value(
        project.config_file, f"registry.{domain}.mirrors.build-workspace"
    )
    text = project.config_file.read_text(encoding="utf-8")
    assert "mirrors" not in text, "the section goes with its last source"
    assert domain in text, "the entry stays: it still says untrusted"

    assert configuration.unset_config_value(project.config_file, f"registry.{domain}.untrusted")
    assert "registry" not in project.config_file.read_text(encoding="utf-8")


def test_unset_answers_false_for_an_entry_that_was_never_there(project: Project) -> None:
    write_project(project, "build:\n  mode: subprocess\n")
    assert not configuration.unset_config_value(project.config_file, "builder.attic.target")
    assert not configuration.unset_config_value(
        project.config_file, "registry.packages.example.org.mirrors.sdk"
    )
    assert project.config_file.read_text(encoding="utf-8") == "build:\n  mode: subprocess\n"


def test_an_entry_key_says_nothing_about_the_entry_being_complete(project: Project) -> None:
    """One `config set` writes one key, and a remote builder needs two.

    So a half-written entry is a state the file passes through, and what
    says what is missing is the resolution — at the file, in the words
    that name the key. The next `config set` still works on it, which is
    what makes the state a passage rather than a trap.
    """
    configuration.set_config_value(project.config_file, "builder.attic.target", "remote", env={})

    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert 'The builder "attic" is missing its server' in caught.value.message

    configuration.set_config_value(
        project.config_file, "builder.attic.server", "10.0.0.5:8291", env={}
    )
    assert resolve_settings(project=project, env={}).value("builder")[0].server == "10.0.0.5:8291"
