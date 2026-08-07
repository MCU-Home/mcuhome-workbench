# CLAUDE.md

@AGENTS.md

## Claude Code specifics

- Project subagents live in `.claude/agents/` (`zephyr-code-reviewer`,
  `twister-runner`, `ncs-reference-miner`).
- Shared project settings and hooks: `.claude/settings.json`. Personal
  overrides go to `.claude/settings.local.json` (gitignored — never commit).
- A `PostToolUse` hook auto-formats edited `.c`/`.h` files with
  clang-format (`.claude/hooks/format-c.sh`); don't hand-format C code.
