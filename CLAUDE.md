# ngn-agent

An autonomous coding agent powered by Claude. Polls a JIRA project for eligible work, validates tickets, implements code changes, and opens pull requests for approved tasks.

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
        ├── main.py        — entry point; continuous polling loop and top-level orchestration
        ├── jira.py        — JIRA API client; fetches tickets, posts comments, drives transitions
        ├── validator.py   — uses Claude to check whether a ticket has sufficient information
        ├── git.py         — clones repositories and checks for existing remote branches
        └── coder.py       — agentic implementation loop; reads/writes files and opens PRs
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
- `JIRA_BASE_URL` — JIRA Cloud base URL, e.g. `https://yourcompany.atlassian.net`
- `JIRA_EMAIL` — email address associated with the JIRA API token
- `JIRA_API_TOKEN` — JIRA Cloud API token
- `JIRA_FILTER_ID` — numeric ID of the saved JIRA filter to poll for candidate tickets
- `WORKSPACE_DIR` — directory where repositories are cloned (default: `workspaces/`)

## Style / conventions

- Keep changes minimal and focused — no over-engineering
- No docstrings or comments unless logic is non-obvious
- Prefer editing existing files over creating new ones
