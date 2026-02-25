# ngn-agent

An autonomous coding agent powered by Claude. In its final form it will:
1. Monitor JIRA for eligible tasks
2. Clone target repositories from Git
3. Validate that a ticket has enough detail to act on
4. Implement code changes
5. Test the implementation
6. Commit to a branch
7. Open a pull request on GitHub

The project is being built incrementally — start simple, add capabilities over time.

## Project structure

```
ngn-agent/
├── CLAUDE.md
├── README.md
├── pyproject.toml
├── uv.lock
├── .venv/
└── src/
    └── ngn_agent/
        ├── __init__.py
        └── main.py
```

- `src/` layout (package is `ngn_agent`)
- Entry point: `ngn-agent` CLI command → `ngn_agent.main:main`

## Build system

- **Package manager / frontend:** `uv`
- **Build backend:** `hatchling`
- **Python:** >=3.11 (3.12.3 on dev machine)

### Common commands

```bash
uv sync                  # install / update dependencies
uv run ngn-agent         # run the entry point
uv run pytest            # run tests
uv run ruff check .      # lint
uv run ruff format .     # format
```

## Dependencies

- `anthropic>=0.40.0` — Claude API / Agent SDK (latest available: 0.83.0)

### Dev dependencies (dependency-groups)

- `pytest>=8.0`
- `ruff>=0.4`

**Note:** Use `[dependency-groups]` in `pyproject.toml` for dev deps, NOT `[tool.uv.dev-dependencies]` (deprecated).

## Environment

- `ANTHROPIC_API_KEY` must be set in the environment before running.

## Style / conventions

- Keep changes minimal and focused — no over-engineering
- No docstrings or comments unless logic is non-obvious
- Prefer editing existing files over creating new ones
