# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Upgrading a project: the rename, the states it produces, the plan.

The interesting half of this module is what happens to *other* commands
while an upgrade runs, and what is left behind when one is killed — so
the concurrency tests spawn a **real second process**. A second session
inside this one would prove nothing: the rename and the lock are both
about processes, and an in-process imitation would pass whether the
mechanism works or not.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcuhome.model.errors import ConfigError, MCUHomeError

from mcuhome.workbench.configuration import resolve_builder, resolve_settings
from mcuhome.workbench.migrations import MIGRATIONS, Migration, plan_upgrade, v2_secrets_layout
from mcuhome.workbench.project import create_project, resolve_project
from mcuhome.workbench.projectfile import (
    PROJECT_MARKER_FILE,
    PROJECT_VERSION,
    UPGRADE_MARKER_FILE,
    ProjectFile,
    ProjectUpgradeRequired,
    new_project_id,
    read_project_file,
    write_project_file,
)
from mcuhome.workbench.projectupgrade import (
    MigrationFailed,
    UpgradeInProgress,
    UpgradeInterrupted,
    find_running_builds,
    is_upgrading,
    open_upgrade_session,
)
from mcuhome.workbench.secrets import find_secret_scopes
from mcuhome.workbench.signing import create_signing_key, generate_key_pem, resolve_signing_key


def legacy_project(root: Path) -> Path:
    """A project as it looked before the file had any content: version 0."""
    root.mkdir(parents=True, exist_ok=True)
    (root / PROJECT_MARKER_FILE).write_text(
        "# This file marks the root of an MCUHome project.\n", encoding="utf-8"
    )
    (root / "devices").mkdir(exist_ok=True)
    return root


def hold_upgrade(root: Path, seconds: float = 5) -> subprocess.Popen:
    """Another process, holding *root* in an upgrade until it is killed."""
    code = (
        "import sys, time\n"
        "from mcuhome.workbench.projectupgrade import open_upgrade_session\n"
        f"with open_upgrade_session({str(root)!r}):\n"
        "    print('held', flush=True)\n"
        f"    time.sleep({seconds})\n"
    )
    peer = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert peer.stdout is not None
    assert peer.stdout.readline().strip() == "held", "the peer never took the project"
    return peer


# --- the plan ---------------------------------------------------------


def test_the_migration_chain_has_no_gaps_and_ends_at_the_current_version() -> None:
    """The one invariant an explicit list can get wrong (migrations/__init__)."""
    version = 0
    for migration in MIGRATIONS:
        assert migration.from_version == version, f"{migration.name} does not follow {version}"
        assert migration.to_version == version + 1, "a migration is exactly one version step"
        version = migration.to_version
    assert version == PROJECT_VERSION, "the chain must reach the version the tools speak"


def test_every_migration_explains_itself_twice() -> None:
    """A line for the plan, and the long form a user reads afterwards."""
    for migration in MIGRATIONS:
        assert migration.description.strip()
        assert "\n" not in migration.description
        assert len(migration.details.splitlines()) > 1


def test_the_plan_is_what_is_still_missing() -> None:
    assert plan_upgrade(0) == MIGRATIONS
    assert plan_upgrade(PROJECT_VERSION) == ()


# --- the upgrade itself -----------------------------------------------


def test_an_upgrade_makes_an_old_project_current(tmp_path: Path) -> None:
    root = legacy_project(tmp_path / "old")
    with pytest.raises(ProjectUpgradeRequired):
        resolve_project(root, env={}, cwd=tmp_path)

    with open_upgrade_session(root) as session:
        result = session.apply()

    assert result.from_version == 0
    assert result.to_version == PROJECT_VERSION
    assert [migration.name for migration in result.applied] == [m.name for m in MIGRATIONS]
    project = resolve_project(root, env={}, cwd=tmp_path)
    assert project.file is not None and project.file.version == PROJECT_VERSION
    assert project.id is not None


def test_the_project_file_is_renamed_for_the_whole_run(tmp_path: Path) -> None:
    """The rename is the guard: while it holds, the project is not findable."""
    root = legacy_project(tmp_path / "old")
    seen = []
    with open_upgrade_session(root) as session:
        assert not (root / PROJECT_MARKER_FILE).exists()
        assert (root / UPGRADE_MARKER_FILE).is_file()
        session.apply(
            on_step=lambda key, **_: seen.append((key, (root / PROJECT_MARKER_FILE).exists()))
        )
    assert seen == [
        (key, False) for _ in MIGRATIONS for key in ("migration_started", "migration_done")
    ]
    assert (root / PROJECT_MARKER_FILE).is_file()
    assert not (root / UPGRADE_MARKER_FILE).exists()


def test_a_migration_reports_itself_as_a_step(tmp_path: Path) -> None:
    """The progress vocabulary, with the facts a client renders.

    The same shape a build reports with — a key and keyword facts — so a
    client that shows one shows the other, and the name and the two
    versions are what "renaming the project identity, 1 to 2" is made
    of.
    """
    root = legacy_project(tmp_path / "old")
    steps: list[tuple[str, dict[str, object]]] = []
    with open_upgrade_session(root) as session:
        session.apply(on_step=lambda key, **facts: steps.append((key, facts)))

    assert [key for key, _ in steps] == [
        key for _ in MIGRATIONS for key in ("migration_started", "migration_done")
    ]
    for migration, (_, facts) in zip(
        [one for one in MIGRATIONS for _ in range(2)], steps, strict=True
    ):
        assert facts == {
            "name": migration.name,
            "from_version": migration.from_version,
            "to_version": migration.to_version,
        }


def test_the_renamed_file_names_the_process_doing_it(tmp_path: Path) -> None:
    root = legacy_project(tmp_path / "old")
    with open_upgrade_session(root):
        record = read_project_file(root / UPGRADE_MARKER_FILE).upgrade
        assert record is not None
        assert record.process > 0
        assert record.started
    assert read_project_file(root / PROJECT_MARKER_FILE).upgrade is None, "the record is not kept"


def test_a_declined_upgrade_puts_the_project_back(tmp_path: Path) -> None:
    """Nothing applied, and the project usable again — the "no" case."""
    root = legacy_project(tmp_path / "old")
    with open_upgrade_session(root) as session:
        assert session.plan
    assert (root / PROJECT_MARKER_FILE).is_file()
    assert read_project_file(root / PROJECT_MARKER_FILE).version == 0


def test_an_abort_before_the_migrations_puts_the_project_back(tmp_path: Path) -> None:
    root = legacy_project(tmp_path / "old")
    with pytest.raises(KeyboardInterrupt), open_upgrade_session(root):
        raise KeyboardInterrupt
    assert (root / PROJECT_MARKER_FILE).is_file()
    assert read_project_file(root / PROJECT_MARKER_FILE).version == 0


def test_a_stop_between_migrations_ends_cleanly_at_the_version_reached(tmp_path: Path) -> None:
    """A clean stop is not a resumption: it leaves a whole, older project."""
    root = legacy_project(tmp_path / "old")
    with open_upgrade_session(root) as session:
        result = session.apply(should_stop=lambda: True)
    assert result.stopped
    assert result.applied == ()
    assert result.to_version == 0
    assert (root / PROJECT_MARKER_FILE).is_file()
    assert plan_upgrade(result.to_version) == MIGRATIONS


def test_a_failing_migration_leaves_the_project_marked_and_says_so(tmp_path: Path) -> None:
    """No repair, no guessing: the supported way out is the backup."""
    root = legacy_project(tmp_path / "old")

    def explode(_root, _file):
        raise RuntimeError("disk is on fire")

    broken = Migration(
        from_version=0,
        to_version=1,
        name="explodes",
        description="fail on purpose",
        details="x\ny",
        run=explode,
    )
    with pytest.raises(MigrationFailed) as caught, open_upgrade_session(root) as session:
        session.plan = (broken,)
        session.apply()
    assert "disk is on fire" in caught.value.message
    assert "Restore the backup" in (caught.value.hint or "")
    assert (root / UPGRADE_MARKER_FILE).is_file(), "the project stays marked as being upgraded"
    assert not (root / PROJECT_MARKER_FILE).exists()

    # And every command now says what happened, rather than "no project".
    with pytest.raises(UpgradeInterrupted) as refusal:
        resolve_project(root, env={}, cwd=tmp_path)
    assert "explodes" in refusal.value.message
    assert "backup" in (refusal.value.hint or "")


def test_an_upgrade_of_a_current_project_has_nothing_to_do(tmp_path: Path) -> None:
    root = create_project(tmp_path / "fresh").project.root
    with open_upgrade_session(root) as session:
        assert session.plan == ()
        result = session.apply()
    assert result.applied == ()
    assert result.from_version == result.to_version == PROJECT_VERSION


def test_upgrading_something_that_is_not_a_project_refuses(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ConfigError) as caught, open_upgrade_session(plain):
        pass
    assert PROJECT_MARKER_FILE in caught.value.message
    assert "mcuhome project init" in (caught.value.hint or "")


# --- two processes ----------------------------------------------------


def test_while_one_upgrade_runs_every_other_command_says_so(tmp_path: Path) -> None:
    root = legacy_project(tmp_path / "old")
    peer = hold_upgrade(root)
    try:
        assert is_upgrading(root)
        with pytest.raises(UpgradeInProgress) as caught:
            resolve_project(root, env={}, cwd=tmp_path)
        assert "being upgraded right now" in caught.value.message
        assert str(peer.pid) in caught.value.message
        assert "wait" in (caught.value.hint or "")
    finally:
        peer.kill()
        peer.wait()


def test_a_second_upgrade_is_refused_not_run(tmp_path: Path) -> None:
    root = legacy_project(tmp_path / "old")
    peer = hold_upgrade(root)
    try:
        with pytest.raises(UpgradeInProgress), open_upgrade_session(root):
            pytest.fail("two upgrades entered the same project")
    finally:
        peer.kill()
        peer.wait()


def test_a_killed_upgrade_is_told_apart_from_a_running_one(tmp_path: Path) -> None:
    """The kernel answers it: a lock nobody holds means nobody is there."""
    root = legacy_project(tmp_path / "old")
    peer = hold_upgrade(root)
    peer.kill()
    peer.wait()
    for _ in range(50):  # the lock is released with the process, not with a poll
        if not is_upgrading(root):
            break
        time.sleep(0.02)

    assert (root / UPGRADE_MARKER_FILE).is_file()
    with pytest.raises(UpgradeInterrupted) as caught:
        resolve_project(root, env={}, cwd=tmp_path)
    assert "interrupted" in caught.value.message
    assert "Restore the backup" in (caught.value.hint or "")


def test_the_upward_search_stops_at_a_project_being_upgraded(tmp_path: Path) -> None:
    """Walking past it would report "no project" for a project in plain sight."""
    root = legacy_project(tmp_path / "old")
    deep = root / "devices"
    peer = hold_upgrade(root)
    try:
        with pytest.raises(MCUHomeError) as caught:
            resolve_project(env={}, cwd=deep)
        assert "being upgraded" in caught.value.message
    finally:
        peer.kill()
        peer.wait()


# --- builds that are still running ------------------------------------


def test_a_running_build_is_reported_so_the_caller_can_wait(tmp_path: Path) -> None:
    root = create_project(tmp_path / "fresh").project.root
    build_dir = root / "build" / "bench-node"
    build_dir.mkdir(parents=True)
    assert find_running_builds(root) == ()

    code = (
        "import time\n"
        "from mcuhome.workbench.buildlock import open_build_lock\n"
        f"with open_build_lock({str(build_dir)!r}, device='bench-node', operation='build'):\n"
        "    print('held', flush=True)\n"
        "    time.sleep(5)\n"
    )
    peer = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert peer.stdout is not None
    peer.stdout.readline()
    try:
        busy = find_running_builds(root)
        assert [entry.name for entry in busy] == ["bench-node"]
        assert busy[0].operation == "build"
        assert busy[0].process == str(peer.pid)
        # And the session answers the same question, for the caller's wait.
        with open_upgrade_session(root) as session:
            assert [entry.name for entry in session.running_builds()] == ["bench-node"]
    finally:
        peer.kill()
        peer.wait()

    assert find_running_builds(root) == (), "the kernel releases the lock with the process"


# --- 1 → 2: the secrets layout ----------------------------------------
#
# The fixture builds the old layout from literals rather than from the
# package's constants: what a version-1 project has on disk is a fact of
# the past, and a constant that moves must not move this fixture with it.


def v1_project(
    root: Path,
    *,
    key_file: str = "mcuboot.pem",
    referenced: str | None = "mcuboot.pem",
) -> Path:
    """A project as version 1 left it: secrets by technology, not by kind.

    *key_file* is the name the private key lies under and *referenced*
    what the secrets YAML points at — the two differ for the project
    whose YAML lost its entry, which is the case the migration has to
    adopt.
    """
    root.mkdir(parents=True, exist_ok=True)
    write_project_file(
        root / PROJECT_MARKER_FILE,
        ProjectFile(root=root, version=1, id=new_project_id()),
    )
    (root / "devices" / "porch").mkdir(parents=True, exist_ok=True)
    (root / "devices" / "porch" / "main.yaml").write_text(
        "device:\n  name: porch\n  board: nrf7002dk/nrf5340/cpuapp\n", encoding="utf-8"
    )
    secrets = root / "secrets"
    secrets.mkdir(exist_ok=True)
    os.chmod(secrets, 0o700)
    _write_secret(secrets / "main.yaml", "wifi_password: hunter2\n")
    _write_secret(secrets / "devices" / "porch.yaml", "device_label: porch\n")
    _write_secret(secrets / "build-server" / "attic.yaml", "token: t0ken\n")
    _write_secret(secrets / "firmware" / key_file, generate_key_pem())
    _write_secret(secrets / "firmware" / "signing.pub", "-----BEGIN PUBLIC KEY-----\n")
    if referenced is not None:
        _write_secret(
            secrets / "firmware" / "mcuboot.yaml",
            f"# MCUHome firmware signing key.\nfirmware_signing_key: !file {referenced}\n",
        )
    return root


def _write_secret(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def upgrade(root: Path) -> None:
    """Run the real upgrade, the way ``mcuhome project upgrade`` does."""
    with open_upgrade_session(root) as session:
        session.apply()


def tree_of(directory: Path) -> dict[str, tuple[str, int]]:
    """Every file below *directory*, with its text and its mode."""
    return {
        str(path.relative_to(directory)): (
            path.read_text(encoding="utf-8"),
            stat.S_IMODE(path.stat().st_mode),
        )
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_a_project_of_the_old_layout_is_refused_until_it_is_upgraded(tmp_path: Path) -> None:
    root = v1_project(tmp_path / "old")
    with pytest.raises(ProjectUpgradeRequired) as caught:
        resolve_project(root, env={}, cwd=tmp_path)
    assert "mcuhome project upgrade" in (caught.value.hint or "")


def test_the_secrets_move_into_one_directory_per_kind(tmp_path: Path) -> None:
    """The move itself: every file where version 2 says, content intact."""
    root = v1_project(tmp_path / "old")
    before = tree_of(root / "secrets")

    upgrade(root)

    secrets = root / "secrets"
    assert not (secrets / "firmware").exists()
    assert not (secrets / "devices").exists()
    assert not (secrets / "build-server").exists()
    assert tree_of(secrets) == {
        "main.yaml": before["main.yaml"],
        "device/porch.yaml": before["devices/porch.yaml"],
        "builder/attic.yaml": before["build-server/attic.yaml"],
        "signing/key.pem": before["firmware/mcuboot.pem"],
        "signing/key.pub": before["firmware/signing.pub"],
        "signing/key.yaml": (
            "# MCUHome firmware signing key.\nfirmware_signing_key: !file key.pem\n",
            0o600,
        ),
    }


def test_the_upgraded_project_works_through_the_calls_that_read_those_files(
    tmp_path: Path,
) -> None:
    """Not the paths but the functions: the project is usable afterwards.

    The signing key resolves, the device's own secrets answer, and the
    builder's token is found — each through the call a command makes,
    against the project the upgrade just produced.
    """
    root = v1_project(tmp_path / "old")
    pem = (root / "secrets" / "firmware" / "mcuboot.pem").read_text(encoding="utf-8")
    (root / "mcuhome.yaml").write_text(
        "builder:\n  attic:\n    target: remote\n    server: 10.0.0.5:8291\n", encoding="utf-8"
    )

    upgrade(root)

    project = resolve_project(root, env={}, cwd=tmp_path)
    key = resolve_signing_key(env={}, project=project)
    assert key.pem == pem
    assert key.path == project.secrets_dir / "signing" / "key.pem"

    scopes = {(scope.kind, scope.name): scope for scope in find_secret_scopes(project)}
    assert scopes[("device", "porch")].exists
    assert scopes[("builder", "attic")].exists
    assert scopes[("signing", "")].file == project.signing_secrets_file

    settings = resolve_settings(project=project, env={})
    assert resolve_builder(settings, name="attic", project=project, env={}).token == "t0ken"


def test_an_upgrade_that_was_interrupted_finishes_on_the_next_run(tmp_path: Path) -> None:
    """Half a move is a state the next attempt completes, not one it trips over."""
    root = v1_project(tmp_path / "old")
    secrets = root / "secrets"
    # What a run that died between two moves leaves: the first directory
    # is already in the new layout, the rest is untouched.
    os.replace(secrets / "devices", secrets / "device")
    v2_secrets_layout.migrate(root, read_project_file(root / PROJECT_MARKER_FILE, root=root))

    assert (secrets / "device" / "porch.yaml").is_file()
    assert (secrets / "builder" / "attic.yaml").is_file()
    assert (secrets / "signing" / "key.pem").is_file()
    assert not (secrets / "devices").exists()


def test_a_project_that_is_already_in_shape_is_not_touched(tmp_path: Path) -> None:
    """Idempotent: running it again changes nothing, byte for byte."""
    root = v1_project(tmp_path / "old")
    upgrade(root)
    after = tree_of(root / "secrets")

    file = read_project_file(root / PROJECT_MARKER_FILE, root=root)
    v2_secrets_layout.migrate(root, file)
    v2_secrets_layout.migrate(root, file)

    assert tree_of(root / "secrets") == after


def test_a_name_that_exists_in_both_places_keeps_the_one_already_moved(tmp_path: Path) -> None:
    """Nothing is overwritten, and what could not move stays visible.

    The window this covers is real: a user who created
    ``secrets/builder/attic.yaml`` by hand while the old file was still
    there must not lose the one MCUHome has been reading.
    """
    root = v1_project(tmp_path / "old")
    secrets = root / "secrets"
    _write_secret(secrets / "builder" / "attic.yaml", "token: the-new-one\n")
    _write_secret(secrets / "build-server" / "other.yaml", "token: moves\n")

    upgrade(root)

    kept = secrets / "builder" / "attic.yaml"
    assert kept.read_text(encoding="utf-8") == "token: the-new-one\n"
    assert (secrets / "builder" / "other.yaml").read_text(encoding="utf-8") == "token: moves\n"
    left = secrets / "build-server" / "attic.yaml"
    assert left.is_file(), "the file that could not move is left where it was"
    assert left.read_text(encoding="utf-8") == "token: t0ken\n"


def test_a_key_no_reference_names_is_adopted(tmp_path: Path) -> None:
    """The case the signing refusal points here for.

    A project whose secrets YAML lost its ``firmware_signing_key`` entry
    still has the key on disk, and creation refuses to draw a second one
    beside it. The migration adopts that file — and ``create_signing_key``
    then answers it rather than making a key.
    """
    root = v1_project(tmp_path / "old", referenced=None)
    pem = (root / "secrets" / "firmware" / "mcuboot.pem").read_text(encoding="utf-8")

    upgrade(root)

    project = resolve_project(root, env={}, cwd=tmp_path)
    assert (project.secrets_dir / "signing" / "key.pem").read_text(encoding="utf-8") == pem
    key = create_signing_key(env={}, project=project)
    assert not key.created, "the adopted key is the project's key"
    assert key.pem == pem
    assert "!file key.pem" in project.signing_secrets_file.read_text(encoding="utf-8")


def test_a_key_the_user_keeps_somewhere_else_is_left_as_it_is(tmp_path: Path) -> None:
    """A reference with a path in it names a file the user placed; it stays."""
    root = v1_project(tmp_path / "old", key_file="own.pem", referenced="../own/key.pem")
    _write_secret(root / "secrets" / "own" / "key.pem", generate_key_pem())

    upgrade(root)

    secrets = root / "secrets"
    assert (secrets / "own" / "key.pem").is_file()
    assert "!file ../own/key.pem" in (secrets / "signing" / "key.yaml").read_text(encoding="utf-8")
    assert (secrets / "signing" / "own.pem").is_file(), "no file is renamed behind the reference"
