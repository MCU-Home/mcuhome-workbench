# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The ``remote`` build method, driven against the real build server.

**The peer here is the real thing wherever it can be.** ``mcuhome-build-server``
is importable in this development environment, so most of these tests run
:mod:`mcuhome.workbench.sessionclient` against
:func:`mcuhome_buildserver.app.create_app` over a real socket: one client
and one server, tested against each other rather than each against a
mock of the other. Only docker is stubbed at the same seam the build
server's own suite stubs it at, because a build server is an orchestrator
and there is no build to stand in for.

**There is no hand-rolled server left.** One existed for a single shape
the real one could not be bent into — a ``capabilities`` payload that
*announces* ingress caps — and E57 made the real server announce them out
of its own configuration, so the caps are now tested where every other
verb is: against the peer that has to get them right. The one number that
is not configurable, the endpoint's maximum WebSocket frame, is lowered
on the *client* after the announcement, which is the case it exists for:
a peer that accepts less than this one does.

Nothing in this file is a pytest-asyncio test. The repository's own dev
dependency is pytest and nothing else, so each test is an ordinary
function that runs its coroutine with :func:`asyncio.run` — which also
means every test owns its event loop and cannot inherit a dirty one.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import hashlib
import importlib
import importlib.util
import inspect
import io
import json
import tarfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from conftest import EXAMPLES_DIR, resolve_file

from mcuhome.compiler.localbackend import LocalOutcome
from mcuhome.model.artifacts import Artifact
from mcuhome.model.context import ContextRequest, SdkPin
from mcuhome.workbench import buildmethods, imgtool, resolve_pins, signing
from mcuhome.workbench import sessionclient as sc
from mcuhome.workbench.contextdir import read_context_request, write_context_request
from mcuhome.workbench.imgtool import BUILD_REPORT_FILE

#: What this file needs beyond the repository's own dev dependencies, and
#: the one command that installs each. The gate below is a **single**
#: skip with one reason naming exactly what is missing: a suite that
#: reports "22 skipped" for seven different reasons is a suite nobody
#: reads, and the two tests here that need none of this have been moved
#: to ``test_packaging.py`` so they run in every environment.
#:
#: Note for CI: ``mcuhome-build-server`` lives in a private sibling
#: repository and ``.github/workflows/ci.yml`` has no deploy key for it
#: (only ``CLI_DEPLOY_KEY``), so everything below is skipped there until
#: one exists. That is a known, stated gap and not a silent one.
NEEDED = {
    "aiohttp": "pip install -e './packaging/workbench[remote]'",
    "zstandard": "pip install -e './packaging/workbench[remote]'",
    "mcuhome_buildserver": "pip install -e ../build-server",
}
MISSING = sorted(name for name in NEEDED if importlib.util.find_spec(name) is None)
if MISSING:
    pytest.skip(
        "the remote build method is tested against the REAL build server over a real "
        f"socket, and this environment is missing: {', '.join(MISSING)}. Install with — "
        + " ; ".join(sorted({NEEDED[name] for name in MISSING})),
        allow_module_level=True,
    )

# Imported through `importlib` rather than with `import` statements
# because they may only be resolved after the gate above.
test_utils = importlib.import_module("aiohttp.test_utils")
zstandard = importlib.import_module("zstandard")
bs_app = importlib.import_module("mcuhome_buildserver.app")
bs_config = importlib.import_module("mcuhome_buildserver.config")
bs_container = importlib.import_module("mcuhome_buildserver.container")
bs_protocol = importlib.import_module("mcuhome_buildserver.protocol")
bs_sessions = importlib.import_module("mcuhome_buildserver.sessions")

TOKEN = "test-token-000000000000000000000000"

#: The image the server below selects, and the §2.1 labels a conforming
#: one carries. Same values as the build server's own suite, because they
#: are what its stubbed docker answers with. The contexts pin no image:
#: they require a Zephyr line (:data:`ZEPHYR_LINE`) and the server answers
#: it out of this inventory (E61).
IMAGE = "ghcr.io/mcu-home/build-container"
IMAGE_DIGEST = "sha256:" + "b" * 64
IMAGE_REFERENCE = f"{IMAGE}@{IMAGE_DIGEST}"
IMAGE_LABELS = {
    "org.mcuhome.contract": "1",
    "org.mcuhome.zephyr": "4.4.0",
    "org.mcuhome.toolchain": "zephyr-sdk-1.0.1",
}

#: The Zephyr line every context here requires — satisfied by the image
#: above, whose label says 4.4.0.
ZEPHYR_LINE = "4.4"

#: A conforming ``describe`` ``program`` block — every field §7.1.1 makes
#: mandatory. ``sdk`` reports ``path: null``: "not in my image, mount it
#: where you like".
PROGRAM = {
    "id": "org.mcuhome.build-container",
    "version": "2.4.0",
    "contract": 1,
    "request": [1],
    "result": [1],
    "actions": ["describe", "verify", "build"],
    "trees": {
        "zephyr": {"path": "/opt/zephyr", "version": "4.4.0"},
        "chip": {"path": "/opt/connectedhomeip", "version": "v1.5.1.0"},
        "mcuboot": {"path": "/opt/bootloader/mcuboot"},
        "sdk": {"path": None},
    },
}

#: The one artifact whose content the contract fixes (§7.2.1), for the one
#: consumer that needs it: the client that signs detached, on the host,
#: after the unsigned image has come back (E55, E56).
BUILD_REPORT = {
    "report": 1,
    "signing": {
        "signature_type": "ecdsa-p256",
        "arguments": {"version": "1.4.0+0", "header-size": 512, "align": 4, "slot-size": 983040},
    },
}

SDK_VERSION = "2.4.0"


# --------------------------------------------------------------------------
# Docker, stubbed at the seam — never run
# --------------------------------------------------------------------------


class FakeProcess:
    """A ``docker exec`` that has already finished, or refuses to."""

    def __init__(self, code: int, *, hang: bool = False) -> None:
        self._code = code
        self._hang = hang
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        while self._hang:
            await asyncio.sleep(0.01)
        return self._code

    def terminate(self) -> None:
        self.terminated = True
        self._hang = False
        self._code = -15

    def kill(self) -> None:
        self.killed = True
        self._hang = False
        self._code = -9


@dataclass
class Invocation:
    action: str
    argv: list[str]
    request: dict[str, Any]


@dataclass
class FakeDocker:
    """Docker as this suite has it: argv in, scripted answers out.

    A condensed twin of ``build-server/tests/conftest.py``'s fake, and
    deliberately a copy rather than an import: this repository does not
    depend on ``mcuhome-build-server`` and must not start doing so
    through a test fixture. What it has to get right is only what the
    *client* path touches — the inventory, the image lookup, ``describe``,
    one container and one exec per invocation.
    """

    calls: list[list[str]] = field(default_factory=list)
    invocations: list[Invocation] = field(default_factory=list)
    images: dict[str, dict[str, Any]] = field(default_factory=dict)
    listed: list[str] = field(default_factory=list)
    version_status: int | None = 0
    program: dict[str, Any] | None = None
    run_program: Any = None
    containers: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        inspected = {
            "Id": "sha256:" + "c" * 64,
            "RepoTags": [f"{IMAGE}:zephyr-4.4.0-r7"],
            "RepoDigests": [IMAGE_REFERENCE],
            "Config": {"Labels": dict(IMAGE_LABELS)},
        }
        self.images.setdefault(IMAGE_REFERENCE, inspected)
        self.images.setdefault(f"{IMAGE}:zephyr-4.4.0-r7", inspected)
        self.listed = self.listed or [f"{IMAGE}:zephyr-4.4.0-r7"]
        if self.program is None:
            self.program = json.loads(json.dumps(PROGRAM))
        if self.run_program is None:
            self.run_program = conforming_program

    async def run(self, argv):
        self.calls.append(list(argv))
        rest = list(argv[1:])
        if self.version_status is None:
            return bs_container.Completed(status=None, output="")
        if rest[:1] == ["version"]:
            return bs_container.Completed(status=self.version_status, output="27.0.0\n")
        if rest[:2] == ["image", "ls"]:
            return bs_container.Completed(status=0, output="\n".join(self.listed) + "\n")
        if rest[:2] == ["image", "inspect"]:
            references = list(rest[4:])
            lines = [
                json.dumps(self.images[reference])
                for reference in references
                if reference in self.images
            ]
            status = 0 if len(lines) == len(references) else 1
            return bs_container.Completed(status=status, output="\n".join(lines))
        if rest[:1] == ["run"] and rest[-2:-1] == ["cat"]:
            # §2.2.1's static self-description. This image carries none,
            # so the backend falls back to invoking `describe`.
            return bs_container.Completed(status=1, output="")
        if rest[:1] == ["run"] and "--rm" in rest:
            return self._describe(rest)
        if rest[:1] == ["run"]:
            identity = f"{len(self.containers):064x}"
            self.containers.append(identity)
            return bs_container.Completed(status=0, output=identity + "\n")
        if rest[:1] == ["rm"]:
            self.removed.append(rest[-1])
            return bs_container.Completed(status=0, output="")
        raise AssertionError(f"the fake docker was asked something unexpected: {argv}")

    async def spawn(self, argv, *, on_line):
        self.calls.append(list(argv))
        action, request_path = argv[-2], Path(argv[-1])
        request = json.loads(request_path.read_text())
        self.invocations.append(Invocation(action=action, argv=list(argv), request=request))
        return self.run_program(action, request, on_line)

    def _describe(self, rest: list[str]) -> Any:
        request = Path(rest[-1])
        document = json.loads(request.read_text())
        Path(document["result"]).write_text(
            json.dumps(
                {
                    "result": 1,
                    "status": "success",
                    "action": "describe",
                    "reason": None,
                    "error": None,
                    "program": self.program,
                }
            )
        )
        return bs_container.Completed(status=0, output="")


def _emit(request: dict[str, Any], name: str, seq: int, **fields: Any) -> None:
    with Path(request["events"]).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": name, "seq": seq, **fields}) + "\n")


def _manifest_id(request: dict[str, Any]) -> str:
    """The context id, read out of the manifest the *server* wrote.

    Read rather than recomputed: a conforming program measures the
    materialized context and arrives at the value the manifest declares,
    and re-deriving the frozen rule in a fixture is the second
    implementation ADR 0020 decision 4 exists to prevent.
    """
    from ruamel.yaml import YAML

    data = YAML(typ="safe", pure=True).load(
        (Path(request["context"]) / "manifest.yaml").read_text()
    )
    return str(data["id"])


#: The artifact set every conforming run below declares. ``paths`` may
#: hold a nested path — the build server packs a member per declared
#: artifact and tar synthesizes no parent directories, so a nested one is
#: what proves the client creates them.
ARTIFACTS: tuple[tuple[str, bytes], ...] = (
    ("firmware.hex", b":020000040000FA\n"),
    ("firmware.bin", b"\x00\x01\x02\x03"),
    ("build-report.json", json.dumps(BUILD_REPORT).encode()),
)


def _start_program(action: str, request: dict[str, Any], on_line) -> str:
    """The first two events of a conforming run, and the context it checked."""
    on_line(f"-- MCUHome {action} starting")
    _emit(request, "invocation.started", 1, action=action)
    identity = _manifest_id(request)
    _emit(request, "context.checked", 2, context=identity)
    return identity


def _finish_program(
    action: str,
    request: dict[str, Any],
    on_line,
    *,
    identity: str,
    artifacts: tuple[tuple[str, bytes], ...] = ARTIFACTS,
) -> None:
    """The artifacts, the result document and the closing event (seq 3..6)."""
    out = Path(request["out"])
    declared = []
    for name, payload in artifacts:
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        declared.append(
            {
                "root": "out",
                "path": name,
                "role": "report" if name.endswith(".json") else "firmware",
                "hashes": {"sha256": hashlib.sha256(payload).hexdigest()},
            }
        )
    for index, entry in enumerate(declared, start=3):
        _emit(request, "artifact.collected", index, role=entry["role"], path=entry["path"], size=1)
    on_line("-- build finished")
    document = {
        "result": 1,
        "status": "success",
        "action": action,
        "session": request["session"],
        "reason": None,
        "error": None,
        "context": identity,
        "artifacts": declared,
    }
    if action == "build":
        # §5.4: `layers` is what a build reports about the trees it
        # patched, and a `verify` result may not carry one at all.
        document["layers"] = {}
    Path(request["result"]).write_text(json.dumps(document))
    _emit(request, "invocation.finished", len(declared) + 3, status="success")


def conforming_program(action: str, request: dict[str, Any], on_line) -> FakeProcess:
    """A build container that does everything contract v1 asks of it."""
    identity = _start_program(action, request, on_line)
    _finish_program(action, request, on_line, identity=identity)
    return FakeProcess(0)


def hanging_program(action: str, request: dict[str, Any], on_line) -> FakeProcess:
    """A program that never finishes on its own — what ``cancel`` is for."""
    _emit(request, "invocation.started", 1, action=action)
    return FakeProcess(0, hang=True)


class GatedProcess(FakeProcess):
    """A program that has started, said so, and is waiting to be let go.

    The one shape a test of *connection loss* needs and neither of the
    programs above has: it emits its first events, then blocks until a
    file appears, and only then writes its artifacts, its result document
    and the closing event. That is what makes "the socket drops **while
    the invocation is running**" a thing a test can arrange rather than
    a thing it hopes for.
    """

    def __init__(self, gate: Path, *, action: str, request: dict[str, Any], on_line, identity: str):
        super().__init__(0)
        self._gate = gate
        self._action = action
        self._request = request
        self._on_line = on_line
        self._identity = identity

    async def wait(self) -> int:
        while not self._gate.exists():
            await asyncio.sleep(0.01)
        _finish_program(self._action, self._request, self._on_line, identity=self._identity)
        # The receipt the test waits on: the events file and the result
        # document are complete from here, so a reattaching client has
        # something to replay and the backend has a verdict to publish.
        self._gate.with_suffix(".done").write_text("done", encoding="utf-8")
        return 0


def gated_program(gate: Path):
    """A ``run_program`` that finishes when *gate* is created."""

    def program(action: str, request: dict[str, Any], on_line) -> FakeProcess:
        identity = _start_program(action, request, on_line)
        return GatedProcess(
            gate, action=action, request=request, on_line=on_line, identity=identity
        )

    return program


def poisoning_program(action: str, request: dict[str, Any], on_line) -> FakeProcess:
    """A run whose own reason poisons the session (§6.2, E39).

    ``error.patch.incomplete`` is one of the two reasons the error
    registry maps to ``session.poisoned``: an interrupted patch
    application leaves trees no future build may trust. The server
    poisons the session while assembling the verdict, so the *verdict*
    carries the code — which is the path a client learns about
    asynchronously rather than from a refused command.
    """
    identity = _start_program(action, request, on_line)
    Path(request["result"]).write_text(
        json.dumps(
            {
                "result": 1,
                "status": "failure",
                "action": action,
                "session": request["session"],
                "reason": "error.patch.incomplete",
                "error": {"message": "the patch application was interrupted"},
                "context": identity,
                "layers": {},
                "artifacts": [],
            }
        )
    )
    _emit(request, "invocation.finished", 3, status="failure")
    return FakeProcess(1)


# --------------------------------------------------------------------------
# The context on disk, and the SDK package the pins name
# --------------------------------------------------------------------------


def make_archive(entries: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return zstandard.ZstdCompressor().compress(raw.getvalue())


def archive_members(spool: Path) -> list[str]:
    """The member names of a packed context — the **archive**, not a list about it.

    The distinction is the whole of a bug this suite once encoded: the
    integrity list (``PackedContext.files``) excludes ``context.yaml`` by
    construction, so asserting on it can never say whether the archive
    carried the file. Only the tar can.
    """
    plain = zstandard.ZstdDecompressor().decompress(spool.read_bytes(), max_output_size=1 << 24)
    with tarfile.open(fileobj=io.BytesIO(plain)) as tar:
        return [member.name for member in tar.getmembers()]


def write_sdk_package(directory: Path, *, declared_sha256: str | None = None) -> str:
    """Put one SDK package where the server's source list will find it.

    And its static ``index.json`` beside it, because since E65 one
    directory serves both parties of the pin: the **client** resolves
    ``(version, sha256)`` out of the index before a context can exist,
    and the **server** finds the archive by the version in its name and
    checks the bytes against the hash. That is the local-source shape E48
    configures, with the two readers it actually has.

    *declared_sha256* overrides what the index declares, which is the one
    way to arrange the case E65 exists for: a context pinning bytes the
    server's source does not hold. The **real** hash is returned either
    way — a caller writing a context by hand pins that one.
    """
    directory.mkdir(parents=True, exist_ok=True)
    archive = make_archive({"mcuhome/__init__.py": b"# the SDK\n"})
    name = f"mcuhome-sdk-{SDK_VERSION}.tar.zst"
    (directory / name).write_bytes(archive)
    real = hashlib.sha256(archive).hexdigest()
    (directory / "index.json").write_text(
        json.dumps(
            {
                "packages": {
                    "mcuhome-sdk": {
                        SDK_VERSION: {
                            "file": name,
                            "sha256": declared_sha256 or real,
                            "size": len(archive),
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return real


#: One known private key, so the invariant test can look for exact bytes.
#: The scalar is fixed rather than drawn, which is what makes "these bytes
#: appear in no frame" a check on values rather than on luck.
KEY_SCALAR = 0x1F2E3D4C5B6A79889796A5B4C3D2E1F00F1E2D3C4B5A69788796A5B4C3D2E1F0


def make_context(root: Path, *, sdk_sha256: str, patches: dict[str, bytes] | None = None) -> str:
    """A base context directory, as ``create_context`` lays one out.

    Written by hand rather than through
    :func:`mcuhome.workbench.contextdir.create_context` for one reason:
    that function needs a resolved :class:`DeviceModel`, and nothing on
    either side of this protocol parses the model — the server carries no
    build logic at all. The parts that *are* protocol are produced by the
    real writers: ``context.yaml`` through
    :func:`~mcuhome.workbench.contextdir.write_context_request`, and the
    public key through :mod:`mcuhome.workbench.signing`.

    Returns the private key PEM, which stays on this side of the wire
    forever — it is written nowhere inside *root*.
    """
    private_pem = signing.generate_key_pem(KEY_SCALAR)
    (root / "model").mkdir(parents=True, exist_ok=True)
    (root / "model" / "device-model.json").write_text('{"device": "test"}\n', encoding="utf-8")
    (root / "keys").mkdir(parents=True, exist_ok=True)
    (root / "keys" / "signing.pub").write_text(
        signing.public_key_pem(private_pem), encoding="utf-8"
    )
    for name, payload in (patches or {}).items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    write_context_request(
        ContextRequest(
            sdk=SdkPin(
                constraint="~=2.3",
                version=SDK_VERSION,
                url="https://example.invalid/mcuhome-sdk.tar.zst",
                sha256=sdk_sha256,
            ),
            zephyr=ZEPHYR_LINE,
            board="nrf7002dk/nrf5340/cpuapp",
            created="2026-08-10T09:00:00Z",
        ),
        out_dir=root,
    )
    return private_pem


# --------------------------------------------------------------------------
# The harness: one real server, one recording client
# --------------------------------------------------------------------------


class RecordingSocket:
    """Every outbound frame, recorded, then forwarded unchanged.

    A wrapper rather than a monkeypatch on aiohttp's own object, so the
    recording is a property of this test and not of the library. It is
    what the key invariant is asserted over: *everything* this client
    sent, text and binary, in order.
    """

    def __init__(self, ws: Any, log: list[tuple[str, Any]]) -> None:
        self._ws = ws
        self.log = log

    @property
    def closed(self) -> bool:
        return self._ws.closed

    async def send_str(self, data: str) -> None:
        self.log.append(("text", data))
        await self._ws.send_str(data)

    async def send_bytes(self, data: bytes) -> None:
        self.log.append(("binary", bytes(data)))
        await self._ws.send_bytes(data)

    async def close(self) -> None:
        await self._ws.close()

    def __aiter__(self):
        return self._ws.__aiter__()


class RecordingClient(sc.SessionClient):
    """A :class:`~mcuhome.workbench.sessionclient.SessionClient` that keeps
    a copy of everything it put on the wire."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.sent: list[tuple[str, Any]] = []

    async def connect(self) -> None:
        await super().connect()
        if not isinstance(self._ws, RecordingSocket):
            self._ws = RecordingSocket(self._ws, self.sent)


@dataclass
class Harness:
    url: str
    state: Any
    docker: FakeDocker
    config: Any


@contextlib.asynccontextmanager
async def real_server(tmp_path: Path, *, docker: FakeDocker | None = None, **overrides: Any):
    """One real build server on a real socket, with docker stubbed out."""
    fake = docker if docker is not None else FakeDocker()
    saved = (bs_container.run_docker, bs_container.spawn_docker)
    bs_container.run_docker = fake.run
    bs_container.spawn_docker = fake.spawn
    config = bs_config.Config(
        host="127.0.0.1",
        port=0,
        token=TOKEN,
        pair_file=None,
        context_root=tmp_path / "sessions",
        sdk_sources=(tmp_path / "packages",),
        **overrides,
    )
    state = bs_app.ServerState(config)
    server = test_utils.TestServer(bs_app.create_app(state))
    await server.start_server()
    try:
        yield Harness(url=str(server.make_url("/ws")), state=state, docker=fake, config=config)
    finally:
        await server.close()
        bs_container.run_docker, bs_container.spawn_docker = saved


def client_for(harness: Harness, tmp_path: Path, **kwargs: Any) -> RecordingClient:
    return RecordingClient(
        harness.url, token=TOKEN, spool_dir=tmp_path / "spool", call_timeout=30.0, **kwargs
    )


def run(coro) -> Any:
    """Every test owns its event loop; nothing here inherits a dirty one."""
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# The handshake and the context path
# --------------------------------------------------------------------------


def test_capabilities_is_the_handshake_and_carries_no_session(tmp_path: Path) -> None:
    """The one verb with no session id, because there is no session yet.

    It is what lets a workbench choose a build container during pin
    resolution rather than discover a mismatch from inside one, so the
    inventory and the patch policy are asserted here rather than assumed
    by everything after.
    """

    async def scenario() -> None:
        async with real_server(tmp_path) as harness, client_for(harness, tmp_path) as client:
            answer = await client.capabilities()
        assert answer.protocol_version == bs_sessions.SESSION_PROTOCOL_VERSION
        assert "oneshot" in answer.profiles
        assert [entry["digest"] for entry in answer.containers] == [IMAGE_DIGEST]
        # Deny by default: the server was configured with no layers.
        assert answer.allows_patch_layer("zephyr") is False
        # E57: the caps are announced, and this server's are its own
        # configuration. Nothing here is a default of the client's.
        assert answer.ingress() == sc.IngressCaps(
            compressed_bytes=harness.config.max_compressed_bytes,
            decompressed_bytes=harness.config.max_decompressed_bytes,
            entries=harness.config.max_entries,
            file_bytes=harness.config.max_file_bytes,
            path_depth=harness.config.max_path_depth,
            frame_bytes=bs_protocol.MAX_FRAME_BYTES,
        )

    run(scenario())


def test_the_full_session_runs_end_to_end_against_the_real_server(tmp_path: Path) -> None:
    """open → send-context → lock → build → get-artifact → close.

    The path the ``remote`` build method is: an unsigned image and the
    §7.2.1 build report come back, attributed to a context ID both sides
    computed independently.
    """

    async def scenario() -> None:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        context.mkdir()
        make_context(context, sdk_sha256=sdk_sha256)
        lines: list[str] = []
        events: list[tuple[str, dict]] = []
        async with (
            real_server(tmp_path) as harness,
            client_for(
                harness,
                tmp_path,
                on_line=lines.append,
                on_event=lambda name, payload: events.append((name, payload)),
            ) as client,
        ):
            await client.capabilities()
            await client.open_session()
            sent = await client.send_context(context)
            assert sent["container"]["contract"] == 1
            identity = await client.lock_context()
            invocation_id = await client.build()
            verdict = await client.wait_finished(invocation_id, timeout=30)
            assert verdict["status"] == "success"
            assert verdict["context"] == identity
            delivery = await client.get_artifact(
                invocation_id,
                into=tmp_path / "out",
                expected={entry["path"]: entry["sha256"] for entry in verdict["artifacts"]},
            )
            await client.close_session()

        assert set(delivery.files) == {"firmware.hex", "firmware.bin", "build-report.json"}
        # The build delivers an UNSIGNED image plus the parameters a host
        # signer needs (E55/E56) — the signature is never part of what
        # comes back over this wire. Asserted on what the *server*
        # decided rather than on the fixture's own constant: the roles it
        # required of a successful build (exactly one report, at least
        # one firmware) and the absence of anything signed.
        roles = [entry["role"] for entry in verdict["artifacts"]]
        assert roles.count("report") == 1
        assert roles.count("firmware") >= 1
        assert not [entry for entry in verdict["artifacts"] if "sign" in entry["path"]]
        assert not any("sign" in name for name in delivery.files)
        assert lines and any("build finished" in line for line in lines)
        # Two frames end an invocation and E58 gives them two names: the
        # program's contract §8 announcement, numbered like every program
        # event, and the server's verdict. Both reach the sink; only the
        # second is what `wait_finished` returned.
        seen = [name for name, _ in events]
        assert seen.count("invocation.finished") == 1
        assert seen.count("invocation.verdict") == 1
        assert seen.index("invocation.finished") < seen.index("invocation.verdict")
        assert dict(events)["invocation.verdict"] == verdict

    run(scenario())


def test_a_second_base_context_is_refused_typed(tmp_path: Path) -> None:
    """E43: one base context per session; a fresh start is a new session."""

    async def scenario() -> None:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        context.mkdir()
        make_context(context, sdk_sha256=sdk_sha256)
        async with real_server(tmp_path) as harness, client_for(harness, tmp_path) as client:
            await client.capabilities()
            await client.open_session()
            await client.send_context(context)
            with pytest.raises(sc.ServerRefusal) as refusal:
                await client.send_context(context)
            await client.close_session()
        assert refusal.value.code == "context.exists"
        assert refusal.value.retryable is False

    run(scenario())


def test_extend_context_adds_and_removes_in_one_call(tmp_path: Path) -> None:
    """E42: the archive and the remove list travel together.

    And the client's own integrity ledger follows both halves, which is
    what makes the E37 comparison after an extension mean anything.
    """

    async def scenario() -> None:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        context.mkdir()
        make_context(
            context,
            sdk_sha256=sdk_sha256,
            patches={"patches/zephyr/0001-old.patch": b"--- old\n"},
        )
        async with (
            real_server(tmp_path, allowed_patch_layers=("zephyr",)) as harness,
            client_for(harness, tmp_path) as client,
        ):
            await client.capabilities()
            await client.open_session()
            await client.send_context(context)
            (context / "patches" / "zephyr" / "0002-new.patch").write_bytes(b"--- new\n")
            answer = await client.extend_context(
                context,
                paths=["patches/zephyr/0002-new.patch"],
                remove=["patches/zephyr/0001-old.patch"],
            )
            identity = await client.lock_context()
            await client.close_session()
        assert answer["removed"] == 1
        assert identity.startswith("sha256:")

    run(scenario())


def test_extend_context_refuses_to_touch_the_pin_file(tmp_path: Path) -> None:
    """``context.yaml`` is untouchable in both directions.

    Refused on this side before a frame goes out — changing the pins is a
    new session, not an extension, and the client knows that without
    asking. Both ways of naming it are refused: as a removal, and as a
    path of the archive half.

    The packer is asserted on the **archive**, because that is what the
    server reads: a base context carries ``context.yaml`` as a member
    (and keeps it out of the integrity list, which is a different
    statement), an extension does not carry it at all.
    """

    async def scenario() -> None:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        context.mkdir()
        make_context(context, sdk_sha256=sdk_sha256)
        async with real_server(tmp_path) as harness, client_for(harness, tmp_path) as client:
            await client.capabilities()
            await client.open_session()
            await client.send_context(context)
            with pytest.raises(sc.RemoteError, match="context.yaml"):
                await client.extend_context(remove=["context.yaml"])
            with pytest.raises(sc.RemoteError, match="context.yaml"):
                await client.extend_context(context, paths=["context.yaml"])
            await client.close_session()

    run(scenario())

    base = tmp_path / "base.tar.zst"
    sc.pack_context(tmp_path / "context", spool=base)
    assert "context.yaml" in archive_members(base)

    extension = tmp_path / "extension.tar.zst"
    packed = sc.pack_context(tmp_path / "context", spool=extension, for_extension=True)
    assert "context.yaml" not in archive_members(extension)
    assert "context.yaml" not in packed.members
    assert all(entry.path != "context.yaml" for entry in packed.files)


def test_an_extension_of_the_whole_directory_is_a_legal_extension(tmp_path: Path) -> None:
    """``extend_context(context_dir)`` with no ``paths`` — the add-everything mode.

    The signature permits it and the docstring documents it, so it has to
    work against the real server: the pin file is excluded from the
    extension archive here, and what arrives is an extension the server
    accepts rather than one it refuses with ``context.pins-immutable``
    after the whole upload.
    """

    async def scenario() -> dict[str, Any]:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        context.mkdir()
        make_context(context, sdk_sha256=sdk_sha256)
        async with real_server(tmp_path) as harness, client_for(harness, tmp_path) as client:
            await client.capabilities()
            await client.open_session()
            await client.send_context(context)
            (context / "model" / "extra.json").write_text('{"more": true}\n', encoding="utf-8")
            answer = await client.extend_context(context)
            identity = await client.lock_context()
            await client.close_session()
        assert identity.startswith("sha256:")
        return answer

    answer = run(scenario())
    assert answer["removed"] == 0


# --------------------------------------------------------------------------
# E37 — the comparison duty, both ways
# --------------------------------------------------------------------------


def test_a_matching_context_id_lets_the_session_proceed(tmp_path: Path) -> None:
    """The client recomputes and agrees, so ``verify`` becomes reachable.

    The ID the server answers is computed from the bytes it received off
    a socket; the ID the client compares it to is computed from the
    integrity list it built while packing. Two implementations of one
    frozen rule agreeing is the whole point of the freeze.
    """

    async def scenario() -> None:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        context.mkdir()
        make_context(context, sdk_sha256=sdk_sha256)
        async with real_server(tmp_path) as harness, client_for(harness, tmp_path) as client:
            await client.capabilities()
            await client.open_session()
            await client.send_context(context)
            identity = await client.lock_context()
            assert identity == client.compute_context_id()
            assert client.context_state == "locked"
            # Only from the lock onwards: verify is now reachable.
            invocation_id = await client.verify()
            assert invocation_id.startswith("inv-")
            await client.wait_finished(invocation_id, timeout=30)
            await client.close_session()

    run(scenario())


def test_a_wrong_context_id_closes_the_session_and_raises(tmp_path: Path) -> None:
    """E37's other half, and the one the server can never raise itself.

    The minimal wire shape means the server never sees the client's
    value, so nothing on that side can notice a disagreement. This makes
    the server answer a different ID and asserts what the *client* does
    about it: name both values, close the session, and refuse to go on.
    """

    async def scenario() -> None:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        context.mkdir()
        make_context(context, sdk_sha256=sdk_sha256)
        wrong = "sha256:" + "f" * 64
        real_freeze = bs_sessions.freeze_context

        def lying_freeze(*args: Any, **kwargs: Any) -> str:
            real_freeze(*args, **kwargs)
            return wrong

        bs_sessions.freeze_context = lying_freeze
        try:
            async with real_server(tmp_path) as harness, client_for(harness, tmp_path) as client:
                await client.capabilities()
                await client.open_session()
                await client.send_context(context)
                with pytest.raises(sc.ContextIdMismatch) as mismatch:
                    await client.lock_context()
                assert client.session_id is None, "the session was closed on the way out"
                assert client.terminal == "context.id-mismatch"
                # There is no session left, so the mismatch is
                # terminal rather than something to retry into.
                with pytest.raises(sc.RemoteTransportError):
                    await client.build()
                assert mismatch.value.remote_id == wrong
                assert mismatch.value.local_id != wrong
                assert wrong in str(mismatch.value)
                assert mismatch.value.local_id in str(mismatch.value)
                assert harness.state.sessions.open_count == 0
        finally:
            bs_sessions.freeze_context = real_freeze

    run(scenario())


# --------------------------------------------------------------------------
# The key invariant
# --------------------------------------------------------------------------


def test_no_frame_this_client_sends_carries_the_private_signing_key(tmp_path: Path) -> None:
    """The security invariant, in its sharpest form.

    ADR 0015 decision 8 as the product owner restated it on 2026-08-10:
    the private signing key never leaves the local machine. For the
    ``remote`` method that means it appears in **no frame sent to the
    build server** — the server is not trusted, which is the whole reason
    the build returns an unsigned image and the host signs afterwards
    (E55, E56).

    A real key pair is generated on disk and the scenario is the hostile
    one: the private half is placed **inside the context directory**, at
    ``keys/signing.key``, which is where a scaffold or a hurried user
    would leave it and which the server's own path whitelist accepts. The
    client must refuse to pack it — a stray key is a loud refusal, not an
    upload — and the session then runs to completion without it.

    Every outbound frame is searched afterwards, and **the decompressed
    archive with them**: a context travels as zstd-compressed tar, so a
    needle search over the raw binary frames could never have found a key
    that rode inside one. That is the only route the key could actually
    take, which makes it the one the search has to cover. The public half
    is searched for too, as a control: it *does* travel (as
    ``keys/signing.pub`` inside the context), so a search that found
    neither would prove nothing about the search.
    """

    async def scenario() -> tuple[list[tuple[str, Any]], str]:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        context.mkdir()
        private_pem = make_context(context, sdk_sha256=sdk_sha256)
        # The key lives on disk, where a real user's does...
        key_file = tmp_path / "signing.key"
        key_file.write_text(private_pem, encoding="utf-8")
        # ...and, in this scenario, also where nobody's should: next to
        # the public half, inside the context that is about to be sent.
        stray = context / "keys" / signing.KEY_FILE
        stray.write_text(private_pem, encoding="utf-8")
        async with real_server(tmp_path) as harness, client_for(harness, tmp_path) as client:
            await client.capabilities()
            await client.open_session()
            with pytest.raises(sc.PrivateKeyRefused, match="private key material"):
                await client.send_context(context)
            assert not [frame for kind, frame in client.sent if kind == "binary"], (
                "the refusal has to come before the first byte of the archive"
            )
            # Removing it is the whole fix, and the session is untouched.
            stray.unlink()
            await client.send_context(context)
            await client.lock_context()
            invocation_id = await client.build()
            await client.wait_finished(invocation_id, timeout=30)
            await client.get_artifact(invocation_id, into=tmp_path / "out")
            await client.close_session()
            return list(client.sent), private_pem

    sent, private_pem = run(scenario())
    assert sent, "the recorder saw nothing, so it proves nothing"

    wrapped = [line for line in private_pem.splitlines() if line and not line.startswith("-----")]
    body = "".join(wrapped)
    secrets = {
        # Every form the private half could plausibly travel in: the file
        # as it is on disk, its base64 body both wrapped and joined, the
        # DER bytes under it, the raw scalar, its hex rendering, and the
        # path of the file — because a client that sent the *path* would
        # be no better than one that sent the key on a machine where the
        # server can read it.
        "pem": private_pem.encode(),
        "pem-first-line": wrapped[0].encode(),
        "pem-body": body.encode(),
        "der": base64.b64decode(body),
        "scalar": KEY_SCALAR.to_bytes(32, "big"),
        "scalar-hex": KEY_SCALAR.to_bytes(32, "big").hex().encode(),
        "path": str(tmp_path / "signing.key").encode(),
    }
    binary = b"".join(frame for kind, frame in sent if kind == "binary")
    plain = zstandard.ZstdDecompressor().decompress(binary)
    haystacks = [
        (f"{kind} frame", frame.encode("utf-8") if kind == "text" else frame)
        for kind, frame in sent
    ]
    haystacks.append(("decompressed archive", plain))
    for where, raw in haystacks:
        for name, needle in secrets.items():
            assert needle not in raw, f"the private key ({name}) reached the wire in a {where}"

    # The control: the *public* half did travel, inside the context
    # archive, so the search above is looking at bytes that really carry
    # key material and not at an empty haystack.
    public = signing.public_key_pem(private_pem)
    assert public.encode() in plain, "the public key is what a context carries"


def test_the_packer_refuses_private_key_material_by_name_and_by_content(tmp_path: Path) -> None:
    """The hole the invariant above would otherwise only measure.

    The API has no slot a private key could be passed in, but a file in
    the context directory needs no slot — it is packed as ordinary
    content and the build server's whitelist takes *any* name under
    ``keys/``. So the packer looks twice: at the name this workbench
    writes its key under, and at the bytes, because a stray key under
    another name is the same accident.
    """
    context = tmp_path / "context"
    context.mkdir()
    private_pem = make_context(context, sdk_sha256="a" * 64)

    (context / "keys" / signing.KEY_FILE).write_text(private_pem, encoding="utf-8")
    with pytest.raises(sc.PrivateKeyRefused, match="keys/signing.key"):
        sc.pack_context(context, spool=tmp_path / "named.tar.zst")
    (context / "keys" / signing.KEY_FILE).unlink()

    # The same key under a name nothing recognizes, found by its bytes.
    (context / "model" / "leftover.txt").write_text(
        f"# kept for reference\n{private_pem}", encoding="utf-8"
    )
    with pytest.raises(sc.PrivateKeyRefused, match="leftover.txt") as refusal:
        sc.pack_context(context, spool=tmp_path / "sniffed.tar.zst")
    assert "signing.pub" in refusal.value.hint
    (context / "model" / "leftover.txt").unlink()

    # And the public half is not key material in this sense: a context
    # that carries it packs, which is what the invariant needs.
    packed = sc.pack_context(context, spool=tmp_path / "clean.tar.zst")
    assert "keys/signing.pub" in packed.members


def test_the_public_api_has_no_slot_a_private_key_could_occupy(tmp_path: Path) -> None:
    """The structural half: a key that cannot be passed cannot be sent.

    The invariant above is a fact about one recorded session; this is a
    fact about the module. Every public callable and every public method
    is introspected, and a parameter whose name so much as mentions a key
    is a defect unless it says *public* — because a caller who can hand
    this client a private key will eventually hand it one.
    """
    offenders: list[str] = []
    for owner, name, function in _public_callables(sc):
        for parameter in inspect.signature(function).parameters:
            if "key" in parameter.lower() and "public" not in parameter.lower():
                offenders.append(f"{owner}.{name}({parameter})")
    assert offenders == [], f"a private key could be passed here: {offenders}"

    # And the reverse, so the search is known to reach real signatures —
    # including the three kinds a `vars()` + `isfunction` scan silently
    # skips, of which the constructor is the one that matters: it is the
    # single most likely place for a `signing_key=` to be added, and
    # "no parameter of this class" is a claim about exactly it.
    seen = {f"{owner}.{name}" for owner, name, _ in _public_callables(sc)}
    assert "sessionclient.run_remote_build" in seen
    assert "SessionClient.send_context" in seen
    assert "SessionClient.build" in seen
    assert "SessionClient.__init__" in seen
    assert "IngressCaps.announced" in seen, "a staticmethod is a callable too"
    assert "Capabilities.protocol_version" in seen, "so is a property"
    assert "RemoteBuildResult.__init__" in seen, "including a dataclass's generated one"


def _public_callables(module: Any):
    """Every public function, every public class, and every public method.

    A class is yielded as a callable in its own right, because
    ``inspect.signature(cls)`` is what reaches ``__init__`` — the
    parameters of the constructor are parameters of the public API, and a
    scan that filtered every name starting with an underscore would drop
    the one signature the invariant above is most about. Descriptors are
    unwrapped for the same reason: ``vars()`` hands back
    ``staticmethod``/``classmethod``/``property`` objects, none of which
    is an ``inspect.isfunction``, so an unwrapping scan is the difference
    between reading a signature and skipping it.
    """
    for name in module.__all__:
        found = getattr(module, name)
        if inspect.isfunction(found):
            yield "sessionclient", name, found
        elif inspect.isclass(found):
            yield found.__name__, "__init__", found
            for member, attribute in vars(found).items():
                if member.startswith("__"):
                    continue
                function = _unwrapped(attribute)
                if function is None:
                    continue
                yield found.__name__, member, function


def _unwrapped(attribute: Any):
    """The function under a descriptor, or ``None`` for a non-callable."""
    for holder in ("__func__", "fget"):
        found = getattr(attribute, holder, None)
        if found is not None:
            return found
    return attribute if inspect.isfunction(attribute) else None


def test_every_public_name_of_the_module_is_exported() -> None:
    """``__all__`` is the declared surface, so it has to be the real one.

    A name missing from it is a name ``from … import *`` does not bind
    and documentation tooling does not show — and the ones that went
    missing were the family's base class ``RemoteError``, which
    ``extend_context`` raises directly, and ``ContextTooLarge``, which
    the exported ``pack_context`` raises six times. A caller cannot
    handle an exception it cannot name.

    Read from the syntax tree rather than from ``dir()``: an imported
    name is not this module's to export, and only the source says which
    is which.
    """
    import ast

    source = Path(sc.__file__).read_text(encoding="utf-8")
    defined: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            defined.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
    public = {name for name in defined if not name.startswith("_")}

    assert public - set(sc.__all__) == set(), "a public name this module defines is not exported"
    assert all(hasattr(sc, name) for name in sc.__all__), "__all__ names something that is gone"
    assert sc.__all__ == sorted(set(sc.__all__), key=_export_order), "__all__ is kept sorted"


def _export_order(name: str) -> tuple[int, str]:
    """Constants, then classes, then functions — the file's own order."""
    if name.upper() == name:
        return (0, name)
    if name[0].isupper():
        return (1, name)
    return (2, name)


# --------------------------------------------------------------------------
# Caps
# --------------------------------------------------------------------------


def test_an_oversized_file_is_refused_before_a_frame_goes_out(tmp_path: Path) -> None:
    """The cheap half of the same refusal the server would give.

    The server enforces its caps while the bytes arrive and answers
    ``policy.ingress-limit-exceeded``; refusing here is the same answer
    without the upload in front of it.
    """
    context = tmp_path / "context"
    context.mkdir()
    make_context(context, sdk_sha256="a" * 64)
    caps = replace(sc.E44_CAPS, file_bytes=8)
    with pytest.raises(sc.ContextTooLarge, match="in one context file"):
        sc.pack_context(context, spool=tmp_path / "small.tar.zst", caps=caps)
    assert (
        not (tmp_path / "small.tar.zst").exists()
        or (tmp_path / "small.tar.zst").stat().st_size == 0
    ), "nothing usable was produced"

    # The other four caps refuse the same way, before anything is sent.
    with pytest.raises(sc.ContextTooLarge, match="path segments"):
        sc.pack_context(
            context,
            spool=tmp_path / "deep.tar.zst",
            caps=replace(sc.E44_CAPS, path_depth=1),
        )
    with pytest.raises(sc.ContextTooLarge, match="archive entries"):
        sc.pack_context(
            context,
            spool=tmp_path / "many.tar.zst",
            caps=replace(sc.E44_CAPS, entries=1),
        )


def test_the_caps_are_counted_across_the_session_not_per_archive(tmp_path: Path) -> None:
    """E44's caps are cumulative, so the client's arithmetic has to be too.

    The server's ledger charges the base context and every extension of
    one session against one budget, and it charges entries *while it
    extracts* — i.e. after the whole upload has arrived. A client that
    checked each archive against the full cap in isolation would pass its
    own check and be refused at ``policy.ingress-limit-exceeded`` after
    60 MiB of upload, which is the exact outcome
    :class:`ContextTooLarge`'s docstring says it exists to prevent.
    """

    async def scenario() -> None:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        context.mkdir()
        make_context(context, sdk_sha256=sdk_sha256)
        for index in range(4):
            (context / "model" / f"part-{index}.json").write_text("{}\n", encoding="utf-8")
        async with real_server(tmp_path) as harness, client_for(harness, tmp_path) as client:
            await client.capabilities()
            await client.open_session()
            # A budget of exactly what the base context costs, and not
            # one entry more.
            await client.send_context(context)
            spent = client.spent
            assert spent.entries == 7, "context.yaml, the model files and the public key"
            assert spent.compressed_bytes > 0 and spent.decompressed_bytes > 0
            client.caps = replace(sc.E44_CAPS, entries=spent.entries)

            (context / "model" / "one-too-many.json").write_text("{}\n", encoding="utf-8")
            with pytest.raises(sc.ContextTooLarge, match="already sent in this session"):
                await client.extend_context(context, paths=["model/one-too-many.json"])
            announced = [frame for kind, frame in client.sent if kind == "text"]
            assert not any("extend-context" in frame for frame in announced), (
                "the refusal came before the extension was announced"
            )
            await client.close_session()

            # A new session starts the budget again, because the server's
            # ledger is per session as well.
            await client.open_session()
            assert client.spent == sc.IngressSpent()

    run(scenario())


def test_the_packer_refuses_a_path_that_leaves_the_context(tmp_path: Path) -> None:
    """``only`` is caller data, and the one path that could point outside.

    Every other path this module consumes comes from walking the context
    root or from an archive it checks member by member. ``paths`` does
    not: it is handed in, it is written verbatim into the tar member
    name, and a ``..`` in it would read a file outside the context and
    stream it to the server before the server ever sees the name. So it
    is refused rather than normalized — the outbound twin of the
    inbound rule.
    """
    context = tmp_path / "context"
    context.mkdir()
    make_context(context, sdk_sha256="a" * 64)
    secret = tmp_path / "secret.key"
    secret.write_bytes(b"PRIVATE-KEY-BYTES")
    spool = tmp_path / "escape.tar.zst"

    for named in ("../secret.key", "/etc/shadow", "model/../../secret.key", "model\\a.json"):
        with pytest.raises(sc.RemoteError):
            sc.pack_context(context, spool=spool, only=[named])
        assert not spool.exists(), "nothing was produced for a refused path"

    # A symlinked segment is refused too: it is the other way out, and
    # the one `..` alone does not cover.
    (context / "elsewhere").symlink_to(tmp_path)
    with pytest.raises(sc.RemoteError, match="does not stay inside"):
        sc.pack_context(context, spool=spool, only=["elsewhere/secret.key"])

    # The legitimate call still works, which is what makes the refusal a
    # refusal rather than a broken parameter.
    packed = sc.pack_context(context, spool=spool, only=["model/device-model.json"])
    assert packed.members == ("model/device-model.json",)


def test_an_oversized_file_the_client_did_send_is_refused_typed(tmp_path: Path) -> None:
    """And the server's own answer, surfaced as the typed refusal it is.

    **The announcement is a courtesy and never the enforcement.** Since
    E57 a client that applies what it was told refuses this context at
    home, which is the point of announcing — so this test puts the
    client's caps back where an announcement-blind client would have
    them (E44's defaults, what an older client or one talking to a
    silent third-party server uses) and sends anyway. What is asserted is
    the far side: the server enforces its own configuration while the
    bytes arrive and answers a typed refusal, rather than dropping the
    connection or accepting what it advertised against.
    """

    async def scenario() -> None:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        context.mkdir()
        make_context(context, sdk_sha256=sdk_sha256)
        async with (
            real_server(tmp_path, max_file_bytes=8) as harness,
            client_for(harness, tmp_path) as client,
        ):
            await client.capabilities()
            assert client.caps.file_bytes == 8, "the small cap was announced"
            client.caps = replace(client.caps, file_bytes=sc.E44_CAPS.file_bytes)
            await client.open_session()
            with pytest.raises(sc.ServerRefusal) as refusal:
                await client.send_context(context)
            await client.close_session()
        assert refusal.value.code == "policy.ingress-limit-exceeded"
        assert refusal.value.retryable is False

    run(scenario())


def test_an_announced_cap_is_refused_at_home_before_a_byte_leaves(tmp_path: Path) -> None:
    """The other half of E57, and the reason the caps are announced at all.

    The same server and the same context as the test above, with the
    client applying what it was told: the refusal is local, typed as
    :class:`~mcuhome.workbench.sessionclient.ContextTooLarge`, and
    nothing was uploaded — no session-quota charge, no minutes of
    transfer, and the number in the message is the *server's*.
    """

    async def scenario() -> None:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        context.mkdir()
        make_context(context, sdk_sha256=sdk_sha256)
        async with (
            real_server(tmp_path, max_file_bytes=8) as harness,
            client_for(harness, tmp_path) as client,
        ):
            await client.capabilities()
            await client.open_session()
            with pytest.raises(sc.ContextTooLarge, match="at most 8"):
                await client.send_context(context)
            await client.close_session()
            assert not [kind for kind, _ in client.sent if kind == "binary"]

    run(scenario())


def test_the_announced_caps_are_the_ones_the_client_applies(tmp_path: Path) -> None:
    """E57 end to end: the server's configuration sizes the client's checks.

    Every cap is given a distinctive value, and none of them is one of
    E44's defaults — an assertion against the defaults would pass on a
    client that read nothing at all. What is asserted is the *effective*
    caps: what ``capabilities`` left on the client, which is what
    :func:`~mcuhome.workbench.sessionclient.pack_context` then refuses
    by.
    """

    async def scenario() -> None:
        async with (
            real_server(
                tmp_path,
                max_compressed_bytes=4001,
                max_decompressed_bytes=4002,
                max_entries=4003,
                max_file_bytes=4004,
                max_path_depth=7,
            ) as harness,
            client_for(harness, tmp_path) as client,
        ):
            await client.capabilities()
            assert client.caps == sc.IngressCaps(
                compressed_bytes=4001,
                decompressed_bytes=4002,
                entries=4003,
                file_bytes=4004,
                path_depth=7,
                # Not configurable: the endpoint's own `max_msg_size`,
                # applied to the socket before any verb exists to refuse
                # anything, and announced for exactly that reason.
                frame_bytes=bs_protocol.MAX_FRAME_BYTES,
            )
            assert client.caps != sc.E44_CAPS

    run(scenario())


def test_a_frame_cap_sizes_the_upload(tmp_path: Path) -> None:
    """The announced frame bound is what chunks an archive.

    Lowered on the client after the announcement rather than in the
    server's configuration, because it is not configurable there — it is
    the endpoint's ``max_msg_size``. A peer with a smaller one is the
    case this covers, and the previous version of this test built a
    hand-rolled server to speak it; the real one announces its own now
    (E57), so what is left to prove is that the number does the
    chunking. The archive still arrives whole, which is the half a
    smaller frame could break.
    """
    context = tmp_path / "context"
    context.mkdir()

    async def scenario() -> None:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        make_context(context, sdk_sha256=sdk_sha256)
        async with real_server(tmp_path) as harness, client_for(harness, tmp_path) as client:
            await client.capabilities()
            assert client.caps.frame_bytes == bs_protocol.MAX_FRAME_BYTES
            client.caps = replace(client.caps, frame_bytes=256)
            await client.open_session()
            packed = await client.send_context(context)
            await client.close_session()
        chunks = [len(data) for kind, data in client.sent if kind == "binary"]
        assert chunks, "nothing was uploaded"
        assert max(chunks) <= 256
        assert len(chunks) > 1, "the archive was chunked, not sent whole"
        # And the server put the whole of it back together: it answered
        # the pins out of the context.yaml inside the archive.
        assert packed["container"]["contract"] == 1

    run(scenario())


# --------------------------------------------------------------------------
# Events, replay and the terminal states
# --------------------------------------------------------------------------


async def _await_file(path: Path, *, timeout: float = 30.0) -> None:
    """Wait until *path* exists, the way a test waits for another task."""
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} never appeared"
        await asyncio.sleep(0.01)


def test_a_reconnect_replays_every_event_exactly_once(tmp_path: Path) -> None:
    """E46: the events file is the replay buffer, and there is no other.

    The connection is dropped **while the invocation is still running**
    and a second client attaches to the same session from the last
    ``seq`` the first one saw. That ordering is the whole test: a drop
    after the verdict leaves ``from_seq`` one past the maximum, the
    server replays nothing, and the two assertions below would hold over
    an empty second half — which is how a reconnect test can pass
    without ever observing a reconnect.

    What the two connections saw together must be the invocation's own
    stream: no event lost, and none delivered twice.
    """

    async def scenario() -> tuple[list[int], list[int], int]:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        context.mkdir()
        make_context(context, sdk_sha256=sdk_sha256)
        gate = tmp_path / "let-it-finish"
        first: list[int] = []
        second: list[int] = []
        docker = FakeDocker(run_program=gated_program(gate))
        async with real_server(tmp_path, docker=docker) as harness:
            client = client_for(
                harness,
                tmp_path,
                on_event=lambda name, payload: _collect(first, payload),
            )
            await client.connect()
            await client.capabilities()
            await client.open_session()
            await client.send_context(context)
            await client.lock_context()
            invocation_id = await client.build()
            # The program has announced itself and is now waiting; the
            # socket goes away in the middle of the invocation.
            while len(first) < 2:
                await asyncio.sleep(0.01)
            session_id = client.session_id
            seen = dict(client._last_seq)
            await client.close()

            # It finishes with nobody attached, so its remaining events
            # are history by the time the second connection asks.
            gate.write_text("go", encoding="utf-8")
            await _await_file(gate.with_suffix(".done"))

            resumed = client_for(
                harness,
                tmp_path,
                on_event=lambda name, payload: _collect(second, payload),
            )
            resumed.session_id = session_id
            resumed._last_seq = dict(seen)
            await resumed.connect()
            answer = await resumed.attach_session(invocation_id=invocation_id)
            await resumed.close_session()
            await resumed.close()
        return first, second, answer["replayed"]

    first, second, replayed = run(scenario())
    assert first == sorted(first), "the live stream arrived in order"
    assert first, "the first connection saw the invocation"
    assert second, "the second connection saw the replay"
    assert replayed == len(second), "the server replayed exactly what the caller received"
    assert set(first) & set(second) == set(), "nothing was delivered twice"
    # The program emits seq 1..6 (contract §8 seeds them); together the
    # two connections saw exactly that, with no hole.
    assert sorted(first + second) == list(range(1, max(first + second) + 1))


def test_a_dropped_socket_does_not_cost_the_verdict(tmp_path: Path) -> None:
    """The reconnect this module advertises, on the client that owns the build.

    ``_read_loop`` tells a caller whose socket died to "reconnect and
    attach-session — a running invocation survives a lost socket". So it
    has to: the same client object reconnects, re-attaches, and learns
    the verdict of the invocation it started. The verdict is the only
    carrier of the status, the artifacts and the context ID, it is
    published exactly once and it is not in the replay file, so a client
    that discarded it would have no second way to get it.
    """

    async def scenario() -> dict[str, Any]:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        context.mkdir()
        make_context(context, sdk_sha256=sdk_sha256)
        gate = tmp_path / "let-it-finish"
        seen: list[int] = []
        docker = FakeDocker(run_program=gated_program(gate))
        async with real_server(tmp_path, docker=docker) as harness:
            client = client_for(
                harness, tmp_path, on_event=lambda name, payload: _collect(seen, payload)
            )
            await client.connect()
            await client.capabilities()
            await client.open_session()
            await client.send_context(context)
            await client.lock_context()
            invocation_id = await client.build()
            while not seen:
                await asyncio.sleep(0.01)

            # The socket dies under a running invocation. A caller
            # already blocked on the verdict is told so — that is the
            # point of failing it — and the hint it gets is the recipe
            # for the rest of this test.
            waiting = asyncio.create_task(client.wait_finished(invocation_id, timeout=30))
            await asyncio.sleep(0)
            await client.close()
            with pytest.raises(sc.RemoteTransportError, match="closed"):
                await waiting

            await client.connect()
            await client.attach_session(invocation_id=invocation_id)
            gate.write_text("go", encoding="utf-8")
            verdict = await client.wait_finished(invocation_id, timeout=30)
            await client.get_artifact(
                invocation_id,
                into=tmp_path / "out",
                expected={entry["path"]: entry["sha256"] for entry in verdict["artifacts"]},
            )
            await client.close_session()
            await client.close()
            return verdict

    verdict = run(scenario())
    assert verdict["status"] == "success"
    assert {entry["path"] for entry in verdict["artifacts"]} == {
        "firmware.hex",
        "firmware.bin",
        "build-report.json",
    }
    assert (tmp_path / "out" / "firmware.bin").is_file()


def test_a_sink_that_raises_is_the_callers_problem_and_not_the_sockets(tmp_path: Path) -> None:
    """A callback is a consumer, never the transport.

    ``on_line`` and ``on_event`` run on the reader task, so an exception
    from one of them used to reach the reader's "the socket died under
    us" handler: every pending call failed with "the connection to the
    build server failed", the reader ended, and a perfectly healthy
    socket was left with nobody reading it. A ``BrokenPipeError`` from a
    sink writing to a closed stdout is the ordinary way that happens.

    The build survives it, the verdict still arrives, and the session
    goes on being usable — including ``get-artifact``, which needs the
    reader that would otherwise be gone.
    """
    seen: list[str] = []

    def angry_line(line: str) -> None:
        seen.append(line)
        raise BrokenPipeError("the consumer of this log went away")

    def angry_event(name: str, payload: dict) -> None:
        raise RuntimeError("this dashboard's socket is closed")

    async def scenario() -> sc.ArtifactDelivery:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        context.mkdir()
        make_context(context, sdk_sha256=sdk_sha256)
        async with (
            real_server(tmp_path) as harness,
            client_for(harness, tmp_path, on_line=angry_line, on_event=angry_event) as client,
        ):
            await client.capabilities()
            await client.open_session()
            await client.send_context(context)
            await client.lock_context()
            invocation_id = await client.build()
            verdict = await client.wait_finished(invocation_id, timeout=30)
            assert verdict["status"] == "success"
            delivery = await client.get_artifact(invocation_id, into=tmp_path / "out")
            await client.close_session()
            return delivery

    delivery = run(scenario())
    assert seen, "the sink really was called"
    assert "firmware.bin" in delivery.files


def test_a_frame_this_client_cannot_read_is_not_a_dead_socket() -> None:
    """A peer's malformed frame is a peer's problem, not the connection's.

    ``payload`` is read as a mapping in both dispatch paths, so a peer
    that sends a list there would raise an ``AttributeError`` on the
    reader task — which the "the socket died under us" handler would then
    report as a connection failure. The real server always builds dict
    payloads; a client that talks to an untrusted peer does not get to
    rely on that.
    """
    lines: list[str] = []
    events: list[tuple[str, dict]] = []
    client = sc.SessionClient(
        "ws://example.invalid/ws",
        on_line=lines.append,
        on_event=lambda name, payload: events.append((name, payload)),
    )
    client._dispatch({"type": "log", "payload": ["not", "a", "mapping"]})
    client._dispatch({"type": "event", "event": "x-vendor.phase", "payload": None})
    client._dispatch({"type": "result", "payload": {}})
    assert lines == [""]
    assert events == [("x-vendor.phase", {})]


def test_a_dead_reader_over_a_live_socket_is_repaired(tmp_path: Path) -> None:
    """Nothing but the reader takes frames off the socket.

    So a client whose reader ended while the socket stayed open is a
    client whose every command waits out its full timeout with nobody
    answering — and ``connect()`` used to return early on the open socket
    without ever looking at the reader. It repairs it instead.
    """

    async def scenario() -> None:
        async with real_server(tmp_path) as harness, client_for(harness, tmp_path) as client:
            await client.capabilities()
            reader = client._reader
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
            assert not client._ws.closed, "the socket is fine; only the reader is gone"

            await client.connect()
            assert client._reader is not reader
            answer = await client.capabilities()
            assert answer.protocol_version == bs_sessions.SESSION_PROTOCOL_VERSION

    run(scenario())


def test_a_spool_that_cannot_be_written_is_not_a_dead_connection(tmp_path: Path) -> None:
    """A full disk under the artifact spool is news about this machine.

    The write happens on the reader task, so an ``OSError`` from it would
    otherwise be caught by "the socket died under us": every pending call
    would fail with a connection error and the reader would end, over a
    socket that never dropped. Only the download fails.
    """

    class FullDisk:
        closed = False

        def write(self, data: bytes) -> int:
            raise OSError(28, "No space left on device")

        def close(self) -> None:
            self.closed = True

    async def scenario() -> None:
        done: asyncio.Future = asyncio.get_running_loop().create_future()
        download = sc._Download(spool=tmp_path / "artifact.tar.zst", frame_id="c-1", done=done)
        download.announce({"archive": {"size": 4096, "sha256": "0" * 64}})
        download._handle = FullDisk()
        download.feed(b"the first chunk")
        assert done.done()
        with pytest.raises(sc.RemoteTransportError, match="could not be written"):
            done.result()

    run(scenario())


def _collect(sink: list[int], payload: dict) -> None:
    seq = payload.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        sink.append(seq)


def test_a_replay_from_the_beginning_is_not_delivered_twice(tmp_path: Path) -> None:
    """The dedupe, asked the hostile way.

    A client that attaches with ``from_seq: 1`` on a connection that
    already saw the whole stream gets every event again from the server —
    that is the verb doing what it was told. The caller must still see
    each event once, and that is this client's own bookkeeping.
    """

    async def scenario() -> list[int]:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        context.mkdir()
        make_context(context, sdk_sha256=sdk_sha256)
        seen: list[int] = []
        async with (
            real_server(tmp_path) as harness,
            client_for(
                harness, tmp_path, on_event=lambda name, payload: _collect(seen, payload)
            ) as client,
        ):
            await client.capabilities()
            await client.open_session()
            await client.send_context(context)
            await client.lock_context()
            invocation_id = await client.build()
            await client.wait_finished(invocation_id, timeout=30)
            answer = await client.attach_session(invocation_id=invocation_id, from_seq=1)
            assert answer["replayed"] > 0, "the server did replay them"
            await client.close_session()
        return seen

    seen = run(scenario())
    assert seen == sorted(set(seen)), "the caller saw each event once"


def test_a_poisoned_session_is_terminal_and_still_gives_up_its_artifacts(
    tmp_path: Path,
) -> None:
    """E39: ``session.poisoned`` refuses work and keeps the diagnosis.

    The session stays open on purpose — the moment a session poisons is
    the moment its owner most wants the logs — so the client marks it
    terminal for *work* while ``get-artifact`` and ``close-session`` go
    on working.
    """

    async def scenario() -> None:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        context.mkdir()
        make_context(context, sdk_sha256=sdk_sha256)
        async with real_server(tmp_path) as harness, client_for(harness, tmp_path) as client:
            await client.capabilities()
            await client.open_session()
            await client.send_context(context)
            await client.lock_context()
            invocation_id = await client.build()
            await client.wait_finished(invocation_id, timeout=30)
            harness.state.sessions.require(client.session_id).poison()

            with pytest.raises(sc.SessionPoisoned) as refusal:
                await client.build()
            assert client.terminal == "session.poisoned"
            assert refusal.value.retryable is False
            # Terminal for work, and only for work.
            delivery = await client.get_artifact(invocation_id, into=tmp_path / "salvage")
            assert "firmware.bin" in delivery.files
            await client.close_session()

    run(scenario())


def test_a_verdict_that_poisons_the_session_is_terminal_at_once(tmp_path: Path) -> None:
    """The *ordinary* way a session poisons is asynchronous (E39, §6.2).

    A refused command is the loud way, and the client marks the session
    terminal for it. But an interrupted patch application ends the
    invocation instead: the server poisons the session while assembling
    the verdict, and the code arrives in the verdict's error envelope.
    Reading it there is what keeps ``terminal`` true of the session the
    moment the client learns it, instead of one refused round trip later.
    """

    async def scenario() -> None:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        context.mkdir()
        make_context(context, sdk_sha256=sdk_sha256)
        docker = FakeDocker(run_program=poisoning_program)
        async with (
            real_server(tmp_path, docker=docker) as harness,
            client_for(harness, tmp_path) as client,
        ):
            await client.capabilities()
            await client.open_session()
            await client.send_context(context)
            await client.lock_context()
            invocation_id = await client.build()
            verdict = await client.wait_finished(invocation_id, timeout=30)
            assert verdict["status"] != "success"
            assert verdict["error"]["code"] == "session.poisoned"
            assert client.terminal == "session.poisoned"
            # Refused here, without a frame: the server would refuse it
            # too, and learning that costs a round trip.
            before = len(client.sent)
            with pytest.raises(sc.RemoteError, match="terminal"):
                await client.build()
            assert len(client.sent) == before
            assert harness.state.sessions.require(client.session_id).poisoned
            await client.close_session()

    run(scenario())


def test_cancel_is_acknowledged_immediately_and_the_session_survives(tmp_path: Path) -> None:
    """E38: the answer means "the stop signal is set", never "it stopped".

    The program here never finishes on its own, which is the case the
    verb exists for: killing a ``docker exec`` client does not stop the
    process inside the container, so cancellation has to be something a
    client *says*. The acknowledgement comes back while the invocation is
    still running, and the actual end arrives on the event stream.
    """

    async def scenario() -> None:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        context.mkdir()
        make_context(context, sdk_sha256=sdk_sha256)
        docker = FakeDocker(run_program=hanging_program)
        async with (
            real_server(tmp_path, docker=docker, cancel_grace_seconds=0) as harness,
            client_for(harness, tmp_path) as client,
        ):
            await client.capabilities()
            await client.open_session()
            await client.send_context(context)
            await client.lock_context()
            invocation_id = await client.build()
            started = time.monotonic()
            answer = await client.cancel(invocation_id)
            elapsed = time.monotonic() - started
            assert answer["cancelled"] is True
            assert answer["already_finished"] is False
            assert elapsed < 5.0, "the acknowledgement waited for the invocation"
            verdict = await client.wait_finished(invocation_id, timeout=30)
            assert verdict["status"] != "success"
            # A cancel that races a natural completion is legitimate.
            again = await client.cancel(invocation_id)
            assert again["already_finished"] is True
            with pytest.raises(sc.ServerRefusal) as unknown:
                await client.cancel("inv-999")
            assert unknown.value.code == "invocation.unknown"
            await client.close_session()

    run(scenario())


# --------------------------------------------------------------------------
# The archive itself
# --------------------------------------------------------------------------


def test_the_packer_is_deterministic_and_orders_its_members(tmp_path: Path) -> None:
    """Two packs of one context are byte-identical, and sorted.

    The same discipline ``scripts/build_sdk_archive.py`` states for the
    SDK package: sorted names, one fixed mtime, uid/gid 0, one mode, one
    zstd level, one thread. Nothing hashes the archive bytes into an
    identity — the context ID is over the *files* — but a pack that
    changed with the filesystem's directory order would make two
    identical uploads look different to everyone who compared them.
    """
    context = tmp_path / "context"
    context.mkdir()
    make_context(
        context,
        sdk_sha256="a" * 64,
        patches={
            "patches/zephyr/0002-b.patch": b"b\n",
            "patches/zephyr/0001-a.patch": b"a\n",
        },
    )
    first = sc.pack_context(context, spool=tmp_path / "one.tar.zst")
    second = sc.pack_context(context, spool=tmp_path / "two.tar.zst")
    assert first.sha256 == second.sha256
    assert (tmp_path / "one.tar.zst").read_bytes() == (tmp_path / "two.tar.zst").read_bytes()

    plain = zstandard.ZstdDecompressor().decompress(
        (tmp_path / "one.tar.zst").read_bytes(), max_output_size=1 << 20
    )
    with tarfile.open(fileobj=io.BytesIO(plain)) as tar:
        members = tar.getmembers()
    assert [member.name for member in members] == sorted(member.name for member in members)
    assert all(member.uid == 0 and member.gid == 0 and member.mtime == 0 for member in members)
    assert all(member.mode == 0o644 for member in members)
    # `manifest.yaml` is never an extraction target and never packed;
    # `context.yaml` is packed but is outside the integrity list.
    assert "manifest.yaml" not in [member.name for member in members]
    assert "context.yaml" in [member.name for member in members]
    assert all(entry.path != "context.yaml" for entry in first.files)


def test_packing_a_context_does_not_block_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tar, a hash per member and zstd-19 are seconds of work with no ``await``.

    Run on the event loop they stall everything the loop owes anybody
    else — including this client's own pong, which aiohttp emits from
    inside the reader's ``async for``, so a server with ``heartbeat=30``
    can tear the connection down before the upload starts. And a process
    that embeds this client (E16/E21's whole reason for asyncio) freezes
    for the duration.

    The pack here is a stand-in that blocks for a fifth of a second; what
    is asserted is that another task kept being scheduled while it ran.
    """
    ticks = 0

    def slow_pack(*args: Any, **kwargs: Any):
        time.sleep(0.2)
        return real_pack(*args, **kwargs)

    real_pack = sc.pack_context
    monkeypatch.setattr(sc, "pack_context", slow_pack)

    async def scenario() -> None:
        nonlocal ticks
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        context.mkdir()
        make_context(context, sdk_sha256=sdk_sha256)

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        async with real_server(tmp_path) as harness, client_for(harness, tmp_path) as client:
            await client.capabilities()
            await client.open_session()
            beat = asyncio.create_task(ticker())
            try:
                await client.send_context(context)
            finally:
                beat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await beat
            await client.close_session()

    run(scenario())
    assert ticks >= 5, f"the loop was blocked for the whole pack ({ticks} turns)"


def test_the_inbound_frame_size_is_bounded(tmp_path: Path) -> None:
    """``max_msg_size=0`` disables the only limit that fires before buffering.

    aiohttp rejects an oversized frame in the reader, *before* the
    payload is assembled — which is the same mechanism the build server's
    own endpoint relies on and documents. Passing 0 removes a limit
    aiohttp would otherwise have applied, on a socket whose peer this
    module elsewhere calls the least trusted component in the system.
    """
    captured: dict[str, Any] = {}

    class FakeSocket:
        closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def close(self) -> None:
            self.closed = True

    class FakeSession:
        closed = False

        async def ws_connect(self, url: str, **kwargs: Any):
            captured.update(kwargs)
            return FakeSocket()

        async def close(self) -> None:
            self.closed = True

    async def scenario() -> None:
        client = sc.SessionClient("ws://example.invalid/ws", spool_dir=tmp_path / "spool")
        client._session = FakeSession()
        await client.connect()
        await client.close()

    run(scenario())
    assert captured["max_msg_size"] == sc.MAX_INBOUND_FRAME_BYTES
    assert sc.MAX_INBOUND_FRAME_BYTES == 8 * 1024 * 1024, "the number the server accepts"


def test_a_download_is_bounded_in_every_direction(tmp_path: Path) -> None:
    """Nothing announces an egress cap, so this client applies its own.

    The sibling in ``mcuhome.compiler.localbackend`` bounds exactly these
    two functions — a ``limit`` on the decompression and a
    ``quota_bytes`` across the extraction — and for exactly this reason:
    a few kilobytes of zstd expand to gigabytes, the archive hash is
    computed over what the server itself announced and therefore proves
    nothing about size, and the spool is on the developer's disk.
    """
    bomb = tmp_path / "bomb.tar.zst"
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        payload = b"\x00" * (1 << 20)
        info = tarfile.TarInfo("firmware.bin")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    bomb.write_bytes(zstandard.ZstdCompressor().compress(raw.getvalue()))
    assert bomb.stat().st_size < 4096, "a small archive that is not a small file"

    plain = tmp_path / "bomb.tar"
    with pytest.raises(sc.RemoteTransportError, match="unpacks to more than"):
        sc._decompress(bomb, plain, limit=4096)
    # Refused mid-stream: the bytes past the limit are never written.
    assert plain.stat().st_size <= 4096

    sc._decompress(bomb, plain, limit=1 << 24)
    with pytest.raises(sc.RemoteTransportError, match="unpacks to more than"):
        sc._safe_extract(plain, into=tmp_path / "out", quota_bytes=4096)

    # And the announcement itself: a delivery too large to accept is
    # refused before its first byte reaches the spool.
    async def announcement() -> None:
        done: asyncio.Future = asyncio.get_running_loop().create_future()
        download = sc._Download(spool=tmp_path / "never.tar.zst", frame_id="c-1", done=done)
        download.announce({"archive": {"size": sc.MAX_ARTIFACT_ARCHIVE_BYTES + 1, "sha256": "x"}})
        assert done.done()
        with pytest.raises(sc.RemoteTransportError, match="announced an artifact archive"):
            done.result()

    run(announcement())
    assert not (tmp_path / "never.tar.zst").exists(), "no spool was opened"


def test_a_nested_artifact_path_is_delivered_not_refused(tmp_path: Path) -> None:
    """``zephyr/zephyr.hex`` is a legal declared path, and a real one.

    The build server packs one tar member per declared artifact and tar
    synthesizes no parent-directory members, so the client meets
    ``zephyr/zephyr.hex`` with no ``zephyr/`` before it. Containment that
    answered "not contained" for a missing intermediate segment would
    report an honest server — precisely the third-party build container
    the contract exists for — as an escape attempt.
    """

    async def scenario() -> sc.ArtifactDelivery:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        context.mkdir()
        make_context(context, sdk_sha256=sdk_sha256)
        nested = (
            ("zephyr/zephyr.hex", b":020000040000FA\n"),
            ("mcuboot/zephyr.signed.bin", b"\x00\x01\x02\x03"),
            ("build-report.json", json.dumps(BUILD_REPORT).encode()),
        )

        def program(action: str, request: dict[str, Any], on_line) -> FakeProcess:
            identity = _start_program(action, request, on_line)
            _finish_program(action, request, on_line, identity=identity, artifacts=nested)
            return FakeProcess(0)

        docker = FakeDocker(run_program=program)
        async with (
            real_server(tmp_path, docker=docker) as harness,
            client_for(harness, tmp_path) as client,
        ):
            await client.capabilities()
            await client.open_session()
            await client.send_context(context)
            await client.lock_context()
            invocation_id = await client.build()
            verdict = await client.wait_finished(invocation_id, timeout=30)
            delivery = await client.get_artifact(
                invocation_id,
                into=tmp_path / "out",
                expected={entry["path"]: entry["sha256"] for entry in verdict["artifacts"]},
            )
            await client.close_session()
            return delivery

    delivery = run(scenario())
    assert set(delivery.files) == {
        "zephyr/zephyr.hex",
        "mcuboot/zephyr.signed.bin",
        "build-report.json",
    }
    assert (tmp_path / "out" / "zephyr" / "zephyr.hex").is_file()
    assert (tmp_path / "out" / "mcuboot" / "zephyr.signed.bin").read_bytes() == b"\x00\x01\x02\x03"


def test_two_sequences_on_one_client_do_not_interleave(tmp_path: Path) -> None:
    """A BINARY frame carries no id, so its only correlation is order.

    The build server holds that discipline with a per-connection lock and
    says why; the client owes the mirror of it. Two ``get-artifact``
    calls gathered on one client used to overwrite each other's download
    slot: the first resolved its future with the announcement frame and
    then hashed a spool nobody had created, while its bytes were fed to
    the second download and dropped.
    """

    async def scenario() -> tuple[sc.ArtifactDelivery, sc.ArtifactDelivery]:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        context.mkdir()
        make_context(context, sdk_sha256=sdk_sha256)
        async with real_server(tmp_path) as harness, client_for(harness, tmp_path) as client:
            await client.capabilities()
            await client.open_session()
            await client.send_context(context)
            await client.lock_context()
            invocation_id = await client.build()
            await client.wait_finished(invocation_id, timeout=30)
            first, second = await asyncio.gather(
                client.get_artifact(invocation_id, into=tmp_path / "a"),
                client.get_artifact(invocation_id, into=tmp_path / "b"),
            )
            await client.close_session()
            return first, second

    first, second = run(scenario())
    assert (
        set(first.files)
        == set(second.files)
        == {
            "firmware.hex",
            "firmware.bin",
            "build-report.json",
        }
    )
    assert first.sha256 == second.sha256, "both downloads got the whole archive"


def test_extraction_refuses_a_member_that_leaves_the_directory(tmp_path: Path) -> None:
    """The containment discipline, on the one input that is hostile.

    ``out`` is written by the least trusted component in the system and
    travels over the network onto other people's machines, so the client
    checks every member the way the build server checks it at egress and
    the way ``mcuhome.compiler.localbackend`` checks it locally: no
    ``..``, no absolute path, no link, segment-by-segment containment.
    """
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo("../escaped.bin")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"bad"))
    plain = tmp_path / "hostile.tar"
    plain.write_bytes(raw.getvalue())
    with pytest.raises(sc.RemoteTransportError, match="usable path"):
        sc._safe_extract(plain, into=tmp_path / "out", quota_bytes=1 << 20)
    assert not (tmp_path / "escaped.bin").exists()

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        link = tarfile.TarInfo("firmware.bin")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/shadow"
        tar.addfile(link)
    plain = tmp_path / "link.tar"
    plain.write_bytes(raw.getvalue())
    with pytest.raises(sc.RemoteTransportError, match="not a regular file"):
        sc._safe_extract(plain, into=tmp_path / "out2", quota_bytes=1 << 20)

    # A symlinked *segment* of an otherwise contained path is the other
    # way out, and the one creating the parents must not open.
    (tmp_path / "elsewhere").mkdir()
    into = tmp_path / "out3"
    into.mkdir()
    (into / "zephyr").symlink_to(tmp_path / "elsewhere")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo("zephyr/zephyr.hex")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"bad"))
    plain = tmp_path / "linked-parent.tar"
    plain.write_bytes(raw.getvalue())
    with pytest.raises(sc.RemoteTransportError, match="does not stay inside"):
        sc._safe_extract(plain, into=into, quota_bytes=1 << 20)
    assert not (tmp_path / "elsewhere" / "zephyr.hex").exists()


def test_a_delivered_artifact_that_does_not_hash_is_refused(tmp_path: Path) -> None:
    """E45's per-file half: the archive hash is a transport check only.

    The hashes that say whether a firmware image is the one that was
    built are the ones the client already holds from the verdict, and
    checking them against the delivered bytes is what keeps the announced
    archive hash from becoming a second integrity claim that could
    disagree with the first.
    """
    root = tmp_path / "out"
    root.mkdir()
    (root / "firmware.bin").write_bytes(b"\x00\x01")
    declared = Artifact(root="out", path="firmware.bin", role="firmware", sha256="0" * 64)
    with pytest.raises(sc.RemoteTransportError, match="does not hash"):
        sc._check_members(root, (declared,), None)

    good = hashlib.sha256(b"\x00\x01").hexdigest()
    sc._check_members(root, (dataclasses.replace(declared, sha256=good),), None)
    # The caller's own list wins where it has one.
    with pytest.raises(sc.RemoteTransportError, match="does not hash"):
        sc._check_members(
            root, (dataclasses.replace(declared, sha256=good),), {"firmware.bin": "1" * 64}
        )


# --------------------------------------------------------------------------
# The composition slice 4 dispatches on
# --------------------------------------------------------------------------


def _remote_build(tmp_path: Path, **kwargs: Any) -> sc.RemoteBuildResult:
    """One ``run_remote_build`` against the real server, from a sync test."""

    async def scenario() -> sc.RemoteBuildResult:
        sdk_sha256 = write_sdk_package(tmp_path / "packages")
        context = tmp_path / "context"
        if not context.exists():
            context.mkdir()
            make_context(context, sdk_sha256=sdk_sha256)
        async with real_server(tmp_path) as harness:
            return await sc.run_remote_build(
                context,
                url=harness.url,
                token=TOKEN,
                work_root=tmp_path / "work",
                timeout=30,
                **kwargs,
            )

    return run(scenario())


def test_run_remote_build_mirrors_the_local_backend_shape(tmp_path: Path) -> None:
    """Context in, unsigned artifacts out — the same answer shape as ``local``.

    "Same fields, same meanings" is a claim about
    :class:`~mcuhome.compiler.localbackend.LocalOutcome`, so it is
    asserted *against* it rather than against this dataclass's own
    values: the shared fields are named, the two sets of fields that are
    deliberately not shared are named too — so a new divergence fails
    here rather than in whatever dispatches over the three methods — and
    one artifact is read through the **same expression** on both
    results, which is what "one answer rather than three" actually means.

    And the build report comes back beside the unsigned image, because
    the host signer is the next step for every method alike (E55, E56).
    """
    lines: list[str] = []
    result = _remote_build(tmp_path, on_line=lines.append)
    assert result.successful is True
    assert result.action == "build"
    assert result.context_id.startswith("sha256:")
    assert result.out == tmp_path / "work" / "out"
    assert (result.out / "build-report.json").is_file()
    assert result.error is None

    remote_fields = {field.name for field in dataclasses.fields(sc.RemoteBuildResult)}
    local_fields = {field.name for field in dataclasses.fields(LocalOutcome)}
    assert {"action", "context_id", "status", "successful", "artifacts", "out"} <= (
        remote_fields & local_fields
    )
    assert remote_fields - local_fields == {"error", "invocation_id"}, (
        "a field this method has and `local` does not — name it here or drop it"
    )
    assert local_fields - remote_fields == {"exit_code", "result", "problems", "violation"}, (
        "a field `local` has and this method does not — a client cannot invent what it "
        "did not observe, but the divergence has to be a decision"
    )

    # The same expression over both answers, which is the property the
    # docstring claims and a `dict` on one side would have broken.
    local = LocalOutcome(
        action="build",
        context_id=result.context_id,
        exit_code=0,
        artifacts=tuple(
            Artifact(root="out", path=name, role="firmware", sha256="0" * 64)
            for name, _ in ARTIFACTS
        ),
    )
    for outcome in (result, local):
        assert {entry.path for entry in outcome.artifacts} == {name for name, _ in ARTIFACTS}
    assert all(isinstance(entry, Artifact) for entry in result.artifacts)


def test_run_remote_build_can_verify_as_well_as_build(tmp_path: Path) -> None:
    """``LocalBackend.run`` takes an action, and so does this.

    The server implements both working verbs and the client exposes both,
    so a composition that hardcoded ``build`` made the remote method the
    only one of the three that cannot check a context — and reported
    ``action="build"`` for whatever it did.
    """
    result = _remote_build(tmp_path, action="verify")
    assert result.action == "verify"
    assert result.successful is True


def test_run_remote_build_refuses_an_action_no_server_performs(tmp_path: Path) -> None:
    """``build`` and ``verify`` are the working verbs, and there is no third."""

    async def scenario() -> None:
        with pytest.raises(sc.RemoteError, match="build and verify"):
            await sc.run_remote_build(
                tmp_path / "context",
                url="ws://example.invalid/ws",
                work_root=tmp_path / "work",
                action="describe",
            )

    run(scenario())


def test_run_remote_build_empties_the_delivery_directory_first(tmp_path: Path) -> None:
    """``work_root`` is reused, so ``out`` cannot be allowed to accumulate.

    ``LocalBackend.run`` wipes the invocation directory before it fills
    it, and states the hazard: "an old ``out/firmware.hex`` that still
    matched a re-declared hash would let a later non-conforming build
    slip through egress". Remotely the same directory is both the
    delivery target and what :attr:`RemoteBuildResult.out` points a host
    signer at — and the signer resolves ``firmware.bin`` beside the build
    report by *scanning the directory*, not by consulting the declared
    list, so a leftover is what it signs.
    """
    out = tmp_path / "work" / "out"
    out.mkdir(parents=True)
    (out / "firmware.bin").write_bytes(b"an image from an older build")
    (out / "stale-extra.bin").write_bytes(b"never declared by anybody")

    result = _remote_build(tmp_path)
    assert result.successful is True
    assert not (out / "stale-extra.bin").exists(), "a leftover survived into a new delivery"
    assert (out / "firmware.bin").read_bytes() == b"\x00\x01\x02\x03"
    assert sorted(path.name for path in out.iterdir()) == [
        "build-report.json",
        "firmware.bin",
        "firmware.hex",
    ]


# --------------------------------------------------------------------------
# The `remote` build method, from a device model (E65)
# --------------------------------------------------------------------------
#
# Everything above drives the session client directly, from a context
# somebody already wrote. These drive `run_build(method="remote")` — the
# supported entry point — from a resolved device model and nothing else,
# which is the gap E65 closed: the SDK pin is resolved on this side, from
# this side's source directories, and the context is created here.
#
# The peer stays the real build server. That matters more here than
# anywhere else in this file, because the property under test is a
# *cross-party* one: the version this client writes into `context.yaml`
# is what the server resolves the package by, and the sha256 it writes is
# what the server checks the bytes it found against.


def _model():
    """The reference device, stages 1-3 run — the ``remote`` method's input."""
    return resolve_file(EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml")


def _public_pem() -> str:
    """The public half of the one known key, which is all a build may see."""
    return signing.public_key_pem(signing.generate_key_pem(KEY_SCALAR))


def _remote_request(tmp_path: Path, sources: Path, harness: Harness, **overrides: Any):
    return buildmethods.BuildRequest(
        model=_model(),
        out_dir=tmp_path / "build",
        signing_pub=_public_pem(),
        sdk_sources=(sources,),
        server=harness.url,
        token=TOKEN,
        **overrides,
    )


def test_the_remote_method_builds_from_a_model_against_the_real_server(tmp_path: Path) -> None:
    """Model in, unsigned image out — no context written by the caller.

    The whole of E65 in one path: the SDK pin is resolved from a local
    source directory, a base context is created from the model and the
    **public** signing key, a real session sends it, the server freezes
    it and answers a context ID this client agreed with, and the
    artifacts come back. The device model never leaves the client as
    anything but context content, and no step of it asked the server what
    it had.

    The last assertion is the E56 seam: what a build delivers is an
    unsigned image plus a build report the one host-side signer reads —
    the same report name, in the same relationship to the same directory,
    as the ``local`` method's delivery.
    """
    sources = tmp_path / "packages"
    write_sdk_package(sources)
    lines: list[str] = []

    async def scenario() -> buildmethods.BuildOutcome:
        async with real_server(tmp_path) as harness:
            return await buildmethods.run_build(
                _remote_request(tmp_path, sources, harness, on_line=lines.append),
                method=buildmethods.REMOTE,
            )

    outcome = run(scenario())
    assert outcome.method == buildmethods.REMOTE
    assert outcome.successful is True and outcome.status == "success"
    assert outcome.context_id.startswith("sha256:")
    assert {entry.path for entry in outcome.artifacts} == {name for name, _ in ARTIFACTS}
    assert lines and any("build finished" in line for line in lines)

    # The delivery, where the shared signing step looks for it.
    work_root = tmp_path / "build" / ".mcuhome-remote"
    assert outcome.out_dir == work_root / "out"
    assert sorted(path.name for path in outcome.out_dir.iterdir()) == [
        "build-report.json",
        "firmware.bin",
        "firmware.hex",
    ]
    assert outcome.report == BUILD_REPORT_FILE
    report = imgtool.read_build_report(outcome.out_dir / outcome.report)
    assert report["signing"]["signature_type"] == "ecdsa-p256"
    # Unsigned, as every method delivers (E55, E56): nothing signed came
    # back, and the signature is the host step after this.
    assert not [path for path in outcome.out_dir.iterdir() if "sign" in path.name]

    # And what the method created on the way: a base context, carrying the
    # public key and no manifest — freezing it is the server's act (E7).
    context = work_root / "context"
    assert (context / "keys" / "signing.pub").read_text(encoding="utf-8") == _public_pem()
    assert (context / "model" / "device-model.json").is_file()
    assert not (context / "manifest.yaml").exists()


def test_the_context_the_remote_method_creates_pins_what_the_resolver_answered(
    tmp_path: Path,
) -> None:
    """The pin in ``context.yaml`` is exactly what the resolver answered.

    Asserted against the resolver rather than against the fixture's own
    hash, because the claim is that one rule produced both: the same
    function the ``local`` method resolves its pin with wrote this one,
    from the same ``--sdk-source`` directories, so a build server and a
    build container are asked for the same package by the same name and
    the same bytes.

    The two never-hashed fields are checked as well, against the
    resolution: ``constraint`` is the verbatim intent (here empty —
    nothing was stated) and ``url`` is empty for a local source, because
    a ``file://`` hint would leak this machine's filesystem layout into
    the uploaded document. Neither value is anybody's invention.
    """
    sources = tmp_path / "packages"
    real_sha256 = write_sdk_package(sources)

    async def scenario() -> None:
        async with real_server(tmp_path) as harness:
            await buildmethods.run_build(
                _remote_request(tmp_path, sources, harness), method=buildmethods.REMOTE
            )

    run(scenario())
    found = resolve_pins.resolve_sdk((sources,))
    written = read_context_request(
        tmp_path / "build" / ".mcuhome-remote" / "context" / "context.yaml"
    )
    assert (written.sdk.version, written.sdk.sha256) == (
        found.package.version,
        found.package.sha256,
    )
    assert written.sdk.sha256 == real_sha256
    assert (written.sdk.constraint, written.sdk.url) == (found.intent, found.url)
    # Both informational fields are honestly empty for a local source:
    # no constraint was stated, and a file:// hint would leak the local
    # filesystem layout into an uploaded document.
    assert (written.sdk.constraint, written.sdk.url) == ("", "")
    # And the three-value helper the `local` method reads answers the same
    # package, with the constraint as *stated*.
    assert resolve_pins.resolve_sdk_pin((sources,)) == (
        resolve_pins.SDK_ANY,
        found.package.version,
        real_sha256,
    )
    # The other half of the context's own statements, from the model.
    assert written.zephyr == ZEPHYR_LINE
    assert written.board == _model().device.board


def test_a_pin_the_servers_source_does_not_hold_is_refused_typed(tmp_path: Path) -> None:
    """E65's guarantee, end to end: the hash decides, not the version.

    The index this client resolves from declares a hash the archive next
    to it does not have — which is what a private or mirrored registry
    serving other bytes under the same version number looks like from
    here. The server finds a file named for that version, hashes it,
    disagrees, and refuses: ``sdk.unavailable``, not retryable, naming
    the version and the pin. That refusal is the whole reason no
    capabilities announcement is needed for the SDK — a build against the
    wrong SDK cannot happen quietly, so the client does not have to ask
    in advance what the server holds.

    The server's own coverage of that check is the server's; what is
    asserted here is that it *arrives*, typed, through the build method a
    user calls.
    """
    sources = tmp_path / "packages"
    write_sdk_package(sources, declared_sha256="ab" * 32)

    async def scenario() -> None:
        async with real_server(tmp_path) as harness:
            with pytest.raises(sc.ServerRefusal) as refusal:
                await buildmethods.run_build(
                    _remote_request(tmp_path, sources, harness), method=buildmethods.REMOTE
                )
        assert refusal.value.code == "sdk.unavailable"
        assert refusal.value.retryable is False
        rendered = str(refusal.value)
        assert SDK_VERSION in rendered or "ab" * 32 in rendered

    run(scenario())
