import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import anthropic

from ngn_agent.coder import (
    _build_prompt,
    _dispatch,
    _list_directory,
    _read_file,
    _run_command,
    _write_file,
    implement_ticket,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ticket(key="PROJ-1", description="Do the thing."):
    return {
        "key": key,
        "issue_type": "Task",
        "summary": f"Summary for {key}",
        "priority": "High",
        "labels": [],
        "parent": None,
        "description": description,
        "comments": [],
    }


def _tool_use_block(name, input_, id_="tu_1"):
    """Build a mock tool_use content block."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.input = input_
    block.id = id_
    return block


def _text_block(text):
    """Build a mock text content block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_response(blocks, stop_reason="tool_use", input_tokens=100):
    """Build a mock Claude API response."""
    response = MagicMock()
    response.content = blocks
    response.stop_reason = stop_reason
    response.usage = MagicMock()
    response.usage.input_tokens = input_tokens
    return response


def _make_client(*responses):
    """Build a mock Anthropic client that returns responses in sequence."""
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.side_effect = list(responses)
    return client


# ---------------------------------------------------------------------------
# _read_file
# ---------------------------------------------------------------------------

def test_read_file_returns_contents(tmp_path):
    f = tmp_path / "hello.txt"
    f.write_text("hello world")
    assert _read_file(str(f)) == "hello world"


def test_read_file_missing_returns_error():
    result = _read_file("/nonexistent/path/file.txt")
    assert result.startswith("Error reading")


# ---------------------------------------------------------------------------
# _write_file
# ---------------------------------------------------------------------------

def test_write_file_creates_file(tmp_path):
    path = str(tmp_path / "out.txt")
    result = _write_file(path, "content")
    assert "Wrote" in result
    assert Path(path).read_text() == "content"


def test_write_file_creates_parent_dirs(tmp_path):
    path = str(tmp_path / "a" / "b" / "c.txt")
    _write_file(path, "deep")
    assert Path(path).read_text() == "deep"


def test_write_file_error_returns_message(tmp_path):
    # Write to a path that is actually a directory
    result = _write_file(str(tmp_path), "oops")
    assert result.startswith("Error writing")


# ---------------------------------------------------------------------------
# _list_directory
# ---------------------------------------------------------------------------

def test_list_directory_shows_entries(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("")
    result = _list_directory(str(tmp_path))
    assert "d  src" in result
    assert "f  README.md" in result


def test_list_directory_dirs_before_files(tmp_path):
    (tmp_path / "z_dir").mkdir()
    (tmp_path / "a_file.txt").write_text("")
    result = _list_directory(str(tmp_path))
    assert result.index("d  z_dir") < result.index("f  a_file.txt")


def test_list_directory_missing_returns_error():
    result = _list_directory("/nonexistent/dir")
    assert result.startswith("Error listing")


def test_list_directory_empty(tmp_path):
    assert _list_directory(str(tmp_path)) == "(empty)"


# ---------------------------------------------------------------------------
# _run_command
# ---------------------------------------------------------------------------

def test_run_command_returns_stdout(tmp_path):
    result = _run_command("echo hello", str(tmp_path))
    assert "hello" in result


def test_run_command_nonzero_exit_includes_code(tmp_path):
    result = _run_command("exit 42", str(tmp_path))
    assert "exit code 42" in result


def test_run_command_timeout_returns_error(tmp_path):
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("sleep", 120)):
        result = _run_command("sleep 999", str(tmp_path))
    assert "timed out" in result


# ---------------------------------------------------------------------------
# _dispatch
# ---------------------------------------------------------------------------

def test_dispatch_read_file(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hi")
    result = _dispatch("read_file", {"path": str(f)}, tmp_path)
    assert result == "hi"


def test_dispatch_unknown_tool(tmp_path):
    result = _dispatch("nonexistent_tool", {}, tmp_path)
    assert "Unknown tool" in result


# ---------------------------------------------------------------------------
# implement_ticket — agentic loop
# ---------------------------------------------------------------------------

def test_implement_ticket_submit_work_returns_success(tmp_path):
    submit = _tool_use_block("submit_work", {"pr_url": "https://github.com/x/y/pull/1", "summary": "done"})
    client = _make_client(_make_response([submit]))

    result = implement_ticket(_make_ticket(), tmp_path, client)

    assert result["success"] is True
    assert result["pr_url"] == "https://github.com/x/y/pull/1"
    assert result["blocked_reason"] is None


def test_implement_ticket_report_blocked_returns_failure(tmp_path):
    blocked = _tool_use_block("report_blocked", {"reason": "too ambiguous"})
    client = _make_client(_make_response([blocked]))

    result = implement_ticket(_make_ticket(), tmp_path, client)

    assert result["success"] is False
    assert result["blocked_reason"] == "too ambiguous"
    assert result["pr_url"] is None


def test_implement_ticket_end_turn_without_tool_returns_blocked(tmp_path):
    client = _make_client(_make_response([], stop_reason="end_turn"))

    result = implement_ticket(_make_ticket(), tmp_path, client)

    assert result["success"] is False
    assert "stopped" in result["blocked_reason"]


def test_implement_ticket_token_limit_returns_blocked(tmp_path):
    submit = _tool_use_block("submit_work", {"pr_url": "http://pr", "summary": "done"})
    client = _make_client(_make_response([submit], input_tokens=180_001))

    result = implement_ticket(_make_ticket(), tmp_path, client)

    assert result["success"] is False
    assert "Context window" in result["blocked_reason"]


def test_implement_ticket_executes_tool_calls_before_terminal(tmp_path):
    """Agent calls list_directory in turn 1, then submit_work in turn 2."""
    list_block = _tool_use_block("list_directory", {"path": str(tmp_path)}, id_="tu_1")
    submit_block = _tool_use_block("submit_work", {"pr_url": "http://pr", "summary": "done"}, id_="tu_2")

    client = _make_client(
        _make_response([list_block]),
        _make_response([submit_block]),
    )

    result = implement_ticket(_make_ticket(), tmp_path, client)

    assert result["success"] is True
    assert client.messages.create.call_count == 2


def test_implement_ticket_max_turns_returns_blocked(tmp_path):
    # Each response is a non-terminal tool call; after _MAX_TURNS the loop exits.
    list_block = _tool_use_block("list_directory", {"path": str(tmp_path)})
    responses = [_make_response([list_block])] * 101  # more than _MAX_TURNS
    client = _make_client(*responses)

    result = implement_ticket(_make_ticket(), tmp_path, client)

    assert result["success"] is False
    assert "maximum turn limit" in result["blocked_reason"]


def _make_api_status_error(status_code):
    """Build a mock APIStatusError with the given HTTP status code."""
    response = MagicMock()
    response.status_code = status_code
    exc = anthropic.APIStatusError(message="error", response=response, body={})
    exc.status_code = status_code
    return exc


def test_implement_ticket_rate_limit_returns_blocked(tmp_path):
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.side_effect = _make_api_status_error(429)

    with patch("ngn_agent.coder.time.sleep"):  # don't actually wait
        result = implement_ticket(_make_ticket(), tmp_path, client)

    assert result["success"] is False
    assert "unavailable" in result["blocked_reason"].lower()


def test_implement_ticket_overloaded_returns_blocked(tmp_path):
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.side_effect = _make_api_status_error(529)

    with patch("ngn_agent.coder.time.sleep"):
        result = implement_ticket(_make_ticket(), tmp_path, client)

    assert result["success"] is False
    assert "unavailable" in result["blocked_reason"].lower()


def test_implement_ticket_api_error_returns_blocked(tmp_path):
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.side_effect = _make_api_status_error(500)

    with patch("ngn_agent.coder.time.sleep"):  # prevent 60×60s retry sleeps
        result = implement_ticket(_make_ticket(), tmp_path, client)

    assert result["success"] is False
    assert "API error" in result["blocked_reason"]


# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------

def test_build_prompt_includes_ticket_and_workspace(tmp_path):
    prompt = _build_prompt(_make_ticket(description="Fix the bug"), tmp_path, None)
    assert "Fix the bug" in prompt
    assert str(tmp_path) in prompt


def test_build_prompt_no_ancestors_omits_header(tmp_path):
    prompt = _build_prompt(_make_ticket(), tmp_path, None)
    assert "Background context" not in prompt


def test_build_prompt_with_ancestors_includes_header_and_content(tmp_path):
    ancestor = _make_ticket(key="EPIC-1", description="EPIC_CONTENT")
    prompt = _build_prompt(_make_ticket(description="CHILD_CONTENT"), tmp_path, [ancestor])
    assert "Background context" in prompt
    assert "do NOT implement" in prompt
    assert "implement THIS ticket only" in prompt
    assert "EPIC_CONTENT" in prompt
    assert "CHILD_CONTENT" in prompt
    assert prompt.index("EPIC_CONTENT") < prompt.index("CHILD_CONTENT")


def test_build_prompt_with_resume_branch_includes_resume_section(tmp_path):
    """Prompt contains the resume branch name and 'Resuming prior attempt' notice."""
    prompt = _build_prompt(_make_ticket(), tmp_path, None, resume_branch="ngn/PROJ-1")
    assert "Resuming prior attempt" in prompt
    assert "ngn/PROJ-1" in prompt


def test_build_prompt_without_resume_branch_omits_resume_section(tmp_path):
    """Prompt does not contain 'Resuming prior attempt' when no resume branch is given."""
    prompt = _build_prompt(_make_ticket(), tmp_path, None)
    assert "Resuming prior attempt" not in prompt


# ---------------------------------------------------------------------------
# implement_ticket — agent text block logging
# ---------------------------------------------------------------------------

def test_implement_ticket_logs_agent_text_blocks(tmp_path):
    """A text block preceding a tool-use block is logged via log.info."""
    reasoning = "I'll explore the top-level directory to understand the project layout."
    text = _text_block(reasoning)
    submit = _tool_use_block("submit_work", {"pr_url": "http://pr", "summary": "done"})
    client = _make_client(_make_response([text, submit]))

    with patch("ngn_agent.coder.log") as mock_log:
        implement_ticket(_make_ticket(), tmp_path, client)

    # Collect all positional arg strings from info calls and verify the
    # reasoning text appears in at least one of them.
    info_calls = mock_log.info.call_args_list
    logged_messages = [str(c) for c in info_calls]
    assert any(reasoning in msg for msg in logged_messages)


def test_implement_ticket_ignores_empty_text_blocks(tmp_path):
    """A text block containing only whitespace must not produce a log.info call."""
    whitespace_text = _text_block("   \n\t  ")
    submit = _tool_use_block("submit_work", {"pr_url": "http://pr", "summary": "done"})
    client = _make_client(_make_response([whitespace_text, submit]))

    with patch("ngn_agent.coder.log") as mock_log:
        implement_ticket(_make_ticket(), tmp_path, client)

    # None of the info calls should contain "Agent: " prefixed reasoning text
    # (the whitespace block should have been silently skipped).
    info_calls = mock_log.info.call_args_list
    agent_calls = [c for c in info_calls if c.args and "Agent: " in str(c.args[0])]
    assert len(agent_calls) == 0


def test_implement_ticket_no_text_block_does_not_error(tmp_path):
    """A response with only tool-use blocks and no text block completes without error."""
    submit = _tool_use_block("submit_work", {"pr_url": "http://pr", "summary": "done"})
    client = _make_client(_make_response([submit]))

    # Should not raise; success result expected.
    result = implement_ticket(_make_ticket(), tmp_path, client)

    assert result["success"] is True


def test_implement_ticket_passes_resume_branch_to_prompt(tmp_path):
    """When resume_branch is provided, the initial message content contains the branch name."""
    submit = _tool_use_block("submit_work", {"pr_url": "http://pr", "summary": "done"})
    client = _make_client(_make_response([submit]))

    implement_ticket(_make_ticket(), tmp_path, client, resume_branch="ngn/PROJ-1")

    # The first call to messages.create carries the initial messages list whose
    # first element is the user turn containing the prompt.
    first_call_kwargs = client.messages.create.call_args_list[0]
    messages_arg = first_call_kwargs.kwargs.get("messages") or first_call_kwargs.args[0]
    initial_user_content = messages_arg[0]["content"]
    assert "ngn/PROJ-1" in initial_user_content
    assert "Resuming prior attempt" in initial_user_content
