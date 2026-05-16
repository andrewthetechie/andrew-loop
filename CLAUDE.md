## Development rules

### Python

- Always assume Python will be used via `uv`. Never use `pip` directly.
- Python 3.14.
- CLI framework: `click`. The newest version at time of writing is 8.3.3
- TUI/rich output: `rich`. The newest version at time of writing is 15.0.0
- All Python code must be async using `asyncio`.
- Linting and formatting: `ruff`. Code must pass all practical ruff checks (default rule set + isort + pyflakes + pycodestyle + bugbear).
- Testing: `pytest` with `pytest-asyncio` for async tests.
- Target as close to 100% test coverage as possible. Every module gets unit tests.
- Use sqlalchemy for any sqlite, postgres, or mysql interactions. Use Alembic for migrations. Sqlalchemy 2.0.49 is newest and alembic 1.18.4

### TypeScript

- Only used for opencode custom tools (`.opencode/tools/`). Keep these thin — shell out to `orch` CLI.
- Testing: `vitest`.
- Linting and formatting: `eslint` + `prettier`.
- Tests required for all tool files.

### Testing

- All code must have unit tests. Use pytest (Python) and vitest (TypeScript).
- Target near-100% test coverage. If a line exists, it should be tested.
- Tests live next to the code or in a parallel `tests/` directory — follow existing project convention.

### Pre-commit hooks

This project uses pre-commit hooks (`.pre-commit-config.yaml`). Hooks run on every commit:

- **Markdown**: format with prettier
- **TypeScript**: format with prettier, lint with eslint
- **Python**: format and lint with ruff (check + format)

Never skip hooks with `--no-verify`.

### General

- Prefer simple, readable code over clever code.
- No unnecessary abstractions for one-time operations.
- Keep dependencies minimal. Justify any new dependency.

## Agent skills

### Issue tracker

Issues are tracked as local markdown files under `.scratch/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default label vocabulary (needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout (one CONTEXT.md + docs/adr/ at the root). See `docs/agents/domain.md`.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **code-orchestra** (4281 symbols, 7861 relationships, 182 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/code-orchestra/context` | Codebase overview, check index freshness |
| `gitnexus://repo/code-orchestra/clusters` | All functional areas |
| `gitnexus://repo/code-orchestra/processes` | All execution flows |
| `gitnexus://repo/code-orchestra/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
