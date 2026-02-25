from unittest.mock import MagicMock, patch

import anthropic
import pytest

from ngn_agent.validator import _format_ticket, validate_ticket


def _make_tool_response(valid=True, missing=None, repo_url="https://github.com/example/repo"):
    block = MagicMock()
    block.type = "tool_use"
    block.name = "submit_validation"
    block.input = {"valid": valid, "missing": missing or [], "repo_url": repo_url}
    response = MagicMock()
    response.content = [block]
    return response


def _make_ticket(key="PROJ-1", description="Do the thing.", comments=None, parent=None):
    return {
        "key": key,
        "issue_type": "Task",
        "summary": f"Summary for {key}",
        "priority": "High",
        "labels": [],
        "parent": parent,
        "description": description,
        "comments": comments or [],
    }


# --- _format_ticket ---

def test_format_ticket_includes_key_and_summary():
    text = _format_ticket(_make_ticket())
    assert "PROJ-1" in text
    assert "Summary for PROJ-1" in text


def test_format_ticket_includes_description():
    text = _format_ticket(_make_ticket(description="Fix the login bug."))
    assert "Fix the login bug." in text


def test_format_ticket_includes_comments():
    ticket = _make_ticket(comments=[{"author": "Alice", "created": "2024-01-01T00:00:00.000+0000", "body": "See PR #42"}])
    text = _format_ticket(ticket)
    assert "Alice" in text
    assert "See PR #42" in text


def test_format_ticket_includes_parent_reference():
    ticket = _make_ticket(parent={"key": "EPIC-1", "summary": "The epic"})
    text = _format_ticket(ticket)
    assert "EPIC-1" in text
    assert "The epic" in text


# --- validate_ticket ---

def _make_api_status_error(status_code):
    response = MagicMock()
    response.status_code = status_code
    exc = anthropic.APIStatusError(message="error", response=response, body={})
    exc.status_code = status_code
    return exc


def test_validate_ticket_retries_on_overloaded():
    good_response = _make_tool_response(valid=True, missing=[])
    client = MagicMock()
    client.messages.create.side_effect = [
        _make_api_status_error(529),
        good_response,
    ]
    with patch("ngn_agent.validator.time.sleep"):
        result = validate_ticket(_make_ticket(), client)
    assert result["valid"] is True
    assert client.messages.create.call_count == 2


def test_validate_ticket_raises_after_max_retries():
    client = MagicMock()
    client.messages.create.side_effect = _make_api_status_error(529)
    with patch("ngn_agent.validator.time.sleep"):
        with pytest.raises(anthropic.APIStatusError):
            validate_ticket(_make_ticket(), client)


def test_validate_ticket_returns_tool_result():
    client = MagicMock()
    client.messages.create.return_value = _make_tool_response(valid=True, missing=[])
    result = validate_ticket(_make_ticket(), client)
    assert result["valid"] is True
    assert result["missing"] == []


def test_validate_ticket_no_ancestors_sends_ticket_only():
    client = MagicMock()
    client.messages.create.return_value = _make_tool_response()
    validate_ticket(_make_ticket(description="Child only"), client)
    content = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "Child only" in content
    assert "Ancestor ticket context" not in content


def test_validate_ticket_with_ancestor_prepends_content():
    client = MagicMock()
    client.messages.create.return_value = _make_tool_response()
    parent = _make_ticket(key="EPIC-1", description="Repo is github.com/example/repo")
    child = _make_ticket(key="PROJ-1", description="Implement the feature")
    validate_ticket(child, client, ancestors=[parent])
    content = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "Ancestor ticket context" in content
    assert "Repo is github.com/example/repo" in content
    assert "Implement the feature" in content


def test_validate_ticket_ancestor_appears_before_child():
    client = MagicMock()
    client.messages.create.return_value = _make_tool_response()
    parent = _make_ticket(key="EPIC-1", description="PARENT_MARKER")
    child = _make_ticket(key="PROJ-1", description="CHILD_MARKER")
    validate_ticket(child, client, ancestors=[parent])
    content = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert content.index("PARENT_MARKER") < content.index("CHILD_MARKER")


def test_validate_ticket_multiple_ancestors_ordered_outermost_first():
    client = MagicMock()
    client.messages.create.return_value = _make_tool_response()
    grandparent = _make_ticket(key="EPIC-1", description="GRANDPARENT_MARKER")
    parent = _make_ticket(key="STORY-1", description="PARENT_MARKER")
    child = _make_ticket(key="TASK-1", description="CHILD_MARKER")
    validate_ticket(child, client, ancestors=[grandparent, parent])
    content = client.messages.create.call_args.kwargs["messages"][0]["content"]
    gp_pos = content.index("GRANDPARENT_MARKER")
    p_pos = content.index("PARENT_MARKER")
    c_pos = content.index("CHILD_MARKER")
    assert gp_pos < p_pos < c_pos
