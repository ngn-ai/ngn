# ngn-agent

## WARNING: This is an experimental project and should not be used in production ##

An autonomous coding agent powered by Claude. Polls a JIRA project for eligible work, validates tickets, implements code changes, and opens pull requests. On subsequent runs, if a pull request already exists for a ticket, the agent reads reviewer comments and pushes updated code to address the feedback — no human intervention required between review cycles.

## Setup

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- An Anthropic API key
- A JIRA Cloud instance with API access

### Install

```bash
uv sync
. .venv/bin/activate
```

### Environment variables

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `JIRA_BASE_URL` | JIRA Cloud base URL, e.g. `https://yourcompany.atlassian.net` |
| `JIRA_EMAIL` | Email address associated with the API token |
| `JIRA_API_TOKEN` | JIRA Cloud API token (generate at id.atlassian.com → Security → API tokens) |
| `JIRA_FILTER_ID` | Numeric ID of the saved JIRA filter to poll for candidate tickets |
| `WORKSPACE_DIR` | Directory where repositories are cloned (default: `workspaces/`) |

### Run

```bash
ngn-agent
```

### Validate JIRA setup

Before running the agent for the first time, verify that your JIRA instance meets all requirements:

    ngn-agent --validate

This checks that your credentials work, the configured filter is accessible, and that the required issue types and statuses exist. It exits with code `0` if all checks pass, `1` if any fail.

**Limitation:** workflow transitions cannot be verified automatically. The check will remind you to confirm that `IN PROGRESS`, `IN REVIEW`, and `BLOCKED` transitions are defined in your project workflow — this must be done manually in JIRA.

---

## Runtime behaviour

The agent runs continuously, polling the configured JIRA filter every 30 seconds. On each poll it picks the highest-priority eligible ticket, validates it, implements the changes, and opens a pull request — then sleeps for the remainder of the 30-second interval before polling again.

**Branch resumption:** Before cloning a repository, the agent checks whether a `ngn/<ticket-key>` branch already exists on the remote. If it does, the branch is checked out and the agent resumes from the existing work rather than starting from scratch.

**PR review handling:** If a `ngn/<ticket-key>` branch already has an open pull request, the agent reads the reviewer feedback and addresses the requested changes on the existing branch and PR rather than opening a duplicate.

### JIRA assumptions

For the runtime to function correctly, the following JIRA setup assumptions must hold:

- **Statuses** — The workflow must include `READY`, `IN PROGRESS`, `IN REVIEW`, and `BLOCKED` statuses. `READY` is the entry point; the agent drives all other transitions.
- **Issue types** — Only `Bug`, `Task`, and `Story` are processed. Epics are ignored.
- **Saved filter** — `JIRA_FILTER_ID` must point to a saved filter whose results the agent further narrows with `AND issuetype in (Bug, Task, Story) AND status = READY`.
- **Transitions** — The workflow must expose `IN PROGRESS`, `IN REVIEW`, and `BLOCKED` as named transitions (matched case-insensitively).

See [JIRA configuration requirements](#jira-configuration-requirements) below for the full details.

---

## JIRA configuration requirements

The agent makes specific assumptions about how your JIRA project is set up. These must match exactly.

### Issue types

The agent only works on the following issue types. All others (including Epics) are ignored.

| Type | Notes |
|---|---|
| `Bug` | Highest priority in the work queue |
| `Task` | Sorted equally with Story, by priority then age |
| `Story` | Sorted equally with Task, by priority then age |

Epics are intentionally excluded — they are created and managed by humans.

### Statuses

| Status | Meaning |
|---|---|
| `READY` | Ticket is eligible for the agent to pick up |
| `BLOCKED` | Set by the agent when a ticket fails validation or implementation is stuck |
| `IN PROGRESS` | Set by the agent when it begins implementation |
| `IN REVIEW` | Set by the agent after a pull request is successfully created |

The status name `READY` is case-sensitive in JQL. Ensure your project's workflow uses this exact name.

### Workflow transitions

The project workflow must include the following transitions:

| Transition | From | Notes |
|---|---|---|
| `BLOCKED` | `READY` or `IN PROGRESS` | Set when validation fails or implementation is stuck |
| `IN PROGRESS` | `READY` | Set when the agent begins implementation |
| `IN REVIEW` | `IN PROGRESS` | Set when the agent opens a pull request |

The agent performs a case-insensitive match on transition names.

### Saved filter

Create a saved JIRA filter and note its numeric ID (visible in the URL when viewing the filter). The agent applies the following additional constraints on top of whatever the filter returns:

```
AND issuetype in (Bug, Task, Story) AND status = READY
```

The filter itself can contain any other constraints (project, labels, components, etc.) to narrow down which tickets the agent considers. A reasonable starting filter might be:

```
project = MYPROJECT ORDER BY created ASC
```

### Work queue priority

Within the results returned by the filter, the agent sorts tickets in this order:

1. **Issue type** — Bugs first, Tasks and Stories treated equally
2. **Priority** — Highest → High → Medium → Low → Lowest
3. **Age** — Oldest created date first (within the same priority)

---

## Ticket content requirements

Before working on a ticket, the agent validates that it contains sufficient information. A ticket that fails validation is transitioned to `Blocked` and a comment is posted explaining what is missing.

A ticket must contain all four of the following:

### 1. Repository URL
A Git URL or clear reference to the codebase where the work should be done. This can appear anywhere in the ticket — the description, a custom field, or a comment.

### 2. Current context / behaviour
A description of how things work today — the "as-is" state. This gives the agent enough background to understand what it is changing and why.

### 3. Desired outcome
A clear description of what the implementation should achieve. Include any technical requirements, constraints, or design decisions that the implementation must satisfy.

### 4. Test requirements
A description of what tests are expected, what scenarios should be covered, or how the implementation will be verified. This can be a list of test cases, a description of testing strategy, or acceptance criteria written in a testable form.

---

## Development

```bash
pytest tests/ -v      # run tests
ruff check .          # lint
ruff format .         # format
```

---

## License

Apache License 2.0. See [LICENSE](LICENSE) for the full text.
