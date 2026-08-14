# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Named builders (ADR 0023): parsing, merge-by-name, selection, credentials."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcuhome.model.errors import ConfigError

from mcuhome.workbench.configuration import (
    CONFIG_FILE,
    resolve_builder,
    resolve_settings,
)
from mcuhome.workbench.project import Project, init_project


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return init_project(tmp_path / "project").project


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


REMOTE_ATTIC = "builders:\n  - name: attic\n    type: remote\n    server: 10.0.0.5:8291\n"


def write_token(project: Project, name: str, text: str) -> Path:
    file = project.builder_secrets_file(name)
    file.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    file.write_text(text, encoding="utf-8")
    file.chmod(0o600)
    return file


# --- parsing ----------------------------------------------------------


def test_a_remote_builder_parses_with_its_server(project: Project) -> None:
    write_project(project, REMOTE_ATTIC)
    settings = resolve_settings(project=project, env={})
    (builder,) = settings.value("builders")
    assert builder.name == "attic"
    assert builder.type == "remote"
    assert builder.server == "10.0.0.5:8291"
    assert builder.layer == "project"


def test_a_local_dev_builder_expands_its_workspace(tmp_path: Path, project: Project) -> None:
    write_project(
        project,
        "builders:\n  - name: bench\n    type: local-dev\n    workspace: ~/zephyr\n",
    )
    env = {"HOME": str(tmp_path / "home")}
    (builder,) = resolve_settings(project=project, env=env).value("builders")
    assert builder.workspace == tmp_path / "home" / "zephyr"


def test_a_relative_workspace_is_relative_to_the_defining_file(project: Project) -> None:
    write_project(
        project,
        "builders:\n  - name: bench\n    type: local-dev\n    workspace: ./ws\n",
    )
    (builder,) = resolve_settings(project=project, env={}).value("builders")
    assert builder.workspace == project.root / "ws"


def test_builders_must_be_a_list(project: Project) -> None:
    write_project(project, "builders: attic\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "must be a list of builder entries" in caught.value.message
    assert "type: remote" in (caught.value.hint or "")


def test_a_builder_without_a_name_is_refused(project: Project) -> None:
    write_project(project, "builders:\n  - type: remote\n    server: x\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "has no name" in caught.value.message
    assert "secrets/build-server/<name>.yaml" in (caught.value.hint or "")


def test_a_name_that_cannot_become_a_file_is_refused(project: Project) -> None:
    write_project(project, "builders:\n  - name: 'Attic Server'\n    type: remote\n    server: x\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "not a usable builder name" in caught.value.message


def test_an_unknown_type_lists_the_real_ones(project: Project) -> None:
    write_project(project, "builders:\n  - name: attic\n    type: cloud\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert '"cloud" is not a builder type' in caught.value.message
    assert "local, local-dev, remote" in (caught.value.hint or "")


def test_a_remote_builder_without_a_server_is_refused_with_the_shape(
    project: Project,
) -> None:
    write_project(project, "builders:\n  - name: attic\n    type: remote\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "missing its server" in caught.value.message
    hint = caught.value.hint or ""
    assert "server: 10.0.0.5:8291" in hint
    assert "secrets/build-server/<name>.yaml" in hint


def test_a_token_in_the_builder_list_is_refused_toward_the_secrets_file(
    project: Project,
) -> None:
    write_project(
        project,
        "builders:\n  - name: attic\n    type: remote\n    server: x\n    token: oops\n",
    )
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "no option called 'token'" in caught.value.message
    assert "secrets/build-server/attic.yaml" in (caught.value.hint or "")


def test_two_builders_of_one_name_in_one_file_are_refused(project: Project) -> None:
    write_project(
        project,
        "builders:\n"
        "  - name: attic\n    type: remote\n    server: a\n"
        "  - name: attic\n    type: remote\n    server: b\n",
    )
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert 'defines the builder "attic" twice' in caught.value.message


def test_builders_cannot_come_from_the_environment(project: Project) -> None:
    """files-only channel: deployment configuration, not an invocation knob."""
    settings = resolve_settings(project=project, env={"MCUHOME_BUILDERS": "x"})
    assert settings.value("builders") == ()


# --- merge by name (ADR 0023 §3) --------------------------------------


def test_layers_merge_by_name_nearer_wins_whole(tmp_path: Path, project: Project) -> None:
    env = user_env(tmp_path)
    write_user(
        tmp_path,
        "builders:\n"
        "  - name: attic\n    type: remote\n    server: user-wide:1\n"
        "  - name: site\n    type: remote\n    server: site:1\n",
    )
    write_project(project, REMOTE_ATTIC)
    settings = resolve_settings(project=project, env=env)
    by_name = {builder.name: builder for builder in settings.value("builders")}
    assert set(by_name) == {"attic", "site"}
    assert by_name["attic"].server == "10.0.0.5:8291"  # project wins whole
    assert by_name["attic"].layer == "project"
    assert by_name["site"].layer == "user"  # untouched, still the user's


def test_default_builder_is_a_nearest_wins_scalar(tmp_path: Path, project: Project) -> None:
    env = user_env(tmp_path)
    write_user(tmp_path, "default_builder: site\n")
    write_project(project, "default_builder: attic\n")
    assert resolve_settings(project=project, env=env).value("default_builder") == "attic"
    assert (
        resolve_settings(project=project, env=env | {"MCUHOME_DEFAULT_BUILDER": "bench"}).value(
            "default_builder"
        )
        == "bench"
    )


def test_config_print_shows_each_builders_layer(tmp_path: Path, project: Project) -> None:
    env = user_env(tmp_path)
    write_user(tmp_path, "builders:\n  - name: site\n    type: local\n")
    write_project(project, REMOTE_ATTIC)
    data = resolve_settings(project=project, env=env).print_data()
    printed = {entry["name"]: entry for entry in data["builders"]["value"]}
    assert printed["site"]["layer"] == "user"
    assert printed["attic"]["layer"] == "project"
    assert printed["attic"]["server"] == "10.0.0.5:8291"


# --- selection (ADR 0023 §2) ------------------------------------------


def test_no_builder_and_no_default_falls_back_to_local(project: Project) -> None:
    settings = resolve_settings(project=project, env={})
    selected = resolve_builder(settings, project=project, env={})
    assert selected.method == "local"
    assert selected.builder is None
    assert selected.server is None and selected.token is None


def test_the_default_builder_selects_by_name(project: Project) -> None:
    write_project(project, REMOTE_ATTIC + "default_builder: attic\n")
    settings = resolve_settings(project=project, env={})
    selected = resolve_builder(settings, project=project, env={})
    assert selected.method == "remote"
    assert selected.builder is not None and selected.builder.name == "attic"
    assert selected.server == "10.0.0.5:8291"


def test_an_explicit_name_beats_the_default(project: Project) -> None:
    write_project(
        project,
        REMOTE_ATTIC + "  - name: bench\n    type: local\n" + "default_builder: attic\n",
    )
    settings = resolve_settings(project=project, env={})
    selected = resolve_builder(settings, name="bench", project=project, env={})
    assert selected.method == "local"
    assert selected.builder is not None and selected.builder.name == "bench"


def test_an_unknown_name_lists_the_configured_builders(project: Project) -> None:
    write_project(project, REMOTE_ATTIC)
    settings = resolve_settings(project=project, env={})
    with pytest.raises(ConfigError) as caught:
        resolve_builder(settings, name="atic", project=project, env={})
    assert '--builder "atic" names no configured builder' in caught.value.message
    hint = caught.value.hint or ""
    assert "attic" in hint
    assert "--build-mode" in hint


def test_an_unknown_default_says_it_was_the_default(project: Project) -> None:
    write_project(project, "default_builder: gone\n")
    settings = resolve_settings(project=project, env={})
    with pytest.raises(ConfigError) as caught:
        resolve_builder(settings, project=project, env={})
    assert 'default_builder "gone" names no configured builder' in caught.value.message
    assert "none are defined" in (caught.value.hint or "")


# --- credentials (ADR 0023 §4) ----------------------------------------


def test_the_token_comes_from_the_projects_secrets(project: Project) -> None:
    write_project(project, REMOTE_ATTIC + "default_builder: attic\n")
    write_token(project, "attic", "token: s3cret\n")
    settings = resolve_settings(project=project, env={})
    selected = resolve_builder(settings, project=project, env={})
    assert selected.token == "s3cret"


def test_a_missing_credentials_file_means_a_tokenless_builder(project: Project) -> None:
    write_project(project, REMOTE_ATTIC + "default_builder: attic\n")
    settings = resolve_settings(project=project, env={})
    assert resolve_builder(settings, project=project, env={}).token is None


def test_the_nearest_credentials_file_answers_whole(tmp_path: Path, project: Project) -> None:
    env = user_env(tmp_path)
    write_project(project, REMOTE_ATTIC + "default_builder: attic\n")
    user_secret = tmp_path / "xdg" / "mcuhome" / "secrets" / "build-server" / "attic.yaml"
    user_secret.parent.mkdir(parents=True, mode=0o700)
    user_secret.write_text("token: from-user\n", encoding="utf-8")
    user_secret.chmod(0o600)
    settings = resolve_settings(project=project, env=env)
    assert resolve_builder(settings, project=project, env=env).token == "from-user"
    # A project file — even one that names no token — wins whole.
    write_token(project, "attic", "# reserved for TLS material\n")
    assert resolve_builder(settings, project=project, env=env).token is None


def test_unknown_keys_in_the_credentials_file_are_the_future_not_a_typo(
    project: Project,
) -> None:
    write_project(project, REMOTE_ATTIC + "default_builder: attic\n")
    write_token(project, "attic", "token: s3cret\ntls_fingerprint: ab:cd\n")
    settings = resolve_settings(project=project, env={})
    assert resolve_builder(settings, project=project, env={}).token == "s3cret"


def test_a_non_string_token_is_refused_with_the_quoting_hint(project: Project) -> None:
    write_project(project, REMOTE_ATTIC + "default_builder: attic\n")
    write_token(project, "attic", "token: 12345\n")
    settings = resolve_settings(project=project, env={})
    with pytest.raises(ConfigError) as caught:
        resolve_builder(settings, project=project, env={})
    assert "must be a string" in caught.value.message
    assert 'token: "12345"' in (caught.value.hint or "")


def test_the_token_may_reference_its_own_file(project: Project) -> None:
    """`token: !file <name>` — the generic mechanism, free of extra code.

    The referenced file follows the old token-file rule (E63): a
    trailing newline is an editor's habit and ignored, the content is
    the token.
    """
    write_project(project, REMOTE_ATTIC + "default_builder: attic\n")
    token_file = project.builder_secrets_file("attic").parent / "attic.token"
    token_file.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    token_file.write_text("s3cret\n", encoding="utf-8")
    token_file.chmod(0o600)
    write_token(project, "attic", "token: !file attic.token\n")
    settings = resolve_settings(project=project, env={})
    assert resolve_builder(settings, project=project, env={}).token == "s3cret"


def test_a_referenced_token_file_with_more_than_a_token_is_refused(project: Project) -> None:
    write_project(project, REMOTE_ATTIC + "default_builder: attic\n")
    token_file = project.builder_secrets_file("attic").parent / "attic.token"
    token_file.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    token_file.write_text("not a\nbare token\n", encoding="utf-8")
    token_file.chmod(0o600)
    write_token(project, "attic", "token: !file attic.token\n")
    settings = resolve_settings(project=project, env={})
    with pytest.raises(ConfigError) as caught:
        resolve_builder(settings, project=project, env={})
    assert "does not hold a bare token" in caught.value.message
    assert str(token_file) in (caught.value.hint or "")


def test_a_missing_referenced_token_file_is_a_located_refusal(project: Project) -> None:
    """The !file contract: a dangling reference stops the run at once."""
    write_project(project, REMOTE_ATTIC + "default_builder: attic\n")
    write_token(project, "attic", "token: !file gone.token\n")
    settings = resolve_settings(project=project, env={})
    with pytest.raises(ConfigError) as caught:
        resolve_builder(settings, project=project, env={})
    assert "gone.token" in caught.value.message
    assert "does not exist" in caught.value.message


def test_an_exposed_credentials_file_draws_a_warning(project: Project) -> None:
    write_project(project, REMOTE_ATTIC + "default_builder: attic\n")
    file = write_token(project, "attic", "token: s3cret\n")
    file.chmod(0o644)
    warnings: list[str] = []
    settings = resolve_settings(project=project, env={})
    selected = resolve_builder(settings, project=project, env={}, on_warning=warnings.append)
    assert selected.token == "s3cret"  # a warning, not a refusal — it is not key material
    assert len(warnings) == 1 and "readable by other users" in warnings[0]


def test_a_local_builders_token_is_never_looked_up(project: Project) -> None:
    write_project(project, "builders:\n  - name: bench\n    type: local\ndefault_builder: bench\n")
    file = write_token(project, "bench", "token: [broken\n")  # would refuse if read
    assert file.is_file()
    settings = resolve_settings(project=project, env={})
    assert resolve_builder(settings, project=project, env={}).token is None
