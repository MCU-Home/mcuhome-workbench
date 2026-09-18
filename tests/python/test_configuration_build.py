# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The ``build`` section: one option per key, through all five channels.

The options that describe *how this machine builds* state their area in
their own name (``build.mode``), which is a real level in every spelling
of them: a section in a configuration file, an underscore in the
environment variable. What is tested here is that the one declaration
still produces all of them — the file layers, the variable, the
arguments channel — that each key's validation refuses the same wrong
value whichever channel supplied it, and that ``config set``/``unset``
write and remove the section without touching what else is in the file.

The layers are driven exactly as :mod:`test_configuration` drives them:
a stated environment, ``XDG_CONFIG_HOME`` for the user layer, a
monkeypatched system directory, and throwaway project directories.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from mcuhome.model.errors import ConfigError

from mcuhome.workbench import configuration
from mcuhome.workbench.build import resolve_build_options
from mcuhome.workbench.buildtarget import (
    BUILD_MODES,
    BUILD_TARGETS,
    DEFAULT_CONTAINER_PIDS,
    DEFAULT_CONTAINER_REPOSITORIES,
    MODE_CONTAINER,
    MODE_SUBPROCESS,
    TARGET_LOCAL,
    TARGET_REMOTE,
)
from mcuhome.workbench.configuration import (
    CONFIG_FILE,
    OPTIONS,
    Argument,
    option,
    resolve_settings,
    set_config_value,
    unset_config_value,
)
from mcuhome.workbench.project import Project, create_project

#: Every key of the section, with the environment variable the naming
#: scheme gives it. Spelled out rather than derived, because a table that
#: computed the answer the same way the code does would agree with a
#: wrong rule as happily as with the right one.
KEYS = {
    "build.target": "MCUHOME_BUILD_TARGET",
    "build.mode": "MCUHOME_BUILD_MODE",
    "build.builder": "MCUHOME_BUILD_BUILDER",
    "build.container_program": "MCUHOME_BUILD_CONTAINER_PROGRAM",
    "build.container_repositories": "MCUHOME_BUILD_CONTAINER_REPOSITORIES",
    "build.cpus": "MCUHOME_BUILD_CPUS",
    "build.memory": "MCUHOME_BUILD_MEMORY",
    "build.pids": "MCUHOME_BUILD_PIDS",
    "build.env_store": "MCUHOME_BUILD_ENV_STORE",
    "build.dev_workspace": "MCUHOME_BUILD_DEV_WORKSPACE",
    "build.python": "MCUHOME_BUILD_PYTHON",
    "build.sdk_sources": "MCUHOME_BUILD_SDK_SOURCES",
    "build.workspace_sources": "MCUHOME_BUILD_WORKSPACE_SOURCES",
    "build.tools_sources": "MCUHOME_BUILD_TOOLS_SOURCES",
    "build.sdk_max_bytes": "MCUHOME_BUILD_SDK_MAX_BYTES",
    "build.workspace_max_bytes": "MCUHOME_BUILD_WORKSPACE_MAX_BYTES",
    "build.tools_max_bytes": "MCUHOME_BUILD_TOOLS_MAX_BYTES",
    "build.cache_root": "MCUHOME_BUILD_CACHE_ROOT",
    "build.cache_local": "MCUHOME_BUILD_CACHE_LOCAL",
    "build.cache_shared": "MCUHOME_BUILD_CACHE_SHARED",
    "build.cache_session": "MCUHOME_BUILD_CACHE_SESSION",
    "build.cache_project": "MCUHOME_BUILD_CACHE_PROJECT",
}


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return create_project(tmp_path / "project").project


def user_env(tmp_path: Path) -> dict[str, str]:
    (tmp_path / "xdg" / "mcuhome").mkdir(parents=True, exist_ok=True)
    return {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}


def write_user(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "xdg" / "mcuhome" / CONFIG_FILE
    path.write_text(text, encoding="utf-8")
    return path


def write_project(project: Project, text: str) -> Path:
    project.config_file.write_text(text, encoding="utf-8")
    return project.config_file


# --- the spellings ----------------------------------------------------


def test_every_build_key_is_declared_with_the_expected_variable() -> None:
    declared = {opt.name for opt in OPTIONS if opt.area == "build"}
    assert declared == set(KEYS)
    for name, variable in KEYS.items():
        assert option(name).env_var == variable


def test_every_key_of_the_section_derives_its_flag() -> None:
    """The key is the flag, with the separators the command line writes."""
    assert option("build.mode").flag == "--build-mode"
    assert option("build.mode").area == "build"
    assert option("build.mode").leaf == "mode"
    assert option("build.sdk_max_bytes").flag == "--build-sdk-max-bytes"
    # Except where the command line is not a channel at all: selecting a
    # builder per invocation is --builder, which carries a call's
    # parameter rather than this key.
    assert option("build.builder").flag == ""
    assert not option("build.builder").arguments


def test_the_pre_area_spelling_of_a_moved_key_is_not_an_option(project: Project) -> None:
    """A key that moved into the section does not answer to its old name.

    ``build.sdk_sources`` was ``sdk_sources`` before the areas existed,
    and nothing is kept compatible with an earlier spelling of itself: a
    file that still carries the old one is refused, naming the real key,
    rather than ignored — a silently dropped source list would send the
    build looking for the SDK in nowhere at all. The variable of the old
    spelling is nobody's option and simply sets nothing.
    """
    write_project(project, "sdk_sources:\n  - /pkgs\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert caught.value.message == "There is no option called 'sdk_sources'."
    assert "build.sdk_sources" in (caught.value.hint or "")
    settings = resolve_settings(project=None, env={"MCUHOME_SDK_SOURCES": "/pkgs"})
    assert settings.value("build.sdk_sources") == ()


# --- the five layers, on a key of the section --------------------------


def test_the_section_is_read_from_a_file(tmp_path: Path, project: Project) -> None:
    file = write_project(project, "build:\n  mode: subprocess\n  python: python3.13\n")
    settings = resolve_settings(project=project, env={})
    assert settings.value("build.mode") == MODE_SUBPROCESS
    assert settings.value("build.python") == "python3.13"
    assert settings.origin("build.mode") == "project"
    assert settings.setting("build.mode").source == str(file)


def test_the_layers_beat_each_other_in_order(
    tmp_path: Path, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """system < user < project < environment < arguments, on one key."""
    system = tmp_path / "etc" / "mcuhome"
    system.mkdir(parents=True)
    (system / CONFIG_FILE).write_text("build:\n  python: system\n", encoding="utf-8")
    monkeypatch.setattr(configuration, "system_config_dir", lambda env: system)
    env = user_env(tmp_path)
    assert resolve_settings(project=project, env=env).value("build.python") == "system"

    write_user(tmp_path, "build:\n  python: user\n")
    assert resolve_settings(project=project, env=env).value("build.python") == "user"

    write_project(project, "build:\n  python: project\n")
    assert resolve_settings(project=project, env=env).value("build.python") == "project"

    from_env = env | {"MCUHOME_BUILD_PYTHON": "environment"}
    settings = resolve_settings(project=project, env=from_env)
    assert settings.value("build.python") == "environment"
    assert settings.setting("build.python").source == "MCUHOME_BUILD_PYTHON"

    settings = resolve_settings(
        project=project, env=from_env, args=[Argument("build.python", "arguments")]
    )
    assert settings.value("build.python") == "arguments"
    assert settings.origin("build.python") == "arguments"
    # The flag the registry derives, because this tool stated none.
    assert settings.setting("build.python").source == "--build-python"


def test_a_section_key_is_nearest_wins_like_every_other_scalar(
    tmp_path: Path, project: Project
) -> None:
    """The section is not merged as a unit: each key is its own option."""
    env = user_env(tmp_path)
    write_user(tmp_path, "build:\n  mode: subprocess\n  python: python3.13\n")
    write_project(project, "build:\n  python: python3.14\n")
    settings = resolve_settings(project=project, env=env)
    assert settings.value("build.mode") == MODE_SUBPROCESS
    assert settings.value("build.python") == "python3.14"


def test_a_list_of_names_is_a_yaml_list_and_keeps_its_order(project: Project) -> None:
    """``build.container_repositories`` is an ordered search list, and the
    order is the whole meaning of it: the first repository holding a
    matching image wins and the rest are never queried."""
    write_project(
        project,
        "build:\n"
        "  container_repositories:\n"
        "    - ghcr.io/mcu-home/build-environment\n"
        "    - registry.example.org/mirror/build-environment\n",
    )
    settings = resolve_settings(project=project, env={})
    assert settings.value("build.container_repositories") == (
        "ghcr.io/mcu-home/build-environment",
        "registry.example.org/mirror/build-environment",
    )
    assert settings.origin("build.container_repositories") == "project"


def test_a_list_of_names_is_comma_separated_in_the_environment(project: Project) -> None:
    """Not ``os.pathsep``: a container reference carries a colon of its own
    — a registry port, a tag — so a colon-separated list would split names
    in half."""
    env = {
        "MCUHOME_BUILD_CONTAINER_REPOSITORIES": (
            "registry.example.org:5000/build-environment, ghcr.io/mcu-home/build-environment"
        )
    }
    settings = resolve_settings(project=project, env=env)
    assert settings.value("build.container_repositories") == (
        "registry.example.org:5000/build-environment",
        "ghcr.io/mcu-home/build-environment",
    )


def test_a_list_of_names_written_as_one_string_is_refused_with_its_shape(
    project: Project,
) -> None:
    """The refusal names the file and says what the value has to look like,
    because a list written as a scalar is the mistake a person makes once."""
    write_project(project, "build:\n  container_repositories: ghcr.io/mcu-home/build-environment\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "container_repositories" in caught.value.message
    assert "a list" in caught.value.message


def test_a_list_of_names_set_through_config_set_reads_back_as_the_list(
    project: Project,
) -> None:
    """``mcuhome config set`` takes the one-value spelling and writes the
    file's own: what it wrote has to read back as the value it was given."""
    set_config_value(
        project.config_file,
        "build.container_repositories",
        "ghcr.io/mcu-home/build-environment,registry.example.org/mirror/build-environment",
        env={},
    )
    settings = resolve_settings(project=project, env={})
    assert settings.value("build.container_repositories") == (
        "ghcr.io/mcu-home/build-environment",
        "registry.example.org/mirror/build-environment",
    )
    unset_config_value(project.config_file, "build.container_repositories")
    settings = resolve_settings(project=project, env={})
    assert settings.value("build.container_repositories") == DEFAULT_CONTAINER_REPOSITORIES


def test_a_cpu_share_is_a_number_and_a_memory_figure_is_a_word(project: Project) -> None:
    """What one build may use of this machine, through the layers.

    ``cpus`` is a number because a CPU share is one — ``docker run
    --cpus 1.5`` means one and a half cores' worth of time. ``memory``
    is written the way a container runtime spells it, and the workbench
    turns it into the byte count the request document carries.
    """
    write_project(project, "build:\n  cpus: 2.5\n  memory: 6g\n")
    settings = resolve_settings(project=project, env={})
    assert settings.value("build.cpus") == 2.5
    assert settings.value("build.memory") == "6g"
    limits = resolve_build_options(settings).limits()
    assert limits.cpus == 2.5
    assert limits.memory_bytes == 6 * 1024**3


def test_the_process_bound_has_a_value_nobody_configured(project: Project) -> None:
    """``build.pids`` is the one figure of the three with a declared
    default: the other two mean "this machine as it is" when unset, and
    a process bound that meant that would be no bound at all."""
    settings = resolve_settings(project=project, env={})
    assert settings.value("build.pids") == DEFAULT_CONTAINER_PIDS
    assert settings.origin("build.pids") == "default"
    assert resolve_build_options(settings).pids == DEFAULT_CONTAINER_PIDS


def test_the_process_bound_travels_the_layers_like_every_other_key(project: Project) -> None:
    write_project(project, "build:\n  pids: 512\n")
    settings = resolve_settings(project=project, env={})
    assert resolve_build_options(settings).pids == 512
    settings = resolve_settings(project=project, env={"MCUHOME_BUILD_PIDS": "256"})
    assert resolve_build_options(settings).pids == 256
    assert settings.origin("build.pids") == "environment"


@pytest.mark.parametrize("stated", ["0", "-2", "viele"])
def test_a_process_bound_that_is_not_one_is_refused(project: Project, stated: str) -> None:
    """A container with zero processes runs nothing, so zero is not a
    smaller bound — it is a value the layer that supplied it is named
    for."""
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={"MCUHOME_BUILD_PIDS": stated})
    assert "MCUHOME_BUILD_PIDS" in caught.value.message


def test_a_cpu_share_from_the_environment_is_a_number_too(project: Project) -> None:
    settings = resolve_settings(project=project, env={"MCUHOME_BUILD_CPUS": "0.5"})
    assert settings.value("build.cpus") == 0.5


@pytest.mark.parametrize("stated", ["0", "-2", "zwei"])
def test_a_cpu_share_that_is_not_one_is_refused_by_the_variable(
    project: Project, stated: str
) -> None:
    """Zero cores is not a share and neither is a word: the layer that
    supplied the value is the one that names it."""
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={"MCUHOME_BUILD_CPUS": stated})
    assert "MCUHOME_BUILD_CPUS" in caught.value.message


def test_a_cpu_share_that_is_not_one_is_refused_in_a_file(project: Project) -> None:
    write_project(project, "build:\n  cpus: -1\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "build.cpus" in caught.value.message
    assert "greater than 0" in caught.value.message


def test_a_path_in_the_section_resolves_against_its_own_file(project: Project) -> None:
    write_project(project, "build:\n  env_store: store\n")
    settings = resolve_settings(project=project, env={})
    assert settings.value("build.env_store") == (project.root / "store").resolve()


def test_the_environment_expands_a_path_and_splits_a_list(project: Project) -> None:
    env = {
        "HOME": "/home/somebody",
        "MCUHOME_BUILD_ENV_STORE": "~/store",
        "MCUHOME_BUILD_TOOLS_SOURCES": "/a:/b",
        "MCUHOME_BUILD_SDK_MAX_BYTES": "4096",
    }
    settings = resolve_settings(project=project, env=env)
    assert settings.value("build.env_store") == Path("/home/somebody/store")
    assert settings.value("build.tools_sources") == (Path("/a"), Path("/b"))
    assert settings.value("build.sdk_max_bytes") == 4096


# --- validation, in every channel that can supply a value --------------


def test_a_mode_outside_the_vocabulary_is_refused_in_a_file(project: Project) -> None:
    write_project(project, "build:\n  mode: vm\n")
    with pytest.raises(ConfigError) as refusal:
        resolve_settings(project=project, env={})
    rendered = str(refusal.value)
    for name in BUILD_MODES:
        assert name in rendered
    # The refusal points at the line inside the section, not at the file.
    assert refusal.value.location is not None
    assert refusal.value.location.line == 2


def test_a_mode_outside_the_vocabulary_is_refused_in_the_environment(project: Project) -> None:
    with pytest.raises(ConfigError) as refusal:
        resolve_settings(project=project, env={"MCUHOME_BUILD_MODE": "vm"})
    assert "MCUHOME_BUILD_MODE" in str(refusal.value)
    assert MODE_SUBPROCESS in str(refusal.value)


def test_a_target_outside_the_vocabulary_is_refused_in_a_file(project: Project) -> None:
    """The other axis is validated in its own right, and by the same rule."""
    write_project(project, "build:\n  target: cloud\n")
    with pytest.raises(ConfigError) as refusal:
        resolve_settings(project=project, env={})
    rendered = str(refusal.value)
    for name in BUILD_TARGETS:
        assert name in rendered
    assert refusal.value.location is not None
    assert refusal.value.location.line == 2


def test_a_target_outside_the_vocabulary_is_refused_in_the_environment(project: Project) -> None:
    with pytest.raises(ConfigError) as refusal:
        resolve_settings(project=project, env={"MCUHOME_BUILD_TARGET": "cloud"})
    assert "MCUHOME_BUILD_TARGET" in str(refusal.value)
    assert TARGET_REMOTE in str(refusal.value)


def test_a_bound_below_its_minimum_is_refused_in_a_file(project: Project) -> None:
    write_project(project, "build:\n  workspace_max_bytes: 0\n")
    with pytest.raises(ConfigError) as refusal:
        resolve_settings(project=project, env={})
    assert "at least 1" in str(refusal.value)


def test_a_bound_below_its_minimum_is_refused_in_the_environment(project: Project) -> None:
    with pytest.raises(ConfigError) as refusal:
        resolve_settings(project=project, env={"MCUHOME_BUILD_TOOLS_MAX_BYTES": "-1"})
    assert "MCUHOME_BUILD_TOOLS_MAX_BYTES" in str(refusal.value)
    assert "at least 1" in str(refusal.value)


def test_a_bound_that_is_not_a_number_is_refused(project: Project) -> None:
    write_project(project, "build:\n  tools_max_bytes: plenty\n")
    with pytest.raises(ConfigError):
        resolve_settings(project=project, env={})


def test_a_source_list_written_as_one_string_is_refused(project: Project) -> None:
    write_project(project, "build:\n  workspace_sources: /a\n")
    with pytest.raises(ConfigError) as refusal:
        resolve_settings(project=project, env={})
    assert "list of paths" in str(refusal.value)


# --- the shape of the section itself ----------------------------------


def test_an_unknown_key_in_the_section_is_refused_with_the_real_ones(
    project: Project,
) -> None:
    write_project(project, "build:\n  moed: subprocess\n")
    with pytest.raises(ConfigError) as refusal:
        resolve_settings(project=project, env={})
    assert "build.moed" in str(refusal.value)
    assert "mode" in (refusal.value.hint or "")


def test_a_section_that_is_not_a_mapping_is_refused(project: Project) -> None:
    write_project(project, "build: subprocess\n")
    with pytest.raises(ConfigError) as refusal:
        resolve_settings(project=project, env={})
    assert "'build'" in str(refusal.value)


def test_a_dotted_key_written_flat_is_refused_with_the_shape_to_use(
    project: Project,
) -> None:
    """The option exists; the spelling does not, and the hint shows the real one."""
    write_project(project, "build.mode: subprocess\n")
    with pytest.raises(ConfigError) as refusal:
        resolve_settings(project=project, env={})
    assert "build.mode" in str(refusal.value)
    hint = refusal.value.hint or ""
    assert "build:" in hint
    assert "mode:" in hint


def test_a_key_of_another_area_is_still_an_unknown_option(project: Project) -> None:
    write_project(project, "nonsense:\n  mode: subprocess\n")
    with pytest.raises(ConfigError) as refusal:
        resolve_settings(project=project, env={})
    assert "nonsense" in str(refusal.value)


# --- writing the section ----------------------------------------------


def test_config_set_writes_the_section_and_keeps_the_rest(project: Project) -> None:
    """What is written reads back, and everything else in the file survives."""
    file = project.config_file
    file.write_text("# a comment\nsigning:\n  imgtool: /opt/imgtool\n", encoding="utf-8")
    set_config_value(file, "build.mode", MODE_SUBPROCESS, env={})
    set_config_value(file, "build.env_store", "/srv/store", env={})
    text = file.read_text(encoding="utf-8")
    assert "# a comment" in text
    assert "imgtool: /opt/imgtool" in text
    assert "build:\n  mode: subprocess\n  env_store: /srv/store\n" in text
    settings = resolve_settings(project=project, env={})
    assert settings.value("build.mode") == MODE_SUBPROCESS
    assert settings.value("build.env_store") == Path("/srv/store")


def test_config_set_refuses_a_value_outside_the_vocabulary(tmp_path: Path) -> None:
    file = tmp_path / CONFIG_FILE
    with pytest.raises(ConfigError) as refusal:
        set_config_value(file, "build.mode", "vm", env={})
    assert MODE_CONTAINER in str(refusal.value)
    assert not file.exists()


def test_config_unset_removes_the_key_and_then_the_empty_section(tmp_path: Path) -> None:
    file = tmp_path / CONFIG_FILE
    set_config_value(file, "build.mode", MODE_SUBPROCESS, env={})
    set_config_value(file, "build.env_store", "/srv/store", env={})
    assert unset_config_value(file, "build.mode") is True
    assert "mode" not in file.read_text(encoding="utf-8")
    assert "build:" in file.read_text(encoding="utf-8")
    assert unset_config_value(file, "build.env_store") is True
    assert "build:" not in file.read_text(encoding="utf-8")
    # Nothing to remove is False rather than a claim that something was.
    assert unset_config_value(file, "build.mode") is False


def test_config_set_refuses_a_section_that_is_not_a_mapping(tmp_path: Path) -> None:
    file = tmp_path / CONFIG_FILE
    file.write_text("build: subprocess\n", encoding="utf-8")
    with pytest.raises(ConfigError) as refusal:
        set_config_value(file, "build.mode", MODE_SUBPROCESS, env={})
    assert "build" in str(refusal.value)


# --- what `mcuhome config print` renders ------------------------------


def test_every_build_key_is_printed_with_its_value_and_origin(project: Project) -> None:
    write_project(project, "build:\n  mode: subprocess\n")
    data = resolve_settings(project=project, env={}).to_dict()
    for name in KEYS:
        assert name in data
    assert data["build.mode"] == {
        "value": MODE_SUBPROCESS,
        "origin": "project",
        "source": str(project.config_file),
    }
    # Paths are rendered as strings, so the whole thing is JSON-ready.
    assert data["build.workspace_sources"]["value"] == []


def test_a_registrys_configured_anchor_is_printed_as_a_string(project: Project) -> None:
    """`mcuhome config print` renders every registry setting, the anchor
    included, and renders it JSON-ready."""
    write_project(
        project,
        "registry:\n  packages.example.org:\n    anchor: anchors/private.json\n",
    )
    data = resolve_settings(project=project, env={}).to_dict()
    printed = data["registry"]["value"]
    assert printed == [
        {
            "base_domain": "packages.example.org",
            # The layer that defined this registry, like a builder's:
            # the layers merge by base domain, so it is a fact per
            # registry and not one of the resolution as a whole.
            "origin": "project",
            "source": str(project.config_file),
            "untrusted": False,
            "anchor": str((project.root / "anchors" / "private.json").resolve()),
            "mirrors": {},
        }
    ]
    # A registry that names none says so rather than omitting the key.
    write_project(project, "registry:\n  packages.example.org: {}\n")
    printed = resolve_settings(project=project, env={}).to_dict()["registry"]["value"]
    assert printed[0]["anchor"] is None


def test_the_retired_developer_tools_key_is_unknown(project: Project) -> None:
    """``build.dev_tools`` is gone, and a file that still states it says so.

    It named a second half that no longer exists: a development build is
    pointed at one west workspace and takes its tools from the ``PATH``
    it was started from. A key left behind in a configuration file would
    otherwise read as configuring something, and configure nothing.
    """
    write_project(project, "build:\n  dev_tools: /somewhere/tools\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert caught.value.message == "There is no option called 'build.dev_tools'."
    # The one that replaced it is in the list of what the section has.
    assert "dev_workspace" in (caught.value.hint or "")


def test_the_target_is_a_key_of_the_section_like_the_mode(project: Project) -> None:
    """``build.target`` resolves through the same five layers ``build.mode`` does.

    The two are the two axes of a build — where it runs, and how the
    machine that runs it executes the work — and each is one key with one
    declaration, so a file, a variable and an invocation state them the
    same way.
    """
    settings = resolve_settings(project=None, env={})
    assert settings.value("build.target") == TARGET_LOCAL
    assert settings.origin("build.target") == "default"

    write_project(project, "build:\n  target: remote\n")
    settings = resolve_settings(project=project, env={})
    assert settings.value("build.target") == TARGET_REMOTE
    assert settings.origin("build.target") == "project"

    settings = resolve_settings(project=project, env={"MCUHOME_BUILD_TARGET": TARGET_LOCAL})
    assert settings.value("build.target") == TARGET_LOCAL
    assert settings.setting("build.target").source == "MCUHOME_BUILD_TARGET"

    settings = resolve_settings(
        project=project, env={}, args=[Argument("build.target", TARGET_LOCAL)]
    )
    assert settings.value("build.target") == TARGET_LOCAL
    assert settings.origin("build.target") == "arguments"


def test_the_build_options_carry_the_target_and_where_it_came_from(project: Project) -> None:
    """What a build reads is the resolved object, source included.

    Every key carries where its value came from, for the reason the
    target and the mode need it most: a refusal one of them caused has to
    be able to say who chose it, and it usually came out of a file the
    person is not looking at.
    """
    write_project(project, "build:\n  target: remote\n")
    options = resolve_build_options(
        resolve_settings(project=project, env={"MCUHOME_BUILD_CACHE_ROOT": "/srv/cache"})
    )
    assert options.target == TARGET_REMOTE
    assert options.source("target") == str(project.root / "mcuhome.yaml")
    # Not only the two keys the object used to carry a field for: a value
    # out of the environment names the variable it came from.
    assert options.cache_root == Path("/srv/cache")
    assert options.source("cache_root") == "MCUHOME_BUILD_CACHE_ROOT"

    unset = resolve_build_options(resolve_settings(project=None, env={}))
    assert unset.target == TARGET_LOCAL
    assert unset.source("target") == "default"

    # And every key of the section can answer, not a chosen few: each
    # field of the object except the map itself has an entry in it.
    carried = {entry.name for entry in dataclasses.fields(unset)} - {"sources"}
    assert carried and carried <= set(unset.sources)
    assert set(unset.sources.values()) == {"default"}
