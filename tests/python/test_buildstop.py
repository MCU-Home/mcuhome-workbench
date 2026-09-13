# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Stopping a build that is already running (``BuildRequest.should_stop``).

The one seam on this surface that decides control flow, so it is tested
where it decides: the supervisor that asks it, the two local profiles
whose step it ends, the remote target that has to tell a build server
about it, and the entry point that turns all of it into one answer —
``ok=False, stopped=True``, with the build directory released.

**No container and no build server here.** The container profile is
driven through ``test_localbuild``'s scripted runtime, with a step that
keeps running until the ladder reaches it; the subprocess profile runs a
real child process that sleeps until it is signalled; the remote target
talks to a client of this module's own making. What is asserted is what
each of them leaves behind: the container removed, the process gone, the
half-written output still there, and a verdict that says the build was
stopped rather than broken.

The ladder's own timings are the module's constants, and the tests that
walk it to the last rung scale them down rather than wait forty seconds —
the bound they check against is computed from the same constants, so what
is asserted is the relation and never a number.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import EXAMPLES_DIR, ScriptedRegistry, resolve_file
from test_buildenvsession import entry_point
from test_localbuild import Seam, make_sdk_source
from test_subprocessbuild import (  # noqa: F401 - the store fixtures of the profile's own suite
    environment,
    freeze,
    run_one_step,
    store,
    thaw,
)

from mcuhome.workbench import api, build, buildlock, buildprocess, containerbuild, sessionclient
from mcuhome.workbench.buildenvsession import StepResult
from mcuhome.workbench.builders import SelectedBuilder
from mcuhome.workbench.buildprocess import Liveness, resolve_shutdown_seconds, spawn_process
from mcuhome.workbench.signing import generate_key_pem, public_key_pem

#: A fixed public key, so nothing here draws one.
_PUBLIC_PEM = public_key_pem(generate_key_pem(scalar=0x5709DE1))

#: The step that has to be stopped, spelled the way ``test_buildprocess``
#: spells it: this interpreter, so nothing here depends on a shell.
SLEEP_30S = (sys.executable, "-c", "import time; time.sleep(30)")


@pytest.fixture
def model():
    return resolve_file(EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml")


class Asked:
    """A stop predicate, and what it was asked.

    Records every call so a test can tell "nobody asked" from "asked and
    said no" — the two failures that look identical in a result.
    """

    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.answer


class Immortal:
    """A step that ignores every signal, so the whole ladder is walked."""

    started = True
    output = ""

    def __init__(self) -> None:
        self.signals: list[str] = []

    def poll(self) -> int | None:
        return None

    def wait(self) -> int | None:  # pragma: no cover - never reached: it never ends
        return None

    def terminate(self) -> None:
        self.signals.append("terminate")

    def kill(self) -> None:
        self.signals.append("kill")


def _scaled_ladder(monkeypatch, *, poll: float = 0.1, rung: float = 0.3) -> None:
    """The ladder in tenths of a second instead of tens of them.

    Every test that walks it to the end patches these, and every bound
    those tests assert is :func:`resolve_shutdown_seconds` of the patched
    values — so the numbers here decide how long the suite takes and
    nothing else.
    """
    monkeypatch.setattr(buildprocess, "_POLL_SECONDS", poll)
    monkeypatch.setattr(buildprocess, "_KILL_AFTER_SECONDS", rung)
    monkeypatch.setattr(buildprocess, "_GIVE_UP_AFTER_SECONDS", rung)


# --------------------------------------------------------------------------
# The ladder and its bound
# --------------------------------------------------------------------------


def test_the_bound_is_the_ladder_the_supervisor_walks(tmp_path, monkeypatch) -> None:
    """Not a number: the rungs, walked, fit inside what the function answers.

    The constants are scaled down so the test costs a second instead of
    forty; the bound is computed from the same constants, so what is
    asserted is that the ladder fits in it — which is the promise a build
    server and a waiting client rely on.
    """
    _scaled_ladder(monkeypatch)
    grace = 0.1
    cancel = tmp_path / "cancel"
    child = Immortal()
    liveness = Liveness(
        cancel=cancel,
        deadline_seconds=3600,
        cancel_grace_seconds=grace,
        should_stop=lambda: True,
    )

    started = time.monotonic()
    status = liveness.supervise(child)
    elapsed = time.monotonic() - started

    # The last rung: nobody knows what a process that survived SIGKILL
    # did, and a number would be an invention.
    assert status is None
    # Signalled first, killed afterwards — and killed again on every tick
    # it survives, which is what "the supervisor gave up" means.
    assert child.signals[0] == "terminate"
    assert "kill" in child.signals[1:]
    # The stop wrote the sentinel, so the step's own directory says why
    # it was torn down.
    assert cancel.exists()
    assert elapsed <= resolve_shutdown_seconds(cancel_grace_seconds=grace)


def test_the_bound_counts_the_caller_s_grace_period_and_the_fixed_rungs() -> None:
    """Two grace periods apart, the bound is two grace periods apart."""
    first = resolve_shutdown_seconds(cancel_grace_seconds=0)
    assert resolve_shutdown_seconds(cancel_grace_seconds=30) == first + 30
    # The fixed part is the ladder's own: signal to kill, kill to giving
    # up, and the slack the half-second tick costs on every rung.
    assert first > buildprocess._KILL_AFTER_SECONDS + buildprocess._GIVE_UP_AFTER_SECONDS


def test_the_bound_is_on_the_public_surface() -> None:
    """A build server that has to wait for a stopped step asks for it here."""
    assert api.resolve_shutdown_seconds is resolve_shutdown_seconds
    assert "resolve_shutdown_seconds" in api.__all__


def test_the_predicate_ends_a_step_that_would_otherwise_run_for_half_a_minute(
    tmp_path,
) -> None:
    """A real child, a predicate that turns true on the second tick."""
    cancel = tmp_path / "cancel"
    asked = Asked(answer=False)
    child = spawn_process(SLEEP_30S)

    def should_stop() -> bool:
        asked.calls += 1
        return asked.calls > 1

    liveness = Liveness(
        cancel=cancel, deadline_seconds=3600, cancel_grace_seconds=0, should_stop=should_stop
    )
    started = time.monotonic()
    status = liveness.supervise(child)
    elapsed = time.monotonic() - started

    assert status is not None, "the process ended rather than being waited out"
    assert elapsed < 20  # well short of the thirty seconds it was going to sleep
    assert cancel.exists()


def test_a_predicate_that_raises_leaves_the_step_alone_and_is_not_asked_again(
    tmp_path, caplog
) -> None:
    """A caller's bug must not end a build that was going to finish.

    The step here ends on its own; what is asserted is that it was
    allowed to, that the predicate was asked exactly once, and that the
    program which supplied it is told.
    """
    asked = Asked()

    def raising() -> bool:
        asked.calls += 1
        raise RuntimeError("the stop button is broken")

    child = spawn_process((sys.executable, "-c", "import time; time.sleep(1.5)"))
    liveness = Liveness(
        cancel=tmp_path / "cancel",
        deadline_seconds=3600,
        cancel_grace_seconds=0,
        should_stop=raising,
    )
    status = liveness.supervise(child)

    assert status == 0, "the step ran to its own end"
    assert asked.calls == 1
    assert not (tmp_path / "cancel").exists()
    assert "stop predicate" in caplog.text


# --------------------------------------------------------------------------
# The container profile: the container is what has to go
# --------------------------------------------------------------------------


class LongStep:
    """A spawned step that keeps running until it is signalled.

    It records what the runtime had been asked to do by the time the
    signal arrived, because the order is the point: removing the
    container is what ends a containerized build, and signalling the
    client in front of it is the tidy-up afterwards. A test that only
    looked for an ``rm`` would be satisfied by the sweep every session
    does at its end.
    """

    started = True
    output = "compiling..."

    def __init__(self, seam: Seam) -> None:
        self.seam = seam
        self.status: int | None = None
        self.signals: list[str] = []
        self.calls_before_signal: list[list[str]] = []

    def poll(self) -> int | None:
        return self.status

    def wait(self) -> int | None:
        return self.status

    def terminate(self) -> None:
        self.calls_before_signal = [list(argv) for argv in self.seam.calls]
        self.signals.append("terminate")
        self.status = -15

    def kill(self) -> None:  # pragma: no cover - the client goes on the first signal
        self.signals.append("kill")
        self.status = -9


class StopSeam(Seam):
    """``test_localbuild``'s scripted runtime, with a step that does not end.

    The build it plays writes a piece of a firmware and then keeps
    running, which is what a stop has to happen *during*: a step that had
    already finished would be stopped by nothing and would prove nothing.
    """

    def __init__(self) -> None:
        super().__init__(build=_half_a_firmware)
        self.running_step = LongStep(self)

    def spawn(self, argv, on_line=None):
        # The parent parses the mounts and plays the scripted build
        # through them; only the handle is this class's own.
        super().spawn(argv, on_line)
        return self.running_step


def _half_a_firmware(request: dict[str, Any], out: Path) -> None:
    """What a build that is interrupted leaves behind: a piece of a file."""
    (out / "firmware.bin.part").write_bytes(b"half a firmware")


def test_a_stopped_container_build_removes_the_container_and_keeps_what_was_written(
    tmp_path, model
) -> None:
    """The real composition, down to the runtime's ``rm``.

    Signalling the client this process started is not what ends a
    containerized build — the build is inside the container — so the stop
    has to reach the container itself. The predicate turns true the
    moment the step has written something, which is the state a person
    pressing stop is actually in.
    """
    make_sdk_source(tmp_path / "src")
    seam = StopSeam()
    partial = tmp_path / "wr" / "backend" / "session" / "out" / "firmware.bin.part"

    result = build.compose_container_build(
        model,
        signing_pub=_PUBLIC_PEM,
        sdk_sources=(tmp_path / "src",),
        work_root=tmp_path / "wr",
        env={},
        images=ScriptedRegistry(),
        runtime=containerbuild.Runtime(runner=seam, spawner=seam.spawn),
        options=build.BuildOptions(
            workspace_sources=(tmp_path / "src",), tools_sources=(tmp_path / "src",)
        ),
        should_stop=partial.is_file,
    )

    # A stopped step wrote no result document, which is what a stopped
    # step is: the specification has no cancelled status.
    assert result.outcome.ok is False
    assert any("no readable result document" in problem for problem in result.outcome.problems)
    # The container was removed by name, and only then was the client
    # this process started signalled — the order the ladder walks, and
    # not the sweep every session does when it ends.
    name = seam.step[seam.step.index("--name") + 1]
    assert seam.running_step.signals == ["terminate"]
    last_before_signal = seam.running_step.calls_before_signal[-1]
    assert last_before_signal[1] == "rm"
    assert name in last_before_signal
    # What the build had produced is still on disk, in the directory the
    # result names.
    assert (result.out_dir / "firmware.bin.part").read_bytes() == b"half a firmware"


# --------------------------------------------------------------------------
# The subprocess profile: the builder itself is what has to go
# --------------------------------------------------------------------------


def test_a_stopped_subprocess_build_ends_the_builder_and_keeps_what_was_written(
    tmp_path,
    store,  # noqa: F811 - the fixture is imported, and a fixture is asked for by name
    environment,  # noqa: F811 - same
) -> None:
    """A real child process, stopped while it is sleeping through a build.

    ``exec`` on purpose: what the ladder signals is the process this side
    started, so the entry point *becomes* the sleeping program instead of
    waiting for one.
    """
    thaw(environment.tools.path)
    entry_point(
        environment.tools.path,
        'printf HALF > "$mc/out/firmware.bin.part"\nexec sleep 30\n',
    )
    freeze(environment.tools.path)
    partial = tmp_path / "work" / "session" / "out" / "firmware.bin.part"

    started = time.monotonic()
    result = run_one_step(tmp_path, environment, should_stop=partial.is_file)
    elapsed = time.monotonic() - started

    assert result.outcome.ok is False
    assert elapsed < 20  # well short of the thirty seconds the step asked for
    assert (result.out_dir / "firmware.bin.part").read_bytes() == b"HALF"


# --------------------------------------------------------------------------
# One answer, whichever target ran: ok=False, stopped=True
# --------------------------------------------------------------------------


def _stopped_step(out_dir: Path) -> StepResult:
    """What a profile answers after the ladder ended its step."""
    return StepResult(
        action="build",
        context_id="sha256:" + "c" * 64,
        exit_code=-15,
        problems=("the build environment wrote no readable result document",),
        out_dir=out_dir,
    )


def _build_request(model, tmp_path, **overrides) -> build.BuildRequest:
    return build.BuildRequest(
        model=model,
        out_dir=tmp_path / "build",
        options=build.BuildOptions(),
        **overrides,
    )


@pytest.mark.parametrize("execution", ["container", "subprocess"])
def test_a_stopped_local_build_answers_stopped_and_releases_the_build_directory(
    model, tmp_path, monkeypatch, execution
) -> None:
    """Both local executions, at the entry point a caller actually uses.

    The composition is stubbed here — the two profiles are driven for
    real above — and the stub does what a supervisor does: it asks the
    predicate it was handed, and comes back with the step the ladder
    ended. What is asserted is the layer above: that the predicate
    arrived, that the answer says *stopped* rather than merely *failed*,
    and that the build directory is free again afterwards.
    """
    out_dir = tmp_path / "build"
    asked = Asked()
    seen: dict[str, Any] = {}

    def fake(model_, **kwargs):
        assert buildlock.is_busy(out_dir), "the build directory is held while a build runs"
        seen["asked"] = kwargs["should_stop"]()
        outcome = _stopped_step(tmp_path / "out")
        if execution == "subprocess":
            return build.subprocessbuild.SubprocessBuildResult(
                outcome=outcome,
                out_dir=tmp_path / "out",
                context_dir=tmp_path / "context",
                environment=None,
            )
        return containerbuild.ContainerBuildResult(
            outcome=outcome,
            out_dir=tmp_path / "out",
            context_dir=tmp_path / "context",
            container_image="ghcr.io/mcu-home/build-environment@sha256:" + "d" * 64,
        )

    monkeypatch.setattr(build, "compose_local_build", fake)
    target = build.LocalBuild(
        execution=build.ContainerExecution()
        if execution == "container"
        else build.SubprocessExecution()
    )
    result = asyncio.run(
        build.build_firmware(_build_request(model, tmp_path, should_stop=asked), target=target)
    )

    assert seen["asked"] is True, "the predicate reached the composition"
    assert result.ok is False
    assert result.stopped is True
    assert result.to_dict()["stopped"] is True
    assert result.out_dir == tmp_path / "out"
    assert buildlock.is_busy(out_dir) is False


def test_a_build_that_failed_without_being_stopped_says_so(model, tmp_path, monkeypatch) -> None:
    """The other half of the verdict, and the one a stuck flag would break.

    A failed compile and a stopped build produce the same empty output
    directory; ``stopped`` is the only thing that tells them apart, so a
    build nobody stopped has to answer ``False`` — including one whose
    caller supplied a predicate that kept saying no.
    """
    asked = Asked(answer=False)

    def fake(model_, **kwargs):
        kwargs["should_stop"]()
        return containerbuild.ContainerBuildResult(
            outcome=_stopped_step(tmp_path / "out"),
            out_dir=tmp_path / "out",
            context_dir=tmp_path / "context",
            container_image="",
        )

    monkeypatch.setattr(build, "compose_local_build", fake)
    result = asyncio.run(
        build.build_firmware(_build_request(model, tmp_path, should_stop=asked), target="local")
    )

    assert asked.calls == 1
    assert result.ok is False
    assert result.stopped is False


def test_a_build_without_a_predicate_hands_none_down(model, tmp_path, monkeypatch) -> None:
    """Nothing to ask is nothing below has to know about."""
    seen: dict[str, Any] = {}

    def fake(model_, **kwargs):
        seen["should_stop"] = kwargs["should_stop"]
        return containerbuild.ContainerBuildResult(
            outcome=StepResult(action="build", context_id="", exit_code=0, status="success"),
            out_dir=tmp_path / "out",
            context_dir=tmp_path / "context",
            container_image="",
        )

    monkeypatch.setattr(build, "compose_local_build", fake)
    result = asyncio.run(build.build_firmware(_build_request(model, tmp_path), target="local"))

    assert seen["should_stop"] is None
    assert result.stopped is False


def test_the_verdict_stays_stopped_after_the_predicate_changes_its_mind(
    model, tmp_path, monkeypatch
) -> None:
    """Latched: a build is not un-stopped by whoever cleared the flag.

    A session asks once per step, so a predicate reading a flag somebody
    else clears can say yes to the step that was stopped and no to the
    question afterwards. The verdict follows the first yes.
    """
    answers = iter([True, False, False])

    def fake(model_, **kwargs):
        assert kwargs["should_stop"]() is True
        assert kwargs["should_stop"]() is True, "asked again, and still stopped"
        return containerbuild.ContainerBuildResult(
            outcome=_stopped_step(tmp_path / "out"),
            out_dir=tmp_path / "out",
            context_dir=tmp_path / "context",
            container_image="",
        )

    monkeypatch.setattr(build, "compose_local_build", fake)
    result = asyncio.run(
        build.build_firmware(
            _build_request(model, tmp_path, should_stop=lambda: next(answers)), target="local"
        )
    )

    assert result.stopped is True


# --------------------------------------------------------------------------
# The remote target: the server has to be told
# --------------------------------------------------------------------------

IDENTITY = "sha256:" + "e" * 64
INVOCATION = "inv-42"


class FakeServer:
    """What the fake clients of one test share: the far side.

    It is not a build server — it is the four verbs a remote build sends
    and the one answer it waits for, so that the client's *own* behaviour
    around a stop is observable: which verbs went out, on how many
    connections, and whether each of them was closed.
    """

    def __init__(self, *, busy: bool = False, retry_after: float = 0.4) -> None:
        self.busy = busy
        self.retry_after = retry_after
        self.verbs: list[str] = []
        self.clients: list[FakeClient] = []
        self.cancelled: list[str] = []
        #: The verdict to answer with when the invocation is cancelled;
        #: ``None`` is the server that says nothing at all.
        self.verdict_on_cancel: dict[str, Any] | None = None
        #: How long the server takes to acknowledge a cancel. A command
        #: frame is normally answered at once; a server that is connected
        #: and silent is the case this exists for.
        self.cancel_takes: float = 0.0
        #: The same for ``close-session``, which is the last frame a
        #: stopped build sends and the one that used to wait out the
        #: whole call timeout on a peer that had stopped answering.
        self.close_takes: float = 0.0
        #: How long the peer leaves the socket's closing handshake
        #: unanswered. A real client waits aiohttp's own ten seconds for
        #: that frame unless it is told otherwise, which is the last
        #: place a stopped build can lose time it promised away.
        self.handshake_takes: float = 0.0
        #: When set, the socket dies instead of answering the verdict —
        #: what the reader does to every pending call when the
        #: connection goes under it.
        self.drops_the_connection = False
        #: The same, but as the stop goes out: the connection survives
        #: until this side asks for the invocation to end, which is the
        #: only order in which a dying socket is a *stopped* build.
        self.drops_when_stopped = False
        self._finished: asyncio.Future | None = None

    def finished(self) -> asyncio.Future:
        if self._finished is None:
            self._finished = asyncio.get_running_loop().create_future()
        return self._finished

    def install(self, monkeypatch) -> FakeServer:
        monkeypatch.setattr(sessionclient, "SessionClient", lambda url, **kwargs: FakeClient(self))
        return self


class FakeClient:
    """One connection to a :class:`FakeServer`, in the shape a build drives."""

    def __init__(self, server: FakeServer) -> None:
        self.server = server
        self.closed = False
        self.session_closed = False
        #: Every artifact download this client was asked for — empty is
        #: what a stopped build has to leave behind.
        self.fetched: list[str] = []
        #: What the caller allowed the closing handshake, if anything.
        self.close_timeout: float | None = None
        server.clients.append(self)

    async def connect(self) -> None:
        self.server.verbs.append("connect")

    async def capabilities(self) -> dict[str, Any]:
        self.server.verbs.append("capabilities")
        return {}

    async def open_session(self, *, profile: str = "oneshot", seat: str | None = None):
        self.server.verbs.append("open-session")
        if self.server.busy:
            raise sessionclient.ServerRefusal(
                {
                    "code": "session.no-room",
                    "message": "every seat is taken",
                    "details": {
                        "seat": "seat-1",
                        "retry_after_seconds": self.server.retry_after,
                    },
                },
                verb="open-session",
            )
        return {"session": {"id": "session-1"}}

    async def send_context(self, context_dir, *, image=None) -> dict[str, Any]:
        self.server.verbs.append("send-context")
        return {}

    async def lock_context(self) -> str:
        self.server.verbs.append("lock-context")
        return IDENTITY

    async def build(self, *, mode: str = "clean") -> str:
        self.server.verbs.append("build")
        return INVOCATION

    async def wait_finished(self, invocation_id: str, *, timeout: float | None = None):
        if self.server.drops_the_connection:
            # What the reader does to every pending call when the socket
            # dies under it (`SessionClient._fail_pending`).
            raise sessionclient.RemoteTransportError(
                "The connection to the build server failed.", hint=""
            )
        return await self.server.finished()

    async def cancel(self, invocation_id: str) -> dict[str, Any]:
        self.server.verbs.append("cancel")
        self.server.cancelled.append(invocation_id)
        if self.server.drops_when_stopped and not self.server.finished().done():
            self.server.finished().set_exception(
                sessionclient.RemoteTransportError(
                    "The connection to the build server failed.", hint=""
                )
            )
            return {"status": "accepted"}
        await asyncio.sleep(self.server.cancel_takes)
        verdict = self.server.verdict_on_cancel
        if verdict is not None and not self.server.finished().done():
            self.server.finished().set_result(verdict)
        return {"status": "accepted"}

    async def get_artifact(self, invocation_id: str, *, into: Path, **kwargs) -> Any:
        self.fetched.append(invocation_id)
        return None

    async def close_session(self) -> dict[str, Any]:
        self.server.verbs.append("close-session")
        await asyncio.sleep(self.server.close_takes)
        self.session_closed = True
        return {}

    async def close(self, *, timeout: float | None = None) -> None:
        # What a real client does with this number: it bounds the wait
        # for the peer's answering close frame and nothing else.
        self.close_timeout = timeout
        stalled = self.server.handshake_takes
        await asyncio.sleep(stalled if timeout is None else min(stalled, timeout))
        self.closed = True


def _remote_build(tmp_path, server: FakeServer, **kwargs):
    return asyncio.run(
        sessionclient.run_remote_build(
            tmp_path / "context",
            url="ws://build.example/session",
            work_root=tmp_path / "work",
            **kwargs,
        )
    )


def test_a_build_waiting_for_a_turn_stops_without_opening_a_session(tmp_path, monkeypatch) -> None:
    """The wait is spent in ticks, and a stop ends it between two of them.

    Nothing has to be told: no session was opened, and the turn this
    client was holding is one the server reclaims when nobody comes back
    for it.
    """
    server = FakeServer(busy=True).install(monkeypatch)
    waits: list[Any] = []

    result = _remote_build(
        tmp_path,
        server,
        on_wait=waits.append,
        should_stop=lambda: bool(waits),
        wait=True,
        max_wait=0,
    )

    assert len(waits) == 1, "refused once, then stopped instead of waiting again"
    assert result.status == sessionclient.STATUS_CANCELLED
    assert result.ok is False
    assert result.out_dir is None
    assert "send-context" not in server.verbs
    assert all(client.closed for client in server.clients)


def test_stopping_a_running_invocation_sends_the_cancel_verb(tmp_path, monkeypatch) -> None:
    """A closed socket is not a stop signal, so the verb goes out.

    The server answers the cancelled verdict, which is the ordinary
    course: the invocation ended and said so, and that word is what the
    result carries.
    """
    server = FakeServer().install(monkeypatch)
    server.verdict_on_cancel = {"status": "cancelled", "artifacts": []}

    # Stopped once the invocation is running, which is the state this
    # test is about: before that, a stop needs no verb at all.
    result = _remote_build(tmp_path, server, should_stop=lambda: "build" in server.verbs)

    assert server.cancelled == [INVOCATION]
    assert result.status == sessionclient.STATUS_CANCELLED
    assert result.ok is False
    assert result.artifacts == ()
    assert result.context_id == IDENTITY
    assert server.clients[0].session_closed, "the session is closed like any other"
    assert server.clients[0].closed


def test_a_server_that_says_nothing_counts_as_stopped_after_the_ladder_s_bound(
    tmp_path, monkeypatch
) -> None:
    """The far side may answer nothing at all, and a person is still waiting.

    The bound is the liveness ladder's, scaled down here the way every
    other ladder test scales it; what is asserted is that the wait ends
    at all, and with the invocation id a person can take to the operator
    of that machine.
    """
    _scaled_ladder(monkeypatch, poll=0.02, rung=0.05)
    monkeypatch.setattr(sessionclient, "_STOP_POLL_SECONDS", 0.02)
    server = FakeServer().install(monkeypatch)

    started = time.monotonic()
    result = _remote_build(tmp_path, server, should_stop=lambda: "build" in server.verbs)
    elapsed = time.monotonic() - started

    assert server.cancelled == [INVOCATION]
    assert result.status == sessionclient.STATUS_CANCELLED
    assert result.invocation_id == INVOCATION
    assert elapsed < 5, "the wait ended on the bound, not on the verdict that never came"


def test_a_server_that_does_not_even_acknowledge_the_stop_is_bounded_too(
    tmp_path, monkeypatch
) -> None:
    """The bound starts at the decision, not at the server's answer to it.

    ``cancel`` is a command frame like any other and waits five minutes
    for a reply. A server that is connected and silent would hold this
    side for all of them — against a promise of forty-four seconds — if
    the wait for the acknowledgement were not on the same clock as the
    wait for the verdict.
    """
    _scaled_ladder(monkeypatch, poll=0.02, rung=0.05)
    monkeypatch.setattr(sessionclient, "_STOP_POLL_SECONDS", 0.02)
    server = FakeServer().install(monkeypatch)
    server.cancel_takes = 30.0
    bound = resolve_shutdown_seconds(cancel_grace_seconds=0)

    started = time.monotonic()
    result = _remote_build(tmp_path, server, should_stop=lambda: "build" in server.verbs)
    elapsed = time.monotonic() - started

    assert server.cancelled == [INVOCATION]
    assert result.status == sessionclient.STATUS_CANCELLED
    assert elapsed < bound * 10, "the acknowledgement is bounded by the ladder, not by the call"


def test_the_whole_tail_of_a_stopped_build_is_inside_the_bound(tmp_path, monkeypatch) -> None:
    """Not only the cancel and the verdict: closing the session too.

    Every one of the three is a command frame, and a frame waits five
    minutes for an answer. A caller of `build_firmware` holds its build
    directory for all of them, so a bound that stopped at the verdict
    would be a bound on paper — which is what this asserts against: one
    server, silent in all three places, and one clock over the lot.
    """
    _scaled_ladder(monkeypatch, poll=0.02, rung=0.05)
    monkeypatch.setattr(sessionclient, "_STOP_POLL_SECONDS", 0.02)
    server = FakeServer().install(monkeypatch)
    server.cancel_takes = 30.0
    server.close_takes = 30.0
    server.handshake_takes = 30.0
    bound = resolve_shutdown_seconds(cancel_grace_seconds=0)

    started = time.monotonic()
    result = _remote_build(tmp_path, server, should_stop=lambda: "build" in server.verbs)
    elapsed = time.monotonic() - started

    client = server.clients[0]
    assert result.status == sessionclient.STATUS_CANCELLED
    assert "close-session" in server.verbs, "it was tried"
    assert not client.session_closed, "and abandoned when it did not answer"
    assert client.close_timeout is not None, (
        "the socket's closing handshake is bounded too, not left to aiohttp's ten seconds"
    )
    assert elapsed < bound * 10, (
        "the stop's bound covers the cancel, the verdict, the close and the handshake"
    )


def test_a_connection_that_dies_while_stopping_is_a_stopped_build(tmp_path, monkeypatch) -> None:
    """The caller asked for it to end, and it ended.

    A socket that dies under a *running* build is news and a refusal;
    under one somebody is stopping it is the same answer the stop was
    going to produce, and raising instead would replace an answer about
    the build with one about the connection.
    """
    monkeypatch.setattr(sessionclient, "_STOP_POLL_SECONDS", 0.02)
    server = FakeServer().install(monkeypatch)
    server.drops_when_stopped = True

    # Stopped once the invocation runs, so the socket dies *after* the
    # decision: before it, a lost connection is the only news there is.
    result = _remote_build(tmp_path, server, should_stop=lambda: "build" in server.verbs)

    assert server.cancelled == [INVOCATION], "the stop went out before the socket died"
    assert result.status == sessionclient.STATUS_CANCELLED
    assert result.ok is False


def test_a_dead_connection_without_a_stop_is_still_a_refusal(tmp_path, monkeypatch) -> None:
    """The other half of the rule: off the stop path nothing is swallowed."""
    server = FakeServer().install(monkeypatch)
    server.drops_the_connection = True

    with pytest.raises(sessionclient.RemoteTransportError):
        _remote_build(tmp_path, server)


def test_a_cancelled_verdict_delivers_nothing_of_what_it_declares(tmp_path, monkeypatch) -> None:
    """A stopped invocation's files stay on the machine that ran it.

    A verdict that says ``cancelled`` and lists artifacts anyway is a
    half-built firmware being offered: fetching it would mean a download
    inside the stop's own bound, and a caller unable to tell those files
    from the ones a finished build delivers.
    """
    server = FakeServer().install(monkeypatch)
    server.verdict_on_cancel = {
        "status": "cancelled",
        "artifacts": [
            {"root": "out", "path": "firmware.bin", "role": "firmware", "sha256": "0" * 64}
        ],
    }

    result = _remote_build(tmp_path, server, should_stop=lambda: "build" in server.verbs)

    assert result.status == sessionclient.STATUS_CANCELLED
    assert result.artifacts == ()
    assert result.out_dir is None
    assert server.clients[0].fetched == [], "nothing was downloaded"


@pytest.mark.parametrize("execution", ["container", "subprocess"])
def test_a_build_that_finished_while_the_stop_arrived_is_not_a_stopped_build(
    model, tmp_path, monkeypatch, execution
) -> None:
    """The race, and the rule it settles: a firmware that exists is the answer.

    The predicate turns true while the last step is already succeeding.
    Telling the caller its build was stopped would throw away what it
    asked for and what is on disk. Both local executions answer it, and
    each reads the verdict in its own function.
    """
    asked = Asked()
    finished = StepResult(
        action="build",
        context_id="sha256:" + "f" * 64,
        exit_code=0,
        status="success",
    )

    def fake(model_, **kwargs):
        kwargs["should_stop"]()
        if execution == "subprocess":
            return build.subprocessbuild.SubprocessBuildResult(
                outcome=finished,
                out_dir=tmp_path / "out",
                context_dir=tmp_path / "context",
                environment=None,
            )
        return containerbuild.ContainerBuildResult(
            outcome=finished,
            out_dir=tmp_path / "out",
            context_dir=tmp_path / "context",
            container_image="",
        )

    monkeypatch.setattr(build, "compose_local_build", fake)
    target = build.LocalBuild(
        execution=build.ContainerExecution()
        if execution == "container"
        else build.SubprocessExecution()
    )
    result = asyncio.run(
        build.build_firmware(_build_request(model, tmp_path, should_stop=asked), target=target)
    )

    assert asked.calls == 1
    assert result.ok is True
    assert result.stopped is False


def test_a_remote_build_that_succeeded_while_the_stop_arrived_is_not_stopped(
    model, tmp_path, monkeypatch
) -> None:
    """The same race at the remote target, where the verdict decides."""
    (tmp_path / "ctx").mkdir()

    async def fake(context_dir, **kwargs):
        kwargs["should_stop"]()
        return sessionclient.RemoteBuildResult(
            action="build",
            context_id=IDENTITY,
            status="success",
            artifacts=(),
            out_dir=tmp_path / "out",
            invocation_id=INVOCATION,
        )

    monkeypatch.setattr(sessionclient, "run_remote_build", fake)
    request = _build_request(
        model,
        tmp_path,
        context_dir=tmp_path / "ctx",
        builder=SelectedBuilder(target=build.TARGET_REMOTE, server="ws://build.example/session"),
        should_stop=Asked(),
    )
    result = asyncio.run(build.build_firmware(request, target=build.TARGET_REMOTE))

    assert result.ok is True
    assert result.stopped is False


def test_a_remote_build_that_was_not_stopped_waits_for_its_verdict(tmp_path, monkeypatch) -> None:
    """The predicate is asked while the invocation runs, says no, and nothing
    is cancelled.

    The tick is scaled down and the verdict held back for several of
    them, so the asking is the follow loop's own: a verdict that arrived
    before the first tick would be answered without the predicate ever
    being consulted, and the test would pass against a client that polls
    nothing.
    """
    monkeypatch.setattr(sessionclient, "_STOP_POLL_SECONDS", 0.02)
    server = FakeServer().install(monkeypatch)
    asked = Asked(answer=False)

    async def answer_later() -> None:
        await asyncio.sleep(0.2)
        server.finished().set_result({"status": "success", "artifacts": []})

    async def scenario():
        task = asyncio.ensure_future(answer_later())
        try:
            return await sessionclient.run_remote_build(
                tmp_path / "context",
                url="ws://build.example/session",
                work_root=tmp_path / "work",
                should_stop=asked,
            )
        finally:
            await task

    result = asyncio.run(scenario())

    # One of them is the admission loop's; the rest are the follow loop
    # asking on its own tick while the invocation ran.
    assert asked.calls >= 3
    assert server.cancelled == []
    assert result.status == "success"


def test_a_stopped_remote_build_answers_stopped_at_the_entry_point(
    model, tmp_path, monkeypatch
) -> None:
    """``build_firmware`` over the remote target, with the client stubbed.

    Two ways in and both are a stopped build: this side asked and was
    told to stop, or the server ended the invocation itself and said
    ``cancelled`` in the verdict.
    """
    (tmp_path / "ctx").mkdir()
    asked = Asked()
    seen: dict[str, Any] = {}

    async def fake(context_dir, **kwargs):
        seen["asked"] = kwargs["should_stop"]()
        return sessionclient.RemoteBuildResult(
            action="build",
            context_id=IDENTITY,
            status="failure",
            artifacts=(),
            out_dir=None,
            invocation_id=INVOCATION,
        )

    monkeypatch.setattr(sessionclient, "run_remote_build", fake)
    request = _build_request(
        model,
        tmp_path,
        context_dir=tmp_path / "ctx",
        builder=SelectedBuilder(target=build.TARGET_REMOTE, server="ws://build.example/session"),
        should_stop=asked,
    )
    result = asyncio.run(build.build_firmware(request, target=build.TARGET_REMOTE))

    assert seen["asked"] is True
    assert result.ok is False
    assert result.stopped is True
    assert buildlock.is_busy(tmp_path / "build") is False


def test_a_cancelled_verdict_is_a_stopped_build_whoever_ended_it(
    model, tmp_path, monkeypatch
) -> None:
    """The operator of the build server can stop an invocation too."""
    (tmp_path / "ctx").mkdir()

    async def fake(context_dir, **kwargs):
        return sessionclient.RemoteBuildResult(
            action="build",
            context_id=IDENTITY,
            status=sessionclient.STATUS_CANCELLED,
            artifacts=(),
            out_dir=None,
            invocation_id=INVOCATION,
        )

    monkeypatch.setattr(sessionclient, "run_remote_build", fake)
    request = _build_request(
        model,
        tmp_path,
        context_dir=tmp_path / "ctx",
        builder=SelectedBuilder(target=build.TARGET_REMOTE, server="ws://build.example/session"),
    )
    result = asyncio.run(build.build_firmware(request, target=build.TARGET_REMOTE))

    assert result.ok is False
    assert result.stopped is True
