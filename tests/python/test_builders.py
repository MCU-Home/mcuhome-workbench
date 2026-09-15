# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Named builders: parsing, merge-by-name, selection, credentials."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcuhome.model.errors import ConfigError

from mcuhome.workbench.api import Diagnostic
from mcuhome.workbench.configuration import (
    CONFIG_FILE,
    resolve_builder,
    resolve_settings,
)
from mcuhome.workbench.project import Project, create_project


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


REMOTE_ATTIC = "builder:\n  attic:\n    target: remote\n    server: 10.0.0.5:8291\n"


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
    (builder,) = settings.value("builder")
    assert builder.name == "attic"
    assert builder.target == "remote"
    assert builder.server == "10.0.0.5:8291"
    assert builder.origin == "project"


def test_builders_must_be_a_list(project: Project) -> None:
    write_project(project, "builder: attic\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "must be a map of builders, keyed by name" in caught.value.message
    assert "target: remote" in (caught.value.hint or "")


def test_an_entry_that_is_not_a_mapping_is_refused_with_the_shape(project: Project) -> None:
    """The key is the name, so what is left to get wrong is the entry.

    A builder cannot be nameless any more — the map's key *is* the name
    and a mapping cannot hold one twice — so the two refusals those
    mistakes used to earn have no shape to fire on. What a person can
    still write is a name with nothing under it.
    """
    write_project(project, "builder:\n  attic: remote\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "must be a mapping" in caught.value.message
    assert "target: local" in (caught.value.hint or "")


def test_a_name_that_cannot_become_a_file_is_refused(project: Project) -> None:
    write_project(project, "builder:\n  'Attic Server':\n    target: remote\n    server: x\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "not a usable builder name" in caught.value.message


def test_an_unknown_type_lists_the_real_ones(project: Project) -> None:
    write_project(project, "builder:\n  attic:\n    target: cloud\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert '"cloud" is not a build target' in caught.value.message
    assert "local, remote" in (caught.value.hint or "")


def test_a_remote_builder_without_a_server_is_refused_with_the_shape(
    project: Project,
) -> None:
    write_project(project, "builder:\n  attic:\n    target: remote\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "missing its server" in caught.value.message
    hint = caught.value.hint or ""
    assert "server: 10.0.0.5:8291" in hint
    assert "secrets/builder/<name>.yaml" in hint


def test_a_token_in_the_builder_list_is_refused_toward_the_secrets_file(
    project: Project,
) -> None:
    write_project(
        project,
        "builder:\n  attic:\n    target: remote\n    server: x\n    token: oops\n",
    )
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "no option called 'token'" in caught.value.message
    assert "secrets/builder/attic.yaml" in (caught.value.hint or "")


def test_two_builders_of_one_name_in_one_file_are_refused(project: Project) -> None:
    """Still refused, and now by the file format itself.

    The key is the name, so defining ``attic`` twice is a duplicate key
    — which the YAML layer refuses before this module ever sees it. One
    rule, one refusal, and no code of ours to keep in step. Two *files*
    defining one name is still the merge's business.
    """
    write_project(
        project,
        "builder:\n"
        "  attic:\n    target: remote\n    server: a\n"
        "  attic:\n    target: remote\n    server: b\n",
    )
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "not valid YAML" in caught.value.message
    assert 'duplicate key "attic"' in caught.value.message


def test_the_builder_map_cannot_come_from_the_environment(project: Project) -> None:
    """files-only channel: deployment configuration, not an invocation knob."""
    settings = resolve_settings(project=project, env={"MCUHOME_BUILDER": "x"})
    assert settings.value("builder") == ()


# --- merge by name ------------------------------------------------------


def test_layers_merge_by_name_nearer_wins_whole(tmp_path: Path, project: Project) -> None:
    env = user_env(tmp_path)
    write_user(
        tmp_path,
        "builder:\n"
        "  attic:\n    target: remote\n    server: user-wide:1\n"
        "  site:\n    target: remote\n    server: site:1\n",
    )
    write_project(project, REMOTE_ATTIC)
    settings = resolve_settings(project=project, env=env)
    by_name = {builder.name: builder for builder in settings.value("builder")}
    assert set(by_name) == {"attic", "site"}
    assert by_name["attic"].server == "10.0.0.5:8291"  # project wins whole
    assert by_name["attic"].origin == "project"
    assert by_name["site"].origin == "user"  # untouched, still the user's


def test_the_selected_builder_is_a_nearest_wins_scalar(tmp_path: Path, project: Project) -> None:
    env = user_env(tmp_path)
    write_user(tmp_path, "build:\n  builder: site\n")
    write_project(project, "build:\n  builder: attic\n")
    assert resolve_settings(project=project, env=env).value("build.builder") == "attic"
    assert (
        resolve_settings(project=project, env=env | {"MCUHOME_BUILD_BUILDER": "bench"}).value(
            "build.builder"
        )
        == "bench"
    )


def test_config_print_shows_the_layer_and_file_of_each_builder(
    tmp_path: Path, project: Project
) -> None:
    env = user_env(tmp_path)
    write_user(tmp_path, "builder:\n  site:\n    target: local\n")
    write_project(project, REMOTE_ATTIC)
    data = resolve_settings(project=project, env=env).to_dict()
    printed = {entry["name"]: entry for entry in data["builder"]["value"]}
    assert printed["site"]["origin"] == "user"
    assert printed["attic"]["origin"] == "project"
    assert printed["attic"]["server"] == "10.0.0.5:8291"
    # Both provenance answers, the way every other resolved value gives
    # them: the layer, and the file inside it.
    assert printed["attic"]["source"].endswith("mcuhome.yaml")
    assert printed["site"]["container_image"] is None


# --- selection ------------------------------------------------------------


def test_no_builder_and_no_default_falls_back_to_local(project: Project) -> None:
    settings = resolve_settings(project=project, env={})
    selected = resolve_builder(settings, project=project, env={})
    assert selected.target == "local"
    assert selected.builder is None
    assert selected.server is None and selected.token is None


def test_the_fallback_is_the_target_the_configuration_names(project: Project) -> None:
    """No builder at all: ``build.target`` is what answers, not a constant.

    The builder map is one way to say where a build runs and the option
    is the other; with neither a name nor a default, the option is the
    only statement there is, and ignoring it would build here while the
    configuration said otherwise.
    """
    write_project(project, "build:\n  target: remote\n")
    settings = resolve_settings(project=project, env={})
    selected = resolve_builder(settings, project=project, env={})
    assert selected.target == "remote"
    assert selected.builder is None
    # And nothing is invented for it: a remote build with no builder
    # carries no server, which is what the build itself refuses over.
    assert selected.server is None and selected.token is None


def test_the_configured_builder_selects_by_name(project: Project) -> None:
    write_project(project, REMOTE_ATTIC + "build:\n  builder: attic\n")
    settings = resolve_settings(project=project, env={})
    selected = resolve_builder(settings, project=project, env={})
    assert selected.target == "remote"
    assert selected.builder is not None and selected.builder.name == "attic"
    assert selected.server == "10.0.0.5:8291"


def test_an_explicit_name_beats_the_default(project: Project) -> None:
    write_project(
        project,
        REMOTE_ATTIC + "  bench:\n    target: local\n" + "build:\n  builder: attic\n",
    )
    settings = resolve_settings(project=project, env={})
    selected = resolve_builder(settings, name="bench", project=project, env={})
    assert selected.target == "local"
    assert selected.builder is not None and selected.builder.name == "bench"


def test_an_unknown_name_lists_the_configured_builders(project: Project) -> None:
    write_project(project, REMOTE_ATTIC)
    settings = resolve_settings(project=project, env={})
    with pytest.raises(ConfigError) as caught:
        resolve_builder(settings, name="atic", project=project, env={})
    assert '--builder "atic" names no configured builder' in caught.value.message
    hint = caught.value.hint or ""
    assert "attic" in hint
    assert "--build-target" in hint


def test_an_unknown_default_says_it_was_the_default(project: Project) -> None:
    write_project(project, "build:\n  builder: gone\n")
    settings = resolve_settings(project=project, env={})
    with pytest.raises(ConfigError) as caught:
        resolve_builder(settings, project=project, env={})
    assert 'build.builder "gone" names no configured builder' in caught.value.message
    assert "none are defined" in (caught.value.hint or "")


# --- credentials ----------------------------------------------------------


def test_the_token_comes_from_the_projects_secrets(project: Project) -> None:
    write_project(project, REMOTE_ATTIC + "build:\n  builder: attic\n")
    write_token(project, "attic", "token: s3cret\n")
    settings = resolve_settings(project=project, env={})
    selected = resolve_builder(settings, project=project, env={})
    assert selected.token == "s3cret"


def test_a_missing_credentials_file_means_a_tokenless_builder(project: Project) -> None:
    write_project(project, REMOTE_ATTIC + "build:\n  builder: attic\n")
    settings = resolve_settings(project=project, env={})
    assert resolve_builder(settings, project=project, env={}).token is None


@pytest.mark.parametrize(
    ("retired", "successor"), [("type", "target"), ("image", "container_image")]
)
def test_a_retired_entry_key_is_refused_with_the_one_it_is_now(
    project: Project, retired: str, successor: str
) -> None:
    """The two keys the list-to-map move renamed inside an entry.

    One word per thing: where a build runs is a target wherever it is
    written, and a bare `image` does not say which of a build's images it
    means.
    """
    write_project(project, f"builder:\n  attic:\n    {retired}: local\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert f"no option called {retired!r}" in caught.value.message
    assert successor in (caught.value.hint or "")
    assert caught.value.location is not None
    assert caught.value.location.file == project.config_file


def test_a_retired_entry_key_is_named_even_beside_a_valid_target(project: Project) -> None:
    """`target: local` plus `image:` — the refusal is about the old key."""
    write_project(project, "builder:\n  attic:\n    target: local\n    image: ghcr.io/x:1\n")
    with pytest.raises(ConfigError) as caught:
        resolve_settings(project=project, env={})
    assert "no option called 'image'" in caught.value.message
    assert "container_image" in (caught.value.hint or "")


def test_credentials_under_the_retired_path_are_refused_not_ignored(
    tmp_path: Path, project: Project
) -> None:
    """A token nothing reads any more must not be walked past in silence.

    The user and system configuration directories lie outside every
    project, so no project upgrade reaches them: the file is named here,
    with the move that fixes it.
    """
    env = user_env(tmp_path)
    write_project(project, REMOTE_ATTIC + "build:\n  builder: attic\n")
    retired = tmp_path / "xdg" / "mcuhome" / "secrets" / "build-server" / "attic.yaml"
    retired.parent.mkdir(parents=True, mode=0o700)
    retired.write_text("token: from-user\n", encoding="utf-8")
    retired.chmod(0o600)
    settings = resolve_settings(project=project, env=env)

    with pytest.raises(ConfigError) as caught:
        resolve_builder(settings, project=project, env=env)

    expected = retired.parent.parent / "builder" / "attic.yaml"
    assert str(retired) in caught.value.message
    assert caught.value.location is not None and caught.value.location.file == retired
    assert f"mv {retired} {expected}" in (caught.value.hint or "")


def test_a_credentials_file_in_the_new_place_wins_over_the_retired_one(
    tmp_path: Path, project: Project
) -> None:
    """Refused only where the old file would otherwise be missed."""
    env = user_env(tmp_path)
    write_project(project, REMOTE_ATTIC + "build:\n  builder: attic\n")
    directory = tmp_path / "xdg" / "mcuhome" / "secrets"
    for kind, token in (("build-server", "old"), ("builder", "new")):
        file = directory / kind / "attic.yaml"
        file.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        file.write_text(f"token: {token}\n", encoding="utf-8")
        file.chmod(0o600)

    settings = resolve_settings(project=project, env=env)
    assert resolve_builder(settings, project=project, env=env).token == "new"


def test_the_nearest_credentials_file_answers_whole(tmp_path: Path, project: Project) -> None:
    env = user_env(tmp_path)
    write_project(project, REMOTE_ATTIC + "build:\n  builder: attic\n")
    user_secret = tmp_path / "xdg" / "mcuhome" / "secrets" / "builder" / "attic.yaml"
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
    write_project(project, REMOTE_ATTIC + "build:\n  builder: attic\n")
    write_token(project, "attic", "token: s3cret\ntls_fingerprint: ab:cd\n")
    settings = resolve_settings(project=project, env={})
    assert resolve_builder(settings, project=project, env={}).token == "s3cret"


def test_a_non_string_token_is_refused_with_the_quoting_hint(project: Project) -> None:
    write_project(project, REMOTE_ATTIC + "build:\n  builder: attic\n")
    write_token(project, "attic", "token: 12345\n")
    settings = resolve_settings(project=project, env={})
    with pytest.raises(ConfigError) as caught:
        resolve_builder(settings, project=project, env={})
    assert "must be a string" in caught.value.message
    assert 'token: "12345"' in (caught.value.hint or "")


def test_the_token_may_reference_its_own_file(project: Project) -> None:
    """`token: !file <name>` — the generic mechanism, free of extra code.

    The referenced file follows the old token-file rule: a
    trailing newline is an editor's habit and ignored, the content is
    the token.
    """
    write_project(project, REMOTE_ATTIC + "build:\n  builder: attic\n")
    token_file = project.builder_secrets_file("attic").parent / "attic.token"
    token_file.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    token_file.write_text("s3cret\n", encoding="utf-8")
    token_file.chmod(0o600)
    write_token(project, "attic", "token: !file attic.token\n")
    settings = resolve_settings(project=project, env={})
    assert resolve_builder(settings, project=project, env={}).token == "s3cret"


def test_a_referenced_token_file_with_more_than_a_token_is_refused(project: Project) -> None:
    write_project(project, REMOTE_ATTIC + "build:\n  builder: attic\n")
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
    write_project(project, REMOTE_ATTIC + "build:\n  builder: attic\n")
    write_token(project, "attic", "token: !file gone.token\n")
    settings = resolve_settings(project=project, env={})
    with pytest.raises(ConfigError) as caught:
        resolve_builder(settings, project=project, env={})
    assert "gone.token" in caught.value.message
    assert "does not exist" in caught.value.message


def test_an_exposed_credentials_file_draws_a_warning(project: Project) -> None:
    write_project(project, REMOTE_ATTIC + "build:\n  builder: attic\n")
    file = write_token(project, "attic", "token: s3cret\n")
    file.chmod(0o644)
    warnings: list[Diagnostic] = []
    settings = resolve_settings(project=project, env={})
    selected = resolve_builder(settings, project=project, env={}, on_warning=warnings.append)
    assert selected.token == "s3cret"  # a warning, not a refusal — it is not key material
    assert len(warnings) == 1
    assert warnings[0].kind == "exposed_secret_file"
    assert warnings[0].location.file == file
    assert "readable by other users" in warnings[0].message


def test_a_local_builders_token_is_never_looked_up(project: Project) -> None:
    write_project(project, "builder:\n  bench:\n    target: local\nbuild:\n  builder: bench\n")
    file = write_token(project, "bench", "token: [broken\n")  # would refuse if read
    assert file.is_file()
    settings = resolve_settings(project=project, env={})
    assert resolve_builder(settings, project=project, env={}).token is None
