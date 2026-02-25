import logging
import subprocess
import time
from pathlib import Path

import anthropic

log = logging.getLogger(__name__)

# Safety limits for the agentic loop.
_MAX_TURNS = 100
_TOKEN_LIMIT = 180_000  # Claude Sonnet context window is 200K; leave headroom
_MAX_RETRIES = 60
_RETRY_DELAY = 60

_SYSTEM_PROMPT = """You are an autonomous coding agent implementing a JIRA ticket. Your job is to:

1. Explore the repository to understand the codebase and its conventions.
2. Implement the required changes following the ticket specification exactly.
3. Run the project's test suite to verify your implementation.
4. Commit your changes and open a pull request.

## Coding standards

- Use idiomatic approaches for the language and frameworks found in the repository.
- Document all functions and methods: include parameter definitions and describe behaviour.
- Add inline comments to explain any logic that may not be immediately obvious to a reader.
- Match the existing code style and conventions you observe in the project.

## Testing

- After implementing, run the test suite to confirm everything passes.
- If tests fail, diagnose and fix the code — never alter existing tests to make them pass on incorrect code.
- If you cannot get tests to pass after several attempts, call report_blocked with a clear explanation.

## Git workflow

1. Create a feature branch: git checkout -b ngn/<ticket-key>
2. Implement and test your changes.
3. Stage and commit with a message that includes the ticket key, e.g. "[PROJ-42] Add login validation".
4. Push the branch: git push -u origin ngn/<ticket-key>
5. Open a PR targeting main: gh pr create --base main --title "..." --body "..."
6. Call submit_work with the PR URL and a brief summary of what was done.

## When to call report_blocked

- The ticket is ambiguous and you cannot proceed safely without clarification.
- Tests continue to fail after multiple attempts to fix the code.
- You encounter an unresolvable error or missing external dependency.
Be specific so a human can act on the reason."""

_TOOLS = [
    {
        "name": "read_file",
        "description": "Read the contents of a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to read."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file, creating it and any missing parent directories.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to write."},
                "content": {"type": "string", "description": "Content to write to the file."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_directory",
        "description": "List the contents of a directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the directory to list."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command. Use this for tests, git operations, gh CLI, and anything else that requires a shell.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run."},
                "cwd": {
                    "type": "string",
                    "description": "Working directory for the command. Defaults to the repository root.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "submit_work",
        "description": "Signal that implementation is complete. Call this after pushing the branch and creating the PR.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pr_url": {"type": "string", "description": "URL of the created pull request."},
                "summary": {"type": "string", "description": "Brief summary of what was implemented."},
            },
            "required": ["pr_url", "summary"],
        },
    },
    {
        "name": "report_blocked",
        "description": "Signal that the implementation cannot proceed. Call this when stuck on ambiguity, persistent test failures, or unresolvable errors.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Clear explanation of why the work is blocked."},
            },
            "required": ["reason"],
        },
    },
]


def implement_ticket(
    ticket: dict,
    workspace: Path,
    client: anthropic.Anthropic,
    ancestors: list[dict] | None = None,
) -> dict:
    """Run the agentic implementation loop for a single ticket.

    Args:
        ticket: Full ticket dict from JiraClient.get_ticket().
        workspace: Path to the cloned repository on disk.
        client: Anthropic API client.
        ancestors: Optional list of ancestor tickets (outermost first) for context.

    Returns:
        A dict with keys:
            success (bool): True if the agent submitted work, False otherwise.
            pr_url (str | None): Pull request URL on success.
            blocked_reason (str | None): Explanation if not successful.
    """
    messages = [{"role": "user", "content": _build_prompt(ticket, workspace, ancestors)}]

    for turn in range(_MAX_TURNS):
        log.info("Turn %d/%d...", turn + 1, _MAX_TURNS)
        try:
            response = _call_with_retry(client, messages)
        except anthropic.APIStatusError as exc:
            if exc.status_code in (429, 529):
                return _blocked("API unavailable after retries (rate limited or overloaded) — try again later")
            return _blocked(f"API error: {exc}")

        # Guard against approaching the context window limit.
        if response.usage.input_tokens >= _TOKEN_LIMIT:
            return _blocked("Context window limit reached — conversation history too long")

        messages.append({"role": "assistant", "content": response.content})

        # Check for terminal tool calls first — they end the loop immediately.
        for block in response.content:
            if block.type == "tool_use" and block.name == "submit_work":
                log.info("Agent submitted work: %s", block.input.get("summary"))
                return {"success": True, "pr_url": block.input.get("pr_url"), "blocked_reason": None}
            if block.type == "tool_use" and block.name == "report_blocked":
                log.warning("Agent reported blocked: %s", block.input.get("reason"))
                return _blocked(block.input.get("reason", "No reason provided"))

        # If the model stopped without any tool call, it finished unexpectedly.
        if response.stop_reason == "end_turn":
            return _blocked("Agent stopped without submitting or reporting blocked")

        # Execute all non-terminal tool calls and collect results.
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result_text = _dispatch(block.name, block.input, workspace)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })

        messages.append({"role": "user", "content": tool_results})

    return _blocked(f"Exceeded maximum turn limit ({_MAX_TURNS})")


def _call_with_retry(client: anthropic.Anthropic, messages: list) -> anthropic.types.Message:
    """Call the Claude API, retrying with exponential backoff on rate limit errors.

    Args:
        client: Anthropic API client.
        messages: Conversation history to send.

    Returns:
        The API response message.

    Raises:
        anthropic.APIStatusError: If all retries are exhausted on a 429/529 response.
    """
    last_exc = None
    for attempt in range(_MAX_RETRIES):
        try:
            return client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=8192,
                system=_SYSTEM_PROMPT,
                tools=_TOOLS,
                messages=messages,
            )
        except anthropic.APIStatusError as exc:
            if exc.status_code not in (429, 500, 529):
                raise
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                log.warning("API unavailable (%s), will retry in %ss (attempt %s/%s)...", exc.status_code, _RETRY_DELAY, attempt + 1, _MAX_RETRIES)
                time.sleep(_RETRY_DELAY)
    raise last_exc


def _dispatch(name: str, inputs: dict, workspace: Path) -> str:
    """Route a tool call to its implementation.

    Args:
        name: Tool name as declared in _TOOLS.
        inputs: Tool input arguments from the model.
        workspace: Repository root, used as the default cwd for run_command.

    Returns:
        String result to return to the model as a tool result.
    """
    if name == "read_file":
        log.info("  read_file:      %s", inputs["path"])
        return _read_file(inputs["path"])
    if name == "write_file":
        log.info("  write_file:     %s", inputs["path"])
        return _write_file(inputs["path"], inputs["content"])
    if name == "list_directory":
        log.info("  list_directory: %s", inputs["path"])
        return _list_directory(inputs["path"])
    if name == "run_command":
        log.info("  run_command:    %s", inputs["command"])
        return _run_command(inputs["command"], inputs.get("cwd") or str(workspace))
    return f"Unknown tool: {name}"


def _read_file(path: str) -> str:
    """Read and return the contents of a file.

    Args:
        path: Path to the file to read.

    Returns:
        File contents as a string, or an error message.
    """
    try:
        return Path(path).read_text()
    except Exception as exc:
        return f"Error reading {path}: {exc}"


def _write_file(path: str, content: str) -> str:
    """Write content to a file, creating parent directories if needed.

    Args:
        path: Path to the file to write.
        content: Content to write.

    Returns:
        Confirmation message, or an error message.
    """
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as exc:
        return f"Error writing {path}: {exc}"


def _list_directory(path: str) -> str:
    """List directory contents, directories first then files, both sorted.

    Args:
        path: Path to the directory.

    Returns:
        Newline-separated list of entries prefixed with 'd' or 'f', or an error message.
    """
    try:
        entries = sorted(Path(path).iterdir(), key=lambda e: (e.is_file(), e.name))
        lines = [f"{'d' if e.is_dir() else 'f'}  {e.name}" for e in entries]
        return "\n".join(lines) or "(empty)"
    except Exception as exc:
        return f"Error listing {path}: {exc}"


def _run_command(command: str, cwd: str) -> str:
    """Run a shell command and return its combined stdout and stderr.

    Args:
        command: Shell command string to execute.
        cwd: Working directory for the command.

    Returns:
        Combined output string. Includes exit code on non-zero exit, or an error message.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=120,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            output += f"\n[exit code {result.returncode}]"
        return output.strip() or "[no output]"
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 120 seconds"
    except Exception as exc:
        return f"Error running command: {exc}"


def _blocked(reason: str) -> dict:
    """Construct a blocked result dict.

    Args:
        reason: Human-readable explanation of why the work is blocked.
    """
    return {"success": False, "pr_url": None, "blocked_reason": reason}


def _build_prompt(ticket: dict, workspace: Path, ancestors: list[dict] | None) -> str:
    """Build the initial user message for the implementation agent.

    Args:
        ticket: The ticket to implement.
        workspace: Path to the cloned repository.
        ancestors: Optional ancestor tickets for context, outermost first.

    Returns:
        Formatted prompt string.
    """
    parts = []
    if ancestors:
        ancestor_sections = "\n\n---\n\n".join(_format_ticket(a) for a in ancestors)
        parts.append(f"Background context (do NOT implement these — for reference only):\n\n{ancestor_sections}\n\n---")
    parts.append(f"Ticket to implement (implement THIS ticket only):\n{_format_ticket(ticket)}")
    parts.append(f"Repository location: {workspace}")
    parts.append("Begin by exploring the repository structure, then implement the ticket.")
    return "\n\n".join(parts)


def _format_ticket(ticket: dict) -> str:
    """Format a ticket dict as plain text for inclusion in a prompt.

    Args:
        ticket: Ticket dict as returned by JiraClient.get_ticket() or similar.

    Returns:
        Multi-line string representation of the ticket.
    """
    lines = [
        f"Key: {ticket['key']}",
        f"Type: {ticket['issue_type']}",
        f"Summary: {ticket['summary']}",
        f"Priority: {ticket.get('priority') or 'none'}",
    ]
    if ticket.get("parent"):
        lines.append(f"Parent: {ticket['parent']['key']} — {ticket['parent']['summary']}")
    if ticket.get("labels"):
        lines.append(f"Labels: {', '.join(ticket['labels'])}")
    lines.append(f"\nDescription:\n{ticket.get('description') or '(none)'}")
    if ticket.get("comments"):
        lines.append("\nComments:")
        for c in ticket["comments"]:
            lines.append(f"  [{c['created']}] {c['author']}:\n  {c['body']}")
    return "\n".join(lines)
