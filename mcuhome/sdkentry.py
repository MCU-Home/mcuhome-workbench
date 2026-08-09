# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The SDK package's entry point: the ``generate`` action of §6.1.

The build-container contract reaches code generation through "a defined
entry point in the SDK package" — an executable the *program* invokes as
a child process, over the very ABI it already speaks:

    <trees.sdk.path>/<generate.program> generate <request-document path>

"That is §5.1 unchanged — two positional operands, the request document
of §5.2, the result document of §5.4, the exit codes of §5.3." This
module is that executable's body; ``bin/generate`` at the SDK root is
the launcher, and ``mcuhome-sdk.json`` next to it is where §6.1 says the
program finds both (three fixed names, no fixed values). The whole
arrangement exists so that the *caller* is language-agnostic: "A build
container written in Rust, Go or shell starts a process and reads its
exit status like any other tool it drives" (E30), and the fact that code
generation is written in Python stays a property of this package.

``generate`` is an action of this entry point and **not** of the
program: §6.1 says it "is never invoked on ``/mcuhome/run`` and never
appears in ``program.actions``". The two fixed facts of the invocation
are implemented literally: "the entry point reads the build context from
``context`` and writes the per-device Zephyr application tree into
``out``". Everything else in the document "is between the SDK package
and itself" — and this package needs nothing else, which is why the
§5.2 fields a working action must be *sent* are accepted and unused
rather than demanded (§4.1: "The program MUST NOT require an entry it
does not need for the requested action").

The outer sequence — argv, parsing, the atomic result document, the
catch-all that turns a crash into a legible failure — is
:func:`mcuhome.abi.run_invocation`, shared rather than transcribed,
because reusing the invocation ABI is §6.1's whole argument for it.

The tree is written with :func:`mcuhome.generate.write_tree` under the
same ``config_name`` convention the command line uses
(``model.device.source``): the generator is deterministic (§1.4), so the
entry point and ``mcuhome build --generate-only`` produce byte-identical
trees for the same model, and nothing about a remote build is a second
code path.

This module reads no process state: argv arrives as an argument and
every path comes out of the request document — the invariant
``tests_py/test_userpaths.py`` enforces on every module here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcuhome.abi import run_invocation
from mcuhome.api import read_model
from mcuhome.errors import MCUHomeError
from mcuhome.generate import write_tree

__all__ = [
    "GENERATE_ACTION",
    "MODEL_FILE",
    "REQUEST_VERSIONS",
    "main",
]

#: The one action this entry point implements (§6.1).
GENERATE_ACTION = "generate"

#: Request format versions this entry point can parse. The same format
#: the program speaks, because §6.1 reuses it unchanged.
REQUEST_VERSIONS = (1,)

#: Where §3.1 puts the canonical device model inside a context.
MODEL_FILE = "model/device-model.json"

_STATUS_SUCCESS = "success"
_STATUS_FAILURE = "failure"
_STATUS_UNSUPPORTED = "unsupported"


def _document(
    echo: dict[str, Any],
    status: str,
    *,
    reason: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """A §5.4 result document, in its field order.

    The echo rule is the caller's (:func:`_generate` fills *echo* from
    what the request carried); ``error.retryable`` is false for
    everything here, because the same model generates the same tree —
    re-running an identical request produces the identical answer.
    """
    result: dict[str, Any] = {"result": 1, "status": status}
    result.update(echo)
    result["reason"] = reason
    result["error"] = (
        None if message is None else {"retryable": False, "message": message, "details": {}}
    )
    return result


def _generate(action: str, document: dict[str, Any]) -> dict[str, Any]:
    """One invocation of the entry point, past the shared outer sequence.

    Failures are ``failure`` with ``reason: "error.build.failed"`` — the
    caller maps any non-``success`` onto that reason anyway (§6.1: "A
    non-zero exit, a missing result document or a ``status`` other than
    ``success`` fails the invocation with ``reason:
    "error.build.failed"``"), so answering in the same vocabulary means
    the message a user finally reads is this one, with the actual cause
    in it, rather than a generic wrapper around a lost detail.
    """
    echo: dict[str, Any] = {"action": action}
    if "session" in document:
        echo["session"] = document["session"]

    version = document.get("request")
    if not isinstance(version, int) or isinstance(version, bool) or version not in REQUEST_VERSIONS:
        return _document(
            echo,
            _STATUS_UNSUPPORTED,
            reason="unsupported.request",
            message=(
                f"request format version {version!r} is not implemented; "
                f"this entry point parses {list(REQUEST_VERSIONS)}"
            ),
        )
    if action != GENERATE_ACTION:
        return _document(
            echo,
            _STATUS_UNSUPPORTED,
            reason="unsupported.action",
            message=f"action {action!r} is not implemented; this entry point implements "
            f"['{GENERATE_ACTION}']",
        )

    context = document.get("context")
    out = document.get("out")
    missing = [name for name, value in (("context", context), ("out", out)) if value is None]
    if missing:
        return _document(
            echo,
            _STATUS_UNSUPPORTED,
            reason="unsupported.request",
            message=f"the request document names no {' and no '.join(missing)}, and §6.1 fixes "
            f"both: the entry point reads the build context from `context` and writes "
            f"the application tree into `out`",
        )

    try:
        model = read_model(Path(str(context)) / MODEL_FILE)
        write_tree(model, out_dir=Path(str(out)), config_name=model.device.source)
    except (MCUHomeError, OSError) as failure:
        detail = getattr(failure, "message", None) or str(failure)
        return _document(
            echo,
            _STATUS_FAILURE,
            reason="error.build.failed",
            message=f"code generation failed: {detail}",
        )
    return _document(echo, _STATUS_SUCCESS)


def main(argv: list[str]) -> int:
    """One invocation of the SDK entry point; the return value is its exit code."""
    return run_invocation(argv, _generate)


if __name__ == "__main__":  # pragma: no cover - bin/generate's entry point
    import sys

    raise SystemExit(main(sys.argv))
