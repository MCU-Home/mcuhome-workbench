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

import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcuhome.model.errors import ConfigError, MCUHomeError

from mcuhome.workbench.migrations import MIGRATIONS, Migration, plan_for
from mcuhome.workbench.project import init_project, resolve_project
from mcuhome.workbench.projectfile import (
    MARKER_FILE,
    PROJECT_VERSION,
    UPGRADE_FILE,
    ProjectUpgradeRequired,
    read_project_file,
)
from mcuhome.workbench.projectupgrade import (
    MigrationFailed,
    UpgradeInProgress,
    UpgradeInterrupted,
    is_upgrading,
    running_builds,
    upgrade_session,
)


def legacy_project(root: Path) -> Path:
    """A project as it looked before the file had any content: version 0."""
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER_FILE).write_text(
        "# This file marks the root of an MCUHome project.\n", encoding="utf-8"
    )
    (root / "devices").mkdir(exist_ok=True)
    return root


def hold_upgrade(root: Path, seconds: float = 5) -> subprocess.Popen:
    """Another process, holding *root* in an upgrade until it is killed."""
    code = (
        "import sys, time\n"
        "from mcuhome.workbench.projectupgrade import upgrade_session\n"
        f"with upgrade_session({str(root)!r}):\n"
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
    assert plan_for(0) == MIGRATIONS
    assert plan_for(PROJECT_VERSION) == ()


# --- the upgrade itself -----------------------------------------------


def test_an_upgrade_makes_an_old_project_current(tmp_path: Path) -> None:
    root = legacy_project(tmp_path / "old")
    with pytest.raises(ProjectUpgradeRequired):
        resolve_project(root, env={}, cwd=tmp_path)

    with upgrade_session(root) as session:
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
    with upgrade_session(root) as session:
        assert not (root / MARKER_FILE).exists()
        assert (root / UPGRADE_FILE).is_file()
        session.apply(on_event=lambda kind, _: seen.append((kind, (root / MARKER_FILE).exists())))
    assert seen == [("start", False), ("done", False)]
    assert (root / MARKER_FILE).is_file()
    assert not (root / UPGRADE_FILE).exists()


def test_the_renamed_file_names_the_process_doing_it(tmp_path: Path) -> None:
    root = legacy_project(tmp_path / "old")
    with upgrade_session(root):
        record = read_project_file(root / UPGRADE_FILE).upgrade
        assert record is not None
        assert record.process > 0
        assert record.started
    assert read_project_file(root / MARKER_FILE).upgrade is None, "the record is not kept"


def test_a_declined_upgrade_puts_the_project_back(tmp_path: Path) -> None:
    """Nothing applied, and the project usable again — the "no" case."""
    root = legacy_project(tmp_path / "old")
    with upgrade_session(root) as session:
        assert session.plan
    assert (root / MARKER_FILE).is_file()
    assert read_project_file(root / MARKER_FILE).version == 0


def test_an_abort_before_the_migrations_puts_the_project_back(tmp_path: Path) -> None:
    root = legacy_project(tmp_path / "old")
    with pytest.raises(KeyboardInterrupt), upgrade_session(root):
        raise KeyboardInterrupt
    assert (root / MARKER_FILE).is_file()
    assert read_project_file(root / MARKER_FILE).version == 0


def test_a_stop_between_migrations_ends_cleanly_at_the_version_reached(tmp_path: Path) -> None:
    """A clean stop is not a resumption: it leaves a whole, older project."""
    root = legacy_project(tmp_path / "old")
    with upgrade_session(root) as session:
        result = session.apply(should_stop=lambda: True)
    assert result.stopped
    assert result.applied == ()
    assert result.to_version == 0
    assert (root / MARKER_FILE).is_file()
    assert plan_for(result.to_version) == MIGRATIONS


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
    with pytest.raises(MigrationFailed) as caught, upgrade_session(root) as session:
        session.plan = (broken,)
        session.apply()
    assert "disk is on fire" in caught.value.message
    assert "Restore the backup" in (caught.value.hint or "")
    assert (root / UPGRADE_FILE).is_file(), "the project stays marked as being upgraded"
    assert not (root / MARKER_FILE).exists()

    # And every command now says what happened, rather than "no project".
    with pytest.raises(UpgradeInterrupted) as refusal:
        resolve_project(root, env={}, cwd=tmp_path)
    assert "explodes" in refusal.value.message
    assert "backup" in (refusal.value.hint or "")


def test_an_upgrade_of_a_current_project_has_nothing_to_do(tmp_path: Path) -> None:
    root = init_project(tmp_path / "fresh").project.root
    with upgrade_session(root) as session:
        assert session.plan == ()
        result = session.apply()
    assert result.applied == ()
    assert result.from_version == result.to_version == PROJECT_VERSION


def test_upgrading_something_that_is_not_a_project_refuses(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ConfigError) as caught, upgrade_session(plain):
        pass
    assert MARKER_FILE in caught.value.message
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
        with pytest.raises(UpgradeInProgress), upgrade_session(root):
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

    assert (root / UPGRADE_FILE).is_file()
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
    root = init_project(tmp_path / "fresh").project.root
    build_dir = root / "build" / "bench-node"
    build_dir.mkdir(parents=True)
    assert running_builds(root) == ()

    code = (
        "import time\n"
        "from mcuhome.workbench.buildlock import build_lock\n"
        f"with build_lock({str(build_dir)!r}, device='bench-node', operation='build'):\n"
        "    print('held', flush=True)\n"
        "    time.sleep(5)\n"
    )
    peer = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert peer.stdout is not None
    peer.stdout.readline()
    try:
        busy = running_builds(root)
        assert [entry.name for entry in busy] == ["bench-node"]
        assert busy[0].operation == "build"
        assert busy[0].process == str(peer.pid)
        # And the session answers the same question, for the caller's wait.
        with upgrade_session(root) as session:
            assert [entry.name for entry in session.running_builds()] == ["bench-node"]
    finally:
        peer.kill()
        peer.wait()

    assert running_builds(root) == (), "the kernel releases the lock with the process"
