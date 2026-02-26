"""Tests for the ngn_agent.main module.

Focus on the polling loop behaviour: that poll_once is called repeatedly and
that the agent sleeps for the remainder of each interval.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from ngn_agent.main import _POLL_INTERVAL, poll_once


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_jira(tickets=None):
    """Return a mock JiraClient that returns *tickets* from get_tickets_from_filter."""
    jira = MagicMock()
    jira.get_tickets_from_filter.return_value = tickets or []
    return jira


def _make_claude():
    return MagicMock()


# ---------------------------------------------------------------------------
# poll_once — no tickets
# ---------------------------------------------------------------------------

def test_poll_once_no_tickets_does_not_call_get_ticket(monkeypatch):
    """When the filter returns no tickets, poll_once should be a no-op."""
    monkeypatch.setenv("JIRA_FILTER_ID", "99")
    jira = _make_jira(tickets=[])
    claude = _make_claude()

    poll_once(jira, claude)

    jira.get_ticket.assert_not_called()


# ---------------------------------------------------------------------------
# Polling loop — rate limiting
# ---------------------------------------------------------------------------

def test_main_loop_sleeps_for_remaining_interval(monkeypatch):
    """The loop should sleep for (_POLL_INTERVAL - elapsed) after each iteration.

    We let two iterations run before breaking out via a side-effect on sleep.
    """
    monkeypatch.setenv("JIRA_FILTER_ID", "99")
    monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "a@b.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")

    poll_call_count = 0

    def fake_poll_once(jira, claude):
        nonlocal poll_call_count
        poll_call_count += 1
        # Stop after the second poll by raising SystemExit from within the loop.
        if poll_call_count >= 2:
            raise SystemExit(0)

    sleep_calls = []

    def fake_sleep(duration):
        sleep_calls.append(duration)

    # monotonic returns a sequence: 0.0 (loop start), 5.0 (after poll) to
    # simulate that the poll itself took 5 seconds.
    monotonic_values = iter([0.0, 5.0, 0.0, 5.0, 0.0, 5.0])

    with patch("ngn_agent.main.poll_once", side_effect=fake_poll_once), \
         patch("ngn_agent.main.time.sleep", side_effect=fake_sleep), \
         patch("ngn_agent.main.time.monotonic", side_effect=monotonic_values), \
         patch("ngn_agent.main.JiraClient"), \
         patch("ngn_agent.main.anthropic.Anthropic"):
        from ngn_agent.main import main
        with pytest.raises(SystemExit):
            main()

    # Should have slept once (after the first poll; the second raises SystemExit).
    assert len(sleep_calls) == 1
    # Elapsed was 5.0 s, so sleep should be _POLL_INTERVAL - 5.0.
    assert sleep_calls[0] == pytest.approx(_POLL_INTERVAL - 5.0)


def test_main_loop_skips_sleep_when_iteration_exceeds_interval(monkeypatch):
    """If a poll takes longer than _POLL_INTERVAL, sleep is skipped (no negative sleep)."""
    monkeypatch.setenv("JIRA_FILTER_ID", "99")
    monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "a@b.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")

    call_count = 0

    def fake_poll_once(jira, claude):
        nonlocal call_count
        call_count += 1
        if call_count >= 1:
            raise SystemExit(0)

    sleep_calls = []

    # Simulate poll taking 60 s — longer than the 30 s interval.
    monotonic_values = iter([0.0, 60.0])

    with patch("ngn_agent.main.poll_once", side_effect=fake_poll_once), \
         patch("ngn_agent.main.time.sleep", side_effect=sleep_calls.append), \
         patch("ngn_agent.main.time.monotonic", side_effect=monotonic_values), \
         patch("ngn_agent.main.JiraClient"), \
         patch("ngn_agent.main.anthropic.Anthropic"):
        from ngn_agent.main import main
        with pytest.raises(SystemExit):
            main()

    # sleep should never have been called (wait would be negative).
    assert sleep_calls == []


def test_main_loop_calls_poll_once_repeatedly(monkeypatch):
    """main() should call poll_once on every iteration of the loop."""
    monkeypatch.setenv("JIRA_FILTER_ID", "99")
    monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "a@b.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")

    call_count = 0

    def fake_poll_once(jira, claude):
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            raise SystemExit(0)

    # Enough monotonic values for 3 iterations (start + end per iteration).
    monotonic_values = iter([0.0, 0.0] * 4)

    with patch("ngn_agent.main.poll_once", side_effect=fake_poll_once), \
         patch("ngn_agent.main.time.sleep"), \
         patch("ngn_agent.main.time.monotonic", side_effect=monotonic_values), \
         patch("ngn_agent.main.JiraClient"), \
         patch("ngn_agent.main.anthropic.Anthropic"):
        from ngn_agent.main import main
        with pytest.raises(SystemExit):
            main()

    assert call_count == 3


# ---------------------------------------------------------------------------
# poll_once — invalid repo URL (clone failure)
# ---------------------------------------------------------------------------

def _make_valid_ticket(key="PROJ-1", repo_url="git@github.com:example/repo.git"):
    """Return a minimal ticket dict that looks valid for poll_once."""
    return {
        "key": key,
        "summary": "Test ticket",
        "issue_type": "Task",
        "priority": "Medium",
        "status": "READY",
        "parent": None,
        "reporter": {"account_id": "abc123", "display_name": "Test User"},
        "description": "Do something",
        "labels": [],
        "comments": [],
    }


def _make_filter_ticket(key="PROJ-1"):
    """Return a minimal ticket summary as returned by get_tickets_from_filter."""
    return {
        "key": key,
        "summary": "Test ticket",
        "issue_type": "Task",
        "priority": "Medium",
        "status": "READY",
        "created": "2024-01-01T00:00:00.000+0000",
    }


def test_poll_once_blocks_ticket_when_clone_fails(monkeypatch):
    """When clone_repo raises RuntimeError (invalid repo URL), poll_once should
    transition the ticket to BLOCKED, post a comment, and return without crashing.
    """
    monkeypatch.setenv("JIRA_FILTER_ID", "99")

    ticket = _make_valid_ticket()
    jira = _make_jira(tickets=[_make_filter_ticket()])
    jira.get_ticket.return_value = ticket

    # validate_ticket returns a valid result with a (bad) repo URL.
    fake_validation = {"valid": True, "repo_url": "git@github.com:example/does-not-exist.git", "missing": []}

    with patch("ngn_agent.main.validate_ticket", return_value=fake_validation), \
         patch("ngn_agent.main.clone_repo", side_effect=RuntimeError("git clone failed: repository not found")):
        poll_once(jira, _make_claude())

    # Ticket should have been moved to BLOCKED.
    jira.transition_ticket.assert_called_once_with(ticket["key"], "BLOCKED")
    # A comment should have been posted explaining the failure.
    jira.post_comment.assert_called_once()
    comment_lines = jira.post_comment.call_args[0][1]
    assert any("repository" in line.lower() or "cloned" in line.lower() for line in comment_lines)


def test_poll_once_does_not_proceed_to_implement_when_clone_fails(monkeypatch):
    """When clone fails, implement_ticket must NOT be called — the loop resumes."""
    monkeypatch.setenv("JIRA_FILTER_ID", "99")

    ticket = _make_valid_ticket()
    jira = _make_jira(tickets=[_make_filter_ticket()])
    jira.get_ticket.return_value = ticket

    fake_validation = {"valid": True, "repo_url": "git@github.com:example/bad.git", "missing": []}

    with patch("ngn_agent.main.validate_ticket", return_value=fake_validation), \
         patch("ngn_agent.main.clone_repo", side_effect=RuntimeError("git clone failed")), \
         patch("ngn_agent.main.implement_ticket") as mock_implement:
        poll_once(jira, _make_claude())

    mock_implement.assert_not_called()


def test_poll_once_includes_clone_error_message_in_comment(monkeypatch):
    """The BLOCKED comment should contain the underlying clone error message."""
    monkeypatch.setenv("JIRA_FILTER_ID", "99")

    ticket = _make_valid_ticket()
    jira = _make_jira(tickets=[_make_filter_ticket()])
    jira.get_ticket.return_value = ticket

    error_text = "git clone failed:\nERROR: Repository not found."
    fake_validation = {"valid": True, "repo_url": "git@github.com:example/bad.git", "missing": []}

    with patch("ngn_agent.main.validate_ticket", return_value=fake_validation), \
         patch("ngn_agent.main.clone_repo", side_effect=RuntimeError(error_text)):
        poll_once(jira, _make_claude())

    comment_lines = jira.post_comment.call_args[0][1]
    full_comment = "\n".join(comment_lines)
    assert error_text in full_comment


# ---------------------------------------------------------------------------
# Polling loop — transient network error handling
# ---------------------------------------------------------------------------

def test_main_loop_continues_after_network_error(monkeypatch):
    """The loop should continue after poll_once raises httpx.ConnectError.

    Patch poll_once to raise httpx.ConnectError on the first call and return
    normally on the second; assert the loop calls poll_once at least twice
    without raising, confirming it recovers from the transient network error.
    """
    monkeypatch.setenv("JIRA_FILTER_ID", "99")
    monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "a@b.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")

    call_count = 0

    def fake_poll_once(jira, claude):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Simulate a transient DNS / connection failure on the first attempt.
            raise httpx.ConnectError("Connection refused")
        # Second call succeeds; exit the loop via SystemExit so the test terminates.
        if call_count >= 2:
            raise SystemExit(0)

    # Enough monotonic values for two iterations (start + end each).
    monotonic_values = iter([0.0, 0.0, 0.0, 0.0])

    with patch("ngn_agent.main.poll_once", side_effect=fake_poll_once), \
         patch("ngn_agent.main.time.sleep"), \
         patch("ngn_agent.main.time.monotonic", side_effect=monotonic_values), \
         patch("ngn_agent.main.JiraClient"), \
         patch("ngn_agent.main.anthropic.Anthropic"):
        from ngn_agent.main import main
        with pytest.raises(SystemExit):
            main()

    # poll_once must have been called at least twice — once raising the error,
    # once succeeding — proving the loop recovered from the network failure.
    assert call_count >= 2


def test_main_loop_logs_warning_on_network_error(monkeypatch):
    """A warning should be logged when poll_once raises httpx.ConnectError.

    Patch poll_once to raise httpx.ConnectError once, then assert that
    log.warning was called with a message that includes the error text.
    """
    monkeypatch.setenv("JIRA_FILTER_ID", "99")
    monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "a@b.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")

    error_message = "Connection refused"
    call_count = 0

    def fake_poll_once(jira, claude):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError(error_message)
        raise SystemExit(0)

    monotonic_values = iter([0.0, 0.0, 0.0, 0.0])

    with patch("ngn_agent.main.poll_once", side_effect=fake_poll_once), \
         patch("ngn_agent.main.time.sleep"), \
         patch("ngn_agent.main.time.monotonic", side_effect=monotonic_values), \
         patch("ngn_agent.main.JiraClient"), \
         patch("ngn_agent.main.anthropic.Anthropic"), \
         patch("ngn_agent.main.log") as mock_log:
        from ngn_agent.main import main
        with pytest.raises(SystemExit):
            main()

    # Verify log.warning was called and that its formatted message contains
    # the network error text.
    mock_log.warning.assert_called_once()
    warning_args = mock_log.warning.call_args[0]
    # The first arg is the format string; combine with remainder for assertion.
    formatted = warning_args[0] % warning_args[1:]
    assert error_message in formatted
