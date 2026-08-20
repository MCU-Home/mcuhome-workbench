# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The project file: version, identity, and what a broken one does.

The file is the project's identity, so the tests that matter here are
the ones about *refusing*: a version these tools do not speak, a file
somebody edited by hand, an id that is not one. None of those may be
guessed past.
"""

from __future__ import annotations

import tomllib
import uuid
from pathlib import Path

import pytest

from mcuhome.workbench.project import init_project
from mcuhome.workbench.projectfile import (
    MARKER_FILE,
    PROJECT_VERSION,
    ProjectFile,
    ProjectFileError,
    ProjectUpgradeRequired,
    ProjectVersionUnsupported,
    new_project_id,
    read_project_file,
    require_current,
    write_project_file,
)

# --- the id -----------------------------------------------------------


def test_the_project_id_is_a_uuid_v7() -> None:
    value = uuid.UUID(new_project_id())
    assert value.version == 7
    # RFC 9562's variant field: the two top bits of the clock-seq octet.
    assert value.variant == uuid.RFC_4122


def test_project_ids_are_drawn_fresh_and_lead_with_the_time() -> None:
    drawn = [new_project_id() for _ in range(20)]
    assert len(set(drawn)) == 20
    # The first 48 bits are the draw time in milliseconds; inside one
    # millisecond the random half decides, so the order is non-strict.
    stamps = [int(uuid.UUID(value).hex[:12], 16) for value in drawn]
    assert stamps == sorted(stamps), "a UUIDv7 leads with its timestamp"


def test_the_short_id_is_the_random_tail(tmp_path: Path) -> None:
    file = ProjectFile(root=tmp_path, version=PROJECT_VERSION, id=new_project_id())
    assert file.short_id == file.id[-6:]
    assert file.token == file.short_id


def test_a_project_is_named_by_its_id_in_three_spellings(tmp_path: Path) -> None:
    identifier = new_project_id()
    file = ProjectFile(root=tmp_path, version=PROJECT_VERSION, id=identifier)
    assert file.matches(identifier)
    assert file.matches(identifier.upper())
    assert file.matches(identifier.replace("-", ""))
    assert file.matches(identifier[-6:])
    assert not file.matches(identifier[-5:])
    assert not file.matches("")
    assert not file.matches(new_project_id())


def test_a_project_without_an_id_is_named_by_its_directory(tmp_path: Path) -> None:
    """Version 0 predates the id — the first migration is what draws it."""
    project = tmp_path / "attic-sensors"
    file = ProjectFile(root=project, version=0)
    assert file.token == "attic-sensors"
    assert file.matches("attic-sensors")
    assert not file.matches("something-else")


# --- reading and writing ----------------------------------------------


def test_written_files_read_back_the_same(tmp_path: Path) -> None:
    path = tmp_path / MARKER_FILE
    written = ProjectFile(root=tmp_path, version=PROJECT_VERSION, id=new_project_id())
    write_project_file(path, written)
    assert read_project_file(path) == written


def test_the_written_file_says_it_must_not_be_edited(tmp_path: Path) -> None:
    path = tmp_path / MARKER_FILE
    write_project_file(path, ProjectFile(root=tmp_path, version=1, id=new_project_id()))
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#")
    assert "DO NOT EDIT" in text
    # The header is comment, the rest is data: TOML, and only what we put in.
    assert set(tomllib.loads(text)) == {"version", "id"}


def test_a_marker_without_a_version_is_version_zero(tmp_path: Path) -> None:
    """What every project created before the file had content looks like."""
    path = tmp_path / MARKER_FILE
    path.write_text("# This file marks the root of an MCUHome project.\n", encoding="utf-8")
    file = read_project_file(path)
    assert file.version == 0
    assert file.id is None


@pytest.mark.parametrize(
    "content",
    [
        'version = "one"',
        "version = -1",
        "version = true",
        'id = "not-a-uuid"',
        "id = 12",
        "this is not toml at all",
    ],
)
def test_a_broken_project_file_is_refused_not_guessed(tmp_path: Path, content: str) -> None:
    path = tmp_path / MARKER_FILE
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ProjectFileError) as caught:
        read_project_file(path)
    assert str(path) in caught.value.message
    assert "backup" in (caught.value.hint or "")


def test_an_unreadable_project_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ProjectFileError):
        read_project_file(tmp_path / MARKER_FILE)


# --- the version gate -------------------------------------------------


def test_the_current_version_passes(tmp_path: Path) -> None:
    file = ProjectFile(root=tmp_path, version=PROJECT_VERSION, id=new_project_id())
    assert require_current(file) is file


def test_an_older_project_is_sent_to_the_upgrade(tmp_path: Path) -> None:
    with pytest.raises(ProjectUpgradeRequired) as caught:
        require_current(ProjectFile(root=tmp_path, version=0))
    assert "needs an upgrade" in caught.value.message
    hint = caught.value.hint or ""
    assert f"mcuhome project upgrade {tmp_path}" in hint
    assert "back the project up" in hint


def test_a_newer_project_asks_for_a_newer_mcuhome(tmp_path: Path) -> None:
    """The other direction, and it must never be an upgrade suggestion."""
    with pytest.raises(ProjectVersionUnsupported) as caught:
        require_current(ProjectFile(root=tmp_path, version=PROJECT_VERSION + 1))
    assert "newer version of MCUHome" in caught.value.message
    assert "project upgrade" not in (caught.value.hint or "")


# --- what init writes -------------------------------------------------


def test_init_writes_the_current_version_and_an_id(tmp_path: Path) -> None:
    result = init_project(tmp_path / "fresh")
    file = result.project.file
    assert file is not None
    assert file.version == PROJECT_VERSION
    assert file.id is not None and uuid.UUID(file.id).version == 7


def test_two_projects_get_two_ids(tmp_path: Path) -> None:
    first = init_project(tmp_path / "a").project
    second = init_project(tmp_path / "b").project
    assert first.id != second.id


def test_init_leaves_an_existing_marker_alone(tmp_path: Path) -> None:
    """Making an old project current is the upgrade's job, not --force's."""
    (tmp_path / MARKER_FILE).write_text("# old marker\n", encoding="utf-8")
    init_project(tmp_path, force=True)
    assert read_project_file(tmp_path / MARKER_FILE).version == 0
