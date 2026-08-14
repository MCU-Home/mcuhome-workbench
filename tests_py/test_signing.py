# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The firmware signing key: where it lives, what it is, how refusals read.

ADR 0015 decision 8 as implemented after the ADR 0022 project model:
the key is **per project**, in ``secrets/firmware/mcuboot.yaml`` under
``firmware_signing_key``, generated on first need; ``--signing-key``
and ``MCUHOME_SIGNING_KEY`` point at a plain PEM file instead (the
dashboard's path). There is deliberately no per-user default any more —
and no fallback when neither a project nor an override is given,
because guessing a directory for a private key is how two things end up
signed with keys nobody meant.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from mcuhome.model.errors import BuildError, ConfigError
from ruamel.yaml import YAML

from mcuhome.workbench.project import Project, init_project
from mcuhome.workbench.signing import (
    FIRMWARE_KEY,
    KEY_VAR,
    generate_key_pem,
    looks_like_p256_key,
    public_key_pem,
    signing_key,
)


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return init_project(tmp_path / "project").project


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def write_key_file(path: Path, text: str | None = None) -> str:
    pem = text if text is not None else generate_key_pem()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pem, encoding="utf-8")
    path.chmod(0o600)
    return pem


# --- resolution: override, variable, project --------------------------


def test_no_project_and_no_override_is_a_refusal_in_words(tmp_path: Path) -> None:
    with pytest.raises(BuildError) as caught:
        signing_key(env={})
    assert "no firmware signing key" in caught.value.message
    hint = caught.value.hint or ""
    assert "secrets/firmware/mcuboot.yaml" in hint
    assert FIRMWARE_KEY in hint
    assert "mcuhome init" in hint
    assert "--signing-key" in hint
    assert KEY_VAR in hint


def test_the_flag_beats_the_variable_beats_the_project(tmp_path: Path, project: Project) -> None:
    by_flag = tmp_path / "flag.key"
    by_var = tmp_path / "var.key"
    flag_pem = write_key_file(by_flag)
    write_key_file(by_var)
    key = signing_key(by_flag, env={KEY_VAR: str(by_var)}, project=project)
    assert key.path == by_flag
    assert key.pem == flag_pem
    assert not key.in_secrets
    assert not project.firmware_secrets_file.exists()


def test_the_variable_beats_the_project(tmp_path: Path, project: Project) -> None:
    by_var = tmp_path / "var.key"
    var_pem = write_key_file(by_var)
    key = signing_key(env={KEY_VAR: str(by_var)}, project=project)
    assert key.path == by_var
    assert key.pem == var_pem
    assert not project.firmware_secrets_file.exists()


def test_a_tilde_override_uses_the_stated_home(tmp_path: Path) -> None:
    pem = write_key_file(tmp_path / "home" / "my.key")
    key = signing_key("~/my.key", env={"HOME": str(tmp_path / "home")})
    assert key.pem == pem


# --- the project key: generated on first need -------------------------


def test_the_first_build_generates_the_project_key_and_says_so(project: Project) -> None:
    key = signing_key(env={}, project=project)
    assert key.created
    assert key.in_secrets
    assert key.path == project.firmware_secrets_file
    assert looks_like_p256_key(key.pem)
    assert public_key_pem(key.pem).startswith("-----BEGIN PUBLIC KEY-----")


def test_the_generated_file_is_readable_by_nobody_else(project: Project) -> None:
    signing_key(env={}, project=project)
    assert mode_of(project.firmware_secrets_file) == 0o600
    assert mode_of(project.firmware_secrets_file.parent) == 0o700
    assert mode_of(project.secrets_dir) == 0o700


def test_the_generated_file_is_yaml_with_the_key_under_its_name(project: Project) -> None:
    key = signing_key(env={}, project=project)
    data = YAML(typ="safe").load(project.firmware_secrets_file.read_text(encoding="utf-8"))
    assert data[FIRMWARE_KEY] == key.pem
    head = project.firmware_secrets_file.read_text(encoding="utf-8").splitlines()[0]
    assert head.startswith("#")  # the file explains itself


def test_the_second_build_reuses_the_first_ones_key(project: Project) -> None:
    first = signing_key(env={}, project=project)
    second = signing_key(env={}, project=project)
    assert not second.created
    assert second.pem == first.pem


def test_the_key_is_added_to_an_existing_secrets_file_without_disturbing_it(
    project: Project,
) -> None:
    file = project.firmware_secrets_file
    file.parent.mkdir(parents=True, mode=0o700)
    file.write_text("# my notes\nother: value\n", encoding="utf-8")
    file.chmod(0o600)
    key = signing_key(env={}, project=project)
    assert key.created
    text = file.read_text(encoding="utf-8")
    assert "# my notes" in text
    data = YAML(typ="safe").load(text)
    assert data["other"] == "value"
    assert looks_like_p256_key(data[FIRMWARE_KEY])


def test_create_false_never_generates(project: Project) -> None:
    with pytest.raises(BuildError) as caught:
        signing_key(env={}, project=project, create=False)
    assert "no such file" in caught.value.message
    assert not project.firmware_secrets_file.exists()

    file = project.firmware_secrets_file
    file.parent.mkdir(parents=True, mode=0o700)
    file.write_text("other: value\n", encoding="utf-8")
    file.chmod(0o600)
    with pytest.raises(BuildError) as caught:
        signing_key(env={}, project=project, create=False)
    assert f"no {FIRMWARE_KEY} entry" in caught.value.message


def test_key_material_that_is_not_a_key_is_never_overwritten(project: Project) -> None:
    file = project.firmware_secrets_file
    file.parent.mkdir(parents=True, mode=0o700)
    file.write_text(f"{FIRMWARE_KEY}: not-a-key\n", encoding="utf-8")
    file.chmod(0o600)
    with pytest.raises(BuildError) as caught:
        signing_key(env={}, project=project)
    assert f"The {FIRMWARE_KEY} entry in {file}" in caught.value.message
    assert "not an ECDSA P-256 private key" in caught.value.message
    assert file.read_text(encoding="utf-8") == f"{FIRMWARE_KEY}: not-a-key\n"


def test_a_secrets_file_that_is_not_a_mapping_is_refused(project: Project) -> None:
    file = project.firmware_secrets_file
    file.parent.mkdir(parents=True, mode=0o700)
    file.write_text("- a list\n", encoding="utf-8")
    file.chmod(0o600)
    with pytest.raises(BuildError) as caught:
        signing_key(env={}, project=project)
    assert "not a mapping" in caught.value.message


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_an_exposed_project_key_file_is_refused_outright(project: Project) -> None:
    signing_key(env={}, project=project)
    project.firmware_secrets_file.chmod(0o644)
    with pytest.raises(ConfigError) as caught:
        signing_key(env={}, project=project)
    assert "refuses to use the key material" in caught.value.message
    assert "chmod 600" in (caught.value.hint or "")


# --- the override file ------------------------------------------------


def test_a_missing_override_file_is_created_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "fresh.key"
    key = signing_key(path, env={})
    assert key.created
    assert not key.in_secrets
    assert mode_of(path) == 0o600
    assert looks_like_p256_key(path.read_text(encoding="utf-8"))


def test_a_key_from_elsewhere_is_used_as_it_is(tmp_path: Path) -> None:
    path = tmp_path / "imported.key"
    pem = write_key_file(path)
    key = signing_key(path, env={})
    assert not key.created
    assert key.pem == pem


def test_a_file_that_is_not_a_key_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("not a key\n", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(BuildError) as caught:
        signing_key(path, env={})
    assert "not an ECDSA P-256 private key" in caught.value.message
    assert path.read_text(encoding="utf-8") == "not a key\n"


def test_binary_rubbish_is_refused_as_a_key_rather_than_as_an_encoding(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rubbish.key"
    path.write_bytes(bytes(range(256)))
    path.chmod(0o600)
    with pytest.raises(BuildError) as caught:
        signing_key(path, env={})
    assert "not an ECDSA P-256 private key" in caught.value.message


def test_a_directory_as_key_path_is_reported_as_such(tmp_path: Path) -> None:
    with pytest.raises(BuildError) as caught:
        signing_key(tmp_path, env={})
    assert "it is a directory" in caught.value.message


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_an_exposed_override_key_is_refused_outright(tmp_path: Path) -> None:
    path = tmp_path / "loose.key"
    write_key_file(path)
    path.chmod(0o644)
    with pytest.raises(ConfigError) as caught:
        signing_key(path, env={})
    assert "refuses to use the key material" in caught.value.message


# --- materialization for imgtool --------------------------------------


def test_a_project_key_exists_as_a_file_only_inside_the_block(project: Project) -> None:
    key = signing_key(env={}, project=project)
    with key.key_file() as path:
        assert path.parent == project.firmware_secrets_file.parent
        assert path != project.firmware_secrets_file
        assert mode_of(path) == 0o600
        assert path.read_text(encoding="utf-8") == key.pem
        seen = path
    assert not seen.exists()


def test_the_materialized_file_is_gone_even_when_the_block_raises(
    project: Project,
) -> None:
    key = signing_key(env={}, project=project)
    with pytest.raises(RuntimeError), key.key_file() as path:
        seen = path
        raise RuntimeError("boom")
    assert not seen.exists()


def test_a_plain_file_key_is_yielded_not_copied(tmp_path: Path) -> None:
    path = tmp_path / "plain.key"
    write_key_file(path)
    key = signing_key(path, env={})
    with key.key_file() as yielded:
        assert yielded == path
    assert path.exists()


# --- no refusal ever prints the key -----------------------------------


def test_no_refusal_ever_prints_the_key(project: Project) -> None:
    key = signing_key(env={}, project=project)
    project.firmware_secrets_file.chmod(0o644)
    with pytest.raises((BuildError, ConfigError)) as caught:
        signing_key(env={}, project=project)
    scalars = key.pem.replace("-----BEGIN PRIVATE KEY-----", "").replace(
        "-----END PRIVATE KEY-----", ""
    )
    for line in filter(None, scalars.splitlines()):
        assert line not in caught.value.message
        assert line not in (caught.value.hint or "")
