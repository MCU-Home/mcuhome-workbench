# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Driving a step of the build environment specification (``buildenvsession.py``).

**No build environment runs here.** The entry point is a shell script
these tests write: it reads the request document the driver placed, notes
what it found (the environment it was given, the request verbatim, how
many entries ``work`` held) into ``out``, and writes whatever result
document the test wants. That is the whole of the specification's
boundary, so a fake that keeps it is enough to test the side that drives
it — and it is the only way to test the arms a real environment never
takes: a document from another specification generation, a success after
a non-zero exit, an entry point that never ends.

The subject is the driver's three promises: §4's tree laid out fresh per
step with ``out`` carried across the session, §6's two documents, and
§6.3's exit-code rule judged together with what the document says.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from mcuhome.workbench import buildenvsession as session_module
from mcuhome.workbench.buildenvsession import (
    ACTION_BUILD,
    BASE_DIR_VAR,
    BuilderSession,
    CacheTier,
    Step,
)
from mcuhome.workbench.orchestrator import Running, spawn_process

# --------------------------------------------------------------------------
# The fake entry point
# --------------------------------------------------------------------------

#: What every fake entry point does before it answers: resolve §4's tree
#: against the one environment variable, and record into ``out`` what a
#: test cannot see from outside — the environment it was handed, the
#: request document it was given, and whether ``work`` was empty.
_PREAMBLE = r"""#!/bin/sh
set -eu
base="$MCUHOME_BUILDER_BASE_DIR"
mc="$base/mcuhome"
id=$(sed -n 's/.*"invocation_id": "\([^"]*\)".*/\1/p' "$mc/invocation-request.json")
env > "$mc/out/environment-$id.txt"
cp "$mc/invocation-request.json" "$mc/out/request-$id.json"
ls -A "$mc/work" | wc -l > "$mc/out/work-entries-$id.txt"
"""


def result(
    status: str = "success",
    *,
    artifacts: tuple[str, ...] = (),
    generation: int = 3,
    invocation: str = "$id",
    message: str = "",
) -> str:
    """Shell that writes one result document (§6.2)."""
    declared = ", ".join(f'"{name}"' for name in artifacts)
    return (
        'cat > "$mc/out/result-$id.json" <<EOF\n'
        f'{{"spec_generation": {generation}, "invocation_id": "{invocation}", '
        f'"status": "{status}", "message": "{message}", "artifacts": [{declared}]}}\n'
        "EOF\n"
    )


def entry_point(directory: Path, body: str) -> Path:
    """A fake entry point at the path a tools package keeps one."""
    path = directory / "bin" / session_module.ENTRY_POINT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_PREAMBLE + body, encoding="utf-8")
    path.chmod(0o755)
    return path


#: The bytes the fake build delivers, and their hash — measured by the
#: driver from disk, because a v3 result document declares none.
FIRMWARE_SHA256 = "407d47dc1f3fb482d08a63269de7eaf19e56590672c78c9bc1bbcc4bb110ba19"

#: A build that delivers one file and says so.
DELIVERS = (
    'printf FIRMWARE > "$mc/out/firmware.bin"\n' + result(artifacts=("firmware.bin",)) + "exit 0\n"
)


def launcher(env: dict[str, str] | None = None):
    """The profile seam, reduced to what every profile has to do.

    Run the entry point at the path the specification fixes, with no
    arguments and with a stated environment. The subprocess profile's own
    launcher adds the environment a real build needs
    (:mod:`mcuhome.workbench.subprocessbuild`); this one adds nothing, so
    what these tests observe is the driver alone.
    """

    def launch(step: Step, on_line) -> Running:
        stated = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            BASE_DIR_VAR: str(step.base_dir),
            **(env or {}),
        }
        return spawn_process([str(step.entry_point)], env=stated, cwd=step.work, on_line=on_line)

    return launch


@pytest.fixture
def environment(tmp_path: Path) -> Path:
    """A directory standing in for an unpacked build environment."""
    root = tmp_path / "environment"
    root.mkdir()
    return root


def make_session(tmp_path: Path, entry: Path, **kwargs) -> BuilderSession:
    context = tmp_path / "context"
    context.mkdir(exist_ok=True)
    (context / "context.yaml").write_text("context: 4\n", encoding="utf-8")
    sdk = tmp_path / "sdk"
    sdk.mkdir(exist_ok=True)
    (sdk / "mcuhome-sdk.json").write_text("{}", encoding="utf-8")
    kwargs.setdefault("launcher", launcher())
    kwargs.setdefault("context_id", "sha256:" + "a" * 64)
    kwargs.setdefault("entry_point", entry)
    return BuilderSession(root=tmp_path / "session", context_dir=context, sdk_tree=sdk, **kwargs)


def recorded_request(session: BuilderSession, invocation_id: str) -> dict:
    return json.loads((session.out / f"request-{invocation_id}.json").read_text(encoding="utf-8"))


def recorded_environment(session: BuilderSession, invocation_id: str) -> dict[str, str]:
    values: dict[str, str] = {}
    text = (session.out / f"environment-{invocation_id}.txt").read_text(encoding="utf-8")
    for line in text.splitlines():
        name, _, value = line.partition("=")
        if _:
            values[name] = value
    return values


# --------------------------------------------------------------------------
# §4: the tree, per step
# --------------------------------------------------------------------------


def test_a_step_gets_the_tree_the_specification_fixes(tmp_path, environment) -> None:
    entry = entry_point(environment, DELIVERS)
    session = make_session(tmp_path, entry)
    step = session.prepare(ACTION_BUILD)

    mcuhome = step.base_dir / "mcuhome"
    assert step.work == mcuhome / "work"
    assert list(step.work.iterdir()) == []
    assert (mcuhome / "out").resolve() == session.out.resolve()
    assert (mcuhome / "sdk").resolve() == session.sdk_tree
    assert (mcuhome / "build-context").resolve() == session.context_dir
    assert (mcuhome / "bin" / session_module.ENTRY_POINT).resolve() == entry.resolve()
    assert (mcuhome / "cache" / "local").is_dir()
    # Every tier the orchestrator did not provide is absent rather than
    # empty: "may be missing entirely" is what an environment reads.
    for name in ("session", "project", "shared"):
        assert not (mcuhome / "cache" / name).exists()


def test_the_request_document_states_the_five_fields(tmp_path, environment) -> None:
    entry = entry_point(environment, DELIVERS)
    session = make_session(tmp_path, entry, session_id="s1")
    step = session.prepare(ACTION_BUILD, parameters={"x": 1})

    document = json.loads(step.request.read_text(encoding="utf-8"))
    assert document == {
        "spec_generation": 3,
        "session_id": "s1",
        "invocation_id": "s1-1",
        "action": "build",
        "parameters": {"x": 1},
    }
    assert step.request.name == "invocation-request.json"


def test_the_entry_point_reads_the_request_it_was_given(tmp_path, environment) -> None:
    entry = entry_point(environment, DELIVERS)
    session = make_session(tmp_path, entry, session_id="s1")
    session.invoke(ACTION_BUILD)

    assert recorded_request(session, "s1-1")["action"] == "build"
    assert recorded_environment(session, "s1-1")[BASE_DIR_VAR].endswith("/steps/s1-1")


def test_work_is_empty_at_the_start_of_every_step(tmp_path, environment) -> None:
    entry = entry_point(
        environment,
        'mkdir -p "$mc/work/tree" && printf x > "$mc/work/tree/object.o"\n' + result() + "exit 0\n",
    )
    session = make_session(tmp_path, entry, session_id="s1")
    session.invoke(ACTION_BUILD)
    session.invoke(ACTION_BUILD)

    for invocation in ("s1-1", "s1-2"):
        entries = (session.out / f"work-entries-{invocation}.txt").read_text(encoding="utf-8")
        assert entries.strip() == "0"


def test_out_is_carried_across_the_steps_of_a_session(tmp_path, environment) -> None:
    entry = entry_point(environment, 'printf x >> "$mc/out/kept.txt"\n' + result() + "exit 0\n")
    session = make_session(tmp_path, entry, session_id="s1")
    session.invoke(ACTION_BUILD)
    session.invoke(ACTION_BUILD)

    assert (session.out / "kept.txt").read_text(encoding="utf-8") == "xx"
    assert (session.out / "result-s1-1.json").is_file()
    assert (session.out / "result-s1-2.json").is_file()


def test_the_previous_step_tree_is_removed_when_the_next_is_prepared(tmp_path, environment) -> None:
    entry = entry_point(environment, DELIVERS)
    session = make_session(tmp_path, entry, session_id="s1")
    first = session.prepare(ACTION_BUILD)
    (first.work / "big").write_bytes(b"x" * 16)
    second = session.prepare(ACTION_BUILD)

    assert not first.base_dir.exists()
    assert second.base_dir.is_dir()
    # The removal must not walk out through the links a step's tree is
    # made of: `out` is the session's, and `build-context` and `sdk` are
    # the caller's.
    assert session.out.is_dir()
    assert (session.context_dir / "context.yaml").is_file()
    assert (session.sdk_tree / "mcuhome-sdk.json").is_file()


def test_a_session_starts_with_an_empty_out_and_no_earlier_step_trees(
    tmp_path, environment
) -> None:
    """A session root is reused — the same build directory builds again."""
    entry = entry_point(environment, DELIVERS)
    first = make_session(tmp_path, entry, session_id="s1")
    first.invoke(ACTION_BUILD)
    (first.out / "stale.bin").write_bytes(b"from the last build")

    second = make_session(tmp_path, entry, session_id="s2")
    assert list(second.out.iterdir()) == []
    second.invoke(ACTION_BUILD)

    # Only this session's own artifacts and result document, and only
    # this session's step tree: one build tree per build, not per build
    # ever run in this directory.
    assert sorted(path.name for path in second.out.iterdir()) == [
        "environment-s2-1.txt",
        "firmware.bin",
        "request-s2-1.json",
        "result-s2-1.json",
        "work-entries-s2-1.txt",
    ]
    assert [path.name for path in (second.root / "steps").iterdir()] == ["s2-1"]


def test_the_cache_tiers_are_the_ones_the_orchestrator_provides(tmp_path, environment) -> None:
    entry = entry_point(environment, DELIVERS)
    durable = tmp_path / "cache" / "durable"
    shared = tmp_path / "cache" / "shared"
    shared.mkdir(parents=True)
    session = make_session(
        tmp_path,
        entry,
        tiers={
            "local": CacheTier(path=durable, writable=True),
            "shared": CacheTier(path=shared, writable=False),
        },
    )
    step = session.prepare(ACTION_BUILD)

    cache = step.base_dir / "mcuhome" / "cache"
    assert (cache / "local").resolve() == durable.resolve()
    assert (cache / "shared").resolve() == shared.resolve()
    assert not (cache / "session").exists()
    # The most local writable tier is where a compiler cache goes.
    assert step.writable_cache == durable


# --------------------------------------------------------------------------
# §6.2 and §6.3: what came back
# --------------------------------------------------------------------------


def test_a_successful_step_delivers_its_artifacts_hashed(tmp_path, environment) -> None:
    entry = entry_point(environment, DELIVERS)
    session = make_session(tmp_path, entry)
    outcome = session.invoke(ACTION_BUILD)

    assert outcome.successful
    assert outcome.status == "success"
    assert outcome.exit_code == 0
    assert outcome.problems == ()
    assert outcome.violation is None
    assert outcome.out == session.out
    assert [(a.root, a.path, a.role) for a in outcome.artifacts] == [
        ("out", "firmware.bin", "firmware")
    ]
    # The hash is measured from the bytes on disk; the document declares
    # none, and a declared one would be advisory anyway.
    assert outcome.artifacts[0].sha256 == FIRMWARE_SHA256


def test_a_failed_step_carries_the_environment_s_message(tmp_path, environment) -> None:
    entry = entry_point(environment, result("failure", message="the compile failed") + "exit 1\n")
    session = make_session(tmp_path, entry)
    outcome = session.invoke(ACTION_BUILD)

    assert not outcome.successful
    assert outcome.status == "failure"
    assert outcome.exit_code == 1
    assert outcome.violation is None
    assert any("the compile failed" in problem for problem in outcome.problems)


def test_an_unsupported_action_keeps_its_status(tmp_path, environment) -> None:
    entry = entry_point(
        environment,
        result("unsupported", message="this environment implements build") + "exit 1\n",
    )
    session = make_session(tmp_path, entry)
    outcome = session.invoke("flash")

    assert not outcome.successful
    # The word travels: it means no environment of this kind can do it,
    # and the caller decides whether to look for another one.
    assert outcome.status == "unsupported"
    assert outcome.violation is None


def test_a_success_document_after_a_non_zero_exit_is_a_violation(tmp_path, environment) -> None:
    entry = entry_point(environment, result() + "exit 3\n")
    session = make_session(tmp_path, entry)
    outcome = session.invoke(ACTION_BUILD)

    assert not outcome.successful
    assert outcome.exit_code == 3
    assert outcome.violation is not None
    assert "exited 3" in outcome.violation


def test_a_zero_exit_after_a_failure_document_is_a_violation(tmp_path, environment) -> None:
    entry = entry_point(environment, result("failure") + "exit 0\n")
    session = make_session(tmp_path, entry)
    outcome = session.invoke(ACTION_BUILD)

    assert not outcome.successful
    assert outcome.violation is not None
    assert "exited 0" in outcome.violation


def test_a_step_that_wrote_no_result_document_failed(tmp_path, environment) -> None:
    entry = entry_point(environment, "exit 0\n")
    session = make_session(tmp_path, entry)
    outcome = session.invoke(ACTION_BUILD)

    assert not outcome.successful
    assert outcome.status == "failure"
    assert outcome.result is None
    assert any("no readable result document" in problem for problem in outcome.problems)
    # Exiting zero is a claim that a success document was written.
    assert outcome.violation == "the build environment exited 0 and wrote no result document"


def test_a_result_document_of_another_generation_is_refused(tmp_path, environment) -> None:
    entry = entry_point(environment, result(generation=4) + "exit 0\n")
    session = make_session(tmp_path, entry)
    outcome = session.invoke(ACTION_BUILD)

    assert not outcome.successful
    assert any("generation 4" in problem for problem in outcome.problems)


def test_a_result_document_echoing_another_step_is_refused(tmp_path, environment) -> None:
    entry = entry_point(environment, result(invocation="somebody-else") + "exit 0\n")
    session = make_session(tmp_path, entry, session_id="s1")
    outcome = session.invoke(ACTION_BUILD)

    assert not outcome.successful
    assert any("somebody-else" in problem for problem in outcome.problems)


def test_a_status_the_specification_does_not_define_is_refused(tmp_path, environment) -> None:
    entry = entry_point(environment, result("cancelled") + "exit 1\n")
    session = make_session(tmp_path, entry)
    outcome = session.invoke(ACTION_BUILD)

    assert not outcome.successful
    assert outcome.status == "failure"
    assert any("'cancelled'" in problem for problem in outcome.problems)


# --------------------------------------------------------------------------
# §7: egress
# --------------------------------------------------------------------------


def test_a_declared_artifact_that_is_absent_is_reported(tmp_path, environment) -> None:
    entry = entry_point(environment, result(artifacts=("firmware.bin",)) + "exit 0\n")
    session = make_session(tmp_path, entry)
    outcome = session.invoke(ACTION_BUILD)

    assert not outcome.successful
    assert outcome.artifacts == ()
    assert any("not present under out" in problem for problem in outcome.problems)


def test_a_declared_artifact_that_is_a_symlink_is_rejected(tmp_path, environment) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    entry = entry_point(
        environment,
        f'ln -s {secret} "$mc/out/firmware.bin"\n'
        + result(artifacts=("firmware.bin",))
        + "exit 0\n",
    )
    session = make_session(tmp_path, entry)
    outcome = session.invoke(ACTION_BUILD)

    assert not outcome.successful
    assert outcome.artifacts == ()
    assert any("not contained in out" in problem for problem in outcome.problems)


def test_a_declared_artifact_that_leaves_out_is_rejected(tmp_path, environment) -> None:
    entry = entry_point(environment, result(artifacts=("../../escape",)) + "exit 0\n")
    session = make_session(tmp_path, entry)
    outcome = session.invoke(ACTION_BUILD)

    assert not outcome.successful
    assert any("not contained in out" in problem for problem in outcome.problems)


def test_a_fixed_name_below_the_top_of_out_is_not_the_artifact_it_names(
    tmp_path, environment
) -> None:
    """The action's table names files at the top of ``out`` and nowhere else."""
    entry = entry_point(
        environment,
        'mkdir -p "$mc/out/extra" && printf x > "$mc/out/extra/firmware.bin"\n'
        + result(artifacts=("extra/firmware.bin",))
        + "exit 0\n",
    )
    session = make_session(tmp_path, entry)
    outcome = session.invoke(ACTION_BUILD)

    assert outcome.successful
    assert [(a.path, a.role) for a in outcome.artifacts] == [("extra/firmware.bin", "")]


def test_an_artifact_the_orchestrator_has_no_role_for_is_still_carried(
    tmp_path, environment
) -> None:
    entry = entry_point(
        environment,
        'printf x > "$mc/out/notes.txt"\n' + result(artifacts=("notes.txt",)) + "exit 0\n",
    )
    session = make_session(tmp_path, entry)
    outcome = session.invoke(ACTION_BUILD)

    assert outcome.successful
    assert [(a.path, a.role) for a in outcome.artifacts] == [("notes.txt", "")]


def test_a_declared_artifact_that_is_a_hardlink_is_rejected(tmp_path, environment) -> None:
    """A second name for bytes that may live outside out."""
    outside = tmp_path / "outside.bin"
    outside.write_text("elsewhere", encoding="utf-8")
    entry = entry_point(
        environment,
        f'ln {outside} "$mc/out/firmware.bin"\n' + result(artifacts=("firmware.bin",)) + "exit 0\n",
    )
    session = make_session(tmp_path, entry)
    outcome = session.invoke(ACTION_BUILD)

    assert not outcome.successful
    assert outcome.artifacts == ()
    assert any("hardlink" in problem for problem in outcome.problems)


def test_a_declared_artifact_that_is_a_directory_is_rejected(tmp_path, environment) -> None:
    entry = entry_point(
        environment,
        'mkdir -p "$mc/out/firmware.bin"\n' + result(artifacts=("firmware.bin",)) + "exit 0\n",
    )
    session = make_session(tmp_path, entry)
    outcome = session.invoke(ACTION_BUILD)

    assert not outcome.successful
    assert any("not a regular file" in problem for problem in outcome.problems)


def test_an_artifacts_field_that_is_not_a_list_is_refused(tmp_path, environment) -> None:
    entry = entry_point(
        environment,
        'cat > "$mc/out/result-$id.json" <<EOF\n'
        '{"spec_generation": 3, "invocation_id": "$id", "status": "success", '
        '"message": "", "artifacts": "firmware.bin"}\n'
        "EOF\nexit 0\n",
    )
    session = make_session(tmp_path, entry)
    outcome = session.invoke(ACTION_BUILD)

    assert not outcome.successful
    assert any("artifacts are not a list" in problem for problem in outcome.problems)


def test_an_artifact_declaration_that_is_not_a_name_is_refused(tmp_path, environment) -> None:
    entry = entry_point(
        environment,
        'cat > "$mc/out/result-$id.json" <<EOF\n'
        '{"spec_generation": 3, "invocation_id": "$id", "status": "success", '
        '"message": "", "artifacts": [7]}\n'
        "EOF\nexit 0\n",
    )
    session = make_session(tmp_path, entry)
    outcome = session.invoke(ACTION_BUILD)

    assert not outcome.successful
    assert any("not a name" in problem for problem in outcome.problems)


# --------------------------------------------------------------------------
# The liveness ladder
# --------------------------------------------------------------------------


def test_a_step_that_never_ends_is_ended_by_the_deadline(tmp_path, environment) -> None:
    # `exec` so that the signal reaches the process that is sleeping
    # rather than a shell that would leave it behind.
    entry = entry_point(environment, "exec sleep 30\n")
    session = make_session(tmp_path, entry, deadline_seconds=0)

    started = time.monotonic()
    outcome = session.invoke(ACTION_BUILD)
    elapsed = time.monotonic() - started

    assert elapsed < 15
    assert not outcome.successful
    assert outcome.result is None


def test_a_stopped_step_is_ended(tmp_path, environment) -> None:
    entry = entry_point(environment, "exec sleep 30\n")
    session = make_session(tmp_path, entry, deadline_seconds=3600)
    step = session.prepare(ACTION_BUILD)
    # Generation 3 has no cancel sentinel the environment could see, so
    # this is the orchestrator's own file and what follows it is a
    # signal — which in this profile reaches the builder itself.
    step.stop()

    started = time.monotonic()
    outcome = step.run()
    elapsed = time.monotonic() - started

    assert elapsed < 15
    assert not outcome.successful
    assert step.cancel.exists()


# --------------------------------------------------------------------------
# The session's own rules
# --------------------------------------------------------------------------


def test_steps_of_a_session_run_strictly_one_after_another(tmp_path, environment) -> None:
    entry = entry_point(environment, DELIVERS)
    refused: list[Exception] = []

    def reentrant(step: Step, on_line):
        try:
            step.session.prepare(ACTION_BUILD)
        except RuntimeError as error:
            refused.append(error)
        return launcher()(step, on_line)

    session = make_session(tmp_path, entry, launcher=reentrant)
    outcome = session.invoke(ACTION_BUILD)

    assert outcome.successful
    assert refused and "one after another" in str(refused[0])


def test_a_closed_session_prepares_nothing(tmp_path, environment) -> None:
    entry = entry_point(environment, DELIVERS)
    session = make_session(tmp_path, entry)
    session.close()

    with pytest.raises(RuntimeError, match="closed"):
        session.prepare(ACTION_BUILD)


def test_a_session_id_that_could_not_name_a_file_is_refused(tmp_path, environment) -> None:
    entry = entry_point(environment, DELIVERS)
    with pytest.raises(ValueError, match="file name"):
        make_session(tmp_path, entry, session_id="../escape")


def test_a_profile_that_delivers_its_own_entry_point_gets_no_link(tmp_path, environment) -> None:
    # A container image carries the entry point at the path the
    # specification fixes; linking over it would replace the
    # environment's own content with the orchestrator's idea of it.
    session = make_session(tmp_path, entry_point(environment, DELIVERS), entry_point=None)
    step = session.prepare(ACTION_BUILD)

    assert not (step.base_dir / "mcuhome" / "bin").exists()
