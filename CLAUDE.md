# CLAUDE.md

@AGENTS.md

## Claude Code specifics

- Shared project settings: `.claude/settings.json`. Personal overrides go
  to `.claude/settings.local.json` (gitignored — never commit).
- No project subagents or hooks live here. The C/Zephyr-shaped ones
  (`zephyr-code-reviewer`, `twister-runner`, `ncs-reference-miner`, the
  clang-format `PostToolUse` hook) belong to the SDK repository,
  [mcu-home/mcuhome-sdk](https://github.com/mcu-home/mcuhome-sdk) — see
  its `.claude/`.
