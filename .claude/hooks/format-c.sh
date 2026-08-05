#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# Claude Code PostToolUse hook: auto-format C sources edited by the agent
# with clang-format (Zephyr style from the repo's .clang-format).
# Non-blocking: always exits 0.

set -u

file=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input", {}).get("file_path", ""))' 2>/dev/null || true)

case "$file" in
  *.c|*.h|*.cpp)
    if command -v clang-format >/dev/null 2>&1 && [ -f "$file" ]; then
      clang-format -i "$file" || true
    fi
    ;;
esac

exit 0
