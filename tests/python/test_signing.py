# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The firmware signing key: where it lives, what it is, how refusals read.

The signing-key invariant as implemented after the project model: the key
is **per project** — its own file, referenced from the secrets YAML
beside it under ``firmware_signing_key`` with the loader's ``!file`` tag;
the option ``signing.key`` points at a plain PEM file instead (the
dashboard's path), through whichever channel its user set it — this
module is handed the resolved value and reads no environment variable of
its own. There is deliberately no per-user default any more — and no
fallback when neither a project nor an override is given, because
guessing a directory for a private key is how two things end up signed
with keys nobody meant.

**The split these tests are built around**: ``resolve_signing_key``
answers the key that is there and writes nothing, ever, while
``create_signing_key`` is the one call that draws one. A read that
generated would mean a client showing a project's public key had
silently made that project a second vendor, so "wrote nothing" is
checked here against the whole secrets tree rather than assumed.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from mcuhome.model.errors import BuildError, ConfigError

from mcuhome.workbench.loader import FileRef, read_yaml_file
from mcuhome.workbench.project import Project, create_project
from mcuhome.workbench.signing import (
    FIRMWARE_KEY,
    SIGNING_KEY_FILE,
    create_signing_key,
    generate_key_pem,
    is_p256_private_key,
    public_key_pem,
    resolve_signing_key,
)


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return create_project(tmp_path / "project").project


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def write_key_file(path: Path, text: str | None = None) -> str:
    pem = text if text is not None else generate_key_pem()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pem, encoding="utf-8")
    path.chmod(0o600)
    return pem


def snapshot(directory: Path) -> dict[str, tuple[int, bytes | None]]:
    """Every path under *directory*, with its mode and its bytes.

    What "wrote nothing" means, checked rather than assumed: a file
    added, a byte changed, a mode relaxed or a directory created all show
    up as a difference between two of these.
    """
    if not directory.exists():
        return {}
    return {
        str(path.relative_to(directory)): (
            mode_of(path),
            path.read_bytes() if path.is_file() else None,
        )
        for path in sorted(directory.rglob("*"))
    }


# --- resolution: override, variable, project --------------------------


def test_no_project_and_no_override_is_a_refusal_in_words() -> None:
    for call in (resolve_signing_key, create_signing_key):
        with pytest.raises(BuildError) as caught:
            call(env={})
        assert "no firmware signing key" in caught.value.message
        hint = caught.value.hint or ""
        assert FIRMWARE_KEY in hint
        assert "mcuhome project init" in hint
        assert "--signing-key" in hint
        assert "signing.key" in hint


def test_the_override_beats_the_project(tmp_path: Path, project: Project) -> None:
    # One override channel, and it is this argument: whichever way the
    # user stated `signing.key` — the flag, the variable, a file — the
    # configuration layer resolved it before this module saw it.
    by_flag = tmp_path / "flag.key"
    flag_pem = write_key_file(by_flag)
    key = resolve_signing_key(by_flag, env={}, project=project)
    assert key.path == by_flag
    assert key.pem == flag_pem
    assert not key.in_secrets
    assert not project.firmware_secrets_file.exists()


def test_a_tilde_override_uses_the_stated_home(tmp_path: Path) -> None:
    pem = write_key_file(tmp_path / "home" / "my.key")
    key = resolve_signing_key("~/my.key", env={"HOME": str(tmp_path / "home")})
    assert key.pem == pem


# --- a read never writes ----------------------------------------------


def test_a_project_without_a_key_is_refused_rather_than_given_one(project: Project) -> None:
    """The behaviour the split exists for.

    A read that generated would mean every client that shows a public
    key — a dashboard listing devices, a host check — draws the
    project's vendor key as a side effect of looking at it.
    """
    before = snapshot(project.secrets_dir)
    with pytest.raises(BuildError) as caught:
        resolve_signing_key(env={}, project=project)
    assert "no firmware signing key yet" in caught.value.message
    assert "--signing-key" in (caught.value.hint or "")
    assert not project.firmware_secrets_file.exists()
    assert snapshot(project.secrets_dir) == before


def test_a_secrets_file_without_the_entry_is_refused_rather_than_completed(
    project: Project,
) -> None:
    file = project.firmware_secrets_file
    file.parent.mkdir(parents=True, mode=0o700)
    file.write_text("other: value\n", encoding="utf-8")
    file.chmod(0o600)
    before = snapshot(project.secrets_dir)
    with pytest.raises(BuildError) as caught:
        resolve_signing_key(env={}, project=project)
    assert f"names no {FIRMWARE_KEY}" in caught.value.message
    assert snapshot(project.secrets_dir) == before


def test_reading_an_existing_key_leaves_the_secrets_directory_untouched(
    project: Project,
) -> None:
    """Not a file, not a mode, not a byte — the whole tree compared."""
    created = create_signing_key(env={}, project=project)
    before = snapshot(project.secrets_dir)
    key = resolve_signing_key(env={}, project=project)
    assert not key.created
    assert key.pem == created.pem
    assert snapshot(project.secrets_dir) == before


def test_reading_a_missing_key_file_never_creates_it(tmp_path: Path) -> None:
    path = tmp_path / "fresh.key"
    with pytest.raises(BuildError) as caught:
        resolve_signing_key(path, env={})
    assert "no such file" in caught.value.message
    assert not path.exists()


# --- the project key: drawn when a caller asks ------------------------


def test_creating_the_project_key_draws_it_and_says_so(project: Project) -> None:
    key = create_signing_key(env={}, project=project)
    assert key.created
    assert key.in_secrets
    assert key.path == project.firmware_secrets_file.parent / SIGNING_KEY_FILE
    assert key.path.read_text(encoding="utf-8") == key.pem
    assert is_p256_private_key(key.pem)
    assert public_key_pem(key.pem).startswith("-----BEGIN PUBLIC KEY-----")


def test_the_generated_files_are_readable_by_nobody_else(project: Project) -> None:
    key = create_signing_key(env={}, project=project)
    assert mode_of(key.path) == 0o600
    assert mode_of(project.firmware_secrets_file) == 0o600
    assert mode_of(project.firmware_secrets_file.parent) == 0o700
    assert mode_of(project.secrets_dir) == 0o700


def test_the_generated_yaml_references_the_key_and_never_holds_it(project: Project) -> None:
    key = create_signing_key(env={}, project=project)
    text = project.firmware_secrets_file.read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("#")  # the file explains itself
    assert f"{FIRMWARE_KEY}: !file {SIGNING_KEY_FILE}" in text
    assert "PRIVATE KEY" not in text  # the material lives in the pem alone
    reference = read_yaml_file(project.firmware_secrets_file)[FIRMWARE_KEY]
    assert isinstance(reference, FileRef)
    assert reference.path == key.path
    assert str(reference) == key.pem


def test_creating_twice_answers_the_first_key_rather_than_a_second(project: Project) -> None:
    """Two keys is the accident worth ruling out: a device only accepts
    images signed with the key its bootloader carries."""
    first = create_signing_key(env={}, project=project)
    after_first = snapshot(project.secrets_dir)
    second = create_signing_key(env={}, project=project)
    assert not second.created
    assert second.pem == first.pem
    assert snapshot(project.secrets_dir) == after_first


def test_the_key_is_added_to_an_existing_secrets_file_without_disturbing_it(
    project: Project,
) -> None:
    file = project.firmware_secrets_file
    file.parent.mkdir(parents=True, mode=0o700)
    file.write_text("# my notes\nother: value\n", encoding="utf-8")
    file.chmod(0o600)
    key = create_signing_key(env={}, project=project)
    assert key.created
    text = file.read_text(encoding="utf-8")
    assert "# my notes" in text
    assert f"{FIRMWARE_KEY}: !file {SIGNING_KEY_FILE}" in text
    data = read_yaml_file(file)
    assert data["other"] == "value"
    assert is_p256_private_key(str(data[FIRMWARE_KEY]))


@pytest.mark.parametrize("creating", [False, True])
def test_an_inline_pem_is_refused_toward_the_two_file_shape(
    project: Project, creating: bool
) -> None:
    """The retired literal form draws the migration, not a silent read."""
    file = project.firmware_secrets_file
    file.parent.mkdir(parents=True, mode=0o700)
    pem = generate_key_pem()
    body = "\n".join("  " + line for line in pem.strip().splitlines())
    original = f"{FIRMWARE_KEY}: |\n{body}\n"
    file.write_text(original, encoding="utf-8")
    file.chmod(0o600)
    with pytest.raises(BuildError) as caught:
        if creating:
            create_signing_key(env={}, project=project)
        else:
            resolve_signing_key(env={}, project=project)
    assert "must be a !file reference" in caught.value.message
    hint = caught.value.hint or ""
    assert f"!file {SIGNING_KEY_FILE}" in hint
    assert "chmod 600" in hint
    assert file.read_text(encoding="utf-8") == original  # nothing touched


@pytest.mark.parametrize("creating", [False, True])
def test_a_referenced_file_that_is_not_a_key_is_never_overwritten(
    project: Project, creating: bool
) -> None:
    file = project.firmware_secrets_file
    file.parent.mkdir(parents=True, mode=0o700)
    bogus = file.parent / SIGNING_KEY_FILE
    bogus.write_text("not a key\n", encoding="utf-8")
    bogus.chmod(0o600)
    file.write_text(f"{FIRMWARE_KEY}: !file {SIGNING_KEY_FILE}\n", encoding="utf-8")
    file.chmod(0o600)
    with pytest.raises(BuildError) as caught:
        if creating:
            create_signing_key(env={}, project=project)
        else:
            resolve_signing_key(env={}, project=project)
    assert "not an ECDSA P-256 private key" in caught.value.message
    assert str(bogus) in caught.value.message
    assert bogus.read_text(encoding="utf-8") == "not a key\n"


def test_a_secrets_file_that_is_not_a_mapping_is_refused(project: Project) -> None:
    file = project.firmware_secrets_file
    file.parent.mkdir(parents=True, mode=0o700)
    file.write_text("- a list\n", encoding="utf-8")
    file.chmod(0o600)
    with pytest.raises(BuildError) as caught:
        resolve_signing_key(env={}, project=project)
    assert "not a mapping" in caught.value.message


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_an_exposed_project_key_file_is_refused_outright(project: Project) -> None:
    create_signing_key(env={}, project=project)
    project.firmware_secrets_file.chmod(0o644)
    with pytest.raises(ConfigError) as caught:
        resolve_signing_key(env={}, project=project)
    assert "refuses to use the key material" in caught.value.message
    assert "chmod 600" in (caught.value.hint or "")


# --- the key file a caller names outright -----------------------------


def test_a_missing_key_file_is_created_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "fresh.key"
    key = create_signing_key(path=path, env={})
    assert key.created
    assert not key.in_secrets
    assert mode_of(path) == 0o600
    assert is_p256_private_key(path.read_text(encoding="utf-8"))


def test_a_tilde_in_a_created_path_uses_the_stated_home(tmp_path: Path) -> None:
    key = create_signing_key(path=Path("~/drawn.key"), env={"HOME": str(tmp_path / "home")})
    assert key.path == tmp_path / "home" / "drawn.key"
    assert key.created


def test_a_key_from_elsewhere_is_used_as_it_is(tmp_path: Path) -> None:
    path = tmp_path / "imported.key"
    pem = write_key_file(path)
    read = resolve_signing_key(path, env={})
    assert not read.created
    assert read.pem == pem
    # And asking for one where there is one answers that one, unchanged.
    made = create_signing_key(path=path, env={})
    assert not made.created
    assert made.pem == pem


@pytest.mark.parametrize("creating", [False, True])
def test_a_file_that_is_not_a_key_is_never_overwritten(tmp_path: Path, creating: bool) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("not a key\n", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(BuildError) as caught:
        if creating:
            create_signing_key(path=path, env={})
        else:
            resolve_signing_key(path, env={})
    assert "not an ECDSA P-256 private key" in caught.value.message
    assert path.read_text(encoding="utf-8") == "not a key\n"


def test_binary_rubbish_is_refused_as_a_key_rather_than_as_an_encoding(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rubbish.key"
    path.write_bytes(bytes(range(256)))
    path.chmod(0o600)
    with pytest.raises(BuildError) as caught:
        resolve_signing_key(path, env={})
    assert "not an ECDSA P-256 private key" in caught.value.message


def test_a_directory_as_key_path_is_reported_as_such(tmp_path: Path) -> None:
    with pytest.raises(BuildError) as caught:
        resolve_signing_key(tmp_path, env={})
    assert "it is a directory" in caught.value.message


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_an_exposed_key_file_is_refused_outright(tmp_path: Path) -> None:
    path = tmp_path / "loose.key"
    write_key_file(path)
    path.chmod(0o644)
    with pytest.raises(ConfigError) as caught:
        resolve_signing_key(path, env={})
    assert "refuses to use the key material" in caught.value.message


# --- the key is a file, ready for imgtool ----------------------------


def test_the_project_key_is_the_referenced_file_itself(project: Project) -> None:
    """Nothing is materialized and nothing cleaned up: the resolved path
    IS the durable key file, and the directory holds exactly the two
    files of the shape — no scratch ever existed."""
    key = create_signing_key(env={}, project=project)
    assert key.path.is_file()
    assert mode_of(key.path) == 0o600
    names = sorted(entry.name for entry in key.path.parent.iterdir())
    assert names == sorted([project.firmware_secrets_file.name, SIGNING_KEY_FILE])


def test_a_missing_referenced_key_file_is_a_located_refusal(project: Project) -> None:
    file = project.firmware_secrets_file
    file.parent.mkdir(parents=True, mode=0o700)
    file.write_text(f"{FIRMWARE_KEY}: !file gone.pem\n", encoding="utf-8")
    file.chmod(0o600)
    with pytest.raises(ConfigError) as caught:
        resolve_signing_key(env={}, project=project)
    assert "gone.pem" in caught.value.message
    assert "does not exist" in caught.value.message
    assert caught.value.location is not None
    assert caught.value.location.line == 1


def test_an_unreferenced_key_at_the_canonical_spot_is_adopted_not_overwritten(
    project: Project,
) -> None:
    """A user-imported pem — or the remains of a crash between the two
    writes — is referenced as it stands; generating over existing key
    material is the one thing creation must never do."""
    pem = write_key_file(project.firmware_secrets_file.parent / SIGNING_KEY_FILE)
    key = create_signing_key(env={}, project=project)
    assert not key.created  # no new material came into the world
    assert key.pem == pem
    assert f"!file {SIGNING_KEY_FILE}" in project.firmware_secrets_file.read_text(encoding="utf-8")


def test_a_key_under_another_name_is_refused_rather_than_doubled(project: Project) -> None:
    """The layout moved, the key did not: two keys is the accident.

    A device accepts images signed with the key its bootloader carries,
    so a project that ends up holding two cannot say which one that is.
    Adoption stays limited to the canonical name; anything else is a
    refusal that names the file and the way to move it.
    """
    directory = project.firmware_secrets_file.parent
    directory.mkdir(parents=True, mode=0o700)
    write_key_file(directory / "mcuboot.pem")
    before = snapshot(project.secrets_dir)

    with pytest.raises(BuildError) as caught:
        create_signing_key(env={}, project=project)

    assert "mcuboot.pem" in caught.value.message
    assert "mcuhome project upgrade" in (caught.value.hint or "")
    assert SIGNING_KEY_FILE in (caught.value.hint or "")
    assert snapshot(project.secrets_dir) == before, "no second key, no reference, nothing"


def test_a_referenced_key_under_another_name_is_used_as_it_stands(project: Project) -> None:
    """The same file, named in the secrets YAML: that is not ambiguous.

    The refusal above is about key material nothing points at. A project
    whose YAML names its key is answered with that key, whatever it is
    called.
    """
    directory = project.firmware_secrets_file.parent
    directory.mkdir(parents=True, mode=0o700)
    pem = write_key_file(directory / "mcuboot.pem")
    project.firmware_secrets_file.write_text(
        f"{FIRMWARE_KEY}: !file mcuboot.pem\n", encoding="utf-8"
    )
    project.firmware_secrets_file.chmod(0o600)
    key = create_signing_key(env={}, project=project)
    assert not key.created
    assert key.pem == pem
    assert key.path == directory / "mcuboot.pem"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_an_exposed_referenced_key_file_is_refused_outright(project: Project) -> None:
    key = create_signing_key(env={}, project=project)
    key.path.chmod(0o644)
    with pytest.raises(ConfigError) as caught:
        resolve_signing_key(env={}, project=project)
    assert "refuses to use the key material" in caught.value.message
    assert str(key.path) in caught.value.message


# --- no refusal ever prints the key -----------------------------------


def test_no_refusal_ever_prints_the_key(project: Project) -> None:
    key = create_signing_key(env={}, project=project)
    project.firmware_secrets_file.chmod(0o644)
    with pytest.raises((BuildError, ConfigError)) as caught:
        resolve_signing_key(env={}, project=project)
    scalars = key.pem.replace("-----BEGIN PRIVATE KEY-----", "").replace(
        "-----END PRIVATE KEY-----", ""
    )
    for line in filter(None, scalars.splitlines()):
        assert line not in caught.value.message
        assert line not in (caught.value.hint or "")


def test_the_variable_is_not_a_channel_this_module_reads(tmp_path: Path, project: Project) -> None:
    """`MCUHOME_SIGNING_KEY` is `signing.key`, and the layer reads it.

    Set in the environment and handed straight to this module, it does
    nothing: the configuration layer resolves that key once, through
    whichever channel its user chose, and what arrives here is the value
    — never a variable to look up a second time.
    """
    elsewhere = tmp_path / "elsewhere.key"
    write_key_file(elsewhere)
    create_signing_key(env={}, project=project)
    key = resolve_signing_key(env={"MCUHOME_SIGNING_KEY": str(elsewhere)}, project=project)
    assert key.path != elsewhere
    assert key.in_secrets  # the project's own, as if the variable were not set
    # Stated as the resolved option, it answers.
    assert resolve_signing_key(elsewhere, env={}, project=project).path == elsewhere
